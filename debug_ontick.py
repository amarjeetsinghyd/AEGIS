#!/usr/bin/env python3
"""
Debug engine on_tick internals
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

# Monkey-patch on_tick to debug
original_on_tick = engine.on_tick
def debug_on_tick(msg):
    tok = str(msg.get("token", ""))
    sym = engine.t2s.get(tok)
    print("  on_tick called: token={}, sym={}".format(tok, sym))
    if not sym:
        print("    No symbol for token!")
        return
    
    st = engine.last_snap.get(sym, {})
    lp = float(msg.get("last_traded_price") or msg.get("lp") or st.get("lp") or 0)
    vol = float(msg.get("volume_trade_for_the_day") or msg.get("v") or st.get("vol", 0))
    dVol = max(0.0, vol - st.get("vol", vol))
    print("    lp={}, vol={}, st.vol={}, dVol={}".format(lp, vol, st.get("vol", "N/A"), dVol))
    
    return original_on_tick(msg)

engine.on_tick = debug_on_tick

print("Processing first 10 ticks with debug...")
for idx, row in df.iterrows():
    if idx >= 10:
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
    
    print("Tick {}: LTP={:.2f} vol={:.0f}".format(idx, row["lp"], row["vol"]))
    engine.on_tick(tick)
    print()

print("Final state:")
print("cur_buy_val:", engine.cur_buy_val)
print("cur_sell_val:", engine.cur_sell_val)
print("cur_tot_val:", engine.cur_tot_val)
