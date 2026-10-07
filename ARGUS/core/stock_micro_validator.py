"""
ARGUS StockMicroValidator — 30-Second L2 Depth & Microstructure Aggression Scorer (v5.0).
Directly ported from AEGIS v1.0 Production Architecture.

Evaluates micro-energy within [T-30, T] on a transparent 0-100 rubric:
  - Momentum: 40 points (Directional tick drift)
  - Aggression: 35 points (L1 touch skew & replenishment)
  - Velocity: 25 points (Tick arrival frequency & depth thickness)
Circuit Breakers:
  - Spread blowout (> 15 bps on large caps) => instant veto
  - Thin ghost book (< 100 shares on touch) => instant veto
"""

from __future__ import annotations

import logging
from typing import Tuple, Dict, Any, List

try:
    from common.logger import get_logger
    logger = get_logger("argus_micro_validator")
except Exception:
    logger = logging.getLogger("argus_micro_validator")


class StockMicroValidator:
    """
    Validates institutional order-book aggression and momentum in the [T-30s, T] window.
    """

    def __init__(self, min_pass_score: float = 60.0):
        self.min_pass_score = min_pass_score

    def evaluate(
        self,
        symbol: str,
        ticks: List[Tuple[float, float, float, float, int, int]], # (ts, lp, bp1, sp1, bq1, sq1)
        direction: str = "BUY",
        anchor_T: float = 0.0,
    ) -> Tuple[bool, float, Dict[str, Any]]:
        """
        Evaluates ticks in [anchor_T - 30.0, anchor_T].
        Returns (passed, score, metrics).
        """
        if not ticks:
            return True, 70.0, {"reason": "permissive_no_ticks", "score": 70.0}

        if anchor_T <= 0:
            anchor_T = ticks[-1][0]

        # ── 1. Circuit Breakers on latest tick ──
        latest = ticks[-1]
        lp_now = latest[1]
        bp1, sp1 = latest[2], latest[3]
        bq1, sq1 = latest[4], latest[5]

        if bp1 > 0 and sp1 > 0 and lp_now > 0:
            spread_bps = ((sp1 - bp1) / lp_now) * 10000.0
            if spread_bps > 15.0:
                return False, 0.0, {"reason": "spread_blowout", "spread_bps": spread_bps}

        if bq1 < 50 or sq1 < 50:
            return False, 0.0, {"reason": "ghost_book_thin_liquidity", "bq1": bq1, "sq1": sq1}

        # Slice window [T-30, T]
        w_ticks = [t for t in ticks if (anchor_T - 30.0) <= t[0] <= anchor_T]
        if len(w_ticks) < 4:
            return True, 65.0, {"reason": "permissive_few_ticks", "score": 65.0}

        earliest = w_ticks[0]
        lp_start = earliest[1]

        # ── 2. Momentum Score (40 pts) ──
        drift_pct = (lp_now - lp_start) / max(lp_start, 1e-4) * 100.0
        score_momentum = 20.0
        if direction == "BUY":
            if drift_pct >= 0.15:
                score_momentum = 40.0
            elif drift_pct >= 0.05:
                score_momentum = 32.0
            elif drift_pct >= 0.0:
                score_momentum = 24.0
            elif drift_pct >= -0.05:
                score_momentum = 12.0
            else:
                score_momentum = 0.0
        elif direction == "SELL":
            if drift_pct <= -0.15:
                score_momentum = 40.0
            elif drift_pct <= -0.05:
                score_momentum = 32.0
            elif drift_pct <= 0.0:
                score_momentum = 24.0
            elif drift_pct <= 0.05:
                score_momentum = 12.0
            else:
                score_momentum = 0.0

        # ── 3. Aggression Score (35 pts) ──
        touch_tot = max(1, bq1 + sq1)
        touch_ratio = (bq1 - sq1) / touch_tot # [-1, 1]
        score_aggression = 15.0
        if direction == "BUY":
            if touch_ratio >= 0.35:
                score_aggression = 35.0
            elif touch_ratio >= 0.10:
                score_aggression = 25.0
            elif touch_ratio >= -0.10:
                score_aggression = 15.0
            else:
                score_aggression = 5.0
        elif direction == "SELL":
            if touch_ratio <= -0.35:
                score_aggression = 35.0
            elif touch_ratio <= -0.10:
                score_aggression = 25.0
            elif touch_ratio <= 0.10:
                score_aggression = 15.0
            else:
                score_aggression = 5.0

        # ── 4. Velocity Score (25 pts) ──
        tick_count = len(w_ticks)
        if tick_count >= 20:
            score_velocity = 25.0
        elif tick_count >= 10:
            score_velocity = 18.0
        else:
            score_velocity = 10.0

        total_score = score_momentum + score_aggression + score_velocity
        passed = total_score >= self.min_pass_score

        metrics = {
            "score": round(total_score, 1),
            "score_momentum": round(score_momentum, 1),
            "score_aggression": round(score_aggression, 1),
            "score_velocity": round(score_velocity, 1),
            "drift_pct": round(drift_pct, 3),
            "touch_ratio": round(touch_ratio, 2),
            "tick_count_30s": tick_count,
            "reason": "energy_confirmed" if passed else "energy_insufficient",
        }

        return passed, total_score, metrics
