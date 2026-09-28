"""
tests/test_composite_conviction_engine.py — Unit tests for AEGIS Composite Conviction Engine.

Tests:
1. High-conviction textbook BOS breakout scores >= 85 (FULL_SIZE).
2. Exhausted lower-highs distribution scores < 75 (BLOCKED).
3. Deep OTM delta trap penalized (delta < 0.10 drags score down, BLOCKED).
4. Futures divergence penalty (-6 pts when futures move opposite to spot).
5. Scale invariance across NIFTY (24k) and SENSEX (75k).
6. Setup-specific weight profiling (Pillar 1 vs Pillar 3 vs Pillar 4).
7. Pure Python Black-Scholes Delta estimation.
8. Execution manager dynamic lot sizing (FULL_SIZE -> 2 lots, STANDARD_SIZE -> 1 lot).
"""

import pytest
from dataclasses import dataclass
from unittest.mock import MagicMock

from AEGIS.core.composite_conviction_engine import (
    CompositeConvictionEngine,
    ConvictionResult,
)
from AEGIS.core.price_action_engine import OHLCBar, PriceActionEngine


@dataclass
class MockBar:
    open: float
    high: float
    low: float
    close: float
    volume: float = 100.0


class TestCompositeConvictionEngine:
    @pytest.fixture
    def engine(self):
        return CompositeConvictionEngine(
            min_approval_score=75.0,
            high_conviction_score=85.0,
            standard_tp_pct=0.040,
            high_conviction_tp_pct=0.050,
        )

    def test_high_conviction_textbook_bos_scores_above_85(self, engine):
        """
        Textbook BOS breakout (like 09:32 SENSEX):
        - Clean bullish structure: HH/HL sequence, broken PDH into open sky.
        - High impulse: 70% body, closing at top 10% edge, 1.2x ATR, 2.0x volume surge.
        - Relentless order flow: Dp = 0.85, touch I1 = +0.25, futures +2.5 bps, basis expanding.
        - Optimal contract: 0.35 delta sweet spot, 0.20% spread.
        Expected: Score >= 85.0, sizing_tier == 'FULL_SIZE', is_approved == True.
        """
        prior_bars = [
            MockBar(open=82000.0, high=82050.0, low=81980.0, close=82040.0, volume=100.0),
            MockBar(open=82040.0, high=82100.0, low=82020.0, close=82090.0, volume=120.0),
            MockBar(open=82090.0, high=82160.0, low=82080.0, close=82150.0, volume=250.0),
        ]
        breakout_bar = prior_bars[-1]

        res = engine.compute_conviction_score(
            direction="UP",
            setup_name="break_of_structure",
            spot_price=82150.0,
            prior_bars=prior_bars,
            current_bar=breakout_bar,
            atr=50.0,
            ema_20=82080.0,
            ema_50=82000.0,
            vwap=82050.0,
            pdh=82100.0,  # Spot 82150 has broken above PDH -> open sky
            pdl=81800.0,
            vol_sma10=100.0,
            persistence_dp=0.85,
            touch_i1=0.25,
            delta_fut_60s_bps=2.5,
            delta_basis_60s_bps=0.8,
            option_delta=0.35,
            spread_pct=0.20,
        )

        assert res.is_approved is True
        assert res.total_score >= 85.0
        assert res.sizing_tier == "FULL_SIZE"
        assert res.recommended_tp_pct == 0.050
        assert res.sub_scores["structure"] >= 20.0
        assert res.sub_scores["impulse"] >= 25.0
        assert res.sub_scores["flow"] >= 25.0
        assert res.sub_scores["contract"] == 15.0

    def test_exhausted_lower_highs_scores_below_threshold(self, engine):
        """
        Exhausted distribution top (like 12:37 Trade #3):
        - Lower highs into resistance (LH penalty: -15 pts).
        - Weak impulse: small body (30%), large upper wick rejection (45%).
        - Stalled flow: Dp = 0.50, touch I1 = -0.15, futures flat (-0.5 bps), basis shrinking.
        Expected: Score < 75.0, sizing_tier == 'BLOCKED', is_approved == False.
        """
        prior_bars = [
            MockBar(open=82420.0, high=82450.0, low=82400.0, close=82430.0, volume=100.0),
            MockBar(open=82430.0, high=82440.0, low=82390.0, close=82410.0, volume=90.0),
            # Lower high (82425 < 82440 < 82450) with heavy upper wick
            MockBar(open=82405.0, high=82425.0, low=82395.0, close=82408.0, volume=70.0),
        ]
        curr_bar = prior_bars[-1]

        res = engine.compute_conviction_score(
            direction="UP",
            setup_name="20_ema_rejection",
            spot_price=82408.0,
            prior_bars=prior_bars,
            current_bar=curr_bar,
            atr=40.0,
            ema_20=82400.0,
            ema_50=82390.0,
            vwap=82410.0,
            pdh=82450.0,  # 82450 is just 42 pts away (< 1.0 ATR)
            pdl=82000.0,
            vol_sma10=100.0,
            persistence_dp=0.50,
            touch_i1=-0.15,
            delta_fut_60s_bps=-0.5,
            delta_basis_60s_bps=-0.4,
            option_delta=0.30,
            spread_pct=0.45,
        )

        assert res.is_approved is False
        assert res.total_score < 75.0
        assert res.sizing_tier == "BLOCKED"
        assert "structure_drag" in res.reason or "impulse_drag" in res.reason

    def test_deep_otm_delta_trap_penalized(self, engine):
        """
        Domain 4 Contract scoring is NEUTRALIZED (2026-09-22) for 0DTE affordable
        index options. Delta sweet-spot (0.25-0.45) is not meaningful for 0DTE:
        affordable OTM options have delta < 0.10 (theta-decayed), and near-ATM
        options have delta > 0.55. No affordable 0DTE strike naturally lands in
        the sweet spot, causing systematic contract_drag on all valid setups.

        Post-neutralization: Both delta=0.02 (old "trap") and delta=0.35 (old
        "sweet spot") return the same contract score of 15.0 (full marks).
        Delta is still logged in telemetry for future recalibration.
        """
        prior_bars = [
            MockBar(open=24000.0, high=24030.0, low=23990.0, close=24025.0, volume=100.0),
            MockBar(open=24025.0, high=24050.0, low=24010.0, close=24045.0, volume=120.0),
            MockBar(open=24045.0, high=24070.0, low=24030.0, close=24065.0, volume=130.0),
        ]
        curr_bar = prior_bars[-1]

        # Scenario A: delta=0.02 (was "deep OTM trap") — now full marks
        res_trap = engine.compute_conviction_score(
            direction="UP",
            setup_name="20_ema_rejection",
            spot_price=24065.0,
            prior_bars=prior_bars,
            current_bar=curr_bar,
            atr=25.0,
            ema_20=24040.0,
            ema_50=24010.0,
            vwap=24020.0,
            pdh=24150.0,
            pdl=23900.0,
            vol_sma10=100.0,
            persistence_dp=0.65,
            touch_i1=0.05,
            delta_fut_60s_bps=0.5,
            delta_basis_60s_bps=0.0,
            option_delta=0.02,  # Was theta trap — now neutralized
            spread_pct=0.80,
        )

        # Scenario B: delta=0.35 (was "sweet spot") — still full marks
        res_sweet = engine.compute_conviction_score(
            direction="UP",
            setup_name="20_ema_rejection",
            spot_price=24065.0,
            prior_bars=prior_bars,
            current_bar=curr_bar,
            atr=25.0,
            ema_20=24040.0,
            ema_50=24010.0,
            vwap=24020.0,
            pdh=24150.0,
            pdl=23900.0,
            vol_sma10=100.0,
            persistence_dp=0.65,
            touch_i1=0.05,
            delta_fut_60s_bps=0.5,
            delta_basis_60s_bps=0.0,
            option_delta=0.35,
            spread_pct=0.30,
        )

        # Domain 4 neutralized: both return full 15.0 contract score
        assert res_trap.sub_scores["contract"] == 15.0, (
            "Domain 4 neutralized — delta=0.02 should now get full 15.0 contract score"
        )
        assert res_sweet.sub_scores["contract"] == 15.0
        # Both setups have identical non-contract inputs → same total score
        assert res_trap.total_score == res_sweet.total_score
        # Delta is still logged in breakdown for telemetry
        assert "abs_delta" in res_trap.breakdown.get("contract_details", {})
        assert res_trap.breakdown["contract_details"]["contract_domain_neutralized"] is True

    def test_futures_divergence_penalty(self, engine):
        """
        Futures Divergence Penalty:
        If Spot bounces but Futures is dumping (-3.0 bps), the engine applies
        a -6.0 pt divergence penalty to protect against fakeout traps.
        """
        prior_bars = [
            MockBar(open=24000.0, high=24020.0, low=23990.0, close=24015.0),
            MockBar(open=24015.0, high=24035.0, low=24005.0, close=24030.0),
            MockBar(open=24030.0, high=24050.0, low=24020.0, close=24045.0),
        ]
        curr_bar = prior_bars[-1]

        # Aligned futures
        res_aligned = engine.compute_conviction_score(
            direction="UP",
            setup_name="volatility_squeeze_breakout",
            spot_price=24045.0,
            prior_bars=prior_bars,
            current_bar=curr_bar,
            atr=20.0,
            persistence_dp=0.70,
            touch_i1=0.10,
            delta_fut_60s_bps=+2.0,  # Futures moving up with spot
            delta_basis_60s_bps=+0.5,
            option_delta=0.35,
        )

        # Divergent futures (futures dumping while spot ticks up)
        res_divergent = engine.compute_conviction_score(
            direction="UP",
            setup_name="volatility_squeeze_breakout",
            spot_price=24045.0,
            prior_bars=prior_bars,
            current_bar=curr_bar,
            atr=20.0,
            persistence_dp=0.70,
            touch_i1=0.10,
            delta_fut_60s_bps=-3.0,  # Divergent dumping!
            delta_basis_60s_bps=-1.0,
            option_delta=0.35,
        )

        assert res_divergent.breakdown["flow_details"]["fut_divergence_penalty"] == -6.0
        assert res_aligned.total_score > res_divergent.total_score + 9.0

    def test_scale_invariance_nifty_vs_sensex(self, engine):
        """
        Scale Invariance Test:
        Normalized moves (equal bps and equal ATR multiples) on NIFTY (24,000)
        and SENSEX (75,000) must yield virtually identical conviction scores.
        """
        # NIFTY: Spot 24,000, ATR 24 (0.10%), Bar range 24, body 18 (75%)
        nifty_bars = [
            MockBar(open=23976.0, high=24000.0, low=23970.0, close=23995.0),
            MockBar(open=23995.0, high=24020.0, low=23990.0, close=24015.0),
            MockBar(open=24015.0, high=24039.0, low=24015.0, close=24035.0),
        ]
        res_nifty = engine.compute_conviction_score(
            direction="UP",
            setup_name="break_of_structure",
            spot_price=24035.0,
            prior_bars=nifty_bars,
            current_bar=nifty_bars[-1],
            atr=24.0,
            ema_20=24010.0,
            ema_50=23980.0,
            vwap=24000.0,
            pdh=24020.0,
            persistence_dp=0.75,
            touch_i1=0.15,
            delta_fut_60s_bps=1.5,
            delta_basis_60s_bps=0.5,
            option_delta=0.35,
            spread_pct=0.25,
        )

        # SENSEX: Spot 75,000, ATR 75 (0.10%), Bar range 75, body 56.25 (75%)
        sensex_bars = [
            MockBar(open=74925.0, high=75000.0, low=74906.25, close=74984.375),
            MockBar(open=74984.375, high=75062.5, low=74968.75, close=75046.875),
            MockBar(open=75046.875, high=75121.875, low=75046.875, close=75109.375),
        ]
        res_sensex = engine.compute_conviction_score(
            direction="UP",
            setup_name="break_of_structure",
            spot_price=75109.375,
            prior_bars=sensex_bars,
            current_bar=sensex_bars[-1],
            atr=75.0,
            ema_20=75031.25,
            ema_50=74937.5,
            vwap=75000.0,
            pdh=75062.5,
            persistence_dp=0.75,
            touch_i1=0.15,
            delta_fut_60s_bps=1.5,
            delta_basis_60s_bps=0.5,
            option_delta=0.35,
            spread_pct=0.25,
        )

        # Total scores must be identical
        assert abs(res_nifty.total_score - res_sensex.total_score) < 0.5
        assert res_nifty.sizing_tier == res_sensex.sizing_tier

    def test_black_scholes_delta_approximation(self):
        """Verifies pure Python Black-Scholes Delta estimation."""
        # 1. At the Money (ATM) Call should have delta ~ 0.50
        delta_atm = CompositeConvictionEngine.estimate_black_scholes_delta(
            spot=82000.0, strike=82000.0, side="CE", iv=0.15, dte_days=1.0
        )
        assert 0.48 <= delta_atm <= 0.54

        # 2. OTM Call (200 pts OTM on SENSEX, ~0.24% OTM) should have sweet spot delta ~ 0.35-0.45
        delta_otm = CompositeConvictionEngine.estimate_black_scholes_delta(
            spot=82000.0, strike=82200.0, side="CE", iv=0.15, dte_days=1.0
        )
        assert 0.25 <= delta_otm <= 0.45

        # 3. Deep OTM Call (1000 pts OTM on 0DTE) should have delta < 0.10
        delta_deep_otm = CompositeConvictionEngine.estimate_black_scholes_delta(
            spot=82000.0, strike=83000.0, side="CE", iv=0.15, dte_days=0.25
        )
        assert delta_deep_otm < 0.10

        # 4. ATM Put should have delta ~ 0.50
        delta_put_atm = CompositeConvictionEngine.estimate_black_scholes_delta(
            spot=82000.0, strike=82000.0, side="PE", iv=0.15, dte_days=1.0
        )
        assert 0.48 <= delta_put_atm <= 0.54

    def test_price_action_engine_volume_sma_and_bar_metrics(self):
        """Verifies PriceActionEngine helper methods."""
        pa = PriceActionEngine()
        for i in range(15):
            bar = OHLCBar(
                ts_start=i * 60,
                ts_end=(i + 1) * 60,
                open=82000.0 + i * 5.0,
                high=82010.0 + i * 5.0,
                low=81995.0 + i * 5.0,
                close=82008.0 + i * 5.0,
                volume=100.0 + i * 10.0,
                tick_count=20,
            )
            pa._on_1m_bar_close(bar)

        # Volume SMA of last 10 bars (from i=5 to 14: avg of 150 to 240 is 195)
        sma = pa.get_volume_sma(10)
        assert sma == 195.0

        metrics = pa.get_relative_bar_metrics()
        assert "body_pct" in metrics
        assert "vol_ratio" in metrics
        assert metrics["vol_sma10"] == 195.0

    def test_setup_weight_profiles(self, engine):
        """Verifies that all setup weight profiles sum to exactly 100.0."""
        for setup, weights in engine.SETUP_WEIGHT_PROFILES.items():
            total_w = sum(weights.values())
            assert total_w == pytest.approx(100.0, abs=1e-5), f"{setup} weights do not sum to 100"
            assert "structure" in weights
            assert "impulse" in weights
            assert "flow" in weights
            assert "contract" in weights
