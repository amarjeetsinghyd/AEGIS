"""ARGUS pillar overlay (pilot): Aegis 3-pillar setups evaluated per stock.

Pilot uses closed 1-minute bars only (no mid-bar hop), paper mode unchanged.
PriceActionEngine is imported lazily so ARGUS hot path stays light.
Per-stock VWAP uses 0.0 (filter disabled) until stock VWAP feed is wired.

Micro validator: after a pillar fires, the previous 30s/60s of Micro data
must confirm a genuine move — no forward waiting. Uses the value engine's
60s persistence window plus trailing MLOFI/flow means.
"""

from __future__ import annotations

import time
from typing import Dict, Tuple, Any


class PillarOverlay:
    """Maintains one PriceActionEngine per stock symbol (1m bars, EMA20/50, ATR)."""

    def __init__(self, max_stocks: int = 30, value_engine: Any = None):
        self.max_stocks = int(max_stocks)
        self.value_engine = value_engine
        self._engines: Dict[str, Any] = {}
        self._order: list = []

    def _get_engine(self, sym: str):
        eng = self._engines.get(sym)
        if eng is None:
            from AEGIS.core.price_action_engine import PriceActionEngine

            eng = PriceActionEngine()
            self._engines[sym] = eng
            self._order.append(sym)
            if len(self._order) > self.max_stocks:
                old = self._order.pop(0)
                self._engines.pop(old, None)
        return eng

    def on_minute(self, sym: str, price: float, ts_sec: float = 0.0, vwap: float = 0.0) -> Tuple[bool, str, Dict[str, Any]]:
        """Feed one minute close; returns (is_setup, setup_name, details)."""
        if price <= 0:
            return False, "no_price", {}
        if ts_sec <= 0:
            ts_sec = time.time()
        eng = self._get_engine(sym)
        try:
            eng.update_tick(float(price), float(ts_sec))
        except Exception:
            return False, "feed_error", {}
        for direction in ("UP", "DOWN"):
            try:
                ok, name, details = eng.evaluate_setup(direction, float(vwap or 0.0))
            except Exception:
                continue
            if not ok:
                continue
            details = dict(details or {})
            passed, reason, vmetrics = self._validate_trailing_micro(sym, direction)
            details["validator"] = reason
            details.update(vmetrics)
            if not passed:
                return False, f"validator_reject_{reason}", details
            return True, str(name), details
        return False, "no_price_action_setup", {}

    @staticmethod
    def _mean_tail(hist, n: int) -> float:
        try:
            vals = [float(v) for v in list(hist or [])[-n:] if v is not None]
        except Exception:
            return 0.0
        vals = [v for v in vals if v == v]  # drop NaN
        if not vals:
            return 0.0
        return sum(vals) / len(vals)

    def _validate_trailing_micro(self, sym: str, direction: str) -> Tuple[bool, str, Dict[str, Any]]:
        """Trailing 30s/60s Micro confirmation for a fired pillar (no forward wait)."""
        ve = self.value_engine
        if ve is None:
            return True, "no_engine_permissive", {}
        metrics: Dict[str, Any] = {}
        # 1. 60s directional persistence window (primary validator).
        try:
            pengines = getattr(ve, "persistence_engines", {}) or {}
            peng = pengines.get(sym)
            if peng is not None:
                ok, score, pm = peng.evaluate_persistence(direction)
                metrics["persist_60s"] = round(float(score or 0.0), 3)
                if not ok:
                    return False, "persistence_opposed", metrics
        except Exception:
            pass
        # 2. Trailing MLOFI means: 30s proxy (last 10) and 60s proxy (last 20).
        try:
            mh = getattr(ve, "mlofi_history", {}).get(sym, [])
            m30 = self._mean_tail(mh, 10)
            m60 = self._mean_tail(mh, 20)
            metrics["mlofi_30s"] = round(m30, 3)
            metrics["mlofi_60s"] = round(m60, 3)
            if len(list(mh or [])) >= 5:
                if direction == "UP" and (m30 < -0.5 and m60 < -0.5):
                    return False, "mlofi_opposed", metrics
                if direction == "DOWN" and (m30 > 0.5 and m60 > 0.5):
                    return False, "mlofi_opposed", metrics
        except Exception:
            pass
        # 3. Flow side from latest snapshot (S_flow long vs short).
        try:
            snap = (getattr(ve, "last_snap", {}) or {}).get(sym, {}) or {}
            fl = float(snap.get("s_flow_long", 0.0) or 0.0)
            fs = float(snap.get("s_flow_short", 0.0) or 0.0)
            metrics["s_flow_long"] = round(fl, 3)
            metrics["s_flow_short"] = round(fs, 3)
            if direction == "UP" and fs > fl + 0.4:
                return False, "flow_opposed", metrics
            if direction == "DOWN" and fl > fs + 0.4:
                return False, "flow_opposed", metrics
        except Exception:
            pass
        return True, "trailing_micro_confirmed", metrics
