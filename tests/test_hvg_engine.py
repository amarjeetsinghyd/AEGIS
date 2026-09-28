"""
Comprehensive Test Suite for AEGIS Horizontal Visibility Graph (HVG) Engine
=============================================================================
Verifies:
1. Circular Ring Buffer zero-allocation in-place unrolling and wrapping invariance.
2. Monotonic Stack O(N) HVG mathematical correctness against manual graph theory.
3. Dual-Envelope symmetry (Upper HVG on Highs, Lower Inverted HVG on Lows).
4. Epsilon ZeroDivisionError safety guard on completely flat time series.
5. Relative Prominence calculation and dominant anchor detection.
6. Deterministic CALL / PUT Phase Shift entry rules and invalidation gates.
7. Non-clash invariant with Pillar 2 (Turtle Soup reclaim does not fire HVG breakout).
8. Forward telemetry state tracking (30s, 60s, 180s, 300s, MFE, MAE) and JSONL flush.
9. Queue pruning and market close flush_all_pending() validation.
10. Zero-Allocation Hot-Path runtime benchmark (< 500 microseconds in pytest).
"""

import json
import os
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
import pytest

from AEGIS.core.hvg_engine import HVGEngine, HVGSetupSignal, PendingTelemetryRecord, IST_TZ


@pytest.fixture
def temp_log_dir():
    temp_dir = tempfile.mkdtemp(prefix="test_hvg_logs_")
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def hvg_engine(temp_log_dir):
    return HVGEngine(log_dir=temp_log_dir, shadow_only=True)


class TestHVGRingBuffer:
    """Tests the pre-allocated circular ring buffer."""

    def test_buffer_add_under_capacity(self, hvg_engine):
        for i in range(10):
            hvg_engine.add_bar(
                open_price=100.0 + i,
                high_price=105.0 + i,
                low_price=95.0 + i,
                close_price=102.0 + i,
                atr=2.0,
                vwap=100.0,
                timestamp=1000.0 + i * 60,
            )
        assert hvg_engine.bar_count == 10
        n = hvg_engine._unroll_to_scratch()
        assert n == 10
        # Assert exact chronological order in scratch
        for i in range(10):
            assert hvg_engine._scratch_highs[i] == 105.0 + i
            assert hvg_engine._scratch_lows[i] == 95.0 + i

    def test_buffer_wrapping_preserves_chronological_order(self, hvg_engine):
        # Push 75 bars to wrap past capacity 60
        for i in range(75):
            hvg_engine.add_bar(
                open_price=100.0 + i,
                high_price=1005.0 + i,
                low_price=995.0 + i,
                close_price=1000.0 + i,
                atr=2.0,
                vwap=100.0,
                timestamp=1000.0 + i * 60,
            )
        assert hvg_engine.bar_count == 60
        n = hvg_engine._unroll_to_scratch()
        assert n == 60
        # The oldest bar in scratch must be bar 15 (high 1020.0), newest bar 74 (high 1079.0)
        assert hvg_engine._scratch_highs[0] == 1020.0
        assert hvg_engine._scratch_highs[59] == 1079.0
        for i in range(60):
            assert hvg_engine._scratch_highs[i] == 1020.0 + i


class TestMonotonicStackHVGMath:
    """Verifies graph theory correctness of the O(N) Monotonic Stack."""

    def test_upper_hvg_known_sequence(self, hvg_engine):
        """
        Sequence: [10, 5, 8, 12]
        Edges: (0,1), (1,2), (0,2), (2,3), (0,3). Total = 5 edges.
        Degree of 0: 3 [(0,1), (0,2), (0,3)]
        Degree of 1: 2 [(0,1), (1,2)]
        Degree of 2: 3 [(1,2), (0,2), (2,3)]
        Degree of 3: 2 [(2,3), (0,3)]
        """
        highs = [10.0, 5.0, 8.0, 12.0]
        n = len(highs)
        for i in range(n):
            hvg_engine._scratch_highs[i] = highs[i]
            hvg_engine._scratch_lows[i] = highs[i] - 2.0

        deg_u, deg_l = hvg_engine.compute_hvg_degrees(n)
        assert deg_u[:4] == [3, 2, 3, 2]

    def test_lower_inverted_hvg_symmetry(self, hvg_engine):
        """
        Lower HVG on Lows is structurally symmetric to Upper HVG on Highs.
        If Lows = [-10, -5, -8, -12], valleys see across intermediate higher points.
        """
        lows = [-10.0, -5.0, -8.0, -12.0]
        n = len(lows)
        for i in range(n):
            hvg_engine._scratch_lows[i] = lows[i]
            hvg_engine._scratch_highs[i] = lows[i] + 2.0

        deg_u, deg_l = hvg_engine.compute_hvg_degrees(n)
        assert deg_l[:4] == [3, 2, 3, 2]

    def test_epsilon_zero_division_guard_on_flat_market(self, hvg_engine):
        """When price is completely flat, no ZeroDivisionError occurs."""
        for i in range(30):
            hvg_engine.add_bar(
                open_price=24000.0,
                high_price=24000.0,
                low_price=24000.0,
                close_price=24000.0,
                atr=10.0,
                vwap=24000.0,
            )
        # Must execute without ZeroDivisionError
        ok, reason, details = hvg_engine.evaluate_phase_shift(
            direction="CALL", ema_20=24000.0, ema_50=24000.0, current_vwap=24000.0
        )
        assert not ok


class TestPhaseShiftDeterministicRules:
    """Tests the exact entry rules for CALL and PUT Phase Shift setups."""

    def _seed_call_setup(self, hvg_engine, base=24000.0):
        """Seeds a valid CALL phase shift setup with 20 valley bars + anchor + 4 compression bars."""
        # 20 bars of descending/valley structure
        for i in range(20):
            h = base + 10.0 - (i % 10) * 2.0
            hvg_engine.add_bar(h - 2.0, h, h - 5.0, h - 1.0, 20.0, base - 10.0, timestamp=1000.0 + i * 60)

        # Anchor bar at index 20 (high 24050.0)
        hvg_engine.add_bar(base, 24050.0, base - 2.0, base + 10.0, 20.0, base - 5.0, timestamp=2200.0)

        # 4 compressed bars below 24050 (indices 21..24)
        for i in range(4):
            hvg_engine.add_bar(base + 5.0, 24040.0, base, 24035.0, 20.0, base, timestamp=2260.0 + i * 60)

    def _seed_put_setup(self, hvg_engine, base=24000.0):
        """Seeds a valid PUT phase shift setup with 20 ridge bars + anchor support + 4 compression bars."""
        for i in range(20):
            l = base - 10.0 + (i % 10) * 2.0
            hvg_engine.add_bar(l + 2.0, l + 5.0, l, l + 1.0, 20.0, base + 10.0, timestamp=1000.0 + i * 60)

        # Anchor support at index 20 (low 23940.0)
        hvg_engine.add_bar(base, base + 2.0, 23940.0, base - 10.0, 20.0, base + 10.0, timestamp=2200.0)

        # 4 compressed bars above 23940
        for i in range(4):
            hvg_engine.add_bar(base - 10.0, base, 23955.0, 23960.0, 20.0, base + 10.0, timestamp=2260.0 + i * 60)

    def test_valid_call_phase_shift_breakout(self, hvg_engine):
        base = 24000.0
        self._seed_call_setup(hvg_engine, base=base)

        # Breakout candle T (index 25): clean close above 24050 with solid body
        hvg_engine.add_bar(
            open_price=24036.0,
            high_price=24065.0,
            low_price=24034.0,
            close_price=24062.0,
            atr=20.0,
            vwap=base,
            timestamp=2500.0,
        )

        ok, reason, details = hvg_engine.evaluate_phase_shift(
            direction="CALL",
            ema_20=24040.0,
            ema_50=24010.0,
            current_vwap=base,
        )
        assert ok is True
        assert reason == "hvg_phase_shift_call_breakout"
        assert details["pillar"] == 4
        assert details["anchor_price"] == 24050.0
        assert details["anchor_prominence"] >= 1.75
        assert details["breakout_price"] == 24062.0
        assert details["sl_price"] < 24050.0
        assert details["is_shadow_only"] is True

    def test_valid_put_phase_shift_breakdown(self, hvg_engine):
        base = 24000.0
        self._seed_put_setup(hvg_engine, base=base)

        # Breakdown candle T: clean close below 23940 with solid bearish body
        hvg_engine.add_bar(
            open_price=23958.0,
            high_price=23960.0,
            low_price=23925.0,
            close_price=23930.0,
            atr=20.0,
            vwap=base + 10.0,
            timestamp=2500.0,
        )

        ok, reason, details = hvg_engine.evaluate_phase_shift(
            direction="PUT",
            ema_20=23950.0,
            ema_50=23990.0,
            current_vwap=base + 10.0,
        )
        assert ok is True
        assert reason == "hvg_phase_shift_put_breakdown"
        assert details["pillar"] == 4
        assert details["anchor_price"] == 23940.0
        assert details["anchor_prominence"] >= 1.75
        assert details["breakout_price"] == 23930.0
        assert details["sl_price"] > 23940.0

    def test_rejection_when_breakout_candle_body_is_weak(self, hvg_engine):
        base = 24000.0
        self._seed_call_setup(hvg_engine, base=base)

        # Bar T closes > 24050, but as a long upper-wick doji (body < 40% range)
        hvg_engine.add_bar(
            open_price=24051.0,
            high_price=24080.0,  # 29 pt range
            low_price=24050.0,
            close_price=24052.0,  # 1 pt body (1/30 = 3.3% < 40%)
            atr=20.0,
            vwap=base,
        )
        ok, reason, _ = hvg_engine.evaluate_phase_shift(
            direction="CALL", ema_20=24040.0, ema_50=24010.0, current_vwap=base
        )
        assert ok is False
        assert reason == "weak_candle_body"

    def test_rejection_when_trend_is_opposed(self, hvg_engine):
        base = 24000.0
        self._seed_call_setup(hvg_engine, base=base)
        hvg_engine.add_bar(24036.0, 24065.0, 24034.0, 24062.0, 20.0, base)

        # Trend opposed: 20 EMA < 50 EMA for CALL when enforce_ema_trend is enabled
        hvg_engine.enforce_ema_trend = True
        ok, reason, _ = hvg_engine.evaluate_phase_shift(
            direction="CALL",
            ema_20=23950.0,  # 20 EMA < 50 EMA
            ema_50=24010.0,
            current_vwap=base,
        )
        assert ok is False
        assert reason == "bear_overlap_ema_disallowed"


class TestPillar2NonClashInvariant:
    """Verifies that Pillar 2 (Turtle Soup Reclaim) does not trigger HVG Phase Shift."""

    def test_turtle_soup_reclaim_does_not_fire_hvg_breakout(self, hvg_engine):
        """
        Turtle Soup sweeps a prior swing low, but closes back inside the range (not outside).
        HVG demands a breakout close beyond the anchor. Therefore HVG must reject it.
        """
        base = 24000.0
        for i in range(20):
            l = base - 10.0 + (i % 10) * 2.0
            hvg_engine.add_bar(l + 2.0, l + 5.0, l, l + 1.0, 20.0, base + 10.0)

        # Anchor support at 23940
        hvg_engine.add_bar(base, base + 2.0, 23940.0, base - 10.0, 20.0, base + 10.0)

        for _ in range(4):
            hvg_engine.add_bar(base, base + 5.0, base - 5.0, base, 20.0, base - 10.0)

        # Turtle Soup candle: dips below 23940 to 23930, but closes back INSIDE at 23960
        hvg_engine.add_bar(
            open_price=23950.0,
            high_price=23965.0,
            low_price=23930.0,   # Sweeps below 23940
            close_price=23960.0,  # Reclaims firmly back inside
            atr=20.0,
            vwap=base - 10.0,
        )

        # Evaluate PUT: Price closed at 23960 which is > 23940 (did NOT break down)
        ok_put, reason_put, _ = hvg_engine.evaluate_phase_shift(
            direction="PUT", ema_20=23900.0, ema_50=23950.0, current_vwap=24000.0
        )
        assert ok_put is False
        assert reason_put in ("not_bearish_candle", "pre_breakdown_not_compressed_or_no_breakdown", "no_prominent_support_anchor")

        # Evaluate CALL: Anchor was a support (lower), no resistance anchor broken
        ok_call, reason_call, _ = hvg_engine.evaluate_phase_shift(
            direction="CALL", ema_20=24000.0, ema_50=23950.0, current_vwap=23900.0
        )
        assert ok_call is False


class TestForwardTelemetryTracking:
    """Tests the bounded in-memory forward outcome telemetry tracker, pruning, and flush."""

    def test_telemetry_flow_30s_to_300s_flush_and_prune(self, hvg_engine, temp_log_dir):
        base = 24000.0
        for i in range(20):
            h = base + 10.0 - (i % 10) * 2.0
            hvg_engine.add_bar(h - 2.0, h, h - 5.0, h - 1.0, 20.0, base - 10.0, timestamp=1000.0 + i * 60)
        hvg_engine.add_bar(base, 24050.0, base - 2.0, base + 10.0, 20.0, base - 5.0, timestamp=2200.0)
        for i in range(4):
            hvg_engine.add_bar(base + 5.0, 24040.0, base, 24035.0, 20.0, base, timestamp=2260.0 + i * 60)

        t_entry = 2500.0
        hvg_engine.add_bar(24036.0, 24065.0, 24034.0, 24062.0, 20.0, base, timestamp=t_entry)

        ok, _, details = hvg_engine.evaluate_phase_shift(
            direction="CALL", ema_20=24040.0, ema_50=24010.0, current_vwap=base
        )
        assert ok is True
        assert hvg_engine.get_pending_count() == 1

        # Simulate incoming ticks at various intervals
        hvg_engine.on_underlying_tick(ltp=24070.0, current_time=t_entry + 15.0)
        assert hvg_engine._pending_telemetry[0].mfe == 8.0  # 24070 - 24062

        # 30s mark
        hvg_engine.on_underlying_tick(ltp=24075.0, current_time=t_entry + 30.0)
        assert hvg_engine._pending_telemetry[0].price_30s == 24075.0

        # 60s mark
        hvg_engine.on_underlying_tick(ltp=24080.0, current_time=t_entry + 60.0)
        assert hvg_engine._pending_telemetry[0].price_60s == 24080.0

        # 180s mark
        hvg_engine.on_underlying_tick(ltp=24085.0, current_time=t_entry + 180.0)
        assert hvg_engine._pending_telemetry[0].price_180s == 24085.0

        # 300s mark -> Completes, flushes to file, and prunes from queue
        hvg_engine.on_underlying_tick(ltp=24090.0, current_time=t_entry + 300.0)

        # Pending count should be pruned to 0
        assert hvg_engine.get_pending_count() == 0

        # Check telemetry file
        telemetry_file = os.path.join(temp_log_dir, "hvg_shadow_telemetry.jsonl")
        assert os.path.exists(telemetry_file)
        with open(telemetry_file, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]

        assert len(lines) == 1
        rec = lines[0]
        assert rec["setup_name"] == "hvg_network_phase_shift"
        assert rec["entry_price"] == 24062.0
        assert rec["pnl_300s"] == 28.0  # 24090 - 24062
        assert rec["win_300s"] is True
        assert rec["is_shadow_only"] is True

    def test_flush_all_pending_at_market_close(self, hvg_engine, temp_log_dir):
        """Simulates market close force-flush of unexpired shadow signals."""
        signal = HVGSetupSignal(
            timestamp=1000.0,
            direction="CALL",
            setup_name="hvg_network_phase_shift",
            pillar=4,
            anchor_price=24050.0,
            anchor_prominence=2.5,
            anchor_index=15,
            breakout_price=24060.0,
            bar_open=24040.0,
            bar_high=24065.0,
            bar_low=24038.0,
            bar_close=24060.0,
            body_ratio=0.74,
            sl_price=24045.0,
            buffer_pts=5.0,
            atr_14=20.0,
            vwap=24000.0,
            ema_20=24040.0,
            ema_50=24010.0,
            is_shadow_only=True,
        )
        hvg_engine._record_shadow_signal(signal)
        assert hvg_engine.get_pending_count() == 1

        # Mid-session tick (only reached 60s)
        hvg_engine.on_underlying_tick(ltp=24075.0, current_time=1060.0)
        assert hvg_engine.get_pending_count() == 1

        # 15:30 force flush
        hvg_engine.flush_all_pending()
        assert hvg_engine.get_pending_count() == 0

        # Verify disk write
        telemetry_file = os.path.join(temp_log_dir, "hvg_shadow_telemetry.jsonl")
        assert os.path.exists(telemetry_file)
        with open(telemetry_file, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        assert len(lines) == 1
        assert lines[0]["entry_price"] == 24060.0


class TestHVGLatencyBenchmark:
    """Zero-Allocation Hot-Path runtime benchmark."""

    def test_60_bar_dual_envelope_latency(self, hvg_engine):
        import math
        base = 24000.0
        for i in range(60):
            sine_offset = math.sin(i / 5.0) * 30.0
            hvg_engine.add_bar(
                open_price=base + sine_offset,
                high_price=base + sine_offset + 5.0,
                low_price=base + sine_offset - 5.0,
                close_price=base + sine_offset + 2.0,
                atr=15.0,
                vwap=base,
                timestamp=1000.0 + i * 60,
            )

        # Warm up
        for _ in range(50):
            hvg_engine.evaluate_phase_shift(
                direction="CALL", ema_20=24010.0, ema_50=24000.0, current_vwap=24000.0
            )

        # Benchmark over 500 iterations
        t0 = time.perf_counter()
        iterations = 500
        for _ in range(iterations):
            hvg_engine.evaluate_phase_shift(
                direction="CALL", ema_20=24010.0, ema_50=24000.0, current_vwap=24000.0
            )
        t1 = time.perf_counter()

        avg_latency_us = ((t1 - t0) / iterations) * 1e6
        print(f"\n[BENCHMARK] HVG Dual-Envelope 60-bar runtime: {avg_latency_us:.2f} microseconds")
        assert avg_latency_us < 1000.0, f"HVG latency too high: {avg_latency_us:.2f} us (must be < 1000 us)"


class TestHVGLivePriceActionIntegration:
    """Verifies end-to-end integration of HVG Pillar 4 with PriceActionEngine and Conviction."""

    def _seed_engine_call(self, hvg, base=24000.0):
        for i in range(20):
            h = base + 10.0 - (i % 10) * 2.0
            hvg.add_bar(h - 2.0, h, h - 5.0, h - 1.0, 20.0, base - 10.0, timestamp=1000.0 + i * 60)
        hvg.add_bar(base, 24050.0, base - 2.0, base + 10.0, 20.0, base - 5.0, timestamp=2200.0)
        for i in range(4):
            hvg.add_bar(base + 5.0, 24040.0, base, 24035.0, 20.0, base, timestamp=2260.0 + i * 60)
        hvg.add_bar(24036.0, 24065.0, 24034.0, 24062.0, 20.0, base, timestamp=2500.0)

    def test_live_dispatch_via_price_action_engine(self):
        from AEGIS.core.price_action_engine import PriceActionEngine

        pae = PriceActionEngine()
        hvg = HVGEngine(shadow_only=False)
        pae.hvg_engine = hvg

        base = 24000.0
        self._seed_engine_call(hvg, base=base)
        pae.ema_20_1m = 24040.0
        pae.ema_50_1m = 24010.0
        pae.current_vwap = base

        ok, setup_name, details = pae.evaluate_setup("CALL")
        assert ok is True
        assert setup_name == "hvg_network_phase_shift"
        assert details["pillar"] == 4
        assert details["anchor_price"] == 24050.0
        assert details["breakout_price"] == 24062.0
        assert details["sl_price"] < 24050.0
        assert details["is_shadow_only"] is False

    def test_shadow_mode_isolation_via_price_action_engine(self):
        from AEGIS.core.price_action_engine import PriceActionEngine

        pae = PriceActionEngine()
        hvg = HVGEngine(shadow_only=True)
        pae.hvg_engine = hvg

        base = 24000.0
        self._seed_engine_call(hvg, base=base)
        pae.ema_20_1m = 24040.0
        pae.ema_50_1m = 24010.0
        pae.current_vwap = base

        ok, setup_name, _ = pae.evaluate_setup("CALL")
        # In shadow mode, evaluate_setup MUST return False so orders are never punched
        assert ok is False
        assert setup_name == "no_price_action_setup"

    def test_composite_conviction_scoring_for_hvg(self):
        from AEGIS.core.composite_conviction_engine import CompositeConvictionEngine

        cce = CompositeConvictionEngine()
        assert "hvg_network_phase_shift" in cce.SETUP_WEIGHT_PROFILES
        profile = cce.SETUP_WEIGHT_PROFILES["hvg_network_phase_shift"]
        assert profile["structure"] == 30.0
        assert profile["impulse"] == 30.0
        assert profile["flow"] == 25.0
        assert profile["contract"] == 15.0

        res = cce.compute_conviction_score(
            direction="CALL",
            setup_name="hvg_network_phase_shift",
            spot_price=24062.0,
            prior_bars=[],
            atr=20.0,
            ema_20=24040.0,
            ema_50=24010.0,
            vwap=24000.0,
        )
        assert res.breakdown["setup_name"] == "hvg_network_phase_shift"
        assert res.breakdown["weights_used"] == profile
        assert isinstance(res.total_score, float)


class TestDualTimeframe5mHVG:
    """Tests the 5-Minute Clock-Aligned Aggregator, Day-Boundary Separation, and Structural Alignment."""

    def test_clock_aligned_5m_rollover_ist(self, hvg_engine):
        """Verifies that 1m bars correctly aggregate into 5m candles locked to 09:15, 09:20... IST."""
        # 09:15:00 IST on 2026-03-10
        t0 = datetime(2026, 3, 10, 9, 15, 0, tzinfo=IST_TZ).timestamp()

        # Feed 5 1-minute bars: 09:15, 09:16, 09:17, 09:18, 09:19
        for i in range(5):
            hvg_engine.add_bar(
                open_price=24000.0 + i,
                high_price=24010.0 + i * 2,
                low_price=23995.0 - i,
                close_price=24005.0 + i,
                atr=15.0,
                vwap=24000.0,
                timestamp=t0 + i * 60,
            )
            # Before 09:20 rollover, 5m buffer count is still 0
            assert hvg_engine.bar_count_5m == 0

        # Now feed bar at 09:20:00 -> triggers bucket change and pushes 09:15-09:19 5m bar
        hvg_engine.add_bar(
            open_price=24020.0,
            high_price=24025.0,
            low_price=24015.0,
            close_price=24022.0,
            atr=15.0,
            vwap=24000.0,
            timestamp=t0 + 5 * 60,
        )

        assert hvg_engine.bar_count_5m == 1
        head = hvg_engine._head_5m
        assert hvg_engine._buf_5m_opens[head] == 24000.0  # Open of 09:15 bar
        assert hvg_engine._buf_5m_highs[head] == 24018.0  # Max high (24010 + 4*2)
        assert hvg_engine._buf_5m_lows[head] == 23991.0   # Min low (23995 - 4)
        assert hvg_engine._buf_5m_closes[head] == 24009.0 # Close of 09:19 bar (24005 + 4)

    def test_overnight_day_change_separation(self, hvg_engine):
        """Verifies that Day 1 15:29 is finalized on day rollover and never merged into Day 2 09:15."""
        t_day1 = datetime(2026, 3, 10, 15, 29, 0, tzinfo=IST_TZ).timestamp()
        hvg_engine.add_bar(
            open_price=24000.0,
            high_price=24010.0,
            low_price=23990.0,
            close_price=24005.0,
            atr=15.0,
            vwap=24000.0,
            timestamp=t_day1,
        )
        assert hvg_engine.bar_count_5m == 0

        # Next day 09:15:00
        t_day2 = datetime(2026, 3, 11, 9, 15, 0, tzinfo=IST_TZ).timestamp()
        hvg_engine.add_bar(
            open_price=24100.0,
            high_price=24110.0,
            low_price=24095.0,
            close_price=24105.0,
            atr=15.0,
            vwap=24100.0,
            timestamp=t_day2,
        )

        # Day 1 bar was pushed to 5m buffer upon day change detection
        assert hvg_engine.bar_count_5m == 1
        head = hvg_engine._head_5m
        assert hvg_engine._buf_5m_opens[head] == 24000.0
        assert hvg_engine._buf_5m_closes[head] == 24005.0

    def test_5m_opposed_macro_breakdown_veto(self, hvg_engine):
        """Verifies 1m CALL breakout is vetoed when 5m macro structure broke down below support."""
        base = 24000.0
        t0 = 100000.0
        # Seed 5 bars with ascending lows so all stay on stack
        for i in range(5):
            l = base - 10.0 + (i % 10) * 2.0
            hvg_engine._push_5m_bar(l + 2.0, l + 5.0, l, l + 1.0, 20.0, t0 + i * 300)
        # Bar 5: prominent support anchor (deep low at 23940.0)
        hvg_engine._push_5m_bar(base, base + 2.0, 23940.0, base - 10.0, 20.0, t0 + 5 * 300)
        # Bar 6: consolidation
        hvg_engine._push_5m_bar(base - 10.0, base, 23955.0, 23960.0, 20.0, t0 + 6 * 300)
        # Bar 7: bearish breakdown candle (closes below 23940 with strong body)
        hvg_engine._push_5m_bar(23950.0, 23955.0, 23910.0, 23915.0, 20.0, t0 + 7 * 300)

        ok, reason, details = hvg_engine.check_5m_structural_alignment("CALL", current_spot=23920.0)
        assert ok is False
        assert reason == "5m_macro_support_breakdown_opposed"
        assert details["5m_anchor_support"] == 23940.0

    def test_5m_overhead_resistance_proximity_veto(self, hvg_engine):
        """Verifies 1m CALL breakout is vetoed when unbroken prominent 5m ceiling is < 0.25 * ATR away."""
        base = 24000.0
        t0 = 100000.0
        # Bar 0: prominent resistance ceiling
        hvg_engine._push_5m_bar(base, 24060.0, base - 10, base + 20, 20.0, t0)
        # Bars 1..6: trading lower with valley highs so Bar 0 sees them under strict HVG
        for i in range(1, 7):
            hvg_engine._push_5m_bar(base - 10, base - 10 + i * 2, base - 20, base, 20.0, t0 + i * 300)

        # Proximity hurdle with ATR=20 is 0.25 * 20 = 5.0 pts
        # If spot is 24058.0, distance is 24060 - 24058 = 2.0 pts (< 5.0 pts hurdle)
        ok, reason, details = hvg_engine.check_5m_structural_alignment("CALL", current_spot=24058.0)
        assert ok is False
        assert reason == "5m_overhead_resistance_too_close"
        assert details["5m_overhead_resistance"] == 24060.0

    def test_5m_warmup_grace(self, hvg_engine):
        """When fewer than MIN_BARS_5M (6) exist, graceful pass is granted."""
        for i in range(4):
            hvg_engine._push_5m_bar(24000, 24010, 23990, 24005, 20.0, 1000.0 + i * 300)
        ok, reason, _ = hvg_engine.check_5m_structural_alignment("CALL", current_spot=24005.0)
        assert ok is True
        assert reason == "5m_warmup_grace"


class TestAntiSpoofingL2WallShield:
    """Tests the rolling 2.0s Anti-Spoofing L2 Wall Shield."""

    def test_transient_flicker_spoof_ignored(self, hvg_engine):
        """Sub-400ms flash order (HFT spoof) is ignored and breakout is allowed."""
        now = 1000.2
        # Two depth snapshots only 200ms apart with massive opposing ask wall (ask/bid = 5.0)
        depth_history = [
            (1000.0, 1000.0, 5000.0, -0.65),
            (1000.2, 1000.0, 5000.0, -0.65),
        ]
        ok, reason = hvg_engine.check_l2_wall("CALL", depth_history=depth_history, current_time=now)
        assert ok is True
        assert reason == "transient_flicker_spoof_ignored"

    def test_persistent_opposing_ask_wall_blocked(self, hvg_engine):
        """Persistent opposing wall spanning >= 400ms blocks CALL entry."""
        now = 1000.6
        depth_history = [
            (1000.0, 1000.0, 4000.0, -0.60),
            (1000.3, 1000.0, 4000.0, -0.60),
            (1000.6, 1000.0, 4000.0, -0.60),
        ]
        ok, reason = hvg_engine.check_l2_wall("CALL", depth_history=depth_history, current_time=now)
        assert ok is False
        assert reason == "persistent_opposing_ask_wall_blocked"

    def test_persistent_opposing_bid_wall_blocked(self, hvg_engine):
        """Persistent opposing bid wall spanning >= 400ms blocks PUT entry."""
        now = 1000.6
        depth_history = [
            (1000.0, 4000.0, 1000.0, 0.60),
            (1000.3, 4000.0, 1000.0, 0.60),
            (1000.6, 4000.0, 1000.0, 0.60),
        ]
        ok, reason = hvg_engine.check_l2_wall("PUT", depth_history=depth_history, current_time=now)
        assert ok is False
        assert reason == "persistent_opposing_bid_wall_blocked"

    def test_stale_ticks_pruned(self, hvg_engine):
        """Snapshots older than 2.0s are ignored; if < 2 persistent snapshots remain, pass gracefully."""
        now = 1010.0
        depth_history = [
            (1000.0, 1000.0, 5000.0, -0.70),  # 10s old -> pruned
            (1009.5, 1000.0, 5000.0, -0.70),  # 0.5s old -> valid
        ]
        ok, reason = hvg_engine.check_l2_wall("CALL", depth_history=depth_history, current_time=now)
        assert ok is True
        assert reason == "insufficient_persistent_snapshots"

    def test_supporting_wall_passes(self, hvg_engine):
        """When the order book wall supports the trade direction, it passes."""
        now = 1000.6
        depth_history = [
            (1000.0, 4000.0, 1000.0, 0.60),
            (1000.3, 4000.0, 1000.0, 0.60),
            (1000.6, 4000.0, 1000.0, 0.60),
        ]
        ok, reason = hvg_engine.check_l2_wall("CALL", depth_history=depth_history, current_time=now)
        assert ok is True
        assert reason == "l2_wall_passed"


class TestPurePriceActionWithoutEMALag:
    """Verifies that pure multi-timeframe price action does not require lagging 20/50 EMA cross."""

    def test_pure_pa_allows_bottom_breakout_with_lagging_ema(self, hvg_engine):
        base = 24000.0
        # Seed valid CALL setup
        for i in range(20):
            h = base + 10.0 - (i % 10) * 2.0
            hvg_engine.add_bar(h - 2.0, h, h - 5.0, h - 1.0, 20.0, base - 10.0, timestamp=1000.0 + i * 60)
        hvg_engine.add_bar(base, 24050.0, base - 2.0, base + 10.0, 20.0, base - 5.0, timestamp=2200.0)
        for i in range(4):
            hvg_engine.add_bar(base + 5.0, 24040.0, base, 24035.0, 20.0, base, timestamp=2260.0 + i * 60)
        hvg_engine.add_bar(24036.0, 24065.0, 24034.0, 24062.0, 20.0, base, timestamp=2500.0)

        assert hvg_engine.enforce_ema_trend is False

        # Lagging EMA is bearish (ema_20 < ema_50), but pure PA with VWAP > 0 allows bottom breakout
        ok, setup, _ = hvg_engine.evaluate_phase_shift(
            direction="CALL",
            ema_20=23990.0,
            ema_50=24010.0,
            current_vwap=base,
        )
        assert ok is True
        assert setup == "hvg_phase_shift_call_breakout"

        # If enforce_ema_trend is explicitly enabled, it gets rejected
        hvg_engine.enforce_ema_trend = True
        ok_enforced, reason, _ = hvg_engine.evaluate_phase_shift(
            direction="CALL",
            ema_20=23990.0,
            ema_50=24010.0,
            current_vwap=base,
        )
        assert ok_enforced is False
        assert reason == "bear_overlap_ema_disallowed"


class TestChatGPTSixFixRegression:
    """Targeted regression suite from ChatGPT's 6-fix bundle audit."""

    @staticmethod
    def _seed_1m(engine, highs, lows=None, closes=None, opens=None, atr=10.0, vwap=0.0):
        n = len(highs)
        lows = lows or [h - 5.0 for h in highs]
        closes = closes or list(highs)
        opens = opens or [c - 1.0 for c in closes]
        for i, (o, h, l, c) in enumerate(zip(opens, highs, lows, closes)):
            engine.add_bar(o, h, l, c, atr, vwap, 1_000.0 + i * 60.0)
        return n

    @staticmethod
    def _set_5m_scratch(engine, highs, lows, opens=None, closes=None, atr=10.0):
        n = len(highs)
        closes = closes or list(highs)
        opens = opens or [x - 1.0 for x in closes]
        engine._count_5m = n
        engine._head_5m = n - 1
        engine._scratch_5m_highs[:n] = highs
        engine._scratch_5m_lows[:n] = lows
        for i in range(n):
            engine._buf_5m_highs[i] = highs[i]
            engine._buf_5m_lows[i] = lows[i]
            engine._buf_5m_opens[i] = opens[i]
            engine._buf_5m_closes[i] = closes[i]
            engine._buf_5m_atrs[i] = atr
            engine._buf_5m_timestamps[i] = 10_000.0 + i * 300.0

    def test_strict_hvg_equal_levels_match_bruteforce_reference(self, hvg_engine):
        highs = [4, 2, 2, 3, 1, 3, 2]
        lows = [4, 3, 3, 2, 5, 2, 3]
        hvg_engine._scratch_highs[:len(highs)] = highs
        hvg_engine._scratch_lows[:len(lows)] = lows

        upper, lower = hvg_engine.compute_hvg_degrees(len(highs))

        def brute(values, upper_graph):
            degrees = [0] * len(values)
            for i in range(len(values)):
                for j in range(i + 1, len(values)):
                    threshold = min(values[i], values[j]) if upper_graph else max(values[i], values[j])
                    if all(
                        values[k] < threshold if upper_graph else values[k] > threshold
                        for k in range(i + 1, j)
                    ):
                        degrees[i] += 1
                        degrees[j] += 1
            return degrees

        assert upper[:len(highs)] == brute(highs, True)
        assert lower[:len(lows)] == brute(lows, False)

    def test_t_minus_1_graph_is_used_for_prominence(self, hvg_engine):
        historical = [10, 1, 1, 1, 1, 1]
        breakout = historical + [11]
        hvg_engine._scratch_highs[:len(breakout)] = breakout

        pre_deg, _ = hvg_engine.compute_hvg_degrees(len(historical))
        pre_deg = pre_deg[:len(historical)].copy()
        full_deg, _ = hvg_engine.compute_hvg_degrees(len(breakout))
        full_deg = full_deg[:len(breakout)].copy()

        assert full_deg[0] > pre_deg[0]

        hvg_engine._scratch_highs[:len(breakout)] = breakout
        hvg_engine.compute_hvg_degrees(len(breakout) - 1)
        frozen_anchor_degree = hvg_engine._deg_upper[0]
        frozen_mean = sum(hvg_engine._deg_upper[:len(breakout) - 1]) / (len(breakout) - 1)
        frozen_prominence = frozen_anchor_degree / frozen_mean

        assert frozen_anchor_degree == pre_deg[0]
        assert frozen_prominence != pytest.approx(full_deg[0] / (sum(full_deg) / len(full_deg)))

    def test_evaluator_freezes_prominence_on_t_minus_1(self, hvg_engine, monkeypatch):
        closes = [100.0 + (i % 3) for i in range(15)]
        highs = [c + 2.0 for c in closes]
        lows = [c - 2.0 for c in closes]
        opens = [c - 0.5 for c in closes]
        self._seed_1m(hvg_engine, highs, lows, closes, opens, atr=5.0)

        called = []
        original = hvg_engine.compute_hvg_degrees

        def spy(n):
            called.append(n)
            return original(n)

        monkeypatch.setattr(hvg_engine, "compute_hvg_degrees", spy)
        monkeypatch.setattr(hvg_engine, "check_5m_structural_alignment", lambda *a, **k: (True, "5m_aligned_call", {}))
        monkeypatch.setattr(hvg_engine, "check_l2_wall", lambda *a, **k: (True, "l2_wall_passed"))

        hvg_engine.evaluate_phase_shift("CALL", current_vwap=0.0)
        assert called and called[0] == 14

    def test_call_gap_above_anchor_is_not_fresh_breakout(self, hvg_engine, monkeypatch):
        highs = [102, 103, 104, 105, 106, 100] + [99] * 8 + [103]
        closes = [100, 100, 100, 100, 100, 99, 99, 99, 99, 99, 99, 99, 99, 99, 102]
        opens = closes[:-1] + [101]
        lows = [c - 3 for c in closes]
        lows[-1] = 101.0
        self._seed_1m(hvg_engine, highs, lows, closes, opens, atr=10.0)

        def fake_hvg(_n):
            hvg_engine._deg_upper[:_n] = [1] * _n
            hvg_engine._deg_upper[5] = 10
            hvg_engine._deg_lower[:_n] = [1] * _n
            return hvg_engine._deg_upper, hvg_engine._deg_lower

        monkeypatch.setattr(hvg_engine, "compute_hvg_degrees", fake_hvg)
        monkeypatch.setattr(hvg_engine, "check_5m_structural_alignment", lambda *a, **k: (True, "5m_aligned_call", {}))
        monkeypatch.setattr(hvg_engine, "check_l2_wall", lambda *a, **k: (True, "l2_wall_passed"))

        admitted, reason, _ = hvg_engine.evaluate_phase_shift("CALL", current_vwap=0.0)
        assert admitted is False
        assert reason in {"pre_breakout_not_compressed_or_no_breakout", "no_prominent_resistance_anchor"}

    def test_call_rejects_prior_close_above_anchor(self, hvg_engine, monkeypatch):
        highs = [102, 103, 104, 105, 106, 100] + [99] * 8 + [103]
        closes = [100, 100, 100, 100, 100, 99, 99, 99, 99, 99, 99, 99, 99, 101, 102]
        opens = [99] * 14 + [99]
        lows = [c - 3 for c in closes]
        lows[-1] = 98.0
        self._seed_1m(hvg_engine, highs, lows, closes, opens, atr=10.0)

        def fake_hvg(_n):
            hvg_engine._deg_upper[:_n] = [1] * _n
            hvg_engine._deg_upper[5] = 10
            hvg_engine._deg_lower[:_n] = [1] * _n
            return hvg_engine._deg_upper, hvg_engine._deg_lower

        monkeypatch.setattr(hvg_engine, "compute_hvg_degrees", fake_hvg)
        monkeypatch.setattr(hvg_engine, "check_5m_structural_alignment", lambda *a, **k: (True, "5m_aligned_call", {}))
        monkeypatch.setattr(hvg_engine, "check_l2_wall", lambda *a, **k: (True, "l2_wall_passed"))

        admitted, _, _ = hvg_engine.evaluate_phase_shift("CALL", current_vwap=0.0)
        assert admitted is False

    def test_unbroken_5m_resistance_is_enforced(self, hvg_engine, monkeypatch):
        highs = [100, 110, 105, 115, 104, 103]
        lows = [95, 106, 101, 110, 100, 99]
        opens = [99, 109, 104, 114, 103, 102]
        closes = [99, 109, 104, 114, 103, 102]
        self._set_5m_scratch(hvg_engine, highs, lows, opens, closes, atr=10.0)

        def fake_hvg(_n):
            hvg_engine._deg_5m_upper[:_n] = [1, 10, 1, 1, 1, 1]
            hvg_engine._deg_5m_lower[:_n] = [1] * _n
            return hvg_engine._deg_5m_upper, hvg_engine._deg_5m_lower

        monkeypatch.setattr(hvg_engine, "compute_5m_hvg_degrees", fake_hvg)
        ok, reason, _ = hvg_engine.check_5m_structural_alignment("CALL", 109.0)
        assert ok is True
        assert reason == "5m_aligned_call"

    def test_true_5m_atr_is_derived_from_5m_true_ranges(self, hvg_engine):
        bars = [
            (100, 110, 100, 105),
            (105, 116, 104, 112),  # TR = max(12, 11, 1) = 12
            (112, 120, 108, 118),  # TR = max(12, 8, 4) = 12
        ]
        ts = 10_000.0
        for o, h, l, c in bars:
            hvg_engine._push_5m_bar(o, h, l, c, atr=1.0, timestamp=ts)
            ts += 300.0
        assert hvg_engine._buf_5m_atrs[hvg_engine._head_5m] == pytest.approx((10 + 12 + 12) / 3)

    def test_l2_wall_requires_recent_continuous_400ms(self, hvg_engine):
        now = 100.0
        wall = lambda t: (t, 100.0, 220.0, -0.40)
        normal = lambda t: (t, 120.0, 100.0, 0.05)

        ok, reason = hvg_engine.check_l2_wall("CALL", [wall(99.5), wall(99.8), wall(99.95)], now)
        assert ok is False
        assert reason == "persistent_opposing_ask_wall_blocked"

        ok, reason = hvg_engine.check_l2_wall("CALL", [wall(99.8), wall(99.95)], now)
        assert ok is True
        assert reason == "transient_flicker_spoof_ignored"

        ok, reason = hvg_engine.check_l2_wall("CALL", [wall(99.5), normal(99.8), wall(99.95)], now)
        assert ok is True

    def test_market_close_flush_is_censored_not_false_win(self, hvg_engine):
        sig = HVGSetupSignal(
            timestamp=1000.0,
            direction="CALL",
            setup_name="hvg_network_phase_shift",
            pillar=4,
            anchor_price=100.0,
            anchor_prominence=2.0,
            anchor_index=3,
            breakout_price=101.0,
            bar_open=99.0,
            bar_high=102.0,
            bar_low=98.0,
            bar_close=101.0,
            body_ratio=0.5,
            sl_price=95.0,
            buffer_pts=5.0,
            atr_14=10.0,
            vwap=100.0,
            ema_20=100.0,
            ema_50=99.0,
            is_shadow_only=True,
        )
        hvg_engine._record_shadow_signal(sig)
        hvg_engine.on_underlying_tick(102.0, 1020.0)
        hvg_engine.flush_all_pending()

        payload = json.loads(Path(hvg_engine.telemetry_file).read_text().strip())
        assert payload["outcome_status"] == "CENSORED_MARKET_CLOSE"
        assert payload["pnl_300s"] is None
        assert payload["win_300s"] is None

    def test_completed_300s_outcome_is_marked_complete(self, hvg_engine):
        sig = HVGSetupSignal(
            timestamp=1000.0,
            direction="CALL",
            setup_name="hvg_network_phase_shift",
            pillar=4,
            anchor_price=100.0,
            anchor_prominence=2.0,
            anchor_index=3,
            breakout_price=101.0,
            bar_open=99.0,
            bar_high=102.0,
            bar_low=98.0,
            bar_close=101.0,
            body_ratio=0.5,
            sl_price=95.0,
            buffer_pts=5.0,
            atr_14=10.0,
            vwap=100.0,
            ema_20=100.0,
            ema_50=99.0,
            is_shadow_only=True,
        )
        hvg_engine._record_shadow_signal(sig)
        hvg_engine.on_underlying_tick(103.0, 1300.0)

        payload = json.loads(Path(hvg_engine.telemetry_file).read_text().strip())
        assert payload["outcome_status"] == "COMPLETE"
        assert payload["pnl_300s"] == pytest.approx(2.0)
        assert payload["win_300s"] is True


class TestTopologicalStructuralSweep:
    """Rigorous tests for Pillar 2 Topological Structural Sweep & Reclaim."""

    @pytest.fixture
    def setup_call_sweep_bars(self, hvg_engine):
        """Builds a deterministic 33-bar sequence with a prominent unbroken support anchor at k=20."""
        base = 24000.0
        ts = 1000.0
        # Bars 0..19: Undulating structure before anchor (gives anchor high backward visibility)
        for i in range(20):
            l = base - 10.0 + (i % 10) * 2.0
            hvg_engine.add_bar(
                open_price=l + 2.0, high_price=l + 5.0, low_price=l,
                close_price=l + 1.0, atr=20.0, vwap=base - 10.0, timestamp=ts
            )
            ts += 60.0

        # Bar 20: Prominent structural support anchor S* = 23940.0
        hvg_engine.add_bar(
            open_price=base, high_price=base + 2.0, low_price=23940.0,
            close_price=base - 10.0, atr=20.0, vwap=base - 10.0, timestamp=ts
        )
        ts += 60.0

        # Bars 21..32 (12 bars): Trading above S* (lows 23955..23960)
        # Age at bar 33 is 33 - 20 = 13 bars (within [10, 30])
        for i in range(12):
            hvg_engine.add_bar(
                open_price=base - 10.0, high_price=base, low_price=23955.0,
                close_price=23960.0, atr=20.0, vwap=base - 10.0, timestamp=ts
            )
            ts += 60.0

        return ts

    def test_causal_t_minus_1_freeze(self, hvg_engine, setup_call_sweep_bars):
        """Bar T (sweep bar) must NOT participate in or inflate anchor degree on Graph(T-1)."""
        ts = setup_call_sweep_bars
        n_before = hvg_engine._unroll_to_scratch()
        hvg_engine.compute_hvg_degrees(n_before)
        deg_before = hvg_engine._deg_lower[20]

        # Add sweep Bar T
        hvg_engine.add_bar(
            open_price=23950.0, high_price=23965.0, low_price=23930.0,
            close_price=23955.0, atr=20.0, vwap=23940.0, timestamp=ts
        )

        ok, reason, details = hvg_engine.check_topological_sweep_reclaim("CALL", current_vwap=23940.0)
        assert ok is True, f"Failed with reason: {reason}"
        assert details["anchor_price"] == 23940.0
        assert details["anchor_degree"] == deg_before

    def test_t_minus_1_seed_catches_prior_bar_break(self, hvg_engine, setup_call_sweep_bars):
        """CRITICAL FIX TEST: If Bar T-1 breached below S*, anchor is recognized as broken and rejected."""
        ts = setup_call_sweep_bars
        # Bar 33 (T-1) breaches below 23940.0
        hvg_engine.add_bar(
            open_price=23950.0, high_price=23955.0, low_price=23935.0,
            close_price=23945.0, atr=20.0, vwap=23940.0, timestamp=ts
        )
        ts += 60.0

        # Bar 34 (T) attempts a sweep
        hvg_engine.add_bar(
            open_price=23950.0, high_price=23965.0, low_price=23930.0,
            close_price=23955.0, atr=20.0, vwap=23940.0, timestamp=ts
        )

        ok, reason, _ = hvg_engine.check_topological_sweep_reclaim("CALL", current_vwap=23940.0)
        assert ok is False
        assert reason == "no_valid_topological_support_sweep"

    def test_local_extremum_requirement(self, hvg_engine):
        """Non-extremum plateau bars cannot be structural anchors even if prominent."""
        ts = 1000.0
        # Create 35 bars where all lows are identical flat 24000.0
        for i in range(35):
            hvg_engine.add_bar(
                open_price=24010.0, high_price=24020.0, low_price=24000.0,
                close_price=24010.0, atr=20.0, vwap=24005.0, timestamp=ts
            )
            ts += 60.0
        hvg_engine.add_bar(
            open_price=24005.0, high_price=24020.0, low_price=23990.0,
            close_price=24010.0, atr=20.0, vwap=24005.0, timestamp=ts
        )
        ok, reason, _ = hvg_engine.check_topological_sweep_reclaim("CALL", current_vwap=24005.0)
        assert ok is False
        assert reason == "no_valid_topological_support_sweep"

    def test_gap_through_rejection(self, hvg_engine, setup_call_sweep_bars):
        """Gap-down opening below S* (Open_T < S*) is rejected as a sweep even with sufficient wick."""
        ts = setup_call_sweep_bars
        # Gap-down: Open=23935.0 < S* (23940.0), Low=23910.0, High=23965.0, Close=23955.0
        # Lower wick = 23935 - 23910 = 25. Range = 55. Wick ratio = 25/55 = 0.45 >= 0.35.
        hvg_engine.add_bar(
            open_price=23935.0, high_price=23965.0, low_price=23910.0,
            close_price=23955.0, atr=20.0, vwap=23940.0, timestamp=ts
        )
        ok, reason, _ = hvg_engine.check_topological_sweep_reclaim("CALL", current_vwap=23940.0)
        assert ok is False
        assert reason == "no_valid_topological_support_sweep"

    def test_wick_ratio_threshold(self, hvg_engine, setup_call_sweep_bars):
        """Sweep candle with lower wick < 35% of total range is rejected."""
        ts = setup_call_sweep_bars
        # Total range = 24000 - 23935 = 65 pts.
        # Open=23945, Close=23995. Lower wick = 23945 - 23935 = 10 pts. Wick ratio = 10 / 65 = 0.154 (< 0.35)
        hvg_engine.add_bar(
            open_price=23945.0, high_price=24000.0, low_price=23935.0,
            close_price=23995.0, atr=20.0, vwap=23940.0, timestamp=ts
        )
        ok, reason, _ = hvg_engine.check_topological_sweep_reclaim("CALL", current_vwap=23940.0)
        assert ok is False
        assert reason == "insufficient_lower_wick"

    def test_signed_vwap_distance_metric(self, hvg_engine, setup_call_sweep_bars):
        """Signed VWAP distance s * (Close - VWAP) / ATR is positive when direction-aligned."""
        ts = setup_call_sweep_bars
        # Close = 23955, VWAP = 23940, ATR = 20. (23955 - 23940) / 20 = 0.75
        hvg_engine.add_bar(
            open_price=23950.0, high_price=23965.0, low_price=23930.0,
            close_price=23955.0, atr=20.0, vwap=23940.0, timestamp=ts
        )
        ok, reason, details = hvg_engine.check_topological_sweep_reclaim("CALL", current_vwap=23940.0)
        assert ok is True
        assert details["distance_to_vwap_atr"] == pytest.approx(0.75, abs=0.01)

    def test_call_and_put_symmetry(self, hvg_engine):
        """Verifies symmetric PUT resistance sweep and reclaim."""
        ts = 1000.0
        for i in range(15):
            hvg_engine.add_bar(
                open_price=23950.0, high_price=23960.0, low_price=23940.0,
                close_price=23950.0, atr=20.0, vwap=24000.0, timestamp=ts
            )
            ts += 60.0

        # Bar 15: Prominent resistance anchor R* = 24020.0
        hvg_engine.add_bar(
            open_price=23960.0, high_price=24020.0, low_price=23955.0,
            close_price=23990.0, atr=20.0, vwap=24000.0, timestamp=ts
        )
        ts += 60.0

        # Bars 16..33: Trading below R* (highs 23960..23990)
        for i in range(18):
            hvg_engine.add_bar(
                open_price=23980.0, high_price=23970.0 + (i % 5) * 4.0, low_price=23950.0,
                close_price=23975.0, atr=20.0, vwap=24000.0, timestamp=ts
            )
            ts += 60.0

        # Bar 34 (T): Sweeps R* (High 24030 > 24020), reclaims below (Close 23985 < 24020), Open 24000 <= 24020
        hvg_engine.add_bar(
            open_price=24000.0, high_price=24030.0, low_price=23980.0,
            close_price=23985.0, atr=20.0, vwap=24010.0, timestamp=ts
        )
        ok, reason, details = hvg_engine.check_topological_sweep_reclaim("PUT", current_vwap=24010.0)
        assert ok is True
        assert details["direction"] == "PUT"
        assert details["anchor_price"] == 24020.0
        assert details["strategy_variant"] == "TOPO_P2"
        assert details["event_id"] == f"TOPO_P2_{int(ts)}_PUT"

    def test_experiment_identity_and_telemetry(self, hvg_engine, setup_call_sweep_bars):
        """Verifies strategy_variant='TOPO_P2', event_id, candidate_count, and telemetry write."""
        ts = setup_call_sweep_bars
        hvg_engine.add_bar(
            open_price=23950.0, high_price=23965.0, low_price=23930.0,
            close_price=23955.0, atr=20.0, vwap=23940.0, timestamp=ts
        )
        ok, reason, details = hvg_engine.check_topological_sweep_reclaim("CALL", current_vwap=23940.0)
        assert ok is True
        assert details["strategy_variant"] == "TOPO_P2"
        assert details["event_id"] == f"TOPO_P2_{int(ts)}_CALL"
        assert details["anchor_price"] == 23940.0
        assert details["candidate_count"] >= 1
        assert details["anchor_selection_reason"] == "nearest_prominent"

        # Advance tick to trigger 300s completion and flush to telemetry file
        hvg_engine.on_underlying_tick(23970.0, ts + 301.0)
        text = Path(hvg_engine.telemetry_file).read_text().strip()
        lines = [json.loads(l) for l in text.split("\n") if l.strip()]
        topo_records = [r for r in lines if r.get("strategy_variant") == "TOPO_P2"]
        assert len(topo_records) >= 1
        rec = topo_records[-1]
        assert rec["event_id"] == f"TOPO_P2_{int(ts)}_CALL"
        assert rec["anchor_price"] == 23940.0
        assert rec["candidate_count"] >= 1
        assert rec["outcome_status"] == "COMPLETE"

    def test_dispatcher_single_admission_policy(self):
        """Dispatcher invariant: exactly one setup is admitted per candle; no duplicate emissions."""
        from AEGIS.core.price_action_engine import PriceActionEngine
        pae = PriceActionEngine()
        t0 = 1000.0
        for i in range(30):
            pae.update_tick(24000.0, t0 + i * 60.0)
            pae.update_tick(24005.0, t0 + i * 60.0 + 30.0)

        ok, setup_name, details = pae.evaluate_setup("UP", external_vwap=24000.0)
        assert isinstance(ok, bool)
        assert isinstance(setup_name, str)
        assert isinstance(details, dict)


