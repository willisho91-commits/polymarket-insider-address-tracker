"""
main.py — Polymarket Wallet Monitor Orchestrator

Run by GitHub Actions every 5 minutes.
Flow per wallet:
  1. Fetch recent trades from Polymarket CLOB API
  2. Filter to only new trades (not seen in state)
  3. Enrich with market metadata
  4. Update position ledger
  5. Run behavioral analysis
  6. Send Telegram alert
  7. Save state

Exit codes:
  0 = success (even if no new trades found)
  1 = fatal error (mis-configuration, repeated API failure)
"""

import logging
import sys
import time
from typing import Optional

# ── Local imports ─────────────────────────────────────────────────────────────
from . import config
from . import state_manager as state
from . import polymarket_client as pm_client
from . import polygonscan_client as poly_client
from .behavioral_analyzer import analyse_trade, compute_wallet_stats
from .telegram_notifier import (
    send_trade_alert,
    send_exit_warning,
    send_error_alert,
    send_startup_message,
)

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("main")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def run(send_startup: bool = False) -> int:
    """
    Main run function.
    Returns 0 on success, 1 on fatal error.
    """
    log.info("=== Polymarket Monitor run started ===")

    # ── Pre-flight checks ────────────────────────────────────────────────────
    if not config.TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN is not set — cannot send alerts")
        return 1
    if not config.TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_CHAT_ID is not set")
        return 1
    if not config.WALLETS:
        log.error("No wallets configured. Add addresses to wallets.json")
        return 1

    # ── Load persistent state ─────────────────────────────────────────────────
    state.load()

    if send_startup:
        send_startup_message(len(config.WALLETS))

    total_alerts = 0

    # ── Process each wallet ───────────────────────────────────────────────────
    for wallet_addr, wallet_meta in config.WALLETS.items():
        wallet_addr  = wallet_addr.lower()
        wallet_label = wallet_meta.get("label", _short(wallet_addr))

        log.info("Checking wallet: %s (%s)", wallet_addr, wallet_label)

        try:
            alerts = _process_wallet(wallet_addr, wallet_label)
            total_alerts += alerts
        except Exception as exc:
            log.exception("Unhandled error for wallet %s: %s", wallet_addr, exc)
            send_error_alert(f"Error monitoring {wallet_label} ({_short(wallet_addr)}): {exc}")

        time.sleep(config.INTER_REQUEST_GAP)

    # ── Save state ────────────────────────────────────────────────────────────
    state.save()
    log.info("=== Run complete — %d alert(s) sent ===", total_alerts)
    return 0


def _process_wallet(wallet_addr: str, wallet_label: str) -> int:
    """
    Process one wallet. Returns number of alerts sent.
    """
    last_checked_ts = state.get_last_checked_ts(wallet_addr)
    now_ts          = int(time.time())
    alerts_sent     = 0

    # ── Fetch trades ──────────────────────────────────────────────────────────
    trades = pm_client.get_trades_for_wallet(
        wallet_addr,
        since_timestamp=last_checked_ts,
    )

    if not trades:
        log.info("  No new trades for %s", wallet_label)
        state.set_last_checked_ts(wallet_addr, now_ts)
        return 0

    log.info("  Found %d trade(s) for %s", len(trades), wallet_label)

    # Sort oldest-first so position ledger builds correctly
    trades.sort(key=lambda t: t["match_time"])

    # ── All positions for behavioural stats ───────────────────────────────────
    all_positions = state.get_all_open_positions(wallet_addr)
    # Also include all positions (open+closed) from full state
    all_positions_full = state._wallet(wallet_addr).get("positions", {})

    for trade in trades:
        trade_id = trade.get("id", "")
        if not trade_id:
            continue

        # Skip already-alerted trades
        if state.is_trade_seen(wallet_addr, trade_id):
            log.debug("  Trade %s already seen — skipping", trade_id)
            continue

        token_id = trade.get("token_id", "")
        side     = trade.get("side", "").upper()

        # ── Enrich with market metadata ───────────────────────────────────────
        market = None
        if token_id:
            market = pm_client.get_market_by_token_id(token_id)
        time.sleep(config.INTER_REQUEST_GAP)

        # ── Update position ledger ────────────────────────────────────────────
        updated_position = state.update_position(wallet_addr, token_id, trade)

        # ── Behavioral analysis ───────────────────────────────────────────────
        wallet_stats = compute_wallet_stats(all_positions_full)

        analysis = analyse_trade(
            trade        = trade,
            position     = updated_position,
            market       = market,
            wallet_label = wallet_label,
            all_positions= all_positions_full,
        )

        # ── Decide alert type ─────────────────────────────────────────────────
        if side == "SELL":
            # Check if we have a copy position that needs the exit warning
            copy_portfolio = state.load_copy_portfolio()
            my_copy_pos    = copy_portfolio.get(wallet_addr, {}).get(token_id)

            if my_copy_pos:
                log.info("  ⚠️  EXIT alert for copied position: %s", token_id)
                send_exit_warning(analysis, my_copy_pos)
            else:
                send_trade_alert(analysis, wallet_stats)
        else:
            send_trade_alert(analysis, wallet_stats)

        # ── Mark trade seen & update counters ─────────────────────────────────
        state.mark_trade_seen(wallet_addr, trade_id)
        state.increment_alerts()
        alerts_sent += 1

        log.info(
            "  Alert sent: %s %s %s @ %.2f (${%.2f})",
            wallet_label, side, trade.get("outcome", ""), trade["price"], trade["size_usdc"]
        )

        # Small gap between alerts to respect Telegram rate limits
        time.sleep(0.5)

    # Update last-checked timestamp to now
    state.set_last_checked_ts(wallet_addr, now_ts)
    return alerts_sent


# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

def _short(addr: str) -> str:
    if not addr or len(addr) < 10:
        return addr
    return f"{addr[:6]}...{addr[-4:]}"


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Polymarket Wallet Monitor")
    parser.add_argument(
        "--startup",
        action="store_true",
        help="Send a startup confirmation message to Telegram",
    )
    args = parser.parse_args()

    exit_code = run(send_startup=args.startup)
    sys.exit(exit_code)
