"""
pullback_analyzer.py — High-Low Zone Retest Engine for STRATOS.
Validates if price retraces into the High-Low Support Zone (Long) or Resistance Zone (Short).
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
        support_level: float = None,
        zone: tuple[float, float] = None
    ) -> dict:
        """
        High-Low Support/Resistance Zone Validator:
        Verifies clean touch anywhere inside the structural High-Low zone without invalidation breach.
        """
        if level is None:
            level = support_level if support_level is not None else 0.0

        closes = arrays.get("close", np.empty(0))
        highs = arrays.get("high", np.empty(0))
        lows = arrays.get("low", np.empty(0))

        n = len(closes)
        if n < 2:
            return {"valid": False, "reason": "Insufficient bars for zone retest"}

        lookback = min(self.cfg.get("max_pullback_buckets", 12), n)
        pb_lows = lows[-lookback:]
        pb_highs = highs[-lookback:]
        pb_closes = closes[-lookback:]

        if side == "BUY":
            # Support Zone: [zone_low, zone_high]
            zone_low = zone[0] if zone else level * 0.9970
            zone_high = zone[1] if zone else level * 1.0025
            inval_floor = zone_low * 0.9980

            min_low = float(np.min(pb_lows))
            cur_close = pb_closes[-1]
            cur_low = pb_lows[-1]

            # Touched inside or below zone top
            touched_zone = (min_low <= zone_high)
            invalidated = (cur_close < inval_floor)

            if invalidated:
                return {"valid": False, "vol_ratio": 0.5, "reason": f"Zone Breakdown! Close {cur_close:.2f} < Floor {inval_floor:.2f}"}
            if not touched_zone:
                return {"valid": False, "vol_ratio": 0.5, "reason": f"Not in Support Zone [{zone_low:.2f} - {zone_high:.2f}]"}

            return {
                "valid": True, "vol_ratio": 0.5, "lowest_wick": min_low,
                "reason": f"Clean Retest in Support Zone [{zone_low:.2f} - {zone_high:.2f}]"
            }

        else: # SELL (Short)
            # Resistance Zone: [zone_low, zone_high]
            zone_low = zone[0] if zone else level * 0.9975
            zone_high = zone[1] if zone else level * 1.0030
            inval_ceiling = zone_high * 1.0020

            max_high = float(np.max(pb_highs))
            cur_close = pb_closes[-1]
            cur_high = pb_highs[-1]

            touched_zone = (max_high >= zone_low)
            invalidated = (cur_close > inval_ceiling)

            if invalidated:
                return {"valid": False, "vol_ratio": 0.5, "reason": f"Zone Breakout! Close {cur_close:.2f} > Ceiling {inval_ceiling:.2f}"}
            if not touched_zone:
                return {"valid": False, "vol_ratio": 0.5, "reason": f"Not in Resistance Zone [{zone_low:.2f} - {zone_high:.2f}]"}

            return {
                "valid": True, "vol_ratio": 0.5, "highest_wick": max_high,
                "reason": f"Clean Retest in Resistance Zone [{zone_low:.2f} - {zone_high:.2f}]"
            }
