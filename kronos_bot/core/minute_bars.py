"""1-minute OHLC aggregation from index LTP ticks for Supertrend gating."""
from __future__ import annotations

import os
from datetime import datetime, date, time as dt_time, timedelta
from typing import Optional

import pandas as pd

from common.logger import get_logger

logger = get_logger("minute_bars")

# Continuous session end: last usable 1m stamp is 15:14 (bar covers 15:14→15:15)
_SESSION_LAST_1M = dt_time(15, 14, 0)
_SESSION_OPEN = dt_time(9, 15, 0)


def _floor_minute(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)


class MinuteBarAggregator:
    """Build completed 1m OHLC bars from spot LTP ticks; optional parquet persist."""

    def __init__(self, data_dir: str, index_name: str = "NIFTY"):
        self.data_dir = data_dir
        self.index_name = (index_name or "NIFTY").upper()
        self._session_date: Optional[date] = None
        self._completed: list[dict] = []
        self._forming_minute: Optional[datetime] = None
        self._forming: Optional[dict] = None

    def _parquet_path(self) -> str:
        return os.path.join(self.data_dir, f"underlying_1m_{self.index_name}.parquet")

    def reset_for_session(self, session_day: date, index_name: str = None):
        if index_name:
            self.index_name = index_name.upper()
        self._session_date = session_day
        self._completed = []
        self._forming_minute = None
        self._forming = None

    def set_index(self, index_name: str):
        name = (index_name or "NIFTY").upper()
        if name != self.index_name:
            self._persist()
            self.index_name = name
            self._completed = []
            self._forming_minute = None
            self._forming = None
            self._load_persisted_today()

    def seed(self, df: pd.DataFrame):
        """Replace completed bars from a REST/historical OHLC frame (completed minutes only)."""
        if df is None or df.empty:
            return
        frame = df.copy()
        if "timestamp" not in frame.columns:
            return
        frame["timestamp"] = pd.to_datetime(frame["timestamp"])
        rows = []
        for _, r in frame.iterrows():
            ts = r["timestamp"].to_pydatetime() if hasattr(r["timestamp"], "to_pydatetime") else r["timestamp"]
            if not isinstance(ts, datetime):
                continue
            ts = _floor_minute(ts)
            if ts.time() < _SESSION_OPEN or ts.time() > _SESSION_LAST_1M:
                continue
            o = float(r.get("open") or 0)
            h = float(r.get("high") or 0)
            l = float(r.get("low") or 0)
            c = float(r.get("close") or 0)
            if c <= 0:
                continue
            rows.append({"timestamp": ts, "open": o or c, "high": h or c, "low": l or c, "close": c})
        rows.sort(key=lambda x: x["timestamp"])
        # Dedupe by timestamp keeping last
        by_ts = {r["timestamp"]: r for r in rows}
        self._completed = [by_ts[k] for k in sorted(by_ts.keys())]
        self._forming_minute = None
        self._forming = None
        if self._completed:
            self._session_date = self._completed[-1]["timestamp"].date()
        self._persist()
        logger.info(f"[1m BARS] Seeded {len(self._completed)} completed bars for {self.index_name}")

    def _load_persisted_today(self):
        path = self._parquet_path()
        if not os.path.exists(path):
            return
        try:
            df = pd.read_parquet(path)
            if df is None or df.empty:
                return
            today = datetime.now().date()
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df = df[df["timestamp"].dt.date == today]
            if len(df):
                self.seed(df)
        except Exception as exc:
            logger.warning(f"[1m BARS] load persist failed: {exc}")

    def _persist(self):
        if not self._completed:
            return
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            df = pd.DataFrame(self._completed)
            df.to_parquet(self._parquet_path(), index=False)
        except Exception as exc:
            logger.warning(f"[1m BARS] persist failed: {exc}")

    def on_tick(self, ltp: float, ts: datetime = None) -> Optional[dict]:
        """
        Feed a spot LTP. Returns the bar just closed when the minute rolls, else None.
        """
        if ltp is None or float(ltp) <= 0:
            return None
        ltp = float(ltp)
        now = ts or datetime.now()
        if not isinstance(now, datetime):
            now = datetime.now()

        day = now.date()
        if self._session_date is None:
            self._session_date = day
            self._load_persisted_today()
        elif day != self._session_date:
            self._persist()
            self.reset_for_session(day)

        if now.time() < _SESSION_OPEN or now.time() > dt_time(15, 15, 0):
            return None

        minute = _floor_minute(now)
        if minute.time() > _SESSION_LAST_1M:
            # Still allow closing the 15:14 bar when clock hits 15:15
            if self._forming is not None and self._forming_minute and self._forming_minute.time() <= _SESSION_LAST_1M:
                closed = self._close_forming()
                self._persist()
                return closed
            return None

        closed = None
        if self._forming_minute is None:
            self._forming_minute = minute
            self._forming = {"timestamp": minute, "open": ltp, "high": ltp, "low": ltp, "close": ltp}
        elif minute > self._forming_minute:
            closed = self._close_forming()
            self._forming_minute = minute
            self._forming = {"timestamp": minute, "open": ltp, "high": ltp, "low": ltp, "close": ltp}
            self._persist()
        else:
            f = self._forming
            f["high"] = max(f["high"], ltp)
            f["low"] = min(f["low"], ltp)
            f["close"] = ltp
        return closed

    def _close_forming(self) -> Optional[dict]:
        if self._forming is None:
            return None
        bar = dict(self._forming)
        # Replace if same timestamp already present
        self._completed = [b for b in self._completed if b["timestamp"] != bar["timestamp"]]
        self._completed.append(bar)
        self._completed.sort(key=lambda x: x["timestamp"])
        self._forming = None
        self._forming_minute = None
        return bar

    def completed_df(self) -> pd.DataFrame:
        if not self._completed:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close"])
        return pd.DataFrame(self._completed).copy()

    def completed_count(self) -> int:
        return len(self._completed)


def seed_minute_bars_from_rest(broker, index_name: str, cash_token: str, cash_exch: str) -> pd.DataFrame:
    """Pull today's 1m cash OHLC via Shoonya historical (for aggregator.seed)."""
    try:
        hist = getattr(broker, "historical_provider", None)
        if hist is None:
            return pd.DataFrame()
        now = datetime.now()
        start = datetime.combine(now.date(), _SESSION_OPEN)
        end = now
        df = hist.get_historical(cash_exch, str(cash_token), "ONE_MINUTE", start, end)
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.copy()
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        # Keep continuous session only
        df = df[
            (df["timestamp"].dt.time >= _SESSION_OPEN)
            & (df["timestamp"].dt.time <= _SESSION_LAST_1M)
        ]
        return df.reset_index(drop=True)
    except Exception as exc:
        logger.warning(f"[1m BARS] REST seed failed for {index_name}: {exc}")
        return pd.DataFrame()
