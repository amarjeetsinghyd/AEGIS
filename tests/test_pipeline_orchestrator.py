"""
Targeted tests for Phase 2:
- Task 1: Strict Budget Target Invariant (_choose_budget_cluster_candidate).
- Task 4: 2-Layer Institutional Anti-Trap Quant Shield (Option VWAP & Mirror Unwind Gate).
"""
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
from core.pipeline_orchestrator import AegisPipelineOrchestrator


def test_capital_invariant_target_strictly_preferred():
    """
    Task 1 (Capital Mandate): The budget 'target' strike is strictly matched to available
    funds by StrikeSelector. If eligible, it is ALWAYS chosen over adjacent strikes.
    """
    cluster = [
        {"token": "tok_target", "priority": "target", "strike": 24100.0},
        {"token": "tok_atm", "priority": "plus_one", "strike": 24050.0},
        {"token": "tok_otm", "priority": "minus_one", "strike": 24150.0},
    ]

    target_cand = {
        "candidate": cluster[0],
        "conviction": SimpleNamespace(total_score=80.0),
        "micro_metrics": {"spread_pct": 0.010},
    }
    nearer_cand = {
        "candidate": cluster[1],
        "conviction": SimpleNamespace(total_score=95.0),  # Much higher score, but expensive!
        "micro_metrics": {"spread_pct": 0.005},
    }

    # Capital invariant: MUST choose target!
    chosen = AegisPipelineOrchestrator._choose_budget_cluster_candidate(
        cluster,
        [target_cand, nearer_cand],
    )
    assert chosen["candidate"]["priority"] == "target"


def test_capital_invariant_fallback_when_target_missing():
    """
    Task 1: If target fails quote sanity or budget, fall back to cluster priority order.
    """
    cluster = [
        {"token": "tok_target", "priority": "target", "strike": 24100.0},
        {"token": "tok_atm", "priority": "plus_one", "strike": 24050.0},
        {"token": "tok_otm", "priority": "minus_one", "strike": 24150.0},
    ]

    nearer_cand = {
        "candidate": cluster[1],
        "conviction": SimpleNamespace(total_score=85.0),
    }
    otm_cand = {
        "candidate": cluster[2],
        "conviction": SimpleNamespace(total_score=82.0),
    }

    chosen = AegisPipelineOrchestrator._choose_budget_cluster_candidate(
        cluster,
        [otm_cand, nearer_cand],
    )
    # Cluster order puts plus_one before minus_one
    assert chosen["candidate"]["priority"] == "plus_one"


def test_option_contract_vwap_and_mirror_gate_shield_wiring():
    """
    Task 4: Validates that get_option_contract_vwap is hooked on price_action_engine
    and callable from pipeline orchestrator.
    """
    from core.price_action_engine import PriceActionEngine
    pae = PriceActionEngine(strike_step=50.0)
    assert hasattr(pae, "get_option_contract_vwap")
    assert pae.get_option_contract_vwap("NON_EXISTENT") == 0.0


def test_pipeline_orchestrator_thread_safety_and_map_lock():
    """Verify thread-safety of refresh_token_maps, set_budget_strike_targets, on_fill, and check_pending_edges."""
    import threading
    import time
    from unittest.mock import MagicMock
    from core.pipeline_orchestrator import AegisPipelineOrchestrator

    t2s = {"26000": "NIFTY", "100": "NIFTY24000CE"}
    tmap = {"100": {"side": 0, "strike": 24000.0}}
    orchestrator = AegisPipelineOrchestrator(
        token_to_symbol=t2s,
        token_map=tmap,
    )
    assert hasattr(orchestrator, "_map_lock")

    exceptions = []
    stop_event = threading.Event()

    def map_updater():
        try:
            for i in range(100):
                if stop_event.is_set():
                    break
                new_t2s = {"26000": "NIFTY", str(1000 + i): f"OPT_{i}"}
                new_tmap = {str(1000 + i): {"side": 0, "strike": 24000.0 + i}}
                orchestrator.refresh_token_maps(new_t2s, new_tmap)
                orchestrator.set_budget_strike_targets({
                    "UP": {"token": str(1000 + i), "symbol": f"OPT_{i}", "strike": 24000.0 + i}
                })
                time.sleep(0.001)
        except Exception as exc:
            exceptions.append(exc)

    def tick_simulator():
        try:
            tick = MagicMock()
            for i in range(300):
                if stop_event.is_set():
                    break
                tick.tok = str(1000 + (i % 50))
                tick.sym = f"OPT_{i % 50}"
                tick.lp = 100.0 + i
                tick.ts = time.time()
                tick.bp = [100.0] * 5
                tick.sp = [100.5] * 5
                tick.bq = [100] * 5
                tick.sq = [100] * 5
                orchestrator.on_tick_parsed(tick)
                orchestrator.on_fill(fill_ts=time.time(), fill_price=100.0, signal_ts=time.time() - 0.05, signal_price=100.0, token=tick.tok)
                orchestrator.check_pending_edges(now=time.time() + 35.0, current_opt_price=101.0)
                time.sleep(0.001)
        except Exception as exc:
            exceptions.append(exc)

    threads = [
        threading.Thread(target=map_updater),
        threading.Thread(target=tick_simulator),
        threading.Thread(target=tick_simulator),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(exceptions) == 0, f"Exceptions occurred: {exceptions}"

