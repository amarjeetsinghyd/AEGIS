"""
Targeted tests for Task 4 and 5:
- Task 4: Removal of dynamic excursion prominence mutation loop.
- Task 5: Strict exclusion of INTERIOR_CLUSTER levels from Pillar 2 Turtle Soup sweeps.
"""
import pytest
from core.price_action_engine import PriceActionEngine, OHLCBar
from core.structural_registry import StructuralLevel


def _make_bar(open_p, high_p, low_p, close_p, vol=1000.0, ts_start=1000.0):
    return OHLCBar(
        ts_start=float(ts_start),
        ts_end=float(ts_start + 60.0),
        open=float(open_p),
        high=float(high_p),
        low=float(low_p),
        close=float(close_p),
        volume=float(vol),
    )


def test_task_4_prominence_excursion_mutation_removed():
    """
    Task 4: Validates that strong trending excursions do NOT mutate or inflate
    the prominence of historical structural levels.
    """
    pae = PriceActionEngine(strike_step=50.0)
    pae.current_atr_1m = 10.0
    now = 1792730040.0

    lvl = StructuralLevel(
        level_type="SUPPORT",
        price=24000.0,
        ts_created=now - 300,
        prominence=1.20,
        bar_idx=5,
        touch_count=1,
        level_role="NORMAL",
    )
    pae.session_levels.append(lvl)

    # Market trends up strongly 150 points (excursion would be 15.0 ATRs)
    trending_bar = _make_bar(24140.0, 24150.0, 24135.0, 24145.0, vol=50000.0, ts_start=now)
    pae.bars_1m.append(trending_bar)
    pae._update_structural_levels(trending_bar)

    # Prominence must remain intact at 1.20 without being artificially inflated to 15.0+!
    assert lvl.prominence == 1.20


def test_task_5_interior_cluster_excluded_from_sweeps():
    """
    Task 5: Pillar 2 (Turtle Soup) strictly ignores INTERIOR_CLUSTER and low prominence levels.
    Only OUTERMOST_FLOOR, OUTERMOST_ROOF, PDH, PDL, or prominent MAJOR_SWING (>= 1.50) are swept.
    """
    pae = PriceActionEngine(strike_step=50.0)
    pae.current_atr_1m = 10.0
    pae._total_bars_seen = 10
    now = 1792730040.0

    # 1. Interior cluster level (chop)
    chop_lvl = StructuralLevel(
        level_type="SUPPORT",
        price=24000.0,
        ts_created=now - 500,
        prominence=2.0,
        bar_idx=2,
        touch_count=3,
        level_role="INTERIOR_CLUSTER",
    )
    # 2. Major swing with low prominence (< 1.50)
    weak_swing = StructuralLevel(
        level_type="SUPPORT",
        price=24000.0,
        ts_created=now - 500,
        prominence=1.30,
        bar_idx=2,
        touch_count=1,
        level_role="MAJOR_SWING",
    )
    # 3. Legitimate prominent major swing (>= 1.50)
    strong_swing = StructuralLevel(
        level_type="SUPPORT",
        price=24000.0,
        ts_created=now - 500,
        prominence=1.60,
        bar_idx=2,
        touch_count=2,
        level_role="MAJOR_SWING",
    )

    prior_bar = _make_bar(24005.0, 24010.0, 23995.0, 24002.0, ts_start=now - 60)
    curr_bar = _make_bar(24002.0, 24015.0, 23998.0, 24010.0, ts_start=now)

    # With only interior cluster level -> None (strictly excluded!)
    pae.session_levels = [chop_lvl]
    assert pae._find_inflight_swept_sr_level("CALL", prior_bar, curr_bar, spot=24010.0, min_sweep_depth=1.5, atr=10.0) is None
    assert pae._find_swept_sr_level("CALL", prior_bar, min_sweep_depth=1.5, atr=10.0) is None

    # With only weak major swing (< 1.50) -> None
    pae.session_levels = [weak_swing]
    assert pae._find_inflight_swept_sr_level("CALL", prior_bar, curr_bar, spot=24010.0, min_sweep_depth=1.5, atr=10.0) is None
    assert pae._find_swept_sr_level("CALL", prior_bar, min_sweep_depth=1.5, atr=10.0) is None

    # With strong major swing (>= 1.50) -> Swept!
    pae.session_levels = [strong_swing]
    assert pae._find_inflight_swept_sr_level("CALL", prior_bar, curr_bar, spot=24010.0, min_sweep_depth=1.5, atr=10.0) == strong_swing
    assert pae._find_swept_sr_level("CALL", prior_bar, min_sweep_depth=1.5, atr=10.0) == strong_swing
