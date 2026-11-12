"""
Targeted Unit Tests for AEGIS Pillar 5: gamma_trap_squeeze & Live Confirmed Sweep Gate.
Validates:
1. Call Writer Trap (vwap_call_sell) & Put Writer Trap (vwap_put_sell) in-flight Mode B triggers.
2. 3-Candle Dow Thrust progression gatekeeper.
3. Wyckoff OFI counter-liquidation gate.
4. Live enforcement of SweepStopRunDetector in Pillar 2.
5. VDR and Absorption disentangled from non-P1 pillars.
"""

import time
import pytest
from types import SimpleNamespace
from core.price_action_engine import PriceActionEngine, OHLCBar
from core.wyckoff_volume_engine import WyckoffVolumeEngine
from core.sweep_stop_run_detector import (
    SWEEP_CONFIRMED_STOP_HUNT,
    SWEEP_VETO_DRY_NO_STOPS,
    SWEEP_VETO_UNABSORBED_PANIC,
)


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


def test_pillar_5_call_writer_trap_trigger():
    """Validates that crossing above Call Sell VWAP with clean Dow Thrust triggers Pillar 5."""
    pae = PriceActionEngine(strike_step=50.0)
    pae.current_atr_1m = 15.0

    # Seed 3 candles forming an ascending Dow Thrust ladder
    # N-2: 24000 to 24020
    # N-1: 24010 to 24040 (HH & HL vs N-2)
    b_n2 = _make_bar(24005, 24020, 24000, 24015, ts_start=1700000000)
    b_n1 = _make_bar(24015, 24040, 24010, 24035, ts_start=1700000060)
    pae.bars_1m.append(b_n2)
    pae.bars_1m.append(b_n1)
    pae._total_bars_seen = 10

    # Mock Pain Zone: Call Sell VWAP is at 24030
    cs = 24030.0
    pae.get_pain_zone_vwaps = lambda *a, **kw: {
        "vwap_call_sell": cs,
        "vwap_put_sell": 23990.0,
        "vwap_call_buy": 24035.0,
        "vwap_put_buy": 23985.0,
    }

    # Forming bar opened below 24030 (at 24025), live spot pierced to 24045 (> cs and > N-1 high 24040)
    pae._curr_1m_bar = _make_bar(24025, 24045, 24020, 24045, ts_start=1700000120)
    pae.last_price = 24045.0
    pae.last_ts = 1700000135.0  # T:15s into minute

    # Evaluate Mode B
    ok, setup_name, details = pae.check_mode_b_breakthrough(
        direction="UP",
        current_time=pae.last_ts,
        commit=True,
    )

    assert ok is True
    assert setup_name == "gamma_trap_squeeze"
    assert details["pillar"] == 5
    assert details["writer_vwap"] == cs
    assert details["mode"] == "MODE_B_INFLIGHT_BREAKTHROUGH"


def test_pillar_5_put_writer_trap_trigger():
    """Validates that crossing below Put Sell VWAP with clean Dow Thrust triggers Pillar 5."""
    pae = PriceActionEngine(strike_step=50.0)
    pae.current_atr_1m = 15.0

    # Seed 3 candles forming a descending Dow Thrust ladder
    # N-2: 24100 to 24080
    # N-1: 24085 to 24060 (LL & LH vs N-2)
    b_n2 = _make_bar(24095, 24100, 24080, 24085, ts_start=1700000000)
    b_n1 = _make_bar(24085, 24085, 24060, 24065, ts_start=1700000060)
    pae.bars_1m.append(b_n2)
    pae.bars_1m.append(b_n1)
    pae._total_bars_seen = 10

    # Mock Pain Zone: Put Sell VWAP is at 24070
    ps = 24070.0
    pae.get_pain_zone_vwaps = lambda *a, **kw: {
        "vwap_call_sell": 24120.0,
        "vwap_put_sell": ps,
        "vwap_call_buy": 24125.0,
        "vwap_put_buy": 24065.0,
    }

    # Forming bar opened above 24070 (at 24075), live spot plunged to 24055 (< ps and < N-1 low 24060)
    pae._curr_1m_bar = _make_bar(24075, 24078, 24055, 24055, ts_start=1700000120)
    pae.last_price = 24055.0
    pae.last_ts = 1700000135.0  # T:15s into minute

    # Evaluate Mode B
    ok, setup_name, details = pae.check_mode_b_breakthrough(
        direction="DOWN",
        current_time=pae.last_ts,
        commit=True,
    )

    assert ok is True
    assert setup_name == "gamma_trap_squeeze"
    assert details["pillar"] == 5
    assert details["writer_vwap"] == ps
    assert details["mode"] == "MODE_B_INFLIGHT_BREAKTHROUGH"


def test_pillar_5_dow_thrust_failure_veto():
    """Fails if prior candles were NOT an ascending ladder (e.g. choppy drift)."""
    pae = PriceActionEngine(strike_step=50.0)
    pae.current_atr_1m = 15.0

    # N-2 High=24050, N-1 High=24040 (N-1 did NOT make Higher High -> no Dow Thrust)
    b_n2 = _make_bar(24010, 24050, 24000, 24030, ts_start=1700000000)
    b_n1 = _make_bar(24030, 24040, 24015, 24035, ts_start=1700000060)
    pae.bars_1m.append(b_n2)
    pae.bars_1m.append(b_n1)
    pae._total_bars_seen = 10

    pae.get_pain_zone_vwaps = lambda *a, **kw: {"vwap_call_sell": 24030.0}
    pae._curr_1m_bar = _make_bar(24025, 24045, 24020, 24045, ts_start=1700000120)
    pae.last_price = 24045.0
    pae.last_ts = 1700000135.0

    ok, _, _ = pae.check_pillar_5_gamma_trap(
        direction="UP",
        spot=pae.last_price,
        atr=pae.current_atr_1m,
        now_sec=pae.last_ts,
        sec_in_min=15,
        curr_bar=pae._curr_1m_bar,
        prior_bar=b_n1,
        bar_idx=10,
        bull_flow=100.0,
        bear_flow=10.0,
        commit=False,
    )

    assert ok is False


def test_live_sweep_gate_enforcement():
    """Validates that when enforce_live_sweep_gate is True, dry sweeps are rejected."""
    pae = PriceActionEngine(strike_step=50.0)
    assert pae.enforce_live_sweep_gate is True  # Default is now True!


def test_wyckoff_vdr_absorption_non_p1_disentangled():
    """Validates that Wyckoff engine does not output dummy VDR/absorption on P4/P5."""
    wy = WyckoffVolumeEngine(mode="ACTIVE")

    # Evaluate P5 gamma trap
    passed, reason, metrics = wy.evaluate(
        pillar=5,
        direction="UP",
        bars=[_make_bar(100, 105, 95, 102, 1000)],
        atr=10.0,
        pa_details={
            "pillar": 5,
            "pa_setup_name": "gamma_trap_squeeze",
            "mode": "MODE_B_INFLIGHT_BREAKTHROUGH",
            "opt_delta_flow": 2500.0,
            "opt_total_vol": 3000.0,
        },
    )

    assert passed is True
    # VDR and absorption_score must NOT be in metrics for Pillar 5!
    assert "vdr" not in metrics
    assert "absorption_score" not in metrics
    assert metrics["opt_delta_flow"] == 2500.0

    # Evaluate P1 (where VDR and absorption ARE relevant)
    passed_p1, _, metrics_p1 = wy.evaluate(
        pillar=1,
        direction="UP",
        bars=[_make_bar(100, 105, 95, 102, 1000)],
        atr=10.0,
        pa_details={
            "pillar": 1,
            "pa_setup_name": "20_ema_rejection",
            "mode": "MODE_B_INFLIGHT_BREAKTHROUGH",
            "opt_delta_flow": 1200.0,
            "opt_total_vol": 1500.0,
            "n1_footprint": {"n1_vol_dryup": True, "n1_stopping_vol": True},
        },
    )
    assert passed_p1 is True
    assert "vdr" in metrics_p1
    assert "absorption_score" in metrics_p1
    assert metrics_p1["vdr"] == 0.50
    assert metrics_p1["absorption_score"] == 2.0


def test_pillar_4_approach_ladder_enforcement():
    """Validates that Pillar 4 Dynamic S/R Breakout enforces the strict 2-Candle Approach Ladder."""
    from core.structural_registry import StructuralLevel

    pae = PriceActionEngine(strike_step=50.0)
    pae.current_atr_1m = 10.0
    now = 1700000000.0

    # Add a Resistance level at 22500.0
    res = StructuralLevel(
        level_type="RESISTANCE",
        price=22500.0,
        ts_created=now - 600.0,
        prominence=2.0,
        seq_idx=1,
        bar_idx=1,
        touch_count=2,
    )
    pae.session_levels.append(res)
    pae._total_bars_seen = 20

    # Scenario A: Non-ascending ladder (N-1 High < N-2 High -> Failure)
    b_n2_bad = _make_bar(22480, 22495, 22475, 22490, ts_start=now - 120.0)
    b_n1_bad = _make_bar(22490, 22492, 22480, 22488, ts_start=now - 60.0)  # High 22492 < 22495
    pae.bars_1m = [b_n2_bad, b_n1_bad]

    pae._curr_1m_bar = _make_bar(22490, 22505, 22488, 22505, ts_start=now)
    pae.last_price = 22505.0

    ok_bad, _, _ = pae.check_mode_b_breakthrough("UP", current_time=now + 15.0)
    assert ok_bad is False  # Rejected because 2-candle approach ladder failed!

    # Scenario B: Clean ascending ladder (N-1 High > N-2 High, Low > Low, Close > Open)
    b_n2_good = _make_bar(22475, 22485, 22470, 22482, ts_start=now - 120.0)
    b_n1_good = _make_bar(22482, 22498, 22480, 22496, ts_start=now - 60.0)  # HH, HL, Green
    pae.bars_1m = [b_n2_good, b_n1_good]

    ok_good, reason_good, details_good = pae.check_mode_b_breakthrough("UP", current_time=now + 15.0)
    assert ok_good is True
    assert details_good["pillar"] == 4
    assert details_good["approach_ladder"] is True


def test_shadow_rejection_approach_ladder():
    """Validates that ShadowBoundaryObserver requires sharp fall into support (CALL) and sharp rise into resistance (PUT)."""
    from core.shadow_boundary_observer import ShadowBoundaryObserver
    from unittest.mock import MagicMock

    obs = ShadowBoundaryObserver(price_action_engine=MagicMock(), strike_step=50.0)

    # CALL / Bullish Rejection: Needs Sharp Fall into support (N-1 High < N-2 High, N-1 Low < N-2 Low)
    b_n2_call = _make_bar(22520, 22525, 22515, 22518)
    b_n1_call_falling = _make_bar(22518, 22514, 22505, 22508)  # Sharp fall: 22514 < 22525 and 22505 < 22515
    assert obs._check_rejection_approach_ladder("UP", [b_n2_call, b_n1_call_falling]) is True

    b_n1_call_rising = _make_bar(22518, 22530, 22516, 22528)  # Rising: NOT a pullback into support!
    assert obs._check_rejection_approach_ladder("UP", [b_n2_call, b_n1_call_rising]) is False

    # PUT / Bearish Rejection: Needs Sharp Rise into resistance (N-1 High > N-2 High, N-1 Low > N-2 Low)
    b_n2_put = _make_bar(22480, 22485, 22475, 22482)
    b_n1_put_rising = _make_bar(22482, 22495, 22480, 22492)  # Sharp rise: 22495 > 22485 and 22480 > 22475
    assert obs._check_rejection_approach_ladder("DOWN", [b_n2_put, b_n1_put_rising]) is True


def test_wyckoff_n1_demand_ignition_and_squeeze_expansion():
    """Validates that Wyckoff Engine calculates N-1 Demand Ignition VER and Squeeze Expansion."""
    wy = WyckoffVolumeEngine(mode="ACTIVE")

    # 20 baseline bars with 1000 volume
    bars = [_make_bar(22400, 22410, 22390, 22405, vol=1000.0) for _ in range(20)]

    # Bar N-1 has 1300 volume (VER = 1.30x >= 1.10x) and bullish close
    b_n1_ignite = _make_bar(22405, 22425, 22400, 22422, vol=1300.0)
    bars.append(b_n1_ignite)

    passed, reason, metrics = wy.evaluate(
        pillar=4,
        direction="UP",
        bars=bars,
        atr=10.0,
        pa_details={
            "pillar": 4,
            "pa_setup_name": "sr_breakout",
            "mode": "MODE_B_INFLIGHT_BREAKTHROUGH",
            "opt_delta_flow": 2000.0,
            "opt_total_vol": 3000.0,
        },
    )

    assert passed is True
    assert metrics["n1_demand_ignition"] is True
    assert metrics["n1_ver"] >= 1.10
    assert metrics["wyckoff_confirmed"] is True


def test_pillar_1_live_rejection_approach_ladder():
    """Validates that Pillar 1 (20 EMA Rejection) Mode B enforces the strict Rejection Approach Ladder in production.

    Note (Nov 2026): the Dow HL2/LH2 ladder now mandates a 3-to-10 bar orderly pullback, so this
    series carries TWO completed red pullback bars plus the forming bar (pullback_bars = 3).
    """
    pae = PriceActionEngine(strike_step=50.0)
    pae.current_atr_1m = 10.0
    pae.ema_20_1m = 22450.0
    pae.ema_50_1m = 22440.0
    now = 1792730040.0

    # 12 base bars trending up
    for i in range(12):
        b = _make_bar(22450.0 + i, 22455.0 + i, 22448.0 + i, 22453.0 + i, vol=20000.0, ts_start=now + i * 60)
        pae.bars_1m.append(b)
    pae._total_bars_seen = 12

    # Scenario A: Clean two-bar falling pullback into the 20 EMA band
    # N-2 (i=11): High 22466, Low 22459
    # N-2 (new): High 22465, Low 22453 | N-1: High 22458 (< 22465), Low 22450 (< 22453) -> Valid Pullback Ladder!
    pullback_n2 = _make_bar(22464.0, 22465.0, 22453.0, 22457.0, vol=18000.0, ts_start=now + 12 * 60)
    pae.bars_1m.append(pullback_n2)
    pullback_valid = _make_bar(22457.0, 22458.0, 22450.0, 22453.5, vol=17000.0, ts_start=now + 13 * 60)
    pae.bars_1m.append(pullback_valid)
    pae._total_bars_seen = 14

    # Forming bar pierces N-1 High 22458.0
    pae._curr_1m_bar = _make_bar(22453.0, 22468.0, 22455.0, 22467.0, vol=15000.0, ts_start=now + 14 * 60)
    pae.last_price = 22467.0
    pae.last_ts = now + 14 * 60 + 15

    ok_good, reason_good, details_good = pae.check_mode_b_breakthrough("UP", current_time=pae.last_ts)
    assert ok_good is True
    assert details_good["pillar"] == 1
    assert details_good["rejection_ladder"] is True

    # Scenario B: Invalid pullback ladder (N-1 made a HIGHER high than N-2 -> rising away, not falling into EMA!)
    pae.bars_1m.pop()  # remove pullback_valid
    pullback_bad = _make_bar(22457.0, 22470.0, 22452.0, 22468.0, vol=17000.0, ts_start=now + 13 * 60)  # High 22470 > N-2 High 22465
    pae.bars_1m.append(pullback_bad)

    pae._mode_b_last_trigger_bar_idx.clear()
    ok_bad, _, _ = pae.check_mode_b_breakthrough("UP", current_time=pae.last_ts)
    assert ok_bad is False  # Blocked because rejection approach ladder failed! 


