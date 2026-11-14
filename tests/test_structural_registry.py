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


def test_level_break_on_1m_bar_immediately_reclassifies_and_syncs_sweep_detector():
    """
    Fix 1: When a level breaks on a 1m bar BETWEEN 5m bar closes
    (len(bars_5m) >= 3 and new_5m_bar == False), _update_structural_levels used to
    return early and wait up to 4 minutes for the next 5m bar close.

    Now: broken outermost levels are retired immediately and newly promoted
    outermost levels are synced into sweep_detector on the same 1m bar close.
    """
    pa = PriceActionEngine(strike_step=50.0)
    pa.current_atr_1m = 10.0
    pa.last_price = 24000.0
    pa.pdc = 24000.0
    now = 1792730040.0
    pa._levels_day = int(now // 86400)  # avoid day-reset clearing session_levels on first call

    # Outermost roof at 24100 (formed earlier) + inner resistance at 24050
    # formed ON the current bar (ts_created >= cand_hist.ts_start), so the prior
    # bar's close cannot invalidate it per the ts_created guard in the break loop.
    res_outer = StructuralLevel(
        level_type="RESISTANCE",
        price=24100.0,
        ts_created=now - 600,
        prominence=2.5,
        bar_idx=0,
        seq_idx=0,
        touch_count=2,
        timeframe="5m",
    )
    res_inner = StructuralLevel(
        level_type="RESISTANCE",
        price=24050.0,
        ts_created=now,  # formed on the current bar -> exempt from this bar's break check
        prominence=1.8,
        bar_idx=0,
        seq_idx=0,
        touch_count=1,
        timeframe="5m",
    )
    pa.session_levels = [res_outer, res_inner]
    pa._classify_level_roles()
    assert res_outer.level_role == "OUTERMOST_ROOF"
    assert 24100.0 in pa.sweep_detector._price_to_slot
    assert 24050.0 in pa.sweep_detector._price_to_slot

    # Seed 3 completed 5m bars -> the 5m fast-path is active
    base = now - 900
    pa.bars_5m.append(OHLCBar(ts_start=base, ts_end=base + 300, open=24000.0, high=24040.0, low=23990.0, close=24030.0, volume=1000.0))
    pa.bars_5m.append(OHLCBar(ts_start=base + 300, ts_end=base + 600, open=24030.0, high=24060.0, low=24020.0, close=24050.0, volume=1000.0))
    pa.bars_5m.append(OHLCBar(ts_start=base + 600, ts_end=base + 900, open=24050.0, high=24055.0, low=24040.0, close=24045.0, volume=1000.0))

    # 3 completed 1m bars; the MIDDLE bar (bars_1m[-2]) closes above 24100 + 0.20*ATR = 24102,
    # so it breaks the outermost resistance on this 1m bar close (not a 5m bar close).
    pa.bars_1m.append(OHLCBar(ts_start=now - 120, ts_end=now - 60, open=24090.0, high=24095.0, low=24088.0, close=24095.0, volume=1000.0))
    bar_breaker = OHLCBar(ts_start=now - 60, ts_end=now, open=24095.0, high=24110.0, low=24094.0, close=24105.0, volume=1000.0)
    pa.bars_1m.append(bar_breaker)
    bar_now = OHLCBar(ts_start=now, ts_end=now + 60, open=24105.0, high=24112.0, low=24104.0, close=24110.0, volume=1000.0)
    pa.bars_1m.append(bar_now)

    # 1m bar close between 5m bar closes (new_5m_bar = False)
    pa._update_structural_levels(bar_now, new_5m_bar=False)

    # 1. Outermost roof broken immediately on the 1m bar close
    assert res_outer.is_broken is True
    assert res_outer.ts_broken > 0.0

    # 2. Inner level (formed on this bar) survives the break check and is promoted
    #    to OUTERMOST_ROOF immediately by the same 1m bar close.
    assert res_inner.is_broken is False
    assert res_inner.level_role == "OUTERMOST_ROOF"

    # 3. sweep_detector synced immediately: broken roof retired, promoted inner roof tracked
    assert 24100.0 not in pa.sweep_detector._price_to_slot
    assert 24050.0 in pa.sweep_detector._price_to_slot
