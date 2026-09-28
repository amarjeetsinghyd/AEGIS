"""
IndexVWAPEngine — True Institutional Index VWAP & Session Structure Engine for AEGIS.

Calculates:
1. Aggregate Raw Volume of Underlying Constituents (NIFTY 50 / SENSEX 30) from constituents.json.
2. Volume-Weighted Average Price (VWAP):
   VWAP_t = sum(Spot_t * Delta_V_t) / sum(Delta_V_t)
3. Session Structure: Session High, Session Low, 5-Min Rolling High/Low.
4. Over-extension Metrics: Distance from Spot to VWAP in scale-invariant Basis Points (bps).

Non-blocking, thread-safe, and zero-impact on sub-millisecond option tick hot path.
"""

from __future__ import annotations
import os
import json
import time
import threading
import logging
from typing import Dict, Optional, Tuple, Any
from collections import deque

logger = logging.getLogger("aegis_vwap")

CONSTITUENTS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "institutional_memory",
    "constituents.json"
)


class IndexVWAPEngine:
    """
    Computes volume-weighted average price (VWAP) for cash index using constituent volumes.
    """

    def __init__(
        self,
        index_name: str = "NIFTY",
        strike_step: float = 50.0,
        max_extension_bps: float = 25.0,
        poll_interval_sec: float = 60.0,
        broker_api: Any = None,
    ):
        self.index_name = index_name.upper()
        self.strike_step = float(strike_step)
        self.max_extension_bps = float(max_extension_bps)
        self.poll_interval_sec = float(poll_interval_sec)
        self.broker_api = broker_api

        self.constituents: Dict[str, str] = self._load_constituents()
        self.last_stock_volumes: Dict[str, float] = {}
        self.cum_pv: float = 0.0
        self.cum_vol: float = 0.0
        self.current_vwap: float = 0.0
        self.total_raw_volume: float = 0.0

        self.session_high: float = 0.0
        self.session_low: float = float("inf")
        self.last_spot: float = 0.0

        self.bars_5m: deque = deque(maxlen=60)
        self._curr_5m_bucket: int = -1
        self._curr_5m_h: float = 0.0
        self._curr_5m_l: float = float("inf")
        self._curr_5m_c: float = 0.0
        self._curr_5m_vol: float = 0.0

        self._lock = threading.Lock()
        self._running = False
        self._worker_thread: Optional[threading.Thread] = None

    def _load_constituents(self) -> Dict[str, str]:
        if not os.path.exists(CONSTITUENTS_PATH):
            logger.warning(f"[IndexVWAP] constituents.json not found at {CONSTITUENTS_PATH}")
            return {}

        try:
            with open(CONSTITUENTS_PATH, "r") as f:
                data = json.load(f)

            if "SENSEX" in self.index_name or self.strike_step >= 100.0:
                return {str(k): str(v) for k, v in data.get("sensex_30", {}).items()}
            else:
                return {str(k): str(v) for k, v in data.get("nifty_50", {}).items()}
        except Exception as e:
            logger.error(f"[IndexVWAP] Failed to load constituents: {e}")
            return {}

    def update_spot(self, spot: float, ts_sec: float = 0.0) -> None:
        if spot <= 0:
            return

        with self._lock:
            self.last_spot = spot
            if self.session_high == 0.0 or spot > self.session_high:
                self.session_high = spot
            if self.session_low == float("inf") or spot < self.session_low:
                self.session_low = spot

            if ts_sec > 0:
                bucket_5m = int(ts_sec // 300)
                if self._curr_5m_bucket == -1:
                    self._curr_5m_bucket = bucket_5m
                    self._curr_5m_h = spot
                    self._curr_5m_l = spot
                    self._curr_5m_c = spot
                elif bucket_5m == self._curr_5m_bucket:
                    if spot > self._curr_5m_h:
                        self._curr_5m_h = spot
                    if spot < self._curr_5m_l:
                        self._curr_5m_l = spot
                    self._curr_5m_c = spot
                else:
                    self.bars_5m.append((
                        self._curr_5m_bucket * 300.0,
                        self._curr_5m_h,
                        self._curr_5m_l,
                        self._curr_5m_c,
                        self._curr_5m_vol
                    ))
                    self._curr_5m_bucket = bucket_5m
                    self._curr_5m_h = spot
                    self._curr_5m_l = spot
                    self._curr_5m_c = spot
                    self._curr_5m_vol = 0.0

    def update_raw_volume(self, delta_vol: float, spot_ref: Optional[float] = None) -> None:
        if delta_vol <= 0:
            return

        with self._lock:
            spot = spot_ref if (spot_ref and spot_ref > 0) else self.last_spot
            if spot <= 0:
                return

            self.cum_pv += spot * delta_vol
            self.cum_vol += delta_vol
            self.total_raw_volume += delta_vol
            self._curr_5m_vol += delta_vol

            if self.cum_vol > 0:
                self.current_vwap = self.cum_pv / self.cum_vol

    def get_vwap_metrics(self, spot: Optional[float] = None) -> Dict[str, Any]:
        with self._lock:
            curr_spot = spot if (spot and spot > 0) else self.last_spot
            vwap = self.current_vwap if self.current_vwap > 0 else curr_spot

            dist_pts = curr_spot - vwap if (curr_spot > 0 and vwap > 0) else 0.0
            dist_bps = (dist_pts / vwap * 10000.0) if vwap > 0 else 0.0

            is_overextended_up = dist_bps > self.max_extension_bps
            is_overextended_down = dist_bps < -self.max_extension_bps

            return {
                "vwap": round(vwap, 2),
                "spot": round(curr_spot, 2),
                "dist_pts": round(dist_pts, 2),
                "dist_bps": round(dist_bps, 2),
                "session_high": round(self.session_high, 2) if self.session_high > 0 else curr_spot,
                "session_low": round(self.session_low, 2) if self.session_low < float("inf") else curr_spot,
                "total_raw_volume": self.total_raw_volume,
                "is_overextended_up": is_overextended_up,
                "is_overextended_down": is_overextended_down,
            }

    def start_background_poller(self) -> None:
        if self._running or not self.broker_api:
            return

        self._running = True
        self._worker_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._worker_thread.start()
        logger.info(f"[IndexVWAP] Started background constituent volume poller ({len(self.constituents)} stocks)")

    def stop_background_poller(self) -> None:
        self._running = False
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2.0)

    def start_poller(self) -> None:
        """Alias of start_background_poller — external callers may use either name."""
        self.start_background_poller()

    def stop(self) -> None:
        """Alias of stop_background_poller."""
        self.stop_background_poller()

    def _poll_loop(self) -> None:
        exch = "BSE" if ("SENSEX" in self.index_name or self.strike_step >= 100.0) else "NSE"

        while self._running:
            try:
                self._poll_constituents_volume(exch)
            except Exception as e:
                logger.warning(f"[IndexVWAP] Error in poll loop: {e}")

            time.sleep(self.poll_interval_sec)

    def _poll_constituents_volume(self, exch: str) -> None:
        if not self.broker_api or not hasattr(self.broker_api, "get_quotes"):
            return

        total_delta_vol = 0.0

        for sym, tok in self.constituents.items():
            if not self._running:
                break
            try:
                q = self.broker_api.get_quotes(exchange=exch, token=str(tok))
                if isinstance(q, dict) and q.get("stat") == "Ok":
                    cum_v = float(q.get("v") or 0.0)
                    if cum_v > 0:
                        prev_v = self.last_stock_volumes.get(str(tok), cum_v)
                        delta = cum_v - prev_v
                        if delta > 0:
                            total_delta_vol += delta
                        self.last_stock_volumes[str(tok)] = cum_v
            except Exception:
                continue

        if total_delta_vol > 0:
            self.update_raw_volume(total_delta_vol)
            logger.info(
                f"[IndexVWAP] Polled {len(self.constituents)} stocks | "
                f"DeltaVol={int(total_delta_vol):,} | VWAP={self.current_vwap:.2f} | "
                f"Spot={self.last_spot:.2f}"
            )
