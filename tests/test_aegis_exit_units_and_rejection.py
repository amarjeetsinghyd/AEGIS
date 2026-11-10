"""
Unit tests for the AEGIS Exit-Quantity Double-Multiplication Shield and
Order-Rejection Fail-Safe (incident 2026-11: SENSEX 578800CE exit SELL 400).

Covers:
1. UNIT INVARIANT — exit / TP order quantity is EXACTLY the open share-units:
   BFO SENSEX 1 lot => 20, NFO NIFTY 1 lot => 65 (never 400 / 5200).
2. Legacy persisted share-valued contexts (a.k.a. the incident state) exit at
   true size (20 / 65), never re-multiplied.
3. Order-REJECTED fail-safe:
   a) CRITICAL alert quotes the broker's exact rejection reason.
   b) Position is NOT marked flat, tracking continues (leg/context stay OPEN).
   c) Emergency reconcile dispatches an exit with the BROKER-VERIFIED net qty.
   d) Anti-spam guard suppresses rapid-fire duplicate emergency orders.
   e) Broker-verified-flat shortcut closes bookkeeping without re-SELL.
"""

import os

import pytest

os.environ.setdefault("AEGIS_SILENCE_ALARM", "1")  # keep tests quiet (no terminal bell)

from execution.trade_context import TradeContext, TradeLeg, resolve_leg_net_qty
from execution.order_lifecycle import ShoonyaOrderLifecycle
from execution.execution_manager import AegisExecutionManager


class DummyDataProvider:
    def __init__(self, quote=None):
        self._quote = quote or {"ltp": 24.70, "bp1": 24.65, "sp1": 24.75, "sq1": 500, "bq1": 500}

    def get_quote(self, exch, token):
        return self._quote


class DummyDataFetcher:
    def __init__(self, active=("SENSEX", "SENSEX"), spot=61200.0):
        self._active = active
        self._spot = spot

    def get_active_instrument(self):
        return self._active

    def get_index_ltp(self):
        return self._spot


class DummyBroker:
    def __init__(self):
        self.market_data_provider = DummyDataProvider()
        self._api = None

    def get_positions(self):
        return self._api.get_positions() if self._api else []


class CapturingGateway:
    """Stub broker gateway for ShoonyaOrderLifecycle (order placement capture)."""

    def __init__(self):
        self.orders = []          # captured place_order kwargs
        self.resp_override = None

    def place_order(self, **kwargs):
        self.orders.append(kwargs)
        if self.resp_override is not None:
            return self.resp_override
        return {"stat": "Ok", "norenordno": f"G{1000 + len(self.orders)}"}


class StubApi:
    """Stub Shoonya REST api: place_order + single_order_history + get_positions."""

    def __init__(self, default_history=None):
        self.orders = []                       # captured place_order kwargs
        self._default_history = default_history or [{"status": "OPEN"}]
        self._history = {}                     # orderno -> list[dict]
        self._positions = []

    # broker seam
    def place_order(self, **kwargs):
        self.orders.append(kwargs)
        oid = f"A{2000 + len(self.orders)}"
        return {"stat": "Ok", "norenordno": oid}

    def get_positions(self):
        return self._positions

    def single_order_history(self, orderno):
        if orderno is not None and str(orderno) in self._history:
            return self._history[str(orderno)]
        return self._default_history

    def modify_order(self, **kwargs):
        return {"stat": "Ok"}

    def cancel_order(self, orderno=None, **kwargs):
        return {"stat": "Ok"}


def make_em(active_instrument, symbol, quantity=1, entry_price=24.70):
    """Minimal AegisExecutionManager assembled via __new__ (no background loop)."""
    import threading as _t

    broker = DummyBroker()
    data_fetcher = DummyDataFetcher(active=active_instrument)
    em = AegisExecutionManager.__new__(AegisExecutionManager)
    em.broker = broker
    em.data_fetcher = data_fetcher
    em.aegis_engine = None
    em.trade_lock = _t.Lock()
    em._order_update_lock = _t.Lock()
    em._order_updates = {}
    em._order_history_poll_ts = {}
    em._order_history_poll_interval_sec = 2.5
    em.trade_context = None
    em.tp_order_id = None
    em.last_tp_order_id = None
    em.strike_cooldowns = {}

    capital = type("CE", (), {"add_funds": staticmethod(lambda amt: None),
                              "deduct_funds": staticmethod(lambda amt: None)})()
    capital._needs_broker_sync = False
    em.capital_engine = capital

    em.order_lifecycle = ShoonyaOrderLifecycle(CapturingGateway())
    em._emerg_recon_state = {}
    em._has_unreconciled_position = False
    em._save_active_context = lambda: None
    em._append_history = lambda reason, pnl: None
    em._is_exiting = False
    em._is_entering = False
    em.trades_today = 0

    context = TradeContext("CALL", "AEGIS_GAMMA_BURST", 61000.0)
    context.entry_time = datetime_now()
    leg = TradeLeg(
        token="57880",
        symbol=symbol,
        strike=57800.0,
        option_type="CALL",
        quantity=quantity,
        entry_price=entry_price,
    )
    context.add_leg(leg)
    context.status = "OPEN"
    em.trade_context = context
    return em, context, leg


def datetime_now():
    from datetime import datetime as _dt
    return _dt.now()


def capture_qty(kwargs):
    for key in ("quantity", "qty", "newquantity"):
        if key in kwargs:
            return str(kwargs[key])
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Resolver unit contract
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,lot,expected", [
    (1, 20, 20),     # SENSEX 1 lot
    (1, 65, 65),     # NIFTY 1 lot
    (2, 65, 130),    # 2 lots (future-proof)
    (20, 20, 20),    # legacy incident state: quantity already share-valued
    (65, 65, 65),    # legacy NFO share-valued
    (0, 20, 20),     # degenerate -> default 1 lot
    (None, 20, 20),  # degenerate -> default 1 lot
])
def test_resolve_leg_net_qty_unit_contract(raw, lot, expected):
    assert resolve_leg_net_qty(raw, lot) == expected


# ─────────────────────────────────────────────────────────────────────────────
# Task 3a — BFO SENSEX exit quantity invariant
# ─────────────────────────────────────────────────────────────────────────────

def test_bfo_sensex_exit_order_quantity_exact_one_lot():
    """SENSEX (BFO) 1-lot position must exit with SELL quantity EXACTLY 20 (never 400)."""
    em, ctx, leg = make_em(("SENSEX", "SENSEX"), "SENSEX26N0578800CE")
    api = StubApi(default_history=[{"status": "COMPLETE", "avgprc": "24.55"}])
    em.broker._api = api
    em.must_force_flatten = lambda now=None: False

    em._do_chase_exit("Premium Hard SL (-8.0% disaster floor)")

    sell_orders = [k for k in api.orders if k.get("buy_or_sell") == "S"]
    assert sell_orders, "No exit order was dispatched!"
    assert all(capture_qty(k) == "20" for k in sell_orders), \
        f"Exit quantity must be 20 for SENSEX 1 lot, got: {[capture_qty(k) for k in sell_orders]}"
    assert leg.status == "CLOSED"
    assert leg.pnl == pytest.approx((24.55 - 24.70) * 20)


# ─────────────────────────────────────────────────────────────────────────────
# Task 3b — NFO NIFTY exit quantity invariant
# ─────────────────────────────────────────────────────────────────────────────

def test_nfo_nifty_exit_order_quantity_exact_one_lot():
    """NIFTY (NFO) 1-lot position must exit with SELL quantity EXACTLY 65 (never 5200)."""
    em, ctx, leg = make_em(("NIFTY", "NIFTY"), "NIFTY26NOV24500PE", entry_price=37.30)
    api = StubApi(default_history=[{"status": "COMPLETE", "avgprc": "37.05"}])
    em.broker._api = api
    em.must_force_flatten = lambda now=None: False

    em._do_chase_exit("Time Stop")

    sell_orders = [k for k in api.orders if k.get("buy_or_sell") == "S"]
    assert sell_orders
    assert all(capture_qty(k) == "65" for k in sell_orders)
    assert leg.status == "CLOSED"
    assert leg.pnl == pytest.approx((37.05 - 37.30) * 65)


# ─────────────────────────────────────────────────────────────────────────────
# Task 1 — legacy share-valued persisted context (THE incident regression)
# ─────────────────────────────────────────────────────────────────────────────

def test_legacy_stale_fillshares_context_exits_at_true_size():
    """Pre-fix crash-recovered context with leg.quantity=20 (SENSEX shares) must
    exit with SELL 20 — NOT 20*20=400 (the live incident on 2026-11)."""
    em, ctx, leg = make_em(("SENSEX", "SENSEX"), "SENSEX26N0578800CE", quantity=20)
    api = StubApi(default_history=[{"status": "COMPLETE", "avgprc": "24.55"}])
    em.broker._api = api
    em.must_force_flatten = lambda now=None: False

    em._do_chase_exit("Force Flatten (>= 15:08)")

    sell_orders = [k for k in api.orders if k.get("buy_or_sell") == "S"]
    assert sell_orders
    assert all(capture_qty(k) == "20" for k in sell_orders), \
        "Double-multiplication regression: exit qty must be 20 (legacy share-valued context)"

    # NIFTY legacy variant: 65 shares exits 65, never 4225
    em2, ctx2, leg2 = make_em(("NIFTY", "NIFTY"), "NIFTY26NOV24500PE", quantity=65, entry_price=37.30)
    api2 = StubApi(default_history=[{"status": "COMPLETE", "avgprc": "37.05"}])
    em2.broker._api = api2
    em2.must_force_flatten = lambda now=None: False
    em2._do_chase_exit("Force Flatten (>= 15:08)")
    sell2 = [k for k in api2.orders if k.get("buy_or_sell") == "S"]
    assert all(capture_qty(k) == "65" for k in sell2)


def test_resting_tp_order_quantity_exact_units():
    """Resting Maker TP order must use exact share-units as well (SENSEX => 20)."""
    em, ctx, leg = make_em(("SENSEX", "SENSEX"), "SENSEX26N0578800CE")
    api = StubApi()
    em.broker._api = api
    em.broker.place_order = api.place_order  # TP placement prefers broker.place_order
    params = em.get_instrument_params()

    em._place_initial_take_profit(leg, params, target_pct=0.04)
    assert em.tp_order_id, "Resting TP order was not placed"
    assert all(capture_qty(k) == "20" for k in api.orders), \
        f"Resting TP qty must be 20 for SENSEX: {[capture_qty(k) for k in api.orders]}"


# ─────────────────────────────────────────────────────────────────────────────
# Task 2 — Order-REJECTED fail-safe & active reconciliation
# ─────────────────────────────────────────────────────────────────────────────

def _reject_exit_setup(em, leg, api):
    """Brokerscape: WS reports a REJECTED exit order, broker still holds 20 units."""
    em.broker._api = api
    em.last_tp_order_id = "7001"
    em.on_order_update({"norenordno": "7001", "status": "REJECTED", "emsg": "marginsmll not available for this order"})
    api._positions = [{"tsym": leg.symbol, "tok": str(leg.token), "netqty": "20"}]


def test_rejected_exit_keeps_tracking_and_reconciles_verified_net_qty(caplog):
    """REJECTED exit must: (a) CRITICAL log with broker reason, (b) keep leg/context
    OPEN (tracking preserved), (c) reconcile with BROKER-VERIFIED net qty 20."""
    import logging
    em, ctx, leg = make_em(("SENSEX", "SENSEX"), "SENSEX26N0578800CE")
    api = StubApi()  # every order stays OPEN forever (never fills)
    _reject_exit_setup(em, leg, api)
    em.order_lifecycle.broker.resp_override = {
        "stat": "Nok", "emsg": "marginsmll not available for this order"
    }
    em.must_force_flatten = lambda now=None: False

    with caplog.at_level(logging.CRITICAL):
        em._do_chase_exit("Emergency Margin Rejected Exit")

    # (a) Exact broker rejection reason surfaced at CRITICAL level
    assert "marginsmll" in caplog.text and "REJECT" in caplog.text.upper()
    # (c) The emergency reconcile dispatched with the broker-verified net qty
    recon_sells = [k for k in api.orders if k.get("buy_or_sell") == "S"]
    assert recon_sells, "Emergency reconcile did not dispatch any exit order!"
    assert all(capture_qty(k) == "20" for k in recon_sells), \
        f"Reconcile must use broker-verified net_qty 20, got {[capture_qty(k) for k in recon_sells]}"
    # (b) tracking NEVER stopped
    assert leg.status == "OPEN", "REJECTED exit must not mark the position flat!"
    assert em.trade_context is ctx
    assert ctx.status == "OPEN"
    assert em._has_unreconciled_position is True
    assert em.capital_engine._needs_broker_sync is True


def test_reconcile_anti_spam_guard_blocks_rapid_fire():
    """A second immediate reconcile attempt must be suppressed (no duplicate order spam)."""
    em, ctx, leg = make_em(("SENSEX", "SENSEX"), "SENSEX26N0578800CE")
    api = StubApi()
    _reject_exit_setup(em, leg, api)

    flat1, _ = em._reconcile_leg_position(leg, reason="TEST1")
    assert flat1 is False
    assert len(api.orders) == 1  # exactly one emergency dispatch

    flat2, _ = em._reconcile_leg_position(leg, reason="TEST2")
    assert flat2 is False
    assert len(api.orders) == 1, "Anti-spam guard failed: duplicate emergency order dispatched!"


def test_broker_verified_flat_closes_without_re_sell():
    """When the broker reports netqty=0, reconcile arms the bookkeeping-only close:
    the next chase pass must NOT send any order and must finalise the close."""
    em, ctx, leg = make_em(("SENSEX", "SENSEX"), "SENSEX26N0578800CE")
    api = StubApi()
    _reject_exit_setup(em, leg, api)
    api._positions = [{"tsym": leg.symbol, "netqty": "0"}]  # position gone (e.g. manual flatten)

    flat, _px = em._reconcile_leg_position(leg, reason="VERIFY")
    assert flat is True
    assert len(api.orders) == 0, "No order should be dispatched for a flat broker position"
    assert getattr(leg, "broker_verified_flat", False) is True

    em.must_force_flatten = lambda now=None: False
    em._do_chase_exit("Reconcile")
    assert leg.status == "CLOSED"
    assert all(k.get("buy_or_sell") != "S" for k in api.orders), \
        "Re-SELL dispatched for a broker-verified flat position!"
    assert ctx.status == "CLOSED"
    assert em._has_unreconciled_position is False


def test_emergency_reconcile_flatten_dispatches_exact_verified_qty():
    """Full-context reconcile places an exact-qty emergency exit while the broker
    holds the verified 20-unit position (chase-crash fallback path, regression:
    the old code called the non-existent order_lifecycle.close_all_market)."""
    em, ctx, leg = make_em(("SENSEX", "SENSEX"), "SENSEX26N0578800CE")
    api = StubApi()
    _reject_exit_setup(em, leg, api)

    flat_all = em._emergency_reconcile_flatten(reason="chase_crash_fallback_TestCrash")
    assert flat_all is False  # broker still holds 20; only the 10s rate-limit stops retries
    sells = [k for k in api.orders if k.get("buy_or_sell") == "S"]
    assert sells
    assert all(capture_qty(k) == "20" for k in sells)
