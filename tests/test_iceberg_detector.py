"""Unit tests for AEGIS Synthetic Iceberg & Passive Absorption Shadow Detector.

Tests:
1. Exact Simulation: 20 visible contracts refilling 15 times against 300 traded contracts
   at stationary price, followed by a 1-tick breakout, verifying exact state transitions.
2. Bid Iceberg Invisible Wall: Support absorption and breakdown.
3. Sub-threshold suppression: Normal flow with refills < 3 or ratio < 5.0 does not flag.
4. Zero Heap Churn & Contiguous C-Arrays validation.
5. 3-Second Decay Window: BROKEN wall decays to STATE_NONE after 3 seconds.
6. Strike Alignment and ATM Rolling buffer remapping.
7. TickCompiler O(1) Routing Integration.
8. AegisPipelineOrchestrator Decision Lifecycle Shadow Telemetry.
"""

import time
import pytest
import numpy as np

from iceberg_detector import (
    IcebergShadowDetector,
    STATE_NONE,
    STATE_BID_ICEBERG_HOLD,
    STATE_ASK_ICEBERG_HOLD,
    STATE_BID_ICEBERG_BROKEN,
    STATE_ASK_ICEBERG_BROKEN,
    VERDICT_VETO_ASK_ICEBERG,
    VERDICT_SUPPORT_BID_ICEBERG,
    VERDICT_AMPLIFY_ICEBERG_BREAK,
    VERDICT_NEUTRAL_PASS,
)
from feed import TickCompiler
from pipeline import AegisPipelineOrchestrator


def test_ask_iceberg_simulation_20_visible_15_refills_300_vol_breakout():
    """Exact test specified in prompt: 20 visible contracts refilling 15 times

    against 300 traded contracts at stationary price, followed by 1-tick breakout.
    """
    detector = IcebergShadowDetector(num_strikes=51)
    idx = 25  # ATM slot
    t0 = 1000.0

    # Initial tick establishing visible book at ask 100.0 (qty 20), bid 99.5 (qty 20)
    detector.on_tick_o1(
        idx=idx,
        ltp=99.5,
        cum_vol=1000.0,
        bid_px=99.5,
        bid_qty=20.0,
        ask_px=100.0,
        ask_qty=20.0,
        now_sec=t0,
    )
    assert detector.iceberg_state[idx] == STATE_NONE
    assert detector.ask_absorbed_vol[idx] == 0.0

    # Simulate 15 consecutive refills of 20 contracts each against 300 traded volume
    curr_vol = 1000.0
    for i in range(1, 16):
        curr_vol += 20.0
        t_tick = t0 + i * 0.1
        # Aggressive buyers hit Ask at 100.0, visible ask refills back to 20 contracts
        detector.on_tick_o1(
            idx=idx,
            ltp=100.0,
            cum_vol=curr_vol,
            bid_px=99.5,
            bid_qty=20.0,
            ask_px=100.0,
            ask_qty=20.0,
            min_refills=3,
            ratio_threshold=5.0,
            now_sec=t_tick,
        )

        if i < 5:
            # Below 5.0 ratio threshold (100 / 20 = 5.0)
            assert detector.iceberg_state[idx] == STATE_NONE
        else:
            # From i=5 (100 absorbed / 20 anchor = 5.0) onwards, ICEBERG_WALL_HOLD is active!
            assert detector.iceberg_state[idx] == STATE_ASK_ICEBERG_HOLD

    # Verify state after all 15 refills and 300 traded contracts
    assert detector.ask_refill_cnt[idx] == 15
    assert detector.ask_absorbed_vol[idx] == 300.0
    assert detector.iceberg_ratio[idx] == pytest.approx(15.0)  # 300.0 / 20.0
    assert detector.iceberg_state[idx] == STATE_ASK_ICEBERG_HOLD
    assert detector.absorbed_vol[idx] == 300.0

    # Verdict check
    verdict_info = detector.evaluate_shadow_verdict(idx, now_sec=t0 + 1.6)
    assert verdict_info["shadow_verdict"] == VERDICT_VETO_ASK_ICEBERG
    assert verdict_info["iceberg_state"] == STATE_ASK_ICEBERG_HOLD
    assert verdict_info["iceberg_ratio"] == 15.0
    assert verdict_info["absorbed_vol"] == 300.0
    assert verdict_info["refill_cnt"] == 15

    # 1-Tick Breakout: Price ticks UP through the exhausted wall (from 100.0 to 100.05)
    t_break = t0 + 2.0
    detector.on_tick_o1(
        idx=idx,
        ltp=100.05,
        cum_vol=curr_vol + 5.0,
        bid_px=100.0,
        bid_qty=20.0,
        ask_px=100.05,
        ask_qty=20.0,
        now_sec=t_break,
    )

    # State transitions to STATE_ASK_ICEBERG_BROKEN (+2)
    assert detector.iceberg_state[idx] == STATE_ASK_ICEBERG_BROKEN
    assert detector.absorbed_vol[idx] == 300.0  # preserved for shadow telemetry

    verdict_broken = detector.evaluate_shadow_verdict(idx, now_sec=t_break + 0.5)
    assert verdict_broken["shadow_verdict"] == VERDICT_AMPLIFY_ICEBERG_BREAK
    assert verdict_broken["iceberg_state"] == STATE_ASK_ICEBERG_BROKEN
    assert verdict_broken["absorbed_vol"] == 300.0

    # 3-Second Decay Window: After 3.1 seconds, state decays to STATE_NONE
    t_decayed = t_break + 3.1
    verdict_decayed = detector.evaluate_shadow_verdict(idx, now_sec=t_decayed)
    assert verdict_decayed["shadow_verdict"] == VERDICT_NEUTRAL_PASS
    assert verdict_decayed["iceberg_state"] == STATE_NONE
    assert detector.iceberg_state[idx] == STATE_NONE


def test_bid_iceberg_support_and_breakdown():
    """Verify invisible support buyer wall at bid and subsequent breakdown."""
    detector = IcebergShadowDetector(num_strikes=51)
    idx = 10
    t0 = 500.0

    # Initial book at bid 150.0 (qty 50), ask 150.5 (qty 50)
    detector.on_tick_o1(
        idx=idx,
        ltp=150.5,
        cum_vol=2000.0,
        bid_px=150.0,
        bid_qty=50.0,
        ask_px=150.5,
        ask_qty=50.0,
        now_sec=t0,
    )

    # 10 refills of 50 contracts traded at bid 150.0 = 500 contracts absorbed
    vol = 2000.0
    for i in range(1, 11):
        vol += 50.0
        detector.on_tick_o1(
            idx=idx,
            ltp=150.0,
            cum_vol=vol,
            bid_px=150.0,
            bid_qty=50.0,
            ask_px=150.5,
            ask_qty=50.0,
            min_refills=3,
            ratio_threshold=5.0,
            now_sec=t0 + i * 0.1,
        )

    # Ratio = 500 / 50 = 10.0x, refills = 10 -> STATE_BID_ICEBERG_HOLD
    assert detector.iceberg_state[idx] == STATE_BID_ICEBERG_HOLD
    assert detector.iceberg_ratio[idx] == pytest.approx(10.0)
    assert detector.absorbed_vol[idx] == 500.0

    v = detector.evaluate_shadow_verdict(idx, now_sec=t0 + 1.2)
    assert v["shadow_verdict"] == VERDICT_SUPPORT_BID_ICEBERG

    # Breakdown: Price ticks DOWN through support (bid moves from 150.0 to 149.5)
    detector.on_tick_o1(
        idx=idx,
        ltp=149.5,
        cum_vol=vol + 10.0,
        bid_px=149.5,
        bid_qty=50.0,
        ask_px=150.0,
        ask_qty=50.0,
        now_sec=t0 + 1.5,
    )
    assert detector.iceberg_state[idx] == STATE_BID_ICEBERG_BROKEN


def test_sub_threshold_normal_flow_no_iceberg():
    """Verify normal trades with low refills or low absorption ratio do not trigger false iceberg."""
    detector = IcebergShadowDetector(num_strikes=51)
    idx = 5
    t0 = 100.0

    # Initial tick
    detector.on_tick_o1(idx=5, ltp=50.0, cum_vol=100.0, bid_px=49.5, bid_qty=100.0, ask_px=50.0, ask_qty=100.0, now_sec=t0)

    # Only 2 refills (less than min_refills=3)
    detector.on_tick_o1(idx=5, ltp=50.0, cum_vol=150.0, bid_px=49.5, bid_qty=100.0, ask_px=50.0, ask_qty=100.0, now_sec=t0 + 0.1)
    detector.on_tick_o1(idx=5, ltp=50.0, cum_vol=200.0, bid_px=49.5, bid_qty=100.0, ask_px=50.0, ask_qty=100.0, now_sec=t0 + 0.2)

    assert detector.ask_refill_cnt[idx] == 2
    assert detector.iceberg_state[idx] == STATE_NONE
    assert detector.evaluate_shadow_verdict(idx)["shadow_verdict"] == VERDICT_NEUTRAL_PASS


def test_preallocated_contiguous_numpy_buffers():
    """Verify strict zero-heap memory constraint: contiguous NumPy arrays with exact dtypes."""
    detector = IcebergShadowDetector(num_strikes=51)

    assert isinstance(detector.last_bid_px, np.ndarray) and detector.last_bid_px.dtype == np.float64
    assert isinstance(detector.last_ask_px, np.ndarray) and detector.last_ask_px.dtype == np.float64
    assert isinstance(detector.last_bid_qty, np.ndarray) and detector.last_bid_qty.dtype == np.float64
    assert isinstance(detector.last_ask_qty, np.ndarray) and detector.last_ask_qty.dtype == np.float64
    assert isinstance(detector.last_cum_vol, np.ndarray) and detector.last_cum_vol.dtype == np.float64
    assert isinstance(detector.bid_absorbed_vol, np.ndarray) and detector.bid_absorbed_vol.dtype == np.float64
    assert isinstance(detector.ask_absorbed_vol, np.ndarray) and detector.ask_absorbed_vol.dtype == np.float64
    assert isinstance(detector.bid_refill_cnt, np.ndarray) and detector.bid_refill_cnt.dtype == np.int32
    assert isinstance(detector.ask_refill_cnt, np.ndarray) and detector.ask_refill_cnt.dtype == np.int32
    assert isinstance(detector.iceberg_state, np.ndarray) and detector.iceberg_state.dtype == np.int8
    assert isinstance(detector.iceberg_ratio, np.ndarray) and detector.iceberg_ratio.dtype == np.float64
    assert isinstance(detector.absorbed_vol, np.ndarray) and detector.absorbed_vol.dtype == np.float64
    assert isinstance(detector.broken_ts, np.ndarray) and detector.broken_ts.dtype == np.float64

    # Contiguity check
    assert detector.iceberg_state.flags.c_contiguous
    assert detector.iceberg_ratio.flags.c_contiguous
    assert detector.absorbed_vol.flags.c_contiguous


def test_align_strikes_and_dynamic_roll_remapping():
    """Verify strike alignment and preservation of state across ATM rolls."""
    detector = IcebergShadowDetector(num_strikes=51, strike_step=50.0)
    detector.align_strikes(24500.0)

    assert len(detector.strikes) == 51
    assert detector.strikes[25] == 24500.0
    assert detector.strike_to_idx[24500.0] == 25
    assert detector.strike_to_idx[24550.0] == 26

    # Activate an iceberg at strike 24550.0 (index 26)
    idx_24550 = detector.strike_to_idx[24550.0]
    detector.iceberg_state[idx_24550] = STATE_ASK_ICEBERG_HOLD
    detector.iceberg_ratio[idx_24550] = 7.5
    detector.absorbed_vol[idx_24550] = 150.0

    # ATM rolls up by 50 pts to 24550.0
    detector.align_strikes(24550.0)
    assert detector.strikes[25] == 24550.0
    new_idx_24550 = detector.strike_to_idx[24550.0]
    assert new_idx_24550 == 25

    # State at 24550.0 should be preserved at its new index 25
    assert detector.iceberg_state[new_idx_24550] == STATE_ASK_ICEBERG_HOLD
    assert detector.iceberg_ratio[new_idx_24550] == pytest.approx(7.5)
    assert detector.absorbed_vol[new_idx_24550] == 150.0


def test_tick_compiler_iceberg_routing():
    """Verify TickCompiler on_message routes ticks directly into IcebergShadowDetector."""
    class MockBroker:
        pass

    compiler = TickCompiler(MockBroker())
    token_map = {
        "1001": {"side": 0, "strike": 24500.0},  # CE
        "1002": {"side": 1, "strike": 24500.0},  # PE
    }
    compiler.set_token_map(token_map)
    assert "1001" in compiler._token_routing
    det, idx = compiler._token_routing["1001"]
    assert det is compiler.iceberg_detector_ce
    assert det.strikes[idx] == 24500.0

    # Feed tick into compiler
    msg = {
        "token": "1001",
        "lp": "120.0",
        "v": "500",
        "bp1": "119.5",
        "bq1": "20",
        "sp1": "120.0",
        "sq1": "20",
    }
    compiler.on_message(msg)
    assert det.last_cum_vol[idx] == 500.0
    assert det.last_ask_px[idx] == 120.0


def test_pipeline_orchestrator_shadow_telemetry_integration():
    """Verify AegisPipelineOrchestrator attaches iceberg telemetry tags without blocking trades."""
    token_to_symbol = {
        "26000": "NIFTY-INDEX",
        "1001": "NIFTY24OCT24500CE",
        "1002": "NIFTY24OCT24500PE",
    }
    token_map = {
        "26000": {"side": 0, "strike": 0.0, "is_index": True},
        "1001": {"side": 0, "strike": 24500.0},
        "1002": {"side": 1, "strike": 24500.0},
    }

    orchestrator = AegisPipelineOrchestrator(
        token_to_symbol=token_to_symbol,
        token_map=token_map,
        strike_step=50.0,
    )

    assert orchestrator.iceberg_detector_ce is not None
    assert orchestrator.iceberg_detector_pe is not None

    class MockTick:
        def __init__(self, tok, lp, vol=0.0, bp1=0.0, sp1=0.0, bq1=20, sq1=20):
            self.tok = tok
            self.lp = lp
            self.vol = vol
            self.bp1 = bp1
            self.sp1 = sp1
            self.bq1 = bq1
            self.sq1 = sq1
            self.bp = [bp1] * 5
            self.bq = [bq1] * 5
            self.sp = [sp1] * 5
            self.sq = [sq1] * 5
            self.ts = 1700000000.0

    # Underlying tick aligns ATM
    orchestrator.on_tick_parsed(MockTick("26000", lp=24500.0))
    assert orchestrator.current_atm_strike == 24500.0

    # Ingest baseline option tick on 1001 CE
    orchestrator.on_tick_parsed(MockTick("1001", lp=120.0, vol=1000.0, bp1=119.5, sp1=120.0, bq1=20, sq1=20))

    # Simulate iceberg on 1001 CE: 15 refills of 20 contracts against 300 volume
    v = 1000.0
    for i in range(15):
        v += 20.0
        orchestrator.on_tick_parsed(MockTick("1001", lp=120.0, vol=v, bp1=119.5, sp1=120.0, bq1=20, sq1=20))

    idx_24500 = orchestrator.iceberg_detector_ce.strike_to_idx[24500.0]
    assert orchestrator.iceberg_detector_ce.iceberg_state[idx_24500] == STATE_ASK_ICEBERG_HOLD

    verdict_ce = orchestrator.iceberg_detector_ce.evaluate_shadow_verdict(idx_24500)
    assert verdict_ce["shadow_verdict"] == VERDICT_VETO_ASK_ICEBERG
    assert verdict_ce["iceberg_ratio"] == 15.0
    assert verdict_ce["absorbed_vol"] == 300.0


def test_edge_case_out_of_bounds_and_negative_volume():
    """Verify out-of-bounds indices and negative dv (e.g. feed reset) never crash."""
    detector = IcebergShadowDetector(num_strikes=51)

    # Out of bounds idx
    detector.on_tick_o1(idx=-1, ltp=100.0, cum_vol=10.0, bid_px=99.0, bid_qty=10.0, ask_px=100.0, ask_qty=10.0)
    detector.on_tick_o1(idx=51, ltp=100.0, cum_vol=10.0, bid_px=99.0, bid_qty=10.0, ask_px=100.0, ask_qty=10.0)
    assert detector.evaluate_shadow_verdict(-1)["shadow_verdict"] == VERDICT_NEUTRAL_PASS
    assert detector.evaluate_shadow_verdict(51)["shadow_verdict"] == VERDICT_NEUTRAL_PASS

    # Negative volume delta
    detector.on_tick_o1(idx=0, ltp=100.0, cum_vol=500.0, bid_px=99.0, bid_qty=10.0, ask_px=100.0, ask_qty=10.0)
    detector.on_tick_o1(idx=0, ltp=100.0, cum_vol=400.0, bid_px=99.0, bid_qty=10.0, ask_px=100.0, ask_qty=10.0)
    assert detector.iceberg_state[0] == STATE_NONE


def test_edge_case_quote_driven_breakout_zero_volume():
    """Verify quote-driven price jump triggers breakout even when dv=0 (quote packet)."""
    detector = IcebergShadowDetector(num_strikes=51)
    idx = 15

    # Establish Ask Iceberg
    detector.on_tick_o1(idx=idx, ltp=100.0, cum_vol=1000.0, bid_px=99.5, bid_qty=20.0, ask_px=100.0, ask_qty=20.0)
    vol = 1000.0
    for _ in range(10):
        vol += 20.0
        detector.on_tick_o1(idx=idx, ltp=100.0, cum_vol=vol, bid_px=99.5, bid_qty=20.0, ask_px=100.0, ask_qty=20.0)
    assert detector.iceberg_state[idx] == STATE_ASK_ICEBERG_HOLD

    # Quote packet arrives with higher ask 100.5 and dv=0 (no trades on this depth packet)
    detector.on_tick_o1(idx=idx, ltp=100.0, cum_vol=vol, bid_px=100.0, bid_qty=20.0, ask_px=100.5, ask_qty=20.0, now_sec=200.0)
    assert detector.iceberg_state[idx] == STATE_ASK_ICEBERG_BROKEN
    assert detector.evaluate_shadow_verdict(idx, now_sec=201.0)["shadow_verdict"] == VERDICT_AMPLIFY_ICEBERG_BREAK


def test_edge_case_decay_boundary_exact_timing():
    """Verify exact 3.0s boundary: stays broken at 2.9s and expires at 3.05s."""
    detector = IcebergShadowDetector(num_strikes=51)
    idx = 20

    detector.last_ask_px[idx] = 104.0
    detector.last_bid_px[idx] = 103.5
    detector.last_cum_vol[idx] = 400.0
    detector.iceberg_state[idx] = STATE_ASK_ICEBERG_HOLD
    detector.absorbed_vol[idx] = 200.0

    # Breakout at t=100.0 (ask ticks up from 104.0 to 105.0)
    detector.on_tick_o1(idx=idx, ltp=105.0, cum_vol=500.0, bid_px=104.5, bid_qty=10.0, ask_px=105.0, ask_qty=10.0, now_sec=100.0)
    assert detector.iceberg_state[idx] == STATE_ASK_ICEBERG_BROKEN


    # At t=102.9 (2.9s later): Still active!
    v_active = detector.evaluate_shadow_verdict(idx, now_sec=102.9)
    assert v_active["shadow_verdict"] == VERDICT_AMPLIFY_ICEBERG_BREAK
    assert detector.iceberg_state[idx] == STATE_ASK_ICEBERG_BROKEN

    # At t=103.05 (3.05s later): Decayed!
    v_decayed = detector.evaluate_shadow_verdict(idx, now_sec=103.05)
    assert v_decayed["shadow_verdict"] == VERDICT_NEUTRAL_PASS
    assert detector.iceberg_state[idx] == STATE_NONE


def test_edge_case_ce_and_pe_dual_isolation():
    """Verify CALL (CE) and PUT (PE) detectors on the same strike step do not interfere."""
    compiler = TickCompiler(None)
    token_map = {
        "1001": {"side": 0, "strike": 24500.0},  # CE
        "1002": {"side": 1, "strike": 24500.0},  # PE
    }
    compiler.set_token_map(token_map)

    det_ce, idx_ce = compiler._token_routing["1001"]
    det_pe, idx_pe = compiler._token_routing["1002"]

    # Both share strike 24500 at index 25
    assert idx_ce == idx_pe

    # Create Ask Iceberg on CE only
    det_ce.on_tick_o1(idx=idx_ce, ltp=100.0, cum_vol=1000.0, bid_px=99.5, bid_qty=20.0, ask_px=100.0, ask_qty=20.0)
    v = 1000.0
    for _ in range(10):
        v += 20.0
        det_ce.on_tick_o1(idx=idx_ce, ltp=100.0, cum_vol=v, bid_px=99.5, bid_qty=20.0, ask_px=100.0, ask_qty=20.0)

    # CE has Ask Iceberg
    assert det_ce.iceberg_state[idx_ce] == STATE_ASK_ICEBERG_HOLD
    # PE has NO Iceberg (complete isolation)
    assert det_pe.iceberg_state[idx_pe] == STATE_NONE
    assert det_ce.evaluate_shadow_verdict(idx_ce)["shadow_verdict"] == VERDICT_VETO_ASK_ICEBERG
    assert det_pe.evaluate_shadow_verdict(idx_pe)["shadow_verdict"] == VERDICT_NEUTRAL_PASS


def test_reset_session_clean_slate():
    """Verify reset_session wipes all states and accumulators back to zero/ones."""
    detector = IcebergShadowDetector(num_strikes=51)
    detector.iceberg_state[5] = STATE_ASK_ICEBERG_HOLD
    detector.iceberg_ratio[5] = 10.0
    detector.ask_refill_cnt[5] = 8
    detector.absorbed_vol[5] = 400.0

    detector.reset_session()
    assert np.all(detector.iceberg_state == 0)
    assert np.all(detector.iceberg_ratio == 0.0)
    assert np.all(detector.ask_refill_cnt == 0)
    assert np.all(detector.absorbed_vol == 0.0)
    assert np.all(detector.bid_visible_anchor == 1.0)
    assert np.all(detector.ask_visible_anchor == 1.0)

