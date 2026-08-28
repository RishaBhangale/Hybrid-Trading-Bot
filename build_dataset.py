#!/usr/bin/env python3
"""
Fast Parallel Dataset Builder for NIFTY 1-Min Spot & Real Options (2021 - 2026)
Compiles all 1,255 daily CSV files in Week_1min into a single high-speed Parquet file.
Extracts:
- Spot OHLC & Volume
- ATM Call & Put Premiums
- OTM+2 Call & OTM-2 Put Premiums (for Debit Spread Payoffs)
"""

import os
import sys
import glob
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor
import pandas as pd
import numpy as np

DATA_DIR = Path("/Users/rishabhbhangale/Desktop/Trading/Week_1min")
OUTPUT_FILE = Path("/Users/rishabhbhangale/Desktop/Trading/index-strategies-lab/nifty_complete_2021_2026.parquet")

def process_single_csv(file_path: str):
    try:
        df = pd.read_csv(file_path)
        if df.empty or 'datetime' not in df.columns or 'spot' not in df.columns:
            return None
            
        # Spot OHLC
        spot = df.groupby('datetime')['spot'].agg(['first', 'max', 'min', 'last']).rename(
            columns={'first': 'open', 'max': 'high', 'min': 'low', 'last': 'close'}
        )
        vol = df.groupby('datetime')['volume'].sum().rename('volume')
        res = pd.concat([spot, vol], axis=1)
        
        # Real ATM Options
        atm_ce = df[(df['strike_label'] == 'ATM') & (df['option_type'] == 'CALL')].drop_duplicates('datetime').set_index('datetime')['close'].rename('atm_ce_close')
        atm_pe = df[(df['strike_label'] == 'ATM') & (df['option_type'] == 'PUT')].drop_duplicates('datetime').set_index('datetime')['close'].rename('atm_pe_close')
        
        # Real OTM Spread Legs (ATM+2 for Bull Call Spread, ATM-2 for Bear Put Spread)
        otm2_ce = df[(df['strike_label'] == 'ATM+2') & (df['option_type'] == 'CALL')].drop_duplicates('datetime').set_index('datetime')['close'].rename('otm2_ce_close')
        otm2_pe = df[(df['strike_label'] == 'ATM-2') & (df['option_type'] == 'PUT')].drop_duplicates('datetime').set_index('datetime')['close'].rename('otm2_pe_close')
        
        full_day = pd.concat([res, atm_ce, atm_pe, otm2_ce, otm2_pe], axis=1).reset_index()
        full_day['datetime'] = pd.to_datetime(full_day['datetime'])
        
        # Data Sanitation: Filter out vendor contamination (BANKNIFTY spots mixed into NIFTY)
        y = full_day['datetime'].dt.year
        valid_mask = (
            ((y == 2021) & (full_day['close'] >= 13000) & (full_day['close'] <= 19000)) |
            ((y == 2022) & (full_day['close'] >= 14500) & (full_day['close'] <= 19500)) |
            ((y == 2023) & (full_day['close'] >= 16000) & (full_day['close'] <= 22500)) |
            ((y >= 2024) & (full_day['close'] >= 20000) & (full_day['close'] <= 27000))
        )
        full_day = full_day[valid_mask].copy()
        return full_day
    except Exception as e:
        return None

def main():
    print("="*70)
    print("🚀 COMPILING 5-YEAR NIFTY SPOT & REAL OPTIONS DATASET")
    print(f"   Source: {DATA_DIR}")
    print("="*70, flush=True)
    
    files = sorted(glob.glob(str(DATA_DIR / "**" / "NIFTY_*.csv"), recursive=True))
    print(f"Found {len(files)} daily CSV files. Processing in parallel...", flush=True)
    
    with ProcessPoolExecutor() as executor:
        results = list(executor.map(process_single_csv, files, chunksize=25))
        
    valid_dfs = [r for r in results if r is not None and not r.empty]
    print(f"Successfully processed {len(valid_dfs)} days. Concatenating...", flush=True)
    
    master_df = pd.concat(valid_dfs, ignore_index=True)
    master_df['datetime'] = pd.to_datetime(master_df['datetime'])
    master_df = master_df.sort_values('datetime').reset_index(drop=True)
    
    print(f"Master Dataset Shape: {master_df.shape}")
    print(f"Date Range: {master_df['datetime'].min()} to {master_df['datetime'].max()}")
    
    master_df.to_parquet(OUTPUT_FILE, index=False)
    print(f"✅ Saved unified dataset to: {OUTPUT_FILE} ({OUTPUT_FILE.stat().st_size / 1e6:.2f} MB)")

if __name__ == "__main__":
    main()
