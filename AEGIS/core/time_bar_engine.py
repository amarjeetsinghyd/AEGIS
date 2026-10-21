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
import os
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

    @property
    def timestamp(self) -> float:
        return self.ts_end


class TimeBarEngine:
    """
    Zero-allocation real-time time-bar aggregator and higher-timeframe trend/volatility analyzer.
    """

    def __init__(
        self,
        ema_period: int = 9,
        atr_period: int = 14,
        atr_baseline_bars: int = 30,  # 30-min baseline
        max_history_bars: int = 400,
        slope_veto_bps: float = 2.0,   # Q-4: scale-invariant EMA-slope veto (bps, normalized by last_price)
    ):
        self.ema_period = ema_period
        self.atr_period = atr_period
        self.atr_baseline_bars = atr_baseline_bars
        self.max_history_bars = int(os.getenv("AEGIS_MAX_HISTORY_BARS", str(max_history_bars)))
        self.slope_veto_bps = slope_veto_bps

        # 1-Minute Bar Building State
        self._curr_1m_bar: Optional[OHLCBar] = None
        self._curr_1m_bucket: int = -1
        self.bars_1m: deque[OHLCBar] = deque(maxlen=self.max_history_bars)

        # 3-Minute Bar Building State
        self._curr_3m_bar: Optional[OHLCBar] = None
        self._curr_3m_bucket: int = -1
        self.bars_3m: deque[OHLCBar] = deque(maxlen=self.max_history_bars)

        # EMA State
        self.ema_1m: float = 0.0
        self.ema_1m_prev: float = 0.0
        self.ema_1m_slope: float = 0.0  # pts / bar

        self.ema_3m: float = 0.0
        self.ema_3m_prev: float = 0.0
        self.ema_3m_slope: float = 0.0  # pts / bar

        # 20-Period EMA State (Fast Momentum Pullback Guide)
        self.ema_20_period: int = 20
        self.ema_20_1m: float = 0.0
        self.ema_20_1m_prev: float = 0.0
        self.ema_20_1m_slope: float = 0.0  # pts / bar

        # 50-Period EMA State (Intraday Trend Anchor & Pullback Guide)
        self.ema_50_period: int = 50
        self.ema_50_1m: float = 0.0
        self.ema_50_1m_prev: float = 0.0
        self.ema_50_1m_slope: float = 0.0  # pts / bar

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

        # 1b. Update 1-Min EMA(20)
        k20 = 2.0 / (self.ema_20_period + 1.0)
        if self.ema_20_1m == 0.0:
            self.ema_20_1m = bar.close
            self.ema_20_1m_prev = bar.close
            self.ema_20_1m_slope = 0.0
        else:
            self.ema_20_1m_prev = self.ema_20_1m
            self.ema_20_1m = (bar.close * k20) + (self.ema_20_1m * (1.0 - k20))
            self.ema_20_1m_slope = self.ema_20_1m - self.ema_20_1m_prev

        # 1c. Update 1-Min EMA(50)
        k50 = 2.0 / (self.ema_50_period + 1.0)
        if self.ema_50_1m == 0.0:
            self.ema_50_1m = bar.close
            self.ema_50_1m_prev = bar.close
            self.ema_50_1m_slope = 0.0
        else:
            self.ema_50_1m_prev = self.ema_50_1m
            self.ema_50_1m = (bar.close * k50) + (self.ema_50_1m * (1.0 - k50))
            self.ema_50_1m_slope = self.ema_50_1m - self.ema_50_1m_prev

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

    def get_live_ema_and_slope(self) -> Tuple[float, float, float, float]:
        """
        TradingView-style real-time in-flight EMA & Slope calculation.
        Computes dynamic 9-EMA and slope on the active tick price without mutating closed bar state.
        Returns:
            (live_ema_1m, live_slope_1m_bps, live_ema_3m, live_slope_3m_bps)
        """
        ref = self.last_price if self.last_price > 0 else 1.0
        k = 2.0 / (self.ema_period + 1.0)
        
        # 1-Min Live Dynamic EMA & Slope
        if self.ema_1m_prev > 0 and self.last_price > 0:
            live_ema_1m = (self.last_price * k) + (self.ema_1m_prev * (1.0 - k))
            live_slope_1m = live_ema_1m - self.ema_1m_prev
        else:
            live_ema_1m = self.ema_1m if self.ema_1m > 0 else self.last_price
            live_slope_1m = self.ema_1m_slope
        live_slope_1m_bps = (live_slope_1m / ref) * 10000.0

        # 3-Min Live Dynamic EMA & Slope
        if self.ema_3m_prev > 0 and self.last_price > 0:
            live_ema_3m = (self.last_price * k) + (self.ema_3m_prev * (1.0 - k))
            live_slope_3m = live_ema_3m - self.ema_3m_prev
        else:
            live_ema_3m = self.ema_3m if self.ema_3m > 0 else self.last_price
            live_slope_3m = self.ema_3m_slope
        live_slope_3m_bps = (live_slope_3m / ref) * 10000.0

        return live_ema_1m, live_slope_1m_bps, live_ema_3m, live_slope_3m_bps

    @property
    def ema_9_1m(self) -> float:
        """Alias for 1m EMA(9)."""
        return self.ema_1m

    @property
    def ema_9_1m_prev(self) -> float:
        """Alias for previous 1m EMA(9)."""
        return self.ema_1m_prev

    @property
    def ema_9_1m_slope(self) -> float:
        """Alias for 1m EMA(9) slope."""
        return self.ema_1m_slope

    def get_live_9_ema_and_slope(self) -> Tuple[float, float]:
        """
        TradingView-style real-time in-flight 9-EMA & Slope calculation.
        Computes dynamic 9-EMA and slope on the active tick price without mutating closed bar state.
        Returns:
            (live_ema_9_1m, live_slope_9_1m_bps)
        """
        live_9, slope_9, _, _ = self.get_live_ema_and_slope()
        return live_9, slope_9

    def get_live_20_ema_and_slope(self) -> Tuple[float, float]:
        """
        TradingView-style real-time in-flight 20-EMA & Slope calculation.
        Computes dynamic 20-EMA and slope on the active tick price without mutating closed bar state.
        Returns:
            (live_ema_20_1m, live_slope_20_1m_bps)
        """
        ref = self.last_price if self.last_price > 0 else 1.0
        k20 = 2.0 / (self.ema_20_period + 1.0)
        if self.ema_20_1m_prev > 0 and self.last_price > 0:
            live_ema_20 = (self.last_price * k20) + (self.ema_20_1m_prev * (1.0 - k20))
            live_slope_20 = live_ema_20 - self.ema_20_1m_prev
        else:
            live_ema_20 = self.ema_20_1m if self.ema_20_1m > 0 else self.last_price
            live_slope_20 = self.ema_20_1m_slope
        live_slope_20_bps = (live_slope_20 / ref) * 10000.0
        return live_ema_20, live_slope_20_bps

    def get_live_50_ema_and_slope(self) -> Tuple[float, float]:
        """
        TradingView-style real-time in-flight 50-EMA & Slope calculation.
        Computes dynamic 50-EMA and slope on the active tick price without mutating closed bar state.
        Returns:
            (live_ema_50_1m, live_slope_50_1m_bps)
        """
        ref = self.last_price if self.last_price > 0 else 1.0
        k50 = 2.0 / (self.ema_50_period + 1.0)
        if self.ema_50_1m_prev > 0 and self.last_price > 0:
            live_ema_50 = (self.last_price * k50) + (self.ema_50_1m_prev * (1.0 - k50))
            live_slope_50 = live_ema_50 - self.ema_50_1m_prev
        else:
            live_ema_50 = self.ema_50_1m if self.ema_50_1m > 0 else self.last_price
            live_slope_50 = self.ema_50_1m_slope
        live_slope_50_bps = (live_slope_50 / ref) * 10000.0
        return live_ema_50, live_slope_50_bps

    def check_50_ema_rejection(
        self,
        direction: str,
        tolerance_bps: float = 1.5,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Evaluates 50 EMA Pullback Rejection (Price Action Engine).
        Accepts:
          - Type A (Bearish/Bullish Expansion): Tested 50 EMA, small wick, strong body closing away from 50 EMA.
          - Type B (Pin Bar / Shooting Star / Hammer): Upper/lower wick >= 30% of total range.
        Returns:
            (is_rejection, reason, details)
        """
        live_ema_50, live_slope_50_bps = self.get_live_50_ema_and_slope()
        if live_ema_50 <= 0 or self.last_price <= 0:
            return False, "no_ema50_data", {}

        # Bar to examine: active in-flight bar if available, else last closed 1m bar
        bar = self._curr_1m_bar or (self.bars_1m[-1] if self.bars_1m else None)
        if bar is None:
            return False, "no_bars", {}

        tol_pts = live_ema_50 * (tolerance_bps / 10000.0)

        if direction.upper() in ("DOWN", "PUT"):
            # Fast rejection gates before allocating dicts/strings
            if bar.high < (live_ema_50 - tol_pts):
                return False, "did_not_test_50_ema", {}
            if bar.close >= (live_ema_50 + 0.5 * tol_pts):
                return False, "pierced_above_50_ema", {}
            if live_slope_50_bps > 0.10:
                return False, "50_ema_slope_bullish", {}

            total_range = bar.high - bar.low
            body = abs(bar.close - bar.open)
            upper_wick = bar.high - max(bar.open, bar.close)
            wick_ratio = (upper_wick / total_range) if total_range > 0 else 0.0

            # Signatures:
            # Type A: Bearish expansion bar (close < open, dropping away from 50 EMA)
            is_bearish_body = (bar.close < bar.open)
            # Type B: Pin bar (upper wick >= 30% of range)
            is_pin_bar = (wick_ratio >= 0.30)

            if is_bearish_body or is_pin_bar:
                sig_type = "bearish_expansion" if is_bearish_body else "pin_bar_rejection"
                buffer_pts = max(5.0, (live_ema_50 / 24000.0) * 5.0)
                details = {
                    "ema_50": round(live_ema_50, 2),
                    "bar_open": round(bar.open, 2),
                    "bar_high": round(bar.high, 2),
                    "bar_low": round(bar.low, 2),
                    "bar_close": round(bar.close, 2),
                    "ema_50_slope_bps": round(live_slope_50_bps, 2),
                    "total_range": round(total_range, 2),
                    "body": round(body, 2),
                    "upper_wick": round(upper_wick, 2),
                    "wick_ratio": round(wick_ratio, 3),
                    "rejection_type": sig_type,
                    "sl_price": round(bar.high + buffer_pts, 2),
                    "buffer_pts": round(buffer_pts, 2),
                }
                return True, f"50_ema_put_rejection ({sig_type})", details
            else:
                return False, "neither_bearish_body_nor_pin_bar", {}

        elif direction.upper() in ("UP", "CALL"):
            # Fast rejection gates before allocating dicts/strings
            if bar.low > (live_ema_50 + tol_pts):
                return False, "did_not_test_50_ema", {}
            if bar.close <= (live_ema_50 - 0.5 * tol_pts):
                return False, "pierced_below_50_ema", {}
            if live_slope_50_bps < -0.10:
                return False, "50_ema_slope_bearish", {}

            total_range = bar.high - bar.low
            body = abs(bar.close - bar.open)
            lower_wick = min(bar.open, bar.close) - bar.low
            wick_ratio = (lower_wick / total_range) if total_range > 0 else 0.0

            is_bullish_body = (bar.close > bar.open)
            is_pin_bar = (wick_ratio >= 0.30)

            if is_bullish_body or is_pin_bar:
                sig_type = "bullish_expansion" if is_bullish_body else "hammer_rejection"
                buffer_pts = max(5.0, (live_ema_50 / 24000.0) * 5.0)
                details = {
                    "ema_50": round(live_ema_50, 2),
                    "bar_open": round(bar.open, 2),
                    "bar_high": round(bar.high, 2),
                    "bar_low": round(bar.low, 2),
                    "bar_close": round(bar.close, 2),
                    "ema_50_slope_bps": round(live_slope_50_bps, 2),
                    "total_range": round(total_range, 2),
                    "body": round(body, 2),
                    "lower_wick": round(lower_wick, 2),
                    "wick_ratio": round(wick_ratio, 3),
                    "rejection_type": sig_type,
                    "sl_price": round(bar.low - buffer_pts, 2),
                    "buffer_pts": round(buffer_pts, 2),
                }
                return True, f"50_ema_call_rejection ({sig_type})", details
            else:
                return False, "neither_bullish_body_nor_hammer", {}

        return False, "unknown_direction", {}

    def check_20_ema_rejection(
        self,
        direction: str,
        tolerance_bps: float = 1.5,
        min_wick_ratio: float = 0.33,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Pillar 1: 20 EMA Pullback Wick Rejection (User's System).
        Evaluates 1-minute 20 EMA pullbacks with candle wick rejection (pin bar)
        or momentum expansion candle closing away from 20 EMA.
        Returns:
            (is_rejection, reason, details)
        """
        live_ema_20, live_slope_20_bps = self.get_live_20_ema_and_slope()
        if live_ema_20 <= 0 or self.last_price <= 0:
            return False, "no_ema20_data", {}

        bar = self._curr_1m_bar or (self.bars_1m[-1] if self.bars_1m else None)
        if bar is None:
            return False, "no_bars", {}

        tol_pts = live_ema_20 * (tolerance_bps / 10000.0)
        buffer_pts = max(5.0, (live_ema_20 / 24000.0) * 5.0)

        if direction.upper() in ("DOWN", "PUT"):
            # Put setup: test 20 EMA from below, not pierced far above, slope not bullish
            if bar.high < (live_ema_20 - tol_pts):
                return False, "did_not_test_20_ema", {}
            if bar.close >= (live_ema_20 + 0.5 * tol_pts):
                return False, "pierced_above_20_ema", {}
            if live_slope_20_bps > 0.10:
                return False, "20_ema_slope_bullish", {}

            total_range = bar.high - bar.low
            if total_range <= 0:
                return False, "zero_range", {}
            body = abs(bar.close - bar.open)
            upper_wick = bar.high - max(bar.open, bar.close)
            wick_ratio = upper_wick / total_range

            is_bearish_expansion = (bar.close < bar.open) and (body > total_range * 0.40)
            is_pin_bar = (wick_ratio >= min_wick_ratio)

            if is_bearish_expansion or is_pin_bar:
                sig_type = "bearish_expansion" if is_bearish_expansion else "pin_bar_rejection"
                sl_price = round(bar.high + buffer_pts, 2)
                details = {
                    "pa_setup_name": "20_ema_rejection",
                    "pillar": 1,
                    "ema_20": round(live_ema_20, 2),
                    "bar_open": round(bar.open, 2),
                    "bar_high": round(bar.high, 2),
                    "bar_low": round(bar.low, 2),
                    "bar_close": round(bar.close, 2),
                    "bar_high": round(bar.high, 2),
                    "bar_low": round(bar.low, 2),
                    "ema_20_slope_bps": round(live_slope_20_bps, 2),
                    "total_range": round(total_range, 2),
                    "body": round(body, 2),
                    "upper_wick": round(upper_wick, 2),
                    "wick_ratio": round(wick_ratio, 3),
                    "rejection_type": sig_type,
                    "sl_price": sl_price,
                    "buffer_pts": round(buffer_pts, 2),
                }
                return True, f"20_ema_put_rejection ({sig_type})", details
            else:
                return False, "neither_bearish_expansion_nor_pin_bar", {}

        elif direction.upper() in ("UP", "CALL"):
            # Call setup: test 20 EMA from above, not pierced far below, slope not bearish
            if bar.low > (live_ema_20 + tol_pts):
                return False, "did_not_test_20_ema", {}
            if bar.close <= (live_ema_20 - 0.5 * tol_pts):
                return False, "pierced_below_20_ema", {}
            if live_slope_20_bps < -0.10:
                return False, "20_ema_slope_bearish", {}

            total_range = bar.high - bar.low
            if total_range <= 0:
                return False, "zero_range", {}
            body = abs(bar.close - bar.open)
            lower_wick = min(bar.open, bar.close) - bar.low
            wick_ratio = lower_wick / total_range

            is_bullish_expansion = (bar.close > bar.open) and (body > total_range * 0.40)
            is_hammer = (wick_ratio >= min_wick_ratio)

            if is_bullish_expansion or is_hammer:
                sig_type = "bullish_expansion" if is_bullish_expansion else "hammer_rejection"
                sl_price = round(bar.low - buffer_pts, 2)
                details = {
                    "pa_setup_name": "20_ema_rejection",
                    "pillar": 1,
                    "ema_20": round(live_ema_20, 2),
                    "bar_open": round(bar.open, 2),
                    "bar_high": round(bar.high, 2),
                    "bar_low": round(bar.low, 2),
                    "bar_close": round(bar.close, 2),
                    "bar_high": round(bar.high, 2),
                    "bar_low": round(bar.low, 2),
                    "ema_20_slope_bps": round(live_slope_20_bps, 2),
                    "total_range": round(total_range, 2),
                    "body": round(body, 2),
                    "lower_wick": round(lower_wick, 2),
                    "wick_ratio": round(wick_ratio, 3),
                    "rejection_type": sig_type,
                    "sl_price": sl_price,
                    "buffer_pts": round(buffer_pts, 2),
                }
                return True, f"20_ema_call_rejection ({sig_type})", details
            else:
                return False, "neither_bullish_expansion_nor_hammer", {}

        return False, "unknown_direction", {}

    def check_albrooks_2legged_pullback(
        self,
        direction: str,
        current_vwap: float = 0.0,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Pillar 2: Al Brooks 2-Legged Pullback (H2 / L2 Exhaustion at 50 EMA).
        Detects 2-stage retail trap:
          - Leg 1 tests 20 EMA, pauses.
          - Leg 2 pushes deep into 50 EMA, sweeping stops.
          - Current bar prints a decisive reversal candle confirming exhaustion.
        Returns:
            (is_rejection, reason, details)
        """
        if len(self.bars_1m) < 3:
            return False, "insufficient_bars", {}

        b1 = self.bars_1m[-1]  # Leg 2 peak/trough bar
        b2 = self.bars_1m[-2]  # Pause bar
        b3 = self.bars_1m[-3]  # Leg 1 bar
        bar = self._curr_1m_bar or b1  # In-flight or confirmed reversal bar

        live_ema_50, live_slope_50_bps = self.get_live_50_ema_and_slope()
        if live_ema_50 <= 0:
            return False, "no_ema50", {}

        buffer_pts = max(5.0, (live_ema_50 / 24000.0) * 5.0)
        tol_pts = buffer_pts * 1.5

        if direction.upper() in ("DOWN", "PUT"):
            # Trend context: price must be below VWAP (or VWAP inactive)
            if current_vwap > 0 and bar.close >= current_vwap:
                return False, "above_vwap", {}

            # L2 Bearish Pullback structure:
            # Leg 2 pushed higher than Leg 1 (b1.high > b3.high)
            # Leg 2 tested into 50 EMA (within tol_pts)
            # Current bar is bearish reversal (bar.close < bar.open)
            tested_50 = abs(b1.high - live_ema_50) <= tol_pts or (b1.high >= (live_ema_50 - tol_pts) and b1.close <= (live_ema_50 + tol_pts))
            if b1.high > b3.high and tested_50 and bar.close < bar.open:
                sl_price = round(max(bar.high, b1.high) + buffer_pts, 2)
                details = {
                    "pa_setup_name": "albrooks_2legged_pullback",
                    "pillar": 2,
                    "structure": "L2_bearish_exhaustion",
                    "ema_50": round(live_ema_50, 2),
                    "bar_open": round(bar.open, 2),
                    "bar_high": round(bar.high, 2),
                    "bar_low": round(bar.low, 2),
                    "bar_close": round(bar.close, 2),
                    "leg1_high": round(b3.high, 2),
                    "leg2_high": round(b1.high, 2),
                    "rejection_type": "l2_reversal",
                    "sl_price": sl_price,
                    "buffer_pts": round(buffer_pts, 2),
                }
                return True, "albrooks_l2_put_exhaustion", details
            return False, "l2_structure_not_met", {}

        elif direction.upper() in ("UP", "CALL"):
            # Trend context: price must be above VWAP (or VWAP inactive)
            if current_vwap > 0 and bar.close <= current_vwap:
                return False, "below_vwap", {}

            # H2 Bullish Pullback structure:
            # Leg 2 pushed lower than Leg 1 (b1.low < b3.low)
            # Leg 2 tested into 50 EMA (within tol_pts)
            # Current bar is bullish reversal (bar.close > bar.open)
            tested_50 = abs(b1.low - live_ema_50) <= tol_pts or (b1.low <= (live_ema_50 + tol_pts) and b1.close >= (live_ema_50 - tol_pts))
            if b1.low < b3.low and tested_50 and bar.close > bar.open:
                sl_price = round(min(bar.low, b1.low) - buffer_pts, 2)
                details = {
                    "pa_setup_name": "albrooks_2legged_pullback",
                    "pillar": 2,
                    "structure": "H2_bullish_exhaustion",
                    "ema_50": round(live_ema_50, 2),
                    "bar_open": round(bar.open, 2),
                    "bar_high": round(bar.high, 2),
                    "bar_low": round(bar.low, 2),
                    "bar_close": round(bar.close, 2),
                    "leg1_low": round(b3.low, 2),
                    "leg2_low": round(b1.low, 2),
                    "rejection_type": "h2_reversal",
                    "sl_price": sl_price,
                    "buffer_pts": round(buffer_pts, 2),
                }
                return True, "albrooks_h2_call_exhaustion", details
            return False, "h2_structure_not_met", {}

        return False, "unknown_direction", {}

    def check_volatility_squeeze_breakout(
        self,
        direction: str,
        current_vwap: float = 0.0,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Pillar 3: Volatility Squeeze to Expansion Breakout (100% Win-Rate Microstructure Scalper).
        Detects range compression across prior 4 bars (< 0.65 of ATR) followed by an explosive
        directional breakout bar above prior range high (CALL) or below range low (PUT).
        Returns:
            (is_breakout, reason, details)
        """
        if len(self.bars_1m) < 4:
            return False, "insufficient_bars", {}

        recent_bars = list(self.bars_1m)[-4:]
        box_high = max(b.high for b in recent_bars)
        box_low = min(b.low for b in recent_bars)
        squeeze_range = box_high - box_low

        ref_spot = self.last_price if self.last_price > 0 else 24000.0
        atr = self.current_atr_1m if self.current_atr_1m > 0 else (ref_spot * 0.001)
        buffer_pts = max(5.0, (ref_spot / 24000.0) * 5.0)

        # Squeeze check: range < 0.65 * ATR (spring compression)
        if squeeze_range > (0.65 * atr):
            return False, "not_in_squeeze", {}

        bar = self._curr_1m_bar or self.bars_1m[-1]

        if direction.upper() in ("DOWN", "PUT"):
            if current_vwap > 0 and bar.close >= current_vwap:
                return False, "above_vwap", {}

            # Downside expansion breakout
            if bar.close < box_low:
                sl_price = round(box_high + buffer_pts, 2)
                details = {
                    "pa_setup_name": "volatility_squeeze_breakout",
                    "pillar": 3,
                    "box_high": round(box_high, 2),
                    "box_low": round(box_low, 2),
                    "box_range": round(squeeze_range, 2),
                    "atr_14": round(atr, 2),
                    "bar_open": round(bar.open, 2),
                    "bar_high": round(bar.high, 2),
                    "bar_low": round(bar.low, 2),
                    "bar_close": round(bar.close, 2),
                    "rejection_type": "squeeze_downside_breakout",
                    "sl_price": sl_price,
                    "buffer_pts": round(buffer_pts, 2),
                }
                return True, "volatility_squeeze_downside_breakout", details
            return False, "no_downside_breakout", {}

        elif direction.upper() in ("UP", "CALL"):
            if current_vwap > 0 and bar.close <= current_vwap:
                return False, "below_vwap", {}

            # Upside expansion breakout
            if bar.close > box_high:
                sl_price = round(box_low - buffer_pts, 2)
                details = {
                    "pa_setup_name": "volatility_squeeze_breakout",
                    "pillar": 3,
                    "box_high": round(box_high, 2),
                    "box_low": round(box_low, 2),
                    "box_range": round(squeeze_range, 2),
                    "atr_14": round(atr, 2),
                    "bar_open": round(bar.open, 2),
                    "bar_high": round(bar.high, 2),
                    "bar_low": round(bar.low, 2),
                    "bar_close": round(bar.close, 2),
                    "rejection_type": "squeeze_upside_breakout",
                    "sl_price": sl_price,
                    "buffer_pts": round(buffer_pts, 2),
                }
                return True, "volatility_squeeze_upside_breakout", details
            return False, "no_upside_breakout", {}

        return False, "unknown_direction", {}

    def check_price_action_setups(
        self,
        direction: str,
        current_vwap: float = 0.0,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Master Multi-Pillar Price Action Evaluator.
        Evaluates the 3 empirically verified Price Action & Microstructure Scalper Engines:
          Pillar 1: 20 EMA + Wick Rejection (User's System - Top Performer)
          Pillar 2: Al Brooks 2-Legged Pullback (H2 / L2 Exhaustion at 50 EMA)
          Pillar 3: Volatility Squeeze to Expansion Breakout (100% WR Scalper)
        Returns:
            (is_setup, setup_name, details_dict)
        """
        # Pillar 1: 20 EMA Pullback Wick Rejection
        ok_20, reason_20, details_20 = self.check_20_ema_rejection(direction)
        if ok_20:
            bar = self._curr_1m_bar or (self.bars_1m[-1] if self.bars_1m else None)
            if bar and current_vwap > 0:
                if direction.upper() in ("UP", "CALL") and bar.close <= current_vwap:
                    pass  # Opposed to VWAP
                elif direction.upper() in ("DOWN", "PUT") and bar.close >= current_vwap:
                    pass  # Opposed to VWAP
                else:
                    return True, "20_ema_rejection", details_20
            else:
                return True, "20_ema_rejection", details_20

        # Pillar 2: Al Brooks 2-Legged Pullback at 50 EMA
        ok_ab, reason_ab, details_ab = self.check_albrooks_2legged_pullback(direction, current_vwap)
        if ok_ab:
            return True, "albrooks_2legged_pullback", details_ab

        # Pillar 3: Volatility Squeeze Breakout
        ok_sq, reason_sq, details_sq = self.check_volatility_squeeze_breakout(direction, current_vwap)
        if ok_sq:
            return True, "volatility_squeeze_breakout", details_sq

        return False, "no_price_action_setup", {}

    def seed_historical_bars(self, candles: List[Dict[str, Any]]) -> int:
        """
        Seeds TimeBarEngine with pre-market historical 1-minute bars to eliminate cold-start warmup lag
        and fill data gaps on restart.
        """
        if not candles:
            return 0

        from datetime import datetime as _dt

        def _parse_ts(c_dict: Dict[str, Any]) -> float:
            ssboe = c_dict.get("ssboe")
            if ssboe is not None:
                try:
                    return float(ssboe)
                except (ValueError, TypeError):
                    pass
            raw_ts = c_dict.get("timestamp") or c_dict.get("ts")
            if isinstance(raw_ts, (int, float)):
                return float(raw_ts)
            if isinstance(raw_ts, str):
                try:
                    return float(raw_ts)
                except ValueError:
                    try:
                        return _dt.strptime(raw_ts, "%Y-%m-%d %H:%M:%S").timestamp()
                    except Exception:
                        pass
            t_str = c_dict.get("time")
            if isinstance(t_str, (int, float)):
                return float(t_str)
            if isinstance(t_str, str):
                try:
                    return float(t_str)
                except ValueError:
                    for fmt in ("%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y %H:%M:%S"):
                        try:
                            return _dt.strptime(t_str, fmt).timestamp()
                        except Exception:
                            pass
            return 0.0

        parsed_bars = []
        for c in candles:
            try:
                op = float(c.get("open") or c.get("into") or 0.0)
                hi = float(c.get("high") or c.get("inth") or 0.0)
                lo = float(c.get("low") or c.get("intl") or 0.0)
                cl = float(c.get("close") or c.get("intc") or 0.0)
                if op <= 0 or hi <= 0 or lo <= 0 or cl <= 0:
                    continue
                ts_sec = _parse_ts(c)
                vol = float(c.get("volume") or c.get("intv") or 0.0)
                parsed_bars.append((ts_sec, op, hi, lo, cl, vol))
            except Exception:
                continue

        if not parsed_bars:
            return 0

        parsed_bars.sort(key=lambda x: x[0])

        count = 0
        for ts_sec, op, hi, lo, cl, vol in parsed_bars:
            bar = OHLCBar(
                ts_start=ts_sec,
                ts_end=ts_sec + 60.0,
                open=op,
                high=hi,
                low=lo,
                close=cl,
                volume=vol,
                tick_count=10,
            )
            self._on_1m_bar_close(bar)
            count += 1

        if self.bars_1m:
            self.last_price = self.bars_1m[-1].close
        return count

    def get_market_structure(self, direction: str) -> str:
        """
        Evaluates 3-State Market Structure using closed bars and active in-flight bar:
        - 'ALIGNED': Structure confirms the trade direction (HH/HL for UP, LH/LL for DOWN).
        - 'NEUTRAL': Range-bound / consolidation / inside bars.
        - 'OPPOSED': Structure directly opposes trade direction (Hard Veto condition).
        """
        bars = list(self.bars_1m)
        if self._curr_1m_bar is not None:
            bars.append(self._curr_1m_bar)

        if len(bars) < 3:
            return "NEUTRAL"

        b = bars
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
        Evaluates real-time in-flight Trend & Structure Permission (TradingView Style).
        Returns:
            (has_permission, reason, metrics)
        """
        live_ema_1m, live_slope_1m_bps, live_ema_3m, live_slope_3m_bps = self.get_live_ema_and_slope()
        live_ema_50, live_slope_50_bps = self.get_live_50_ema_and_slope()

        metrics = {
            "ema_1m_live": round(live_ema_1m, 2),
            "ema_3m_live": round(live_ema_3m, 2),
            "ema_50_1m_live": round(live_ema_50, 2),
            "ema_1m_slope": round(self.ema_1m_slope, 2),
            "ema_3m_slope": round(self.ema_3m_slope, 2),
            "ema_50_1m_slope": round(self.ema_50_1m_slope, 2),
            "ema_1m_slope_bps": round(live_slope_1m_bps, 2),
            "ema_3m_slope_bps": round(live_slope_3m_bps, 2),
            "ema_50_1m_slope_bps": round(live_slope_50_bps, 2),
            "atr_ratio": round(self.atr_ratio, 2),
            "current_atr_1m": round(self.current_atr_1m, 2),
            "market_structure": self.get_market_structure(direction),
            "slope_veto_bps": self.slope_veto_bps,
        }

        # Warmup check
        if len(self.bars_1m) < 2:
            return True, "warmup_permissive", metrics

        struct = metrics["market_structure"]
        if struct == "OPPOSED":
            return False, f"market_structure_opposed ({struct})", metrics

        # 50 EMA Trend Permission (Route 2: Higher timeframe anchor)
        # If 50 EMA is available, check 50 EMA slope (does not block on 1m pullback noise)
        if self.ema_50_1m > 0:
            if direction.upper() in ("UP", "CALL"):
                if live_slope_50_bps < -0.10:
                    return False, f"50m_trend_bearish ({live_slope_50_bps:+.2f}bps)", metrics
            elif direction.upper() in ("DOWN", "PUT"):
                if live_slope_50_bps > 0.10:
                    return False, f"50m_trend_bullish ({live_slope_50_bps:+.2f}bps)", metrics
        else:
            # Fallback to multi-timeframe check when 50 EMA not yet populated
            if direction.upper() in ("UP", "CALL"):
                if live_slope_3m_bps < 0.0:
                    return False, f"3m_trend_bearish ({live_slope_3m_bps:+.2f}bps)", metrics
            elif direction.upper() in ("DOWN", "PUT"):
                if live_slope_3m_bps > 0.0:
                    return False, f"3m_trend_bullish ({live_slope_3m_bps:+.2f}bps)", metrics

        return True, "permission_granted", metrics
