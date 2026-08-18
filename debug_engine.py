#!/usr/bin/env python3
"""
Debug adaptive engine - check initialization and bucket processing
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

TARGET = ["TATASTEEL", "SUNPHARMA", "ICICIPRULI", "CANBK", "PNB", "JIOFIN"]
target_tokens = {sym: sym_to_tok[sym] for sym in TARGET if sym in sym_to_tok}
print("Target symbols:", list(target_tokens.keys()))

parts = sorted(DATA_DIR.glob("l2_raw_2026-08-17_part_*.parquet"))
print("Loading", len(parts), "parquet partitions...")

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

print("Total ticks:", len(df), "| Unique tokens:", df["tok"].nunique())

df["sym"] = df["tok"].map(tok_to_sym)

# Lower bucket turnover for faster debugging
bucket_map = {sym: 50_00_000.0 for sym in target_tokens.keys()}  # 50L instead of 2Cr
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

print("engine.sym_to_idx:", engine.sym_to_idx)
print("engine.t2s:", engine.t2s)

signals = []
bucket_count = 0

for idx, row in df.iterrows():
    if idx >= 50000:
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
    
    engine.on_tick(tick)
    
    while not engine.signal_queue.empty():
        sig = engine.signal_queue.get()
        print("SIGNAL:", sig)
        signals.append(sig)
    
    for sym in target_tokens.keys():
        st = engine.last_snap.get(sym, {})
        bts = st.get("bts", 0)
        last_bts = st.get("_last_bucket_ts", 0)
        if bts != last_bts and bts > last_bts:
            bucket_count += 1
            print("Bucket #{}: {} at tick {}".format(bucket_count, sym, idx))
            adv = st.get("_adv", {})
            print("  Branching:", adv.get("branching", "N/A"))
            print("  WhaleZ:", adv.get("whale_z", "N/A"))
            print("  Mom5m:", adv.get("mom_5m", "N/A"))
            print("  Toxicity:", adv.get("dkla_toxicity", "N/A"))
            print("  NetVac:", adv.get("net_vacuum_bps", "N/A"))
            print("  MPAR:", adv.get("mp_accel", "N/A"), "align:", adv.get("mp_align", "N/A"))
            print("  LRA:", adv.get("lra_asym", "N/A"))
            print("  DKLA:", adv.get("dkla_asym", "N/A"))
            if sym in engine.sym_to_idx:
                sym_idx = engine.sym_to_idx[sym]
                regime_names = {0:"NOISE",1:"BURST",2:"GRIND",3:"WHALE"}
                regime = regime_names.get(engine.regime_id[sym_idx], "UNK")
                print("  Regime:", regime, "conf:", engine.regime_conf[sym_idx])
                print("  Beta EMA:", engine.regime_beta_ema[sym_idx])
                print("  W EMA:", engine.regime_W_ema[sym_idx])
                print("  Mom EMA:", engine.regime_mom_ema[sym_idx])
                print("  Dynamic thresholds:", engine._get_dynamic_thresholds(sym_idx))
            st["_last_bucket_ts"] = bts

print("Total signals:", len(signals))
print("Total buckets:", bucket_count)

print("Final regime state:")
for sym in target_tokens.keys():
    if sym in engine.sym_to_idx:
        sym_idx = engine.sym_to_idx[sym]
        regime_names = {0: "NOISE", 1: "BURST", 2: "GRIND", 3: "WHALE"}
        regime = regime_names.get(engine.regime_id[sym_idx], "UNKNOWN")
        conf = engine.regime_conf[sym_idx]
        beta_ema = engine.regime_beta_ema[sym_idx]
        w_ema = engine.regime_W_ema[sym_idx]
        mom_ema = engine.regime_mom_ema[sym_idx]
        buckets = engine.bucket_count[sym_idx]
        print("   {}: {} (conf={:.2f}) | beta={:.3f} W={:.2f} M={:+.3f}% | Buckets: {}".format(
            sym, regime, conf, beta_ema, w_ema, mom_ema, buckets))
