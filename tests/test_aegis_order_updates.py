import threading
from types import SimpleNamespace

from execution import execution_manager as execution_module
from execution.execution_manager import AegisExecutionManager
from broker.shoonya_adapter import ShoonyaMarketDataProvider


class _HistoryApi:
    def __init__(self):
        self.calls = []

    def single_order_history(self, orderno):
        self.calls.append(orderno)
        return []


def _manager(api):
    manager = AegisExecutionManager.__new__(AegisExecutionManager)
    manager.broker = SimpleNamespace(_api=api)
    manager._order_update_lock = threading.Lock()
    manager._order_updates = {}
    manager._order_history_poll_ts = {}
    manager._order_history_poll_interval_sec = 2.5
    return manager


def test_order_update_fill_is_used_before_rest_history():
    api = _HistoryApi()
    manager = _manager(api)

    manager.on_order_update({"norenordno": "tp-1", "status": "COMPLETE", "avgprc": "104.25"})

    assert manager._order_fill("tp-1", allow_rest=True) == (True, 104.25)
    assert api.calls == []


def test_rest_order_history_fallback_is_throttled_per_order(monkeypatch):
    api = _HistoryApi()
    manager = _manager(api)
    monotonic = [100.0]
    monkeypatch.setattr(execution_module.time, "monotonic", lambda: monotonic[0])

    assert manager._order_fill("tp-1", allow_rest=True) == (False, 0.0)
    monotonic[0] = 102.4
    assert manager._order_fill("tp-1", allow_rest=True) == (False, 0.0)
    assert api.calls == ["tp-1"]

    monotonic[0] = 102.5
    assert manager._order_fill("tp-1", allow_rest=True) == (False, 0.0)
    assert api.calls == ["tp-1", "tp-1"]


def test_shoonya_order_listener_dispatches_websocket_updates():
    provider = ShoonyaMarketDataProvider.__new__(ShoonyaMarketDataProvider)
    provider._order_listeners = []
    received = []
    callback = received.append

    provider.add_order_listener(callback)
    provider._dispatch_order_update({"norenordno": "order-1", "status": "FILLED"})

    assert received == [{"norenordno": "order-1", "status": "FILLED"}]
