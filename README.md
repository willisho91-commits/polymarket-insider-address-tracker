# 📡 Polymarket Wallet Monitor

> Real-time copy-trading intelligence for Polymarket — alerts via Telegram, hosted on GitHub Actions (free tier).

---

## What This Does

Polls Polymarket's CLOB API every 5 minutes for a list of watched wallets and sends you a Telegram alert whenever one of them trades.

Each alert includes:
- Market name + link
- Direction, price, and size
- Behavioural analysis: swing vs. conviction, early exit flag
- Entry price vs current price (unrealized P&L)
- Copy-trade risk score: max loss, suggested size
- Exit warnings if an insider you're copying starts selling

---

## Alert Examples

### 🟢 New BUY Detected

```
🟢 NEW BUY DETECTED

👤 Wallet: 0xABCD...1234  Whale A
📋 Market: Will the Fed cut rates in March 2025?

  💰 Direction: Yes @ 73¢
  📦 Size: $500.00 USDC (685 shares)
  💹 Current price: 74¢  (+1¢ since buy)
  ⏰ Resolves in: 12.3 days
  🕐 Time: 2025-01-15 14:32 UTC
  🔗 Tx  |  📊 Market

📊 WALLET HISTORY
  Win rate: 67%  (8/12 closed)
  Avg hold: 38.4h
  Early exits: 5
  Style: ⚡ Swing Trader

💼 COPY TRADE ANALYSIS
  Size to copy: $37.50 USDC
  Max loss on $50 position: $36.50
  Risk level: 🟡 MEDIUM RISK — moderate upside
```

### 🔴 SELL Detected (Early Exit)

```
🔴 SELL DETECTED

👤 Wallet: 0xABCD...1234  Whale A
📋 Market: Will the Fed cut rates in March 2025?

  💰 Sold: Yes @ 81¢
  📦 Size: $554.85 USDC
  📈 Entry → Exit: 73¢ → 81¢
  ✅ Realized P&L: +$54.80  (+11.0%)
  ⏱ Held for: 2d 4h 32m after entry
  🎭 Style: Swing
  ⚠️ EARLY EXIT — 10 days before resolution!
     This looks like profit-taking, not conviction.
  🕐 Time: 2025-01-17 19:04 UTC
```

### 🚨 Exit Alert (You're copying this wallet)

```
🚨 EXIT ALERT — INSIDER SELLING (EARLY — before resolution!)

👤 Wallet: 0xABCD...1234  Whale A
📋 Market: Will the Fed cut rates in March 2025?

  🔴 Selling Yes @ 81¢
  📦 Size: $554.85 USDC
  ⏱ Held: 2d 4h 32m after entry

📋 YOUR COPY POSITION
  Entry: 73¢  →  Current: 81¢
  ✅ Unrealized P&L: +$5.48  (+11.0%)
  👉 Consider exiting now if following this wallet
```

---

## Architecture

```
GitHub Actions (cron: */5 * * * *)
        │
        ▼
   run.py  ──► src/main.py (orchestrator)
                    │
                    ├──► src/polymarket_client.py  ──► CLOB API (trades + market data)
                    ├──► src/polygonscan_client.py ──► Polygonscan (on-chain verification)
                    ├──► src/state_manager.py       ──► data/state.json (position ledger)
                    ├──► src/behavioral_analyzer.py ──► pattern detection
                    └──► src/telegram_notifier.py   ──► Telegram Bot API
                    
State persists via git commit back to repo after each run.
```

**Data sources used:**
| Source | Used for | Rate limit (free) |
|---|---|---|
| Polymarket CLOB API | Trade detection, market metadata, current price | ~100 req/min (unauthenticated) |
| Polygonscan API | On-chain verification, block timestamps | 5 req/sec free tier |
| Telegram Bot API | Alert delivery | 30 msg/sec |

---

## Setup Guide

### Step 1 — Fork / Clone this Repo

```bash
git clone https://github.com/YOUR_USERNAME/polymarket-monitor.git
cd polymarket-monitor
```

### Step 2 — Create a Telegram Bot

1. Open Telegram → search `@BotFather`
2. Send `/newbot` → follow prompts → copy your **Bot Token**
3. Start a chat with your new bot (send it any message)
4. Get your **Chat ID**: visit  
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`  
   and find `"chat":{"id": YOUR_CHAT_ID}` in the JSON

### Step 3 — Get a Polygonscan API Key (free)

1. Register at [polygonscan.com](https://polygonscan.com)
2. Go to **My Account → API Keys → Add**
3. Free tier gives 5 req/sec — plenty for 5–20 wallets

### Step 4 — Add GitHub Secrets

In your repo: **Settings → Secrets and variables → Actions → New repository secret**

| Secret name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your bot token from BotFather |
| `TELEGRAM_CHAT_ID` | Your personal chat ID (or group ID) |
| `POLYGONSCAN_API_KEY` | Your Polygonscan API key |

**Optional GitHub Variables** (Settings → Variables → Actions):

| Variable | Default | Description |
|---|---|---|
| `MY_COPY_SIZE_USDC` | `50` | Your default copy trade size in USDC |
| `MAX_COPY_LOSS_PCT` | `80` | Max acceptable loss % before alert |

### Step 5 — Configure Wallets

Edit `wallets.json` in the repo root:

```json
{
  "0xYOUR_WALLET_ADDRESS": {
    "label": "Whale A",
    "copy_multiplier": 1.0,
    "notes": "Optional notes"
  }
}
```

**How to find a whale's wallet address:**
- Go to a Polymarket market page
- Click on a large position in the leaderboard
- Copy the wallet address from the URL or transaction

> ⚠️ **Proxy wallets**: Polymarket creates a "proxy" wallet for each user that actually holds their funds on-chain. The address shown in Polymarket's UI is often the proxy, not the EOA. Use the address that appears in on-chain transactions (Polygonscan) for best results.

### Step 6 — Enable GitHub Actions

1. Go to your repo → **Actions** tab
2. If prompted, click **"I understand my workflows, go ahead and enable them"**
3. The workflow runs automatically every 5 minutes

### Step 7 — First Run Test

Trigger a manual run with startup message:
1. **Actions** → `Polymarket Monitor` → **Run workflow**
2. Set `Send Telegram startup message?` to `true`
3. Click **Run workflow**

You should receive a Telegram message within 60 seconds confirming the bot is live.

### Step 8 — Local Testing

```bash
pip install -r requirements.txt

# Set env vars
export TELEGRAM_BOT_TOKEN="your_token"
export TELEGRAM_CHAT_ID="your_chat_id"
export POLYGONSCAN_API_KEY="your_key"

# Validate config without sending alerts
python run.py --test

# Full run
python run.py

# Full run with startup ping
python run.py --startup
```

---

## Copy Portfolio Tracking

To enable **exit warnings** (the bot alerts you when an insider you're copying starts to sell), add your copy positions to `data/copy_portfolio.json`:

```json
{
  "0xinsider_wallet_address": {
    "TOKEN_ID_FROM_TRADE_ALERT": {
      "avg_entry": 0.45,
      "shares": 100,
      "total_cost": 45.0,
      "opened_ts": 1705329120
    }
  }
}
```

You can find the `token_id` in the Telegram trade alert (logged internally — check Actions logs if you need it).

---

## Tuning Behavioural Parameters

Edit `src/config.py` to adjust:

| Parameter | Default | Meaning |
|---|---|---|
| `SWING_TRADE_HOURS` | `72` | Holds < 72h are classified as swing trades |
| `EARLY_EXIT_DAYS` | `3` | Selling > 3 days before resolution = "early exit / flip" |
| `MIN_TRADE_SIZE_USD` | `5.0` | Ignore trades smaller than $5 (dust filter) |

---

## GitHub Actions Free Tier — Limits

| Limit | Value |
|---|---|
| Minutes/month | 2,000 (public repos: unlimited) |
| Min cron frequency | Every 5 minutes |
| Actual delay | Usually on-time; up to ~10 min under GH load |

**Make your repo public** to get unlimited Actions minutes. If you want to keep it private, 2,000 min/month = 5-min cron for ~7 days. Use a free VPS (Oracle Cloud, Fly.io) for private 24/7 operation.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| No alerts at all | Check Actions logs → verify secrets are set → run `python run.py --test` locally |
| `403` on Polymarket API | Polymarket may have rate-limited your IP temporarily. Wait 10–15 min. |
| `state.json` conflicts | Delete `data/state.json`, push, let it regenerate fresh |
| Bot sends duplicate alerts | Caused by a failed state commit. Check the "Commit state" step in Actions logs |
| Wrong wallet (proxy vs EOA) | Use Polygonscan to find the proxy address for the wallet you want to track |

---

## File Structure

```
polymarket-monitor/
├── .github/
│   └── workflows/
│       └── monitor.yml       ← GitHub Actions cron job
├── src/
│   ├── __init__.py
│   ├── config.py             ← All settings + env var loading
│   ├── polymarket_client.py  ← CLOB API + Gamma market data
│   ├── polygonscan_client.py ← On-chain verification
│   ├── state_manager.py      ← Position ledger + seen-trade dedup
│   ├── behavioral_analyzer.py← Pattern detection + P&L calc
│   ├── telegram_notifier.py  ← Alert formatting + delivery
│   └── main.py               ← Orchestrator
├── data/
│   ├── state.json            ← Auto-updated each run (committed by bot)
│   └── copy_portfolio.json   ← Your manual copy trade records
├── wallets.json              ← Your watchlist
├── run.py                    ← CLI entry point
├── requirements.txt
└── README.md
```

---

## Known Limitations

1. **~5 min latency** — GitHub Actions cron has no sub-5-minute option. For faster alerts, run on a persistent server (even a free Raspberry Pi).
2. **Proxy wallets** — Polymarket routes on-chain activity through proxy contracts. The address you see on Polymarket's UI may differ from what appears in the CLOB API. Test with a known whale trade to confirm you have the right address.
3. **Market name accuracy** — Relies on Gamma API availability. If Gamma is down, market question shows "Unknown Market" (trade alert still sends).
4. **No Dune Analytics integration by default** — Dune free tier has low query rate limits. Historical deep-analysis queries are reserved for manual use (Dune dashboard links included in alerts where available).

---

## License

MIT — use freely, at your own risk. Not financial advice.
