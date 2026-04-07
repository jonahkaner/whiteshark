# Quicksand Setup Guide

Get the bot running in ~15 minutes. You need a Kalshi account and a server.

---

## Step 1: Create a Kalshi Account (5 min)

Kalshi is a CFTC-regulated prediction market — fully legal in the US.

1. Go to [kalshi.com](https://kalshi.com) and create an account
2. Complete identity verification (required, it's regulated)
3. Deposit money:
   - **Bank transfer (ACH)**: Free, takes 1-3 days
   - **Debit card**: Instant, 2% fee
4. Create API keys:
   - Go to **Settings → API Keys**
   - Create a new key
   - Save the **API Key** — you'll need this

**How much to start with?**
- Minimum: $100 (enough to test with real trades in demo)
- Recommended start: $1,000-$5,000
- The bot starts in **demo mode** (play money) so you can test first

---

## Step 2: Set Up Telegram Alerts (3 min, optional)

Get trade notifications on your phone:

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the prompts
3. Save the **token** it gives you
4. Message your new bot "hi", then visit:
   ```
   https://api.telegram.org/botYOUR_TOKEN/getUpdates
   ```
5. Find `"chat":{"id":XXXXXXXX}` — that's your **chat_id**

---

## Step 3: Configure the Bot (2 min)

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml`:
```yaml
mode: paper  # Start with paper! Change to "live" when ready

kalshi:
  api_key: "your-kalshi-api-key"
  demo: true  # true = play money, false = real money

alerts:
  telegram:
    enabled: true
    bot_token: "your-telegram-bot-token"
    chat_id: "your-chat-id"
```

---

## Step 4: Run the Bot

```bash
# Install
pip install -e .

# Start the web dashboard (access from your phone!)
python -m quicksand.web.run

# Open on your phone: http://your-server-ip:8000
# Hit the "Start Bot" button
```

That's it. The dashboard shows your balance, P&L, open positions, and trades.

---

## Step 5: Go Live

After testing in demo mode for a few days:

1. Edit `config.yaml`:
   ```yaml
   mode: live
   kalshi:
     demo: false
   ```
2. Start small ($500-$1,000)
3. Watch it run for a week
4. Scale up as you're comfortable

---

## How the Strategy Works

**Market Making on Prediction Markets**

Kalshi has binary event contracts (YES/NO). Example:
- "Will BTC be above $100K on April 15?"
- YES is priced at 55¢, NO at 45¢
- Spread: 5¢

The bot:
1. Scans 200+ active markets for wide spreads
2. Places buy orders on both sides (buy YES low, buy NO low)
3. When both sides fill, captures the spread as profit
4. **Maker fees are ZERO** — every cent of spread is profit

It runs across 20+ markets simultaneously, making small profits on each.

**Expected returns:**
- Conservative: 10-20% annually
- Moderate: 20-40% annually
- Aggressive: 40%+ (higher risk, more markets, tighter spreads)

---

## Running 24/7

### Option A: DigitalOcean Droplet ($6/month, easiest)
1. Create account at [digitalocean.com](https://digitalocean.com)
2. Create a Droplet (Ubuntu, $6/month)
3. SSH in:
   ```bash
   git clone https://github.com/jonahkaner/whiteshark.git
   cd whiteshark
   pip install -e .
   cp config.example.yaml config.yaml
   # Edit config.yaml with your keys
   screen -S quicksand
   python -m quicksand.web.run
   # Press Ctrl+A then D to detach
   ```
4. Bookmark `http://your-droplet-ip:8000` on your phone

### Option B: Docker
```bash
docker compose up -d
```

---

## Safety Notes

- **Start with demo mode** (play money) — always test first
- The bot has a **circuit breaker** — stops trading at 2% daily loss
- You'll get **Telegram alerts** if anything goes wrong
- Kalshi is CFTC-regulated — your funds are protected
- Maximum $25,000 per contract position (Kalshi limit)
- The bot uses **zero leverage** — you can't lose more than you deposit

---

## Quick Reference

| What | Command |
|------|---------|
| Start dashboard | `python -m quicksand.web.run` |
| View on phone | `http://server-ip:8000` |
| Paper mode | Set `mode: paper` in config |
| Live mode | Set `mode: live` and `demo: false` |
