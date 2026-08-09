from __future__ import annotations

import threading
from collections import defaultdict
from datetime import datetime, time as dtime

import pandas as pd

from common.logger import get_logger

logger = get_logger("sofbarbook")

SESSION_START = dtime(9, 15)
SESSION_END = dtime(15, 15)


def floor_3m(ts: datetime) -> datetime:
    m = ts.minute - (ts.minute % 3)
    return ts.replace(minute=m, second=0, microsecond=0)


class BarBook:
    def __init__(self, minutes: int = 3):
        self.minutes = minutes
        self.completed = defaultdict(list)
        self.forming = {}
        self.lock = threading.Lock()

    def floor_bar(self, ts: datetime) -> datetime:
        m = ts.minute - (ts.minute % self.minutes)
        return ts.replace(minute=m, second=0, microsecond=0)

    @staticmethod
    def in_session(ts: datetime) -> bool:
        return SESSION_START <= ts.time() <= SESSION_END

    def seed(self, token: str, df: pd.DataFrame):
        if df is None or len(df) == 0:
            return
        with self.lock:
            existing = {b["timestamp"] for b in self.completed[token]}
            for r in df.itertuples(index=False):
                ts = pd.to_datetime(r.timestamp).to_pydatetime()
                if not self.in_session(ts) or ts in existing:
                    continue
                self.completed[token].append(
                    {
                        "timestamp": ts,
                        "open": float(r.open),
                        "high": float(r.high),
                        "low": float(r.low),
                        "close": float(r.close),
                        "volume": float(getattr(r, "volume", 0.0)),
                    }
                )
                existing.add(ts)
            self.completed[token].sort(key=lambda b: b["timestamp"])
            if len(self.completed[token]) > 2000:
                self.completed[token] = self.completed[token][-2000:]
            logger.info(f"{self.minutes}m BARS: Seeded {len(self.completed[token])} completed bars for key={token}")

    def on_tick(self, token: str, ltp: float, ts: datetime, volume_delta: float = 0.0):
        if ltp <= 0 or not self.in_session(ts):
            return
        bucket = self.floor_bar(ts)
        with self.lock:
            f = self.forming.get(token)
            if f is None or f["timestamp"] != bucket:
                if f is not None:
                    self.completed[token].append(f)
                    if len(self.completed[token]) > 2000:
                        self.completed[token] = self.completed[token][-2000:]
                f = {
                    "timestamp": bucket,
                    "open": ltp,
                    "high": ltp,
                    "low": ltp,
                    "close": ltp,
                    "volume": 0.0,
                }
                self.forming[token] = f
            else:
                f["high"] = max(f["high"], ltp)
                f["low"] = min(f["low"], ltp)
                f["close"] = ltp
            if volume_delta and volume_delta > 0:
                f["volume"] += volume_delta

    def force_close_forming(self, token: str, now: datetime):
        with self.lock:
            f = self.forming.get(token)
            if f is None:
                return
            if f["timestamp"] < self.floor_bar(now):
                self.completed[token].append(f)
                if len(self.completed[token]) > 2000:
                    self.completed[token] = self.completed[token][-2000:]
                self.forming.pop(token, None)

    def completed_df(self, token: str, min_bars: int = 2):
        with self.lock:
            bars = list(self.completed[token])
        if len(bars) < min_bars:
            return None
        df = pd.DataFrame(bars)
        if df.empty:
            return None
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df.sort_values("timestamp").reset_index(drop=True)


class ThreeMinuteBarBook(BarBook):
    def __init__(self):
        super().__init__(3)


def resample_bars(df: pd.DataFrame, minutes: int = 15) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return None
    d = df.copy()
    d["timestamp"] = pd.to_datetime(d["timestamp"])
    d = d.set_index("timestamp")
    r = d.resample(f"{minutes}min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["close"])
    return r.reset_index()
