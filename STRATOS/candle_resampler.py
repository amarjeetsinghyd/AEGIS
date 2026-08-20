"""
candle_resampler.py — High-performance, zero-allocation multi-timeframe candle aggregator.
Constructs live 1-Hour, 15-Minute, and 5-Minute OHLCV candles from raw WebSocket tick stream.
"""
from __future__ import annotations
import time
from collections import deque
from datetime import datetime, timezone
import numpy as np

class Candle:
    __slots__ = ("open", "high", "low", "close", "volume", "start_ts", "end_ts", "tick_count", "buy_vol", "sell_vol")
    def __init__(self, ts: float, open_p: float, high_p: float, low_p: float, close_p: float, vol: float):
        self.start_ts = ts
        self.end_ts = ts
        self.open = float(open_p)
        self.high = float(high_p)
        self.low = float(low_p)
        self.close = float(close_p)
        self.volume = float(vol)
        self.tick_count = 1
        self.buy_vol = 0.0
        self.sell_vol = 0.0

    def update(self, price: float, dvol: float, is_buy: bool):
        if price > self.high:
            self.high = price
        elif price < self.low:
            self.low = price
        self.close = price
        self.volume += dvol
        self.tick_count += 1
        if is_buy:
            self.buy_vol += dvol
        else:
            self.sell_vol += dvol

    def to_dict(self):
        return {
            "open": self.open, "high": self.high, "low": self.low, "close": self.close,
            "volume": self.volume, "start_ts": self.start_ts, "buy_vol": self.buy_vol,
            "sell_vol": self.sell_vol, "delta": self.buy_vol - self.sell_vol
        }

class SymbolCandleSeries:
    def __init__(self, symbol: str, max_bars: int = 200):
        self.symbol = symbol
        self.max_bars = max_bars
        
        # Completed historical bars (ring buffers)
        self.bars_1h = deque(maxlen=max_bars)
        self.bars_15m = deque(maxlen=max_bars)
        self.bars_5m = deque(maxlen=max_bars)
        
        # Current active (forming) bars
        self.active_1h: Candle | None = None
        self.active_15m: Candle | None = None
        self.active_5m: Candle | None = None
        
        self.last_vol = None

    def on_tick(self, ts: float, price: float, cumulative_vol: float, is_buy: bool) -> dict:
        """
        Process tick, update active bars, roll completed bars, and return events.
        Returns dict with keys: 'closed_5m', 'closed_15m', 'closed_1h' (True/False).
        """
        if price <= 0:
            return {"closed_5m": False, "closed_15m": False, "closed_1h": False}
            
        dvol = 0.0 if self.last_vol is None else max(0.0, cumulative_vol - self.last_vol)
        self.last_vol = cumulative_vol

        events = {"closed_5m": False, "closed_15m": False, "closed_1h": False}
        
        # Compute bar boundary epoch intervals
        bar_ts_5m = int(ts // 300) * 300
        bar_ts_15m = int(ts // 900) * 900
        bar_ts_1h = int(ts // 3600) * 3600

        # ── 5-Minute Candle Logic ──
        if self.active_5m is None:
            self.active_5m = Candle(bar_ts_5m, price, price, price, price, dvol)
        elif bar_ts_5m != self.active_5m.start_ts:
            self.bars_5m.append(self.active_5m)
            events["closed_5m"] = True
            self.active_5m = Candle(bar_ts_5m, price, price, price, price, dvol)
        else:
            self.active_5m.update(price, dvol, is_buy)

        # ── 15-Minute Candle Logic ──
        if self.active_15m is None:
            self.active_15m = Candle(bar_ts_15m, price, price, price, price, dvol)
        elif bar_ts_15m != self.active_15m.start_ts:
            self.bars_15m.append(self.active_15m)
            events["closed_15m"] = True
            self.active_15m = Candle(bar_ts_15m, price, price, price, price, dvol)
        else:
            self.active_15m.update(price, dvol, is_buy)

        # ── 1-Hour Candle Logic ──
        if self.active_1h is None:
            self.active_1h = Candle(bar_ts_1h, price, price, price, price, dvol)
        elif bar_ts_1h != self.active_1h.start_ts:
            self.bars_1h.append(self.active_1h)
            events["closed_1h"] = True
            self.active_1h = Candle(bar_ts_1h, price, price, price, price, dvol)
        else:
            self.active_1h.update(price, dvol, is_buy)

        return events

    def get_df_arrays(self, tf: str = "5m"):
        """Extract fast numpy arrays (opens, highs, lows, closes, volumes, deltas) for TA-Lib."""
        if tf == "5m":
            bars = list(self.bars_5m)
            if self.active_5m:
                bars.append(self.active_5m)
        elif tf == "15m":
            bars = list(self.bars_15m)
            if self.active_15m:
                bars.append(self.active_15m)
        else:
            bars = list(self.bars_1h)
            if self.active_1h:
                bars.append(self.active_1h)

        n = len(bars)
        if n == 0:
            return None

        opens = np.array([b.open for b in bars], dtype=np.float64)
        highs = np.array([b.high for b in bars], dtype=np.float64)
        lows = np.array([b.low for b in bars], dtype=np.float64)
        closes = np.array([b.close for b in bars], dtype=np.float64)
        volumes = np.array([b.volume for b in bars], dtype=np.float64)
        deltas = np.array([b.buy_vol - b.sell_vol for b in bars], dtype=np.float64)

        return {
            "open": opens, "high": highs, "low": lows, "close": closes,
            "volume": volumes, "delta": deltas, "count": n
        }

class MultiTimeframeResampler:
    def __init__(self, symbols: list[str]):
        self.series = {sym: SymbolCandleSeries(sym) for sym in symbols}

    def on_tick(self, symbol: str, ts: float, price: float, cumulative_vol: float, is_buy: bool) -> dict:
        ser = self.series.get(symbol)
        if not ser:
            ser = SymbolCandleSeries(symbol)
            self.series[symbol] = ser
        return ser.on_tick(ts, price, cumulative_vol, is_buy)

    def get_arrays(self, symbol: str, tf: str = "5m"):
        ser = self.series.get(symbol)
        return ser.get_df_arrays(tf) if ser else None
