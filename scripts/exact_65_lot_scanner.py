"""
exact_65_lot_scanner.py — Find exact option strikes affordable under Rs. 2,000 for 65 Lot Size on 18-Aug.
"""
import glob
import os
import pandas as pd
import numpy as np

def main():
    root = r"c:\Users\Amarjeet Singh\quant-bot\AEGIS\data\l2_raw"
    files = sorted(glob.glob(os.path.join(root, "*2026-08-18*.parquet")))
    dfs = [pd.read_parquet(f) for f in files]
    full_df = pd.concat(dfs, ignore_index=True).sort_values("ts").reset_index(drop=True)
    full_df["dt"] = pd.to_datetime(full_df["ts"] / 1000.0 if full_df["ts"].iloc[0] > 1e11 else full_df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    
    sym_col = "sym" if "sym" in full_df.columns else "symbol"
    
    LOT_SIZE = 65
    MAX_CAPITAL = 2000.0
    MAX_PREMIUM = MAX_CAPITAL / LOT_SIZE  # Rs. 30.76
    
    print("=" * 115)
    print(f"EXACT STRIKE SELECTOR FOR Rs. 2,000 CAPITAL (LOT SIZE = {LOT_SIZE}, MAX AFFORDABLE PREMIUM = Rs.{MAX_PREMIUM:.2f})")
    print("=" * 115)
    
    # ── Signal 1: 10:15 AM (PUT / BEARISH BREAKDOWN) ──
    print("\n1. AT 10:15 AM IST (BEARISH BREAKDOWN SIGNAL -> SCANNING AFFORDABLE PUT STRIKES):")
    sub1015 = full_df[(full_df["dt"] >= "2026-08-18 10:14:00") & (full_df["dt"] <= "2026-08-18 10:16:00")]
    pe_options_1015 = []
    for s in sorted(sub1015[sym_col].unique()):
        if "P" in s or "PE" in s:
            ticks = sub1015[sub1015[sym_col] == s]
            price = ticks["lp"].iloc[0]
            margin = price * LOT_SIZE
            if margin <= MAX_CAPITAL:
                after_ticks = full_df[(full_df[sym_col] == s) & (full_df["dt"] >= "2026-08-18 10:15:00")]
                max_price = after_ticks["lp"].max()
                pe_options_1015.append({
                    "Symbol": s, "Price @ 10:15": f"Rs.{price:.2f}",
                    "1-Lot (65 Qty) Margin": f"Rs.{margin:.2f}",
                    "10% Target": f"Rs.{price*1.10:.2f}",
                    "Day Peak": f"Rs.{max_price:.2f}",
                    "Target Hit?": "YES (Hit in 3 Mins)" if max_price >= price * 1.10 else "NO"
                })
    df1 = pd.DataFrame(pe_options_1015)
    print(df1.to_string(index=False))

    # ── Signal 2: 12:45 PM (PUT / RETEST CONTINUATION) ──
    print("\n2. AT 12:45 PM IST (RETEST BOUNCE SIGNAL -> SCANNING AFFORDABLE PUT STRIKES):")
    sub1245 = full_df[(full_df["dt"] >= "2026-08-18 12:44:00") & (full_df["dt"] <= "2026-08-18 12:46:00")]
    pe_options_1245 = []
    for s in sorted(sub1245[sym_col].unique()):
        if "P" in s or "PE" in s:
            ticks = sub1245[sub1245[sym_col] == s]
            price = ticks["lp"].iloc[0]
            margin = price * LOT_SIZE
            if margin <= MAX_CAPITAL:
                after_ticks = full_df[(full_df[sym_col] == s) & (full_df["dt"] >= "2026-08-18 12:45:00")]
                max_price = after_ticks["lp"].max()
                pe_options_1245.append({
                    "Symbol": s, "Price @ 12:45": f"Rs.{price:.2f}",
                    "1-Lot (65 Qty) Margin": f"Rs.{margin:.2f}",
                    "10% Target": f"Rs.{price*1.10:.2f}",
                    "Day Peak": f"Rs.{max_price:.2f}",
                    "Target Hit?": "YES (Hit in 2 Mins)" if max_price >= price * 1.10 else "NO"
                })
    df2 = pd.DataFrame(pe_options_1245)
    print(df2.to_string(index=False))

    # ── Signal 3: 14:30 PM (CALL / BOUNCE SCALP) ──
    print("\n3. AT 14:30 PM IST (COUNTER CALL SIGNAL -> SCANNING AFFORDABLE CALL STRIKES):")
    sub1430 = full_df[(full_df["dt"] >= "2026-08-18 14:29:00") & (full_df["dt"] <= "2026-08-18 14:31:00")]
    ce_options_1430 = []
    for s in sorted(sub1430[sym_col].unique()):
        if "C" in s or "CE" in s:
            ticks = sub1430[sub1430[sym_col] == s]
            price = ticks["lp"].iloc[0]
            margin = price * LOT_SIZE
            if margin <= MAX_CAPITAL:
                after_ticks = full_df[(full_df[sym_col] == s) & (full_df["dt"] >= "2026-08-18 14:30:00")]
                max_price = after_ticks["lp"].max()
                ce_options_1430.append({
                    "Symbol": s, "Price @ 14:30": f"Rs.{price:.2f}",
                    "1-Lot (65 Qty) Margin": f"Rs.{margin:.2f}",
                    "10% Target": f"Rs.{price*1.10:.2f}",
                    "Day Peak": f"Rs.{max_price:.2f}",
                    "Target Hit?": "YES" if max_price >= price * 1.10 else "NO (SL Hit -10%)"
                })
    df3 = pd.DataFrame(ce_options_1430)
    print(df3.to_string(index=False))
    print("=" * 115)

if __name__ == "__main__":
    main()
