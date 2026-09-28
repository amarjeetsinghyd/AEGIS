"""Unit tests for AEGIS AdaptiveMicroValidator.

Verifies:
1. Clean explosive trade passes with full score (100/100).
2. Compensatory trade passes (flat price but massive buyer aggression & depth >= 60).
3. Decaying/dead trade rejected (< 60/100, e.g. 52/100).
4. Circuit breaker: Spread blowout (> 2.5%) instant veto.
5. Circuit breaker: Ghost book (< 200 depth) instant veto.
6. Circuit breaker: Severe flush (< -1.5%) instant veto.
7. Event-bound T slicing strictly within [T-30, T].
"""

import pytest
from AEGIS.core.adaptive_micro_validator import AdaptiveMicroValidator


def _make_tick(ts, lp, bp1, sp1, b1=100, a1=100, b2=100, a2=100, b3=100, a3=100, b4=100, a4=100, b5=100, a5=100):
    return (ts, lp, bp1, sp1, b1, a1, b2, a2, b3, a3, b4, a4, b5, a5)


def test_clean_explosive_pass_100():
    validator = AdaptiveMicroValidator(min_pass_score=60.0)
    anchor_T = 1000.0
    
    # 40 ticks from T-30 to T
    # Price rises from 27.00 to 27.50 (+1.85% -> 40 pts)
    # Touch I1: b1=500, a1=100 -> (500-100)/(600) = +0.667 (>= 0.35 -> 35 pts)
    # Velocity: 40 ticks, depth = 500*5 = 2500 (>= 30 ticks & >= 1000 depth -> 25 pts)
    ticks = []
    for i in range(40):
        t = anchor_T - 30.0 + i * (30.0 / 39)
        p = 27.00 + (0.50 * i / 39)
        ticks.append(_make_tick(t, p, p - 0.05, p + 0.05, b1=500, a1=100, b2=500, a2=100, b3=500, a3=100, b4=500, a4=100, b5=500, a5=100))

    passed, score, m = validator.evaluate("NIFTY26SEP23650CE", ticks, anchor_T=anchor_T, direction="CALL")
    assert passed is True
    assert score == 100.0
    assert m["score_momentum"] == 40.0
    assert m["score_aggression"] == 35.0
    assert m["score_velocity"] == 25.0
    assert m["reason"] == "energy_confirmed"


def test_compensatory_pass_flat_price_strong_buyers():
    """If price is consolidating (0.0% return = 24 pts) but buyers and volume step in heavily:
    24 + 35 + 25 = 84 pts >= 60 -> PASS."""
    validator = AdaptiveMicroValidator(min_pass_score=60.0)
    anchor_T = 1000.0
    
    ticks = []
    for i in range(35):
        t = anchor_T - 30.0 + i * (30.0 / 34)
        p = 30.00  # flat price
        ticks.append(_make_tick(t, p, 29.95, 30.05, b1=600, a1=100, b2=500, a2=100, b3=500, a3=100, b4=500, a4=100, b5=500, a5=100))

    passed, score, m = validator.evaluate("NIFTY26SEP23650CE", ticks, anchor_T=anchor_T, direction="CALL")
    assert passed is True
    assert score == 84.0  # 24 + 35 + 25
    assert m["score_momentum"] == 24.0
    assert m["score_aggression"] == 35.0
    assert m["score_velocity"] == 25.0


def test_decaying_trade_rejected_under_60():
    """Trade like 14:54: mild stall (-0.25% = 12 pts), balanced book (I1=0 = 20 pts), moderate ticks (20 pts)
    12 + 20 + 20 = 52 pts < 60 -> REJECT."""
    validator = AdaptiveMicroValidator(min_pass_score=60.0)
    anchor_T = 1000.0
    
    ticks = []
    for i in range(20):
        t = anchor_T - 30.0 + i * (30.0 / 19)
        p = 20.05 - (0.05 * i / 19)  # drops from 20.05 to 20.00 (-0.25%)
        # balanced touch
        ticks.append(_make_tick(t, p, p - 0.05, p + 0.05, b1=100, a1=100, b2=100, a2=100, b3=100, a3=100, b4=100, a4=100, b5=100, a5=100))

    passed, score, m = validator.evaluate("NIFTY26SEP23650CE", ticks, anchor_T=anchor_T, direction="CALL")
    assert passed is False
    assert score == 52.0  # 12 + 20 + 20
    assert m["reason"] == "energy_insufficient"


def test_circuit_breaker_spread_blowout():
    validator = AdaptiveMicroValidator(min_pass_score=60.0)
    anchor_T = 1000.0
    
    # Spread > 2.5% (e.g. bp1=20.0, sp1=21.0 -> mid 20.5, spread 4.88%)
    ticks = [
        _make_tick(anchor_T - 20.0, 20.0, 19.5, 20.5),
        _make_tick(anchor_T - 1.0, 20.5, 20.0, 21.0),
    ]
    passed, score, m = validator.evaluate("NIFTY26SEP23650CE", ticks, anchor_T=anchor_T)
    assert passed is False
    assert score == 0.0
    assert m["reason"] == "circuit_breaker_spread_blowout"


def test_circuit_breaker_ghost_book():
    validator = AdaptiveMicroValidator(min_pass_score=60.0)
    anchor_T = 1000.0
    
    # Total depth = 50 < 200
    ticks = [
        _make_tick(anchor_T - 20.0, 20.0, 19.95, 20.05, b1=10, a1=10, b2=5, a2=5, b3=5, a3=5, b4=5, a4=5, b5=0, a5=0),
        _make_tick(anchor_T - 1.0, 20.2, 20.15, 20.25, b1=10, a1=10, b2=5, a2=5, b3=5, a3=5, b4=5, a4=5, b5=0, a5=0),
    ]
    passed, score, m = validator.evaluate("NIFTY26SEP23650CE", ticks, anchor_T=anchor_T)
    assert passed is False
    assert score == 0.0
    assert m["reason"] == "circuit_breaker_ghost_book"


def test_circuit_breaker_severe_dump():
    validator = AdaptiveMicroValidator(min_pass_score=60.0)
    anchor_T = 1000.0
    
    # Drops from 30.00 to 29.40 (-2.0% < -1.5%)
    ticks = [
        _make_tick(anchor_T - 25.0, 30.00, 29.95, 30.05, b1=500, a1=500),
        _make_tick(anchor_T - 2.0, 29.40, 29.35, 29.45, b1=500, a1=500),
    ]
    passed, score, m = validator.evaluate("NIFTY26SEP23650CE", ticks, anchor_T=anchor_T)
    assert passed is False
    assert score == 0.0
    assert m["reason"] == "circuit_breaker_severe_dump"


def test_kyle_lambda_volume_parity():
    validator = AdaptiveMicroValidator()
    anchor_T = 1000.0
    # Tick with 17 elements: (ts, lp, bp1, sp1, b1..5, a1..5, d_ofi, d_q, vol)
    t0 = (anchor_T - 20.0, 100.0, 99.9, 100.1, 100, 100, 50, 50, 50, 50, 50, 50, 50, 50, 10.0, 200.0, 5000.0)
    t1 = (anchor_T - 1.0, 102.0, 101.9, 102.1, 100, 100, 50, 50, 50, 50, 50, 50, 50, 50, 25.0, 200.0, 5400.0)
    
    passed, score, m = validator.evaluate("NIFTY26SEP24000CE", [t0, t1], anchor_T=anchor_T)
    # delta_px = 102.0 - 100.0 = 2.0
    # delta_vol = 5400.0 - 5000.0 = 400.0
    # kyle_lambda = 2.0 / 400.0 = 0.0050
    assert m["kyle_lambda_30s"] == pytest.approx(0.0050, abs=1e-5)


def test_strict_bos_vetoes_balanced_book_breakout():
    """BOS-strict gate (2026-09-16): price explodes (+1.85% = 40 pts) but the
    book stays balanced (I1 = 0.0). Legacy path scores 40+20+25 = 85 and
    passes; strict BOS must discard it as bos_weak_aggression — no momentum
    behind the break."""
    validator = AdaptiveMicroValidator()
    anchor_T = 1000.0

    ticks = []
    for i in range(40):
        t = anchor_T - 30.0 + i * (30.0 / 39)
        # Accelerating curve (flat start, steep finish): momentum builds
        # into the close so only the aggression veto can fire.
        p = 27.00 + 0.50 * (i / 39) ** 2
        ticks.append(_make_tick(t, p, p - 0.05, p + 0.05, b1=100, a1=100, b2=100, a2=100, b3=100, a3=100, b4=100, a4=100, b5=100, a5=100))

    passed_loose, score_loose, _ = validator.evaluate("NIFTY26SEP24000CE", ticks, anchor_T=anchor_T, direction="CALL")
    assert passed_loose is True
    assert score_loose == 85.0

    passed, score, m = validator.evaluate(
        "NIFTY26SEP24000CE", ticks, anchor_T=anchor_T, direction="CALL", strict_bos=True
    )
    assert passed is False
    assert score == 85.0
    assert m["reason"] == "bos_weak_aggression"
    assert m["strict_bos"] is True


def test_strict_bos_passes_genuine_momentum_breakout():
    """BOS-strict gate passes a genuine breakout: rising price, aggressive
    buyers (I1 >= 0.10), accelerating close, no deep sell wall."""
    validator = AdaptiveMicroValidator()
    anchor_T = 1000.0

    ticks = []
    for i in range(40):
        t = anchor_T - 30.0 + i * (30.0 / 39)
        p = 27.00 + 0.50 * (i / 39) ** 2
        ticks.append(_make_tick(t, p, p - 0.05, p + 0.05, b1=500, a1=100, b2=500, a2=100, b3=500, a3=100, b4=500, a4=100, b5=500, a5=100))

    passed, score, m = validator.evaluate(
        "NIFTY26SEP24000CE", ticks, anchor_T=anchor_T, direction="CALL", strict_bos=True
    )
    assert passed is True
    assert score == 100.0
    assert m["reason"] == "energy_confirmed"


def test_strict_bos_vetoes_deep_sell_wall():
    """BOS-strict gate discards a breakout into a deep L3-L5 sell wall
    (deep_pressure < -0.60) even when touch buyers look aggressive."""
    validator = AdaptiveMicroValidator()
    anchor_T = 1000.0

    ticks = []
    for i in range(40):
        t = anchor_T - 30.0 + i * (30.0 / 39)
        p = 27.00 + 0.50 * (i / 39) ** 2
        # L1 aggressive buyers, but L3-L5 stacked with sellers.
        ticks.append(_make_tick(t, p, p - 0.05, p + 0.05, b1=500, a1=100, b2=500, a2=100, b3=50, a3=500, b4=50, a4=500, b5=50, a5=500))

    passed, score, m = validator.evaluate(
        "NIFTY26SEP24000CE", ticks, anchor_T=anchor_T, direction="CALL", strict_bos=True
    )
    assert passed is False
    assert m["reason"] == "bos_deep_opposition"


def test_prior_60s_candle_lookback_captures_early_burst():
    """Validates that lookback_sec=60.0 captures an order-flow burst that occurred
    in the first half of the 1-minute candle [T-60 to T-30] even if [T-30 to T] is quiet."""
    validator = AdaptiveMicroValidator()
    anchor_T = 1000.0

    ticks = []
    # 1. First 30s of the candle (T-60 to T-30): massive buyer aggression and price surge
    for i in range(20):
        t = anchor_T - 60.0 + i * (30.0 / 19)
        p = 20.0 + 1.50 * (i / 19)  # 20.0 -> 21.50 (+7.5% surge)
        ticks.append(_make_tick(t, p, p - 0.05, p + 0.05, b1=600, a1=100, b2=500, a2=100))

    # 2. Final 30s of the candle (T-30 to T): quiet drift (price drifts 21.50 -> 21.35, mild ask dominance)
    for i in range(20):
        t = anchor_T - 30.0 + (i + 1) * (30.0 / 20)
        p = 21.50 - 0.15 * (i / 19)  # mild dip (-0.7%)
        ticks.append(_make_tick(t, p, p - 0.05, p + 0.05, b1=100, a1=300, b2=100, a2=300))

    # Slicing strictly to 30s misses the early surge -> rejected
    passed_30, score_30, m_30 = validator.evaluate(
        "NIFTY26SEP24000CE", ticks, anchor_T=anchor_T, direction="CALL", lookback_sec=30.0
    )
    assert passed_30 is False

    # Slicing to 60s captures the full candle surge -> passed!
    passed_60, score_60, m_60 = validator.evaluate(
        "NIFTY26SEP24000CE", ticks, anchor_T=anchor_T, direction="CALL", lookback_sec=60.0
    )
    assert passed_60 is True
    assert score_60 >= 60.0
    assert m_60["micro_ret"] > 0.05

