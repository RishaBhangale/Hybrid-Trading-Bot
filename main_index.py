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

import pytz
IST = pytz.timezone("Asia/Kolkata")  # Hard-fail: if pytz missing, all time logic is wrong

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
from execution_engine import ExecutionEngine

# ============================================================
# CONFIGURATION & CAPITAL ALLOCATION BUCKETS
# ============================================================
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Capital Management Allocation (from deployment specs)
TOTAL_EQUITY = 300000.0          # ₹3,00,000 Total Account Equity
STRATEGY_CEILING = 180000.0      # ₹1,80,000 Strategy Allocation Ceiling (Max Normal Deployment)
PROTECTED_RESERVE = 120000.0     # ₹1,20,000 Protected Reserve (Never intentionally deploy)
DAILY_LOSS_LIMIT = 15000.0       # ₹15,000 Daily Loss Limit

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
        # Peak premium seen since entry — used to ratchet options trailing SL
        self.peak_premium: float = entry_price
        # Set True if exit order failed and position remains at broker
        self.orphaned: bool = False

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
        self.lock = Lock()
        self.daily_realized_pnl = 0.0
        
        self.session_capital = self.base_capital
        self.overall_pnl = 0.0
        self.load_state()

    def load_state(self):
        today_str = now_ist().strftime("%Y-%m-%d")
        # 1. Prefer capital_state.json (local file updated by latest run/end_day)
        loaded = False
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text())
                self.session_capital = float(data.get("session_capital", self.base_capital))
                self.overall_pnl = float(data.get("overall_pnl", 0.0))
                # Restore today's running P&L total so the daily loss limit survives
                # mid-day restarts. Only apply if the state was written today — yesterday's
                # daily P&L is irrelevant (it was already absorbed into session_capital).
                last_updated = data.get("last_updated", "")
                if last_updated.startswith(today_str):
                    self.daily_realized_pnl = float(data.get("daily_realized_pnl", 0.0))
                    if self.daily_realized_pnl != 0.0:
                        self.logger(f"💼 Restored today's running P&L: ₹{self.daily_realized_pnl:+,.2f} (daily loss limit intact)")
                self.logger(f"💼 Capital state loaded from file: Session Capital=₹{self.session_capital:,.2f}, Overall P&L=₹{self.overall_pnl:,.2f}")
                loaded = True
            except Exception as e:
                self.logger(f"⚠️ Error reading capital_state.json: {e}")

        # 2. Fall back to environment variables (e.g. initial container deployment)
        if not loaded:
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
                loaded = True

        if not loaded:
            self.session_capital = self.base_capital
            self.overall_pnl = 0.0
            self.save_state()
        else:
            # Sync to os.environ so in-memory values match across components
            os.environ["SESSION_CAPITAL"] = str(self.session_capital)
            os.environ["OVERALL_PNL"] = str(self.overall_pnl)

    def _save_local_state(self):
        """Write state to local JSON only — does NOT push to Render env vars.
        Safe to call after every trade because it never triggers a Render redeploy.
        Includes daily_realized_pnl so the loss limit survives intraday restarts."""
        os.environ["SESSION_CAPITAL"] = str(self.session_capital)
        os.environ["OVERALL_PNL"] = str(self.overall_pnl)
        try:
            data = {
                "base_capital": self.base_capital,
                "session_capital": self.session_capital,
                "overall_pnl": self.overall_pnl,
                "daily_realized_pnl": self.daily_realized_pnl,
                "last_updated": now_ist().strftime("%Y-%m-%d %H:%M:%S")
            }
            self.state_file.write_text(json.dumps(data, indent=2))
        except Exception as e:
            self.logger(f"⚠️ Error saving capital_state.json (local): {e}")

    def save_state(self):
        """Full state save: local file + Render env vars. Call only at EOD.
        Note: pushing to Render env vars triggers a service redeploy."""
        # Always sync to in-memory os.environ first
        os.environ["SESSION_CAPITAL"] = str(self.session_capital)
        os.environ["OVERALL_PNL"] = str(self.overall_pnl)

        # 1. Save to local file (fast, for same-container restarts)
        try:
            data = {
                "base_capital": self.base_capital,
                "session_capital": self.session_capital,
                "overall_pnl": self.overall_pnl,
                "daily_realized_pnl": self.daily_realized_pnl,
                "last_updated": now_ist().strftime("%Y-%m-%d %H:%M:%S")
            }
            self.state_file.write_text(json.dumps(data, indent=2))
        except Exception as e:
            self.logger(f"⚠️ Error saving capital_state.json: {e}")

        # 2. Push to Render env vars (survives redeployments / new containers)
        self._push_to_render_env()

    def _push_to_render_env(self):
        """Persist SESSION_CAPITAL and OVERALL_PNL as Render env vars so they
        survive container teardowns and new deployments.
        Uses GET-merge-PUT pattern to preserve all existing env vars."""
        import math
        api_key = os.environ.get("RENDER_API_KEY")
        service_id = os.environ.get("RENDER_SERVICE_ID")
        if not api_key or not service_id:
            return  # Not on Render or keys not configured — silently skip
        
        # Guard against persisting NaN or Inf
        if not math.isfinite(self.session_capital) or not math.isfinite(self.overall_pnl):
            self.logger("⚠️ Refusing to persist non-finite capital values to Render")
            return
        
        try:
            import urllib.request
            base_url = f"https://api.render.com/v1/services/{service_id}/env-vars"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            
            # 1. GET all existing env vars
            get_req = urllib.request.Request(base_url, method="GET", headers=headers)
            with urllib.request.urlopen(get_req, timeout=10) as resp:
                existing = json.loads(resp.read().decode())
            
            # 2. Build env var dict from existing, update our keys
            env_dict = {}
            for item in existing:
                env_dict[item["envVar"]["key"]] = item["envVar"]["value"]
            
            env_dict["SESSION_CAPITAL"] = str(self.session_capital)
            env_dict["OVERALL_PNL"] = str(self.overall_pnl)
            
            # 3. PUT full list back (preserves other env vars)
            payload = json.dumps([{"key": k, "value": v} for k, v in env_dict.items()]).encode()
            put_req = urllib.request.Request(base_url, data=payload, method="PUT", headers=headers)
            urllib.request.urlopen(put_req, timeout=10)
            
            self.logger(f"💾 Capital persisted to Render — Session: ₹{self.session_capital:,.2f} | P&L: ₹{self.overall_pnl:+,.2f}")
        except Exception as e:
            self.logger(f"⚠️ Could not push capital to Render env vars: {e}")

    def can_open_position(self, instrument: str = "OPTIONS") -> tuple:
        with self.lock:
            if self.session_capital <= 0:
                return False, "Strategy capital is zero or depleted. Trading halted."
            if self.daily_realized_pnl <= -DAILY_LOSS_LIMIT:
                return False, f"Daily loss limit hit: ₹{self.daily_realized_pnl:,.2f} exceeds -₹{DAILY_LOSS_LIMIT:,.2f}. Trading halted for today."
            if instrument == "OPTIONS":
                if self.session_capital < self.options_min_capital:
                    return False, f"Strategy capital (₹{self.session_capital:,.2f}) is below minimum required for Options (₹{self.options_min_capital:,.2f})."
            elif instrument == "FUTURES":
                if self.session_capital < self.futures_min_capital:
                    return False, f"Strategy capital (₹{self.session_capital:,.2f}) is below required margin for Futures (₹{self.futures_min_capital:,.2f})."
            return True, "OK"

    def record_trade_pnl(self, pnl: float):
        """Record a closed trade's P&L for daily loss limit tracking.
        Persists to local file immediately so the limit survives mid-day restarts."""
        with self.lock:
            self.daily_realized_pnl += pnl
            self._save_local_state()  # Local-only save — no Render push, no redeploy

    def end_day(self, day_net_pnl: float) -> dict:
        capital_used = self.session_capital
        
        # New capital after today's P&L
        new_capital = self.session_capital + day_net_pnl
        self.overall_pnl += day_net_pnl
        
        # Two-tier principal recovery:
        # Tier 1: If new_capital >= base_capital, base is restored and excess is reaped as profit
        # Tier 2: If new_capital < base_capital, capital remains in deficit to recover next day
        if new_capital >= self.base_capital:
            next_day_capital = self.base_capital
            profit_reaped = new_capital - self.base_capital
            is_base_restored = True
        else:
            next_day_capital = max(0.0, new_capital)
            profit_reaped = 0.0
            is_base_restored = False
            
        summary = {
            "capital_used": capital_used,
            "day_pnl": day_net_pnl,
            "is_profit": day_net_pnl >= 0,
            "is_base_restored": is_base_restored,
            "profit_reaped": profit_reaped,
            "capital_remaining": new_capital,
            "next_day_capital": next_day_capital,
            "overall_pnl": self.overall_pnl,
            "base_capital": self.base_capital
        }
        
        self.session_capital = next_day_capital
        self.daily_realized_pnl = 0.0  # Reset for next day before saving
        self.save_state()  # Full save (local + Render) only at EOD
        return summary


# ============================================================
# INDEX TRADER ENGINE
# ============================================================
class IndexTrader:
    def __init__(self, logger, kite=None, nfo_df=None, telegram=None, capital_tracker=None, bot_controller=None, execution_engine=None):
        self.logger = logger
        self.kite = kite
        self.nfo_df = nfo_df
        self.telegram = telegram
        self.capital_tracker = capital_tracker
        self.bot_controller = bot_controller
        self.execution_engine = execution_engine
        
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
        self.last_option_ltp: Dict[str, float] = {}       # Updated from WebSocket ticks
        self.last_option_tick_time: Dict[str, datetime] = {}  # Timestamp of last tick per option (T2-E staleness)
        self.nifty_fut_symbol: Optional[str] = None        # Set by load_market_metadata (T2-B)
        
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
                    "timestamp": candle_ts, "open": ltp, "high": ltp, "low": ltp, "close": ltp, "volume": 0
                }
                self.last_candle_time = candle_ts
                self._prev_volume = volume  # Seed for next tick's delta computation
            else:
                self.current_candle["high"] = max(self.current_candle["high"], ltp)
                self.current_candle["low"] = min(self.current_candle["low"], ltp)
                self.current_candle["close"] = ltp
                # volume from Kite is CUMULATIVE day volume — store only the delta per tick
                prev = getattr(self, "_prev_volume", 0)
                delta = max(0, volume - prev) if volume >= prev else volume
                self.current_candle["volume"] += delta
                self._prev_volume = volume

            self._check_trailing_stops(ltp)
            
    def _check_trailing_stops(self, ltp: float):
        for pos in list(self.positions):
            if getattr(pos, "orphaned", False):
                continue  # Exit order already failed — skip, don't double-trigger
            if pos.instrument == "OPTIONS":
                opt_ltp = self.last_option_ltp.get(pos.tradingsymbol, 0.0)
                if opt_ltp > 0:
                    # Ratchet peak premium up whenever we see a new high
                    if opt_ltp > pos.peak_premium:
                        pos.peak_premium = opt_ltp
                    if opt_ltp <= pos.trailing_sl:
                        self.logger(f"🔔 [NIFTY SL] Option LTP ₹{opt_ltp:.2f} ≤ SL ₹{pos.trailing_sl:.2f}")
                        self._close_position(pos, opt_ltp, "SL_HIT")
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
        # Use up to 260 candles (~65 trading days) so EMA-200 has valid seed data.
        # Require >=200 candles before computing — with fewer bars EMA-200 is meaningless.
        df = pd.DataFrame(self.candles[-260:])
        if len(df) < 200:
            self.last_filter_reason = f"Waiting for 200-candle history for valid EMA-200 (have {len(df)})"
            return
        hl = df['high'] - df['low']
        hc = (df['high'] - df['close'].shift(1)).abs()
        lc = (df['low'] - df['close'].shift(1)).abs()
        tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        self.last_atr = tr.ewm(span=14, adjust=False).mean().iloc[-1]
        self.last_ema50 = df['close'].ewm(span=50, adjust=False).mean().iloc[-1]
        self.last_ema200 = df['close'].ewm(span=200, adjust=False).mean().iloc[-1]
        don_high = df['high'].iloc[-21:-1].max() if len(df) >= 21 else df['high'].max()
        don_low = df['low'].iloc[-21:-1].min() if len(df) >= 21 else df['low'].min()

        # 1. Update Trailing SLs on Closed Candle
        for pos in self.positions:
            if getattr(pos, "orphaned", False):
                continue
            if pos.strategy == "1B_ORB_OPTIONS":
                # Ratchet off peak_premium (highest LTP seen since entry), not entry_price.
                # This ensures winners actually protect profits — the old entry_price * 0.75
                # was a permanent no-op that reset the stop to the same level every candle.
                pos.trailing_sl = max(pos.trailing_sl, pos.peak_premium * 0.75)
            elif pos.strategy == "2_FUTURES_TREND":
                if pos.position_type == "LONG":
                    pos.trailing_sl = max(pos.trailing_sl, c_high - (2.0 * self.last_atr))
                elif pos.position_type == "SHORT":
                    pos.trailing_sl = min(pos.trailing_sl, c_low + (2.0 * self.last_atr))

        # 2. Strategy 1B: Filtered 15M ORB Evaluation (09:30 to 14:00)
        # Note: vol_ok filter removed — NIFTY 50 index volume from Kite ticks is always 0,
        # making the 1.2x MA volume filter a permanent pass-through with misleading logs.
        if STRATEGY_MODE in ["ORB_OPTIONS", "HYBRID"] and not self.orb_traded_today and self.orb_high is not None:
            if datetime.strptime("09:30", "%H:%M").time() <= c_time <= datetime.strptime("14:00", "%H:%M").time():
                width_ok = (0.20 * self.last_atr <= self.orb_width <= 0.65 * self.last_atr)

                # LONG ORB Breakout (BUY ATM CALL)
                if c_close > self.orb_high and width_ok and (c_close > self.last_ema200):
                    self._enter_options_position("CALL", c_close)
                    self.orb_traded_today = True
                    self.last_filter_reason = "1B ORB Long Call Executed"
                elif c_close < self.orb_low and width_ok and (c_close < self.last_ema200):
                    self._enter_options_position("PUT", c_close)
                    self.orb_traded_today = True
                    self.last_filter_reason = "1B ORB Short Put Executed"
                else:
                    if not width_ok:
                        self.last_filter_reason = f"ORB Width {self.orb_width:.1f} outside ATR bounds"

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

                    # Use live futures LTP for entry price — spot and futures diverge by 50-150pts
                    fut_sym = getattr(self, "nifty_fut_symbol", None)
                    entry_p = c_close  # fallback
                    if fut_sym and self.kite:
                        try:
                            fut_quote = self.kite.ltp([f"NFO:{fut_sym}"])
                            fut_ltp = fut_quote.get(f"NFO:{fut_sym}", {}).get("last_price", 0.0)
                            if fut_ltp > 0:
                                entry_p = fut_ltp
                        except Exception as eq:
                            self.logger(f"⚠️ [NIFTY Futures LONG] Could not fetch futures LTP, using spot: {eq}")

                    tradingsymbol = fut_sym or "NIFTY_FUT"
                    sl = entry_p - (2.0 * self.last_atr)
                    qty = self.lot_size
                    if self.execution_engine:
                        res = self.execution_engine.place_entry_order(tradingsymbol, qty, entry_p, "BUY")
                        if res["status"] != "COMPLETE":
                            self.logger(f"❌ [NIFTY Futures LONG] Order failed: {res.get('error', res['status'])}")
                            return
                        entry_p = res["fill_price"]
                        qty = res.get("fill_qty", qty)

                    pos = IndexPosition("2_FUTURES_TREND", "LONG", "FUTURES", tradingsymbol, entry_p, sl, qty, c_ts, c_close)
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

                    # Use live futures LTP for entry price
                    fut_sym = getattr(self, "nifty_fut_symbol", None)
                    entry_p = c_close  # fallback
                    if fut_sym and self.kite:
                        try:
                            fut_quote = self.kite.ltp([f"NFO:{fut_sym}"])
                            fut_ltp = fut_quote.get(f"NFO:{fut_sym}", {}).get("last_price", 0.0)
                            if fut_ltp > 0:
                                entry_p = fut_ltp
                        except Exception as eq:
                            self.logger(f"⚠️ [NIFTY Futures SHORT] Could not fetch futures LTP, using spot: {eq}")

                    tradingsymbol = fut_sym or "NIFTY_FUT"
                    sl = entry_p + (2.0 * self.last_atr)
                    qty = self.lot_size
                    if self.execution_engine:
                        res = self.execution_engine.place_entry_order(tradingsymbol, qty, entry_p, "SELL")
                        if res["status"] != "COMPLETE":
                            self.logger(f"❌ [NIFTY Futures SHORT] Order failed: {res.get('error', res['status'])}")
                            return
                        entry_p = res["fill_price"]
                        qty = res.get("fill_qty", qty)

                    pos = IndexPosition("2_FUTURES_TREND", "SHORT", "FUTURES", tradingsymbol, entry_p, sl, qty, c_ts, c_close)
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
        token = int(contract['instrument_token'])
        lot = int(contract['lot_size'])
        
        quote = self.kite.ltp([f"NFO:{tsym}"])
        live_price = quote.get(f"NFO:{tsym}", {}).get("last_price", 0.0)
        if live_price <= 0:
            self.logger(f"⚠️ [NIFTY ATM {opt_type}] LTP unavailable for {tsym} — aborting entry (refusing to fabricate price)")
            return
        
        # T3-D: Quick margin sanity check before submitting order.
        # Options require premium upfront; verify available cash margin is adequate.
        required_margin = live_price * lot * 1.10  # 10% buffer over raw premium
        if self.execution_engine and not self.execution_engine.paper_trading and self.kite:
            try:
                margins = self.kite.margins(segment=self.kite.MARGIN_NFO)
                available = float(margins.get("net", 0.0) or 0.0)
                if available < required_margin:
                    self.logger(
                        f"⚠️ [NIFTY ATM {opt_type}] Insufficient margin for {tsym} — "
                        f"need ₹{required_margin:,.0f}, have ₹{available:,.0f}. Aborting entry."
                    )
                    if self.telegram:
                        try:
                            self.telegram.send_message(
                                f"⚠️ <b>Entry Blocked: Insufficient Margin</b>\n\n"
                                f"<b>Symbol:</b> {tsym}\n"
                                f"<b>Required:</b> ₹{required_margin:,.0f}\n"
                                f"<b>Available:</b> ₹{available:,.0f}"
                            )
                        except Exception:
                            pass
                    return
            except Exception as me:
                self.logger(f"⚠️ [NIFTY ATM {opt_type}] Could not check margins: {me} — proceeding anyway")

        # Execute via ExecutionEngine
        if self.execution_engine:
            res = self.execution_engine.place_entry_order(tsym, lot, live_price, "BUY")
            if res["status"] != "COMPLETE":
                self.logger(f"❌ [NIFTY ATM {opt_type}] Order failed: {res.get('error', res['status'])}")
                return
            live_price = res["fill_price"]
            lot = res.get("fill_qty", lot)
        
        sl = max(0.50, live_price * 0.75)  # 25% option stop-loss
        pos = IndexPosition("1B_ORB_OPTIONS", opt_type, "OPTIONS", tsym, live_price, sl, lot, now_ist(), spot)

        # Subscribe option token to WebSocket BEFORE creating the position.
        # If subscription fails, we abort the entry entirely — holding a position
        # without a live SL feed is more dangerous than missing the trade.
        if self.bot_controller and self.bot_controller.ticker:
            try:
                self.bot_controller.ticker.subscribe([token])
                self.bot_controller.ticker.set_mode(self.bot_controller.ticker.MODE_FULL, [token])
                self.bot_controller.token_to_symbol[token] = f"OPT_{tsym}"
                self.last_option_ltp[tsym] = live_price  # Seed with entry price until first tick
                self.logger(f"📡 Subscribed NIFTY option token {token} ({tsym}) to WebSocket")
            except Exception as e:
                self.logger(f"❌ [NIFTY ATM {opt_type}] ABORTING ENTRY — failed to subscribe option token {token} ({tsym}): {e}")
                if self.telegram:
                    try:
                        self.telegram.send_message(
                            f"❌ <b>NIFTY {opt_type} Entry Aborted</b>\n\n"
                            f"Could not subscribe option token to WebSocket feed.\n"
                            f"<b>Symbol:</b> {tsym}\n"
                            f"<b>Error:</b> {e}\n\n"
                            f"<i>Entry skipped to avoid holding an unmonitored position.</i>"
                        )
                    except Exception:
                        pass
                return  # Abort — no position created, no P&L risk
        else:
            # No ticker available — seed LTP dict but warn that SL monitoring may not work
            self.last_option_ltp[tsym] = live_price
            self.logger(f"⚠️ [NIFTY ATM {opt_type}] No ticker available to subscribe {tsym} — SL monitoring via REST fallback only")

        self.positions.append(pos)
        self._notify_entry(pos, f"NIFTY ATM {opt_type} ({tsym} @ ₹{live_price:.2f})")

    def _notify_entry(self, pos: IndexPosition, desc: str):
        trade_amount = pos.quantity * pos.entry_price
        emoji = "🟢" if "LONG" in pos.position_type or "CALL" in pos.position_type else "🔴"
        print(f"\n{'='*60}", flush=True)
        print(f"{emoji} [NIFTY ENTRY] {pos.strategy} | {pos.tradingsymbol} | {desc}", flush=True)
        print(f"   Entry: ₹{pos.entry_price:.2f} | Initial SL: ₹{pos.initial_sl:.2f} | Qty: {pos.quantity}", flush=True)
        print(f"   Amount Required: ₹{trade_amount:,.2f}", flush=True)
        print(f"{'='*60}\n", flush=True)
        
        if self.telegram:
            self.telegram.notify_trade_entry("NIFTY", pos.position_type, pos.spot_at_entry, pos.entry_price, 0, pos.initial_sl, pos.quantity, f"{pos.strategy} ({desc})", amount_required=trade_amount)

    def _close_position(self, pos: IndexPosition, exit_price: float, reason: str):
        if pos not in self.positions: return

        # Determine transaction type for exit
        txn = "SELL" if pos.instrument == "OPTIONS" or pos.position_type == "LONG" else "BUY"
        confirmed_fill = False
        fill_price = exit_price  # Default to decision price (paper or fallback)

        if self.execution_engine:
            res = self.execution_engine.place_exit_order(pos.tradingsymbol, pos.quantity, txn, exit_price)

            if res["status"] == "COMPLETE" and res["fill_price"] > 0:
                fill_price = res["fill_price"]
                confirmed_fill = True
            elif not res.get("paper"):
                # Live exit failed — retry once with MARKET order to guarantee fill
                self.logger(f"⚠️ [EXIT FAILED] {pos.tradingsymbol} {reason}: {res.get('status')} — retrying with MARKET order...")
                retry_res = self.execution_engine.place_exit_order(pos.tradingsymbol, pos.quantity, txn, 0.0)
                if retry_res["status"] == "COMPLETE" and retry_res["fill_price"] > 0:
                    fill_price = retry_res["fill_price"]
                    confirmed_fill = True
                    self.logger(f"✅ [EXIT RETRY OK] {pos.tradingsymbol} filled @ ₹{fill_price:.2f}")
                else:
                    # Both attempts failed — keep position alive and alert loudly
                    pos.orphaned = True
                    alert_msg = (
                        f"🚨 <b>EXIT ORDER FAILED — MANUAL INTERVENTION REQUIRED</b>\n\n"
                        f"<b>Symbol:</b> {pos.tradingsymbol}\n"
                        f"<b>Reason triggered:</b> {reason}\n"
                        f"<b>Order status:</b> {retry_res.get('status', 'UNKNOWN')}\n"
                        f"<b>Error:</b> {retry_res.get('error', 'No error detail')}\n\n"
                        f"Position is <b>still open at broker</b>. Bot has NOT booked P&amp;L.\n"
                        f"Please close manually in Zerodha immediately."
                    )
                    self.logger(f"🚨 EXIT FAILED FOR {pos.tradingsymbol} — ORPHANED POSITION. Manual action required!")
                    if self.telegram:
                        try:
                            self.telegram.send_message(alert_msg)
                        except Exception:
                            pass
                    return  # Do NOT remove from positions, do NOT book P&L
            else:
                # Paper trading — always treat as confirmed at decision price
                confirmed_fill = True

        else:
            # No execution engine — paper/manual mode, treat as confirmed
            confirmed_fill = True

        # Only reach here on a confirmed fill (or paper mode)
        self.positions.remove(pos)
        pos.close(fill_price, reason)
        self.closed_trades.append(pos)

        # Record trade P&L to capital tracker (also persists daily_realized_pnl)
        if self.capital_tracker:
            self.capital_tracker.record_trade_pnl(pos.net_pnl)

        emoji = "✅" if pos.net_pnl > 0 else "🛑"
        print(f"\n{emoji} [NIFTY EXIT] {pos.tradingsymbol} - {reason}", flush=True)
        print(f"   Entry: ₹{pos.entry_price:.2f} → Exit: ₹{fill_price:.2f} | Net P&L: ₹{pos.net_pnl:+,.2f}", flush=True)
        print(f"{'='*60}\n", flush=True)

        if self.telegram:
            self.telegram.notify_trade_exit("NIFTY", pos.position_type, pos.spot_at_entry, pos.entry_price, fill_price, pos.net_pnl, reason)

        # Unsubscribe option token from WebSocket
        if pos.instrument == "OPTIONS" and self.bot_controller and self.bot_controller.ticker:
            try:
                for tok, sym in list(self.bot_controller.token_to_symbol.items()):
                    if sym == f"OPT_{pos.tradingsymbol}":
                        self.bot_controller.ticker.unsubscribe([tok])
                        self.bot_controller.token_to_symbol.pop(tok, None)
                        break
                self.last_option_ltp.pop(pos.tradingsymbol, None)
            except Exception:
                pass

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
        self.nifty_fut_symbol: Optional[str] = None  # Nearest-expiry NIFTY futures tradingsymbol
        self.token_to_symbol: Dict[int, str] = {self.spot_token: "NIFTY"}
        self._needs_restart = False
        self._needs_reauth = False
        self._ws_last_error = ""
        self._ws_connected = False
        self._watchdog_retries = 0
        self.log_file = LOG_DIR / f"index_{now_ist().strftime('%Y%m%d')}.log"
        self.capital_tracker = CapitalTracker(base_capital=STRATEGY_CEILING, options_min_capital=25000.0, futures_min_capital=120000.0, logger=self._log)
        self.execution_engine = ExecutionEngine(paper_trading=PAPER_TRADING, logger=self._log)
        self.trader = IndexTrader(self._log, None, None, self.telegram, self.capital_tracker, self, self.execution_engine)

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
                self.execution_engine.kite = self.kite
                return True
            except Exception:
                pass
        token = auto_login.login()
        if token:
            self.kite = auto_login.kite
            self.execution_engine.kite = self.kite
            self._log("✅ Fresh auto-login successful!")
            return True
        return False

    def load_market_metadata(self):
        """Load live NIFTY spot token, lot size, and nearest futures contract from Kite API."""
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
            self.token_to_symbol[self.spot_token] = "NIFTY"

        # 3. Resolve nearest-expiry NIFTY futures contract and store tradingsymbol.
        #    Futures entries must use futures LTP, not spot price — they trade 50-150 pts apart.
        nifty_futs = self.nfo_df[
            (self.nfo_df['name'] == 'NIFTY') & (self.nfo_df['instrument_type'] == 'FUT')
        ].sort_values('expiry')
        if not nifty_futs.empty:
            self.trader.lot_size = int(nifty_futs.iloc[0]['lot_size'])
            self.nifty_fut_symbol = nifty_futs.iloc[0]['tradingsymbol']
            self.trader.nifty_fut_symbol = self.nifty_fut_symbol

        self.trader.kite = self.kite
        self.trader.nfo_df = self.nfo_df
        self._log(f"   ✓ NIFTY Spot Token: {self.spot_token} | Lot Size: {self.trader.lot_size} | Futures: {self.nifty_fut_symbol}")

    def fetch_historical(self):
        if not self.kite: return
        self._log("📊 Fetching historical 15M NIFTY candles (60-day window for valid EMA-200)...")
        to_d = now_ist()
        from_d = to_d - timedelta(days=60)  # 10 days → 60 days; need >=200 candles for EMA-200
        data = self.kite.historical_data(self.spot_token, from_date=from_d, to_date=to_d, interval="15minute")
        self.trader.candles = []  # Clear on retry to avoid duplicates
        for c in data[-260:]:   # Keep last 260 candles (~65 trading days × ~4 candles/hr)
            self.trader.candles.append({
                "timestamp": c["date"], "open": c["open"], "high": c["high"],
                "low": c["low"], "close": c["close"], "volume": c.get("volume", 0)
            })
        self._log(f"   ✓ Loaded {len(self.trader.candles)} historical 15M candles")

    def start_live_feed(self):
        creds = load_credentials()
        self.ticker = KiteTicker(creds["api_key"], self.kite.access_token)
        self._last_tick_time = None  # Set only when first real tick arrives
        self._feed_start_time = now_ist()  # Initialized at launch so watchdog can detect starvation
        self._ws_connected = False
        
        def on_connect(ws, resp):
            tokens = list(self.token_to_symbol.keys())
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_FULL, tokens)
            self._feed_start_time = now_ist()  # Mark when feed actually connected
            self._ws_connected = True
            self._ws_last_error = ""
            self._log(f"✅ WebSocket connected — subscribed to {len(tokens)} tokens.")
            
        def on_ticks(ws, ticks):
            self._last_tick_time = now_ist()
            now = now_ist()
            for t in ticks:
                tok = t.get("instrument_token")
                if tok in self.token_to_symbol:
                    sym = self.token_to_symbol[tok]
                    ltp = t.get("last_price")
                    vol = t.get("volume_traded", 0)
                    if sym.startswith("OPT_"):
                        opt_tsym = sym[4:]
                        if ltp:
                            self.trader.last_option_ltp[opt_tsym] = ltp
                            self.trader.last_option_tick_time[opt_tsym] = now
                    elif sym == "NIFTY" and ltp:
                        self.trader.process_tick(ltp, now, vol)

        def on_error(ws, code, reason):
            err_msg = f"[{code}] {reason}"
            self._ws_last_error = err_msg
            self._log(f"⚠️ WebSocket error {err_msg} — will attempt reconnect.")
            # Only treat as auth failure on explicit 403/Forbidden.
            # Plain 1006 = transient network closure — let KiteTicker's built-in
            # backoff retry handle it. Escalating to reauth on 1006 kills the
            # built-in retry and triggers a Selenium re-login race.
            if "403" in str(reason) or "Forbidden" in str(reason):
                self._needs_reauth = True
                self._needs_restart = True

        def on_close(ws, code, reason):
            self._ws_connected = False
            self._ws_last_error = f"[{code}] {reason}"
            self._log(f"⚠️ WebSocket closed [{code}]: {reason}.")
            if self.is_running and self.is_market_open():
                self._log("🔄 Market is open — waiting for KiteTicker auto-reconnect...")

        def on_reconnect(ws, attempt):
            self._log(f"🔄 WebSocket reconnecting... attempt #{attempt}")

        def on_noreconnect(ws):
            self._log("❌ WebSocket exhausted all reconnect attempts — setting restart flag.")
            self._needs_restart = True

        self.ticker.on_connect = on_connect
        self.ticker.on_ticks = on_ticks
        self.ticker.on_error = on_error
        self.ticker.on_close = on_close
        self.ticker.on_reconnect = on_reconnect
        self.ticker.on_noreconnect = on_noreconnect
        self.ticker.connect(threaded=True)

    def _restart_ticker(self):
        """Hard-restart the KiteTicker, correctly handling the Twisted reactor singleton.

        Problem: KiteTicker.connect(threaded=True) starts reactor.run() in a daemon thread.
        After ticker.close(), the reactor keeps running (reactor.running == True).
        Calling connect() again skips the `if not reactor.running:` block, so the reactor
        thread is never recreated — but connectWS() IS still called which schedules a TCP
        connection inside the already-running reactor. However, factory state from the old
        connection can be stale, so the on_connect callback never fires reliably.

        Fix: After close(), we manually call _create_connection() to build a fresh factory
        with clean state, then use reactor.callFromThread(connectWS, ...) to schedule the
        new WebSocket handshake inside the already-running reactor event loop. This bypasses
        the broken `if not reactor.running:` guard and guarantees the connection is made.
        """
        from twisted.internet import reactor, ssl
        from autobahn.twisted.websocket import connectWS

        self._needs_restart = False
        reauth_needed = getattr(self, "_needs_reauth", False)
        self._needs_reauth = False

        # 1. Close old ticker cleanly (stops retry loop, closes WS — does NOT stop reactor)
        try:
            if self.ticker:
                self.ticker.stop_retry()
                self.ticker._close()
        except Exception:
            pass
        time.sleep(2)  # Let the old connection fully close before rebuilding factory

        # 2. Validate session; re-authenticate if the access token is dead
        is_session_valid = False
        if not reauth_needed and self.kite and getattr(self.kite, "access_token", None):
            try:
                self.kite.profile()
                is_session_valid = True
            except Exception as e:
                self._log(f"⚠️ Access token check failed ({e}) — session expired or invalidated.")
                is_session_valid = False

        if not is_session_valid or reauth_needed:
            self._log("🔄 Re-authenticating with Kite to obtain fresh access token...")
            if self.authenticate():
                self._log("✅ Re-authentication successful! Reconnecting with fresh token.")
            else:
                self._log("❌ Re-authentication failed!")
                return False

        # 3. Build a brand-new KiteTicker with fresh factory state and attach all callbacks
        creds = load_credentials()
        new_ticker = KiteTicker(creds["api_key"], self.kite.access_token)

        # Reset tracking state before the new connection attempt
        self._last_tick_time = None
        self._feed_start_time = now_ist()
        self._ws_connected = False

        def on_connect(ws, resp):
            # Read token_to_symbol INSIDE the callback so it's always current.
            # If captured outside, any option token subscribed after this restart
            # won't be in the list — future auto-reconnects would miss re-subscribing it,
            # silently killing that option's SL feed.
            tokens = list(self.token_to_symbol.keys())
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_FULL, tokens)
            self._feed_start_time = now_ist()
            self._ws_connected = True
            self._ws_last_error = ""
            self._log(f"✅ WebSocket reconnected — subscribed to {len(tokens)} tokens.")

        def on_ticks(ws, ticks):
            self._last_tick_time = now_ist()
            now = now_ist()
            for t in ticks:
                tok = t.get("instrument_token")
                if tok in self.token_to_symbol:
                    sym = self.token_to_symbol[tok]
                    ltp = t.get("last_price")
                    vol = t.get("volume_traded", 0)
                    if sym.startswith("OPT_"):
                        opt_tsym = sym[4:]
                        if ltp:
                            self.trader.last_option_ltp[opt_tsym] = ltp
                            self.trader.last_option_tick_time[opt_tsym] = now
                    elif sym == "NIFTY" and ltp:
                        self.trader.process_tick(ltp, now, vol)

        def on_error(ws, code, reason):
            err_msg = f"[{code}] {reason}"
            self._ws_last_error = err_msg
            self._log(f"⚠️ WebSocket error {err_msg} — will attempt reconnect.")
            # Only treat as auth failure on explicit 403/Forbidden (not plain 1006)
            if "403" in str(reason) or "Forbidden" in str(reason):
                self._needs_reauth = True
                self._needs_restart = True

        def on_close(ws, code, reason):
            self._ws_connected = False
            self._ws_last_error = f"[{code}] {reason}"
            self._log(f"⚠️ WebSocket closed [{code}]: {reason}.")
            if self.is_running and self.is_market_open():
                self._log("🔄 Market is open — waiting for KiteTicker auto-reconnect...")

        def on_reconnect(ws, attempt):
            self._log(f"🔄 WebSocket reconnecting... attempt #{attempt}")

        def on_noreconnect(ws):
            self._log("❌ WebSocket exhausted all reconnect attempts — setting restart flag.")
            self._needs_restart = True

        new_ticker.on_connect = on_connect
        new_ticker.on_ticks = on_ticks
        new_ticker.on_error = on_error
        new_ticker.on_close = on_close
        new_ticker.on_reconnect = on_reconnect
        new_ticker.on_noreconnect = on_noreconnect

        # 4. Build fresh factory (clean state, no stale connection artifacts)
        new_ticker._create_connection(
            new_ticker.socket_url,
            useragent=new_ticker._user_agent(),
            headers={"X-Kite-Version": "3"},
        )
        self.ticker = new_ticker

        # 5. Schedule the WebSocket handshake into the ALREADY-RUNNING reactor.
        #    connectWS() alone just queues the TCP connect — the reactor picks it up
        #    on its next iteration. Since reactor.running == True, this is the only
        #    correct way to reconnect without spawning a second reactor thread.
        context_factory = ssl.ClientContextFactory() if new_ticker.factory.isSecure else None
        reactor.callFromThread(
            connectWS,
            new_ticker.factory,
            contextFactory=context_factory,
            timeout=new_ticker.connect_timeout,
        )

        self._log("✅ WebSocket reconnection scheduled in Twisted reactor (reactor.callFromThread).")
        return True

    def is_market_open(self) -> bool:
        now = now_ist()
        if now.weekday() >= 5: return False
        return now.replace(hour=9, minute=15, second=0) <= now <= now.replace(hour=15, minute=30, second=0)

    def generate_report(self):
        today = now_ist().strftime("%Y-%m-%d")
        # Force-close any remaining open positions and P&L-account them before generating report.
        # Use trader.lock to prevent a race with the WebSocket tick thread.
        with self.trader.lock:
            for pos in list(self.trader.positions):
                if getattr(pos, "orphaned", False):
                    self._log(f"⚠️ ORPHANED position {pos.tradingsymbol} — skipping EOD close (still open at broker!)")
                    continue
                self._log(f"⚠️ Force-closing open NIFTY position {pos.tradingsymbol} before EOD report")
                try:
                    # Resolve best available exit price — don't use entry_price for EOD accounting
                    eod_price = pos.entry_price  # fallback
                    if pos.instrument == "OPTIONS":
                        opt_ltp = self.trader.last_option_ltp.get(pos.tradingsymbol, 0.0)
                        if opt_ltp > 0:
                            eod_price = opt_ltp
                    elif pos.instrument == "FUTURES" and self.kite and pos.tradingsymbol != "NIFTY_FUT":
                        try:
                            q = self.kite.ltp([f"NFO:{pos.tradingsymbol}"])
                            fut_ltp = q.get(f"NFO:{pos.tradingsymbol}", {}).get("last_price", 0.0)
                            if fut_ltp > 0:
                                eod_price = fut_ltp
                        except Exception:
                            pass
                    self.trader._close_position(pos, eod_price, "EOD_REPORT_CLOSE")
                except Exception as e:
                    self._log(f"❌ Failed to close {pos.tradingsymbol} before EOD report: {e}")

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

    def stop(self):
        self.is_running = False
        self.stop_event.set()
        if self.ticker:
            try:
                self.ticker.close()
            except Exception:
                pass


if __name__ == "__main__":
    from app import run_trading_bot
    run_trading_bot()
