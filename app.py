#!/usr/bin/env python3
"""
NIFTY Master Index Hybrid Bot Web Server (FastAPI)
Deploys Strategy 3 (Master Hybrid: Filtered ORB Options + Futures Trend) as a 24/7 cloud service.

Features:
- FastAPI responds to Render & UptimeRobot health checks immediately
- Autonomous daily trading loop in background thread (08:50 AM to 15:35 IST)
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
        bot_status["status"] = "sleeping"
        bot_status["market_status"] = "Weekend"
        add_log("📅 Weekend - Market closed")
        return True

    # After hours check — clear any stale error status
    if now > market_close:
        bot_status["status"] = "sleeping"
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
    bot_instance.is_running = True

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

    eod_summary_sent = False
    try:
        bot_instance.load_market_metadata()
        bot_instance.fetch_historical()
        bot_status["candles_loaded"] = len(bot_instance.trader.candles)

        if bot_instance.telegram:
            bot_instance.telegram.notify_bot_start(["NIFTY 50 (Master Hybrid)"], capital_tracker=bot_instance.capital_tracker)

        bot_instance.start_live_feed()

        heartbeat_sent = False
        # Run until 15:35 IST regardless of WebSocket timing (prevents early exit at 09:15)
        session_end = now_ist().replace(hour=15, minute=35, second=0, microsecond=0)

        while now_ist() < session_end:
            now = now_ist()
            bot_status["active_positions"] = len(bot_instance.trader.positions)
            bot_status["daily_pnl"] = sum(t.net_pnl for t in bot_instance.trader.closed_trades)

            # 1. Mid-Day Heartbeat at 12:00 PM IST (fires once)
            if not heartbeat_sent and now.hour == 12 and now.minute >= 0:
                if bot_instance.telegram:
                    status_dict = {"NIFTY": bot_instance.trader.get_diagnostics()}
                    bot_instance.telegram.notify_midday_heartbeat(
                        status_dict,
                        bot_instance.trader.tick_count,
                        len(bot_instance.trader.positions)
                    )
                heartbeat_sent = True

            # 2. Tick-Starvation Watchdog (two cases handled):
            #    A) Had ticks before, now silent for >5 mins → restart
            #    B) Never got any tick, and feed has been up for >10 mins → restart
            #    C) on_noreconnect set _needs_restart flag → restart from main thread
            if bot_instance.is_market_open():
                last_tick = getattr(bot_instance, "_last_tick_time", None)
                feed_start = getattr(bot_instance, "_feed_start_time", None)

                needs_restart = getattr(bot_instance, "_needs_restart", False)
                restart_reason = ""

                if needs_restart:
                    restart_reason = "WebSocket exhausted reconnect attempts (flag set by on_noreconnect)"
                elif last_tick is not None and (now - last_tick).total_seconds() > 300:
                    needs_restart = True
                    restart_reason = "no ticks for 5+ minutes (feed dropped)"
                elif last_tick is None and feed_start is not None and (now - feed_start).total_seconds() > 600:
                    needs_restart = True
                    restart_reason = "no ticks received in first 10 mins (feed never connected)"

                if needs_restart:
                    add_log(f"Watchdog triggered: {restart_reason} — restarting WebSocket...")
                    try:
                        bot_instance._restart_ticker()
                        add_log("WebSocket feed restarted by watchdog.")
                    except Exception as wd_err:
                        add_log(f"Watchdog restart failed: {wd_err}")

            # 3. Time-based forced square-off at 15:20 IST (safety net)
            if now.hour == 15 and now.minute >= 20 and now.minute < 25:
                for pos in list(bot_instance.trader.positions):
                    add_log(f"⏰ Force-closing open NIFTY position {pos.tradingsymbol} at 15:20 (time-based safety)")
                    try:
                        bot_instance.trader._close_position(pos, pos.entry_price, "EOD_FORCE_CLOSE")
                    except Exception as sq_err:
                        add_log(f"Force square-off error: {sq_err}")

            # 4. EOD Summary at 15:31 IST (fires once, inside loop — survives loop exit or restart)
            if not eod_summary_sent and now.hour == 15 and now.minute >= 31:
                add_log("15:31 IST — generating EOD summary...")
                try:
                    bot_instance.generate_report()
                    eod_summary_sent = True
                except Exception as eod_err:
                    add_log(f"EOD summary error: {eod_err}")

            time.sleep(5)

        add_log("Market session window closed.")
        
        # Ticker cleanup — prevent thread leaks
        bot_instance.is_running = False
        try:
            if bot_instance.ticker:
                bot_instance.ticker.close()
                add_log("WebSocket ticker closed cleanly.")
        except Exception as tc_err:
            add_log(f"Ticker cleanup warning: {tc_err}")

        if not eod_summary_sent:
            add_log("Sending delayed EOD summary...")
            bot_instance.generate_report()
        bot_status["status"] = "day_complete"
        bot_status["market_status"] = "Market Closed"
        return True

    except Exception as e:
        add_log(f"Session error: {e}")
        traceback.print_exc()
        # Clean up ticker on session error to prevent thread leaks
        try:
            if bot_instance:
                bot_instance.is_running = False
                if bot_instance.ticker:
                    bot_instance.ticker.close()
                    add_log("WebSocket ticker closed after session error.")
        except Exception as cleanup_err:
            add_log(f"Error during ticker cleanup: {cleanup_err}")

        # Notify via Telegram if possible so user knows something failed
        try:
            if bot_instance and bot_instance.telegram:
                bot_instance.telegram.send_message(
                    f"<b>NIFTY Bot Session Error</b>\n\n"
                    f"<code>{str(e)[:300]}</code>\n\n"
                    f"<i>Bot will retry in 15 minutes.</i>"
                )
        except Exception:
            pass

        # Try EOD summary only if market closed and summary wasn't sent yet
        try:
            now = now_ist()
            if (now.hour > 15 or (now.hour == 15 and now.minute >= 30)) and bot_instance and not eod_summary_sent:
                bot_instance.generate_report()
                eod_summary_sent = True
        except Exception:
            pass
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
                bot_status["status"] = "error_retry"
                time.sleep(900)
                # After retry wait, clear the error status before attempting next day
                bot_status["status"] = "sleeping"
                bot_status["error"] = None
                continue

            add_log("💤 Session complete. Sleeping until 08:45 AM tomorrow...")
            bot_status["status"] = "sleeping"
            next_morning = (now_ist() + timedelta(days=1)).replace(hour=8, minute=45, second=0)
            while now_ist() < next_morning:
                time.sleep(300)

        except Exception as e:
            add_log(f"❌ Daemon loop error: {e}")
            traceback.print_exc()
            bot_status["status"] = "sleeping"
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
    yield
    add_log("🛑 FastAPI shutting down...")


app = FastAPI(
    title="NIFTY Master Index Hybrid Bot",
    description="Strategy 3: Filtered ORB Options + Futures Trend Following - Autonomous Daily Runner",
    version="3.1.0",
    lifespan=lifespan
)


@app.get("/", response_class=PlainTextResponse)
@app.head("/")
async def root():
    bot_status["last_health_check"] = now_ist().isoformat()
    st = bot_status.get("status", "unknown")
    day = bot_status.get("trading_day", "N/A")
    pnl = bot_status.get("daily_pnl", 0.0)
    cap = bot_instance.capital_tracker.session_capital if bot_instance and hasattr(bot_instance, "capital_tracker") else STRATEGY_CEILING
    text = f"NIFTY Bot: {st} | Day: {day} | Capital: ₹{cap:,.0f} | Daily P&L: ₹{pnl:+,.2f}"
    status_code = 503 if st in ("error", "error_retry") else 200
    return PlainTextResponse(content=text, status_code=status_code)


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
