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
        min_premium: float = 4.0,           # Testing phase: allow Rs 4+ (was 5.0)
        max_premium: float = 400.0,         # Cap ultra-deep ITM options
    ):
        self.max_spread_pct = max_spread_pct
        self.min_top5_depth_qty = min_top5_depth_qty
        self.min_premium = min_premium
        self.max_premium = max_premium

    def effective_floor(self, available_funds: float = 0.0, lot_size: int = 0) -> float:
        """Dynamic floor override for Tiny Funds / Testing Mode.

        Mirrors common/execution/strike_selector.effective_premium_floor:
        Effective Floor = min(env_premium_min, budget_ceiling - safety_buffer),
        applied only when tiny funds would otherwise put the configured floor
        above the per-lot budget ceiling (dead zone). Without funds context the
        configured floor is returned unchanged.
        """
        try:
            import os as _os
            funds = float(available_funds or 0)
            lot = int(lot_size or 0)
            tiny = float(_os.getenv("AEGIS_TESTING_TINY_FUNDS", "2000.0"))
            if funds <= 0 or lot <= 0 or funds >= tiny:
                return float(self.min_premium)
            env_floor = float(_os.getenv("AEGIS_MIN_PREMIUM", "4.0"))
            buf = float(_os.getenv("AEGIS_PREMIUM_SAFETY_BUFFER", "0.50"))
            dyn = min(env_floor, funds / lot - buf)
            if dyn < float(self.min_premium):
                return max(0.0, dyn)
            return float(self.min_premium)
        except Exception:
            return float(self.min_premium)

    def qualify_option(
        self,
        sym: str,
        lp: float,
        bp1: float,
        sp1: float,
        top5_bid_qty: float,
        top5_ask_qty: float,
        available_funds: float = 0.0,
        lot_size: int = 0,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Validates tradability and liquidity of the target option strike.
        When available_funds/lot_size are provided in Tiny Funds mode, the
        premium floor is dynamically clamped so it can never exceed the
        per-lot budget ceiling (dead-zone fix). Callers without funds context
        keep the configured floor exactly as before.
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

        # 1. Price range check (dynamic floor in testing mode)
        _eff_min = self.effective_floor(available_funds, lot_size)
        if lp < _eff_min:
            return False, f"premium_too_low ({lp:.2f} < {_eff_min})", metrics
        if lp > self.max_premium:
            return False, f"premium_too_high ({lp:.2f} > {self.max_premium})", metrics

        # 2. Spread calculation
        mid = (bp1 + sp1) / 2.0 if (bp1 > 0 and sp1 > 0) else lp
        spread = max(0.0, sp1 - bp1) if (bp1 > 0 and sp1 > 0) else 0.05
        spread_pct = (spread / mid * 100.0) if mid > 0 else 999.0
        metrics["spread_pct"] = round(spread_pct, 2)

        if spread_pct > self.max_spread_pct:
            return False, f"spread_too_wide ({spread_pct:.2f}% > {self.max_spread_pct}%)", metrics

        # 3. Depth liquidity check (fail-closed: missing/zero depth rejects, with liquid touchline fallback)
        total_depth = top5_bid_qty + top5_ask_qty
        if total_depth < self.min_top5_depth_qty:
            if total_depth == 0 and spread_pct <= 1.0 and bp1 > 0 and sp1 > 0:
                total_depth = self.min_top5_depth_qty
                metrics["top5_bid_qty"] = self.min_top5_depth_qty / 2.0
                metrics["top5_ask_qty"] = self.min_top5_depth_qty / 2.0
            else:
                return False, f"insufficient_depth ({total_depth:.0f} < {self.min_top5_depth_qty})", metrics

        return True, "option_qualified", metrics
