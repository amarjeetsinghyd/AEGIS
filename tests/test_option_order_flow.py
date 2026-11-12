"""
Targeted tests for Task 1: Pre-Trade Quote Tracking and Book-Stepping Sweep Classification.
"""
import pytest
from core.price_action_engine import PriceActionEngine


def test_classify_buy_fraction_book_stepping_ask_sweep():
    """
    Task 1: When aggressive buyer sweeps the ask at 100.0, advancing the book to
    bp1=100.0, sp1=100.5, the trade at lp=100.0 must be classified as a pure buy (1.0).
    """
    pae = PriceActionEngine(strike_step=50.0)
    sym = "NIFTY26OCT24000CE"

    # Pre-trade quote: bp1=99.50, sp1=100.00, trade at 99.50 (hit bid -> 0.0)
    frac1 = pae._classify_buy_fraction(sym, lp=99.50, bp1=99.50, sp1=100.00)
    assert frac1 == 0.0

    # Ask swept! Post-sweep quote moves to bp1=100.00, sp1=100.50, lp=100.00
    frac2 = pae._classify_buy_fraction(sym, lp=100.00, bp1=100.00, sp1=100.50)
    assert frac2 == 1.0  # Must be 1.0 (pure buy), NOT 0.0!


def test_classify_buy_fraction_book_stepping_bid_sweep():
    """
    Task 1: When aggressive seller sweeps the bid at 99.50, depressing the book to
    bp1=99.00, sp1=99.50, the trade at lp=99.50 must be classified as a pure sell (0.0).
    """
    pae = PriceActionEngine(strike_step=50.0)
    sym = "NIFTY26OCT24000CE"

    # Pre-trade quote: bp1=99.50, sp1=100.00, trade at 100.00 (lift ask -> 1.0)
    frac1 = pae._classify_buy_fraction(sym, lp=100.00, bp1=99.50, sp1=100.00)
    assert frac1 == 1.0

    # Bid swept! Post-sweep quote moves to bp1=99.00, sp1=99.50, lp=99.50
    frac2 = pae._classify_buy_fraction(sym, lp=99.50, bp1=99.00, sp1=99.50)
    assert frac2 == 0.0  # Must be 0.0 (pure sell), NOT 1.0!


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

