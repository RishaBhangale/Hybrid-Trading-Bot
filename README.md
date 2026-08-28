# 🏛️ NIFTY Master Index Hybrid Bot (Strategy 3)

Autonomous algorithmic trading bot for **NIFTY 50** deploying **Strategy 3 (Master Hybrid)** — combining **Strategy 1B (Filtered 15M ORB on ATM Options)** with **Strategy 2 (Futures Trend Following)**. Built for 24/7 cloud execution on **Render** (kept awake via **UptimeRobot**), sub-second headless Kite Connect 2FA auto-login, strict capital protection rules, and real-time Telegram alerting.

---

## 💰 Capital Allocation Rules (Strict Risk Buckets)

The bot operates under strict capital allocation rules designed to withstand extreme multi-year drawdowns:

| Capital Bucket | Allocation | Purpose |
| :--- | :---: | :--- |
| **Total Equity** | **₹3,00,000** | Entire trading account corpus |
| **Strategy Allocation Ceiling** | **₹1,80,000** | Maximum normal deployment across Futures + Options |
| **Protected Reserve** | **₹1,20,000** | **Never intentionally deployed** — shields account |
| **Emergency Buffer** | Part of Reserve | Contingency buffer for margin fluctuations & multi-day drawdown |

---

## 🎯 Strategy 3: Master Hybrid Architecture

Strategy 3 fuses two non-correlated structural alphas:

```
                                  ┌──────────────────────────────────────────────┐
                                  │             NIFTY 50 Market Open             │
                                  └──────────────────────┬───────────────────────┘
                                                         │
                                    09:15 - 09:30 IST (15M Candle)
                                                         │
                                  ┌──────────────────────▼───────────────────────┐
                                  │      15M Opening Range Established           │
                                  └──────┬────────────────────────────────┬──────┘
                                         │                                │
                 Breakout + Vol Surge + ATR Bounds          50 EMA > 200 EMA + 20 Donchian Breakout
                                         │                                │
                     ┌───────────────────▼───────────┐        ┌───────────▼──────────────────┐
                     │ Strategy 1B: ATM Option Buy   │        │ Strategy 2: NIFTY Futures    │
                     │ (Delta 0.5 ATM CE/PE)         │        │ (1 Lot Trend Following)      │
                     │ SL: 1.5x ATR Trailing Stop    │        │ SL: 2.0x ATR Trailing Stop   │
                     └───────────────────┬───────────┘        └───────────┬──────────────────┘
                                         │                                │
                                         └────────────────┬───────────────┘
                                                          │
                                         ┌────────────────▼───────────────┐
                                         │   15:15 IST Auto Square-Off    │
                                         │   15:30 IST EOD Telegram Rep   │
                                         └────────────────────────────────┘
```

---

## 🏆 Backtest & Robustness Summary (5.5 Years: 2021 – 2026)

* **Net Post-Tax Profit**: 🏆 **₹+66,98,583.52** (includes all Zerodha & NSE charges)
* **Profit Factor**: **8.61** (Walk-Forward OOS Profit Factor: **4.73**)
* **CAGR**: **97.7%**
* **Win Rate**: **45.8%** | **Win/Loss Ratio**: **10.20x** (Avg Win: ₹21,229 vs Avg Loss: ₹2,081)
* **Max Historical Drawdown**: **₹40,219.66 (5.86%)**
* **10,000-Run Monte Carlo Risk of Ruin**: **2.11%** (100% of simulations ended net profitable)

---

## 🛠️ Autonomous Cloud Deployment (Render + UptimeRobot)

No local computer or terminal required. The bot runs 24/7 in the cloud:

```
 08:50 AM IST ───► Headless Auto-Login (Generates fresh Kite token via HTTP + TOTP)
 09:14 AM IST ───► Pre-Market Setup (Subscribes to live NIFTY WebSocket tick feed)
 09:15 AM IST ───► Market Opens (Builds 15M ORB, tracks EMA 50/200, Donchian, & ATR)
 Intraday     ───► Live Order Execution + Instant Telegram Notifications (Entry, Trailing SL, Exit)
 03:15 PM IST ───► Auto Square-Off (Closes intraday positions to eliminate overnight gap risk)
 03:30 PM IST ───► EOD Reporting (Sends P&L breakdown to Telegram, writes logs/)
 03:31 PM IST ───► Standby Mode (Sleeps until 08:50 AM next morning)
```

---

## 🚀 How to Deploy in 3 Steps

### Step 1: Push to your GitHub Repository
```bash
git init
git add .
git commit -m "feat(nifty-bot): deploy Strategy 3 Master Hybrid with FastAPI cloud runner and capital rules"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/nifty-hybrid-bot.git
git push -u origin main
```

### Step 2: Deploy on Render
1. Go to [Render.com](https://dashboard.render.com) $\to$ **New Web Service**.
2. Connect your `nifty-hybrid-bot` repository.
3. Set **Runtime** to `Docker` (or select `render.yaml`).
4. Under **Environment Variables**, add:
   * `KITE_API_KEY`, `KITE_API_SECRET`, `KITE_USER_ID`, `KITE_PASSWORD`, `KITE_TOTP_SECRET`
   * `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
   * `INDEX_STRATEGY` = `HYBRID`
   * `PAPER_TRADING` = `true` (or `false` for live execution)
5. Click **Create Web Service**.

### Step 3: Keep Awake via UptimeRobot (Free 24/7 Monitoring)
1. Go to [UptimeRobot.com](https://uptimerobot.com) (Free).
2. Click **Add New Monitor**:
   * **Monitor Type**: `HTTP(s)`
   * **URL**: `https://<your-render-app-name>.onrender.com/ping`
   * **Interval**: `5 minutes`
3. Click **Create Monitor**. Render will **never sleep** during trading hours.

---

## 📱 Telegram Alerts

* **Bot Start**: `🤖 NIFTY Master Hybrid Bot Initialized | Capital: ₹3.0L (₹1.8L Ceiling / ₹1.2L Reserve)`
* **Trade Entry**: `🟢 NIFTY ENTRY: 1B_ORB_OPTIONS | CALL @ 24,250 | SL: 24,190 | Qty: 50`
* **Trade Exit**: `✅ NIFTY EXIT: 2_FUTURES_TREND | LONG | Pts: +180.0 | Net P&L: ₹+8,850.00`
* **Daily EOD Summary**: Full P&L breakdown, win rate, and total daily net cash.

---

## 📁 Clean Repository Structure

```
index-strategies-lab/
├── app.py                         # FastAPI server + keepalive self-pinger + background daemon
├── main_index.py                  # Core Strategy 3 Master Hybrid Execution Engine
├── run_index_bot.py               # Autonomous daily runner (for local / VPS execution)
├── auto_login.py                  # Headless HTTP + 2FA TOTP Zerodha auto-login
├── telegram_notifier.py           # Real-time Telegram notification engine
├── tradingview_index_strategies.pine # TradingView Pine Script v5 Visualizer
├── Dockerfile                     # Docker container for Render deployment
├── render.yaml                    # Render cloud infrastructure blueprint
├── requirements.txt               # Production Python dependencies
├── .env.example                   # Template for environment variables
└── logs/                          # Daily JSON execution logs
```
