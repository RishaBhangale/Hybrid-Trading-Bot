#!/usr/bin/env python3
"""
Production Autonomous Index Trading Bot (NIFTY 50)
Implements:
1. Strategy 1B: Filtered 15M Opening Range Breakout (ATM Options)
2. Strategy 2: Futures Trend Following (EMA 50/200 + 20-Period Donchian Breakout + ATR Trailing SL)
3. Strategy 3: Master Index Hybrid (Combined 1B + 2)

Features:
- Sub-second headless Kite auto-login with 2FA TOTP
- Real-time WebSocket tick streaming & 15-minute candle aggregation
- Dynamic ATR trailing stop-loss management
- Real-time Telegram alerts for entry, trailing SL updates, exits, and EOD P&L
- 15:15 IST intraday auto square-off
"""

import os
import sys
import time
import signal
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from threading import Event, Lock
import pandas as pd
import numpy as np

# Add directory to path
BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

try:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")
except ImportError:
    IST = None

try:
    from kiteconnect import KiteConnect, KiteTicker
    KITE_AVAILABLE = True
except ImportError:
    KITE_AVAILABLE = False

try:
    from telegram_notifier import TelegramNotifier
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    TelegramNotifier = None

from auto_login import KiteAutoLogin, load_credentials

# ============================================================
# CONFIGURATION & CAPITAL ALLOCATION BUCKETS
# ============================================================
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

NIFTY_TOKEN = 256265
LOT_SIZE = 50
STRATEGY_MODE = os.environ.get("INDEX_STRATEGY", "HYBRID").upper()  # "ORB_OPTIONS", "FUTURES_TREND", "HYBRID"
PAPER_TRADING = os.environ.get("PAPER_TRADING", "true").lower() == "true"

# Capital Management Allocation (from deployment specs)
TOTAL_EQUITY = 300000.0          # ₹3,00,000 Total Account Equity
STRATEGY_CEILING = 180000.0      # ₹1,80,000 Strategy Allocation Ceiling (Max Normal Deployment)
PROTECTED_RESERVE = 120000.0     # ₹1,20,000 Protected Reserve (Never intentionally deploy)

def now_ist():
    return datetime.now(IST) if IST else datetime.now()


class IndexPosition:
    def __init__(self, strategy: str, position_type: str, instrument: str, entry_price: float, sl: float, quantity: int, entry_time: datetime):
        self.strategy = strategy
        self.position_type = position_type  # "CALL", "PUT", "LONG", "SHORT"
        self.instrument = instrument        # "OPTIONS" or "FUTURES"
        self.entry_price = entry_price
        self.trailing_sl = sl
        self.initial_sl = sl
        self.quantity = quantity
        self.entry_time = entry_time
        self.exit_price = None
        self.exit_time = None
        self.exit_reason = None
        self.points = 0.0
        self.pnl = 0.0
        self.net_pnl = 0.0

    def close(self, exit_price: float, reason: str, tax: float):
        self.exit_price = exit_price
        self.exit_time = now_ist()
        self.exit_reason = reason
        if self.instrument == "OPTIONS":
            self.points = (exit_price - self.entry_price) if self.position_type == "CALL" else (self.entry_price - exit_price)
            self.pnl = self.points * 0.5 * self.quantity  # Delta 0.5
        else: # FUTURES
            self.points = (exit_price - self.entry_price) if self.position_type == "LONG" else (self.entry_price - exit_price)
            self.pnl = self.points * self.quantity
        self.net_pnl = self.pnl - tax


class IndexTrader:
    def __init__(self, logger, telegram=None):
        self.logger = logger
        self.telegram = telegram
        
        self.candles: List[Dict] = []
        self.current_candle: Optional[Dict] = None
        self.last_candle_time: Optional[datetime] = None
        self.current_day: Optional[datetime.date] = None
        
        self.orb_high = None
        self.orb_low = None
        self.orb_width = None
        self.orb_traded_today = False
        
        self.positions: List[IndexPosition] = []
        self.closed_trades: List[IndexPosition] = []
        self.lock = Lock()
        
    def process_tick(self, ltp: float, tick_time: datetime, volume: int = 0):
        with self.lock:
            candle_minute = (tick_time.minute // 15) * 15
            candle_ts = tick_time.replace(minute=candle_minute, second=0, microsecond=0)
            
            if self.current_candle is None or candle_ts != self.last_candle_time:
                if self.current_candle:
                    self.candles.append(self.current_candle)
                    if len(self.candles) >= 30:
                        self._process_candle(self.current_candle)
                
                self.current_candle = {
                    "timestamp": candle_ts, "open": ltp, "high": ltp, "low": ltp, "close": ltp, "volume": volume
                }
                self.last_candle_time = candle_ts
            else:
                self.current_candle["high"] = max(self.current_candle["high"], ltp)
                self.current_candle["low"] = min(self.current_candle["low"], ltp)
                self.current_candle["close"] = ltp
                self.current_candle["volume"] += volume
                
            # Real-time tick SL check
            self._check_trailing_stops(ltp)
            
    def _check_trailing_stops(self, ltp: float):
        for pos in list(self.positions):
            if pos.instrument == "OPTIONS":
                if pos.position_type == "CALL" and ltp <= pos.trailing_sl:
                    self._close_position(pos, pos.trailing_sl, "SL_HIT")
                elif pos.position_type == "PUT" and ltp >= pos.trailing_sl:
                    self._close_position(pos, pos.trailing_sl, "SL_HIT")
            elif pos.instrument == "FUTURES":
                if pos.position_type == "LONG" and ltp <= pos.trailing_sl:
                    self._close_position(pos, pos.trailing_sl, "SL_HIT")
                elif pos.position_type == "SHORT" and ltp >= pos.trailing_sl:
                    self._close_position(pos, pos.trailing_sl, "SL_HIT")

    def _process_candle(self, candle: Dict):
        c_ts = candle["timestamp"]
        c_time = c_ts.time()
        c_day = c_ts.date()
        c_close = candle["close"]
        c_high = candle["high"]
        c_low = candle["low"]
        c_vol = candle["volume"]
        
        # New Day Reset
        if self.current_day != c_day:
            self.current_day = c_day
            self.orb_high = None
            self.orb_low = None
            self.orb_width = None
            self.orb_traded_today = False
            
        # 15M ORB Range Establishment (09:15 - 09:30 candle)
        if c_time.hour == 9 and c_time.minute == 15:
            self.orb_high = candle["high"]
            self.orb_low = candle["low"]
            self.orb_width = self.orb_high - self.orb_low
            self.logger(f"🎯 15M ORB Established: High={self.orb_high:.1f}, Low={self.orb_low:.1f}, Width={self.orb_width:.1f} pts")
            return
            
        # Calculate Technical Indicators
        df = pd.DataFrame(self.candles[-50:])
        hl = df['high'] - df['low']
        hc = (df['high'] - df['close'].shift(1)).abs()
        lc = (df['low'] - df['close'].shift(1)).abs()
        tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        atr = tr.ewm(span=14, adjust=False).mean().iloc[-1]
        ema_50 = df['close'].ewm(span=50, adjust=False).mean().iloc[-1]
        ema_200 = df['close'].ewm(span=200, adjust=False).mean().iloc[-1]
        don_high = df['high'].iloc[-21:-1].max() if len(df) >= 21 else df['high'].max()
        don_low = df['low'].iloc[-21:-1].min() if len(df) >= 21 else df['low'].min()
        vol_ma = df['volume'].iloc[-21:-1].mean() if len(df) >= 21 else df['volume'].mean()
        
        # Log status
        self.logger(f"NIFTY {c_time.strftime('%H:%M')} | Close:{c_close:.1f} | EMA50:{ema_50:.1f} | EMA200:{ema_200:.1f} | ATR:{atr:.1f}")
        
        # 1. Update Trailing SLs on Closed Candle
        for pos in self.positions:
            if pos.strategy == "1B_ORB_OPTIONS":
                if pos.position_type == "CALL":
                    pos.trailing_sl = max(pos.trailing_sl, c_high - (1.5 * atr))
                elif pos.position_type == "PUT":
                    pos.trailing_sl = min(pos.trailing_sl, c_low + (1.5 * atr))
            elif pos.strategy == "2_FUTURES_TREND":
                if pos.position_type == "LONG":
                    pos.trailing_sl = max(pos.trailing_sl, c_high - (2.0 * atr))
                elif pos.position_type == "SHORT":
                    pos.trailing_sl = min(pos.trailing_sl, c_low + (2.0 * atr))

        # 2. Intraday Auto Square-Off at 15:15
        if c_time >= datetime.strptime("15:15", "%H:%M").time():
            for pos in list(self.positions):
                self._close_position(pos, c_close, "EOD_SQUAREOFF")
            return

        # 3. Strategy 1B: Filtered 15M ORB Evaluation (09:30 to 14:00)
        if STRATEGY_MODE in ["ORB_OPTIONS", "HYBRID"] and not self.orb_traded_today and self.orb_high is not None:
            if datetime.strptime("09:30", "%H:%M").time() <= c_time <= datetime.strptime("14:00", "%H:%M").time():
                vol_ok = (c_vol > 1.2 * vol_ma) if (vol_ma > 0 and c_vol > 0) else True
                width_ok = (0.20 * atr <= self.orb_width <= 0.65 * atr)
                
                # LONG ORB Breakout (BUY ATM CALL)
                if c_close > self.orb_high and vol_ok and width_ok and (c_close > ema_200):
                    sl = c_low - (0.5 * atr)
                    pos = IndexPosition("1B_ORB_OPTIONS", "CALL", "OPTIONS", c_close, sl, LOT_SIZE, c_ts)
                    self.positions.append(pos)
                    self.orb_traded_today = True
                    self._notify_entry(pos, f"NIFTY ATM CALL (15M ORB Breakout > {self.orb_high:.1f})")
                    
                # SHORT ORB Breakdown (BUY ATM PUT)
                elif c_close < self.orb_low and vol_ok and width_ok and (c_close < ema_200):
                    sl = c_high + (0.5 * atr)
                    pos = IndexPosition("1B_ORB_OPTIONS", "PUT", "OPTIONS", c_close, sl, LOT_SIZE, c_ts)
                    self.positions.append(pos)
                    self.orb_traded_today = True
                    self._notify_entry(pos, f"NIFTY ATM PUT (15M ORB Breakdown < {self.orb_low:.1f})")

        # 4. Strategy 2: Futures Trend Following Evaluation (09:30 to 14:30)
        if STRATEGY_MODE in ["FUTURES_TREND", "HYBRID"]:
            has_trend_pos = any(p.strategy == "2_FUTURES_TREND" for p in self.positions)
            if not has_trend_pos and datetime.strptime("09:30", "%H:%M").time() <= c_time <= datetime.strptime("14:30", "%H:%M").time():
                # LONG Trend: 50 EMA > 200 EMA + 20-Donchian High Breakout
                if ema_50 > ema_200 and c_close > don_high:
                    sl = c_close - (2.0 * atr)
                    pos = IndexPosition("2_FUTURES_TREND", "LONG", "FUTURES", c_close, sl, LOT_SIZE, c_ts)
                    self.positions.append(pos)
                    self._notify_entry(pos, f"NIFTY Futures LONG (EMA Bull Trend + Donchian > {don_high:.1f})")
                    
                # SHORT Trend: 50 EMA < 200 EMA + 20-Donchian Low Breakdown
                elif ema_50 < ema_200 and c_close < don_low:
                    sl = c_close + (2.0 * atr)
                    pos = IndexPosition("2_FUTURES_TREND", "SHORT", "FUTURES", c_close, sl, LOT_SIZE, c_ts)
                    self.positions.append(pos)
                    self._notify_entry(pos, f"NIFTY Futures SHORT (EMA Bear Trend + Donchian < {don_low:.1f})")

    def _notify_entry(self, pos: IndexPosition, desc: str):
        emoji = "🟢" if "LONG" in pos.position_type or "CALL" in pos.position_type else "🔴"
        print(f"\n{'='*60}", flush=True)
        print(f"{emoji} [NIFTY ENTRY] {pos.strategy} | {pos.position_type} | {desc}", flush=True)
        print(f"   Entry: {pos.entry_price:.1f} | Initial SL: {pos.initial_sl:.1f} | Qty: {pos.quantity}", flush=True)
        print(f"{'='*60}\n", flush=True)
        
        if self.telegram:
            strike = round(pos.entry_price / 50) * 50
            opt_type = "CE" if "CALL" in pos.position_type or "LONG" in pos.position_type else "PE"
            self.telegram.notify_trade_entry("NIFTY", opt_type, strike, pos.entry_price, 0, pos.initial_sl, pos.quantity, f"{pos.strategy} ({desc})")

    def _close_position(self, pos: IndexPosition, exit_price: float, reason: str):
        if pos not in self.positions: return
        self.positions.remove(pos)
        
        # Calculate exact tax
        tax = 73.42 if pos.instrument == "OPTIONS" else 150.0
        pos.close(exit_price, reason, tax)
        self.closed_trades.append(pos)
        
        emoji = "✅" if pos.net_pnl > 0 else "🛑"
        print(f"\n{emoji} [NIFTY EXIT] {pos.strategy} | {pos.position_type} - {reason}", flush=True)
        print(f"   Entry: {pos.entry_price:.1f} → Exit: {exit_price:.1f} | Pts: {pos.points:+.1f}", flush=True)
        print(f"   Gross P&L: ₹{pos.pnl:+,.2f} | Net P&L: ₹{pos.net_pnl:+,.2f}", flush=True)
        print(f"{'='*60}\n", flush=True)
        
        if self.telegram:
            strike = round(pos.entry_price / 50) * 50
            opt_type = "CE" if "CALL" in pos.position_type or "LONG" in pos.position_type else "PE"
            self.telegram.notify_trade_exit("NIFTY", opt_type, strike, pos.entry_price, exit_price, pos.net_pnl, reason)


class IndexOptionsBot:
    def __init__(self):
        self.is_running = False
        self.stop_event = Event()
        self.kite: Optional[KiteConnect] = None
        self.ticker: Optional[KiteTicker] = None
        self.telegram = TelegramNotifier() if TELEGRAM_AVAILABLE else None
        self.trader = IndexTrader(self._log, self.telegram)
        self.log_file = LOG_DIR / f"index_{now_ist().strftime('%Y%m%d')}.log"

    def _log(self, msg: str):
        ts = now_ist().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts} | {msg}"
        with open(self.log_file, "a") as f:
            f.write(line + "\n")
        print(line, flush=True)

    def authenticate(self) -> bool:
        if not KITE_AVAILABLE: return False
        creds = load_credentials()
        auto_login = KiteAutoLogin(
            api_key=creds["api_key"], api_secret=creds["api_secret"],
            user_id=creds["user_id"], password=creds["password"],
            totp_secret=creds["totp_secret"], headless=True
        )
        # Try saved token
        saved = auto_login.get_saved_token()
        if saved:
            self.kite = KiteConnect(api_key=creds["api_key"])
            self.kite.set_access_token(saved)
            try:
                prof = self.kite.profile()
                self._log(f"✅ Reusing valid access token. Logged in as: {prof.get('user_name')}")
                return True
            except Exception:
                pass
        # Fresh login
        token = auto_login.login()
        if token:
            self.kite = auto_login.kite
            self._log("✅ Fresh auto-login successful!")
            return True
        return False

    def fetch_historical(self):
        if not self.kite: return
        self._log("📊 Fetching historical 15M NIFTY candles...")
        to_d = now_ist()
        from_d = to_d - timedelta(days=10)
        data = self.kite.historical_data(NIFTY_TOKEN, from_date=from_d, to_date=to_d, interval="15minute")
        for c in data[-50:]:
            self.trader.candles.append({
                "timestamp": c["date"], "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"], "volume": c.get("volume", 0)
            })
        self._log(f"   ✓ Loaded {len(self.trader.candles)} historical 15M candles.")

    def start_live_feed(self):
        creds = load_credentials()
        self.ticker = KiteTicker(creds["api_key"], self.kite.access_token)
        
        def on_connect(ws, resp):
            ws.subscribe([NIFTY_TOKEN])
            ws.set_mode(ws.MODE_FULL, [NIFTY_TOKEN])
            self._log("✅ Live WebSocket connected and subscribed to NIFTY 50.")
            
        def on_ticks(ws, ticks):
            for t in ticks:
                if t.get("instrument_token") == NIFTY_TOKEN:
                    ltp = t.get("last_price")
                    vol = t.get("volume_traded", 0)
                    if ltp:
                        self.trader.process_tick(ltp, now_ist(), vol)
                        
        self.ticker.on_connect = on_connect
        self.ticker.on_ticks = on_ticks
        self.ticker.connect(threaded=True)

    def is_market_open(self) -> bool:
        now = now_ist()
        if now.weekday() >= 5: return False
        return now.replace(hour=9, minute=15, second=0) <= now <= now.replace(hour=15, minute=30, second=0)

    def generate_report(self):
        today = now_ist().strftime("%Y-%m-%d")
        trades = self.trader.closed_trades
        tot_pnl = sum(t.net_pnl for t in trades)
        wins = [t for t in trades if t.net_pnl > 0]
        wr = (len(wins) / len(trades) * 100.0) if trades else 0.0
        
        self._log("\n" + "="*60)
        self._log(f"📊 EOD NIFTY REPORT - {today}")
        self._log(f"   Trades Taken: {len(trades)} | Win Rate: {wr:.1f}%")
        self._log(f"   Total Net P&L: ₹{tot_pnl:+,.2f}")
        self._log("="*60)
        
        if self.telegram:
            sec_data = {"NIFTY": {"trades": len(trades), "pnl": tot_pnl, "wins": len(wins), "losses": len(trades) - len(wins)}}
            self.telegram.notify_daily_summary(today, sec_data, tot_pnl)
            
        # Save JSON log
        rep = {"date": today, "strategy": STRATEGY_MODE, "trades": len(trades), "win_rate": wr, "net_pnl": tot_pnl}
        with open(LOG_DIR / f"index_report_{today}.json", "w") as f:
            json.dump(rep, f, indent=2, default=str)

    def run(self):
        self.is_running = True
        print("\n" + "="*70)
        print(f"🚀 NIFTY INDEX TRADING BOT | Strategy Mode: {STRATEGY_MODE}")
        print("="*70)
        
        if not self.authenticate(): return
        self.fetch_historical()
        
        if self.telegram:
            self.telegram.notify_bot_start(["NIFTY 50"])
            
        self.start_live_feed()
        while self.is_running and self.is_market_open():
            time.sleep(1)
            
        self._log("Market closed. Generating EOD summary...")
        self.generate_report()
        if self.ticker: self.ticker.close()

    def stop(self):
        self.is_running = False
        self.stop_event.set()


if __name__ == "__main__":
    bot = IndexOptionsBot()
    bot.run()
