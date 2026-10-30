from types import SimpleNamespace
from unittest.mock import Mock

from AEGIS.execution.aegis_execution_manager import AegisExecutionManager
from common.execution.strike_selector import StrikeSelector


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

    monkeypatch.setattr("AEGIS.execution.aegis_execution_manager.time.monotonic", lambda: 105.0)
    manager._refresh_budget_strike_targets()

    engine.set_budget_strike_targets.assert_not_called()
