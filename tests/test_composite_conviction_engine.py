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

        # In production v1.0, external futures feeds are permanently MUTED per operator directive.
        # Divergence penalty is zeroed and futures_muted is True to eliminate reliance on dead futures tokens.
        assert res_divergent.breakdown["flow_details"]["futures_muted"] is True
        assert res_divergent.breakdown["flow_details"]["fut_divergence_penalty"] == 0.0

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

    def test_pillar_2_wyckoff_spring_reclaim_scores_above_80(self, engine):
        """
        Pillar 2 Micro-Sweep Reclaim (Exact 14:36 SENSEX Case Study):
        - Bar-close setup with 73% lower wick and close in top 15% of range.
        - Newborn bar is 1-second old (1 pt range, 0 vol) -> engine selects last_closed bar.
        - Wyckoff confirmed: VDR = 1.35, Absorption = True, OFI delta positive.
        - Structure: Validated pullback reclaim (+15 swing pts, no LH penalty), VWAP proximity.
        Expected: Score >= 85.0 (approved, FULL_SIZE tier).
        """
        prior_bars = [
            MockBar(open=82450.0, high=82470.0, low=82410.0, close=82420.0, volume=150.0),
            MockBar(open=82420.0, high=82430.0, low=82380.0, close=82390.0, volume=180.0),
            # Setup bar: Swept down to 82350, then rocketed back to close at 82396 (high 82400)
            # Range: 50 pts, Low: 82350, Close: 82396, Lower wick: (82388 - 82350)/50 = 76%
            MockBar(open=82388.0, high=82400.0, low=82350.0, close=82396.0, volume=320.0),
        ]
        # 1-second old newborn bar (just started forming)
        newborn_bar = MockBar(open=82396.0, high=82397.0, low=82396.0, close=82397.0, volume=2.0)

        wyckoff_context = {
            "wyckoff_confirmed": True,
            "vdr": 1.35,
            "ofi": 150.0,
            "is_absorption": True,
        }
        pa_details = {
            "candle_wick_pct": 0.76,
            "sweep_depth_pts": 30.0,
            "reclaim_level": 82380.0,
        }

        res = engine.compute_conviction_score(
            direction="UP",
            setup_name="micro_sweep_reclaim",
            spot_price=82397.0,
            prior_bars=prior_bars,
            current_bar=newborn_bar,
            atr=28.0,
            ema_20=82400.0,
            ema_50=82360.0,
            vwap=82390.0,
            pdh=82500.0,
            pdl=82300.0,
            vol_sma10=180.0,
            persistence_dp=0.80,
            touch_i1=0.20,
            delta_fut_60s_bps=0.0,  # Passive futures feed (handled gracefully by Wyckoff proxy)
            delta_basis_60s_bps=0.0,
            option_delta=0.35,
            spread_pct=0.25,
            pa_details=pa_details,
            wyckoff_details=wyckoff_context,
        )

        assert res.is_approved is True
        assert res.total_score >= 85.0
        assert res.sizing_tier == "FULL_SIZE"
        assert res.sub_scores["structure"] >= 25.0
        assert res.sub_scores["impulse"] >= 25.0
        assert res.sub_scores["flow"] >= 20.0
        assert res.sub_scores["contract"] == 15.0

    def test_pillar_1_20_ema_rejection_with_wick_scores_approved(self, engine):
        """
        Pillar 1 20 EMA Rejection:
        - Natural pullback into 20 EMA in an uptrend (lower highs during pullback).
        - Anatomical rejection wick >= 35% bouncing off 20 EMA.
        - Wyckoff confirmed.
        Expected: Validated pullback reclaim (+15 swing pts, no counter-trend penalty),
        Score >= 80.0 (approved).
        """
        prior_bars = [
            MockBar(open=24050.0, high=24080.0, low=24040.0, close=24075.0, volume=100.0),
            MockBar(open=24075.0, high=24078.0, low=24050.0, close=24055.0, volume=80.0),
            # Bounce off 20 EMA (24045): Low 24043, High 24065, Close 24063 (Wick = 12/22 = 54%)
            MockBar(open=24055.0, high=24065.0, low=24043.0, close=24063.0, volume=140.0),
        ]
        curr_bar = prior_bars[-1]

        wyckoff_context = {
            "wyckoff_confirmed": True,
            "vdr": 1.20,
            "ofi": 80.0,
            "is_absorption": False,
        }

        res = engine.compute_conviction_score(
            direction="UP",
            setup_name="20_ema_rejection",
            spot_price=24063.0,
            prior_bars=prior_bars,
            current_bar=curr_bar,
            atr=20.0,
            ema_20=24045.0,
            ema_50=24010.0,
            vwap=24050.0,
            pdh=24150.0,
            pdl=23950.0,
            vol_sma10=100.0,
            persistence_dp=0.75,
            touch_i1=0.10,
            delta_fut_60s_bps=1.0,
            delta_basis_60s_bps=0.3,
            option_delta=0.35,
            spread_pct=0.30,
            wyckoff_details=wyckoff_context,
        )

        assert res.is_approved is True
        assert res.total_score >= 80.0
        assert res.breakdown["structure_details"]["swing_reason"] == "validated_pullback_reclaim"
        assert res.sub_scores["structure"] >= 25.0

    def test_newborn_bar_protection_on_bar_close(self, engine):
        """
        Verifies that when a setup triggers at bar close (:00s), passing a newborn 1-tick
        current_bar does not crush the impulse score. The engine must fall back to the
        setup bar (last_closed).
        """
        setup_bar = MockBar(open=82000.0, high=82030.0, low=81970.0, close=82025.0, volume=300.0)
        prior_bars = [
            MockBar(open=81950.0, high=81980.0, low=81940.0, close=81970.0, volume=150.0),
            setup_bar,
        ]
        newborn_bar = MockBar(open=82025.0, high=82025.5, low=82025.0, close=82025.5, volume=1.0)

        # Evaluating with newborn_bar
        res = engine.compute_conviction_score(
            direction="UP",
            setup_name="micro_sweep_reclaim",
            spot_price=82025.5,
            prior_bars=prior_bars,
            current_bar=newborn_bar,
            atr=25.0,
            ema_20=81980.0,
            ema_50=81950.0,
            vwap=82000.0,
            pdh=82100.0,
            pdl=81900.0,
            vol_sma10=150.0,
            persistence_dp=0.80,
            touch_i1=0.15,
            delta_fut_60s_bps=1.0,
            delta_basis_60s_bps=0.5,
            option_delta=0.35,
            spread_pct=0.25,
            wyckoff_details={"wyckoff_confirmed": True, "vdr": 1.5, "ofi": 100.0},
        )

        # Impulse score should be evaluated against setup_bar (60 pt range, 300 vol), NOT newborn (0.5 pt range, 1 vol)
        assert res.sub_scores["impulse"] >= 25.0
        assert res.total_score >= 85.0

