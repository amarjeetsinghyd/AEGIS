"""
AEGIS OptionQualityEngine — Instrument Tradability, Spread % & Depth Liquidity Filter.

Architecture:
1. Rejects illiquid, wide-spread option contracts before execution dispatch.
2. Evaluates:
   - Bid-Ask Spread Percentage: (Ask1 - Bid1) / Mid <= max_spread_pct (default 2.0%)
   - Depth Liquidity: Minimum TOTAL top-5 depth AND a two-sided per-side floor
     (rejects the one-sided ask/bid trap that causes full-lot entry slippage)
   - Premium Velocity: Option price momentum supporting the underlying movement
"""

from __future__ import annotations
from typing import Dict, Tuple, Any


class OptionQualityEngine:
    """
    Qualifies the specific Option Contract for retail buying execution.
    """

    def __init__(
        self,
        max_spread_pct: float = 2.5,        # Max 2.5% bid-ask spread
        min_top5_depth_qty: float = 500.0,  # Minimum TOTAL top-5 L2 depth (both sides summed)
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

        # 2. Spread calculation (Fail-closed: missing quote or crossed quote rejects immediately)
        if bp1 <= 0 or sp1 <= 0 or sp1 <= bp1:
            return False, f"invalid_or_missing_l1_quote (bp1={bp1}, sp1={sp1})", metrics

        mid = (bp1 + sp1) / 2.0
        spread = sp1 - bp1
        spread_pct = (spread / mid) * 100.0
        metrics["spread_pct"] = round(spread_pct, 2)

        if spread_pct > self.max_spread_pct:
            return False, f"spread_too_wide ({spread_pct:.2f}% > {self.max_spread_pct}%)", metrics

        # 3. Depth liquidity check (Fail-closed, scale-invariant)
        # Two-sided book mandate: a 5000-bid / 1-ask book is NOT tradable liquidity for a
        # BUY order — it guarantees a full-lot sweep through the ask wall (entry slippage trap).
        total_depth = top5_bid_qty + top5_ask_qty
        min_side_depth = min(top5_bid_qty, top5_ask_qty)
        eff_lot = float(lot_size or 0.0)
        eff_min_total = max(float(self.min_top5_depth_qty or 0.0), 5.0 * eff_lot, 50.0)
        eff_min_side = max(2.0 * eff_lot, 25.0)
        metrics["total_depth"] = round(total_depth, 1)
        metrics["min_side_depth"] = round(min_side_depth, 1)
        metrics["min_required_total_depth"] = round(eff_min_total, 1)
        metrics["min_required_side_depth"] = round(eff_min_side, 1)
        if total_depth < eff_min_total:
            return False, f"insufficient_depth ({total_depth:.0f} < {eff_min_total:.0f})", metrics
        if min_side_depth < eff_min_side:
            return False, (
                f"one_sided_book_trap (bid={top5_bid_qty:.0f}, ask={top5_ask_qty:.0f} "
                f"-> min side {min_side_depth:.0f} < {eff_min_side:.0f})"
            ), metrics

        return True, "option_qualified", metrics
