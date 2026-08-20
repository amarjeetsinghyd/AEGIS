"""
analyze_aegis_18aug_strikes.py — Analyze actual price action of 18-Aug options contracts.
"""
import glob
import os
import pandas as pd
import numpy as np

def main():
    root = r"c:\Users\Amarjeet Singh\quant-bot\AEGIS\data\l2_raw"
    files = sorted(glob.glob(os.path.join(root, "*2026-08-18*.parquet")))
    print(f"Loading {len(files)} AEGIS L2 Parquet files for 18-Aug...")
    dfs = [pd.read_parquet(f) for f in files]
    full_df = pd.concat(dfs, ignore_index=True).sort_values("ts").reset_index(drop=True)
    
    print(f"Total Ticks: {len(full_df):,}")
    tok_col = "tok" if "tok" in full_df.columns else "token"
    sym_col = "sym" if "sym" in full_df.columns else "symbol"
    
    # Analyze key active ATM strikes on 18-Aug
    active_strikes = [
        "NIFTY18AUG26C24200", "NIFTY18AUG26P24200",
        "NIFTY18AUG26C24250", "NIFTY18AUG26P24250",
        "NIFTY18AUG26C24300", "NIFTY18AUG26P24300",
        "NIFTY18AUG26C24400", "NIFTY18AUG26P24400",
        "NIFTY18AUG26C24500", "NIFTY18AUG26P24500"
    ]
    
    print("\n" + "=" * 105)
    print("18-AUG NIFTY ATM OPTION CONTRACTS REAL PRICE MOVEMENT (724,530 Ticks)")
    print("=" * 105)
    
    for sym in active_strikes:
        sub = full_df[full_df[sym_col] == sym].copy()
        if sub.empty:
            continue
        
        min_p = sub["lp"].min()
        max_p = sub["lp"].max()
        first_p = sub["lp"].iloc[0]
        last_p = sub["lp"].iloc[-1]
        
        max_gain_pct = ((max_p - min_p) / min_p) * 100.0 if min_p > 0 else 0.0
        
        print(f"Symbol: {sym:<20} | Ticks: {len(sub):6,d} | Open: Rs.{first_p:6.2f} | Low: Rs.{min_p:6.2f} | High: Rs.{max_p:6.2f} | Close: Rs.{last_p:6.2f} | Max Intra-Day Swing: +{max_gain_pct:6.1f}%")
        
    print("=" * 105)

if __name__ == "__main__":
    main()
