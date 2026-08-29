# -*- coding: utf-8 -*-
"""
AEGIS — LOB Quantum Lab (Colab GPU)
====================================

Self-supervised LOB Masked Autoencoder (Idea B) + Hawkes Excitation Skeleton
(Idea D) for institutional microstructure signal discovery on NSE option L2
depth data.

Designed to run end-to-end on Google Colab (T4/A100). The script is a single
self-contained file but is structured with "# %%" cell markers so it can be
split into Jupyter / Colab cells.

This file is RESEARCH-GRADE OFF-HOT-PATH code. It is not loaded by the live
O(1) engine. The only products exported from this lab that touch the live
engine are:
    - linear / tiny-GRU weights (JSON / .npz)
    - scaler statistics
    - a Hawkes branching matrix summary

Author: AEGIS Research (Amarjeet Singh / Quant-Bot)
"""

# =============================================================================
# CELL 0 — CONFIG, ENVIRONMENT, GPU ASSERTS, SEEDS
# =============================================================================
# %%
from __future__ import annotations

import os
import sys
import json
import math
import time
import hashlib
import warnings
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

import numpy as np
import pandas as pd

# Torch — imported lazily / safely so the script can still run a CPU audit
# pass if CUDA is missing (we will warn but not hard-fail the data audit).
try:
    import torch
    TORCH_OK = True
except Exception as _e:
    torch = None
    TORCH_OK = False

warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

# ------------------------------- Tunables -----------------------------------
@dataclass
class CFG:
    # ---- Paths ----
    DATA_PATH: str = "nifty_l2_merged.parquet"   # or upload to /content/...
    OUT_DIR:   str = "/content/aegis_l2_lab_out" # Colab default; override on VPS
    EXPORT_DIR: str = "/content/aegis_l2_lab_export"

    # ---- Tensorization ----
    WINDOW_TICKS:   int   = 64        # event-clocked window length
    STRIDE:         int   = 8         # stride between consecutive windows
    MAX_WALL_SEC:   float = 45.0      # drop windows spanning more wall time than this
    PRICE_BPS_SCALE: float = 1e4     # bp = (px - mid)/mid * 1e4
    DELTA_CLIP_SIGMA: float = 5.0    # ±5σ clipping for queue_delta
    LOG1P_QTY: bool = True            # log1p on raw quantities before z-score

    # ---- Audit / Forensic ----
    ZERO_DEPTH_DROP: bool = True      # drop rows with all bq1..5/sq1..5 == 0
    DUPLICATE_TS_DROP: bool = True    # drop exact duplicate timestamps per token
    SESSION_GAP_SEC: float = 5.0      # gap >5s flagged in audit (not dropped)

    # ---- Day-boundary policy ----
    # Indian market day boundary = IST 18:30 = UTC 13:00; we use this for day_id.
    IST_OFFSET_HRS: int = 5
    IST_MIN_OFFSET_MIN: int = 30      # IST = UTC + 5:30

    # ---- Train-day normalization ----
    # Stats (mean/std for qty z-score) are computed on the first N_TRAIN_DAYS
    # days only; then frozen and applied to all windows. Prevents leakage.
    N_TRAIN_DAYS: int = 4             # tune per available history

    # ---- Determinism ----
    SEED: int = 20260816

    # ---- Misc ----
    VERBOSE: bool = True


CFG = CFG()
Path(CFG.OUT_DIR).mkdir(parents=True, exist_ok=True)
Path(CFG.EXPORT_DIR).mkdir(parents=True, exist_ok=True)


def _seed_all(seed: int) -> None:
    np.random.seed(seed)
    if TORCH_OK:
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False


_seed_all(CFG.SEED)


def _env_audit() -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "python": sys.version.split()[0],
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "torch": getattr(torch, "__version__", "n/a"),
        "cuda_available": bool(TORCH_OK and torch.cuda.is_available()),
        "cuda_device_count": (
            int(torch.cuda.device_count()) if (TORCH_OK and torch.cuda.is_available()) else 0
        ),
        "cuda_name": (
            torch.cuda.get_device_name(0)
            if (TORCH_OK and torch.cuda.is_available())
            else "cpu-only"
        ),
    }
    return info


ENV = _env_audit()

if CFG.VERBOSE:
    print("=" * 72)
    print("AEGIS LOB Quantum Lab — Block 1 (Cells 0–2)")
    print("=" * 72)
    for k, v in ENV.items():
        print(f"  {k:>18}: {v}")
    print(f"  {'config_window':>18}: W={CFG.WINDOW_TICKS} S={CFG.STRIDE} "
          f"max_wall={CFG.MAX_WALL_SEC}s")
    print(f"  {'data_path':>18}: {CFG.DATA_PATH}")
    print("=" * 72)

# Hard warn (not fail) if CUDA absent — research script must remain useful.
if not ENV["cuda_available"]:
    print("⚠️  CUDA not available — proceeding on CPU. "
          "Pretraining will be slow; switch to a Colab GPU runtime.")


# =============================================================================
# CELL 1 — FAST PARQUET INGESTION, SCHEMA VALIDATION, FORENSIC AUDIT, DAY_ID
# =============================================================================
# %%
# ---- Expected schema (frozen; parquet write side must match) ----
EXPECTED_COLUMNS: List[str] = [
    "ts", "tok", "sym", "kind", "strike", "lp", "vol", "oi",
    "bp1", "bp2", "bp3", "bp4", "bp5",
    "bq1", "bq2", "bq3", "bq4", "bq5",
    "sp1", "sp2", "sp3", "sp4", "sp5",
    "sq1", "sq2", "sq3", "sq4", "sq5",
]
DTYPE_MAP: Dict[str, Any] = {
    "ts":     "int64",
    "tok":    "int64",
    "sym":    "string",
    "kind":   "int8",
    "strike": "float32",
    "lp":     "float32",
    "vol":    "float32",
    "oi":     "float32",
    "bp1": "float32", "bp2": "float32", "bp3": "float32",
    "bp4": "float32", "bp5": "float32",
    "bq1": "int32",   "bq2": "int32",   "bq3": "int32",
    "bq4": "int32",   "bq5": "int32",
    "sp1": "float32", "sp2": "float32", "sp3": "float32",
    "sp4": "float32", "sp5": "float32",
    "sq1": "int32",   "sq2": "int32",   "sq3": "int32",
    "sq4": "int32",   "sq5": "int32",
}


def load_l2_parquet(path: str) -> pd.DataFrame:
    """
    Load the pre-cleaned NSE L2 parquet with strict schema validation.

    Returns a *single* DataFrame sorted by (ts, tok). All option tokens AND
    the underlying index token live together; downstream code filters by
    `kind` and `sym`.
    """
    p = Path(path)
    if not p.exists():
        # Convenience: try the conventional /content/ location
        alt = Path("/content") / path
        if alt.exists():
            p = alt
        else:
            raise FileNotFoundError(
                f"Parquet not found at '{path}'. Upload to Colab or set CFG.DATA_PATH."
            )

    # pyarrow engine preferred for schema fidelity; fall back to fastparquet.
    try:
        df = pd.read_parquet(p, engine="pyarrow")
    except Exception:
        df = pd.read_parquet(p, engine="fastparquet")

    # ---- Schema validation ----
    missing = [c for c in EXPECTED_COLUMNS if c not in df.columns]
    extra   = [c for c in df.columns if c not in EXPECTED_COLUMNS]
    if missing:
        raise ValueError(f"Parquet missing required columns: {missing}")
    if extra:
        print(f"⚠️  Parquet has extra columns (ignored): {extra}")

    # ---- Cast dtypes to canonical ----
    for col, dt in DTYPE_MAP.items():
        if col in df.columns:
            try:
                df[col] = df[col].astype(dt, copy=False)
            except Exception as e:
                print(f"⚠️  could not cast {col} → {dt}: {e}")

    # ---- Sort & drop exact duplicate timestamps per token ----
    df = df.sort_values(["ts", "tok"], kind="mergesort").reset_index(drop=True)
    if CFG.DUPLICATE_TS_DROP:
        before = len(df)
        df = df.drop_duplicates(subset=["ts", "tok"], keep="last").reset_index(drop=True)
        if len(df) != before:
            print(f"  dropped exact duplicate (ts,tok): {before - len(df):,}")

    # ---- Drop zero-depth junk rows ----
    if CFG.ZERO_DEPTH_DROP:
        qty_cols = [f"{s}{i}" for s in ("bq", "sq") for i in range(1, 6)]
        nonzero_mask = (df[qty_cols].sum(axis=1) > 0)
        before = len(df)
        df = df[nonzero_mask].reset_index(drop=True)
        if len(df) != before:
            print(f"  dropped zero-depth rows: {before - len(df):,}")

    return df


def add_day_id(df: pd.DataFrame, ist_hrs: int = 5, ist_min: int = 30) -> pd.Series:
    """
    Convert Unix-ms `ts` to an IST-naive 'day_id' (YYYYMMDD integer).

    Indian market session day boundary = 00:00 IST = 18:30 UTC of the prior
    calendar date, but we use the simpler convention: IST date-of-day derived
    from `ts` by adding IST offset. This is acceptable for *walk-forward
    splits* because we never trade across midnight during the lab window.
    """
    ts = pd.to_datetime(df["ts"].values, unit="ms", utc=True)
    ts_ist = ts + pd.Timedelta(hours=ist_hrs, minutes=ist_min)
    day_id = (ts_ist.dt.year * 10000
              + ts_ist.dt.month * 100
              + ts_ist.dt.day).astype("int32")
    return day_id


def forensic_audit(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Audit pass over the loaded L2 tape. Per options-l2-forensics guidance:
    counts, zero-depth %, gap stats, per-symbol tick counts, day breakdown.

    Returns a flat dict suitable for printing / saving.
    """
    out: Dict[str, Any] = {}

    out["n_rows"]        = int(len(df))
    out["n_tokens"]      = int(df["tok"].nunique())
    out["n_symbols"]     = int(df["sym"].nunique())
    out["ts_min_ms"]     = int(df["ts"].min())
    out["ts_max_ms"]     = int(df["ts"].max())
    out["ts_span_hours"] = (out["ts_max_ms"] - out["ts_min_ms"]) / 3_600_000.0
    out["zero_lp_pct"]   = float((df["lp"] <= 0).mean() * 100.0)

    # Per-symbol counts
    sym_counts = df["sym"].value_counts().to_dict()
    out["per_symbol_counts"] = {str(k): int(v) for k, v in sym_counts.items()}

    # Per-kind counts (0=Index 1=CE 2=PE)
    kind_counts = df["kind"].value_counts().to_dict()
    out["per_kind_counts"] = {str(k): int(v) for k, v in kind_counts.items()}

    # Inter-arrival gaps (per token) — sanity
    gaps = df.groupby("tok")["ts"].diff().dropna()
    out["gap_ms_p50"]   = float(gaps.quantile(0.50)) if len(gaps) else 0.0
    out["gap_ms_p95"]   = float(gaps.quantile(0.95)) if len(gaps) else 0.0
    out["gap_ms_p99"]   = float(gaps.quantile(0.99)) if len(gaps) else 0.0
    out["gap_ms_max"]   = float(gaps.max())       if len(gaps) else 0.0
    out["gaps_over_5s"] = int((gaps > CFG.SESSION_GAP_SEC * 1000.0).sum())

    # Day breakdown
    out["n_days"] = int(df["day_id"].nunique())
    out["per_day_counts"] = (
        df.groupby("day_id").size().to_dict()
    )

    # Hash for audit reproducibility
    sig = hashlib.sha256(
        pd.util.hash_pandas_object(df["ts"].head(50_000), index=False).values.tobytes()
    ).hexdigest()[:16]
    out["audit_signature"] = sig
    return out


# ---- Run Cell 1 ----
print("\n[Cell 1] Loading L2 parquet…")
_t0 = time.time()
DF = load_l2_parquet(CFG.DATA_PATH)
DF["day_id"] = add_day_id(DF, CFG.IST_OFFSET_HRS, CFG.IST_MIN_OFFSET_MIN)
_load_sec = time.time() - _t0

print(f"  loaded {len(DF):,} rows × {len(DF.columns)} cols in {_load_sec:.2f}s "
      f"({DF.memory_usage(deep=True).sum()/1e6:.1f} MB)")

AUDIT = forensic_audit(DF)
print("\n[AUDIT]")
for k, v in AUDIT.items():
    if isinstance(v, dict):
        print(f"  {k}:")
        for kk, vv in v.items():
            print(f"    {kk}: {vv}")
    else:
        print(f"  {k}: {v}")

# Save audit
audit_path = Path(CFG.OUT_DIR) / "01_audit.json"
with open(audit_path, "w") as f:
    json.dump(AUDIT, f, indent=2, default=str)
print(f"  audit → {audit_path}")


# =============================================================================
# CELL 2 — LOB TENSORIZATION (event-clocked W=64, S=8, 10 levels × 3 channels)
# =============================================================================
# %%
# Channel definitions (frozen — exported to JSON so the live engine can match):
#   ch0 = price_offset_bps   : (px - mid)/mid * 1e4
#   ch1 = log1p_qty (z-scored on train days only)
#   ch2 = queue_delta (clipped ±kσ on train days only)
#
# Level layout: index 0..4 = bids L1..L5 (signed negative bps from mid),
#               index 5..9 = asks L1..L5 (signed positive bps from mid).
#
# Output:  X      : float32 array (N_windows, 10, W, 3)
#          META   : structured array (ts_center, ts_start, ts_end,
#                                   tok, sym, strike, kind, day_id, side)
#                   length = N_windows
#
# Stale-window guard: any window whose (ts_end - ts_start) > CFG.MAX_WALL_SEC
# is marked stale=True in META; downstream training/eval can filter.

# Stratified per-symbol window builder — preserves sequential structure
# inside each token (so event-clocking is real, not global shuffling).

def _mid_price(df: pd.DataFrame) -> np.ndarray:
    """Mid = (bp1 + sp1)/2, NaN-safe."""
    bp1 = df["bp1"].to_numpy(dtype=np.float32)
    sp1 = df["sp1"].to_numpy(dtype=np.float32)
    valid = (bp1 > 0) & (sp1 > 0)
    mid = np.where(valid, 0.5 * (bp1 + sp1), np.nan).astype(np.float32)
    return mid


def _build_windows_for_token(
    g: pd.DataFrame,
    W: int,
    S: int,
    max_wall_ms: int,
    price_bps_scale: float,
    delta_clip_sigma: float,
    log1p_qty: bool,
    norm_stats: Optional[Dict[str, np.ndarray]],
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Build all (W, 10, 3) windows for one token group `g` (already sorted by ts).
    Returns (X_windows, mid_zeros_mask, stale_mask) where each entry aligns
    with the *center* tick (last tick) of its window.

    Stats (mu/sd for ch1 and ch2) are pre-computed on TRAIN-DAYS-ONLY rows;
    `norm_stats=None` ⇒ compute from the entire token (only used for
    final-all-days re-normalization in evaluation scripts).
    """
    bp = g[[f"bp{i}" for i in range(1, 6)]].to_numpy(dtype=np.float32)  # (T, 5)
    bq = g[[f"bq{i}" for i in range(1, 6)]].to_numpy(dtype=np.int32)    # (T, 5)
    sp = g[[f"sp{i}" for i in range(1, 6)]].to_numpy(dtype=np.float32)
    sq = g[[f"sq{i}" for i in range(1, 6)]].to_numpy(dtype=np.int32)
    ts = g["ts"].to_numpy(dtype=np.int64)
    lp = g["lp"].to_numpy(dtype=np.float32)
    n  = len(g)

    if n < W:
        return (np.empty((0, W, 10, 3), dtype=np.float32),
                np.empty((0,), dtype=bool),
                np.empty((0,), dtype=bool))

    mid = np.where((bp[:, 0] > 0) & (sp[:, 0] > 0),
                   0.5 * (bp[:, 0] + sp[:, 0]), np.nan).astype(np.float32)

    # Stack levels: bids L1..L5 then asks L1..L5
    px = np.concatenate([bp, sp], axis=1)   # (T, 10)
    qt = np.concatenate([bq.astype(np.float32),
                         sq.astype(np.float32)], axis=1)  # (T, 10)

    # ---- ch0: price bps from mid ----
    # price_offset[i] = (px[i] - mid)/mid * 1e4
    mid_safe = np.where(np.isfinite(mid) & (mid > 0), mid, 1.0)
    bps = (px - mid_safe[:, None]) / mid_safe[:, None] * price_bps_scale  # (T, 10)
    bps = np.where(np.isfinite(px) & (px > 0), bps, 0.0).astype(np.float32)

    # ---- ch1: qty (log1p optional), z-scored using norm_stats ----
    if log1p_qty:
        q_ch = np.log1p(np.clip(qt, 0, None)).astype(np.float32)
    else:
        q_ch = qt.astype(np.float32)

    # ---- ch2: queue delta vs previous tick (event-level) ----
    d_q = np.zeros_like(q_ch)
    d_q[1:] = q_ch[1:] - q_ch[:-1]

    if norm_stats is None:
        # Compute per-token stats — only safe for unlabelled bulk pretraining
        # where we don't need cross-symbol consistency.
        mu_q, sd_q = q_ch.mean(), q_ch.std() + 1e-6
        mu_d, sd_d = d_q.mean(), d_q.std() + 1e-6
    else:
        mu_q = norm_stats["mu_q"]; sd_q = norm_stats["sd_q"]
        mu_d = norm_stats["mu_d"]; sd_d = norm_stats["sd_d"]

    q_ch = ((q_ch - mu_q) / sd_q).astype(np.float32)
    # Delta: z-score, then ±kσ clip
    d_ch = ((d_q - mu_d) / sd_d).astype(np.float32)
    d_ch = np.clip(d_ch, -delta_clip_sigma, delta_clip_sigma).astype(np.float32)

    # Stack channels: (T, 10, 3) — ch axis last so we can broadcast into
    # the final (T, 10, W, 3) tensor via sliding_window_view cleanly.
    ch = np.stack([bps, q_ch, d_ch], axis=-1).astype(np.float32)  # (T, 10, 3)

    # ---- Sliding windows: center-aligned, last tick is the "current" tick ----
    # numpy sliding_window_view over axis=0 with window=W.
    from numpy.lib.stride_tricks import sliding_window_view
    sw = sliding_window_view(ch, window_shape=W, axis=0)  # (T-W+1, 10, 3, W)
    # Reorder so the window axis is axis=2 → (T-W+1, 10, W, 3)
    sw = np.moveaxis(sw, -1, 2).astype(np.float32, copy=False)

    # Stride subsample
    starts = np.arange(0, sw.shape[0], S, dtype=np.int64)
    sw = sw[starts]                                # (N, 10, W, 3)
    win_starts = starts
    win_ends   = starts + (W - 1)

    # ---- Stale-window guard (event-clock vs wall-clock) ----
    # A window is "stale" if its real wall-clock span exceeds the limit
    # OR if any tick inside has zero mid (illiquid artifact).
    win_ts_start = ts[win_starts]
    win_ts_end   = ts[win_ends]
    wall_ms      = (win_ts_end - win_ts_start).astype(np.int64)
    stale_mask   = wall_ms > int(max_wall_ms * 1000.0)

    # Mid-zero mask per window (any tick inside lacks valid mid)
    mid_win = sliding_window_view(mid, window_shape=W, axis=0)[starts]
    mid_zero_mask = ~np.all(np.isfinite(mid_win) & (mid_win > 0), axis=1)

    stale_mask = stale_mask | mid_zero_mask

    # Also mask the *latest tick*'s mid-zero (no fair price anchor)
    last_mid_zero = ~np.isfinite(mid[win_ends]) | (mid[win_ends] <= 0)
    stale_mask = stale_mask | last_mid_zero

    return sw, mid_zero_mask, stale_mask


def compute_train_day_norm_stats(
    df: pd.DataFrame,
    n_train_days: int,
    ist_hrs: int = 5,
    ist_min: int = 30,
    log1p_qty: bool = True,
) -> Tuple[Dict[str, np.ndarray], List[int]]:
    """
    Compute per-channel normalization stats (mu/sd for ch1 + ch2) using ONLY
    rows from the first `n_train_days` IST days. Returns the dict and the
    list of train-day ids (for the downstream purged walk-forward split).
    """
    # day_id should already be in df
    day_ids = sorted(df["day_id"].unique().tolist())
    if len(day_ids) < n_train_days:
        print(f"⚠️  only {len(day_ids)} days available; "
              f"using all of them for norm stats.")
    train_days = day_ids[:max(1, n_train_days)]
    sub = df[df["day_id"].isin(train_days)]

    bp = sub[[f"bp{i}" for i in range(1, 6)]].to_numpy(dtype=np.float32)
    bq = sub[[f"bq{i}" for i in range(1, 6)]].to_numpy(dtype=np.int32)
    sp = sub[[f"sp{i}" for i in range(1, 6)]].to_numpy(dtype=np.float32)
    sq = sub[[f"sq{i}" for i in range(1, 6)]].to_numpy(dtype=np.int32)

    qt = np.concatenate([bq.astype(np.float32),
                         sq.astype(np.float32)], axis=1)
    if log1p_qty:
        q_ch = np.log1p(np.clip(qt, 0, None))
    else:
        q_ch = qt
    d_ch = np.zeros_like(q_ch)
    d_ch[1:] = q_ch[1:] - q_ch[:-1]

    stats = {
        "mu_q": float(q_ch.mean()),
        "sd_q": float(q_ch.std() + 1e-6),
        "mu_d": float(d_ch.mean()),
        "sd_d": float(d_ch.std() + 1e-6),
    }
    return stats, train_days


def build_all_windows(
    df: pd.DataFrame,
    norm_stats: Dict[str, np.ndarray],
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Iterate tokens, build per-token (W, 10, 3) windows, concatenate.
    Returns:
        X          : (N, W, 10, 3) float32
        META_DF    : pandas DataFrame, length N, columns:
                       ts_start, ts_end, ts_center,
                       tok, sym, kind, strike, lp_center, day_id, stale
    """
    pieces_X: List[np.ndarray] = []
    pieces_meta: List[pd.DataFrame] = []

    W = CFG.WINDOW_TICKS
    S = CFG.STRIDE
    max_wall_ms = int(CFG.MAX_WALL_SEC * 1000.0)

    grouped = df.groupby("tok", sort=False)
    t_start = time.time()
    n_tok = df["tok"].nunique()
    for i, (tok, g) in enumerate(grouped, start=1):
        if len(g) < W:
            continue
        g = g.sort_values("ts", kind="mergesort")
        sw, mid_zero, stale = _build_windows_for_token(
            g=g, W=W, S=S, max_wall_ms=max_wall_ms,
            price_bps_scale=CFG.PRICE_BPS_SCALE,
            delta_clip_sigma=CFG.DELTA_CLIP_SIGMA,
            log1p_qty=CFG.LOG1P_QTY,
            norm_stats=norm_stats,
        )
        if sw.shape[0] == 0:
            continue

        ts = g["ts"].to_numpy(dtype=np.int64)
        sym = g["sym"].to_numpy()
        kind = g["kind"].to_numpy(dtype=np.int8)
        strike = g["strike"].to_numpy(dtype=np.float32)
        lp = g["lp"].to_numpy(dtype=np.float32)
        day_id = g["day_id"].to_numpy(dtype=np.int32)

        starts = np.arange(0, sw.shape[0], S, dtype=np.int64)
        ends   = starts + (W - 1)

        meta = pd.DataFrame({
            "tok":       tok,
            "sym":       sym[ends],
            "kind":      kind[ends],
            "strike":    strike[ends],
            "lp_center": lp[ends],
            "ts_start":  ts[starts],
            "ts_end":    ts[ends],
            "ts_center": ts[ends],
            "day_id":    day_id[ends],
            "stale":     stale,
        })
        pieces_X.append(sw)
        pieces_meta.append(meta)

        if CFG.VERBOSE and (i % 50 == 0 or i == n_tok):
            elapsed = time.time() - t_start
            print(f"  [tensorize] {i:>4}/{n_tok} tokens | "
                  f"windows so far: {sum(p.shape[0] for p in pieces_X):,} | "
                  f"{elapsed:.1f}s")

    X = np.concatenate(pieces_X, axis=0).astype(np.float32, copy=False)
    META = pd.concat(pieces_meta, axis=0, ignore_index=True)
    # Reorder: (N, W, 10, 3) → our later code uses (N, 10, W, 3). Transpose once.
    X = np.transpose(X, (0, 2, 1, 3)).copy()  # (N, 10, W, 3)
    return X, META


# ---- Run Cell 2 ----
print("\n[Cell 2] Computing train-day normalization stats…")
NORM_STATS, TRAIN_DAYS = compute_train_day_norm_stats(
    DF, n_train_days=CFG.N_TRAIN_DAYS,
    ist_hrs=CFG.IST_OFFSET_HRS, ist_min=CFG.IST_MIN_OFFSET_MIN,
    log1p_qty=CFG.LOG1P_QTY,
)
print(f"  train_days = {TRAIN_DAYS}")
print(f"  norm_stats = {NORM_STATS}")

print("\n[Cell 2] Building event-clocked LOB windows "
      f"(W={CFG.WINDOW_TICKS}, S={CFG.STRIDE})…")
_t0 = time.time()
X, META = build_all_windows(DF, norm_stats=NORM_STATS)
_build_sec = time.time() - _t0

print(f"\n  windows built: {X.shape[0]:,} | shape (N,10,W,3)={X.shape} "
      f"| dtype={X.dtype}")
print(f"  stale windows: {int(META['stale'].sum()):,} "
      f"({100.0 * META['stale'].mean():.2f}%)")
print(f"  meta rows: {len(META):,} | unique days: {META['day_id'].nunique()}")
print(f"  build time: {_build_sec:.1f}s")

# Persist the tensor + metadata for downstream blocks
np.save(Path(CFG.OUT_DIR) / "X_windows.npy", X)
META.to_parquet(Path(CFG.OUT_DIR) / "META_windows.parquet", index=False)
with open(Path(CFG.OUT_DIR) / "NORM_STATS.json", "w") as f:
    json.dump(NORM_STATS, f, indent=2)
with open(Path(CFG.OUT_DIR) / "TRAIN_DAYS.json", "w") as f:
    json.dump(TRAIN_DAYS, f, indent=2)

print(f"\n  artifacts saved under: {CFG.OUT_DIR}")
print("    X_windows.npy              (N, 10, W, 3)  float32")
print("    META_windows.parquet       window metadata")
print("    NORM_STATS.json            train-day scaler")
print("    TRAIN_DAYS.json            train day-id list (for purged splits)")
print("\n[Block 1 complete — ready for Block 2: labels + LOB-MAE pretraining]")
