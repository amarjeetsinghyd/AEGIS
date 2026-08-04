from enum import Enum

class DecisionLifecycle(str, Enum):
    """
    Canonical Decision Lifecycle Stages.
    Standardized Enum to trace every opportunity transition through the platform.
    """
    OBSERVED = "OBSERVED"     # Level 0: Logged on normal index ticks (no candidate setup)
    CANDIDATE = "CANDIDATE"   # Level 1: Strategy trigger hit (crossover/rejection/breakout)
    FILTERED = "FILTERED"     # Level 1: Blocked by trading limit, cooldown, or hour filters
    SNIPER = "SNIPER"         # Level 1: Setup accepted, locked on option contract and delta
    EXECUTED = "EXECUTED"     # Level 2: Position entry executed (premium delta crossed)
    EXITED = "EXITED"         # Level 2: Position closed (target, stop-loss, or gamma stall)
    RESOLVED = "RESOLVED"     # Level 2: Offline EOD processing & counterfactual analysis finalized
