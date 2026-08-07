"""
Astra V4.4 — Signal Router
MUTEX STATE MACHINE: Evaluated ONCE per 1m candle close. Never per tick.
Priority hierarchy (explicit, non-negotiable): C(veto) > B(absorption) > A(breakout) > D(imbalance)
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


# Priority: lower number = higher priority
SETUP_PRIORITY = {
    'B': 0,  # Absorption overrides all
    'A': 1,  # Breakout overrides imbalance
    'D': 2,  # Imbalance is lowest
}


class SignalRouter:
    """
    Route(bar, flow, structure) -> RouterState
    - Hard mutex: armed is not None or trader.has_open_trade() => HOLD
    - Context gate: NO_MANS_LAND => IDLE (zero logic)
    - Veto shield from Setup C blocks A and D (both sides), but NOT B
    - Select winner by priority then strength
    - Arm SINGLETON signal; new arm rejected while any arm/trade exists
    """
    
    def __init__(self):
        self.armed: Optional[ArmedSignal] = None
        self.cooldown_until: Optional[float] = None
        self.veto_until: Optional[float] = None
        
        # Telemetry counters
        self.counters: Dict[str, int] = {
            'armed': 0,
            'triggered': 0,
            'expired': 0,
            'invalidated': 0,
            'won': 0,
            'trap_guard_block': 0,
        }
    
    def route(self, bar: dict, norm_delta: float, prev_norm_delta: Optional[float],
              structure, now: float, detectors) -> RouterState:
        """
        Main routing logic called once per 1m candle close.
        Returns RouterState for logging/telemetry.
        """
        # Hard mutex: if armed or trade open, hold
        if self.armed is not None:
            return RouterState.HOLD
        
        # Cooldown check
        if self.cooldown_until is not None and now < self.cooldown_until:
            return RouterState.COOLDOWN
        
        # Context gate
        ctx = structure.context_state(bar['c'])
        if ctx == 'NO_MANS_LAND':
            return RouterState.CONTEXT_NO_SIGNAL
        
        # Check Setup C veto (fires if conditions met)
        if detectors.detect_C(bar, norm_delta, structure, now):
            # Veto activated
            pass
        
        veto_active = self.veto_until is not None and now < self.veto_until
        
        # Collect candidates
        cands: List = []
        
        # Setup B runs even under veto (reversal-aligned)
        b = detectors.detect_B([bar], norm_delta, prev_norm_delta, structure)
        if b:
            cands.append(b)
        
        # Veto kills momentum setups (A and D)
        if not veto_active:
            a = detectors.detect_A([bar], norm_delta, structure)
            if a:
                cands.append(a)
            
            d = detectors.detect_D([bar], norm_delta, structure)
            if d:
                cands.append(d)
        
        if not cands:
            return RouterState.CONTEXT_NO_SIGNAL
        
        # Select winner: priority first, then strength (higher strength wins)
        winner = sorted(cands, key=lambda c: (SETUP_PRIORITY[c.setup], -c.strength))[0]
        
        # Arm the signal (singleton - mutual exclusion guaranteed)
        # Note: spot0, prem0, target_token filled by main.py when arming
        self.armed = ArmedSignal(
            setup=winner.setup,
            side=winner.side,
            strength=winner.strength,
            arm_time=now,
            spot0=0.0,  # Filled by main.py
            prem0=0.0,  # Filled by main.py
            target_token='',  # Filled by main.py
        )
        
        self.counters['armed'] += 1
        
        return RouterState.ARM
    
    def trigger_armed(self):
        """Called when pending window triggers."""
        self.counters['triggered'] += 1
        self.armed = None
        self.cooldown_until = None  # Reset cooldown on successful trigger
    
    def expire_armed(self, now: float):
        """Called when pending window expires or invalidates."""
        if self.armed is not None:
            self.counters['expired'] += 1
        self.armed = None
        self.cooldown_until = now + 60.0  # 1-min cooldown on expire/invalidate
    
    def invalidate_armed(self, now: float):
        """Called when pending window invalidates."""
        if self.armed is not None:
            self.counters['invalidated'] += 1
        self.armed = None
        self.cooldown_until = now + 60.0
    
    def trade_won(self):
        """Called when a trade closes in profit."""
        self.counters['won'] += 1
    
    def trap_guard_block(self):
        """Called when L2 trap guard blocks entry."""
        self.counters['trap_guard_block'] += 1
        self.armed = None
        self.cooldown_until = None  # No cooldown on trap block
    
    def get_telemetry(self) -> dict:
        """Return current telemetry counters."""
        return self.counters.copy()
    
    def clear_veto_if_expired(self, now: float):
        """Clear veto if expired."""
        if self.veto_until is not None and now >= self.veto_until:
            self.veto_until = None
