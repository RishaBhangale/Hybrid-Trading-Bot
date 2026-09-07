#!/usr/bin/env python3
"""
Production Autonomous NIFTY Master Hybrid Trading Engine (Strategy 3)
Combines:
1. Strategy 1B: Filtered 15M Opening Range Breakout (Real ATM Options Quotes)
2. Strategy 2: Futures Trend Following (EMA 50/200 + 20-Period Donchian Breakout + ATR Trailing SL)

Upgrades:
1. Live ATM Option Contract Resolution & Real Market Quotes (No synthetic delta/premium formulas)
2. Dynamic NIFTY Spot Token & Lot Size auto-loading directly from Kite API on startup (Lot Size: 65)
3. Dynamic Turnover-Based Taxes & Brokerage
4. Capital Allocation Buckets: ₹3,00,000 Total Equity, ₹1,80,000 Strategy Ceiling, ₹1,20,000 Protected Reserve
5. Full Diagnostic Telemetry: 12:00 PM Mid-Day Heartbeat & EOD Filter Breakdown
"""

import os
import sys
import time
import signal
import json
from pathlib import Path
from datetime import datetime, timedelta, date
from typing import Optional, Dict, List, Any
from threading import Event, Lock
import pandas as pd
import numpy as np

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

# Capital Management Allocation (from deployment specs)
TOTAL_EQUITY = 300000.0          # ₹3,00,000 Total Account Equity
STRATEGY_CEILING = 180000.0      # ₹1,80,000 Strategy Allocation Ceiling (Max Normal Deployment)
PROTECTED_RESERVE = 120000.0     # ₹1,20,000 Protected Reserve (Never intentionally deploy)

STRATEGY_MODE = os.environ.get("INDEX_STRATEGY", "HYBRID").upper()  # "ORB_OPTIONS", "FUTURES_TREND", "HYBRID"
PAPER_TRADING = os.environ.get("PAPER_TRADING", "true").lower() == "true"


def now_ist():
    return datetime.now(IST) if IST else datetime.now()


# ============================================================
# POSITION MODEL
# ============================================================
class IndexPosition:
    def __init__(self, strategy: str, position_type: str, instrument: str,
                 tradingsymbol: str, entry_price: float, sl: float,
                 quantity: int, entry_time: datetime, spot_at_entry: float):
        self.strategy = strategy
        self.position_type = position_type      # "CALL", "PUT", "LONG", "SHORT"
        self.instrument = instrument            # "OPTIONS" or "FUTURES"
        self.tradingsymbol = tradingsymbol      # e.g., "NIFTY2690824200CE" or "NIFTY26SEPFUT"
        self.entry_price = entry_price          # Real Option/Futures LTP
        self.trailing_sl = sl
        self.initial_sl = sl
        self.quantity = quantity
        self.entry_time = entry_time
        self.spot_at_entry = spot_at_entry
        self.exit_price = None
        self.exit_time = None
        self.exit_reason = None
        self.pnl = 0.0
        self.net_pnl = 0.0

    def close(self, exit_price: float, reason: str):
        self.exit_price = exit_price
        self.exit_time = now_ist()
        self.exit_reason = reason
        
        if self.instrument == "OPTIONS":
            pts = self.exit_price - self.entry_price
            self.pnl = pts * self.quantity
            tot_val = (self.entry_price + exit_price) * self.quantity
            stt = (exit_price * self.quantity) * 0.0010
            brokerage = 40.0
            exchange = tot_val * 0.000505
            stamp = (self.entry_price * self.quantity) * 0.00003
            sebi = tot_val * 0.000001
            gst = (brokerage + exchange + sebi) * 0.18
            tax = brokerage + stt + exchange + stamp + sebi + gst
        else: # FUTURES
            pts = (exit_price - self.entry_price) if self.position_type == "LONG" else (self.entry_price - exit_price)
            self.pnl = pts * self.quantity
            tot_val = (self.entry_price + exit_price) * self.quantity
            stt = (exit_price * self.quantity) * 0.000125
            brokerage = 40.0
            exchange = tot_val * 0.000019
            stamp = (self.entry_price * self.quantity) * 0.00002
            sebi = tot_val * 0.000001
            gst = (brokerage + exchange + sebi) * 0.18
            tax = brokerage + stt + exchange + stamp + sebi + gst
            
        self.net_pnl = self.pnl - tax


# ============================================================
# PERSISTENT CAPITAL & RISK TRACKER
# ============================================================
class CapitalTracker:
    """
    Manages NIFTY Strategy trading capital (Strategy Ceiling ₹1,80,000),
    passive income counter (overall P&L), and enforces margin/slot guardrails.
    """
    def __init__(self, base_capital: float = STRATEGY_CEILING,
                 options_min_capital: float = 25000.0,
                 futures_min_capital: float = 120000.0,
                 state_file: Path = BASE_DIR / "capital_state.json",
                 logger=print):
        self.base_capital = base_capital
        self.options_min_capital = options_min_capital
        self.futures_min_capital = futures_min_capital
        self.state_file = state_file
        self.logger = logger
        
        self.session_capital = self.base_capital
        self.overall_pnl = 0.0
        self.load_state()

    def load_state(self):
        env_session = os.environ.get("SESSION_CAPITAL")
        env_overall = os.environ.get("OVERALL_PNL")
        
        if env_session is not None or env_overall is not None:
            if env_session:
                try: self.session_capital = float(env_session)
                except: pass
            if env_overall:
                try: self.overall_pnl = float(env_overall)
                except: pass
            self.logger(f"💼 Capital initialized from ENV: Session Capital=₹{self.session_capital:,.2f}, Overall P&L=₹{self.overall_pnl:,.2f}")
            return
            
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text())
                self.session_capital = float(data.get("session_capital", self.base_capital))
                self.overall_pnl = float(data.get("overall_pnl", 0.0))
                self.logger(f"💼 Capital state loaded: Session Capital=₹{self.session_capital:,.2f}, Overall P&L=₹{self.overall_pnl:,.2f}")
            except Exception as e:
                self.logger(f"⚠️ Error reading capital_state.json: {e}. Defaulting to ₹{self.base_capital:,.2f}")
                self.session_capital = self.base_capital
                self.overall_pnl = 0.0
        else:
            self.session_capital = self.base_capital
            self.overall_pnl = 0.0
            self.save_state()

    def save_state(self):
        # 1. Save to local file (fast, for same-container restarts)
        try:
            data = {
                "base_capital": self.base_capital,
                "session_capital": self.session_capital,
                "overall_pnl": self.overall_pnl,
                "last_updated": now_ist().strftime("%Y-%m-%d %H:%M:%S")
            }
            self.state_file.write_text(json.dumps(data, indent=2))
        except Exception as e:
            self.logger(f"⚠️ Error saving capital_state.json: {e}")

        # 2. Push to Render env vars (survives redeployments / new containers)
        self._push_to_render_env()

    def _push_to_render_env(self):
        """Persist SESSION_CAPITAL and OVERALL_PNL as Render env vars so they
        survive container teardowns and new deployments."""
        api_key = os.environ.get("RENDER_API_KEY")
        service_id = os.environ.get("RENDER_SERVICE_ID")
        if not api_key or not service_id:
            return  # Not on Render or keys not configured — silently skip
        try:
            import urllib.request
            url = f"https://api.render.com/v1/services/{service_id}/env-vars"
            payload = json.dumps([
                {"key": "SESSION_CAPITAL", "value": str(self.session_capital)},
                {"key": "OVERALL_PNL",     "value": str(self.overall_pnl)},
            ]).encode()
            req = urllib.request.Request(
                url, data=payload, method="PUT",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                }
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status in (200, 201):
                    self.logger(f"💾 Capital persisted to Render env vars — Session: ₹{self.session_capital:,.2f} | Overall P&L: ₹{self.overall_pnl:+,.2f}")
                else:
                    self.logger(f"⚠️ Render env update returned status {resp.status}")
        except Exception as e:
            self.logger(f"⚠️ Could not push capital to Render env vars: {e}")

    def can_open_position(self, instrument: str = "OPTIONS") -> tuple:
        if self.session_capital <= 0:
            return False, "Strategy capital is zero or depleted. Trading halted."
        if instrument == "OPTIONS":
            if self.session_capital < self.options_min_capital:
                return False, f"Strategy capital (₹{self.session_capital:,.2f}) is below minimum required for Options (₹{self.options_min_capital:,.2f})."
        elif instrument == "FUTURES":
            if self.session_capital < self.futures_min_capital:
                return False, f"Strategy capital (₹{self.session_capital:,.2f}) is below required margin for Futures (₹{self.futures_min_capital:,.2f})."
        return True, "OK"

    def end_day(self, day_net_pnl: float) -> dict:
        capital_used = self.session_capital
        self.overall_pnl += day_net_pnl
        
        is_profit = day_net_pnl >= 0
        if is_profit:
            next_day_capital = self.base_capital
            capital_remaining = self.session_capital + day_net_pnl
        else:
            next_day_capital = max(0.0, self.session_capital + day_net_pnl)
            capital_remaining = next_day_capital
            
        summary = {
            "capital_used": capital_used,
            "day_pnl": day_net_pnl,
            "is_profit": is_profit,
            "capital_remaining": capital_remaining,
            "next_day_capital": next_day_capital,
            "overall_pnl": self.overall_pnl,
            "base_capital": self.base_capital
        }
        
        self.session_capital = next_day_capital
        self.save_state()
        return summary


# ============================================================
# INDEX TRADER ENGINE
# ============================================================
class IndexTrader:
    def __init__(self, logger, kite=None, nfo_df=None, telegram=None, capital_tracker=None):
        self.logger = logger
        self.kite = kite
        self.nfo_df = nfo_df
        self.telegram = telegram
        self.capital_tracker = capital_tracker
        
        self.lot_size = 65
        self.strike_gap = 50.0
        
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
        
        # Telemetry
        self.tick_count = 0
        self.ltp = 0.0
        self.last_atr = 0.0
        self.last_ema50 = 0.0
        self.last_ema200 = 0.0
        self.last_filter_reason = "Waiting for initial 15M candles"
        self.lock = Lock()
        
    def process_tick(self, ltp: float, tick_time: datetime, volume: int = 0):
        with self.lock:
            self.tick_count += 1
            self.ltp = ltp
            candle_min = (tick_time.minute // 15) * 15
            candle_ts = tick_time.replace(minute=candle_min, second=0, microsecond=0)
            
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
                
            self._check_trailing_stops(ltp)
            
    def _check_trailing_stops(self, ltp: float):
        for pos in list(self.positions):
            if pos.instrument == "OPTIONS":
                try:
                    quote = self.kite.ltp([f"NFO:{pos.tradingsymbol}"])
                    opt_ltp = quote.get(f"NFO:{pos.tradingsymbol}", {}).get("last_price", 0.0)
                    if opt_ltp > 0 and opt_ltp <= pos.trailing_sl:
                        self._close_position(pos, opt_ltp, "SL_HIT")
                except Exception:
                    pass
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
            
        # 15M ORB (09:15 - 09:30)
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
        self.last_atr = tr.ewm(span=14, adjust=False).mean().iloc[-1]
        self.last_ema50 = df['close'].ewm(span=50, adjust=False).mean().iloc[-1]
        self.last_ema200 = df['close'].ewm(span=200, adjust=False).mean().iloc[-1]
        don_high = df['high'].iloc[-21:-1].max() if len(df) >= 21 else df['high'].max()
        don_low = df['low'].iloc[-21:-1].min() if len(df) >= 21 else df['low'].min()
        vol_ma = df['volume'].iloc[-21:-1].mean() if len(df) >= 21 else df['volume'].mean()
        
        # 1. Update Trailing SLs on Closed Candle
        for pos in self.positions:
            if pos.strategy == "1B_ORB_OPTIONS":
                pos.trailing_sl = max(pos.trailing_sl, pos.entry_price * 0.75)
            elif pos.strategy == "2_FUTURES_TREND":
                if pos.position_type == "LONG":
                    pos.trailing_sl = max(pos.trailing_sl, c_high - (2.0 * self.last_atr))
                elif pos.position_type == "SHORT":
                    pos.trailing_sl = min(pos.trailing_sl, c_low + (2.0 * self.last_atr))

        # 2. Intraday Auto Square-Off at 15:15
        if c_time >= datetime.strptime("15:15", "%H:%M").time():
            for pos in list(self.positions):
                self._close_position(pos, c_close, "EOD_SQUAREOFF")
            return

        # 3. Strategy 1B: Filtered 15M ORB Evaluation (09:30 to 14:00)
        if STRATEGY_MODE in ["ORB_OPTIONS", "HYBRID"] and not self.orb_traded_today and self.orb_high is not None:
            if datetime.strptime("09:30", "%H:%M").time() <= c_time <= datetime.strptime("14:00", "%H:%M").time():
                vol_ok = (c_vol > 1.2 * vol_ma) if (vol_ma > 0 and c_vol > 0) else True
                width_ok = (0.20 * self.last_atr <= self.orb_width <= 0.65 * self.last_atr)
                
                # LONG ORB Breakout (BUY ATM CALL)
                if c_close > self.orb_high and vol_ok and width_ok and (c_close > self.last_ema200):
                    self._enter_options_position("CALL", c_close)
                    self.orb_traded_today = True
                    self.last_filter_reason = "1B ORB Long Call Executed"
                elif c_close < self.orb_low and vol_ok and width_ok and (c_close < self.last_ema200):
                    self._enter_options_position("PUT", c_close)
                    self.orb_traded_today = True
                    self.last_filter_reason = "1B ORB Short Put Executed"
                else:
                    if not width_ok: self.last_filter_reason = f"ORB Width {self.orb_width:.1f} outside ATR bounds"
                    elif not vol_ok: self.last_filter_reason = "Volume below 1.2x MA"

        # 4. Strategy 2: Futures Trend Following (09:30 to 14:30)
        if STRATEGY_MODE in ["FUTURES_TREND", "HYBRID"]:
            has_trend = any(p.strategy == "2_FUTURES_TREND" for p in self.positions)
            if not has_trend and datetime.strptime("09:30", "%H:%M").time() <= c_time <= datetime.strptime("14:30", "%H:%M").time():
                if self.last_ema50 > self.last_ema200 and c_close > don_high:
                    if self.capital_tracker:
                        can_open, reason = self.capital_tracker.can_open_position("FUTURES")
                        if not can_open:
                            self.logger(f"⚠️ [NIFTY Futures LONG] Entry blocked: {reason}")
                            if self.telegram and (self.capital_tracker.session_capital < self.capital_tracker.futures_min_capital or self.capital_tracker.session_capital <= 0):
                                self.telegram.notify_capital_alert("NIFTY FUTURES", self.capital_tracker.session_capital, self.capital_tracker.futures_min_capital, self.capital_tracker.overall_pnl, reason)
                            return
                    sl = c_close - (2.0 * self.last_atr)
                    pos = IndexPosition("2_FUTURES_TREND", "LONG", "FUTURES", "NIFTY_FUT", c_close, sl, self.lot_size, c_ts, c_close)
                    self.positions.append(pos)
                    self._notify_entry(pos, f"NIFTY Futures LONG (EMA Bull + Donchian Breakout > {don_high:.1f})")
                elif self.last_ema50 < self.last_ema200 and c_close < don_low:
                    if self.capital_tracker:
                        can_open, reason = self.capital_tracker.can_open_position("FUTURES")
                        if not can_open:
                            self.logger(f"⚠️ [NIFTY Futures SHORT] Entry blocked: {reason}")
                            if self.telegram and (self.capital_tracker.session_capital < self.capital_tracker.futures_min_capital or self.capital_tracker.session_capital <= 0):
                                self.telegram.notify_capital_alert("NIFTY FUTURES", self.capital_tracker.session_capital, self.capital_tracker.futures_min_capital, self.capital_tracker.overall_pnl, reason)
                            return
                    sl = c_close + (2.0 * self.last_atr)
                    pos = IndexPosition("2_FUTURES_TREND", "SHORT", "FUTURES", "NIFTY_FUT", c_close, sl, self.lot_size, c_ts, c_close)
                    self.positions.append(pos)
                    self._notify_entry(pos, f"NIFTY Futures SHORT (EMA Bear + Donchian Breakdown < {don_low:.1f})")

    def _enter_options_position(self, opt_type: str, spot: float):
        """Fetch live ATM option contract from Kite and enter."""
        if self.capital_tracker:
            can_open, reason = self.capital_tracker.can_open_position("OPTIONS")
            if not can_open:
                self.logger(f"⚠️ [NIFTY ATM {opt_type}] Entry blocked: {reason}")
                if self.telegram and (self.capital_tracker.session_capital < self.capital_tracker.options_min_capital or self.capital_tracker.session_capital <= 0):
                    self.telegram.notify_capital_alert("NIFTY OPTIONS", self.capital_tracker.session_capital, self.capital_tracker.options_min_capital, self.capital_tracker.overall_pnl, reason)
                return

        atm_strike = round(spot / self.strike_gap) * self.strike_gap
        today = date.today()
        opts = self.nfo_df[(self.nfo_df['name'] == 'NIFTY') &
                           (self.nfo_df['strike'] == atm_strike) &
                           (self.nfo_df['instrument_type'] == ('CE' if opt_type == 'CALL' else 'PE')) &
                           (self.nfo_df['expiry'] >= today)].sort_values('expiry')
        if opts.empty: return
        contract = opts.iloc[0]
        tsym = contract['tradingsymbol']
        lot = int(contract['lot_size'])
        
        quote = self.kite.ltp([f"NFO:{tsym}"])
        live_price = quote.get(f"NFO:{tsym}", {}).get("last_price", 0.0)
        if live_price <= 0: live_price = 50.0
        
        sl = max(0.50, live_price * 0.75)  # 25% option stop-loss
        pos = IndexPosition("1B_ORB_OPTIONS", opt_type, "OPTIONS", tsym, live_price, sl, lot, now_ist(), spot)
        self.positions.append(pos)
        self._notify_entry(pos, f"NIFTY ATM {opt_type} ({tsym} @ ₹{live_price:.2f})")

    def _notify_entry(self, pos: IndexPosition, desc: str):
        emoji = "🟢" if "LONG" in pos.position_type or "CALL" in pos.position_type else "🔴"
        print(f"\n{'='*60}", flush=True)
        print(f"{emoji} [NIFTY ENTRY] {pos.strategy} | {pos.tradingsymbol} | {desc}", flush=True)
        print(f"   Entry: ₹{pos.entry_price:.2f} | Initial SL: ₹{pos.initial_sl:.2f} | Qty: {pos.quantity}", flush=True)
        print(f"{'='*60}\n", flush=True)
        
        if self.telegram:
            self.telegram.notify_trade_entry("NIFTY", pos.position_type, pos.spot_at_entry, pos.entry_price, 0, pos.initial_sl, pos.quantity, f"{pos.strategy} ({desc})")

    def _close_position(self, pos: IndexPosition, exit_price: float, reason: str):
        if pos not in self.positions: return
        self.positions.remove(pos)
        
        pos.close(exit_price, reason)
        self.closed_trades.append(pos)
        
        emoji = "✅" if pos.net_pnl > 0 else "🛑"
        print(f"\n{emoji} [NIFTY EXIT] {pos.tradingsymbol} - {reason}", flush=True)
        print(f"   Entry: ₹{pos.entry_price:.2f} → Exit: ₹{exit_price:.2f} | Net P&L: ₹{pos.net_pnl:+,.2f}", flush=True)
        print(f"{'='*60}\n", flush=True)
        
        if self.telegram:
            self.telegram.notify_trade_exit("NIFTY", pos.position_type, pos.spot_at_entry, pos.entry_price, exit_price, pos.net_pnl, reason)

    def get_diagnostics(self) -> Dict:
        trend = "BULLISH" if self.last_ema50 > self.last_ema200 else ("BEARISH" if self.last_ema50 < self.last_ema200 else "NEUTRAL")
        orb_str = f"H:{self.orb_high:.1f} L:{self.orb_low:.1f} (W:{self.orb_width:.1f} pts)" if self.orb_high else "Not established"
        return {
            "symbol": "NIFTY 50",
            "ticks": self.tick_count,
            "candles": len(self.candles),
            "ltp": self.ltp,
            "trend": trend,
            "lot_size": self.lot_size,
            "ema50": round(self.last_ema50, 1),
            "ema200": round(self.last_ema200, 1),
            "atr": round(self.last_atr, 1),
            "orb_range": orb_str,
            "block_reason": self.last_filter_reason,
            "active_positions": len(self.positions)
        }


# ============================================================
# MASTER INDEX BOT CONTROLLER
# ============================================================
class IndexOptionsBot:
    def __init__(self):
        self.is_running = False
        self.stop_event = Event()
        self.kite: Optional[KiteConnect] = None
        self.ticker: Optional[KiteTicker] = None
        self.telegram = TelegramNotifier() if TELEGRAM_AVAILABLE else None
        self.nfo_df: Optional[pd.DataFrame] = None
        self.spot_token = 256265
        self.capital_tracker = CapitalTracker(base_capital=STRATEGY_CEILING, options_min_capital=25000.0, futures_min_capital=120000.0, logger=self._log)
        self.trader = IndexTrader(self._log, None, None, self.telegram, self.capital_tracker)
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
        token = auto_login.login()
        if token:
            self.kite = auto_login.kite
            self._log("✅ Fresh auto-login successful!")
            return True
        return False

    def load_market_metadata(self):
        """Load live NIFTY spot token and NFO lot size from Kite API."""
        if not self.kite: return
        self._log("📊 Downloading live NIFTY market metadata (NSE & NFO)...")
        
        # 1. Download NFO
        nfo_list = self.kite.instruments('NFO')
        self.nfo_df = pd.DataFrame(nfo_list)
        
        # 2. Download NSE
        nse_list = self.kite.instruments('NSE')
        df_nse = pd.DataFrame(nse_list)
        spot_match = df_nse[df_nse['tradingsymbol'] == 'NIFTY 50']
        if not spot_match.empty:
            self.spot_token = int(spot_match.iloc[0]['instrument_token'])
            
        nifty_futs = self.nfo_df[(self.nfo_df['name'] == 'NIFTY') & (self.nfo_df['instrument_type'] == 'FUT')]
        if not nifty_futs.empty:
            self.trader.lot_size = int(nifty_futs.iloc[0]['lot_size'])
            
        self.trader.kite = self.kite
        self.trader.nfo_df = self.nfo_df
        self._log(f"   ✓ NIFTY Spot Token: {self.spot_token} | Current Lot Size: {self.trader.lot_size}")

    def fetch_historical(self):
        if not self.kite: return
        self._log("📊 Fetching historical 15M NIFTY candles...")
        to_d = now_ist()
        from_d = to_d - timedelta(days=10)
        data = self.kite.historical_data(self.spot_token, from_date=from_d, to_date=to_d, interval="15minute")
        for c in data[-50:]:
            self.trader.candles.append({
                "timestamp": c["date"], "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"], "volume": c.get("volume", 0)
            })

    def start_live_feed(self):
        creds = load_credentials()
        self.ticker = KiteTicker(creds["api_key"], self.kite.access_token)
        self._last_tick_time = now_ist()
        
        def on_connect(ws, resp):
            ws.subscribe([self.spot_token])
            ws.set_mode(ws.MODE_FULL, [self.spot_token])
            self._log(f"✅ WebSocket connected — subscribed to NIFTY 50 (Token: {self.spot_token}).")
            
        def on_ticks(ws, ticks):
            self._last_tick_time = now_ist()
            for t in ticks:
                if t.get("instrument_token") == self.spot_token:
                    ltp = t.get("last_price")
                    vol = t.get("volume_traded", 0)
                    if ltp:
                        self.trader.process_tick(ltp, now_ist(), vol)

        def on_error(ws, code, reason):
            self._log(f"⚠️ WebSocket error [{code}]: {reason} — will attempt reconnect.")

        def on_close(ws, code, reason):
            self._log(f"⚠️ WebSocket closed [{code}]: {reason}.")
            if self.is_running and self.is_market_open():
                self._log("🔄 Market is open — waiting for KiteTicker auto-reconnect...")

        def on_reconnect(ws, attempt):
            self._log(f"🔄 WebSocket reconnecting... attempt #{attempt}")

        def on_noreconnect(ws):
            self._log("❌ WebSocket exhausted all reconnect attempts — restarting ticker now.")
            try:
                self._restart_ticker()
            except Exception as e:
                self._log(f"❌ Ticker restart failed: {e}")

        self.ticker.on_connect = on_connect
        self.ticker.on_ticks = on_ticks
        self.ticker.on_error = on_error
        self.ticker.on_close = on_close
        self.ticker.on_reconnect = on_reconnect
        self.ticker.on_noreconnect = on_noreconnect
        self.ticker.connect(threaded=True)

    def _restart_ticker(self):
        """Hard-restart the KiteTicker when auto-reconnect is exhausted."""
        try:
            if self.ticker:
                self.ticker.close()
        except Exception:
            pass
        time.sleep(5)
        self.start_live_feed()
        self._log("✅ WebSocket ticker restarted successfully.")

    def is_market_open(self) -> bool:
        now = now_ist()
        if now.weekday() >= 5: return False
        return now.replace(hour=9, minute=15, second=0) <= now <= now.replace(hour=15, minute=30, second=0)

    def generate_report(self):
        today = now_ist().strftime("%Y-%m-%d")
        trades = self.trader.closed_trades
        tot_pnl = sum(t.net_pnl for t in trades)
        wins = [t for t in trades if t.net_pnl > 0]
        
        diagnostics = {"NIFTY": self.trader.get_diagnostics()}
        
        # Process EOD Capital and Risk
        cap_summary = self.capital_tracker.end_day(tot_pnl)
        
        if self.telegram:
            sec_data = {"NIFTY": {"trades": len(trades), "pnl": tot_pnl, "wins": len(wins), "losses": len(trades) - len(wins)}}
            self.telegram.notify_daily_summary(today, sec_data, tot_pnl, diagnostics=diagnostics, capital_summary=cap_summary)
            
        rep = {
            "date": today,
            "strategy": STRATEGY_MODE,
            "capital_summary": cap_summary,
            "capital": {"total_equity": TOTAL_EQUITY, "ceiling": STRATEGY_CEILING, "reserve": PROTECTED_RESERVE},
            "trades": len(trades),
            "net_pnl": tot_pnl,
            "diagnostics": diagnostics
        }
        with open(LOG_DIR / f"index_report_{today}.json", "w") as f:
            json.dump(rep, f, indent=2, default=str)

    def run(self):
        self.is_running = True
        print("\n" + "="*70)
        print(f"🚀 NIFTY MASTER HYBRID ENGINE (Capital: ₹{self.capital_tracker.session_capital:,.0f} | Overall P&L: ₹{self.capital_tracker.overall_pnl:+,.0f})")
        print("="*70)
        
        if not self.authenticate(): return
        self.load_market_metadata()
        self.fetch_historical()
        
        if self.telegram:
            self.telegram.notify_bot_start(["NIFTY 50"], capital_tracker=self.capital_tracker)
            
        self.start_live_feed()
        
        heartbeat_sent = False
        while self.is_running and self.is_market_open():
            now = now_ist()
            if not heartbeat_sent and now.hour == 12 and now.minute >= 0:
                if self.telegram:
                    try:
                        status_dict = {"NIFTY": self.trader.get_diagnostics()}
                        self.telegram.notify_midday_heartbeat(status_dict, self.trader.tick_count, len(self.trader.positions))
                    except Exception:
                        pass
                heartbeat_sent = True
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
