"""
AEGIS Horizontal Visibility Graph (HVG) Dual-Timeframe Engine
==============================================================
Pillar 4 (Topological Break of Structure & Network Phase Shift Engine).

Architectural Invariants:
1. Allocation-Controlled Hot Path:
   - Pre-allocated 60-bar 1-minute Circular Ring Buffer with reusable scratch storage.
   - Pre-allocated 30-bar 5-minute Circular Ring Buffer for macro structural alignment.
   - Avoids unbounded data structures on the candle-evaluation path; telemetry/file I/O remains outside the core graph math.
2. Dual-Timeframe Dual-Envelope Monotonic Stack:
   - 1-Minute Upper/Lower strict HVG: high-resolution structural anchor detection in O(N).
   - 5-Minute Upper/Lower strict HVG: macro structural regime and proximity verification in O(N).
3. Clock-Aligned 5-Minute Aggregation:
   - IST timezone locked to NSE intraday 5-minute boundaries.
   - Day-Boundary Separation: in-flight 5m bar is finalized on day rollover; sessions are not mixed.
   - 5m ATR is derived from 5m True Ranges, not inherited from 1m ATR.
4. Pure Multi-Timeframe Price Action:
   - Macro confirmation is driven by 5m HVG structural alignment and VWAP; EMA trend remains optional.
5. Persistent Opposing Liquidity Shield:
   - Evaluates recent depth snapshots within a 2.0s horizon.
   - A wall must be continuously evidenced for >= 400ms with fresh evidence near the evaluation timestamp.
   - Uses depth imbalance/side-depth thresholds; this is a persistence filter, not an anti-spoof classifier.
6. Deterministic Network Phase Shift Entry:
   - Historical 1m anchor prominence is frozen on Graph(T-1), excluding the breakout bar from anchor strength.
   - 4-bar pre-breakout closes remain on the valid side of the anchor and within the configured ATR proximity hurdle.
   - Breakout bar must cross the anchor (no gap-through), with body >= 40% of range and capped extension.
7. Configurable Live / Shadow Mode:
   - Live dispatch when shadow_only=False or AEGIS_HVG_MODE=ACTIVE.
   - Forward outcome telemetry records 30s/60s/180s/300s, MFE/MAE, and explicit COMPLETE/CENSORED status.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("aegis.hvg_engine")

IST_TZ = timezone(timedelta(hours=5, minutes=30))


@dataclass
class HVGSetupSignal:
    """Represents an admitted Topological candidate signal (Pillar 4 Phase Shift or Pillar 2 Structural Sweep)."""
    timestamp: float
    direction: str                     # "CALL" or "PUT"
    setup_name: str                    # "hvg_network_phase_shift" or "topological_sweep_reclaim"
    pillar: int                        # 4 or 2
    anchor_price: float                # R* (CALL) or S* (PUT)
    anchor_prominence: float           # P*_U or P*_L (>= 1.75)
    anchor_index: int                  # Historical bar index of anchor [0..N-4]
    breakout_price: float              # Close_T
    bar_open: float
    bar_high: float
    bar_low: float
    bar_close: float
    body_ratio: float
    sl_price: float
    buffer_pts: float
    atr_14: float
    vwap: float
    ema_20: float
    ema_50: float
    is_shadow_only: bool = True
    macro_5m_status: str = "ALIGNED"
    l2_wall_status: str = "PASS"
    strategy_variant: str = "HVG_P4"    # "HVG_P4", "TOPO_P2", "LEGACY_P2"
    event_id: str = ""
    anchor_age: int = 0
    anchor_degree: int = 0
    sweep_depth_atr: float = 0.0
    wick_ratio: float = 0.0
    distance_to_vwap_atr: float = 0.0  # Signed direction-normalized: s * (Close - VWAP) / ATR
    candidate_count: int = 0
    anchor_selection_reason: str = "nearest_prominent"


@dataclass
class PendingTelemetryRecord:
    """Lightweight in-memory tracker for forward outcome telemetry."""
    signal: HVGSetupSignal
    entry_time: float
    entry_price: float
    mfe: float = 0.0                   # Max Favorable Excursion in points
    mae: float = 0.0                   # Max Adverse Excursion in points
    price_30s: Optional[float] = None
    price_60s: Optional[float] = None
    price_180s: Optional[float] = None
    price_300s: Optional[float] = None
    completed: bool = False
    outcome_status: str = "PENDING"


class HVGEngine:
    """
    AEGIS Horizontal Visibility Graph (HVG) Dual-Timeframe Engine.
    Evaluates 1m and 5m Topological Visibility Graphs in real time in O(N) time.
    Provides shared topological computation for Pillar 4 (Phase Shift Breakout)
    and Pillar 2 (Topological Structural Sweep & Reclaim).
    """

    CAPACITY: int = 60                 # Fixed 60-minute lookback window
    MIN_BARS: int = 15                 # Minimum mature 1m bars before evaluation
    CAPACITY_5M: int = 30              # Fixed 30-bar 5-minute lookback window (2.5 hours)
    MIN_BARS_5M: int = 6               # Minimum mature 5m bars for structural check
    MIN_PROMINENCE: float = 1.75       # Minimum Relative Prominence threshold for anchor
    COMPRESSION_HURDLE_MULT: float = 0.20  # Max pre-breakout close penetration beyond anchor (k * ATR)
    PROXIMITY_5M_MULT: float = 0.25    # Minimum clearance from unbroken 5m opposing anchor (k * ATR_5m)
    EPSILON: float = 1e-9              # ZeroDivisionError safety guard
    MAX_PENDING: int = 50              # Maximum concurrent in-flight shadow records
    IST_TZ = IST_TZ

    # -----------------------------------------------------------------
    # Pillar 2: Topological Structural Sweep Configuration
    # -----------------------------------------------------------------
    SWEEP_MIN_ANCHOR_AGE: int = 10
    SWEEP_MAX_ANCHOR_AGE: int = 30
    SWEEP_MIN_WICK_RATIO: float = 0.35
    SWEEP_MAX_PENETRATION_ATR: float = 0.0  # 0.0 disables penetration cap (data observation first)

    def __init__(self, log_dir: Optional[str] = None, shadow_only: Optional[bool] = None):
        if shadow_only is not None:
            self.shadow_only = bool(shadow_only)
        else:
            self.shadow_only = os.getenv("AEGIS_HVG_MODE", "SHADOW").upper() != "ACTIVE"

        self.compression_hurdle_mult: float = float(
            os.getenv("AEGIS_HVG_COMPRESSION_MULT", str(self.COMPRESSION_HURDLE_MULT))
        )
        self.proximity_5m_mult: float = float(
            os.getenv("AEGIS_HVG_5M_PROXIMITY_MULT", str(self.PROXIMITY_5M_MULT))
        )
        self.enforce_ema_trend: bool = os.getenv("AEGIS_HVG_ENFORCE_EMA_TREND", "0") == "1"

        # Pillar 2 Environment Config
        self.sweep_min_anchor_age: int = int(
            os.getenv("AEGIS_HVG_SWEEP_MIN_AGE", str(self.SWEEP_MIN_ANCHOR_AGE))
        )
        self.sweep_max_anchor_age: int = int(
            os.getenv("AEGIS_HVG_SWEEP_MAX_AGE", str(self.SWEEP_MAX_ANCHOR_AGE))
        )
        self.sweep_min_wick_ratio: float = float(
            os.getenv("AEGIS_HVG_SWEEP_MIN_WICK", str(self.SWEEP_MIN_WICK_RATIO))
        )
        self.sweep_max_penetration_atr: float = float(
            os.getenv(
                "AEGIS_HVG_SWEEP_MAX_PENETRATION_ATR",
                str(self.SWEEP_MAX_PENETRATION_ATR),
            )
        )
        if self.sweep_min_anchor_age < 2:
            raise ValueError("AEGIS_HVG_SWEEP_MIN_AGE must be >= 2")
        if self.sweep_max_anchor_age < self.sweep_min_anchor_age:
            raise ValueError("AEGIS_HVG_SWEEP_MAX_AGE must be >= AEGIS_HVG_SWEEP_MIN_AGE")
        if self.sweep_min_wick_ratio < 0.0 or self.sweep_min_wick_ratio > 1.0:
            raise ValueError("AEGIS_HVG_SWEEP_MIN_WICK must be in [0, 1]")
        if self.sweep_max_penetration_atr < 0.0:
            raise ValueError("AEGIS_HVG_SWEEP_MAX_PENETRATION_ATR must be >= 0")

        self.log_dir: str = log_dir or os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
        self.telemetry_file: str = os.path.join(self.log_dir, "hvg_shadow_telemetry.jsonl")

        # -----------------------------------------------------------------
        # Pre-allocated 1-Minute Circular Ring Buffer Storage (Size 60)
        # -----------------------------------------------------------------
        self._buf_opens: List[float] = [0.0] * self.CAPACITY
        self._buf_highs: List[float] = [0.0] * self.CAPACITY
        self._buf_lows: List[float] = [0.0] * self.CAPACITY
        self._buf_closes: List[float] = [0.0] * self.CAPACITY
        self._buf_atrs: List[float] = [0.0] * self.CAPACITY
        self._buf_vwaps: List[float] = [0.0] * self.CAPACITY
        self._buf_timestamps: List[float] = [0.0] * self.CAPACITY

        self._head: int = -1
        self._count: int = 0

        # Pre-allocated Scratch Arrays for 1m Monotonic Stack
        self._scratch_highs: List[float] = [0.0] * self.CAPACITY
        self._scratch_lows: List[float] = [0.0] * self.CAPACITY
        self._deg_upper: List[int] = [0] * self.CAPACITY
        self._deg_lower: List[int] = [0] * self.CAPACITY

        # Internal pre-allocated stack reusable lists
        self._stack_upper: List[int] = []
        self._stack_lower: List[int] = []

        # -----------------------------------------------------------------
        # Clock-Aligned 5-Minute In-Flight Bar Aggregator State
        # -----------------------------------------------------------------
        self._curr_5m_bucket: int = -1
        self._curr_5m_day: Tuple[int, int, int] = (-1, -1, -1)
        self._in_flight_5m_open: float = 0.0
        self._in_flight_5m_high: float = 0.0
        self._in_flight_5m_low: float = 0.0
        self._in_flight_5m_close: float = 0.0
        self._in_flight_5m_atr: float = 0.0
        self._in_flight_5m_ts_start: float = 0.0
        self._in_flight_5m_bar_count: int = 0

        # -----------------------------------------------------------------
        # Pre-allocated 5-Minute Circular Ring Buffer Storage (Size 30)
        # -----------------------------------------------------------------
        self._buf_5m_opens: List[float] = [0.0] * self.CAPACITY_5M
        self._buf_5m_highs: List[float] = [0.0] * self.CAPACITY_5M
        self._buf_5m_lows: List[float] = [0.0] * self.CAPACITY_5M
        self._buf_5m_closes: List[float] = [0.0] * self.CAPACITY_5M
        self._buf_5m_atrs: List[float] = [0.0] * self.CAPACITY_5M
        self._buf_5m_timestamps: List[float] = [0.0] * self.CAPACITY_5M

        self._head_5m: int = -1
        self._count_5m: int = 0

        # Pre-allocated Scratch Arrays for 5m Monotonic Stack
        self._scratch_5m_highs: List[float] = [0.0] * self.CAPACITY_5M
        self._scratch_5m_lows: List[float] = [0.0] * self.CAPACITY_5M
        self._deg_5m_upper: List[int] = [0] * self.CAPACITY_5M
        self._deg_5m_lower: List[int] = [0] * self.CAPACITY_5M

        # -----------------------------------------------------------------
        # Forward Telemetry Bounded Queue (Max 50 pending records)
        # -----------------------------------------------------------------
        self._pending_telemetry: deque[PendingTelemetryRecord] = deque(maxlen=self.MAX_PENDING)

    @property
    def bar_count(self) -> int:
        return self._count

    @property
    def bar_count_5m(self) -> int:
        return self._count_5m

    def add_bar(
        self,
        open_price: float,
        high_price: float,
        low_price: float,
        close_price: float,
        atr: float,
        vwap: float,
        timestamp: float = 0.0,
    ) -> None:
        """
        Pushes a finalized 1-minute bar into the circular ring buffer
        and updates the clock-aligned 5-minute bar aggregator.
        Zero heap allocation.
        """
        ts = float(timestamp or time.time())
        if ts > 1e11:
            ts = ts / 1000.0  # Handle millisecond timestamps gracefully

        # 1. Update 1-minute Circular Ring Buffer
        self._head = (self._head + 1) % self.CAPACITY
        self._buf_opens[self._head] = float(open_price)
        self._buf_highs[self._head] = float(high_price)
        self._buf_lows[self._head] = float(low_price)
        self._buf_closes[self._head] = float(close_price)
        self._buf_atrs[self._head] = float(atr)
        self._buf_vwaps[self._head] = float(vwap)
        self._buf_timestamps[self._head] = ts

        if self._count < self.CAPACITY:
            self._count += 1

        # 2. Clock-Aligned 5-Minute Aggregation (IST 09:15 / 09:20 Boundary Lock)
        try:
            d = datetime.fromtimestamp(ts, tz=self.IST_TZ)
            day_key = (d.year, d.month, d.day)
            b5 = (d.hour * 60 + d.minute) // 5
        except Exception:
            day_key = (1970, 1, 1)
            b5 = int((ts + 19800) // 300)

        # Handle Day-Change (Overnight boundary separation)
        if self._curr_5m_day != (-1, -1, -1) and day_key != self._curr_5m_day:
            if self._in_flight_5m_bar_count > 0:
                self._push_5m_bar(
                    self._in_flight_5m_open,
                    self._in_flight_5m_high,
                    self._in_flight_5m_low,
                    self._in_flight_5m_close,
                    self._in_flight_5m_atr,
                    self._in_flight_5m_ts_start,
                )
            self._curr_5m_day = day_key
            self._curr_5m_bucket = b5
            self._in_flight_5m_open = float(open_price)
            self._in_flight_5m_high = float(high_price)
            self._in_flight_5m_low = float(low_price)
            self._in_flight_5m_close = float(close_price)
            self._in_flight_5m_atr = float(atr)
            self._in_flight_5m_ts_start = ts
            self._in_flight_5m_bar_count = 1
        elif self._curr_5m_bucket != -1 and b5 != self._curr_5m_bucket:
            # 5m bucket rolled over within the same day!
            if self._in_flight_5m_bar_count > 0:
                self._push_5m_bar(
                    self._in_flight_5m_open,
                    self._in_flight_5m_high,
                    self._in_flight_5m_low,
                    self._in_flight_5m_close,
                    self._in_flight_5m_atr,
                    self._in_flight_5m_ts_start,
                )
            self._curr_5m_bucket = b5
            self._in_flight_5m_open = float(open_price)
            self._in_flight_5m_high = float(high_price)
            self._in_flight_5m_low = float(low_price)
            self._in_flight_5m_close = float(close_price)
            self._in_flight_5m_atr = float(atr)
            self._in_flight_5m_ts_start = ts
            self._in_flight_5m_bar_count = 1
        else:
            if self._in_flight_5m_bar_count == 0:
                self._curr_5m_day = day_key
                self._curr_5m_bucket = b5
                self._in_flight_5m_open = float(open_price)
                self._in_flight_5m_high = float(high_price)
                self._in_flight_5m_low = float(low_price)
                self._in_flight_5m_close = float(close_price)
                self._in_flight_5m_atr = float(atr)
                self._in_flight_5m_ts_start = ts
                self._in_flight_5m_bar_count = 1
            else:
                if high_price > self._in_flight_5m_high:
                    self._in_flight_5m_high = float(high_price)
                if low_price < self._in_flight_5m_low:
                    self._in_flight_5m_low = float(low_price)
                self._in_flight_5m_close = float(close_price)
                self._in_flight_5m_atr = float(atr)
                self._in_flight_5m_bar_count += 1

    def _calculate_current_5m_atr(self, period: int = 14) -> float:
        """Calculates session-local ATR from finalized 5-minute True Ranges."""
        if self._count_5m <= 0 or self._head_5m < 0:
            return 0.0

        current_ts = self._buf_5m_timestamps[self._head_5m]
        current_dt = datetime.fromtimestamp(current_ts, tz=self.IST_TZ)
        current_day = (current_dt.year, current_dt.month, current_dt.day)

        total_tr = 0.0
        samples = 0
        limit = min(period, self._count_5m)

        for offset in range(limit):
            idx = (self._head_5m - offset) % self.CAPACITY_5M
            bar_ts = self._buf_5m_timestamps[idx]
            bar_dt = datetime.fromtimestamp(bar_ts, tz=self.IST_TZ)
            bar_day = (bar_dt.year, bar_dt.month, bar_dt.day)
            if bar_day != current_day:
                break

            high = self._buf_5m_highs[idx]
            low = self._buf_5m_lows[idx]
            tr = high - low

            if offset + 1 < self._count_5m:
                prev_idx = (idx - 1) % self.CAPACITY_5M
                prev_ts = self._buf_5m_timestamps[prev_idx]
                prev_dt = datetime.fromtimestamp(prev_ts, tz=self.IST_TZ)
                prev_day = (prev_dt.year, prev_dt.month, prev_dt.day)
                if prev_day == current_day:
                    prev_close = self._buf_5m_closes[prev_idx]
                    gap_up = abs(high - prev_close)
                    gap_down = abs(low - prev_close)
                    if gap_up > tr:
                        tr = gap_up
                    if gap_down > tr:
                        tr = gap_down

            total_tr += tr
            samples += 1

        return total_tr / samples if samples > 0 else 0.0

    def _push_5m_bar(
        self,
        open_price: float,
        high_price: float,
        low_price: float,
        close_price: float,
        atr: float,
        timestamp: float,
    ) -> None:
        """Pushes a finalized 5-minute bar and stores a true 5-minute ATR.

        The legacy ``atr`` argument is retained for caller compatibility but is
        intentionally ignored; 5-minute volatility must be derived from 5-minute OHLC.
        """
        self._head_5m = (self._head_5m + 1) % self.CAPACITY_5M
        self._buf_5m_opens[self._head_5m] = float(open_price)
        self._buf_5m_highs[self._head_5m] = float(high_price)
        self._buf_5m_lows[self._head_5m] = float(low_price)
        self._buf_5m_closes[self._head_5m] = float(close_price)
        self._buf_5m_timestamps[self._head_5m] = float(timestamp)
        if self._count_5m < self.CAPACITY_5M:
            self._count_5m += 1
        self._buf_5m_atrs[self._head_5m] = self._calculate_current_5m_atr()

    def _unroll_to_scratch(self) -> int:
        """
        Unrolls 1m circular ring buffer Highs and Lows into pre-allocated scratch arrays.
        Uses C-level fast slice assignment (~3.9 microseconds).
        """
        n = self._count
        if n == 0:
            return 0

        if n < self.CAPACITY:
            self._scratch_highs[:n] = self._buf_highs[:n]
            self._scratch_lows[:n] = self._buf_lows[:n]
        else:
            start = (self._head + 1) % self.CAPACITY
            rem = self.CAPACITY - start
            self._scratch_highs[:rem] = self._buf_highs[start:]
            self._scratch_highs[rem:] = self._buf_highs[:start]
            self._scratch_lows[:rem] = self._buf_lows[start:]
            self._scratch_lows[rem:] = self._buf_lows[:start]

        return n

    def _unroll_5m_to_scratch(self) -> int:
        """
        Unrolls 5m circular ring buffer Highs and Lows into pre-allocated scratch arrays.
        """
        n = self._count_5m
        if n == 0:
            return 0

        if n < self.CAPACITY_5M:
            self._scratch_5m_highs[:n] = self._buf_5m_highs[:n]
            self._scratch_5m_lows[:n] = self._buf_5m_lows[:n]
        else:
            start = (self._head_5m + 1) % self.CAPACITY_5M
            rem = self.CAPACITY_5M - start
            self._scratch_5m_highs[:rem] = self._buf_5m_highs[start:]
            self._scratch_5m_highs[rem:] = self._buf_5m_highs[:start]
            self._scratch_5m_lows[:rem] = self._buf_5m_lows[start:]
            self._scratch_5m_lows[rem:] = self._buf_5m_lows[:start]

        return n

    def compute_hvg_degrees(self, n: int) -> Tuple[List[int], List[int]]:
        """
        O(N) strict Horizontal Visibility Graph degree computation for 1-minute bars.

        Equal-height nodes remain mutually visible but the newest equal node replaces
        the older stack representative so an equal plateau cannot create false long edges.
        """
        for i in range(n):
            self._deg_upper[i] = 0
            self._deg_lower[i] = 0

        # Upper strict HVG on Highs
        self._stack_upper.clear()
        for i in range(n):
            val = self._scratch_highs[i]
            while self._stack_upper and self._scratch_highs[self._stack_upper[-1]] < val:
                top_idx = self._stack_upper.pop()
                self._deg_upper[i] += 1
                self._deg_upper[top_idx] += 1

            if self._stack_upper:
                top_idx = self._stack_upper[-1]
                self._deg_upper[i] += 1
                self._deg_upper[top_idx] += 1
                if self._scratch_highs[top_idx] == val:
                    self._stack_upper.pop()

            self._stack_upper.append(i)

        # Lower strict HVG on Lows
        self._stack_lower.clear()
        for i in range(n):
            val = self._scratch_lows[i]
            while self._stack_lower and self._scratch_lows[self._stack_lower[-1]] > val:
                top_idx = self._stack_lower.pop()
                self._deg_lower[i] += 1
                self._deg_lower[top_idx] += 1

            if self._stack_lower:
                top_idx = self._stack_lower[-1]
                self._deg_lower[i] += 1
                self._deg_lower[top_idx] += 1
                if self._scratch_lows[top_idx] == val:
                    self._stack_lower.pop()

            self._stack_lower.append(i)

        return self._deg_upper, self._deg_lower

    def compute_5m_hvg_degrees(self, n: int) -> Tuple[List[int], List[int]]:
        """O(N) strict Horizontal Visibility Graph degree computation for 5-minute bars."""
        for i in range(n):
            self._deg_5m_upper[i] = 0
            self._deg_5m_lower[i] = 0

        # Upper strict HVG on 5m Highs
        self._stack_upper.clear()
        for i in range(n):
            val = self._scratch_5m_highs[i]
            while self._stack_upper and self._scratch_5m_highs[self._stack_upper[-1]] < val:
                top_idx = self._stack_upper.pop()
                self._deg_5m_upper[i] += 1
                self._deg_5m_upper[top_idx] += 1

            if self._stack_upper:
                top_idx = self._stack_upper[-1]
                self._deg_5m_upper[i] += 1
                self._deg_5m_upper[top_idx] += 1
                if self._scratch_5m_highs[top_idx] == val:
                    self._stack_upper.pop()

            self._stack_upper.append(i)

        # Lower strict HVG on 5m Lows
        self._stack_lower.clear()
        for i in range(n):
            val = self._scratch_5m_lows[i]
            while self._stack_lower and self._scratch_5m_lows[self._stack_lower[-1]] > val:
                top_idx = self._stack_lower.pop()
                self._deg_5m_lower[i] += 1
                self._deg_5m_lower[top_idx] += 1

            if self._stack_lower:
                top_idx = self._stack_lower[-1]
                self._deg_5m_lower[i] += 1
                self._deg_5m_lower[top_idx] += 1
                if self._scratch_5m_lows[top_idx] == val:
                    self._stack_lower.pop()

            self._stack_lower.append(i)

        return self._deg_5m_upper, self._deg_5m_lower

    def check_5m_structural_alignment(
        self,
        direction: str,
        current_spot: float,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """Evaluates 5-minute macro alignment using completed 5m strict HVG structure."""
        n5 = self._unroll_5m_to_scratch()
        if n5 < self.MIN_BARS_5M:
            return True, "5m_warmup_grace", {"n_5m_bars": n5}

        self.compute_5m_hvg_degrees(n5)

        sum_u5 = 0
        sum_l5 = 0
        for i in range(n5):
            sum_u5 += self._deg_5m_upper[i]
            sum_l5 += self._deg_5m_lower[i]
        denom_u5 = (sum_u5 / n5) + self.EPSILON
        denom_l5 = (sum_l5 / n5) + self.EPSILON

        last_5m_close = self._buf_5m_closes[self._head_5m]
        last_5m_open = self._buf_5m_opens[self._head_5m]
        last_5m_high = self._buf_5m_highs[self._head_5m]
        last_5m_low = self._buf_5m_lows[self._head_5m]
        last_5m_atr = self._buf_5m_atrs[self._head_5m]
        if last_5m_atr <= 0:
            last_5m_atr = max(current_spot * 0.0015, 15.0)

        proximity_hurdle = self.proximity_5m_mult * last_5m_atr

        if direction.upper() in ("UP", "CALL"):
            # Latest completed 5m bar can break a previously intact prominent support.
            suffix_min = float("inf")
            for k in range(n5 - 2, -1, -1):
                # suffix_min represents lows in (k, n5-1), excluding the latest bar.
                prom_l5 = self._deg_5m_lower[k] / denom_l5
                s_star_5m = self._scratch_5m_lows[k]
                anchor_intact_before_last = suffix_min >= s_star_5m
                if prom_l5 >= self.MIN_PROMINENCE and anchor_intact_before_last:
                    rng_5m = last_5m_high - last_5m_low
                    if (
                        rng_5m > 0
                        and last_5m_close < s_star_5m
                        and (last_5m_open - last_5m_close) / rng_5m >= 0.40
                    ):
                        return False, "5m_macro_support_breakdown_opposed", {
                            "5m_anchor_support": s_star_5m,
                            "5m_prominence": round(prom_l5, 2),
                            "5m_close": round(last_5m_close, 2),
                        }
                if s_star_5m < suffix_min:
                    suffix_min = s_star_5m

            # Active overhead resistance must remain unbroken after its anchor formation.
            suffix_max = float("-inf")
            for k in range(n5 - 1, -1, -1):
                prom_u5 = self._deg_5m_upper[k] / denom_u5
                r_star_5m = self._scratch_5m_highs[k]
                anchor_unbroken = suffix_max <= r_star_5m
                if prom_u5 >= self.MIN_PROMINENCE and anchor_unbroken:
                    dist = r_star_5m - current_spot
                    if 0.0 < dist < proximity_hurdle:
                        return False, "5m_overhead_resistance_too_close", {
                            "5m_overhead_resistance": round(r_star_5m, 2),
                            "dist_pts": round(dist, 2),
                            "hurdle_pts": round(proximity_hurdle, 2),
                            "atr_5m": round(last_5m_atr, 2),
                        }
                if r_star_5m > suffix_max:
                    suffix_max = r_star_5m

            return True, "5m_aligned_call", {"n_5m_bars": n5, "atr_5m": round(last_5m_atr, 2)}

        elif direction.upper() in ("DOWN", "PUT"):
            # Latest completed 5m bar can break a previously intact prominent resistance.
            suffix_max = float("-inf")
            for k in range(n5 - 2, -1, -1):
                prom_u5 = self._deg_5m_upper[k] / denom_u5
                r_star_5m = self._scratch_5m_highs[k]
                anchor_intact_before_last = suffix_max <= r_star_5m
                if prom_u5 >= self.MIN_PROMINENCE and anchor_intact_before_last:
                    rng_5m = last_5m_high - last_5m_low
                    if (
                        rng_5m > 0
                        and last_5m_close > r_star_5m
                        and (last_5m_close - last_5m_open) / rng_5m >= 0.40
                    ):
                        return False, "5m_macro_resistance_breakout_opposed", {
                            "5m_anchor_resistance": r_star_5m,
                            "5m_prominence": round(prom_u5, 2),
                            "5m_close": round(last_5m_close, 2),
                        }
                if r_star_5m > suffix_max:
                    suffix_max = r_star_5m

            # Active underlying support must remain unbroken after its anchor formation.
            suffix_min = float("inf")
            for k in range(n5 - 1, -1, -1):
                prom_l5 = self._deg_5m_lower[k] / denom_l5
                s_star_5m = self._scratch_5m_lows[k]
                anchor_unbroken = suffix_min >= s_star_5m
                if prom_l5 >= self.MIN_PROMINENCE and anchor_unbroken:
                    dist = current_spot - s_star_5m
                    if 0.0 < dist < proximity_hurdle:
                        return False, "5m_underlying_support_too_close", {
                            "5m_underlying_support": round(s_star_5m, 2),
                            "dist_pts": round(dist, 2),
                            "hurdle_pts": round(proximity_hurdle, 2),
                            "atr_5m": round(last_5m_atr, 2),
                        }
                if s_star_5m < suffix_min:
                    suffix_min = s_star_5m

            return True, "5m_aligned_put", {"n_5m_bars": n5, "atr_5m": round(last_5m_atr, 2)}

        return True, "5m_aligned_unknown", {}

    def check_l2_wall(
        self,
        direction: str,
        depth_history: Optional[List[Tuple[float, float, float, float]]] = None,
        current_time: float = 0.0,
    ) -> Tuple[bool, str]:
        """Checks whether an opposing depth wall has persisted continuously for >=400ms."""
        if not depth_history:
            return True, "no_depth_history"

        now = current_time if current_time > 0 else time.time()
        min_persistence = 0.400
        max_history = 2.0

        valid_snapshots = [s for s in depth_history if 0.0 <= (now - s[0]) <= max_history]
        if len(valid_snapshots) < 2:
            return True, "insufficient_persistent_snapshots"

        is_call = direction.upper() in ("UP", "CALL")
        is_put = direction.upper() in ("DOWN", "PUT")

        def _has_opposing_wall(bid: float, ask: float, imb: float) -> bool:
            if is_call:
                ratio = ask / (bid + self.EPSILON)
                return ratio >= 1.80 or imb < -0.35
            elif is_put:
                ratio = bid / (ask + self.EPSILON)
                return ratio >= 1.80 or imb > 0.35
            return False

        # If the latest snapshot doesn't have an opposing wall, the path is clear
        latest_ts, latest_bid, latest_ask, latest_imb = valid_snapshots[-1]
        if not _has_opposing_wall(latest_bid, latest_ask, latest_imb):
            return True, "l2_wall_passed"

        # Walk backwards from latest to find continuous duration of the opposing wall
        first_wall_ts = latest_ts
        for ts, bid, ask, imb in reversed(valid_snapshots[:-1]):
            if _has_opposing_wall(bid, ask, imb):
                first_wall_ts = ts
            else:
                break

        duration = now - first_wall_ts
        if duration >= min_persistence:
            if is_call:
                return False, "persistent_opposing_ask_wall_blocked"
            if is_put:
                return False, "persistent_opposing_bid_wall_blocked"

        return True, "transient_flicker_spoof_ignored"

    def evaluate_phase_shift(
        self,
        direction: str,
        ema_20: float = 0.0,
        ema_50: float = 0.0,
        current_vwap: float = 0.0,
        l2_depth_history: Optional[List[Tuple[float, float, float, float]]] = None,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Deterministic Network Phase Shift Breakout Evaluator (Pillar 4).
        Evaluates whether the most recent closed bar (Bar T) triggers a Phase Shift Breakout.
        Zero heap allocation on candle close.
        """
        n = self._unroll_to_scratch()
        if n < self.MIN_BARS:
            return False, "insufficient_bars", {}

        # 1. Freeze historical 1m HVG topology at T-1. Bar T is deliberately
        # excluded so its breakout edge cannot manufacture anchor prominence.
        pre_n = n - 1
        self.compute_hvg_degrees(pre_n)

        # 2. Mean Degree Calculation over the pre-breakout graph only.
        sum_u = 0
        sum_l = 0
        for i in range(pre_n):
            sum_u += self._deg_upper[i]
            sum_l += self._deg_lower[i]

        mean_u = (sum_u / pre_n) if pre_n > 0 else 1.0
        mean_l = (sum_l / pre_n) if pre_n > 0 else 1.0

        denom_u = mean_u + self.EPSILON
        denom_l = mean_l + self.EPSILON

        # Bar T is the most recent closed candle at _head
        t_open = self._buf_opens[self._head]
        t_high = self._buf_highs[self._head]
        t_low = self._buf_lows[self._head]
        t_close = self._buf_closes[self._head]
        t_atr = self._buf_atrs[self._head] if self._buf_atrs[self._head] > 0 else (t_close * 0.001)
        t_vwap = current_vwap if current_vwap > 0 else self._buf_vwaps[self._head]
        t_ts = self._buf_timestamps[self._head]

        total_range = t_high - t_low
        if total_range <= 0:
            return False, "zero_range", {}

        ref_spot = t_close if t_close > 0 else 24000.0
        buffer_pts = max(5.0, (ref_spot / 24000.0) * 5.0)

        # 3. 5-Minute Macro Structural Alignment Check
        ok_5m, reason_5m, details_5m = self.check_5m_structural_alignment(direction, t_close)
        if not ok_5m:
            return False, reason_5m, details_5m

        # 4. Anti-Spoofing L2 Wall Shield Check
        ok_wall, reason_wall = self.check_l2_wall(direction, l2_depth_history, t_ts)
        if not ok_wall:
            return False, reason_wall, {"l2_wall_status": reason_wall}

        # 5. Pre-breakout compression calculation directly from buffer
        comp_count = min(4, n - 1)
        if comp_count < 2:
            return False, "insufficient_compression_bars", {}

        idx_1 = (self._head - 1) % self.CAPACITY
        max_comp_close = self._buf_closes[idx_1]
        min_comp_close = self._buf_closes[idx_1]
        for offset in range(2, comp_count + 1):
            c_val = self._buf_closes[(self._head - offset) % self.CAPACITY]
            if c_val > max_comp_close:
                max_comp_close = c_val
            if c_val < min_comp_close:
                min_comp_close = c_val

        # -----------------------------------------------------------------
        # EVALUATE CALL (Bullish Network Phase Shift Breakout)
        # -----------------------------------------------------------------
        if direction.upper() in ("UP", "CALL"):
            # 1. VWAP alignment
            if t_vwap > 0 and t_close <= t_vwap:
                return False, "below_vwap", {}

            # Optional legacy EMA trend check (disabled by default in favor of pure 5m price action)
            if self.enforce_ema_trend and ema_20 > 0 and ema_50 > 0 and ema_20 < ema_50:
                return False, "bear_overlap_ema_disallowed", {}

            # 2. Candle quality: Must be bullish impulse with solid body
            if t_close <= t_open:
                return False, "not_bullish_candle", {}
            body = t_close - t_open
            body_ratio = body / total_range
            if body_ratio < 0.40:
                return False, "weak_candle_body", {}

            # 3. Evaluate Candidate Resistance Anchors in [0, n-4]
            best_anchor = None
            best_prom = -1.0

            for k in range(n - 3):
                prom_u = self._deg_upper[k] / denom_u
                if prom_u < self.MIN_PROMINENCE:
                    continue
                r_star = self._scratch_highs[k]

                # Pre-condition: all prior compression closes must remain at/below R*
                # and the prior compression close must sit within 1.0 * ATR of R*.
                if max_comp_close > r_star:
                    continue
                if (r_star - max_comp_close) > max(1.0 * t_atr, buffer_pts):
                    continue

                # Fresh breakout semantics: T must cross the anchor, not gap above it.
                if t_open > r_star or t_close <= r_star:
                    continue

                # Extension Cap: Prevent chasing exhausted climaxes
                ext_cap = max(1.25 * t_atr, buffer_pts)
                if (t_close - r_star) > ext_cap:
                    continue

                # Candidate valid: select highest prominence (tie-break on higher level)
                if prom_u > best_prom or (abs(prom_u - best_prom) < 1e-4 and r_star > (best_anchor[1] if best_anchor else 0)):
                    best_prom = prom_u
                    best_anchor = (k, r_star, prom_u)

            if best_anchor is None:
                has_any_prom = any((self._deg_upper[k] / denom_u) >= self.MIN_PROMINENCE for k in range(n - 3))
                if not has_any_prom:
                    return False, "no_prominent_resistance_anchor", {}
                return False, "pre_breakout_not_compressed_or_no_breakout", {}

            anchor_idx, r_star, prom_u = best_anchor
            sl_price = round(r_star - buffer_pts, 2)
            details = {
                "pa_setup_name": "hvg_network_phase_shift",
                "pillar": 4,
                "direction": "CALL",
                "anchor_price": round(r_star, 2),
                "anchor_prominence": round(prom_u, 3),
                "anchor_index": anchor_idx,
                "breakout_price": round(t_close, 2),
                "bar_open": round(t_open, 2),
                "bar_high": round(t_high, 2),
                "bar_low": round(t_low, 2),
                "bar_close": round(t_close, 2),
                "body_ratio": round(body_ratio, 3),
                "sl_price": sl_price,
                "buffer_pts": round(buffer_pts, 2),
                "atr_14": round(t_atr, 2),
                "vwap": round(t_vwap, 2),
                "ema_20": round(ema_20, 2),
                "ema_50": round(ema_50, 2),
                "timestamp": t_ts,
                "strategy_variant": "HVG_P4",
                "event_id": f"HVG_P4_{int(t_ts)}_CALL",
                "anchor_age_bars": (n - 1) - anchor_idx,
                "anchor_degree": self._deg_upper[anchor_idx],
                "distance_to_vwap_atr": round((t_close - t_vwap) / t_atr, 3) if (t_atr > 0 and t_vwap > 0) else 0.0,
                "is_shadow_only": self.shadow_only,
                "macro_5m_status": reason_5m,
                "l2_wall_status": reason_wall,
            }

            signal = HVGSetupSignal(
                timestamp=t_ts,
                direction="CALL",
                setup_name="hvg_network_phase_shift",
                pillar=4,
                anchor_price=round(r_star, 2),
                anchor_prominence=round(prom_u, 3),
                anchor_index=anchor_idx,
                breakout_price=round(t_close, 2),
                bar_open=round(t_open, 2),
                bar_high=round(t_high, 2),
                bar_low=round(t_low, 2),
                bar_close=round(t_close, 2),
                body_ratio=round(body_ratio, 3),
                sl_price=sl_price,
                buffer_pts=round(buffer_pts, 2),
                atr_14=round(t_atr, 2),
                vwap=round(t_vwap, 2),
                ema_20=round(ema_20, 2),
                ema_50=round(ema_50, 2),
                is_shadow_only=self.shadow_only,
                macro_5m_status=reason_5m,
                l2_wall_status=reason_wall,
                strategy_variant="HVG_P4",
                event_id=f"HVG_P4_{int(t_ts)}_CALL",
                anchor_age=(n - 1) - anchor_idx,
                anchor_degree=self._deg_upper[anchor_idx],
                distance_to_vwap_atr=round((t_close - t_vwap) / t_atr, 3) if (t_atr > 0 and t_vwap > 0) else 0.0,
            )
            self._record_shadow_signal(signal)
            return True, "hvg_phase_shift_call_breakout", details

        # -----------------------------------------------------------------
        # EVALUATE PUT (Bearish Network Phase Shift Breakdown)
        # -----------------------------------------------------------------
        elif direction.upper() in ("DOWN", "PUT"):
            # 1. VWAP alignment
            if t_vwap > 0 and t_close >= t_vwap:
                return False, "above_vwap", {}

            # Optional legacy EMA trend check
            if self.enforce_ema_trend and ema_20 > 0 and ema_50 > 0 and ema_20 > ema_50:
                return False, "bull_overlap_ema_disallowed", {}

            # 2. Candle quality: Must be bearish impulse with solid body
            if t_close >= t_open:
                return False, "not_bearish_candle", {}
            body = t_open - t_close
            body_ratio = body / total_range
            if body_ratio < 0.40:
                return False, "weak_candle_body", {}

            # 3. Evaluate Candidate Support Anchors in [0, n-4]
            best_anchor = None
            best_prom = -1.0

            for k in range(n - 3):
                prom_l = self._deg_lower[k] / denom_l
                if prom_l < self.MIN_PROMINENCE:
                    continue
                s_star = self._scratch_lows[k]

                # Pre-condition: all prior compression closes must remain at/above S*
                # and the prior compression close must sit within 1.0 * ATR of S*.
                if min_comp_close < s_star:
                    continue
                if (min_comp_close - s_star) > max(1.0 * t_atr, buffer_pts):
                    continue

                # Fresh breakdown semantics: T must cross the anchor, not gap below it.
                if t_open < s_star or t_close >= s_star:
                    continue

                # Extension Cap
                ext_cap = max(1.25 * t_atr, buffer_pts)
                if (s_star - t_close) > ext_cap:
                    continue

                if prom_l > best_prom or (abs(prom_l - best_prom) < 1e-4 and s_star < (best_anchor[1] if best_anchor else float("inf"))):
                    best_prom = prom_l
                    best_anchor = (k, s_star, prom_l)

            if best_anchor is None:
                has_any_prom = any((self._deg_lower[k] / denom_l) >= self.MIN_PROMINENCE for k in range(n - 3))
                if not has_any_prom:
                    return False, "no_prominent_support_anchor", {}
                return False, "pre_breakdown_not_compressed_or_no_breakdown", {}

            anchor_idx, s_star, prom_l = best_anchor
            sl_price = round(s_star + buffer_pts, 2)
            details = {
                "pa_setup_name": "hvg_network_phase_shift",
                "pillar": 4,
                "direction": "PUT",
                "anchor_price": round(s_star, 2),
                "anchor_prominence": round(prom_l, 3),
                "anchor_index": anchor_idx,
                "breakout_price": round(t_close, 2),
                "bar_open": round(t_open, 2),
                "bar_high": round(t_high, 2),
                "bar_low": round(t_low, 2),
                "bar_close": round(t_close, 2),
                "body_ratio": round(body_ratio, 3),
                "sl_price": sl_price,
                "buffer_pts": round(buffer_pts, 2),
                "atr_14": round(t_atr, 2),
                "vwap": round(t_vwap, 2),
                "ema_20": round(ema_20, 2),
                "ema_50": round(ema_50, 2),
                "timestamp": t_ts,
                "strategy_variant": "HVG_P4",
                "event_id": f"HVG_P4_{int(t_ts)}_PUT",
                "anchor_age_bars": (n - 1) - anchor_idx,
                "anchor_degree": self._deg_lower[anchor_idx],
                "distance_to_vwap_atr": round((t_vwap - t_close) / t_atr, 3) if (t_atr > 0 and t_vwap > 0) else 0.0,
                "is_shadow_only": self.shadow_only,
                "macro_5m_status": reason_5m,
                "l2_wall_status": reason_wall,
            }

            signal = HVGSetupSignal(
                timestamp=t_ts,
                direction="PUT",
                setup_name="hvg_network_phase_shift",
                pillar=4,
                anchor_price=round(s_star, 2),
                anchor_prominence=round(prom_l, 3),
                anchor_index=anchor_idx,
                breakout_price=round(t_close, 2),
                bar_open=round(t_open, 2),
                bar_high=round(t_high, 2),
                bar_low=round(t_low, 2),
                bar_close=round(t_close, 2),
                body_ratio=round(body_ratio, 3),
                sl_price=sl_price,
                buffer_pts=round(buffer_pts, 2),
                atr_14=round(t_atr, 2),
                vwap=round(t_vwap, 2),
                ema_20=round(ema_20, 2),
                ema_50=round(ema_50, 2),
                is_shadow_only=self.shadow_only,
                macro_5m_status=reason_5m,
                l2_wall_status=reason_wall,
                strategy_variant="HVG_P4",
                event_id=f"HVG_P4_{int(t_ts)}_PUT",
                anchor_age=(n - 1) - anchor_idx,
                anchor_degree=self._deg_lower[anchor_idx],
                distance_to_vwap_atr=round((t_vwap - t_close) / t_atr, 3) if (t_atr > 0 and t_vwap > 0) else 0.0,
            )
            self._record_shadow_signal(signal)
            return True, "hvg_phase_shift_put_breakdown", details

    def check_topological_sweep_reclaim(
        self,
        direction: str,
        current_vwap: float = 0.0,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Pillar 2 — Topological Structural Sweep & Reclaim.
        Uses:
        - Graph(T-1) strictly for historical anchor discovery (causal freeze).
        - Lower HVG for CALL-side structural support anchors.
        - Upper HVG for PUT-side structural resistance anchors.
        - Historical anchor age constraint [sweep_min_anchor_age, sweep_max_anchor_age].
        - Local-extremum validation (no monotonic plateau anchors).
        - Unbroken-anchor validation seeded with bar T-1 (catches breaches up to T-1).
        - Current bar T for fresh breach/reclaim trigger mechanics (no gap-through).
        Complexity: O(N), where N <= CAPACITY.
        """
        n = self._unroll_to_scratch()
        if n < (self.sweep_max_anchor_age + 2):
            return False, "insufficient_bars_for_topological_sweep", {}

        pre_n = n - 1

        # 1. Freeze topology on Graph(T-1)
        self.compute_hvg_degrees(pre_n)
        sum_u = 0
        sum_l = 0
        for i in range(pre_n):
            sum_u += self._deg_upper[i]
            sum_l += self._deg_lower[i]
        denom_u = ((sum_u / pre_n) if pre_n > 0 else 0.0) + self.EPSILON
        denom_l = ((sum_l / pre_n) if pre_n > 0 else 0.0) + self.EPSILON

        # 2. Current closed bar T
        t_idx = self._head
        t_open = self._buf_opens[t_idx]
        t_high = self._buf_highs[t_idx]
        t_low = self._buf_lows[t_idx]
        t_close = self._buf_closes[t_idx]
        t_atr = self._buf_atrs[t_idx]
        t_vwap = current_vwap if current_vwap > 0.0 else self._buf_vwaps[t_idx]
        t_ts = self._buf_timestamps[t_idx]

        total_range = t_high - t_low
        if total_range <= 0.0:
            return False, "zero_range", {}
        if t_atr <= 0.0:
            return False, "atr_unavailable", {}

        direction_u = direction.upper()

        # 3. Signed Direction-Normalized VWAP distance: s * (Close - VWAP) / ATR
        if t_vwap > 0.0 and t_atr > 0.0:
            if direction_u in ("UP", "CALL"):
                dist_vwap_atr = (t_close - t_vwap) / t_atr
            else:
                dist_vwap_atr = (t_vwap - t_close) / t_atr
        else:
            dist_vwap_atr = 0.0

        ref_spot = t_close if t_close > 0.0 else 24000.0
        buffer_pts = max(5.0, (ref_spot / 24000.0) * 5.0)

        # -----------------------------------------------------------------
        # EVALUATE CALL (Bullish Support Sweep & Reclaim)
        # -----------------------------------------------------------------
        if direction_u in ("UP", "CALL"):
            if t_vwap > 0.0 and t_close <= t_vwap:
                return False, "below_vwap", {}
            if t_close <= t_open:
                return False, "not_bullish_reclaim", {}

            lower_wick = min(t_open, t_close) - t_low
            wick_ratio = lower_wick / total_range
            if wick_ratio < self.sweep_min_wick_ratio:
                return False, "insufficient_lower_wick", {}

            # CRITICAL FIX: Seed with bar T-1 (index pre_n - 1)
            post_anchor_min = self._scratch_lows[pre_n - 1]
            best_anchor_idx = -1
            best_anchor_price = float("-inf")
            best_anchor_prom = -1.0
            best_anchor_age = 0
            candidate_count = 0

            # Scan backwards from pre_n - 2 down to 1
            for k in range(pre_n - 2, 0, -1):
                age = (n - 1) - k
                if age > self.sweep_max_anchor_age:
                    break

                if age >= self.sweep_min_anchor_age:
                    s_star = self._scratch_lows[k]
                    left_low = self._scratch_lows[k - 1]
                    right_low = self._scratch_lows[k + 1]
                    is_local_low = (
                        s_star <= left_low
                        and s_star <= right_low
                        and (s_star < left_low or s_star < right_low)
                    )
                    if is_local_low:
                        anchor_unbroken = post_anchor_min >= s_star
                        if anchor_unbroken:
                            prom_l = self._deg_lower[k] / denom_l
                            if prom_l >= self.MIN_PROMINENCE:
                                candidate_count += 1
                                # Fresh sweep geometry (no gap-through)
                                fresh_sweep_geometry = (
                                    t_open >= s_star
                                    and t_low < s_star
                                    and t_close > s_star
                                )
                                if fresh_sweep_geometry:
                                    sweep_depth_pts = s_star - t_low
                                    sweep_depth_atr = sweep_depth_pts / t_atr
                                    penetration_ok = (
                                        self.sweep_max_penetration_atr <= 0.0
                                        or sweep_depth_atr <= self.sweep_max_penetration_atr
                                    )
                                    if penetration_ok:
                                        # Prefer the nearest higher structural support below T
                                        if (
                                            s_star > best_anchor_price
                                            or (
                                                abs(s_star - best_anchor_price) < self.EPSILON
                                                and prom_l > best_anchor_prom
                                            )
                                        ):
                                            best_anchor_idx = k
                                            best_anchor_price = s_star
                                            best_anchor_prom = prom_l
                                            best_anchor_age = age

                # Maintain min(low[k ... T-1]) for next older anchor k-1
                if self._scratch_lows[k] < post_anchor_min:
                    post_anchor_min = self._scratch_lows[k]

            if best_anchor_idx < 0:
                return False, "no_valid_topological_support_sweep", {}

            s_star = best_anchor_price
            sweep_depth_pts = s_star - t_low
            sweep_depth_atr = sweep_depth_pts / t_atr
            sl_price = round(t_low - buffer_pts, 2)
            event_id = f"TOPO_P2_{int(t_ts)}_CALL"

            details = {
                "pa_setup_name": "topological_sweep_reclaim",
                "pillar": 2,
                "direction": "CALL",
                "strategy_variant": "TOPO_P2",
                "event_id": event_id,
                "anchor_price": round(s_star, 2),
                "anchor_index": best_anchor_idx,
                "anchor_age_bars": best_anchor_age,
                "anchor_degree": self._deg_lower[best_anchor_idx],
                "anchor_prominence": round(best_anchor_prom, 3),
                "sweep_low": round(t_low, 2),
                "reclaim_close": round(t_close, 2),
                "sweep_depth_pts": round(sweep_depth_pts, 2),
                "sweep_depth_atr": round(sweep_depth_atr, 3),
                "wick_ratio": round(wick_ratio, 3),
                "distance_to_vwap_atr": round(dist_vwap_atr, 3),
                "candidate_count": candidate_count,
                "anchor_selection_reason": "nearest_prominent",
                "atr_1m": round(t_atr, 2),
                "vwap": round(t_vwap, 2),
                "sl_price": sl_price,
                "timestamp": t_ts,
            }

            signal = HVGSetupSignal(
                timestamp=t_ts,
                direction="CALL",
                setup_name="topological_sweep_reclaim",
                pillar=2,
                anchor_price=round(s_star, 2),
                anchor_prominence=round(best_anchor_prom, 3),
                anchor_index=best_anchor_idx,
                breakout_price=round(t_close, 2),
                bar_open=round(t_open, 2),
                bar_high=round(t_high, 2),
                bar_low=round(t_low, 2),
                bar_close=round(t_close, 2),
                body_ratio=round((t_close - t_open) / total_range, 3),
                sl_price=sl_price,
                buffer_pts=round(buffer_pts, 2),
                atr_14=round(t_atr, 2),
                vwap=round(t_vwap, 2),
                ema_20=0.0,
                ema_50=0.0,
                is_shadow_only=True,
                macro_5m_status="ALIGNED",
                l2_wall_status="PASS",
                strategy_variant="TOPO_P2",
                event_id=event_id,
                anchor_age=best_anchor_age,
                anchor_degree=self._deg_lower[best_anchor_idx],
                sweep_depth_atr=round(sweep_depth_atr, 3),
                wick_ratio=round(wick_ratio, 3),
                distance_to_vwap_atr=round(dist_vwap_atr, 3),
                candidate_count=candidate_count,
                anchor_selection_reason="nearest_prominent",
            )
            self._record_shadow_signal(signal)
            return True, "topological_sweep_call_reclaim", details

        # -----------------------------------------------------------------
        # EVALUATE PUT (Bearish Resistance Sweep & Reclaim)
        # -----------------------------------------------------------------
        elif direction_u in ("DOWN", "PUT"):
            if t_vwap > 0.0 and t_close >= t_vwap:
                return False, "above_vwap", {}
            if t_close >= t_open:
                return False, "not_bearish_reclaim", {}

            upper_wick = t_high - max(t_open, t_close)
            wick_ratio = upper_wick / total_range
            if wick_ratio < self.sweep_min_wick_ratio:
                return False, "insufficient_upper_wick", {}

            # CRITICAL FIX: Seed with bar T-1 (index pre_n - 1)
            post_anchor_max = self._scratch_highs[pre_n - 1]
            best_anchor_idx = -1
            best_anchor_price = float("inf")
            best_anchor_prom = -1.0
            best_anchor_age = 0
            candidate_count = 0

            for k in range(pre_n - 2, 0, -1):
                age = (n - 1) - k
                if age > self.sweep_max_anchor_age:
                    break

                if age >= self.sweep_min_anchor_age:
                    r_star = self._scratch_highs[k]
                    left_high = self._scratch_highs[k - 1]
                    right_high = self._scratch_highs[k + 1]
                    is_local_high = (
                        r_star >= left_high
                        and r_star >= right_high
                        and (r_star > left_high or r_star > right_high)
                    )
                    if is_local_high:
                        anchor_unbroken = post_anchor_max <= r_star
                        if anchor_unbroken:
                            prom_u = self._deg_upper[k] / denom_u
                            if prom_u >= self.MIN_PROMINENCE:
                                candidate_count += 1
                                fresh_sweep_geometry = (
                                    t_open <= r_star
                                    and t_high > r_star
                                    and t_close < r_star
                                )
                                if fresh_sweep_geometry:
                                    sweep_depth_pts = t_high - r_star
                                    sweep_depth_atr = sweep_depth_pts / t_atr
                                    penetration_ok = (
                                        self.sweep_max_penetration_atr <= 0.0
                                        or sweep_depth_atr <= self.sweep_max_penetration_atr
                                    )
                                    if penetration_ok:
                                        # Prefer the nearest lower structural resistance above T
                                        if (
                                            r_star < best_anchor_price
                                            or (
                                                abs(r_star - best_anchor_price) < self.EPSILON
                                                and prom_u > best_anchor_prom
                                            )
                                        ):
                                            best_anchor_idx = k
                                            best_anchor_price = r_star
                                            best_anchor_prom = prom_u
                                            best_anchor_age = age

                if self._scratch_highs[k] > post_anchor_max:
                    post_anchor_max = self._scratch_highs[k]

            if best_anchor_idx < 0:
                return False, "no_valid_topological_resistance_sweep", {}

            r_star = best_anchor_price
            sweep_depth_pts = t_high - r_star
            sweep_depth_atr = sweep_depth_pts / t_atr
            sl_price = round(t_high + buffer_pts, 2)
            event_id = f"TOPO_P2_{int(t_ts)}_PUT"

            details = {
                "pa_setup_name": "topological_sweep_reclaim",
                "pillar": 2,
                "direction": "PUT",
                "strategy_variant": "TOPO_P2",
                "event_id": event_id,
                "anchor_price": round(r_star, 2),
                "anchor_index": best_anchor_idx,
                "anchor_age_bars": best_anchor_age,
                "anchor_degree": self._deg_upper[best_anchor_idx],
                "anchor_prominence": round(best_anchor_prom, 3),
                "sweep_high": round(t_high, 2),
                "reclaim_close": round(t_close, 2),
                "sweep_depth_pts": round(sweep_depth_pts, 2),
                "sweep_depth_atr": round(sweep_depth_atr, 3),
                "wick_ratio": round(wick_ratio, 3),
                "distance_to_vwap_atr": round(dist_vwap_atr, 3),
                "candidate_count": candidate_count,
                "anchor_selection_reason": "nearest_prominent",
                "atr_1m": round(t_atr, 2),
                "vwap": round(t_vwap, 2),
                "sl_price": sl_price,
                "timestamp": t_ts,
            }

            signal = HVGSetupSignal(
                timestamp=t_ts,
                direction="PUT",
                setup_name="topological_sweep_reclaim",
                pillar=2,
                anchor_price=round(r_star, 2),
                anchor_prominence=round(best_anchor_prom, 3),
                anchor_index=best_anchor_idx,
                breakout_price=round(t_close, 2),
                bar_open=round(t_open, 2),
                bar_high=round(t_high, 2),
                bar_low=round(t_low, 2),
                bar_close=round(t_close, 2),
                body_ratio=round((t_open - t_close) / total_range, 3),
                sl_price=sl_price,
                buffer_pts=round(buffer_pts, 2),
                atr_14=round(t_atr, 2),
                vwap=round(t_vwap, 2),
                ema_20=0.0,
                ema_50=0.0,
                is_shadow_only=True,
                macro_5m_status="ALIGNED",
                l2_wall_status="PASS",
                strategy_variant="TOPO_P2",
                event_id=event_id,
                anchor_age=best_anchor_age,
                anchor_degree=self._deg_upper[best_anchor_idx],
                sweep_depth_atr=round(sweep_depth_atr, 3),
                wick_ratio=round(wick_ratio, 3),
                distance_to_vwap_atr=round(dist_vwap_atr, 3),
                candidate_count=candidate_count,
                anchor_selection_reason="nearest_prominent",
            )
            self._record_shadow_signal(signal)
            return True, "topological_sweep_put_reclaim", details

        return False, "unknown_direction", {}

    def _record_shadow_signal(self, signal: HVGSetupSignal) -> None:
        """Records a new candidate signal for forward outcome telemetry tracking."""
        record = PendingTelemetryRecord(
            signal=signal,
            entry_time=signal.timestamp,
            entry_price=signal.breakout_price,
        )
        self._pending_telemetry.append(record)

    def on_underlying_tick(self, ltp: float, current_time: float) -> None:
        """
        Processes real-time underlying ticks to update forward telemetry metrics
        (30s, 60s, 180s, 300s, MFE, MAE) and flushes completed outcomes to disk.
        """
        if ltp <= 0 or current_time <= 0 or not self._pending_telemetry:
            return

        to_flush: List[PendingTelemetryRecord] = []

        for record in list(self._pending_telemetry):
            dt = current_time - record.entry_time
            if dt < 0:
                continue

            # Directional excursion calculation
            if record.signal.direction == "CALL":
                pnl = ltp - record.entry_price
            else:
                pnl = record.entry_price - ltp

            if pnl > record.mfe:
                record.mfe = round(pnl, 2)
            if pnl < record.mae:
                record.mae = round(pnl, 2)

            # Check snapshot milestones
            if dt >= 30.0 and record.price_30s is None:
                record.price_30s = ltp
            if dt >= 60.0 and record.price_60s is None:
                record.price_60s = ltp
            if dt >= 180.0 and record.price_180s is None:
                record.price_180s = ltp
            if dt >= 300.0 and record.price_300s is None:
                record.price_300s = ltp
                record.completed = True
                record.outcome_status = "COMPLETE"
                to_flush.append(record)

        for rec in to_flush:
            self._flush_telemetry_record(rec)
            try:
                self._pending_telemetry.remove(rec)
            except ValueError:
                pass

    def _flush_telemetry_record(self, record: PendingTelemetryRecord) -> None:
        """Appends a telemetry record to JSONL without fabricating incomplete 300s outcomes."""
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            pnl_300s: Optional[float] = None
            win_300s: Optional[bool] = None
            if record.price_300s is not None:
                if record.signal.direction == "CALL":
                    pnl_300s = record.price_300s - record.entry_price
                else:
                    pnl_300s = record.entry_price - record.price_300s
                win_300s = pnl_300s > 0

            payload = {
                "timestamp": record.signal.timestamp,
                "direction": record.signal.direction,
                "strategy_variant": getattr(record.signal, "strategy_variant", "HVG_P4"),
                "event_id": getattr(record.signal, "event_id", ""),
                "setup_name": record.signal.setup_name,
                "pillar": record.signal.pillar,
                "anchor_price": record.signal.anchor_price,
                "anchor_prominence": record.signal.anchor_prominence,
                "anchor_age": getattr(record.signal, "anchor_age", 0),
                "anchor_degree": getattr(record.signal, "anchor_degree", 0),
                "sweep_depth_atr": getattr(record.signal, "sweep_depth_atr", 0.0),
                "wick_ratio": getattr(record.signal, "wick_ratio", 0.0),
                "distance_to_vwap_atr": getattr(record.signal, "distance_to_vwap_atr", 0.0),
                "candidate_count": getattr(record.signal, "candidate_count", 0),
                "anchor_selection_reason": getattr(record.signal, "anchor_selection_reason", "nearest_prominent"),
                "entry_price": record.entry_price,
                "mfe": record.mfe,
                "mae": record.mae,
                "pnl_300s": round(pnl_300s, 2) if pnl_300s is not None else None,
                "win_300s": win_300s,
                "outcome_status": record.outcome_status,
                "price_30s": record.price_30s,
                "price_60s": record.price_60s,
                "price_180s": record.price_180s,
                "price_300s": record.price_300s,
                "macro_5m_status": record.signal.macro_5m_status,
                "l2_wall_status": record.signal.l2_wall_status,
                "is_shadow_only": record.signal.is_shadow_only,
            }
            with open(self.telemetry_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload) + "\n")
        except Exception as e:
            logger.warning(f"[HVGEngine] Failed to flush telemetry record: {e}")

    def flush_all_pending(self) -> None:
        """Flushes pending records as censored at market close when 300s is unavailable."""
        if not self._pending_telemetry:
            return
        for rec in list(self._pending_telemetry):
            if not rec.completed:
                rec.outcome_status = "CENSORED_MARKET_CLOSE"
            self._flush_telemetry_record(rec)
        self._pending_telemetry.clear()

    def get_pending_count(self) -> int:
        return len(self._pending_telemetry)
