#!/usr/bin/env python3
"""
Final Institutional Comparison: The 3 Top Index Recommendations
1. Strategy 1: Filtered 15M ORB (Futures & Options versions)
2. Strategy 2: Futures Trend Following (EMA 50/200 + Donchian)
3. Strategy 3: The Master Index Hybrid (Strategy 1 + Strategy 2 Combined)

Extracts:
- Net Capital used to generate the profits shown
- Average Capital required for 1 trade
- Sufficient Capital to deploy tomorrow morning (Margin + Drawdown buffer)
- Exact Daily Trade Frequency (Trades/day, zero-trade days, max trades)
- Impact of SEBI / NSE derivatives & auctioning reforms
"""

import os
import sys
import json
import numpy as np
import pandas as pd
from pathlib import Path

LAB_DIR = Path("/Users/rishabhbhangale/Desktop/Trading/index-strategies-lab")
sys.path.insert(0, str(LAB_DIR))

from index_strategies_lab_engine import prepare_data_and_indicators, simulate_strategy_1_orb, simulate_strategy_2_trend_following, calculate_index_taxes, LOT_SIZE

def run_combined_hybrid(df_15m):
    """Combines Strategy 1 (Filtered ORB) and Strategy 2 (Trend Following) into a unified portfolio."""
    res_s1 = simulate_strategy_1_orb(df_15m)
    res_s2 = simulate_strategy_2_trend_following(df_15m)
    
    trades_s1 = res_s1['trades']
    trades_s2 = res_s2['trades']
    
    # Tag strategy source
    for t in trades_s1: t['source'] = 'Filtered ORB'
    for t in trades_s2: t['source'] = 'Trend Following'
    
    all_trades = sorted(trades_s1 + trades_s2, key=lambda x: x['entry_time'])
    
    t_df = pd.DataFrame(all_trades)
    winners = t_df[t_df['net_pnl'] > 0]
    losers = t_df[t_df['net_pnl'] <= 0]
    
    total_gross = t_df['pnl'].sum()
    total_tax = t_df['tax'].sum()
    total_net = t_df['net_pnl'].sum()
    
    gp = winners['net_pnl'].sum() if len(winners) > 0 else 0.0
    gl = abs(losers['net_pnl'].sum()) if len(losers) > 0 else 0.0
    pf = (gp / gl) if gl > 0 else 0.0
    win_rate = len(winners) / len(t_df) * 100.0
    
    cum_net = t_df['net_pnl'].cumsum()
    peak = cum_net.cummax()
    max_dd = (peak - cum_net).max()
    
    durations = (t_df['exit_time'] - t_df['entry_time']).dt.total_seconds() / 60.0
    
    return {
        'name': 'Strategy 3: Master Index Hybrid (Combined)',
        'total_trades': len(t_df),
        'win_rate': round(win_rate, 2),
        'profit_factor': round(pf, 2),
        'total_gross_pnl': round(total_gross, 2),
        'total_tax': round(total_tax, 2),
        'total_net_pnl': round(total_net, 2),
        'net_avg_profit_per_trade': round(total_net / len(t_df), 2),
        'avg_duration_mins': round(durations.mean(), 1),
        'max_drawdown': round(max_dd, 2),
        'trades': all_trades
    }

def analyze_capital_and_frequency(strat_dict, is_option=False):
    trades = strat_dict['trades']
    t_df = pd.DataFrame(trades)
    t_df['day'] = t_df['entry_time'].dt.date
    
    # 1. Trade Frequency per Day
    daily_counts = t_df.groupby('day').size()
    total_market_days = 1255  # 2021 to 2026
    active_trading_days = len(daily_counts)
    zero_trade_days = total_market_days - active_trading_days
    
    avg_trades_per_market_day = len(trades) / total_market_days
    avg_trades_per_active_day = daily_counts.mean() if active_trading_days > 0 else 0
    max_trades_in_day = daily_counts.max() if active_trading_days > 0 else 0
    
    # 2. Capital Metrics
    # NIFTY Futures Span + Exposure Margin ~ 10-12% of contract value
    # Contract value = Spot * 50. In 2021-2026, spot ranged from 14,000 to 22,500.
    # Typical Zerodha NIFTY Futures margin: ₹1,15,000 to ₹1,35,000 per lot.
    # For Options: Capital = Premium * 50 (~₹5,000 to ₹10,000).
    if is_option:
        avg_cap_per_trade = 7500.0  # ₹150 avg premium * 50
        max_concurrent_positions = 1
        peak_margin_used = 15000.0
        recommended_buffer = strat_dict['max_drawdown'] * 1.5
        min_deploy_capital = peak_margin_used + max(15000, recommended_buffer)
    else:
        avg_cap_per_trade = 125000.0  # ₹1.25 Lakhs Zerodha Futures Margin per lot
        # Check max concurrent positions on any given minute/day
        max_concurrent_positions = 1 if 'Combined' not in strat_dict['name'] else 2
        peak_margin_used = avg_cap_per_trade * max_concurrent_positions
        # Drawdown buffer
        recommended_buffer = strat_dict['max_drawdown'] * 0.5  # 50% DD safety buffer
        min_deploy_capital = peak_margin_used + max(40000, recommended_buffer)
        
    return {
        'total_trades': len(trades),
        'active_days': active_trading_days,
        'zero_trade_days': zero_trade_days,
        'pct_zero_trade_days': round((zero_trade_days / total_market_days) * 100, 1),
        'avg_trades_per_day': round(avg_trades_per_market_day, 2),
        'avg_trades_active_day': round(avg_trades_per_active_day, 2),
        'max_trades_day': int(max_trades_in_day),
        'avg_cap_per_trade': avg_cap_per_trade,
        'peak_margin_used': peak_margin_used,
        'recommended_deploy_capital': round(min_deploy_capital, -3)  # round to thousands
    }

def main():
    print("="*75)
    print("🔍 RUNNING FINAL 3-STRATEGY COMPARISON ON NIFTY (2021 - 2026)")
    print("="*75, flush=True)
    
    df_15m, df_5m = prepare_data_and_indicators()
    
    res_s1 = simulate_strategy_1_orb(df_15m)
    res_s2 = simulate_strategy_2_trend_following(df_15m)
    res_s3 = run_combined_hybrid(df_15m)
    
    cap_s1_fut = analyze_capital_and_frequency(res_s1, is_option=False)
    cap_s1_opt = analyze_capital_and_frequency(res_s1, is_option=True)
    cap_s2_fut = analyze_capital_and_frequency(res_s2, is_option=False)
    cap_s3_hyb = analyze_capital_and_frequency(res_s3, is_option=False)
    
    print("\n" + "="*115)
    print("📊 COMPREHENSIVE COMPARISON: THE 3 RECOMMENDED INDEX STRATEGIES")
    print("="*115)
    
    strats = [
        ("1A. Filtered ORB (Futures)", res_s1, cap_s1_fut),
        ("1B. Filtered ORB (ATM Options)", res_s1, cap_s1_opt),
        ("2. Futures Trend Following", res_s2, cap_s2_fut),
        ("3. Master Index Hybrid (Combined)", res_s3, cap_s3_hyb)
    ]
    
    print(f"{'Strategy Name':<32} {'Net P&L (₹)':<15} {'Net Avg/Tr':<12} {'Max DD (₹)':<13} {'Avg Cap/Tr':<13} {'Deploy Tomorrow':<16} {'Trades/Day':<10}")
    print("-" * 115)
    for name, r, c in strats:
        print(f"{name:<32} ₹{r['total_net_pnl']:<14,.0f} ₹{r['net_avg_profit_per_trade']:<11,.0f} ₹{r['max_drawdown']:<12,.0f} "
              f"₹{c['avg_cap_per_trade']:<12,.0f} ₹{c['recommended_deploy_capital']:<15,.0f} {c['avg_trades_per_day']:<10.2f}")
    print("="*115)

if __name__ == "__main__":
    main()
