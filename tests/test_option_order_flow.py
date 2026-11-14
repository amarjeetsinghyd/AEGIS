"""
Targeted tests for Task 1: Pre-Trade Quote Tracking and Book-Stepping Sweep Classification.
"""
import pytest
from core.price_action_engine import PriceActionEngine


def test_classify_buy_fraction_current_quote_first():
    """
    Task 1: Current live quote checked BEFORE book-stepping. lp == bp1 classifies as sell (0.0),
    even when lp >= prev_sp1 (operator bid-dump after upward quote drift cannot be misclassified
    as a 100% aggressive buy).
    """
    pae = PriceActionEngine(strike_step=50.0)
    sym = "NIFTY26OCT24000CE"

    # Pre-trade quote: bp1=99.50, sp1=100.00, trade at 99.50 (hit bid -> 0.0)
    frac1 = pae._classify_buy_fraction(sym, lp=99.50, bp1=99.50, sp1=100.00)
    assert frac1 == 0.0

    # Upward quote drift: bp1=100.00, sp1=100.50, lp=100.00 (lp == bp1 >= prev_sp1=100.00)
    # Must classify as 0.0 (sell), NOT 1.0 (buy) — operator bid-dump defense.
    frac2 = pae._classify_buy_fraction(sym, lp=100.00, bp1=100.00, sp1=100.50)
    assert frac2 == 0.0  # lp == bp1 -> hit current bid -> pure sell


def test_classify_buy_fraction_bid_hit_after_upward_drift():
    """
    Task 1: lp == bp1 >= prev_sp1 classifies as 0.0 (sell), not 1.0 (buy).
    This is the operator trap defense: bid-dump after upward quote drift.
    """
    pae = PriceActionEngine(strike_step=50.0)
    sym = "NIFTY26OCT24000CE"

    # Seed prev quote: bp1=99.50, sp1=100.00
    pae._classify_buy_fraction(sym, lp=99.50, bp1=99.50, sp1=100.00)

    # Now: bp1=100.00, sp1=100.50, lp=100.00 (lp == bp1 >= prev_sp1)
    frac = pae._classify_buy_fraction(sym, lp=100.00, bp1=100.00, sp1=100.50)
    assert frac == 0.0  # Sell, not buy — operator trap blocked


def test_classify_buy_fraction_direct_touch_and_tick_rule():
    """Validates direct touch and tick-rule fallbacks with pre-trade quote memory."""
    pae = PriceActionEngine(strike_step=50.0)
    sym = "NIFTY26OCT24000CE"

    # Direct ask lift
    assert pae._classify_buy_fraction(sym, lp=100.00, bp1=99.00, sp1=100.00) == 1.0
    # Inside spread uptick
    assert pae._classify_buy_fraction(sym, lp=100.50, bp1=99.00, sp1=101.00) == 1.0
    # Inside spread zero-tick carries previous fraction
    assert pae._classify_buy_fraction(sym, lp=100.50, bp1=99.00, sp1=101.00) == 1.0
    # Inside spread downtick
    assert pae._classify_buy_fraction(sym, lp=100.25, bp1=99.00, sp1=101.00) == 0.0


def test_option_contract_vwap_tracking():
    """Validates True Option-Contract Intraday VWAP calculation."""
    pae = PriceActionEngine(strike_step=50.0)
    sym = "NIFTY26OCT24000CE"

    # Initially 0.0
    assert pae.get_option_contract_vwap(sym) == 0.0

    # Seed tick
    pae.update_option_tick(sym, strike=24000.0, side=0, lp=100.0, vol=1000.0, bp1=99.5, sp1=100.0, now_ts=100.0)

    # Tick 1: 500 contracts at 100.0
    pae.update_option_tick(sym, strike=24000.0, side=0, lp=100.0, vol=1500.0, bp1=99.5, sp1=100.0, now_ts=101.0)
    # Tick 2: 500 contracts at 110.0
    pae.update_option_tick(sym, strike=24000.0, side=0, lp=110.0, vol=2000.0, bp1=109.5, sp1=110.0, now_ts=102.0)

    # Weighted VWAP: (100.0*500 + 110.0*500) / 1000 = 105.0
    vwap = pae.get_option_contract_vwap(sym)
    assert abs(vwap - 105.0) < 1e-4


def test_live_1m_option_flow_ring_blending():
    """Validates 15s rolling flow ring blending during sawtooth reset."""
    pae = PriceActionEngine(strike_step=50.0)
    sym = "NIFTY26OCT24000CE"

    # Seed baseline
    pae.update_option_tick(sym, strike=24000.0, side=0, lp=100.0, vol=1000.0, bp1=99.5, sp1=100.0, now_ts=100.0)

    # 1000 contracts bought in last 5 seconds
    pae.update_option_tick(sym, strike=24000.0, side=0, lp=100.0, vol=2000.0, bp1=99.5, sp1=100.0, now_ts=105.0)

    flow_before_reset = pae.get_live_1m_option_flow()
    assert flow_before_reset["call_buy_vol"] == 1000.0

    # Simulate 1m bar close: _opt_flow_1m resets to 0 (Sawtooth Reset)
    pae._opt_flow_1m = {k: 0.0 for k in pae._opt_flow_1m}
    assert pae.get_in_flight_opt_flow()["total_vol"] == 0.0

    # But rolling 15s flow ring preserves visibility!
    blended_flow = pae.get_live_1m_option_flow()
    assert blended_flow["call_buy_vol"] == 1000.0
    assert blended_flow["total_vol"] == 1000.0

