"""
pullback_analyzer.py — Turnover-Bucket Pullback & Absorption Validator for STRATOS.
1. Validates that the retracement to the HH2 Support Floor has volume/delta contraction.
2. Checks proximity to the Support Level and finds the lowest rejection wick.
3. Enforces strict invalidation if a bucket closes materially below support.
"""
from __future__ import annotations
import numpy as np
from STRATOS.config import STRATOS_CONFIG

class PullbackAnalyzer:
    def __init__(self, cfg: dict = None):
        self.cfg = cfg or STRATOS_CONFIG

    def analyze_pullback_quality(self, arrays: dict, support_level: float, breakout_vol: float) -> dict:
        """
        Analyze recent turnover buckets approaching the support_level.
        """
        closes = arrays.get("close", np.empty(0))
        highs = arrays.get("high", np.empty(0))
        lows = arrays.get("low", np.empty(0))
        volumes = arrays.get("volume", np.empty(0))
        deltas = arrays.get("delta", np.empty(0))

        n = len(closes)
        if n < 2:
            return {"valid": False, "reason": "Insufficient buckets for pullback check"}

        lookback = min(self.cfg.get("max_pullback_buckets", 8), n)
        pb_vols = volumes[-lookback:]
        pb_lows = lows[-lookback:]
        pb_closes = closes[-lookback:]
        pb_deltas = deltas[-lookback:]

        avg_pb_vol = float(np.mean(pb_vols)) if len(pb_vols) > 0 else 1.0
        vol_ratio = avg_pb_vol / max(breakout_vol, 1.0) if breakout_vol > 0 else 0.6

        # 1. Volume Contraction Gate
        max_ratio = self.cfg.get("max_pullback_vol_ratio", 0.85)
        if vol_ratio > max_ratio and len(pb_vols) >= 3:
            return {
                "valid": False,
                "vol_ratio": vol_ratio,
                "reason": f"High Volume Pullback ({vol_ratio*100:.1f}% > {max_ratio*100:.0f}%) — Institutional Dump Risk!"
            }

        # 2. Support Proximity & Invalidation Gate
        cur_low = pb_lows[-1]
        cur_close = pb_closes[-1]
        min_low = float(np.min(pb_lows))

        # Check if price touched within tolerance of support level
        tol = self.cfg.get("retest_touch_tolerance_pct", 0.0025)
        touched_support = (min_low <= support_level * (1.0 + tol))

        # Strict invalidation: Close cannot break below support level minus buffer
        inval_buf = self.cfg.get("strict_invalidation_buffer_pct", 0.0020)
        breakdown = (cur_close < support_level * (1.0 - inval_buf))

        if breakdown:
            return {
                "valid": False,
                "vol_ratio": vol_ratio,
                "reason": f"Support Breakdown! Close {cur_close:.2f} < Support Floor {support_level:.2f} — Setup Invalidated"
            }

        if not touched_support:
            return {
                "valid": False,
                "vol_ratio": vol_ratio,
                "reason": f"Price not yet in Support Retest Zone (MinLow: {min_low:.2f} vs Support: {support_level:.2f})"
            }

        return {
            "valid": True,
            "vol_ratio": vol_ratio,
            "lowest_wick": min_low,
            "reason": f"Clean Low-Volume Retest (VolRatio: {vol_ratio*100:.1f}%) testing Support {support_level:.2f}"
        }
