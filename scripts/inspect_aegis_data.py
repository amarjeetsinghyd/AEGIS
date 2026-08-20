"""
inspect_aegis_data.py — Inspect AEGIS L2 Options Archiver data for 2026-08-20 (Sensex Expiry).
"""
import glob
import os
import pandas as pd
import numpy as np

def main():
    root = "/home/opc/AEGIS/data/l2_raw"
    files = sorted(glob.glob(os.path.join(root, "aegis_l2_raw_2026-08-20_*.parquet")))
    print(f"Total AEGIS parquet files for 20-Aug: {len(files)}")
    if not files:
        print("No files found!")
        return

    dfs = []
    for f in files:
        df = pd.read_parquet(f)
        dfs.append(df)

    full_df = pd.concat(dfs, ignore_index=True)
    print(f"Total ticks: {len(full_df):,}")
    print(f"Columns: {list(full_df.columns)}")
    print(f"Tokens in dataset: {full_df['token'].nunique() if 'token' in full_df.columns else full_df['tok'].nunique()}")

    tok_col = 'token' if 'token' in full_df.columns else 'tok'
    ts_col = 'ts' if 'ts' in full_df.columns else 'exch_ts'
    
    # Show time range
    min_ts = full_df[ts_col].min()
    max_ts = full_df[ts_col].max()
    if min_ts > 1e11:
        min_ts /= 1000.0
        max_ts /= 1000.0
    min_dt = pd.to_datetime(min_ts, unit='s', utc=True).tz_convert('Asia/Kolkata')
    max_dt = pd.to_datetime(max_ts, unit='s', utc=True).tz_convert('Asia/Kolkata')
    print(f"Time Range: {min_dt} to {max_dt}")

    # Inspect token breakdown
    print("\nToken breakdown:")
    for tok, g in full_df.groupby(tok_col):
        min_p = g['lp'].min() if 'lp' in g.columns else g['price'].min()
        max_p = g['lp'].max() if 'lp' in g.columns else g['price'].max()
        last_p = g['lp'].iloc[-1] if 'lp' in g.columns else g['price'].iloc[-1]
        print(f"Token {tok}: {len(g):,} ticks | Min Price: ₹{min_p:.2f} | Max Price: ₹{max_p:.2f} | Last Price: ₹{last_p:.2f}")

if __name__ == "__main__":
    main()
