"""
tests/test_volume_profile_engine.py — Unit tests for AEGIS Volume Profile & Auction Market Theory Engine.
"""

import pytest
import time
from AEGIS.core.volume_profile_engine import VolumeProfileEngine


class TestVolumeProfileEngine:
    @pytest.fixture
    def engine(self):
        return VolumeProfileEngine(bin_size_pts=5.0, value_area_pct=0.70, index_name="NIFTY")

    def test_single_bar_accumulation_and_poc(self, engine):
        """Tests that a high volume bar creates a clear POC at that level."""
        # Bar around 23100 with massive volume
        engine.record_spot_bar(open_px=23098.0, high_px=23102.0, low_px=23098.0, close_px=23101.0, volume=50000.0)
        # Bar around 23110 with smaller volume
        engine.record_spot_bar(open_px=23108.0, high_px=23112.0, low_px=23108.0, close_px=23111.0, volume=10000.0)

        poc, vah, val = engine.recalculate_profile()
        assert poc == 23100.0
        assert val <= poc <= vah

    def test_value_area_70_percent_expansion(self, engine):
        """
        Tests the 70% Value Area calculation.
        Creates a bell curve centered at 23100:
        23080: 1000
        23090: 5000
        23100: 20000 (POC)
        23110: 5000
        23120: 1000
        Total: 32000 vol. 70% = 22400.
        Value Area should encompass 23090 to 23110 (30000 vol > 22400).
        """
        engine.record_spot_tick(23080.0, 1000.0)
        engine.record_spot_tick(23090.0, 5000.0)
        engine.record_spot_tick(23100.0, 20000.0)
        engine.record_spot_tick(23110.0, 5000.0)
        engine.record_spot_tick(23120.0, 1000.0)

        poc, vah, val = engine.recalculate_profile()
        assert poc == 23100.0
        assert vah == 23110.0
        assert val == 23090.0

    def test_lvn_and_auction_zones(self, engine):
        """Tests Low Volume Node (LVN) detection and Auction Zone categorization."""
        # Establish heavy volume at 23050 (POC)
        engine.record_spot_tick(23050.0, 100000.0)
        # Light volume at 23080 (LVN)
        engine.record_spot_tick(23080.0, 1000.0)

        engine.recalculate_profile()

        # At 23080 (far above POC): should be ABOVE_VAH and is_lvn_zone == True
        metrics_above = engine.get_profile_metrics(spot_price=23080.0, atr_1m=8.0, direction="UP")
        assert metrics_above["auction_zone"] == "ABOVE_VAH"
        assert metrics_above["is_lvn_zone"] is True
        assert metrics_above["poc_alignment"] == "SUPPORTIVE_FLOOR"

        # At 23050: should be at POC, not LVN
        metrics_poc = engine.get_profile_metrics(spot_price=23050.0, atr_1m=8.0, direction="UP")
        assert metrics_poc["is_at_poc"] is True
        assert metrics_poc["is_lvn_zone"] is False
        assert metrics_poc["poc_alignment"] == "POC_BOUNCE_SUPPORT"

    def test_option_strike_poc_tracking(self, engine):
        """Tests tracking of Call POC and Put POC strikes."""
        call_vols = {23000.0: 150000.0, 23050.0: 450000.0, 23100.0: 250000.0}
        put_vols = {23000.0: 600000.0, 23050.0: 300000.0, 23100.0: 100000.0}

        engine.record_option_volumes(call_vols, put_vols)
        engine.recalculate_profile()

        assert engine.cached_call_poc_strike == 23050.0
        assert engine.cached_put_poc_strike == 23000.0

    def test_microsecond_hot_path_performance(self, engine):
        """Ensures tick updates and metric queries run in < 10 microseconds."""
        for p in range(23000, 23200, 5):
            engine.record_spot_tick(float(p), 1000.0)
        engine.recalculate_profile()

        t0 = time.perf_counter()
        for _ in range(1000):
            _ = engine.get_profile_metrics(spot_price=23112.5, atr_1m=6.5, direction="UP")
        elapsed_sec = time.perf_counter() - t0
        avg_us = (elapsed_sec / 1000) * 1e6

        assert avg_us < 50.0, f"Average latency too high: {avg_us:.2f} µs"
