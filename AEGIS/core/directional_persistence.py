"""
AEGIS DirectionalPersistence — Rolling 60s State Sampling & Signed Directional Persistence.

V3.0 Event-Bound Architecture:
1. Maintains 12 discrete 5-second snapshot observations across a rolling 60-second sliding window.
2. Signed state arithmetic:
   - Bullish State = +1.0
   - Neutral State =  0.0
   - Bearish State = -1.0
3. Tracks Directional Persistence and Opposite-State Count separately.
4. If opposite_state_count > max_allowed_opposite (default 2), directional persistence is invalidated.
5. V3.0: Evaluation is strictly bound to closed-candle timestamp T via anchor_ts_sec.
   Pool = snapshots with ts in [T-lookback, T]; post-T snapshots (ts > T) are excluded
   so T+1s ticks can never pollute the trigger decision. Thresholds: Dp >= 0.70,
   opposite_count <= 2.
"""

from __future__ import annotations
from collections import deque
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass


@dataclass
class StateSnapshot:
    ts_sec: float
    direction: str       # "UP", "DOWN", "NEUTRAL"
    strength: float      # +1.0 (Bull), 0.0 (Neutral), -1.0 (Bear)
    micro_score: float   # 0 to 100
    spot_drift: float    # pts in last snapshot window


class DirectionalPersistenceEngine:
    """
    Evaluates survival of directional pressure rather than raw burst counts.
    """

    def __init__(
        self,
        window_sec: float = 60.0,
        sample_interval_sec: float = 5.0,
        max_snapshots: int | None = None,
        min_valid_snapshots: int = 3,
        target_persistence_threshold: float = 0.70,
        max_allowed_opposite: int = 2,
    ):
        self.window_sec = window_sec
        self.sample_interval_sec = sample_interval_sec
        self.max_snapshots = max_snapshots or max(1, int(window_sec / sample_interval_sec))
        self.min_valid_snapshots = min_valid_snapshots
        self.target_persistence_threshold = target_persistence_threshold
        self.max_allowed_opposite = max_allowed_opposite

        # History of 5-second snapshots: deque of StateSnapshot
        self.snapshots: deque[StateSnapshot] = deque(maxlen=self.max_snapshots)
        self.last_sample_ts: float = 0.0

    def should_sample(self, now_sec: float) -> bool:
        """Returns True if at least sample_interval_sec (5.0s) has elapsed since last snapshot."""
        return (now_sec - self.last_sample_ts) >= self.sample_interval_sec

    def record_snapshot(
        self,
        now_sec: float,
        direction: str,
        micro_score: float,
        spot_drift: float,
    ) -> None:
        """
        Records a discrete 5-second observation snapshot.
        """
        dir_norm = direction.upper()
        if dir_norm in ("UP", "BULL", "CALL"):
            strength = +1.0
        elif dir_norm in ("DOWN", "BEAR", "PUT"):
            strength = -1.0
        else:
            strength = 0.0
            dir_norm = "NEUTRAL"

        snapshot = StateSnapshot(
            ts_sec=now_sec,
            direction=dir_norm,
            strength=strength,
            micro_score=micro_score,
            spot_drift=spot_drift,
        )

        self.snapshots.append(snapshot)
        self.last_sample_ts = now_sec

        # Prune snapshots older than window_sec (60s)
        cutoff = now_sec - self.window_sec
        while self.snapshots and self.snapshots[0].ts_sec < cutoff:
            self.snapshots.popleft()

    def evaluate_persistence(
        self,
        target_direction: str,
        lookback_sec: Optional[float] = None,
        min_threshold: Optional[float] = None,
        max_allowed_opposite: Optional[int] = None,
        anchor_ts_sec: Optional[float] = None,
    ) -> Tuple[bool, float, Dict[str, Any]]:
        """
        Evaluates directional persistence for a proposed target trade direction.

        V3.0 Event-Bound Slicing:
        - When anchor_ts_sec (closed-candle timestamp T) is provided, the pool is
          strictly snapshots with ts in [T - lookback, T]. Snapshots with
          ts > T (post-close T+1s ticks) are excluded by construction.
        - When anchor is None, legacy behaviour is kept (cutoff from
          last_sample_ts) for backward compatibility with unit tests.

        Returns:
            (is_confirmed, persistence_score_0_to_1, metrics_dict)
        """
        if anchor_ts_sec is not None and anchor_ts_sec > 0:
            eff_lookback = lookback_sec if lookback_sec is not None else self.window_sec
            cutoff = anchor_ts_sec - eff_lookback
            pool = [s for s in self.snapshots if s.ts_sec >= cutoff and s.ts_sec <= anchor_ts_sec]
        elif lookback_sec is not None and self.last_sample_ts > 0:
            cutoff = self.last_sample_ts - lookback_sec
            pool = [s for s in self.snapshots if s.ts_sec >= cutoff]
        else:
            pool = list(self.snapshots)

        n_obs = len(pool)
        metrics: Dict[str, Any] = {
            "total_observations": n_obs,
            "bullish_count": 0,
            "neutral_count": 0,
            "bearish_count": 0,
            "opposite_count": 0,
            "directional_persistence": 0.0,
            "avg_micro_score": 0.0,
        }

        if n_obs == 0:
            return True, 1.0, {**metrics, "reason": "warmup_zero_obs_permissive"}

        bull_cnt = sum(1 for s in pool if s.strength > 0)
        bear_cnt = sum(1 for s in pool if s.strength < 0)
        neut_cnt = sum(1 for s in pool if s.strength == 0)
        avg_score = sum(s.micro_score for s in pool) / n_obs

        metrics["bullish_count"] = bull_cnt
        metrics["bearish_count"] = bear_cnt
        metrics["neutral_count"] = neut_cnt
        metrics["avg_micro_score"] = round(avg_score, 1)

        is_call = target_direction.upper() in ("UP", "CALL", "BULL")
        is_put = target_direction.upper() in ("DOWN", "PUT", "BEAR")

        if is_call:
            aligned_count = bull_cnt
            opposite_count = bear_cnt
            signed_sum = sum(s.strength for s in pool)
        elif is_put:
            aligned_count = bear_cnt
            opposite_count = bull_cnt
            signed_sum = sum(-s.strength for s in pool)
        else:
            return False, 0.0, {**metrics, "reason": "invalid_direction"}

        metrics["opposite_count"] = opposite_count

        # During warmup with few observations (1 <= n_obs < min_valid_snapshots):
        # Confirm if no opposite observations exist
        if n_obs < self.min_valid_snapshots:
            if opposite_count == 0:
                return True, 1.0, {**metrics, "reason": "warmup_aligned_permissive"}
            return False, 0.0, {**metrics, "reason": "insufficient_observations"}

        # Directional Persistence = Normalized Signed Alignment (0.0 to 1.0)
        persistence = max(0.0, signed_sum / float(n_obs))
        metrics["directional_persistence"] = round(persistence, 3)

        eff_threshold = min_threshold if min_threshold is not None else self.target_persistence_threshold
        eff_max_opposite = max_allowed_opposite if max_allowed_opposite is not None else self.max_allowed_opposite

        # Invalidation Condition 1: Too many opposite states
        if opposite_count > eff_max_opposite:
            return False, persistence, {
                **metrics,
                "reason": f"excessive_opposite_states ({opposite_count} > {eff_max_opposite})",
            }

        # Invalidation Condition 2: Persistence below research threshold
        if persistence < eff_threshold:
            return False, persistence, {
                **metrics,
                "reason": f"low_persistence ({persistence:.2f} < {eff_threshold:.2f})",
            }

        return True, persistence, {**metrics, "reason": "persistence_confirmed"}
