"""
state_manager.py — Persistent JSON state for the bot

State survives between GitHub Actions runs by being committed back to the repo.

State schema:
{
  "wallets": {
    "<wallet_addr>": {
      "last_checked_ts": int,        # unix timestamp of last successful poll
      "last_block":      int,        # last Polygonscan block checked
      "seen_trade_ids":  [str],      # trade IDs already alerted (keep last 500)
      "positions": {
        "<token_id>": {
          "outcome":      str,       # "Yes" | "No"
          "shares":       float,
          "avg_entry":    float,     # weighted average entry price
          "total_cost":   float,     # total USDC spent
          "first_buy_ts": int,
          "last_trade_ts": int,
          "status":       "OPEN" | "CLOSED",
          "exit_price":   float | null,
          "exit_ts":      int | null,
        }
      }
    }
  },
  "meta": {
    "last_run_ts":    int,
    "total_alerts":   int,
    "schema_version": int
  }
}
"""

import json
import logging
import time
from copy import deepcopy
from pathlib import Path
from typing import Optional

from . import config

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2
MAX_SEEN_IDS   = 600   # cap seen_trade_ids list to avoid unbounded growth

_state: dict = {}


# ─────────────────────────────────────────────────────────────────────────────
# Load / Save
# ─────────────────────────────────────────────────────────────────────────────

def load() -> None:
    """Load state from disk into module-level _state dict."""
    global _state
    config.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    if config.STATE_FILE.exists():
        try:
            with open(config.STATE_FILE) as f:
                _state = json.load(f)
            log.info("State loaded (%d wallets)", len(_state.get("wallets", {})))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read state.json (%s) — starting fresh", exc)
            _state = {}
    else:
        log.info("No existing state.json — initialising fresh state")
        _state = {}

    # Ensure structure
    _state.setdefault("wallets", {})
    _state.setdefault("meta", {
        "last_run_ts":    0,
        "total_alerts":   0,
        "schema_version": SCHEMA_VERSION,
    })


def save() -> None:
    """Persist _state to disk."""
    _state["meta"]["last_run_ts"] = int(time.time())
    config.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(config.STATE_FILE, "w") as f:
            json.dump(_state, f, indent=2)
        log.info("State saved")
    except OSError as exc:
        log.error("Failed to save state: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Wallet-level helpers
# ─────────────────────────────────────────────────────────────────────────────

def _wallet(addr: str) -> dict:
    """Return (and auto-create) the state sub-dict for a wallet."""
    addr = addr.lower()
    if addr not in _state["wallets"]:
        _state["wallets"][addr] = {
            "last_checked_ts": 0,
            "last_block":      0,
            "seen_trade_ids":  [],
            "positions":       {},
        }
    return _state["wallets"][addr]


def get_last_checked_ts(addr: str) -> int:
    return _wallet(addr).get("last_checked_ts", 0)


def set_last_checked_ts(addr: str, ts: int) -> None:
    _wallet(addr)["last_checked_ts"] = ts


def get_last_block(addr: str) -> int:
    return _wallet(addr).get("last_block", 0)


def set_last_block(addr: str, block: int) -> None:
    _wallet(addr)["last_block"] = block


def is_trade_seen(addr: str, trade_id: str) -> bool:
    return trade_id in _wallet(addr)["seen_trade_ids"]


def mark_trade_seen(addr: str, trade_id: str) -> None:
    seen = _wallet(addr)["seen_trade_ids"]
    if trade_id not in seen:
        seen.append(trade_id)
    # Trim to cap
    if len(seen) > MAX_SEEN_IDS:
        _wallet(addr)["seen_trade_ids"] = seen[-MAX_SEEN_IDS:]


def increment_alerts() -> None:
    _state["meta"]["total_alerts"] = _state["meta"].get("total_alerts", 0) + 1


# ─────────────────────────────────────────────────────────────────────────────
# Position tracking
# ─────────────────────────────────────────────────────────────────────────────

def get_position(addr: str, token_id: str) -> Optional[dict]:
    """Return current position for wallet+token, or None if no position."""
    pos = _wallet(addr)["positions"].get(token_id)
    return deepcopy(pos) if pos else None


def update_position(addr: str, token_id: str, trade: dict) -> dict:
    """
    Apply a new trade to the position ledger and return the updated position.

    BUY  → increases shares, recalculates weighted avg entry
    SELL → reduces shares; marks CLOSED if shares reach ~0
    """
    positions = _wallet(addr)["positions"]
    pos = positions.get(token_id)

    side     = trade["side"].upper()
    shares   = float(trade.get("shares", 0))
    price    = float(trade.get("price", 0))
    trade_ts = int(trade.get("match_time", 0))

    if pos is None:
        # First time we see this token for this wallet
        pos = {
            "outcome":      trade.get("outcome", ""),
            "shares":       0.0,
            "avg_entry":    0.0,
            "total_cost":   0.0,
            "first_buy_ts": trade_ts,
            "last_trade_ts": trade_ts,
            "status":       "OPEN",
            "exit_price":   None,
            "exit_ts":      None,
        }

    if side == "BUY":
        old_cost        = pos["shares"] * pos["avg_entry"]
        new_cost        = shares * price
        pos["shares"]   = round(pos["shares"] + shares, 6)
        pos["total_cost"] = round(pos["total_cost"] + shares * price, 4)
        pos["avg_entry"] = round(
            (old_cost + new_cost) / pos["shares"]
            if pos["shares"] > 0 else price,
            6,
        )
        pos["status"]       = "OPEN"
        pos["last_trade_ts"] = trade_ts

    elif side == "SELL":
        pos["shares"]        = max(0.0, round(pos["shares"] - shares, 6))
        pos["last_trade_ts"] = trade_ts
        if pos["shares"] < 0.01:
            pos["status"]     = "CLOSED"
            pos["exit_price"] = price
            pos["exit_ts"]    = trade_ts

    positions[token_id] = pos
    return deepcopy(pos)


def get_all_open_positions(addr: str) -> dict:
    """Return all OPEN positions for a wallet as {token_id: position_dict}."""
    return {
        tid: deepcopy(pos)
        for tid, pos in _wallet(addr)["positions"].items()
        if pos.get("status") == "OPEN"
    }


# ─────────────────────────────────────────────────────────────────────────────
# Copy portfolio
# ─────────────────────────────────────────────────────────────────────────────

def load_copy_portfolio() -> dict:
    """Load my personal copy-trade records."""
    if config.COPY_PORTFOLIO_FILE.exists():
        try:
            with open(config.COPY_PORTFOLIO_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_copy_portfolio(portfolio: dict) -> None:
    config.COPY_PORTFOLIO_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(config.COPY_PORTFOLIO_FILE, "w") as f:
        json.dump(portfolio, f, indent=2)
