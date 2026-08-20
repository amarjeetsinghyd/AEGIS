"""
backtest.py — STRATOS Pure Turnover Bucket Backtest Runner.
Replays raw L2 parquet files through Information-Time Turnover Buckets:
- Turnover Bucket Aggregator (Rs. 20,000,000 / Rs. 2 Cr per bucket)
- Turnover-Wave Market Structure (HH2 Breakout Support Detector)
- Pullback Volume Contraction Validator
- Order-Flow Positive Delta & Absorption Trigger
- Anti-Liquidity Hunting Stop Loss (0.15% below lowest pullback wick)
- Session-Adaptive Trailing Engine
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime, timezone, time as dt_time
import numpy as np
import pandas as pd

from STRATOS.config import STRATOS_CONFIG
from STRATOS.turnover_bucket_engine import TurnoverBucketEngine
from STRATOS.structure_engine import MarketStructureEngine
from STRATOS.pullback_analyzer import PullbackAnalyzer
from STRATOS.flip_trigger import FlipTriggerEngine
from STRATOS.anti_hunt_risk import AntiHuntRiskEngine
from STRATOS.trade_manager import ActiveTradeManager

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
                print(f"[backtest] Loaded token->symbol map: {path} ({len(m)} symbols)")
                return m
            except Exception:
                pass
    return {}

def run_backtest(data_dir: str, outdir: str = "./stratos_backtest_out", bucket_size: float = 20_000_000.0):
    files = sorted(glob.glob(os.path.join(data_dir, "**", "*.parquet"), recursive=True))
    if not files:
        print(f"[error] No *.parquet files found under {data_dir}")
        sys.exit(1)

    print(f"[backtest] Found {len(files)} parquet files. Loading...")
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

    full["token"] = full["token"].astype(str)
    full["exch_ts"] = full["exch_ts"].astype(np.float64)
    if full["exch_ts"].median() > 1e11:
        full["exch_ts"] = full["exch_ts"] / 1000.0

    full["lp"] = full["lp"].astype(np.float64)
    full["vol"] = full["vol"].astype(np.float64)

    ist = pd.to_datetime(full["exch_ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    full["trade_date"] = ist.dt.date.astype(str)
    full["time_of_day"] = ist.dt.time

    sym_map = load_token_symbol_map(data_dir)
    full["symbol"] = full["token"].map(lambda t: sym_map.get(t, t))

    full = full.sort_values(["symbol", "exch_ts"], kind="mergesort").reset_index(drop=True)
    symbols = sorted(full["symbol"].unique())
    print(f"[backtest] Replaying {len(full):,} ticks across {len(symbols)} symbols and {full['trade_date'].nunique()} day(s)...")

    cfg = dict(STRATOS_CONFIG)
    cfg["turnover_bucket_inr"] = bucket_size

    bucket_engine = TurnoverBucketEngine(symbols, default_turnover_inr=bucket_size, max_history=200)
    structure = MarketStructureEngine(cfg)
    pullback = PullbackAnalyzer(cfg)
    trigger = FlipTriggerEngine(cfg)
    risk = AntiHuntRiskEngine(cfg)
    trades_mgr = ActiveTradeManager(cfg)

    all_trades = []
    capital = 50000.0

    groups = full.groupby(["symbol", "trade_date"], sort=False)

    for gi, ((symbol, date), sub) in enumerate(groups):
        ts_arr = sub["exch_ts"].to_numpy()
        lp_arr = sub["lp"].to_numpy()
        vol_arr = sub["vol"].to_numpy()
        tod_arr = sub["time_of_day"].to_numpy()
        n_ticks = len(ts_arr)

        for i in range(n_ticks):
            t = ts_arr[i]
            p = lp_arr[i]
            v = vol_arr[i]
            tod = tod_arr[i]

            if p <= 0:
                continue

            # 1. Update Turnover Bucket Accumulator
            bucket = bucket_engine.on_tick(symbol, t, p, v, is_buy=True)

            # 2. Check Exits on tick
            if symbol in trades_mgr.open_trades:
                arrays = bucket_engine.get_arrays(symbol)
                exit_res = trades_mgr.evaluate_exits(symbol, p, arrays, tod)
                if exit_res:
                    closed = trades_mgr.close_trade(symbol, exit_res)
                    if closed:
                        all_trades.append(closed)
                continue

            # 3. Only evaluate entry when a Turnover Bucket completes
            if not bucket:
                continue

            # Time Gate: Only trade between 09:20 and 14:30 IST
            if tod < cfg.get("entry_start_time", dt_time(9, 20)) or tod > cfg.get("entry_cutoff_time", dt_time(14, 30)):
                continue

            if trades_mgr.get_open_count() >= cfg["max_open_positions"]:
                continue
            if trades_mgr.get_today_trades_count() >= cfg["max_trades_per_day"]:
                continue

            arrays = bucket_engine.get_arrays(symbol)

            # Macro Trend
            macro = structure.check_macro_trend(arrays)
            if macro["trend"] != "BULLISH":
                continue

            # Structure Swings
            struct = structure.update_structure(symbol, arrays)
            if struct["status"] != "AWAITING_RETEST":
                continue

            support_line = struct["support_level"]
            bk_vol = structure.breakout_bucket.get(symbol, {}).get("volume", 1000.0)

            # Pullback Volume Contraction
            pb = pullback.analyze_pullback_quality(arrays, support_line, bk_vol)
            if not pb["valid"]:
                continue

            # Order Flow Trigger
            trig = trigger.evaluate_trigger(arrays, support_line)
            if not trig["triggered"]:
                continue

            # Risk Sizing
            levels = risk.calculate_trade_levels(p, support_line, pb["lowest_wick"], arrays)
            qty = risk.calculate_position_size(capital, p, levels["risk_dist"])
            if qty <= 0:
                continue

            trade_entry = {
                "symbol": symbol, "trade_date": date, "entry_ts": t,
                "entry_time_ist": str(tod), "entry_price": p, "qty": qty,
                "stop_loss": levels["stop_loss"], "tp1_price": levels["tp1_price"],
                "tp2_price": levels["tp2_price"], "risk_dist": levels["risk_dist"],
                "risk_pct": levels["risk_pct"], "support_level": support_line,
                "pattern": trig["pattern"], "delta": trig["delta"],
                "highest_price": p
            }
            trades_mgr.register_trade(symbol, trade_entry)

    print("\n" + "=" * 90)
    print(f"STRATOS TURNOVER BUCKET BACKTEST RESULTS ({len(all_trades)} Trades Executed)")
    print("=" * 90)

    if not all_trades:
        print("[warn] Zero trades executed during backtest. Market was either in chop or criteria unfulfilled.")
        return

    tdf = pd.DataFrame(all_trades)
    wins = tdf[tdf["pnl"] > 0]
    losses = tdf[tdf["pnl"] <= 0]

    win_rate = (len(wins) / len(tdf)) * 100.0
    tot_pnl = tdf["pnl"].sum()
    tot_gain = wins["pnl"].sum()
    tot_loss = abs(losses["pnl"].sum())
    profit_factor = (tot_gain / tot_loss) if tot_loss > 0 else 999.0
    avg_win = wins["pnl_pct"].mean() if len(wins) > 0 else 0.0
    avg_loss = losses["pnl_pct"].mean() if len(losses) > 0 else 0.0

    print(f"Total Trades       : {len(tdf)}")
    print(f"Winning Trades     : {len(wins)} ({win_rate:.1f}% Win Rate)")
    print(f"Losing Trades      : {len(losses)} ({100.0 - win_rate:.1f}% Loss Rate)")
    print(f"Profit Factor      : {profit_factor:.2f}")
    print(f"Total Net PnL (Rs) : Rs. {tot_pnl:,.2f}")
    print(f"Average Win (%)    : +{avg_win:.2f}%")
    print(f"Average Loss (%)   : {avg_loss:.2f}%")

    print("\n--- INDIVIDUAL TRADE LOG ---")
    log_cols = ["symbol", "trade_date", "entry_time_ist", "pattern", "entry_price", "exit_price", "pnl_pct", "reason"]
    avail_cols = [c for c in log_cols if c in tdf.columns]
    print(tdf[avail_cols].to_string())

    if outdir:
        os.makedirs(outdir, exist_ok=True)
        csv_path = os.path.join(outdir, "stratos_bucket_backtest_trades.csv")
        tdf.to_csv(csv_path, index=False)
        print(f"\n[saved] Full trade log exported -> {csv_path}")

def main():
    ap = argparse.ArgumentParser(description="STRATOS Pure Turnover Bucket Backtester")
    ap.add_argument("--data-dir", default="/home/opc/ARGUS/data/l2_raw", help="Path to raw L2 parquet files")
    ap.add_argument("--outdir", default="./stratos_backtest_out", help="Output directory for CSV log")
    ap.add_argument("--bucket-size", type=float, default=20000000.0, help="Turnover Bucket Size in INR")
    args = ap.parse_args()
    run_backtest(args.data_dir, args.outdir, args.bucket_size)

if __name__ == "__main__":
    main()
