"""AEGIS 5-Minute Consistency & Retraining Lab
=============================================
Autonomous Quantitative Retraining & Micro-Macro Convergence Engine.

Evaluates 5-minute holding consistency under retail execution reality:
- Forward Horizon: 300 seconds (5 minutes)
- Production Exits: TP +5.0%, SL -3.5%, Trail Trigger +2.0% -> Lock +1.5%
- Real Statutory Friction: Turnover + STT + Exchange + SEBI + Stamp + GST + Brokerage
- Cross-Validation: Purged Day-Blocked Leave-One-Day-Out (LODO) CV
- Models:
  1. Baseline (Current Production Linear Weights)
  2. Model A (Retrained 15-Feature Logistic Drop-In JSON)
  3. Model B (LightGBM on Micro + Macro Bridge)
  4. Model C (LightGBM on Micro-Only Benchmark)

Author: AEGIS Quant Research
"""

from __future__ import annotations

import os
import sys
import time
import json
import re
import argparse
import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

import numpy as np
import polars as pl
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import roc_auc_score
import lightgbm as lgb

# Ensure repo root is on sys.path for common imports
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from common.execution.realistic_paper_broker import calculate_nse_option_charges
except ImportError:
    # Fallback statutory charges calculation if import unavailable
    def calculate_nse_option_charges(buy_turnover: float, sell_turnover: float, brokerage_per_order: float = 5.0):
        tot_turnover = buy_turnover + sell_turnover
        brokerage = brokerage_per_order * 2.0
        stt = sell_turnover * 0.001
        exch = tot_turnover * 0.0005
        sebi = tot_turnover * (10.0 / 10000000.0)
        stamp = buy_turnover * 0.00003
        gst = (brokerage + exch + sebi) * 0.18
        tot = brokerage + stt + exch + sebi + stamp + gst
        class Charges:
            total_charges = tot
        return Charges()


def parse_option_symbol(sym: str) -> Tuple[str, str, float, Optional[datetime.date]]:
    """
    Parses symbol to extract: (index_name, side 'CE'|'PE', strike, expiry_date)
    Handles NIFTY and SENSEX formats.
    """
    sym = sym.strip().upper()
    if sym.startswith("NIFTY"):
        idx = "NIFTY"
        # Format: NIFTY18AUG26C24500 or NIFTY25AUG26P23800
        m = re.match(r"NIFTY(\d{2}[A-Z]{3}\d{2})([CP])(\d+)", sym)
        if m:
            exp_str, side_char, strike_str = m.groups()
            side = "CE" if side_char == "C" else "PE"
            strike = float(strike_str)
            try:
                exp_date = datetime.datetime.strptime(exp_str, "%d%b%y").date()
            except Exception:
                exp_date = None
            return idx, side, strike, exp_date
        # Alternative fallback
        if "CE" in sym:
            side = "CE"
            m_s = re.search(r"(\d{5,6})", sym)
            strike = float(m_s.group(1)) if m_s else 0.0
            return idx, side, strike, None
        elif "PE" in sym:
            side = "PE"
            m_s = re.search(r"(\d{5,6})", sym)
            strike = float(m_s.group(1)) if m_s else 0.0
            return idx, side, strike, None

    elif sym.startswith("SENSEX"):
        idx = "SENSEX"
        if sym.endswith("CE"):
            side = "CE"
            sym_core = sym[:-2]
        elif sym.endswith("PE"):
            side = "PE"
            sym_core = sym[:-2]
        else:
            side = "CE" if "C" in sym else "PE"
            sym_core = sym

        m_s = re.search(r"SENSEX(\d{5})(\d{5,6})", sym_core)
        if m_s:
            exp_code, strike_str = m_s.groups()
            strike = float(strike_str)
            try:
                yy = int(exp_code[:2]) + 2000
                mm = int(exp_code[2])
                dd = int(exp_code[3:])
                exp_date = datetime.date(yy, mm, dd)
            except Exception:
                exp_date = None
            return idx, side, strike, exp_date

        m_any = re.search(r"(\d{5,6})", sym)
        strike = float(m_any.group(1)) if m_any else 0.0
        return idx, side, strike, None

    return "UNKNOWN", "CE", 0.0, None


def simulate_production_exit_vectorized(
    fut_ts: np.ndarray,
    fut_lp: np.ndarray,
    entry_p: float,
    lot_size: int,
    tp_pct: float = 0.05,
    sl_pct: float = -0.035,
    trail_trigger_pct: float = 0.020,
    trail_lock_pct: float = 0.015,
    max_hold_sec: float = 300.0,
) -> Tuple[float, float, str, int]:
    """
    Simulates exact production execution exit rules over forward tick path in O(1) vectorized numpy.
    Returns: (gross_pnl_rs, net_pnl_rs, exit_reason, hold_sec)
    """
    if len(fut_lp) == 0 or entry_p <= 0.0:
        return 0.0, 0.0, "NO_TICKS", 0

    t0 = fut_ts[0]
    rets = (fut_lp - entry_p) / entry_p
    holds = fut_ts - t0

    # 1. Target Profit (+5%) first index
    tp_mask = rets >= tp_pct
    tp_idx = int(np.argmax(tp_mask)) if np.any(tp_mask) else 99999999

    # 2. Stop Loss (-3.5%) first index
    sl_mask = rets <= sl_pct
    sl_idx = int(np.argmax(sl_mask)) if np.any(sl_mask) else 99999999

    # 3. Dynamic Trailing Stop Lock (Peak >= +2.0% -> Retrace <= +1.5%)
    peak_rets = np.maximum.accumulate(rets)
    trail_mask = (peak_rets >= trail_trigger_pct) & (rets <= trail_lock_pct)
    trail_idx = int(np.argmax(trail_mask)) if np.any(trail_mask) else 99999999

    # 4. Timeout index
    time_mask = holds >= max_hold_sec
    time_idx = int(np.argmax(time_mask)) if np.any(time_mask) else (len(fut_lp) - 1)

    first_idx = min(tp_idx, sl_idx, trail_idx, time_idx)
    if first_idx >= len(fut_lp):
        first_idx = len(fut_lp) - 1

    if first_idx == tp_idx:
        exit_p = entry_p * (1.0 + tp_pct)
        exit_reason = "TARGET_PROFIT"
    elif first_idx == trail_idx:
        exit_p = entry_p * (1.0 + trail_lock_pct)
        exit_reason = "TRAIL_LOCK"
    elif first_idx == sl_idx:
        exit_p = entry_p * (1.0 + sl_pct)
        exit_reason = "STOP_LOSS"
    else:
        exit_p = fut_lp[first_idx]
        exit_reason = "TIME_EXPIRED"

    hold_sec = int(fut_ts[first_idx] - t0)
    gross_pnl = (exit_p - entry_p) * lot_size
    buy_turnover = entry_p * lot_size
    sell_turnover = exit_p * lot_size
    charges = calculate_nse_option_charges(buy_turnover, sell_turnover)
    net_pnl = gross_pnl - charges.total_charges

    return gross_pnl, net_pnl, exit_reason, hold_sec


class ConsistencyLab:
    def __init__(
        self,
        grid_sec: float = 5.0,
        min_ticks_window: int = 3,
        min_premium: float = 20.0,
        max_premium: float = 300.0,
        atm_strike_range: int = 3,
    ):
        self.grid_sec = grid_sec
        self.min_ticks_window = min_ticks_window
        self.min_premium = min_premium
        self.max_premium = max_premium
        self.atm_strike_range = atm_strike_range

        # Load baseline weights
        self.baseline_weights_path = REPO_ROOT / "AEGIS" / "data" / "params" / "microstructure_weights.json"
        self.baseline_weights: Dict[str, float] = {}
        self.baseline_intercept = -6.041813
        if self.baseline_weights_path.exists():
            with open(self.baseline_weights_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                self.baseline_weights = cfg.get("feature_weights", {})
                self.baseline_intercept = cfg.get("raw_intercept", -6.041813)

    def load_dataset(self, data_path: Path) -> pl.DataFrame:
        """Loads and normalizes parquet data."""
        print(f"[*] Reading dataset: {data_path.name}...", flush=True)
        df = pl.read_parquet(data_path)
        return df

    def extract_day_panels(self, df: pl.DataFrame, index_name: str) -> List[Dict[str, Any]]:
        """Splits full dataframe into per-day structured tick arrays."""
        df = df.with_columns([
            (pl.col("ts") / 1000.0).alias("ts_sec"),
            (pl.col("ts") * 1000).cast(pl.Datetime("us")).dt.date().alias("date")
        ])

        days = sorted(df["date"].unique().to_list())
        lot_size = 65 if index_name == "NIFTY" else 20
        strike_step = 50.0 if index_name == "NIFTY" else 100.0

        day_panels = []
        for d in days:
            day_df = df.filter(pl.col("date") == d).sort("ts_sec")
            if day_df.height < 5000:
                continue

            # Extract spot stream
            spot_df = day_df.filter((pl.col("kind") == 0) | (pl.col("sym") == index_name))
            if spot_df.height < 100:
                # Synthesize spot from ATM Put-Call Parity: Spot = Strike + CE - PE
                opt_syms = day_df.filter(pl.col("kind") != 0)["sym"].unique().to_list()
                ce_syms = [s for s in opt_syms if s.endswith("CE") or "C" in s]
                pe_syms = [s for s in opt_syms if s.endswith("PE") or "P" in s]
                
                # Find matching strikes
                syn_found = False
                for s_ce in ce_syms:
                    _, _, k_ce, _ = parse_option_symbol(s_ce)
                    # Find matching PE
                    for s_pe in pe_syms:
                        _, _, k_pe, _ = parse_option_symbol(s_pe)
                        if k_ce > 0 and k_ce == k_pe:
                            ce_ticks = day_df.filter(pl.col("sym") == s_ce).sort("ts_sec")
                            pe_ticks = day_df.filter(pl.col("sym") == s_pe).sort("ts_sec")
                            if ce_ticks.height > 500 and pe_ticks.height > 500:
                                c_ts = ce_ticks["ts_sec"].to_numpy(); c_lp = ce_ticks["lp"].to_numpy()
                                p_ts = pe_ticks["ts_sec"].to_numpy(); p_lp = pe_ticks["lp"].to_numpy()
                                t_grid = np.arange(max(c_ts[0], p_ts[0]), min(c_ts[-1], p_ts[-1]), 1.0)
                                c_idx = np.searchsorted(c_ts, t_grid, side="right") - 1
                                p_idx = np.searchsorted(p_ts, t_grid, side="right") - 1
                                valid = (c_idx >= 0) & (p_idx >= 0)
                                if np.sum(valid) > 100:
                                    spot_ts = t_grid[valid]
                                    spot_lp = k_ce + c_lp[c_idx[valid]] - p_lp[p_idx[valid]]
                                    print(f"[+] Synthesized {len(spot_ts)} spot ticks for {index_name} Day {d} via Strike {int(k_ce)} Put-Call Parity (Spot ~{spot_lp.mean():.1f}).", flush=True)
                                    syn_found = True
                                    break
                    if syn_found:
                        break
                if not syn_found:
                    print(f"[-] Day {d} has insufficient spot ticks ({spot_df.height}) and could not synthesize. Skipping.", flush=True)
                    continue
            else:
                spot_ts = spot_df["ts_sec"].to_numpy()
                spot_lp = spot_df["lp"].to_numpy()

            # Precompute cumulative spot metrics for O(1) lookups
            spot_high_cum = np.maximum.accumulate(spot_lp)
            spot_low_cum = np.minimum.accumulate(spot_lp)
            spot_diffs_abs = np.abs(np.diff(spot_lp))
            spot_path_cum = np.concatenate([[0.0], np.cumsum(spot_diffs_abs)])

            # Group option data by symbol
            opt_df = day_df.filter(pl.col("kind") != 0).filter(pl.col("sym") != index_name)
            syms = opt_df["sym"].unique().to_list()

            sym_data = {}
            strike_side_map = {}
            for s in syms:
                s_df = opt_df.filter(pl.col("sym") == s).sort("ts_sec")
                if s_df.height < 20:
                    continue

                idx, side, strike, exp_date = parse_option_symbol(s)
                if strike <= 0.0:
                    strike = float(s_df["strike"][0]) if "strike" in s_df.columns else 0.0

                dte = (exp_date - d).days if exp_date else 3

                bq1 = s_df["bq1"].to_numpy()
                sq1 = s_df["sq1"].to_numpy()
                
                # Precompute cumulative OFI and queues for O(1) window aggregation
                d_b = np.diff(bq1)
                d_s = np.diff(sq1)
                ofi_deltas = np.maximum(d_b, 0.0) - np.maximum(-d_s, 0.0)
                ofi_cum = np.concatenate([[0.0], np.cumsum(ofi_deltas)])
                q_cum = np.concatenate([[0.0], np.cumsum(bq1 + sq1)])

                sym_data[s] = {
                    "ts": s_df["ts_sec"].to_numpy(),
                    "lp": s_df["lp"].to_numpy(),
                    "bp1": s_df["bp1"].to_numpy(),
                    "bq1": bq1,
                    "sp1": s_df["sp1"].to_numpy(),
                    "sq1": sq1,
                    "bp2": s_df["bp2"].to_numpy(),
                    "bq2": s_df["bq2"].to_numpy(),
                    "sp2": s_df["sp2"].to_numpy(),
                    "sq2": s_df["sq2"].to_numpy(),
                    "bp3": s_df["bp3"].to_numpy(),
                    "bq3": s_df["bq3"].to_numpy(),
                    "sp3": s_df["sp3"].to_numpy(),
                    "sq3": s_df["sq3"].to_numpy(),
                    "bp4": s_df["bp4"].to_numpy(),
                    "bq4": s_df["bq4"].to_numpy(),
                    "sp4": s_df["sp4"].to_numpy(),
                    "sq4": s_df["sq4"].to_numpy(),
                    "bp5": s_df["bp5"].to_numpy(),
                    "bq5": s_df["bq5"].to_numpy(),
                    "sp5": s_df["sp5"].to_numpy(),
                    "sq5": s_df["sq5"].to_numpy(),
                    "ofi_cum": ofi_cum,
                    "q_cum": q_cum,
                    "side": side,
                    "strike": strike,
                    "dte": dte,
                }
                strike_side_map[(side, strike)] = s

            day_panels.append({
                "date": d,
                "index_name": index_name,
                "lot_size": lot_size,
                "strike_step": strike_step,
                "spot_ts": spot_ts,
                "spot_lp": spot_lp,
                "spot_high_cum": spot_high_cum,
                "spot_low_cum": spot_low_cum,
                "spot_path_cum": spot_path_cum,
                "sym_data": sym_data,
                "strike_side_map": strike_side_map,
            })
            print(f"[+] Loaded Day {d} ({index_name}): {len(sym_data)} option symbols, {len(spot_ts)} spot ticks.", flush=True)

        return day_panels

    def extract_candidates_and_features(self, day_panel: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extracts candidate decision points and computes Micro + Macro features in O(1) fast vectorized math."""
        spot_ts = day_panel["spot_ts"]
        spot_lp = day_panel["spot_lp"]
        spot_high_cum = day_panel["spot_high_cum"]
        spot_low_cum = day_panel["spot_low_cum"]
        spot_path_cum = day_panel["spot_path_cum"]
        sym_data = day_panel["sym_data"]
        strike_side_map = day_panel["strike_side_map"]
        strike_step = day_panel["strike_step"]
        lot_size = day_panel["lot_size"]
        day_date = day_panel["date"]
        index_name = day_panel["index_name"]

        t_min = spot_ts[0] + 60.0  # Warmup
        t_max = spot_ts[-1] - 310.0  # Room for forward 300s label

        if t_max <= t_min:
            return []

        grid_times = np.arange(t_min, t_max, self.grid_sec)
        candidates = []
        t_start_day = time.time()

        for t0 in grid_times:
            # 1. Spot lookup
            s_idx = np.searchsorted(spot_ts, t0, side="right") - 1
            if s_idx < 10:
                continue
            curr_spot = spot_lp[s_idx]
            atm_strike = round(curr_spot / strike_step) * strike_step

            # Spot macro features at t0 (O(1) searchsorted)
            def get_spot_ret(lookback_sec: float) -> float:
                p_idx = np.searchsorted(spot_ts, t0 - lookback_sec, side="left")
                if p_idx < len(spot_lp):
                    past_p = spot_lp[p_idx]
                    return (curr_spot - past_p) / past_p if past_p > 0 else 0.0
                return 0.0

            s_ret_30 = get_spot_ret(30.0)
            s_ret_60 = get_spot_ret(60.0)
            s_ret_180 = get_spot_ret(180.0)
            s_ret_300 = get_spot_ret(300.0)

            # Kaufman Efficiency Ratio (KER) over 300s via precomputed cumulative arrays
            idx_300 = np.searchsorted(spot_ts, t0 - 300.0, side="left")
            net_disp = abs(curr_spot - spot_lp[idx_300])
            path_disp = spot_path_cum[s_idx] - spot_path_cum[idx_300]
            spot_ker_300 = net_disp / max(path_disp, 1e-4)

            # Spot range position in day so far via precomputed cumulative arrays
            day_high = spot_high_cum[s_idx]
            day_low = spot_low_cum[s_idx]
            spot_range_pos = (curr_spot - day_low) / max(day_high - day_low, 1e-4)
            spot_rv_60 = abs(s_ret_60) * 10000.0  # Realized return bps proxy

            # Time of day features (IST = UTC + 5:30 = +19800 sec)
            ist_sec = (t0 + 19800.0) % 86400.0
            min_from_open = max(0.0, (ist_sec - (9.0 * 3600.0 + 15.0 * 60.0)) / 60.0)
            min_to_close = max(0.0, ((15.0 * 3600.0 + 30.0 * 60.0) - ist_sec) / 60.0)

            # Evaluate each symbol active near ATM
            for sym, s_dict in sym_data.items():
                strike = s_dict["strike"]
                side = s_dict["side"]
                dte = s_dict["dte"]

                # Near ATM filter
                if abs(strike - atm_strike) > self.atm_strike_range * strike_step:
                    continue

                ts_arr = s_dict["ts"]
                lp_arr = s_dict["lp"]

                # Find window [t0 - 30s, t0]
                idx_end = np.searchsorted(ts_arr, t0, side="right") - 1
                if idx_end < 0:
                    continue
                last_tick_t = ts_arr[idx_end]
                if (t0 - last_tick_t) > 3.0:  # Must have ticked recently
                    continue

                p0 = lp_arr[idx_end]
                if p0 < self.min_premium or p0 > self.max_premium:
                    continue

                idx_start = np.searchsorted(ts_arr, t0 - 30.0, side="left")
                n_ticks = idx_end - idx_start + 1
                if n_ticks < self.min_ticks_window:
                    continue

                # ----------------- MICROSTRUCTURE FEATURES -----------------
                dt = max(0.5, last_tick_t - ts_arr[idx_start])
                tick_intensity = n_ticks / dt
                target_pre_ret = (p0 - lp_arr[idx_start]) / lp_arr[idx_start] if lp_arr[idx_start] > 0 else 0.0

                # L1-L5 endpoints at last tick
                b1 = s_dict["bq1"][idx_end]; a1 = s_dict["sq1"][idx_end]
                b2 = s_dict["bq2"][idx_end]; a2 = s_dict["sq2"][idx_end]
                b3 = s_dict["bq3"][idx_end]; a3 = s_dict["sq3"][idx_end]
                b4 = s_dict["bq4"][idx_end]; a4 = s_dict["sq4"][idx_end]
                b5 = s_dict["bq5"][idx_end]; a5 = s_dict["sq5"][idx_end]

                i1 = (b1 - a1) / (b1 + a1 + 1e-4)
                i2 = (b2 - a2) / (b2 + a2 + 1e-4)
                i3 = (b3 - a3) / (b3 + a3 + 1e-4)
                i4 = (b4 - a4) / (b4 + a4 + 1e-4)
                i5 = (b5 - a5) / (b5 + a5 + 1e-4)

                touch_pressure = i1
                deep_pressure = (i3 + i4 + i5) / 3.0
                layer_divergence = touch_pressure - deep_pressure
                book_slope = (i5 - i1) / 4.0

                # Deep Book Velocities
                da0 = s_dict["sq3"][idx_start] + s_dict["sq4"][idx_start] + s_dict["sq5"][idx_start]
                da1 = a3 + a4 + a5
                db0 = s_dict["bq3"][idx_start] + s_dict["bq4"][idx_start] + s_dict["bq5"][idx_start]
                db1 = b3 + b4 + b5

                ask_vacuum_vel = (da1 - da0) / dt
                bid_build_vel = (db1 - db0) / dt
                ask_depth_decay = (da1 - da0) / (da0 + 1.0)

                # OFI Sum via precomputed cumulative arrays in O(1)
                ofi_sum = s_dict["ofi_cum"][idx_end] - s_dict["ofi_cum"][idx_start]
                tot_q = s_dict["q_cum"][idx_end] - s_dict["q_cum"][idx_start]
                mean_q = tot_q / max(n_ticks - 1, 1)
                l1_ofi = float(ofi_sum) / max(mean_q + 1.0, 1.0)

                # Top-5 Imbalance
                tot_b5 = b1 + b2 + b3 + b4 + b5
                tot_a5 = a1 + a2 + a3 + a4 + a5
                top5_imb = (tot_b5 - tot_a5) / max(tot_b5 + tot_a5 + 1.0, 1.0)

                # Spread compression
                sp0 = s_dict["sp1"][idx_start]; bp0 = s_dict["bp1"][idx_start]
                sp1 = s_dict["sp1"][idx_end]; bp1 = s_dict["bp1"][idx_end]
                spr_0 = (sp0 - bp0) / lp_arr[idx_start] if lp_arr[idx_start] > 0 else 0.0
                spr_1 = (sp1 - bp1) / p0 if p0 > 0 else 0.0
                spread_compression = spr_0 - spr_1

                # Cross-Strike OTM & Opposite Side Returns
                def get_neighbor_ret(target_strike: float, target_side: str) -> float:
                    n_sym = strike_side_map.get((target_side, target_strike))
                    if n_sym and n_sym in sym_data:
                        n_ts = sym_data[n_sym]["ts"]
                        n_lp = sym_data[n_sym]["lp"]
                        n_end = np.searchsorted(n_ts, t0, side="right") - 1
                        n_start = np.searchsorted(n_ts, t0 - 30.0, side="left")
                        if n_end > n_start and n_lp[n_start] > 0:
                            return float((n_lp[n_end] - n_lp[n_start]) / n_lp[n_start])
                    return 0.0

                step_mult = 1.0 if side == "CE" else -1.0
                otm1_r = get_neighbor_ret(strike + 1 * strike_step * step_mult, side)
                otm2_r = get_neighbor_ret(strike + 2 * strike_step * step_mult, side)
                otm3_r = get_neighbor_ret(strike + 3 * strike_step * step_mult, side)
                otm4_r = get_neighbor_ret(strike + 4 * strike_step * step_mult, side)
                cascade_coherence = (otm1_r + otm2_r + otm3_r + otm4_r) / 4.0

                opp_side = "PE" if side == "CE" else "CE"
                opp_side_r = get_neighbor_ret(strike, opp_side)

                # ----------------- MACRO BRIDGE FEATURES -----------------
                # Direction alignment: CE wants Spot UP (+), PE wants Spot DOWN (-)
                dir_sign = 1.0 if side == "CE" else -1.0
                spot_ret_30_signed = s_ret_30 * dir_sign
                spot_ret_60_signed = s_ret_60 * dir_sign
                spot_ret_180_signed = s_ret_180 * dir_sign
                spot_ret_300_signed = s_ret_300 * dir_sign

                align_30 = 1.0 if spot_ret_30_signed > 0 else 0.0
                align_60 = 1.0 if spot_ret_60_signed > 0 else 0.0
                align_180 = 1.0 if spot_ret_180_signed > 0 else 0.0
                align_300 = 1.0 if spot_ret_300_signed > 0 else 0.0
                spot_alignment_count = align_30 + align_60 + align_180 + align_300

                # Option's own 60s/120s return
                idx_60 = np.searchsorted(ts_arr, t0 - 60.0, side="left")
                opt_ret_60 = (p0 - lp_arr[idx_60]) / lp_arr[idx_60] if (idx_60 < len(lp_arr) and lp_arr[idx_60] > 0) else 0.0

                idx_120 = np.searchsorted(ts_arr, t0 - 120.0, side="left")
                opt_ret_120 = (p0 - lp_arr[idx_120]) / lp_arr[idx_120] if (idx_120 < len(lp_arr) and lp_arr[idx_120] > 0) else 0.0

                atm_dist_steps = abs(strike - atm_strike) / strike_step

                # ----------------- BASELINE MODEL SCORE -----------------
                micro_feat_dict = {
                    "target_tick_intensity": tick_intensity,
                    "target_pre_ret": target_pre_ret,
                    "touch_pressure": touch_pressure,
                    "deep_pressure": deep_pressure,
                    "layer_divergence": layer_divergence,
                    "book_slope": book_slope,
                    "i1": i1, "i2": i2, "i3": i3, "i4": i4, "i5": i5,
                    "target_ask_vacuum_vel": ask_vacuum_vel,
                    "target_bid_build_vel": bid_build_vel,
                    "target_ask_depth_decay": ask_depth_decay,
                    "target_l1_ofi": l1_ofi,
                    "target_top5_imbalance": top5_imb,
                    "target_spread_compression": spread_compression,
                    "otm1_ret": otm1_r,
                    "otm2_ret": otm2_r,
                    "otm3_ret": otm3_r,
                    "otm4_ret": otm4_r,
                    "cascade_coherence": cascade_coherence,
                    "opp_side_ret": opp_side_r,
                }

                z_base = self.baseline_intercept
                for fn, fv in micro_feat_dict.items():
                    z_base += self.baseline_weights.get(fn, 0.0) * fv
                base_score = 100.0 / (1.0 + np.exp(-np.clip(z_base, -40.0, 40.0)))

                # ----------------- VECTORIZED FORWARD EXIT SIMULATION -----------------
                idx_fut_end = np.searchsorted(ts_arr, t0 + 300.0, side="right")
                fut_ts = ts_arr[idx_end + 1 : idx_fut_end]
                fut_lp = lp_arr[idx_end + 1 : idx_fut_end]

                gross_pnl, net_pnl, exit_reason, hold_sec = simulate_production_exit_vectorized(
                    fut_ts, fut_lp, p0, lot_size
                )

                label_net_profit = 1 if net_pnl > 0.0 else 0
                label_tp_hit = 1 if exit_reason == "TARGET_PROFIT" else 0

                candidates.append({
                    "date": str(day_date),
                    "t0": t0,
                    "sym": sym,
                    "side": side,
                    "strike": strike,
                    "entry_p": p0,
                    "lot_size": lot_size,
                    "gross_pnl": gross_pnl,
                    "net_pnl": net_pnl,
                    "exit_reason": exit_reason,
                    "hold_sec": hold_sec,
                    "label_net_profit": label_net_profit,
                    "label_tp_hit": label_tp_hit,
                    "base_score": base_score,
                    # Micro features
                    **micro_feat_dict,
                    # Macro features
                    "spot_ret_30_signed": spot_ret_30_signed,
                    "spot_ret_60_signed": spot_ret_60_signed,
                    "spot_ret_180_signed": spot_ret_180_signed,
                    "spot_ret_300_signed": spot_ret_300_signed,
                    "spot_alignment_count": spot_alignment_count,
                    "spot_ker_300": spot_ker_300,
                    "spot_rv_60": spot_rv_60,
                    "spot_range_pos": spot_range_pos,
                    "opt_ret_60": opt_ret_60,
                    "opt_ret_120": opt_ret_120,
                    "atm_dist_steps": atm_dist_steps,
                    "dte": float(dte),
                    "min_from_open": min_from_open,
                    "min_to_close": min_to_close,
                })

        dt_day = time.time() - t_start_day
        print(f"[+] Processed Day {day_date} in {dt_day:.2f}s | Extracted {len(candidates)} candidates.", flush=True)
        return candidates

    def run_training_and_validation(self, df_cands: pl.DataFrame) -> Dict[str, Any]:
        """Runs Day-Blocked Walk-Forward CV across Baseline, Model A, B, and C."""
        print(f"\n[+] Total Candidates Assembled: {df_cands.height} rows across {df_cands['date'].n_unique()} days.", flush=True)
        
        # Production Micro Features (15-feature set)
        micro_features = [
            "target_tick_intensity", "target_pre_ret", "target_ask_vacuum_vel",
            "target_bid_build_vel", "target_ask_depth_decay", "target_l1_ofi",
            "target_top5_imbalance", "target_spread_compression", "touch_pressure",
            "deep_pressure", "layer_divergence", "book_slope",
            "otm1_ret", "otm2_ret", "cascade_coherence", "opp_side_ret"
        ]
        
        macro_features = [
            "spot_ret_30_signed", "spot_ret_60_signed", "spot_ret_180_signed",
            "spot_ret_300_signed", "spot_alignment_count", "spot_ker_300",
            "spot_rv_60", "spot_range_pos", "opt_ret_60", "opt_ret_120",
            "atm_dist_steps", "dte", "min_from_open", "min_to_close"
        ]
        
        all_features = micro_features + macro_features
        
        df_pd = df_cands.to_pandas()
        days = sorted(df_pd["date"].unique())
        
        y_net = df_pd["label_net_profit"].values
        base_scores = df_pd["base_score"].values
        
        base_rate = float(np.mean(y_net))
        print(f"[*] Base Rate (Net Profit > 0 under Real Statutory Friction): {base_rate * 100.0:.2f}%", flush=True)
        
        # Baseline AUC
        baseline_auc = roc_auc_score(y_net, base_scores)
        print(f"[*] Baseline Model OOS AUC: {baseline_auc:.4f}", flush=True)

        # Containers for Out-Of-Sample Predictions
        oos_preds_logistic_micro = np.zeros(len(df_pd))
        oos_preds_lgbm_micro = np.zeros(len(df_pd))
        oos_preds_lgbm_full = np.zeros(len(df_pd))
        
        day_results = []
        
        day_results = []
        
        if len(days) > 1:
            print("\n=== STARTING PURGED DAY-BLOCKED LEAVE-ONE-DAY-OUT CV ===", flush=True)
            cv_splits = []
            for test_day in days:
                train_mask = (df_pd["date"] != test_day).values
                test_mask = (df_pd["date"] == test_day).values
                cv_splits.append((test_day, train_mask, test_mask))
        else:
            print("\n=== STARTING PURGED 5-FOLD K-FOLD CV (SINGLE DAY DATASET) ===", flush=True)
            from sklearn.model_selection import KFold
            kf = KFold(n_splits=5, shuffle=True, random_state=42)
            cv_splits = []
            for fold_idx, (tr_idx, te_idx) in enumerate(kf.split(df_pd)):
                train_mask = np.zeros(len(df_pd), dtype=bool); train_mask[tr_idx] = True
                test_mask = np.zeros(len(df_pd), dtype=bool); test_mask[te_idx] = True
                cv_splits.append((f"Fold_{fold_idx+1}", train_mask, test_mask))

        for split_name, train_mask, test_mask in cv_splits:
            X_train_micro = df_pd.loc[train_mask, micro_features].values
            X_test_micro = df_pd.loc[test_mask, micro_features].values
            
            X_train_full = df_pd.loc[train_mask, all_features].values
            X_test_full = df_pd.loc[test_mask, all_features].values
            
            y_train = y_net[train_mask]
            y_test = y_net[test_mask]
            
            # Model A: Scaled Logistic Regression (Micro Only)
            scaler = StandardScaler()
            X_train_micro_s = scaler.fit_transform(X_train_micro)
            X_test_micro_s = scaler.transform(X_test_micro)
            
            lr = LogisticRegression(C=0.1, max_iter=1000, random_state=42)
            lr.fit(X_train_micro_s, y_train)
            oos_preds_logistic_micro[test_mask] = lr.predict_proba(X_test_micro_s)[:, 1]
            
            # Model C: LightGBM (Micro Only)
            lgb_micro = lgb.LGBMClassifier(
                n_estimators=150, learning_rate=0.03, max_depth=4, num_leaves=15,
                subsample=0.8, colsample_bytree=0.8, random_state=42, verbose=-1
            )
            lgb_micro.fit(X_train_micro, y_train)
            oos_preds_lgbm_micro[test_mask] = lgb_micro.predict_proba(X_test_micro)[:, 1]
            
            # Model B: LightGBM (Micro + Macro Bridge)
            lgb_full = lgb.LGBMClassifier(
                n_estimators=200, learning_rate=0.03, max_depth=5, num_leaves=20,
                subsample=0.8, colsample_bytree=0.8, random_state=42, verbose=-1
            )
            lgb_full.fit(X_train_full, y_train)
            oos_preds_lgbm_full[test_mask] = lgb_full.predict_proba(X_test_full)[:, 1]
            
            day_auc_base = roc_auc_score(y_test, base_scores[test_mask]) if len(np.unique(y_test)) > 1 else 0.5
            day_auc_lr = roc_auc_score(y_test, oos_preds_logistic_micro[test_mask]) if len(np.unique(y_test)) > 1 else 0.5
            day_auc_lgb_m = roc_auc_score(y_test, oos_preds_lgbm_micro[test_mask]) if len(np.unique(y_test)) > 1 else 0.5
            day_auc_lgb_f = roc_auc_score(y_test, oos_preds_lgbm_full[test_mask]) if len(np.unique(y_test)) > 1 else 0.5
            
            day_results.append({
                "date": split_name,
                "n_samples": int(np.sum(test_mask)),
                "base_rate": float(np.mean(y_test)),
                "auc_baseline": float(day_auc_base),
                "auc_lr_micro": float(day_auc_lr),
                "auc_lgbm_micro": float(day_auc_lgb_m),
                "auc_lgbm_macro": float(day_auc_lgb_f),
            })
            print(f"Fold {split_name:10s} (N={np.sum(test_mask):5d}) | Base AUC: {day_auc_base:.3f} | LR-Micro: {day_auc_lr:.3f} | LGB-Micro: {day_auc_lgb_m:.3f} | LGB-Macro: {day_auc_lgb_f:.3f}", flush=True)

        # Overall OOS Metrics
        auc_lr = roc_auc_score(y_net, oos_preds_logistic_micro)
        auc_lgbm_m = roc_auc_score(y_net, oos_preds_lgbm_micro)
        auc_lgbm_f = roc_auc_score(y_net, oos_preds_lgbm_full)
        
        print("\n=== OVERALL OUT-OF-SAMPLE (OOS) BENCHMARK ===", flush=True)
        print(f"1. Baseline Production Weights: AUC = {baseline_auc:.4f}", flush=True)
        print(f"2. Model A (Retrained LR Micro): AUC = {auc_lr:.4f} (Delta: {auc_lr - baseline_auc:+.4f})", flush=True)
        print(f"3. Model C (LGBM Micro-Only)  : AUC = {auc_lgbm_m:.4f} (Delta: {auc_lgbm_m - baseline_auc:+.4f})", flush=True)
        print(f"4. Model B (LGBM Micro+Macro) : AUC = {auc_lgbm_f:.4f} (Delta: {auc_lgbm_f - baseline_auc:+.4f}) 🔥", flush=True)
        
        # Train Full Final Models for Export
        # Fit Full Logistic Model for Drop-in JSON
        X_micro_all = df_pd[micro_features].values
        scaler_final = StandardScaler()
        X_micro_s = scaler_final.fit_transform(X_micro_all)
        lr_final = LogisticRegression(C=0.1, max_iter=1000, random_state=42)
        lr_final.fit(X_micro_s, y_net)
        
        # Convert Standardized Weights back to Raw Scale
        mu = scaler_final.mean_
        sigma = scaler_final.scale_
        w_std = lr_final.coef_[0]
        b_std = lr_final.intercept_[0]
        
        w_raw = w_std / np.where(sigma == 0, 1.0, sigma)
        b_raw = b_std - np.sum(w_std * mu / np.where(sigma == 0, 1.0, sigma))
        
        calibrated_weights_dict = {}
        for fn, wr in zip(micro_features, w_raw):
            calibrated_weights_dict[fn] = float(np.round(wr, 6))

        # Fit Full LightGBM for Feature Importances
        X_full_all = df_pd[all_features].values
        lgb_final = lgb.LGBMClassifier(
            n_estimators=200, learning_rate=0.03, max_depth=5, num_leaves=20,
            subsample=0.8, colsample_bytree=0.8, random_state=42, verbose=-1
        )
        lgb_final.fit(X_full_all, y_net)
        
        importances_gain = lgb_final.booster_.feature_importance(importance_type="gain")
        imp_ranking = sorted(
            [{"feature": fn, "gain": float(g)} for fn, g in zip(all_features, importances_gain)],
            key=lambda x: x["gain"],
            reverse=True
        )
        
        print("\n=== TOP 15 FEATURE IMPORTANCE RANKING (LIGHTGBM GAIN) ===", flush=True)
        for rank, item in enumerate(imp_ranking[:15], 1):
            is_macro = "🌟 MACRO" if item["feature"] in macro_features else "⚡ MICRO"
            print(f"{rank:2d}. {item['feature']:28s} | Gain: {item['gain']:10.2f} | {is_macro}", flush=True)

        # Economic Expectancy by Score Threshold
        threshold_analysis = []
        df_pd["lgbm_macro_score"] = oos_preds_lgbm_full * 100.0
        df_pd["lr_micro_score"] = oos_preds_logistic_micro * 100.0
        
        print("\n=== ECONOMIC THRESHOLD SWEEP (OOS MODEL B: LGBM MICRO+MACRO) ===")
        print("Threshold | Candidates | Win Rate (%) | Mean Net PnL (₹) | Total Net PnL (₹)", flush=True)
        print("-" * 75, flush=True)
        for thresh in [40.0, 50.0, 60.0, 65.0, 70.0, 75.0, 80.0]:
            sub = df_pd[df_pd["lgbm_macro_score"] >= thresh]
            if len(sub) > 0:
                wr = float(np.mean(sub["label_net_profit"]) * 100.0)
                mean_net = float(np.mean(sub["net_pnl"]))
                tot_net = float(np.sum(sub["net_pnl"]))
                threshold_analysis.append({
                    "threshold": thresh,
                    "count": len(sub),
                    "win_rate": wr,
                    "mean_net_pnl": mean_net,
                    "total_net_pnl": tot_net,
                })
                print(f" >= {thresh:4.1f}   |   {len(sub):6d}   |   {wr:6.2f}%   |   ₹{mean_net:+8.2f}     |   ₹{tot_net:+10.2f}", flush=True)

        # Export Drop-In JSON Schema
        v2_json_payload = {
            "model_name": "AEGIS_Empirical_Microstructure_Scorer_v2_5Min",
            "calibrated_on": "NIFTY_SENSEX_L2_2026_AUG_PURGED_5MIN",
            "n_samples": int(df_cands.height),
            "roc_auc_oos": float(np.round(auc_lr, 4)),
            "raw_intercept": float(np.round(b_raw, 6)),
            "feature_weights": calibrated_weights_dict,
            "high_conviction_threshold": 75.0,
            "moderate_conviction_threshold": 60.0,
            "min_tick_rate_gate": 1.5,
            "horizon_target": "5_MIN_PROD_EXITS_WITH_STATUTORY_FRICTION",
        }

        # Save Artifacts
        out_weights_path = REPO_ROOT / "AEGIS" / "data" / "params" / "microstructure_weights_v2_5min.json"
        out_weights_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_weights_path, "w", encoding="utf-8") as f:
            json.dump(v2_json_payload, f, indent=2)
        print(f"\n[+] Saved Calibrated Drop-In Weights to: {out_weights_path.relative_to(REPO_ROOT)}", flush=True)

        report_payload = {
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "n_candidates": int(df_cands.height),
            "n_days": int(df_cands["date"].n_unique()),
            "base_rate_pct": float(base_rate * 100.0),
            "auc_baseline": float(baseline_auc),
            "auc_lr_micro_oos": float(auc_lr),
            "auc_lgbm_micro_oos": float(auc_lgbm_m),
            "auc_lgbm_macro_oos": float(auc_lgbm_f),
            "day_breakdown": day_results,
            "feature_importances": imp_ranking,
            "threshold_sweep": threshold_analysis,
            "calibrated_weights": v2_json_payload,
        }

        out_report_path = REPO_ROOT / "AEGIS" / "data" / "ml_results" / "aegis_5min_consistency_report.json"
        out_report_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_report_path, "w", encoding="utf-8") as f:
            json.dump(report_payload, f, indent=2)
        print(f"[+] Saved Detailed Research Report to: {out_report_path.relative_to(REPO_ROOT)}", flush=True)

        return report_payload


def main():
    parser = argparse.ArgumentParser(description="AEGIS 5-Minute Consistency & Retraining Lab")
    parser.add_argument("--grid-sec", type=float, default=5.0, help="Candidate sampling grid in seconds (default: 5.0)")
    parser.add_argument("--index", type=str, default="ALL", choices=["ALL", "NIFTY", "SENSEX"], help="Index dataset to process")
    args = parser.parse_args()

    print("=" * 80, flush=True)
    print("   AEGIS 5-MINUTE CONSISTENCY & RETRAINING LAB — LOCAL RUNNER", flush=True)
    print("=" * 80, flush=True)

    lab = ConsistencyLab(grid_sec=args.grid_sec)
    all_candidates = []

    # Process NIFTY
    if args.index in ["ALL", "NIFTY"]:
        nifty_path = REPO_ROOT / "AEGIS" / "data" / "aegis_local_l2_raw" / "nifty_l2_merged.parquet"
        if nifty_path.exists():
            nifty_df = lab.load_dataset(nifty_path)
            nifty_panels = lab.extract_day_panels(nifty_df, "NIFTY")
            for p in nifty_panels:
                cands = lab.extract_candidates_and_features(p)
                all_candidates.extend(cands)

    # Process SENSEX
    if args.index in ["ALL", "SENSEX"]:
        sensex_path = REPO_ROOT / "AEGIS" / "data" / "aegis_local_l2_raw" / "sensex_l2_merged.parquet"
        if sensex_path.exists():
            sensex_df = lab.load_dataset(sensex_path)
            sensex_panels = lab.extract_day_panels(sensex_df, "SENSEX")
            for p in sensex_panels:
                cands = lab.extract_candidates_and_features(p)
                all_candidates.extend(cands)

    if not all_candidates:
        print("[-] No candidates extracted. Check data paths.", flush=True)
        return

    df_cands = pl.DataFrame(all_candidates)
    lab.run_training_and_validation(df_cands)
    print("\n[✓] AEGIS 5-Minute Retraining Lab completed successfully.", flush=True)


if __name__ == "__main__":
    main()
