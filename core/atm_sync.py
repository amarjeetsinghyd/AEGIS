"""
atm_sync.py — one ATM, one version, one event stream for AEGIS.

Replaces four independent ATM computations (orchestrator tick path, aegis_main
minute loop, StrikeSelector.calculate_atm_strike, EM pre-market bridge) with a
single hysteresis-protected tracker whose output is an immutable, versioned
snapshot. Everything else reads or subscribes to that snapshot.

Threading contract
  * HysteresisAtmTracker.update()  : called from ONE thread (the tick thread).
  * HysteresisAtmTracker.force()   : any thread (pre-market bridge, tests).
  * snapshot / atm reads           : lock-free from any thread (single
                                     reference read of an immutable tuple).
  * AtmEventBus "fast" listeners   : run inline on the publishing thread. They
                                     MUST be O(strikes) and non-blocking.
  * AtmEventBus "slow" listeners   : run on one daemon worker, latest-wins.
                                     Anything touching the broker lives here.
"""
from __future__ import annotations

import logging
import math
import queue
import threading
import time
from typing import Callable, Dict, FrozenSet, Iterable, List, NamedTuple, Optional, Sequence, Tuple

logger = logging.getLogger("aegis_atm_sync")


# ───────────────────────────── snapshot + tracker ─────────────────────────────

def nearest_strike(spot: float, step: float) -> float:
    """Round-half-up. Python's round() is banker's rounding: exact .5 ties go to
    the even multiple, so 24925 -> 24900 but 24975 -> 25000 (asymmetric)."""
    return math.floor(spot / step + 0.5) * step


class AtmSnapshot(NamedTuple):
    atm: float
    version: int      # monotonically increasing; 0 = not seeded yet
    step: float
    spot: float       # spot that triggered this snapshot
    ts: float         # epoch seconds
    reason: str       # "seed" | "roll" | "force"


class HysteresisAtmTracker:
    """
    Schmitt-trigger ATM.

    The plain-rounding boundary between ATM=A and A+S is the midpoint A+S/2.
    Here a roll away from A needs |spot - A| > S * (0.5 + deadband_frac), so the
    "roll up" and "roll back down" thresholds are 2*deadband_frac*S apart:

        S=50, deadband_frac=0.20 -> roll 24900->24950 at spot > 24935,
                                    roll 24950->24900 at spot < 24915,
                                    no-man's-land 24915..24935 (20 pts).

    Bounds: |spot - ATM| <= S*(0.5+deadband_frac) = 35 pts at steady state.
    min_dwell_sec rate-limits consecutive rolls, but is bypassed when spot is
    already a full strike (catchup_frac*S) away - a real move must never lag.
    """

    __slots__ = ("step", "_roll_dist", "_catchup_dist", "_min_dwell", "_lock", "_snap", "_last_roll_ts")

    def __init__(self, step: float, deadband_frac: float = 0.20,
                 min_dwell_sec: float = 1.0, catchup_frac: float = 1.0):
        if step <= 0:
            raise ValueError("step must be > 0")
        if not 0.0 <= deadband_frac < 0.5:
            raise ValueError("deadband_frac must be in [0, 0.5)")
        if catchup_frac < 0.5 + deadband_frac:
            raise ValueError("catchup_frac must be >= 0.5 + deadband_frac")
        self.step = float(step)
        self._roll_dist = self.step * (0.5 + deadband_frac)
        self._catchup_dist = self.step * catchup_frac
        self._min_dwell = float(min_dwell_sec)
        self._lock = threading.Lock()
        self._snap = AtmSnapshot(0.0, 0, self.step, 0.0, 0.0, "init")
        self._last_roll_ts = 0.0

    @property
    def snapshot(self) -> AtmSnapshot:
        return self._snap

    @property
    def atm(self) -> float:
        return self._snap.atm

    def update(self, spot: float, ts: float) -> Optional[AtmSnapshot]:
        """Hot path. Returns a new snapshot only when ATM actually rolls."""
        snap = self._snap
        if spot <= 0.0:
            return None
        if snap.atm <= 0.0:
            return self._commit(nearest_strike(spot, self.step), spot, ts, "seed")
        dist = abs(spot - snap.atm)
        if dist <= self._roll_dist:
            return None                                   # >99.9% of ticks exit here
        if dist < self._catchup_dist and (ts - self._last_roll_ts) < self._min_dwell:
            return None
        return self._commit(nearest_strike(spot, self.step), spot, ts, "roll")

    def force(self, atm: float, spot: float = 0.0, ts: Optional[float] = None,
              reason: str = "force") -> Optional[AtmSnapshot]:
        """Bypass hysteresis (pre-market pre-align, startup seed, tests)."""
        if atm <= 0.0 or atm == self._snap.atm:
            return None
        return self._commit(float(atm), spot, time.time() if ts is None else ts, reason)

    def _commit(self, atm: float, spot: float, ts: float, reason: str) -> Optional[AtmSnapshot]:
        with self._lock:
            prev = self._snap
            if atm == prev.atm:
                return None
            snap = AtmSnapshot(atm, prev.version + 1, self.step, spot, ts, reason)
            self._snap = snap
            self._last_roll_ts = ts
            return snap


# ───────────────────────────────── event bus ──────────────────────────────────

class AtmEventBus:
    """
    Ordered, fault-isolated fan-out of AtmSnapshot.

    fast lane : inline, registration order, each listener in its own try/except
                (one broken engine can never leave the others on a stale ATM).
    slow lane : single daemon worker; bursts are coalesced (latest snapshot wins).
    acks      : name -> last version a listener finished successfully, so a
                watchdog can assert the invariant "every consumer is on the
                tracker's version" instead of polling ATM once a minute.
    """

    def __init__(self, fast_budget_ms: float = 2.0):
        self._fast: List[Tuple[str, Callable[[AtmSnapshot], None]]] = []
        self._slow: List[Tuple[str, Callable[[AtmSnapshot], None]]] = []
        self._acks: Dict[str, int] = {}
        self._fast_budget_ms = fast_budget_ms
        self._q: "queue.SimpleQueue[AtmSnapshot]" = queue.SimpleQueue()
        self._worker: Optional[threading.Thread] = None
        self._worker_lock = threading.Lock()
        self._last_pub: Tuple[int, float] = (0, 0.0)

    def subscribe(self, name: str, fn: Callable[[AtmSnapshot], None], lane: str = "fast") -> None:
        if lane not in ("fast", "slow"):
            raise ValueError("lane must be 'fast' or 'slow'")
        (self._fast if lane == "fast" else self._slow).append((name, fn))
        self._acks.setdefault(name, 0)

    def publish(self, snap: AtmSnapshot) -> None:
        self._last_pub = (snap.version, time.monotonic())
        for name, fn in self._fast:
            self._call(name, fn, snap, self._fast_budget_ms)
        if self._slow:
            self._ensure_worker()
            self._q.put(snap)

    def lagging(self, current_version: int, grace_sec: float = 2.0) -> List[str]:
        """Listeners not yet on `current_version` after `grace_sec` (watchdog)."""
        ver, t = self._last_pub
        if ver != current_version or (time.monotonic() - t) < grace_sec:
            return []
        return [n for n, v in self._acks.items() if v < current_version]

    # internals
    def _call(self, name: str, fn: Callable[[AtmSnapshot], None], snap: AtmSnapshot,
              budget_ms: Optional[float]) -> None:
        t0 = time.perf_counter()
        try:
            fn(snap)
            self._acks[name] = snap.version
        except Exception:
            logger.exception("[ATM-BUS] listener %s failed on v%d (ATM %.0f)", name, snap.version, snap.atm)
            return
        dt_ms = (time.perf_counter() - t0) * 1000.0
        if budget_ms is not None and dt_ms > budget_ms:
            logger.warning("[ATM-BUS] fast listener %s took %.2f ms (> %.1f ms budget); move it to the slow lane",
                           name, dt_ms, budget_ms)

    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        with self._worker_lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._drain, name="atm-bus-slow", daemon=True)
                self._worker.start()

    def _drain(self) -> None:
        while True:
            snap = self._q.get()
            try:
                while True:                      # coalesce: only the newest ATM matters
                    snap = self._q.get_nowait()
            except queue.Empty:
                pass
            for name, fn in self._slow:
                self._call(name, fn, snap, None)


class AtmSync:
    """Facade owned by the orchestrator: tracker + bus, one call per spot tick."""

    def __init__(self, step: float, deadband_frac: float = 0.20, min_dwell_sec: float = 1.0):
        self.tracker = HysteresisAtmTracker(step, deadband_frac, min_dwell_sec)
        self.bus = AtmEventBus()

    @property
    def atm(self) -> float:
        return self.tracker.atm

    @property
    def snapshot(self) -> AtmSnapshot:
        return self.tracker.snapshot

    def on_spot(self, spot: float, ts: float) -> Optional[AtmSnapshot]:
        snap = self.tracker.update(spot, ts)
        if snap is not None:
            logger.info("[AEGIS] ATM %s: %.0f (v%d, spot=%.2f)", snap.reason, snap.atm, snap.version, snap.spot)
            self.bus.publish(snap)
        return snap

    def force(self, atm: float, spot: float = 0.0, reason: str = "force") -> Optional[AtmSnapshot]:
        snap = self.tracker.force(atm, spot=spot, reason=reason)
        if snap is not None:
            logger.info("[AEGIS] ATM %s: %.0f (v%d)", snap.reason, snap.atm, snap.version)
            self.bus.publish(snap)
        return snap

    def subscribe(self, name: str, fn: Callable[[AtmSnapshot], None], lane: str = "fast") -> None:
        self.bus.subscribe(name, fn, lane)


# ───────────────── sub-engine adapters (duck-typed, never raise) ─────────────────

def make_alignment_listeners(
    gex_engine=None, iceberg_detector=None, shadow_boundary_observer=None,
    orchestrator=None, execution_manager=None,
) -> List[Tuple[str, Callable[[AtmSnapshot], None]]]:
    """
    One listener per consumer, using whichever method that consumer exposes.
    Missing consumers are skipped; the returned list is what got wired, so the
    caller can log it at startup.
    """
    out: List[Tuple[str, Callable[[AtmSnapshot], None]]] = []

    def _bind(name, obj, *method_names):
        if obj is None:
            return
        for m in method_names:
            fn = getattr(obj, m, None)
            if callable(fn):
                out.append((name, lambda s, _fn=fn: _fn(s.atm)))
                return
        logger.warning("[ATM-BUS] %s has none of %s; not wired", name, method_names)

    if orchestrator is not None and hasattr(orchestrator, "_on_atm_roll"):
        out.append(("orchestrator", orchestrator._on_atm_roll))
    _bind("gex_engine", gex_engine, "align_strikes")
    _bind("iceberg_detector", iceberg_detector, "align_strikes")
    _bind("shadow_boundary_observer", shadow_boundary_observer, "update_atm", "set_atm_strike")
    if execution_manager is not None and hasattr(execution_manager, "on_atm_roll"):
        out.append(("execution_manager", execution_manager.on_atm_roll))
    return out


# ───────────────────────── wide-band subscription policy ──────────────────────────

class BandPlan(NamedTuple):
    center: float
    rows: object                    # pandas.DataFrame: chain rows inside the band
    tokens: FrozenSet[str]          # full band
    new_tokens: Tuple[str, ...]     # in band, not yet subscribed
    stale_tokens: Tuple[str, ...]   # subscribed earlier, now outside band


class BandSubscriptionManager:
    """
    Anchored band with lazy recentering - policy only, no broker I/O of its own.

        band     = center +/- half_width strikes (both sides)         e.g. +/-25
        corridor = strikes the engine actually evaluates around ATM   e.g. +/-7
        recenter = only when |ATM - center| >= recenter_at strikes    e.g. 8

    With 25/7/8 the nearest un-subscribed strike is always >= 10 strikes
    (500 NIFTY pts) beyond the corridor, and any strike added at a recenter sits
    >= 17 strikes from ATM, so it accumulates ticks long before it becomes
    tradeable. Subscriptions are add-only; unsubscribe happens only if the
    token cap would be exceeded.
    """

    def __init__(self, step: float, half_width: int = 25, corridor: int = 7,
                 recenter_at: int = 8, max_tokens: int = 190, strike_scale: float = 1.0):
        self.step = float(step)
        self.half_width = int(half_width)
        self.corridor = int(corridor)
        self.recenter_at = int(recenter_at)
        self.max_tokens = int(max_tokens)
        self.strike_scale = float(strike_scale)      # 100.0 for Angel-style strike*100 chains
        self.center: Optional[float] = None
        self.subscribed: set = set()
        self._token_strike: Dict[str, float] = {}
        self._refresh_requested = threading.Event()
        self._last_attempt = 0.0

    # decisions
    def drift_steps(self, atm: float) -> float:
        return float("inf") if self.center is None else abs(atm - self.center) / self.step

    def should_refresh(self, atm: float) -> bool:
        return self.center is None or self.drift_steps(atm) >= self.recenter_at or self._refresh_requested.is_set()

    def corridor_covered(self, atm: float) -> bool:
        """The re-subscription invariant: ATM +/- corridor lies inside the band."""
        return self.center is not None and self.drift_steps(atm) + self.corridor <= self.half_width

    def request_refresh(self, reason: str = "") -> None:
        if reason:
            logger.info("[BAND] refresh requested: %s", reason)
        self._refresh_requested.set()

    def refresh_pending(self, min_gap_sec: float = 5.0) -> bool:
        """True when a refresh was requested and the last attempt was >= min_gap_sec ago.
        Non-consuming: the flag is cleared only by apply()/mark_refreshed(), so a failed
        fetch is retried, but never faster than min_gap_sec."""
        if not self._refresh_requested.is_set():
            return False
        now = time.monotonic()
        if now - self._last_attempt < min_gap_sec:
            return False
        self._last_attempt = now
        return True

    def on_atm(self, snap: AtmSnapshot) -> None:
        """Slow-lane listener: never calls the broker, only raises a flag that
        the existing main loop (which owns the broker handles) polls each pass."""
        if not self.corridor_covered(snap.atm):
            self.request_refresh(f"corridor not covered at ATM {snap.atm:.0f} (center {self.center})")
        elif self.drift_steps(snap.atm) >= self.recenter_at:
            self.request_refresh(f"ATM drifted {self.drift_steps(snap.atm):.0f} strikes from band center")

    def mark_refreshed(self, center: float, chain, strike_col: str = "strike",
                       token_col: str = "token") -> None:
        """Call after the existing subscribe/refresh path succeeded with `chain`.
        Measures the symmetric half-width the chain really covers, so the policy
        is correct whatever window the fetcher returns."""
        strikes = chain[strike_col].astype(float) / self.strike_scale
        up = (float(strikes.max()) - center) / self.step
        dn = (center - float(strikes.min())) / self.step
        self.half_width = max(0, int(min(up, dn) + 1e-6))
        if self.recenter_at + self.corridor >= self.half_width:
            fixed = max(1, self.half_width - self.corridor - 2)
            logger.warning("[BAND] universe covers only +/-%d strikes around %.0f; recenter_at %d -> %d. "
                           "Widen the fetcher window to >= +/-%d for lazy recentering.",
                           self.half_width, center, self.recenter_at, fixed, self.corridor + self.recenter_at + 3)
            self.recenter_at = fixed
        self.subscribed |= {str(t) for t in chain[token_col].tolist()}
        self.center = float(center)
        self._refresh_requested.clear()

    # planning / commit
    def plan(self, chain, atm: float, strike_col: str = "strike", token_col: str = "token") -> BandPlan:
        reach = self.half_width * self.step
        strikes = chain[strike_col].astype(float) / self.strike_scale
        rows = chain[(strikes - atm).abs() <= reach + 1e-6]
        tokens = frozenset(str(t) for t in rows[token_col].tolist())
        new = tuple(sorted(tokens - self.subscribed))
        stale = tuple(sorted(self.subscribed - tokens))
        self._token_strike.update(
            {str(t): float(s) / self.strike_scale for t, s in zip(rows[token_col], rows[strike_col])}
        )
        return BandPlan(float(atm), rows, tokens, new, stale)

    def apply(self, plan: BandPlan, subscribe_fn: Callable[[Sequence[str], str], None],
              unsubscribe_fn: Optional[Callable[[Sequence[str]], None]] = None,
              batch: int = 50) -> int:
        """Subscribe only the delta (t+d feeds), then commit. Returns #tokens added."""
        for i in range(0, len(plan.new_tokens), batch):
            chunk = list(plan.new_tokens[i:i + batch])
            subscribe_fn(chunk, "t")
            subscribe_fn(chunk, "d")
        self.subscribed |= set(plan.new_tokens)
        overflow = len(self.subscribed) - self.max_tokens
        if overflow > 0 and unsubscribe_fn is not None and plan.stale_tokens:
            far_first = sorted(plan.stale_tokens,
                               key=lambda t: -abs(self._token_strike.get(t, plan.center) - plan.center))
            drop = far_first[:overflow]
            unsubscribe_fn(drop)
            self.subscribed -= set(drop)
        self.center = plan.center
        self._refresh_requested.clear()
        return len(plan.new_tokens)


# ─────────────────────────────── warm-cache check ────────────────────────────────

def cold_corridor_symbols(strike_to_symbol: Dict[Tuple[int, float], str],
                          tick_ring: Dict[str, Sequence[float]],
                          atm: float, step: float, depth: int = 3,
                          min_ticks: int = 3) -> List[Tuple[str, int]]:
    """
    Symbols inside ATM +/- depth with fewer than `min_ticks` ticks in the rolling
    30 s ring. Pure function: call it on each roll (slow lane) and LOG the result
    first; gate entries on it only after you have seen real numbers.
    """
    cold: List[Tuple[str, int]] = []
    for k in range(-depth, depth + 1):
        strike = atm + k * step
        for side in (0, 1):
            sym = strike_to_symbol.get((side, strike))
            if sym is None:
                cold.append((f"<missing {side}:{strike:.0f}>", 0))
                continue
            n = len(tick_ring.get(sym, ()))
            if n < min_ticks:
                cold.append((sym, n))
    return cold
