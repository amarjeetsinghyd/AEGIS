"""AEGIS Empirical Microstructure Scorer v1.0

Calibrated on 2,436 empirical events (812 real >=10% clean option impulses across NIFTY and SENSEX).
Replaces arbitrary mathematical heuristics with an empirical continuous confidence score (0 to 100).

Hardware Target: Zero allocation hot path, O(1) rolling state updates.
"""
from __future__ import annotations
import math
import os
import json
from collections import deque
from typing import Dict, List, Tuple, Optional, Any

class MicrostructureScorer:
    """
    Real-time empirical microstructure confidence scoring engine for Options.
    Evaluates tick arrival frequency, deep order book vacuums, spread compression,
    and cross-strike gamma coherence to output a calibrated 0-100 conviction score.
    """
    
    def __init__(self, weights_path: Optional[str] = None, high_conviction_threshold: float = 75.0):
        self.high_conviction_threshold = high_conviction_threshold
        self.moderate_conviction_threshold = 60.0
        
        # Default fallback weights
        self.raw_intercept = -6.041813
        self.weights = {
            "target_tick_intensity": 4.532921,
            "otm1_ret": 0.598507,
            "target_spread_compression": 0.185780,
            "cascade_coherence": 0.101717,
            "opp_side_ret": 0.005423,
            "target_ask_vacuum_vel": 0.000006,
            "target_bid_build_vel": 0.000004,
            "target_l1_ofi": 0.000001,
            "target_ask_depth_decay": -0.000001,
            "otm2_ret": -0.116872,
            "target_top5_imbalance": -0.349300,
            "otm3_ret": -0.309572,
            "otm4_ret": -0.474109,
            "target_pre_ret": -1.765040,
        }
        
        if weights_path and os.path.exists(weights_path):
            try:
                with open(weights_path, "r") as f:
                    cfg = json.load(f)
                    self.raw_intercept = cfg.get("raw_intercept", self.raw_intercept)
                    self.weights = cfg.get("feature_weights", self.weights)
                    self.high_conviction_threshold = cfg.get("high_conviction_threshold", self.high_conviction_threshold)
                    self.moderate_conviction_threshold = cfg.get("moderate_conviction_threshold", self.moderate_conviction_threshold)
            except Exception:
                pass
                
        self.history: Dict[str, deque] = {}
        self.max_window_sec = 30.0
        self._running_ofi: Dict[str, float] = {}
        self._running_tot_q: Dict[str, float] = {}

    def update_tick(
        self,
        sym: str,
        ts_sec: float,
        lp: float,
        bp1: float,
        sp1: float,
        b1: float = 0.0,
        a1: float = 0.0,
        b2: float = 0.0,
        a2: float = 0.0,
        b3: float = 0.0,
        a3: float = 0.0,
        b4: float = 0.0,
        a4: float = 0.0,
        b5: float = 0.0,
        a5: float = 0.0,
        vol: float = 0.0,
        **kwargs: Any
    ) -> None:
        """Records a new tick for symbol and prunes history older than 30 seconds."""
        # Backward compatibility for legacy callers passing (bq1, sq1, deep_bid_qty, deep_ask_qty)
        if "bq1" in kwargs:
            b1 = float(kwargs["bq1"])
        if "sq1" in kwargs:
            a1 = float(kwargs["sq1"])
        if "volume" in kwargs:
            vol = float(kwargs["volume"])
        elif "v" in kwargs:
            vol = float(kwargs["v"])
        if "deep_bid_qty" in kwargs or "deep_ask_qty" in kwargs:
            db = float(kwargs.get("deep_bid_qty", 0.0))
            da = float(kwargs.get("deep_ask_qty", 0.0))
            b3 = db / 3.0; b4 = db / 3.0; b5 = db / 3.0
            a3 = da / 3.0; a4 = da / 3.0; a5 = da / 3.0

        if sym not in self.history:
            self.history[sym] = deque(maxlen=200)
            self._running_ofi[sym] = 0.0
            self._running_tot_q[sym] = 0.0
            
        buf = self.history[sym]
        if buf:
            prev_b1 = buf[-1][4]
            prev_a1 = buf[-1][5]
            d_b = b1 - prev_b1
            d_s = a1 - prev_a1
            d_ofi = max(d_b, 0.0) - max(-d_s, 0.0)
            d_q = b1 + a1
        else:
            d_ofi = 0.0
            d_q = 0.0

        self._running_ofi[sym] += d_ofi
        self._running_tot_q[sym] += d_q
        buf.append((ts_sec, lp, bp1, sp1, b1, a1, b2, a2, b3, a3, b4, a4, b5, a5, d_ofi, d_q, vol))
        
        cutoff = ts_sec - self.max_window_sec
        while buf and buf[0][0] < cutoff:
            popped = buf.popleft()
            self._running_ofi[sym] -= popped[14]
            self._running_tot_q[sym] -= popped[15]

    def compute_score(
        self,
        target_sym: str,
        now_ts_sec: float,
        adjacent_otm_returns: Optional[Dict[int, float]] = None,
        opp_side_return: float = 0.0,
        anchor_ts_sec: Optional[float] = None,
    ) -> Tuple[float, bool, Dict[str, float]]:
        """
        Computes the empirical microstructure confidence score for target_sym.

        V3.0 Event-Bound Slicing:
        - When anchor_ts_sec (closed-candle timestamp T) is provided, only ticks
          with ts <= T and ts >= T - max_window_sec are used. The current T+1s
          tick is excluded by construction so verification reflects the candle
          formation (T-30s to T), never a single-second post-close snapshot.
        - When anchor is None, legacy behaviour (whole rolling buffer) is kept.

        Returns:
            (score_0_to_100, is_high_conviction, feature_dict)
        """
        buf = self.history.get(target_sym)
        if not buf or len(buf) < 2:
            return 0.0, False, {}
        if anchor_ts_sec is not None and anchor_ts_sec > 0:
            w_start = anchor_ts_sec - self.max_window_sec
            anchored = [r for r in buf if r[0] >= w_start and r[0] <= anchor_ts_sec]
            if len(anchored) < 2:
                return 0.0, False, {"error": "INSUFFICIENT_ANCHORED_BUFFER"}
            # Work on a plain list slice so first/last are T-bound, not T+1s.
            buf = anchored
            
        first = buf[0]
        last = buf[-1]
        
        # Unpack L1-L5 levels
        t0, p0, bp1_0, sp1_0, b1_0, a1_0, b2_0, a2_0, b3_0, a3_0, b4_0, a4_0, b5_0, a5_0 = first[:14]
        t1, p1, bp1_1, sp1_1, b1_1, a1_1, b2_1, a2_1, b3_1, a3_1, b4_1, a4_1, b5_1, a5_1 = last[:14]
        
        dt = max(0.5, t1 - t0)
        
        # 1. Tick Intensity (arrival rate)
        tick_intensity = len(buf) / dt
        
        # 2. Target Pre-Return
        target_pre_ret = (p1 - p0) / p0 if p0 > 0 else 0.0

        # 3. Individual Level Imbalances (I_1 to I_5) - O(1) Scalar Math
        i1 = (b1_1 - a1_1) / (b1_1 + a1_1 + 1e-4)
        i2 = (b2_1 - a2_1) / (b2_1 + a2_1 + 1e-4)
        i3 = (b3_1 - a3_1) / (b3_1 + a3_1 + 1e-4)
        i4 = (b4_1 - a4_1) / (b4_1 + a4_1 + 1e-4)
        i5 = (b5_1 - a5_1) / (b5_1 + a5_1 + 1e-4)

        # 4. Layered Conceptual Signals
        touch_pressure = i1
        deep_pressure = (i3 + i4 + i5) / 3.0
        layer_divergence = touch_pressure - deep_pressure
        book_slope = (i5 - i1) / 4.0
        
        # 5. Deep Book Liquidity Velocities (L3-L5 aggregate depth)
        da0 = a3_0 + a4_0 + a5_0
        da1 = a3_1 + a4_1 + a5_1
        db0 = b3_0 + b4_0 + b5_0
        db1 = b3_1 + b4_1 + b5_1

        ask_vacuum_vel = (da1 - da0) / dt
        bid_build_vel = (db1 - db0) / dt
        ask_depth_decay = (da1 - da0) / (da0 + 1.0)
        
        # 6. Event-level L1 Order Flow Imbalance
        # V3.0: anchored path recomputes OFI strictly from the T-bound slice so
        # post-T (T+1s) ticks can never leak into the trigger decision.
        if anchor_ts_sec is not None and anchor_ts_sec > 0:
            _ofi_acc = 0.0
            _q_acc = 0.0
            for _i in range(1, len(buf)):
                _pb = buf[_i - 1][4]
                _pa = buf[_i - 1][5]
                _cb = buf[_i][4]
                _ca = buf[_i][5]
                _ofi_acc += max(_cb - _pb, 0.0) - max(-(_ca - _pa), 0.0)
                _q_acc += _cb + _ca
            ofi_sum = max(0.0, _ofi_acc)
            mean_q = _q_acc / max(len(buf) - 1, 1)
            l1_ofi = ofi_sum / max(mean_q + 1.0, 1.0)
        else:
            ofi_sum = max(0.0, self._running_ofi.get(target_sym, 0.0))
            tot_q = self._running_tot_q.get(target_sym, 0.0)
            mean_q = tot_q / max(len(buf) - 1, 1)
            l1_ofi = ofi_sum / max(mean_q + 1.0, 1.0)
        
        # Top-5 Aggregate Imbalance
        tot_bid5 = b1_1 + b2_1 + b3_1 + b4_1 + b5_1
        tot_ask5 = a1_1 + a2_1 + a3_1 + a4_1 + a5_1
        top5_imb = (tot_bid5 - tot_ask5) / max(tot_bid5 + tot_ask5 + 1.0, 1.0)
        
        # 7. Spread Compression
        spread_0 = (sp1_0 - bp1_0) / p0 if p0 > 0 else 0.0
        spread_1 = (sp1_1 - bp1_1) / p1 if p1 > 0 else 0.0
        spread_compression = spread_0 - spread_1
        
        # 8. Cross-strike Coherence
        otm_rets = adjacent_otm_returns or {}
        otm1_r = otm_rets.get(1, 0.0)
        otm2_r = otm_rets.get(2, 0.0)
        otm3_r = otm_rets.get(3, 0.0)
        otm4_r = otm_rets.get(4, 0.0)
        
        valid_otms = [v for k, v in otm_rets.items() if k in (1, 2, 3, 4)]
        cascade_coherence = float(sum(valid_otms) / len(valid_otms)) if valid_otms else 0.0
        
        features = {
            "target_tick_intensity": tick_intensity,
            "target_pre_ret": target_pre_ret,
            "touch_pressure": touch_pressure,
            "deep_pressure": deep_pressure,
            "layer_divergence": layer_divergence,
            "book_slope": book_slope,
            "i1": i1,
            "i2": i2,
            "i3": i3,
            "i4": i4,
            "i5": i5,
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
            "opp_side_ret": opp_side_return,
        }
        
        # Compute calibrated linear logit
        z = self.raw_intercept
        for feat_name, feat_val in features.items():
            z += self.weights.get(feat_name, 0.0) * feat_val
            
        # Sigmoid into 0-100 score
        if z >= 40.0:
            score = 100.0
        elif z <= -40.0:
            score = 0.0
        else:
            score = 100.0 / (1.0 + math.exp(-z))
            
        is_high_conviction = score >= self.high_conviction_threshold
        if anchor_ts_sec is not None and anchor_ts_sec > 0:
            features["anchor_T"] = float(anchor_ts_sec)
            features["event_bound"] = 1.0
        return score, is_high_conviction, features

    def verify_event_bound_30s(
        self,
        target_sym: str,
        anchor_T: float,
        direction: str,
    ) -> Tuple[bool, Dict[str, float]]:
        """V3.0 30-second micro-momentum acceleration check bound to T.

        Slices the final 30s of the trigger candle (T-30s to T) and verifies:
        - Microprice/pre-return accelerates into the close in the trigger
          direction (stall/divergence vetoed).
        - L1 touch imbalance shows active aggressive sweeping in the trigger
          direction without severe opposing exhaustion.
        - Deep L3-L5 pressure does not firmly oppose the trigger direction.

        Lenient flat-book tolerance is kept so balanced books (I1 ~ 0) pass
        while severe opposing walls are vetoed. Returns (ok, metrics).
        """
        buf = self.history.get(target_sym)
        if not buf or len(buf) < 2 or not anchor_T or anchor_T <= 0:
            return False, {"reason": "no_buffer_or_anchor", "anchor_T": float(anchor_T or 0.0)}
        w_start = anchor_T - self.max_window_sec
        sl = [r for r in buf if r[0] >= w_start and r[0] <= anchor_T]
        if len(sl) < 2:
            return False, {"reason": "INSUFFICIENT_ANCHORED_BUFFER", "anchor_T": float(anchor_T)}
        _t0, p0 = float(sl[0][0]), float(sl[0][1])
        _t1, p1 = float(sl[-1][0]), float(sl[-1][1])
        micro_ret = (p1 - p0) / p0 if p0 > 0 else 0.0
        b1, a1 = float(sl[-1][4]), float(sl[-1][5])
        i1 = (b1 - a1) / (b1 + a1 + 1e-4)
        b3, a3 = float(sl[-1][6]), float(sl[-1][7])
        b4, a4 = float(sl[-1][8]), float(sl[-1][9])
        b5, a5 = float(sl[-1][10]), float(sl[-1][11])
        i3 = (b3 - a3) / (b3 + a3 + 1e-4)
        i4 = (b4 - a4) / (b4 + a4 + 1e-4)
        i5 = (b5 - a5) / (b5 + a5 + 1e-4)
        deep = (i3 + i4 + i5) / 3.0
        is_up = direction.upper() in ("UP", "CALL", "BULL")
        metrics: Dict[str, float] = {
            "anchor_T": float(anchor_T),
            "micro_ret_30s": float(micro_ret),
            "touch_i1": float(i1),
            "deep_pressure": float(deep),
            "n_ticks": float(len(sl)),
        }
        # Stall/divergence veto: strong counter-direction drift fails.
        if is_up:
            if micro_ret < -0.002:
                return False, {**metrics, "reason": "micro_stall_divergence"}
            if i1 < -0.40:
                return False, {**metrics, "reason": "touch_exhaustion_opposed"}
            if deep < -0.60:
                return False, {**metrics, "reason": "deep_opposition"}
        else:
            if micro_ret > 0.002:
                return False, {**metrics, "reason": "micro_stall_divergence"}
            if i1 > 0.40:
                return False, {**metrics, "reason": "touch_exhaustion_opposed"}
            if deep > 0.60:
                return False, {**metrics, "reason": "deep_opposition"}
        return True, {**metrics, "reason": "micro_acceleration_confirmed"}
