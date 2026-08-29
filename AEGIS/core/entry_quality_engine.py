"""
AEGIS EntryQualityEngine — 4-Domain Unified Entry Quality Score (EQS 0 to 100).

Architecture:
    EQS = Permission Quality + Persistence Quality + Trigger Quality + Option Quality

Domains & Provisional Weights (Research Hypotheses):
1. Permission Quality (0 to 30 pts):
   - Multi-Timeframe Trend (1m & 3m EMA Slopes): up to 15 pts
   - Continuous ATR Volatility Expansion: up to 10 pts
   - Market Structure (Aligned=5, Neutral=2.5, Opposed=0): up to 5 pts
2. Persistence Quality (0 to 25 pts):
   - 60s Signed Persistence Ratio: up to 15 pts
   - Zero-Opposite State Bonus: 10 pts (penalized if opposite > 0)
3. Trigger Quality (0 to 25 pts):
   - Microstructure Scorer (L1-L5 OFI, Depth Decay): up to 15 pts
   - Trigger Freshness (<= 3s = 10 pts, 3-8s = 5 pts, > 8s = 0 pts): up to 10 pts
4. Option Quality (0 to 20 pts):
   - Spread Tightness (Spread <= 1.0% = 10 pts, <= 2.0% = 5 pts): up to 10 pts
   - Depth Liquidity: up to 10 pts

Total: 0 to 100 pts. Default provisional dispatch threshold = 75.0.
"""

from __future__ import annotations
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass


class EntryQualityEngine:
    """
    Computes a single unified Entry Quality Score (EQS) across the 4 core domains.
    """

    def __init__(self, dispatch_threshold: float = 70.0):
        self.dispatch_threshold = dispatch_threshold

    def compute_eqs(
        self,
        # Domain 1: Permission
        ema_1m_slope: float,
        ema_3m_slope: float,
        atr_ratio: float,
        market_structure: str,
        direction: str,
        # Domain 2: Persistence
        persistence_score: float,
        opposite_count: int,
        # Domain 3: Trigger
        micro_score: float,
        trigger_age_sec: float,
        # Domain 4: Option Quality
        spread_pct: float,
        total_depth_qty: float,
    ) -> Tuple[float, bool, Dict[str, Any]]:
        """
        Calculates EQS and checks if dispatch threshold is met.
        
        Returns:
            (eqs_score_0_to_100, is_qualified, score_breakdown_dict)
        """
        # Hard Veto Condition: Opposed Market Structure
        if market_structure.upper() == "OPPOSED":
            return 0.0, False, {"reason": "hard_veto_market_structure_opposed"}

        # Hard Veto Condition: Stale Trigger
        if trigger_age_sec > 8.0:
            return 0.0, False, {"reason": "hard_veto_stale_trigger"}

        # ─── Domain 1: Permission Quality (30 pts max) ───
        is_call = direction.upper() in ("UP", "CALL", "BULL")
        slope_1m_eff = ema_1m_slope if is_call else -ema_1m_slope
        slope_3m_eff = ema_3m_slope if is_call else -ema_3m_slope

        trend_score = 0.0
        if slope_1m_eff > 0:
            trend_score += min(7.5, slope_1m_eff * 5.0)
        if slope_3m_eff > 0:
            trend_score += min(7.5, slope_3m_eff * 5.0)

        # Volatility expansion
        vol_score = 0.0
        if atr_ratio >= 1.20:
            vol_score = 10.0
        elif atr_ratio >= 1.05:
            vol_score = 7.0
        elif atr_ratio >= 0.95:
            vol_score = 4.0

        # Market structure
        struct_score = 0.0
        if market_structure == "ALIGNED":
            struct_score = 5.0
        elif market_structure == "NEUTRAL":
            struct_score = 2.5

        permission_pts = trend_score + vol_score + struct_score  # Max 30

        # ─── Domain 2: Persistence Quality (25 pts max) ───
        pers_base = min(15.0, persistence_score * 15.0)
        opp_bonus = 10.0 if opposite_count == 0 else (5.0 if opposite_count == 1 else 0.0)
        persistence_pts = pers_base + opp_bonus  # Max 25

        # ─── Domain 3: Trigger Quality (25 pts max) ───
        micro_norm = max(0.0, (micro_score - 50.0) / 50.0)  # 50-100 -> 0.0-1.0
        micro_pts = min(15.0, micro_norm * 15.0)

        fresh_pts = 0.0
        if trigger_age_sec <= 3.0:
            fresh_pts = 10.0
        elif trigger_age_sec <= 8.0:
            fresh_pts = 5.0

        trigger_pts = micro_pts + fresh_pts  # Max 25

        # ─── Domain 4: Option Quality (20 pts max) ───
        spread_pts = 0.0
        if spread_pct <= 1.0:
            spread_pts = 10.0
        elif spread_pct <= 2.0:
            spread_pts = 5.0

        depth_pts = min(10.0, (total_depth_qty / 2000.0) * 10.0)
        option_pts = spread_pts + depth_pts  # Max 20

        # Total Composite EQS
        eqs = round(permission_pts + persistence_pts + trigger_pts + option_pts, 2)
        is_qualified = (eqs >= self.dispatch_threshold)

        breakdown = {
            "eqs": eqs,
            "permission_pts": round(permission_pts, 2),
            "persistence_pts": round(persistence_pts, 2),
            "trigger_pts": round(trigger_pts, 2),
            "option_pts": round(option_pts, 2),
            "dispatch_threshold": self.dispatch_threshold,
        }

        return eqs, is_qualified, breakdown
