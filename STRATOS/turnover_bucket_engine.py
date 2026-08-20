"""
turnover_bucket_engine.py — Pure Information-Time Bucket Engine for STRATOS.
Accumulates ticks by traded turnover (INR) rather than arbitrary clock-time.
Emits discrete Turnover Buckets with OHLCV, VWAP, Delta, and Duration.
"""
from __future__ import annotations
import numpy as np

class TurnoverBucket:
    __slots__ = (
        "open", "high", "low", "close", "volume", "turnover",
        "buy_volume", "sell_volume", "delta", "vwap",
        "start_ts", "end_ts", "duration_sec", "tick_count"
    )

    def __init__(
        self,
        open_p: float, high_p: float, low_p: float, close_p: float,
        volume: float, turnover: float, buy_volume: float, sell_volume: float,
        delta: float, vwap: float, start_ts: float, end_ts: float,
        duration_sec: float, tick_count: int
    ):
        self.open = open_p
        self.high = high_p
        self.low = low_p
        self.close = close_p
        self.volume = volume
        self.turnover = turnover
        self.buy_volume = buy_volume
        self.sell_volume = sell_volume
        self.delta = delta
        self.vwap = vwap
        self.start_ts = start_ts
        self.end_ts = end_ts
        self.duration_sec = duration_sec
        self.tick_count = tick_count

    def to_dict(self) -> dict:
        return {
            "open": self.open, "high": self.high, "low": self.low, "close": self.close,
            "volume": self.volume, "turnover": self.turnover,
            "buy_volume": self.buy_volume, "sell_volume": self.sell_volume,
            "delta": self.delta, "vwap": self.vwap,
            "start_ts": self.start_ts, "end_ts": self.end_ts,
            "duration_sec": self.duration_sec, "tick_count": self.tick_count
        }


class SymbolBucketAccumulator:
    """Zero-copy in-memory accumulator for a single symbol."""
    def __init__(self, target_turnover_inr: float = 20_000_000.0, max_history: int = 200):
        self.target_turnover = target_turnover_inr
        self.max_history = max_history

        # Current in-progress bucket
        self.reset_current(0.0, 0.0)

        # Historical completed buckets
        self.buckets: list[TurnoverBucket] = []

        # Fast numpy cache
        self.cache_open = np.empty(0, dtype=np.float64)
        self.cache_high = np.empty(0, dtype=np.float64)
        self.cache_low = np.empty(0, dtype=np.float64)
        self.cache_close = np.empty(0, dtype=np.float64)
        self.cache_volume = np.empty(0, dtype=np.float64)
        self.cache_turnover = np.empty(0, dtype=np.float64)
        self.cache_delta = np.empty(0, dtype=np.float64)
        self.last_cum_vol = 0.0

    def reset_current(self, ts: float, price: float):
        self.curr_open = price
        self.curr_high = price
        self.curr_low = price
        self.curr_close = price
        self.curr_vol = 0.0
        self.curr_turnover = 0.0
        self.curr_buy_vol = 0.0
        self.curr_sell_vol = 0.0
        self.curr_pv = 0.0
        self.curr_start_ts = ts
        self.curr_ticks = 0

    def on_tick(self, ts: float, price: float, cumulative_vol: float, is_buy: bool = True) -> TurnoverBucket | None:
        """
        Process a new tick. If accumulated turnover reaches threshold, returns completed TurnoverBucket.
        """
        if price <= 0:
            return None

        # Determine tick volume
        if self.last_cum_vol > 0 and cumulative_vol >= self.last_cum_vol:
            tick_vol = cumulative_vol - self.last_cum_vol
        else:
            tick_vol = 1.0  # fallback for synthetic or single-tick feed
        self.last_cum_vol = cumulative_vol

        tick_turnover = price * tick_vol

        if self.curr_ticks == 0:
            self.curr_open = price
            self.curr_high = price
            self.curr_low = price
            self.curr_start_ts = ts

        # Update OHLC
        if price > self.curr_high:
            self.curr_high = price
        if price < self.curr_low or self.curr_low == 0.0:
            self.curr_low = price
        self.curr_close = price

        self.curr_vol += tick_vol
        self.curr_turnover += tick_turnover
        self.curr_pv += price * tick_vol
        self.curr_ticks += 1

        if is_buy:
            self.curr_buy_vol += tick_vol
        else:
            self.curr_sell_vol += tick_vol

        # Check if bucket turnover threshold is met
        if self.curr_turnover >= self.target_turnover:
            duration = max(0.001, ts - self.curr_start_ts)
            delta = self.curr_buy_vol - self.curr_sell_vol
            vwap = self.curr_pv / self.curr_vol if self.curr_vol > 0 else self.curr_close

            bucket = TurnoverBucket(
                open_p=self.curr_open,
                high_p=self.curr_high,
                low_p=self.curr_low,
                close_p=self.curr_close,
                volume=self.curr_vol,
                turnover=self.curr_turnover,
                buy_volume=self.curr_buy_vol,
                sell_volume=self.curr_sell_vol,
                delta=delta,
                vwap=vwap,
                start_ts=self.curr_start_ts,
                end_ts=ts,
                duration_sec=duration,
                tick_count=self.curr_ticks
            )

            self.buckets.append(bucket)
            if len(self.buckets) > self.max_history:
                self.buckets.pop(0)

            self._update_cache()
            self.reset_current(ts, price)
            return bucket

        return None

    def _update_cache(self):
        n = len(self.buckets)
        if n == 0:
            return
        self.cache_open = np.array([b.open for b in self.buckets], dtype=np.float64)
        self.cache_high = np.array([b.high for b in self.buckets], dtype=np.float64)
        self.cache_low = np.array([b.low for b in self.buckets], dtype=np.float64)
        self.cache_close = np.array([b.close for b in self.buckets], dtype=np.float64)
        self.cache_volume = np.array([b.volume for b in self.buckets], dtype=np.float64)
        self.cache_turnover = np.array([b.turnover for b in self.buckets], dtype=np.float64)
        self.cache_delta = np.array([b.delta for b in self.buckets], dtype=np.float64)

    def get_arrays(self) -> dict[str, np.ndarray]:
        return {
            "open": self.cache_open,
            "high": self.cache_high,
            "low": self.cache_low,
            "close": self.cache_close,
            "volume": self.cache_volume,
            "turnover": self.cache_turnover,
            "delta": self.cache_delta,
        }


class TurnoverBucketEngine:
    """Multi-symbol Turnover Bucket Engine."""
    def __init__(self, symbols: list[str], default_turnover_inr: float = 20_000_000.0, max_history: int = 200):
        self.symbols = symbols
        self.default_turnover = default_turnover_inr
        self.max_history = max_history
        self.accumulators: dict[str, SymbolBucketAccumulator] = {
            sym: SymbolBucketAccumulator(default_turnover_inr, max_history)
            for sym in symbols
        }

    def on_tick(self, symbol: str, ts: float, price: float, cumulative_vol: float, is_buy: bool = True) -> TurnoverBucket | None:
        if symbol not in self.accumulators:
            self.accumulators[symbol] = SymbolBucketAccumulator(self.default_turnover, self.max_history)
        return self.accumulators[symbol].on_tick(ts, price, cumulative_vol, is_buy)

    def get_arrays(self, symbol: str) -> dict[str, np.ndarray]:
        if symbol in self.accumulators:
            return self.accumulators[symbol].get_arrays()
        empty = np.empty(0, dtype=np.float64)
        return {k: empty for k in ["open", "high", "low", "close", "volume", "turnover", "delta"]}

    def get_bucket_count(self, symbol: str) -> int:
        if symbol in self.accumulators:
            return len(self.accumulators[symbol].buckets)
        return 0
