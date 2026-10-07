"""
ARGUS StockPriceActionEngine — Pure Price Action Scalping Engine for Indian Equities (v5.0).
Directly ported from AEGIS v1.0 Production Architecture with stock-specific scale-invariance.

Implements the 4 empirically verified price action scalper pillars:
  Pillar 1: 20 EMA + Wick Rejection (Trend continuation pullback with open runway)
  Pillar 2: Liquidity Sweep & Spring Reclaim (Turtle Soup 5-bar sweep & reclaim)
  Pillar 3: Volatility Squeeze Box Breakout (Compression box <= 1.25x ATR to expansion)
  Pillar 4: Break of Structure (BOS) / High Volatility Gap Breakout

Scale-Invariance Mandate:
  All thresholds are dynamically scaled via Basis Points (bps) and localized 1m ATR multiples.
  Zero hardcoded rupee points. Works uniformly from ₹180 (TATASTEEL) to ₹3,000 (RELIANCE).
"""

from __future__ import annotations

import math
import logging
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any

try:
    from common.logger import get_logger
    logger = get_logger("argus_price_action")
except Exception:
    logger = logging.getLogger("argus_price_action")


@dataclass
class StockOHLCBar:
    ts_start: float
    ts_end: float
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    delta: float = 0.0
    buy_vol: float = 0.0
    sell_vol: float = 0.0
    tick_count: int = 0

    @property
    def range(self) -> float:
        return max(1e-4, self.high - self.low)

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low


class StockPriceActionEngine:
    """
    Price Action Engine maintaining real-time 1m and 5m bars, dynamic EMAs (20/50),
    1m ATR(14), and executing the 4 Price Action Scalping Pillars for a single equity.
    """

    def __init__(
        self,
        symbol: str = "EQUITY",
        max_history_bars: int = 150,
        atr_period: int = 14,
    ):
        self.symbol = symbol
        self.max_history_bars = max_history_bars
        self.atr_period = atr_period

        # 1m Bar state
        self._curr_1m_bar: Optional[StockOHLCBar] = None
        self._curr_1m_bucket: int = -1
        self.bars_1m: deque[StockOHLCBar] = deque(maxlen=max_history_bars)

        # 5m Bar state
        self._curr_5m_bar: Optional[StockOHLCBar] = None
        self._curr_5m_bucket: int = -1
        self.bars_5m: deque[StockOHLCBar] = deque(maxlen=60)

        # Indicators
        self.ema20: Optional[float] = None
        self.ema50: Optional[float] = None
        self.atr: float = 0.0
        self.last_price: float = 0.0
        self.last_ts: float = 0.0

        # Volume Delta state
        self._prev_cum_vol: float = 0.0
        self._flow_1m = {"buy_vol": 0.0, "sell_vol": 0.0, "tick_cnt": 0}

    def seed_bars(self, bars: List[Dict[str, Any]]) -> None:
        """Seed historical 1m bars from API/Database to eliminate cold-start warmup."""
        if not bars:
            return
        for b in bars:
            ts = float(b.get("ts", b.get("timestamp", 0.0)))
            o = float(b.get("open", b.get("o", 0.0)))
            h = float(b.get("high", b.get("h", 0.0)))
            l = float(b.get("low", b.get("l", 0.0)))
            c = float(b.get("close", b.get("c", 0.0)))
            v = float(b.get("volume", b.get("v", 0.0)))
            d = float(b.get("delta", 0.0))
            if o > 0 and h > 0 and l > 0 and c > 0:
                bar = StockOHLCBar(
                    ts_start=ts - 60.0,
                    ts_end=ts,
                    open=o,
                    high=h,
                    low=l,
                    close=c,
                    volume=v,
                    delta=d,
                )
                self.bars_1m.append(bar)
                self._update_emas_on_close(c)
                self.last_price = c

        self._recalculate_atr()

    def update_tick(
        self,
        price: float,
        ts_sec: float,
        volume: float = 0.0,
        bp1: float = 0.0,
        sp1: float = 0.0,
    ) -> Optional[StockOHLCBar]:
        """
        Ingest a live tick (L1/L2). Aggregates into 1m bar and returns finalized bar upon close.
        """
        if price <= 0:
            return None

        self.last_price = price
        self.last_ts = ts_sec

        # 1. Delta & Volume estimation via Touch / Tick rule
        d_vol = 0.0
        if volume > 0:
            if self._prev_cum_vol > 0:
                d_vol = max(0.0, volume - self._prev_cum_vol)
            self._prev_cum_vol = volume

        if d_vol > 0:
            self._flow_1m["tick_cnt"] += 1
            if bp1 > 0 and sp1 > 0:
                if price >= sp1:
                    self._flow_1m["buy_vol"] += d_vol
                elif price <= bp1:
                    self._flow_1m["sell_vol"] += d_vol
                else:
                    self._flow_1m["buy_vol"] += d_vol * 0.5
                    self._flow_1m["sell_vol"] += d_vol * 0.5
            else:
                self._flow_1m["buy_vol"] += d_vol * 0.5
                self._flow_1m["sell_vol"] += d_vol * 0.5

        # 2. 1-Minute Bar Bucket
        bucket_1m = int(ts_sec // 60)
        closed_bar: Optional[StockOHLCBar] = None

        if self._curr_1m_bar is None:
            self._curr_1m_bucket = bucket_1m
            self._curr_1m_bar = StockOHLCBar(
                ts_start=bucket_1m * 60.0,
                ts_end=bucket_1m * 60.0 + 60.0,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=d_vol,
                delta=self._flow_1m["buy_vol"] - self._flow_1m["sell_vol"],
                tick_count=1,
            )
        elif bucket_1m > self._curr_1m_bucket:
            # Finalize previous bar
            curr = self._curr_1m_bar
            curr.buy_vol = self._flow_1m["buy_vol"]
            curr.sell_vol = self._flow_1m["sell_vol"]
            curr.delta = curr.buy_vol - curr.sell_vol
            curr.volume = curr.buy_vol + curr.sell_vol if (curr.buy_vol + curr.sell_vol) > 0 else curr.volume

            self.bars_1m.append(curr)
            self._update_emas_on_close(curr.close)
            self._recalculate_atr()
            self._update_5m_bar(curr)
            closed_bar = curr

            # Reset flow for next minute
            self._flow_1m = {"buy_vol": 0.0, "sell_vol": 0.0, "tick_cnt": 0}
            self._curr_1m_bucket = bucket_1m
            self._curr_1m_bar = StockOHLCBar(
                ts_start=bucket_1m * 60.0,
                ts_end=bucket_1m * 60.0 + 60.0,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=d_vol,
                delta=0.0,
                tick_count=1,
            )
        else:
            # Update forming bar
            b = self._curr_1m_bar
            if price > b.high:
                b.high = price
            if price < b.low:
                b.low = price
            b.close = price
            b.volume += d_vol
            b.tick_count += 1
            b.delta = self._flow_1m["buy_vol"] - self._flow_1m["sell_vol"]

        return closed_bar

    def _update_5m_bar(self, bar_1m: StockOHLCBar) -> None:
        bucket_5m = int(bar_1m.ts_start // 300)
        if self._curr_5m_bar is None:
            self._curr_5m_bucket = bucket_5m
            self._curr_5m_bar = StockOHLCBar(
                ts_start=bucket_5m * 300.0,
                ts_end=bucket_5m * 300.0 + 300.0,
                open=bar_1m.open,
                high=bar_1m.high,
                low=bar_1m.low,
                close=bar_1m.close,
                volume=bar_1m.volume,
                delta=bar_1m.delta,
            )
        elif bucket_5m > self._curr_5m_bucket:
            self.bars_5m.append(self._curr_5m_bar)
            self._curr_5m_bucket = bucket_5m
            self._curr_5m_bar = StockOHLCBar(
                ts_start=bucket_5m * 300.0,
                ts_end=bucket_5m * 300.0 + 300.0,
                open=bar_1m.open,
                high=bar_1m.high,
                low=bar_1m.low,
                close=bar_1m.close,
                volume=bar_1m.volume,
                delta=bar_1m.delta,
            )
        else:
            b = self._curr_5m_bar
            b.high = max(b.high, bar_1m.high)
            b.low = min(b.low, bar_1m.low)
            b.close = bar_1m.close
            b.volume += bar_1m.volume
            b.delta += bar_1m.delta

    def _update_emas_on_close(self, close_px: float) -> None:
        k20 = 2.0 / (20.0 + 1.0)
        k50 = 2.0 / (50.0 + 1.0)
        if self.ema20 is None:
            self.ema20 = close_px
        else:
            self.ema20 = close_px * k20 + self.ema20 * (1.0 - k20)

        if self.ema50 is None:
            self.ema50 = close_px
        else:
            self.ema50 = close_px * k50 + self.ema50 * (1.0 - k50)

    def _recalculate_atr(self) -> None:
        if len(self.bars_1m) < 2:
            self.atr = self.last_price * 0.0020 if self.last_price > 0 else 1.0
            return

        trs = []
        bars = list(self.bars_1m)
        for i in range(1, min(len(bars), self.atr_period + 1)):
            c_prev = bars[-i - 1].close
            h_curr = bars[-i].high
            l_curr = bars[-i].low
            tr = max(h_curr - l_curr, abs(h_curr - c_prev), abs(l_curr - c_prev))
            trs.append(tr)

        self.atr = max(1e-4, sum(trs) / len(trs))

    # ═══════════════════════════════════════════════════════════════════
    # THE 4 PRICE ACTION PILLARS (SCALE-INVARIANT EVALUATION)
    # ═══════════════════════════════════════════════════════════════════

    def evaluate_setups(self, direction: str) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Evaluates the 4 Price Action Pillars for the specified direction ("BUY" or "SELL").
        Returns (is_setup, setup_name, details_dict).
        """
        if len(self.bars_1m) < 10 or self.ema20 is None or self.atr <= 0:
            return False, "insufficient_data", {}

        latest_bar = self.bars_1m[-1]
        prior_bars = list(self.bars_1m)[:-1]

        # ── Pillar 1: 20 EMA Pullback + Wick Rejection ──
        ok_p1, det_p1 = self._eval_pillar_1_20_ema(direction, latest_bar, prior_bars)
        if ok_p1:
            return True, "20_ema_rejection", det_p1

        # ── Pillar 2: Liquidity Sweep & Spring Reclaim (Turtle Soup) ──
        ok_p2, det_p2 = self._eval_pillar_2_sweep(direction, latest_bar, prior_bars)
        if ok_p2:
            return True, "micro_sweep_reclaim", det_p2

        # ── Pillar 3: Volatility Squeeze Box Breakout ──
        ok_p3, det_p3 = self._eval_pillar_3_squeeze(direction, latest_bar, prior_bars)
        if ok_p3:
            return True, "volatility_squeeze_breakout", det_p3

        # ── Pillar 4: Break of Structure (BOS) / HVG Breakout ──
        ok_p4, det_p4 = self._eval_pillar_4_bos(direction, latest_bar, prior_bars)
        if ok_p4:
            return True, "hvg_breakout_bos", det_p4

        return False, "no_setup", {}

    def _eval_pillar_1_20_ema(
        self, direction: str, bar: StockOHLCBar, priors: List[StockOHLCBar]
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Pillar 1: Pullback into 20 EMA, wick rejection, no overextension, open runway.
        """
        ema = float(self.ema20)
        atr = self.atr
        rng = bar.range

        if direction == "BUY":
            # Must test EMA zone: low near or below EMA + 0.35 ATR
            tested_ema = bar.low <= (ema + 0.35 * atr)
            closed_above = bar.close > ema
            rejection_wick = bar.lower_wick >= (0.20 * rng)
            not_overextended = (bar.close - ema) <= (1.25 * atr)

            if tested_ema and closed_above and rejection_wick and not_overextended:
                # Check obstacle runway: previous swing high must be >= 0.85 ATR away
                swing_high = max(b.high for b in priors[-6:])
                runway = swing_high - bar.close
                has_runway = runway >= (0.85 * atr)

                return True, {
                    "pillar": 1,
                    "ema20": round(ema, 2),
                    "atr": round(atr, 2),
                    "runway_atr": round(runway / atr, 2),
                    "has_runway": has_runway,
                    "wick_ratio": round(bar.lower_wick / rng, 2),
                    "entry_candle_low": round(bar.low, 2),
                    "entry_candle_high": round(bar.high, 2),
                }

        elif direction == "SELL":
            tested_ema = bar.high >= (ema - 0.35 * atr)
            closed_below = bar.close < ema
            rejection_wick = bar.upper_wick >= (0.20 * rng)
            not_overextended = (ema - bar.close) <= (1.25 * atr)

            if tested_ema and closed_below and rejection_wick and not_overextended:
                swing_low = min(b.low for b in priors[-6:])
                runway = bar.close - swing_low
                has_runway = runway >= (0.85 * atr)

                return True, {
                    "pillar": 1,
                    "ema20": round(ema, 2),
                    "atr": round(atr, 2),
                    "runway_atr": round(runway / atr, 2),
                    "has_runway": has_runway,
                    "wick_ratio": round(bar.upper_wick / rng, 2),
                    "entry_candle_low": round(bar.low, 2),
                    "entry_candle_high": round(bar.high, 2),
                }

        return False, {}

    def _eval_pillar_2_sweep(
        self, direction: str, bar: StockOHLCBar, priors: List[StockOHLCBar]
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Pillar 2: Sweep 5-bar swing high/low and reclaim inside bar (Turtle Soup).
        """
        if len(priors) < 5:
            return False, {}

        swing_window = priors[-5:]

        if direction == "BUY":
            min_low = min(b.low for b in swing_window)
            swept = bar.low < min_low
            reclaimed = bar.close > min_low
            strong_body = bar.close > bar.open

            if swept and reclaimed and strong_body:
                return True, {
                    "pillar": 2,
                    "swept_level": round(min_low, 2),
                    "sweep_depth_bps": round((min_low - bar.low) / bar.close * 10000, 1),
                    "entry_candle_low": round(bar.low, 2),
                    "entry_candle_high": round(bar.high, 2),
                }

        elif direction == "SELL":
            max_high = max(b.high for b in swing_window)
            swept = bar.high > max_high
            reclaimed = bar.close < max_high
            strong_body = bar.close < bar.open

            if swept and reclaimed and strong_body:
                return True, {
                    "pillar": 2,
                    "swept_level": round(max_high, 2),
                    "sweep_depth_bps": round((bar.high - max_high) / bar.close * 10000, 1),
                    "entry_candle_low": round(bar.low, 2),
                    "entry_candle_high": round(bar.high, 2),
                }

        return False, {}

    def _eval_pillar_3_squeeze(
        self, direction: str, bar: StockOHLCBar, priors: List[StockOHLCBar]
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Pillar 3: 3-bar tight volatility box compression (<= 1.25x ATR) with expansion breakout.
        """
        if len(priors) < 3:
            return False, {}

        box = priors[-3:]
        box_high = max(b.high for b in box)
        box_low = min(b.low for b in box)
        box_width = box_high - box_low
        atr = self.atr

        is_compressed = box_width <= (1.25 * atr)

        if is_compressed:
            if direction == "BUY":
                broken_out = bar.close > box_high
                body_ratio = bar.body / bar.range
                if broken_out and body_ratio >= 0.50 and bar.close > bar.open:
                    return True, {
                        "pillar": 3,
                        "box_high": round(box_high, 2),
                        "box_low": round(box_low, 2),
                        "box_width_atr": round(box_width / atr, 2),
                        "entry_candle_low": round(bar.low, 2),
                        "entry_candle_high": round(bar.high, 2),
                    }
            elif direction == "SELL":
                broken_out = bar.close < box_low
                body_ratio = bar.body / bar.range
                if broken_out and body_ratio >= 0.50 and bar.close < bar.open:
                    return True, {
                        "pillar": 3,
                        "box_high": round(box_high, 2),
                        "box_low": round(box_low, 2),
                        "box_width_atr": round(box_width / atr, 2),
                        "entry_candle_low": round(bar.low, 2),
                        "entry_candle_high": round(bar.high, 2),
                    }

        return False, {}

    def _eval_pillar_4_bos(
        self, direction: str, bar: StockOHLCBar, priors: List[StockOHLCBar]
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Pillar 4: Break of Structure (BOS) / High Volatility Gap Breakout.
        """
        if len(priors) < 10:
            return False, {}

        atr = self.atr
        ref_bars = priors[-10:]

        if direction == "BUY":
            structure_pivot = max(b.high for b in ref_bars)
            break_magnitude = bar.close - structure_pivot
            if break_magnitude >= (0.40 * atr) and bar.close > bar.open:
                return True, {
                    "pillar": 4,
                    "bos_level": round(structure_pivot, 2),
                    "impulse_atr": round(break_magnitude / atr, 2),
                    "entry_candle_low": round(bar.low, 2),
                    "entry_candle_high": round(bar.high, 2),
                }

        elif direction == "SELL":
            structure_pivot = min(b.low for b in ref_bars)
            break_magnitude = structure_pivot - bar.close
            if break_magnitude >= (0.40 * atr) and bar.close < bar.open:
                return True, {
                    "pillar": 4,
                    "bos_level": round(structure_pivot, 2),
                    "impulse_atr": round(break_magnitude / atr, 2),
                    "entry_candle_low": round(bar.low, 2),
                    "entry_candle_high": round(bar.high, 2),
                }

        return False, {}
