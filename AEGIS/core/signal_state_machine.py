"""
AEGIS SignalStateMachine — Centralized Signal Lifecycle & Freshness Gating Engine.

State Transitions:
    OBSERVE ──[Permission Active & Score >= 78]──> PRIMED
    PRIMED  ──[Signed Persistence >= 70% & 0 Opposite]──> CONFIRMED
    CONFIRMED ──[Fresh Microstructure Trigger <= 3s]──> FIRED
    FIRED   ──[Order Dispatched]──> COOLDOWN
    COOLDOWN ──[2-min Cooldown Elapsed]──> OBSERVE

Invalidation:
    Any state reverts to OBSERVE if opposite structure, opposite persistence, or stale trigger (> 8s) occurs.
"""

from __future__ import annotations
import enum
import time
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass


class SignalState(enum.Enum):
    OBSERVE = "OBSERVE"
    PRIMED = "PRIMED"
    CONFIRMED = "CONFIRMED"
    FIRED = "FIRED"
    COOLDOWN = "COOLDOWN"


@dataclass
class StrikeLifecycle:
    symbol: str
    state: SignalState = SignalState.OBSERVE
    direction: str = "NEUTRAL"
    prime_ts: float = 0.0
    confirmed_ts: float = 0.0
    fired_ts: float = 0.0
    cooldown_until: float = 0.0
    last_trigger_ts: float = 0.0
    prime_score: float = 0.0


class SignalStateMachine:
    """
    Centralized FSM managing state transitions for each active strike.
    """

    def __init__(
        self,
        max_trigger_freshness_sec: float = 3.0,     # Excellent freshness window
        max_trigger_acceptable_sec: float = 8.0,    # Max acceptable latency
        cooldown_duration_sec: float = 120.0,       # 2-minute post-trade cooldown
        prime_timeout_sec: float = 60.0,            # Reset if not confirmed within 60s
    ):
        self.max_trigger_freshness_sec = max_trigger_freshness_sec
        self.max_trigger_acceptable_sec = max_trigger_acceptable_sec
        self.cooldown_duration_sec = cooldown_duration_sec
        self.prime_timeout_sec = prime_timeout_sec

        self.lifecycles: Dict[str, StrikeLifecycle] = {}

    def get_or_create(self, sym: str) -> StrikeLifecycle:
        if sym not in self.lifecycles:
            self.lifecycles[sym] = StrikeLifecycle(symbol=sym)
        return self.lifecycles[sym]

    def update_trigger(
        self,
        sym: str,
        now_sec: float,
        direction: str,
        micro_score: float,
        has_permission: bool,
        persistence_confirmed: bool,
        option_qualified: bool,
    ) -> Tuple[SignalState, bool, str]:
        """
        Updates the strike lifecycle based on incoming events.
        
        Returns:
            (current_state, should_fire_trade, log_reason)
        """
        lc = self.get_or_create(sym)

        # 1. Cooldown check
        if lc.state == SignalState.COOLDOWN:
            if now_sec < lc.cooldown_until:
                return SignalState.COOLDOWN, False, "in_cooldown"
            else:
                lc.state = SignalState.OBSERVE

        # 2. Permission Hard Gate
        if not has_permission:
            if lc.state in (SignalState.PRIMED, SignalState.CONFIRMED):
                lc.state = SignalState.OBSERVE
                return SignalState.OBSERVE, False, "permission_lost_reset"
            return SignalState.OBSERVE, False, "no_permission"

        # 3. State Invalidation on Direction Flip
        if lc.direction != "NEUTRAL" and lc.direction != direction:
            lc.state = SignalState.OBSERVE
            lc.direction = direction

        lc.last_trigger_ts = now_sec
        trigger_age = now_sec - lc.last_trigger_ts

        # 4. State Transitions
        if lc.state == SignalState.OBSERVE:
            # Transition to PRIMED if score >= 78.0 and permission active
            if micro_score >= 78.0:
                lc.state = SignalState.PRIMED
                lc.direction = direction
                lc.prime_ts = now_sec
                lc.prime_score = micro_score
                return SignalState.PRIMED, False, f"primed_score_{micro_score:.1f}"
            return SignalState.OBSERVE, False, "observing"

        elif lc.state == SignalState.PRIMED:
            # Check timeout
            if (now_sec - lc.prime_ts) > self.prime_timeout_sec:
                lc.state = SignalState.OBSERVE
                return SignalState.OBSERVE, False, "prime_timeout_reset"

            # Transition to CONFIRMED once persistence is confirmed
            if persistence_confirmed and option_qualified:
                lc.state = SignalState.CONFIRMED
                lc.confirmed_ts = now_sec
                # Immediately check trigger freshness for instant fire
                if trigger_age <= self.max_trigger_freshness_sec:
                    lc.state = SignalState.FIRED
                    lc.fired_ts = now_sec
                    return SignalState.FIRED, True, "instant_fresh_confirmed_fire"
                return SignalState.CONFIRMED, False, "persistence_confirmed"
            return SignalState.PRIMED, False, "awaiting_persistence"

        elif lc.state == SignalState.CONFIRMED:
            # State is already confirmed! Check fresh microstructure trigger for exact entry timing
            if not persistence_confirmed or not option_qualified:
                lc.state = SignalState.OBSERVE
                return SignalState.OBSERVE, False, "confirmation_lost_reset"

            # Stale trigger check
            if trigger_age > self.max_trigger_acceptable_sec:
                lc.state = SignalState.OBSERVE
                return SignalState.OBSERVE, False, f"stale_trigger_reset ({trigger_age:.1f}s)"

            if trigger_age <= self.max_trigger_freshness_sec and micro_score >= 75.0:
                lc.state = SignalState.FIRED
                lc.fired_ts = now_sec
                return SignalState.FIRED, True, f"fresh_trigger_fired (age={trigger_age:.2f}s)"

            return SignalState.CONFIRMED, False, "awaiting_fresh_timing_trigger"

        elif lc.state == SignalState.FIRED:
            # Order was fired, transition to cooldown
            lc.state = SignalState.COOLDOWN
            lc.cooldown_until = now_sec + self.cooldown_duration_sec
            return SignalState.COOLDOWN, False, "order_dispatched_entering_cooldown"

        return SignalState.OBSERVE, False, "default"

    def register_trade_executed(self, sym: str, now_sec: float) -> None:
        """Call when an order has been executed to lock in cooldown."""
        lc = self.get_or_create(sym)
        lc.state = SignalState.COOLDOWN
        lc.cooldown_until = now_sec + self.cooldown_duration_sec
