"""
AEGIS DirectionalPersistence — Rolling 60s State Sampling & Signed Directional Persistence.

Architecture:
1. Maintains 12 discrete 5-second snapshot observations across a rolling 60-second sliding window.
2. Signed state arithmetic:
   - Bullish State = +1.0
   - Neutral State =  0.0
   - Bearish State = -1.0
3. Tracks Directional Persistence and Opposite-State Count separately.
4. If opposite_state_count >= max_allowed_opposite (default 2), directional persistence is invalidated.
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
        window_sec: float = 30.0,
        sample_interval_sec: float = 5.0,
        max_snapshots: int | None = None,
        min_valid_snapshots: int = 3,
        target_persistence_threshold: float = 0.60,
        max_allowed_opposite: int = 1,
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

    def evaluate_persistence(self, target_direction: str) -> Tuple[bool, float, Dict[str, Any]]:
        """
        Evaluates directional persistence for a proposed target trade direction.
        
        Returns:
            (is_confirmed, persistence_score_0_to_1, metrics_dict)
        """
        n_obs = len(self.snapshots)
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

        bull_cnt = sum(1 for s in self.snapshots if s.strength > 0)
        bear_cnt = sum(1 for s in self.snapshots if s.strength < 0)
        neut_cnt = sum(1 for s in self.snapshots if s.strength == 0)
        avg_score = sum(s.micro_score for s in self.snapshots) / n_obs

        metrics["bullish_count"] = bull_cnt
        metrics["bearish_count"] = bear_cnt
        metrics["neutral_count"] = neut_cnt
        metrics["avg_micro_score"] = round(avg_score, 1)

        is_call = target_direction.upper() in ("UP", "CALL", "BULL")
        is_put = target_direction.upper() in ("DOWN", "PUT", "BEAR")

        if is_call:
            aligned_count = bull_cnt
            opposite_count = bear_cnt
            signed_sum = sum(s.strength for s in self.snapshots)
        elif is_put:
            aligned_count = bear_cnt
            opposite_count = bull_cnt
            signed_sum = sum(-s.strength for s in self.snapshots)
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
        # E.g. All aligned: signed_sum == n_obs -> 1.0
        # Half aligned, half neutral: signed_sum == n_obs/2 -> 0.5
        persistence = max(0.0, signed_sum / float(n_obs))
        metrics["directional_persistence"] = round(persistence, 3)

        # Invalidation Condition 1: Too many opposite states (Market is alternating / reversing)
        if opposite_count > self.max_allowed_opposite:
            return False, persistence, {
                **metrics,
                "reason": f"excessive_opposite_states ({opposite_count} > {self.max_allowed_opposite})",
            }

        # Invalidation Condition 2: Persistence below research threshold
        if persistence < self.target_persistence_threshold:
            return False, persistence, {
                **metrics,
                "reason": f"low_persistence ({persistence:.2f} < {self.target_persistence_threshold:.2f})",
            }

        return True, persistence, {**metrics, "reason": "persistence_confirmed"}
