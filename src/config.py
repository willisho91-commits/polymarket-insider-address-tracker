"""
config.py — Central configuration for Polymarket Monitor
All secrets come from environment variables / GitHub Secrets.
"""

import os
import json
from pathlib import Path

# ── API Base URLs ─────────────────────────────────────────────────────────────
CLOB_API_BASE    = "https://clob.polymarket.com"
GAMMA_API_BASE   = "https://gamma-api.polymarket.com"
POLYMARKET_BASE  = "https://polymarket.com"
POLYGONSCAN_BASE = "https://api.polygonscan.com/api"

# ── Polymarket on-chain contracts (Polygon mainnet) ───────────────────────────
CTF_EXCHANGE_CONTRACT = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_EXCHANGE     = "0xC5d563A36AE78145C45a50134d48A1215220f80a"  # neg-risk markets
USDC_CONTRACT         = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e on Polygon

# ── Secrets (set as GitHub Secrets, read from env) ───────────────────────────
TELEGRAM_BOT_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID     = os.environ.get("TELEGRAM_CHAT_ID", "")
POLYGONSCAN_API_KEY  = os.environ.get("POLYGONSCAN_API_KEY", "")

# ── Wallet Watchlist ──────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent


def load_wallets() -> dict:
    """
    Load wallet watchlist from wallets.json.
    Format:
    {
      "0xABCD...": { "label": "Whale A", "copy_multiplier": 1.0 },
      ...
    }
    Falls back to WALLET_ADDRESSES env var (comma-separated) if file missing.
    """
    wallet_file = ROOT / "wallets.json"
    if wallet_file.exists():
        with open(wallet_file) as f:
            data = json.load(f)
        return data

    raw = os.environ.get("WALLET_ADDRESSES", "")
    if raw:
        addresses = [a.strip().lower() for a in raw.split(",") if a.strip()]
        return {
            addr: {"label": f"Wallet {i + 1}", "copy_multiplier": 1.0}
            for i, addr in enumerate(addresses)
        }
    return {}


WALLETS: dict = load_wallets()

# ── Copy Trading Settings ─────────────────────────────────────────────────────
MY_BASE_COPY_SIZE_USDC = float(os.environ.get("MY_COPY_SIZE_USDC", "50"))
MAX_COPY_LOSS_PCT       = float(os.environ.get("MAX_COPY_LOSS_PCT", "80"))

# ── Behavioural Thresholds ────────────────────────────────────────────────────
SWING_TRADE_HOURS  = 72    # hold < 72 h → labelled "swing trade"
EARLY_EXIT_DAYS    = 3     # exit > N days before resolution → "early exit / flip"
MIN_TRADE_SIZE_USD = 5.0   # ignore dust trades below this threshold

# ── State & Portfolio Persistence ────────────────────────────────────────────
STATE_FILE          = ROOT / "data" / "state.json"
COPY_PORTFOLIO_FILE = ROOT / "data" / "copy_portfolio.json"

# ── HTTP Request Settings ─────────────────────────────────────────────────────
REQUEST_TIMEOUT   = 15     # seconds
INTER_REQUEST_GAP = 0.4    # seconds between calls per wallet (rate-limit safety)
MAX_TRADES_FETCH  = 100    # trades fetched per wallet per run
