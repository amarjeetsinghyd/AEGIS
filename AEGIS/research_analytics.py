#!/usr/bin/env python3
"""AEGIS Research Analytics — Options Gamma / IV / Strike Heatmap Forensics.

Reads partitioned AEGIS L2 options parquet (from l2_options_archiver) and
generates Plotly charts for the AEGIS Research tab in the dashboard.

Engine-specific charts (designed for the Gamma Burst strategy):
  1. Strike Heatmap (Gamma surface) — CE/PE volume × strike × time
  2. IV Crush — ATM IV trajectory across the session
  3. OI Buildup — open-interest change per strike (positioning)
  4. Premium Decay — premium erosion per strike (theta bleed proxy)
  5. Book Imbalance Distribution — OIB histograms for CE vs PE

Supports Single Date, Date Range, and All-Time via the analytics_persistence
layer (reads pre-aggregated daily snapshots for Range/All-Time, raw partitions
for Single Date deep-dive).
"""
from __future__ import annotations

import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, time as dtime

import numpy as np
import pandas as pd

# ─── BOOTSTRAP ───
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.analytics_persistence import (
    AEGIS_SCHEMA_COLS,
    _load_aegis_partitions,
    load_daily_snapshots,
)
from common.runtime.paths import get_runtime_paths

# ─── BSM for IV backfill (off-hot-path; research only) ───
def _bsm_iv(price, S, K, T, r=0.06, is_call=True):
    """Implied vol via Newton-Raphson (vectorized over scalar args)."""
    if T <= 0 or S <= 0 or K <= 0 or price <= 0:
        return 0.0
    sigma = 0.2
    for _ in range(50):
        d1 = (np.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * np.sqrt(T))
        d2 = d1 - sigma * np.sqrt(T)
        pdf = np.exp(-0.5 * d1 * d1) / np.sqrt(2 * np.pi)
        cdf_d1 = 0.5 * (1 + np.erf(d1 / np.sqrt(2)))
        cdf_d2 = 0.5 * (1 + np.erf(d2 / np.sqrt(2)))
        if is_call:
            model = S * cdf_d1 - K * np.exp(-r * T) * cdf_d2
        else:
            model = K * np.exp(-r * T) * (1 - cdf_d2) - S * (1 - cdf_d1)
        vega = S * np.sqrt(T) * pdf
        diff = model - price
        if abs(diff) < 1e-4 or vega < 1e-8:
            break
        sigma = max(0.01, min(5.0, sigma - diff / vega))
    return float(sigma)


def _days_to_expiry(expiry_str: str, ref_date: datetime) -> float:
    """Parse expiry (YYYY-MM-DD or datetime) → days to expiry."""
    try:
        if isinstance(expiry_str, str):
            exp = datetime.fromisoformat(expiry_str.split(" ")[0])
        else:
            exp = expiry_str
        return max((exp - ref_date).total_seconds() / 86400.0, 1e-4)
    except Exception:
        return 7.0  # default weekly


# ═══════════════════════════════════════════════════════════════════
# CHART BUILDERS
# ═══════════════════════════════════════════════════════════════════

def create_strike_heatmap(df: pd.DataFrame) -> dict:
    """Gamma surface: CE/PE volume × strike × time heatmap."""
    if df.empty:
        return {"data": [], "layout": {"title": "No Data"}}

    opt = df[df["kind"] != 0].copy()
    if opt.empty:
        return {"data": [], "layout": {"title": "No option ticks"}}

    opt["ts_sec"] = opt["ts"] / 1000.0
    opt["time_bin"] = (opt["ts_sec"] // 300).astype(int)  # 5-min bins
    opt["dvol"] = opt.groupby(["strike", "kind"])["vol"].transform(lambda x: x.diff().fillna(x).clip(lower=0))

    # Pivot: strike × time → volume
    ce = opt[opt["kind"] == 1]
    pe = opt[opt["kind"] == 2]

    def _pivot(side_df):
        if side_df.empty:
            return None
        pv = side_df.pivot_table(index="strike", columns="time_bin", values="dvol", aggfunc="sum", fill_value=0)
        return pv

    ce_pv = _pivot(ce)
    pe_pv = _pivot(pe)

    traces = []
    if ce_pv is not None and not ce_pv.empty:
        time_labels = [datetime.fromtimestamp(t * 300).strftime("%H:%M") for t in ce_pv.columns]
        traces.append({
            "type": "heatmap",
            "z": ce_pv.values.tolist(),
            "x": time_labels,
            "y": [f"{s:.0f}" for s in ce_pv.index],
            "colorscale": "Greens",
            "name": "CE Volume",
            "colorbar": {"title": "CE Vol", "x": 1.0},
            "hovertemplate": "Strike: %{y}<br>Time: %{x}<br>Vol: %{z}<extra></extra>",
        })
    if pe_pv is not None and not pe_pv.empty:
        time_labels = [datetime.fromtimestamp(t * 300).strftime("%H:%M") for t in pe_pv.columns]
        traces.append({
            "type": "heatmap",
            "z": pe_pv.values.tolist(),
            "x": time_labels,
            "y": [f"{s:.0f}" for s in pe_pv.index],
            "colorscale": "Reds",
            "name": "PE Volume",
            "colorbar": {"title": "PE Vol", "x": 1.15},
            "hovertemplate": "Strike: %{y}<br>Time: %{x}<br>Vol: %{z}<extra></extra>",
            "xaxis": "x2", "yaxis": "y2",
        })

    layout = {
        "title": "Strike Heatmap — CE (left) / PE (right) Volume",
        "height": 550,
        "grid": {"rows": 1, "columns": 2} if len(traces) == 2 else {"rows": 1, "columns": 1},
        "margin": {"l": 60, "r": 80, "t": 40, "b": 40},
        "plot_bgcolor": "#0d1117",
        "paper_bgcolor": "#0d1117",
        "font": {"color": "#c9d1d9"},
    }
    return {"data": traces, "layout": layout}


def create_iv_crush(df: pd.DataFrame, expiry: str | None = None) -> dict:
    """ATM IV trajectory across the session (IV crush visualization)."""
    if df.empty:
        return {"data": [], "layout": {"title": "No Data"}}

    idx = df[df["kind"] == 0].copy()
    opt = df[df["kind"] != 0].copy()
    if idx.empty or opt.empty:
        return {"data": [], "layout": {"title": "Need index + option ticks"}}

    idx = idx.sort_values("ts")
    spot_series = idx[["ts", "lp"]].drop_duplicates("ts").reset_index(drop=True)

    # For each option tick, interpolate spot, compute IV
    ref_date = datetime.fromtimestamp(idx["ts"].iloc[0] / 1000.0)
    T = _days_to_expiry(expiry, ref_date) if expiry else 7.0

    opt = opt.sort_values("ts")
    iv_records = []
    for _, row in opt.iterrows():
        ts = row["ts"]
        # nearest spot
        spot = spot_series.iloc[(spot_series["ts"] - ts).abs().argsort().iloc[0]]["lp"]
        if spot <= 0 or row["lp"] <= 0:
            continue
        iv = _bsm_iv(row["lp"], spot, row["strike"], T, is_call=(row["kind"] == 1))
        if iv > 0:
            iv_records.append({"ts": ts / 1000.0, "strike": row["strike"], "kind": int(row["kind"]), "iv": iv})

    if not iv_records:
        return {"data": [], "layout": {"title": "IV computation failed"}}

    iv_df = pd.DataFrame(iv_records)
    # ATM IV = strike closest to spot, smoothed
    spot_final = spot_series["lp"].iloc[-1]
    iv_df["dist"] = (iv_df["strike"] - spot_final).abs()
    # Use strikes within 2 steps of ATM
    step = 50 if spot_final < 20000 else 100
    atm_mask = iv_df["dist"] <= step * 2
    atm = iv_df[atm_mask].copy()
    if atm.empty:
        atm = iv_df

    # Resample to 5-min bins, mean IV per kind
    atm["time_bin"] = (atm["ts"] // 300).astype(int)
    ce_iv = atm[atm["kind"] == 1].groupby("time_bin")["iv"].mean()
    pe_iv = atm[atm["kind"] == 2].groupby("time_bin")["iv"].mean()

    traces = []
    if not ce_iv.empty:
        traces.append({
            "type": "scatter", "mode": "lines",
            "x": [datetime.fromtimestamp(t * 300).strftime("%H:%M") for t in ce_iv.index],
            "y": (ce_iv.values * 100).tolist(),
            "name": "ATM CE IV %", "line": {"color": "#1f7a4c", "width": 2},
        })
    if not pe_iv.empty:
        traces.append({
            "type": "scatter", "mode": "lines",
            "x": [datetime.fromtimestamp(t * 300).strftime("%H:%M") for t in pe_iv.index],
            "y": (pe_iv.values * 100).tolist(),
            "name": "ATM PE IV %", "line": {"color": "#b42318", "width": 2},
        })

    return {
        "data": traces,
        "layout": {
            "title": "ATM IV Crush (session trajectory)",
            "xaxis": {"title": "Time"},
            "yaxis": {"title": "Implied Volatility (%)"},
            "height": 450,
            "margin": {"l": 60, "r": 20, "t": 40, "b": 40},
            "plot_bgcolor": "#0d1117",
            "paper_bgcolor": "#0d1117",
            "font": {"color": "#c9d1d9"},
            "legend": {"orientation": "h", "y": -0.2},
        }
    }


def create_oi_buildup(df: pd.DataFrame) -> dict:
    """Open-interest change per strike (positioning visualization)."""
    if df.empty:
        return {"data": [], "layout": {"title": "No Data"}}

    opt = df[df["kind"] != 0].copy()
    if opt.empty:
        return {"data": [], "layout": {"title": "No option ticks"}}

    # OI change per strike: last OI - first OI
    opt = opt.sort_values(["strike", "kind", "ts"])
    oi_change = opt.groupby(["strike", "kind"]).agg(
        oi_open=("oi", "first"), oi_close=("oi", "last")
    ).reset_index()
    oi_change["delta"] = oi_change["oi_close"] - oi_change["oi_open"]

    ce = oi_change[oi_change["kind"] == 1]
    pe = oi_change[oi_change["kind"] == 2]

    traces = []
    if not ce.empty:
        traces.append({
            "type": "bar", "x": ce["strike"].tolist(), "y": ce["delta"].tolist(),
            "name": "CE OI Δ", "marker": {"color": "#1f7a4c"},
        })
    if not pe.empty:
        traces.append({
            "type": "bar", "x": pe["strike"].tolist(), "y": pe["delta"].tolist(),
            "name": "PE OI Δ", "marker": {"color": "#b42318"},
        })

    return {
        "data": traces,
        "layout": {
            "title": "OI Buildup per Strike (positioning)",
            "xaxis": {"title": "Strike"},
            "yaxis": {"title": "OI Change (contracts)"},
            "barmode": "group",
            "height": 450,
            "margin": {"l": 60, "r": 20, "t": 40, "b": 40},
            "plot_bgcolor": "#0d1117",
            "paper_bgcolor": "#0d1117",
            "font": {"color": "#c9d1d9"},
            "legend": {"orientation": "h", "y": -0.2},
        }
    }


def create_premium_decay(df: pd.DataFrame) -> dict:
    """Premium erosion per strike (theta bleed proxy)."""
    if df.empty:
        return {"data": [], "layout": {"title": "No Data"}}

    opt = df[df["kind"] != 0].copy()
    if opt.empty:
        return {"data": [], "layout": {"title": "No option ticks"}}

    opt = opt.sort_values(["strike", "kind", "ts"])
    decay = opt.groupby(["strike", "kind"]).agg(
        open=("lp", "first"), close=("lp", "last")
    ).reset_index()
    decay["decay_pct"] = (decay["close"] - decay["open"]) / decay["open"] * 100

    ce = decay[decay["kind"] == 1]
    pe = decay[decay["kind"] == 2]

    traces = []
    if not ce.empty:
        traces.append({
            "type": "bar", "x": ce["strike"].tolist(), "y": ce["decay_pct"].tolist(),
            "name": "CE Premium Δ%", "marker": {"color": "#1f7a4c"},
        })
    if not pe.empty:
        traces.append({
            "type": "bar", "x": pe["strike"].tolist(), "y": pe["decay_pct"].tolist(),
            "name": "PE Premium Δ%", "marker": {"color": "#b42318"},
        })

    return {
        "data": traces,
        "layout": {
            "title": "Premium Decay per Strike (theta bleed proxy)",
            "xaxis": {"title": "Strike"},
            "yaxis": {"title": "Premium Change (%)"},
            "barmode": "group",
            "height": 450,
            "margin": {"l": 60, "r": 20, "t": 40, "b": 40},
            "plot_bgcolor": "#0d1117",
            "paper_bgcolor": "#0d1117",
            "font": {"color": "#c9d1d9"},
            "legend": {"orientation": "h", "y": -0.2},
        }
    }


def create_oib_distribution(df: pd.DataFrame) -> dict:
    """Book imbalance distribution: CE vs PE."""
    if df.empty:
        return {"data": [], "layout": {"title": "No Data"}}

    opt = df[df["kind"] != 0].copy()
    if opt.empty:
        return {"data": [], "layout": {"title": "No option ticks"}}

    bp = opt[[f"bp{i}" for i in range(1, 6)]].values
    bq = opt[[f"bq{i}" for i in range(1, 6)]].values
    sp = opt[[f"sp{i}" for i in range(1, 6)]].values
    sq = opt[[f"sq{i}" for i in range(1, 6)]].values
    b_val = np.sum(bp * bq, axis=1)
    s_val = np.sum(sp * sq, axis=1)
    oimb = (b_val - s_val) / np.maximum(b_val + s_val, 1.0)

    ce_oimb = oimb[opt["kind"].values == 1]
    pe_oimb = oimb[opt["kind"].values == 2]

    traces = []
    if len(ce_oimb):
        traces.append({"type": "histogram", "x": ce_oimb.tolist(), "name": "CE OIB", "nbinsx": 50, "opacity": 0.7, "marker": {"color": "#1f7a4c"}})
    if len(pe_oimb):
        traces.append({"type": "histogram", "x": pe_oimb.tolist(), "name": "PE OIB", "nbinsx": 50, "opacity": 0.7, "marker": {"color": "#b42318"}})

    return {
        "data": traces,
        "layout": {
            "title": "Book Imbalance Distribution (CE vs PE)",
            "xaxis": {"title": "OIB"},
            "yaxis": {"title": "Count"},
            "barmode": "overlay",
            "height": 400,
            "margin": {"l": 60, "r": 20, "t": 40, "b": 40},
            "plot_bgcolor": "#0d1117",
            "paper_bgcolor": "#0d1117",
            "font": {"color": "#c9d1d9"},
            "legend": {"orientation": "h", "y": -0.2},
        }
    }


# ═══════════════════════════════════════════════════════════════════
# MAIN ENTRY (subprocess contract: print JSON to stdout)
# ═══════════════════════════════════════════════════════════════════

def run_research(target_date: str, start_date: str | None = None, end_date: str | None = None) -> dict:
    """Build all AEGIS research charts for a date or range.

    Single Date → deep-dive on raw partitions (full tick resolution).
    Date Range / All-Time → use pre-aggregated daily snapshots (instant load).
    """
    use_snapshots = start_date is not None or end_date is not None
    if use_snapshots:
        df = load_daily_snapshots("aegis", start_date, end_date)
        mode = "range"
    else:
        df = _load_aegis_partitions(target_date)
        mode = "single"

    if df.empty:
        return {"error": f"No AEGIS data for {target_date}" + (f"–{end_date}" if end_date else "")}

    # Meta
    if "ts" in df.columns and len(df):
        ts_min = df["ts"].min() / 1000.0
        ts_max = df["ts"].max() / 1000.0
        timerange = f"{datetime.fromtimestamp(ts_min).strftime('%H:%M:%S')} - {datetime.fromtimestamp(ts_max).strftime('%H:%M:%S')}"
        ticks = int(len(df))
    else:
        timerange = "—"
        ticks = 0

    meta = {
        "engine": "aegis",
        "mode": mode,
        "date": target_date,
        "start_date": start_date,
        "end_date": end_date,
        "ticks": ticks,
        "timerange": timerange,
    }

    return {
        "meta": meta,
        "strike_heatmap": create_strike_heatmap(df),
        "iv_crush": create_iv_crush(df),
        "oi_buildup": create_oi_buildup(df),
        "premium_decay": create_premium_decay(df),
        "oib_distribution": create_oib_distribution(df),
    }


def main():
    parser = argparse.ArgumentParser(description="AEGIS Research Analytics (Gamma / IV / Strike Heatmaps)")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"), help="Single date YYYY-MM-DD")
    parser.add_argument("--start", default=None, help="Range start YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="Range end YYYY-MM-DD")
    args = parser.parse_args()

    result = run_research(args.date, args.start, args.end)
    print(json.dumps(result, default=lambda o: float(o) if isinstance(o, (np.floating, np.integer)) else o.tolist() if isinstance(o, np.ndarray) else str(o)))


if __name__ == "__main__":
    main()
