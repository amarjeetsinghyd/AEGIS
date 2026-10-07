"""
ARGUS StockConvictionEngine — Continuous Composite Conviction Scoring Stack (v5.0).
Directly ported from AEGIS v1.0 Production Architecture.

Single Unified Rubric (0 to 100):
  - Price Action Quality: 40 points
  - Wyckoff Volume & OFI: 30 points
  - Microstructure Energy: 20 points
  - Structural Runway: 10 points

Hard Hurdle:
  Score >= 80.0 => ADMIT TRADE (Pass to execution)
  Score < 80.0  => VETO (Discard weak conviction)
"""

from __future__ import annotations

import logging
from typing import Tuple, Dict, Any

try:
    from common.logger import get_logger
    logger = get_logger("argus_conviction")
except Exception:
    logger = logging.getLogger("argus_conviction")


class StockConvictionEngine:
    """
    Computes unified composite conviction score for stock setups.
    """

    def __init__(self, hurdle_score: float = 80.0):
        self.hurdle_score = hurdle_score

    def evaluate(
        self,
        pillar_id: int,
        pa_details: Dict[str, Any],
        wyckoff_metrics: Dict[str, Any],
        micro_score: float,
    ) -> Tuple[bool, float, Dict[str, Any]]:
        """
        Combines PA, Wyckoff, and Micro scores into a continuous 0-100 score.
        Returns: (admit, score, breakdown)
        """
        # 1. Price Action Score (0-40)
        score_pa = 30.0
        if pillar_id == 1:
            score_pa = 40.0 if pa_details.get("has_runway", False) else 32.0
        elif pillar_id == 2:
            score_pa = 38.0
        elif pillar_id == 3:
            score_pa = 37.0
        elif pillar_id == 4:
            score_pa = 36.0

        # 2. Wyckoff Volume Score (0-30)
        score_wyck = 0.0
        if wyckoff_metrics.get("has_vdr", False):
            score_wyck += 15.0
        if wyckoff_metrics.get("has_surge", False):
            score_wyck += 10.0
        if wyckoff_metrics.get("delta_aligned", False):
            score_wyck += 5.0

        # 3. Microstructure Energy Score (0-20)
        # Scaled from 0-100 micro validator score
        score_micro = max(0.0, min(20.0, (micro_score / 100.0) * 20.0))

        # 4. Runway Score (0-10)
        runway_atr = float(pa_details.get("runway_atr", 1.0))
        score_runway = min(10.0, max(4.0, runway_atr * 8.0))

        total_score = score_pa + score_wyck + score_micro + score_runway
        admit = total_score >= self.hurdle_score

        breakdown = {
            "score": round(total_score, 1),
            "score_pa": round(score_pa, 1),
            "score_wyck": round(score_wyck, 1),
            "score_micro": round(score_micro, 1),
            "score_runway": round(score_runway, 1),
            "pillar_id": pillar_id,
            "admit": admit,
            "hurdle": self.hurdle_score,
        }

        return admit, total_score, breakdown
