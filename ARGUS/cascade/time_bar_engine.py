"""
AEGIS TimeBarEngine — Time-Based OHLC, EMA Slope, ATR Expansion & Market Structure.

Architecture:
1. Real-time tick aggregation into true time-bucketed 1-minute and 3-minute OHLC bars.
2. True EMA(9) and EMA Slope on completed time bars (independent of tick frequency).
3. 14-period ATR and Continuous ATR Ratio (Current 1-min ATR / Baseline 30-min ATR).
4. 3-State Market Structure Detector:
   - ALIGNED: HH/HL for Bullish, LH/LL for Bearish
   - NEUTRAL: Range consolidation / inside bar
   - OPPOSED: Counter-trend swing violation (Hard Veto condition)
"""

from __future__ import annotations
import math
from collections import deque
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass


@dataclass
class OHLCBar:
    ts_start: float
    ts_end: float
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    tick_count: int = 0


class TimeBarEngine:
    """
    Zero-allocation real-time time-bar aggregator and higher-timeframe trend/volatility analyzer.
    """

    def __init__(
        self,
        ema_period: int = 9,
        atr_period: int = 14,
        atr_baseline_bars: int = 30,  # 30-min baseline
        max_history_bars: int = 100,
        slope_veto_bps: float = 2.0,   # Q-4: scale-invariant EMA-slope veto (bps, normalized by last_price)
    ):
        self.ema_period = ema_period
        self.atr_period = atr_period
        self.atr_baseline_bars = atr_baseline_bars
        self.max_history_bars = max_history_bars
        self.slope_veto_bps = slope_veto_bps

        # 1-Minute Bar Building State
        self._curr_1m_bar: Optional[OHLCBar] = None
        self._curr_1m_bucket: int = -1
        self.bars_1m: deque[OHLCBar] = deque(maxlen=max_history_bars)

        # 3-Minute Bar Building State
        self._curr_3m_bar: Optional[OHLCBar] = None
        self._curr_3m_bucket: int = -1
        self.bars_3m: deque[OHLCBar] = deque(maxlen=max_history_bars)

        # EMA State
        self.ema_1m: float = 0.0
        self.ema_1m_prev: float = 0.0
        self.ema_1m_slope: float = 0.0  # pts / bar

        self.ema_3m: float = 0.0
        self.ema_3m_prev: float = 0.0
        self.ema_3m_slope: float = 0.0  # pts / bar

        # ATR State
        self.atr_1m_history: deque[float] = deque(maxlen=max_history_bars)
        self.current_atr_1m: float = 0.0
        self.baseline_atr_1m: float = 0.0
        self.atr_ratio: float = 1.0

        # Last tick info
        self.last_price: float = 0.0
        self.last_ts: float = 0.0

    def update_tick(self, price: float, ts_sec: float) -> Tuple[bool, bool]:
        """
        Updates the engine with a new underlying spot/future tick.
        Returns:
            (new_1m_bar_completed, new_3m_bar_completed)
        """
        if price <= 0 or ts_sec < 0:
            return False, False

        self.last_price = price
        self.last_ts = ts_sec

        new_1m = False
        new_3m = False

        # 1. 1-Minute Bar (60-second time bucket)
        bucket_1m = int(ts_sec // 60)
        if self._curr_1m_bucket == -1:
            self._curr_1m_bucket = bucket_1m
            self._curr_1m_bar = OHLCBar(
                ts_start=bucket_1m * 60.0,
                ts_end=(bucket_1m + 1) * 60.0,
                open=price,
                high=price,
                low=price,
                close=price,
                tick_count=1,
            )
        elif bucket_1m == self._curr_1m_bucket:
            # Update current in-flight 1m bar
            b = self._curr_1m_bar
            if b is not None:
                if price > b.high:
                    b.high = price
                if price < b.low:
                    b.low = price
                b.close = price
                b.tick_count += 1
        else:
            # Close previous 1m bar and start new one
            if self._curr_1m_bar is not None:
                self._on_1m_bar_close(self._curr_1m_bar)
                new_1m = True
            self._curr_1m_bucket = bucket_1m
            self._curr_1m_bar = OHLCBar(
                ts_start=bucket_1m * 60.0,
                ts_end=(bucket_1m + 1) * 60.0,
                open=price,
                high=price,
                low=price,
                close=price,
                tick_count=1,
            )

        # 2. 3-Minute Bar (180-second time bucket)
        bucket_3m = int(ts_sec // 180)
        if self._curr_3m_bucket == -1:
            self._curr_3m_bucket = bucket_3m
            self._curr_3m_bar = OHLCBar(
                ts_start=bucket_3m * 180.0,
                ts_end=(bucket_3m + 1) * 180.0,
                open=price,
                high=price,
                low=price,
                close=price,
                tick_count=1,
            )
        elif bucket_3m == self._curr_3m_bucket:
            # Update current in-flight 3m bar
            b = self._curr_3m_bar
            if b is not None:
                if price > b.high:
                    b.high = price
                if price < b.low:
                    b.low = price
                b.close = price
                b.tick_count += 1
        else:
            # Close previous 3m bar and start new one
            if self._curr_3m_bar is not None:
                self._on_3m_bar_close(self._curr_3m_bar)
                new_3m = True
            self._curr_3m_bucket = bucket_3m
            self._curr_3m_bar = OHLCBar(
                ts_start=bucket_3m * 180.0,
                ts_end=(bucket_3m + 1) * 180.0,
                open=price,
                high=price,
                low=price,
                close=price,
                tick_count=1,
            )

        return new_1m, new_3m

    def _on_1m_bar_close(self, bar: OHLCBar) -> None:
        """Called when a 1-minute bar finishes."""
        self.bars_1m.append(bar)

        # 1. Update 1-Min EMA(9)
        k = 2.0 / (self.ema_period + 1.0)
        if self.ema_1m == 0.0:
            self.ema_1m = bar.close
            self.ema_1m_prev = bar.close
            self.ema_1m_slope = 0.0
        else:
            self.ema_1m_prev = self.ema_1m
            self.ema_1m = (bar.close * k) + (self.ema_1m * (1.0 - k))
            self.ema_1m_slope = self.ema_1m - self.ema_1m_prev

        # 2. Update 1-Min ATR(14)
        if len(self.bars_1m) >= 2:
            prev_close = self.bars_1m[-2].close
            tr = max(
                bar.high - bar.low,
                abs(bar.high - prev_close),
                abs(bar.low - prev_close),
            )
        else:
            tr = bar.high - bar.low

        self.atr_1m_history.append(tr)

        if len(self.atr_1m_history) >= self.atr_period:
            recent_tr = list(self.atr_1m_history)[-self.atr_period:]
            self.current_atr_1m = sum(recent_tr) / len(recent_tr)
            
            # 30-Min Baseline ATR (Q-3 fix: use atr_baseline_bars, not all 100)
            baseline_tr = list(self.atr_1m_history)[-self.atr_baseline_bars:]
            self.baseline_atr_1m = sum(baseline_tr) / len(baseline_tr) if baseline_tr else 0.0
            
            if self.baseline_atr_1m > 1e-4:
                self.atr_ratio = self.current_atr_1m / self.baseline_atr_1m
            else:
                self.atr_ratio = 1.0

    def _on_3m_bar_close(self, bar: OHLCBar) -> None:
        """Called when a 3-minute bar finishes."""
        self.bars_3m.append(bar)

        # Update 3-Min EMA(9)
        k = 2.0 / (self.ema_period + 1.0)
        if self.ema_3m == 0.0:
            self.ema_3m = bar.close
            self.ema_3m_prev = bar.close
            self.ema_3m_slope = 0.0
        else:
            self.ema_3m_prev = self.ema_3m
            self.ema_3m = (bar.close * k) + (self.ema_3m * (1.0 - k))
            self.ema_3m_slope = self.ema_3m - self.ema_3m_prev

    def get_market_structure(self, direction: str) -> str:
        """
        Evaluates 3-State Market Structure:
        - 'ALIGNED': Structure confirms the trade direction (HH/HL for UP, LH/LL for DOWN).
        - 'NEUTRAL': Range-bound / consolidation / inside bars.
        - 'OPPOSED': Structure directly opposes trade direction (Hard Veto condition).
        """
        if len(self.bars_1m) < 4:
            return "NEUTRAL"

        b = list(self.bars_1m)
        h1, h2, h3 = b[-3].high, b[-2].high, b[-1].high
        l1, l2, l3 = b[-3].low, b[-2].low, b[-1].low

        is_higher_highs = (h3 > h2) and (h2 >= h1)
        is_higher_lows = (l3 > l2) and (l2 >= l1)
        is_lower_highs = (h3 < h2) and (h2 <= h1)
        is_lower_lows = (l3 < l2) and (l2 <= l1)

        if direction.upper() in ("UP", "CALL"):
            if is_higher_highs or (is_higher_lows and b[-1].close > b[-2].close):
                return "ALIGNED"
            elif is_lower_highs and is_lower_lows and b[-1].close < b[-2].close:
                return "OPPOSED"
            else:
                return "NEUTRAL"

        elif direction.upper() in ("DOWN", "PUT"):
            if is_lower_lows or (is_lower_highs and b[-1].close < b[-2].close):
                return "ALIGNED"
            elif is_higher_highs and is_higher_lows and b[-1].close > b[-2].close:
                return "OPPOSED"
            else:
                return "NEUTRAL"

        return "NEUTRAL"

    def get_permission_state(self, direction: str) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Evaluates higher-timeframe Trend & Structure Permission.
        Returns:
            (has_permission, reason, metrics)
        """
        metrics = {
            "ema_1m_slope": round(self.ema_1m_slope, 2),
            "ema_3m_slope": round(self.ema_3m_slope, 2),
            "atr_ratio": round(self.atr_ratio, 2),
            "current_atr_1m": round(self.current_atr_1m, 2),
            "market_structure": self.get_market_structure(direction),
        }

        # Warmup check
        if len(self.bars_1m) < 2:
            return True, "warmup_permissive", metrics

        struct = metrics["market_structure"]
        if struct == "OPPOSED":
            return False, f"market_structure_opposed ({struct})", metrics

        # Multi-timeframe slope check (Q-4 fix: normalize absolute slope by price
        # into basis points so NIFTY (~25k) and SENSEX (~78k) share one threshold.
        # 2.0 bps reproduces the prior NIFTY 0.5pt / 24800 = ~2.0 bps behavior.)
        ref = self.last_price if self.last_price > 0 else 1.0
        slope_3m_bps = (self.ema_3m_slope / ref) * 10000.0
        slope_1m_bps = (self.ema_1m_slope / ref) * 10000.0
        metrics["slope_veto_bps"] = self.slope_veto_bps
        metrics["ema_3m_slope_bps"] = round(slope_3m_bps, 2)
        metrics["ema_1m_slope_bps"] = round(slope_1m_bps, 2)

        if direction.upper() in ("UP", "CALL"):
            # If 3-min slope is strongly negative, veto CALL
            if slope_3m_bps < -self.slope_veto_bps:
                return False, f"3m_trend_bearish_slope ({slope_3m_bps:+.2f}bps)", metrics
            # If 1-min slope is strongly negative, veto CALL
            if slope_1m_bps < -self.slope_veto_bps:
                return False, f"1m_trend_bearish_slope ({slope_1m_bps:+.2f}bps)", metrics

        elif direction.upper() in ("DOWN", "PUT"):
            # If 3-min slope is strongly positive, veto PUT
            if slope_3m_bps > self.slope_veto_bps:
                return False, f"3m_trend_bullish_slope ({slope_3m_bps:+.2f}bps)", metrics
            # If 1-min slope is strongly positive, veto PUT
            if slope_1m_bps > self.slope_veto_bps:
                return False, f"1m_trend_bullish_slope ({slope_1m_bps:+.2f}bps)", metrics

        return True, "permission_granted", metrics
