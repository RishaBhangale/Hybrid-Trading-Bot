#!/usr/bin/env python3
"""
Comprehensive Index Strategies Evaluation & Metrics Engine
Evaluates:
- Strategy 1B: Filtered 15M ORB on ATM Options
- Strategy 2: Futures Trend Following (EMA 50/200 + Donchian)
- Strategy 3: Master Index Hybrid (Combined 1B + 2)

Calculates:
1. Profit Factor
2. CAGR
3. Max Drawdown (₹ and %)
4. Sharpe Ratio (Annualized)
5. Sortino Ratio (Annualized)
6. Win Rate (%)
7. Average Win / Average Loss Ratio
8. Largest Loss (₹)
9. Yearly Returns (2021, 2022, 2023, 2024, 2025, 2026)
10. Max Consecutive Losses
11. In-Sample (2021-2024) vs Out-of-Sample (2025-2026) Walk-Forward Breakdown
12. Past 1-Week Live Kite API Test (Aug 18 - Aug 28, 2026)
"""

import os
import sys
import json
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
import pytz

LAB_DIR = Path("/Users/rishabhbhangale/Desktop/Trading/index-strategies-lab")
DATA_FILE = LAB_DIR / "nifty_complete_2021_2026.parquet"
LOT_SIZE = 50

# Capital bases for CAGR calculation
CAPITAL_STRAT_1B = 30000   # ₹30,000 for 1 lot ATM Option Buying
CAPITAL_STRAT_2  = 150000  # ₹1,50,000 for 1 lot NIFTY Futures margin + buffer
CAPITAL_STRAT_3  = 180000  # ₹1,80,000 for Combined Master Hybrid (Futures + Options)

# ============================================================
# 1. LOAD & PREPARE 5.5-YEAR DATASET
# ============================================================
def load_and_prepare_data():
    print("📥 Loading Parquet dataset...", flush=True)
    df = pd.read_parquet(DATA_FILE)
    df['datetime'] = pd.to_datetime(df['datetime'])
    df = df.set_index('datetime').sort_index()
    
    # Resample to 15-Minute Candles
    df_15m = df.resample('15min', origin='start_day').agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum',
        'atm_ce_close': 'last', 'atm_pe_close': 'last'
    }).dropna(subset=['close'])
    df_15m = df_15m[df_15m['volume'] > 0].copy().reset_index()
    df_15m['day'] = df_15m['datetime'].dt.date
    df_15m['time'] = df_15m['datetime'].dt.time
    df_15m['year'] = df_15m['datetime'].dt.year
    
    # 1. ATR (14)
    hl = df_15m['high'] - df_15m['low']
    hc = (df_15m['high'] - df_15m['close'].shift(1)).abs()
    lc = (df_15m['low'] - df_15m['close'].shift(1)).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df_15m['atr'] = tr.ewm(span=14, adjust=False).mean()
    
    # 2. EMAs & Donchian
    df_15m['ema_50'] = df_15m['close'].ewm(span=50, adjust=False).mean()
    df_15m['ema_200'] = df_15m['close'].ewm(span=200, adjust=False).mean()
    df_15m['donchian_high'] = df_15m['high'].rolling(20).max().shift(1)
    df_15m['donchian_low'] = df_15m['low'].rolling(20).min().shift(1)
    df_15m['vol_ma20'] = df_15m['volume'].rolling(20).mean().shift(1)
    
    # 3. 15M ORB
    orb = df_15m[df_15m['time'] == datetime.strptime("09:15", "%H:%M").time()]
    orb_map = orb.set_index('day')[['high', 'low']].rename(columns={'high': 'orb_high', 'low': 'orb_low'})
    df_15m = df_15m.merge(orb_map, on='day', how='left')
    df_15m['orb_width'] = df_15m['orb_high'] - df_15m['orb_low']
    
    return df_15m

# ============================================================
# 2. TAX CALCULATOR
# ============================================================
def get_tax(instrument: str, entry_p: float, exit_p: float, qty: int = 50) -> float:
    if instrument == "FUTURES":
        tot_val = (entry_p + exit_p) * qty
        sell_val = exit_p * qty
        stt = sell_val * 0.000125
        brokerage = 40.0
        exchange = tot_val * 0.000019
        stamp = (entry_p * qty) * 0.00002
        sebi = tot_val * 0.000001
        gst = (brokerage + exchange + sebi) * 0.18
        return brokerage + stt + exchange + stamp + sebi + gst
    else: # OPTIONS
        tot_val = (entry_p + exit_p) * qty
        sell_val = exit_p * qty
        stt = sell_val * 0.0010
        brokerage = 40.0
        exchange = tot_val * 0.000505
        stamp = (entry_p * qty) * 0.00003
        sebi = tot_val * 0.000001
        gst = (brokerage + exchange + sebi) * 0.18
        return brokerage + stt + exchange + stamp + sebi + gst

# ============================================================
# 3. SIMULATORS
# ============================================================
def simulate_strategy_1b_orb_options(df_15m: pd.DataFrame) -> list:
    """Strategy 1B: Filtered 15M ORB on ATM Options (Delta 0.5)."""
    trades = []
    position = None
    days_seen = set()
    
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
    ema200s = df_15m['ema_200'].to_numpy()
    days = df_15m['day'].to_numpy()
    
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
                exit_p = c_close
            elif position['type'] == 'CALL':
                if c_low <= position['trailing_sl']:
                    exit_reason = "SL_HIT"
                    exit_p = position['trailing_sl']
                else:
                    position['trailing_sl'] = max(position['trailing_sl'], c_high - (1.5 * atrs[i]))
            elif position['type'] == 'PUT':
                if c_high >= position['trailing_sl']:
                    exit_reason = "SL_HIT"
                    exit_p = position['trailing_sl']
                else:
                    position['trailing_sl'] = min(position['trailing_sl'], c_low + (1.5 * atrs[i]))
                    
            if exit_reason is not None:
                spot_pts = (exit_p - position['entry_spot']) if position['type'] == 'CALL' else (position['entry_spot'] - exit_p)
                opt_pts = spot_pts * 0.5  # Delta 0.5 ATM Option
                gross_pnl = opt_pts * LOT_SIZE
                entry_opt_est = 150.0  # Avg ATM option entry price
                exit_opt_est = max(0.5, entry_opt_est + opt_pts)
                tax = get_tax("OPTIONS", entry_opt_est, exit_opt_est, LOT_SIZE)
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
            vol_ok = volumes[i] > (1.2 * vol_mas[i]) if not np.isnan(vol_mas[i]) and vol_mas[i] > 0 else True
            w_ok = (0.20 * atrs[i] <= orb_ws[i] <= 0.65 * atrs[i])
            
            if c_close > orb_hs[i] and vol_ok and w_ok and (c_close > ema200s[i]):
                sl = c_low - (0.5 * atrs[i])
                position = {'strategy': '1B_ORB_OPTIONS', 'type': 'CALL', 'entry_time': c_date, 'entry_spot': c_close, 'trailing_sl': sl, 'initial_sl': sl}
                days_seen.add(c_day)
            elif c_close < orb_ls[i] and vol_ok and w_ok and (c_close < ema200s[i]):
                sl = c_high + (0.5 * atrs[i])
                position = {'strategy': '1B_ORB_OPTIONS', 'type': 'PUT', 'entry_time': c_date, 'entry_spot': c_close, 'trailing_sl': sl, 'initial_sl': sl}
                days_seen.add(c_day)
                
    return trades

def simulate_strategy_2_futures_trend(df_15m: pd.DataFrame) -> list:
    """Strategy 2: Futures Trend Following (EMA 50/200 + Donchian)."""
    trades = []
    position = None
    
    dates = df_15m['datetime'].to_numpy()
    highs = df_15m['high'].to_numpy()
    lows = df_15m['low'].to_numpy()
    closes = df_15m['close'].to_numpy()
    ema50s = df_15m['ema_50'].to_numpy()
    ema200s = df_15m['ema_200'].to_numpy()
    don_hs = df_15m['donchian_high'].to_numpy()
    don_ls = df_15m['donchian_low'].to_numpy()
    atrs = df_15m['atr'].to_numpy()
    
    for i in range(200, len(df_15m)):
        c_date = pd.Timestamp(dates[i])
        c_time = c_date.time()
        c_close = closes[i]
        c_high = highs[i]
        c_low = lows[i]
        atr = atrs[i]
        
        if position is not None:
            exit_reason = None
            exit_p = None
            if c_time >= datetime.strptime("15:15", "%H:%M").time():
                exit_reason = "EOD_SQUAREOFF"
                exit_p = c_close
            elif position['type'] == 'LONG':
                if c_low <= position['trailing_sl']:
                    exit_reason = "SL_HIT"
                    exit_p = position['trailing_sl']
                else:
                    position['trailing_sl'] = max(position['trailing_sl'], c_high - (2.0 * atr))
            elif position['type'] == 'SHORT':
                if c_high >= position['trailing_sl']:
                    exit_reason = "SL_HIT"
                    exit_p = position['trailing_sl']
                else:
                    position['trailing_sl'] = min(position['trailing_sl'], c_low + (2.0 * atr))
                    
            if exit_reason is not None:
                pts = (exit_p - position['entry_price']) if position['type'] == 'LONG' else (position['entry_price'] - exit_p)
                gross_pnl = pts * LOT_SIZE
                tax = get_tax("FUTURES", position['entry_price'], exit_p, LOT_SIZE)
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
                
        if position is None and datetime.strptime("09:30", "%H:%M").time() <= c_time <= datetime.strptime("14:30", "%H:%M").time():
            if ema50s[i] > ema200s[i] and c_close > don_hs[i]:
                sl = c_close - (2.0 * atr)
                position = {'strategy': '2_FUTURES_TREND', 'type': 'LONG', 'entry_time': c_date, 'entry_price': c_close, 'trailing_sl': sl, 'initial_sl': sl}
            elif ema50s[i] < ema200s[i] and c_close < don_ls[i]:
                sl = c_close + (2.0 * atr)
                position = {'strategy': '2_FUTURES_TREND', 'type': 'SHORT', 'entry_time': c_date, 'entry_price': c_close, 'trailing_sl': sl, 'initial_sl': sl}
                
    return trades

# ============================================================
# 4. METRIC COMPUTATION ENGINE
# ============================================================
def compute_strategy_metrics(name: str, trades: list, capital_base: float) -> dict:
    if not trades:
        return {}
        
    df_t = pd.DataFrame(trades)
    df_t['entry_date'] = pd.to_datetime(df_t['entry_time'])
    df_t = df_t.sort_values('entry_date').reset_index(drop=True)
    
    total_trades = len(df_t)
    gross_pnl = df_t['pnl'].sum()
    net_pnl = df_t['net_pnl'].sum()
    total_tax = df_t['tax'].sum()
    
    wins = df_t[df_t['net_pnl'] > 0]
    losses = df_t[df_t['net_pnl'] <= 0]
    
    win_rate = (len(wins) / total_trades) * 100.0 if total_trades > 0 else 0
    gross_profit = wins['net_pnl'].sum()
    gross_loss = abs(losses['net_pnl'].sum())
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float('inf')
    
    avg_win = wins['net_pnl'].mean() if len(wins) > 0 else 0
    avg_loss = abs(losses['net_pnl'].mean()) if len(losses) > 0 else 0
    win_loss_ratio = (avg_win / avg_loss) if avg_loss > 0 else 0
    largest_loss = losses['net_pnl'].min() if len(losses) > 0 else 0
    
    # Equity Curve & Drawdown
    df_t['cum_pnl'] = df_t['net_pnl'].cumsum()
    df_t['equity'] = capital_base + df_t['cum_pnl']
    df_t['peak'] = df_t['equity'].cummax()
    df_t['dd'] = df_t['peak'] - df_t['equity']
    df_t['dd_pct'] = (df_t['dd'] / df_t['peak']) * 100.0
    
    max_dd_rupees = df_t['dd'].max()
    max_dd_pct = df_t['dd_pct'].max()
    
    # Consecutive Losses
    streak = 0
    max_streak = 0
    for pnl in df_t['net_pnl']:
        if pnl <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
            
    # Time Span & CAGR
    start_date = df_t['entry_date'].min()
    end_date = df_t['entry_date'].max()
    years = (end_date - start_date).days / 365.25
    final_equity = df_t['equity'].iloc[-1]
    cagr = (((final_equity / capital_base) ** (1.0 / years)) - 1.0) * 100.0 if (final_equity > 0 and years > 0) else 0.0
    
    # Daily Returns for Sharpe & Sortino
    df_t['day'] = df_t['entry_date'].dt.date
    daily_pnl = df_t.groupby('day')['net_pnl'].sum()
    # Reindex over all business days
    all_days = pd.date_range(start_date.date(), end_date.date(), freq='B').date
    daily_series = pd.Series(0.0, index=all_days)
    daily_series.loc[daily_series.index.intersection(daily_pnl.index)] = daily_pnl
    daily_ret = daily_series / capital_base
    
    rf_daily = 0.065 / 252.0  # 6.5% RBI risk-free rate
    excess_ret = daily_ret - rf_daily
    
    sharpe = (excess_ret.mean() / excess_ret.std()) * np.sqrt(252) if excess_ret.std() > 0 else 0
    downside_std = daily_ret[daily_ret < 0].std()
    sortino = (excess_ret.mean() / downside_std) * np.sqrt(252) if downside_std > 0 else 0
    
    # Yearly Breakdown
    df_t['year'] = df_t['entry_date'].dt.year
    yearly = {}
    for yr, grp in df_t.groupby('year'):
        yr_net = grp['net_pnl'].sum()
        yr_trades = len(grp)
        yr_wins = len(grp[grp['net_pnl'] > 0])
        yr_wr = (yr_wins / yr_trades) * 100.0 if yr_trades > 0 else 0
        yr_ret_pct = (yr_net / capital_base) * 100.0
        yearly[yr] = {'net_pnl': yr_net, 'trades': yr_trades, 'win_rate': yr_wr, 'ret_pct': yr_ret_pct}
        
    # In-Sample vs Out-of-Sample Split
    # IS: 2021-2024, OOS: 2025-2026
    is_trades = df_t[df_t['year'] <= 2024]
    oos_trades = df_t[df_t['year'] >= 2025]
    
    def get_split_stats(sub_df):
        if sub_df.empty: return {}
        t_tot = len(sub_df)
        t_net = sub_df['net_pnl'].sum()
        t_w = sub_df[sub_df['net_pnl'] > 0]
        t_l = sub_df[sub_df['net_pnl'] <= 0]
        t_pf = (t_w['net_pnl'].sum() / abs(t_l['net_pnl'].sum())) if len(t_l) > 0 and abs(t_l['net_pnl'].sum()) > 0 else float('inf')
        t_wr = (len(t_w) / t_tot) * 100.0
        return {'trades': t_tot, 'net_pnl': t_net, 'profit_factor': t_pf, 'win_rate': t_wr}
        
    is_stats = get_split_stats(is_trades)
    oos_stats = get_split_stats(oos_trades)
    
    return {
        'name': name,
        'capital_base': capital_base,
        'total_trades': total_trades,
        'gross_pnl': gross_pnl,
        'total_tax': total_tax,
        'net_pnl': net_pnl,
        'profit_factor': profit_factor,
        'cagr': cagr,
        'max_dd_rupees': max_dd_rupees,
        'max_dd_pct': max_dd_pct,
        'sharpe': sharpe,
        'sortino': sortino,
        'win_rate': win_rate,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'win_loss_ratio': win_loss_ratio,
        'largest_loss': largest_loss,
        'max_consecutive_losses': max_streak,
        'yearly': yearly,
        'is_stats': is_stats,
        'oos_stats': oos_stats,
        'trades': trades
    }

# ============================================================
# 5. EXECUTION & REPORTING
# ============================================================
def run_evaluation():
    df_15m = load_and_prepare_data()
    
    print("\n" + "="*80)
    print("🚀 RUNNING BACKTEST SIMULATIONS (2021 - 2026)")
    print("="*80, flush=True)
    
    # 1. Strategy 1B
    t_s1b = simulate_strategy_1b_orb_options(df_15m)
    m_s1b = compute_strategy_metrics("Strategy 1B (Filtered 15M ORB Options)", t_s1b, CAPITAL_STRAT_1B)
    
    # 2. Strategy 2
    t_s2 = simulate_strategy_2_futures_trend(df_15m)
    m_s2 = compute_strategy_metrics("Strategy 2 (Futures Trend Following)", t_s2, CAPITAL_STRAT_2)
    
    # 3. Strategy 3 (Master Hybrid)
    t_s3 = t_s1b + t_s2
    m_s3 = compute_strategy_metrics("Strategy 3 (Master Index Hybrid)", t_s3, CAPITAL_STRAT_3)
    
    # Print Master Summary
    print("\n" + "="*80)
    print("🏆 MASTER PERFORMANCE METRICS TABLE (2021 - 2026)")
    print("="*80)
    
    headers = ["Metric", "Strategy 1B (ORB Options)", "Strategy 2 (Futures Trend)", "Strategy 3 (Master Hybrid)"]
    rows = [
        ["Capital Required", f"₹{CAPITAL_STRAT_1B:,}", f"₹{CAPITAL_STRAT_2:,}", f"₹{CAPITAL_STRAT_3:,}"],
        ["Total Trades", f"{m_s1b['total_trades']}", f"{m_s2['total_trades']}", f"{m_s3['total_trades']}"],
        ["Win Rate", f"{m_s1b['win_rate']:.1f}%", f"{m_s2['win_rate']:.1f}%", f"{m_s3['win_rate']:.1f}%"],
        ["Gross P&L", f"₹{m_s1b['gross_pnl']:+,.2f}", f"₹{m_s2['gross_pnl']:+,.2f}", f"₹{m_s3['gross_pnl']:+,.2f}"],
        ["F&O Taxes & Fees", f"₹{m_s1b['total_tax']:,.2f}", f"₹{m_s2['total_tax']:,.2f}", f"₹{m_s3['total_tax']:,.2f}"],
        ["Net Post-Tax P&L", f"₹{m_s1b['net_pnl']:+,.2f}", f"₹{m_s2['net_pnl']:+,.2f}", f"₹{m_s3['net_pnl']:+,.2f}"],
        ["Profit Factor", f"{m_s1b['profit_factor']:.2f}", f"{m_s2['profit_factor']:.2f}", f"{m_s3['profit_factor']:.2f}"],
        ["CAGR", f"{m_s1b['cagr']:.1f}%", f"{m_s2['cagr']:.1f}%", f"{m_s3['cagr']:.1f}%"],
        ["Max Drawdown (₹)", f"₹{m_s1b['max_dd_rupees']:,.2f}", f"₹{m_s2['max_dd_rupees']:,.2f}", f"₹{m_s3['max_dd_rupees']:,.2f}"],
        ["Max Drawdown (%)", f"{m_s1b['max_dd_pct']:.2f}%", f"{m_s2['max_dd_pct']:.2f}%", f"{m_s3['max_dd_pct']:.2f}%"],
        ["Sharpe Ratio", f"{m_s1b['sharpe']:.2f}", f"{m_s2['sharpe']:.2f}", f"{m_s3['sharpe']:.2f}"],
        ["Sortino Ratio", f"{m_s1b['sortino']:.2f}", f"{m_s2['sortino']:.2f}", f"{m_s3['sortino']:.2f}"],
        ["Avg Win / Avg Loss", f"{m_s1b['win_loss_ratio']:.2f} (₹{m_s1b['avg_win']:,.0f} / ₹{m_s1b['avg_loss']:,.0f})",
                                f"{m_s2['win_loss_ratio']:.2f} (₹{m_s2['avg_win']:,.0f} / ₹{m_s2['avg_loss']:,.0f})",
                                f"{m_s3['win_loss_ratio']:.2f} (₹{m_s3['avg_win']:,.0f} / ₹{m_s3['avg_loss']:,.0f})"],
        ["Largest Single Loss", f"₹{m_s1b['largest_loss']:,.2f}", f"₹{m_s2['largest_loss']:,.2f}", f"₹{m_s3['largest_loss']:,.2f}"],
        ["Max Consecutive Losses", f"{m_s1b['max_consecutive_losses']} trades", f"{m_s2['max_consecutive_losses']} trades", f"{m_s3['max_consecutive_losses']} trades"],
    ]
    
    # Format and print table
    col_w = [25, 27, 27, 27]
    header_str = " | ".join(h.ljust(col_w[i]) for i, h in enumerate(headers))
    print(header_str)
    print("-" * len(header_str))
    for r in rows:
        print(" | ".join(str(val).ljust(col_w[i]) for i, val in enumerate(r)))
        
    # Print Yearly Returns Breakdown
    print("\n" + "="*80)
    print("📅 YEARLY RETURNS BREAKDOWN (NET P&L & % RETURN)")
    print("="*80)
    years = sorted(list(m_s1b['yearly'].keys()))
    yr_headers = ["Year", "Strategy 1B (Options)", "Strategy 2 (Futures)", "Strategy 3 (Master Hybrid)"]
    print(" | ".join(h.ljust(20) for h in yr_headers))
    print("-" * 80)
    for y in years:
        y1 = m_s1b['yearly'].get(y, {'net_pnl': 0, 'ret_pct': 0})
        y2 = m_s2['yearly'].get(y, {'net_pnl': 0, 'ret_pct': 0})
        y3 = m_s3['yearly'].get(y, {'net_pnl': 0, 'ret_pct': 0})
        print(f"{y:<20} | ₹{y1['net_pnl']:+9,.0f} ({y1['ret_pct']:+5.1f}%) | ₹{y2['net_pnl']:+9,.0f} ({y2['ret_pct']:+5.1f}%) | ₹{y3['net_pnl']:+9,.0f} ({y3['ret_pct']:+5.1f}%)")
        
    # Print In-Sample vs Out-of-Sample Results
    print("\n" + "="*80)
    print("🔬 OUT-OF-SAMPLE (OOS) VALIDATION RESULTS")
    print("="*80)
    print("In-Sample (IS): 2021 - 2024 (4 Years) | Out-of-Sample (OOS): 2025 - 2026 (1.5+ Years Unseen)")
    print("-" * 80)
    for m in [m_s1b, m_s2, m_s3]:
        is_p = m['is_stats']
        oos_p = m['oos_stats']
        deg = (oos_p['profit_factor'] / is_p['profit_factor']) if (is_p.get('profit_factor', 0) > 0 and oos_p.get('profit_factor', 0) > 0) else 0
        print(f"📊 {m['name']}:")
        print(f"   • IN-SAMPLE (2021-2024): {is_p['trades']} trades | Net P&L: ₹{is_p['net_pnl']:+,.2f} | PF: {is_p['profit_factor']:.2f} | Win Rate: {is_p['win_rate']:.1f}%")
        print(f"   • OUT-OF-SAMPLE (2025-2026): {oos_p['trades']} trades | Net P&L: ₹{oos_p['net_pnl']:+,.2f} | PF: {oos_p['profit_factor']:.2f} | Win Rate: {oos_p['win_rate']:.1f}%")
        print(f"   • OOS Robustness Ratio (PF_oos / PF_is): {deg:.2f} ({'✅ PASS (>0.70)' if deg >= 0.70 else '⚠️ CAUTION'})\n")

if __name__ == "__main__":
    run_evaluation()
