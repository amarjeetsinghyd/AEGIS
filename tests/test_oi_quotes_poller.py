"""Tests for OIQuotesPoller — background ATM OI enricher (telemetry only)."""
from AEGIS.core.oi_builtup_gate import OIBuiltupGate
from AEGIS.core.oi_quotes_poller import OIQuotesPoller


def _universe(rows, spot=23670.0, step=50.0):
    return {"spot": spot, "strike_step": step, "rows": rows}


def test_poller_picks_atm_band_only():
    gate = OIBuiltupGate(strike_step=50.0)
    rows = [
        {"token": "1", "sym": "NIFTY23300PE", "strike": 23300.0, "kind": 2},  # -350 -> out
        {"token": "2", "sym": "NIFTY23650PE", "strike": 23650.0, "kind": 2},  # ATM -> in
        {"token": "3", "sym": "NIFTY23700CE", "strike": 23700.0, "kind": 1},  # +50 -> in
        {"token": "4", "sym": "NIFTY24000CE", "strike": 24000.0, "kind": 1},  # +350 -> out
    ]
    poller = OIQuotesPoller(oi_gate=gate, broker_api=None, strikes_each_side=5,
                            universe_provider=lambda: _universe(rows))
    picked = {r["token"] for r in poller._pick_targets()}
    assert picked == {"2", "3"}


def test_poller_enriches_gate_without_touching_thresholds():
    gate = OIBuiltupGate(strike_step=50.0, require_builtup_confirmation=False)

    class FakeAPI:
        def get_quotes(self, exchange, token):
            assert exchange in ("NFO", "BFO")
            return {"stat": "Ok", "oi": 500000.0, "lp": 25.0}

    rows = [{"token": "9", "sym": "NIFTY23650PE", "strike": 23650.0, "kind": 2}]
    poller = OIQuotesPoller(oi_gate=gate, broker_api=FakeAPI(), strikes_each_side=5,
                            universe_provider=lambda: _universe(rows))
    poller._running = True
    done = poller.poll_once()
    assert done == 1
    assert poller.enriched == 1
    assert gate._strikes["NIFTY23650PE"]["latest_oi"] == 500000.0


def test_poller_fail_soft_without_broker():
    gate = OIBuiltupGate(strike_step=50.0)
    poller = OIQuotesPoller(oi_gate=gate, broker_api=None,
                            universe_provider=lambda: _universe([]))
    poller._running = True
    assert poller.poll_once() == 0
