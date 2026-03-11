"""
behavioral_analyzer.py — Trading pattern analysis

Answers the key questions:
  1. Exit before market resolution? (flip/profit-taking vs conviction)
  2. Swing trader or resolution holder?
  3. Entry price vs current price (unrealized P&L)
  4. How fast did they exit after entry?
  5. Copy trade risk: max loss calculation
  6. Historical win-rate for this wallet
"""

import time
import logging
from dataclasses import dataclass, field
from typing import Optional

from . import config

log = logging.getLogger(__name__)

SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY  = 86400


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TradeAnalysis:
    """Full analysis for a single detected trade event."""

    # ── Trade basics ──────────────────────────────────────────────────────────
    wallet:        str
    wallet_label:  str
    side:          str      # BUY | SELL
    outcome:       str      # Yes | No
    price:         float
    size_usdc:     float
    shares:        float
    token_id:      str
    tx_hash:       str
    trade_ts:      int

    # ── Market context ────────────────────────────────────────────────────────
    market_question:  str  = ""
    market_url:       str  = ""
    market_end_ts:    int  = 0
    current_price:    float = 0.0

    # ── Position context ──────────────────────────────────────────────────────
    avg_entry_price:  float = 0.0
    total_shares:     float = 0.0
    first_buy_ts:     int   = 0
    position_status:  str   = "OPEN"   # OPEN | CLOSED

    # ── Computed insights ─────────────────────────────────────────────────────
    unrealized_pnl_usdc:   float = 0.0
    unrealized_pnl_pct:    float = 0.0
    hold_duration_seconds: int   = 0   # 0 for fresh BUY
    is_early_exit:         bool  = False
    days_to_resolution:    Optional[float] = None
    trade_style:           str   = ""   # "swing", "holding", "new_position"
    exit_speed_label:      str   = ""   # e.g. "2h 14m after entry"

    # ── Copy trade risk ───────────────────────────────────────────────────────
    copy_max_loss_usdc:    float = 0.0
    copy_max_loss_pct:     float = 0.0
    copy_size_suggested:   float = 0.0
    copy_risk_label:       str   = ""

    # ── Flags ─────────────────────────────────────────────────────────────────
    is_exit_alert:  bool = False   # True when we detect an insider selling


@dataclass
class WalletStats:
    """Aggregated behavioural stats across all tracked positions for a wallet."""
    total_closed:    int   = 0
    profitable:      int   = 0
    win_rate_pct:    float = 0.0
    avg_hold_hours:  float = 0.0
    early_exits:     int   = 0    # closed before resolution
    style_label:     str   = ""   # "Swing Trader" | "Conviction Holder" | "Mixed"


# ─────────────────────────────────────────────────────────────────────────────
# Main analysis function
# ─────────────────────────────────────────────────────────────────────────────

def analyse_trade(
    trade:    dict,
    position: Optional[dict],
    market:   Optional[dict],
    wallet_label: str,
    all_positions: dict,
) -> TradeAnalysis:
    """
    Build a full TradeAnalysis for a newly detected trade.

    Args:
        trade:         Normalised trade dict from polymarket_client
        position:      Updated position dict from state_manager (post-trade)
        market:        Market metadata dict from polymarket_client
        wallet_label:  Human-readable label for the wallet
        all_positions: All positions for this wallet (for stats)
    """
    now_ts = int(time.time())
    side   = trade.get("side", "").upper()

    a = TradeAnalysis(
        wallet        = trade.get("wallet", ""),
        wallet_label  = wallet_label,
        side          = side,
        outcome       = trade.get("outcome", ""),
        price         = float(trade.get("price", 0)),
        size_usdc     = float(trade.get("size_usdc", 0)),
        shares        = float(trade.get("shares", 0)),
        token_id      = trade.get("token_id", ""),
        tx_hash       = trade.get("tx_hash", ""),
        trade_ts      = int(trade.get("match_time", 0)),
    )

    # ── Market context ────────────────────────────────────────────────────────
    if market:
        a.market_question = market.get("question", "Unknown Market")
        a.market_url      = market.get("url", "")
        a.market_end_ts   = int(market.get("end_ts", 0))
        a.current_price   = float(market.get("current_price", 0))

        if a.market_end_ts > 0:
            secs_to_end              = a.market_end_ts - now_ts
            a.days_to_resolution     = round(secs_to_end / SECONDS_PER_DAY, 1)

    # ── Position context ──────────────────────────────────────────────────────
    if position:
        a.avg_entry_price = float(position.get("avg_entry", 0))
        a.total_shares    = float(position.get("shares", 0))
        a.first_buy_ts    = int(position.get("first_buy_ts", 0))
        a.position_status = position.get("status", "OPEN")

    # ── Hold duration ─────────────────────────────────────────────────────────
    if a.first_buy_ts and a.first_buy_ts < a.trade_ts:
        a.hold_duration_seconds = a.trade_ts - a.first_buy_ts

    # ── Unrealized P&L (only meaningful for OPEN positions after BUY) ─────────
    if side == "BUY" and a.current_price > 0 and a.avg_entry_price > 0:
        price_delta          = a.current_price - a.avg_entry_price
        a.unrealized_pnl_usdc = round(price_delta * a.total_shares, 2)
        if a.avg_entry_price > 0:
            a.unrealized_pnl_pct = round(price_delta / a.avg_entry_price * 100, 1)

    # ── Trading style ─────────────────────────────────────────────────────────
    if side == "BUY" and a.first_buy_ts == a.trade_ts:
        a.trade_style = "new_position"
    elif side == "SELL":
        hold_hours = a.hold_duration_seconds / SECONDS_PER_HOUR
        if hold_hours < config.SWING_TRADE_HOURS:
            a.trade_style = "swing"
        else:
            a.trade_style = "holding"

        # Exit speed label
        a.exit_speed_label = _format_duration(a.hold_duration_seconds) + " after entry"
        a.is_exit_alert    = True

    # ── Early exit check ──────────────────────────────────────────────────────
    if side == "SELL" and a.days_to_resolution is not None:
        if a.days_to_resolution > config.EARLY_EXIT_DAYS:
            a.is_early_exit = True

    # ── Copy trade risk ───────────────────────────────────────────────────────
    if side == "BUY":
        base = config.MY_BASE_COPY_SIZE_USDC
        # Max loss = amount spent if price goes to 0
        a.copy_max_loss_usdc  = round(base * a.price, 2)   # you buy at this price
        a.copy_max_loss_pct   = round(a.price * 100, 1)    # e.g. 73% of position lost if → 0
        # Suggested size scales inversely with price (higher price = less upside)
        risk_factor           = max(0.25, 1.0 - a.price)   # 0.25..0.75
        a.copy_size_suggested = round(base * risk_factor, 2)
        a.copy_risk_label     = _copy_risk_label(a.price)

    return a


# ─────────────────────────────────────────────────────────────────────────────
# Wallet-level stats (computed from state positions)
# ─────────────────────────────────────────────────────────────────────────────

def compute_wallet_stats(all_positions: dict) -> WalletStats:
    """
    Aggregate behavioural stats from a wallet's full position history.
    all_positions: dict of {token_id: position_dict}
    """
    stats = WalletStats()
    hold_durations = []

    for pos in all_positions.values():
        if pos.get("status") != "CLOSED":
            continue
        stats.total_closed += 1

        entry = float(pos.get("avg_entry", 0))
        exit_ = float(pos.get("exit_price") or 0)
        if exit_ > entry:
            stats.profitable += 1

        first = int(pos.get("first_buy_ts", 0))
        exit_ts = int(pos.get("exit_ts") or 0)
        if first and exit_ts and exit_ts > first:
            hold_durations.append((exit_ts - first) / SECONDS_PER_HOUR)

        if pos.get("exit_ts") and pos.get("market_end_ts", 0):
            days_before = (pos["market_end_ts"] - pos["exit_ts"]) / SECONDS_PER_DAY
            if days_before > config.EARLY_EXIT_DAYS:
                stats.early_exits += 1

    if stats.total_closed > 0:
        stats.win_rate_pct = round(stats.profitable / stats.total_closed * 100, 1)

    if hold_durations:
        stats.avg_hold_hours = round(sum(hold_durations) / len(hold_durations), 1)

    # Style label
    if stats.total_closed < 3:
        stats.style_label = "Insufficient data"
    elif stats.avg_hold_hours < config.SWING_TRADE_HOURS:
        stats.style_label = "⚡ Swing Trader"
    elif stats.early_exits / max(stats.total_closed, 1) > 0.6:
        stats.style_label = "🎯 Profit Flipper"
    else:
        stats.style_label = "🏔 Conviction Holder"

    return stats


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "unknown time"
    days  = seconds // SECONDS_PER_DAY
    hours = (seconds % SECONDS_PER_DAY) // SECONDS_PER_HOUR
    mins  = (seconds % SECONDS_PER_HOUR) // 60
    parts = []
    if days:  parts.append(f"{days}d")
    if hours: parts.append(f"{hours}h")
    if mins:  parts.append(f"{mins}m")
    return " ".join(parts) if parts else "<1m"


def _copy_risk_label(price: float) -> str:
    if price >= 0.80:
        return "🔴 HIGH RISK — already priced in"
    elif price >= 0.55:
        return "🟡 MEDIUM RISK — moderate upside"
    elif price >= 0.30:
        return "🟢 LOWER RISK — good upside if correct"
    else:
        return "⚪ SPECULATIVE — high upside, likely losing"
