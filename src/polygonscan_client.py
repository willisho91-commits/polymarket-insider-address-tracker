"""
polygonscan_client.py — Polygonscan API for on-chain trade verification

Used as a secondary data source and to:
- Confirm trades the CLOB API might miss (e.g. during outages)
- Enrich with exact block timestamps
- Detect direct contract interactions (advanced users bypassing UI)
"""

import time
import logging
from typing import Optional
import requests

from . import config

log = logging.getLogger(__name__)

_session = requests.Session()
_session.headers.update({"User-Agent": "polymarket-monitor/1.0"})

# ERC-1155 Transfer topic (keccak of TransferSingle event signature)
TRANSFER_SINGLE_TOPIC = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
# OrderFilled topic on CTF Exchange
ORDER_FILLED_TOPIC    = "0xd0a08e8c493f9c94f29311604c9de1b4e8c8d4c06de0b47e952c1c4c5f0f26a0"


def get_wallet_transactions(
    wallet_addr: str,
    since_block: Optional[int] = None,
    max_results: int = 200,
) -> list[dict]:
    """
    Fetch recent normal + ERC-1155 token transactions for a wallet,
    filtered to only those interacting with Polymarket contracts.

    Returns list of dicts:
    {
      "tx_hash":    str,
      "block":      int,
      "timestamp":  int,
      "from":       str,
      "to":         str,
      "value_usdc": float,
      "type":       "normal" | "token_transfer",
    }
    """
    if not config.POLYGONSCAN_API_KEY:
        log.debug("POLYGONSCAN_API_KEY not set — skipping on-chain verification")
        return []

    results = []
    start_block = since_block or 0

    # ── 1. Normal transactions (direct CTF Exchange calls) ────────────────────
    txns = _polygonscan_call(
        module="account",
        action="txlist",
        address=wallet_addr,
        startblock=start_block,
        endblock=99999999,
        sort="desc",
        offset=max_results,
        page=1,
    )
    for tx in txns or []:
        if _is_polymarket_contract(tx.get("to", "")):
            results.append({
                "tx_hash":    tx.get("hash", ""),
                "block":      int(tx.get("blockNumber", 0)),
                "timestamp":  int(tx.get("timeStamp", 0)),
                "from":       tx.get("from", "").lower(),
                "to":         tx.get("to", "").lower(),
                "value_usdc": _wei_to_usdc(tx.get("value", "0")),
                "type":       "normal",
            })

    time.sleep(config.INTER_REQUEST_GAP)

    # ── 2. ERC-20 (USDC) transfers to/from Polymarket ─────────────────────────
    token_txns = _polygonscan_call(
        module="account",
        action="tokentx",
        address=wallet_addr,
        contractaddress=config.USDC_CONTRACT,
        startblock=start_block,
        endblock=99999999,
        sort="desc",
        offset=max_results,
        page=1,
    )
    for tx in token_txns or []:
        counterpart = tx.get("to", "") if tx.get("from", "").lower() == wallet_addr.lower() else tx.get("from", "")
        if _is_polymarket_contract(counterpart):
            value_raw = float(tx.get("value", 0)) / 1e6  # USDC has 6 decimals
            results.append({
                "tx_hash":    tx.get("hash", ""),
                "block":      int(tx.get("blockNumber", 0)),
                "timestamp":  int(tx.get("timeStamp", 0)),
                "from":       tx.get("from", "").lower(),
                "to":         tx.get("to", "").lower(),
                "value_usdc": round(value_raw, 4),
                "type":       "usdc_transfer",
            })

    # Deduplicate by tx_hash
    seen = set()
    unique = []
    for r in sorted(results, key=lambda x: x["timestamp"], reverse=True):
        if r["tx_hash"] not in seen:
            seen.add(r["tx_hash"])
            unique.append(r)

    return unique[:max_results]


def get_latest_block() -> Optional[int]:
    """Return the latest block number on Polygon."""
    data = _polygonscan_call(module="proxy", action="eth_blockNumber")
    if data:
        try:
            return int(data, 16)
        except (TypeError, ValueError):
            pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _polygonscan_call(module: str, action: str, **kwargs) -> Optional[any]:
    """Generic Polygonscan API call. Returns result field or None on error."""
    params = {
        "module": module,
        "action": action,
        "apikey": config.POLYGONSCAN_API_KEY,
    }
    params.update(kwargs)
    try:
        resp = _session.get(
            config.POLYGONSCAN_BASE,
            params=params,
            timeout=config.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") == "1" or body.get("message") == "OK":
            return body.get("result")
        log.debug("Polygonscan non-success: %s", body.get("message"))
        return None
    except Exception as exc:
        log.warning("Polygonscan error (%s/%s): %s", module, action, exc)
        return None


_POLYMARKET_CONTRACTS = {
    config.CTF_EXCHANGE_CONTRACT.lower(),
    config.NEG_RISK_EXCHANGE.lower(),
}


def _is_polymarket_contract(addr: str) -> bool:
    return addr.lower() in _POLYMARKET_CONTRACTS


def _wei_to_usdc(wei_str: str) -> float:
    """Convert MATIC wei string to float (not USDC — just for reference)."""
    try:
        return round(int(wei_str) / 1e18, 6)
    except (TypeError, ValueError):
        return 0.0
