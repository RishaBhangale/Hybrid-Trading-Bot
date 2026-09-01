# NIFTY Hybrid Algorithmic Trading Engine

Autonomous algorithmic trading engine for **NIFTY 50** combining **Filtered Opening Range Breakouts (ATM Options)** with **Trend Following (Futures)**. Built for 24/7 cloud execution on **Render** (monitored via **UptimeRobot**), sub-second headless Zerodha Kite Connect 2FA auto-login, real exchange ATM option quotes, dynamic lot size resolution (Lot Size: 65), strict capital reserve management, and automated Telegram alerting.

---

## 🎯 System Architecture & Strategy

The engine runs a dual-module hybrid framework to capture both morning breakout momentum and intraday trend extensions:

| Component | Specification | Description |
| :--- | :--- | :--- |
| **Instrument Focus** | NIFTY 50 (Index) | Traded via Real ATM Options (CE/PE) & Futures |
| **Execution Timeframe** | 15-Minute Candles | Aggregated in real-time from WebSocket tick stream |
| **Module A: Opening Range Breakout** | 15M ORB (09:15–09:30) | Triggers on confirmed breakout with Volume Surge ($V > 1.2 \times \text{MA}_{20}$) & ATR width filters |
| **Module B: Trend Following** | 50 EMA / 200 EMA + 20-Donchian | Triggers when 50 EMA aligns with 200 EMA and price breaks the 20-period Donchian channel |
| **Real Market Quotes** | Live Kite API LTP | Resolves exact ATM option contract (`NFO:NIFTY...CE/PE`) from Kite; exits on real option LTP |
| **Dynamic Lot Size** | Auto-Loaded on Startup | Dynamically pulls current NIFTY lot size (65) from Kite API |
| **Dynamic Risk Management** | ATR Trailing Stop-Loss | 25% Stop-Loss for Options; 2.0× ATR dynamic trailing SL for Futures |
| **Intraday Square-Off** | 15:15 IST Auto Square-Off | Eliminates overnight gap and black-swan risk |

---

## 💰 Capital Allocation Rules (Strict Risk Buckets)

The system operates under strict capital protection rules:

| Capital Bucket | Allocation | Purpose |
| :--- | :---: | :--- |
| **Total Account Equity** | **₹3,00,000** | Full trading account corpus basis |
| **Strategy Allocation Ceiling** | **₹1,80,000** | Maximum normal deployment across Futures (~₹1.15L margin) + Options buffer |
| **Protected Reserve** | **₹1,20,000** | **Strictly unallocated** — shields account against adverse regimes |
| **Emergency Buffer** | Part of Reserve | Contingency buffer for margin fluctuations |

---

## 🛠️ Autonomous Daily Lifecycle

Runs autonomously without requiring manual intervention:

```
 08:50 AM IST ───► Headless Auto-Login (Refreshes Zerodha Kite token in <1 sec via HTTP + 2FA TOTP)
 09:14 AM IST ───► Pre-Market Setup (Downloads live NIFTY lot size & spot token from Kite API)
 09:15 AM IST ───► Market Opens (Builds 15M ORB, tracks EMA 50/200, Donchian, & ATR)
 09:30–15:15  ───► Live Execution (Evaluates entries, manages dynamic trailing SLs, sends Telegram alerts)
 12:00 PM IST ───► Mid-Day Heartbeat (Sends live container health, tick counts, and trend status to Telegram)
 03:15 PM IST ───► Auto Square-Off (Closes open intraday positions)
 03:30 PM IST ───► EOD Reporting (Sends P&L & Filter Diagnostic Matrix to Telegram, writes JSON logs)
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

* **Bot Initialization**: Confirms Kite login, NIFTY spot token, lot size (65), and active capital buckets.
* **Trade Entry**: Alerts direction (CALL / PUT / LONG / SHORT), contract symbol, entry price, dynamic SL, and quantity.
* **Trailing Stop Updates**: Real-time notifications when trailing stop locks in profit.
* **Mid-Day Heartbeat (12:00 PM)**: Confirms container health, total ticks processed, and NIFTY trend status.
* **Trade Exit**: Realized exit price, points captured, exit reason, and net realized P&L.
* **EOD Diagnostic Summary**: Complete P&L breakdown plus the **Daily Filter Matrix** showing candle counts, ORB status, and filter telemetry.

---

## 📁 Repository Structure

```
index-strategies-lab/
├── app.py                         # FastAPI web server + keepalive pinger for Render
├── main_index.py                  # Core Hybrid Execution Engine & Capital Management
├── run_index_bot.py               # Standalone runner for local/VPS execution
├── auto_login.py                  # Sub-second headless HTTP + 2FA TOTP Zerodha auto-login
├── telegram_notifier.py           # Real-time Telegram alerting engine
├── tradingview_index_strategies.pine # TradingView Pine Script v5 Visualizer
├── Dockerfile                     # Docker container configuration
├── render.yaml                    # Render cloud infrastructure blueprint
├── requirements.txt               # Production Python dependencies
├── .env.example                   # Environment variable template
└── logs/                          # Daily JSON execution logs
```
