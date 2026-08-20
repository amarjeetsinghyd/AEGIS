"""
End-of-Day (EOD) Automated ML Calibrator v6.0 (Reverse-Engineered Empirical Medians)
Extracts true physical distributions and median microstructure thresholds 
directly from real breakout events (+0.45% move without >0.20% adverse excursion) across 3-day rolling L2 raw data.
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
from numba import njit

# ─── BOOTSTRAP ───
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from common.config import get_data_dir
from common.logger import get_logger
from common.symbols import SUBSCRIPTION_UNIVERSE

logger = get_logger("eod_calibrator")
warnings.filterwarnings("ignore")

CALIB_OUTPUT = os.path.join(BASE_DIR, "ARGUS", "stock_calibration.json")


@njit(fastmath=True)
def compute_forward_excursions(lp, ts_ms, lookahead_ms=600000):
    """Fast C-level computation of forward 10-minute max up/down excursions."""
    n = len(lp)
    max_up = np.zeros(n, dtype=np.float32)
    max_dn = np.zeros(n, dtype=np.float32)
    
    j = 0
    for i in range(n):
        t_limit = ts_ms[i] + lookahead_ms
        while j < n and ts_ms[j] <= t_limit:
            j += 1
        
        # Sub-window is from i+1 to j
        if j > i + 1:
            p0 = lp[i]
            if p0 > 0:
                hi = p0
                lo = p0
                for k in range(i + 1, j):
                    p = lp[k]
                    if p > hi:
                        hi = p
                    if p < lo:
                        lo = p
                max_up[i] = (hi - p0) / p0 * 100.0
                max_dn[i] = (p0 - lo) / p0 * 100.0
                
    return max_up, max_dn


def find_l2_raw_files() -> tuple[list[str], dict[str, list[str]]]:
    """Search all candidate directories on VPS / local machine for L2 parquet files."""
    candidate_roots = [
        os.path.join(BASE_DIR, "ARGUS", "data", "l2_raw"),
        os.path.join(BASE_DIR, "AEGIS", "data", "l2_raw"),
        os.path.join(BASE_DIR, "data", "l2_raw"),
        os.path.join(BASE_DIR, "ARGUS", "calibration_notes", "l2_data"),
        os.path.join(get_data_dir(), "l2_raw"),
        "/home/opc/ARGUS/data/l2_raw",
        "/home/opc/AEGIS/data/l2_raw",
        "/home/opc/data/l2_raw",
    ]
    
    date_to_files = {}
    for root in candidate_roots:
        if not os.path.exists(root):
            continue
        for sub in os.listdir(root):
            sub_path = os.path.join(root, sub)
            if os.path.isdir(sub_path) and len(sub) == 10 and sub.startswith("20"):
                p_files = sorted(glob.glob(os.path.join(sub_path, "*.parquet")))
                if p_files:
                    date_to_files.setdefault(sub, []).extend(p_files)
        direct_files = sorted(glob.glob(os.path.join(root, "*.parquet")))
        for f in direct_files:
            bname = os.path.basename(f)
            for part in bname.split("_"):
                if len(part) == 10 and part.startswith("20"):
                    date_to_files.setdefault(part, []).append(f)
                    
    for dt in date_to_files:
        date_to_files[dt] = sorted(list(set(date_to_files[dt])))
        
    sorted_dates = sorted(date_to_files.keys())
    return sorted_dates, date_to_files


def run_eod_calibration():
    t0 = time.time()
    logger.info("=" * 80)
    logger.info("STARTING ARGUS EOD REVERSE-ENGINEERED CALIBRATOR v6.0 (PHYSICAL MEDIANS)")
    logger.info("=" * 80)
    
    sorted_dates, date_to_files = find_l2_raw_files()
    if not sorted_dates:
        logger.error("No L2 raw data directories or parquet files found!")
        return

    target_days = sorted_dates[-3:]
    logger.info(f"Using 3-Day Rolling L2 Data: {target_days}")

    tc_candidates = [
        os.path.join(BASE_DIR, "ARGUS", "calibration_notes", "l2_data", "tokens_cache.json"),
        os.path.join(BASE_DIR, "ARGUS", "data", "tokens_cache.json"),
        "/home/opc/ARGUS/data/tokens_cache.json",
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
    logger.info(f"Total Combined Ticks across 3 days: {len(full_raw_df):,}")

    calib_results = {}
    total_breakouts = 0

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
        
        # C-accelerated forward excursions
        max_up, max_dn = compute_forward_excursions(lp, ts_ms, lookahead_ms=600000)
        
        is_long_move = (max_up >= 0.45) & (max_dn <= 0.20)
        is_short_move = (max_dn >= 0.45) & (max_up <= 0.20)
        is_breakout = is_long_move | is_short_move
        n_breakouts = int(np.sum(is_breakout))
        total_breakouts += n_breakouts

        # ─── REVERSE-ENGINEERED EMPIRICAL MEDIANS AT BREAKOUT INCEPTION ───
        if n_breakouts >= 5:
            opt_branching = float(np.median(branching_proxy[is_breakout]))
            opt_whale_z   = float(np.median(whale_z[is_breakout]))
            mom_5m_min    = float(np.median(np.abs(mom_5m[is_breakout])))
            
            # Bound within robust operational envelopes
            opt_branching = float(np.clip(opt_branching, 0.10, 0.45))
            opt_whale_z   = float(np.clip(opt_whale_z, 1.20, 3.00))
            mom_5m_min    = float(np.clip(mom_5m_min, 0.08, 0.35))
            
            # Regime classification based on empirical spread & breakout density
            breakout_density = n_breakouts / len(g) * 1000.0  # per 1k ticks
            if median_spread_bps > 5.0 and breakout_density < 1.0:
                profile = "CHOPPY_NOISE_REJECT"
                conv_burst, conv_grind, min_ticks = 6.2, 5.8, 4
            elif breakout_density >= 4.0:
                profile = "TRENDING_UP_SURGE" if np.sum(is_long_move) > np.sum(is_short_move) else "TRENDING_DOWN_LIQUIDATION"
                conv_burst, conv_grind, min_ticks = 4.8, 4.5, 2
            else:
                profile = "BALANCED_TREND_GRINDER"
                conv_burst, conv_grind, min_ticks = 5.2, 4.8, 3
        else:
            # Conservative defaults for thin liquidity
            profile = "BALANCED_TREND_GRINDER"
            opt_branching = 0.15
            opt_whale_z   = 1.60
            mom_5m_min    = 0.12
            conv_burst, conv_grind, min_ticks = 5.2, 4.8, 3

        calib_results[sym] = {
            "profile": profile,
            "spread_median_bps": round(median_spread_bps, 2),
            "median_price": round(median_price, 2),
            "total_ticks_analyzed": len(g),
            "long_moves_observed": int(np.sum(is_long_move)),
            "short_moves_observed": int(np.sum(is_short_move)),
            "opt_branching": round(opt_branching, 3),
            "opt_whale_z": round(opt_whale_z, 2),
            "mom_5m_min_pct": round(mom_5m_min, 3),
            "conv_thresh_burst": conv_burst,
            "conv_thresh_grind": conv_grind,
            "conv_thresh_whale": 6.0,
            "conv_thresh_noise": 6.5,
            "min_ticks": min_ticks,
            "prior_alpha_beta": round(float(opt_branching * 20.0), 2),
            "prior_beta_beta": 20.0,
            "prior_alpha_W": round(float(opt_whale_z * 2.0), 2),
            "prior_beta_W": 10.0,
            "est_tp_pct": 0.005,
            "est_sl_pct": 0.0025
        }
        logger.info(f"[CALIB] {sym:12s} | Moves: {n_breakouts:4d} | B_median: {opt_branching:.3f} | Whale_Z: {opt_whale_z:.2f} | Mom5m: {mom_5m_min:.3f}% | Profile: {profile}")

    os.makedirs(os.path.dirname(CALIB_OUTPUT), exist_ok=True)
    with open(CALIB_OUTPUT, "w") as f:
        json.dump(calib_results, f, indent=2)
        
    elapsed = time.time() - t0
    logger.info("=" * 80)
    logger.info(f"CALIBRATION COMPLETE: {len(calib_results)} stocks calibrated | Total True Breakouts: {total_breakouts:,} | Time: {elapsed:.2f}s")
    logger.info(f"Saved to: {CALIB_OUTPUT}")
    logger.info("=" * 80)


def get_next_eod_time(now_dt: Optional[datetime] = None) -> datetime:
    """Returns the exact datetime for the next 16:00:00 IST EOD calibration run."""
    from common.market.market_calendar import is_trading_day
    from datetime import date, time as dt_time, timedelta
    
    if now_dt is None:
        now_dt = datetime.now()
    current_date = now_dt.date()
    eod_target_time = dt_time(16, 0, 0)
    
    if is_trading_day(current_date) and now_dt.time() < eod_target_time:
        return datetime.combine(current_date, eod_target_time)
        
    candidate_date = current_date + timedelta(days=1)
    while not is_trading_day(candidate_date):
        candidate_date += timedelta(days=1)
    return datetime.combine(candidate_date, eod_target_time)


def run_cron_service():
    """Runs EOD Calibrator as a long-lived service that triggers at 16:00:00 IST every trading day."""
    logger.info("EOD Calibrator Service Started. Target Trigger: 16:00:00 IST every trading day.")
    while True:
        now_dt = datetime.now()
        next_run = get_next_eod_time(now_dt)
        sleep_sec = (next_run - now_dt).total_seconds()
        hours = sleep_sec / 3600.0
        logger.info(f"EOD Calibrator sleeping for {hours:.2f} hours until {next_run.strftime('%Y-%m-%d %H:%M:%S')} IST...")
        
        target_ts = next_run.timestamp()
        while datetime.now().timestamp() < target_ts:
            remaining = target_ts - datetime.now().timestamp()
            if remaining <= 0:
                break
            time.sleep(min(60.0, remaining))
            
        logger.info("Triggering scheduled 16:00 IST EOD Calibration...")
        try:
            run_eod_calibration()
        except Exception as e:
            logger.error(f"Scheduled EOD calibration failed: {e}", exc_info=True)
            
        time.sleep(60.0)


if __name__ == "__main__":
    if "--cron" in sys.argv:
        run_cron_service()
    else:
        run_eod_calibration()
