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

    def test_20_ema_put_trapped_near_support_arms_bos_and_triggers_on_breakdown(self):
        """
        Verifies the Trade 2 protection:
        1. When a 20-EMA rejection forms within 0.60x ATR of a prior swing low,
           the trade is NOT taken prematurely into the support wall.
           Instead, it arms pending BOS ('awaiting_bos_breakdown').
        2. When a subsequent bar closes firmly BELOW that support level,
           the Break-of-Structure (BOS) confirms and triggers the trade.
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

        # Step 1: Must NOT enter immediately. Must arm BOS!
        ok, reason, details = engine.check_20_ema_rejection("DOWN")
        assert ok is False
        assert reason == "awaiting_bos_breakdown"
        assert engine._pending_bos is not None
        assert engine._pending_bos["direction"] == "PUT"
        assert engine._pending_bos["level"] == round(pivot_low_level, 2)

        # Step 2: Next bar (T+1) breaks down and closes firmly below the pivot low!
        breakdown_bar = OHLCBar(
            ts_start=1920.0, ts_end=1980.0,
            open=live_ema_20 - 3.0,
            high=live_ema_20 - 2.5,
            low=pivot_low_level - 3.0,
            close=pivot_low_level - 1.5,  # Closed below support level!
        )
        engine._on_1m_bar_close(breakdown_bar)
        engine.last_price = breakdown_bar.close

        # Step 3: Pending BOS resolves and fires the trade!
        ok_bos, reason_bos, details_bos = engine.check_20_ema_rejection("DOWN")
        assert ok_bos is True
        assert details_bos["rejection_type"] == "bos_breakdown_confirmation"
        assert details_bos["confirmation_path"] == "pending_bos_breakout"
        assert details_bos["runway_state"] == "bos_breakout"
        assert details_bos["broken_level"] == round(pivot_low_level, 2)
        # Pending BOS state cleared after trigger
        assert engine._pending_bos is None

    def test_20_ema_put_bos_invalidation_above_20_ema(self):
        """Verifies that if price surges back above 20 EMA, pending BOS is cleanly canceled."""
        engine = PriceActionEngine()
        for i in range(25):
            engine._on_1m_bar_close(OHLCBar(
                i * 60, (i + 1) * 60,
                24000.0 - i * 1.0, 24002.0 - i * 1.0, 23998.0 - i * 1.0, 24000.0 - i * 1.0
            ))

        live_ema_20, _ = engine.get_live_20_ema_and_slope()
        engine._on_1m_bar_close(OHLCBar(1500.0, 1560.0, live_ema_20 - 1.5, live_ema_20 + 0.1, live_ema_20 - 2.0, live_ema_20 - 1.0))
        live_ema_20, _ = engine.get_live_20_ema_and_slope()

        # Create pivot low
        engine._on_1m_bar_close(OHLCBar(1560.0, 1620.0, live_ema_20 - 1.5, live_ema_20 - 1.0, live_ema_20 - 3.0, live_ema_20 - 2.5))
        pivot_low = live_ema_20 - 5.0
        engine._on_1m_bar_close(OHLCBar(1620.0, 1680.0, live_ema_20 - 2.5, live_ema_20 - 2.0, pivot_low, live_ema_20 - 4.0))
        engine._on_1m_bar_close(OHLCBar(1680.0, 1740.0, live_ema_20 - 4.0, live_ema_20 - 1.5, live_ema_20 - 3.5, live_ema_20 - 2.0))
        # Retest bars fulfilling two-touch spacing
        engine._on_1m_bar_close(OHLCBar(1740.0, 1800.0, live_ema_20 - 2.0, live_ema_20 - 0.5, live_ema_20 - 2.5, live_ema_20 - 1.0))
        engine._on_1m_bar_close(OHLCBar(1800.0, 1860.0, live_ema_20 - 1.0, live_ema_20 + 0.2, live_ema_20 - 2.0, live_ema_20 - 1.0))

        live_ema_20, _ = engine.get_live_20_ema_and_slope()

        # Rejection bar near support
        rejection_bar = OHLCBar(1860.0, 1920.0, live_ema_20 - 1.5, live_ema_20 + 0.2, live_ema_20 - 3.2, live_ema_20 - 3.0)
        engine._on_1m_bar_close(rejection_bar)
        engine.last_price = rejection_bar.close

        ok, _, _ = engine.check_20_ema_rejection("DOWN")
        assert ok is False
        assert engine._pending_bos is not None

        # Next bar blows off HIGHER, closing above 20-EMA
        invalid_bar = OHLCBar(1920.0, 1980.0, live_ema_20 - 2.0, live_ema_20 + 10.0, live_ema_20 - 2.5, live_ema_20 + 8.0)
        engine._on_1m_bar_close(invalid_bar)
        engine.last_price = invalid_bar.close

        ok_inv, reason_inv, _ = engine.check_20_ema_rejection("DOWN")
        assert ok_inv is False
        # Cleanly invalidated and canceled!
        assert engine._pending_bos is None

    def test_20_ema_call_trapped_near_resistance_arms_bos_and_triggers_on_breakout(self):
        """
        Verifies CALL direction BOS protection:
        1. 20-EMA rejection forming within 0.60x ATR below prior swing high arms BOS.
        2. Candle closing above the swing high confirms BOS and triggers CALL.
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

        # Must arm pending BOS
        ok, reason, details = engine.check_20_ema_rejection("UP")
        assert ok is False
        assert reason == "awaiting_bos_breakout"
        assert engine._pending_bos is not None
        assert engine._pending_bos["direction"] == "CALL"

        # Next bar breaks out above resistance
        breakout_bar = OHLCBar(
            ts_start=1920.0, ts_end=1980.0,
            open=live_ema_20 + 3.0,
            high=pivot_high_level + 3.0,
            low=live_ema_20 + 2.5,
            close=pivot_high_level + 1.5,
        )
        engine._on_1m_bar_close(breakout_bar)
        engine.last_price = breakout_bar.close

        ok_bos, reason_bos, details_bos = engine.check_20_ema_rejection("UP")
        assert ok_bos is True
        assert details_bos["rejection_type"] == "bos_breakout_confirmation"
        assert details_bos["confirmation_path"] == "pending_bos_breakout"
        assert details_bos["runway_state"] == "bos_breakout"
        assert engine._pending_bos is None

    def test_20_ema_call_weak_breakout_discarded_no_momentum(self):
        """
        Weak break = no momentum = discard (2026-09-16):
        Breakout bar closes above resistance but with a tiny body and a
        mid-range close (wicky chop like the 12:43 SENSEX bar). The pending
        BOS must be cleared WITHOUT firing so a genuine retest can arrive
        later via a fresh signal.
        """
        engine = PriceActionEngine()
        for i in range(25):
            engine._on_1m_bar_close(OHLCBar(
                i * 60, (i + 1) * 60,
                24000.0 + i * 1.0, 24002.0 + i * 1.0, 23998.0 + i * 1.0, 24000.0 + i * 1.0
            ))

        live_ema_20, _ = engine.get_live_20_ema_and_slope()
        engine._on_1m_bar_close(OHLCBar(1500.0, 1560.0, live_ema_20 + 1.5, live_ema_20 + 2.0, live_ema_20 - 0.1, live_ema_20 + 1.0))
        live_ema_20, _ = engine.get_live_20_ema_and_slope()

        engine._on_1m_bar_close(OHLCBar(1560.0, 1620.0, live_ema_20 + 1.5, live_ema_20 + 3.0, live_ema_20 + 1.0, live_ema_20 + 2.5))
        pivot_high_level = live_ema_20 + 5.0
        engine._on_1m_bar_close(OHLCBar(1620.0, 1680.0, live_ema_20 + 2.5, pivot_high_level, live_ema_20 + 2.0, live_ema_20 + 4.0))
        engine._on_1m_bar_close(OHLCBar(1680.0, 1740.0, live_ema_20 + 4.0, live_ema_20 + 3.5, live_ema_20 + 1.5, live_ema_20 + 2.0))
        engine._on_1m_bar_close(OHLCBar(1740.0, 1800.0, live_ema_20 + 2.0, live_ema_20 + 2.5, live_ema_20 + 0.5, live_ema_20 + 1.0))
        engine._on_1m_bar_close(OHLCBar(1800.0, 1860.0, live_ema_20 + 1.0, live_ema_20 + 2.0, live_ema_20 - 0.2, live_ema_20 + 1.0))

        live_ema_20, _ = engine.get_live_20_ema_and_slope()
        rejection_bar = OHLCBar(
            ts_start=1860.0, ts_end=1920.0,
            open=live_ema_20 + 1.5,
            high=live_ema_20 + 3.2,
            low=live_ema_20 - 0.2,
            close=live_ema_20 + 3.0,
        )
        engine._on_1m_bar_close(rejection_bar)
        engine.last_price = rejection_bar.close

        ok, reason, _ = engine.check_20_ema_rejection("UP")
        assert ok is False
        assert reason == "awaiting_bos_breakout"
        assert engine._pending_bos is not None

        # Weak breakout: closes a hair above resistance but body is tiny
        # and close sits mid-range (long upper wick, no momentum).
        weak_breakout_bar = OHLCBar(
            ts_start=1920.0, ts_end=1980.0,
            open=pivot_high_level - 1.5,
            high=pivot_high_level + 2.5,
            low=pivot_high_level - 2.0,
            close=pivot_high_level + 0.3,
        )
        engine._on_1m_bar_close(weak_breakout_bar)
        engine.last_price = weak_breakout_bar.close

        ok_weak, reason_weak, _ = engine.check_20_ema_rejection("UP")
        assert ok_weak is False
        assert reason_weak == "bos_weak_breakout_discarded"
        assert engine._pending_bos is None

    def test_20_ema_put_weak_breakdown_discarded_no_momentum(self):
        """
        PUT mirror of the weak-break discard rule: breakdown bar closes
        below support but with a tiny body and mid-range close.
        """
        engine = PriceActionEngine()
        for i in range(25):
            engine._on_1m_bar_close(OHLCBar(
                i * 60, (i + 1) * 60,
                24000.0 - i * 1.0, 24002.0 - i * 1.0, 23998.0 - i * 1.0, 24000.0 - i * 1.0
            ))

        live_ema_20, _ = engine.get_live_20_ema_and_slope()
        engine._on_1m_bar_close(OHLCBar(1500.0, 1560.0, live_ema_20 - 1.5, live_ema_20 + 0.1, live_ema_20 - 2.0, live_ema_20 - 1.0))
        live_ema_20, _ = engine.get_live_20_ema_and_slope()

        engine._on_1m_bar_close(OHLCBar(1560.0, 1620.0, live_ema_20 - 1.5, live_ema_20 - 1.0, live_ema_20 - 3.0, live_ema_20 - 2.5))
        pivot_low_level = live_ema_20 - 5.0
        engine._on_1m_bar_close(OHLCBar(1620.0, 1680.0, live_ema_20 - 2.5, live_ema_20 - 2.0, pivot_low_level, live_ema_20 - 4.0))
        engine._on_1m_bar_close(OHLCBar(1680.0, 1740.0, live_ema_20 - 4.0, live_ema_20 - 1.5, live_ema_20 - 3.5, live_ema_20 - 2.0))
        engine._on_1m_bar_close(OHLCBar(1740.0, 1800.0, live_ema_20 - 2.0, live_ema_20 - 0.5, live_ema_20 - 2.5, live_ema_20 - 1.0))
        engine._on_1m_bar_close(OHLCBar(1800.0, 1860.0, live_ema_20 - 1.0, live_ema_20 + 0.2, live_ema_20 - 2.0, live_ema_20 - 1.0))

        live_ema_20, _ = engine.get_live_20_ema_and_slope()
        rejection_bar = OHLCBar(
            ts_start=1860.0, ts_end=1920.0,
            open=live_ema_20 - 1.5,
            high=live_ema_20 + 0.2,
            low=live_ema_20 - 3.2,
            close=live_ema_20 - 3.0,
        )
        engine._on_1m_bar_close(rejection_bar)
        engine.last_price = rejection_bar.close

        ok, reason, _ = engine.check_20_ema_rejection("DOWN")
        assert ok is False
        assert reason == "awaiting_bos_breakdown"
        assert engine._pending_bos is not None

        # Weak breakdown: closes a hair below support, tiny body, mid-range.
        weak_breakdown_bar = OHLCBar(
            ts_start=1920.0, ts_end=1980.0,
            open=pivot_low_level + 1.5,
            high=pivot_low_level + 2.0,
            low=pivot_low_level - 2.5,
            close=pivot_low_level - 0.3,
        )
        engine._on_1m_bar_close(weak_breakdown_bar)
        engine.last_price = weak_breakdown_bar.close

        ok_weak, reason_weak, _ = engine.check_20_ema_rejection("DOWN")
        assert ok_weak is False
        assert reason_weak == "bos_weak_breakdown_discarded"
        assert engine._pending_bos is None




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
        # Seed 25 bars in uptrend
        for i in range(25):
            engine._on_1m_bar_close(OHLCBar(i * 60, (i + 1) * 60, 24000.0 + i * 1.0, 24003.0 + i * 1.0, 23999.0 + i * 1.0, 24001.0 + i * 1.0))

        # Prior 5 bars have swing low at 24020.0
        for i in range(5):
            engine._on_1m_bar_close(OHLCBar((25 + i) * 60, (26 + i) * 60, 24022.0, 24026.0, 24020.0, 24024.0))

        # Sweep bar: dips to 24017.0 (< 24020.0), but closes at 24025.0 with long lower wick (8 pts / 10 pts = 80%)
        sweep_bar = OHLCBar(1860, 1920, 24021.0, 24027.0, 24017.0, 24025.0)
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

        # Sweep bar: pops to 23982.0 (> 23978.0), but closes at 23973.0 with long upper wick
        sweep_bar = OHLCBar(1860, 1920, 23977.0, 23982.0, 23971.0, 23973.0)
        engine._on_1m_bar_close(sweep_bar)
        engine.last_price = sweep_bar.close

        current_vwap = 23995.0  # Safe below VWAP
        is_sweep, reason, details = engine.check_micro_sweep_reclaim("DOWN", current_vwap)
        assert is_sweep is True
        assert details["pillar"] == 2
        assert details["pa_setup_name"] == "micro_sweep_reclaim"
        assert details["rejection_type"] == "liquidity_sweep_put_reclaim"
        assert details["sl_price"] >= 23982.0 + 5.0


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
        # Force a retest candle with hammer rejection off 20 EMA
        test_bar = OHLCBar(3000.0, 3060.0, 24000.0, 24001.0, 23999.5, 24000.5)
        engine._on_1m_bar_close(test_bar)

        ok, reason, details = engine.check_20_ema_rejection("UP")
        assert ok is True
        assert details.get("pillar") == 1

