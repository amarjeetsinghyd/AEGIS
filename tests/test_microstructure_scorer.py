import pytest
from core.option_order_flow import MicrostructureScorer, CompositeConvictionEngine

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
        assert len(buf) == 40  # All 40 ticks stored (within maxlen=1000)
        assert buf[-1][0] - buf[0][0] <= 30.5 + 10.0  # time span matches input range

    def test_deprecated_compute_score_and_verify(self):
        scorer = MicrostructureScorer(high_conviction_threshold=75.0)
        sym = "SENSEX2682076400CE"
        score, is_high_conv, feats = scorer.compute_score(sym, 2030.0)
        assert score == 0.0
        assert is_high_conv is False
        assert feats == {}

        ok, res = scorer.verify_event_bound_30s(sym, 2030.0, "CALL")
        assert ok is True
        assert res.get("reason") == "delegated_to_conviction_engine"

    def test_live_tick_intensity_uses_effective_span_not_tick_spacing(self):
        """Tick intensity must divide by the observed span (floored at 5s), never by
        inter-tick spacing and never by the full lookback when the buffer is cold."""
        scorer = MicrostructureScorer()
        sym = "NIFTY26AUG24500CE"
        for ts in (1000.0, 1000.9):
            scorer.update_tick(
                sym=sym,
                ts_sec=ts,
                lp=100.0,
                bp1=99.9,
                sp1=100.1,
                b1=200,
                a1=100,
            )

        micro = CompositeConvictionEngine.extract_micro_metrics(
            tick_buffer=scorer.history[sym],
            anchor_T=1001.0,
            lookback_sec=10.0,
        )
        assert micro["is_valid"] is True
        # 2 ticks over a 0.9s buffer -> span floored to 5.0s -> 2/5.0 == 0.4
        # (would have been 2/0.9 == 2.22 with tick spacing, or 2/10.0 == 0.2 with a cold buffer)
        assert micro["tick_intensity"] == pytest.approx(0.4)
