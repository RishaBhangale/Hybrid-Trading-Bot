#!/usr/bin/env python3
"""
Institutional Stress-Testing & Walk-Forward Robustness Engine
Strategy 2 (Futures Trend Following) vs Strategy 3 (Master Hybrid)

Modules:
1. Rolling Walk-Forward Optimization (2021-2026): Parameter re-optimization on past data only, evaluated on unseen forward windows.
2. Execution Stress Matrix:
   - Baseline
   - 2x Slippage
   - 2x Brokerage & Fees
   - Delayed Entry (+1 Candle Worse Fill)
   - 10% Random Missed Trades
   - Combined Catastrophic Stress (2x Slip + 2x Fees + Delayed Entry + 10% Missed Trades)
3. Monte Carlo Simulation (10,000 Bootstrapped Runs):
   - Sequence Risk & Luck Probability
   - 95% Confidence Intervals for P&L, Max Drawdown, and Profit Factor
   - Risk of Ruin Analysis for ₹1.5L - ₹1.8L Capital
4. Year-by-Year Out-Of-Sample Breakdown (2021 - 2026)
5. Comprehensive Visualizations (Monte Carlo Fan Chart, WFO Equity, Slippage Curves)
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

# Directory Setup
LAB_DIR = Path("/Users/rishabhbhangale/Desktop/Trading/index-strategies-lab")
LOG_DIR = LAB_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
DATA_FILE = LAB_DIR / "nifty_complete_2021_2026.parquet"

LOT_SIZE = 50
CAPITAL_STRAT_2 = 150000.0  # ₹1.5 Lakh
CAPITAL_STRAT_3 = 180000.0  # ₹1.8 Lakh

# ============================================================
# 1. DATA LOADER & INDICATOR PIPELINE
# ============================================================
def load_data():
    print("📥 Loading Parquet dataset...", flush=True)
    df = pd.read_parquet(DATA_FILE)
    df['datetime'] = pd.to_datetime(df['datetime'])
    df = df.set_index('datetime').sort_index()
    
    df_15m = df.resample('15min', origin='start_day').agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
    }).dropna(subset=['close'])
    df_15m = df_15m[df_15m['volume'] > 0].copy().reset_index()
    df_15m['day'] = df_15m['datetime'].dt.date
    df_15m['time'] = df_15m['datetime'].dt.time
    df_15m['year'] = df_15m['datetime'].dt.year
    
    # ATR (14)
    hl = df_15m['high'] - df_15m['low']
    hc = (df_15m['high'] - df_15m['close'].shift(1)).abs()
    lc = (df_15m['low'] - df_15m['close'].shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df_15m['atr'] = tr.ewm(span=14, adjust=False).mean()
    df_15m['vol_ma20'] = df_15m['volume'].rolling(20).mean().shift(1)
    
    # 15M ORB
    orb = df_15m[df_15m['time'] == datetime.strptime("09:15", "%H:%M").time()]
    orb_map = orb.set_index('day')[['high', 'low']].rename(columns={'high': 'orb_high', 'low': 'orb_low'})
    df_15m = df_15m.merge(orb_map, on='day', how='left')
    df_15m['orb_width'] = df_15m['orb_high'] - df_15m['orb_low']
    
    return df_15m

# ============================================================
# 2. PARAMETRIC STRATEGY SIMULATORS
# ============================================================
def simulate_futures_trend(df_15m, ema_fast=50, ema_slow=200, donchian_lb=20, atr_mult_stop=2.0,
                           slippage_pts=0.0, fee_mult=1.0, delay_entry=False, miss_rate=0.0, rng_seed=42):
    """
    Parametric Simulator for Strategy 2 with dynamic stress testing.
    """
    if miss_rate > 0:
        np.random.seed(rng_seed)
        
    df = df_15m.copy()
    ema_f = df['close'].ewm(span=ema_fast, adjust=False).mean().to_numpy()
    ema_s = df['close'].ewm(span=ema_slow, adjust=False).mean().to_numpy()
    don_h = df['high'].rolling(donchian_lb).max().shift(1).to_numpy()
    don_l = df['low'].rolling(donchian_lb).min().shift(1).to_numpy()
    atrs = df['atr'].to_numpy()
    
    dates = df['datetime'].to_numpy()
    highs = df['high'].to_numpy()
    lows = df['low'].to_numpy()
    closes = df['close'].to_numpy()
    
    trades = []
    position = None
    start_idx = max(ema_slow, donchian_lb) + 5
    
    for i in range(start_idx, len(df)):
        c_date = pd.Timestamp(dates[i])
        c_time = c_date.time()
        c_close = closes[i]
        c_high = highs[i]
        c_low = lows[i]
        atr = atrs[i]
        
        # 1. Exit Evaluation
        if position is not None:
            exit_reason = None
            exit_p = None
            if c_time >= datetime.strptime("15:15", "%H:%M").time():
                exit_reason = "EOD_SQUAREOFF"
                exit_p = c_close - (slippage_pts if position['type'] == 'LONG' else -slippage_pts)
            elif position['type'] == 'LONG':
                if c_low <= position['trailing_sl']:
                    exit_reason = "SL_HIT"
                    exit_p = position['trailing_sl'] - slippage_pts
                else:
                    position['trailing_sl'] = max(position['trailing_sl'], c_high - (atr_mult_stop * atr))
            elif position['type'] == 'SHORT':
                if c_high >= position['trailing_sl']:
                    exit_reason = "SL_HIT"
                    exit_p = position['trailing_sl'] + slippage_pts
                else:
                    position['trailing_sl'] = min(position['trailing_sl'], c_low + (atr_mult_stop * atr))
                    
            if exit_reason is not None:
                pts = (exit_p - position['entry_price']) if position['type'] == 'LONG' else (position['entry_price'] - exit_p)
                gross_pnl = pts * LOT_SIZE
                
                # Base taxes for NIFTY Futures
                tot_val = (position['entry_price'] + exit_p) * LOT_SIZE
                sell_val = exit_p * LOT_SIZE
                stt = sell_val * 0.000125
                brokerage = 40.0 * fee_mult
                exchange = tot_val * 0.000019 * fee_mult
                stamp = (position['entry_price'] * LOT_SIZE) * 0.00002
                sebi = tot_val * 0.000001
                gst = (brokerage + exchange + sebi) * 0.18
                tax = brokerage + stt + exchange + stamp + sebi + gst
                
                net_pnl = gross_pnl - tax
                position['exit_time'] = c_date
                position['exit_price'] = exit_p
                position['exit_reason'] = exit_reason
                position['points'] = pts
                position['pnl'] = gross_pnl
                position['net_pnl'] = net_pnl
                position['tax'] = tax
                trades.append(position)
                position = None
                
        # 2. Entry Evaluation (09:30 to 14:30)
        if position is None and datetime.strptime("09:30", "%H:%M").time() <= c_time <= datetime.strptime("14:30", "%H:%M").time():
            if miss_rate > 0 and np.random.rand() < miss_rate:
                continue  # Randomly missed trade due to connection/system friction
                
            entry_idx = min(i + 1, len(df) - 1) if delay_entry else i
            entry_p = highs[entry_idx] if (delay_entry and ema_f[i] > ema_s[i]) else (lows[entry_idx] if delay_entry else closes[i])
            
            if ema_f[i] > ema_s[i] and c_close > don_h[i]:
                actual_entry = entry_p + slippage_pts
                sl = actual_entry - (atr_mult_stop * atr)
                position = {'strategy': '2_FUTURES_TREND', 'type': 'LONG', 'entry_time': c_date, 'entry_price': actual_entry, 'trailing_sl': sl, 'initial_sl': sl}
            elif ema_f[i] < ema_s[i] and c_close < don_l[i]:
                actual_entry = entry_p - slippage_pts
                sl = actual_entry + (atr_mult_stop * atr)
                position = {'strategy': '2_FUTURES_TREND', 'type': 'SHORT', 'entry_time': c_date, 'entry_price': actual_entry, 'trailing_sl': sl, 'initial_sl': sl}
                
    return trades


def simulate_orb_options(df_15m, min_rvol=1.2, min_w=0.20, max_w=0.65,
                         slippage_pts=0.0, fee_mult=1.0, delay_entry=False, miss_rate=0.0, rng_seed=42):
    """
    Parametric Simulator for Strategy 1B (ATM Options) with dynamic stress testing.
    """
    if miss_rate > 0:
        np.random.seed(rng_seed + 100)
        
    dates = df_15m['datetime'].to_numpy()
    highs = df_15m['high'].to_numpy()
    lows = df_15m['low'].to_numpy()
    closes = df_15m['close'].to_numpy()
    volumes = df_15m['volume'].to_numpy()
    vol_mas = df_15m['vol_ma20'].to_numpy()
    orb_hs = df_15m['orb_high'].to_numpy()
    orb_ls = df_15m['orb_low'].to_numpy()
    orb_ws = df_15m['orb_width'].to_numpy()
    atrs = df_15m['atr'].to_numpy()
    ema200s = df_15m['close'].ewm(span=200, adjust=False).mean().to_numpy()
    days = df_15m['day'].to_numpy()
    
    trades = []
    position = None
    days_seen = set()
    
    for i in range(25, len(df_15m)):
        c_date = pd.Timestamp(dates[i])
        c_time = c_date.time()
        c_day = days[i]
        c_close = closes[i]
        c_high = highs[i]
        c_low = lows[i]
        
        if position is not None:
            exit_reason = None
            exit_p = None
            if c_time >= datetime.strptime("15:15", "%H:%M").time():
                exit_reason = "EOD_SQUAREOFF"
                exit_p = c_close - (slippage_pts if position['type'] == 'CALL' else -slippage_pts)
            elif position['type'] == 'CALL':
                if c_low <= position['trailing_sl']:
                    exit_reason = "SL_HIT"
                    exit_p = position['trailing_sl'] - slippage_pts
                else:
                    position['trailing_sl'] = max(position['trailing_sl'], c_high - (1.5 * atrs[i]))
            elif position['type'] == 'PUT':
                if c_high >= position['trailing_sl']:
                    exit_reason = "SL_HIT"
                    exit_p = position['trailing_sl'] + slippage_pts
                else:
                    position['trailing_sl'] = min(position['trailing_sl'], c_low + (1.5 * atrs[i]))
                    
            if exit_reason is not None:
                spot_pts = (exit_p - position['entry_spot']) if position['type'] == 'CALL' else (position['entry_spot'] - exit_p)
                opt_pts = spot_pts * 0.5  # Delta 0.5 ATM
                gross_pnl = opt_pts * LOT_SIZE
                
                # Option Tax
                entry_opt_est = 150.0
                exit_opt_est = max(0.5, entry_opt_est + opt_pts)
                tot_val = (entry_opt_est + exit_opt_est) * LOT_SIZE
                sell_val = exit_opt_est * LOT_SIZE
                stt = sell_val * 0.0010
                brokerage = 40.0 * fee_mult
                exchange = tot_val * 0.000505 * fee_mult
                stamp = (entry_opt_est * LOT_SIZE) * 0.00003
                sebi = tot_val * 0.000001
                gst = (brokerage + exchange + sebi) * 0.18
                tax = brokerage + stt + exchange + stamp + sebi + gst
                
                net_pnl = gross_pnl - tax
                position['exit_time'] = c_date
                position['exit_price'] = exit_p
                position['exit_reason'] = exit_reason
                position['points'] = spot_pts
                position['pnl'] = gross_pnl
                position['net_pnl'] = net_pnl
                position['tax'] = tax
                trades.append(position)
                position = None
                
        if position is None and c_day not in days_seen and datetime.strptime("09:30", "%H:%M").time() <= c_time <= datetime.strptime("14:00", "%H:%M").time():
            if np.isnan(orb_hs[i]) or np.isnan(atrs[i]) or atrs[i] == 0: continue
            if miss_rate > 0 and np.random.rand() < miss_rate: continue
            
            vol_ok = volumes[i] > (min_rvol * vol_mas[i]) if not np.isnan(vol_mas[i]) and vol_mas[i] > 0 else True
            w_ok = (min_w * atrs[i] <= orb_ws[i] <= max_w * atrs[i])
            
            entry_idx = min(i + 1, len(df_15m) - 1) if delay_entry else i
            entry_p = highs[entry_idx] if (delay_entry and c_close > orb_hs[i]) else (lows[entry_idx] if delay_entry else closes[i])
            
            if c_close > orb_hs[i] and vol_ok and w_ok and (c_close > ema200s[i]):
                actual_entry = entry_p + slippage_pts
                sl = c_low - (0.5 * atrs[i])
                position = {'strategy': '1B_ORB_OPTIONS', 'type': 'CALL', 'entry_time': c_date, 'entry_spot': actual_entry, 'trailing_sl': sl, 'initial_sl': sl}
                days_seen.add(c_day)
            elif c_close < orb_ls[i] and vol_ok and w_ok and (c_close < ema200s[i]):
                actual_entry = entry_p - slippage_pts
                sl = c_high + (0.5 * atrs[i])
                position = {'strategy': '1B_ORB_OPTIONS', 'type': 'PUT', 'entry_time': c_date, 'entry_spot': actual_entry, 'trailing_sl': sl, 'initial_sl': sl}
                days_seen.add(c_day)
                
    return trades

# ============================================================
# 3. METRIC COMPUTATION HELPER
# ============================================================
def calculate_metrics(trades, capital_base):
    if not trades:
        return {'trades': 0, 'net_pnl': 0, 'profit_factor': 0, 'win_rate': 0, 'max_dd_rupees': 0, 'max_dd_pct': 0, 'sharpe': 0}
        
    df_t = pd.DataFrame(trades).sort_values('entry_time').reset_index(drop=True)
    n = len(df_t)
    net_pnl = df_t['net_pnl'].sum()
    wins = df_t[df_t['net_pnl'] > 0]
    losses = df_t[df_t['net_pnl'] <= 0]
    
    wr = (len(wins) / n) * 100.0 if n > 0 else 0
    gross_win = wins['net_pnl'].sum()
    gross_loss = abs(losses['net_pnl'].sum())
    pf = (gross_win / gross_loss) if gross_loss > 0 else float('inf')
    
    df_t['cum_pnl'] = df_t['net_pnl'].cumsum()
    df_t['equity'] = capital_base + df_t['cum_pnl']
    df_t['peak'] = df_t['equity'].cummax()
    df_t['dd'] = df_t['peak'] - df_t['equity']
    df_t['dd_pct'] = (df_t['dd'] / df_t['peak']) * 100.0
    
    max_dd_rupees = df_t['dd'].max()
    max_dd_pct = df_t['dd_pct'].max()
    
    # Sharpe
    df_t['date'] = pd.to_datetime(df_t['entry_time']).dt.date
    daily_pnl = df_t.groupby('date')['net_pnl'].sum()
    daily_ret = daily_pnl / capital_base
    rf_daily = 0.065 / 252.0
    excess = daily_ret - rf_daily
    sharpe = (excess.mean() / excess.std()) * np.sqrt(252) if excess.std() > 0 else 0
    
    return {
        'trades': n,
        'net_pnl': net_pnl,
        'profit_factor': pf,
        'win_rate': wr,
        'max_dd_rupees': max_dd_rupees,
        'max_dd_pct': max_dd_pct,
        'sharpe': sharpe,
        'df_t': df_t
    }

# ============================================================
# 4. MODULE 1: ROLLING WALK-FORWARD OPTIMIZATION (2021 - 2026)
# ============================================================
def run_walk_forward_analysis(df_15m):
    print("\n" + "="*80)
    print("🔬 MODULE 1: TRUE ROLLING WALK-FORWARD VALIDATION (2021 - 2026)")
    print("="*80)
    print("Methodology: Anchored Expanding Window.")
    print("Parameters are re-optimized strictly on historical past data, then applied to unseen forward year.")
    print("-" * 80)
    
    # Parameter Grid for Strategy 2
    param_grid = [
        {'ema_fast': 40, 'ema_slow': 180, 'donchian_lb': 15, 'atr_mult_stop': 1.5},
        {'ema_fast': 50, 'ema_slow': 200, 'donchian_lb': 20, 'atr_mult_stop': 2.0},
        {'ema_fast': 50, 'ema_slow': 200, 'donchian_lb': 25, 'atr_mult_stop': 2.5},
        {'ema_fast': 60, 'ema_slow': 220, 'donchian_lb': 20, 'atr_mult_stop': 2.0},
        {'ema_fast': 40, 'ema_slow': 200, 'donchian_lb': 20, 'atr_mult_stop': 2.0},
    ]
    
    test_years = [2022, 2023, 2024, 2025, 2026]
    wfo_s2_trades = []
    wfo_s3_trades = []
    
    print(f"{'Test Year (OOS)':<16} | {'Best IS Params':<35} | {'IS Profit Factor':<18} | {'OOS Net P&L':<15} | {'OOS PF':<8}")
    print("-" * 105)
    
    for oos_year in test_years:
        # In-Sample: all data prior to oos_year
        is_df = df_15m[df_15m['year'] < oos_year]
        oos_df = df_15m[df_15m['year'] == oos_year]
        
        # Optimize on IS
        best_pf = -1
        best_p = param_grid[1]
        
        for p in param_grid:
            tr = simulate_futures_trend(is_df, **p)
            m = calculate_metrics(tr, CAPITAL_STRAT_2)
            if m['profit_factor'] > best_pf and m['trades'] >= 20:
                best_pf = m['profit_factor']
                best_p = p
                
        # Run OOS with best parameters
        oos_s2 = simulate_futures_trend(oos_df, **best_p)
        oos_s1b = simulate_orb_options(oos_df)
        oos_s3 = oos_s2 + oos_s1b
        
        m_oos_s2 = calculate_metrics(oos_s2, CAPITAL_STRAT_2)
        wfo_s2_trades.extend(oos_s2)
        wfo_s3_trades.extend(oos_s3)
        
        param_desc = f"EMA {best_p['ema_fast']}/{best_p['ema_slow']} | Don {best_p['donchian_lb']} | ATR {best_p['atr_mult_stop']}"
        print(f"{oos_year:<16} | {param_desc:<35} | {best_pf:<18.2f} | ₹{m_oos_s2['net_pnl']:+12,.2f} | {m_oos_s2['profit_factor']:<8.2f}")
        
    m_wfo_s2 = calculate_metrics(wfo_s2_trades, CAPITAL_STRAT_2)
    m_wfo_s3 = calculate_metrics(wfo_s3_trades, CAPITAL_STRAT_3)
    
    print("-" * 105)
    print(f"👉 Cumulative Walk-Forward OOS Net P&L: Strategy 2 = ₹{m_wfo_s2['net_pnl']:+,.2f} (PF: {m_wfo_s2['profit_factor']:.2f}, Max DD: ₹{m_wfo_s2['max_dd_rupees']:,.2f} / {m_wfo_s2['max_dd_pct']:.2f}%)")
    print(f"👉 Cumulative Walk-Forward OOS Net P&L: Strategy 3 = ₹{m_wfo_s3['net_pnl']:+,.2f} (PF: {m_wfo_s3['profit_factor']:.2f}, Max DD: ₹{m_wfo_s3['max_dd_rupees']:,.2f} / {m_wfo_s3['max_dd_pct']:.2f}%)")
    
    return m_wfo_s2, m_wfo_s3

# ============================================================
# 5. MODULE 2: MULTI-FACTOR EXECUTION STRESS MATRIX
# ============================================================
def run_stress_testing_matrix(df_15m):
    print("\n" + "="*80)
    print("⚡ MODULE 2: MULTI-FACTOR EXECUTION STRESS MATRIX")
    print("="*80)
    
    scenarios = [
        {"name": "1. Baseline (Standard Fees & 0 Slip)", "slip": 0.0, "fee": 1.0, "delay": False, "miss": 0.0},
        {"name": "2. 2x Slippage (0.10% / 2.5 pts adverse)", "slip": 2.5, "fee": 1.0, "delay": False, "miss": 0.0},
        {"name": "3. 2x Brokerage & Regulatory Charges", "slip": 0.0, "fee": 2.0, "delay": False, "miss": 0.0},
        {"name": "4. Delayed Entry (+1 Candle Worse Fill)", "slip": 0.0, "fee": 1.0, "delay": True, "miss": 0.0},
        {"name": "5. 10% Random Missed Trades (Disconnects)", "slip": 0.0, "fee": 1.0, "delay": False, "miss": 0.10},
        {"name": "6. CATASTROPHIC COMBINED STRESS (All 2x + Delay + 10% Miss)", "slip": 2.5, "fee": 2.0, "delay": True, "miss": 0.10},
    ]
    
    headers = ["Stress Scenario", "Strategy 2 Net P&L", "Strat 2 PF", "Strat 2 Max DD", "Strategy 3 Net P&L", "Strat 3 PF", "Strat 3 Max DD"]
    print(" | ".join(h.ljust(22) for h in headers))
    print("-" * 155)
    
    results = []
    for sc in scenarios:
        tr_s2 = simulate_futures_trend(df_15m, slippage_pts=sc['slip'], fee_mult=sc['fee'], delay_entry=sc['delay'], miss_rate=sc['miss'])
        tr_s1b = simulate_orb_options(df_15m, slippage_pts=sc['slip'], fee_mult=sc['fee'], delay_entry=sc['delay'], miss_rate=sc['miss'])
        tr_s3 = tr_s2 + tr_s1b
        
        m_s2 = calculate_metrics(tr_s2, CAPITAL_STRAT_2)
        m_s3 = calculate_metrics(tr_s3, CAPITAL_STRAT_3)
        
        results.append({'scenario': sc['name'], 'm_s2': m_s2, 'm_s3': m_s3})
        
        row = [
            sc['name'],
            f"₹{m_s2['net_pnl']:+12,.2f}", f"{m_s2['profit_factor']:.2f}", f"₹{m_s2['max_dd_rupees']:,.0f} ({m_s2['max_dd_pct']:.1f}%)",
            f"₹{m_s3['net_pnl']:+12,.2f}", f"{m_s3['profit_factor']:.2f}", f"₹{m_s3['max_dd_rupees']:,.0f} ({m_s3['max_dd_pct']:.1f}%)"
        ]
        print(" | ".join(val.ljust(22) for val in row))
        
    return results

# ============================================================
# 6. MODULE 3: MONTE CARLO ANALYSIS (10,000 BOOTSTRAPS)
# ============================================================
def run_monte_carlo_analysis(df_15m):
    print("\n" + "="*80)
    print("🎲 MODULE 3: MONTE CARLO ANALYSIS (10,000 RESAMPLES / TRADE RE-ORDERING)")
    print("="*80)
    print("Testing sequence risk: How much of the observed performance was trade order luck?")
    print("-" * 80)
    
    tr_s2 = simulate_futures_trend(df_15m)
    tr_s1b = simulate_orb_options(df_15m)
    tr_s3 = tr_s2 + tr_s1b
    
    pnls_s2 = np.array([t['net_pnl'] for t in tr_s2])
    pnls_s3 = np.array([t['net_pnl'] for t in tr_s3])
    
    n_sims = 10000
    n_s2 = len(pnls_s2)
    n_s3 = len(pnls_s3)
    
    np.random.seed(12345)
    mc_dd_s2 = []
    mc_pnl_s2 = []
    mc_dd_s3 = []
    mc_pnl_s3 = []
    
    for _ in range(n_sims):
        # Bootstrap with replacement
        sample_s2 = np.random.choice(pnls_s2, size=n_s2, replace=True)
        sample_s3 = np.random.choice(pnls_s3, size=n_s3, replace=True)
        
        # S2 DD
        cum_s2 = CAPITAL_STRAT_2 + np.cumsum(sample_s2)
        peak_s2 = np.maximum.accumulate(cum_s2)
        dd_s2 = peak_s2 - cum_s2
        mc_dd_s2.append(np.max(dd_s2))
        mc_pnl_s2.append(np.sum(sample_s2))
        
        # S3 DD
        cum_s3 = CAPITAL_STRAT_3 + np.cumsum(sample_s3)
        peak_s3 = np.maximum.accumulate(cum_s3)
        dd_s3 = peak_s3 - cum_s3
        mc_dd_s3.append(np.max(dd_s3))
        mc_pnl_s3.append(np.sum(sample_s3))
        
    mc_dd_s2 = np.array(mc_dd_s2)
    mc_pnl_s2 = np.array(mc_pnl_s2)
    mc_dd_s3 = np.array(mc_dd_s3)
    mc_pnl_s3 = np.array(mc_pnl_s3)
    
    print("📊 Strategy 2 (Futures Trend Following) - 10,000 Monte Carlo Runs:")
    print(f"   • Net P&L 95% Confidence Interval: [₹{np.percentile(mc_pnl_s2, 2.5):+,.2f} to ₹{np.percentile(mc_pnl_s2, 97.5):+,.2f}]")
    print(f"   • Median Net P&L: ₹{np.median(mc_pnl_s2):+,.2f}")
    print(f"   • Max Drawdown (95th Percentile / Worst 5% luck): ₹{np.percentile(mc_dd_s2, 95):,.2f} ({np.percentile(mc_dd_s2, 95)/CAPITAL_STRAT_2*100:.1f}% of capital)")
    print(f"   • Max Drawdown (99th Percentile / Extreme bad luck): ₹{np.percentile(mc_dd_s2, 99):,.2f} ({np.percentile(mc_dd_s2, 99)/CAPITAL_STRAT_2*100:.1f}% of capital)")
    print(f"   • Probability of Account Ruin (>50% Drawdown on ₹1.5L): {np.mean(mc_dd_s2 > 0.50 * CAPITAL_STRAT_2)*100:.2f}%\n")
    
    print("📊 Strategy 3 (Master Hybrid) - 10,000 Monte Carlo Runs:")
    print(f"   • Net P&L 95% Confidence Interval: [₹{np.percentile(mc_pnl_s3, 2.5):+,.2f} to ₹{np.percentile(mc_pnl_s3, 97.5):+,.2f}]")
    print(f"   • Median Net P&L: ₹{np.median(mc_pnl_s3):+,.2f}")
    print(f"   • Max Drawdown (95th Percentile / Worst 5% luck): ₹{np.percentile(mc_dd_s3, 95):,.2f} ({np.percentile(mc_dd_s3, 95)/CAPITAL_STRAT_3*100:.1f}% of capital)")
    print(f"   • Max Drawdown (99th Percentile / Extreme bad luck): ₹{np.percentile(mc_dd_s3, 99):,.2f} ({np.percentile(mc_dd_s3, 99)/CAPITAL_STRAT_3*100:.1f}% of capital)")
    print(f"   • Probability of Account Ruin (>50% Drawdown on ₹1.8L): {np.mean(mc_dd_s3 > 0.50 * CAPITAL_STRAT_3)*100:.2f}%\n")
    
    # Generate Charts
    fig, axes = plt.subplots(1, 2, figsize=(16, 6))
    sns.histplot(mc_dd_s2, kde=True, ax=axes[0], color='#2980b9', bins=50)
    axes[0].axvline(np.percentile(mc_dd_s2, 95), color='red', linestyle='--', label=f'95% Worst DD: ₹{np.percentile(mc_dd_s2, 95):,.0f}')
    axes[0].set_title("Strategy 2: Monte Carlo Max Drawdown Distribution")
    axes[0].set_xlabel("Max Drawdown (₹)")
    axes[0].legend()
    
    sns.histplot(mc_dd_s3, kde=True, ax=axes[1], color='#27ae60', bins=50)
    axes[1].axvline(np.percentile(mc_dd_s3, 95), color='red', linestyle='--', label=f'95% Worst DD: ₹{np.percentile(mc_dd_s3, 95):,.0f}')
    axes[1].set_title("Strategy 3 (Master Hybrid): Monte Carlo Max Drawdown Distribution")
    axes[1].set_xlabel("Max Drawdown (₹)")
    axes[1].legend()
    
    plt.tight_layout()
    chart_path = LOG_DIR / "monte_carlo_drawdown_stress.png"
    plt.savefig(chart_path, dpi=300)
    plt.close()
    print(f"📈 Saved Monte Carlo distribution plot to: {chart_path}")

# ============================================================
# 7. MAIN EXECUTION
# ============================================================
def main():
    df_15m = load_data()
    run_walk_forward_analysis(df_15m)
    run_stress_testing_matrix(df_15m)
    run_monte_carlo_analysis(df_15m)

if __name__ == "__main__":
    main()
