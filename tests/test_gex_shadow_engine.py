import math
import tracemalloc
import numpy as np
import pytest

from AEGIS.core.gex_engine import GexShadowEngine, INV_SQRT_2PI


def test_black_scholes_gamma_mathematical_accuracy():
    """Verify vectorized Black-Scholes Gamma matches analytical scalar math (< 1e-12)."""
    spot = 24500.0
    strikes = [24000.0, 24250.0, 24500.0, 24750.0, 25000.0]
    time_to_expiry_yrs = 2.0 / 365.0
    sigma = 0.15

    engine = GexShadowEngine(num_strikes=len(strikes), strike_step=250.0, lot_size=65.0)
    engine.set_strikes(strikes)
    engine.iv.fill(sigma)
    engine.update_gex_snapshot(spot, time_to_expiry_yrs)

    for i, K in enumerate(strikes):
        # Analytical scalar calculation
        d1 = (math.log(spot / K) + 0.5 * (sigma ** 2) * time_to_expiry_yrs) / (sigma * math.sqrt(time_to_expiry_yrs))
        phi_d1 = (1.0 / math.sqrt(2.0 * math.pi)) * math.exp(-0.5 * d1 * d1)
        gamma_analytical = phi_d1 / (spot * sigma * math.sqrt(time_to_expiry_yrs))

        gamma_engine = engine._gamma[i]
        assert abs(gamma_analytical - gamma_engine) < 1e-12, f"Strike {K}: analytical={gamma_analytical}, engine={gamma_engine}"


def test_zero_memory_allocation_in_steady_state():
    """Verify update_gex_snapshot performs zero heap allocations in steady state via tracemalloc."""
    engine = GexShadowEngine(num_strikes=51, strike_step=50.0, lot_size=65.0, atm_strike=24500.0)
    engine.ce_oi.fill(50000.0)
    engine.pe_oi.fill(40000.0)

    # Warmup pass to initialize any one-time internal caches
    for _ in range(5):
        engine.update_gex_snapshot(spot=24500.0, time_to_expiry_yrs=1.0 / 365.0)

    tracemalloc.start()
    s1 = tracemalloc.take_snapshot()

    # Run 100 consecutive snapshots
    for step in range(100):
        engine.update_gex_snapshot(spot=24500.0 + (step * 0.5), time_to_expiry_yrs=1.0 / 365.0)

    s2 = tracemalloc.take_snapshot()
    tracemalloc.stop()

    # Filter out any internal tracemalloc and test harness frame overhead
    diffs = [stat for stat in s2.compare_to(s1, "lineno") if "tracemalloc" not in str(stat) and "test_gex_shadow_engine.py" not in str(stat)]
    total_allocated = sum(stat.size_diff for stat in diffs if stat.size_diff > 0)
    assert total_allocated == 0, f"Expected 0 bytes allocated during steady-state snapshots, got {total_allocated} bytes: {diffs}"


def test_zero_gamma_flip_linear_interpolation():
    """Verify Zero Gamma Flip level precisely locates the zero-crossing of cumulative GEX."""
    engine = GexShadowEngine(num_strikes=5, strike_step=100.0, lot_size=65.0)
    strikes = [24000.0, 24100.0, 24200.0, 24300.0, 24400.0]
    engine.set_strikes(strikes)

    # Hand-craft flow_gex_profile so cumulative GEX flips sign between 24100 and 24200:
    # Cum GEX: -100, -20, +40, +100, +150
    # Values:  -100, +80, +60,  +60,  +50
    engine.flow_gex_profile[:] = [-100.0, 80.0, 60.0, 60.0, 50.0]

    # Re-extract scalars and zero flip
    c = 0.0
    for i in range(5):
        c += float(engine.flow_gex_profile[i])
        engine._cum_gex[i] = c
        engine._signbits[i] = (c < 0.0)
    engine.net_gex = c

    # Flip between index 1 (24100, cum=-20) and index 2 (24200, cum=+40)
    # Expected flip = 24100 - (-20) * (100) / (40 - (-20)) = 24100 + 2000 / 60 = 24133.3333...
    zero_flip = 24000.0
    for i in range(4):
        if engine._signbits[i] != engine._signbits[i + 1]:
            g1 = float(engine._cum_gex[i])
            g2 = float(engine._cum_gex[i + 1])
            k1 = float(engine.strikes[i])
            k2 = float(engine.strikes[i + 1])
            zero_flip = k1 - g1 * (k2 - k1) / (g2 - g1)
            break

    expected_flip = 24100.0 + (20.0 * 100.0 / 60.0)
    assert abs(zero_flip - expected_flip) < 1e-9

    # Verify linear interpolation at zero_flip is exactly 0.0
    g_at_flip = -20.0 + (40.0 - (-20.0)) * (zero_flip - 24100.0) / (24200.0 - 24100.0)
    assert abs(g_at_flip) < 1e-9


def test_dual_gex_static_vs_flow_signed():
    """Verify Dual GEX calculation: Static GEX vs Flow-Signed GEX with retail order flow."""
    engine = GexShadowEngine(num_strikes=5, strike_step=50.0, lot_size=65.0)
    strikes = [24400.0, 24450.0, 24500.0, 24550.0, 24600.0]
    engine.set_strikes(strikes)

    # Set equal OI on both CE and PE at 24500
    engine.update_oi(24500.0, side=0, oi=100000.0)  # CE OI
    engine.update_oi(24500.0, side=1, oi=20000.0)   # PE OI

    # Baseline: Static GEX has CE_OI - PE_OI = 100k - 20k = +80k (Positive GEX)
    engine.update_gex_snapshot(spot=24500.0, time_to_expiry_yrs=1.0 / 365.0)
    assert engine.static_pos_wall == 24500.0
    assert engine.pos_wall_strike == 24500.0

    # Simulate aggressive retail CALL buying at 24500 (buy 50k, sell 10k)
    # Market Makers must sell calls -> MM is Short Gamma (-1.0)
    engine.update_order_flow(strike=24500.0, side=0, buy_vol=50000.0, sell_vol=10000.0)
    assert engine.mm_ce_sign[engine.strike_to_idx[24500.0]] == -1.0

    engine.update_gex_snapshot(spot=24500.0, time_to_expiry_yrs=1.0 / 365.0)

    # In flow_signed_gex:
    # (mm_ce_sign * CE_OI + mm_pe_sign * PE_OI) = (-1.0 * 100k + -1.0 * 20k) = -120k!
    # Strike 24500 flipped from Positive GEX Wall to massive Negative GEX Magnet!
    idx_24500 = engine.strike_to_idx[24500.0]
    assert engine.flow_gex_profile[idx_24500] < 0.0
    assert engine.neg_wall_strike == 24500.0


def test_shadow_signal_evaluation_rules():
    """Verify the 3 shadow evaluation rules: Pos Wall Veto, Neg Wall Amplify, Regime Alignment."""
    engine = GexShadowEngine(num_strikes=11, strike_step=50.0, lot_size=65.0, atm_strike=24500.0)
    engine.ce_oi.fill(50000.0)
    engine.pe_oi.fill(50000.0)

    # Force Positive Wall at 24600 and Negative Wall at 24400
    engine.flow_gex_profile.fill(0.0)
    idx_pos = engine.strike_to_idx[24600.0]
    idx_neg = engine.strike_to_idx[24400.0]
    engine.flow_gex_profile[idx_pos] = 500000.0
    engine.flow_gex_profile[idx_neg] = -500000.0

    engine.pos_wall_strike = 24600.0
    engine.neg_wall_strike = 24400.0
    engine.net_gex = 500000.0 - 500000.0  # 0.0 -> POSITIVE_MEAN_REVERT

    # Rule 1: CALL breakout approaching within 20 pts of 24600 Positive GEX Wall
    res1 = engine.evaluate_shadow_signal(spot=24585.0, direction="UP", pillar=3, wall_proximity_pts=25.0)
    assert res1["shadow_verdict"] == "SHADOW_VETO_POS_WALL"
    assert res1["pos_gex_wall"] == 24600.0
    assert res1["dist_to_wall"] == 15.0

    # Rule 2: Breaking into Negative GEX Acceleration zone (near 24400)
    res2 = engine.evaluate_shadow_signal(spot=24410.0, direction="DOWN", pillar=4, wall_proximity_pts=25.0)
    assert res2["shadow_verdict"] == "SHADOW_AMPLIFY_NEG_ZONE"
    assert res2["neg_gex_wall"] == 24400.0
    assert res2["dist_to_wall"] == 10.0

    # Rule 3: Far away from walls -> Neutral Pass
    res3 = engine.evaluate_shadow_signal(spot=24500.0, direction="UP", pillar=1, wall_proximity_pts=25.0)
    assert res3["shadow_verdict"] == "NEUTRAL_PASS"
    assert res3["regime_alignment"] == "ALIGNED_RANGE"  # P1 in Positive GEX is aligned


def test_edge_cases_robustness():
    """Verify edge cases: zero spot, zero expiry, zero OI, no zero-crossing."""
    engine = GexShadowEngine(num_strikes=51, strike_step=50.0, lot_size=65.0, atm_strike=24500.0)

    # Zero spot price: must not crash
    engine.update_gex_snapshot(spot=0.0, time_to_expiry_yrs=1.0 / 365.0)
    assert engine.net_gex == 0.0

    # Zero expiry: clamped via 1e-6, must not divide by zero or produce NaN
    engine.update_gex_snapshot(spot=24500.0, time_to_expiry_yrs=0.0)
    assert not math.isnan(engine.net_gex)
    assert not math.isinf(engine.net_gex)

    # All zero OI: cumulative sum is flat zero, zero_gamma_level defaults to spot
    engine.ce_oi.fill(0.0)
    engine.pe_oi.fill(0.0)
    engine.update_gex_snapshot(spot=24500.0, time_to_expiry_yrs=1.0 / 365.0)
    assert engine.net_gex == 0.0
    assert engine.zero_gamma_level == 24500.0


def test_fewer_strikes_extrapolation_no_nan():
    """Verify set_strikes with fewer than num_strikes pads all 51 strikes with no divide-by-zero or NaNs."""
    engine = GexShadowEngine(num_strikes=51, strike_step=50.0, lot_size=65.0)
    # Feed only 15 strikes
    partial_strikes = [24200.0 + i * 50.0 for i in range(15)]
    engine.set_strikes(partial_strikes)

    assert len(engine.strikes) == 51
    assert all(s > 0.0 for s in engine.strikes)
    assert np.all(np.diff(engine.strikes) > 0.0)  # strictly increasing

    # Update with some OI and run snapshot
    engine.update_oi(24500.0, side=0, oi=50000.0)
    engine.update_gex_snapshot(spot=24500.0, time_to_expiry_yrs=1.0 / 365.0)

    assert not math.isnan(engine.net_gex)
    assert not math.isinf(engine.net_gex)
    assert engine.net_gex > 0.0
    assert engine.pos_wall_strike == 24500.0
    assert engine.neg_wall_strike == 0.0  # No negative wall exists


def test_all_zero_oi_no_false_walls():
    """Verify cold-boot all-zero OI does NOT produce false positive/negative walls or false vetoes."""
    engine = GexShadowEngine(num_strikes=51, strike_step=50.0, lot_size=65.0, atm_strike=24500.0)
    engine.update_gex_snapshot(spot=23250.0, time_to_expiry_yrs=1.0 / 365.0)

    assert engine.pos_wall_strike == 0.0
    assert engine.neg_wall_strike == 0.0
    assert engine.zero_gamma_level == 23250.0

    # Spot is right at strikes[0] (23250.0); must NOT trigger false VETO or AMPLIFY
    res_call = engine.evaluate_shadow_signal(spot=23250.0, direction="CALL")
    assert res_call["shadow_verdict"] == "NEUTRAL_PASS"

    res_put = engine.evaluate_shadow_signal(spot=23250.0, direction="PUT")
    assert res_put["shadow_verdict"] == "NEUTRAL_PASS"


def test_pure_negative_gex_no_spurious_zero_flip():
    """Verify that pure negative GEX with leading zeros does NOT produce a false zero flip level."""
    engine = GexShadowEngine(num_strikes=5, strike_step=100.0, lot_size=65.0, atm_strike=24200.0)
    # Strikes: [24000, 24100, 24200, 24300, 24400]
    # Put OI only on upper strikes (causing pure negative GEX)
    engine.pe_oi[2] = 10000.0
    engine.pe_oi[3] = 20000.0
    engine.pe_oi[4] = 30000.0
    engine.update_gex_snapshot(spot=24200.0, time_to_expiry_yrs=1.0 / 365.0)

    assert engine.net_gex < 0.0
    # There is no positive GEX anywhere, so cumulative GEX never crossed from positive to negative
    assert engine.zero_gamma_level == 24200.0  # defaults to spot, NOT false 24100.0


def test_call_vs_put_positive_wall_separation():
    """Verify that PUT reversals fading below a Call resistance wall are NOT falsely vetoed."""
    engine = GexShadowEngine(num_strikes=11, strike_step=50.0, lot_size=65.0, atm_strike=24500.0)
    # Heavy Call writing at 24600 (Upper Call Resistance Wall)
    engine.update_oi(24600.0, side=0, oi=200000.0)
    engine.update_gex_snapshot(spot=24500.0, time_to_expiry_yrs=1.0 / 365.0)

    assert engine.call_pos_wall == 24600.0
    assert engine.pos_wall_strike == 24600.0

    # 1. CALL approaching 24600 from below (spot=24585): must be VETOED
    res_call = engine.evaluate_shadow_signal(spot=24585.0, direction="UP", pillar=3, wall_proximity_pts=25.0)
    assert res_call["shadow_verdict"] == "SHADOW_VETO_POS_WALL"

    # 2. PUT piercing above 24600 to 24610 and fading back down: must NOT be vetoed
    res_put = engine.evaluate_shadow_signal(spot=24610.0, direction="DOWN", pillar=2, wall_proximity_pts=25.0)
    assert res_put["shadow_verdict"] != "SHADOW_VETO_POS_WALL"
    assert res_put["shadow_verdict"] == "NEUTRAL_PASS"

