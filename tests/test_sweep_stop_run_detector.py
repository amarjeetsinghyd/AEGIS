"""
Unit Tests for AEGIS SweepStopRunDetector & Pillar 2 Order-Flow Validation
==========================================================================
Verifies O(1) Zero-Allocation Stop Hunt vs Dry Sweep Detection in Shadow & Live modes.
"""

import pytest
import numpy as np
from core.sweep_stop_run_detector import (
    SweepStopRunDetector,
    SWEEP_NONE,
    SWEEP_CONFIRMED_STOP_HUNT,
    SWEEP_VETO_DRY_NO_STOPS,
    SWEEP_VETO_UNABSORBED_PANIC,
)
from core.price_action_engine import PriceActionEngine
from core.structural_registry import StructuralLevel


def test_detector_initialization_and_sync():
    """Verifies pre-allocated NumPy arrays and O(1) level synchronization."""
    detector = SweepStopRunDetector(num_levels=32)
    assert detector.sr_levels.shape == (32,)
    assert detector.in_breach.shape == (32,)
    assert detector.last_verdict.shape == (32,)

    # Create dummy S/R levels
    levels = [
        StructuralLevel(level_type="SUPPORT", price=24000.0, ts_created=100.0, prominence=2.0, bar_idx=1),
        StructuralLevel(level_type="RESISTANCE", price=24200.0, ts_created=100.0, prominence=2.0, bar_idx=1),
    ]
    detector.sync_levels(levels, pdh=24250.0, pdl=23950.0)

    assert detector._active_slot_count == 4
    assert 24000.0 in detector._price_to_slot
    assert 24200.0 in detector._price_to_slot
    assert 24250.0 in detector._price_to_slot
    assert 23950.0 in detector._price_to_slot


def test_support_stop_hunt_confirmed():
    """
    Simulates a Support breach where panic options orders fire heavily:
    - Long Call stop-losses dump into Call Bid (ce_sell)
    - Short Put stop-losses panic buy Call/Put Ask (pe_buy)
    Followed by positive absorption and reclaim back above support.
    """
    detector = SweepStopRunDetector(num_levels=32)
    levels = [StructuralLevel(level_type="SUPPORT", price=24000.0, ts_created=100.0, prominence=2.0, bar_idx=1)]
    detector.sync_levels(levels)

    # 1. Seed baseline volume (100 vol/sec over 10 seconds)
    for sec in range(100, 110):
        detector.on_option_tick(
            sym="NIFTY24000CE", strike=24000.0, side=0, buy_vol=50.0, sell_vol=50.0, ts=float(sec), atm_strike=24000.0
        )

    # 2. Spot is above support
    detector.on_spot_tick(spot=24010.0, ts=110.0)
    slot = detector._price_to_slot[24000.0]
    assert detector.in_breach[slot] == 0

    # 3. Spot breaches support at T=111.0s
    detector.on_spot_tick(spot=23992.0, ts=111.0)
    assert detector.in_breach[slot] == 1

    # 4. Massive panic flow during breach at T=111.5s
    # Downside breach panic: Call Sell (200) + Put Buy (300) = 500 panic vol in 0.5s -> Rate = 1000/s
    detector.on_option_tick(
        sym="NIFTY24000CE", strike=24000.0, side=0, buy_vol=10.0, sell_vol=200.0, ts=111.5, atm_strike=24000.0
    )
    detector.on_option_tick(
        sym="NIFTY24000PE", strike=24000.0, side=1, buy_vol=300.0, sell_vol=10.0, ts=111.5, atm_strike=24000.0
    )

    # 5. Spot reclaims back above support at T=112.0s with aggressive Call Buying (absorption)
    detector.on_option_tick(
        sym="NIFTY24000CE", strike=24000.0, side=0, buy_vol=150.0, sell_vol=10.0, ts=112.0, atm_strike=24000.0
    )
    detector.on_spot_tick(spot=24005.0, ts=112.0)
    assert detector.in_breach[slot] == 0

    # 6. Evaluate sweep verdict
    verdict, reason, metrics = detector.evaluate_sweep(swept_level=24000.0, direction="CALL", current_time=112.5)
    assert verdict == SWEEP_CONFIRMED_STOP_HUNT
    assert "confirmed_stop_hunt" in reason
    assert metrics["burst_ratio"] >= 2.2
    assert metrics["reclaim_flow"] > 0.0


def test_dry_sweep_detection():
    """
    Simulates a dry sweep fakeout:
    Spot breaches support at 24000.0, but zero/dry panic orders arrive.
    The ratio stays < 1.2, resulting in SWEEP_VETO_DRY_NO_STOPS.
    """
    detector = SweepStopRunDetector(num_levels=32)
    levels = [StructuralLevel(level_type="SUPPORT", price=24000.0, ts_created=100.0, prominence=2.0, bar_idx=1)]
    detector.sync_levels(levels)

    # 1. Seed baseline volume (100 vol/sec)
    for sec in range(100, 110):
        detector.on_option_tick(
            sym="NIFTY24000CE", strike=24000.0, side=0, buy_vol=50.0, sell_vol=50.0, ts=float(sec), atm_strike=24000.0
        )

    # 2. Spot breaches support at T=111.0s
    detector.on_spot_tick(spot=23995.0, ts=111.0)

    # 3. Only tiny trickle of volume (dry sweep: 10 vol total)
    detector.on_option_tick(
        sym="NIFTY24000CE", strike=24000.0, side=0, buy_vol=5.0, sell_vol=5.0, ts=112.0, atm_strike=24000.0
    )

    # 4. Spot reclaims at T=113.0s
    detector.on_spot_tick(spot=24002.0, ts=113.0)

    # 5. Evaluate sweep
    verdict, reason, metrics = detector.evaluate_sweep(swept_level=24000.0, direction="CALL", current_time=113.5)
    assert verdict == SWEEP_VETO_DRY_NO_STOPS
    assert "dry_sweep_no_stops" in reason
    assert metrics["burst_ratio"] < 1.2


def test_price_action_engine_integration_shadow_vs_live():
    """
    Verifies PriceActionEngine integrates the detector:
    - Shadow mode (default): logs [SWEEP SHADOW], passes through.
    - Live mode: actively vetoes dry sweeps.
    """
    engine = PriceActionEngine(strike_step=50.0)
    assert hasattr(engine, "sweep_detector")
    assert engine.enforce_live_sweep_gate is True  # Active in v1.0.3 production

    # Register an S/R level
    engine.seed_daily_levels(pdh=24300.0, pdl=24000.0)
    assert 24000.0 in engine.sweep_detector._price_to_slot

    # Simulate spot ticks
    engine.update_tick(price=24010.0, ts_sec=100.0)
    engine.update_tick(price=23992.0, ts_sec=101.0)
    engine.update_tick(price=24005.0, ts_sec=102.0)

    # Evaluate sweep on engine
    verdict, reason, metrics = engine.sweep_detector.evaluate_sweep(24000.0, "CALL", 102.5)
    assert "swept_level" in metrics


def test_resistance_stop_hunt_confirmed():
    """
    Simulates a Resistance breach where Short Call SLs panic buy (ce_buy)
    and Long Put SLs dump into Bid (pe_sell), followed by institutional reclaim down.
    """
    detector = SweepStopRunDetector(num_levels=32)
    levels = [StructuralLevel(level_type="RESISTANCE", price=24200.0, ts_created=100.0, prominence=2.0, bar_idx=1)]
    detector.sync_levels(levels)

    # 1. Seed baseline volume (100 vol/sec)
    for sec in range(100, 110):
        detector.on_option_tick(
            sym="NIFTY24200CE", strike=24200.0, side=0, buy_vol=50.0, sell_vol=50.0, ts=float(sec), atm_strike=24200.0
        )

    # 2. Spot is below resistance
    detector.on_spot_tick(spot=24190.0, ts=110.0)
    slot = detector._price_to_slot[24200.0]
    assert detector.in_breach[slot] == 0

    # 3. Spot surges above resistance to 24212.0
    detector.on_spot_tick(spot=24212.0, ts=111.0)
    assert detector.in_breach[slot] == 1

    # 4. Upside panic burst: Call Buy (350) + Put Sell (150) = 500 panic vol in 0.5s -> Rate = 1000/s
    detector.on_option_tick(
        sym="NIFTY24200CE", strike=24200.0, side=0, buy_vol=350.0, sell_vol=10.0, ts=111.5, atm_strike=24200.0
    )
    detector.on_option_tick(
        sym="NIFTY24200PE", strike=24200.0, side=1, buy_vol=10.0, sell_vol=150.0, ts=111.5, atm_strike=24200.0
    )

    # 5. Spot plunges back below resistance at T=112.0s with Put Buying (absorption)
    detector.on_option_tick(
        sym="NIFTY24200PE", strike=24200.0, side=1, buy_vol=200.0, sell_vol=10.0, ts=112.0, atm_strike=24200.0
    )
    detector.on_spot_tick(spot=24195.0, ts=112.0)
    assert detector.in_breach[slot] == 0

    # 6. Evaluate sweep verdict
    verdict, reason, metrics = detector.evaluate_sweep(swept_level=24200.0, direction="PUT", current_time=112.5)
    assert verdict == SWEEP_CONFIRMED_STOP_HUNT
    assert "confirmed_stop_hunt" in reason
    assert metrics["burst_ratio"] >= 2.2
    assert metrics["reclaim_flow"] > 0.0


def test_live_enforce_gate_blocking_simulation():
    """
    Verifies that when enforce_live_sweep_gate=True, a dry sweep verdict blocks the trade.
    """
    engine = PriceActionEngine(strike_step=50.0)
    engine.enforce_live_sweep_gate = True
    assert engine.enforce_live_sweep_gate is True

    # Artificially record a dry sweep verdict on level 24000.0
    engine.seed_daily_levels(pdh=24300.0, pdl=24000.0)
    slot = engine.sweep_detector._price_to_slot[24000.0]
    engine.sweep_detector.last_verdict[slot] = SWEEP_VETO_DRY_NO_STOPS
    engine.sweep_detector.last_reclaim_ts[slot] = 100.0

    verdict, reason, metrics = engine.sweep_detector.evaluate_sweep(24000.0, "CALL", 101.0)
    assert verdict == SWEEP_VETO_DRY_NO_STOPS

    # Check live blocking behavior
    should_block = engine.enforce_live_sweep_gate and verdict in (SWEEP_VETO_DRY_NO_STOPS, SWEEP_VETO_UNABSORBED_PANIC)
    assert should_block is True


def test_interior_cluster_unlocked_for_sweep_and_banned_for_breakout():
    """
    Verifies that an INTERIOR_CLUSTER level (tagged B_SWEEP_ONLY):
      1. Is ACCEPTED for Pillar 2 (Micro-Sweep Reclaim via _find_inflight_swept_sr_level)
      2. Is BANNED for Pillar 4 (Breakout via is_tradeable_breakout == False)
    """
    engine = PriceActionEngine(strike_step=50.0)

    # Create an interior cluster support level (e.g. 24050, with outermost floor at 24000)
    inner_lvl = StructuralLevel(
        level_type="SUPPORT",
        price=24050.0,
        ts_created=100.0,
        prominence=1.5,
        bar_idx=0,
        touch_count=2,
        level_role="INTERIOR_CLUSTER",
        is_tradeable_breakout=False,
        breakout_tier="B_SWEEP_ONLY",
    )
    engine.session_levels.append(inner_lvl)
    engine._total_bars_seen = 10

    # 1. Check Pillar 4 Breakout eligibility -> must be False
    assert inner_lvl.is_tradeable_breakout is False
    assert inner_lvl.breakout_tier == "B_SWEEP_ONLY"

    # 2. Check Pillar 2 In-flight Sweep eligibility -> must qualify and NOT be vetoed!
    from core.price_action_engine import OHLCBar
    prior_bar = OHLCBar(ts_start=1000.0, ts_end=1060.0, open=24060.0, high=24065.0, low=24040.0, close=24055.0)
    curr_bar = OHLCBar(ts_start=1060.0, ts_end=1120.0, open=24055.0, high=24070.0, low=24048.0, close=24068.0)

    swept = engine._find_inflight_swept_sr_level(
        direction="CALL",
        prior_bar=prior_bar,
        curr_bar=curr_bar,
        spot=24068.0,
        min_sweep_depth=2.0,
        atr=15.0,
    )
    assert swept is not None
    assert swept.price == 24050.0
    assert swept.level_role == "INTERIOR_CLUSTER"
    assert swept.breakout_tier == "B_SWEEP_ONLY"


