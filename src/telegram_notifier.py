"""
telegram_notifier.py — Alert formatting and Telegram delivery

Produces richly formatted messages for:
  - New BUY detected
  - SELL / position close detected  
  - Exit alert (insider selling while you might be copying)
  - Error / system alerts
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional
import requests

from . import config
from .behavioral_analyzer import TradeAnalysis, WalletStats

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_MSG_LEN  = 4096   # Telegram hard limit


def send_trade_alert(
    analysis:      TradeAnalysis,
    wallet_stats:  Optional[WalletStats] = None,
) -> bool:
    """
    Format and send a Telegram alert for a detected trade.
    Returns True on success.
    """
    if analysis.side == "BUY":
        msg = _format_buy_alert(analysis, wallet_stats)
    else:
        msg = _format_sell_alert(analysis, wallet_stats)

    return _send(msg)


def send_exit_warning(
    analysis:     TradeAnalysis,
    my_position:  Optional[dict] = None,
) -> bool:
    """
    Alert when an insider you're copying starts to EXIT.
    Includes your copy position P&L.
    """
    msg = _format_exit_warning(analysis, my_position)
    return _send(msg)


def send_error_alert(message: str) -> bool:
    msg = f"⚠️ *Monitor Error*\n\n`{_escape(message)}`"
    return _send(msg)


def send_startup_message(wallet_count: int) -> bool:
    msg = (
        f"✅ *Polymarket Monitor Started*\n\n"
        f"Watching *{wallet_count}* wallet(s)\n"
        f"Polling every ~5 minutes via GitHub Actions\n\n"
        f"_You'll receive alerts for every new BUY or SELL._"
    )
    return _send(msg)


# ─────────────────────────────────────────────────────────────────────────────
# Message formatters
# ─────────────────────────────────────────────────────────────────────────────

def _format_buy_alert(a: TradeAnalysis, stats: Optional[WalletStats]) -> str:
    ts_str    = _ts_to_utc(a.trade_ts)
    price_pct = round(a.price * 100, 1)

    # Outcome token direction label
    direction = (
        f"{a.outcome} @ {price_pct}¢"
        if a.outcome
        else f"@ {price_pct}¢"
    )

    # P&L context if current price differs from entry
    pnl_line = ""
    if a.current_price > 0 and abs(a.current_price - a.price) > 0.005:
        cp = round(a.current_price * 100, 1)
        diff = round((a.current_price - a.price) * 100, 1)
        sign = "+" if diff >= 0 else ""
        pnl_line = f"  💹 *Current price:* {cp}¢  ({sign}{diff}¢ since buy)\n"

    # Resolution line
    res_line = ""
    if a.days_to_resolution is not None:
        if a.days_to_resolution < 0:
            res_line = "  ⏰ *Resolves:* Market already resolved!\n"
        elif a.days_to_resolution < 1:
            res_line = "  ⏰ *Resolves:* <24 hours ⚡\n"
        else:
            res_line = f"  ⏰ *Resolves in:* {a.days_to_resolution} days\n"

    # Stats block
    stats_block = _stats_block(stats) if stats else ""

    # Copy trade block
    copy_block = (
        f"\n💼 *COPY TRADE ANALYSIS*\n"
        f"  Size to copy: `${a.copy_size_suggested}` USDC\n"
        f"  Max loss on `${ config.MY_BASE_COPY_SIZE_USDC}` position: `${a.copy_max_loss_usdc}`\n"
        f"  Risk level: {a.copy_risk_label}\n"
    )

    tx_line = f"  🔗 [Tx](https://polygonscan.com/tx/{a.tx_hash})" if a.tx_hash else ""
    mkt_line = f"  📊 [Market]({a.market_url})" if a.market_url else ""
    links_line = "  " + "  |  ".join(filter(None, [tx_line, mkt_line])) if (tx_line or mkt_line) else ""

    msg = (
        f"🟢 *NEW BUY DETECTED*\n\n"
        f"👤 *Wallet:* `{_short(a.wallet)}`  _{a.wallet_label}_\n"
        f"📋 *Market:* {_escape(a.market_question)}\n\n"
        f"  💰 *Direction:* {direction}\n"
        f"  📦 *Size:* ${a.size_usdc:,.2f} USDC ({a.shares:,.0f} shares)\n"
        f"{pnl_line}"
        f"{res_line}"
        f"  🕐 *Time:* {ts_str} UTC\n"
        f"{links_line}\n"
        f"{stats_block}"
        f"{copy_block}"
    )
    return msg.strip()


def _format_sell_alert(a: TradeAnalysis, stats: Optional[WalletStats]) -> str:
    ts_str    = _ts_to_utc(a.trade_ts)
    price_pct = round(a.price * 100, 1)

    # P&L on exit
    entry_pct   = round(a.avg_entry_price * 100, 1) if a.avg_entry_price else None
    pnl_usdc    = round((a.price - a.avg_entry_price) * a.shares, 2) if a.avg_entry_price else 0
    pnl_pct     = round((a.price - a.avg_entry_price) / a.avg_entry_price * 100, 1) if a.avg_entry_price else 0
    pnl_sign    = "+" if pnl_usdc >= 0 else ""
    pnl_emoji   = "✅" if pnl_usdc >= 0 else "❌"

    pnl_line = ""
    if entry_pct:
        pnl_line = (
            f"  📈 *Entry → Exit:* {entry_pct}¢ → {price_pct}¢\n"
            f"  {pnl_emoji} *Realized P&L:* {pnl_sign}${pnl_usdc:,.2f}  ({pnl_sign}{pnl_pct}%)\n"
        )

    # Hold duration
    hold_line = ""
    if a.hold_duration_seconds > 0:
        hold_line = f"  ⏱ *Held for:* {a.exit_speed_label}\n"

    # Early exit warning
    early_line = ""
    if a.is_early_exit and a.days_to_resolution:
        early_line = (
            f"  ⚠️ *EARLY EXIT — {a.days_to_resolution:.0f} days before resolution!*\n"
            f"  _This looks like profit-taking, not conviction._\n"
        )

    # Style
    style_line = f"  🎭 *Style:* {a.trade_style.capitalize()}\n" if a.trade_style else ""

    stats_block = _stats_block(stats) if stats else ""

    tx_line  = f"  🔗 [Tx](https://polygonscan.com/tx/{a.tx_hash})" if a.tx_hash else ""
    mkt_line = f"  📊 [Market]({a.market_url})" if a.market_url else ""
    links    = "  " + "  |  ".join(filter(None, [tx_line, mkt_line])) if (tx_line or mkt_line) else ""

    msg = (
        f"🔴 *SELL DETECTED*\n\n"
        f"👤 *Wallet:* `{_short(a.wallet)}`  _{a.wallet_label}_\n"
        f"📋 *Market:* {_escape(a.market_question)}\n\n"
        f"  💰 *Sold:* {a.outcome} @ {price_pct}¢\n"
        f"  📦 *Size:* ${a.size_usdc:,.2f} USDC\n"
        f"{pnl_line}"
        f"{hold_line}"
        f"{style_line}"
        f"{early_line}"
        f"  🕐 *Time:* {ts_str} UTC\n"
        f"{links}\n"
        f"{stats_block}"
    )
    return msg.strip()


def _format_exit_warning(a: TradeAnalysis, my_pos: Optional[dict]) -> str:
    """Alert for copy-traders: the insider is getting out."""
    price_pct = round(a.price * 100, 1)

    my_block = ""
    if my_pos:
        my_entry    = float(my_pos.get("avg_entry", 0))
        my_shares   = float(my_pos.get("shares", 0))
        my_pnl      = round((a.price - my_entry) * my_shares, 2)
        my_pnl_pct  = round((a.price - my_entry) / my_entry * 100, 1) if my_entry else 0
        sign        = "+" if my_pnl >= 0 else ""
        emoji       = "✅" if my_pnl >= 0 else "❌"
        my_block = (
            f"\n📋 *YOUR COPY POSITION*\n"
            f"  Entry: {round(my_entry*100,1)}¢  →  Current: {price_pct}¢\n"
            f"  {emoji} Unrealized P&L: {sign}${my_pnl:,.2f}  ({sign}{my_pnl_pct}%)\n"
            f"  👉 *Consider exiting now if following this wallet*\n"
        )

    early_tag = " *(EARLY — before resolution!)*" if a.is_early_exit else ""

    msg = (
        f"🚨 *EXIT ALERT — INSIDER SELLING{early_tag}*\n\n"
        f"👤 *Wallet:* `{_short(a.wallet)}`  _{a.wallet_label}_\n"
        f"📋 *Market:* {_escape(a.market_question)}\n\n"
        f"  🔴 Selling {a.outcome} @ {price_pct}¢\n"
        f"  📦 Size: ${a.size_usdc:,.2f} USDC\n"
        f"  ⏱ Held: {a.exit_speed_label}\n"
        f"{my_block}"
    )
    return msg.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Shared blocks
# ─────────────────────────────────────────────────────────────────────────────

def _stats_block(stats: WalletStats) -> str:
    if stats.total_closed < 2:
        return "\n📊 *Wallet History:* _Not enough data yet_\n"
    return (
        f"\n📊 *WALLET HISTORY*\n"
        f"  Win rate: *{stats.win_rate_pct}%*  ({stats.profitable}/{stats.total_closed} closed)\n"
        f"  Avg hold: *{stats.avg_hold_hours}h*\n"
        f"  Early exits: *{stats.early_exits}*\n"
        f"  Style: *{stats.style_label}*\n"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Telegram delivery
# ─────────────────────────────────────────────────────────────────────────────

def _send(text: str, retries: int = 3) -> bool:
    """Send a message via Telegram Bot API with retry logic."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not configured")
        return False

    url     = TELEGRAM_API.format(token=config.TELEGRAM_BOT_TOKEN)
    payload = {
        "chat_id":                  config.TELEGRAM_CHAT_ID,
        "text":                     text[:MAX_MSG_LEN],
        "parse_mode":               "Markdown",
        "disable_web_page_preview": True,
    }

    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                log.info("Telegram alert sent (%d chars)", len(text))
                return True
            # 429 = rate limited
            if resp.status_code == 429:
                retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                log.warning("Telegram rate limit — waiting %ds", retry_after)
                time.sleep(retry_after)
                continue
            log.warning("Telegram HTTP %d: %s", resp.status_code, resp.text[:200])
        except requests.RequestException as exc:
            log.warning("Telegram send attempt %d failed: %s", attempt, exc)
            if attempt < retries:
                time.sleep(2 ** attempt)

    return False


# ─────────────────────────────────────────────────────────────────────────────
# Formatting utilities
# ─────────────────────────────────────────────────────────────────────────────

def _short(addr: str) -> str:
    """Shorten a wallet address: 0x1234...5678"""
    if not addr or len(addr) < 10:
        return addr
    return f"{addr[:6]}...{addr[-4:]}"


def _ts_to_utc(ts: int) -> str:
    if not ts:
        return "unknown"
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)


def _escape(text: str) -> str:
    """Escape Markdown special chars in plain text fields."""
    for char in ("_", "*", "`", "["):
        text = text.replace(char, f"\\{char}")
    return text
