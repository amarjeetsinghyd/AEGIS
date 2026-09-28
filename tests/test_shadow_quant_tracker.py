"""
Tests for AEGIS ShadowQuantTracker — Passive Options Quant Telemetry.
Verifies all 5 novel academic/quant edges:
1. ATM Straddle Volatility Expansion
2. Call vs Put Skew Dislocation
3. Option-to-Spot Velocity Lead-Lag
4. 0-DTE Vanna & Charm Window
5. Intraday Seasonality Zone
"""

import pytest
import time
from datetime import datetime, timezone, timedelta
from AEGIS.core.shadow_quant_tracker import ShadowQuantTracker, IST


def test_shadow_quant_tracker_straddle_expansion():
    tracker = ShadowQuantTracker(history_len=60)
    base_ts = 1726030000.0  # arbitrary epoch timestamp

    # Add flat straddle for 60 seconds (Rs 200.0)
    for i in range(60):
        tracker.update_straddle(ce_price=100.0, pe_price=100.0, ts_sec=base_ts + i)

    # At t=60, straddle expands by +2.5% (CE jumps to 105.0)
    m = tracker.get_shadow_metrics(
        symbol="NIFTY24SEP25000CE",
        direction="UP",
        current_opt_price=105.0,
        current_spot_price=25010.0,
        ce_price=105.0,
        pe_price=100.0,
        now_sec=base_ts + 60.0,
    )

    assert m["shadow_straddle_px"] == 205.0
    assert m["shadow_straddle_ret_60s_pct"] == pytest.approx(2.5, abs=0.1)
    assert m["shadow_straddle_expanding"] is True


def test_shadow_quant_tracker_skew_dislocation():
    tracker = ShadowQuantTracker()
    now_sec = 1726030000.0

    # Strong Call Skew (CE=130, PE=100 -> ratio 1.30)
    m_up = tracker.get_shadow_metrics(
        symbol="NIFTY24SEP25000CE",
        direction="UP",
        current_opt_price=130.0,
        current_spot_price=25000.0,
        ce_price=130.0,
        pe_price=100.0,
        now_sec=now_sec,
    )
    assert m_up["shadow_skew_bias"] == "CALL_SKEW"
    assert m_up["shadow_skew_aligned"] is True

    # Put Skew (CE=80, PE=110 -> ratio 0.727)
    m_down = tracker.get_shadow_metrics(
        symbol="NIFTY24SEP25000PE",
        direction="DOWN",
        current_opt_price=110.0,
        current_spot_price=24990.0,
        ce_price=80.0,
        pe_price=110.0,
        now_sec=now_sec,
    )
    assert m_down["shadow_skew_bias"] == "PUT_SKEW"
    assert m_down["shadow_skew_aligned"] is True


def test_shadow_quant_tracker_option_leads_spot():
    tracker = ShadowQuantTracker()
    base_ts = 1726030000.0
    sym = "NIFTY24SEP25000CE"

    # Feed history at t=0
    tracker.update_option(sym, 100.0, base_ts)
    tracker.update_spot(25000.0, base_ts)

    # At t=30s, Option jumps +2% (+200 bps), spot moves only +0.02% (+2 bps)
    # Option velocity is 100x spot velocity!
    m = tracker.get_shadow_metrics(
        symbol=sym,
        direction="UP",
        current_opt_price=102.0,
        current_spot_price=25005.0,
        ce_price=102.0,
        pe_price=98.0,
        now_sec=base_ts + 30.0,
    )

    assert m["shadow_opt_velocity_30s_bps"] == pytest.approx(200.0, abs=1.0)
    assert m["shadow_option_leads_spot"] is True
    assert m["shadow_velocity_ratio"] >= 2.5


def test_shadow_quant_tracker_charm_and_seasonality():
    tracker = ShadowQuantTracker()

    # Create timestamp for 14:15 IST (Charm Window + Afternoon Gamma)
    dt_1415 = datetime(2026, 9, 14, 14, 15, 0, tzinfo=IST)
    ts_1415 = dt_1415.timestamp()

    m_charm = tracker.get_shadow_metrics(
        symbol="NIFTY24SEP25000CE",
        direction="UP",
        current_opt_price=100.0,
        current_spot_price=25000.0,
        now_sec=ts_1415,
    )
    assert m_charm["shadow_is_charm_window"] is True
    assert m_charm["shadow_seasonality_zone"] == "AFTERNOON_GAMMA"

    # Create timestamp for 12:30 IST (Lunch Chop Trap)
    dt_1230 = datetime(2026, 9, 14, 12, 30, 0, tzinfo=IST)
    ts_1230 = dt_1230.timestamp()

    m_lunch = tracker.get_shadow_metrics(
        symbol="NIFTY24SEP25000CE",
        direction="UP",
        current_opt_price=100.0,
        current_spot_price=25000.0,
        now_sec=ts_1230,
    )
    assert m_lunch["shadow_is_charm_window"] is False
    assert m_lunch["shadow_seasonality_zone"] == "LUNCH_CHOP_TRAP"
