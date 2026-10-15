"""
tests/test_price_action_engines.py — Complete Unit Test Suite for AEGIS 3-Pillar Price Action Engines:

1. Pillar 1: 20 EMA + Wick Rejection (Bearish/Bullish Expansion + Pin-Bar/Hammer)
2. Pillar 2: Al Brooks 2-Legged Pullback (H2 / L2 Exhaustion at 50 EMA)
3. Pillar 3: Volatility Squeeze to Expansion Breakout (Compression < 0.65 ATR followed by Breakout)
4. Master Price Action Dispatcher (check_price_action_setups)
5. Watertight Gate Bypass (Bypasses Kaufman KER, Regime Chop Gate, Macro Drift, and Anti-Exhaustion)
"""

import queue
import time
from collections import deque
import pytest

from AEGIS.core.price_action_engine import PriceActionEngine, OHLCBar
from AEGIS.core.regime_chop_gate import RegimeChopGate
from AEGIS.aegis_gamma_engine import AegisGammaEngine


# ═══════════════════════════════════════════════════════════════
# PILLAR 1: 20 EMA + WICK REJECTION TESTS
# ═══════════════════════════════════════════════════════════════

class TestPillar1_20_EMA_Rejection:
    def test_20_ema_bar_and_live_calculation(self):
        engine = PriceActionEngine(atr_period=14)
        assert engine.ema_20_1m == 0.0

        for i in range(1, 30):
            bar = OHLCBar(
                ts_start=(i - 1) * 60.0,
                ts_end=i * 60.0,
                open=24000.0 - (i - 1) * 2.0,
                high=24000.0 - (i - 1) * 2.0 + 1.0,
                low=24000.0 - i * 2.0 - 1.0,
                close=24000.0 - i * 2.0,
                volume=100.0,
                tick_count=10,
            )
            engine._on_1m_bar_close(bar)

        assert engine.ema_20_1m > 0.0
        assert engine.ema_20_1m_slope < 0.0

        engine.last_price = 23940.0
        live_20, live_slope_20_bps = engine.get_live_20_ema_and_slope()
        assert live_20 > 0.0
        assert live_slope_20_bps < 0.0

    def test_20_ema_put_rejection_pin_bar(self):
        engine = PriceActionEngine()
        for i in range(25):
            engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 24000.0 - i * 1.0, 24002.0 - i * 1.0, 23998.0 - i * 1.0, 24000.0 - i * 1.0))

        live_ema_20, _ = engine.get_live_20_ema_and_slope()
        assert live_ema_20 > 0

        # Two-touch: FIRST touch closes (Leg 1), then 6 same-side pause bars,
        # then the rejection trigger closes (full-candle entries only).
        engine._on_1m_bar_close(OHLCBar(1500.0, 1560.0, live_ema_20 - 1.5, live_ema_20 + 0.1, live_ema_20 - 2.0, live_ema_20 - 1.0))
        live_ema_20, _ = engine.get_live_20_ema_and_slope()
        for k in range(6):
            engine._on_1m_bar_close(OHLCBar(1560.0 + k * 60, 1620.0 + k * 60, live_ema_20 - 2.5, live_ema_20 - 1.5, live_ema_20 - 3.5, live_ema_20 - 2.5))

        live_ema_20, _ = engine.get_live_20_ema_and_slope()
        engine.last_price = live_ema_20 - 2.0
        pin_bar = OHLCBar(
            ts_start=2000.0,
            ts_end=2060.0,
            open=live_ema_20 - 3.0,
            high=live_ema_20 + 0.2,
            low=live_ema_20 - 3.5,
            close=live_ema_20 - 2.8,
        )
        engine._on_1m_bar_close(pin_bar)
        engine.last_price = pin_bar.close

        is_rejection, reason, details = engine.check_20_ema_rejection("DOWN")
        assert is_rejection is True
        assert details["pillar"] == 1
        assert details["pa_setup_name"] == "20_ema_rejection"
        assert details["rejection_type"] == "pin_bar_rejection"
        assert details["sl_price"] == pytest.approx(pin_bar.high + 5.0, abs=0.02)

    def test_20_ema_put_rejection_bearish_expansion(self):
        engine = PriceActionEngine()
        for i in range(25):
            engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 24000.0 - i * 1.0, 24002.0 - i * 1.0, 23998.0 - i * 1.0, 24000.0 - i * 1.0))

        live_ema_20, _ = engine.get_live_20_ema_and_slope()
        # Two-touch: FIRST touch closes, then pause bars, then expansion closes.
        engine._on_1m_bar_close(OHLCBar(1500.0, 1560.0, live_ema_20 - 1.5, live_ema_20 + 0.1, live_ema_20 - 2.0, live_ema_20 - 1.0))
        live_ema_20, _ = engine.get_live_20_ema_and_slope()
        for k in range(6):
            engine._on_1m_bar_close(OHLCBar(1560.0 + k * 60, 1620.0 + k * 60, live_ema_20 - 2.5, live_ema_20 - 1.5, live_ema_20 - 3.5, live_ema_20 - 2.5))
        live_ema_20, _ = engine.get_live_20_ema_and_slope()
        engine.last_price = live_ema_20 - 6.0
        exp_bar = OHLCBar(
            ts_start=2000.0,
            ts_end=2060.0,
            open=live_ema_20 - 1.0,
            high=live_ema_20 + 0.3,
            low=live_ema_20 - 7.0,
            close=live_ema_20 - 6.0,
        )
        engine._on_1m_bar_close(exp_bar)
        engine.last_price = exp_bar.close

        is_rejection, reason, details = engine.check_20_ema_rejection("DOWN")
        assert is_rejection is True
        assert details["rejection_type"] == "bearish_expansion"

    def test_20_ema_call_rejection_hammer(self):
        engine = PriceActionEngine()
        for i in range(25):
            engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 24000.0 + i * 1.0, 24002.0 + i * 1.0, 23998.0 + i * 1.0, 24000.0 + i * 1.0))

        live_ema_20, _ = engine.get_live_20_ema_and_slope()
        # Two-touch: FIRST touch closes, then pause bars, then hammer closes.
        engine._on_1m_bar_close(OHLCBar(1500.0, 1560.0, live_ema_20 + 1.5, live_ema_20 + 2.0, live_ema_20 - 0.1, live_ema_20 + 1.0))
        live_ema_20, _ = engine.get_live_20_ema_and_slope()
        for k in range(6):
            engine._on_1m_bar_close(OHLCBar(1560.0 + k * 60, 1620.0 + k * 60, live_ema_20 + 2.5, live_ema_20 + 3.5, live_ema_20 + 1.5, live_ema_20 + 2.5))
        live_ema_20, _ = engine.get_live_20_ema_and_slope()
        engine.last_price = live_ema_20 + 4.0
        hammer_bar = OHLCBar(
            ts_start=2000.0,
            ts_end=2060.0,
            open=live_ema_20 + 3.0,
            high=live_ema_20 + 5.0,
            low=live_ema_20 - 0.5,
            close=live_ema_20 + 4.5,
        )
        engine._on_1m_bar_close(hammer_bar)
        engine.last_price = hammer_bar.close

        is_rejection, reason, details = engine.check_20_ema_rejection("UP")
        assert is_rejection is True
        assert details["pillar"] == 1
        assert details["sl_price"] <= hammer_bar.low - 5.0

    def test_20_ema_single_touch_rejected(self):
        """Two-touch gate: lone rejection with NO prior touch must NOT fire (11:12 lesson)."""
        engine = PriceActionEngine()
        for i in range(25):
            engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 24000.0 - i * 1.0, 24002.0 - i * 1.0, 23998.0 - i * 1.0, 24000.0 - i * 1.0))

        live_ema_20, _ = engine.get_live_20_ema_and_slope()
        engine.last_price = live_ema_20 - 2.0
        pin_bar = OHLCBar(1500.0, 1560.0, live_ema_20 - 3.0, live_ema_20 + 0.2, live_ema_20 - 3.5, live_ema_20 - 2.8)
        engine._on_1m_bar_close(pin_bar)
        engine.last_price = pin_bar.close

        # Sanity: seeded downtrend bars never touched the falling EMA...
        assert engine._had_prior_ema_touch("PUT") is False
        is_rejection, reason, details = engine.check_20_ema_rejection("DOWN")
        assert is_rejection is False
        assert reason in ("single_touch_no_confirmation", "did_not_test_20_ema")

    def test_20_ema_cold_engine_rejected(self):
        """Cold start (zero closed bars, e.g. post-restart) must NOT fire pillar 1 (14:20 lesson)."""
        engine = PriceActionEngine()
        engine.last_price = 75000.0
        engine._curr_1m_bar = OHLCBar(0.0, 60.0, 74990.0, 75010.0, 74985.0, 75005.0)
        is_rejection, reason, details = engine.check_20_ema_rejection("UP")
        assert is_rejection is False

    def test_20_ema_put_trapped_near_support_rejected(self):
        """
        Verifies support trap protection:
        When a 20-EMA rejection forms within 0.60x ATR of a prior swing low,
        the trade is rejected with reason 'trapped_near_support' rather than taking
        a bad entry directly into structural support.
        """
        engine = PriceActionEngine()
        # Seed 25 bars in downtrend
        for i in range(25):
            engine._on_1m_bar_close(OHLCBar(
                i * 60, (i + 1) * 60,
                24000.0 - i * 1.0, 24002.0 - i * 1.0, 23998.0 - i * 1.0, 24000.0 - i * 1.0
            ))

        live_ema_20, _ = engine.get_live_20_ema_and_slope()

        # Leg 1: touches 20-EMA from below
        engine._on_1m_bar_close(OHLCBar(1500.0, 1560.0, live_ema_20 - 1.5, live_ema_20 + 0.1, live_ema_20 - 2.0, live_ema_20 - 1.0))
        live_ema_20, _ = engine.get_live_20_ema_and_slope()

        # Establish an intraday pivot low at live_ema_20 - 5.0 (forming a true 3-bar V-pivot)
        # Bar 1: drops to live_ema_20 - 3.0
        engine._on_1m_bar_close(OHLCBar(1560.0, 1620.0, live_ema_20 - 1.5, live_ema_20 - 1.0, live_ema_20 - 3.0, live_ema_20 - 2.5))
        # Bar 2 (Pivot Low): drops deep to live_ema_20 - 5.0, closes at -4.0
        pivot_low_level = live_ema_20 - 5.0
        engine._on_1m_bar_close(OHLCBar(1620.0, 1680.0, live_ema_20 - 2.5, live_ema_20 - 2.0, pivot_low_level, live_ema_20 - 4.0))
        # Bar 3: bounces back up (low is live_ema_20 - 3.5, higher than Bar 2)
        engine._on_1m_bar_close(OHLCBar(1680.0, 1740.0, live_ema_20 - 4.0, live_ema_20 - 1.5, live_ema_20 - 3.5, live_ema_20 - 2.0))
        # Bar 4 & 5: retests 20-EMA
        engine._on_1m_bar_close(OHLCBar(1740.0, 1800.0, live_ema_20 - 2.0, live_ema_20 - 0.5, live_ema_20 - 2.5, live_ema_20 - 1.0))
        engine._on_1m_bar_close(OHLCBar(1800.0, 1860.0, live_ema_20 - 1.0, live_ema_20 + 0.2, live_ema_20 - 2.0, live_ema_20 - 1.0))

        live_ema_20, _ = engine.get_live_20_ema_and_slope()

        # Bar T: Rejection pin-bar touching 20-EMA, but close is at live_ema_20 - 3.0.
        # Room to pivot_low = (live_ema_20 - 3.0) - (live_ema_20 - 5.0) = 2.0 pts.
        # ATR is ~4.0 pts. 0.60 * ATR = 2.40 pts.
        # 2.0 pts < 2.40 pts threshold -> Trapped near support!
        rejection_bar = OHLCBar(
            ts_start=1860.0, ts_end=1920.0,
            open=live_ema_20 - 1.5,
            high=live_ema_20 + 0.2,
            low=live_ema_20 - 3.2,
            close=live_ema_20 - 3.0,
        )
        engine._on_1m_bar_close(rejection_bar)
        engine.last_price = rejection_bar.close

        # Must reject cleanly without arming pending BOS
        ok, reason, details = engine.check_20_ema_rejection("DOWN")
        assert ok is False
        assert reason == "trapped_near_support"
        assert details["support_level"] == round(pivot_low_level, 2)
        assert details["room_pts"] == round(rejection_bar.close - pivot_low_level, 2)

    def test_20_ema_call_trapped_near_resistance_rejected(self):
        """
        Verifies CALL direction resistance trap protection:
        When a 20-EMA bounce forms within 0.60x ATR below prior swing high,
        the trade is rejected with reason 'trapped_near_resistance'.
        """
        engine = PriceActionEngine()
        for i in range(25):
            engine._on_1m_bar_close(OHLCBar(
                i * 60, (i + 1) * 60,
                24000.0 + i * 1.0, 24002.0 + i * 1.0, 23998.0 + i * 1.0, 24000.0 + i * 1.0
            ))

        live_ema_20, _ = engine.get_live_20_ema_and_slope()

        # Leg 1: touches 20-EMA from above
        engine._on_1m_bar_close(OHLCBar(1500.0, 1560.0, live_ema_20 + 1.5, live_ema_20 + 2.0, live_ema_20 - 0.1, live_ema_20 + 1.0))
        live_ema_20, _ = engine.get_live_20_ema_and_slope()

        # Form an intraday pivot high at live_ema_20 + 5.0
        engine._on_1m_bar_close(OHLCBar(1560.0, 1620.0, live_ema_20 + 1.5, live_ema_20 + 3.0, live_ema_20 + 1.0, live_ema_20 + 2.5))
        pivot_high_level = live_ema_20 + 5.0
        engine._on_1m_bar_close(OHLCBar(1620.0, 1680.0, live_ema_20 + 2.5, pivot_high_level, live_ema_20 + 2.0, live_ema_20 + 4.0))
        engine._on_1m_bar_close(OHLCBar(1680.0, 1740.0, live_ema_20 + 4.0, live_ema_20 + 3.5, live_ema_20 + 1.5, live_ema_20 + 2.0))
        # Retest bars
        engine._on_1m_bar_close(OHLCBar(1740.0, 1800.0, live_ema_20 + 2.0, live_ema_20 + 2.5, live_ema_20 + 0.5, live_ema_20 + 1.0))
        engine._on_1m_bar_close(OHLCBar(1800.0, 1860.0, live_ema_20 + 1.0, live_ema_20 + 2.0, live_ema_20 - 0.2, live_ema_20 + 1.0))

        live_ema_20, _ = engine.get_live_20_ema_and_slope()

        # Rejection hammer near resistance (close is at live_ema_20 + 3.0, only 2 pts from pivot high +5.0)
        rejection_bar = OHLCBar(
            ts_start=1860.0, ts_end=1920.0,
            open=live_ema_20 + 1.5,
            high=live_ema_20 + 3.2,
            low=live_ema_20 - 0.2,
            close=live_ema_20 + 3.0,
        )
        engine._on_1m_bar_close(rejection_bar)
        engine.last_price = rejection_bar.close

        # Must reject cleanly without arming pending BOS
        ok, reason, details = engine.check_20_ema_rejection("UP")
        assert ok is False
        assert reason == "trapped_near_resistance"
        assert details["resistance_level"] == round(pivot_high_level, 2)
        assert details["room_pts"] == round(pivot_high_level - rejection_bar.close, 2)

    def test_20_ema_stale_bar_entry_window_expired(self):
        """Verifies Upgrade 2: Bars closed > 12s ago are rejected to prevent late chase entries."""
        engine = PriceActionEngine()
        for i in range(25):
            engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 24000.0 + i * 1.0, 24002.0 + i * 1.0, 23998.0 + i * 1.0, 24000.0 + i * 1.0))
        live_ema_20, _ = engine.get_live_20_ema_and_slope()
        bar = OHLCBar(1500.0, 1560.0, live_ema_20 + 0.5, live_ema_20 + 2.0, live_ema_20 - 0.1, live_ema_20 + 1.5)
        engine._on_1m_bar_close(bar)
        engine.last_price = bar.close

        # Call with current_time = 1560.0 + 31.0s (31s late, like 12:11 trade)
        ok, reason, details = engine.check_20_ema_rejection("UP", current_time=1591.0)
        assert ok is False
        assert reason == "stale_bar_entry_window_expired"
        assert details["time_since_close"] == 31.0

    def test_20_ema_live_price_pierce_guard(self):
        """Verifies Upgrade 3: If live price is piercing against 20 EMA, setup is immediately invalidated."""
        engine = PriceActionEngine()
        for i in range(25):
            engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 24000.0 + i * 1.0, 24002.0 + i * 1.0, 23998.0 + i * 1.0, 24000.0 + i * 1.0))
        live_ema_20, _ = engine.get_live_20_ema_and_slope()
        bar = OHLCBar(1500.0, 1560.0, live_ema_20 + 0.5, live_ema_20 + 2.0, live_ema_20 - 0.1, live_ema_20 + 1.5)
        engine._on_1m_bar_close(bar)

        # Live tick has plunged below 20 EMA (forming candle is falling red)
        engine.last_price = live_ema_20 - 5.0
        ok, reason, details = engine.check_20_ema_rejection("UP")
        assert ok is False
        assert reason == "live_price_piercing_below_20_ema"
        assert details["last_price"] == round(live_ema_20 - 5.0, 2)

    def test_human_eye_rejection_geometry_and_two_bar_transition(self):
        """
        Verifies that:
        1. A red spinning-top candle touching 20 EMA (like 10:26 IST) is NOT misclassified as a hammer for Signal A.
        2. When followed by a strong green confirmation candle (Signal B / Two-Bar), it confirms cleanly!
        """
        engine = PriceActionEngine()
        # Seed 25 bars in an uptrend
        for i in range(25):
            engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 73600.0 + i * 4.0, 73605.0 + i * 4.0, 73595.0 + i * 4.0, 73602.0 + i * 4.0))

        live_ema_20, _ = engine.get_live_20_ema_and_slope()
        assert live_ema_20 > 73600.0

        # Prior touch bar at EMA 20 (Leg 1 HL)
        engine._on_1m_bar_close(OHLCBar(1500.0, 1560.0, live_ema_20 + 2.0, live_ema_20 + 5.0, live_ema_20 - 0.5, live_ema_20 + 2.0))
        live_ema_20, _ = engine.get_live_20_ema_and_slope()

        # 6 pause / spacing bars
        for k in range(6):
            engine._on_1m_bar_close(OHLCBar(1560.0 + k * 60, 1620.0 + k * 60, live_ema_20 + 10.0, live_ema_20 + 15.0, live_ema_20 + 5.0, live_ema_20 + 12.0))

        live_ema_20, _ = engine.get_live_20_ema_and_slope()

        # Bar T-1: Red spinning-top (like 10:26 IST):
        # Open > Close, with upper wick (8 pts) and lower wick (6 pts)
        red_spinning_top = OHLCBar(
            ts_start=1920.0,
            ts_end=1980.0,
            open=live_ema_20 + 12.0,
            high=live_ema_20 + 20.0,
            low=live_ema_20 + 1.0,   # Touches EMA zone
            close=live_ema_20 + 7.0,  # Red candle (Close < Open) with upper wick
        )
        engine._on_1m_bar_close(red_spinning_top)
        engine.last_price = red_spinning_top.close

        # Must NOT trigger Signal A (red spinning top is rejected as a hammer!)
        ok_A, reason_A, det_A = engine.check_20_ema_rejection("UP")
        assert ok_A is False

        # Now Bar T: Green expansion bounce candle (like 10:28 IST)
        # Opens at EMA + 16, expands above T-1 high, closes strong above prior swing high (T low does NOT retest EMA)
        green_expansion = OHLCBar(
            ts_start=1980.0,
            ts_end=2040.0,
            open=live_ema_20 + 16.0,
            high=live_ema_20 + 35.0,
            low=live_ema_20 + 15.0,  # Does not touch EMA zone (tol_pts is ~11 pts)
            close=live_ema_20 + 33.0,
        )
        engine._on_1m_bar_close(green_expansion)
        engine.last_price = green_expansion.close

        # Must trigger Signal B (Two-Bar Path B confirmed cleanly!)
        ok_B, reason_B, det_B = engine.check_20_ema_rejection("UP")
        assert ok_B is True
        assert det_B["confirmation_path"] == "two_bar"
        assert det_B["rejection_type"] == "two_bar_retest_bullish_expansion"




# ═══════════════════════════════════════════════════════════════
# PILLAR 2: AL BROOKS 2-LEGGED PULLBACK TESTS
# ═══════════════════════════════════════════════════════════════

class TestPillar2_AlBrooks_2Legged_Pullback:
    def test_albrooks_l2_put_exhaustion(self):
        engine = PriceActionEngine()
        # Seed 55 bars in downtrend
        for i in range(55):
            engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 24000.0 - i * 0.5, 24002.0 - i * 0.5, 23998.0 - i * 0.5, 24000.0 - i * 0.5))

        live_ema_50, _ = engine.get_live_50_ema_and_slope()
        assert live_ema_50 > 0

        # Leg 1 tests 50 EMA from below (closed), then 6 same-side pause bars,
        # then Leg 2 sweeps higher into 50 EMA with bearish reversal close.
        b3 = OHLCBar(3300, 3360, live_ema_50 - 15.0, live_ema_50 - 8.0, live_ema_50 - 16.0, live_ema_50 - 10.0)
        engine.bars_1m.append(b3)
        for k in range(6):
            engine.bars_1m.append(OHLCBar(3360 + k * 60, 3420 + k * 60, live_ema_50 - 12.0, live_ema_50 - 9.0, live_ema_50 - 14.0, live_ema_50 - 11.0))
        # Leg 2 deep sweep right into 50 EMA (higher than b3) with reversal close
        b1 = OHLCBar(3720, 3780, live_ema_50 - 1.0, live_ema_50 + 1.0, live_ema_50 - 10.0, live_ema_50 - 7.0)
        engine._on_1m_bar_close(b1)
        engine.last_price = b1.close

        current_vwap = live_ema_50 + 20.0  # Safe below VWAP
        is_rejection, reason, details = engine.check_albrooks_2legged_pullback("DOWN", current_vwap)

        assert is_rejection is True
        assert details["pillar"] == 2
        assert details["pa_setup_name"] == "albrooks_2legged_pullback"
        assert details["structure"] == "L2_bearish_exhaustion"
        assert details["sl_price"] >= b1.high + 5.0

    def test_albrooks_h2_call_exhaustion(self):
        engine = PriceActionEngine()
        # Seed 55 bars in uptrend
        for i in range(55):
            engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 24000.0 + i * 0.5, 24002.0 + i * 0.5, 23998.0 + i * 0.5, 24000.0 + i * 0.5))

        live_ema_50, _ = engine.get_live_50_ema_and_slope()

        # Leg 1 tests 50 EMA from above (closed), then 6 same-side pause bars,
        # then Leg 2 sweeps lower into 50 EMA with bullish reversal close.
        b3 = OHLCBar(3300, 3360, live_ema_50 + 15.0, live_ema_50 + 16.0, live_ema_50 + 8.0, live_ema_50 + 10.0)
        engine.bars_1m.append(b3)
        for k in range(6):
            engine.bars_1m.append(OHLCBar(3360 + k * 60, 3420 + k * 60, live_ema_50 + 12.0, live_ema_50 + 14.0, live_ema_50 + 9.0, live_ema_50 + 11.0))
        # Leg 2 deep sweep right down into 50 EMA (lower than b3) with reversal
        b1 = OHLCBar(3720, 3780, live_ema_50 + 1.0, live_ema_50 + 10.0, live_ema_50 - 1.0, live_ema_50 + 7.0)
        engine._on_1m_bar_close(b1)
        engine.last_price = b1.close

        current_vwap = live_ema_50 - 20.0  # Safe above VWAP
        is_rejection, reason, details = engine.check_albrooks_2legged_pullback("UP", current_vwap)

        assert is_rejection is True
        assert details["pillar"] == 2
        assert details["structure"] == "H2_bullish_exhaustion"
        assert details["sl_price"] <= b1.low - 5.0

    def test_micro_sweep_call_reclaim(self):
        engine = PriceActionEngine()
        # Seed 25 bars in uptrend leading into consolidation
        for i in range(25):
            engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 24000.0 + i * 0.5, 24002.0 + i * 0.5, 23999.0 + i * 0.5, 24001.0 + i * 0.5))

        # Prior 5 bars have swing low at 24020.0
        for i in range(5):
            engine._on_1m_bar_close(OHLCBar((25 + i) * 60, (26 + i) * 60, 24022.0, 24026.0, 24020.0, 24024.0))

        # Sweep bar: dips to 24017.0 (< 24020.0), but closes at 24026.0 with long lower wick (9 pts / 10 pts = 90%)
        sweep_bar = OHLCBar(1860, 1920, 24021.0, 24027.0, 24017.0, 24026.0)
        engine._on_1m_bar_close(sweep_bar)
        engine.last_price = sweep_bar.close

        current_vwap = 24005.0  # Safe above VWAP
        is_sweep, reason, details = engine.check_micro_sweep_reclaim("UP", current_vwap)
        assert is_sweep is True
        assert details["pillar"] == 2
        assert details["pa_setup_name"] == "micro_sweep_reclaim"
        assert details["rejection_type"] == "liquidity_sweep_call_reclaim"
        assert details["sl_price"] <= 24017.0 - 5.0

    def test_micro_sweep_put_reclaim(self):
        engine = PriceActionEngine()
        # Seed 25 bars in downtrend
        for i in range(25):
            engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 24000.0 - i * 1.0, 24002.0 - i * 1.0, 23997.0 - i * 1.0, 23999.0 - i * 1.0))

        # Prior 5 bars have swing high at 23978.0
        for i in range(5):
            engine._on_1m_bar_close(OHLCBar((25 + i) * 60, (26 + i) * 60, 23976.0, 23978.0, 23972.0, 23974.0))

        # Sweep bar: pops to 23982.0 (> 23978.0), but closes at 23972.0 with long upper wick
        sweep_bar = OHLCBar(1860, 1920, 23977.0, 23982.0, 23971.0, 23972.0)
        engine._on_1m_bar_close(sweep_bar)
        engine.last_price = sweep_bar.close

        current_vwap = 23995.0  # Safe below VWAP
        is_sweep, reason, details = engine.check_micro_sweep_reclaim("DOWN", current_vwap)
        assert is_sweep is True
        assert details["pillar"] == 2
        assert details["pa_setup_name"] == "micro_sweep_reclaim"
        assert details["rejection_type"] == "liquidity_sweep_put_reclaim"
        assert details["sl_price"] >= 23982.0 + 5.0

    def test_micro_sweep_vetoed_in_lunch_window(self):
        """Verifies that Pillar 2 micro-sweeps are strictly vetoed during 11:15 to 13:00 IST."""
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        _IST = _tz(_td(hours=5, minutes=30))
        # 11:30:00 IST on 2026-10-12
        lunch_dt = _dt(2026, 10, 12, 11, 30, 0, tzinfo=_IST)
        lunch_ts = lunch_dt.timestamp()

        engine = PriceActionEngine()
        for i in range(5):
            engine._on_1m_bar_close(OHLCBar(lunch_ts - (5 - i) * 60, lunch_ts - (4 - i) * 60, 24000.0, 24010.0, 23990.0, 24005.0))

        sweep_bar = OHLCBar(lunch_ts - 60, lunch_ts, 24001.0, 24025.0, 23985.0, 24020.0)
        engine._on_1m_bar_close(sweep_bar)
        engine.last_price = sweep_bar.close

        is_sweep, reason, details = engine.check_micro_sweep_reclaim("UP", current_vwap=23950.0, current_time=lunch_ts)
        assert is_sweep is False
        assert reason == "lunch_chop_sweep_veto"
        assert details["verdict"] == "LUNCH_CHOP_MICRO_SWEEP_BLOCKED"

    def test_micro_sweep_allowed_outside_lunch_window(self):
        """Verifies that Pillar 2 micro-sweeps are allowed during morning momentum (e.g. 10:45 IST)."""
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        _IST = _tz(_td(hours=5, minutes=30))
        # 10:45:00 IST on 2026-10-12
        morning_dt = _dt(2026, 10, 12, 10, 45, 0, tzinfo=_IST)
        morning_ts = morning_dt.timestamp()

        engine = PriceActionEngine()
        # 5 bars establishing swing low at 24020.0
        for i in range(5):
            engine._on_1m_bar_close(OHLCBar(morning_ts - (6 - i) * 60, morning_ts - (5 - i) * 60, 24022.0, 24030.0, 24020.0, 24026.0))

        # Sweep bar: dips to 24017.0 (< 24020.0), closes at 24026.0 (wick = 9 pts / 10 pts = 90%)
        sweep_bar = OHLCBar(morning_ts - 60, morning_ts, 24021.0, 24027.0, 24017.0, 24026.0)
        engine._on_1m_bar_close(sweep_bar)
        engine.last_price = sweep_bar.close

        is_sweep, reason, details = engine.check_micro_sweep_reclaim("UP", current_vwap=23950.0, current_time=morning_ts)
        assert is_sweep is True
        assert details["pa_setup_name"] == "micro_sweep_reclaim"
        assert details["upper_wick_ratio"] <= 0.15
        assert details["close_edge"] >= 0.55

    def test_micro_sweep_call_rejected_on_heavy_upper_wick(self):
        """Verifies that Pillar 2 CALL rejects candles with heavy upper selling wick (> 28%)."""
        engine = PriceActionEngine()
        for i in range(5):
            engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 24022.0, 24030.0, 24020.0, 24026.0))

        # Sweep bar: sweeps low to 24016.0 (< 24020.0), bounces to 24040.0, but gets slammed down to 24024.0.
        # Range = 24.0. Open = 24021.0, Close = 24024.0 (body = 3.0). Lower wick = 5.0 (20.8%).
        # Upper wick = 24040 - 24024 = 16.0 (66.7% > 28%).
        sweep_bar = OHLCBar(300, 360, 24021.0, 24040.0, 24016.0, 24024.0)
        engine._on_1m_bar_close(sweep_bar)
        engine.last_price = sweep_bar.close

        is_sweep, reason, details = engine.check_micro_sweep_reclaim("UP", current_vwap=23950.0)
        assert is_sweep is False
        assert reason == "sweep_structure_not_met"

    def test_micro_sweep_call_rejected_on_weak_close_edge(self):
        """Verifies that Pillar 2 CALL rejects candles closing in lower half (< 55% close_edge)."""
        engine = PriceActionEngine()
        for i in range(5):
            engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 24022.0, 24030.0, 24020.0, 24026.0))

        # Sweep bar: low 24018.0 (< 24020.0), close 24021.0 (> 24020.0), but high 24035.0.
        # Range = 17.0. close_edge = (24021 - 24018) / 17.0 = 3.0 / 17.0 = 0.176 (< 0.55).
        # Open = 24019.0. Lower wick = 1.0. Upper wick = 14.0.
        sweep_bar = OHLCBar(300, 360, 24019.0, 24035.0, 24018.0, 24021.0)
        engine._on_1m_bar_close(sweep_bar)
        engine.last_price = sweep_bar.close

        is_sweep, reason, details = engine.check_micro_sweep_reclaim("UP", current_vwap=23950.0)
        assert is_sweep is False
        assert reason == "sweep_structure_not_met"

    def test_micro_sweep_put_rejected_on_heavy_lower_wick(self):
        """Verifies that Pillar 2 PUT rejects candles with heavy lower buying wick (> 28%)."""
        engine = PriceActionEngine()
        for i in range(25):
            engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 24000.0 - i * 1.0, 24002.0 - i * 1.0, 23997.0 - i * 1.0, 23999.0 - i * 1.0))
        for i in range(5):
            engine._on_1m_bar_close(OHLCBar((25 + i) * 60, (26 + i) * 60, 23976.0, 23978.0, 23972.0, 23974.0))

        # Sweep bar: pops to 23982.0 (> 23978.0), dumps to 23960.0, but buyers push it back to 23975.0.
        # Range = 22.0. Open = 23977.0, Close = 23975.0. Upper wick = 5.0 (22.7%).
        # Lower wick = 23975.0 - 23960.0 = 15.0 (68.2% > 28%).
        sweep_bar = OHLCBar(1860, 1920, 23977.0, 23982.0, 23960.0, 23975.0)
        engine._on_1m_bar_close(sweep_bar)
        engine.last_price = sweep_bar.close

        is_sweep, reason, details = engine.check_micro_sweep_reclaim("DOWN", current_vwap=23995.0)
        assert is_sweep is False
        assert reason == "sweep_structure_not_met"


# ═══════════════════════════════════════════════════════════════
# PILLAR 3: VOLATILITY SQUEEZE BREAKOUT TESTS
# ═══════════════════════════════════════════════════════════════

class TestPillar3_VolatilitySqueezeBreakout:
    def test_volatility_squeeze_upside_breakout(self):
        engine = PriceActionEngine()
        engine.current_atr_1m = 30.0  # ATR is 30 pts

        # 4 tight bars with total box range of 12 pts (< 0.65 * 30 = 19.5 pts)
        for i in range(4):
            engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 24100.0, 24106.0, 24095.0, 24101.0))

        # Breakout bar closes above 24106.0 (full-candle entry, no mid-bar hop)
        breakout_bar = OHLCBar(240, 300, 24101.0, 24118.0, 24100.0, 24115.0)
        engine._on_1m_bar_close(breakout_bar)
        engine.last_price = breakout_bar.close
        engine.current_atr_1m = 30.0  # Pin ATR so box stays squeezed

        current_vwap = 24050.0  # Spot is well above VWAP
        is_breakout, reason, details = engine.check_volatility_squeeze_breakout("UP", current_vwap)

        assert is_breakout is True
        assert details["pillar"] == 3
        assert details["pa_setup_name"] == "volatility_squeeze_breakout"
        assert details["box_high"] == 24106.0
        assert details["box_low"] == 24095.0
        assert details["sl_price"] <= 24095.0 - 5.0  # SL placed below box low

    def test_volatility_squeeze_downside_breakout(self):
        engine = PriceActionEngine()
        engine.current_atr_1m = 30.0

        for i in range(4):
            engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 24100.0, 24106.0, 24095.0, 24101.0))

        # Breakout bar closes below 24095.0 (full-candle entry, no mid-bar hop)
        breakout_bar = OHLCBar(240, 300, 24101.0, 24102.0, 24082.0, 24085.0)
        engine._on_1m_bar_close(breakout_bar)
        engine.last_price = breakout_bar.close
        engine.current_atr_1m = 30.0  # Pin ATR so box stays squeezed

        current_vwap = 24150.0  # Spot is below VWAP
        is_breakout, reason, details = engine.check_volatility_squeeze_breakout("DOWN", current_vwap)

        assert is_breakout is True
        assert details["pillar"] == 3
        assert details["sl_price"] >= 24106.0 + 5.0  # SL placed above box high

    def test_volatility_squeeze_weak_breakout_candle_rejected(self):
        """Verifies Upgrade 2: Breakout candles that are wicked-out or wrong color are rejected."""
        engine = PriceActionEngine()
        engine.current_atr_1m = 30.0

        for i in range(4):
            engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 24100.0, 24106.0, 24095.0, 24101.0))

        # Breakout bar closes above 24106.0 BUT is a red candle (Close < Open)
        weak_bar = OHLCBar(240, 300, 24115.0, 24120.0, 24100.0, 24108.0)
        engine._on_1m_bar_close(weak_bar)
        engine.last_price = weak_bar.close
        engine.current_atr_1m = 30.0

        current_vwap = 24050.0
        is_breakout, reason, details = engine.check_volatility_squeeze_breakout("UP", current_vwap)
        assert is_breakout is False
        assert reason == "weak_squeeze_breakout_candle"


# ═══════════════════════════════════════════════════════════════
# MASTER DISPATCHER & WATERTIGHT FILTER BYPASS TESTS
# ═══════════════════════════════════════════════════════════════

class TestWatertightPriceActionIntegration:
    def test_master_price_action_dispatcher(self):
        engine = PriceActionEngine()
        # Test when no setup is active
        is_setup, name, details = engine.check_price_action_setups("UP", 24000.0)
        assert is_setup is False

    def test_watertight_regime_chop_gate_bypass(self):
        """
        Critical Watertight Test:
        Verifies that when an active Price Action setup triggers,
        RegimeChopGate (Kaufman KER < 0.04 and RangeComp) CANNOT veto the trade!
        """
        sig_q = queue.Queue()
        token_map = {"100": {"side": 0, "strike": 24000.0, "is_index": True}}
        engine = AegisGammaEngine(
            token_to_symbol={"100": "NIFTY_SPOT", "200": "NIFTY24000CE"},
            token_map={"200": {"side": 0, "strike": 24000.0}},
            bucket_map={},
            strike_step=50.0,
            signal_queue=sig_q,
        )
        engine.last_spot = 24000.0
        engine.high_conviction_threshold = 75.0
        engine.moderate_conviction_threshold = 60.0

        # Equip with RegimeChopGate that is in HARD CHOP (KER = 0.01)
        engine.chop_gate = RegimeChopGate(ker_veto_threshold=0.04)
        # Feed back-and-forth ticks so KER collapses to near 0
        now = time.time()
        for i in range(40):
            px = 24000.0 + (0.5 if i % 2 == 0 else -0.5)
            engine.chop_gate.update(px, now - (40 - i) * 1.0, evaluate=False)

        ok, reason, metrics = engine.chop_gate.get_regime_state(now)
        assert ok is False  # Confirmed: RegimeChopGate is actively vetoing normal trades!

        # Setup PriceActionEngine with an active Pillar 1 (20 EMA Hammer rejection)
        pa_engine = PriceActionEngine()
        for i in range(25):
            pa_engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 24000.0 + i * 0.5, 24002.0 + i * 0.5, 23998.0 + i * 0.5, 24000.0 + i * 0.5))

        live_ema_20, _ = pa_engine.get_live_20_ema_and_slope()
        pa_engine.last_price = live_ema_20 + 3.0
        hammer = OHLCBar(1500, 1560, live_ema_20 + 2.0, live_ema_20 + 4.0, live_ema_20 - 0.5, live_ema_20 + 3.5)
        pa_engine._on_1m_bar_close(hammer)
        pa_engine._curr_1m_bar = hammer
        engine.price_action_engine = pa_engine
        engine.time_bar_engine = pa_engine

        # Verify evaluate_setup finds it
        is_pa, pa_name, pa_det = pa_engine.evaluate_setup("UP", 0.0)
        assert is_pa is True
        assert pa_name == "20_ema_rejection"

        class MockParsedTick:
            def __init__(self, tok, lp, vol=1000.0, ts=1787023500.0):
                import numpy as np
                self.tok = str(tok)
                self.lp = float(lp)
                self.vol = float(vol)
                self.ts = float(ts)
                self.bp = np.array([lp - 0.2, lp - 0.3, lp - 0.4, lp - 0.5, lp - 0.6], dtype=np.float64)
                self.bq = np.array([500, 400, 300, 200, 100], dtype=np.float64)
                self.sp = np.array([lp + 0.2, lp + 0.3, lp + 0.4, lp + 0.5, lp + 0.6], dtype=np.float64)
                self.sq = np.array([500, 400, 300, 200, 100], dtype=np.float64)

        # Mock option tick passing Option Quality
        # Even with KER collapsing and RegimeChopGate vetoing, the trade MUST fire!
        tick = MockParsedTick(
            tok="200",
            lp=150.0,
            vol=1000.0,
            ts=now,
        )
        engine.on_tick_parsed(tick)

        # Verify that signal was placed into queue!
        assert not sig_q.empty()
        sig = sig_q.get_nowait()
        assert sig["type"] == "GAMMA_BURST_ENTRY"
        assert sig["metrics"]["is_price_action_setup"] is True
        assert sig["metrics"]["pa_setup_name"] == "20_ema_rejection"
        assert sig["metrics"]["sl_price"] > 0
        assert sig["metrics"]["recommended_tp_pct"] == 0.040  # Constant 4% TP

    def test_microstructure_validator_opposing_wall_veto(self):
        """Verifies that an extreme opposing ask wall (top5_imb < -0.40) vetoes price action entry."""
        sig_q = queue.Queue()
        token_map = {"100": {"side": 0, "strike": 24000.0, "is_index": True}}
        engine = AegisGammaEngine(
            token_to_symbol={"100": "NIFTY_SPOT", "200": "NIFTY24000CE"},
            token_map={"200": {"side": 0, "strike": 24000.0}},
            bucket_map={},
            strike_step=50.0,
            signal_queue=sig_q,
        )
        engine.last_spot = 24000.0
        pa_engine = PriceActionEngine()
        for i in range(25):
            pa_engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 24000.0 + i * 0.5, 24002.0 + i * 0.5, 23998.0 + i * 0.5, 24000.0 + i * 0.5))
        live_ema_20, _ = pa_engine.get_live_20_ema_and_slope()
        pa_engine.last_price = live_ema_20 + 3.0
        pa_engine._curr_1m_bar = OHLCBar(1500, 1560, live_ema_20 + 2.0, live_ema_20 + 4.0, live_ema_20 - 0.5, live_ema_20 + 3.5)
        engine.price_action_engine = pa_engine
        engine.time_bar_engine = pa_engine

        class WallTick:
            def __init__(self):
                import numpy as np
                self.tok = "200"
                self.lp = 150.0
                self.vol = 1000.0
                self.ts = time.time()
                self.bp = np.array([149.8, 149.7, 149.6, 149.5, 149.4], dtype=np.float64)
                self.bq = np.array([50, 50, 50, 50, 50], dtype=np.float64)  # Tiny bid depth: 250
                self.sp = np.array([150.2, 150.3, 150.4, 150.5, 150.6], dtype=np.float64)
                self.sq = np.array([500, 500, 500, 500, 500], dtype=np.float64)  # Massive ask wall: 2500 (imb = -0.81)

        engine.on_tick_parsed(WallTick())
        assert sig_q.empty()  # Blocked by opposing depth wall veto!

    def test_microstructure_validator_wide_spread_veto(self):
        """Verifies that an excessive spread (> 1.0%) vetoes price action entry."""
        sig_q = queue.Queue()
        token_map = {"100": {"side": 0, "strike": 24000.0, "is_index": True}}
        engine = AegisGammaEngine(
            token_to_symbol={"100": "NIFTY_SPOT", "200": "NIFTY24000CE"},
            token_map={"200": {"side": 0, "strike": 24000.0}},
            bucket_map={},
            strike_step=50.0,
            signal_queue=sig_q,
        )
        engine.last_spot = 24000.0
        pa_engine = PriceActionEngine()
        for i in range(25):
            pa_engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 24000.0 + i * 0.5, 24002.0 + i * 0.5, 23998.0 + i * 0.5, 24000.0 + i * 0.5))
        live_ema_20, _ = pa_engine.get_live_20_ema_and_slope()
        pa_engine.last_price = live_ema_20 + 3.0
        pa_engine._curr_1m_bar = OHLCBar(1500, 1560, live_ema_20 + 2.0, live_ema_20 + 4.0, live_ema_20 - 0.5, live_ema_20 + 3.5)
        engine.price_action_engine = pa_engine
        engine.time_bar_engine = pa_engine

        class WideSpreadTick:
            def __init__(self):
                import numpy as np
                self.tok = "200"
                self.lp = 150.0
                self.vol = 1000.0
                self.ts = time.time()
                # 2.5 pt spread on 150 -> 1.67% spread > 1.0%
                self.bp = np.array([148.5, 148.0, 147.5, 147.0, 146.5], dtype=np.float64)
                self.bq = np.array([500, 500, 500, 500, 500], dtype=np.float64)
                self.sp = np.array([151.0, 151.5, 152.0, 152.5, 153.0], dtype=np.float64)
                self.sq = np.array([500, 500, 500, 500, 500], dtype=np.float64)

        engine.on_tick_parsed(WideSpreadTick())
        assert sig_q.empty()  # Blocked by spread friction veto!


# ═══════════════════════════════════════════════════════════════
# SPOT BAD-TICK GUARD (verified live 2026-09-09: 7.40 print on 75k SENSEX)
# ═══════════════════════════════════════════════════════════════

class TestSpotBadTickGuard:
    def _sensex_engine(self):
        sig_q = queue.Queue()
        return AegisGammaEngine(
            token_to_symbol={"1": "SENSEX_SPOT"},
            token_map={},
            bucket_map={},
            strike_step=100.0,
            signal_queue=sig_q,
        )

    def test_static_plausibility_matrix(self):
        g = AegisGammaEngine.is_plausible_spot_tick
        assert g(75068.0, 75060.0, 1.0) is True     # normal drift
        assert g(7.40, 75068.0, 1.0) is False       # the live bad tick
        assert g(0.0, 75068.0, 1.0) is False
        assert g(float("nan"), 75068.0, 1.0) is False
        assert g(75068.0, 0.0, 1.0) is True         # no reference yet
        assert g(76000.0, 75000.0, 3600.0) is True  # overnight gap re-syncs
        assert g(75000.0 * 1.0049, 75000.0, 1.0) is True   # just inside 0.5%
        assert g(75000.0 * 1.0051, 75000.0, 1.0) is False  # just outside 0.5%

    def test_bad_tick_dropped_before_state(self):
        engine = self._sensex_engine()
        t0 = 1_000_000.0
        engine._on_underlying_tick("SENSEX", 75068.0, t0)
        n_hist = len(engine.spot_history)
        assert engine.last_spot == 75068.0

        engine._on_underlying_tick("SENSEX", 7.40, t0 + 1.0)  # bad tick
        assert engine.last_spot == 75068.0                    # untouched
        assert len(engine.spot_history) == n_hist             # no pollution
        assert engine.session_low != 7.40                     # extremes safe
        assert engine.time_bar_engine.last_price == 75068.0   # PA bar safe

        engine._on_underlying_tick("SENSEX", 75070.0, t0 + 2.0)  # good tick flows
        assert engine.last_spot == 75070.0
        assert len(engine.spot_history) == n_hist + 1

    def test_gap_resync_after_feed_outage(self):
        engine = self._sensex_engine()
        t0 = 2_000_000.0
        engine._on_underlying_tick("SENSEX", 75000.0, t0)
        # Next-day open 1.3% higher after a long gap -> must NOT be mistaken for a bad tick
        engine._on_underlying_tick("SENSEX", 76000.0, t0 + 7200.0)
        assert engine.last_spot == 76000.0


class TestRefinedPriceActionFilters:
    def test_path_b_overextended_rejected(self):
        """Verifies that Path B rejects expansion candles closing too far from 20 EMA (anti-chasing)."""
        engine = PriceActionEngine()
        for i in range(25):
            engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 24000.0 + i * 1.0, 24002.0 + i * 1.0, 23998.0 + i * 1.0, 24000.0 + i * 1.0))

        live_ema_20, _ = engine.get_live_20_ema_and_slope()
        # Leg 1: touches EMA
        engine._on_1m_bar_close(OHLCBar(1500.0, 1560.0, live_ema_20 + 1.0, live_ema_20 + 2.0, live_ema_20 - 0.5, live_ema_20 + 0.5))
        live_ema_20, _ = engine.get_live_20_ema_and_slope()
        for k in range(6):
            engine._on_1m_bar_close(OHLCBar(1560.0 + k * 60, 1620.0 + k * 60, live_ema_20 + 2.0, live_ema_20 + 3.0, live_ema_20 + 1.0, live_ema_20 + 2.0))

        live_ema_20, _ = engine.get_live_20_ema_and_slope()
        # Bar T-1: tests EMA without piercing
        bar_prev = OHLCBar(2000.0, 2060.0, live_ema_20 + 1.0, live_ema_20 + 2.0, live_ema_20 - 0.2, live_ema_20 + 0.5)
        engine._on_1m_bar_close(bar_prev)

        # Bar T: massive vertical blow-off 40 points above 20 EMA!
        bar_overextended = OHLCBar(2060.0, 2120.0, live_ema_20 + 2.0, live_ema_20 + 42.0, live_ema_20 + 1.0, live_ema_20 + 40.0)
        engine._on_1m_bar_close(bar_overextended)
        engine.last_price = bar_overextended.close

        ok, reason, details = engine.check_20_ema_rejection("UP")
        # Must be rejected because price ran 40 points away!
        assert ok is False
        assert details.get("confirmation_path") != "two_bar"

    def test_20_ema_rejection_works_without_50_ema_lag(self):
        """Verifies that 20 EMA rejection triggers on valid 20 EMA test/rejection without being blocked by 50 EMA lag."""
        engine = PriceActionEngine()
        for i in range(50):
            engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 24000.0, 24001.0, 23999.0, 24000.0))

        engine.last_price = 24000.0
        # Force a retest candle with genuine hammer rejection off 20 EMA
        test_bar = OHLCBar(3000.0, 3060.0, 24000.0, 24001.1, 23999.1, 24001.0)
        engine._on_1m_bar_close(test_bar)

        ok, reason, details = engine.check_20_ema_rejection("UP")
        assert ok is True
        assert details.get("pillar") == 1

    def test_1m_option_flow_accumulation(self):
        """
        Verifies that PriceActionEngine:
        1. Accumulates option volume by trade initiation (Lee-Ready mid-quote rule).
        2. Filters out strikes outside ATM ± 7 strikes.
        3. Attaches Call/Put buy/sell volume and OFI directly to the closed OHLCBar.
        4. Resets the accumulator for the subsequent minute.
        """
        engine = PriceActionEngine(strike_step=100.0)
        engine.last_price = 75000.0  # ATM is 75000

        # Initial baseline seed tick (cumulative day volume)
        engine.update_option_tick("75000CE", 75000.0, 0, lp=100.0, bp1=99.8, sp1=100.2, vol=10000.0, atm_strike=75000.0)
        engine.update_option_tick("75000PE", 75000.0, 1, lp=120.0, bp1=119.8, sp1=120.2, vol=8000.0, atm_strike=75000.0)

        # 1. Aggressive Call Buying at Ask (lp=100.2 >= mid=100.0, delta_vol=500)
        engine.update_option_tick("75000CE", 75000.0, 0, lp=100.2, bp1=99.8, sp1=100.2, vol=10500.0, atm_strike=75000.0)
        # 2. Passive Call Selling at Bid (lp=99.8 < mid=100.0, delta_vol=200)
        engine.update_option_tick("75000CE", 75000.0, 0, lp=99.8, bp1=99.8, sp1=100.2, vol=10700.0, atm_strike=75000.0)
        # 3. Aggressive Put Buying at Ask (lp=120.2 >= mid=120.0, delta_vol=150)
        engine.update_option_tick("75000PE", 75000.0, 1, lp=120.2, bp1=119.8, sp1=120.2, vol=8150.0, atm_strike=75000.0)

        # 4. Out-of-window strike: ATM ± 10 strikes (76000 is 10 strikes away from 75000 > 7 threshold)
        engine.update_option_tick("76000CE", 76000.0, 0, lp=10.0, bp1=9.8, sp1=10.2, vol=5000.0, atm_strike=75000.0)
        engine.update_option_tick("76000CE", 76000.0, 0, lp=10.2, bp1=9.8, sp1=10.2, vol=6000.0, atm_strike=75000.0)

        # Close the 1-minute bar
        bar = OHLCBar(0, 60, 75000.0, 75020.0, 74990.0, 75015.0)
        engine._on_1m_bar_close(bar)

        # Verify bar attributes
        assert bar.call_buy_vol == 500.0
        assert bar.call_sell_vol == 200.0
        assert bar.put_buy_vol == 150.0
        assert bar.put_sell_vol == 0.0
        assert bar.opt_delta_flow == 350.0  # 500 - 150
        assert bar.opt_total_vol == 850.0   # 500 + 200 + 150 + 0 (76000 was filtered out!)
        assert bar.opt_pcr == round(150.0 / 700.0, 3)

        # Verify that accumulator was reset for the next candle
        bar2 = OHLCBar(60, 120, 75015.0, 75025.0, 75010.0, 75020.0)
        engine._on_1m_bar_close(bar2)
        assert bar2.call_buy_vol == 0.0
        assert bar2.opt_total_vol == 0.0


def test_hybrid_option_flow_and_pain_zone_vwaps():
    """
    Tests the 4-Line Pain Zone VWAP and Hybrid trade classification:
    1. Touch rule: lp >= sp1 -> buy, lp <= bp1 -> sell
    2. Inside spread: Tick Rule fallback (lp > prev_lp -> buy, lp < prev_lp -> sell)
    3. Inside spread zero-tick: carry forward prior sign
    4. Broken/zero quote: unclassified drop without buy bias
    5. Day rollover reset
    6. Pain Zone VWAP calculation and warmup gate
    """
    engine = PriceActionEngine(strike_step=50.0)
    engine.last_price = 22850.0

    # 1. Seed tick
    engine.update_option_tick("NIFTY_CE", 22850.0, 0, lp=100.0, bp1=99.0, sp1=101.0, vol=1000.0)
    assert engine._opt_flow_1m["call_buy_vol"] == 0.0

    # 2. Touch rule pure buy (lp >= sp1) -> delta_vol=500 -> 100% buy
    engine.update_option_tick("NIFTY_CE", 22850.0, 0, lp=101.5, bp1=100.0, sp1=101.0, vol=1500.0)
    assert engine._opt_flow_1m["call_buy_vol"] == 500.0
    assert engine._opt_flow_1m["call_sell_vol"] == 0.0

    # 3. Touch rule pure sell (lp <= bp1) -> delta_vol=300 -> 100% sell
    engine.update_option_tick("NIFTY_CE", 22850.0, 0, lp=99.5, bp1=100.0, sp1=101.0, vol=1800.0)
    assert engine._opt_flow_1m["call_sell_vol"] == 300.0

    # 4. Inside spread with Tick Rule fallback:
    # bp1=99.0, sp1=102.0, lp=100.5. Previous lp was 99.5.
    # lp > prev_lp (100.5 > 99.5) -> Tick rule says BUY!
    engine.update_option_tick("NIFTY_CE", 22850.0, 0, lp=100.5, bp1=99.0, sp1=102.0, vol=2000.0)
    assert engine._opt_flow_1m["call_buy_vol"] == 500.0 + 200.0  # +200

    # 5. Inside spread zero-tick carry forward:
    # lp=100.5 again, prev was 100.5. Carry forward prior buy frac (1.0).
    engine.update_option_tick("NIFTY_CE", 22850.0, 0, lp=100.5, bp1=99.0, sp1=102.0, vol=2100.0)
    assert engine._opt_flow_1m["call_buy_vol"] == 700.0 + 100.0  # +100

    # 6. Inside spread with downtick:
    # lp=100.0 < 100.5 -> Tick rule says SELL!
    engine.update_option_tick("NIFTY_CE", 22850.0, 0, lp=100.0, bp1=99.0, sp1=102.0, vol=2250.0)
    assert engine._opt_flow_1m["call_sell_vol"] == 300.0 + 150.0  # +150

    # 7. Broken/zero quote guard:
    # bp1=0, sp1=0 -> no quotes! Unclassified drop!
    engine.update_option_tick("NIFTY_FAR_OTM", 22850.0, 0, lp=5.0, bp1=0.0, sp1=0.0, vol=100.0)  # seed
    engine.update_option_tick("NIFTY_FAR_OTM", 22850.0, 0, lp=5.0, bp1=0.0, sp1=0.0, vol=200.0)  # no prev_lp inside quote
    assert engine._opt_flow_1m["unclass_vol"] == 100.0

    # 8. Put flow testing:
    engine.update_option_tick("NIFTY_PE", 22850.0, 1, lp=80.0, bp1=79.0, sp1=81.0, vol=1000.0)  # seed
    # Put Buy at ask:
    engine.update_option_tick("NIFTY_PE", 22850.0, 1, lp=81.5, bp1=79.0, sp1=81.0, vol=1400.0)  # +400 buy
    # Put Sell at bid:
    engine.update_option_tick("NIFTY_PE", 22850.0, 1, lp=79.0, bp1=79.0, sp1=81.0, vol=1600.0)  # +200 sell
    assert engine._opt_flow_1m["put_buy_vol"] == 400.0
    assert engine._opt_flow_1m["put_sell_vol"] == 200.0

    # 9. Verify Pain Zone VWAPs:
    # Spot is 22850.0. All trades happened at Spot 22850.0.
    vwaps = engine.get_pain_zone_vwaps(lot_size=1.0, min_bars=0)
    # Warmup threshold is 200 lots * 1.0 = 200 vol.
    # Call buy vol = 800 >= 200 -> VWAP active!
    assert round(vwaps["vwap_call_buy"], 1) == 22850.0
    # Call sell vol = 450 >= 200 -> VWAP active!
    assert round(vwaps["vwap_call_sell"], 1) == 22850.0
    # Put buy vol = 400 >= 200 -> VWAP active!
    assert round(vwaps["vwap_put_buy"], 1) == 22850.0
    # Put sell vol = 200 >= 200 -> VWAP active!
    assert round(vwaps["vwap_put_sell"], 1) == 22850.0
    # Composite VWAP is also active:
    assert round(vwaps["vwap_composite"], 1) == 22850.0

    # 10. Day Rollover reset test:
    engine._reset_option_day_state()
    assert engine._cum_cb_vol == 0.0
    assert engine._cum_pb_vol == 0.0
    vwaps_reset = engine.get_pain_zone_vwaps(lot_size=1.0, min_bars=0)
    assert vwaps_reset["vwap_call_buy"] == 0.0


def test_pain_zone_confluence_scorecard():
    """
    Tests DeepSeek V4 Pro 4 Setups x 4 Lines Ruleset:
    - P1: 20 EMA Wick Rejection (Boost, Veto, Hard Block)
    - P3: Squeeze Expansion (Positive gamma fade vs expansion boost)
    - P4: BOS (Max fuel vs late extension)
    """
    engine = PriceActionEngine(strike_step=50.0)
    engine.current_atr_1m = 10.0
    # Warmup requirement: 30 bars for institutional stabilization
    for i in range(30):
        engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 22800.0, 22805.0, 22795.0, 22800.0))

    # Setup simulated lines (> 13000.0 warmup threshold with 200 lots * 65):
    # CallBuy = 22850.0 (vol=15000), PutBuy = 22820.0 (vol=15000)
    # CallSell = 22860.0 (vol=15000), PutSell = 22810.0 (vol=15000)
    engine._cum_cb_pv = 22850.0 * 15000.0; engine._cum_cb_vol = 15000.0
    engine._cum_pb_pv = 22820.0 * 15000.0; engine._cum_pb_vol = 15000.0
    engine._cum_cs_pv = 22860.0 * 15000.0; engine._cum_cs_vol = 15000.0
    engine._cum_ps_pv = 22810.0 * 15000.0; engine._cum_ps_vol = 15000.0

    # Test 1: P1 PE setup with Spot at 22805 (Below CallBuy 22850 AND Below PutSell 22810)
    # Call buyers trapped (< 22850) AND Put writers underwater (< 22810) -> DUAL FUEL BOOST!
    ok, score, reason, _ = engine.evaluate_pain_zone_confluence(pillar=1, direction="PUT", spot=22805.0)
    assert ok is True
    assert score == 15.0
    assert reason == "p1_pe_dual_fuel_boost"

    # Test 2: P1 PE setup with Spot at 22828 (Above PutBuy 22820 by 8 pts > 0.5 ATR = 5 pts)
    # Put buyers trapped, dumping risk -> Veto -10 pts
    ok, score, reason, _ = engine.evaluate_pain_zone_confluence(pillar=1, direction="PUT", spot=22828.0)
    assert ok is True
    assert score == -10.0
    assert reason == "veto_put_buyer_dump_risk"

    # Test 3: P1 PE setup with Spot at 22835 (Above PutBuy 22820 by 15 pts >= 1.0 ATR = 10 pts)
    # Severe put buyer pain -> HARD BLOCK!
    ok, score, reason, _ = engine.evaluate_pain_zone_confluence(pillar=1, direction="PUT", spot=22835.0)
    assert ok is False
    assert score == -999.0
    assert reason == "hard_block_put_buyer_pain_severe"

    # Test 4: P1 CE setup with Spot at 22865 (Above PutBuy 22820 AND Above CallSell 22860)
    # Put buyers trapped (> 22820) AND Call writers underwater (> 22860) -> DUAL FUEL BOOST!
    ok, score, reason, _ = engine.evaluate_pain_zone_confluence(pillar=1, direction="CALL", spot=22865.0)
    assert ok is True
    assert score == 15.0
    assert reason == "p1_ce_dual_fuel_boost"

    # Test 5: P3 Squeeze Expansion with Spot at 22840 (Below CallBuy 22850) under POSITIVE gamma
    # Positive gamma pinning fade -> Hard Block!
    ok, score, reason, _ = engine.evaluate_pain_zone_confluence(pillar=3, direction="CALL", spot=22840.0, gamma_regime="POSITIVE")
    assert ok is False
    assert score == -999.0
    assert reason == "fade_p3_ce_under_call_buy_pos_gamma"

    # Test 6: P4 BOS PE with Spot at 22800 (Below PutSell 22810)
    # Put writers capitulation -> Max Fuel!
    ok, score, reason, _ = engine.evaluate_pain_zone_confluence(pillar=4, direction="PUT", spot=22800.0)
    assert ok is True
    assert score == 15.0
    assert reason == "p4_pe_max_fuel_writer_capitulation"

    # Test 7: P1 PE setup with PutBuy neutral, but Spot at 22866 (Above CallSell 22860 + 0.5 ATR = 22865)
    # Veto Check 2: Call writers broken / short squeeze risk -> Soft Veto (-10)
    engine._cum_pb_pv = 0.0; engine._cum_pb_vol = 0.0
    ok, score, reason, _ = engine.evaluate_pain_zone_confluence(pillar=1, direction="PUT", spot=22866.0)
    assert ok is True
    assert score == -10.0
    assert reason == "veto_call_seller_broken_risk"



def test_flow_symmetry_diagnostic_and_and_gate_warmup():
    """Verifies EOD Flow Symmetry Diagnostic ratio & AND-gate time + volume warmup."""
    engine = PriceActionEngine()
    engine._cum_cb_vol = 5000.0
    engine._cum_cs_vol = 5000.0
    engine._cum_pb_vol = 5000.0
    engine._cum_ps_vol = 5000.0
    diag = engine.get_flow_symmetry_diagnostic()
    assert diag["is_balanced"] is True
    assert diag["buyer_ratio"] == 0.50
    assert diag["alarm"] is False

    # Skewed volume (> 55% buyers) -> Alarm fires!
    engine._cum_cb_vol = 12000.0
    diag_skew = engine.get_flow_symmetry_diagnostic()
    assert diag_skew["buyer_ratio"] > 0.55
    assert diag_skew["alarm"] is True

    # AND-Gate Warmup: Even with 200 lots volume, if min_bars=30 and len(bars) < 30 -> Inactive!
    engine._cum_cb_pv = 15000.0 * 22800.0
    engine._cum_cb_vol = 15000.0
    pz_inactive = engine.get_pain_zone_vwaps(lot_size=65.0, min_bars=30)
    assert pz_inactive["vwap_call_buy"] == 0.0  # Blocked by time gate!

    # Once 30 bars are present -> Active!
    for i in range(30):
        engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 22800.0, 22805.0, 22795.0, 22800.0))
    pz_active = engine.get_pain_zone_vwaps(lot_size=65.0, min_bars=30)
    assert pz_active["vwap_call_buy"] == 22800.0




