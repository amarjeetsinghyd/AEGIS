"""
Astra V4.4 — Signal Router
MUTEX STATE MACHINE: Evaluated ONCE per 1m candle close. Never per tick.
Priority: C(veto) > B(absorption) > A(breakout) > D(imbalance)
"""
from dataclasses import dataclass
from typing import Optional, Dict, List
from enum import Enum

class RouterState(Enum):
    IDLE = "IDLE"
    HOLD = "HOLD"
    COOLDOWN = "COOLDOWN"
    ARM = "ARM"
    CONTEXT_NO_SIGNAL = "CONTEXT_NO_SIGNAL"

@dataclass
class ArmedSignal:
    setup: str
    side: str
    strength: float
    arm_time: float
    spot0: float
    prem0: float
    target_token: str

SETUP_PRIORITY = {'B': 0, 'A': 1, 'D': 2}

class SignalRouter:
    def __init__(self):
        self.armed: Optional[ArmedSignal] = None
        self.cooldown_until: Optional[float] = None
        self.veto_until: Optional[float] = None
        self.counters: Dict[str, int] = {
            'armed': 0, 'triggered': 0, 'expired': 0,
            'invalidated': 0, 'won': 0, 'trap_guard_block': 0,
        }

    def route(self, bars, deltas, structure, now, detectors):
        if self.armed is not None:
            return RouterState.HOLD
        if self.cooldown_until is not None and now < self.cooldown_until:
            return RouterState.COOLDOWN
        self.clear_veto_if_expired(now)
        bar = bars[-1]
        norm_delta = deltas[-1] if deltas else 0.0
        prev_norm_delta = deltas[-2] if len(deltas) >= 2 else None
        ctx = structure.context_state(bar['c'])
        if ctx == 'NO_MANS_LAND':
            return RouterState.CONTEXT_NO_SIGNAL
        if detectors.detect_C(bar, norm_delta, structure, now):
            self.veto_until = now + 300.0
        veto_active = self.veto_until is not None and now < self.veto_until
        cands = []
        b = detectors.detect_B(bars, norm_delta, prev_norm_delta, structure)
        if b: cands.append(b)
        if not veto_active:
            a = detectors.detect_A(bars, norm_delta, structure)
            if a: cands.append(a)
            d = detectors.detect_D(bars, deltas, structure)
            if d: cands.append(d)
        if not cands:
            return RouterState.CONTEXT_NO_SIGNAL
        winner = sorted(cands, key=lambda c: (SETUP_PRIORITY[c.setup], -c.strength))[0]
        self.armed = ArmedSignal(
            setup=winner.setup, side=winner.side, strength=winner.strength,
            arm_time=now, spot0=0.0, prem0=0.0, target_token=''
        )
        self.counters['armed'] += 1
        return RouterState.ARM

    def trigger_armed(self):
        self.counters['triggered'] += 1
        self.armed = None
        self.cooldown_until = None

    def expire_armed(self, now: float):
        if self.armed is not None:
            self.counters['expired'] += 1
        self.armed = None
        self.cooldown_until = now + 60.0

    def invalidate_armed(self, now: float):
        if self.armed is not None:
            self.counters['invalidated'] += 1
        self.armed = None
        self.cooldown_until = now + 60.0

    def trade_won(self):
        self.counters['won'] += 1

    def trap_guard_block(self):
        self.counters['trap_guard_block'] += 1
        self.armed = None

    def get_telemetry(self):
        return self.counters.copy()

    def clear_veto_if_expired(self, now: float):
        if self.veto_until is not None and now >= self.veto_until:
            self.veto_until = None
