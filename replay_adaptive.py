#!/usr/bin/env python3
"""
Adaptive Gating Architecture - Backtest Replay on 2026-08-17 L2 Data
FIXED: token_to_symbol mapping
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

HIGH_VALUE = ["TATASTEEL", "SUNPHARMA", "ICICIPRULI"]
NOISE = ["CANBK", "PNB", "JIOFIN"]
ALL_SYMBOLS = HIGH_VALUE + NOISE + ["BEL", "VEDL", "ITC", "WIPRO", "KOTAKBANK", "IDFCFIRSTB", "IRCTC", "POWERGRID", "COALINDIA"]

# FIXED: engine expects {token: symbol} mapping
target_tokens = {sym: sym_to_tok[sym] for sym in ALL_SYMBOLS if sym in sym_to_tok}
token_to_symbol = {tok: sym for sym, tok in target_tokens.items()}  # {token: symbol}
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

bucket_map = {sym: 2_00_00_000.0 for sym in target_tokens.keys()}
vacuum_map = {sym: 2_500_000.0 for sym in target_tokens.keys()}

engine = CascadeValueEngine(
    token_to_symbol=token_to_symbol,  # FIXED: {token: symbol}
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

signals = []

print("Processing ticks through adaptive engine...")
total_ticks = len(df)
for idx, row in df.iterrows():
    if idx % 50000 == 0:
        print("  {}/{} ({:.1f}%)".format(idx, total_ticks, idx/total_ticks*100))
    
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
        sym = sig["sym"]
        sym_idx = engine.sym_to_idx[sym]
        regime = engine.regime_id[sym_idx]
        regime_names = {0: "NOISE", 1: "BURST", 2: "GRIND", 3: "WHALE"}
        
        signals.append({
            "sym": sym, "ts": row["ts"], "side": sig["signal"], "lp": sig["lp"],
            "conviction": sig["conviction"], "regime": regime_names.get(regime, "UNKNOWN"),
            "branching": sig["metrics"].get("branching", 0),
            "whale_z": sig["metrics"].get("whale_z", 0),
            "mom_5m": sig["metrics"].get("mom_5m", 0),
        })

while not engine.signal_queue.empty():
    sig = engine.signal_queue.get()
    sym = sig["sym"]
    sym_idx = engine.sym_to_idx[sym]
    regime = engine.regime_id[sym_idx]
    regime_names = {0: "NOISE", 1: "BURST", 2: "GRIND", 3: "WHALE"}
    signals.append({
        "sym": sym, "ts": sig["metrics"].get("ts", 0), "side": sig["signal"], "lp": sig["lp"],
        "conviction": sig["conviction"], "regime": regime_names.get(regime, "UNKNOWN"),
        "branching": sig["metrics"].get("branching", 0),
        "whale_z": sig["metrics"].get("whale_z", 0),
        "mom_5m": sig["metrics"].get("mom_5m", 0),
    })

print("Total signals generated:", len(signals))

def check_forward_move(sym, signal_ts, signal_lp, side, forward_sec=900, target_pct=0.52):
    sym_df = df[df["sym"] == sym].copy()
    if sym_df.empty:
        return False, 0.0
    mask = sym_df["ts"] > signal_ts
    if not mask.any():
        return False, 0.0
    future = sym_df[mask].head(1000)
    if future.empty:
        return False, 0.0
    max_future_lp = future["lp"].max()
    min_future_lp = future["lp"].min()
    up_move = (max_future_lp - signal_lp) / signal_lp * 100
    dn_move = (signal_lp - min_future_lp) / signal_lp * 100
    if side == "BUY":
        return up_move >= target_pct, up_move
    else:
        return dn_move >= target_pct, dn_move

print("Analyzing signal performance...")
results = []
for sig in signals:
    captured, move_pct = check_forward_move(sig["sym"], sig["ts"], sig["lp"], sig["side"])
    results.append({
        "sym": sig["sym"], "side": sig["side"], "lp": sig["lp"],
        "conviction": sig["conviction"], "regime": sig["regime"],
        "branching": sig["branching"], "whale_z": sig["whale_z"],
        "mom_5m": sig["mom_5m"], "captured": captured, "move_pct": move_pct,
    })

results_df = pd.DataFrame(results)

baseline_results = {
    "TATASTEEL": {"curr_f1": 0.000, "curr_capture": 0.000},
    "SUNPHARMA": {"curr_f1": 0.000, "curr_capture": 0.000},
    "ICICIPRULI": {"curr_f1": 0.000, "curr_capture": 0.000},
    "CANBK": {"curr_f1": 0.000, "curr_capture": 0.000},
    "PNB": {"curr_f1": 0.000, "curr_capture": 0.000},
    "JIOFIN": {"curr_f1": 0.000, "curr_capture": 0.000},
    "BEL": {"curr_f1": 0.000, "curr_capture": 0.000},
    "VEDL": {"curr_f1": 0.000, "curr_capture": 0.000},
}

other = [s for s in target_tokens.keys() if s not in HIGH_VALUE and s not in NOISE]

print("="*80)
print("ADAPTIVE GATING ARCHITECTURE - BACKTEST REPLAY REPORT")
print("Date: 2026-08-17 | Engine: CascadeValueEngine v3.0 (Adaptive)")
print("="*80)

if len(results_df) == 0:
    print("NO SIGNALS GENERATED")
else:
    total_signals = len(results_df)
    total_captured = results_df["captured"].sum()
    overall_capture = total_captured / total_signals * 100 if total_signals > 0 else 0
    
    print("OVERALL PERFORMANCE")
    print("   Total Signals:", total_signals)
    print("   Captured:", total_captured, "({:.1f}%)".format(overall_capture))
    print("   Avg Conviction: {:.2f}".format(results_df["conviction"].mean()))
    print("   Regime Distribution:")
    for reg, cnt in results_df["regime"].value_counts().items():
        print("      {}: {} ({:.1f}%)".format(reg, cnt, cnt/total_signals*100))
    
    print("HIGH-VALUE SYMBOLS (TATASTEEL, SUNPHARMA, ICICIPRULI)")
    for sym in HIGH_VALUE:
        sym_df = results_df[results_df["sym"] == sym]
        if len(sym_df) == 0:
            print("   {}: NO SIGNALS".format(sym))
            continue
        
        n_sig = len(sym_df)
        n_cap = sym_df["captured"].sum()
        cap_rate = n_cap / n_sig * 100
        
        print("   {}:".format(sym))
        print("      Signals: {} | Captured: {} | Precision: {:.1f}%".format(n_sig, n_cap, cap_rate))
        print("      Avg Conviction: {:.2f}".format(sym_df["conviction"].mean()))
        print("      Regimes:", dict(sym_df["regime"].value_counts()))
        print("      Avg Branching: {:.3f} | Avg WhaleZ: {:.2f} | Avg Mom5m: {:+.2f}%".format(
            sym_df["branching"].mean(), sym_df["whale_z"].mean(), sym_df["mom_5m"].mean()))
        
        for _, r in sym_df.iterrows():
            status = "OK" if r["captured"] else "MISS"
            print("      {} {} @ {:.2f} | Conv: {:.1f} | Regime: {} | beta={:.3f} W={:.2f} M={:+.2f}% | Move: {:+.2f}%".format(
                status, r["side"], r["lp"], r["conviction"], r["regime"],
                r["branching"], r["whale_z"], r["mom_5m"], r["move_pct"]))
    
    print("NOISE SYMBOLS (CANBK, PNB, JIOFIN) - Should be filtered")
    for sym in NOISE:
        sym_df = results_df[results_df["sym"] == sym]
        n_sig = len(sym_df)
        if n_sig == 0:
            print("   {}: OK NO SIGNALS (correctly filtered by NOISE regime)".format(sym))
        else:
            n_cap = sym_df["captured"].sum()
            print("   {}: WARNING {} signals generated ({} captured)".format(sym, n_sig, n_cap))
            for _, r in sym_df.iterrows():
                print("      {} @ {:.2f} | Regime: {} | beta={:.3f} W={:.2f}".format(
                    r["side"], r["lp"], r["regime"], r["branching"], r["whale_z"]))
    
    print("OTHER SYMBOLS")
    for sym in other:
        sym_df = results_df[results_df["sym"] == sym]
        if len(sym_df) == 0:
            print("   {}: No signals".format(sym))
        else:
            n_cap = sym_df["captured"].sum()
            print("   {}: {} signals, {} captured ({:.1f}%)".format(
                sym, len(sym_df), n_cap, n_cap/len(sym_df)*100))

print("="*80)
print("COMPARISON: BASELINE (Fixed 0.35/1.20) vs ADAPTIVE")
print("="*80)
print("{:<14} | {:<12} | {:<12} | {:<12} | {:<12} | {:<8}".format(
    "Symbol", "Baseline F1", "Adaptive F1*", "Baseline Cap", "Adaptive Cap", "Signals"))
print("-"*90)

for sym in HIGH_VALUE + NOISE + other:
    if sym not in target_tokens:
        continue
    sym_df = results_df[results_df["sym"] == sym] if len(results_df) > 0 else pd.DataFrame()
    n_sig = len(sym_df)
    n_cap = sym_df["captured"].sum() if n_sig > 0 else 0
    adaptive_prec = n_cap / n_sig * 100 if n_sig > 0 else 0
    baseline = baseline_results.get(sym, {"curr_f1": 0, "curr_capture": 0})
    
    print("{:<14} | {:<12.3f} | {:<12} | {:<12.1f}% | {:<12.1f}% | {:<8}".format(
        sym, baseline["curr_f1"], "N/A", baseline["curr_capture"], adaptive_prec, n_sig))

print("* Adaptive F1 requires full move timeline (recall). Precision shown as proxy.")

print("="*80)
print("REGIME CLASSIFIER STATE (End of Day)")
print("="*80)
regime_names = {0: "NOISE", 1: "BURST", 2: "GRIND", 3: "WHALE"}
for sym in target_tokens.keys():
    sym_idx = engine.sym_to_idx[sym]
    regime = regime_names.get(engine.regime_id[sym_idx], "UNKNOWN")
    conf = engine.regime_conf[sym_idx]
    beta_ema = engine.regime_beta_ema[sym_idx]
    w_ema = engine.regime_W_ema[sym_idx]
    mom_ema = engine.regime_mom_ema[sym_idx]
    buckets = engine.bucket_count[sym_idx]
    print("   {:<12}: {:<6} (conf={:.2f}) | beta={:.3f} W={:.2f} M={:+.3f}% | Buckets: {}".format(
        sym, regime, conf, beta_ema, w_ema, mom_ema, buckets))

print("="*80)
print("BAYESIAN THRESHOLD POSTERIORS (End of Day)")
print("="*80)
for sym in target_tokens.keys():
    sym_idx = engine.sym_to_idx[sym]
    a_beta = engine.post_alpha_beta[sym_idx]
    b_beta = engine.post_beta_beta[sym_idx]
    a_W = engine.post_alpha_W[sym_idx]
    b_W = engine.post_beta_W[sym_idx]
    n_beta = a_beta + b_beta
    n_W = a_W + b_W
    mean_beta = a_beta / n_beta if n_beta > 0 else 0
    mean_W = a_W / n_W if n_W > 0 else 0
    print("   {:<12}: beta ~ Beta({:.1f}, {:.1f}) mean={:.3f} | W ~ Beta({:.1f}, {:.1f}) mean={:.3f} | N_buckets={}".format(
        sym, a_beta, b_beta, mean_beta, a_W, b_W, mean_W, engine.bucket_count[sym_idx]))

output = {
    "date": "2026-08-17",
    "engine": "CascadeValueEngine v3.0 Adaptive",
    "total_signals": int(total_signals) if "total_signals" in locals() else 0,
    "total_captured": int(total_captured) if "total_captured" in locals() else 0,
    "overall_capture_pct": float(overall_capture) if "overall_capture" in locals() else 0,
    "signals": results,
    "regime_final": {sym: regime_names.get(engine.regime_id[engine.sym_to_idx[sym]], "UNKNOWN") for sym in target_tokens.keys()},
    "posteriors": {sym: {
        "beta_alpha": float(engine.post_alpha_beta[engine.sym_to_idx[sym]]),
        "beta_beta": float(engine.post_beta_beta[engine.sym_to_idx[sym]]),
        "W_alpha": float(engine.post_alpha_W[engine.sym_to_idx[sym]]),
        "W_beta": float(engine.post_beta_W[engine.sym_to_idx[sym]]),
    } for sym in target_tokens.keys()}
}

out_path = BASE_DIR / "stock_order_flow" / "calibration_notes" / "adaptive_replay_2026-08-17.json"
with open(out_path, "w") as f:
    json.dump(output, f, indent=2, default=str)
print("Results saved to:", out_path)

print("BACKTEST REPLAY COMPLETE")
