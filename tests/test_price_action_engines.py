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
            open=live_ema_20 - 2.8,
            high=live_ema_20 + 0.2,
            low=live_ema_20 - 3.5,
            close=live_ema_20 - 3.2,
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
        pin_bar = OHLCBar(1500.0, 1560.0, live_ema_20 - 2.8, live_ema_20 + 0.2, live_ema_20 - 3.5, live_ema_20 - 3.2)
        engine._on_1m_bar_close(pin_bar)
        engine.last_price = pin_bar.close

        # Sanity: seeded downtrend bars never touched the falling EMA...
        assert engine._validate_dow_hl2_lh2_structure("PUT").is_valid is False
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

class TestPillar2_Micro_Sweep_Reclaim:

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

        # Breakout bar clears box_high by > min_box_clearance (24110.0 > 24108.4) BUT is a red candle (Close < Open)
        weak_bar = OHLCBar(240, 300, 24115.0, 24120.0, 24100.0, 24110.0)
        engine._on_1m_bar_close(weak_bar)
        engine.last_price = weak_bar.close
        engine.current_atr_1m = 30.0

        current_vwap = 24050.0
        is_breakout, reason, details = engine.check_volatility_squeeze_breakout("UP", current_vwap)
        assert is_breakout is False
        assert reason == "weak_squeeze_breakout_candle"

    def test_volatility_squeeze_micro_penetration_rejected(self):
        """Verifies Upgrade 3: Breakout candle closing only 0.5 pts above box is rejected by min_box_clearance."""
        engine = PriceActionEngine()
        engine.current_atr_1m = 30.0

        for i in range(4):
            engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 24100.0, 24106.0, 24095.0, 24101.0))

        # Candle closes at 24106.5 (only 0.5 pts above 24106.0, whereas min_box_clearance is 2.4 pts)
        micro_bar = OHLCBar(240, 300, 24102.0, 24107.0, 24100.0, 24106.5)
        engine._on_1m_bar_close(micro_bar)
        engine.last_price = micro_bar.close
        engine.current_atr_1m = 30.0

        current_vwap = 24050.0
        is_breakout, reason, details = engine.check_volatility_squeeze_breakout("UP", current_vwap)
        assert is_breakout is False
        assert reason == "no_upside_breakout"


# ═══════════════════════════════════════════════════════════════
# MASTER DISPATCHER & WATERTIGHT FILTER BYPASS TESTS
# ═══════════════════════════════════════════════════════════════

class TestWatertightPriceActionIntegration:
    def test_master_price_action_dispatcher(self):
        engine = PriceActionEngine()
        # Test when no setup is active
        is_setup, name, details = engine.check_price_action_setups("UP", 24000.0)
        assert is_setup is False

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
        for i in range(25):
            engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 24000.0 + i * 1.0, 24002.0 + i * 1.0, 23998.0 + i * 1.0, 24000.0 + i * 1.0))

        live_ema_20, _ = engine.get_live_20_ema_and_slope()
        # Touch 1 (HL1)
        engine._on_1m_bar_close(OHLCBar(1500.0, 1560.0, live_ema_20 + 1.5, live_ema_20 + 2.0, live_ema_20 - 0.1, live_ema_20 + 1.0))
        live_ema_20, _ = engine.get_live_20_ema_and_slope()
        # 6-bar orderly pullback
        for k in range(6):
            engine._on_1m_bar_close(OHLCBar(1560.0 + k * 60, 1620.0 + k * 60, live_ema_20 + 2.5, live_ema_20 + 3.5, live_ema_20 + 1.5, live_ema_20 + 2.5))

        live_ema_20, _ = engine.get_live_20_ema_and_slope()
        engine.last_price = live_ema_20 + 4.0
        # Force a retest candle with genuine hammer rejection off 20 EMA
        test_bar = OHLCBar(2000.0, 2060.0, live_ema_20 + 3.0, live_ema_20 + 5.0, live_ema_20 - 0.5, live_ema_20 + 4.5)
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


def test_argon_p4_prominence_anti_exhaustion_and_origin_caps():
    """
    Verifies Gemini for Argon Pillar 4 anti-exhaustion and origin caps:
    1. Bar range <= 1.35x ATR
    2. Hub displacement <= 1.25x ATR
    3. Bar open origin constraint (within 0.25x ATR of hub)
    4. Close edge >= 0.70
    """
    from unittest.mock import MagicMock
    engine = PriceActionEngine()
    engine.prominence_stack = MagicMock()
    engine.prominence_stack.compute_prominence.return_value = 2.5

    for i in range(60):
        engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 74400.0, 74410.0, 74390.0, 74400.0, volume=100.0))

    # Test 1: Gigantic bar range (> 1.35x ATR) -> Rejected as exhausted
    huge_bar = OHLCBar(3600, 3660, 74400.0, 74440.0, 74365.0, 74370.0, volume=200.0)
    engine._on_1m_bar_close(huge_bar)
    engine.last_price = 74370.0
    ok, reason, details = engine.check_topological_prominence_breakout("DOWN", current_vwap=74500.0)
    assert ok is False
    assert reason == "exhausted_breakout_candle_range"

    # Test 2: Overextended displacement from hub (> 1.25x ATR)
    engine2 = PriceActionEngine()
    engine2.prominence_stack = MagicMock()
    engine2.prominence_stack.compute_prominence.return_value = 2.5
    for i in range(58):
        engine2._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 74400.0, 74415.0, 74385.0, 74400.0, volume=100.0))
    # Hub at T-1 with close = 74430.0
    engine2._on_1m_bar_close(OHLCBar(3540, 3600, 74440.0, 74440.0, 74425.0, 74430.0, volume=100.0))
    # Breakout bar range = 40 pts (<= 1.35 * ATR), but displacement from hub = 60 pts (> 1.25 * ATR)
    overext_bar = OHLCBar(3600, 3660, 74410.0, 74410.0, 74370.0, 74370.0, volume=200.0)
    engine2._on_1m_bar_close(overext_bar)
    engine2.last_price = 74370.0
    engine2.bars_5m[-1].open = 74450.0
    engine2.bars_5m[-1].close = 74420.0
    ok2, reason2, details2 = engine2.check_topological_prominence_breakout("DOWN", current_vwap=74500.0)
    assert ok2 is False
    assert reason2 == "overextended_from_hub"


def test_argon_p4_day_low_runway_veto_and_breakout_pass():
    """
    Verifies Macro Runway & Day Extreme Clearance Rule:
    - Trade #2 (09:56 candle): Close is only 35.7 pts above Day Low (< 1.25x ATR) -> VETOED!
    - True breakdown below Day Low -> PASSES!
    """
    from unittest.mock import MagicMock
    engine = PriceActionEngine()
    engine.prominence_stack = MagicMock()
    engine.prominence_stack.compute_prominence.return_value = 2.5

    morning_day_low = 74323.0
    engine._on_1m_bar_close(OHLCBar(0, 60, 74350.0, 74360.0, morning_day_low, 74340.0, volume=100.0))
    for i in range(1, 58):
        engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 74400.0, 74415.0, 74385.0, 74400.0, volume=100.0))

    # Hub at 74388
    engine._on_1m_bar_close(OHLCBar(3480, 3540, 74388.0, 74395.0, 74380.0, 74388.0, volume=100.0))
    engine._on_1m_bar_close(OHLCBar(3540, 3600, 74388.0, 74390.0, 74380.0, 74385.0, volume=100.0))

    # Setup Bar: closes at 74,358.0 (Distance to Day Low = 35.0 pts < 62.5 pts runway!)
    setup_bar = OHLCBar(3600, 3660, 74385.0, 74385.0, 74355.0, 74358.0, volume=250.0)
    engine._on_1m_bar_close(setup_bar)
    engine.last_price = 74358.0

    ok, reason, details = engine.check_topological_prominence_breakout("DOWN", current_vwap=74500.0)
    assert ok is False
    assert "insufficient_runway_to_day_low" in reason
    assert details["room_pts"] == 35.0

    # True Breakdown Bar: Closes decisively below Day Low
    engine3 = PriceActionEngine()
    engine3._on_1m_bar_close(OHLCBar(0, 60, 74350.0, 74360.0, morning_day_low, 74340.0, volume=100.0))
    for i in range(1, 58):
        engine3._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 74380.0, 74390.0, 74360.0, 74370.0, volume=100.0))
    engine3._on_1m_bar_close(OHLCBar(3480, 3540, 74360.0, 74370.0, 74340.0, 74350.0, volume=100.0))
    engine3._on_1m_bar_close(OHLCBar(3540, 3600, 74350.0, 74355.0, 74330.0, 74335.0, volume=100.0))
    true_breakdown_bar = OHLCBar(3600, 3660, 74335.0, 74335.0, 74305.0, 74310.0, volume=250.0)
    engine3._on_1m_bar_close(true_breakdown_bar)
    engine3.last_price = 74310.0

    ok_bd, reason_bd, details_bd = engine3._check_day_extreme_runway("DOWN", true_breakdown_bar.close)
    assert ok_bd is True
    assert reason_bd == "clear_day_extreme_runway"


def test_argon_p3_day_high_runway_veto_and_breakout_pass():
    """
    Verifies Pillar 3 Volatility Squeeze respect for Day High Runway:
    - Box breakout into Day High without runway -> VETOED!
    """
    engine = PriceActionEngine()
    engine.current_atr_1m = 20.0

    day_high = 24050.0
    engine._on_1m_bar_close(OHLCBar(0, 60, 24000.0, day_high, 23990.0, 24010.0))
    for i in range(1, 25):
        engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 24000.0, 24015.0, 23995.0, 24010.0))

    for i in range(4):
        engine._on_1m_bar_close(OHLCBar((25 + i) * 60, (26 + i) * 60, 24012.0, 24025.0, 24010.0, 24020.0))

    squeeze_bo_bar = OHLCBar(1800, 1860, 24020.0, 24036.0, 24018.0, 24035.0)
    engine._on_1m_bar_close(squeeze_bo_bar)
    engine.last_price = squeeze_bo_bar.close

    ok, reason, details = engine.check_volatility_squeeze_breakout("UP", current_vwap=24000.0)
    assert ok is False
    assert "insufficient_runway_to_day_high" in reason
    assert details["room_pts"] == 15.0


def test_price_action_engine_full_session_capacity_375_bars():
    """
    Verifies that PriceActionEngine retains at least 375 1m bars (a full Indian trading session 09:15-15:30)
    without evicting early morning opening bars / Day High / Day Low.
    """
    engine = PriceActionEngine()
    assert engine.max_history_bars >= 400
    assert engine.bars_1m.maxlen >= 400
    assert engine.ema_20_history.maxlen >= 400

    # Seed 375 consecutive 1m bars across a full session
    # Bar 0 (09:15) establishes morning Day Low at 74,300.0
    engine._on_1m_bar_close(OHLCBar(0, 60, 74320.0, 74330.0, 74300.0, 74310.0))
    for i in range(1, 375):
        engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 74400.0, 74410.0, 74390.0, 74400.0))

    # All 375 bars must be preserved in memory
    assert len(engine.bars_1m) == 375
    # The first bar (09:15 AM) must still be present as bars_1m[0]!
    assert engine.bars_1m[0].low == 74300.0

    # Day Low calculated from intraday prior bars must still reflect the morning Day Low!
    day_low = min(b.low for b in list(engine.bars_1m)[:-1])
    assert day_low == 74300.0


def test_dynamic_sr_level_registry_and_historical_breakout():
    """
    Verifies that PriceActionEngine dynamically registers intraday Support & Resistance levels,
    maintains them across the session, and triggers Pillar 4 when a historical support (e.g. formed 30 bars ago)
    is decisively broken downwards.
    """
    engine = PriceActionEngine()
    engine.current_atr_1m = 40.0

    # 1. Bar 0 to 5: Form an initial support trough at 74,230.0 (Bar 2 has low=74,230.0)
    engine._on_1m_bar_close(OHLCBar(0, 60, 74300.0, 74310.0, 74270.0, 74280.0, volume=100.0))
    engine._on_1m_bar_close(OHLCBar(60, 120, 74280.0, 74290.0, 74230.0, 74240.0, volume=100.0))
    engine._on_1m_bar_close(OHLCBar(120, 180, 74240.0, 74350.0, 74240.0, 74340.0, volume=100.0))

    # Support at 74,230 should now be registered!
    supports = [l for l in engine.session_levels if l.level_type == "SUPPORT"]
    assert len(supports) >= 1
    assert any(abs(s.price - 74230.0) < 2.0 for s in supports)
    s_target = [s for s in supports if abs(s.price - 74230.0) < 2.0][0]
    assert s_target.is_broken is False

    # 2. Add 25 consolidation bars hovering between 74,260 and 74,320 (30 minutes elapse)
    for i in range(3, 28):
        engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 74280.0, 74310.0, 74260.0, 74290.0, volume=100.0))

    # Support at 74,230 must still remain intact and unbroken!
    assert s_target.is_broken is False

    # 3. Bar 28: Decisive breakdown candle slicing through 74,230
    # Opens at 74,235 (near support), closes at 74,205 (below support by 25 pts), solid red body
    breakdown_bar = OHLCBar(28 * 60, 29 * 60, 74235.0, 74238.0, 74200.0, 74205.0, volume=300.0)
    engine._on_1m_bar_close(breakdown_bar)
    engine.last_price = 74205.0

    # Ensure 5m trend aligns bearish
    engine.bars_5m[-1].open = 74300.0
    engine.bars_5m[-1].close = 74205.0

    ok, reason, details = engine.check_topological_prominence_breakout("DOWN", current_vwap=74350.0)
    assert ok is True
    assert reason == "prominence_hub_breakout_down"
    assert details["level_source"] == "sr_registry"
    assert abs(details["hub_price"] - 74230.0) < 2.0
    assert details["anchor_age_bars"] >= 25

    # The support level must now be marked as broken!
    assert s_target.is_broken is True

    # 4. A subsequent candle cannot re-trigger on this already-broken support
    subsequent_bar = OHLCBar(29 * 60, 30 * 60, 74205.0, 74210.0, 74180.0, 74185.0, volume=300.0)
    engine._on_1m_bar_close(subsequent_bar)
    engine.last_price = 74185.0
    # Because 74,230 is now broken, it won't re-trigger from sr_registry
    cand_unbroken = [l for l in engine.session_levels if l.level_type == "SUPPORT" and not l.is_broken and l.price >= 74200.0]
    assert len(cand_unbroken) == 0


def test_argon_sr_registry_4_bug_fixes():
    """
    Verifies all 4 fixes from Gemini for Argon's architectural review:
    1. Deque full (>400 bars) does not freeze bar_idx/seq_idx.
    2. Completed historical bars unconditionally mark broken levels.
    3. Dynamic post-excursion prominence updates as price moves away.
    4. Daily levels (PDH/PDL) are seeded into session_levels.
    5. get_active_sr_registry_snapshot returns valid telemetry dictionaries.
    """
    engine = PriceActionEngine(max_history_bars=400)
    engine.current_atr_1m = 40.0

    # 1. Test Daily Levels Seeding
    engine.seed_daily_levels(pdh=75000.0, pdl=74000.0, pdc=74500.0)
    assert any(l.level_type == "RESISTANCE" and l.price == 75000.0 for l in engine.session_levels)
    assert any(l.level_type == "SUPPORT" and l.price == 74000.0 for l in engine.session_levels)

    # 2. Test Deque Full (>400 bars) and Monotonic Counter
    # Seed 410 bars. Bar 10 forms a support at 74,100.
    engine._on_1m_bar_close(OHLCBar(0, 60, 74200.0, 74210.0, 74150.0, 74160.0, volume=100.0))
    engine._on_1m_bar_close(OHLCBar(60, 120, 74160.0, 74170.0, 74100.0, 74110.0, volume=100.0))
    engine._on_1m_bar_close(OHLCBar(120, 180, 74110.0, 74250.0, 74110.0, 74240.0, volume=100.0))

    s_100 = [l for l in engine.session_levels if l.level_type == "SUPPORT" and abs(l.price - 74100.0) < 2.0][0]
    assert s_100.seq_idx > 0

    # Now add 405 bars up to 74,300 so deque reaches and stays at maxlen=400
    for i in range(3, 410):
        engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 74250.0, 74300.0, 74240.0, 74280.0, volume=100.0))

    assert len(engine.bars_1m) == 400  # Deque full!
    assert engine._total_bars_seen == 410

    # Dynamic post-excursion: price reached high of 74,300, so prominence of 74,100 must have grown!
    # (74300 - 74100) / ATR(60.0) = 3.33x ATR!
    assert s_100.prominence >= 3.0

    # 3. Test Unconditional Break Invalidation
    # Add a bar that closes way below 74,100 without calling Pillar 4
    engine._on_1m_bar_close(OHLCBar(410 * 60, 411 * 60, 74110.0, 74115.0, 74050.0, 74060.0, volume=100.0))
    # On the very next bar, s_100 must be unconditionally marked is_broken = True!
    engine._on_1m_bar_close(OHLCBar(411 * 60, 412 * 60, 74060.0, 74070.0, 74040.0, 74050.0, volume=100.0))
    assert s_100.is_broken is True

    # 4. Test Telemetry Snapshot Helper
    snap = engine.get_active_sr_registry_snapshot()
    assert isinstance(snap, list)
    assert len(snap) >= 3
    assert any(item["price"] == 74100.0 and item["is_broken"] is True for item in snap)


def test_sr_registry_volume_and_order_flow_prominence_boost():
    """
    Verifies that StructuralLevel:
    1. Captures anchor volume and option order flow footprint (call/put buy/sell vol, delta, PCR).
    2. Calculates volume ratio vs 60m median and applies volume ratio boost to prominence.
    3. Adds institutional OFI boost (e.g. Put writing at Support, Call writing at Resistance).
    4. Accumulates volume and order flow on re-test touches.
    5. Exposes all order flow metrics in get_active_sr_registry_snapshot.
    6. Propagates anchor volume & flow into check_topological_prominence_breakout details.
    """
    engine = PriceActionEngine(max_history_bars=400)
    engine.current_atr_1m = 40.0

    # Establish baseline volume of 100.0 across 10 bars
    for i in range(10):
        engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 74300.0, 74320.0, 74280.0, 74310.0, volume=100.0))

    # Form a Support Trough at 74,200.0 with HEAVY institutional volume and Put Writing:
    # cand = Bar 11 (ts: 660-720) with low=74,200.0, volume=300.0 (VR=3.0),
    # put_sell_vol=1500.0 (heavy put writing), call_buy_vol=500.0, opt_total_vol=2500.0
    bar_prev = OHLCBar(10 * 60, 11 * 60, 74310.0, 74315.0, 74250.0, 74260.0, volume=100.0)
    bar_cand = OHLCBar(
        11 * 60, 12 * 60, 74260.0, 74270.0, 74200.0, 74210.0,
        volume=300.0,
        call_buy_vol=500.0,
        call_sell_vol=200.0,
        put_buy_vol=300.0,
        put_sell_vol=1500.0,
        opt_delta_flow=200.0,
        opt_total_vol=2500.0,
        opt_pcr=1.8 / 0.7,
    )
    bar_curr = OHLCBar(12 * 60, 13 * 60, 74210.0, 74320.0, 74210.0, 74310.0, volume=150.0)

    engine._on_1m_bar_close(bar_prev)
    engine._on_1m_bar_close(bar_cand)
    engine._on_1m_bar_close(bar_curr)

    supports = [l for l in engine.session_levels if l.level_type == "SUPPORT" and abs(l.price - 74200.0) < 2.0]
    assert len(supports) == 1
    s_lvl = supports[0]

    # Verify footprint captured
    assert s_lvl.volume == 300.0
    assert s_lvl.vr_anchor >= 2.5
    assert s_lvl.put_sell_vol == 1500.0
    assert s_lvl.call_buy_vol == 500.0
    assert s_lvl.opt_delta_flow == 200.0

    # Prominence should be boosted:
    # Base depth >= 1.50 ATR
    # Volume ratio boost + OFI boost
    assert s_lvl.prominence >= 2.20

    # Test Telemetry Snapshot
    snap = engine.get_active_sr_registry_snapshot()
    snap_s = [x for x in snap if x["type"] == "SUPPORT" and abs(x["price"] - 74200.0) < 2.0][0]
    assert snap_s["volume"] == 300.0
    assert snap_s["put_sell_vol"] == 1500.0
    assert snap_s["vr_anchor"] >= 2.5
    assert snap_s["prominence"] == s_lvl.prominence

    # Add 25 consolidation bars and then test breakdown trigger details
    for i in range(13, 38):
        engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 74240.0, 74260.0, 74230.0, 74250.0, volume=100.0))

    breakdown_bar = OHLCBar(38 * 60, 39 * 60, 74205.0, 74208.0, 74170.0, 74175.0, volume=350.0)
    engine._on_1m_bar_close(breakdown_bar)
    engine.last_price = 74175.0
    engine.bars_5m[-1].open = 74260.0
    engine.bars_5m[-1].close = 74175.0

    ok, reason, details = engine.check_topological_prominence_breakout("DOWN", current_vwap=74300.0)
    assert ok is True
    assert details["anchor_volume"] == 300.0
    assert details["anchor_put_sell_vol"] == 1500.0
    assert details["anchor_vr"] >= 2.5


def test_pillar4_wyckoff_stopping_volume_trap_blocked():
    """
    Verifies that the 21 Oct 09:56 Stopping Volume Trap is strictly blocked:
    A high-volume red candle (VR=1.57x) breaking below support, but having positive net
    bullish delta (Put Writing PutSell > PutBuy, BullFlow > BearFlow by +1.29M) is
    STRICTLY VETOED by wyckoff_p4_stopping_volume_bullish_absorption!
    """
    engine = PriceActionEngine(max_history_bars=400)
    engine.current_atr_1m = 40.0

    # 1. Register a Support at 74,323.0
    engine._on_1m_bar_close(OHLCBar(0, 60, 74380.0, 74390.0, 74350.0, 74360.0, volume=100.0))
    engine._on_1m_bar_close(OHLCBar(60, 120, 74360.0, 74370.0, 74323.0, 74330.0, volume=100.0))
    engine._on_1m_bar_close(OHLCBar(120, 180, 74330.0, 74450.0, 74330.0, 74440.0, volume=100.0))

    supports = [l for l in engine.session_levels if l.level_type == "SUPPORT" and abs(l.price - 74323.0) < 2.0]
    assert len(supports) == 1
    s_lvl = supports[0]

    # Add 15 consolidation bars hovering around 74,350 - 74,400
    for i in range(3, 18):
        engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 74360.0, 74380.0, 74340.0, 74370.0, volume=100.0))

    # 2. Replay the exact 09:56 candle:
    # High volume (157.0 vs 100.0 median = 1.57x), big red body, closes at 74,310 (below 74,323 support).
    # BUT order flow is Net Bullish by +1.29M:
    # CallBuy=1576520, CallSell=1276620, PutBuy=1738740, PutSell=2730220
    # BullFlow = 1576520 + 2730220 = 4,306,740
    # BearFlow = 1738740 + 1276620 = 3,015,360
    # Net Bullish Delta = +1,291,380!
    stopping_vol_bar = OHLCBar(
        18 * 60, 19 * 60, 74330.0, 74335.0, 74305.0, 74310.0,
        volume=157.0,
        call_buy_vol=1576520.0,
        call_sell_vol=1276620.0,
        put_buy_vol=1738740.0,
        put_sell_vol=2730220.0,
    )
    engine._on_1m_bar_close(stopping_vol_bar)
    engine.last_price = 74310.0
    engine.bars_5m[-1].open = 74360.0
    engine.bars_5m[-1].close = 74310.0

    # MUST BE VETOED!
    ok, reason, details = engine.check_topological_prominence_breakout("DOWN", current_vwap=74400.0)
    assert ok is False
    assert reason == "wyckoff_p4_stopping_volume_bullish_absorption"
    assert details["verdict"] == "STOPPING_VOLUME_BULLISH_PUT_WRITING_ABSORPTION"
    assert details["net_inst_delta"] > 1_000_000.0

    # 3. Now verify that when genuine institutional selling occurs (BearFlow >= 1.18 * BullFlow), it triggers!
    engine_gen = PriceActionEngine(max_history_bars=400)
    engine_gen.current_atr_1m = 40.0
    engine_gen._on_1m_bar_close(OHLCBar(0, 60, 74380.0, 74390.0, 74350.0, 74360.0, volume=100.0))
    engine_gen._on_1m_bar_close(OHLCBar(60, 120, 74360.0, 74370.0, 74323.0, 74330.0, volume=100.0))
    engine_gen._on_1m_bar_close(OHLCBar(120, 180, 74330.0, 74450.0, 74330.0, 74440.0, volume=100.0))
    for i in range(3, 18):
        engine_gen._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 74360.0, 74380.0, 74340.0, 74370.0, volume=100.0))

    genuine_bear_bar = OHLCBar(
        18 * 60, 19 * 60, 74330.0, 74335.0, 74300.0, 74305.0,
        volume=250.0,
        call_buy_vol=500000.0,
        call_sell_vol=1500000.0,
        put_buy_vol=3500000.0,
        put_sell_vol=600000.0,
    )
    engine_gen._on_1m_bar_close(genuine_bear_bar)
    engine_gen.last_price = 74305.0
    engine_gen.bars_5m[-1].close = 74305.0

    ok_gen, reason_gen, details_gen = engine_gen.check_topological_prominence_breakout("DOWN", current_vwap=74400.0)
    assert ok_gen is True
    assert reason_gen == "prominence_hub_breakout_down"
    assert details_gen["bear_ratio"] >= 1.18


def test_wyckoff_flow_gates_across_all_pillars():
    """
    Verifies that Pillar 1, 2, and 3 properly enforce Wyckoff Order Flow Delta gates.
    """
    # 1. Pillar 2 (Micro-Sweep Reclaim):
    # Form 5-bar swing low at 74,200.0
    engine_p2_a = PriceActionEngine()
    engine_p2_a.current_atr_1m = 40.0
    for i in range(5):
        engine_p2_a._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 74250.0, 74260.0, 74200.0 + i * 5.0, 74240.0, volume=100.0))

    # Candle dips to 74,185 (sweeping 74,200) and closes at 74,215 with lower wick >= 35% of range.
    # Total range = 74220 - 74185 = 35. lower_wick = 74205 - 74185 = 20 (20/35 = 57% >= 35%).
    # Case A: Heavy opposing bear supply (PutBuy 2000 vs CallBuy 500, bear_ratio >= 1.15) -> VETO!
    failed_spring_bar = OHLCBar(
        5 * 60, 6 * 60, 74205.0, 74220.0, 74185.0, 74215.0,
        volume=250.0,
        call_buy_vol=500.0,
        call_sell_vol=200.0,
        put_buy_vol=2000.0,
        put_sell_vol=100.0,
    )
    engine_p2_a._on_1m_bar_close(failed_spring_bar)
    engine_p2_a.last_price = 74215.0
    ok, reason, details = engine_p2_a.check_micro_sweep_reclaim("CALL", current_vwap=0.0)
    assert ok is False
    assert reason == "wyckoff_p2_failed_spring_heavy_supply"

    # Case B: Spring with institutional absorption (PutSell 1500, CallBuy 1000) -> PASS!
    engine_p2_b = PriceActionEngine()
    engine_p2_b.current_atr_1m = 40.0
    for i in range(5):
        engine_p2_b._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 74250.0, 74260.0, 74200.0 + i * 5.0, 74240.0, volume=100.0))

    spring_absorption_bar = OHLCBar(
        5 * 60, 6 * 60, 74205.0, 74220.0, 74185.0, 74215.0,
        volume=250.0,
        call_buy_vol=1000.0,
        call_sell_vol=200.0,
        put_buy_vol=300.0,
        put_sell_vol=1500.0,
    )
    engine_p2_b._on_1m_bar_close(spring_absorption_bar)
    engine_p2_b.last_price = 74215.0
    ok_abs, reason_abs, details_abs = engine_p2_b.check_micro_sweep_reclaim("CALL", current_vwap=0.0)
    assert ok_abs is True
    assert reason_abs == "micro_sweep_call_reclaim"
    assert details_abs["bull_ratio"] >= 1.0

    # 2. Pillar 3 (Volatility Squeeze Breakout):
    engine_p3 = PriceActionEngine()
    engine_p3.current_atr_1m = 40.0
    for i in range(25):
        engine_p3._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 74300.0, 74310.0, 74290.0, 74305.0, volume=100.0))

    # Trigger bar with opposing bear delta (bear_ratio >= 1.15) should be vetoed
    opposing_p3_bar = OHLCBar(
        25 * 60, 26 * 60, 74305.0, 74350.0, 74300.0, 74345.0,
        volume=300.0,
        call_buy_vol=500.0,
        call_sell_vol=2500.0,
        put_buy_vol=3000.0,
        put_sell_vol=200.0,
    )
    engine_p3._on_1m_bar_close(opposing_p3_bar)
    engine_p3.last_price = 74345.0
    engine_p3.squeeze_on = True
    ok_p3, reason_p3, _ = engine_p3.check_volatility_squeeze_breakout("CALL", current_vwap=74300.0)
    assert ok_p3 is False
    assert reason_p3 == "wyckoff_p3_call_negative_delta_trap"


def test_pillar3_climax_exhaustion_cap_and_box_displacement():
    """
    UPGRADE 3: Pillar 3 Climax Exhaustion Cap & Box Displacement Veto.
    Prevents chasing blowout / climax bars (> 1.35x ATR) or bars overextended from the box (> 1.20x ATR).
    """
    # 1. Climax Candle Range Veto (> 1.35x ATR)
    engine = PriceActionEngine()
    engine.current_atr_1m = 40.0
    for i in range(25):
        engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 74300.0, 74315.0, 74295.0, 74305.0, volume=100.0))

    # Box is ~74315 high to ~74295 low (range 20.0). ATR is ~20.0.
    # An 80-pt giant candle (like 22 Oct 09:50) far exceeds 1.35 * ATR (27 pts)!
    climax_bar = OHLCBar(
        25 * 60, 26 * 60, 74305.0, 74385.0, 74305.0, 74380.0,
        volume=300.0,
        call_buy_vol=2000.0,
        call_sell_vol=200.0,
        put_buy_vol=100.0,
        put_sell_vol=1000.0,
    )
    engine._on_1m_bar_close(climax_bar)
    engine.last_price = 74380.0
    engine.squeeze_on = True

    ok, reason, details = engine.check_volatility_squeeze_breakout("CALL", current_vwap=74300.0)
    assert ok is False
    assert reason == "exhausted_squeeze_breakout_candle_range"
    assert details["bar_range"] == 80.0

    # 2. Box Displacement Veto (> 1.20x ATR beyond box_high)
    # Candle range itself is moderate (25 pts <= 1.35 * ATR), but opened and closed way outside the box
    engine_disp = PriceActionEngine()
    engine_disp.current_atr_1m = 40.0
    for i in range(25):
        engine_disp._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 74300.0, 74310.0, 74290.0, 74305.0, volume=100.0))

    atr_val = engine_disp.current_atr_1m
    displaced_bar = OHLCBar(
        25 * 60, 26 * 60, 74340.0, 74355.0, 74338.0, 74350.0,
        volume=300.0,
        call_buy_vol=2000.0,
        call_sell_vol=200.0,
        put_buy_vol=100.0,
        put_sell_vol=1000.0,
    )
    engine_disp._on_1m_bar_close(displaced_bar)
    engine_disp.last_price = 74350.0
    engine_disp.squeeze_on = True

    ok_d, reason_d, details_d = engine_disp.check_volatility_squeeze_breakout("CALL", current_vwap=74300.0)
    assert ok_d is False
    assert reason_d == "overextended_from_squeeze_box"


def test_freak_preopen_settlement_spike_filtered_in_levels_and_runway():
    """
    UPGRADE 4: Fix 09:15 Freak Settlement Spike Masking 09:16 Resistance.
    Verifies:
    1. 09:15 settlement tick (near-zero range, huge outlier) is ignored, allowing 09:16 high to register as Resistance.
    2. _check_day_extreme_runway filters out the freak tick when calculating day_high and day_low.
    """
    engine = PriceActionEngine()
    engine.current_atr_1m = 40.0

    # Bar 0 (09:15 freak pre-open settlement tick: high=74400, low=74400, range=0, 1000 pts above market)
    engine._on_1m_bar_close(OHLCBar(0, 60, 74400.0, 74400.0, 74400.0, 74400.0, volume=10.0))
    # Bar 1 (09:16 real opening candle: open 73400, high 73423, low 73380, close 73410)
    engine._on_1m_bar_close(OHLCBar(60, 120, 73400.0, 73423.0, 73380.0, 73410.0, volume=150.0))
    # Bar 2 (09:17 subsequent candle: high 73405 < 73423, confirming 09:16 as Swing High!)
    engine._on_1m_bar_close(OHLCBar(120, 180, 73410.0, 73405.0, 73370.0, 73380.0, volume=120.0))

    # Resistance at 73,423.0 MUST be registered!
    resistances = [l for l in engine.session_levels if l.level_type == "RESISTANCE"]
    assert len(resistances) >= 1
    assert any(abs(r.price - 73423.0) < 2.0 for r in resistances)

    # _check_day_extreme_runway must NOT treat 74,400 as day_high
    engine.last_price = 73415.0
    engine.current_atr_1m = 40.0
    # Add another bar to have >= 3 intraday prior bars
    engine._on_1m_bar_close(OHLCBar(180, 240, 73380.0, 73415.0, 73375.0, 73415.0, volume=100.0))
    ok, reason, details = engine._check_day_extreme_runway("CALL", 73415.0)
    # Day high should be 73423.0, NOT 74400.0!
    # With day_high = 73423.0 and current_price = 73415.0, room to day high is only 8 pts (< 1.25x ATR of 50 pts) -> Insufficient runway!
    assert ok is False
    assert reason == "insufficient_runway_to_day_high"
    assert details["day_high"] == 73423.0
