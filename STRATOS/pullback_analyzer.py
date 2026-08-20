"""
pullback_analyzer.py — Pure Price Action & Market Structure Retest Engine for STRATOS.
Evaluates:
1. Long: Retracement into HH2 Support Floor with strict invalidation guard & lowest wick detection.
2. Short: Retracement up into LL2 Resistance Ceiling with strict invalidation guard & highest wick detection.
"""
from __future__ import annotations
import numpy as np
from STRATOS.config import STRATOS_CONFIG

class PullbackAnalyzer:
    def __init__(self, cfg: dict = None):
        self.cfg = cfg or STRATOS_CONFIG

    def analyze_pullback_quality(
        self,
        arrays: dict,
        level: float = None,
        breakout_vol: float = 1000.0,
        side: str = "BUY",
        support_level: float = None
    ) -> dict:
        """
        Pure Price Action Retest Validator:
        Verifies clean touch of the horizontal support/resistance floor/ceiling without invalidation breach.
        """
        if level is None:
            level = support_level if support_level is not None else 0.0

        closes = arrays.get("close", np.empty(0))
        highs = arrays.get("high", np.empty(0))
        lows = arrays.get("low", np.empty(0))
        volumes = arrays.get("volume", np.empty(0))

        n = len(closes)
        if n < 2:
            return {"valid": False, "reason": "Insufficient bars for retest check"}

        lookback = min(self.cfg.get("max_pullback_buckets", 12), n)
        pb_lows = lows[-lookback:]
        pb_highs = highs[-lookback:]
        pb_closes = closes[-lookback:]
        pb_vols = volumes[-lookback:] if len(volumes) > 0 else np.ones(lookback)

        avg_pb_vol = float(np.mean(pb_vols)) if len(pb_vols) > 0 else 1.0
        vol_ratio = avg_pb_vol / max(breakout_vol, 1.0) if breakout_vol > 0 else 0.5

        tol = self.cfg.get("retest_touch_tolerance_pct", 0.0025)
        inval_buf = self.cfg.get("strict_invalidation_buffer_pct", 0.0020)

        if side == "BUY":
            min_low = float(np.min(pb_lows))
            cur_close = pb_closes[-1]
            touched = (min_low <= level * (1.0 + tol))
            invalidated = (cur_close < level * (1.0 - inval_buf))

            if invalidated:
                return {"valid": False, "vol_ratio": vol_ratio, "reason": f"Support Breakdown! Close {cur_close:.2f} < Floor {level:.2f}"}
            if not touched:
                return {"valid": False, "vol_ratio": vol_ratio, "reason": f"Not in Support Zone (MinLow: {min_low:.2f} vs Floor: {level:.2f})"}

            return {
                "valid": True, "vol_ratio": vol_ratio, "lowest_wick": min_low,
                "reason": f"Clean Pure Price Action Support Retest @ {level:.2f}"
            }

        else: # SELL (Short)
            max_high = float(np.max(pb_highs))
            cur_close = pb_closes[-1]
            touched = (max_high >= level * (1.0 - tol))
            invalidated = (cur_close > level * (1.0 + inval_buf))

            if invalidated:
                return {"valid": False, "vol_ratio": vol_ratio, "reason": f"Resistance Breakout! Close {cur_close:.2f} > Ceiling {level:.2f}"}
            if not touched:
                return {"valid": False, "vol_ratio": vol_ratio, "reason": f"Not in Resistance Zone (MaxHigh: {max_high:.2f} vs Ceiling: {level:.2f})"}

            return {
                "valid": True, "vol_ratio": vol_ratio, "highest_wick": max_high,
                "reason": f"Clean Pure Price Action Resistance Retest @ {level:.2f}"
            }
