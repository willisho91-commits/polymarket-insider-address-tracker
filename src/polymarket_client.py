"""
polymarket_client.py — Polymarket CLOB + Gamma API queries

Responsibilities:
- Fetch recent trades for a wallet address
- Enrich trades with market metadata (question, end date, current price)
- Handle pagination and rate limiting
"""

import time
import logging
from typing import Optional
import requests

from . import config

log = logging.getLogger(__name__)

_session = requests.Session()
_session.headers.update({
    "Accept": "application/json",
    "User-Agent": "polymarket-monitor/1.0",
})

# ── Market metadata cache (avoids redundant API calls within one run) ─────────
_market_cache: dict = {}


# ─────────────────────────────────────────────────────────────────────────────
# Trades
# ─────────────────────────────────────────────────────────────────────────────

def get_trades_for_wallet(
    wallet_addr: str,
    since_timestamp: Optional[int] = None,
    limit: int = config.MAX_TRADES_FETCH,
) -> list[dict]:
    """
    Return recent CONFIRMED trades for a wallet (maker + taker roles combined).

    Polymarket CLOB uses a proxy-wallet architecture:
      - EOA signs transactions
      - The actual orders are placed by a proxy (created per user)
    We query both maker_address and taker_address to catch all activity.

    Each returned trade dict is normalised to:
    {
      "id":               str,
      "wallet":           str (the address we're monitoring),
      "side":             "BUY" | "SELL",
      "outcome":          "Yes" | "No",
      "price":            float,   # 0..1
      "size_usdc":        float,
      "token_id":         str,     # ERC-1155 outcome token id
      "match_time":       int,     # unix timestamp
      "tx_hash":          str,
    }
    """
    addr = wallet_addr.lower()
    raw_trades: dict[str, dict] = {}

    # ── Primary: Gamma API (public, no auth required) ─────────────────────────
    # Gamma indexes all Polymarket activity and is queryable by user address.
    offset  = 0
    fetched = 0

    while fetched < limit:
        params = {
            "user":   addr,
            "limit":  min(100, limit - fetched),
            "offset": offset,
        }
        if since_timestamp:
            params["after"] = since_timestamp

        try:
            resp = _session.get(
                f"{config.GAMMA_API_BASE}/trades",
                params=params,
                timeout=config.REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            log.warning("Gamma /trades error (wallet=%s): %s", addr, exc)
            break

        if not isinstance(data, list):
            data = data.get("data") or data.get("trades") or []

        if not data:
            break

        for raw in data:
            # Gamma returns a slightly different shape — normalise below
            trade_id = str(raw.get("id") or raw.get("tradeId") or "")
            if not trade_id:
                continue

            side_raw   = (raw.get("side") or raw.get("type") or "").upper()
            price      = float(raw.get("price") or raw.get("avgPrice") or 0)
            size       = float(raw.get("size") or raw.get("shares") or 0)
            match_time = int(raw.get("timestamp") or raw.get("createdAt") or 0)
            if isinstance(match_time, str):
                from datetime import datetime, timezone
                try:
                    match_time = int(datetime.fromisoformat(
                        match_time.replace("Z", "+00:00")
                    ).timestamp())
                except Exception:
                    match_time = 0

            if since_timestamp and match_time and match_time <= since_timestamp:
                continue

            normalised = {
                "id":         trade_id,
                "wallet":     addr,
                "side":       side_raw,
                "outcome":    raw.get("outcome") or raw.get("side") or "",
                "price":      price,
                "size_usdc":  round(price * size, 4),
                "shares":     size,
                "token_id":   str(raw.get("asset_id") or raw.get("tokenId") or raw.get("conditionId") or ""),
                "match_time": match_time,
                "tx_hash":    raw.get("transactionHash") or raw.get("txHash") or "",
            }
            raw_trades[trade_id] = normalised

        fetched += len(data)
        offset  += len(data)
        if len(data) < 100:
            break
        time.sleep(config.INTER_REQUEST_GAP)

    # ── Fallback: CLOB API (may require auth on some endpoints) ───────────────
    if not raw_trades:
        for role in ("maker_address", "taker_address"):
            params = {role: addr, "limit": min(100, limit)}
            try:
                resp = _session.get(
                    f"{config.CLOB_API_BASE}/trades",
                    params=params,
                    timeout=config.REQUEST_TIMEOUT,
                )
                if resp.status_code == 401:
                    log.debug("CLOB /trades returned 401 — skipping (auth required)")
                    break
                resp.raise_for_status()
                body = resp.json()
                data = body.get("data") or []
            except requests.RequestException as exc:
                log.warning("CLOB /trades fallback error (role=%s): %s", role, exc)
                break

            for raw in data:
                if raw.get("status", "").upper() != "CONFIRMED":
                    continue
                trade_id = raw.get("id", "")
                match_ts = int(raw.get("match_time", 0) or 0)
                if since_timestamp and match_ts <= since_timestamp:
                    continue
                price = float(raw.get("price", 0) or 0)
                size  = float(raw.get("size", 0) or 0)
                normalised = {
                    "id":         trade_id,
                    "wallet":     addr,
                    "side":       raw.get("side", "").upper(),
                    "outcome":    raw.get("outcome", ""),
                    "price":      price,
                    "size_usdc":  round(price * size, 4),
                    "shares":     size,
                    "token_id":   raw.get("asset_id", raw.get("market", "")),
                    "match_time": match_ts,
                    "tx_hash":    raw.get("transaction_hash", ""),
                }
                raw_trades[trade_id] = normalised

    # Sort newest-first
    trades = sorted(raw_trades.values(), key=lambda t: t["match_time"], reverse=True)

    # Drop dust trades
    trades = [t for t in trades if t["size_usdc"] >= config.MIN_TRADE_SIZE_USD]

    return trades[:limit]


# ─────────────────────────────────────────────────────────────────────────────
# Market metadata
# ─────────────────────────────────────────────────────────────────────────────

def get_market_by_token_id(token_id: str) -> Optional[dict]:
    """
    Fetch market metadata via the Gamma API.
    Returns a normalised dict:
    {
      "question":      str,
      "end_date_iso":  str,   # ISO-8601
      "end_ts":        int,   # unix timestamp
      "active":        bool,
      "closed":        bool,
      "url":           str,
      "current_price": float,  # latest mid-price for this outcome token
    }
    """
    if token_id in _market_cache:
        return _market_cache[token_id]

    # ── Gamma API lookup ──────────────────────────────────────────────────────
    try:
        resp = _session.get(
            f"{config.GAMMA_API_BASE}/markets",
            params={"clob_token_ids": token_id},
            timeout=config.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        markets = resp.json()
        if not markets:
            raise ValueError("empty response")
        mkt = markets[0]
    except Exception as exc:
        log.warning("Gamma market lookup failed (token=%s): %s", token_id, exc)

        # ── CLOB fallback ─────────────────────────────────────────────────────
        try:
            resp2 = _session.get(
                f"{config.CLOB_API_BASE}/markets/{token_id}",
                timeout=config.REQUEST_TIMEOUT,
            )
            resp2.raise_for_status()
            mkt = resp2.json()
        except Exception as exc2:
            log.warning("CLOB market lookup also failed: %s", exc2)
            return None

    # Normalise fields (Gamma and CLOB have slightly different shapes)
    question   = mkt.get("question") or mkt.get("description") or "Unknown Market"
    end_raw    = (
        mkt.get("endDate")
        or mkt.get("end_date_iso")
        or mkt.get("end_date")
        or ""
    )
    active     = bool(mkt.get("active", True))
    closed     = bool(mkt.get("closed", False))
    slug       = mkt.get("slug") or mkt.get("condition_id") or token_id[:12]
    url        = f"{config.POLYMARKET_BASE}/event/{slug}"

    # Current mid-price for this specific token
    current_price = _get_current_price(token_id)

    # Parse end date to timestamp
    end_ts = _iso_to_ts(end_raw)

    result = {
        "question":      question,
        "end_date_iso":  end_raw,
        "end_ts":        end_ts,
        "active":        active,
        "closed":        closed,
        "url":           url,
        "current_price": current_price,
    }
    _market_cache[token_id] = result
    return result


def _get_current_price(token_id: str) -> float:
    """Fetch the current best-bid mid-price for an outcome token."""
    try:
        resp = _session.get(
            f"{config.CLOB_API_BASE}/midpoint",
            params={"token_id": token_id},
            timeout=config.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()
        return float(body.get("mid", 0) or 0)
    except Exception:
        return 0.0


def _iso_to_ts(iso_str: str) -> int:
    """Convert ISO-8601 date string to unix timestamp (0 on failure)."""
    if not iso_str:
        return 0
    from datetime import datetime, timezone
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(iso_str, fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue
    return 0
