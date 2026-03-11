#!/usr/bin/env python3
"""
run.py — Top-level entry point for GitHub Actions and local testing.

Usage:
    python run.py              # normal poll run
    python run.py --startup    # send startup ping to Telegram first
    python run.py --test       # validate config and exit (no alerts sent)
"""

import sys
import os
import argparse
import logging

# Make sure src/ is importable when run from repo root
sys.path.insert(0, os.path.dirname(__file__))

from src import config
from src.main import run


def test_config():
    """Validate environment and print a status report without running."""
    ok = True
    print("\n=== Polymarket Monitor — Config Check ===\n")

    checks = [
        ("TELEGRAM_BOT_TOKEN",  config.TELEGRAM_BOT_TOKEN),
        ("TELEGRAM_CHAT_ID",    config.TELEGRAM_CHAT_ID),
        ("POLYGONSCAN_API_KEY", config.POLYGONSCAN_API_KEY),
    ]
    for name, val in checks:
        status = "✅" if val else "❌ NOT SET"
        masked = f"{val[:6]}...{val[-4:]}" if val and len(val) > 10 else val
        print(f"  {status}  {name}: {masked or 'missing'}")

    print(f"\n  Wallets configured: {len(config.WALLETS)}")
    for addr, meta in config.WALLETS.items():
        print(f"    • {addr[:10]}...  label={meta.get('label', 'N/A')}")

    print(f"\n  Copy size:        ${config.MY_BASE_COPY_SIZE_USDC} USDC")
    print(f"  Max copy loss:    {config.MAX_COPY_LOSS_PCT}%")
    print(f"  Swing threshold:  < {config.SWING_TRADE_HOURS}h")
    print(f"  Early exit days:  > {config.EARLY_EXIT_DAYS}d before resolution")
    print(f"  State file:       {config.STATE_FILE}")
    print()

    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        print("❌ Telegram credentials missing — alerts will NOT be sent\n")
        ok = False
    if not config.WALLETS:
        print("❌ No wallets configured — nothing to monitor\n")
        ok = False

    return ok


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
    )

    parser = argparse.ArgumentParser()
    parser.add_argument("--startup", action="store_true",
                        help="Send Telegram startup message before first poll")
    parser.add_argument("--test", action="store_true",
                        help="Validate config only, do not run")
    args = parser.parse_args()

    if args.test:
        ok = test_config()
        sys.exit(0 if ok else 1)

    sys.exit(run(send_startup=args.startup))
