#!/usr/bin/env python3
"""
Debug trade value accumulation
"""
import sys, os, json, numpy as np, pandas as pd
from pathlib import Path
from datetime import datetime, time as dt_time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stock_order_flow.cascade.value_engine import CascadeValueEngine

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

print("Total ticks:", len(df))

df["sym"] = df["tok"].map(tok_to_sym)

# Check volume and trade value
print("Volume stats:")
print(df["vol"].describe())
print("Volume diff (trade volume):")
dvol = df["vol"].diff().clip(lower=0)
print(dvol.describe())
print("Trade value (dvol * lp):")
trade_val = dvol * df["lp"]
print(trade_val.describe())
print("Sum trade value:", trade_val.sum())
print("Cumulative trade value:")
cum_val = trade_val.cumsum()
print(cum_val.tail(10))

# Check how many ticks have non-zero trade volume
print("Ticks with trade:", (dvol > 0).sum())
print("Total trade value:", trade_val.sum())
