"""
build_canonical_candles.py — Resamples Raw L2 Ticks into Canonical 5m, 15m, and 1h Candles.
Saves unified, partitioned parquet datasets on the VPS.
"""
import glob
import json
import os
import sys
import time
from datetime import datetime
import numpy as np
import pandas as pd

REQUIRED_COLS = ["token", "exch_ts", "lp", "vol"]

def load_token_symbol_map(data_dir: str) -> dict:
    candidates = [
        os.path.join(data_dir, "tokens_cache.json"),
        os.path.join(os.path.dirname(data_dir.rstrip("/\\")), "tokens_cache.json"),
        "/home/opc/ARGUS/data/tokens_cache.json",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    tc = json.load(f)
                m = {str(meta["token"]): sym for sym, meta in tc.items() if "token" in meta}
                print(f"[load] Token->Symbol map loaded: {path} ({len(m)} symbols)")
                return m
            except Exception:
                pass
    return {}

def process_symbol_date_group(symbol: str, date: str, sub: pd.DataFrame, freq: str) -> pd.DataFrame:
    """Resample raw ticks into high-precision canonical OHLCV + Delta candles."""
    sub = sub.sort_values("dt").set_index("dt")
    
    # Calculate per-tick incremental volume
    vol_diff = sub["vol"].diff().fillna(0)
    vol_diff = np.where(vol_diff < 0, 1.0, vol_diff)
    vol_diff[0] = max(1.0, vol_diff[0])
    sub["tick_vol"] = vol_diff
    sub["turnover"] = sub["lp"] * sub["tick_vol"]
    
    # Directional volume (buyer vs seller approximation)
    price_diff = sub["lp"].diff().fillna(0)
    is_buy = price_diff >= 0
    sub["buy_vol"] = np.where(is_buy, sub["tick_vol"], 0.0)
    sub["sell_vol"] = np.where(~is_buy, sub["tick_vol"], 0.0)

    # Resample
    ohlc = sub["lp"].resample(freq, label="left", closed="left").ohlc()
    vols = sub["tick_vol"].resample(freq, label="left", closed="left").sum()
    turnovers = sub["turnover"].resample(freq, label="left", closed="left").sum()
    buy_vols = sub["buy_vol"].resample(freq, label="left", closed="left").sum()
    sell_vols = sub["sell_vol"].resample(freq, label="left", closed="left").sum()
    tick_counts = sub["lp"].resample(freq, label="left", closed="left").count()

    res = ohlc.copy()
    res["volume"] = vols
    res["turnover"] = turnovers
    res["vwap"] = np.where(res["volume"] > 0, res["turnover"] / res["volume"], res["close"])
    res["buy_volume"] = buy_vols
    res["sell_volume"] = sell_vols
    res["delta"] = buy_vols - sell_vols
    res["ticks"] = tick_counts

    # Filter out intervals with 0 ticks / empty bars
    res = res[res["ticks"] > 0].copy()
    res.reset_index(inplace=True)
    res["symbol"] = symbol
    res["trade_date"] = date
    return res

def build_canonical_dataset(raw_dir: str, output_base_dir: str):
    t0 = time.time()
    files = sorted(glob.glob(os.path.join(raw_dir, "**", "*.parquet"), recursive=True))
    if not files:
        print(f"[error] No raw parquet files found in {raw_dir}")
        sys.exit(1)

    print(f"[build] Found {len(files)} raw L2 parquet files. Loading...")
    frames = []
    for fp in files:
        try:
            df = pd.read_parquet(fp, engine="pyarrow")
            if "tok" in df.columns:
                df = df.rename(columns={"tok": "token", "ts": "exch_ts"})
            if all(c in df.columns for c in REQUIRED_COLS):
                frames.append(df[REQUIRED_COLS])
        except Exception:
            continue

    if not frames:
        print("[error] Could not load valid parquet frames.")
        sys.exit(1)

    full = pd.concat(frames, ignore_index=True)
    del frames
    print(f"[build] Loaded {len(full):,} ticks into memory. Parsing timestamps...")

    full["token"] = full["token"].astype(str)
    full["exch_ts"] = full["exch_ts"].astype(np.float64)
    if full["exch_ts"].median() > 1e11:
        full["exch_ts"] = full["exch_ts"] / 1000.0

    full["lp"] = full["lp"].astype(np.float64)
    full["vol"] = full["vol"].astype(np.float64)

    ist = pd.to_datetime(full["exch_ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    full["dt"] = ist
    full["trade_date"] = ist.dt.date.astype(str)

    sym_map = load_token_symbol_map(raw_dir)
    full["symbol"] = full["token"].map(lambda t: sym_map.get(t, t))

    symbols = sorted(full["symbol"].unique())
    dates = sorted(full["trade_date"].unique())
    print(f"[build] Processing {len(symbols)} symbols across {len(dates)} dates: {dates}")

    timeframes = {
        "5m": "5min",
        "15m": "15min",
        "1h": "1h"
    }

    groups = full.groupby(["symbol", "trade_date"], sort=False)
    
    for tf_name, freq in timeframes.items():
        print(f"\n[resample] Generating canonical {tf_name} dataset...")
        tf_frames = []
        for (symbol, date), sub in groups:
            if len(sub) < 5:
                continue
            df_bar = process_symbol_date_group(symbol, date, sub, freq)
            if not df_bar.empty:
                tf_frames.append(df_bar)

        if tf_frames:
            tf_full = pd.concat(tf_frames, ignore_index=True)
            tf_full = tf_full.sort_values(["symbol", "dt"]).reset_index(drop=True)
            
            tf_outdir = os.path.join(output_base_dir, tf_name)
            os.makedirs(tf_outdir, exist_ok=True)
            
            # Save single consolidated file
            main_path = os.path.join(tf_outdir, f"canonical_{tf_name}_all_symbols.parquet")
            tf_full.to_parquet(main_path, engine="pyarrow", index=False)
            
            print(f"[saved] ✅ Canonical {tf_name} saved: {main_path}")
            print(f"        Total Bars: {len(tf_full):,} | Unique Symbols: {tf_full['symbol'].nunique()} | Dates: {tf_full['trade_date'].nunique()}")
            print(f"        Columns: {list(tf_full.columns)}")

    elapsed = time.time() - t0
    print(f"\n[DONE] 🎉 All canonical datasets (5m, 15m, 1h) successfully generated in {elapsed:.2f}s!")

if __name__ == "__main__":
    raw_dir = sys.argv[1] if len(sys.argv) > 1 else "/home/opc/ARGUS/data/l2_raw"
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "/home/opc/data/canonical_candles"
    build_canonical_dataset(raw_dir, out_dir)
