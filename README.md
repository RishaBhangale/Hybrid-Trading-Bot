# NIFTY Hybrid Algorithmic Trading Engine

Autonomous algorithmic trading bot for **NIFTY 50** combining **Filtered Opening Range Breakouts (ATM Options)** with **Trend Following (Futures)**. Designed for 24/7 cloud execution on **Render** (monitored via **UptimeRobot**), headless Zerodha Kite Connect 2FA auto-login, strict capital reserve management, and real-time Telegram alerting.

---

## 🎯 System Architecture & Strategy

The engine runs a dual-module hybrid framework to trade both morning breakout momentum and intraday trend extensions:

| Component | Specification | Description |
| :--- | :--- | :--- |
| **Instrument Focus** | NIFTY 50 (Index) | Traded via ATM Options (CE/PE) & Futures |
| **Execution Timeframe** | 15-Minute Candles | Built from real-time WebSocket tick stream |
| **Module A: Opening Range Breakout** | 15M ORB (09:15–09:30) | Triggers on confirmed breakout with Volume Surge ($V > 1.2 \times \text{MA}_{20}$) & ATR width filters |
| **Module B: Trend Following** | 50 EMA / 200 EMA + 20-Donchian | Triggers when 50 EMA is aligned with 200 EMA and price breaks the 20-period Donchian channel |
| **Dynamic Risk Management** | ATR Trailing Stop-Loss | 1.5× ATR trailing SL for Options; 2.0× ATR trailing SL for Futures |
| **Intraday Square-Off** | 15:15 IST Auto Square-Off | Eliminates overnight gap and black-swan risk |

---

## 💰 Capital Allocation Rules (Strict Risk Buckets)

The system operates under strict capital protection rules:

| Capital Bucket | Allocation | Purpose |
| :--- | :---: | :--- |
| **Total Account Equity** | **₹3,00,000** | Full trading account corpus basis |
| **Strategy Allocation Ceiling** | **₹1,80,000** | Maximum normal deployment across Futures + Options |
| **Protected Reserve** | **₹1,20,000** | **Strictly unallocated** — shields account against adverse regimes |
| **Emergency Buffer** | Part of Reserve | Contingency buffer for margin fluctuations |

---

## 🛠️ Autonomous Daily Lifecycle

Runs autonomously without requiring manual intervention:

```
 08:50 AM IST ───► Headless Auto-Login (Refreshes Zerodha Kite token in <1 sec via HTTP + 2FA TOTP)
 09:14 AM IST ───► Pre-Market Setup (Connects live WebSocket tick stream for NIFTY 50)
 09:15 AM IST ───► Market Opens (Builds 15M ORB, tracks EMA 50/200, Donchian, & ATR)
 09:30–15:15  ───► Live Execution (Evaluates entries, manages dynamic trailing SLs, sends Telegram alerts)
 03:15 PM IST ───► Auto Square-Off (Closes open intraday positions)
 03:30 PM IST ───► EOD Reporting (Sends P&L breakdown to Telegram, writes JSON logs)
 03:31 PM IST ───► Standby Mode (Sleeps until 08:50 AM next trading morning)
```

---

## 🚀 Deployment Guide

### Option 1: Cloud Deployment (Render + UptimeRobot)

1. **Push to GitHub**:
   ```bash
   git add .
   git commit -m "feat(nifty-bot): update production configuration"
   git push origin main
   ```
2. **Deploy on Render**:
   * Create a **New Web Service** $\to$ Connect repository $\to$ Runtime: **Docker**.
   * Add Environment Variables:
     * `KITE_API_KEY`, `KITE_API_SECRET`, `KITE_USER_ID`, `KITE_PASSWORD`, `KITE_TOTP_SECRET`
     * `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
     * `INDEX_STRATEGY` = `HYBRID`
     * `PAPER_TRADING` = `true` (or `false` for live execution)
3. **Keep Awake via UptimeRobot**:
   * Create a free HTTP monitor at [UptimeRobot.com](https://uptimerobot.com) pointing to `https://<your-render-app>.onrender.com/ping` at 5-minute intervals.

---

### Option 2: Local Mac / VPS Runner

Run locally in the background (surviving terminal closure / sleep):
```bash
caffeinate -dis nohup python3 run_index_bot.py > index_bot_output.log 2>&1 &
```

* **Monitor live output**:
  ```bash
  tail -f index_bot_output.log
  ```
* **Stop bot**:
  ```bash
  pkill -f run_index_bot.py
  ```

---

## 📱 Telegram Notifications

* **Bot Initialization**: Confirms Kite login, symbols loaded, and active capital buckets.
* **Trade Entry**: Alerts direction (CALL / PUT / LONG / SHORT), entry price, dynamic SL, and quantity.
* **Trailing Stop Updates**: Real-time notifications when trailing stop locks in profit.
* **Trade Exit**: Realized points captured, exit reason, and net trade P&L.
* **EOD Summary**: Daily win rate, trade count, and net realized P&L.

---

## 📁 Repository Structure

```
index-strategies-lab/
├── app.py                         # FastAPI server + keepalive pinger for Render
├── main_index.py                  # Core Hybrid Execution Engine & Capital Management
├── run_index_bot.py               # Standalone runner for local/VPS execution
├── auto_login.py                  # Headless HTTP + 2FA TOTP Zerodha auto-login
├── telegram_notifier.py           # Real-time Telegram alerting engine
├── tradingview_index_strategies.pine # TradingView Pine Script v5 Visualizer
├── Dockerfile                     # Docker container configuration
├── render.yaml                    # Render cloud infrastructure blueprint
├── requirements.txt               # Production Python dependencies
├── .env.example                   # Environment variable template
└── logs/                          # Daily JSON execution logs
```
