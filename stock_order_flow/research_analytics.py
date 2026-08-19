#!/usr/bin/env python3
"""
ARGUS On-Demand Microstructure Forensics — Research Analytics Engine
(Algorithmic Real-Time Granular Ultra-Microstructure Scalper)
Reads partitioned L2 Parquet from l2_archiver, generates Plotly charts for dashboard Research Mode.
Compatible with v3.6 data structures (includes QUEUE_DECAY, MPAR, DKLA, LRA, VAC, WHALE_Z, etc.)

Supports Single Date (raw partition deep-dive), Date Range, and All-Time
(pre-aggregated daily snapshots via common.analytics_persistence for instant load).
"""
from __future__ import annotations

import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, time as dtime
import numpy as np
import pandas as pd

# ─── PATHS ───
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DATA_DIR = PROJECT_ROOT / "stock_order_flow" / "data"
L2_RAW_DIR = DATA_DIR / "l2_raw"
UNIVERSE_PATH = PROJECT_ROOT / "stock_order_flow" / "universe.json"
TOKENS_CACHE = DATA_DIR / "tokens_cache.json"

from common.analytics_persistence import load_daily_snapshots

# ─── SCHEMA (matches l2_archiver.py SCHEMA_DTYPES) ───
SCHEMA_COLS = [
    "ts", "tok", "lp", "vol",
    "bp1", "bq1", "bp2", "bq2", "bp3", "bq3", "bp4", "bq4", "bp5", "bq5",
    "sp1", "sq1", "sp2", "sq2", "sp3", "sq3", "sp4", "sq4", "sp5", "sq5",
]

def load_universe():
    """Load universe.json and return a flat symbol -> token mapping."""
    if UNIVERSE_PATH.exists():
        with open(UNIVERSE_PATH) as f:
            data = json.load(f)
        # universe.json has {"date": "...", "stocks": [{"symbol": "...", "token": "..."}, ...]}
        stocks = data.get("stocks", [])
        if isinstance(stocks, list):
            return {s["symbol"]: s["token"] for s in stocks if "symbol" in s and "token" in s}
    return {}

def load_tokens_cache():
    """Load tokens_cache.json and return a flat symbol -> token mapping."""
    if TOKENS_CACHE.exists():
        with open(TOKENS_CACHE) as f:
            data = json.load(f)
        # tokens_cache.json has {"SYMBOL": {"token": "...", "tsym": "..."}, ...}
        if isinstance(data, dict):
            return {sym: info.get("token", "") for sym, info in data.items() if isinstance(info, dict) and "token" in info}
    return {}

def get_token_for_symbol(symbol: str, universe: dict, tokens_cache: dict) -> str | None:
    """Resolve symbol -> token using universe.json then tokens_cache.json"""
    if symbol in universe:
        return str(universe[symbol])
    if symbol in tokens_cache:
        return str(tokens_cache[symbol])
    return None

def load_l2_data(symbol: str, target_date: str, token: str) -> pd.DataFrame:
    """Load and filter L2 parquet partitions for a symbol/date"""
    date_dir = L2_RAW_DIR
    if not date_dir.exists():
        return pd.DataFrame()
    
    files = sorted(date_dir.glob(f"l2_raw_{target_date}_part_*.parquet"))
    if not files:
        return pd.DataFrame()
    
    dfs = []
    for f in files:
        try:
            df = pd.read_parquet(f, columns=SCHEMA_COLS)
            df = df[df["tok"] == int(token)]
            if not df.empty:
                dfs.append(df)
        except Exception:
            continue
    
    if not dfs:
        return pd.DataFrame()
    
    df = pd.concat(dfs, ignore_index=True)
    df = df.sort_values("ts").reset_index(drop=True)
    return df

def compute_microstructure(df: pd.DataFrame) -> dict:
    """Compute microstructure primitives from raw L2 ticks (vectorized NumPy)"""
    if df.empty:
        return {}
    
    # Basic arrays
    ts = df["ts"].values / 1000.0  # to seconds
    lp = df["lp"].values
    vol = df["vol"].values
    
    # Depth arrays
    bp = df[[f"bp{i}" for i in range(1,6)]].values
    bq = df[[f"bq{i}" for i in range(1,6)]].values
    sp = df[[f"sp{i}" for i in range(1,6)]].values
    sq = df[[f"sq{i}" for i in range(1,6)]].values
    
    # Mid price
    mid = (bp[:,0] + sp[:,0]) / 2.0
    spread = sp[:,0] - bp[:,0]
    
    # Volume-weighted book imbalance (OIB)
    b_val = np.sum(bp * bq, axis=1)
    s_val = np.sum(sp * sq, axis=1)
    oimb = (b_val - s_val) / np.maximum(b_val + s_val, 1.0)
    
    # Microprice
    mp = mid + spread * (oimb - 0.5)
    micro_dev = (lp - mp) / np.maximum(mp, 1e-6)
    
    # Trade classification (Lee-Ready proxy)
    dvol = np.diff(vol, prepend=vol[0])
    dvol = np.maximum(dvol, 0)
    trade_val = dvol * lp
    
    buy_val = np.where(lp >= sp[:,0], trade_val, 
              np.where(lp <= bp[:,0], 0, trade_val * 0.5))
    sell_val = np.where(lp <= bp[:,0], trade_val,
               np.where(lp >= sp[:,0], 0, trade_val * 0.5))
    
    # VPIN proxy
    bucket_size = 2_00_00_000  # 2 Cr
    cum_val = np.cumsum(trade_val)
    bucket_id = (cum_val // bucket_size).astype(int)
    
    # Hawkes branching ratio (simplified)
    trade_times = ts[trade_val > 0]
    branching = 0.0
    if len(trade_times) >= 10:
        lookback = 600  # 10 min
        bin_sec = 10
        bins = int(lookback / bin_sec)
        cutoff = trade_times[-1] - lookback
        recent = trade_times[trade_times >= cutoff]
        if len(recent) >= 10:
            counts = np.zeros(bins)
            for t in recent:
                bi = int((t - cutoff) // bin_sec)
                if 0 <= bi < bins:
                    counts[bi] += 1
            mc, vc = np.mean(counts), np.var(counts)
            if vc > 0 and mc > 0:
                branching = max(0.0, 1.0 - np.sqrt(mc / vc))
    
    # Whale concentration
    whale_conc = np.max(trade_val) / np.maximum(np.sum(trade_val), 1.0)
    
    # Vacuum score (simplified)
    vac_bps = np.zeros(len(df))
    for i in range(1, len(df)):
        bid_dropped = np.maximum(bq[i-1,0] - bq[i,0], 0)
        ask_dropped = np.maximum(sq[i-1,0] - sq[i,0], 0)
        if bid_dropped > 0 or ask_dropped > 0:
            vac_bps[i] = (ask_dropped * sp[i,0] - bid_dropped * bp[i,0]) / np.maximum(b_val[i] + s_val[i], 1.0) * 10000
    
    net_vacuum = np.sum(vac_bps)
    
    # DKLA (impact asymmetry)
    notional = 1_00_00_000
    lam_buy = np.zeros(len(df))
    lam_sell = np.zeros(len(df))
    for i in range(len(df)):
        if bp[i,0] > 0 and sp[i,0] > 0:
            # Simplified book walk
            rem = notional
            shares = 0.0
            for lvl in range(5):
                lv = sp[i,lvl] * sq[i,lvl]
                if lv >= rem:
                    shares += rem / max(sp[i,lvl], 1e-9)
                    rem = 0
                    break
                shares += sq[i,lvl]
                rem -= lv
            if rem <= 1e-6 and shares > 0:
                lam_buy[i] = ((notional / shares) - mid[i]) / mid[i] * 10000
            
            rem = notional
            shares = 0.0
            for lvl in range(5):
                lv = bp[i,lvl] * bq[i,lvl]
                if lv >= rem:
                    shares += rem / max(bp[i,lvl], 1e-9)
                    rem = 0
                    break
                shares += bq[i,lvl]
                rem -= lv
            if rem <= 1e-6 and shares > 0:
                lam_sell[i] = ((notional / shares) - mid[i]) / mid[i] * 10000
    
    dkla_asym = (lam_buy - lam_sell) / np.maximum(lam_buy + lam_sell, 1e-6)
    eff_lam = np.maximum(lam_buy, lam_sell)
    toxicity = eff_lam * np.abs((buy_val - sell_val) / np.maximum(buy_val + sell_val, 1.0))
    
    # LRA (simplified - liquidity replenishment asymmetry)
    lra_asym = np.zeros(len(df))
    for i in range(1, len(df)):
        bid_added = np.maximum(bq[i,0] - bq[i-1,0], 0)
        ask_added = np.maximum(sq[i,0] - sq[i-1,0], 0)
        denom = bid_added + ask_added
        if denom > 0:
            lra_asym[i] = (bid_added - ask_added) / denom
    
    # MPAR (microprice acceleration)
    mp_vel = np.gradient(mp, ts)
    mp_accel = np.gradient(mp_vel, ts)
    mp_align = np.sign(mp_accel * oimb)
    
    # Queue Position Decay (adverse selection)
    bid_decay = np.zeros(len(df))
    ask_decay = np.zeros(len(df))
    for i in range(1, len(df)):
        if bq[i,0] > 0:
            bid_cancelled = max(0, bq[i-1,0] - bq[i,0])
            trade_at_bid = sell_val[i] if lp[i] <= bp[i,0] else 0
            bid_decay[i] = (bid_cancelled + trade_at_bid) / bq[i,0] * 10000
        if sq[i,0] > 0:
            ask_cancelled = max(0, sq[i-1,0] - sq[i,0])
            trade_at_ask = buy_val[i] if lp[i] >= sp[i,0] else 0
            ask_decay[i] = (ask_cancelled + trade_at_ask) / sq[i,0] * 10000
    
    return {
        "ts": ts, "lp": lp, "mid": mid, "spread": spread,
        "oimb": oimb, "micro_dev": micro_dev,
        "buy_val": buy_val, "sell_val": sell_val, "trade_val": trade_val,
        "branching": branching, "whale_conc": whale_conc,
        "net_vacuum_bps": net_vacuum, "vac_bps": vac_bps,
        "dkla_asym": dkla_asym, "eff_lam": eff_lam, "toxicity": toxicity,
        "lra_asym": lra_asym, "mp_accel": mp_accel, "mp_align": mp_align,
        "bid_queue_decay": bid_decay, "ask_queue_decay": ask_decay,
    }

def create_volume_profile(df: pd.DataFrame, micro: dict) -> dict:
    """Volume Profile chart"""
    if df.empty:
        return {"data": [], "layout": {"title": "No Data"}}
    
    lp = df["lp"].values
    vol = df["vol"].values
    dvol = np.diff(vol, prepend=vol[0])
    dvol = np.maximum(dvol, 0)
    
    # Price bins
    price_min, price_max = lp.min(), lp.max()
    bins = np.linspace(price_min, price_max, 50)
    hist, bin_edges = np.histogram(lp, bins=bins, weights=dvol)
    bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
    
    # Color by buy/sell
    buy_vol = np.where(lp >= df[["sp1","sp2","sp3","sp4","sp5"]].min(axis=1).values, dvol, 0)
    sell_vol = np.where(lp <= df[["bp1","bp2","bp3","bp4","bp5"]].max(axis=1).values, dvol, 0)
    
    buy_hist, _ = np.histogram(lp, bins=bins, weights=buy_vol)
    sell_hist, _ = np.histogram(lp, bins=bins, weights=sell_vol)
    
    return {
        "data": [
            {"type": "bar", "x": bin_centers.tolist(), "y": buy_hist.tolist(), "name": "Buy Volume", "marker": {"color": "#1f7a4c"}, "orientation": "h"},
            {"type": "bar", "x": (-sell_hist).tolist(), "y": bin_centers.tolist(), "name": "Sell Volume", "marker": {"color": "#b42318"}, "orientation": "h"},
        ],
        "layout": {
            "title": "Volume Profile (Buy/Sell)",
            "barmode": "overlay",
            "xaxis": {"title": "Volume", "zeroline": True},
            "yaxis": {"title": "Price", "autorange": "reversed"},
            "height": 500,
            "margin": {"l": 60, "r": 20, "t": 40, "b": 40},
            "plot_bgcolor": "#fbfcfd",
            "paper_bgcolor": "#fbfcfd",
        }
    }

def create_depth_heatmap(df: pd.DataFrame, side: str) -> dict:
    """Bid/Ask Depth Heatmap (L1-L5 over time)"""
    if df.empty:
        return {"data": [], "layout": {"title": "No Data"}}
    
    ts = df["ts"].values / 1000.0
    time_labels = [datetime.fromtimestamp(t).strftime("%H:%M:%S") for t in ts[::max(1, len(ts)//50)]]
    
    if side == "bid":
        prices = df[[f"bp{i}" for i in range(1,6)]].values.T
        qtys = df[[f"bq{i}" for i in range(1,6)]].values.T
        colorscale = "Greens"
        title = "Bid Depth (L1-L5)"
    else:
        prices = df[[f"sp{i}" for i in range(1,6)]].values.T
        qtys = df[[f"sq{i}" for i in range(1,6)]].values.T
        colorscale = "Reds"
        title = "Ask Depth (L1-L5)"
    
    # Downsample for performance
    step = max(1, len(ts) // 200)
    prices_ds = prices[:, ::step]
    qtys_ds = qtys[:, ::step]
    ts_ds = ts[::step]
    
    return {
        "data": [{
            "type": "heatmap",
            "z": qtys_ds.tolist(),
            "x": [datetime.fromtimestamp(t).strftime("%H:%M") for t in ts_ds],
            "y": [f"L{i+1}" for i in range(5)],
            "colorscale": colorscale,
            "colorbar": {"title": "Qty"},
            "hovertemplate": "Level: %{y}<br>Time: %{x}<br>Qty: %{z}<extra></extra>",
        }],
        "layout": {
            "title": title,
            "xaxis": {"title": "Time"},
            "yaxis": {"title": "Level", "autorange": "reversed"},
            "height": 500,
            "margin": {"l": 60, "r": 40, "t": 40, "b": 40},
            "plot_bgcolor": "#fbfcfd",
            "paper_bgcolor": "#fbfcfd",
        }
    }

def create_microstructure_dist(micro: dict) -> dict:
    """Microstructure Distributions (OIB, Micro Dev, Toxicity, Queue Decay)"""
    if not micro:
        return {"data": [], "layout": {"title": "No Data"}}
    
    oimb = micro.get("oimb", [])
    micro_dev = micro.get("micro_dev", [])
    toxicity = micro.get("toxicity", [])
    bid_decay = micro.get("bid_queue_decay", [])
    ask_decay = micro.get("ask_queue_decay", [])
    
    traces = []
    if len(oimb) > 0:
        traces.append({"type": "histogram", "x": oimb.tolist(), "name": "OIB", "nbinsx": 50, "opacity": 0.7, "marker": {"color": "#1f5f8b"}})
    if len(micro_dev) > 0:
        traces.append({"type": "histogram", "x": micro_dev.tolist(), "name": "Micro Dev", "nbinsx": 50, "opacity": 0.7, "marker": {"color": "#6e0b4f"}, "xaxis": "x2", "yaxis": "y2"})
    if len(toxicity) > 0:
        traces.append({"type": "histogram", "x": toxicity.tolist(), "name": "Toxicity", "nbinsx": 50, "opacity": 0.7, "marker": {"color": "#4b2a8a"}, "xaxis": "x3", "yaxis": "y3"})
    if len(bid_decay) > 0:
        traces.append({"type": "histogram", "x": bid_decay.tolist(), "name": "Bid Queue Decay", "nbinsx": 50, "opacity": 0.7, "marker": {"color": "#0b6e4f"}, "xaxis": "x4", "yaxis": "y4"})
    if len(ask_decay) > 0:
        traces.append({"type": "histogram", "x": ask_decay.tolist(), "name": "Ask Queue Decay", "nbinsx": 50, "opacity": 0.7, "marker": {"color": "#a15c00"}, "xaxis": "x5", "yaxis": "y5"})
    
    return {
        "data": traces,
        "layout": {
            "title": "Microstructure Distributions",
            "grid": {"rows": 3, "columns": 2, "pattern": "independent"},
            "height": 600,
            "margin": {"l": 50, "r": 20, "t": 40, "b": 40},
            "plot_bgcolor": "#fbfcfd",
            "paper_bgcolor": "#fbfcfd",
        }
    }

def create_auction_analysis(df: pd.DataFrame) -> dict:
    """Auction Imbalance (Pre-open / Close)"""
    if df.empty:
        return {"data": [], "layout": {"title": "No Data"}}
    
    ts = df["ts"].values / 1000.0
    lp = df["lp"].values
    
    # Calculate imbalance at each tick
    bq_sum = df[[f"bq{i}" for i in range(1,6)]].sum(axis=1).values
    sq_sum = df[[f"sq{i}" for i in range(1,6)]].sum(axis=1).values
    imb = (bq_sum - sq_sum) / np.maximum(bq_sum + sq_sum, 1.0)
    
    # Identify auction windows
    pre_open_mask = np.array([dtime(9,0) <= datetime.fromtimestamp(t).time() < dtime(9,15) for t in ts])
    close_auc_mask = np.array([dtime(15,30) <= datetime.fromtimestamp(t).time() < dtime(15,40) for t in ts])
    
    traces = []
    if np.any(pre_open_mask):
        traces.append({"type": "scatter", "x": ts[pre_open_mask].tolist(), "y": imb[pre_open_mask].tolist(), "mode": "lines", "name": "Pre-Open Imbalance", "line": {"color": "#1f7a4c"}})
    if np.any(close_auc_mask):
        traces.append({"type": "scatter", "x": ts[close_auc_mask].tolist(), "y": imb[close_auc_mask].tolist(), "mode": "lines", "name": "Close Auction Imbalance", "line": {"color": "#b42318"}})
    if not np.any(pre_open_mask) and not np.any(close_auc_mask):
        traces.append({"type": "scatter", "x": ts.tolist(), "y": imb.tolist(), "mode": "lines", "name": "Book Imbalance", "line": {"color": "#1f5f8b"}})
    
    return {
        "data": traces,
        "layout": {
            "title": "Auction Imbalance",
            "xaxis": {"title": "Time", "tickformat": "%H:%M:%S"},
            "yaxis": {"title": "Imbalance", "range": [-1, 1]},
            "height": 350,
            "margin": {"l": 60, "r": 20, "t": 40, "b": 40},
            "plot_bgcolor": "#fbfcfd",
            "paper_bgcolor": "#fbfcfd",
        }
    }

def run_research(symbol: str, target_date: str | None = None,
                  start: str | None = None, end: str | None = None,
                  all_time: bool = False) -> dict:
    """
    Unified entry point.
    - Single date: deep-dive raw L2 partitions (full microstructure charts).
    - Range / All-Time: use pre-aggregated daily snapshots (instant load).
      Returns aggregated microstructure distribution + summary stats.
    """
    symbol = symbol.upper()

    # ─── Range / All-Time path (pre-aggregated snapshots) ───
    if all_time or (start and end):
        try:
            df = load_daily_snapshots("vortex", start, end)
        except Exception as e:
            return {"error": f"Snapshot load failed: {e}"}

        if df.empty:
            return {"error": f"No aggregated snapshots for {symbol} in range"}
        if "symbol" in df.columns:
            df = df[df["symbol"].str.upper() == symbol]
        if df.empty:
            return {"error": f"No aggregated data for {symbol} in range"}

        # Build summary charts from aggregated per-day rows
        meta = {
            "symbol": symbol,
            "mode": "all-time" if all_time else "range",
            "range": f"{start or 'first'} → {end or 'last'}",
            "days": int(len(df)),
        }
        # Aggregate metrics across days
        agg_cols = [c for c in df.columns if c not in ("symbol", "date")]
        summary = {}
        for c in agg_cols:
            if pd.api.types.is_numeric_dtype(df[c]):
                summary[c] = {
                    "mean": float(df[c].mean()),
                    "min": float(df[c].min()),
                    "max": float(df[c].max()),
                    "last": float(df[c].iloc[-1]),
                }

        # Time-series of daily OIB / spread / whale
        import plotly.graph_objs as go
        if "date" in df.columns:
            x = df["date"].astype(str)
        else:
            x = list(range(len(df)))
        ts_charts = {}
        for metric, title in [("oib_mean", "Daily OIB Mean"), ("spread_mean", "Daily Spread Mean"),
                              ("whale_conc", "Daily Whale Concentration"), ("bid_queue_decay", "Daily Bid Queue Decay")]:
            if metric in df.columns:
                ts_charts[metric] = go.Figure(
                    data=[go.Scatter(x=x, y=df[metric], mode="lines+markers", line=dict(color="#0969da", width=2))],
                    layout=go.Layout(
                        title=title, height=320,
                        template="plotly_dark",
                        paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
                        font=dict(color="#c9d1d9"),
                        margin=dict(l=50, r=20, t=40, b=40),
                    ),
                ).to_dict()

        # ── Dashboard-compatible chart keys (range mode) ──
        # Dashboard renderVortexCharts() expects: volume_profile, bid_depth,
        # ask_depth, micro_dist, auction. Build these from aggregated daily rows
        # so the Research Mode UI renders correctly in range/all-time mode.
        def _ts_fig(y_col, title, color="#0969da"):
            if y_col not in df.columns:
                return None
            return go.Figure(
                data=[go.Scatter(x=x, y=df[y_col], mode="lines+markers",
                                 line=dict(color=color, width=2))],
                layout=go.Layout(
                    title=title, height=320,
                    template="plotly_dark",
                    paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
                    font=dict(color="#c9d1d9"),
                    margin=dict(l=50, r=20, t=40, b=40),
                ),
            ).to_dict()

        volume_profile = _ts_fig("oib_mean", "Daily Volume / OIB Profile", "#0969da")
        bid_depth = _ts_fig("bid_queue_decay_mean", "Daily Bid Queue Decay (L1-L5)", "#2ea043")
        ask_depth = _ts_fig("ask_queue_decay_mean", "Daily Ask Queue Decay (L1-L5)", "#d73a49")
        micro_dist = _ts_fig("whale_conc", "Daily Microstructure (Whale Conc.)", "#f0883e")
        auction = _ts_fig("spread_mean", "Daily Auction / Spread Mean", "#8957e5")

        return {
            "meta": meta,
            "summary": summary,
            "time_series": ts_charts,
            # Dashboard-compatible keys (range mode)
            "volume_profile": volume_profile,
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
            "micro_dist": micro_dist,
            "auction": auction,
        }

    # ─── Single-date deep-dive (raw partitions) ───
    target_date = target_date or datetime.now().strftime("%Y-%m-%d")
    universe = load_universe()
    tokens_cache = load_tokens_cache()
    token = get_token_for_symbol(symbol, universe, tokens_cache)
    if not token:
        return {"error": f"Token not found for {symbol}"}

    df = load_l2_data(symbol, target_date, token)
    if df.empty:
        return {"error": f"No L2 data for {symbol} on {target_date}"}

    micro = compute_microstructure(df)
    price_min, price_max = df["lp"].min(), df["lp"].max()
    time_min = datetime.fromtimestamp(df["ts"].min() / 1000).strftime("%H:%M:%S")
    time_max = datetime.fromtimestamp(df["ts"].max() / 1000).strftime("%H:%M:%S")
    meta = {
        "symbol": symbol,
        "date": target_date,
        "mode": "single",
        "ticks": int(len(df)),
        "timerange": f"{time_min} - {time_max}",
        "price_range": f"{price_min:.2f} - {price_max:.2f}",
    }
    return {
        "meta": meta,
        "volume_profile": create_volume_profile(df, micro),
        "bid_depth": create_depth_heatmap(df, "bid"),
        "ask_depth": create_depth_heatmap(df, "ask"),
        "micro_dist": create_microstructure_dist(micro),
        "auction": create_auction_analysis(df),
    }


def main():
    parser = argparse.ArgumentParser(description="ARGUS (Algorithmic Real-Time Granular Ultra-Microstructure Scalper) Research Analytics")
    parser.add_argument("symbol", help="Symbol to analyze (e.g., RELIANCE)")
    parser.add_argument("--date", default=None, help="Single date YYYY-MM-DD (deep-dive raw partitions)")
    parser.add_argument("--start", default=None, help="Range start YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="Range end YYYY-MM-DD")
    parser.add_argument("--all-time", action="store_true", help="Use all available aggregated snapshots")
    args = parser.parse_args()

    out = run_research(
        args.symbol,
        target_date=args.date,
        start=args.start,
        end=args.end,
        all_time=args.all_time,
    )
    print(json.dumps(out, default=lambda o: float(o) if isinstance(o, (np.floating, np.integer)) else o.tolist() if isinstance(o, np.ndarray) else str(o)))

if __name__ == "__main__":
    main()