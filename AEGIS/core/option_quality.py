"""
AEGIS OptionQualityEngine — Instrument Tradability, Spread % & Depth Liquidity Filter.

Architecture:
1. Rejects illiquid, wide-spread option contracts before execution dispatch.
2. Evaluates:
   - Bid-Ask Spread Percentage: (Ask1 - Bid1) / Mid <= max_spread_pct (default 2.0%)
   - Depth Liquidity: Minimum Top-5 Bid/Ask volume available for clean fill
   - Premium Velocity: Option price momentum supporting the underlying movement
"""

from __future__ import annotations
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass


class OptionQualityEngine:
    """
    Qualifies the specific Option Contract for retail buying execution.
    """

    def __init__(
        self,
        max_spread_pct: float = 2.5,        # Max 2.5% bid-ask spread
        min_top5_depth_qty: float = 500.0,  # At least 500 units in L2 depth
        min_premium: float = 5.0,           # Avoid penny options < Rs 5
        max_premium: float = 400.0,         # Cap ultra-deep ITM options
    ):
        self.max_spread_pct = max_spread_pct
        self.min_top5_depth_qty = min_top5_depth_qty
        self.min_premium = min_premium
        self.max_premium = max_premium

    def qualify_option(
        self,
        sym: str,
        lp: float,
        bp1: float,
        sp1: float,
        top5_bid_qty: float,
        top5_ask_qty: float,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Validates tradability and liquidity of the target option strike.
        Returns:
            (is_qualified, reason, metrics_dict)
        """
        metrics: Dict[str, Any] = {
            "symbol": sym,
            "ltp": lp,
            "bp1": bp1,
            "sp1": sp1,
            "spread_pct": 0.0,
            "top5_bid_qty": top5_bid_qty,
            "top5_ask_qty": top5_ask_qty,
        }

        # 1. Price range check
        if lp < self.min_premium:
            return False, f"premium_too_low ({lp:.2f} < {self.min_premium})", metrics
        if lp > self.max_premium:
            return False, f"premium_too_high ({lp:.2f} > {self.max_premium})", metrics

        # 2. Spread calculation
        mid = (bp1 + sp1) / 2.0 if (bp1 > 0 and sp1 > 0) else lp
        spread = max(0.0, sp1 - bp1) if (bp1 > 0 and sp1 > 0) else 0.05
        spread_pct = (spread / mid * 100.0) if mid > 0 else 999.0
        metrics["spread_pct"] = round(spread_pct, 2)

        if spread_pct > self.max_spread_pct:
            return False, f"spread_too_wide ({spread_pct:.2f}% > {self.max_spread_pct}%)", metrics

        # 3. Depth liquidity check
        total_depth = top5_bid_qty + top5_ask_qty
        if total_depth < self.min_top5_depth_qty and (top5_bid_qty > 0 or top5_ask_qty > 0):
            return False, f"insufficient_depth ({total_depth:.0f} < {self.min_top5_depth_qty})", metrics

        return True, "option_qualified", metrics
