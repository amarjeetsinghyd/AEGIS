"""
Unit tests for AEGIS 5-Minute Rolling Confluence Memory & Dropped Trade Audit Engine.
Verifies:
1. 1-minute ML score recording in rolling memory.
2. Confluence passes when peak score in last 5 minutes >= 40.0%.
3. Confluence vetoes when all scores in last 5 minutes < 40.0%.
4. Stale scores older than 300s are correctly expired.
5. Counterfactual 5-minute outcome tracking on dropped trades.
"""

import time
import pytest
from AEGIS.core.ml_gatekeeper import MLGatekeeper
from AEGIS.core.outcome_telemetry import OutcomeTelemetryEngine, ForwardOutcomeRecord


def test_record_1m_bar_stores_in_deque():
    gk = MLGatekeeper()
    assert len(gk.recent_scores) == 0

    sample_feats = {
        "put_aggression": 0.1,
        "call_buy_vol": 100000.0,
        "call_aggression": 0.2,
        "avg_book_imb": 0.05,
        "upper_wick_bps": 2.0,
        "ema20_dist_bps": 5.0,
        "lower_wick_bps": 1.0,
        "opt_flow_ratio": 1.5,
        "range_bps": 10.0,
        "body_bps": 4.0,
        "put_buy_vol": 50000.0,
        "opt_delta_3m": 150000.0,
        "ema5_dist_bps": 3.0,
        "opt_total_vol": 200000.0,
        "opt_pcr": 0.5,
        "opt_delta_flow": 50000.0,
        "ret_1m_bps": 3.0,
    }

    now = time.time()
    score = gk.record_1m_bar(now, sample_feats, index_name="SENSEX")
    assert len(gk.recent_scores) == 1
    assert gk.recent_scores[0]["score"] == score
    assert gk.recent_scores[0]["ts"] == now


def test_rolling_confluence_passes_when_recent_peak_above_threshold():
    gk = MLGatekeeper()
    now = 1789540000.0  # Fixed timestamp

    # Minute T-180s: High-conviction flow spike (score = 0.504)
    gk.recent_scores.append({"ts": now - 180.0, "score": 0.504, "features": {}})
    # Minute T-60s: Pullback retest (score = 0.38)
    gk.recent_scores.append({"ts": now - 60.0, "score": 0.380, "features": {}})
    # Minute T (current): Bounce (score = 0.36)
    gk.recent_scores.append({"ts": now, "score": 0.360, "features": {}})

    # Check confluence at current time T
    passed, max_score, details = gk.check_rolling_confluence(
        threshold=0.40, window_sec=300, current_ts=now
    )

    assert passed is True
    assert max_score == pytest.approx(0.504, abs=0.001)
    assert details["peak_age_sec"] == pytest.approx(180.0, abs=0.5)
    assert "CONFLUENCE_PASSED" in details["reason"]


def test_rolling_confluence_vetoes_when_all_scores_below_threshold():
    gk = MLGatekeeper()
    now = 1789540000.0

    # Low scores across all 5 minutes (choppy market)
    gk.recent_scores.append({"ts": now - 240.0, "score": 0.22, "features": {}})
    gk.recent_scores.append({"ts": now - 180.0, "score": 0.25, "features": {}})
    gk.recent_scores.append({"ts": now - 120.0, "score": 0.31, "features": {}})
    gk.recent_scores.append({"ts": now - 60.0, "score": 0.28, "features": {}})
    gk.recent_scores.append({"ts": now, "score": 0.29, "features": {}})

    passed, max_score, details = gk.check_rolling_confluence(
        threshold=0.40, window_sec=300, current_ts=now
    )

    assert passed is False
    assert max_score == pytest.approx(0.31, abs=0.001)
    assert "CONFLUENCE_VETO" in details["reason"]


def test_rolling_confluence_ignores_stale_scores():
    gk = MLGatekeeper()
    now = 1789540000.0

    # Old spike 400 seconds ago (older than 300s window)
    gk.recent_scores.append({"ts": now - 400.0, "score": 0.65, "features": {}})
    # In-window recent score is cold
    gk.recent_scores.append({"ts": now - 60.0, "score": 0.25, "features": {}})

    passed, max_score, details = gk.check_rolling_confluence(
        threshold=0.40, window_sec=300, current_ts=now
    )

    assert passed is False
    assert max_score == pytest.approx(0.25, abs=0.001)


def test_outcome_telemetry_tracks_dropped_trades(tmp_path):
    telemetry = OutcomeTelemetryEngine(log_dir=str(tmp_path))
    t0 = 1789540000.0

    # Register a trade that was dropped by ML
    telemetry.register_entry(
        event_id="VETO_TEST_1",
        symbol="SENSEX26SEP75000CE",
        direction="UP",
        entry_ts=t0,
        entry_opt_price=100.0,
        entry_spot_price=75000.0,
        eqs_score=80.0,
        trade_status="DROPPED_BY_ML",
    )

    assert "VETO_TEST_1" in telemetry.active_records
    assert telemetry.active_records["VETO_TEST_1"].trade_status == "DROPPED_BY_ML"

    # Simulate price decay over 5 minutes (a trap that would have hit SL)
    telemetry.update_tick("SENSEX26SEP75000CE", 98.0, t0 + 60.0)
    telemetry.update_tick("SENSEX26SEP75000CE", 95.0, t0 + 180.0)
    completed = telemetry.update_tick("SENSEX26SEP75000CE", 92.0, t0 + 301.0)

    assert len(completed) == 1
    rec = completed[0]
    assert rec.is_complete is True
    assert rec.mfe_300s_pct == pytest.approx(0.0)
    assert rec.mae_300s_pct == pytest.approx(-8.0)
    assert rec.hit_tp_5pct is False


def test_adaptive_day_median_threshold():
    gk = MLGatekeeper()
    now = 1789540000.0

    # 1. Cold start returns safety floor 0.15
    assert gk.get_day_median_score() == 0.15

    # 2. Simulate 10 completed bars across the session with low afternoon scores
    scores = [0.10, 0.12, 0.13, 0.14, 0.16, 0.18, 0.19, 0.21, 0.22, 0.25]
    gk.day_scores.extend(scores)
    # Median of [0.10, 0.12, 0.13, 0.14, 0.16, 0.18, 0.19, 0.21, 0.22, 0.25] is (0.16 + 0.18) / 2 = 0.17
    assert gk.get_day_median_score() == pytest.approx(0.17, abs=0.001)

    # 3. Simulate rolling 5m scores with peak = 0.20 (which is above day median 0.17, but below static 0.40)
    gk.recent_scores.append({"ts": now - 120.0, "score": 0.20, "features": {}})
    gk.recent_scores.append({"ts": now - 60.0, "score": 0.16, "features": {}})
    gk.recent_scores.append({"ts": now, "score": 0.18, "features": {}})

    # Adaptive check (threshold=None, use_adaptive=True) -> PASSES!
    passed, max_score, details = gk.check_rolling_confluence(
        threshold=None, window_sec=300, current_ts=now, use_adaptive=True
    )
    assert passed is True
    assert max_score == pytest.approx(0.20, abs=0.001)
    assert details["threshold"] == pytest.approx(0.17, abs=0.001)
    assert details["day_median"] == pytest.approx(0.17, abs=0.001)
    assert details["is_adaptive"] is True
    assert "Day Median" in details["reason"]

    # 4. Now simulate sub-median peak (0.14) -> VETOED by adaptive median
    gk.recent_scores.clear()
    gk.recent_scores.append({"ts": now - 60.0, "score": 0.14, "features": {}})
    passed_sub, max_sub, details_sub = gk.check_rolling_confluence(
        threshold=None, window_sec=300, current_ts=now, use_adaptive=True
    )
    assert passed_sub is False
    assert max_sub == pytest.approx(0.14, abs=0.001)
    assert "CONFLUENCE_VETO" in details_sub["reason"]

