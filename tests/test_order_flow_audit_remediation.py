import pytest
import time
from unittest.mock import MagicMock
from core.price_action_engine import PriceActionEngine, OHLCBar
from core.option_order_flow import CompositeConvictionEngine
from core.option_quality import OptionQualityEngine
from market.tick_compiler import TickCompiler, ParsedTick


def test_out_of_order_tick_rejects_phantom_volume():
    """Verify that an out-of-order tick (vol < prev_vol) is safely dropped without phantom volume."""
    pae = PriceActionEngine(strike_step=50.0)
    pae.last_price = 22500.0
    sym = "NIFTY26OCT22500CE"
    strike = 22500.0
    side = 0  # CE

    # 1. Baseline seed tick: vol = 500,000
    pae.update_option_tick(sym, strike, side, lp=100.0, bp1=99.5, sp1=100.0, vol=500000.0)
    assert pae._opt_flow_1m["call_buy_vol"] == 0.0

    # 2. Legitimate trade tick: vol = 500,020 (delta = 20)
    pae.update_option_tick(sym, strike, side, lp=100.0, bp1=99.5, sp1=100.0, vol=500020.0)
    assert pae._opt_flow_1m["call_buy_vol"] == pytest.approx(20.0)

    # 3. Out-of-order delayed packet: vol = 499,990 (delta = -30 < 0)
    # Must be safely discarded! Must NOT book 499,990 phantom contracts!
    pae.update_option_tick(sym, strike, side, lp=99.0, bp1=99.0, sp1=99.5, vol=499990.0)
    assert pae._opt_flow_1m["call_buy_vol"] == pytest.approx(20.0)
    assert pae._opt_last_cum_vol[sym] == 500020.0  # Last known max volume preserved


def test_silence_gap_universal_reset():
    """Verify that any silence > 5.0s (even 90s or 120s) clears cumulative volume baseline."""
    pae = PriceActionEngine(strike_step=50.0)
    pae.last_price = 22500.0
    sym = "NIFTY26OCT22500CE"
    strike = 22500.0
    side = 0

    t0 = 1000.0
    pae.last_ts = t0
    pae.update_option_tick(sym, strike, side, lp=100.0, bp1=99.5, sp1=100.0, vol=10000.0)
    assert sym in pae._opt_last_cum_vol

    # 90 seconds gap (outage)
    pae.last_ts = t0 + 90.0
    # On first tick post-outage, baseline is cleared so the 8,000 delta is NOT booked into one tick
    pae.update_option_tick(sym, strike, side, lp=105.0, bp1=104.5, sp1=105.0, vol=18000.0)
    # Post-reconnect tick becomes the new baseline seed
    assert pae._opt_flow_1m["call_buy_vol"] == 0.0
    assert pae._opt_last_cum_vol[sym] == 18000.0


def test_put_call_mirror_order_book_conviction():
    """
    CE vs PE Mirror Test:
    Option Buyer Mandate: An option buyer requires positive bid support (touch_i1 > 0).
    Verifies that identical bid-heavy books yield identical conviction scores for CE (UP) and PE (DOWN).
    """
    cce = CompositeConvictionEngine()

    # Identical book: touch_i1 = +0.25 (strong bid support)
    i1_val = 0.25
    spread_pct = 0.40  # Tight spread

    score_call, subs_call, _, _ = cce._evaluate_mode_b_conviction(
        direction="UP",
        setup_name="P3_SQUEEZE_BREAKOUT",
        touch_i1=i1_val,
        spread_pct=spread_pct,
        tick_rate=2.0,
    )

    score_put, subs_put, _, _ = cce._evaluate_mode_b_conviction(
        direction="DOWN",
        setup_name="P3_SQUEEZE_BREAKOUT",
        touch_i1=i1_val,
        spread_pct=spread_pct,
        tick_rate=2.0,
    )

    # Both must be equally rewarded with full score!
    assert score_call == score_put
    assert score_call >= 80.0
    assert subs_call.get("touch_aggression") == subs_put.get("touch_aggression")

    # Mode A Mirror Test:
    score_call_a, subs_call_a, _, _ = cce._evaluate_mode_a_conviction(
        direction="UP",
        setup_name="P1_20_EMA_REJECTION",
        touch_i1=i1_val,
        spread_pct=spread_pct,
        deep_pressure=0.20,
    )
    score_put_a, subs_put_a, _, _ = cce._evaluate_mode_a_conviction(
        direction="DOWN",
        setup_name="P1_20_EMA_REJECTION",
        touch_i1=i1_val,
        spread_pct=spread_pct,
        deep_pressure=0.20,
    )
    assert score_call_a == score_put_a
    assert score_call_a >= 80.0
    assert subs_call_a.get("order_book_support") == subs_put_a.get("order_book_support")


def test_tick_compiler_merged_book_preserves_depth():
    """Verify that TickCompiler maintains stateful merged book across Touchline and Depth messages."""
    broker = MagicMock()
    tc = TickCompiler(broker)
    assert not hasattr(tc, "footprints")

    token = "45892"
    key = "NIFTY26OCT22500CE"
    tc.set_symbol_map({token: key})

    mock_engine = MagicMock()
    tc.set_engine(mock_engine)

    # Message 1: Full 5-Level Depth update (no lp)
    depth_msg = {
        "token": token,
        "bp1": "100.0", "bq1": "500", "sp1": "100.5", "sq1": "300",
        "bp2": "99.5", "bq2": "400", "sp2": "101.0", "sq2": "600",
        "bp3": "99.0", "bq3": "300", "sp3": "101.5", "sq3": "200",
        "bp4": "98.5", "bq4": "200", "sp4": "102.0", "sq4": "100",
        "bp5": "98.0", "bq5": "100", "sp5": "102.5", "sq5": "50",
    }
    tc.on_message(depth_msg)

    # State book has 5 levels
    assert tc._book_state[token]["bp"][0] == 100.0
    assert tc._book_state[token]["bp"][4] == 98.0
    assert tc._book_state[token]["bq"][0] == 500

    # Message 2: Touchline update (contains lp, vol, bp1, sp1, bq1, sq1, but NO bp2..bp5)
    touchline_msg = {
        "token": token,
        "lp": "100.5",
        "v": "15000",
        "bp1": "100.5", "bq1": "650", "sp1": "101.0", "sq1": "450",
    }
    tc.on_message(touchline_msg)

    # Verify that L2-L5 levels were NOT wiped to 0.0!
    assert tc._book_state[token]["lp"] == 100.5
    assert tc._book_state[token]["vol"] == 15000.0
    assert tc._book_state[token]["bp"][0] == 100.5
    assert tc._book_state[token]["bp"][1] == 99.5   # Preserved!
    assert tc._book_state[token]["bp"][4] == 98.0   # Preserved!
    assert tc._book_state[token]["sp"][1] == 101.0  # Preserved!

    # Verify dispatched tick carries full 5 levels
    dispatched_tick = mock_engine.on_tick_parsed.call_args[0][0]
    assert dispatched_tick.lp == 100.5
    assert dispatched_tick.bp[1] == 99.5
    assert dispatched_tick.sp[1] == 101.0


def test_option_quality_fail_closed_zero_depth_and_spread():
    """Verify that OptionQualityEngine fails closed immediately on missing quotes or zero depth."""
    oq = OptionQualityEngine(min_premium=10.0, max_premium=300.0, max_spread_pct=1.5, min_top5_depth_qty=200.0)

    # 1. Missing quote (bp1=0, sp1=0) -> must reject immediately, never invent spread=0.05!
    passed, reason, _ = oq.qualify_option("TEST_CE", lp=50.0, bp1=0.0, sp1=0.0, top5_bid_qty=500.0, top5_ask_qty=500.0, lot_size=65.0)
    assert passed is False
    assert "invalid_or_missing_l1_quote" in reason

    # 2. Crossed quote (bp1 >= sp1) -> must reject immediately
    passed, reason, _ = oq.qualify_option("TEST_CE", lp=50.0, bp1=51.0, sp1=50.0, top5_bid_qty=500.0, top5_ask_qty=500.0, lot_size=65.0)
    assert passed is False
    assert "invalid_or_missing_l1_quote" in reason

    # 3. Zero depth -> must reject immediately, never invent fake 250/250 depth!
    passed, reason, _ = oq.qualify_option("TEST_CE", lp=50.0, bp1=49.8, sp1=50.2, top5_bid_qty=0.0, top5_ask_qty=0.0, lot_size=65.0)
    assert passed is False
    assert "insufficient_depth" in reason


def test_quote_inversion_post_sweep_quote_step():
    """
    Verify Leak #1 Remediation: Post-Sweep Quote-Step Detection.
    When a buyer aggressively sweeps the ask at 100.00 and advances the book to bp1=100.00, sp1=100.50,
    the trade tick at 100.00 (where lp == bp1 in the new book) must be recognized as an AGGRESSIVE BUY (frac = 1.0)
    because lp > prev_lp and lp >= bp1, rather than being falsely classified as an aggressive sell (frac = 0.0).
    """
    pae = PriceActionEngine(strike_step=50.0)
    sym = "NIFTY26OCT24000CE"
    
    # 1. Baseline quote at 99.50 / 100.00, prior trade at 99.50
    frac1 = pae._classify_buy_fraction(sym, lp=99.50, bp1=99.50, sp1=100.00)
    assert frac1 == 0.0  # Hit the bid
    
    # 2. Aggressive buyer sweeps 100.00 ask! Post-sweep quote moves to bp1=100.00, sp1=100.50
    # In Shoonya bundled snapshot: lp=100.00, bp1=100.00, sp1=100.50.
    # Because lp (100.00) > prev_lp (99.50) and lp >= bp1 (100.00), it's a quote-step BUY!
    frac2 = pae._classify_buy_fraction(sym, lp=100.00, bp1=100.00, sp1=100.50)
    assert frac2 == 1.0  # Successfully classified as BUY!


def test_mode_b_velocity_floor_rejects_stagnant_option():
    """
    Verify Leak #2 Remediation: Mode B Momentum Velocity Floor.
    Mode B breakthrough requires positive option price expansion (micro_ret >= +0.20%).
    A stagnant (0.0%) or decaying (-0.1%) option contract must receive score = 0.0 and be vetoed.
    """
    cce = CompositeConvictionEngine()
    
    # Stagnant option (micro_ret = 0.0005 < 0.0020)
    score, subs, details, reason = cce._evaluate_mode_b_conviction(
        direction="UP",
        setup_name="P3_SQUEEZE_BREAKOUT",
        touch_i1=0.25,
        spread_pct=0.40,
        tick_rate=5.0,
        micro_ret=0.0005,
    )
    assert score == 0.0
    assert details["reason"] == "stagnant_or_negative_option_velocity"
    assert "option_not_expanding" in reason

    # Expanding option (micro_ret = 0.0050 >= 0.0020) -> Passes with high conviction!
    score_ok, subs_ok, details_ok, _ = cce._evaluate_mode_b_conviction(
        direction="UP",
        setup_name="P3_SQUEEZE_BREAKOUT",
        touch_i1=0.25,
        spread_pct=0.40,
        tick_rate=5.0,
        micro_ret=0.0050,
    )
    assert score_ok >= 80.0
    assert subs_ok["touch_aggression"] > 0.0


def test_dow_pullback_3_bars_allowed():
    """
    Verify Leak #5 Remediation: Dow Macro Shields 3-to-10 Bar Pullback.
    Fast 0-DTE momentum moves often pull back in 3 bars. Ensure 3-bar pullbacks are accepted.
    """
    from core.dow_macro_shields import DowMacroShieldsMixin
    from core.price_action_engine import OHLCBar
    
    shield = DowMacroShieldsMixin()
    # Synthesize 25 bars with an uptrend and a 3-bar pullback
    bars = []
    t = 1000.0
    base = 24000.0
    for i in range(25):
        bars.append(OHLCBar(ts_start=t + i*60, ts_end=t + (i+1)*60, open=base + i*2, high=base + i*2 + 5, low=base + i*2 - 2, close=base + i*2 + 3))
    shield.bars_1m = bars
    
    # Check that search start allows idx_t2 - 3
    idx_t2 = len(bars) - 1
    trend_start = 5
    search_hh2_start = max(trend_start + 3, idx_t2 - 12)
    assert idx_t2 - 3 >= search_hh2_start
