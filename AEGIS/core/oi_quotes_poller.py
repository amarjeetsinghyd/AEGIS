"""
OIQuotesPoller — Background ATM OI Enricher for AEGIS (off hot path).

The live touchline feed carries OI on only ~1.6% of option ticks, so the
OI kinematics shield spends its mornings fail-open (thin history). This
poller tops up the OIBuiltupGate every 30 seconds by pulling REST quotes
for the ATM ± N strikes — the exact contracts the trigger path evaluates.

Threading model mirrors IndexVWAPEngine: one daemon thread, poll interval
sleep, fail-soft on every error. NEVER touches gate thresholds — it only
feeds/update_tick telemetry data. Hot-path tick flow is untouched.
"""

from __future__ import annotations
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

try:
    from common.logger import get_logger as _get_logger
    logger = _get_logger("aegis_oi_poller")
except Exception:
    logger = logging.getLogger("aegis_oi_poller")


class OIQuotesPoller:
    """30-second background OI enricher (daemon thread)."""

    def __init__(
        self,
        oi_gate: Any = None,
        broker_api: Any = None,
        poll_interval_sec: float = 30.0,
        strikes_each_side: int = 5,
        universe_provider: Optional[Callable[[], Dict[str, Any]]] = None,
    ):
        self.oi_gate = oi_gate
        self.broker_api = broker_api
        self.poll_interval_sec = float(poll_interval_sec or 30.0)
        self.strikes_each_side = int(strikes_each_side or 5)
        self.universe_provider = universe_provider
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.polls = 0
        self.enriched = 0
        self.errors = 0

    def set_broker_api(self, broker_api: Any) -> None:
        self.broker_api = broker_api

    def set_oi_gate(self, oi_gate: Any) -> None:
        self.oi_gate = oi_gate

    def start_background(self) -> bool:
        """Starts the daemon poll thread. Returns True when running."""
        try:
            if self._running and self._thread is not None and self._thread.is_alive():
                return True
            self._running = True
            self._thread = threading.Thread(
                target=self._poll_loop, name="oi-quotes-poller", daemon=True
            )
            self._thread.start()
            logger.info(
                f"[OIQuotesPoller] started (interval={self.poll_interval_sec:.0f}s, "
                f"ATM±{self.strikes_each_side})"
            )
            return True
        except Exception as e:
            logger.warning(f"[OIQuotesPoller] start failed (non-fatal): {e}")
            self._running = False
            return False

    def stop(self) -> None:
        self._running = False

    def _poll_loop(self) -> None:
        while self._running:
            try:
                self.poll_once()
            except Exception as e:
                self.errors += 1
                logger.warning(f"[OIQuotesPoller] poll error (non-fatal): {e}")
            time.sleep(self.poll_interval_sec)

    def _pick_targets(self) -> List[Dict[str, Any]]:
        """Selects ATM ± N strike rows from the universe provider snapshot."""
        if self.universe_provider is None:
            return []
        try:
            uni = self.universe_provider() or {}
        except Exception:
            return []
        spot = float(uni.get("spot", 0.0) or 0.0)
        step = float(uni.get("strike_step", 50.0) or 50.0)
        rows = uni.get("rows", []) or []
        if spot <= 0 or not rows:
            return []
        atm = round(spot / step) * step
        span = self.strikes_each_side * step
        targets = []
        for r in rows:
            try:
                stk = float(r.get("strike", 0.0) or 0.0)
            except Exception:
                continue
            if stk <= 0 or abs(stk - atm) > span:
                continue
            targets.append(r)
        return targets

    @staticmethod
    def _exchange_for(sym: str) -> str:
        s = str(sym or "").upper()
        if "SENSEX" in s:
            return "BFO"
        return "NFO"

    def poll_once(self) -> int:
        """One enrichment pass. Returns number of contracts enriched."""
        gate = self.oi_gate
        api = self.broker_api
        if gate is None or api is None or not hasattr(api, "get_quotes"):
            return 0
        if not self._running:
            return 0
        targets = self._pick_targets()
        if not targets:
            return 0
        now = time.time()
        done = 0
        for r in targets:
            if not self._running:
                break
            try:
                tok = str(r.get("token", "") or "")
                sym = str(r.get("sym", "") or "")
                stk = float(r.get("strike", 0.0) or 0.0)
                kind = int(r.get("kind", 0) or 0)
                if not tok or not sym or stk <= 0 or kind not in (1, 2):
                    continue
                q = api.get_quotes(exchange=self._exchange_for(sym), token=tok)
                if not isinstance(q, dict) or q.get("stat") != "Ok":
                    continue
                oi = q.get("oi", 0.0)
                try:
                    oi_f = float(oi or 0.0)
                except Exception:
                    continue
                if oi_f <= 0:
                    continue
                lp = q.get("lp", 0.0)
                try:
                    lp_f = float(lp or 0.0)
                except Exception:
                    lp_f = 0.0
                with self._lock:
                    gate.update_tick(sym, stk, kind, lp_f, oi_f, now)
                done += 1
            except Exception:
                self.errors += 1
                continue
        if done > 0:
            self.polls += 1
            self.enriched += done
            logger.info(f"[OIQuotesPoller] enriched {done} contracts (total={self.enriched})")
        return done
