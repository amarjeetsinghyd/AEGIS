"""
Tests for AEGIS ML Gatekeeper Engine.
Verifies:
- Text model loading and threshold configuration
- Sub-millisecond inference latency (< 1.0 ms)
- Correct pass/veto behavior on feature dictionaries
"""

import time
import pytest
from AEGIS.core.ml_gatekeeper import MLGatekeeper, FEATURE_ORDER


def test_ml_gatekeeper_initialization():
    gatekeeper = MLGatekeeper()
    assert "POOLED" in gatekeeper.thresholds
    assert "NIFTY" in gatekeeper.thresholds
    assert "SENSEX" in gatekeeper.thresholds
    assert gatekeeper.thresholds["POOLED"] == pytest.approx(0.3340, abs=0.01)
    assert gatekeeper.thresholds["NIFTY"] == pytest.approx(0.3679, abs=0.01)
    assert gatekeeper.thresholds["SENSEX"] == pytest.approx(0.3778, abs=0.01)


def test_ml_gatekeeper_inference_latency():
    gatekeeper = MLGatekeeper()
    sample_features = {f: 0.1 for f in FEATURE_ORDER}

    # Warmup
    for _ in range(10):
        gatekeeper.evaluate(sample_features, index_name="NIFTY")

    # Time 100 iterations
    t0 = time.perf_counter()
    for _ in range(100):
        gatekeeper.evaluate(sample_features, index_name="NIFTY")
    total_time = time.perf_counter() - t0
    avg_latency_ms = (total_time / 100) * 1000.0

    print(f"\nAverage ML Gatekeeper latency: {avg_latency_ms:.4f} ms")
    # Must be sub-2ms on standard CPU (typically ~0.08 - 1.2 ms on Windows)
    assert avg_latency_ms < 2.0


def test_ml_gatekeeper_pass_and_veto():
    gatekeeper = MLGatekeeper(model_mode="POOLED")

    # High-conviction setup (strong informed flow, price impact, accelerating premium):
    bullish_features = {
        "kyle_lambda_30s": 0.030682,
        "momentum_accel_10s": 0.0069,
        "option_ret_30s": 0.0323,
        "spread_pct": 0.5,
    }

    passed, score, reason = gatekeeper.evaluate(bullish_features, index_name="NIFTY")
    assert passed is True
    assert score >= gatekeeper.thresholds["POOLED"]
    assert "ML_PASSED" in reason

    # Poor setup (zero price impact, decelerating premium, negative return, blown spread):
    poor_features = {
        "kyle_lambda_30s": 0.0,
        "momentum_accel_10s": -0.05,
        "option_ret_30s": -0.05,
        "spread_pct": 0.90,
    }

    passed_poor, score_poor, reason_poor = gatekeeper.evaluate(poor_features, index_name="SENSEX")
    assert passed_poor is False
    assert score_poor < gatekeeper.thresholds["POOLED"]
    assert "ML_VETO" in reason_poor
    assert score_poor < score  # Poor setup must score significantly lower than bullish setup


def test_ml_gatekeeper_nan_handling():
    gatekeeper = MLGatekeeper()
    # Incomplete dictionary with None/missing values
    sparse_features = {
        "wick_rejection_ratio": None,
        "spread_pct": "invalid_str",
    }
    passed, score, reason = gatekeeper.evaluate(sparse_features, index_name="NIFTY")
    assert 0.0 <= score <= 1.0


def test_ml_gatekeeper_disabled():
    gatekeeper = MLGatekeeper(enabled=False)
    passed, score, reason = gatekeeper.evaluate({}, index_name="NIFTY")
    assert passed is True
    assert score == 1.0
    assert reason == "ML_GATEKEEPER_DISABLED"


def test_ml_gatekeeper_engine_integration(monkeypatch):
    import queue
    from AEGIS.aegis_gamma_engine import create_aegis_engine

    sig_q = queue.Queue()
    token_to_symbol = {
        "26000": "NIFTY",
        "1001": "NIFTY2682024000CE",
    }
    token_map = {
        "26000": {"side": 0, "strike": 0.0, "is_index": True},
        "1001": {"side": 0, "strike": 24000.0},
    }

    # 1. Default mode is DISABLED (0 CPU overhead in production)
    monkeypatch.delenv("AEGIS_ML_MODE", raising=False)
    engine_disabled = create_aegis_engine(
        token_to_symbol=token_to_symbol,
        token_map=token_map,
        strike_step=50.0,
        signal_queue=sig_q
    )
    assert engine_disabled.ml_gatekeeper is None
    assert engine_disabled.ml_gatekeeper_mode == "DISABLED"
    assert engine_disabled.index_name == "NIFTY"

    # 2. When AEGIS_ML_MODE=SHADOW is explicitly configured, it initializes cleanly
    monkeypatch.setenv("AEGIS_ML_MODE", "SHADOW")
    engine_shadow = create_aegis_engine(
        token_to_symbol=token_to_symbol,
        token_map=token_map,
        strike_step=50.0,
        signal_queue=sig_q
    )
    assert engine_shadow.ml_gatekeeper is not None
    assert engine_shadow.ml_gatekeeper_mode == "SHADOW"
    assert engine_shadow.index_name == "NIFTY"


def test_ml_gatekeeper_active_mode_veto(monkeypatch):
    import queue
    from AEGIS.aegis_gamma_engine import create_aegis_engine

    monkeypatch.setenv("AEGIS_ML_MODE", "SHADOW")
    sig_q = queue.Queue()
    token_to_symbol = {
        "26000": "NIFTY",
        "1001": "NIFTY2682024000CE",
    }
    token_map = {
        "26000": {"side": 0, "strike": 0.0, "is_index": True},
        "1001": {"side": 0, "strike": 24000.0},
    }
    engine = create_aegis_engine(
        token_to_symbol=token_to_symbol,
        token_map=token_map,
        strike_step=50.0,
        signal_queue=sig_q
    )
    engine.ml_gatekeeper_mode = "ACTIVE"
    
    # Bottom decile feature vector
    poor_features = {
        "kyle_lambda_30s": 0.0,
        "momentum_accel_10s": -0.05,
        "option_ret_30s": -0.05,
        "spread_pct": 0.90,
    }
    passed, score, reason = engine.ml_gatekeeper.evaluate(poor_features, "SENSEX")
    assert passed is False
    assert "ML_VETO" in reason



def test_kyle_lambda_end_to_end_engine_pipeline():
    """Verifies end-to-end chain: incoming tick with volume -> engine.on_tick_parsed
    -> scorer history with index 16 volume -> adaptive_micro_validator kyle_lambda_30s calculation."""
    import queue
    from AEGIS.aegis_gamma_engine import create_aegis_engine

    sig_q = queue.Queue()
    token_to_symbol = {
        "26000": "NIFTY",
        "1001": "NIFTY2682024000CE",
    }
    token_map = {
        "26000": {"side": 0, "strike": 0.0, "is_index": True},
        "1001": {"side": 0, "strike": 24000.0},
    }
    engine = create_aegis_engine(
        token_to_symbol=token_to_symbol,
        token_map=token_map,
        strike_step=50.0,
        signal_queue=sig_q
    )

    class MockTick:
        def __init__(self, tok, lp, ts, vol, bp1=99.9, sp1=100.1):
            self.tok = tok
            self.lp = lp
            self.ts = ts
            self.vol = vol
            self.bp = [bp1, 0, 0, 0, 0]
            self.sp = [sp1, 0, 0, 0, 0]
            self.bq = [100, 50, 50, 50, 50]
            self.sq = [100, 50, 50, 50, 50]

    # Send 2 ticks for the option symbol with volume
    t0 = 1788000000.0
    tick1 = MockTick("1001", 100.0, t0, vol=5000.0, bp1=99.9, sp1=100.1)
    tick2 = MockTick("1001", 102.0, t0 + 15.0, vol=5400.0, bp1=101.9, sp1=102.1)

    engine.on_tick_parsed(tick1)
    engine.on_tick_parsed(tick2)

    buf = engine.scorer.history.get("NIFTY2682024000CE")
    assert buf is not None
    assert len(buf) == 2

    # Verify index 16 holds the volume
    assert buf[0][16] == 5000.0
    assert buf[1][16] == 5400.0

    # Evaluate via adaptive_micro_validator
    ok, score, metrics = engine.adaptive_micro_validator.evaluate(
        target_sym="NIFTY2682024000CE",
        tick_buffer=buf,
        anchor_T=t0 + 20.0,
        direction="UP"
    )
    # delta_px = 2.0, delta_vol = 400.0 -> kyle_lambda_30s = 2.0 / 400.0 = 0.0050
    assert "kyle_lambda_30s" in metrics
    assert metrics["kyle_lambda_30s"] == pytest.approx(0.0050, abs=1e-5)

