"""
End-of-Day (EOD) Automated ML Calibrator v5.0 (Nemotron + Universal Data Discovery)
Runs daily after market close (16:00 IST) to mathematically cluster regimes (GMM)
and calibrate per-stock microstructure thresholds using 3-day rolling L2 raw data.
"""
from __future__ import annotations
import os
import sys
import glob
import json
import time
from datetime import datetime
import numpy as np
import pandas as pd
import warnings
from sklearn.mixture import GaussianMixture
from sklearn.preprocessing import RobustScaler
from sklearn.metrics import precision_score, recall_score

# ─── BOOTSTRAP ───
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from common.config import get_data_dir
from common.logger import get_logger
from common.symbols import SUBSCRIPTION_UNIVERSE

logger = get_logger("eod_calibrator")
warnings.filterwarnings("ignore")

CALIB_OUTPUT = os.path.join(BASE_DIR, "stock_order_flow", "stock_calibration.json")

def find_l2_raw_files() -> tuple[list[str], dict[str, list[str]]]:
    """
    Search all candidate directories on VPS / local machine for L2 parquet files.
    Returns sorted date list and mapping: date_str -> list of parquet filepaths.
    """
    candidate_roots = [
        os.path.join(BASE_DIR, "AEGIS", "data", "l2_raw"),
        os.path.join(BASE_DIR, "data", "l2_raw"),
        os.path.join(BASE_DIR, "stock_order_flow", "data", "l2_raw"),
        os.path.join(BASE_DIR, "stock_order_flow", "calibration_notes", "l2_data"),
        os.path.join(get_data_dir(), "l2_raw"),
        "/home/opc/AEGIS/data/l2_raw",
        "/home/opc/stock_order_flow/data/l2_raw",
        "/home/opc/data/l2_raw",
    ]
    
    date_to_files = {}
    for root in candidate_roots:
        if not os.path.exists(root):
            continue
        # Check subdirectories YYYY-MM-DD
        for sub in os.listdir(root):
            sub_path = os.path.join(root, sub)
            if os.path.isdir(sub_path) and len(sub) == 10 and sub.startswith("20"):
                p_files = sorted(glob.glob(os.path.join(sub_path, "*.parquet")))
                if p_files:
                    date_to_files.setdefault(sub, []).extend(p_files)
        # Check direct parquet files with date in filename (e.g. aegis_l2_raw_2026-08-19_part_000.parquet)
        direct_files = sorted(glob.glob(os.path.join(root, "*.parquet")))
        for f in direct_files:
            bname = os.path.basename(f)
            # Find 10-char date
            for part in bname.split("_"):
                if len(part) == 10 and part.startswith("20"):
                    date_to_files.setdefault(part, []).append(f)
                    
    # Deduplicate and sort
    for dt in date_to_files:
        date_to_files[dt] = sorted(list(set(date_to_files[dt])))
        
    sorted_dates = sorted(date_to_files.keys())
    return sorted_dates, date_to_files

def run_eod_calibration():
    logger.info("=" * 80)
    logger.info("STARTING EOD 3-DAY ML STOCK-BY-STOCK CALIBRATION (GMM + MICROSTRUCTURE)")
    logger.info("=" * 80)
    
    sorted_dates, date_to_files = find_l2_raw_files()
    if not sorted_dates:
        logger.error("No L2 raw data directories or parquet files found!")
        return

    target_days = sorted_dates[-3:]
    logger.info(f"Using L2 Data for Days: {target_days}")

    tc_candidates = [
        os.path.join(BASE_DIR, "stock_order_flow", "calibration_notes", "l2_data", "tokens_cache.json"),
        os.path.join(BASE_DIR, "stock_order_flow", "data", "tokens_cache.json"),
        "/home/opc/stock_order_flow/data/tokens_cache.json",
        os.path.join(get_data_dir(), "tokens_cache.json"),
    ]
    tc_path = next((p for p in tc_candidates if os.path.exists(p)), None)
    if not tc_path:
        logger.error("Tokens cache missing!")
        return
        
    with open(tc_path, "r") as f:
        tc = json.load(f)
        
    tok_to_sym = {int(meta["token"]): sym for sym, meta in tc.items() if "token" in meta}
    tok_to_sym = {tok: sym for tok, sym in tok_to_sym.items() if sym in SUBSCRIPTION_UNIVERSE}

    all_day_dfs = []
    for dt_str in target_days:
        files = date_to_files.get(dt_str, [])
        if not files:
            continue
        dfs = [pd.read_parquet(f) for f in files]
        df_cat = pd.concat(dfs, ignore_index=True)
        df_cat["date"] = dt_str
        all_day_dfs.append(df_cat)
        logger.info(f"Loaded {dt_str}: {len(df_cat):,} ticks ({len(files)} partitions)")

    if not all_day_dfs:
        logger.error("No tick data loaded!")
        return

    full_raw_df = pd.concat(all_day_dfs, ignore_index=True)
    logger.info(f"Total Combined Ticks across {len(target_days)} days: {len(full_raw_df):,}")

    # ─── 1. DAILY SUMMARIES FOR GMM REGIME CLUSTERING ───
    daily_summaries = []
    for (tok, dt), g in full_raw_df.groupby(["tok", "date"]):
        if tok not in tok_to_sym or len(g) < 500:
            continue
        sym = tok_to_sym[tok]
        lp = g["lp"].values.astype(np.float64)
        ts_ms = g["ts"].values.astype(np.int64)
        vol = g["vol"].values.astype(np.float64)
        bp1 = g["bp1"].values.astype(np.float64)
        sp1 = g["sp1"].values.astype(np.float64)
        bq1 = g["bq1"].values.astype(np.float64)
        sq1 = g["sq1"].values.astype(np.float64)

        mid = (bp1 + sp1) / 2.0
        mid[mid == 0] = 1.0
        spread_bps = (sp1 - bp1) / mid * 10000.0

        weights = np.array([1.0, 0.5, 0.333, 0.25, 0.2])
        w_bid = bq1 * weights[0]
        w_ask = sq1 * weights[0]
        for i in range(2, 6):
            if f"bq{i}" in g.columns: w_bid += g[f"bq{i}"].values * weights[i-1]
            if f"sq{i}" in g.columns: w_ask += g[f"sq{i}"].values * weights[i-1]
        denom_obi = w_bid + w_ask
        denom_obi[denom_obi == 0] = 1.0
        obi_l5 = (w_bid - w_ask) / denom_obi

        d_vol = np.diff(np.insert(vol, 0, vol[0]))
        d_vol[d_vol < 0] = 0
        d_price = np.diff(np.insert(lp, 0, lp[0]))
        v_sum = np.sum(d_vol)
        kyle_lambda = (np.sum(np.abs(d_price)) / v_sum * 1e4) if v_sum > 0 else 0.0

        dt_sec = np.diff(np.insert(ts_ms, 0, ts_ms[0])) / 1000.0
        dt_sec[dt_sec <= 0] = 0.05
        intensity = 1.0 / dt_sec

        ret_day = (lp[-1] - lp[0]) / lp[0] * 100.0 if lp[0] > 0 else 0.0

        daily_summaries.append({
            "tok": tok,
            "symbol": sym,
            "date": dt,
            "avg_spread_bps": float(np.mean(spread_bps)),
            "spread_vol": float(np.std(spread_bps)),
            "total_volume": float(v_sum),
            "trade_intensity": float(np.mean(intensity)),
            "obi_mean": float(np.mean(obi_l5)),
            "kyle_lambda": float(kyle_lambda),
            "day_return_pct": float(ret_day),
            "tick_count": len(g)
        })

    df_daily = pd.DataFrame(daily_summaries)
    
    gmm_regimes = {}
    if len(df_daily) >= 10:
        feature_cols = ["avg_spread_bps", "spread_vol", "trade_intensity", "obi_mean", "kyle_lambda", "day_return_pct"]
        X_daily = df_daily[feature_cols].values
        scaler = RobustScaler()
        X_scaled = scaler.fit_transform(X_daily)
        
        gmm = GaussianMixture(n_components=min(4, len(df_daily)), covariance_type="full", random_state=42, n_init=5)
        df_daily["cluster"] = gmm.fit_predict(X_scaled)
        cluster_means = df_daily.groupby("cluster")[feature_cols].mean()

        regime_mapping = {}
        for c in cluster_means.index:
            row = cluster_means.loc[c]
            if row["avg_spread_bps"] > cluster_means["avg_spread_bps"].median() and row["trade_intensity"] < cluster_means["trade_intensity"].median():
                regime_mapping[c] = "CHOPPY_NOISE_REJECT"
            elif row["day_return_pct"] > 0.30:
                regime_mapping[c] = "TRENDING_UP_SURGE"
            elif row["day_return_pct"] < -0.30:
                regime_mapping[c] = "TRENDING_DOWN_LIQUIDATION"
            else:
                regime_mapping[c] = "QUIET_ACCUMULATION"

        df_daily["regime"] = df_daily["cluster"].map(regime_mapping)
        for sym, sub in df_daily.groupby("symbol"):
            latest = sub.sort_values("date").iloc[-1]
            gmm_regimes[sym] = latest["regime"]

    # ─── 2. PER-STOCK MICROSTRUCTURE CALIBRATION ───
    calib_results = {}
    for tok, g in full_raw_df.groupby("tok"):
        if tok not in tok_to_sym or len(g) < 1000:
            continue
        sym = tok_to_sym[tok]
        g = g.sort_values("ts").reset_index(drop=True)
        
        lp = g["lp"].values.astype(np.float64)
        ts_ms = g["ts"].values.astype(np.int64)
        vol = g["vol"].values.astype(np.float64)
        bp1 = g["bp1"].values.astype(np.float64)
        sp1 = g["sp1"].values.astype(np.float64)
        bq1 = g["bq1"].values.astype(np.float64)
        sq1 = g["sq1"].values.astype(np.float64)
        
        mid = (bp1 + sp1) / 2.0
        mid[mid == 0] = 1.0
        spread_bps = (sp1 - bp1) / mid * 10000.0
        median_spread_bps = float(np.median(spread_bps))
        median_price = float(np.median(lp))
        
        # 5-min Momentum
        idx_5m = np.searchsorted(ts_ms, ts_ms - 300000)
        idx_5m = np.clip(idx_5m, 0, len(lp) - 1)
        p_5m = lp[idx_5m]
        mom_5m = np.where(p_5m > 0, (lp - p_5m) / p_5m * 100.0, 0.0)
        
        # Volume Delta & Whale Z-Score
        dvol = np.diff(np.insert(vol, 0, vol[0]))
        dvol[dvol < 0] = 0
        v_roll_mean = pd.Series(dvol).rolling(100, min_periods=1).mean().values.copy()
        v_roll_std = pd.Series(dvol).rolling(100, min_periods=1).std().fillna(1.0).values.copy()
        v_roll_std[v_roll_std == 0] = 1.0
        whale_z = (dvol - v_roll_mean) / v_roll_std
        
        # Hawkes Branching Intensity Proxy
        dt_arr = np.diff(np.insert(ts_ms, 0, ts_ms[0])) / 1000.0
        dt_arr[dt_arr <= 0] = 0.05
        intensity = 1.0 / dt_arr
        intensity_mean = pd.Series(intensity).rolling(100, min_periods=1).mean().values.copy()
        intensity_mean[intensity_mean == 0] = 1.0
        branching_proxy = np.clip((intensity - intensity_mean) / intensity_mean, 0.0, 2.0)
        
        # Forward 10m Excursions
        idx_fwd = np.searchsorted(ts_ms, ts_ms + 600000)
        n_pts = len(lp)
        max_up = np.zeros(n_pts, dtype=np.float32)
        max_dn = np.zeros(n_pts, dtype=np.float32)
        
        for i in range(0, n_pts - 20):
            fw_end = min(idx_fwd[i], n_pts)
            if fw_end > i + 1:
                sub = lp[i+1 : fw_end]
                max_up[i] = (np.max(sub) - lp[i]) / lp[i] * 100.0
                max_dn[i] = (lp[i] - np.min(sub)) / lp[i] * 100.0
                
        is_long_move = (max_up >= 0.45) & (max_dn <= 0.20)
        is_short_move = (max_dn >= 0.45) & (max_up <= 0.20)
        is_either_move = (is_long_move | is_short_move).astype(int)
        n_total_moves = int(np.sum(is_either_move))
        
        # ML Grid-Search Calibration
        best_f1 = -1.0
        best_opt = {"opt_branching": 0.12, "opt_whale_z": 1.50, "mom_5m_min": 0.10}
        
        for b_th in [0.08, 0.12, 0.18, 0.25]:
            for w_th in [0.80, 1.20, 1.80, 2.50]:
                for m_th in [0.06, 0.10, 0.15, 0.22]:
                    pred_signal = (branching_proxy >= b_th) & (whale_z >= w_th) & (np.abs(mom_5m) >= m_th)
                    n_sig = int(np.sum(pred_signal))
                    if n_sig >= 5 and n_sig <= max(30, n_total_moves * 3):
                        prec = precision_score(is_either_move, pred_signal.astype(int), zero_division=0)
                        rec = recall_score(is_either_move, pred_signal.astype(int), zero_division=0)
                        f1 = 2 * (prec * rec) / (prec + rec) if (prec + rec) > 0 else 0
                        if f1 > best_f1:
                            best_f1 = f1
                            best_opt["opt_branching"] = b_th
                            best_opt["opt_whale_z"] = w_th
                            best_opt["mom_5m_min"] = m_th

        gmm_reg = gmm_regimes.get(sym, "BALANCED_TREND_GRINDER")
        if gmm_reg == "CHOPPY_NOISE_REJECT":
            conv_burst, conv_grind, min_ticks = 6.2, 5.8, 4
        elif gmm_reg in ("TRENDING_UP_SURGE", "TRENDING_DOWN_LIQUIDATION"):
            conv_burst, conv_grind, min_ticks = 4.8, 4.5, 2
        else:
            conv_burst, conv_grind, min_ticks = 5.2, 4.8, 3

        calib_results[sym] = {
            "profile": gmm_reg,
            "spread_median_bps": round(median_spread_bps, 2),
            "median_price": round(median_price, 2),
            "total_ticks_analyzed": len(g),
            "long_moves_observed": int(np.sum(is_long_move)),
            "short_moves_observed": int(np.sum(is_short_move)),
            "opt_branching": best_opt["opt_branching"],
            "opt_whale_z": best_opt["opt_whale_z"],
            "mom_5m_min_pct": best_opt["mom_5m_min"],
            "conv_thresh_burst": conv_burst,
            "conv_thresh_grind": conv_grind,
            "conv_thresh_whale": 6.0,
            "conv_thresh_noise": 6.5,
            "min_ticks": min_ticks,
            "prior_alpha_beta": round(float(best_opt["opt_branching"] * 20.0), 2),
            "prior_beta_beta": 20.0,
            "prior_alpha_W": round(float(best_opt["opt_whale_z"] * 2.0), 2),
            "prior_beta_W": 10.0,
            "est_tp_pct": 0.005,
            "est_sl_pct": 0.0025
        }

    # Merge with existing file (protect un-traded stocks)
    if os.path.exists(CALIB_OUTPUT):
        with open(CALIB_OUTPUT, "r") as f:
            existing = json.load(f)
        existing.update(calib_results)
        calib_results = existing

    with open(CALIB_OUTPUT, "w") as f:
        json.dump(calib_results, f, indent=2)

    logger.info(f"Calibration successful! Updated profile for {len(calib_results)} stocks.")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--cron":
        logger.info("EOD Calibrator starting in Cron mode. Will run daily at 16:00 IST.")
        while True:
            now = datetime.now()
            if now.hour == 16 and now.minute == 0:
                run_eod_calibration()
                time.sleep(60 * 60)
            time.sleep(30)
    else:
        run_eod_calibration()
