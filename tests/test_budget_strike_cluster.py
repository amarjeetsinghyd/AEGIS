from types import SimpleNamespace
from unittest.mock import Mock, MagicMock

from execution.execution_manager import AegisExecutionManager
from core.pipeline_orchestrator import AegisPipelineOrchestrator
from execution.strike_selector import StrikeSelector


def test_execution_manager_stages_independent_directional_budget_targets(monkeypatch):
    engine = SimpleNamespace(
        last_spot=24000.0,
        token_map={
            "call-token": {"side": 0, "strike": 24050.0, "expiry": "2026-11-03"},
            "put-token": {"side": 1, "strike": 23950.0, "expiry": "2026-11-03"},
        },
        t2s={
            "call-token": "NIFTY2610324050CE",
            "put-token": "NIFTY2610323950PE",
        },
        option_cache={"call-token": 14.0, "put-token": 12.0},
    )
    engine.set_budget_strike_targets = lambda targets: setattr(engine, "budget_strike_targets", targets)

    manager = AegisExecutionManager.__new__(AegisExecutionManager)
    manager._last_budget_target_refresh = 0.0
    manager.aegis_engine = engine
    manager.capital_engine = SimpleNamespace(get_available_funds=lambda: 1000.0)
    manager.get_instrument_params = lambda: {
        "index_name": "NIFTY",
        "lot_size": 65,
        "premium_min": 4.0,
        "premium_max": 50.0,
    }

    calls = []

    def select_strikes(**kwargs):
        calls.append(kwargs)
        if kwargs["signal_type"] == "CALL":
            return [({"token": "call-token", "symbol": "NIFTY2610324050CE", "strike": 24050.0}, 1, 14.0)]
        return [({"token": "put-token", "symbol": "NIFTY2610323950PE", "strike": 23950.0}, 1, 12.0)]

    monkeypatch.setattr(StrikeSelector, "select_strikes", Mock(side_effect=select_strikes))
    manager._refresh_budget_strike_targets()

    assert [call["signal_type"] for call in calls] == ["CALL", "PUT"]
    assert all(call["available_funds"] == 1000.0 for call in calls)
    assert calls[0]["weekly_opts"]["side"].tolist() == [0]
    assert calls[1]["weekly_opts"]["side"].tolist() == [1]
    assert engine.budget_strike_targets["UP"]["token"] == "call-token"
    assert engine.budget_strike_targets["DOWN"]["token"] == "put-token"


def test_execution_manager_budget_anchor_refresh_is_limited_to_ten_seconds(monkeypatch):
    engine = SimpleNamespace(set_budget_strike_targets=Mock())
    manager = AegisExecutionManager.__new__(AegisExecutionManager)
    manager._last_budget_target_refresh = 100.0
    manager.aegis_engine = engine

    monkeypatch.setattr("execution.execution_manager.time.monotonic", lambda: 105.0)
    manager._refresh_budget_strike_targets()

    engine.set_budget_strike_targets.assert_not_called()


def test_budget_cluster_priority_fallback_waterfall():
    """
    Test that _choose_budget_cluster_candidate enforces strict waterfall priority:
    Priority 1 (target) -> Priority 2 (minus_one / farther OTM) -> Priority 3 (plus_one / nearer ATM).
    """
    cluster = [
        {"token": "tok_target", "priority": "target", "strike": 24100.0},
        {"token": "tok_otm", "priority": "minus_one", "strike": 24150.0},
        {"token": "tok_atm", "priority": "plus_one", "strike": 24050.0},
    ]

    # Case 1: All eligible -> Target selected
    eligible = [
        {"candidate": cluster[1], "conviction": "otm"},
        {"candidate": cluster[0], "conviction": "target"},
        {"candidate": cluster[2], "conviction": "atm"},
    ]
    chosen = AegisPipelineOrchestrator._choose_budget_cluster_candidate(cluster, eligible)
    assert chosen["candidate"]["priority"] == "target"

    # Case 2: Target fails conviction/volume -> Fallback to minus_one (Priority 2)
    eligible_no_target = [
        {"candidate": cluster[1], "conviction": "otm"},
        {"candidate": cluster[2], "conviction": "atm"},
    ]
    chosen = AegisPipelineOrchestrator._choose_budget_cluster_candidate(cluster, eligible_no_target)
    assert chosen["candidate"]["priority"] == "minus_one"

    # Case 3: Both Target and minus_one fail -> Fallback to plus_one (Priority 3)
    eligible_only_atm = [
        {"candidate": cluster[2], "conviction": "atm"},
    ]
    chosen = AegisPipelineOrchestrator._choose_budget_cluster_candidate(cluster, eligible_only_atm)
    assert chosen["candidate"]["priority"] == "plus_one"

    # Case 4: None eligible -> Return None
    chosen = AegisPipelineOrchestrator._choose_budget_cluster_candidate(cluster, [])
    assert chosen is None


def test_budget_cluster_pure_individual_candidate_evaluation_prevents_leakage():
    """
    🛡️ Phase 2 Architectural Purity Test:
    Verify that candidate strikes in a cluster are evaluated strictly on their OWN individual
    touch_i1, eliminating historical max(cand_i1, ensemble_median_i1) leakage.
    A selling target strike with touch_i1 = -0.15 MUST receive touch_i1 = -0.15 (NOT +0.25)
    and fail conviction, allowing clean Priority 2 fallback to minus_one.
    """
    import queue
    import numpy as np
    from core.pipeline_orchestrator import create_aegis_engine

    class MockParsedTick:
        def __init__(self, tok, lp, vol, ts):
            self.tok = str(tok)
            self.lp = float(lp)
            self.vol = float(vol)
            self.ts = int(ts)
            self.bp = np.array([lp - 0.5, 0, 0, 0, 0], dtype=np.float64)
            self.bq = np.array([100, 0, 0, 0, 0], dtype=np.float64)
            self.sp = np.array([lp + 0.5, 0, 0, 0, 0], dtype=np.float64)
            self.sq = np.array([100, 0, 0, 0, 0], dtype=np.float64)

    sig_q = queue.Queue()
    token_to_symbol = {
        "26000": "NIFTY",
        "c_target": "NIFTY24100CE",
        "c_otm": "NIFTY24150CE",
        "c_atm": "NIFTY24050CE",
    }
    token_map = {
        "26000": {"side": 0, "strike": 0.0, "is_index": True},
        "c_target": {"side": 0, "strike": 24100.0},
        "c_otm": {"side": 0, "strike": 24150.0},
        "c_atm": {"side": 0, "strike": 24050.0},
    }
    engine = create_aegis_engine(
        token_to_symbol=token_to_symbol,
        token_map=token_map,
        strike_step=50.0,
        signal_queue=sig_q,
    )
    now = 1787023500.0
    now_ms = int(now * 1000)
    engine.last_spot = 24000.0
    engine._last_spot_ts = now
    engine.current_atm_strike = 24000.0
    engine.set_budget_strike_targets({
        "UP": {
            "token": "c_target",
            "symbol": "NIFTY24100CE",
            "strike": 24100.0,
            "available_funds": 10000.0,
            "lot_size": 65,
            "effective_premium_min": 5.0,
            "premium_max": 200.0,
        }
    })

    # Mock PA and Wyckoff engines to pass
    engine.price_action_engine.peek_setup = Mock(return_value=(True, "P1_WICK_REJECTION", {"pillar": 1, "mode": "MODE_B"}))
    engine.price_action_engine.band_veto_blocks = Mock(return_value=(False, 0.0, 0.0, 0.0))
    engine.wyckoff_volume_engine.evaluate = Mock(return_value=(True, "passed", {"wyckoff_confirmed": True}))

    # Feed ticks via update_tick to initialize buffers properly
    for sym, px in [("NIFTY24100CE", 50.0), ("NIFTY24150CE", 30.0), ("NIFTY24050CE", 70.0)]:
        engine.scorer.update_tick(sym, now - 1.0, px, px - 0.1, px + 0.1, 100, 100, 0, 0, 0, 0, 0, 0, 0, 0, vol=500)
        engine.scorer.update_tick(sym, now - 0.1, px, px - 0.1, px + 0.1, 100, 100, 0, 0, 0, 0, 0, 0, 0, 0, vol=1000)

    # Micro metrics: Target has -0.15 (selling pressure), OTM has +0.25 (buying pressure)
    def mock_extract(tick_buffer, anchor_T=None, lookback_sec=30.0):
        first_lp = tick_buffer[0][1]
        if first_lp == 50.0:
            return {"is_valid": True, "touch_i1": -0.15, "spread_pct": 0.4, "tot_bid_depth": 400, "tot_ask_depth": 400, "bp1": 49.9, "sp1": 50.1}
        elif first_lp == 30.0:
            return {"is_valid": True, "touch_i1": 0.25, "spread_pct": 0.4, "tot_bid_depth": 400, "tot_ask_depth": 400, "bp1": 29.9, "sp1": 30.1}
        else:
            return {"is_valid": True, "touch_i1": 0.10, "spread_pct": 0.4, "tot_bid_depth": 400, "tot_ask_depth": 400, "bp1": 69.9, "sp1": 70.1}

    engine.conviction_engine.extract_micro_metrics = Mock(side_effect=mock_extract)

    conviction_calls = []
    def mock_compute(**kwargs):
        conviction_calls.append(kwargs)
        t_i1 = kwargs.get("touch_i1", 0.0)
        score = 85.0 if t_i1 > 0 else 60.0
        return SimpleNamespace(
            total_score=score,
            is_approved=(score >= 80.0),
            recommended_tp_pct=0.040,
            sizing_tier="STANDARD_SIZE",
            breakdown={},
            sub_scores={},
            summary=lambda: "conviction",
        )

    engine.conviction_engine.compute_conviction_score = Mock(side_effect=mock_compute)

    tick = MockParsedTick(tok="c_target", lp=50.0, vol=1000, ts=now_ms)
    engine.on_tick_parsed(tick)

    # Verify conviction calls: Nearer-ATM (plus_one) is now prioritized before Farther-OTM (minus_one)
    assert len(conviction_calls) == 3
    assert conviction_calls[0]["touch_i1"] == -0.15
    assert conviction_calls[1]["touch_i1"] == 0.10  # plus_one (Nearer ATM) evaluated first!
    assert conviction_calls[2]["touch_i1"] == 0.25  # minus_one (Farther OTM) evaluated last

    # Verify signal emitted: Target failed conviction, so Nearer ATM (plus_one / NIFTY24050CE) was selected!
    assert not sig_q.empty()
    signal = sig_q.get()
    assert signal["sym"] == "NIFTY24050CE"
    assert signal["metrics"]["cluster_priority"] == "plus_one"
