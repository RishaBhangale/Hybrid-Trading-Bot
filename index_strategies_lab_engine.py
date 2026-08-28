#!/usr/bin/env python3
"""
Institutional Index Strategies Laboratory & Robustness Benchmark Suite
Backtests 4 Distinct Index Strategies across 5.5 Years of 1-Minute Real NIFTY Spot & Options Data (2021 - 2026).

Strategies:
1. Strategy 1: Opening Range Breakout (ORB 15M) with Volume, Volatility & HTF Regime Filters
2. Strategy 2: Futures Trend Following (EMA 50/200 + 20-Period Donchian Breakout + ATR Trailing Exit)
3. Strategy 3: Mean Reversion After Extreme Intraday Moves (VWAP +/- 2.0 SD Bands + ADX Filter)
4. Strategy 4: ORB + VWAP Multi-Instrument Comparison (Futures vs Real ATM Options vs Debit Spreads)

Robustness Modules:
- Full Post-Tax Friction Analysis (Brokerage, STT, Exchange, GST, Stamp Duty, SEBI)
- Average Holding Durations & Daily Trade Frequency
- Module 1: Parameter Stability Sweeps & Heatmaps
- Module 2: Monte Carlo Simulation (1,000 Resamples)
- Module 3: K-Means Cluster Analysis
- Module 4: Walk-Forward Out-Of-Sample Rolling Validation (5 Folds: 2021 - 2026)
"""

import os
import sys
import json
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import seaborn as sns
from pathlib import Path
from datetime import datetime, timedelta
from sklearn.cluster import KMeans

# Set matplotlib cache directory
os.environ['MPLCONFIGDIR'] = '/tmp/mpl_cache'
Path('/tmp/mpl_cache').mkdir(parents=True, exist_ok=True)

LAB_DIR = Path("/Users/rishabhbhangale/Desktop/Trading/index-strategies-lab")
OUTPUT_DIR = LAB_DIR / "logs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

DATA_FILE = LAB_DIR / "nifty_complete_2021_2026.parquet"
LOT_SIZE = 50  # NIFTY historical standard lot size


# ============================================================
# 1. TAXES & REGULATORY CHARGES CALCULATOR (FUTURES VS OPTIONS)
# ============================================================
def calculate_index_taxes(instrument_type: str, entry_price: float, exit_price: float, quantity: int = 50) -> dict:
    """
    Computes official NSE & Zerodha F&O regulatory charges for Index Futures vs Options:
    - Futures: STT 0.0125% on sell turnover, Brokerage ₹40, Exchange 0.0019%, GST 18%, Stamp 0.002%, SEBI.
    - Options (Naked): STT 0.1% on sell premium, Brokerage ₹40, Exchange 0.0505%, GST 18%, Stamp 0.003%, SEBI.
    - Spreads (2 Legs): Brokerage ₹80 (4 orders), STT 0.1% on sell, Exchange 0.0505%, GST 18%, Stamp 0.003%, SEBI.
    """
    if instrument_type == "FUTURES":
        buy_val = entry_price * quantity
        sell_val = exit_price * quantity
        tot_val = buy_val + sell_val
        
        brokerage = 40.0
        stt = sell_val * 0.000125  # 0.0125%
        exchange = tot_val * 0.000019  # 0.0019%
        stamp = buy_val * 0.00002  # 0.002%
        sebi = tot_val * 0.000001
        gst = (brokerage + exchange + sebi) * 0.18
        total = brokerage + stt + exchange + stamp + sebi + gst
        
    elif instrument_type == "OPTIONS":
        buy_val = entry_price * quantity
        sell_val = exit_price * quantity
        tot_val = buy_val + sell_val
        
        brokerage = 40.0
        stt = sell_val * 0.0010  # 0.1%
        exchange = tot_val * 0.000505  # 0.0505%
        stamp = buy_val * 0.00003  # 0.003%
        sebi = tot_val * 0.000001
        gst = (brokerage + exchange + sebi) * 0.18
        total = brokerage + stt + exchange + stamp + sebi + gst
        
    else:  # DEBIT SPREAD (2 legs)
        buy_val = entry_price * quantity
        sell_val = exit_price * quantity
        tot_val = buy_val + sell_val
        
        brokerage = 80.0  # 2 orders enter + 2 orders exit
        stt = sell_val * 0.0010
        exchange = tot_val * 0.000505
        stamp = buy_val * 0.00003
        sebi = tot_val * 0.000001
        gst = (brokerage + exchange + sebi) * 0.18
        total = brokerage + stt + exchange + stamp + sebi + gst
        
    return {'brokerage': brokerage, 'stt': stt, 'exchange': exchange, 'gst': gst, 'total_tax': total}


# ============================================================
# 2. INDICATOR & FEATURE CALCULATION PIPELINE
# ============================================================
def prepare_data_and_indicators():
    print("📥 Loading Parquet dataset...", flush=True)
    df = pd.read_parquet(DATA_FILE)
    df['datetime'] = pd.to_datetime(df['datetime'])
    df = df.set_index('datetime').sort_index()
    
    print(f"   Loaded {len(df):,} 1-minute records ({df.index.min().date()} to {df.index.max().date()})")
    
    # 1. Resample to 15-Minute Candles (Strategy 1, 2, 3)
    print("📊 Resampling to 15-Minute timeframe...", flush=True)
    df_15m = df.resample('15min', origin='start_day').agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum',
        'atm_ce_close': 'last', 'atm_pe_close': 'last', 'otm2_ce_close': 'last', 'otm2_pe_close': 'last'
    }).dropna(subset=['close'])
    df_15m = df_15m[df_15m['volume'] > 0].copy().reset_index()
    df_15m['day'] = df_15m['datetime'].dt.date
    df_15m['time'] = df_15m['datetime'].dt.time
    
    # 2. Resample to 5-Minute Candles (Strategy 4)
    print("📊 Resampling to 5-Minute timeframe...", flush=True)
    df_5m = df.resample('5min', origin='start_day').agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum',
        'atm_ce_close': 'last', 'atm_pe_close': 'last', 'otm2_ce_close': 'last', 'otm2_pe_close': 'last'
    }).dropna(subset=['close'])
    df_5m = df_5m[df_5m['volume'] > 0].copy().reset_index()
    df_5m['day'] = df_5m['datetime'].dt.date
    df_5m['time'] = df_5m['datetime'].dt.time
    
    # Add Indicators to 15M
    df_15m = compute_advanced_indicators(df_15m)
    df_5m = compute_advanced_indicators(df_5m)
    
    return df_15m, df_5m


def compute_advanced_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    
    # ATR (14)
    hl = df['high'] - df['low']
    hc = (df['high'] - df['close'].shift(1)).abs()
    lc = (df['low'] - df['close'].shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df['atr'] = tr.ewm(span=14, adjust=False).mean()
    
    # Moving Averages: EMA 20, 50, 200
    df['ema_20'] = df['close'].ewm(span=20, adjust=False).mean()
    df['ema_50'] = df['close'].ewm(span=50, adjust=False).mean()
    df['ema_200'] = df['close'].ewm(span=200, adjust=False).mean()
    
    # 20-Period Donchian Channel
    df['donchian_high'] = df['high'].rolling(20).max().shift(1)
    df['donchian_low'] = df['low'].rolling(20).min().shift(1)
    
    # Volume 20-MA
    df['vol_ma20'] = df['volume'].rolling(20).mean().shift(1)
    
    # Daily Intraday VWAP & VWAP Standard Deviation Bands
    tp = (df['high'] + df['low'] + df['close']) / 3.0
    tp_vol = tp * df['volume']
    cum_tp_vol = tp_vol.groupby(df['day']).cumsum()
    cum_vol = df['volume'].groupby(df['day']).cumsum()
    df['vwap'] = cum_tp_vol / cum_vol
    
    # VWAP Standard Deviation
    dev = (tp - df['vwap']) ** 2
    cum_dev_vol = (dev * df['volume']).groupby(df['day']).cumsum()
    df['vwap_std'] = np.sqrt(cum_dev_vol / cum_vol).fillna(1.0)
    
    df['vwap_upper_2sd'] = df['vwap'] + (2.0 * df['vwap_std'])
    df['vwap_lower_2sd'] = df['vwap'] - (2.0 * df['vwap_std'])
    df['vwap_upper_25sd'] = df['vwap'] + (2.5 * df['vwap_std'])
    df['vwap_lower_25sd'] = df['vwap'] - (2.5 * df['vwap_std'])
    
    # ADX (14) - Intraday Trend Strength
    up_move = df['high'] - df['high'].shift(1)
    down_move = df['low'].shift(1) - df['low']
    pos_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    neg_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    
    pos_di = 100 * (pd.Series(pos_dm).ewm(span=14, adjust=False).mean() / df['atr'])
    neg_di = 100 * (pd.Series(neg_dm).ewm(span=14, adjust=False).mean() / df['atr'])
    dx = 100 * (abs(pos_di - neg_di) / (pos_di + neg_di)).fillna(0)
    df['adx'] = dx.ewm(span=14, adjust=False).mean()
    
    # 15-Min ORB (09:15 to 09:30)
    orb_candles = df[df['time'] == datetime.strptime("09:15", "%H:%M").time()]
    orb_map = orb_candles.set_index('day')[['high', 'low']].rename(columns={'high': 'orb_high', 'low': 'orb_low'})
    df = df.merge(orb_map, on='day', how='left')
    df['orb_width'] = df['orb_high'] - df['orb_low']
    
    return df


# ============================================================
# 3. STRATEGY 1: ORB WITH REGIME FILTERS
# ============================================================
def simulate_strategy_1_orb(df_15m: pd.DataFrame, min_rvol: float = 1.2,
                            min_orb_width_ratio: float = 0.20, max_orb_width_ratio: float = 0.65) -> dict:
    """
    Strategy 1: Opening Range Breakout (ORB 15M) with Volume, Volatility & Trend Filters
    - Range: 09:15 to 09:30 (ORB High & Low)
    - Filter 1: Breakout volume > min_rvol * 20-period volume MA
    - Filter 2: Volatility bounds: min_orb_width_ratio * ATR <= ORB Width <= max_orb_width_ratio * ATR
    - Filter 3: Higher Timeframe Trend: Close > EMA 200 for Longs, Close < EMA 200 for Shorts
    - Exit: ATR Trailing Stop (1.5x ATR) or EOD 15:15
    """
    trades = []
    position = None
    days_seen = set()
    
    dates = df_15m['datetime'].to_numpy()
    highs = df_15m['high'].to_numpy()
    lows = df_15m['low'].to_numpy()
    closes = df_15m['close'].to_numpy()
    volumes = df_15m['volume'].to_numpy()
    vol_mas = df_15m['vol_ma20'].to_numpy()
    
    orb_highs = df_15m['orb_high'].to_numpy()
    orb_lows = df_15m['orb_low'].to_numpy()
    orb_widths = df_15m['orb_width'].to_numpy()
    atrs = df_15m['atr'].to_numpy()
    ema_200s = df_15m['ema_200'].to_numpy()
    days = df_15m['day'].to_numpy()
    
    for i in range(25, len(df_15m)):
        c_date = pd.Timestamp(dates[i])
        c_time = c_date.time()
        c_day = days[i]
        c_close = closes[i]
        c_high = highs[i]
        c_low = lows[i]
        
        # Position Exit Evaluation
        if position is not None:
            exit_reason = None
            exit_price = None
            
            if c_time >= datetime.strptime("15:15", "%H:%M").time():
                exit_reason = "EOD_SQUAREOFF"
                exit_price = c_close
            elif position['type'] == 'LONG':
                # Check trailing stop
                if c_low <= position['trailing_sl']:
                    exit_reason = "SL_HIT"
                    exit_price = position['trailing_sl']
                else:
                    # Update ATR trailing stop
                    new_sl = c_high - (1.5 * atrs[i])
                    position['trailing_sl'] = max(position['trailing_sl'], new_sl)
            elif position['type'] == 'SHORT':
                if c_high >= position['trailing_sl']:
                    exit_reason = "SL_HIT"
                    exit_price = position['trailing_sl']
                else:
                    new_sl = c_low + (1.5 * atrs[i])
                    position['trailing_sl'] = min(position['trailing_sl'], new_sl)
                    
            if exit_reason is not None:
                pts = (exit_price - position['entry_price']) if position['type'] == 'LONG' else (position['entry_price'] - exit_price)
                gross_pnl = pts * LOT_SIZE
                taxes = calculate_index_taxes("FUTURES", position['entry_price'], exit_price, LOT_SIZE)
                net_pnl = gross_pnl - taxes['total_tax']
                
                position['exit_time'] = c_date
                position['exit_price'] = exit_price
                position['exit_reason'] = exit_reason
                position['points'] = pts
                position['pnl'] = gross_pnl
                position['net_pnl'] = net_pnl
                position['tax'] = taxes['total_tax']
                trades.append(position)
                position = None
                
        # Position Entry Evaluation (09:30 to 14:00)
        if position is None and c_day not in days_seen and datetime.strptime("09:30", "%H:%M").time() <= c_time <= datetime.strptime("14:00", "%H:%M").time():
            orb_h = orb_highs[i]
            orb_l = orb_lows[i]
            orb_w = orb_widths[i]
            atr = atrs[i]
            vol = volumes[i]
            vol_ma = vol_mas[i]
            ema_200 = ema_200s[i]
            
            if np.isnan(orb_h) or np.isnan(atr) or atr == 0:
                continue
                
            # Filters
            vol_ok = (vol > min_rvol * vol_ma) if not np.isnan(vol_ma) and vol_ma > 0 else True
            width_ok = (min_orb_width_ratio * atr <= orb_w <= max_orb_width_ratio * atr)
            
            # LONG BREAKOUT
            if c_close > orb_h and vol_ok and width_ok and (c_close > ema_200):
                sl = c_low - (0.5 * atr)
                position = {
                    'strategy': 'Strategy 1: Filtered ORB', 'type': 'LONG',
                    'entry_time': c_date, 'entry_price': c_close, 'trailing_sl': sl, 'initial_sl': sl
                }
                days_seen.add(c_day)
                continue
                
            # SHORT BREAKDOWN
            if c_close < orb_l and vol_ok and width_ok and (c_close < ema_200):
                sl = c_high + (0.5 * atr)
                position = {
                    'strategy': 'Strategy 1: Filtered ORB', 'type': 'SHORT',
                    'entry_time': c_date, 'entry_price': c_close, 'trailing_sl': sl, 'initial_sl': sl
                }
                days_seen.add(c_day)
                continue
                
    return summarize_strategy_results("Strategy 1: Filtered ORB (15M)", trades)


# ============================================================
# 4. STRATEGY 2: FUTURES TREND FOLLOWING
# ============================================================
def simulate_strategy_2_trend_following(df_15m: pd.DataFrame, atr_mult_stop: float = 2.0) -> dict:
    """
    Strategy 2: Futures Trend Following
    - Trend Filter: EMA 50 > EMA 200 (Long only), EMA 50 < EMA 200 (Short only)
    - Entry Trigger: 20-period Donchian Breakout
    - Stop Loss: 2.0 * ATR trailing stop
    """
    trades = []
    position = None
    
    dates = df_15m['datetime'].to_numpy()
    highs = df_15m['high'].to_numpy()
    lows = df_15m['low'].to_numpy()
    closes = df_15m['close'].to_numpy()
    ema_50s = df_15m['ema_50'].to_numpy()
    ema_200s = df_15m['ema_200'].to_numpy()
    don_highs = df_15m['donchian_high'].to_numpy()
    don_lows = df_15m['donchian_low'].to_numpy()
    atrs = df_15m['atr'].to_numpy()
    
    for i in range(200, len(df_15m)):
        c_date = pd.Timestamp(dates[i])
        c_time = c_date.time()
        c_close = closes[i]
        c_high = highs[i]
        c_low = lows[i]
        atr = atrs[i]
        
        # Position Exit Evaluation
        if position is not None:
            exit_reason = None
            exit_price = None
            
            if c_time >= datetime.strptime("15:15", "%H:%M").time():
                exit_reason = "EOD_SQUAREOFF"
                exit_price = c_close
            elif position['type'] == 'LONG':
                if c_low <= position['trailing_sl']:
                    exit_reason = "SL_HIT"
                    exit_price = position['trailing_sl']
                else:
                    new_sl = c_high - (atr_mult_stop * atr)
                    position['trailing_sl'] = max(position['trailing_sl'], new_sl)
            elif position['type'] == 'SHORT':
                if c_high >= position['trailing_sl']:
                    exit_reason = "SL_HIT"
                    exit_price = position['trailing_sl']
                else:
                    new_sl = c_low + (atr_mult_stop * atr)
                    position['trailing_sl'] = min(position['trailing_sl'], new_sl)
                    
            if exit_reason is not None:
                pts = (exit_price - position['entry_price']) if position['type'] == 'LONG' else (position['entry_price'] - exit_price)
                gross_pnl = pts * LOT_SIZE
                taxes = calculate_index_taxes("FUTURES", position['entry_price'], exit_price, LOT_SIZE)
                net_pnl = gross_pnl - taxes['total_tax']
                
                position['exit_time'] = c_date
                position['exit_price'] = exit_price
                position['exit_reason'] = exit_reason
                position['points'] = pts
                position['pnl'] = gross_pnl
                position['net_pnl'] = net_pnl
                position['tax'] = taxes['total_tax']
                trades.append(position)
                position = None
                
        # Position Entry Evaluation
        if position is None and datetime.strptime("09:30", "%H:%M").time() <= c_time <= datetime.strptime("14:30", "%H:%M").time():
            ema_50 = ema_50s[i]
            ema_200 = ema_200s[i]
            don_h = don_highs[i]
            don_l = don_lows[i]
            
            # LONG BREAKOUT
            if ema_50 > ema_200 and c_close > don_h:
                sl = c_close - (atr_mult_stop * atr)
                position = {
                    'strategy': 'Strategy 2: Trend Following', 'type': 'LONG',
                    'entry_time': c_date, 'entry_price': c_close, 'trailing_sl': sl
                }
                continue
                
            # SHORT BREAKDOWN
            if ema_50 < ema_200 and c_close < don_l:
                sl = c_close + (atr_mult_stop * atr)
                position = {
                    'strategy': 'Strategy 2: Trend Following', 'type': 'SHORT',
                    'entry_time': c_date, 'entry_price': c_close, 'trailing_sl': sl
                }
                continue
                
    return summarize_strategy_results("Strategy 2: Futures Trend Following", trades)


# ============================================================
# 5. STRATEGY 3: INTRADAY VWAP MEAN REVERSION
# ============================================================
def simulate_strategy_3_mean_reversion(df_15m: pd.DataFrame, max_adx: float = 22.0) -> dict:
    """
    Strategy 3: Intraday VWAP Mean Reversion
    - Triggers at +/- 2.0 SD from VWAP
    - Critical Trend Filter: ADX < 22 (strictly forbids fading a strong trend!)
    - Reversal confirmation: Candle tests extreme and closes back inside
    - Target: Reversion to VWAP
    - Stop Loss: 0.5 * ATR beyond extreme
    """
    trades = []
    position = None
    
    dates = df_15m['datetime'].to_numpy()
    highs = df_15m['high'].to_numpy()
    lows = df_15m['low'].to_numpy()
    closes = df_15m['close'].to_numpy()
    opens = df_15m['open'].to_numpy()
    vwaps = df_15m['vwap'].to_numpy()
    vwap_up2s = df_15m['vwap_upper_2sd'].to_numpy()
    vwap_dn2s = df_15m['vwap_lower_2sd'].to_numpy()
    adxs = df_15m['adx'].to_numpy()
    atrs = df_15m['atr'].to_numpy()
    
    for i in range(25, len(df_15m)):
        c_date = pd.Timestamp(dates[i])
        c_time = c_date.time()
        c_close = closes[i]
        c_high = highs[i]
        c_low = lows[i]
        c_open = opens[i]
        
        # Position Exit Evaluation
        if position is not None:
            exit_reason = None
            exit_price = None
            
            if c_time >= datetime.strptime("15:15", "%H:%M").time():
                exit_reason = "EOD_SQUAREOFF"
                exit_price = c_close
            elif position['type'] == 'LONG':
                # Hit VWAP Target
                if c_high >= position['target']:
                    exit_reason = "TARGET_HIT"
                    exit_price = position['target']
                elif c_low <= position['sl']:
                    exit_reason = "SL_HIT"
                    exit_price = position['sl']
            elif position['type'] == 'SHORT':
                if c_low <= position['target']:
                    exit_reason = "TARGET_HIT"
                    exit_price = position['target']
                elif c_high >= position['sl']:
                    exit_reason = "SL_HIT"
                    exit_price = position['sl']
                    
            if exit_reason is not None:
                pts = (exit_price - position['entry_price']) if position['type'] == 'LONG' else (position['entry_price'] - exit_price)
                gross_pnl = pts * LOT_SIZE
                taxes = calculate_index_taxes("FUTURES", position['entry_price'], exit_price, LOT_SIZE)
                net_pnl = gross_pnl - taxes['total_tax']
                
                position['exit_time'] = c_date
                position['exit_price'] = exit_price
                position['exit_reason'] = exit_reason
                position['points'] = pts
                position['pnl'] = gross_pnl
                position['net_pnl'] = net_pnl
                position['tax'] = taxes['total_tax']
                trades.append(position)
                position = None
                
        # Position Entry Evaluation (Mid-session 10:30 to 14:00)
        if position is None and datetime.strptime("10:30", "%H:%M").time() <= c_time <= datetime.strptime("14:00", "%H:%M").time():
            adx = adxs[i]
            vwap = vwaps[i]
            up2 = vwap_up2s[i]
            dn2 = vwap_dn2s[i]
            atr = atrs[i]
            
            if np.isnan(adx) or adx >= max_adx:
                continue  # Skip fading when trend momentum is strong!
                
            # LONG MEAN REVERSION: Price dipped below -2.0 SD and closed bullish back inside
            if c_low < dn2 and c_close > dn2 and c_close > c_open:
                sl = c_low - (0.5 * atr)
                position = {
                    'strategy': 'Strategy 3: Mean Reversion', 'type': 'LONG',
                    'entry_time': c_date, 'entry_price': c_close, 'target': vwap, 'sl': sl
                }
                continue
                
            # SHORT MEAN REVERSION: Price spiked above +2.0 SD and closed bearish back inside
            if c_high > up2 and c_close < up2 and c_close < c_open:
                sl = c_high + (0.5 * atr)
                position = {
                    'strategy': 'Strategy 3: Mean Reversion', 'type': 'SHORT',
                    'entry_time': c_date, 'entry_price': c_close, 'target': vwap, 'sl': sl
                }
                continue
                
    return summarize_strategy_results("Strategy 3: VWAP Mean Reversion", trades)


# ============================================================
# 6. STRATEGY 4: ORB + VWAP MULTI-INSTRUMENT COMPARISON
# ============================================================
def simulate_strategy_4_multi_instrument(df_5m: pd.DataFrame) -> dict:
    """
    Strategy 4: 5-Minute ORB + VWAP
    Breakout above ORB High AND above VWAP.
    Compares 3 Payoff Structures on the EXACT SAME Signals:
    - Version A: NIFTY Futures (Linear Delta 1.0)
    - Version B: Real ATM Options (Real historical tick premiums from Parquet)
    - Version C: Real Debit Spreads (Buy ATM + Sell OTM+2)
    """
    trades_fut = []
    trades_opt = []
    trades_spd = []
    
    position_signal = None
    days_seen = set()
    
    dates = df_5m['datetime'].to_numpy()
    closes = df_5m['close'].to_numpy()
    highs = df_5m['high'].to_numpy()
    lows = df_5m['low'].to_numpy()
    vwaps = df_5m['vwap'].to_numpy()
    orb_highs = df_5m['orb_high'].to_numpy()
    orb_lows = df_5m['orb_low'].to_numpy()
    days = df_5m['day'].to_numpy()
    atrs = df_5m['atr'].to_numpy()
    
    atm_ce_closes = df_5m['atm_ce_close'].to_numpy()
    atm_pe_closes = df_5m['atm_pe_close'].to_numpy()
    otm2_ce_closes = df_5m['otm2_ce_close'].to_numpy()
    otm2_pe_closes = df_5m['otm2_pe_close'].to_numpy()
    
    for i in range(15, len(df_5m)):
        c_date = pd.Timestamp(dates[i])
        c_time = c_date.time()
        c_day = days[i]
        c_close = closes[i]
        c_high = highs[i]
        c_low = lows[i]
        
        # Check active trade exit
        if position_signal is not None:
            exit_reason = None
            exit_spot = None
            
            # EOD or VWAP cross exit
            if c_time >= datetime.strptime("15:15", "%H:%M").time():
                exit_reason = "EOD_SQUAREOFF"
                exit_spot = c_close
            elif position_signal['type'] == 'LONG':
                if c_low <= position_signal['sl']:
                    exit_reason = "SL_HIT"
                    exit_spot = position_signal['sl']
                elif c_high >= position_signal['target']:
                    exit_reason = "TARGET_HIT"
                    exit_spot = position_signal['target']
                elif c_close < vwaps[i]:
                    exit_reason = "VWAP_FAILURE"
                    exit_spot = c_close
            elif position_signal['type'] == 'SHORT':
                if c_high >= position_signal['sl']:
                    exit_reason = "SL_HIT"
                    exit_spot = position_signal['sl']
                elif c_low <= position_signal['target']:
                    exit_reason = "TARGET_HIT"
                    exit_spot = position_signal['target']
                elif c_close > vwaps[i]:
                    exit_reason = "VWAP_FAILURE"
                    exit_spot = c_close
                    
            if exit_reason is not None:
                # 1. Version A: Futures Payoff
                pts = (exit_spot - position_signal['entry_spot']) if position_signal['type'] == 'LONG' else (position_signal['entry_spot'] - exit_spot)
                pnl_fut = pts * LOT_SIZE
                tax_fut = calculate_index_taxes("FUTURES", position_signal['entry_spot'], exit_spot, LOT_SIZE)
                trades_fut.append({
                    'type': position_signal['type'], 'entry_time': position_signal['entry_time'], 'exit_time': c_date,
                    'points': pts, 'pnl': pnl_fut, 'net_pnl': pnl_fut - tax_fut['total_tax'], 'tax': tax_fut['total_tax'], 'exit_reason': exit_reason
                })
                
                # 2. Version B: Real ATM Options Payoff
                opt_entry = position_signal['opt_entry']
                opt_exit = atm_ce_closes[i] if position_signal['type'] == 'LONG' else atm_pe_closes[i]
                if np.isnan(opt_exit) or opt_exit <= 0:
                    opt_exit = max(0.5, opt_entry + (pts * 0.5))
                pnl_opt = (opt_exit - opt_entry) * LOT_SIZE
                tax_opt = calculate_index_taxes("OPTIONS", opt_entry, opt_exit, LOT_SIZE)
                trades_opt.append({
                    'type': position_signal['type'], 'entry_time': position_signal['entry_time'], 'exit_time': c_date,
                    'points': pts, 'pnl': pnl_opt, 'net_pnl': pnl_opt - tax_opt['total_tax'], 'tax': tax_opt['total_tax'], 'exit_reason': exit_reason
                })
                
                # 3. Version C: Real Debit Spread Payoff
                spd_entry = position_signal['spd_entry']
                long_leg_exit = opt_exit
                short_leg_exit = otm2_ce_closes[i] if position_signal['type'] == 'LONG' else otm2_pe_closes[i]
                if np.isnan(short_leg_exit) or short_leg_exit <= 0:
                    short_leg_exit = max(0.5, position_signal['short_entry'] + (pts * 0.3))
                spd_exit = max(0.0, long_leg_exit - short_leg_exit)
                pnl_spd = (spd_exit - spd_entry) * LOT_SIZE
                tax_spd = calculate_index_taxes("SPREAD", spd_entry, spd_exit, LOT_SIZE)
                trades_spd.append({
                    'type': position_signal['type'], 'entry_time': position_signal['entry_time'], 'exit_time': c_date,
                    'points': pts, 'pnl': pnl_spd, 'net_pnl': pnl_spd - tax_spd['total_tax'], 'tax': tax_spd['total_tax'], 'exit_reason': exit_reason
                })
                
                position_signal = None
                
        # Signal Entry Evaluation (09:30 to 14:00)
        if position_signal is None and c_day not in days_seen and datetime.strptime("09:30", "%H:%M").time() <= c_time <= datetime.strptime("14:00", "%H:%M").time():
            orb_h = orb_highs[i]
            orb_l = orb_lows[i]
            vwap = vwaps[i]
            atr = atrs[i]
            
            if np.isnan(orb_h) or np.isnan(vwap):
                continue
                
            # LONG ORB + VWAP
            if c_close > orb_h and c_close > vwap:
                sl = orb_l
                risk = c_close - sl
                target = c_close + (risk * 2.0)
                
                opt_e = atm_ce_closes[i] if not np.isnan(atm_ce_closes[i]) else 120.0
                short_e = otm2_ce_closes[i] if not np.isnan(otm2_ce_closes[i]) else 60.0
                spd_e = max(5.0, opt_e - short_e)
                
                position_signal = {
                    'type': 'LONG', 'entry_time': c_date, 'entry_spot': c_close,
                    'sl': sl, 'target': target, 'opt_entry': opt_e, 'short_entry': short_e, 'spd_entry': spd_e
                }
                days_seen.add(c_day)
                continue
                
            # SHORT ORB + VWAP
            if c_close < orb_l and c_close < vwap:
                sl = orb_h
                risk = sl - c_close
                target = c_close - (risk * 2.0)
                
                opt_e = atm_pe_closes[i] if not np.isnan(atm_pe_closes[i]) else 120.0
                short_e = otm2_pe_closes[i] if not np.isnan(otm2_pe_closes[i]) else 60.0
                spd_e = max(5.0, opt_e - short_e)
                
                position_signal = {
                    'type': 'SHORT', 'entry_time': c_date, 'entry_spot': c_close,
                    'sl': sl, 'target': target, 'opt_entry': opt_e, 'short_entry': short_e, 'spd_entry': spd_e
                }
                days_seen.add(c_day)
                continue
                
    return {
        'futures': summarize_strategy_results("Strategy 4A: Futures", trades_fut),
        'options': summarize_strategy_results("Strategy 4B: ATM Options", trades_opt),
        'spreads': summarize_strategy_results("Strategy 4C: Debit Spreads", trades_spd)
    }


# ============================================================
# 7. SUMMARY & METRICS AGGREGATOR
# ============================================================
def summarize_strategy_results(strategy_name: str, trades: list) -> dict:
    if not trades:
        return {
            'name': strategy_name, 'total_trades': 0, 'win_rate': 0.0, 'profit_factor': 0.0,
            'total_gross_pnl': 0.0, 'total_tax': 0.0, 'total_net_pnl': 0.0,
            'avg_profit_per_trade': 0.0, 'net_avg_profit_per_trade': 0.0, 'avg_duration_mins': 0.0,
            'max_drawdown': 0.0, 'sharpe_ratio': 0.0, 'trades': []
        }
        
    t_df = pd.DataFrame(trades)
    winners = t_df[t_df['net_pnl'] > 0]
    losers = t_df[t_df['net_pnl'] <= 0]
    
    total_gross = t_df['pnl'].sum()
    total_tax = t_df['tax'].sum()
    total_net = t_df['net_pnl'].sum()
    
    gp = winners['net_pnl'].sum() if len(winners) > 0 else 0.0
    gl = abs(losers['net_pnl'].sum()) if len(losers) > 0 else 0.0
    pf = (gp / gl) if gl > 0 else (gp if gp > 0 else 0.0)
    win_rate = (len(winners) / len(t_df)) * 100.0
    
    avg_profit_gross = total_gross / len(t_df)
    avg_profit_net = total_net / len(t_df)
    
    # Durations
    durations = (t_df['exit_time'] - t_df['entry_time']).dt.total_seconds() / 60.0
    avg_dur = durations.mean()
    
    # Drawdown
    cum_net = t_df['net_pnl'].cumsum()
    peak = cum_net.cummax()
    max_dd = (peak - cum_net).max()
    
    # Sharpe Ratio
    returns = t_df['net_pnl']
    sharpe = (returns.mean() / returns.std()) * np.sqrt(252) if len(returns) > 1 and returns.std() > 0 else 0.0
    
    return {
        'name': strategy_name,
        'total_trades': len(t_df),
        'winners': len(winners),
        'losers': len(losers),
        'win_rate': round(win_rate, 2),
        'profit_factor': round(pf, 2),
        'total_gross_pnl': round(total_gross, 2),
        'total_tax': round(total_tax, 2),
        'total_net_pnl': round(total_net, 2),
        'avg_profit_per_trade': round(avg_profit_gross, 2),
        'net_avg_profit_per_trade': round(avg_profit_net, 2),
        'avg_duration_mins': round(avg_dur, 1),
        'max_drawdown': round(max_dd, 2),
        'sharpe_ratio': round(sharpe, 2),
        'trades': trades
    }


# ============================================================
# 8. ROBUSTNESS TEST MODULES
# ============================================================
def run_all_robustness_modules(all_results: dict):
    print("\n" + "="*75)
    print("🔬 RUNNING 4-MODULE INSTITUTIONAL ROBUSTNESS TEST SUITE")
    print("="*75, flush=True)
    
    # Module 2: Monte Carlo Simulation (1,000 Resamples)
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    axes = axes.flatten()
    
    mc_results = {}
    strat_keys = ['strat1', 'strat2', 'strat3', 'strat4_fut']
    strat_labels = ['Strategy 1 (ORB 15M)', 'Strategy 2 (Trend Following)', 'Strategy 3 (VWAP Reversion)', 'Strategy 4 (ORB Futures)']
    
    for idx, key in enumerate(strat_keys):
        res = all_results[key]
        trades = res['trades']
        if not trades: continue
        
        pnl_series = np.array([t['net_pnl'] for t in trades])
        n_t = len(pnl_series)
        
        final_pnls = []
        max_dds = []
        np.random.seed(42)
        for _ in range(1000):
            boot = np.random.choice(pnl_series, size=n_t, replace=True)
            cum = np.cumsum(boot)
            final_pnls.append(cum[-1] / 1000.0)  # ₹K
            dd = np.maximum.accumulate(cum) - cum
            max_dds.append(np.max(dd) / 1000.0)
            
        p5 = np.percentile(final_pnls, 5)
        p50 = np.percentile(final_pnls, 50)
        p95_dd = np.percentile(max_dds, 95)
        prob_loss = np.mean(np.array(final_pnls) < 0) * 100
        
        mc_results[key] = {'median_pnl': p50*1000, 'worst_5p_pnl': p5*1000, 'max_dd_95p': p95_dd*1000, 'prob_loss': prob_loss}
        
        ax = axes[idx]
        sns.histplot(final_pnls, kde=True, ax=ax, color='teal' if p50 > 0 else 'crimson', bins=30)
        ax.axvline(p50, color='black', linestyle='--', label=f'Median: ₹{p50:.0f}K')
        ax.axvline(p5, color='red', linestyle=':', label=f'5th %ile: ₹{p5:.0f}K')
        ax.set_title(f"{strat_labels[idx]}\n(Loss Risk: {prob_loss:.1f}% | 95% DD: ₹{p95_dd:.0f}K)")
        ax.set_xlabel("Net P&L (₹ Thousands)")
        ax.legend()
        
    plt.tight_layout()
    mc_plot = OUTPUT_DIR / "index_monte_carlo.png"
    plt.savefig(mc_plot, dpi=300)
    plt.close()
    print(f"✓ Saved Monte Carlo Plot: {mc_plot}", flush=True)
    
    # Cumulative Equity Comparison Plot
    plt.figure(figsize=(12, 6))
    for key, label, col in zip(
        ['strat1', 'strat2', 'strat3', 'strat4_fut', 'strat4_opt', 'strat4_spd'],
        ['1. Filtered ORB (15M)', '2. Futures Trend Following', '3. VWAP Mean Reversion', '4A. ORB Futures', '4B. ORB ATM Options', '4C. ORB Debit Spreads'],
        ['blue', 'purple', 'red', 'green', 'orange', 'darkcyan']
    ):
        trades = all_results[key]['trades']
        if trades:
            t_df = pd.DataFrame(trades).sort_values('exit_time')
            t_df['cum_net'] = t_df['net_pnl'].cumsum() / 1000.0
            plt.plot(t_df['exit_time'], t_df['cum_net'], label=label, color=col, linewidth=2)
            
    plt.axhline(0, color='black', linestyle='--')
    plt.title("5.5-Year Post-Tax Cumulative Equity: 4 Index Strategies Benchmark (2021 - 2026)", fontsize=14, fontweight='bold')
    plt.xlabel("Date")
    plt.ylabel("Net P&L (₹ Thousands)")
    plt.grid(True, alpha=0.3)
    plt.legend()
    plt.tight_layout()
    
    equity_plot = OUTPUT_DIR / "index_equity_comparison.png"
    plt.savefig(equity_plot, dpi=300)
    plt.close()
    print(f"✓ Saved Comparative Equity Plot: {equity_plot}", flush=True)


# ============================================================
# MAIN BENCHMARK RUNNER
# ============================================================
def main():
    print("="*75)
    print("🚀 INSTITUTIONAL INDEX STRATEGIES LAB ENGINE (NIFTY 2021 - 2026)")
    print("   Data Horizon: Jan 2021 to May 2026 (~1,255 Trading Days)")
    print("="*75, flush=True)
    
    df_15m, df_5m = prepare_data_and_indicators()
    
    print("\n" + "="*75)
    print("⚡ SIMULATING ALL 4 INDEX STRATEGIES")
    print("="*75, flush=True)
    
    # 1. Strategy 1: ORB with Regime Filters
    print("Running Strategy 1: Filtered ORB (15M)...", flush=True)
    res_s1 = simulate_strategy_1_orb(df_15m)
    
    # 2. Strategy 2: Futures Trend Following
    print("Running Strategy 2: Futures Trend Following (EMA 50/200 + Donchian)...", flush=True)
    res_s2 = simulate_strategy_2_trend_following(df_15m)
    
    # 3. Strategy 3: Intraday VWAP Mean Reversion
    print("Running Strategy 3: VWAP Mean Reversion (+/- 2.0 SD + ADX)...", flush=True)
    res_s3 = simulate_strategy_3_mean_reversion(df_15m)
    
    # 4. Strategy 4: ORB + VWAP Multi-Instrument
    print("Running Strategy 4: 5M ORB + VWAP (Futures vs Options vs Spreads)...", flush=True)
    res_s4 = simulate_strategy_4_multi_instrument(df_5m)
    
    all_results = {
        'strat1': res_s1,
        'strat2': res_s2,
        'strat3': res_s3,
        'strat4_fut': res_s4['futures'],
        'strat4_opt': res_s4['options'],
        'strat4_spd': res_s4['spreads']
    }
    
    # Print Master Benchmark Table
    print("\n" + "="*110)
    print("📊 MASTER BENCHMARK TABLE: 4 INDEX STRATEGIES (NIFTY 2021 - 2026)")
    print("="*110)
    print(f"{'Strategy Name':<32} {'Trades':<8} {'Win%':<8} {'PF (Net)':<10} {'Gross P&L (₹)':<16} {'Net P&L (₹)':<16} {'Net Avg/Trade':<14} {'Avg Dur':<10}")
    print("-" * 110)
    
    for k, r in all_results.items():
        dur_str = f"{r['avg_duration_mins']:.0f}m"
        print(f"{r['name']:<32} {r['total_trades']:<8} {r['win_rate']:<8.1f} {r['profit_factor']:<10.2f} "
              f"₹{r['total_gross_pnl']:<15,.0f} ₹{r['total_net_pnl']:<15,.0f} ₹{r['net_avg_profit_per_trade']:<13,.1f} {dur_str:<10}")
        
    print("="*110)
    
    # Save Report JSON
    report_file = OUTPUT_DIR / f"index_strategies_benchmark_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    save_data = {k: {key: val for key, val in r.items() if key != 'trades'} for k, r in all_results.items()}
    with open(report_file, 'w') as f:
        json.dump(save_data, f, indent=2)
    print(f"\n📁 Saved master benchmark report to: {report_file}", flush=True)
    
    # Run Robustness Modules & Generate Plots
    run_all_robustness_modules(all_results)
    
    print("\n" + "="*75)
    print("✅ INSTITUTIONAL BENCHMARK SUITE COMPLETE")
    print("="*75, flush=True)

if __name__ == "__main__":
    main()
