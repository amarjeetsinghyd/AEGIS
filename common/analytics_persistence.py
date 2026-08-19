"""Analytics Persistence Layer — Big-Data Aggregation Pipeline for ARGUS + AEGIS.

Design goals
------------
1. **Daily runs at 15:40 IST** — a single scheduler-friendly entry point
   (`run_daily_aggregation`) that aggregates BOTH engines' raw L2 parquet
   partitions into compact per-day summary snapshots.
2. **Instant Range / All-Time views** — the dashboard never scans raw ticks.
   It reads pre-aggregated daily snapshots (`analytics/daily/*.parquet`) which
   are tiny (a few KB per symbol per day). Range/All-Time views are just a
   `pd.concat` of N daily snapshots — O(days), not O(ticks).
3. **Engine-specific metrics** — ARGUS snapshots carry microstructure
   primitives (OIB, toxicity, queue decay, branching); AEGIS snapshots carry
   gamma/IV/strike-heatmap primitives. Both share the same storage shape so
   the dashboard can treat them uniformly.

Storage layout
--------------
    <engine>/data/analytics/daily/argus_YYYY-MM-DD.parquet    (per-symbol rows)
    <engine>/data/analytics/daily/aegis_YYYY-MM-DD.parquet    (per-strike rows)
    <engine>/data/analytics/manifest.json                     (run log)

Each daily snapshot is a single parquet file with one row per symbol (ARGUS)
or per (strike, kind) (AEGIS), holding ~30 aggregated columns. A 250-trading-
day All-Time view is therefore ~250 tiny files — concat takes <1s.
"""
from __future__ import annotations

import os
import sys
import json
import argparse
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

# ─── BOOTSTRAP ───
BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from common.logger import get_logger
from common.runtime.paths import get_runtime_paths

logger = get_logger("analytics_persistence")

# ─── IST market close + aggregation trigger ───
AGG_TRIGGER_TIME = dtime(15, 40)  # 15:40 IST — after 15:30 close auction


# ═══════════════════════════════════════════════════════════════════
# ARGUS (Equities Microstructure) Daily Aggregation
# ═══════════════════════════════════════════════════════════════════

ARGUS_SCHEMA_COLS = [
    "ts", "tok", "lp", "vol",
    "bp1", "bq1", "bp2", "bq2", "bp3", "bq3", "bp4", "bq4", "bp5", "bq5",
    "sp1", "sq1", "sp2", "sq2", "sp3", "sq3", "sp4", "sq4", "sp5", "sq5",
]
VORTEX_SCHEMA_COLS = ARGUS_SCHEMA_COLS


def _load_argus_partitions(target_date: str) -> pd.DataFrame:
    """Load all ARGUS L2 partitions for a given date."""
    paths = get_runtime_paths("stock_order_flow")
    l2_dir = Path(paths["data_dir"]) / "l2_raw"
    files = sorted(l2_dir.glob(f"l2_raw_{target_date}_part_*.parquet"))
    if not files:
        return pd.DataFrame()
    dfs = []
    for f in files:
        try:
            df = pd.read_parquet(f, columns=ARGUS_SCHEMA_COLS)
            if not df.empty:
                dfs.append(df)
        except Exception:
            continue
    if not dfs:
        return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True)

_load_vortex_partitions = _load_argus_partitions


def _argus_symbol_map() -> dict[int, str]:
    """Load token→symbol from universe.json + tokens_cache.json."""
    paths = get_runtime_paths("stock_order_flow")
    data_dir = Path(paths["data_dir"])
    t2s: dict[int, str] = {}
    uni_path = data_dir / "universe.json"
    if uni_path.exists():
        try:
            with open(uni_path) as f:
                uni = json.load(f)
            for s in uni.get("stocks", []):
                t2s[int(s["token"])] = s["symbol"]
        except Exception:
            pass
    cache_path = data_dir / "tokens_cache.json"
    if cache_path.exists():
        try:
            with open(cache_path) as f:
                cache = json.load(f)
            for sym, tok in cache.items():
                try:
                    t2s[int(tok)] = sym
                except (ValueError, TypeError):
                    continue
        except Exception:
            pass
    return t2s

_vortex_symbol_map = _argus_symbol_map


def aggregate_argus_day(target_date: str) -> pd.DataFrame:
    """Aggregate ARGUS raw L2 ticks for one day into a per-symbol snapshot.

    Each row = one symbol with ~30 microstructure summary stats. This is the
    unit that Range/All-Time views concat — never the raw ticks.
    """
    df = _load_argus_partitions(target_date)
    if df.empty:
        return pd.DataFrame()

    t2s = _argus_symbol_map()
    df["symbol"] = df["tok"].map(t2s).fillna("")

    # Per-symbol vectorized aggregation (NumPy, no python loops over ticks)
    rows = []
    for sym, g in df.groupby("symbol"):
        if not sym:
            continue
        lp = g["lp"].values
        vol = g["vol"].values
        ts = g["ts"].values / 1000.0
        bp = g[[f"bp{i}" for i in range(1, 6)]].values
        bq = g[[f"bq{i}" for i in range(1, 6)]].values
        sp = g[[f"sp{i}" for i in range(1, 6)]].values
        sq = g[[f"sq{i}" for i in range(1, 6)]].values

        mid = (bp[:, 0] + sp[:, 0]) / 2.0
        spread = sp[:, 0] - bp[:, 0]
        b_val = np.sum(bp * bq, axis=1)
        s_val = np.sum(sp * sq, axis=1)
        oimb = (b_val - s_val) / np.maximum(b_val + s_val, 1.0)

        dvol = np.diff(vol, prepend=vol[0])
        dvol = np.maximum(dvol, 0)
        trade_val = dvol * lp
        buy_val = np.where(lp >= sp[:, 0], trade_val, np.where(lp <= bp[:, 0], 0, trade_val * 0.5))
        sell_val = np.where(lp <= bp[:, 0], trade_val, np.where(lp >= sp[:, 0], 0, trade_val * 0.5))

        # Queue decay (L1 cancellation proxy)
        bid_decay = np.zeros(len(g))
        ask_decay = np.zeros(len(g))
        bq0 = bq[:, 0]
        sq0 = sq[:, 0]
        bid_cancel = np.maximum(np.diff(bq0, prepend=bq0[0]), 0)
        ask_cancel = np.maximum(np.diff(sq0, prepend=sq0[0]), 0)
        bid_decay = bid_cancel / np.maximum(bq0, 1.0) * 10000
        ask_decay = ask_cancel / np.maximum(sq0, 1.0) * 10000

        rows.append({
            "date": target_date,
            "symbol": sym,
            "ticks": int(len(g)),
            "open": float(lp[0]),
            "high": float(lp.max()),
            "low": float(lp.min()),
            "close": float(lp[-1]),
            "vwap": float(np.sum(trade_val) / max(np.sum(dvol), 1.0)),
            "total_volume": float(np.sum(dvol)),
            "total_notional": float(np.sum(trade_val)),
            "buy_notional": float(np.sum(buy_val)),
            "sell_notional": float(np.sum(sell_val)),
            "oib_mean": float(np.mean(oimb)),
            "oib_std": float(np.std(oimb)),
            "spread_mean": float(np.mean(spread)),
            "spread_p95": float(np.percentile(spread, 95)),
            "microprice_mean": float(np.mean(mid + spread * (oimb - 0.5))),
            "bid_queue_decay_mean": float(np.mean(bid_decay)),
            "ask_queue_decay_mean": float(np.mean(ask_decay)),
            "bid_queue_decay_p95": float(np.percentile(bid_decay, 95)),
            "ask_queue_decay_p95": float(np.percentile(ask_decay, 95)),
            "whale_conc": float(np.max(trade_val) / max(np.sum(trade_val), 1.0)),
            "trade_intensity": float(len(g) / max((ts[-1] - ts[0]), 1.0)),  # ticks/sec
            "first_ts": float(ts[0]),
            "last_ts": float(ts[-1]),
        })

    return pd.DataFrame(rows)


# ═══════════════════════════════════════════════════════════════════
# AEGIS (Options Gamma) Daily Aggregation
# ═══════════════════════════════════════════════════════════════════

AEGIS_SCHEMA_COLS = [
    "ts", "tok", "sym", "kind", "strike", "lp", "vol", "oi",
    "bp1", "bq1", "bp2", "bq2", "bp3", "bq3", "bp4", "bq4", "bp5", "bq5",
    "sp1", "sq1", "sp2", "sq2", "sp3", "sq3", "sp4", "sq4", "sp5", "sq5",
]


def _load_aegis_partitions(target_date: str) -> pd.DataFrame:
    """Load all AEGIS L2 options partitions for a given date."""
    paths = get_runtime_paths("aegis")
    l2_dir = Path(paths["data_dir"]) / "l2_raw"
    files = sorted(l2_dir.glob(f"aegis_l2_raw_{target_date}_part_*.parquet"))
    if not files:
        return pd.DataFrame()
    dfs = []
    for f in files:
        try:
            df = pd.read_parquet(f, columns=AEGIS_SCHEMA_COLS)
            if not df.empty:
                dfs.append(df)
        except Exception:
            continue
    if not dfs:
        return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True)


def aggregate_aegis_day(target_date: str) -> pd.DataFrame:
    """Aggregate AEGIS raw options L2 ticks for one day into a per-strike snapshot.

    Each row = one (strike, kind) with gamma-relevant summary stats: IV crush,
    OI change, premium range, volume, book imbalance. The underlying index row
    (kind=0) carries spot OHLC + ATM IV proxy.
    """
    df = _load_aegis_partitions(target_date)
    if df.empty:
        return pd.DataFrame()

    rows = []
    # Underlying index row (kind=0)
    idx_df = df[df["kind"] == 0]
    if not idx_df.empty:
        lp = idx_df["lp"].values
        vol = idx_df["vol"].values
        ts = idx_df["ts"].values / 1000.0
        rows.append({
            "date": target_date,
            "symbol": "INDEX",
            "kind": 0,
            "strike": 0.0,
            "ticks": int(len(idx_df)),
            "open": float(lp[0]),
            "high": float(lp.max()),
            "low": float(lp.min()),
            "close": float(lp[-1]),
            "vwap": float(np.sum(np.diff(vol, prepend=vol[0]) * lp) / max(np.sum(np.diff(vol, prepend=vol[0])), 1.0)),
            "total_volume": float(np.sum(np.diff(vol, prepend=vol[0]))),
            "oi_open": 0.0,
            "oi_close": 0.0,
            "oi_change": 0.0,
            "iv_open": 0.0,
            "iv_close": 0.0,
            "iv_crush_bps": 0.0,
            "premium_range": 0.0,
            "oib_mean": 0.0,
            "oib_std": 0.0,
            "trade_intensity": float(len(idx_df) / max((ts[-1] - ts[0]), 1.0)),
            "first_ts": float(ts[0]),
            "last_ts": float(ts[-1]),
        })

    # Options rows (kind=1 CE, kind=2 PE)
    opt_df = df[df["kind"] != 0]
    for (strike, kind), g in opt_df.groupby(["strike", "kind"]):
        lp = g["lp"].values
        vol = g["vol"].values
        oi = g["oi"].values
        ts = g["ts"].values / 1000.0
        bp = g[[f"bp{i}" for i in range(1, 6)]].values
        bq = g[[f"bq{i}" for i in range(1, 6)]].values
        sp = g[[f"sp{i}" for i in range(1, 6)]].values
        sq = g[[f"sq{i}" for i in range(1, 6)]].values

        b_val = np.sum(bp * bq, axis=1)
        s_val = np.sum(sp * sq, axis=1)
        oimb = (b_val - s_val) / np.maximum(b_val + s_val, 1.0)

        dvol = np.diff(vol, prepend=vol[0])
        dvol = np.maximum(dvol, 0)
        trade_val = dvol * lp

        # IV proxy: premium / spot (rough ATM IV crush proxy when spot available)
        # True IV requires BSM; we store premium-derived metrics for the snapshot
        # and let the research UI compute proper IV from the spot row + strike.
        premium_open = float(lp[0]) if len(lp) else 0.0
        premium_close = float(lp[-1]) if len(lp) else 0.0
        oi_open = float(oi[0]) if len(oi) else 0.0
        oi_close = float(oi[-1]) if len(oi) else 0.0

        rows.append({
            "date": target_date,
            "symbol": str(g["sym"].iloc[0]) if len(g) else "",
            "kind": int(kind),
            "strike": float(strike),
            "ticks": int(len(g)),
            "open": premium_open,
            "high": float(lp.max()) if len(lp) else 0.0,
            "low": float(lp.min()) if len(lp) else 0.0,
            "close": premium_close,
            "vwap": float(np.sum(trade_val) / max(np.sum(dvol), 1.0)),
            "total_volume": float(np.sum(dvol)),
            "oi_open": oi_open,
            "oi_close": oi_close,
            "oi_change": float(oi_close - oi_open),
            "iv_open": 0.0,   # filled by research UI with BSM
            "iv_close": 0.0,
            "iv_crush_bps": 0.0,
            "premium_range": float(lp.max() - lp.min()) if len(lp) else 0.0,
            "oib_mean": float(np.mean(oimb)),
            "oib_std": float(np.std(oimb)),
            "trade_intensity": float(len(g) / max((ts[-1] - ts[0]), 1.0)),
            "first_ts": float(ts[0]),
            "last_ts": float(ts[-1]),
        })

    return pd.DataFrame(rows)


def _normalize_engine_name(engine: str) -> str:
    eng = (engine or "").strip().lower()
    if eng in ("vortex", "stock_order_flow", "argus", "of"):
        return "argus"
    return eng


def _analytics_dir(engine: Literal["argus", "vortex", "aegis"]) -> Path:
    norm_engine = _normalize_engine_name(engine)
    paths = get_runtime_paths("stock_order_flow" if norm_engine == "argus" else "aegis")
    d = Path(paths["data_dir"]) / "analytics" / "daily"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _manifest_path(engine: Literal["argus", "vortex", "aegis"]) -> Path:
    norm_engine = _normalize_engine_name(engine)
    paths = get_runtime_paths("stock_order_flow" if norm_engine == "argus" else "aegis")
    return Path(paths["data_dir"]) / "analytics" / "manifest.json"


def _read_manifest(engine: str) -> dict:
    p = _manifest_path(engine)
    if p.exists():
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            pass
    return {"runs": []}


def _write_manifest(engine: str, manifest: dict) -> None:
    p = _manifest_path(engine)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        json.dump(manifest, f, indent=2)


def save_daily_snapshot(engine: Literal["argus", "vortex", "aegis"], target_date: str, df: pd.DataFrame) -> Path | None:
    """Persist a daily aggregated snapshot to parquet + update the manifest."""
    norm_engine = _normalize_engine_name(engine)
    if df.empty:
        logger.info(f"[{norm_engine}] No data for {target_date} — skipping snapshot")
        return None
    out_dir = _analytics_dir(norm_engine)
    out_path = out_dir / f"{norm_engine}_{target_date}.parquet"
    df.to_parquet(out_path, index=False, compression="snappy")
    size_kb = out_path.stat().st_size / 1024
    logger.info(f"[{norm_engine}] Saved daily snapshot {out_path.name} | {len(df)} rows | {size_kb:.1f} KB")

    manifest = _read_manifest(norm_engine)
    runs = manifest.setdefault("runs", [])
    # De-dupe by date
    runs = [r for r in runs if r.get("date") != target_date]
    runs.append({
        "date": target_date,
        "rows": int(len(df)),
        "size_kb": round(size_kb, 1),
        "run_at": datetime.now().isoformat(),
    })
    runs.sort(key=lambda r: r["date"])
    manifest["runs"] = runs
    _write_manifest(norm_engine, manifest)
    return out_path


def load_daily_snapshots(engine: Literal["argus", "vortex", "aegis"], start_date: str | None = None, end_date: str | None = None) -> pd.DataFrame:
    """Load and concat daily snapshots for a date range (or all-time).

    This is the fast path the dashboard uses for Range/All-Time views: it
    reads N tiny parquet files (one per day) instead of scanning raw ticks.
    """
    norm_engine = _normalize_engine_name(engine)
    out_dir = _analytics_dir(norm_engine)
    # Check both current prefix and legacy vortex_ prefix for full backward-compatibility
    if norm_engine == "argus":
        files = sorted(set(list(out_dir.glob("argus_*.parquet")) + list(out_dir.glob("vortex_*.parquet"))))
    else:
        files = sorted(out_dir.glob(f"{norm_engine}_*.parquet"))

    if not files:
        return pd.DataFrame()

    selected = []
    for f in files:
        # filename: argus_YYYY-MM-DD.parquet or vortex_YYYY-MM-DD.parquet
        date_str = f.stem.split("_", 1)[1] if "_" in f.stem else ""
        if start_date and date_str < start_date:
            continue
        if end_date and date_str > end_date:
            continue
        selected.append(f)

    if not selected:
        return pd.DataFrame()

    dfs = []
    for f in selected:
        try:
            dfs.append(pd.read_parquet(f))
        except Exception:
            continue
    if not dfs:
        return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True)


def list_available_dates(engine: Literal["argus", "vortex", "aegis"]) -> list[str]:
    """Return sorted list of dates that have a daily snapshot."""
    norm_engine = _normalize_engine_name(engine)
    out_dir = _analytics_dir(norm_engine)
    if norm_engine == "argus":
        files = sorted(set(list(out_dir.glob("argus_*.parquet")) + list(out_dir.glob("vortex_*.parquet"))))
    else:
        files = sorted(out_dir.glob(f"{norm_engine}_*.parquet"))
    dates = []
    for f in files:
        date_str = f.stem.split("_", 1)[1] if "_" in f.stem else ""
        if date_str and date_str not in dates:
            dates.append(date_str)
    return sorted(dates)


# ═══════════════════════════════════════════════════════════════════
# Daily Run Entry Point (15:40 IST scheduler)
# ═══════════════════════════════════════════════════════════════════

def run_daily_aggregation(target_date: str | None = None, engines: list[str] | None = None) -> dict:
    """Aggregate both engines for a given date (default: today).

    Designed to be invoked by a 15:40 IST cron / PM2 cron-style loop or by
    `python -m common.analytics_persistence --date YYYY-MM-DD`.
    Returns a summary dict suitable for logging / dashboard status.
    """
    target_date = target_date or datetime.now().strftime("%Y-%m-%d")
    raw_engines = engines or ["argus", "aegis"]
    # Normalize engine list while preserving order
    engines = []
    for e in raw_engines:
        ne = _normalize_engine_name(e)
        if ne not in engines:
            engines.append(ne)

    summary = {"date": target_date, "engines": {}}

    for engine in engines:
        try:
            if engine == "argus":
                df = aggregate_argus_day(target_date)
            elif engine == "aegis":
                df = aggregate_aegis_day(target_date)
            else:
                continue
            saved = save_daily_snapshot(engine, target_date, df)
            summary["engines"][engine] = {
                "rows": int(len(df)),
                "saved": saved is not None,
                "path": str(saved) if saved else None,
            }
        except Exception as e:
            logger.error(f"[{engine}] daily aggregation failed for {target_date}: {e}", exc_info=True)
            summary["engines"][engine] = {"error": str(e)}

    logger.info(f"Daily aggregation complete: {summary}")
    return summary


def main():
    parser = argparse.ArgumentParser(description="Analytics Persistence — Daily Aggregation (15:40 IST)")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"), help="Date YYYY-MM-DD (default: today)")
    parser.add_argument("--engines", nargs="*", default=["argus", "aegis"], help="Engines to aggregate (default: both)")
    args = parser.parse_args()
    summary = run_daily_aggregation(args.date, args.engines)
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()

