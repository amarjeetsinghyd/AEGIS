"""
flip_trigger.py — High-Low Zone Reversal & Order Flow Trigger Engine for STRATOS.
Triggers:
1. Long: Rejection Wick (>=35%) or Bullish Close + Positive Delta inside/touching the Support Zone.
2. Short: Rejection Wick (>=35%) or Bearish Close + Negative Delta inside/touching the Resistance Zone.
"""
from __future__ import annotations
import numpy as np
from STRATOS.config import STRATOS_CONFIG

class FlipTriggerEngine:
    def __init__(self, cfg: dict = None):
        self.cfg = cfg or STRATOS_CONFIG

    def evaluate_trigger(
        self,
        arrays: dict,
        level: float = None,
        side: str = "BUY",
        live_order_flow: dict = None,
        support_level: float = None,
        zone: tuple[float, float] = None
    ) -> dict:
        if level is None:
            level = support_level if support_level is not None else 0.0

        opens = arrays.get("open", np.empty(0))
        highs = arrays.get("high", np.empty(0))
        lows = arrays.get("low", np.empty(0))
        closes = arrays.get("close", np.empty(0))
        deltas = arrays.get("delta", np.empty(0))

        n = len(closes)
        if n < 1:
            return {"triggered": False, "reason": "No bar data"}

        cur_open = opens[-1]
        cur_high = highs[-1]
        cur_low = lows[-1]
        cur_close = closes[-1]
        cur_delta = deltas[-1] if len(deltas) > 0 else 0.0
        bar_range = max(cur_high - cur_low, 1e-4)

        if side == "BUY":
            zone_low = zone[0] if zone else level * 0.9970
            zone_high = zone[1] if zone else level * 1.0025

            lower_wick = min(cur_open, cur_close) - cur_low
            lower_wick_ratio = lower_wick / bar_range
            is_bullish_close = cur_close >= cur_open
            is_rejection_wick = lower_wick_ratio >= self.cfg.get("min_rejection_wick_ratio", 0.35)
            has_action = is_bullish_close or is_rejection_wick
            is_delta_valid = cur_delta >= 0

            if live_order_flow and live_order_flow.get("delta", 0.0) > 0:
                is_delta_valid = True

            in_zone = (cur_low <= zone_high) and (cur_close >= zone_low * 0.9980)

            if in_zone and has_action and is_delta_valid:
                pattern = "BULLISH_BUCKET_CLOSE" if is_bullish_close else "ABSORPTION_REJECTION_WICK"
                return {
                    "triggered": True, "side": "BUY", "pattern": pattern, "delta": cur_delta,
                    "reason": f"Absorption @ Support Zone [{zone_low:.2f}-{zone_high:.2f}] | {pattern} | Delta: {cur_delta:+.0f}"
                }

        else: # SELL (Short)
            zone_low = zone[0] if zone else level * 0.9975
            zone_high = zone[1] if zone else level * 1.0030

            upper_wick = cur_high - max(cur_open, cur_close)
            upper_wick_ratio = upper_wick / bar_range
            is_bearish_close = cur_close <= cur_open
            is_rejection_wick = upper_wick_ratio >= self.cfg.get("min_rejection_wick_ratio", 0.35)
            has_action = is_bearish_close or is_rejection_wick
            is_delta_valid = cur_delta <= 0

            if live_order_flow and live_order_flow.get("delta", 0.0) < 0:
                is_delta_valid = True

            in_zone = (cur_high >= zone_low) and (cur_close <= zone_high * 1.0020)

            if in_zone and has_action and is_delta_valid:
                pattern = "BEARISH_BUCKET_CLOSE" if is_bearish_close else "ABSORPTION_UPPER_WICK"
                return {
                    "triggered": True, "side": "SELL", "pattern": pattern, "delta": cur_delta,
                    "reason": f"Rejection @ Resistance Zone [{zone_low:.2f}-{zone_high:.2f}] | {pattern} | Delta: {cur_delta:+.0f}"
                }

        return {"triggered": False, "reason": "Criteria unmet"}
