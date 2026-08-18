#!/usr/bin/env python3
"""
Debug engine trade accumulation step by step
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

df["sym"] = df["tok"].map(tok_to_sym)

bucket_map = {sym: 1_00_000.0 for sym in target_tokens.keys()}
vacuum_map = {sym: 2_500_000.0 for sym in target_tokens.keys()}

engine = CascadeValueEngine(
    token_to_symbol=target_tokens,
    bucket_map=bucket_map,
    vacuum_map=vacuum_map,
    dkla_notional=1_00_00_000.0,
    trigger_config={
        "hawkes_lookback_sec": 300,
        "hawkes_bin_sec": 10,
        "minimax_beta": 0.10,
        "minimax_W": 0.50,
        "minimax_mom": 0.30,
        "minimax_tox_pct": 50,
        "conv_thresh_burst": 5.5,
        "conv_thresh_grind": 6.0,
        "conv_thresh_whale": 7.0,
        "min_buckets_for_bayes": 10,
    }
)

engine._hawkes_bins = int(300 / 10)

print("Processing first 100 ticks with debug...")
for idx, row in df.iterrows():
    if idx >= 100:
        break
    
    tick = {
        "token": str(int(row["tok"])),
        "last_traded_price": float(row["lp"]),
        "volume_trade_for_the_day": float(row["vol"]),
        "timestamp": float(row["ts"]) / 1000.0,
        "bp1": float(row["bp1"]), "bq1": float(row["bq1"]),
        "bp2": float(row["bp2"]), "bq2": float(row["bq2"]),
        "bp3": float(row["bp3"]), "bq3": float(row["bq3"]),
        "bp4": float(row["bp4"]), "bq4": float(row["bq4"]),
        "bp5": float(row["bp5"]), "bq5": float(row["bq5"]),
        "sp1": float(row["sp1"]), "sq1": float(row["sq1"]),
        "sp2": float(row["sp2"]), "sq2": float(row["sq2"]),
        "sp3": float(row["sp3"]), "sq3": float(row["sq3"]),
        "sp4": float(row["sp4"]), "sq4": float(row["sq4"]),
        "sp5": float(row["sp5"]), "sq5": float(row["sq5"]),
    }
    
    sym = "TATASTEEL"
    before_buy = engine.cur_buy_val.get(sym, 0)
    before_sell = engine.cur_sell_val.get(sym, 0)
    before_tot = engine.cur_tot_val.get(sym, 0)
    before_st_vol = engine.last_snap.get(sym, {}).get("vol", "N/A")
    
    engine.on_tick(tick)
    
    after_buy = engine.cur_buy_val.get(sym, 0)
    after_sell = engine.cur_sell_val.get(sym, 0)
    after_tot = engine.cur_tot_val.get(sym, 0)
    after_st_vol = engine.last_snap.get(sym, {}).get("vol", "N/A")
    
    d_buy = after_buy - before_buy
    d_sell = after_sell - before_sell
    d_tot = after_tot - before_tot
    
    if d_tot > 0:
        print("Tick {}: LTP={:.2f} vol={:.0f} dVol={:.0f} buy={:.0f} sell={:.0f} tot={:.0f} st_vol {}->{}".format(
            idx, row["lp"], row["vol"], d_buy+d_sell, d_buy, d_sell, d_tot, before_st_vol, after_st_vol))

print("Final state:")
print("cur_buy_val:", engine.cur_buy_val)
print("cur_sell_val:", engine.cur_sell_val)
print("cur_tot_val:", engine.cur_tot_val)
