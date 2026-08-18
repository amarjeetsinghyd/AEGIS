#!/usr/bin/env python3
"""
Debug trade classification
"""
import sys, os, json, numpy as np, pandas as pd
from pathlib import Path
from datetime import datetime, time as dt_time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "stock_order_flow" / "calibration_notes" / "l2_data" / "2026-08-17"
UNI_PATH = BASE_DIR / "stock_order_flow" / "calibration_notes" / "l2_data" / "universe.json"

MARKET_OPEN = dt_time(9, 15)
MARKET_CLOSE = dt_time(15, 30)

with open(UNI_PATH) as f:
    uni = json.load(f)

tok_to_sym = {}
sym_to_tok = {}
for s in uni.get("stocks", []):
    tok_to_sym[int(s["token"])] = s["symbol"]
    sym_to_tok[s["symbol"]] = int(s["token"])

TARGET = ["TATASTEEL"]
target_tokens = {sym: sym_to_tok[sym] for sym in TARGET if sym in sym_to_tok}

parts = sorted(DATA_DIR.glob("l2_raw_2026-08-17_part_*.parquet"))

dfs = []
for p in parts:
    df = pd.read_parquet(p)
    df = df[df["tok"].isin(target_tokens.values())]
    if not df.empty:
        dfs.append(df)

df = pd.concat(dfs, ignore_index=True).sort_values("ts").reset_index(drop=True)

df["dt"] = (pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_convert("Asia/Kolkata"))
t = df["dt"].dt.time
df = df[(t >= MARKET_OPEN) & (t <= MARKET_CLOSE)].reset_index(drop=True)

df["sym"] = df["tok"].map(tok_to_sym)

dvol = df["vol"].diff().clip(lower=0)
trade_val = dvol * df["lp"]

bp1 = df["bp1"]
sp1 = df["sp1"]
lp = df["lp"]

buy_mask = (lp >= sp1) & (dvol > 0)
sell_mask = (lp <= bp1) & (dvol > 0)
mid_mask = (~buy_mask & ~sell_mask) & (dvol > 0)

print("Total trades:", int((dvol > 0).sum()))
print("Buy trades:", int(buy_mask.sum()))
print("Sell trades:", int(sell_mask.sum()))
print("Mid trades:", int(mid_mask.sum()))

print("Buy trade value:", trade_val[buy_mask].sum())
print("Sell trade value:", trade_val[sell_mask].sum())
print("Mid trade value:", trade_val[mid_mask].sum())

print("LTP >= sp1:", int((lp >= sp1).sum()))
print("LTP <= bp1:", int((lp <= bp1).sum()))
print("bp1 < LTP < sp1:", int(((lp > bp1) & (lp < sp1)).sum()))

trade_rows = df[dvol > 0].head(20)
for _, row in trade_rows.iterrows():
    d = row["vol"] - (df.loc[df.index < row.name, "vol"].iloc[-1] if len(df[df.index < row.name]) > 0 else 0)
    if d <= 0:
        continue
    tval = d * row["lp"]
    if row["lp"] >= row["sp1"]:
        side = "BUY"
    elif row["lp"] <= row["bp1"]:
        side = "SELL"
    else:
        side = "MID"
    print("  LTP={:.2f} bp1={:.2f} sp1={:.2f} dvol={:.0f} tval={:.0f} side={}".format(
        row["lp"], row["bp1"], row["sp1"], d, tval, side))
