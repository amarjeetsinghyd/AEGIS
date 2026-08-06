"""Canonical 3-minute OHLC bars built from WebSocket LTP / volume ticks."""
from __future__ import annotations

from datetime import datetime, time as dt_time
from typing import Optional

import pandas as pd

from common.logger import get_logger

logger = get_logger("sof_bar_book")

BAR_MINUTES = 3
_SESSION_OPEN = dt_time(9, 15, 0)
_SESSION_END = dt_time(15, 15, 0)


def floor_3m(ts: datetime) -> datetime:
    """Align to 09:15 grid: 09:15, 09:18, 09:21, … 15:12."""
    ts = ts.replace(second=0, microsecond=0)
    # minutes 0,3,6,...,57 already on grid with 15%3==0
    m = ts.minute - (ts.minute % BAR_MINUTES)
    return ts.replace(minute=m)


class ThreeMinuteBarBook:
    """Per-token completed 3m OHLCV from WS (completed bars only for TSF)."""

    def __init__(self):
        self._completed: dict[str, list[dict]] = {}
        self._forming: dict[str, dict] = {}
        self._forming_bucket: dict[str, datetime] = {}

    def seed(self, token: str, df: pd.DataFrame) -> int:
        """Optional REST THREE_MINUTE warmup so TSF is ready near open."""
        token = str(token)
        if df is None or getattr(df, "empty", True) or "close" not in df.columns:
            return 0
        frame = df.copy()
        if "timestamp" in frame.columns:
            frame["timestamp"] = pd.to_datetime(frame["timestamp"])
        else:
            return 0
        rows = []
        for _, r in frame.iterrows():
            ts = r["timestamp"]
            if hasattr(ts, "to_pydatetime"):
                ts = ts.to_pydatetime()
            if not isinstance(ts, datetime):
                continue
            ts = floor_3m(ts)
            if ts.time() < _SESSION_OPEN or ts.time() >= _SESSION_END:
                continue
            c = float(r.get("close") or 0)
            if c <= 0:
                continue
            rows.append(
                {
                    "timestamp": ts,
                    "open": float(r.get("open") or c),
                    "high": float(r.get("high") or c),
                    "low": float(r.get("low") or c),
                    "close": c,
                    "volume": float(r.get("volume") or 0),
                }
            )
        by_ts = {r["timestamp"]: r for r in rows}
        self._completed[token] = [by_ts[k] for k in sorted(by_ts.keys())]
        self._forming.pop(token, None)
        self._forming_bucket.pop(token, None)
        n = len(self._completed[token])
        if n:
            logger.info(f"[3m BARS] Seeded {n} completed bars for token={token}")
        return n

    def on_tick(self, token: str, ltp: float, ts: datetime = None, volume_delta: float = 0.0):
        token = str(token)
        if ltp is None or float(ltp) <= 0:
            return
        ltp = float(ltp)
        now = ts or datetime.now()
        if not isinstance(now, datetime):
            now = datetime.now()
        if now.time() < _SESSION_OPEN or now.time() >= _SESSION_END:
            return

        bucket = floor_3m(now)
        forming = self._forming.get(token)
        cur_bucket = self._forming_bucket.get(token)

        if forming is None or cur_bucket is None:
            self._forming_bucket[token] = bucket
            self._forming[token] = {
                "timestamp": bucket,
                "open": ltp,
                "high": ltp,
                "low": ltp,
                "close": ltp,
                "volume": max(0.0, float(volume_delta or 0)),
            }
            return

        if bucket > cur_bucket:
            # Close prior completed bar
            self._completed.setdefault(token, []).append(dict(forming))
            self._forming_bucket[token] = bucket
            self._forming[token] = {
                "timestamp": bucket,
                "open": ltp,
                "high": ltp,
                "low": ltp,
                "close": ltp,
                "volume": max(0.0, float(volume_delta or 0)),
            }
            return

        forming["high"] = max(forming["high"], ltp)
        forming["low"] = min(forming["low"], ltp)
        forming["close"] = ltp
        forming["volume"] = float(forming.get("volume") or 0) + max(0.0, float(volume_delta or 0))

    def force_close_forming(self, token: str, now: datetime = None) -> Optional[dict]:
        """At boundary, close forming bar if its bucket has ended."""
        token = str(token)
        now = now or datetime.now()
        forming = self._forming.get(token)
        cur_bucket = self._forming_bucket.get(token)
        if forming is None or cur_bucket is None:
            return None
        live_bucket = floor_3m(now)
        if live_bucket > cur_bucket:
            closed = dict(forming)
            self._completed.setdefault(token, []).append(closed)
            self._forming.pop(token, None)
            self._forming_bucket.pop(token, None)
            return closed
        return None

    def completed_df(self, token: str, min_bars: int = 14) -> Optional[pd.DataFrame]:
        token = str(token)
        rows = self._completed.get(token) or []
        if len(rows) < min_bars:
            return None
        df = pd.DataFrame(rows)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df.reset_index(drop=True)

    def completed_count(self, token: str) -> int:
        return len(self._completed.get(str(token)) or [])
