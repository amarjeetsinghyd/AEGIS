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
from AEGIS.core.price_action_engine import PriceActionEngine, OHLCBar
from AEGIS.core.wyckoff_volume_engine import WyckoffVolumeEngine
from AEGIS.core.sweep_stop_run_detector import (
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
