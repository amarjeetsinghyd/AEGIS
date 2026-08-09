"""Multi-timeframe OHLCV bar books (3m primary + 15m for ATR15).
Thread-safe, session-filtered, deduped, capped at 2000 bars per token.
Books are keyed by SYMBOL (main/tick_compiler translate token->symbol)."""
from __future__ import annotations
import threading
from collections import defaultdict
from datetime import datetime, time as dtime
import pandas as pd
from common.logger import get_logger

logger = get_logger("sof_bar_book")

SESSION_START = dtime(9, 15)
SESSION_END = dtime(15, 15)


def floor_3m(ts: datetime) -> datetime:
    """Module-level helper (kept for historical_data_manager import compatibility)."""
    m = ts.minute - (ts.minute % 3)
    return ts.replace(minute=m, second=0, microsecond=0)


class BarBook:
    def __init__(self, minutes: int = 3):
        self.minutes = int(minutes)
        self._completed = defaultdict(list)
        self._forming = {}
        self.lock = threading.Lock()

    def floor_bar(self, ts: datetime) -> datetime:
        m = ts.minute - (ts.minute % self.minutes)
        return ts.replace(minute=m, second=0, microsecond=0)

    @staticmethod
    def _in_session(ts: datetime) -> bool:
        return SESSION_START <= ts.time() <= SESSION_END

    def seed(self, token: str, df: pd.DataFrame):
        if df is None or len(df) == 0:
            return
        with self.lock:
            existing = {b["timestamp"] for b in self._completed[token]}
            for r in df.itertuples(index=False):
                ts = r.timestamp
                if not self._in_session(ts) or ts in existing:
                    continue
                self._completed[token].append({
                    "timestamp": ts,
                    "open": float(r.open), "high": float(r.high), "low": float(r.low),
                    "close": float(r.close), "volume": float(r.volume),
                })
                existing.add(ts)
            self._completed[token].sort(key=lambda b: b["timestamp"])
            if len(self._completed[token]) > 2000:
                self._completed[token] = self._completed[token][-2000:]
            logger.info(f"[{self.minutes}m BARS] Seeded {len(self._completed[token])} completed bars for key={token}")

    def on_tick(self, token: str, ltp: float, ts: datetime, volume_delta: float = 0.0):
        if ltp <= 0 or not self._in_session(ts):
            return
        bucket = self.floor_bar(ts)
        with self.lock:
            f = self._forming.get(token)
            if f is None or f["timestamp"] != bucket:
                if f is not None:
                    self._completed[token].append(f)
                    if len(self._completed[token]) > 2000:
                        self._completed[token] = self._completed[token][-2000:]
                f = {"timestamp": bucket, "open": ltp, "high": ltp, "low": ltp,
                     "close": ltp, "volume": 0.0}
                self._forming[token] = f
            f["high"] = max(f["high"], ltp)
            f["low"] = min(f["low"], ltp)
            f["close"] = ltp
            if volume_delta and volume_delta > 0:
                f["volume"] += volume_delta

    def force_close_forming(self, token: str, now: datetime):
        """Close the forming bar only if its window has ended (3m book at each boundary)."""
        with self.lock:
            f = self._forming.pop(token, None)
            if f is None:
                return
            if f["timestamp"] < self.floor_bar(now):
                self._completed[token].append(f)
                if len(self._completed[token]) > 2000:
                    self._completed[token] = self._completed[token][-2000:]
            else:
                self._forming[token] = f

    def completed_df(self, token: str, min_bars: int = 2) -> pd.DataFrame:
        with self.lock:
            bars = list(self._completed[token])
        if len(bars) < min_bars:
            return None
        return pd.DataFrame(bars)


class ThreeMinuteBarBook(BarBook):
    def __init__(self):
        super().__init__(3)


def resample_bars(df: pd.DataFrame, minutes: int = 15) -> pd.DataFrame:
    """Resample 3m bars into higher-TF bars (for seeding the 15m book)."""
    if df is None or len(df) == 0:
        return None
    d = df.copy()
    d["timestamp"] = pd.to_datetime(d["timestamp"])
    d = d.set_index("timestamp")
    r = d.resample(f"{minutes}min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["close"])
    return r.reset_index()