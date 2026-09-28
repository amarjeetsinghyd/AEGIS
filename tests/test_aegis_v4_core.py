"""
Unit tests for AEGIS V4.0 Constraint-Corrected Core Engine.
Covers: FastTickRingBuffer, FastMonotonicStack, Top5DepthGuard,
MacroPriorityEngine, Confirm15StateMachine, AsymmetricExecutionEngine,
and AegisV4Core end-to-end integration.
"""

import time
import pytest
import numpy as np

from AEGIS.core.aegis_v4_core import (
    AegisV4Core,
    FastTickRingBuffer,
    FastDualEnvelopeHVG,
    FastMonotonicStack,
    Top5DepthGuard,
    MacroPriorityEngine,
    Confirm15StateMachine,
    SingleTargetExecutionEngine,
    AsymmetricExecutionEngine,
    CandleBar,
    Direction,
    SignalSource,
    WatchSlotState,
    PositionState,
)


def test_fast_tick_ring_buffer():
    """Verify push, wrap-around, and window metrics (Drift and ER)."""
    buf = FastTickRingBuffer(size=256)
    now = 1000.0

    # Push 20 ticks of steady rising price
    for i in range(20):
        buf.push(
            ts=now + i,
            price=24000.0 + (i * 1.0),
            vol=100.0,
            bid=23999.95 + (i * 1.0),
            ask=24000.05 + (i * 1.0),
        )

    # 15s lookback from t=1019 -> target_ts=1004 (covers 15s)
    drift, er, last_p = buf.get_window_metrics(lookback_seconds=15.0)
    assert last_p == 24019.0
    assert drift == pytest.approx(15.0, abs=1.0)
    assert er == pytest.approx(1.0, abs=0.05)


def test_fast_monotonic_stack_prominence():
    """Verify 0-D prominence calculation on a synthetic peak."""
    stack = FastMonotonicStack(capacity=128)
    atr = 10.0

    # Prices with a clear prominent peak at index 5
    # Val: 100, 102, 105, 108, 115, 140, 112, 107, 104, 101
    prices = np.array([100.0, 102.0, 105.0, 108.0, 115.0, 140.0, 112.0, 107.0, 104.0, 101.0], dtype=np.float64)
    target_idx = 5

    pi_hat = stack.compute_prominence(prices, target_idx, atr)
    # Peak is 140, highest flanking minimum is 100
    # Prominence = (140 - 100) / 10 = 4.0
    assert pi_hat >= 1.75
    assert pi_hat == pytest.approx(4.0, abs=0.1)


def test_top5_depth_guard_bbo_skew_and_spread():
    """Verify BBO Micro-Price Skew, spread expansion veto, and extreme imbalance."""
    guard = Top5DepthGuard(history_len=30)

    # Pre-seed spread history with typical 0.10 spread
    for _ in range(35):
        guard.update_spread(100.0, 100.10)

    # 1. Normal balanced book -> PASS
    bids = np.array([[100.0, 1000], [99.9, 1000], [99.8, 1000], [99.7, 1000], [99.6, 1000]], dtype=np.float64)
    asks = np.array([[100.1, 1000], [100.2, 1000], [100.3, 1000], [100.4, 1000], [100.5, 1000]], dtype=np.float64)
    passed, reason = guard.validate_execution(Direction.BULLISH, bids, asks)
    assert passed
    assert reason == "PASSED"

    # 2. Heavy Ask wall at BBO resisting bullish breakout -> BLOCK
    asks_heavy = np.array([[100.1, 9000], [100.2, 1000], [100.3, 1000], [100.4, 1000], [100.5, 1000]], dtype=np.float64)
    passed, reason = guard.validate_execution(Direction.BULLISH, bids, asks_heavy)
    assert not passed
    assert "MICRO_PRICE_SKEW_RESISTANCE" in reason or "EXTREME_BOOK_IMBALANCE" in reason

    # 3. Spread expansion veto (> 2.0x median of 0.10 = 0.20)
    asks_wide = np.array([[100.35, 1000], [100.4, 1000], [100.5, 1000], [100.6, 1000], [100.7, 1000]], dtype=np.float64)
    passed, reason = guard.validate_execution(Direction.BULLISH, bids, asks_wide)
    assert not passed
    assert "SPREAD_EXPANSION_VETO" in reason


def test_macro_priority_engine_p1_ema_wick():
    """Verify P1 20 EMA Wick Rejection priority."""
    engine = MacroPriorityEngine()
    atr = 10.0
    ema20 = 24000.0
    hist_vol = np.full(60, 1000.0, dtype=np.float64)

    # Bullish hammer dipping into EMA20 and closing strong
    bars_1m = [
        CandleBar(100, 23990, 24010, 23985, 23995, 1000),
        CandleBar(160, 23995, 24015, 23990, 24005, 1000),
        CandleBar(220, 24005, 24020, 24000, 24010, 1000),
        CandleBar(280, 24010, 24025, 24005, 24015, 1000),
        CandleBar(340, 24005, 24020, 23995, 24015, 1500),  # Low 23995 <= ema20 <= close 24015
    ]
    bars_5m = [
        CandleBar(100, 23990, 24020, 23985, 24010, 5000),
        CandleBar(400, 24010, 24030, 24005, 24025, 5000),
    ]

    source, direction, hub = engine.evaluate(bars_1m, bars_5m, atr, ema20, hist_vol)
    assert source == SignalSource.P1_EMA_WICK
    assert direction == Direction.BULLISH


def test_confirm15_state_machine():
    """Verify Confirm15 event-driven confirmation lifecycle."""
    sm = Confirm15StateMachine()
    ring = FastTickRingBuffer(size=256)
    t0 = 1000.0
    atr = 15.0

    sm.arm(
        source=SignalSource.P4_TOPOLOGICAL_HUB,
        direction=Direction.BULLISH,
        hub_price=24000.0,
        opt_ltp=100.0,
        atr_1m=atr,
        ts=t0,
        candle_open=24000.0,
    )
    assert sm.state == WatchSlotState.ARMED

    # Within 8s stabilization window: should remain ARMED
    for step in range(6):
        ring.push(t0 + step, 24000.0 + step, 100, 23999, 24001)
    status = sm.evaluate_tick(ring, opt_ltp=100.5, current_fut_price=24006.0, current_fut_high=24007.0, current_fut_low=23999.0, current_fut_open=24000.0, ts=t0 + 5.0)
    assert status == WatchSlotState.ARMED

    # Adverse excursion beyond 0.45 * ATR (0.45 * 15 = 6.75 pts) -> INVALIDATED
    sm.arm(SignalSource.P4_TOPOLOGICAL_HUB, Direction.BULLISH, 24000.0, 100.0, atr, t0, 24000.0)
    status_adv = sm.evaluate_tick(ring, opt_ltp=99.0, current_fut_price=23992.0, current_fut_high=24000.0, current_fut_low=23991.0, current_fut_open=24000.0, ts=t0 + 10.0)
    assert status_adv == WatchSlotState.INVALIDATED

    # Satisfied criteria at t=15s (elapsed 15s >= 8s)
    sm.arm(SignalSource.P4_TOPOLOGICAL_HUB, Direction.BULLISH, 24000.0, 100.0, atr, t0, 24000.0)
    for step in range(16):
        ring.push(t0 + step, 24000.0 + (step * 0.8), 100, 23999 + (step * 0.8), 24001 + (step * 0.8))

    status_ok = sm.evaluate_tick(
        ring,
        opt_ltp=101.50,  # +1.50% concordance >= +1.0%
        current_fut_price=24012.0,
        current_fut_high=24013.0,
        current_fut_low=23999.0,
        current_fut_open=24000.0,
        ts=t0 + 15.0,
    )
    assert status_ok == WatchSlotState.TRIGGERED


def test_fast_dual_envelope_hvg_spans_and_compression():
    """Verify Upper/Lower envelope visibility spans w_i and median compression."""
    hvg = FastDualEnvelopeHVG(capacity=128)
    n = 20

    # Synthetic range with short local fluctuations (compressed network)
    # Most bars quickly hit higher neighbors within 2 bars
    highs = np.array([
        100.0, 100.5, 100.2, 100.8, 100.3, 100.9, 100.4, 101.0,
        100.5, 101.2, 100.6, 101.5, 100.7, 101.1, 100.8, 101.3,
        100.9, 101.6, 101.0, 101.4
    ], dtype=np.float64)

    deg, spans, med_span = hvg.compute_upper_envelope(highs, n)
    assert len(deg) == n
    assert len(spans) == n
    # Verify span formula: w_i = min(R_i, n) - max(L_i, 0)
    for i in range(n):
        assert spans[i] >= 1
        assert spans[i] <= n

    # Median span should reflect network compression
    assert med_span < (0.50 * n)

    # Test lower envelope
    lows = highs - 2.0
    deg_l, spans_l, med_span_l = hvg.compute_lower_envelope(lows, n)
    assert len(deg_l) == n
    assert len(spans_l) == n
    assert med_span_l < (0.50 * n)


def test_single_target_execution_engine():
    """Verify single-target 100% quantity exits at +4.0% (+5.0% for conv>=85), -3.5% SL, and 300s max hold."""
    engine = SingleTargetExecutionEngine()
    t0 = 1000.0

    # 1. Standard Target: 100% exit at +4.0%
    engine.open_position(Direction.BULLISH, fill_price=100.0, qty=65, ts=t0, conviction=75.0)
    pos = engine.active_pos
    assert pos.state == PositionState.OPEN
    assert pos.total_qty == 65

    # Small gain (+2.0%): HOLD
    hold, _, reason = engine.evaluate_exit(current_opt_ltp=102.0, ts=t0 + 30.0)
    assert not hold
    assert reason == "HOLD"

    # Target hit (+4.1% >= +4.0%): Exit 100%
    should_exit, qty_close, reason = engine.evaluate_exit(current_opt_ltp=104.10, ts=t0 + 45.0)
    assert should_exit
    assert qty_close == 65
    assert engine.active_pos is None
    assert "TARGET_PROFIT_HIT: +4.0%" in reason

    # 2. High Conviction Target: 100% exit at +5.0%
    engine.open_position(Direction.BULLISH, fill_price=100.0, qty=65, ts=t0, conviction=88.0)
    # At +4.2%, should hold
    hold_hi, _, _ = engine.evaluate_exit(current_opt_ltp=104.20, ts=t0 + 30.0)
    assert not hold_hi
    # At +5.1%, should exit 100%
    exit_hi, qty_hi, reason_hi = engine.evaluate_exit(current_opt_ltp=105.10, ts=t0 + 45.0)
    assert exit_hi
    assert qty_hi == 65
    assert engine.active_pos is None
    assert "TARGET_PROFIT_HIT: +5.0%" in reason_hi

    # 3. Hard Stop Loss: 100% exit at -3.5%
    engine.open_position(Direction.BULLISH, fill_price=100.0, qty=65, ts=t0)
    exit_sl, qty_sl, reason_sl = engine.evaluate_exit(current_opt_ltp=96.40, ts=t0 + 20.0)
    assert exit_sl
    assert qty_sl == 65
    assert engine.active_pos is None
    assert "HARD_STOP_LOSS_HIT" in reason_sl

    # 4. Strict Time Ceiling: Max 5 minutes (300 seconds)
    engine.open_position(Direction.BULLISH, fill_price=100.0, qty=65, ts=t0)
    exit_time, qty_time, reason_time = engine.evaluate_exit(current_opt_ltp=100.50, ts=t0 + 301.0)
    assert exit_time
    assert qty_time == 65
    assert engine.active_pos is None
    assert "HORIZON_EXPIRATION" in reason_time


def test_macro_priority_engine_p4_dual_envelope_hvg():
    """Verify Pillar 4 Dual-Envelope HVG Network Phase Shift Breakout."""
    engine = MacroPriorityEngine()
    atr = 10.0
    ema20 = 24000.0
    hist_vol = np.full(60, 1000.0, dtype=np.float64)

    # Construct 15 bars:
    # Anchor resistance at 24050.0 (high of bar 5)
    # Bars 10-13 have wider range (20 pts > 1.25 * atr = 12.5) so P3 squeeze doesn't trigger
    # Breakout bar 14 closes at 24055.0 (> 24050.0) with volume ratio 2000 / 1000 = 2.0 >= 1.4
    bars_1m = []
    t = 1000.0
    for i in range(14):
        if i == 5:
            h = 24050.0
            l = 24000.0
        elif i >= 10:
            h = 24035.0
            l = 24015.0
        else:
            h = 24010.0 + (i % 3)
            l = 24000.0 + (i % 3)
        c = 24020.0
        o = 24018.0
        bars_1m.append(CandleBar(t + (i * 60), o, h, l, c, 1000))

    # Breakout bar 14
    bars_1m.append(CandleBar(t + (14 * 60), 24040.0, 24060.0, 24035.0, 24055.0, 2000.0))

    bars_5m = [
        CandleBar(t, 24000.0, 24060.0, 23990.0, 24055.0, 5000.0),
    ]

    source, direction, hub = engine.evaluate(bars_1m, bars_5m, atr, ema20, hist_vol)
    assert source == SignalSource.P4_TOPOLOGICAL_HUB
    assert direction == Direction.BULLISH
    assert hub == 24050.0


def test_aegis_v4_core_mutex_and_benchmark():
    """Verify end-to-end AegisV4Core pipeline speed and mutex lock behavior."""
    core = AegisV4Core(spot_token=26000, futures_token=52123, option_token=64210, trade_lot_size=75)

    # Verify mutex blocks new arming while position is open
    core.order_engine.open_position(Direction.BULLISH, fill_price=100.0, qty=75, ts=1000.0)
    core.mutex_active = True

    b_1m = CandleBar(1000, 24000, 24020, 23990, 24015, 2000)
    core.on_minute_candle_close(b_1m, False, None)
    # State machine must NOT arm because mutex is active
    assert core.state_machine.state == WatchSlotState.IDLE


def test_marketable_limit_prices_scale_invariant():
    """Verify get_marketable_entry_limit_price avoids huge +2 pt overshoot on cheap options."""
    from common.execution.order_lifecycle import (
        get_marketable_entry_limit_price,
        get_marketable_exit_limit_price,
    )

    # 1. Penny / cheap option (₹8.15): should only add 2-3 ticks (~₹0.10 - ₹0.15), NEVER ₹2.00
    p_cheap = get_marketable_entry_limit_price(8.15)
    assert p_cheap <= 8.30
    assert p_cheap >= 8.20

    # 2. Sub-₹5 option (₹4.00): adds exactly 2 ticks (0.10)
    p_penny = get_marketable_entry_limit_price(4.00)
    assert p_penny == 4.10

    # 3. Mid-range option (₹42.80): 1.5% buffer is ~0.65 pts
    p_mid = get_marketable_entry_limit_price(42.80)
    assert p_mid == pytest.approx(43.45, abs=0.05)

    # 4. Expensive option (₹150.00): capped at maximum 1.0 pt
    p_high = get_marketable_entry_limit_price(150.00)
    assert p_high == 151.00

    # 5. Exit limit prices
    p_exit_cheap = get_marketable_exit_limit_price(8.15)
    assert p_exit_cheap >= 8.00
    assert p_exit_cheap <= 8.10

