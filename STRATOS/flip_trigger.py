"""
flip_trigger.py — Turnover-Bucket Order Flow & Absorption Trigger Engine for STRATOS.
Fires when price touches or tests the HH2 Support Floor and exhibits:
1. Positive Cumulative Delta (Aggressive Buying / Absorption of Sell Orders).
2. Bullish Reversal Price Action on the Turnover Bucket (Close > Open or Rejection Wick).
3. Trend Confluence (Price maintaining above the structure support).
"""
from __future__ import annotations
import numpy as np
from STRATOS.config import STRATOS_CONFIG

class FlipTriggerEngine:
    def __init__(self, cfg: dict = None):
        self.cfg = cfg or STRATOS_CONFIG

    def evaluate_trigger(self, arrays: dict, support_level: float, live_order_flow: dict = None) -> dict:
        """
        Evaluate reversal trigger on completed turnover bucket at the Support Level.
        """
        opens = arrays.get("open", np.empty(0))
        highs = arrays.get("high", np.empty(0))
        lows = arrays.get("low", np.empty(0))
        closes = arrays.get("close", np.empty(0))
        volumes = arrays.get("volume", np.empty(0))
        deltas = arrays.get("delta", np.empty(0))

        n = len(closes)
        if n < 1:
            return {"triggered": False, "reason": "No bucket data"}

        cur_open = opens[-1]
        cur_high = highs[-1]
        cur_low = lows[-1]
        cur_close = closes[-1]
        cur_vol = volumes[-1] if len(volumes) > 0 else 1.0
        cur_delta = deltas[-1] if len(deltas) > 0 else 0.0

        bucket_range = max(cur_high - cur_low, 1e-4)
        lower_wick = min(cur_open, cur_close) - cur_low
        lower_wick_ratio = lower_wick / bucket_range

        # 1. Price Action: Bullish close or strong rejection wick
        is_bullish_close = cur_close >= cur_open
        is_rejection_wick = lower_wick_ratio >= self.cfg.get("min_rejection_wick_ratio", 0.35)
        has_bullish_action = is_bullish_close or is_rejection_wick

        # 2. Delta & Order Flow Gate
        require_pos_delta = self.cfg.get("require_positive_delta", True)
        is_positive_delta = cur_delta >= 0

        # If live_order_flow is passed from tick stream, incorporate it
        if live_order_flow:
            tick_delta = live_order_flow.get("delta", 0.0)
            if tick_delta > 0:
                is_positive_delta = True

        delta_valid = is_positive_delta if require_pos_delta else True

        # 3. Support Retest Confirmation
        tol = self.cfg.get("retest_touch_tolerance_pct", 0.0025)
        near_support = (cur_low <= support_level * (1.0 + tol)) and (cur_close >= support_level * (1.0 - self.cfg.get("strict_invalidation_buffer_pct", 0.0020)))

        if near_support and has_bullish_action and delta_valid:
            pattern = "BULLISH_BUCKET_CLOSE" if is_bullish_close else "ABSORPTION_REJECTION_WICK"
            return {
                "triggered": True,
                "pattern": pattern,
                "delta": cur_delta,
                "lower_wick_ratio": lower_wick_ratio,
                "reason": f"Institutional Absorption @ Support {support_level:.2f} | {pattern} | Delta: {cur_delta:+.0f}"
            }

        return {
            "triggered": False,
            "pattern": "NONE",
            "delta": cur_delta,
            "reason": f"Criteria unmet: near_support={near_support}, bullish_action={has_bullish_action}, pos_delta={delta_valid}"
        }
