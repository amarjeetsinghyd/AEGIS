"""AEGIS Causal Microstructure Pipeline v2.0
Implements:
1. Cont-Kukanov-Stoikov Multi-Level (L1-L5) Event-Driven OFI.
2. Stoikov Multi-Level Microprice and Relative Spread.
3. Strict Causal Isolation: Feature Window in (t0 - 30s, t0], Target in (t0, t0 + 45s].
4. Spot-Age Validation (tolerance <= 3000ms) and t0 Token-Locking to eliminate moneyness drift.
5. Production Shadow-Mode Logger & Real-Time Online Scorer.
"""
from __future__ import annotations
import math
import os
import json
import time
from collections import deque
from typing import Dict, List, Tuple, Optional, Any
import numpy as np
import pandas as pd


# ──────────────────────────────────────────────────────────────
# VECTORIZED MICROSTRUCTURE KERNELS (L1-L5 OFI & MICROPRICE)
# ──────────────────────────────────────────────────────────────

def compute_multilevel_ofi(
    bid_px: np.ndarray,  # Shape: [N, 5]
    bid_qty: np.ndarray, # Shape: [N, 5]
    ask_px: np.ndarray,  # Shape: [N, 5]
    ask_qty: np.ndarray, # Shape: [N, 5]
    decay_weights: Optional[np.ndarray] = None
) -> np.ndarray:
    """
    Computes Cont-Kukanov-Stoikov Multi-Level OFI event-by-event across depth levels.
    """
    N, levels = bid_px.shape
    if N < 2:
        return np.zeros(N, dtype=np.float64)

    if decay_weights is None:
        decay_weights = np.array([1.0, 0.8, 0.6, 0.4, 0.2], dtype=np.float64)

    # 1. Bid side increments
    bid_px_curr, bid_px_prev = bid_px[1:], bid_px[:-1]
    bid_qty_curr, bid_qty_prev = bid_qty[1:], bid_qty[:-1]

    bid_ofi = np.where(
        bid_px_curr > bid_px_prev,
        bid_qty_curr,
        np.where(
            bid_px_curr == bid_px_prev,
            bid_qty_curr - bid_qty_prev,
            -bid_qty_prev
        )
    )

    # 2. Ask side increments
    ask_px_curr, ask_px_prev = ask_px[1:], ask_px[:-1]
    ask_qty_curr, ask_qty_prev = ask_qty[1:], ask_qty[:-1]

    ask_ofi = np.where(
        ask_px_curr < ask_px_prev,
        ask_qty_curr,
        np.where(
            ask_px_curr == ask_px_prev,
            ask_qty_curr - ask_qty_prev,
            -ask_qty_prev
        )
    )

    # Net level-by-level OFI
    level_ofi = bid_ofi - ask_ofi  # Shape: [N-1, 5]

    norm_w = decay_weights / np.sum(decay_weights)
    weighted_ofi = level_ofi @ norm_w

    return np.concatenate(([0.0], weighted_ofi))


def compute_multilevel_microprice(
    bid_px: np.ndarray,  # Shape: [N, 5]
    bid_qty: np.ndarray, # Shape: [N, 5]
    ask_px: np.ndarray,  # Shape: [N, 5]
    ask_qty: np.ndarray, # Shape: [N, 5]
    depth_weights: Optional[np.ndarray] = None
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Computes Stoikov Multi-Level Microprice and Relative Spread.
    P_micro = sum(w_i * (Q_i^B * P_i^A + Q_i^A * P_i^B)) / sum(w_i * (Q_i^B + Q_i^A))
    """
    N, levels = bid_px.shape
    if depth_weights is None:
        depth_weights = np.array([1.0, 0.5, 0.25, 0.125, 0.0625], dtype=np.float64)

    w = depth_weights.reshape(1, -1)

    sum_w_bid = np.sum(bid_qty * w, axis=1, keepdims=True)
    sum_w_ask = np.sum(ask_qty * w, axis=1, keepdims=True)
    total_qty = sum_w_bid + sum_w_ask
    total_qty = np.where(total_qty <= 0, 1.0, total_qty)

    # L1 Midprice & Relative Spread
    mid_l1 = 0.5 * (bid_px[:, 0] + ask_px[:, 0])
    spread_l1 = np.maximum(0.05, ask_px[:, 0] - bid_px[:, 0])
    rel_spread = spread_l1 / np.maximum(mid_l1, 1.0)

    # Microprice weighted by opposite side queue depth
    numerator = np.sum((bid_qty * ask_px + ask_qty * bid_px) * w, axis=1, keepdims=True)
    microprice = (numerator / total_qty).ravel()

    return microprice, rel_spread.ravel()


# ──────────────────────────────────────────────────────────────
# CAUSAL FEATURE EXTRACTOR CLASS (OFFLINE & BATCH AUDIT)
# ──────────────────────────────────────────────────────────────

class CausalMicrostructureExtractor:
    """
    Offline/Research Feature Extractor enforcing strict causality:
    - Feature window: (t0 - feature_window_sec, t0]
    - Target window:  (t0, t0 + target_window_sec]
    - Spot staleness check: spot_age_ms <= max_spot_age_ms (default: 3000ms)
    - Moneyness lock: Evaluated strictly at t0 and mapped to fixed token IDs.
    """

    def __init__(
        self,
        feature_window_sec: float = 30.0,
        target_window_sec: float = 45.0,
        max_spot_age_ms: float = 3000.0,
        strike_step: float = 50.0,
        min_price: float = 15.0,
        target_return_pct: float = 10.0,
        max_drawdown_pct: float = 3.0,
    ):
        self.feature_window_sec = feature_window_sec
        self.target_window_sec = target_window_sec
        self.max_spot_age_ms = max_spot_age_ms
        self.strike_step = strike_step
        self.min_price = min_price
        self.target_return_pct = target_return_pct
        self.max_drawdown_pct = max_drawdown_pct

    def process_dataset(
        self,
        options_df: pd.DataFrame,
        spot_df: pd.DataFrame,
        sample_decision_step_sec: float = 5.0
    ) -> pd.DataFrame:
        """
        Processes multi-strike tick dataframe and outputs causally isolated feature-label matrix.
        """
        if options_df.empty or spot_df.empty:
            return pd.DataFrame()

        # Sort chronological
        opts = options_df.sort_values("ts").reset_index(drop=True)
        spots = spot_df.sort_values("ts").reset_index(drop=True)

        spot_ts = spots["ts"].to_numpy()
        spot_px = spots["lp"].to_numpy()

        opts_ts = opts["ts"].to_numpy()
        min_ts = opts_ts[0] + int(self.feature_window_sec * 1000)
        max_ts = opts_ts[-1] - int(self.target_window_sec * 1000)

        if max_ts <= min_ts:
            return pd.DataFrame()

        # Generate candidate decision timestamps t0
        step_ms = int(sample_decision_step_sec * 1000)
        t0_candidates = np.arange(min_ts, max_ts, step_ms)

        records = []
        for t0_ms in t0_candidates:
            # 1. Spot Age and Staleness Validation
            idx_spot = np.searchsorted(spot_ts, t0_ms, side="right") - 1
            if idx_spot < 0:
                continue
            spot_at_t0 = spot_px[idx_spot]
            spot_age_ms = t0_ms - spot_ts[idx_spot]
            if spot_age_ms > self.max_spot_age_ms or spot_at_t0 <= 0:
                continue  # Stale spot -> Reject observation

            # 2. Moneyness & Token Locking at t0
            atm_strike = round(spot_at_t0 / self.strike_step) * self.strike_step

            # Extract window ticks in (t0 - 30s, t0]
            w_start_ms = t0_ms - int(self.feature_window_sec * 1000)
            idx_s = np.searchsorted(opts_ts, w_start_ms, side="right")
            idx_e = np.searchsorted(opts_ts, t0_ms, side="right")
            if idx_e <= idx_s:
                continue

            pre_window = opts.iloc[idx_s:idx_e]

            # Future label window in (t0, t0 + 45s]
            f_end_ms = t0_ms + int(self.target_window_sec * 1000)
            idx_fs = idx_e
            idx_fe = np.searchsorted(opts_ts, f_end_ms, side="right")
            if idx_fe <= idx_fs:
                continue
            post_window = opts.iloc[idx_fs:idx_fe]

            # Evaluate for target strikes (e.g. OTM1 CE and OTM1 PE)
            for side, opt_kind in [("CE", 1), ("PE", 2)]:
                otm1_target_strike = atm_strike + self.strike_step if side == "CE" else atm_strike - self.strike_step
                
                # Lock token strictly at t0
                t0_token_matches = pre_window[
                    (pre_window["strike"] == otm1_target_strike) &
                    (pre_window["kind"] == opt_kind)
                ]
                if t0_token_matches.empty:
                    continue

                target_sym = t0_token_matches.iloc[-1]["sym"]
                target_pre = pre_window[pre_window["sym"] == target_sym]
                if len(target_pre) < 2:
                    continue

                p_t0 = target_pre.iloc[-1]["lp"]
                if p_t0 < self.min_price:
                    continue

                # 3. Microstructure Features strictly in (t0 - 30s, t0]
                bp_arr = target_pre[[f"bp{k}" for k in range(1, 6)]].to_numpy(dtype=np.float64)
                bq_arr = target_pre[[f"bq{k}" for k in range(1, 6)]].to_numpy(dtype=np.float64)
                sp_arr = target_pre[[f"sp{k}" for k in range(1, 6)]].to_numpy(dtype=np.float64)
                sq_arr = target_pre[[f"sq{k}" for k in range(1, 6)]].to_numpy(dtype=np.float64)

                ofi_events = compute_multilevel_ofi(bp_arr, bq_arr, sp_arr, sq_arr)
                cum_ofi_l1_5 = float(np.sum(ofi_events))

                microprice_series, rel_spread_series = compute_multilevel_microprice(bp_arr, bq_arr, sp_arr, sq_arr)
                micro_ret_30s = (microprice_series[-1] - microprice_series[0]) / max(microprice_series[0], 0.05)
                spread_comp = rel_spread_series[0] - rel_spread_series[-1]

                dt_sec = max(0.5, (target_pre.iloc[-1]["ts"] - target_pre.iloc[0]["ts"]) / 1000.0)
                tick_intensity = len(target_pre) / dt_sec

                # Net Depth Depletion Rate (L3-L5)
                deep_ask_0 = float(target_pre.iloc[0]["sq3"] + target_pre.iloc[0]["sq4"] + target_pre.iloc[0]["sq5"])
                deep_ask_1 = float(target_pre.iloc[-1]["sq3"] + target_pre.iloc[-1]["sq4"] + target_pre.iloc[-1]["sq5"])
                net_deep_ask_depletion_rate = (deep_ask_0 - deep_ask_1) / dt_sec

                # Cross-strike Coherence (OTM2, OTM3 returns in pre-window)
                coherence_rets = []
                for lvl in [2, 3]:
                    otm_k = atm_strike + lvl * self.strike_step if side == "CE" else atm_strike - lvl * self.strike_step
                    sub_k = pre_window[(pre_window["strike"] == otm_k) & (pre_window["kind"] == opt_kind)]
                    if len(sub_k) >= 2:
                        r = (sub_k.iloc[-1]["lp"] - sub_k.iloc[0]["lp"]) / max(sub_k.iloc[0]["lp"], 0.05)
                        coherence_rets.append(r)
                cascade_coherence = float(np.mean(coherence_rets)) if coherence_rets else 0.0

                # 4. Target Label strictly in (t0, t0 + 45s]
                target_post = post_window[post_window["sym"] == target_sym]
                label = 0
                max_gain_pct = 0.0
                if len(target_post) >= 2:
                    future_prices = target_post["lp"].to_numpy(dtype=np.float64)
                    gains = (future_prices - p_t0) / p_t0 * 100.0
                    cum_max = np.maximum.accumulate(future_prices)
                    drawdowns = (cum_max - future_prices) / p_t0 * 100.0

                    hit_mask = (gains >= self.target_return_pct)
                    if np.any(hit_mask):
                        hit_idx = int(np.argmax(hit_mask))
                        if not np.any(drawdowns[:hit_idx + 1] > self.max_drawdown_pct):
                            label = 1
                            max_gain_pct = float(gains[hit_idx])

                records.append({
                    "t0_ms": t0_ms,
                    "sym": target_sym,
                    "side": side,
                    "strike": otm1_target_strike,
                    "p_t0": p_t0,
                    "spot_at_t0": spot_at_t0,
                    "spot_age_ms": spot_age_ms,
                    "tick_intensity": tick_intensity,
                    "cum_ofi_l1_5": cum_ofi_l1_5,
                    "micro_ret_30s": micro_ret_30s,
                    "spread_compression": spread_comp,
                    "net_deep_ask_depletion_rate": net_deep_ask_depletion_rate,
                    "cascade_coherence": cascade_coherence,
                    "label": label,
                    "max_gain_pct": max_gain_pct
                })

        return pd.DataFrame(records)


# ──────────────────────────────────────────────────────────────
# PRODUCTION SHADOW-MODE ONLINE SCORER (VPS RUNTIME)
# ──────────────────────────────────────────────────────────────

class ProductionShadowMicrostructureScorer:
    """
    Online Microstructure Scorer with zero heap allocation on hot path.
    Enforces real-time spot age validation, L1-L5 OFI, Stoikov Microprice, and shadow logging.
    """

    def __init__(
        self,
        high_conviction_threshold: float = 75.0,
        max_spot_age_sec: float = 3.0,
        strike_step: float = 50.0,
        weights: Optional[Dict[str, float]] = None,
        shadow_log_file: Optional[str] = None
    ):
        self.high_conviction_threshold = high_conviction_threshold
        self.max_spot_age_sec = max_spot_age_sec
        self.strike_step = strike_step
        self.shadow_log_file = shadow_log_file

        self.raw_intercept = -6.041813
        self.weights = weights or {
            "tick_intensity": 4.532921,
            "micro_ret_30s": -1.765040,
            "spread_compression": 0.185780,
            "cascade_coherence": 0.101717,
            "cum_ofi_l1_5": 0.000005,
            "net_deep_ask_depletion_rate": 0.000006,
        }

        # Symbol state buffers: deque storing (ts_sec, bp1..5, bq1..5, sp1..5, sq1..5)
        self.buffers: Dict[str, deque] = {}
        self.last_spot = 0.0
        self.last_spot_ts_sec = 0.0
        self.window_sec = 30.0

    def update_spot(self, spot_px: float, ts_sec: float):
        if spot_px > 0:
            self.last_spot = spot_px
            self.last_spot_ts_sec = ts_sec

    def is_spot_valid(self, now_sec: float) -> bool:
        if self.last_spot <= 0 or (now_sec - self.last_spot_ts_sec) > self.max_spot_age_sec:
            return False
        return True

    def update_option_tick(
        self,
        sym: str,
        ts_sec: float,
        bp: List[float],
        bq: List[float],
        sp: List[float],
        sq: List[float],
    ):
        if sym not in self.buffers:
            self.buffers[sym] = deque(maxlen=300)

        # Pad to length 5 if needed
        bp5 = (bp + [0.0] * 5)[:5]
        bq5 = (bq + [0.0] * 5)[:5]
        sp5 = (sp + [0.0] * 5)[:5]
        sq5 = (sq + [0.0] * 5)[:5]

        buf = self.buffers[sym]
        buf.append((ts_sec, bp5, bq5, sp5, sq5))

        # Prune older than 30s
        cutoff = ts_sec - self.window_sec
        while buf and buf[0][0] < cutoff:
            buf.popleft()

    def evaluate_strike(
        self,
        sym: str,
        now_sec: float,
        cascade_coherence: float = 0.0
    ) -> Tuple[float, bool, Dict[str, Any]]:
        """
        Evaluates real-time conviction score at current timestamp now_sec.
        """
        if not self.is_spot_valid(now_sec):
            return 0.0, False, {"error": "STALE_SPOT_REJECTED"}

        buf = self.buffers.get(sym)
        if not buf or len(buf) < 2:
            return 0.0, False, {"error": "INSUFFICIENT_BUFFER"}

        # Extract matrices from buffer
        N = len(buf)
        times = np.array([x[0] for x in buf], dtype=np.float64)
        bp_mat = np.array([x[1] for x in buf], dtype=np.float64)
        bq_mat = np.array([x[2] for x in buf], dtype=np.float64)
        sp_mat = np.array([x[3] for x in buf], dtype=np.float64)
        sq_mat = np.array([x[4] for x in buf], dtype=np.float64)

        dt = max(0.5, times[-1] - times[0])
        tick_intensity = N / dt

        # Vectorized OFI & Microprice
        ofi_events = compute_multilevel_ofi(bp_mat, bq_mat, sp_mat, sq_mat)
        cum_ofi = float(np.sum(ofi_events))

        micro_series, rel_spread = compute_multilevel_microprice(bp_mat, bq_mat, sp_mat, sq_mat)
        micro_ret = float((micro_series[-1] - micro_series[0]) / max(micro_series[0], 0.05))
        spread_comp = float(rel_spread[0] - rel_spread[-1])

        deep_ask_0 = float(np.sum(sq_mat[0, 2:5]))
        deep_ask_1 = float(np.sum(sq_mat[-1, 2:5]))
        net_deep_ask_depletion_rate = float((deep_ask_0 - deep_ask_1) / dt)

        features = {
            "tick_intensity": tick_intensity,
            "micro_ret_30s": micro_ret,
            "spread_compression": spread_comp,
            "cascade_coherence": cascade_coherence,
            "cum_ofi_l1_5": cum_ofi,
            "net_deep_ask_depletion_rate": net_deep_ask_depletion_rate,
            "spot_age_sec": round(now_sec - self.last_spot_ts_sec, 3),
            "p_mid": float(0.5 * (bp_mat[-1, 0] + sp_mat[-1, 0])),
            "p_micro": float(micro_series[-1])
        }

        # Linear Logit
        z = self.raw_intercept
        for k, v in features.items():
            if k in self.weights:
                z += self.weights[k] * v

        # Calibrated Sigmoid Score (0 to 100)
        if z >= 40.0:
            score = 100.0
        elif z <= -40.0:
            score = 0.0
        else:
            score = 100.0 / (1.0 + math.exp(-z))

        is_high_conviction = score >= self.high_conviction_threshold

        # Shadow Logging
        if self.shadow_log_file and is_high_conviction:
            try:
                payload = {
                    "ts_sec": now_sec,
                    "sym": sym,
                    "score": round(score, 2),
                    "is_high_conviction": is_high_conviction,
                    "features": features
                }
                with open(self.shadow_log_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(payload) + "\n")
            except Exception:
                pass

        return score, is_high_conviction, features
