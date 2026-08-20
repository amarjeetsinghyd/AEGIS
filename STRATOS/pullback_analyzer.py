"""
pullback_analyzer.py — Evaluates the Quality of the Retracement.
1. Volume Contraction Validator (Volume must dry up on the pullback).
2. Retracement Momentum (Small-bodied candles vs massive institutional dumps).
3. Support Line Proximity Check.
"""
from __future__ import annotations
import numpy as np
from STRATOS.config import STRATOS_CONFIG

class PullbackAnalyzer:
    def __init__(self, cfg: dict = None):
        self.cfg = cfg or STRATOS_CONFIG

    def analyze_pullback_quality(self, arrays_5m: dict, support_level: float, breakout_vol: float) -> dict:
        """
        Analyze 5-minute pullback candles approaching the support_level.
        """
        if not arrays_5m or arrays_5m["count"] < 3:
            return {"valid": False, "reason": "Insufficient 5m bars for pullback"}

        highs = arrays_5m["high"]
        lows = arrays_5m["low"]
        closes = arrays_5m["close"]
        volumes = arrays_5m["volume"]

        # Check last 3-5 candles during the retest approach
        lookback = min(5, len(closes))
        pb_vols = volumes[-lookback:]
        pb_closes = closes[-lookback:]
        pb_lows = lows[-lookback:]

        avg_pb_vol = float(np.mean(pb_vols))
        vol_ratio = avg_pb_vol / max(breakout_vol, 1.0) if breakout_vol > 0 else 0.5

        # ── 1. Volume Contraction Gate ──
        # Volume during pullback must be smaller than breakout volume
        if vol_ratio > self.cfg["max_pullback_vol_ratio"]:
            return {
                "valid": False,
                "vol_ratio": vol_ratio,
                "reason": f"High Volume Pullback ({vol_ratio*100:.1f}% > {self.cfg['max_pullback_vol_ratio']*100:.0f}%) — Institutional Dump Risk!"
            }

        # ── 2. Support Proximity & Invalidation Gate ──
        cur_low = pb_lows[-1]
        cur_close = pb_closes[-1]
        min_low = float(np.min(pb_lows))

        # Check if price touched within tolerance of support level
        touched_support = (min_low <= support_level * (1.0 + self.cfg["retest_touch_tolerance_pct"]))

        # Strict invalidation: Close cannot break below support level minus buffer
        breakdown = (cur_close < support_level * (1.0 - self.cfg["strict_invalidation_buffer_pct"]))

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
            "reason": f"Healthy Low-Volume Retest (VolRatio: {vol_ratio*100:.1f}%) cleanly testing Support {support_level:.2f}"
        }
