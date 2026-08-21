"""
candle_resampler.py — High-performance, zero-allocation multi-timeframe candle aggregator & historical preloader.
Constructs live 1-Hour, 15-Minute, and 5-Minute OHLCV candles from raw WebSocket tick stream and REST API quotes,
and preloads canonical historical parquet datasets to eliminate historical gaps.
"""
from __future__ import annotations
import os
import sys
import time
from collections import deque
from datetime import datetime, timezone
import numpy as np
import pandas as pd
from common.logger import get_logger

logger = get_logger("stratos_resampler")

class Candle:
    __slots__ = ("open", "high", "low", "close", "volume", "start_ts", "end_ts", "tick_count", "buy_vol", "sell_vol")
    def __init__(self, ts: float, open_p: float, high_p: float, low_p: float, close_p: float, vol: float, buy_vol: float = 0.0, sell_vol: float = 0.0):
        self.start_ts = ts
        self.end_ts = ts
        self.open = float(open_p)
        self.high = float(high_p)
        self.low = float(low_p)
        self.close = float(close_p)
        self.volume = float(vol)
        self.tick_count = 1
        self.buy_vol = float(buy_vol)
        self.sell_vol = float(sell_vol)

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

    def append_historical_bar(self, tf: str, open_p: float, high_p: float, low_p: float, close_p: float, vol: float, delta: float = 0.0, ts: float = 0.0):
        buy_vol = max(0.0, (vol + delta) / 2.0)
        sell_vol = max(0.0, (vol - delta) / 2.0)
        c = Candle(ts, open_p, high_p, low_p, close_p, vol, buy_vol, sell_vol)
        if tf == "15m":
            self.bars_15m.append(c)
        elif tf == "1h":
            self.bars_1h.append(c)
        elif tf == "5m":
            self.bars_5m.append(c)

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
        self.preload_historical_candles()

    def preload_historical_candles(self, data_root: str = None):
        """
        Preloads multi-day 15m and 1h historical candles from canonical parquet datasets
        so that 15m pivots, 20-EMA, 9-EMA, and 1H trends have immediate historical depth with zero gaps.
        """
        candidates = [
            data_root,
            "/home/opc/data/canonical_candles",
            os.path.join(os.getcwd(), "data", "canonical_candles"),
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "canonical_candles")
        ]
        valid_root = None
        for c in candidates:
            if c and os.path.exists(c):
                valid_root = c
                break

        if not valid_root:
            logger.info("[PRELOAD] No canonical historical candle directory found — starting clean.")
            return

        # 1. Preload 15m Canonical Candles
        path_15m = os.path.join(valid_root, "15m", "canonical_15m_all_symbols.parquet")
        if os.path.exists(path_15m):
            try:
                df_15 = pd.read_parquet(path_15m)
                loaded_cnt = 0
                for sym, grp in df_15.groupby("symbol"):
                    if sym in self.series:
                        sub = grp.sort_values("dt").tail(100)
                        for _, r in sub.iterrows():
                            self.series[sym].append_historical_bar(
                                tf="15m",
                                open_p=float(r["open"]),
                                high_p=float(r["high"]),
                                low_p=float(r["low"]),
                                close_p=float(r["close"]),
                                vol=float(r.get("volume", 0)),
                                delta=float(r.get("delta", 0)),
                                ts=float(pd.to_datetime(r["dt"]).timestamp()) if "dt" in r else 0.0
                            )
                        loaded_cnt += 1
                logger.info(f"[PRELOAD] ✅ Loaded 15m historical candles for {loaded_cnt} symbols from {path_15m}")
            except Exception as e:
                logger.warning(f"[PRELOAD] Failed loading 15m parquet: {e}")

        # 2. Preload 1H Canonical Candles
        path_1h = os.path.join(valid_root, "1h", "canonical_1h_all_symbols.parquet")
        if os.path.exists(path_1h):
            try:
                df_1h = pd.read_parquet(path_1h)
                loaded_1h = 0
                for sym, grp in df_1h.groupby("symbol"):
                    if sym in self.series:
                        sub = grp.sort_values("dt").tail(100)
                        for _, r in sub.iterrows():
                            self.series[sym].append_historical_bar(
                                tf="1h",
                                open_p=float(r["open"]),
                                high_p=float(r["high"]),
                                low_p=float(r["low"]),
                                close_p=float(r["close"]),
                                vol=float(r.get("volume", 0)),
                                delta=float(r.get("delta", 0)),
                                ts=float(pd.to_datetime(r["dt"]).timestamp()) if "dt" in r else 0.0
                            )
                        loaded_1h += 1
                logger.info(f"[PRELOAD] ✅ Loaded 1H historical candles for {loaded_1h} symbols from {path_1h}")
            except Exception as e:
                logger.warning(f"[PRELOAD] Failed loading 1H parquet: {e}")

        # 3. Preload 5m Canonical Candles
        path_5m = os.path.join(valid_root, "5m", "canonical_5m_all_symbols.parquet")
        if os.path.exists(path_5m):
            try:
                df_5m = pd.read_parquet(path_5m)
                loaded_5m = 0
                for sym, grp in df_5m.groupby("symbol"):
                    if sym in self.series:
                        sub = grp.sort_values("dt").tail(100)
                        for _, r in sub.iterrows():
                            self.series[sym].append_historical_bar(
                                tf="5m",
                                open_p=float(r["open"]),
                                high_p=float(r["high"]),
                                low_p=float(r["low"]),
                                close_p=float(r["close"]),
                                vol=float(r.get("volume", 0)),
                                delta=float(r.get("delta", 0)),
                                ts=float(pd.to_datetime(r["dt"]).timestamp()) if "dt" in r else 0.0
                            )
                        loaded_5m += 1
                logger.info(f"[PRELOAD] ✅ Loaded 5m historical candles for {loaded_5m} symbols from {path_5m}")
            except Exception as e:
                logger.warning(f"[PRELOAD] Failed loading 5m parquet: {e}")

    def on_tick(self, symbol: str, ts: float, price: float, cumulative_vol: float, is_buy: bool) -> dict:
        ser = self.series.get(symbol)
        if not ser:
            ser = SymbolCandleSeries(symbol)
            self.series[symbol] = ser
        return ser.on_tick(ts, price, cumulative_vol, is_buy)

    def get_arrays(self, symbol: str, tf: str = "5m"):
        ser = self.series.get(symbol)
        return ser.get_df_arrays(tf) if ser else None
