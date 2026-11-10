import os
import sys
import queue
import numpy as np
import pandas as pd
import pytest

from core.pipeline_orchestrator import AegisPipelineOrchestrator, create_aegis_engine
from execution.strike_selector import StrikeSelector


class MockParsedTick:
    def __init__(self, tok, lp, vol, ts=1787023500000, bp=None, bq=None, sp=None, sq=None):
        self.tok = str(tok)
        self.lp = float(lp)
        self.vol = float(vol)
        self.ts = int(ts)
        self.bp = np.array(bp if bp is not None else [lp - 0.5, 0, 0, 0, 0], dtype=np.float64)
        self.bq = np.array(bq if bq is not None else [100, 0, 0, 0, 0], dtype=np.float64)
        self.sp = np.array(sp if sp is not None else [lp + 0.5, 0, 0, 0, 0], dtype=np.float64)
        self.sq = np.array(sq if sq is not None else [100, 0, 0, 0, 0], dtype=np.float64)


def test_aegis_initialization_and_params():
    token_to_symbol = {
        "26000": "NIFTY",
        "1001": "NIFTY2682024000CE",
        "1002": "NIFTY2682024000PE",
        "1003": "NIFTY2682024100CE",
        "1004": "NIFTY2682023900PE"
    }
    token_map = {
        "26000": {"side": 0, "strike": 0.0, "is_index": True},
        "1001": {"side": 0, "strike": 24000.0},
        "1002": {"side": 1, "strike": 24000.0},
        "1003": {"side": 0, "strike": 24100.0},
        "1004": {"side": 1, "strike": 23900.0}
    }
    
    engine = create_aegis_engine(
        token_to_symbol=token_to_symbol,
        token_map=token_map,
        strike_step=50.0,
        signal_queue=queue.Queue()
    )
    
    assert engine is not None
    assert engine.strike_step == 50.0
    assert engine.last_spot == 0.0
    assert engine.current_atm_strike == 0.0
    assert engine.high_conviction_threshold >= 60.0
    assert engine.scorer is not None


def test_budget_cluster_uses_otm_rank_and_waterfall_priority_for_both_sides():
    token_to_symbol = {"26000": "NIFTY"}
    token_map = {"26000": {"side": 0, "strike": 0.0, "is_index": True}}
    for token, side, strike in (
        ("c_itm", 0, 23950.0),
        ("c_atm", 0, 24000.0),
        ("c_otm1", 0, 24050.0),
        ("c_target", 0, 24100.0),
        ("c_otm3", 0, 24150.0),
        ("p_itm", 1, 24050.0),
        ("p_atm", 1, 24000.0),
        ("p_otm1", 1, 23950.0),
        ("p_target", 1, 23900.0),
        ("p_otm3", 1, 23850.0),
    ):
        symbol = f"NIFTY{int(strike)}{'CE' if side == 0 else 'PE'}"
        token_to_symbol[token] = symbol
        token_map[token] = {"side": side, "strike": strike}

    engine = create_aegis_engine(
        token_to_symbol=token_to_symbol,
        token_map=token_map,
        strike_step=50.0,
    )
    engine.current_atm_strike = 24000.0
    engine.set_budget_strike_targets({
        "UP": {"token": "c_target", "symbol": token_to_symbol["c_target"], "strike": 24100.0},
        "DOWN": {"token": "p_target", "symbol": token_to_symbol["p_target"], "strike": 23900.0},
    })

    call_cluster = engine._get_budget_strike_cluster("UP")
    put_cluster = engine._get_budget_strike_cluster("DOWN")
    assert [(item["priority"], item["strike"]) for item in call_cluster] == [
        ("target", 24100.0),
        ("minus_one", 24150.0),
        ("plus_one", 24050.0),
    ]
    assert [(item["priority"], item["strike"]) for item in put_cluster] == [
        ("target", 23900.0),
        ("minus_one", 23850.0),
        ("plus_one", 23950.0),
    ]

    eligible = [
        {"candidate": item, "conviction": item["priority"]}
        for item in (call_cluster[1], call_cluster[2])
    ]
    assert engine._choose_budget_cluster_candidate(call_cluster, eligible) == eligible[0]
    target_eligible = {"candidate": call_cluster[0], "conviction": "target"}
    assert engine._choose_budget_cluster_candidate(
        call_cluster,
        [eligible[0], target_eligible, eligible[1]],
    ) == target_eligible
    assert engine._get_budget_strike_cluster("UNKNOWN") == []

    engine.set_budget_strike_targets({
        "UP": {"token": "c_atm", "symbol": token_to_symbol["c_atm"], "strike": 24000.0},
        "DOWN": {"token": "p_atm", "symbol": token_to_symbol["p_atm"], "strike": 24000.0},
    })
    assert [(item["priority"], item["strike"]) for item in engine._get_budget_strike_cluster("UP")] == [
        ("target", 24000.0),
        ("minus_one", 24050.0),
    ]
    assert [(item["priority"], item["strike"]) for item in engine._get_budget_strike_cluster("DOWN")] == [
        ("target", 24000.0),
        ("minus_one", 23950.0),
    ]


def test_aegis_spot_tick_and_dynamic_atm_roll():
    token_to_symbol = {
        "26000": "NIFTY",
        "1001": "NIFTY2682024000CE",
        "1002": "NIFTY2682024100CE"
    }
    token_map = {
        "26000": {"side": 0, "strike": 0.0, "is_index": True},
        "1001": {"side": 0, "strike": 24000.0},
        "1002": {"side": 0, "strike": 24100.0}
    }
    
    engine = create_aegis_engine(
        token_to_symbol=token_to_symbol,
        token_map=token_map,
        strike_step=50.0,
        signal_queue=queue.Queue()
    )
    
    # 1. Feed initial Spot tick at 24000
    spot_tick_1 = MockParsedTick("26000", lp=24000.0, vol=1000)
    engine.on_tick_parsed(spot_tick_1)
    
    assert engine.last_spot == 24000.0
    assert engine.current_atm_strike == 24000.0
    
    # 2. Feed Option tick at 24000 CE
    opt_tick_1 = MockParsedTick("1001", lp=150.0, vol=500, bp=[149.5, 149.0, 148.5, 148.0, 147.5], bq=[200, 200, 200, 200, 200], sp=[150.5, 151.0, 151.5, 152.0, 152.5], sq=[200, 200, 200, 200, 200])
    engine.on_tick_parsed(opt_tick_1)
    
    # Verify scorer has tick recorded
    assert "NIFTY2682024000CE" in engine.scorer.history
    
    # 3. Simulate Spot moving UP to 24100 (+2 strikes for 50-step)
    spot_tick_2 = MockParsedTick("26000", lp=24100.0, vol=2000)
    engine.on_tick_parsed(spot_tick_2)
    
    assert engine.last_spot == 24100.0
    assert engine.current_atm_strike == 24100.0


def test_stale_spot_blocks_option_signal_evaluation():
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
        signal_queue=queue.Queue(),
    )
    tick_ts = 1787023500.0
    engine.last_spot = 24000.0
    engine._last_spot_ts = tick_ts - 2.01

    engine.on_tick_parsed(
        MockParsedTick(
            "1001",
            lp=150.0,
            vol=500,
            ts=int(tick_ts * 1000),
        )
    )

    assert "NIFTY2682024000CE" not in engine.scorer.history


def test_aegis_microstructure_signal_emission():
    # PA-only mode: pure microstructure burst WITHOUT a pillar setup must NOT emit.
    # Hidden 4th route removed — pillars are the sole trigger source.
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
    
    t0_ms = 1787023500000
    # Feed rapid institutional burst ticks (60 ticks in 20s)
    for i in range(60):
        tick = MockParsedTick(
            tok="1001",
            lp=50.0 + i * 0.2,
            vol=500 + i * 10,
            ts=t0_ms + int(i * 300),
            bp=[50.0 + i * 0.2 - 0.05, 49.5, 49.0, 48.5, 48.0],
            bq=[200 + i * 10, 200, 200, 200, 200],
            sp=[50.0 + i * 0.2 + 0.05, 51.0, 51.5, 52.0, 52.5],
            sq=[20, 20, 20, 20, 20]
        )
        engine.on_tick_parsed(tick)

    assert sig_q.empty()  # PA-only: no pillar setup, no signal.


def test_emit_signal_reports_queue_delivery():
    sig_q = queue.Queue(maxsize=1)
    engine = create_aegis_engine(
        token_to_symbol={"26000": "NIFTY"},
        token_map={"26000": {"side": 0, "strike": 0.0, "is_index": True}},
        strike_step=50.0,
        signal_queue=sig_q,
    )
    args = {
        "sym": "NIFTY24000CE",
        "lp": 100.0,
        "direction": "UP",
        "conviction": 85.0,
        "gates": {},
        "metrics": {},
    }

    assert engine._emit_signal(**args) is True
    assert engine._emit_signal(**args) is False


def test_strike_selector_otm_sweet_spot():
    atm = 24000.0
    otm_calls = StrikeSelector.get_otm_strikes(atm, "CALL", "NIFTY", max_depth=4)
    assert otm_calls == [24050.0, 24100.0, 24150.0, 24000.0]
    
    otm_puts = StrikeSelector.get_otm_strikes(atm, "PUT", "NIFTY", max_depth=4)
    assert otm_puts == [23950.0, 23900.0, 23850.0, 24000.0]


def test_strike_selector_budget_adaptation_2000():
    # Simulate Sensex with lot size 10 and user budget ₹2000
    # Strikes:
    # 77600 CE (ATM) @ 250 -> Cost 2500 (Out of budget)
    # 77700 CE (OTM1) @ 220 -> Cost 2200 (Out of budget)
    # 77800 CE (OTM2) @ 190 -> Cost 1900 (Fits budget ₹2000!)
    # 77900 CE (OTM3) @ 140 -> Cost 1400 (Fits budget)
    weekly_opts = pd.DataFrame([
        {"symbol": "SENSEX2682077600CE", "token": "101", "strike": 77600.0},
        {"symbol": "SENSEX2682077700CE", "token": "102", "strike": 77700.0},
        {"symbol": "SENSEX2682077800CE", "token": "103", "strike": 77800.0},
        {"symbol": "SENSEX2682077900CE", "token": "104", "strike": 77900.0},
    ])
    option_cache = {
        "101": 250.0,
        "102": 220.0,
        "103": 190.0,
        "104": 140.0,
    }
    
    alloc = StrikeSelector.select_strikes(
        signal_type="CALL",
        index_price=77620.0,
        index_name="SENSEX",
        weekly_opts=weekly_opts,
        option_cache=option_cache,
        available_funds=2000.0,
        lot_size=10,
    )
    
    assert len(alloc) == 1
    chosen_row, qty, ltp = alloc[0]
    # 10% charge-reserve policy: effective budget = 2000 * 0.90 = Rs 1800, so
    # 77800 CE (Rs 190, cost Rs 1900) no longer spends every rupee; the highest
    # affordable premium under the reserve is 77900 CE @ Rs 140 (cost Rs 1400)!
    assert chosen_row["symbol"] == "SENSEX2682077900CE"
    assert ltp == 140.0
    assert qty == 1


def _stub_with_history(rows):
    """Minimal stub exposing history_30s for the delta inflection helper."""
    import types
    from collections import deque
    return types.SimpleNamespace(history_30s={"SYM": deque(rows, maxlen=300)})


def test_delta_inflection_veto_on_flip_against_up():
    """Buyers (+I1) flipping to sellers (-I1) vetoes an UP entry."""
    rows = [(float(i), 10.0, 10.0, 10.1, 800.0, 200.0) for i in range(6)]
    rows += [(float(i + 6), 10.0, 10.0, 10.1, 200.0, 800.0) for i in range(6)]
    vetoed, detail = AegisPipelineOrchestrator._delta_inflection_check(_stub_with_history(rows), "SYM", "UP")
    assert vetoed
    assert "flip" in detail


def test_delta_inflection_no_veto_when_aligned():
    """Consistent buyer aggression never vetoes an UP entry."""
    rows = [(float(i), 10.0, 10.0, 10.1, 800.0, 200.0) for i in range(12)]
    vetoed, _ = AegisPipelineOrchestrator._delta_inflection_check(_stub_with_history(rows), "SYM", "UP")
    assert not vetoed


def test_delta_inflection_fail_open_on_sparse_history():
    """Fewer than 6 ticks => fail-open, never a veto."""
    rows = [(float(i), 10.0, 10.0, 10.1, 800.0, 200.0) for i in range(3)]
    vetoed, detail = AegisPipelineOrchestrator._delta_inflection_check(_stub_with_history(rows), "SYM", "DOWN")
    assert not vetoed
    assert detail == "insufficient_history"


def test_delta_inflection_propagates_direction_wide():
    """2026-10-26: a flip detected on one symbol's L1 book opens a direction-wide
    trap window — a second symbol with ALIGNED history must also be vetoed while
    the window is open (thin-book hot-strike escape fixed)."""
    flip_rows = [(float(i), 10.0, 10.0, 10.1, 800.0, 200.0) for i in range(6)]
    flip_rows += [(float(i + 6), 10.0, 10.0, 10.1, 200.0, 800.0) for i in range(6)]
    stub_a = _stub_with_history(flip_rows)
    vetoed_a, detail_a = AegisPipelineOrchestrator._delta_inflection_check(stub_a, "SYM", "UP")
    assert vetoed_a
    assert "flip" in detail_a

    # Second symbol: perfectly aligned buyer aggression — would pass alone,
    # but must be vetoed by the direction-wide trap window opened above.
    aligned_rows = [(float(i), 10.0, 10.0, 10.1, 800.0, 200.0) for i in range(12)]
    stub_b = _stub_with_history(aligned_rows)
    stub_b._spot_inflection_veto_until = stub_a._spot_inflection_veto_until
    import time as _time
    vetoed_b, detail_b = AegisPipelineOrchestrator._delta_inflection_check(stub_b, "SYM", "UP", _time.time())
    assert vetoed_b
    assert "trap window" in detail_b


def test_delta_inflection_trap_window_expires():
    """2026-10-26: the direction-wide window is time-boxed (45s default) — an
    aligned symbol evaluated after expiry must pass again."""
    import time as _time
    flip_rows = [(float(i), 10.0, 10.0, 10.1, 800.0, 200.0) for i in range(6)]
    flip_rows += [(float(i + 6), 10.0, 10.0, 10.1, 200.0, 800.0) for i in range(6)]
    stub_a = _stub_with_history(flip_rows)
    vetoed_a, _ = AegisPipelineOrchestrator._delta_inflection_check(stub_a, "SYM", "UP")
    assert vetoed_a

    now = _time.time()
    # Artificially expire the window
    stub_a._spot_inflection_veto_until["UP"] = now - 1.0
    aligned_rows = [(float(i), 10.0, 10.0, 10.1, 800.0, 200.0) for i in range(12)]
    stub_b = _stub_with_history(aligned_rows)
    stub_b._spot_inflection_veto_until = stub_a._spot_inflection_veto_until
    vetoed_b, detail_b = AegisPipelineOrchestrator._delta_inflection_check(stub_b, "SYM", "UP", now)
    assert not vetoed_b
