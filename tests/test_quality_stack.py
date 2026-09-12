"""
Unit tests for AEGIS Quality Stack:
- QualitySlotPolicy (3 quality slots/day, afternoon reservation, dynamic bar, consecutive-loss stop, daily cap)
- PostTriggerStabilityTracker (10s post-trigger stability telemetry, hold/fade verdict)
- Execution Manager safety state cross-wiring
"""

import time
import pytest
from datetime import datetime

from AEGIS.core.quality_stack import QualitySlotPolicy, PostTriggerStabilityTracker
from AEGIS.aegis_execution_manager import AegisExecutionManager


def test_quality_slot_policy_max_trades_limit():
    policy = QualitySlotPolicy(max_trades_per_day=3, reserve_release_min=900, dynamic_bar=False, consec_loss_stop=2)
    dt_afternoon = datetime(2026, 9, 14, 15, 0, 0)
    ts = dt_afternoon.timestamp()

    # Slot 1
    admit, reason = policy.admit(score=75.0, now_sec=ts)
    assert admit is True
    assert reason == "slot_admit"
    policy.mark_admitted(75.0)
    assert policy.used == 1

    # Slot 2
    admit, reason = policy.admit(score=80.0, now_sec=ts)
    assert admit is True
    assert reason == "slot_admit"
    policy.mark_admitted(80.0)
    assert policy.used == 2

    # Slot 3 (release time reached)
    admit, reason = policy.admit(score=85.0, now_sec=ts)
    assert admit is True
    assert reason == "slot_admit"
    policy.mark_admitted(85.0)
    assert policy.used == 3

    # Slot 4 -> Must be rejected (limit reached)
    admit, reason = policy.admit(score=90.0, now_sec=ts)
    assert admit is False
    assert reason == "slot_limit"


def test_quality_slot_policy_consecutive_loss_stop():
    policy = QualitySlotPolicy(max_trades_per_day=3, consec_loss_stop=2)
    dt = datetime(2026, 9, 14, 15, 0, 0)
    ts = dt.timestamp()

    # 1 loss: still allowed
    admit, reason = policy.admit(score=80.0, now_sec=ts, consec_losses=1)
    assert admit is True

    # 2 consecutive losses: circuit breaker fires!
    admit, reason = policy.admit(score=85.0, now_sec=ts, consec_losses=2)
    assert admit is False
    assert reason == "consec_loss_stop"


def test_quality_slot_policy_daily_loss_cap():
    policy = QualitySlotPolicy(max_trades_per_day=3, daily_loss_cap_rs=2000.0)
    dt = datetime(2026, 9, 14, 15, 0, 0)
    ts = dt.timestamp()

    # PnL = -1500 (under cap): allowed
    admit, reason = policy.admit(score=80.0, now_sec=ts, daily_pnl=-1500.0)
    assert admit is True

    # PnL = -2100 (exceeded cap): blocked!
    admit, reason = policy.admit(score=80.0, now_sec=ts, daily_pnl=-2100.0)
    assert admit is False
    assert reason == "daily_loss_cap"


def test_quality_slot_policy_afternoon_reservation():
    # Reserve release at 14:45 IST = 885 minutes
    policy = QualitySlotPolicy(max_trades_per_day=3, reserve_release_min=885, dynamic_bar=False)
    
    # Morning: 10:30 IST = 630 minutes
    dt_morning = datetime(2026, 9, 14, 10, 30, 0)
    ts_morning = dt_morning.timestamp()

    # Slot 1 in morning: allowed
    admit, _ = policy.admit(score=75.0, now_sec=ts_morning)
    assert admit is True
    policy.mark_admitted(75.0)

    # Slot 2 in morning: allowed
    admit, _ = policy.admit(score=80.0, now_sec=ts_morning)
    assert admit is True
    policy.mark_admitted(80.0)

    # Slot 3 in morning: BLOCKED because 3rd slot is reserved for afternoon!
    admit, reason = policy.admit(score=90.0, now_sec=ts_morning)
    assert admit is False
    assert reason == "slot_reserved_afternoon"

    # Afternoon: 14:50 IST = 890 minutes (> 885)
    dt_afternoon = datetime(2026, 9, 14, 14, 50, 0)
    ts_afternoon = dt_afternoon.timestamp()

    # Slot 3 in afternoon: now allowed!
    admit, reason = policy.admit(score=90.0, now_sec=ts_afternoon)
    assert admit is True
    assert reason == "slot_admit"


def test_quality_slot_policy_dynamic_bar():
    policy = QualitySlotPolicy(max_trades_per_day=3, dynamic_bar=True, reserve_release_min=0)
    ts = datetime(2026, 9, 14, 14, 0, 0).timestamp()

    # Trade 1 with score 70.0
    admit, _ = policy.admit(score=70.0, now_sec=ts)
    assert admit is True
    policy.mark_admitted(70.0)

    # Trade 2 with score 65.0 <= 70.0 -> BLOCKED by dynamic bar!
    admit, reason = policy.admit(score=65.0, now_sec=ts)
    assert admit is False
    assert reason == "slot_dynamic_bar"

    # Trade 2 with score 78.0 > 70.0 -> ALLOWED!
    admit, reason = policy.admit(score=78.0, now_sec=ts)
    assert admit is True
    policy.mark_admitted(78.0)


def test_post_trigger_stability_tracker_lifecycle():
    tracker = PostTriggerStabilityTracker(window_sec=10.0, fade_frac=0.25)
    t0 = 1000.0

    tracker.arm(
        sym="NIFTY24000CE",
        direction="CALL",
        t_sec=t0,
        mid_T=100.0,
        meta={"opt_ret_30s": 0.02, "ml_score": 0.38},
    )
    assert tracker.pending_count() == 1

    # At T + 5s: not due yet
    assert len(tracker.due(now_sec=t0 + 5.0)) == 0

    # At T + 10s: due!
    due_items = tracker.due(now_sec=t0 + 10.0)
    assert len(due_items) == 1

    # Case A: Price holds
    item = due_items[0]
    verdict = tracker.resolve(item, mid_now=101.5)
    assert verdict["hold"] is True
    assert verdict["reason"] == "hold"
    assert tracker.pending_count() == 0

    # Case B: Arm another trade that fades
    tracker.arm(
        sym="NIFTY24000PE",
        direction="PUT",
        t_sec=t0 + 20.0,
        mid_T=50.0,
        meta={"opt_ret_30s": 0.04},
    )
    due_fade = tracker.due(now_sec=t0 + 30.0)[0]
    verdict_fade = tracker.resolve(due_fade, mid_now=48.0)
    assert verdict_fade["hold"] is False
    assert verdict_fade["reason"] == "fade"


def test_execution_manager_get_slot_state():
    em = AegisExecutionManager.__new__(AegisExecutionManager)
    em.trades_today = 1
    em.daily_pnl = 450.0
    em._consec_losses = 0
    em.consec_loss_stop = 2
    em.daily_loss_cap_rs = 2000.0
    em.last_trade_loss = False

    slot_state = em.get_slot_state()
    assert slot_state["trades_today"] == 1
    assert slot_state["max_trades_per_day"] == 3
    assert slot_state["daily_pnl"] == 450.0
    assert slot_state["consec_losses"] == 0
    assert slot_state["consec_loss_stop"] == 2
    assert slot_state["daily_loss_cap_rs"] == 2000.0
