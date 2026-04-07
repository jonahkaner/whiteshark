# Quicksand Setup Guide

Get the bot running in ~15 minutes. You need two things: a crypto exchange account (where the money lives) and a server to run the bot.

---

## Step 1: Create a Crypto Exchange Account (5 min)

The bot needs an exchange account to trade on. Pick ONE:

### Option A: Bybit (Recommended — easiest for US users)
1. Go to [bybit.com](https://www.bybit.com) and create an account
2. Complete identity verification (KYC)
3. Deposit USDT:
   - Buy USDT with credit card on Bybit, OR
   - Transfer USDT from Coinbase/another exchange
4. Create API keys:
   - Go to **Account → API Management → Create New Key**
   - Name it "quicksand"
   - Permissions: **Read + Trade** (do NOT enable Withdraw)
   - Save the **API Key** and **Secret Key** — you'll need these

### Option B: Binance (Largest exchange, best liquidity)
1. Go to [binance.com](https://www.binance.com) and create an account
2. Complete identity verification
3. Deposit USDT (bank transfer, card, or crypto transfer)
4. Create API keys:
   - Go to **Account → API Management → Create API**
   - Permissions: **Enable Spot & Margin Trading + Enable Futures**
   - **IP restrict recommended** (add your server's IP)
   - Save the API Key and Secret

### Option C: OKX
1. Go to [okx.com](https://www.okx.com) and create an account
2. Verify identity, deposit USDT
3. Create API keys under **Settings → API**

**How much to start with?**
- Minimum: $1,000 (enough to test with real trades)
- Recommended: $5,000-$10,000 (enough for meaningful returns)
- The bot starts in **paper mode** (fake money) so you can test first

---

## Step 2: Set Up Telegram Alerts (3 min)

Get trade notifications on your phone:

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the prompts to create a bot
3. BotFather gives you a **token** like `123456:ABCdefGHIjklMNO` — save it
4. Open your new bot in Telegram and send it any message (like "hi")
5. Visit this URL in your browser (replace YOUR_TOKEN):
   ```
   https://api.telegram.org/botYOUR_TOKEN/getUpdates
   ```
6. Find `"chat":{"id":XXXXXXXX}` in the response — that's your **chat_id**

---

## Step 3: Configure the Bot (2 min)

1. Copy the example config:
   ```bash
   cp config.example.yaml config.yaml
   ```

2. Edit `config.yaml` with your values:
   ```yaml
   mode: paper  # Start with paper! Change to "live" when ready

   exchanges:
     bybit:                              # or "binance" or "okx"
       api_key: "your-api-key-here"
       secret: "your-secret-key-here"
       sandbox: false                     # false = real exchange

   alerts:
     telegram:
       enabled: true
       bot_token: "your-telegram-bot-token"
       chat_id: "your-chat-id"
   ```

---

## Step 4: Run the Bot (1 min)

```bash
# Install dependencies
pip install -e .

# Test connection (connects, shows balance, exits)
quicksand --config config.yaml --dry-run

# Start in paper mode (simulated trades, no real money)
quicksand --config config.yaml --paper

# Start the web dashboard (access from phone!)
python -m quicksand.web.run
# Then open http://your-server-ip:8000 on your phone
```

---

## Step 5: Go Live

Once you've run paper mode for a few days and are happy:

1. Edit `config.yaml`:
   ```yaml
   mode: live
   ```

2. Start with a small amount ($1,000) to verify real trades work
3. Scale up gradually: $1K → $5K → $10K → $50K

---

## Running 24/7 (Server Options)

The bot needs to run continuously. Options from easiest to most robust:

### Option A: DigitalOcean Droplet ($6/month)
1. Create account at [digitalocean.com](https://www.digitalocean.com)
2. Create a Droplet (Ubuntu, $6/month Basic)
3. SSH in, clone the repo, install Python, run the bot
4. Use `screen` or `tmux` to keep it running

### Option B: Docker (any VPS)
```bash
docker compose up -d
```

### Option C: Railway / Render (easiest, no server management)
1. Connect your GitHub repo
2. Set environment variables for API keys
3. Deploy — it runs automatically

---

## Safety Notes

- **Start with paper mode** — always test before using real money
- **Never enable withdrawal permissions** on your API keys
- **IP-restrict your API keys** if your exchange supports it
- The bot has a **circuit breaker** — if it loses 2% in a day, it stops automatically
- You'll get a **Telegram alert** if anything goes wrong

---

## Quick Reference

| Command | What it does |
|---------|-------------|
| `quicksand --dry-run` | Test exchange connection |
| `quicksand --paper` | Run with fake money |
| `quicksand` | Run live (uses config.yaml mode) |
| `python -m quicksand.web.run` | Start web dashboard |
| Open `http://server:8000` | View dashboard on phone |
