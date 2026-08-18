# -*- coding: utf-8 -*-
"""
L2 Calibration Analysis — Stock Order Flow
===========================================
Run: python calibration_analysis.py
     python calibration_analysis.py --date 2026-08-17 --forward-mins 15 --target-pct 0.52 --hawkes-window 300

WHAT THE PARQUET CAPTURES (every column explained):
─────────────────────────────────────────────────────
  ts          Timestamp in MILLISECONDS since Unix epoch (UTC).
              Convert to IST: pd.to_datetime(ts, unit='ms', utc=True).tz_convert('Asia/Kolkata')

  tok         Shoonya token (integer). Maps to stock symbol via universe.json.

  lp          *** LAST TRADED PRICE (LTP) ***
              The price of the most recent actual transaction.
              This is exactly what you see moving on TradingView.

  vol         Cumulative day volume in SHARES (not rupees).
              Per-tick shares traded = np.diff(vol).clip(0)
              Per-tick value in Rs   = diff(vol) * lp

  bp1..bp5    5 best BID price levels  (bp1 = best/tightest bid)
  bq1..bq5    Quantities (shares) available at each bid level
  sp1..sp5    5 best ASK (offer) price levels (sp1 = best ask)
  sq1..sq5    Quantities (shares) available at each ask level

  One row = one market event (a trade OR a book change).
  ~8,000-10,000 rows per stock per full trading day.

HOW SIGNALS CORRELATE WITH LTP MOVEMENT:
─────────────────────────────────────────
  The engine is supposed to detect microstructure stress BEFORE price moves.
  This script answers: "At time T, what were the signal values — and what
  did LTP actually do in the next N minutes from T?"
  That correlation reveals the optimal threshold for each gate pillar.

Dependencies: pandas, numpy, pyarrow (for parquet)
  pip install pandas numpy pyarrow
"""

import os, json, glob, argparse
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import time as dt_time
import warnings
warnings.filterwarnings("ignore")

# ── PATHS ─────────────────────────────────────────────────────────────────────
BASE     = Path(__file__).resolve().parent
DATA_DIR = BASE / "l2_data"
UNI_PATH = DATA_DIR / "universe.json"

MARKET_OPEN  = dt_time(9, 15)
MARKET_CLOSE = dt_time(15, 30)


# ── LOAD UNIVERSE ─────────────────────────────────────────────────────────────
def load_universe():
    with open(UNI_PATH) as f:
        uni = json.load(f)
    tok_to_sym = {}
    sym_to_tok = {}
    for s in uni.get("stocks", []):
        tok_to_sym[int(s["token"])] = s["symbol"]
        sym_to_tok[s["symbol"]]     = int(s["token"])
    return tok_to_sym, sym_to_tok


# ── LOAD PARQUET PARTITIONS ───────────────────────────────────────────────────
def load_day(date_str: str) -> pd.DataFrame:
    folder = DATA_DIR / date_str
    parts  = sorted(folder.glob(f"l2_raw_{date_str}_part_*.parquet"))
    if not parts:
        raise FileNotFoundError(f"No parquet files in {folder}")

    dfs = [pd.read_parquet(p) for p in parts]
    df  = pd.concat(dfs, ignore_index=True).sort_values("ts").reset_index(drop=True)

    # Convert timestamp and filter to market hours only
    df["dt"] = (pd.to_datetime(df["ts"], unit="ms", utc=True)
                  .dt.tz_convert("Asia/Kolkata"))
    t = df["dt"].dt.time
    df = df[(t >= MARKET_OPEN) & (t <= MARKET_CLOSE)].reset_index(drop=True)

    print(f"  Loaded {len(df):,} rows from {len(parts)} partitions "
          f"| Unique tokens: {df['tok'].nunique()}")
    return df


# ── SIGNAL COMPUTATION ────────────────────────────────────────────────────────
def compute_signals(df: pd.DataFrame, hawkes_window: int = 300) -> pd.DataFrame:
    """
    Adds microstructure signal columns to a single-stock DataFrame.

    Output columns:
      mid         : Midprice = (bp1 + sp1) / 2
      spread_bps  : Bid-ask spread in basis points
      oib         : Order Imbalance [-1,+1]. Positive = more bid-side value.
                    Formula: (sum(bp*bq) - sum(sp*sq)) / (sum(bp*bq) + sum(sp*sq))
      tval        : Trade value of THIS tick in Rs (dvol * lp)
      whale_z     : Z-score of tval vs rolling 10-min session mean/std.
                    High whale_z = unusually large single trade just printed.
      branching   : Hawkes branching ratio over `hawkes_window` seconds.
                    Formula: max(0, 1 - sqrt(mean/variance)) over 10-sec bins.
                    ~0 = trades arrive uniformly (no momentum clustering).
                    ~1 = trades arrive in self-exciting bursts (momentum).
      lp_mom_5m   : % LTP change vs 5 minutes ago (signed)
      lp_mom_10m  : % LTP change vs 10 minutes ago (signed)
    """
    lp  = df["lp"].values.astype(float)
    vol = df["vol"].values.astype(float)
    bp  = df[["bp1","bp2","bp3","bp4","bp5"]].values.astype(float)
    bq  = df[["bq1","bq2","bq3","bq4","bq5"]].values.astype(float)
    sp  = df[["sp1","sp2","sp3","sp4","sp5"]].values.astype(float)
    sq  = df[["sq1","sq2","sq3","sq4","sq5"]].values.astype(float)
    ts  = df["ts"].values / 1000.0   # seconds

    # Mid & spread
    mid        = (bp[:,0] + sp[:,0]) / 2.0
    spread_bps = np.where(mid > 0, (sp[:,0] - bp[:,0]) / mid * 10000, 0.0)

    # OIB
    b_val = np.sum(bp * bq, axis=1)
    s_val = np.sum(sp * sq, axis=1)
    oib   = (b_val - s_val) / np.maximum(b_val + s_val, 1.0)

    # Per-tick trade value
    dvol = np.diff(vol, prepend=vol[0])
    dvol = np.maximum(dvol, 0.0)
    tval = dvol * lp

    # WhaleZ — rolling 10-min
    whale_z    = np.zeros(len(df))
    roll_win   = 600   # always 10-min for whale, independent of hawkes_window
    for i in range(len(df)):
        mask = (ts >= ts[i] - roll_win) & (ts <= ts[i])
        vals = tval[mask]
        if len(vals) >= 5 and vals.std() > 0:
            whale_z[i] = (tval[i] - vals.mean()) / vals.std()

    # Hawkes branching ratio
    bin_sec   = 10
    bins      = hawkes_window // bin_sec
    branching = np.zeros(len(df))
    trade_ts  = ts[tval > 0]
    for i in range(len(df)):
        cutoff = ts[i] - hawkes_window
        recent = trade_ts[(trade_ts >= cutoff) & (trade_ts <= ts[i])]
        if len(recent) >= 10:
            counts = np.zeros(bins)
            for t in recent:
                bi = int((t - cutoff) // bin_sec)
                if 0 <= bi < bins:
                    counts[bi] += 1
            mc, vc = counts.mean(), counts.var()
            if vc > 0 and mc > 0:
                branching[i] = max(0.0, 1.0 - np.sqrt(mc / vc))

    # Price momentum (lookback)
    def mom(sec):
        out = np.zeros(len(df))
        for i in range(len(df)):
            j = np.searchsorted(ts, ts[i] - sec)
            if j < i and lp[j] > 0:
                out[i] = (lp[i] - lp[j]) / lp[j] * 100
        return out

    out = df.copy()
    out["mid"]        = mid
    out["spread_bps"] = spread_bps
    out["oib"]        = oib
    out["tval"]       = tval
    out["whale_z"]    = whale_z
    out["branching"]  = branching
    out["lp_mom_5m"]  = mom(300)
    out["lp_mom_10m"] = mom(600)
    return out


# ── FORWARD RETURNS ───────────────────────────────────────────────────────────
def add_forward_returns(df: pd.DataFrame, forward_mins: int = 15,
                        target_pct: float = 0.52) -> pd.DataFrame:
    """
    For every tick adds:
      fwd_ret      : Actual % LTP change N minutes later (signed)
      fwd_max_up   : Max upside % the price reached in next N minutes
      fwd_max_dn   : Max downside % the price fell in next N minutes
      move_up      : bool — price went up >= target in next N minutes
      move_dn      : bool — price went down >= target in next N minutes
      move_any     : bool — any direction move >= target
    """
    lp      = df["lp"].values.astype(float)
    ts      = df["ts"].values / 1000.0
    fwd_sec = forward_mins * 60

    fwd_ret    = np.zeros(len(df))
    fwd_max_up = np.zeros(len(df))
    fwd_max_dn = np.zeros(len(df))

    for i in range(len(df)):
        if lp[i] <= 0:
            continue
        mask = (ts > ts[i]) & (ts <= ts[i] + fwd_sec)
        if mask.any():
            fut            = lp[mask]
            fwd_ret[i]     = (fut[-1] - lp[i]) / lp[i] * 100
            fwd_max_up[i]  = (fut.max() - lp[i]) / lp[i] * 100
            fwd_max_dn[i]  = (lp[i] - fut.min()) / lp[i] * 100

    df = df.copy()
    df["fwd_ret"]    = fwd_ret
    df["fwd_max_up"] = fwd_max_up
    df["fwd_max_dn"] = fwd_max_dn
    df["move_up"]    = fwd_max_up >= target_pct
    df["move_dn"]    = fwd_max_dn >= target_pct
    df["move_any"]   = df["move_up"] | df["move_dn"]
    return df


# ── THRESHOLD SWEEP ───────────────────────────────────────────────────────────
def sweep_thresholds(df: pd.DataFrame, symbol: str, target_pct: float = 0.52):
    """
    Sweep branching and whale_z thresholds. For each combo compute:
      Precision, Recall, F1, capture rate vs actual moves.
    Returns the best combo and the full results DataFrame.
    """
    df = df[df["fwd_ret"] != 0].copy()   # drop rows without forward data
    if len(df) < 50:
        return None, None

    n_actual = df["move_any"].sum()
    if n_actual == 0:
        print(f"    {symbol}: no moves >= {target_pct}% today — skipping sweep")
        return None, None

    branch_grid = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45]
    whale_grid  = [0.5,  1.0,  1.2,  1.5,  2.0,  2.5,  3.0]

    rows = []
    for bt in branch_grid:
        for wt in whale_grid:
            fired     = (df["branching"] >= bt) & (df["whale_z"] >= wt)
            tp        = int((fired &  df["move_any"]).sum())
            fp        = int((fired & ~df["move_any"]).sum())
            fn        = int((~fired &  df["move_any"]).sum())
            precision = tp / max(tp + fp, 1)
            recall    = tp / max(tp + fn, 1)
            f1        = 2 * precision * recall / max(precision + recall, 1e-9)
            rows.append(dict(
                symbol=symbol, branch=bt, whale=wt,
                tp=tp, fp=fp, fn=fn,
                precision=round(precision, 3),
                recall=round(recall, 3),
                f1=round(f1, 3),
                capture=round(tp / max(n_actual, 1), 3),
                n_signals=int(fired.sum()),
                n_actual=int(n_actual),
            ))

    res  = pd.DataFrame(rows).sort_values("f1", ascending=False)
    best = res.iloc[0]

    # Current engine performance
    curr = res[(res["branch"] == 0.35) & (res["whale"] == 1.20)]
    curr_f1 = float(curr["f1"].values[0]) if len(curr) else 0.0

    summary = dict(
        symbol          = symbol,
        n_ticks         = len(df),
        n_actual_moves  = int(n_actual),
        curr_branch     = 0.35,
        curr_whale      = 1.20,
        curr_f1         = round(curr_f1, 3),
        curr_capture    = round(float(curr["capture"].values[0]) if len(curr) else 0, 3),
        opt_branch      = float(best["branch"]),
        opt_whale       = float(best["whale"]),
        opt_f1          = float(best["f1"]),
        opt_precision   = float(best["precision"]),
        opt_recall      = float(best["recall"]),
        opt_capture     = float(best["capture"]),
        opt_signals     = int(best["n_signals"]),
        f1_improvement  = round(float(best["f1"]) - curr_f1, 3),
    )
    return summary, res


# ── MOVE TIMELINE ─────────────────────────────────────────────────────────────
def print_move_timeline(df: pd.DataFrame, symbol: str, target_pct: float = 0.52):
    """Show each actual move window with what the signals looked like."""
    moves = df[df["move_any"]].copy().sort_values("ts")
    if moves.empty:
        print(f"  {symbol:<14}: no moves >= {target_pct}% today")
        return

    # Cluster: group moves within 15-min of each other
    clusters, last_ts = [], -99999
    for _, row in moves.iterrows():
        if row["ts"]/1000 - last_ts > 900:
            clusters.append(row)
            last_ts = row["ts"] / 1000

    for row in clusters:
        g_curr    = (row["branching"] >= 0.35) and (row["whale_z"] >= 1.20)
        g_relaxed = (row["branching"] >= 0.20) and (row["whale_z"] >= 1.20)
        verdict   = ("[FIRES-NOW]" if g_curr else
                     "[FIRES-RELAXED]" if g_relaxed else "[MISSED]")
        direction = "UP" if row["move_up"] else "DN"
        print(f"  {symbol:<14} {row['dt'].strftime('%H:%M')} "
              f"LTP={row['lp']:.2f}  dir={direction}  "
              f"fwd={row['fwd_ret']:+.2f}%  "
              f"branch={row['branching']:.3f}  "
              f"whale={row['whale_z']:.2f}  "
              f"oib={row['oib']:+.3f}  "
              f"mom5m={row['lp_mom_5m']:+.2f}%  "
              f"-> {verdict}")


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date",          default="2026-08-17")
    parser.add_argument("--forward-mins",  default=15,   type=int)
    parser.add_argument("--target-pct",    default=0.52, type=float)
    parser.add_argument("--hawkes-window", default=300,  type=int,
                        help="Hawkes lookback seconds. Engine uses 600. Try 300 per calibration note.")
    args = parser.parse_args()

    print(f"\n{'='*72}")
    print(f"  L2 CALIBRATION ANALYSIS")
    print(f"  Date           : {args.date}")
    print(f"  Forward window : {args.forward_mins} min")
    print(f"  Target move    : {args.target_pct}%")
    print(f"  Hawkes window  : {args.hawkes_window}s  (live engine uses 600s)")
    print(f"{'='*72}\n")

    tok_to_sym, sym_to_tok = load_universe()

    print(f"Loading parquet for {args.date} ...")
    all_df = load_day(args.date)

    all_summaries = []
    all_stock_dfs = {}

    print(f"\nComputing signals per stock (this takes ~2-3 min for 15 stocks)...\n")
    for symbol, token in sorted(sym_to_tok.items()):
        df_s = all_df[all_df["tok"] == token].copy().reset_index(drop=True)
        if len(df_s) < 100:
            print(f"  {symbol:<14}: only {len(df_s)} ticks — skipping")
            continue

        print(f"  {symbol:<14}: {len(df_s):>6,} ticks ...", end="", flush=True)
        df_s = compute_signals(df_s, hawkes_window=args.hawkes_window)
        df_s = add_forward_returns(df_s, args.forward_mins, args.target_pct)
        all_stock_dfs[symbol] = df_s

        summary, _ = sweep_thresholds(df_s, symbol, args.target_pct)
        if summary:
            all_summaries.append(summary)
            print(f" moves={summary['n_actual_moves']}  "
                  f"curr_F1={summary['curr_f1']:.3f}  "
                  f"opt_F1={summary['opt_f1']:.3f}  "
                  f"opt_branch={summary['opt_branch']:.2f}")
        else:
            print(" (no moves)")

    # ── When did moves happen and what did signals show? ────────────────────
    print(f"\n{'='*72}")
    print(f"  MOVE TIMELINE — LTP moves >= {args.target_pct}% and signal state at entry")
    print(f"{'='*72}")
    for symbol, df_s in all_stock_dfs.items():
        print_move_timeline(df_s, symbol, args.target_pct)

    # ── Cross-stock calibration table ────────────────────────────────────────
    if not all_summaries:
        print("\nNo moves found today — cannot calibrate.")
        return

    sum_df = pd.DataFrame(all_summaries).sort_values("f1_improvement", ascending=False)

    print(f"\n{'='*72}")
    print(f"  CALIBRATION TABLE -- Sorted by F1 improvement opportunity")
    print(f"{'='*72}")
    print(f"  {'Symbol':<14} {'Moves':>5} {'CurrF1':>7} {'OptBranch':>10} "
          f"{'OptWhale':>9} {'OptF1':>7} {'Capture':>8} {'F1Gain':>8}")
    print("  " + "-"*72)
    for _, r in sum_df.iterrows():
        flag = " <- CALIBRATE" if r["f1_improvement"] > 0.05 else ""
        print(f"  {r['symbol']:<14} {r['n_actual_moves']:>5} "
              f"{r['curr_f1']:>7.3f} {r['opt_branch']:>10.2f} "
              f"{r['opt_whale']:>9.2f} {r['opt_f1']:>7.3f} "
              f"{r['opt_capture']*100:>7.0f}% {r['f1_improvement']:>+8.3f}{flag}")

    # -- Aggregate recommendation ----------------------------------------------
    print(f"\n{'='*72}")
    print(f"  AGGREGATE RECOMMENDATION")
    print(f"{'='*72}")
    med_branch = sum_df["opt_branch"].median()
    med_whale  = sum_df["opt_whale"].median()
    avg_curr   = sum_df["curr_f1"].mean()
    avg_opt    = sum_df["opt_f1"].mean()
    n_improve  = (sum_df["f1_improvement"] > 0.05).sum()

    print(f"  Current thresholds   : branching >= 0.35  |  whale_z >= 1.20")
    print(f"  Suggested thresholds : branching >= {med_branch:.2f}  |  whale_z >= {med_whale:.2f}")
    print(f"  Avg F1 current       : {avg_curr:.3f}")
    print(f"  Avg F1 optimal       : {avg_opt:.3f}  (+{avg_opt-avg_curr:.3f})")
    print(f"  Stocks needing change: {n_improve} / {len(sum_df)}")

    print(f"\n  Per-stock optimal thresholds (for tomorrow's calibration):")
    for _, r in sum_df.sort_values("opt_f1", ascending=False).iterrows():
        print(f"    {r['symbol']:<14}: branch>={r['opt_branch']:.2f}  "
              f"whale>={r['opt_whale']:.2f}  "
              f"F1={r['opt_f1']:.3f}  "
              f"capture={r['opt_capture']*100:.0f}%  "
              f"signals/day={r['opt_signals']}")

    # ── Save JSON ─────────────────────────────────────────────────────────────
    out_path = BASE / f"calibration_results_{args.date}_hw{args.hawkes_window}s.json"
    with open(out_path, "w") as f:
        json.dump(all_summaries, f, indent=2, default=str)
    print(f"\n  Results saved -> {out_path.name}")
    print(f"\n  TIP: Run again with --hawkes-window 600 to compare 5min vs 10min Hawkes.")
    print(f"  TIP: Run with --forward-mins 10 to see tighter target windows.\n")


if __name__ == "__main__":
    main()

