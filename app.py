#!/usr/bin/env python3
"""
NIFTY Master Index Hybrid Bot Web Server (FastAPI)
Deploys Strategy 3 (Master Hybrid: Filtered ORB Options + Futures Trend) as a 24/7 cloud service for Render free tier.

Features:
- FastAPI responds to Render & UptimeRobot health checks immediately
- Autonomous daily trading loop in background thread (08:50 AM to 15:30 PM IST)
- Built-in keepalive self-pinger to prevent 15-minute Render free-tier sleep
- Real-time Telegram alerting on entries, exits, trailing SLs, and daily EOD summary
- Strict Capital Management: ₹3.0L Total Equity, ₹1.8L Strategy Ceiling, ₹1.2L Protected Reserve
"""

import os
import sys
import threading
import traceback
import time
from pathlib import Path
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse, JSONResponse

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from main_index import IndexOptionsBot, STRATEGY_MODE, PAPER_TRADING, now_ist, TOTAL_EQUITY, STRATEGY_CEILING, PROTECTED_RESERVE
from auto_login import KiteAutoLogin, load_credentials

# Global state
bot_instance = None
bot_thread = None
bot_logs = []

bot_status = {
    "status": "initialized",
    "strategy": STRATEGY_MODE,
    "paper_trading": PAPER_TRADING,
    "started_at": None,
    "last_health_check": None,
    "authenticated": False,
    "kite_user": None,
    "error": None,
    "market_status": None,
    "candles_loaded": 0,
    "active_positions": 0,
    "daily_pnl": 0.0,
    "capital": {
        "total_equity": TOTAL_EQUITY,
        "strategy_ceiling": STRATEGY_CEILING,
        "protected_reserve": PROTECTED_RESERVE
    },
    "trading_day": None,
    "days_run": 0
}


def add_log(message: str):
    """Add log message with timestamp."""
    timestamp = now_ist().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    print(log_entry, flush=True)
    bot_logs.append(log_entry)
    if len(bot_logs) > 250:
        bot_logs.pop(0)


def keepalive_pinger():
    """Background thread that pings RENDER_EXTERNAL_URL every 8 minutes to prevent Render free-tier sleep."""
    import requests
    render_url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("SELF_PING_URL")
    if not render_url:
        add_log("ℹ️ No RENDER_EXTERNAL_URL detected; use UptimeRobot for external pinging if on Render free tier.")
        return
        
    ping_url = render_url.rstrip("/") + "/ping"
    add_log(f"⏰ Render Keep-Alive self-pinger active: pinging {ping_url} every 8 minutes...")
    
    while True:
        try:
            time.sleep(480)  # 8 minutes
            now = now_ist()
            if now.weekday() < 5 and (8 <= now.hour < 16):
                r = requests.get(ping_url, timeout=10)
                if r.status_code == 200:
                    add_log("💓 Keep-alive self-ping sent to Render router (container awake)")
        except Exception as e:
            add_log(f"⚠️ Keep-alive ping warning: {e}")


def run_single_trading_day() -> bool:
    """Run a single trading day session."""
    global bot_instance, bot_status
    
    today = now_ist().strftime("%Y-%m-%d")
    bot_status["trading_day"] = today
    bot_status["days_run"] += 1
    
    add_log(f"📅 Starting NIFTY trading day: {today} (Day #{bot_status['days_run']})")
    
    now = now_ist()
    login_time = now.replace(hour=8, minute=50, second=0, microsecond=0)
    market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    
    # Weekend check
    if now.weekday() >= 5:
        bot_status["market_status"] = "Weekend"
        add_log("📅 Weekend - Market closed")
        return True
        
    # After hours check
    if now > market_close:
        bot_status["market_status"] = "After Hours"
        add_log("📅 After market hours - waiting for tomorrow")
        return True
        
    # Wait for login time (8:50 AM)
    if now < login_time:
        mins = int((login_time - now).total_seconds() / 60)
        add_log(f"⏰ Waiting {mins} mins until 08:50 AM login time...")
        bot_status["status"] = "waiting_for_login_time"
        while now_ist() < login_time:
            time.sleep(60)
            
    # Fresh Authentication
    add_log("🔐 Performing automated Kite Connect login...")
    bot_status["status"] = "authenticating"
    
    bot_instance = IndexOptionsBot()
    if not bot_instance.authenticate():
        add_log("❌ Kite authentication failed!")
        bot_status["status"] = "error"
        bot_status["error"] = "Auth failed"
        return False
        
    bot_status["authenticated"] = True
    bot_status["status"] = "waiting_for_market"
    
    # Wait for market open
    now = now_ist()
    if now < market_open:
        mins = int((market_open - now).total_seconds() / 60)
        add_log(f"⏳ Waiting {mins} mins for market open at 09:15 AM...")
        while now_ist() < market_open:
            time.sleep(30)
            
    # Start Trading Session
    add_log("📊 Starting NIFTY Master Hybrid trading session...")
    bot_status["status"] = "running"
    bot_status["market_status"] = "Market Open"
    
    try:
        bot_instance.load_market_metadata()
        bot_instance.fetch_historical()
        bot_status["candles_loaded"] = len(bot_instance.trader.candles)

        if bot_instance.telegram:
            bot_instance.telegram.notify_bot_start(["NIFTY 50 (Master Hybrid)"])

        bot_instance.start_live_feed()

        heartbeat_sent = False
        # Run until 15:35 IST regardless of WebSocket timing (prevents early exit at 09:15)
        session_end = now_ist().replace(hour=15, minute=35, second=0, microsecond=0)

        while now_ist() < session_end:
            now = now_ist()
            bot_status["active_positions"] = len(bot_instance.trader.positions)
            bot_status["daily_pnl"] = sum(t.net_pnl for t in bot_instance.trader.closed_trades)

            # Mid-Day Heartbeat at 12:00 PM IST (fires once)
            if not heartbeat_sent and now.hour == 12 and now.minute >= 0:
                if bot_instance.telegram:
                    status_dict = {"NIFTY": bot_instance.trader.get_diagnostics()}
                    bot_instance.telegram.notify_midday_heartbeat(
                        status_dict,
                        bot_instance.trader.tick_count,
                        len(bot_instance.trader.positions)
                    )
                heartbeat_sent = True

            time.sleep(5)

        add_log("🏁 Market closed. Concluding session...")
        bot_instance.generate_report()
        bot_status["status"] = "day_complete"
        bot_status["market_status"] = "Market Closed"
        return True

    except Exception as e:
        add_log(f"❌ Session error: {e}")
        traceback.print_exc()
        bot_status["status"] = "error"
        bot_status["error"] = str(e)
        return False


def run_trading_bot():
    """Main daemon loop running day after day."""
    add_log("🚀 NIFTY Bot Daemon started.")
    
    while True:
        try:
            success = run_single_trading_day()
            if not success:
                add_log("⚠️ Session failed. Retrying in 15 minutes...")
                time.sleep(900)
                continue
                
            add_log("💤 Session complete. Sleeping until 08:45 AM tomorrow...")
            next_morning = (now_ist() + timedelta(days=1)).replace(hour=8, minute=45, second=0)
            while now_ist() < next_morning:
                if now_ist().weekday() >= 5:
                    break
                time.sleep(300)
                
        except Exception as e:
            add_log(f"❌ Daemon loop error: {e}")
            time.sleep(60)


def start_bot_thread():
    """Start bot in background thread."""
    global bot_thread
    if bot_thread is not None and bot_thread.is_alive():
        return
    bot_thread = threading.Thread(target=run_trading_bot, name="IndexBotDaemon", daemon=True)
    bot_thread.start()
    add_log("✅ Index bot background daemon thread launched.")


# FastAPI App
@asynccontextmanager
async def lifespan(app: FastAPI):
    add_log("🌐 FastAPI initializing...")
    start_bot_thread()
    pinger = threading.Thread(target=keepalive_pinger, name="KeepAlivePinger", daemon=True)
    pinger.start()
    yield
    add_log("🛑 FastAPI shutting down...")


app = FastAPI(
    title="NIFTY Master Index Hybrid Bot",
    description="Strategy 3: Filtered ORB Options + Futures Trend Following - Autonomous Daily Runner",
    version="3.0.0",
    lifespan=lifespan
)


@app.get("/", response_class=PlainTextResponse)
@app.head("/")
async def root():
    bot_status["last_health_check"] = now_ist().isoformat()
    st = bot_status.get("status", "unknown")
    day = bot_status.get("trading_day", "N/A")
    pnl = bot_status.get("daily_pnl", 0.0)
    return f"NIFTY Bot: {st} | Day: {day} | Strategy: {STRATEGY_MODE} | Daily P&L: ₹{pnl:+,.2f}"


@app.get("/ping", response_class=PlainTextResponse)
@app.head("/ping")
async def ping():
    return "pong"


@app.get("/status")
async def status():
    global bot_instance, bot_status
    res = {
        "bot": bot_status.copy(),
        "current_time": now_ist().isoformat(),
        "market_open": bot_instance.is_market_open() if bot_instance else False
    }
    if bot_instance and hasattr(bot_instance, 'trader'):
        trader = bot_instance.trader
        res["positions"] = [{
            "strategy": p.strategy, "type": p.position_type, "entry_price": p.entry_price, "sl": p.trailing_sl, "qty": p.quantity
        } for p in trader.positions]
        res["closed_trades_today"] = len(trader.closed_trades)
        res["net_pnl_today"] = sum(t.net_pnl for t in trader.closed_trades)
    return res


@app.get("/logs")
async def logs():
    return {"logs": bot_logs[-50:], "count": len(bot_logs)}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    add_log(f"🌐 Starting FastAPI server on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
