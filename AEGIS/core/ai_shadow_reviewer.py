"""
AEGIS AI Shadow Reviewer — Lightweight Dormant Stub (2026-09-23).

LAYA and external heavy NLP dependencies dropped.
Kept as a zero-overhead, non-blocking telemetry interface for future quant/timeseries models.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

try:
    from common.logger import get_logger
    logger = get_logger("aegis_ai_reviewer")
except Exception:
    logger = logging.getLogger("aegis.ai_shadow_reviewer")


class AIShadowReviewer:
    """
    Lightweight, zero-overhead stub for AI Trade Reviewer.
    Preserves exact method signature to prevent regressions.
    """

    def __init__(
        self,
        mode: str = "DISABLED",
        model_name: str = "disabled",
        log_path: Optional[str] = None,
        max_bars_history: int = 60,
    ):
        self.mode = mode.upper()
        self.model_name = model_name
        self.max_bars_history = max_bars_history
        self._last_call_ts: Dict[str, float] = {}

    def review_setup_async(
        self,
        symbol: str,
        direction: str,
        setup_name: str,
        pillar: int,
        spot_price: float,
        pdh: float,
        pdl: float,
        pdc: float,
        session_high: float,
        session_low: float,
        day_open: float,
        vwap: float,
        ema_20: float,
        ema_50: float,
        atr_1m: float,
        index_name: str,
        strike_step: float,
        machine_conviction_score: float,
        bars_1m: Optional[List[Any]] = None,
        prior_bars: Optional[List[Any]] = None,
        micro_metrics: Optional[Dict[str, Any]] = None,
        conviction_breakdown: Optional[Dict[str, Any]] = None,
        now_sec: Optional[float] = None,
        trigger_ts: Optional[float] = None,
        **kwargs: Any,
    ) -> None:
        """Non-blocking no-op stub."""
        pass

    def close(self) -> None:
        """No-op cleanup."""
        pass
