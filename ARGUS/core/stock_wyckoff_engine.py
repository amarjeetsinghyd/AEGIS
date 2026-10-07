"""
ARGUS StockWyckoffEngine — Wyckoff Volume & OFI Delta Harmony Gate (v5.0).
Directly ported from AEGIS v1.0 Production Architecture.

Eliminates Hawkes Fragility:
  Replaces fragile Hawkes branching ratio with robust volume-to-dispersion ratio (VDR >= 1.0),
  institutional volume surge (>= 1.15x), and directional Order Flow Intensity (OFI) delta harmony.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Tuple, Dict, Any, List

try:
    from common.logger import get_logger
    logger = get_logger("argus_wyckoff")
except Exception:
    logger = logging.getLogger("argus_wyckoff")


class StockWyckoffEngine:
    """
    Evaluates Wyckoff Volume Spread Analysis (VSA) and Cumulative Delta Harmony for stocks.
    """

    def __init__(
        self,
        symbol: str = "EQUITY",
        vdr_threshold: float = 1.0,
        volume_surge_ratio: float = 1.15,
        history_window: int = 15,
    ):
        self.symbol = symbol
        self.vdr_threshold = vdr_threshold
        self.volume_surge_ratio = volume_surge_ratio
        self.history_window = history_window

        self.volume_history: deque[float] = deque(maxlen=history_window)
        self.spread_bps_history: deque[float] = deque(maxlen=history_window)
        self.delta_history: deque[float] = deque(maxlen=history_window)

    def record_bar(self, volume: float, delta: float, spread_bps: float) -> None:
        """Record completed 1-minute bar volume and spread metrics."""
        if volume > 0:
            self.volume_history.append(volume)
        if spread_bps > 0:
            self.spread_bps_history.append(spread_bps)
        self.delta_history.append(delta)

    def evaluate_gate(
        self,
        direction: str,
        current_volume: float,
        current_delta: float,
        current_spread_bps: float,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Validates Wyckoff volume surge, VDR >= 1.0, and delta harmony.
        Returns: (passed, reason, metrics)
        """
        # If not enough history, compute baseline or pass with warmup flag
        if len(self.volume_history) < 5:
            return True, "wyckoff_warmup_permissive", {
                "vol": current_volume,
                "delta": current_delta,
                "vdr": 1.2,
                "surge": 1.2,
            }

        avg_vol = sum(self.volume_history) / max(len(self.volume_history), 1)
        avg_spread = sum(self.spread_bps_history) / max(len(self.spread_bps_history), 1) if self.spread_bps_history else 5.0

        # 1. Volume Surge
        surge_ratio = current_volume / max(avg_vol, 1.0)
        has_surge = surge_ratio >= self.volume_surge_ratio

        # 2. Volume-to-Dispersion Ratio (VDR)
        norm_vol = current_volume / max(avg_vol, 1.0)
        norm_spread = max(current_spread_bps, 0.5) / max(avg_spread, 0.5)
        vdr = norm_vol / max(norm_spread, 0.1)
        has_vdr = vdr >= self.vdr_threshold

        # 3. Delta Harmony
        delta_aligned = (direction == "BUY" and current_delta >= 0.0) or (direction == "SELL" and current_delta <= 0.0)

        metrics = {
            "vol": round(current_volume, 0),
            "avg_vol": round(avg_vol, 0),
            "surge_ratio": round(surge_ratio, 2),
            "vdr": round(vdr, 2),
            "delta": round(current_delta, 0),
            "delta_aligned": delta_aligned,
            "has_surge": has_surge,
            "has_vdr": has_vdr,
        }

        # Delta harmony is non-negotiable for institutional confirmation
        if not delta_aligned:
            return False, "delta_opposed", metrics

        # VDR or Surge confirmation
        if not (has_vdr or has_surge):
            return False, "insufficient_volume_vdr", metrics

        return True, "wyckoff_confirmed", metrics
