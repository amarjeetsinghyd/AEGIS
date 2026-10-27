import os
import time
import pytest
import numpy as np
from AEGIS.core.option_order_flow import MicrostructureScorer

class TestMicrostructureScorer:
    def test_initialization(self):
        scorer = MicrostructureScorer(high_conviction_threshold=75.0)
        assert scorer.high_conviction_threshold == 75.0
        assert "target_tick_intensity" in scorer.weights
        assert scorer.raw_intercept < 0

    def test_update_and_pruning(self):
        scorer = MicrostructureScorer()
        sym = "NIFTY26AUG24500CE"
        t_base = 1000.0
        
        # Add ticks spanning 40 seconds with 5 distinct levels
        for i in range(40):
            scorer.update_tick(
                sym=sym,
                ts_sec=t_base + i,
                lp=100.0 + i * 0.1,
                bp1=99.9,
                sp1=100.1,
                b1=50, a1=50,
                b2=40, a2=40,
                b3=60, a3=60,
                b4=70, a4=70,
                b5=80, a5=80
            )
            
        buf = scorer.history[sym]
        # Should have pruned ticks older than 30s
        assert len(buf) <= 32
        assert buf[-1][0] - buf[0][0] <= 30.5

    def test_score_bounds_and_features(self):
        scorer = MicrostructureScorer(high_conviction_threshold=75.0)
        sym = "SENSEX2682076400CE"
        t0 = 2000.0
        
        # Calm market ticks (1 tick every 5 seconds)
        for i in range(6):
            scorer.update_tick(
                sym=sym,
                ts_sec=t0 + i * 5.0,
                lp=50.0,
                bp1=49.9,
                sp1=50.1,
                b1=20, a1=20,
                b2=30, a2=30,
                b3=40, a3=40,
                b4=30, a4=30,
                b5=30, a5=30
            )
            
        score, is_high_conv, feats = scorer.compute_score(sym, t0 + 30.0)
        assert 0.0 <= score <= 100.0
        assert not is_high_conv  # Calm market should NOT trigger high conviction
        assert "target_tick_intensity" in feats
        assert "touch_pressure" in feats
        assert "deep_pressure" in feats
        assert "layer_divergence" in feats
        assert "book_slope" in feats
        assert "i1" in feats
        assert "i5" in feats

    def test_layered_decomposition_math(self):
        scorer = MicrostructureScorer()
        sym = "NIFTY26AUG24500CE"
        t0 = 1000.0
        
        # First tick
        scorer.update_tick(
            sym=sym, ts_sec=t0, lp=100.0, bp1=99.9, sp1=100.1,
            b1=100, a1=20,
            b2=80, a2=20,
            b3=60, a3=30,
            b4=40, a4=40,
            b5=20, a5=100
        )
        # Second tick (same quantities to verify exact scalar math)
        scorer.update_tick(
            sym=sym, ts_sec=t0 + 1.0, lp=100.1, bp1=100.0, sp1=100.2,
            b1=100, a1=20,
            b2=80, a2=20,
            b3=60, a3=30,
            b4=40, a4=40,
            b5=20, a5=100
        )

        score, is_conv, feats = scorer.compute_score(sym, t0 + 1.0)
        
        # Exact Expected Math:
        # I_1 = (100 - 20) / (120 + 1e-4) = 80 / 120 = 0.666666...
        # I_2 = (80 - 20) / (100 + 1e-4) = 60 / 100 = 0.60
        # I_3 = (60 - 30) / (90 + 1e-4) = 30 / 90 = 0.333333...
        # I_4 = (40 - 40) / (80 + 1e-4) = 0.0
        # I_5 = (20 - 100) / (120 + 1e-4) = -80 / 120 = -0.666666...
        
        # Touch Pressure = I_1 = 0.6667
        # Deep Pressure = (I_3 + I_4 + I_5) / 3.0 = (0.333333 + 0 - 0.666667) / 3.0 = -0.333333 / 3.0 = -0.111111...
        # Layer Divergence = Touch Pressure - Deep Pressure = 0.666667 - (-0.111111) = 0.777778
        # Book Slope = (I_5 - I_1) / 4.0 = (-0.666667 - 0.666667) / 4.0 = -1.333333 / 4.0 = -0.333333...

        assert abs(feats["touch_pressure"] - (80.0 / 120.0)) < 1e-3
        expected_deep = (30.0 / 90.0 + 0.0 - 80.0 / 120.0) / 3.0
        assert abs(feats["deep_pressure"] - expected_deep) < 1e-3
        expected_div = (80.0 / 120.0) - expected_deep
        assert abs(feats["layer_divergence"] - expected_div) < 1e-3
        expected_slope = (-80.0 / 120.0 - 80.0 / 120.0) / 4.0
        assert abs(feats["book_slope"] - expected_slope) < 1e-3

    def test_legacy_backward_compatibility(self):
        scorer = MicrostructureScorer()
        sym = "NIFTY26AUG24500CE"
        t0 = 1000.0
        
        # Old legacy signature with deep_bid_qty and deep_ask_qty
        scorer.update_tick(
            sym=sym, ts_sec=t0, lp=100.0, bp1=99.9, sp1=100.1,
            b1=50, a1=50, deep_bid_qty=300, deep_ask_qty=300
        )
        scorer.update_tick(
            sym=sym, ts_sec=t0 + 1.0, lp=100.0, bp1=99.9, sp1=100.1,
            b1=50, a1=50, deep_bid_qty=300, deep_ask_qty=300
        )
        score, is_conv, feats = scorer.compute_score(sym, t0 + 1.0)
        assert "touch_pressure" in feats
        assert "deep_pressure" in feats
        assert abs(feats["touch_pressure"]) < 1e-3

    def test_high_conviction_surge(self):
        scorer = MicrostructureScorer(high_conviction_threshold=75.0)
        sym = "NIFTY26AUG24500CE"
        t0 = 3000.0
        
        # Rapid institutional burst: 60 ticks in 20s (intensity = 3.0 ticks/sec) + ask depth vacuum + spread compression
        for i in range(60):
            scorer.update_tick(
                sym=sym,
                ts_sec=t0 + i * 0.3,
                lp=50.0 + i * 0.2,
                bp1=50.0 + i * 0.2 - 0.05,
                sp1=50.0 + i * 0.2 + 0.05,
                b1=100 + i * 5, a1=10,
                b2=150 + i * 3, a2=20,
                b3=200 + i * 3, a3=max(5, 100 - i * 2),
                b4=150 + i * 2, a4=max(5, 100 - i * 2),
                b5=150 + i * 2, a5=max(5, 100 - i * 1)
            )
            
        score, is_high_conv, feats = scorer.compute_score(
            sym,
            t0 + 20.0,
            adjacent_otm_returns={1: 0.08, 2: 0.05}
        )
        assert 0.0 <= score <= 100.0
        assert score > 70.0  # Burst should produce very high score
        assert feats["touch_pressure"] > 0.5  # Strong touch pressure
        assert feats["deep_pressure"] > 0.5   # Strong deep pressure
