"""
Unit tests for AEGIS Adaptive Exit Engine (Claude's 4-Pillar Quantitative Framework).
Verifies:
1. Dynamic noise floor calculation (spread + rolling stdev + rupee floor).
2. Empirical / Realized local delta (Δprem / Δspot) and moneyness fallback.
3. Trade 1 fix: ₹19.30 option is NOT stopped out at ₹18.20 by index noise.
4. Trade 3 fix: 40-paise flicker (+3%) does NOT choke trade into breakeven prematurely.
5. Trade 2 fix: Dynamic runner rides past +5.4% and exits on 9-EMA slope cross-back or trail.
"""

from collections import deque
from datetime import datetime, timedelta
import time
import pytest

from execution.trade_context import TradeContext, TradeLeg
from execution.execution_manager import (
    AegisExecutionManager,
    DEFAULT_MIN_NOISE_FLOOR_RS,
    get_adaptive_target_profit_pct,
)


class DummyDataProvider:
    def __init__(self, quote=None):
        self._quote = quote or {"ltp": 19.30, "bp1": 19.25, "sp1": 19.45, "sq1": 500, "bq1": 500}

    def get_quote(self, exch, token):
        return self._quote


class DummyBroker:
    def __init__(self, ltp=19.30, bp1=19.25, sp1=19.45):
        self.market_data_provider = DummyDataProvider({"ltp": ltp, "bp1": bp1, "sp1": sp1, "sq1": 500, "bq1": 500})
        self._api = None


class DummyDataFetcher:
    def __init__(self, spot=23700.0):
        self._spot = spot

    def get_active_instrument(self):
        return ("NIFTY", "NIFTY")

    def get_index_ltp(self):
        return self._spot


class DummyEngine:
    def __init__(self, data_fetcher):
        self.option_cache = {}
        self.time_bar_engine = None
        self.data_fetcher = data_fetcher
        self._spot_override = None
        self._spot_timestamp_override = None

    @property
    def last_spot(self):
        return self._spot_override if self._spot_override is not None else self.data_fetcher._spot

    @last_spot.setter
    def last_spot(self, value):
        self._spot_override = value

    @property
    def _last_spot_ts(self):
        return self._spot_timestamp_override if self._spot_timestamp_override is not None else time.time()

    @_last_spot_ts.setter
    def _last_spot_ts(self, value):
        self._spot_timestamp_override = value


def create_test_manager(entry_price=19.30, spot_entry=23700.0, signal_type="PUT"):
    broker = DummyBroker(ltp=entry_price, bp1=entry_price - 0.10, sp1=entry_price + 0.10)
    data_fetcher = DummyDataFetcher(spot=spot_entry)
    engine = DummyEngine(data_fetcher)
    
    # Instantiate manager without starting background loop
    em = AegisExecutionManager.__new__(AegisExecutionManager)
    em.broker = broker
    em.data_fetcher = data_fetcher
    em.aegis_engine = engine
    em.tp_order_id = None
    import threading
    em.trade_lock = threading.Lock()
    em._order_update_lock = threading.Lock()
    em._order_updates = {}
    em._order_history_poll_ts = {}
    em._order_history_poll_interval_sec = 2.5
    em._trade_quote_history = deque(maxlen=60)
    em._option_cache = {}
    em._prestaged_cache = {}
    em.strike_cooldowns = {}
    em.last_trade_exit_time = 0.0
    em.last_trade_loss = False
    em.last_trade_date = None
    em.last_exited_route = ""
    em.consecutive_route_cooldown_sec = 180.0
    em.trades_today = 0
    em.daily_pnl = 0.0
    em.inter_trade_cooldown_sec = 120.0
    em.post_loss_cooloff_sec = 120.0
    em.consec_loss_stop = 2
    em.daily_loss_cap_rs = 2000.0
    em._consec_losses = 0
    em._is_exiting = False
    em._is_entering = False
    em._latest_option_book = lambda _symbol: (
        time.time() - 0.05,
        float(broker.market_data_provider._quote.get("ltp", 0.0)),
        float(broker.market_data_provider._quote.get("bp1", 0.0)),
        float(broker.market_data_provider._quote.get("sp1", 0.0)),
    )

    # Clock independence: after 15:10 IST the real must_force_flatten() returns
    # True and force-flattens every open trade, breaking exit-behavior assertions.
    # Mock it so these unit tests pass 24x7 regardless of run time.
    em.must_force_flatten = lambda now=None: False

    class DummyCapitalEngine:
        def get_available_funds(self):
            return 50000.0
        def deduct_funds(self, amt):
            pass
        def add_funds(self, amt):
            pass
    em.capital_engine = DummyCapitalEngine()
    em._save_trades_today = lambda: None
    em._save_active_context = lambda: None
    em._load_trades_today = lambda: 0

    class DummyOrderLifecycle:
        def execute_entry(self, leg, live_ask=0.0):
            return True
        def execute_exit(self, leg, reason=""):
            return True
    em.order_lifecycle = DummyOrderLifecycle()
    
    # Create trade context
    context = TradeContext(
        signal_type=signal_type,
        strategy="AEGIS_GAMMA_BURST",
        index_entry=spot_entry,
    )
    context.entry_time = datetime.now()
    leg = TradeLeg(
        token="12345",
        symbol=f"NIFTY_{signal_type}",
        strike=23700.0,
        option_type=signal_type,
        quantity=1,
        entry_price=entry_price,
    )
    leg.highest_price = entry_price
    leg.breakeven_locked = False
    context.add_leg(leg)
    context.status = "OPEN"
    em.trade_context = context
    
    # Track exits
    em.exit_reasons = []
    def fake_close_all_legs(reason, is_tp_maker=False, limit_fill_price=0.0):
        em.exit_reasons.append(reason)
    em._close_all_legs = fake_close_all_legs
    em.must_force_flatten = lambda now=None: False
    
    return em, context, leg


def attach_scored_contract(em, signal, token="9001", strike=None):
    """Adds the exact option identity required by the live execution contract."""
    symbol = str(signal.get("sym", ""))
    signal_type = "CALL" if str(signal.get("signal", "")).upper() in ("UP", "CALL") else "PUT"
    suffix = "CE" if signal_type == "CALL" else "PE"
    if not symbol.upper().endswith(suffix):
        symbol = f"{symbol.rstrip('0123456789CEPUT')}{int(strike or signal.get('spot', 0))}{suffix}"
    strike_value = float(strike or signal.get("metrics", {}).get("strike", 0.0) or 0.0)
    if strike_value <= 0.0:
        import re
        match = re.search(r"(\d+)(?:CE|PE)$", symbol.upper())
        assert match is not None, f"Could not determine strike from {symbol}"
        strike_value = float(match.group(1))

    signal["sym"] = symbol
    signal["contract_token"] = str(token)
    signal.setdefault("metrics", {}).update({
        "strike": strike_value,
        "side": suffix,
    })
    if em.aegis_engine is not None:
        em.aegis_engine.token_map = {
            str(token): {"side": 0 if signal_type == "CALL" else 1, "strike": strike_value}
        }
        em.aegis_engine.t2s = {str(token): symbol}
        em.aegis_engine.history_30s = {
            symbol: deque([(
                float(signal.get("timestamp") or time.time()),
                float(signal.get("lp", 0.0) or 100.0),
                float(signal.get("lp", 0.0) or 100.0) - 0.1,
                float(signal.get("lp", 0.0) or 100.0) + 0.1,
                100.0,
                100.0,
            )])
        }
    if not isinstance(getattr(em, "_option_cache", None), dict):
        em._option_cache = {}
    em._option_cache[str(token)] = float(signal.get("lp", 0.0) or 100.0)
    return signal


def test_noise_floor_calculation():
    """Verify that noise floor is at least DEFAULT_MIN_NOISE_FLOOR_RS and scales with spread."""
    em, context, leg = create_test_manager(entry_price=13.00)
    
    # Case A: tight spread (0.10) -> clamped to default minimum ₹1.50
    floor_tight = em._compute_noise_floor(leg, live_bid=13.00, live_ask=13.10)
    assert floor_tight >= DEFAULT_MIN_NOISE_FLOOR_RS
    assert floor_tight == 1.50
    
    # Case B: wide spread (1.00) -> 2.5 * 1.00 = ₹2.50
    floor_wide = em._compute_noise_floor(leg, live_bid=13.00, live_ask=14.00)
    assert floor_wide >= 2.50


def test_realized_delta_calculation():
    """Verify empirical delta calculation and bounds."""
    em, context, leg = create_test_manager(entry_price=19.30, spot_entry=23700.0, signal_type="PUT")
    
    # Spot moves 10 points down (favorable for PUT), premium moves 4.0 points up
    delta = em._compute_realized_delta(context, leg, curr_spot=23690.0)
    # Δprem = 0 (current_price hasn't changed yet), fallback moneyness used
    assert 0.25 <= delta <= 0.70
    
    # Now set leg current_price to 23.30 (4 pts gain on 10 pts spot move)
    leg.current_price = 23.30
    delta_emp = em._compute_realized_delta(context, leg, curr_spot=23690.0)
    assert round(delta_emp, 2) == 0.40


def test_spot_invalidation_sl_prevents_trade1_noise_stopout():
    """
    V4.2 (Nov 12, 2026 mandate): The tick-cadence Option Premium Hard SL is PERMANENTLY
    DECOMMISSIONED (AEGIS_ENABLE_PREMIUM_DISASTER_FLOOR defaults to "0"). AEGIS now exits
    strictly on Universal Spot Invalidation (Entry Candle High/Low + normalized cushion).

    - Premium noise dip (-3.1%): stays OPEN.
    - Premium spread dip (-5.7%): stays OPEN.
    - Catastrophic premium crash (-12.9%) with spot 100% intact: STILL OPEN (option bid wiggles
      on 0-DTE must never shake out a trade whose underlying is safe).
    - Only a genuine spot breach of the entry candle high (PUT invalidation) exits.
    """
    em, context, leg = create_test_manager(entry_price=19.30, spot_entry=23700.0, signal_type="PUT")
    context.pillar = 2
    context.pa_setup_name = "micro_sweep_reclaim"
    context.candle_high = 23710.0

    # Shallow dip: spot intact, premium -3.1% -> stays OPEN
    em.data_fetcher._spot = 23702.0
    em.broker.market_data_provider._quote = {"ltp": 18.70, "bp1": 18.70, "sp1": 18.85}
    em._manage_open_trade()
    assert len(em.exit_reasons) == 0, f"Premature exit occurred: {em.exit_reasons}"
    assert leg.status == "OPEN"

    # Spread/noise dip at -5.7% (18.20): stays OPEN
    em.broker.market_data_provider._quote = {"ltp": 18.20, "bp1": 18.20, "sp1": 18.35}
    em._manage_open_trade()
    assert len(em.exit_reasons) == 0, f"Premature exit occurred: {em.exit_reasons}"
    assert leg.status == "OPEN"

    # Catastrophic premium crash (-12.95%) with spot intact -> premium floor is decommissioned, stays OPEN
    em.broker.market_data_provider._quote = {"ltp": 16.80, "bp1": 16.80, "sp1": 16.90}
    em._manage_open_trade()
    assert len(em.exit_reasons) == 0, f"Premium bid wiggle must not exit: {em.exit_reasons}"
    assert leg.status == "OPEN"

    # Pure spot invalidation: spot rallies through the entry candle high + cushion -> PUT exits
    em.data_fetcher._spot = 23770.0
    em.broker.market_data_provider._quote = {"ltp": 16.80, "bp1": 16.80, "sp1": 16.90}
    em._manage_open_trade()
    assert len(em.exit_reasons) == 1
    assert "Candle High" in em.exit_reasons[0]


def test_breakeven_guard_two_key_prevents_trade3_premature_cut():
    """
    Trade 3 Reproduction: Entry @ ₹13.35.
    Price wiggles to ₹13.75 (+3.0% = +40 paise).
    In the old code, +3% immediately armed Breakeven and cut at 13.35.
    In the new code, 40 paise does NOT meet the 2.5x Noise Floor hurdle (₹3.75), so it stays OPEN.
    """
    em, context, leg = create_test_manager(entry_price=13.35, spot_entry=23830.0, signal_type="PUT")
    
    # Touch ₹13.75 briefly (+40 paise)
    em.broker.market_data_provider._quote = {"ltp": 13.75, "bp1": 13.70, "sp1": 13.80}
    em._manage_open_trade()
    
    # Breakeven must NOT be locked because 40 paise < 2.5 * 1.50 (₹3.75)
    assert not getattr(leg, "breakeven_locked", False)
    
    # Now price dips back to entry 13.35
    em.broker.market_data_provider._quote = {"ltp": 13.35, "bp1": 13.35, "sp1": 13.45}
    em._manage_open_trade()
    
    # Trade must still be alive!
    assert len(em.exit_reasons) == 0, f"Trade was choked by breakeven: {em.exit_reasons}"


def test_breakeven_guard_arms_on_genuine_impulse():
    """Verify that Cost-Shield / Breakeven arms once peak gain reaches >= +3.0%."""
    em, context, leg = create_test_manager(entry_price=13.00, spot_entry=23700.0, signal_type="PUT")
    
    # Gain reaches +3.7% (13.48 >= 3.5% STEP2_TRIGGER_PCT, arms cost-shield / breakeven)
    em.broker.market_data_provider._quote = {"ltp": 13.48, "bp1": 13.48, "sp1": 13.50}
    # Simulate 65s elapsed
    context.entry_time = datetime.now() - timedelta(seconds=65)
    
    em._manage_open_trade()
    
    # Breakeven / Cost-Shield must be armed!
    assert getattr(leg, "breakeven_locked", False)
    
    # Now if price drops back to entry (13.00), it should exit scratch
    em.broker.market_data_provider._quote = {"ltp": 13.00, "bp1": 13.00, "sp1": 13.05}
    em._manage_open_trade()
    
    assert len(em.exit_reasons) == 1
    assert any(term in em.exit_reasons[0] for term in ("Cost-Shield Scratch Exit", "Breakeven Guard", "Near-Target Profit Lock"))


def test_scale_invariance_across_nifty_and_sensex():
    """
    Verify scale-invariance:
    NIFTY (spot=24000) vs SENSEX (spot=80000).
    Points must scale proportionally with spot in basis points:
    - Spot SL at 6.5 bps: Nifty ~15.6 pts, Sensex ~52.0 pts.
    - Breakeven confirm at 3.5 bps: Nifty ~8.4 pts, Sensex ~28.0 pts.
    """
    # 1. NIFTY
    em_nifty, ctx_nifty, leg_nifty = create_test_manager(entry_price=20.0, spot_entry=24000.0, signal_type="PUT")
    em_nifty.data_fetcher.get_active_instrument = lambda: ("NIFTY", "NIFTY")
    
    # 10 pts adverse move on Nifty (10/24000 = 4.17 bps < 6.5 bps) -> Must stay OPEN
    # (premium dip kept inside the -3.5% V4 floor so only spot scaling is tested)
    em_nifty.data_fetcher._spot = 24010.0
    em_nifty.broker.market_data_provider._quote = {"ltp": 19.50, "bp1": 19.40, "sp1": 19.60}
    em_nifty._manage_open_trade()
    assert len(em_nifty.exit_reasons) == 0
    
    # 2. SENSEX
    em_sensex, ctx_sensex, leg_sensex = create_test_manager(entry_price=50.0, spot_entry=80000.0, signal_type="PUT")
    em_sensex.data_fetcher.get_active_instrument = lambda: ("SENSEX", "SENSEX")
    
    # 35 pts adverse move on Sensex (35/80000 = 4.37 bps < 6.5 bps) -> In old hardcoded code this was stopped out!
    # In normalized code, 35 pts is within the 6.5 bps buffer (52 pts), so it must stay OPEN!
    # (premium dip kept inside the -3.5% V4 floor so only spot scaling is tested)
    em_sensex.data_fetcher._spot = 80035.0
    em_sensex.broker.market_data_provider._quote = {"ltp": 48.75, "bp1": 48.60, "sp1": 48.90}
    em_sensex._manage_open_trade()
    assert len(em_sensex.exit_reasons) == 0, f"Sensex falsely exited on 35 pts: {em_sensex.exit_reasons}"
    
    # 55 pts adverse move on Sensex (55/80000 = 6.87 bps > 6.5 bps) -> Must trigger Spot Adverse SL
    em_sensex.data_fetcher._spot = 80055.0
    em_sensex._manage_open_trade()
    assert len(em_sensex.exit_reasons) == 1
    assert "Spot Adverse Rally SL" in em_sensex.exit_reasons[0]


def test_entry_exception_clears_trade_context():
    """Verify that an unexpected exception in execute_entry leaves trade_context as None."""
    from unittest.mock import patch
    em, _, _ = create_test_manager()
    em.trade_context = None
    em.can_enter = lambda now: True
    em.get_instrument_params = lambda: {"index_name": "NIFTY", "lot_size": 65, "exchange": "NFO"}
    em.data_fetcher.get_weekly_option_tokens = lambda: []
    em.strike_cooldowns = {}

    class DummyCapEngine:
        def get_available_funds(self):
            return 10000.0
        def deduct_funds(self, amt):
            pass
    em.capital_engine = DummyCapEngine()

    with patch("execution.execution_manager.StrikeSelector.select_strikes") as mock_sel:
        mock_sel.return_value = [({"symbol": "NIFTY24500CE", "token": "12345", "strike": 24500.0}, 1, 25.0)]

        # Force execute_entry to raise an unhandled broker exception
        def crash_entry(*args, **kwargs):
            raise ConnectionResetError("Broker connection reset during order placement")
        em.order_lifecycle.execute_entry = crash_entry

        signal = {
            "signal": "UP",
            "conviction": 85.0,
            "lp": 25.0,
            "sym": "NIFTY26SEP24500CE",
            "metrics": {"recommended_tp_pct": 0.35, "strike": 24500.0, "spot_price": 24000.0},
            "timestamp": time.time(),
        }

        # Should handle exception gracefully without crashing or leaving trade_context hanging
        em._process_aegis_signal(signal)
        assert em.trade_context is None
        assert em.has_open_trade() is False
        assert em._is_entering is False


def test_stale_spot_history_returns_zero_drift():
    """Verify that get_spot_drift and get_spot_drift_bps return 0.0 when spot feed is older than 60s."""
    from core.pipeline_orchestrator import AegisPipelineOrchestrator
    engine = AegisPipelineOrchestrator(
        token_to_symbol={"26000": "NIFTY"},
        token_map={"26000": {"side": 0, "strike": 0.0, "is_index": True}},
        strike_step=50.0,
        underlying_tokens={"26000"},
    )
    now = 1700000100.0
    # Add a tick from 120s ago
    engine.spot_history.append((now - 120.0, 24000.0))
    engine.last_spot = 24100.0

    # Must detect stale feed and return 0.0
    assert engine.get_spot_drift(now, lookback_sec=60.0) == 0.0
    assert engine.get_spot_drift_bps(now, lookback_sec=60.0) == 0.0


def test_backtest_breakeven_spot_confirmation_parity():
    """Verify backtest engine arms breakeven on favorable spot move (Key 2) even before 60s."""
    from backtest import SimulatedPositionManager
    # NOTE: BE-arming needs peak gain above 2.5x noise floor WITHOUT hitting TP first,
    # so this test pins a wide 35% target to isolate breakeven logic (live default is 4%).
    pos_mgr = SimulatedPositionManager(target_pct=35.0)
    pos_mgr.update_spot(24000.0)

    sig = {
        "sym": "NIFTY26SEP24500CE",
        "signal": "UP",
        "lp": 25.0,
        "metrics": {"eqs_score": 80.0, "spot_price": 24000.0},
    }
    pos_mgr.on_signal(sig, current_ts_sec=100.0)
    trade = pos_mgr.active_trade
    assert trade is not None
    assert trade.breakeven_locked is False

    # Simulate 20 seconds later (hold_time = 20s < 60s)
    # Peak gain reaches 30.0 (+20% gain, ₹5.00 > 2.5x noise floor ₹3.75, below 35% target)
    # Spot moves favorably by +12 pts (> 3.5 bps = 8.4 pts)
    pos_mgr.update_spot(24012.0)
    pos_mgr.on_tick("NIFTY26SEP24500CE", 30.0, current_ts_sec=120.0)

    # Breakeven MUST be armed thanks to spot confirmation!
    assert trade.breakeven_locked is True


def test_adaptive_time_exit_behavior():
    """Verify adaptive time stop: 5m hard stop on flat/loss vs 6m conditional extension on profit."""
    # Case 1: Unprofitable / flat trade at 300s exits immediately with 5-Min Hard Time Stop
    em, context, leg = create_test_manager(entry_price=100.0, spot_entry=24000.0, signal_type="PUT")
    em._is_active_instrument_expiry = lambda *args: False
    context.entry_time = datetime.now() - timedelta(seconds=301)

    # Current price flat @ 100.0 (0% gain)
    em.broker.market_data_provider = DummyDataProvider({"ltp": 100.0, "bp1": 100.0, "sp1": 100.1})
    em._manage_open_trade()

    assert len(em.exit_reasons) == 1
    assert "5-Min Hard Time Stop" in em.exit_reasons[0]

    # Case 2: Profitable trade (+3.0%) at 300s extends beyond 300s without premature exit
    em2, context2, leg2 = create_test_manager(entry_price=100.0, spot_entry=24000.0, signal_type="PUT")
    em2._is_active_instrument_expiry = lambda *args: False
    context2.entry_time = datetime.now() - timedelta(seconds=305)

    # Current price is 103.0 (+3.0% gain > HOLD_EXTEND_MIN_PROFIT_PCT 2.0%)
    em2.broker.market_data_provider = DummyDataProvider({"ltp": 103.0, "bp1": 103.0, "sp1": 103.1})
    em2._manage_open_trade()

    # Should NOT exit at 305s — extension is granted!
    assert len(em2.exit_reasons) == 0

    # Case 3: Reaches 360s ceiling -> Exits with 6-Min Extended Time Stop
    context2.entry_time = datetime.now() - timedelta(seconds=361)
    em2._manage_open_trade()

    assert len(em2.exit_reasons) == 1
    assert "6-Min Extended Time Stop" in em2.exit_reasons[0]


def test_trade_attribution_and_telemetry_capture():
    """Verify rich trade telemetry, MFE/MAE calculation, futures correlation, and JSON persistence."""
    import tempfile
    import json
    import os

    em, context, leg = create_test_manager(entry_price=100.0, spot_entry=24000.0, signal_type="CALL")
    leg.symbol = "NIFTY24000CE"
    leg.status = "CLOSED"
    leg.exit_price = 104.0
    leg.highest_price = 106.0
    leg.time_to_highest_sec = 45.0
    leg.lowest_price = 99.0
    leg.time_to_lowest_sec = 10.0
    context.entry_time = datetime.now() - timedelta(seconds=120)
    context.exit_time = datetime.now()

    context.decision_payload = {
        "impulse_timestamp": time.time() - 120.5,
        "impulse_time_iso": "2026-09-08 13:27:29.000",
        "fill_timestamp": time.time() - 120.0,
        "fill_time_iso": "2026-09-08 13:27:29.500",
        "entry_latency_ms": 500.0,
        "futures_price": 24050.0,
        "basis_pts": 50.0,
        "basis_bps": 20.8,
        "futures_volume": 150000.0,
        "volatility_burst_age_sec": 12.5,
        "trigger_age_sec": 1.2,
        "eqs_score": 85.0,
        "microstructure_score": 90.0,
        "persistence_score": 0.88,
    }
    em.aegis_engine.last_futures_price = 24065.0 # Futures moved +15 pts favorably

    with tempfile.TemporaryDirectory() as tmpdir:
        em.history_file = os.path.join(tmpdir, "trade_history.json")
        em._append_history("Take-Profit Hit (+4.0%)", total_pnl=260.0)

        assert os.path.exists(em.history_file)
        with open(em.history_file, "r") as f:
            data = json.load(f)

        assert len(data) == 1
        record = data[0]
        assert record["symbol"] == "NIFTY24000CE"
        assert record["pnl"] == 260.0
        assert record["exit_reason"] == "Take-Profit Hit (+4.0%)"

        # Check attribution structure
        attr = record["post_trade_attribution"]
        assert attr["outcome"] == "WINNER"
        assert attr["classification"] == "TAKE_PROFIT_LIMIT"
        assert attr["peak_mfe_pct"] == 6.0
        assert attr["peak_mfe_rs"] == 6.0
        assert attr["peak_mae_pct"] == -1.0
        assert attr["entry_latency_ms"] == 500.0
        assert attr["futures_correlation"] == "CONFIRMED_DIRECTION"
        assert "WINNER CALL on NIFTY24000CE" in attr["diagnostic_summary"]

        # Check telemetry structure
        telem = record["telemetry"]
        assert telem["peak_mfe_pct"] == 6.0
        assert telem["entry_latency_ms"] == 500.0
        assert telem["futures_move_pts"] == 15.0


def test_volatility_squeeze_sustains_five_minute_baseline():
    """
    Verifies that Volatility Squeeze trades sustain the user's 5-minute baseline hold time:
    - Premature 30s timeout is disabled.
    - Squeeze trade with low/moderate momentum at 35s stays open to allow 1m candle formation / TP.
    - Squeeze trade running strong at 35s stays open.
    - Flat squeeze trade exits at 5-Min Hard Time Stop (300s).
    """
    # 1. Squeeze trade at 35s -> Must STAY OPEN (no premature 30s cut)!
    em, ctx, leg = create_test_manager(entry_price=100.0, spot_entry=24000.0, signal_type="CALL")
    em._is_active_instrument_expiry = lambda *args: False
    ctx.pa_setup_name = "volatility_squeeze_breakout"
    ctx.entry_time = datetime.now() - timedelta(seconds=35)
    em.broker.market_data_provider._quote = {"ltp": 101.0, "bp1": 100.95, "sp1": 101.05}
    em._manage_open_trade()

    assert len(em.exit_reasons) == 0  # Sustains through 35s!

    # 2. At 301s (5 minutes elapsed) -> Exits via 5-Min Hard Time Stop
    ctx.entry_time = datetime.now() - timedelta(seconds=301)
    em._manage_open_trade()
    assert len(em.exit_reasons) == 1
    assert "5-Min Hard Time Stop" in em.exit_reasons[0]


def test_consecutive_route_lockout_and_alternate_route_switch():
    """
    Verifies Consecutive Route Lockout & Cooldown:
    1. If a trade exits, the SAME route is blocked from triggering within 180s.
    2. An ALTERNATE route CAN trigger after general inter-trade cooldown.
    3. After 180s have elapsed, the original route can trigger again.
    """
    em, ctx, leg = create_test_manager(entry_price=100.0, spot_entry=24000.0, signal_type="CALL")
    em.last_exited_route = "volatility_squeeze_breakout"
    em.last_trade_exit_time = time.time() - 70.0  # Exited 70s ago (general 60s cooldown passed, but 180s route lockout active)
    em.consecutive_route_cooldown_sec = 180.0
    em.inter_trade_cooldown_sec = 60.0
    em.trade_context = None  # No open trade

    # Case 1: Same route arrives at 70s -> Must be BLOCKED!
    same_signal = {
        "type": "GAMMA_BURST_ENTRY",
        "signal": "UP",
        "conviction": 85.0,
        "lp": 100.0,
        "sym": "NIFTY24000CE",
        "metrics": {"pa_setup_name": "volatility_squeeze_breakout"}
    }
    em._process_aegis_signal(same_signal)
    assert em.trade_context is None  # Blocked by consecutive route lockout!

    # Case 2: Alternate route arrives at 70s (e.g. 20_ema_rejection) -> Must be ALLOWED!
    alt_signal = {
        "type": "GAMMA_BURST_ENTRY",
        "signal": "UP",
        "conviction": 85.0,
        "lp": 100.0,
        "sym": "NIFTY24000CE",
        "metrics": {"pa_setup_name": "20_ema_rejection"}
    }
    attach_scored_contract(em, alt_signal)
    attach_scored_contract(em, same_signal)
    import pandas as pd
    weekly_df = pd.DataFrame([
        {"strike": 24000.0, "option_type": "CE", "token": "1001", "symbol": "NIFTY24000CE"}
    ])
    em.data_fetcher.get_active_instrument = lambda: ("NIFTY", "NIFTY")
    em.data_fetcher.get_weekly_option_tokens = lambda: weekly_df
    em._option_cache = {"1001": 50.0}
    em.can_enter = lambda now: True
    em._process_aegis_signal(alt_signal)
    assert em.trade_context is not None
    assert em.trade_context.pa_setup_name == "20_ema_rejection"

    # Case 3: Momentum chain — same route after cooldown (300s) -> ALLOWED!
    # Strict unconditional lockout removed; 3rd/4th-high chain trades permitted.
    em.trade_context = None
    em.last_exited_route = "volatility_squeeze_breakout"
    em.last_trade_exit_time = time.time() - 300.0  # Cooldown (180s) elapsed
    em._process_aegis_signal(same_signal)
    assert em.trade_context is not None  # Chain allowed after cooldown.

    # Case 4: Alternate route executed and exited (setting last_exited_route to alternate)
    em.last_exited_route = "20_ema_rejection"
    em.last_trade_exit_time = time.time() - 70.0  # General 60s cooldown passed
    em._process_aegis_signal(same_signal)
    assert em.trade_context is not None
    assert em.trade_context.pa_setup_name == "volatility_squeeze_breakout"


def _make_backtest_squeeze_signal(lp=100.0, pa_setup="volatility_squeeze_breakout"):
    return {
        "sym": "NIFTY26SEP24500CE",
        "signal": "UP",
        "lp": lp,
        "metrics": {
            "eqs_score": 85.0,
            "spot_price": 24000.0,
            "recommended_tp_pct": 0.040,
            "pa_setup_name": pa_setup,
        },
    }


def test_backtest_squeeze_timeout_and_route_lockout_parity():
    """Backtest must allow squeeze trades to sustain the 5-minute window (live parity)."""
    from backtest import SimulatedPositionManager
    pos_mgr = SimulatedPositionManager()
    pos_mgr.update_spot(24000.0)

    assert pos_mgr.on_signal(_make_backtest_squeeze_signal(), current_ts_sec=1000.0) is True
    trade = pos_mgr.active_trade
    assert trade is not None
    assert trade.pa_setup_name == "volatility_squeeze_breakout"
    assert trade.target_pct == pytest.approx(4.0)

    # +0.5% at 35s hold -> trade STAYS OPEN (no premature 30s exit)
    pos_mgr.on_tick("NIFTY26SEP24500CE", 100.5, current_ts_sec=1035.0)
    assert pos_mgr.active_trade is not None

    # At 301s (5m time stop) -> exits with TIME_EXPIRED (5m)
    pos_mgr.on_tick("NIFTY26SEP24500CE", 100.5, current_ts_sec=1305.0)
    assert pos_mgr.active_trade is None
    assert "TIME_EXPIRED (5m)" in pos_mgr.completed_trades[-1].exit_reason
    assert pos_mgr.last_exited_route == "volatility_squeeze_breakout"

    # Same route immediately after cooldown -> BLOCKED by strict lockout
    assert pos_mgr.on_signal(_make_backtest_squeeze_signal(), current_ts_sec=1370.0) is False
    # Alternate route -> ALLOWED
    assert pos_mgr.on_signal(_make_backtest_squeeze_signal(pa_setup="20_ema_rejection"), current_ts_sec=1370.0) is True


def test_backtest_fast_four_percent_tp():
    """Backtest must honor the live +4.0% resting TP instead of the legacy 35% floor."""
    from backtest import SimulatedPositionManager
    pos_mgr = SimulatedPositionManager()
    pos_mgr.update_spot(24000.0)

    assert pos_mgr.on_signal(_make_backtest_squeeze_signal(), current_ts_sec=1000.0) is True
    pos_mgr.on_tick("NIFTY26SEP24500CE", 104.5, current_ts_sec=1010.0)
    assert pos_mgr.active_trade is None
    assert "TARGET_HIT (+4.0%)" in pos_mgr.completed_trades[-1].exit_reason


def test_backtest_adaptive_six_min_extension():
    """Backtest time ceiling: flat trade exits at 5m, running winner extends to 6m (live parity)."""
    from backtest import SimulatedPositionManager

    # Winner (+3%): survives 305s, exits at 360s ceiling
    pos_mgr = SimulatedPositionManager()
    pos_mgr.update_spot(24000.0)
    assert pos_mgr.on_signal(_make_backtest_squeeze_signal(), current_ts_sec=1000.0) is True
    pos_mgr.on_tick("NIFTY26SEP24500CE", 103.0, current_ts_sec=1305.0)
    assert pos_mgr.active_trade is not None  # extension granted, no premature exit
    pos_mgr.on_tick("NIFTY26SEP24500CE", 103.0, current_ts_sec=1361.0)
    assert pos_mgr.active_trade is None
    assert "TIME_EXPIRED (6m)" in pos_mgr.completed_trades[-1].exit_reason

    # Flat trade (non-squeeze route): exits at 5m hard stop
    pos_mgr2 = SimulatedPositionManager()
    pos_mgr2.update_spot(24000.0)
    assert pos_mgr2.on_signal(_make_backtest_squeeze_signal(pa_setup="20_ema_rejection"), current_ts_sec=1000.0) is True
    pos_mgr2.on_tick("NIFTY26SEP24500CE", 100.0, current_ts_sec=1301.0)
    assert pos_mgr2.active_trade is None
    assert "TIME_EXPIRED (5m)" in pos_mgr2.completed_trades[-1].exit_reason


def test_tp_maker_fill_preserves_profit_when_tp_id_cleared():
    """Verify that if tp_order_id was cleared in Section 0B, _do_chase_exit still verifies last_tp_order_id and records maker fill."""
    em, ctx, leg = create_test_manager(entry_price=10.0, spot_entry=24000.0, signal_type="CALL")
    em.tp_order_id = None
    em.last_tp_order_id = "123456"
    leg.tp_price = 10.40
    leg.status = "OPEN"
    ctx = TradeContext("CALL", "AEGIS_GAMMA_BURST", 24000.0)
    ctx.entry_time = datetime.now()
    ctx.add_leg(leg)
    em.trade_context = ctx
    
    class MockCapitalEngine:
        def add_funds(self, amt):
            pass
    em.capital_engine = MockCapitalEngine()
    em.history_file = "test_hist_tmp.json"
    em.active_context_file = "test_ctx_tmp.json"
    
    class MockApi:
        def single_order_history(self, orderno):
            assert orderno == "123456"
            return [{"status": "COMPLETE", "avgprc": "10.45"}]
    em.broker._api = MockApi()
    
    em._do_chase_exit("Take-Profit Hit", is_tp_maker=True, limit_fill_price=10.40)
    
    assert leg.status == "CLOSED"
    assert leg.exit_price == 10.45
    assert leg.pnl == pytest.approx((10.45 - 10.0) * 1 * 65)
    import os
    for f in ["test_hist_tmp.json", "test_ctx_tmp.json"]:
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass


def test_point4_gate1_breakeven_lock_at_three_percent():
    """Verify V4.1 Stepped Risk Compression: +3.0% peak arms -3.0% safety net, does NOT choke pullback at +2.0%."""
    em, ctx, leg = create_test_manager(entry_price=100.0, spot_entry=24000.0, signal_type="CALL")
    ctx.entry_time = datetime.now() - timedelta(seconds=40)
    
    # Tick 1: Peak gain reaches +2.2% (102.20) -> Stepped Safety Net is NOT armed yet (breathing room preserved)
    em.broker.market_data_provider._quote = {"ltp": 102.20, "bp1": 102.20, "sp1": 102.30, "sq1": 500, "bq1": 500}
    em._manage_open_trade()
    assert getattr(leg, "step1_sl_armed", False) is False
    assert len(em.exit_reasons) == 0

    # Tick 1b: Peak gain reaches +3.2% (103.20) -> Stepped Safety Net (-3.0% SL) arms
    em.broker.market_data_provider._quote = {"ltp": 103.20, "bp1": 103.20, "sp1": 103.30, "sq1": 500, "bq1": 500}
    em._manage_open_trade()
    assert getattr(leg, "step1_sl_armed", False) is True
    assert len(em.exit_reasons) == 0

    # Tick 2: Pullback to 100.25 (User fix: must NOT choke winning trades!)
    em.broker.market_data_provider._quote = {"ltp": 100.25, "bp1": 100.25, "sp1": 100.35, "sq1": 500, "bq1": 500}
    em._manage_open_trade()
    assert len(em.exit_reasons) == 0, f"Trade was prematurely choked on 25p pullback: {em.exit_reasons}"

    # Tick 3: Extreme drop to 96.90 (<= -3.0% SL floor) -> triggers stepped safety exit
    em.broker.market_data_provider._quote = {"ltp": 96.90, "bp1": 96.90, "sp1": 97.00, "sq1": 500, "bq1": 500}
    em._manage_open_trade()
    assert len(em.exit_reasons) == 1
    assert "Stepped Safety Net SL" in em.exit_reasons[0]


def test_pillar4_entry_candle_low_high_sl():
    """Verify Pillar 4 Pure Price Action Entry Candle Low (CALL) / High (PUT) Invalidation."""
    # Case A: CALL trade
    em, ctx, leg = create_test_manager(entry_price=22.25, spot_entry=72443.0, signal_type="CALL")
    ctx.entry_time = datetime.now() - timedelta(seconds=20)
    ctx.pa_setup_name = "hvg_network_phase_shift"
    ctx.candle_low = 72392.5
    ctx.candle_high = 72431.0

    # Spot is above candle low (72450 > 72392.5) -> trade stays open even if option wiggles
    em.data_fetcher._spot = 72450.0
    em.broker.market_data_provider._quote = {"ltp": 21.25, "bp1": 21.25, "sp1": 21.35}  # -4.5% option dip
    em._manage_open_trade()
    assert len(em.exit_reasons) == 0

    # Spot breaks below candle low + cushion (72388.0 <= 72388.9) -> structural exit triggers
    em.data_fetcher._spot = 72388.0
    em._manage_open_trade()
    assert len(em.exit_reasons) == 1
    assert "Pillar 4 Entry Candle Low Broken SL" in em.exit_reasons[0]

    # Case B: PUT trade
    em2, ctx2, leg2 = create_test_manager(entry_price=25.0, spot_entry=72400.0, signal_type="PUT")
    ctx2.entry_time = datetime.now() - timedelta(seconds=20)
    ctx2.pa_setup_name = "hvg_network_phase_shift"
    ctx2.candle_low = 72380.0
    ctx2.candle_high = 72420.0

    # Spot is below candle high (72395 < 72420.0) -> trade stays open
    em2.data_fetcher._spot = 72395.0
    em2._manage_open_trade()
    assert len(em2.exit_reasons) == 0

    # Spot breaks above candle high + cushion (72424.0 >= 72423.6) -> structural exit triggers
    em2.data_fetcher._spot = 72424.0
    em2._manage_open_trade()
    assert len(em2.exit_reasons) == 1
    assert "Pillar 4 Entry Candle High Broken SL" in em2.exit_reasons[0]


def test_v41_near_target_profit_lock():
    """Verify that when peak reaches >= +3.5%, a 0.5% pullback locks profit."""
    em, ctx, leg = create_test_manager(entry_price=100.0, spot_entry=24000.0, signal_type="CALL")
    ctx.entry_time = datetime.now() - timedelta(seconds=40)

    # Peak reaches +3.8% (103.80)
    em.broker.market_data_provider._quote = {"ltp": 103.80, "bp1": 103.80, "sp1": 103.90}
    em._manage_open_trade()
    assert len(em.exit_reasons) == 0

    # Pullback by 0.90% from peak (drops to 102.90 = +2.90% gain, drop is 0.90% >= 0.8% tolerance)
    em.broker.market_data_provider._quote = {"ltp": 102.90, "bp1": 102.90, "sp1": 103.00}
    em._manage_open_trade()
    assert len(em.exit_reasons) == 1
    assert "Near-Target Profit Lock" in em.exit_reasons[0]


def test_no_premature_three_minute_harvest_choke():
    """Verify that a trade held >= 180s with profit +1.5% to +2.5% is NOT prematurely choked,
    allowing it to continue running towards the target or 300s baseline."""
    em, ctx, leg = create_test_manager(entry_price=100.0, spot_entry=24000.0, signal_type="CALL")
    em._is_active_instrument_expiry = lambda *args: False
    ctx.entry_time = datetime.now() - timedelta(seconds=185)
    
    # Floating gain is +2.5% (102.50)
    em.broker.market_data_provider._quote = {"ltp": 102.50, "bp1": 102.50, "sp1": 102.60, "sq1": 500, "bq1": 500}
    em._manage_open_trade()
    assert len(em.exit_reasons) == 0  # No premature choke!


def test_point2_0dte_afternoon_time_stop():
    """Verify that on Expiry day after 12:30 PM, maximum holding duration is compressed to 180s."""
    em, ctx, leg = create_test_manager(entry_price=100.0, spot_entry=24000.0, signal_type="CALL")
    em._is_active_instrument_expiry = lambda now=None, l=None: True
    sim_now = datetime(2026, 9, 16, 13, 0, 0)
    ctx.entry_time = sim_now - timedelta(seconds=182)
    
    # Flat trade at 100.0 (gain 0%)
    em.broker.market_data_provider._quote = {"ltp": 100.0, "bp1": 100.0, "sp1": 100.10, "sq1": 500, "bq1": 500}
    
    # Simulate post-12:30 PM by passing sim_now
    em._manage_open_trade(now=sim_now)
    assert len(em.exit_reasons) == 1
    assert "Time Stop" in em.exit_reasons[0]


def test_point2_0dte_stagnation_trap_exit():
    """Verify that on Expiry after 12:30 PM, holding >= 90s with gain < +0.5% and opposed 50-EMA exits."""
    em, ctx, leg = create_test_manager(entry_price=100.0, spot_entry=24000.0, signal_type="CALL")
    em._is_active_instrument_expiry = lambda now=None, l=None: True
    sim_now = datetime(2026, 9, 16, 13, 0, 0)
    ctx.entry_time = sim_now - timedelta(seconds=95)
    
    # Gain is +0.2% (100.20)
    em.broker.market_data_provider._quote = {"ltp": 100.20, "bp1": 100.20, "sp1": 100.30, "sq1": 500, "bq1": 500}
    
    # Mock time bar engine with 20 EMA opposed (Spot 24000 < 20-EMA 24050 for CALL)
    class MockTBEngine:
        def get_market_structure(self, sig):
            return "NEUTRAL"
        def get_live_20_ema_and_slope(self):
            return (24050.0, -1.0)
    em.aegis_engine.time_bar_engine = MockTBEngine()
    
    em._manage_open_trade(now=sim_now)
    assert len(em.exit_reasons) == 1
    assert "0DTE_STAGNATION_TRAP_EXIT" in em.exit_reasons[0]


def test_pillar3_premium_disaster_floor_decommissioned_and_flag_gated(monkeypatch):
    """
    Production truth (Nov 12, 2026 mandate): the option premium disaster floor is DECOMMISSIONED
    by default (AEGIS_ENABLE_PREMIUM_DISASTER_FLOOR = "0"), so a -12.7% premium bid drop with the
    underlying 100% safe must NOT exit. The legacy floor is retained strictly as an optional,
    env-flag-gated emergency breaker and must still fire correctly when explicitly enabled.
    """
    import execution.execution_manager as _em_mod

    em, ctx, leg = create_test_manager(entry_price=25.20, spot_entry=74884.0, signal_type="CALL")
    ctx.entry_time = datetime.now() - timedelta(seconds=20)

    # 1. Default (flag OFF): premium crash -12.7% with spot intact -> stays OPEN
    assert _em_mod.ENABLE_PREMIUM_DISASTER_FLOOR is False, "Premium disaster floor must be OFF in production default"
    em.broker.market_data_provider._quote = {"ltp": 22.00, "bp1": 22.00, "sp1": 22.10, "sq1": 500, "bq1": 500}
    em._manage_open_trade()
    assert len(em.exit_reasons) == 0, f"Decommissioned premium floor fired: {em.exit_reasons}"
    assert leg.status == "OPEN"

    # 2. Flag explicitly enabled: the same -12.7% crash must trip the disaster breaker
    monkeypatch.setattr(_em_mod, "ENABLE_PREMIUM_DISASTER_FLOOR", True)
    em.broker.market_data_provider._quote = {"ltp": 22.00, "bp1": 22.00, "sp1": 22.10, "sq1": 500, "bq1": 500}
    em._manage_open_trade()
    assert len(em.exit_reasons) == 1
    assert "disaster floor" in em.exit_reasons[0]


def test_pillar2_entry_candle_invalidation_and_no_9ema_choke():
    """Verify that Pillar 2 (Micro-Sweep & Reclaim) is NOT choked by 9-EMA bar close,
    and exits ONLY when spot breaks the Entry Candle Low (CALL) / High (PUT)."""
    from core.price_action_engine import OHLCBar
    em, ctx, leg = create_test_manager(entry_price=25.20, spot_entry=74884.0, signal_type="CALL")
    t0 = datetime.now() - timedelta(seconds=70)
    ctx.entry_time = t0
    ctx.pa_setup_name = "micro_sweep_reclaim"
    ctx.pillar = 2
    ctx.candle_low = 74865.0
    ctx.candle_high = 74885.0
    
    # Mock TimeBarEngine with completed 1m bar closing below 9-EMA
    class MockTBEngineWith9EMA:
        def __init__(self):
            self.ema_1m = 74875.0
            self.bars_1m = [
                OHLCBar(
                    ts_start=t0.timestamp(),
                    ts_end=t0.timestamp() + 5.0,
                    open=74880.0,
                    high=74882.0,
                    low=74868.0,
                    close=74870.0,  # Closed below 9-EMA (74870.0 < 74875.0)
                    volume=1000
                )
            ]
        def get_market_structure(self, sig):
            return "NEUTRAL"
        def get_live_20_ema_and_slope(self):
            return (74875.0, 0.0)

    em.aegis_engine.time_bar_engine = MockTBEngineWith9EMA()
    em.data_fetcher._spot = 74870.0  # Above candle low (74870.0 > 74865.0)
    em.broker.market_data_provider._quote = {"ltp": 24.80, "bp1": 24.80, "sp1": 24.90, "sq1": 500, "bq1": 500}

    # Step 1: Manage open trade. Even though 1m bar closed below 9-EMA, P2 MUST NOT exit!
    em._manage_open_trade()
    assert len(em.exit_reasons) == 0, "Pillar 2 was prematurely choked by 9-EMA!"

    # Step 2: Spot breaks below Entry Candle Low + Cushion (74860.0 <= 74861.26) -> Pure PA SL triggers!
    em.data_fetcher._spot = 74860.0
    em._manage_open_trade()
    assert len(em.exit_reasons) == 1
    assert "Pillar 2 Entry Candle Low Broken SL" in em.exit_reasons[0]


def test_pillar1_entry_candle_invalidation_and_no_9ema_choke():
    """Verify that Pillar 1 (20 EMA Pullback) is NOT choked by 9-EMA bar close,
    and exits ONLY when spot breaks the Entry Candle Low (CALL) / High (PUT)."""
    from core.price_action_engine import OHLCBar
    em, ctx, leg = create_test_manager(entry_price=25.20, spot_entry=74884.0, signal_type="CALL")
    t0 = datetime.now() - timedelta(seconds=70)
    ctx.entry_time = t0
    ctx.pa_setup_name = "20_ema_rejection"
    ctx.pillar = 1
    ctx.candle_low = 74865.0
    ctx.candle_high = 74885.0
    
    class MockTBEngineWith9EMA:
        def __init__(self):
            self.ema_1m = 74875.0
            self.bars_1m = [
                OHLCBar(
                    ts_start=t0.timestamp(),
                    ts_end=t0.timestamp() + 5.0,
                    open=74880.0,
                    high=74882.0,
                    low=74868.0,
                    close=74870.0,
                    volume=1000
                )
            ]
        def get_market_structure(self, sig):
            return "NEUTRAL"
        def get_live_20_ema_and_slope(self):
            return (74875.0, 0.0)

    em.aegis_engine.time_bar_engine = MockTBEngineWith9EMA()
    em.data_fetcher._spot = 74870.0  # Above candle low (74870.0 > 74865.0)
    em.broker.market_data_provider._quote = {"ltp": 24.80, "bp1": 24.80, "sp1": 24.90, "sq1": 500, "bq1": 500}

    # Step 1: Manage open trade. Bar closed below 9-EMA, but P1 MUST NOT exit!
    em._manage_open_trade()
    assert len(em.exit_reasons) == 0, "Pillar 1 was prematurely choked by 9-EMA!"

    # Step 2: Spot breaks below Entry Candle Low + Cushion (74860.0 <= 74861.26) -> Pure PA SL triggers!
    em.data_fetcher._spot = 74860.0
    em._manage_open_trade()
    assert len(em.exit_reasons) == 1
    assert "Pillar 1 Entry Candle Low Broken SL" in em.exit_reasons[0]


def test_pillar3_entry_candle_invalidation_and_no_9ema_choke():
    """Verify that Pillar 3 (Volatility Squeeze Breakout) is NOT choked by 9-EMA bar close,
    and exits ONLY when spot breaks the Entry Candle Low (CALL) / High (PUT)."""
    from core.price_action_engine import OHLCBar
    em, ctx, leg = create_test_manager(entry_price=25.20, spot_entry=74884.0, signal_type="CALL")
    t0 = datetime.now() - timedelta(seconds=70)
    ctx.entry_time = t0
    ctx.pa_setup_name = "volatility_squeeze_breakout"
    ctx.pillar = 3
    ctx.candle_low = 74865.0
    ctx.candle_high = 74885.0
    
    class MockTBEngineWith9EMA:
        def __init__(self):
            self.ema_1m = 74875.0
            self.bars_1m = [
                OHLCBar(
                    ts_start=t0.timestamp(),
                    ts_end=t0.timestamp() + 5.0,
                    open=74880.0,
                    high=74882.0,
                    low=74868.0,
                    close=74870.0,
                    volume=1000
                )
            ]
        def get_market_structure(self, sig):
            return "NEUTRAL"
        def get_live_20_ema_and_slope(self):
            return (74875.0, 0.0)

    em.aegis_engine.time_bar_engine = MockTBEngineWith9EMA()
    em.data_fetcher._spot = 74870.0  # Above candle low (74870.0 > 74865.0)
    em.broker.market_data_provider._quote = {"ltp": 24.80, "bp1": 24.80, "sp1": 24.90, "sq1": 500, "bq1": 500}

    # Step 1: Manage open trade. Bar closed below 9-EMA, but P3 MUST NOT exit!
    em._manage_open_trade()
    assert len(em.exit_reasons) == 0, "Pillar 3 was prematurely choked by 9-EMA!"

    # Step 2: Spot breaks below Entry Candle Low + Cushion (74860.0 <= 74861.26) -> Pure PA SL triggers!
    em.data_fetcher._spot = 74860.0
    em._manage_open_trade()
    assert len(em.exit_reasons) == 1
    assert "Pillar 3 Entry Candle Low Broken SL" in em.exit_reasons[0]


def test_atomic_modify_in_place_on_exit():
    """Verify that when resting TP exists, exit modifies the TP in-place without separate prior cancel."""
    em, ctx, leg = create_test_manager(entry_price=100.0, spot_entry=24000.0, signal_type="CALL")
    ctx.entry_time = datetime.now() - timedelta(seconds=30)
    em.tp_order_id = "240925000999"
    leg.highest_bid = 103.60  # +3.6% reached
    
    # Tick: pullback to 102.70 (pullback 0.9% >= 0.8% tolerance) -> Near-Target Profit Lock
    em.broker.market_data_provider._quote = {"ltp": 102.70, "bp1": 102.70, "sp1": 102.80, "sq1": 500, "bq1": 500}
    
    em._manage_open_trade()
    assert len(em.exit_reasons) == 1
    assert "Near-Target Profit Lock" in em.exit_reasons[0]
    # TP order ID MUST NOT be wiped prematurely!
    assert em.tp_order_id == "240925000999"

    # Now verify _do_chase_exit executes modify_order directly
    modified_orders = []
    cancelled_orders = []
    placed_orders = []

    class MockShoonyaAPI:
        def __init__(self):
            self.modified = False
        def modify_order(self, **kwargs):
            modified_orders.append(kwargs)
            self.modified = True
            em.on_order_update({
                "norenordno": kwargs.get("orderno"),
                "status": "COMPLETE",
                "avgprc": "102.70",
            })
            return {"stat": "Ok", "result": kwargs.get("orderno")}
        def cancel_order(self, orderno):
            cancelled_orders.append(orderno)
            return {"stat": "Ok"}
        def place_order(self, **kwargs):
            placed_orders.append(kwargs)
            return {"stat": "Ok", "norenordno": "NEW_SELL_123"}
        def single_order_history(self, orderno):
            if self.modified:
                return [{"status": "COMPLETE", "flprc": "102.70"}]
            return [{"status": "OPEN", "flprc": "0.00"}]

    em.broker._api = MockShoonyaAPI()
    em.get_instrument_params = lambda: {"exchange": "NFO", "lot_size": 25}
    em._append_history = lambda r, p: None
    
    em._do_chase_exit(em.exit_reasons[0])
    
    # Must have called modify_order with resting TP orderno
    assert len(modified_orders) == 1
    assert modified_orders[0]["orderno"] == "240925000999"
    assert modified_orders[0]["newprice_type"] == "LMT"
    # Must NOT have cancelled or placed fresh order
    assert len(cancelled_orders) == 0
    assert len(placed_orders) == 0
    assert leg.status == "CLOSED"
    assert em.tp_order_id is None  # Cleaned up at end of trade


def test_atomic_modify_fallback_on_error():
    """Verify that if modify_order fails, safe fallback cancels TP and places fresh exit order."""
    em, ctx, leg = create_test_manager(entry_price=100.0, spot_entry=24000.0, signal_type="CALL")
    ctx.entry_time = datetime.now() - timedelta(seconds=30)
    em.tp_order_id = "240925000888"
    leg.highest_bid = 103.60
    
    em.broker.market_data_provider._quote = {"ltp": 102.70, "bp1": 102.70, "sp1": 102.80, "sq1": 500, "bq1": 500}
    em._manage_open_trade()
    
    modified_orders = []
    cancelled_orders = []
    placed_orders = []

    class FailingShoonyaAPI:
        def __init__(self):
            self.placed = False
        def modify_order(self, **kwargs):
            modified_orders.append(kwargs)
            return {"stat": "Not_Ok", "emsg": "Order cannot be modified"}
        def cancel_order(self, orderno):
            cancelled_orders.append(orderno)
            return {"stat": "Ok"}
        def place_order(self, **kwargs):
            placed_orders.append(kwargs)
            self.placed = True
            return {"stat": "Ok", "norenordno": "NEW_SELL_456"}
        def single_order_history(self, orderno):
            if self.placed:
                return [{"status": "COMPLETE", "flprc": "102.50"}]
            return [{"status": "OPEN", "flprc": "0.00"}]

    em.broker._api = FailingShoonyaAPI()
    em.get_instrument_params = lambda: {"exchange": "NFO", "lot_size": 25}
    em._append_history = lambda r, p: None
    
    em._do_chase_exit(em.exit_reasons[0])
    
    assert len(modified_orders) == 1
    # On failure, it safely cancelled the old TP order
    assert len(cancelled_orders) == 1
    assert cancelled_orders[0] == "240925000888"
    # And placed fresh sell order
    assert len(placed_orders) == 1
    assert leg.status == "CLOSED"


def test_trailing_structural_sl_on_entry_bar_close():
    """Verify that once the entry candle closes, the structural SL trails to the entry candle's low."""
    from core.price_action_engine import OHLCBar
    em, ctx, leg = create_test_manager(entry_price=14.0, spot_entry=23122.0, signal_type="CALL")
    t_signal = datetime.now() - timedelta(seconds=90)
    t_entry = t_signal + timedelta(seconds=65)  # 5s into the entry bar
    ctx.entry_time = t_entry
    ctx.pillar = 4
    ctx.pa_setup_name = "hvg_bos_breakout"
    ctx.candle_low = 23111.55  # Signal candle low
    ctx.candle_high = 23117.85

    class MockTBEngine:
        def __init__(self):
            self.bars_1m = [
                # Signal Bar (closed before entry)
                OHLCBar(
                    ts_start=t_signal.timestamp(),
                    ts_end=t_signal.timestamp() + 60.0,
                    open=23115.70,
                    high=23117.85,
                    low=23111.55,
                    close=23117.50,
                )
            ]

    em.aegis_engine.time_bar_engine = MockTBEngine()

    # Step 1: During entry bar, spot drops to 23,114.0 (above signal candle low 23,111.55) -> Trade stays open
    em.data_fetcher._spot = 23114.0
    em.broker.market_data_provider._quote = {"ltp": 13.50, "bp1": 13.50, "sp1": 13.60, "sq1": 500, "bq1": 500}
    em._manage_open_trade()
    assert len(em.exit_reasons) == 0

    # Step 2: Entry bar closes with Low = 23,115.95, High = 23,122.55
    entry_bar = OHLCBar(
        ts_start=t_signal.timestamp() + 60.0,
        ts_end=t_signal.timestamp() + 120.0,
        open=23118.50,
        high=23122.55,
        low=23115.95,
        close=23122.30,
    )
    em.aegis_engine.time_bar_engine.bars_1m.append(entry_bar)

    # Step 3: Next tick spot drops to 23,114.0.
    # 23,114.0 is > 23,111.55 (signal candle low), BUT <= 23,114.8 (entry candle low 23,115.95 - cushion 1.15)!
    # Structural trailing exit MUST fire!
    em._manage_open_trade()
    assert len(em.exit_reasons) == 1
    assert "Pillar 4 Entry Candle Low Broken SL" in em.exit_reasons[0]
    assert "23114.8" in em.exit_reasons[0]
    assert "Trailed with cushion" in em.exit_reasons[0]


def test_trailing_structural_sl_put():
    """Verify that for PUT trades, once the entry candle closes, structural SL trails down to entry candle's high + cushion."""
    from core.price_action_engine import OHLCBar
    em, ctx, leg = create_test_manager(entry_price=18.0, spot_entry=23100.0, signal_type="PUT")
    t_signal = datetime.now() - timedelta(seconds=90)
    t_entry = t_signal + timedelta(seconds=65)
    ctx.entry_time = t_entry
    ctx.pillar = 1
    ctx.pa_setup_name = "20_ema_rejection"
    ctx.candle_low = 23090.0
    ctx.candle_high = 23120.0  # Signal candle high

    class MockTBEngine:
        def __init__(self):
            self.bars_1m = [
                # Signal bar
                OHLCBar(
                    ts_start=t_signal.timestamp(),
                    ts_end=t_signal.timestamp() + 60.0,
                    open=23110.0,
                    high=23120.0,
                    low=23090.0,
                    close=23095.0,
                )
            ]

    em.aegis_engine.time_bar_engine = MockTBEngine()

    # Step 1: During entry bar, spot rallies to 23,112.0 (below signal high + cushion) -> Stays open
    em.data_fetcher._spot = 23112.0
    em._manage_open_trade()
    assert len(em.exit_reasons) == 0

    # Step 2: Entry bar closes with High = 23,108.0 (lower high than signal bar 23,120.0)
    entry_bar = OHLCBar(
        ts_start=t_signal.timestamp() + 60.0,
        ts_end=t_signal.timestamp() + 120.0,
        open=23095.0,
        high=23108.0,
        low=23085.0,
        close=23088.0,
    )
    em.aegis_engine.time_bar_engine.bars_1m.append(entry_bar)

    # Step 3: Spot rallies to 23,110.0 (above trailed high 23,108.0 + cushion 1.15 = 23,109.15)
    # Structural trailing exit MUST fire!
    em.data_fetcher._spot = 23110.0
    em._manage_open_trade()
    assert len(em.exit_reasons) == 1
    assert "Pillar 1 Entry Candle High Broken SL" in em.exit_reasons[0]
    assert "23109.2" in em.exit_reasons[0]
    assert "Trailed with cushion" in em.exit_reasons[0]


def test_trade37_forensic_replication():
    """Forensic verification: Trade #37 on 16 Oct exits early on entry candle low breakdown rather than 300s timeout."""
    from core.price_action_engine import OHLCBar
    em, ctx, leg = create_test_manager(entry_price=14.05, spot_entry=23122.30, signal_type="CALL")
    t0 = datetime.now() - timedelta(seconds=140)  # ~2.3 minutes ago (well within 300s time ceiling)
    ctx.entry_time = t0 + timedelta(seconds=65)  # 13:11:05
    ctx.pillar = 4
    ctx.pa_setup_name = "hvg_bos_breakout"
    ctx.candle_low = 23111.55  # 13:10 Signal Candle Low
    ctx.candle_high = 23117.85

    class MockTBEngine:
        def __init__(self):
            self.bars_1m = [
                # 13:10 Signal Bar
                OHLCBar(ts_start=t0.timestamp(), ts_end=t0.timestamp() + 60.0,
                        open=23115.70, high=23117.85, low=23111.55, close=23117.50),
                # 13:11 Entry Bar (closed at 13:12:00)
                OHLCBar(ts_start=t0.timestamp() + 60.0, ts_end=t0.timestamp() + 120.0,
                        open=23118.50, high=23122.55, low=23115.95, close=23122.30),
            ]

    em.aegis_engine.time_bar_engine = MockTBEngine()

    # At 13:13:20, spot fell to 23,112.85 (breaking entry bar low 23,115.95 - cushion 1.15 = 23,114.80)
    em.data_fetcher._spot = 23112.85
    em.broker.market_data_provider._quote = {"ltp": 13.00, "bp1": 13.00, "sp1": 13.10}
    em._manage_open_trade()

    # In old code: would NOT exit (23,112.85 > 23,111.55) and hold until 300s decay down to ₹10.95 (-8.4%)
    # In new code: exits cleanly on Entry Candle Low break at ~₹13.00 (-4.1%)
    assert len(em.exit_reasons) == 1
    assert "Pillar 4 Entry Candle Low Broken SL" in em.exit_reasons[0]
    assert "23114.8" in em.exit_reasons[0]


def test_thin_candle_bypass_and_cushion_protection():
    """Verify that thin candles (< 1.5 bps hurdle) are skipped for trailing,
    and micro-wicks within the cushion buffer do not prematurely stop out the trade."""
    from core.price_action_engine import OHLCBar
    em, ctx, leg = create_test_manager(entry_price=20.0, spot_entry=23988.0, signal_type="CALL")
    t0 = datetime.now() - timedelta(seconds=150)
    ctx.entry_time = t0 + timedelta(seconds=65)
    ctx.pillar = 2
    ctx.pa_setup_name = "micro_sweep_reclaim"
    ctx.candle_low = 23980.0  # Signal candle low
    ctx.candle_high = 23995.0

    class MockTBEngine:
        def __init__(self):
            self.bars_1m = [
                # Signal Bar
                OHLCBar(ts_start=t0.timestamp(), ts_end=t0.timestamp() + 60.0,
                        open=23985.0, high=23995.0, low=23980.0, close=23992.0),
            ]

    em.aegis_engine.time_bar_engine = MockTBEngine()

    # 1. First Candle Cushion Test:
    # Cushion = 24000 * 0.00005 = 1.2 pts. Initial SL = 23980.0 - 1.2 = 23978.8.
    # Micro-wick to 23979.5 (0.5 pts below signal low 23980.0, but inside cushion):
    em.data_fetcher._spot = 23979.5
    em._manage_open_trade()
    assert len(em.exit_reasons) == 0, "Micro-wick inside first candle cushion prematurely stopped out!"

    # 2. Thin Candle (Option A) Test:
    # Entry candle forms as a very thin doji (Range = 2.0 pts < 3.6 pts hurdle [1.5 bps])
    thin_bar = OHLCBar(
        ts_start=t0.timestamp() + 60.0,
        ts_end=t0.timestamp() + 120.0,
        open=23995.0,
        high=23996.0,
        low=23994.0,  # Range = 2.0 pts
        close=23995.5,
    )
    em.aegis_engine.time_bar_engine.bars_1m.append(thin_bar)
    em.data_fetcher._spot = 23990.0
    em._manage_open_trade()
    # Structural SL should NOT trail to thin candle (which would be 23994.0 - 1.2 = 23992.8)!
    # If it had trailed, spot 23990.0 would have stopped out!
    assert len(em.exit_reasons) == 0, "Thin candle was improperly trailed!"

    # 3. Genuine Expansion Candle Test:
    # Next bar is a strong expansion candle (Range = 12.0 pts >= 3.6 pts)
    expansion_bar = OHLCBar(
        ts_start=t0.timestamp() + 120.0,
        ts_end=t0.timestamp() + 180.0,
        open=23995.0,
        high=24007.0,
        low=23995.0,
        close=24005.0,
    )
    em.aegis_engine.time_bar_engine.bars_1m.append(expansion_bar)
    em.data_fetcher._spot = 24000.0
    em._manage_open_trade()
    # Now structural SL trails to expansion_bar.low (23995.0) - cushion (~1.2) = 23993.8
    assert round(getattr(ctx, "trailing_spot_sl", 0.0), 1) == 23993.8

    # Spot dips below trailed structural SL (23993.0 <= 23993.8) -> Exits cleanly!
    em.data_fetcher._spot = 23993.0
    em._manage_open_trade()
    assert len(em.exit_reasons) == 1
    assert "Pillar 2 Entry Candle Low Broken SL" in em.exit_reasons[0]
    assert "23993.8" in em.exit_reasons[0]


def test_ratchet_only_rule_never_loosens_sl():
    """Verify Ratchet-Only Rule:
    If a candle has range >= hurdle and green close, but its low is lower than current SL,
    the stop loss MUST NEVER loosen (drop)."""
    from core.price_action_engine import OHLCBar
    em, ctx, leg = create_test_manager(entry_price=25.0, spot_entry=24000.0, signal_type="CALL")
    t0 = datetime.now() - timedelta(seconds=180)
    ctx.entry_time = t0 + timedelta(seconds=65)
    ctx.pillar = 1
    ctx.pa_setup_name = "20_ema_rejection"
    ctx.candle_low = 23990.0  # Signal candle low
    ctx.candle_high = 24005.0

    class MockTBEngine:
        def __init__(self):
            self.bars_1m = [
                # Signal Bar
                OHLCBar(ts_start=t0.timestamp(), ts_end=t0.timestamp() + 60.0,
                        open=23995.0, high=24005.0, low=23990.0, close=24002.0),
                # T+1 Entry Bar (High 24015.0, Low 23998.0, Close 24012.0) -> Solid bar (Range 17 pts)
                OHLCBar(ts_start=t0.timestamp() + 60.0, ts_end=t0.timestamp() + 120.0,
                        open=24003.0, high=24015.0, low=23998.0, close=24012.0),
            ]

    em.aegis_engine.time_bar_engine = MockTBEngine()

    # Step 1: Manage trade after T+1. Cushion = 24000 * 0.00005 = 1.2 pts.
    # Trailed SL becomes 23998.0 - 1.2 = 23996.8
    em.data_fetcher._spot = 24010.0
    em._manage_open_trade()
    assert round(getattr(ctx, "trailing_spot_sl", 0.0), 1) == 23996.8

    # Step 2: T+2 forms a wide candle (Range 8 pts >= 4.8 pts hurdle), but with a deeper wick/low (Low = 23994.0, Close = 24002.0).
    # Its candidate SL would be 23994.0 - 1.2 = 23992.8, which is LOWER than 23996.8!
    deep_bar = OHLCBar(
        ts_start=t0.timestamp() + 120.0,
        ts_end=t0.timestamp() + 180.0,
        open=24000.0,
        high=24008.0,
        low=23994.0,
        close=24002.0,
    )
    em.aegis_engine.time_bar_engine.bars_1m.append(deep_bar)
    em.data_fetcher._spot = 24005.0
    em._manage_open_trade()

    # Ratchet Guard: SL MUST stay at 23996.8 and NOT loosen to 23992.8!
    assert round(getattr(ctx, "trailing_spot_sl", 0.0), 1) == 23996.8, "SL improperly loosened/dropped on deeper low!"


def test_multi_bar_continuous_trailing_t1_t2_t3():
    """Verify that Bar-by-Bar Trailing SL runs continuously across every subsequent bar (T+1, T+2, T+3...)
    until the trade exits."""
    from core.price_action_engine import OHLCBar
    em, ctx, leg = create_test_manager(entry_price=30.0, spot_entry=24000.0, signal_type="CALL")
    t0 = datetime.now() - timedelta(seconds=240)
    ctx.entry_time = t0 + timedelta(seconds=65)
    ctx.pillar = 3
    ctx.pa_setup_name = "volatility_squeeze_breakout"
    ctx.candle_low = 23985.0
    ctx.candle_high = 24000.0

    class MockTBEngine:
        def __init__(self):
            self.bars_1m = [
                # T: Signal Bar
                OHLCBar(ts_start=t0.timestamp(), ts_end=t0.timestamp() + 60.0,
                        open=23990.0, high=24000.0, low=23985.0, close=23998.0),
                # T+1: Entry Bar (Low 23995.0, High 24010.0, Close 24008.0)
                OHLCBar(ts_start=t0.timestamp() + 60.0, ts_end=t0.timestamp() + 120.0,
                        open=23999.0, high=24010.0, low=23995.0, close=24008.0),
            ]

    em.aegis_engine.time_bar_engine = MockTBEngine()

    # 1. T+1 closed -> SL trails to 23995.0 - 1.2 = 23993.8
    em.data_fetcher._spot = 24007.0
    em._manage_open_trade()
    assert round(getattr(ctx, "trailing_spot_sl", 0.0), 1) == 23993.8

    # 2. T+2 closes (Low 24002.0, High 24018.0, Close 24015.0) -> SL trails to 24002.0 - 1.2 = 24000.8
    bar_t2 = OHLCBar(
        ts_start=t0.timestamp() + 120.0,
        ts_end=t0.timestamp() + 180.0,
        open=24008.0,
        high=24018.0,
        low=24002.0,
        close=24015.0,
    )
    em.aegis_engine.time_bar_engine.bars_1m.append(bar_t2)
    em.data_fetcher._spot = 24014.0
    em._manage_open_trade()
    assert round(getattr(ctx, "trailing_spot_sl", 0.0), 1) == 24000.8

    # 3. T+3 closes (Low 24010.0, High 24025.0, Close 24020.0) -> SL trails to 24010.0 - 1.2 = 24008.8
    bar_t3 = OHLCBar(
        ts_start=t0.timestamp() + 180.0,
        ts_end=t0.timestamp() + 240.0,
        open=24015.0,
        high=24025.0,
        low=24010.0,
        close=24020.0,
    )
    em.aegis_engine.time_bar_engine.bars_1m.append(bar_t3)
    em.data_fetcher._spot = 24019.0
    em._manage_open_trade()
    assert round(getattr(ctx, "trailing_spot_sl", 0.0), 1) == 24008.8

    # 4. T+4 breakdowns below T+3 low (Spot 24007.0 <= 24008.8) -> Exits at profit/cost-shield on structural breakdown!
    em.data_fetcher._spot = 24007.0
    em._manage_open_trade()
    assert len(em.exit_reasons) == 1
    assert "Pillar 3 Entry Candle Low Broken SL" in em.exit_reasons[0]
    assert "24008.8" in em.exit_reasons[0]


def test_friday_adaptive_target_profit_and_near_target_lock():
    """
    Verifies Friday 4 DTE adaptive 3.0% TP and Near-Target Profit Lock (+2.4% trigger, 0.5% tolerance).
    """
    # 1. Test get_adaptive_target_profit_pct() returns 0.030 on Friday, 0.040 on Mon-Thu
    dt_friday = datetime(2026, 10, 16, 13, 10, 0)  # weekday == 4 (Friday)
    dt_thursday = datetime(2026, 10, 15, 13, 10, 0)  # weekday == 3 (Thursday)
    assert get_adaptive_target_profit_pct(dt_friday) == 0.030
    assert get_adaptive_target_profit_pct(dt_thursday) == 0.040

    # 2. Test manager with Friday 3.0% TP setup
    em, context, leg = create_test_manager(entry_price=11.90, spot_entry=23115.0, signal_type="PUT")
    params = em.get_instrument_params()
    em._place_initial_take_profit(leg, params, target_pct=0.030)
    assert leg.initial_tp_pct == 0.030
    # Entry 11.90 * 1.03 = 12.257 -> floored tick 12.25 (+2.94% >= +2.85%)
    assert leg.tp_price == 12.25

    # 3. Near-Target Lock on Friday (+2.4% trigger, 0.5% pullback tolerance)
    # Price rises to 12.20 (gain: +0.30 / 11.90 = +2.52% >= +2.4%)
    em.broker.market_data_provider._quote = {"ltp": 12.20, "bp1": 12.20, "sp1": 12.25}
    em._manage_open_trade()
    assert len(em.exit_reasons) == 0  # Still open at peak

    # Now bid pulls back by 0.6% to 12.10 (cur gain: +1.68%, pullback = 2.52% - 1.68% = 0.84% >= 0.5%)
    em.broker.market_data_provider._quote = {"ltp": 12.10, "bp1": 12.10, "sp1": 12.15}
    em._manage_open_trade()
    assert len(em.exit_reasons) == 1
    assert "Near-Target Profit Lock" in em.exit_reasons[0]


def test_spot_mfe_and_mae_tick_tracking():
    """
    Verifies that Spot MFE and Spot MAE are accurately tracked in index points
    with millisecond float timestamps across ticks for both CALL and PUT trades.
    """
    # Test CALL trade: Entry spot = 23120.0
    em, context, leg = create_test_manager(entry_price=20.00, spot_entry=23120.0, signal_type="CALL")
    assert context.spot_peak_mfe_pts == 0.0
    assert context.spot_peak_mae_pts == 0.0

    # Tick 1: Spot rises to 23126.50 (+6.5 pts favorable)
    em.data_fetcher._spot = 23126.50
    em._manage_open_trade()
    assert round(context.spot_peak_mfe_pts, 2) == 6.50
    assert round(context.spot_at_mfe, 2) == 23126.50
    assert context.spot_time_to_mfe_sec >= 0.0

    # Tick 2: Spot dips to 23117.20 (-2.8 pts adverse against CALL entry)
    em.data_fetcher._spot = 23117.20
    em._manage_open_trade()
    assert round(context.spot_peak_mfe_pts, 2) == 6.50  # Peak MFE preserved
    assert round(context.spot_peak_mae_pts, 2) == -2.80  # MAE tracked
    assert round(context.spot_at_mae, 2) == 23117.20

    # Test PUT trade: Entry spot = 23120.0
    em_put, context_put, leg_put = create_test_manager(entry_price=20.00, spot_entry=23120.0, signal_type="PUT")

    # Tick 1: Spot drops to 23114.50 (+5.5 pts favorable for PUT)
    em_put.data_fetcher._spot = 23114.50
    em_put._manage_open_trade()
    assert round(context_put.spot_peak_mfe_pts, 2) == 5.50
    assert round(context_put.spot_at_mfe, 2) == 23114.50

    # Tick 2: Spot jumps to 23123.80 (-3.8 pts adverse against PUT entry)
    em_put.data_fetcher._spot = 23123.80
    em_put._manage_open_trade()
    assert round(context_put.spot_peak_mfe_pts, 2) == 5.50  # Peak MFE preserved
    assert round(context_put.spot_peak_mae_pts, 2) == -3.80  # MAE tracked
    assert round(context_put.spot_at_mae, 2) == 23123.80















def test_argon_last_mile_spot_hygiene_gate_abort_and_pass():
    """
    Verifies AegisExecutionManager Last-Mile Pre-Trade Spot Hygiene Gate:
    1. For PUT: If spot bounces above signal_bar_close + noise_buf, entry is aborted and slot rolled back.
    2. For CALL: If spot dumps below signal_bar_close - noise_buf, entry is aborted and slot rolled back.
    3. Aligned trades proceed normally without abort.
    """
    from unittest.mock import MagicMock, patch
    import queue
    from execution.execution_manager import AegisExecutionManager

    broker = MagicMock()
    data_fetcher = MagicMock()
    data_fetcher.get_active_instrument.return_value = ("SENSEX", "BFO")
    data_fetcher.get_weekly_option_tokens.return_value = [
        {"symbol": "SENSEX26OCT74300PE", "token": "1001", "strike": 74300.0, "type": "PE", "side": 1}
    ]
    data_fetcher.get_index_ltp.return_value = 74380.0

    aegis_engine = MagicMock()
    aegis_engine.last_spot = 74380.0
    aegis_engine.get_spot_drift.return_value = 15.0  # +15 pts positive 3s velocity
    aegis_engine.slot_policy = MagicMock()

    sig_q = queue.Queue()
    em = AegisExecutionManager(broker, data_fetcher, aegis_engine, sig_q)
    em.trade_context = None
    em.trades_today = 0
    em._consec_losses = 0
    em.daily_pnl = 0.0
    em.has_open_trade = MagicMock(return_value=False)
    em.can_enter = MagicMock(return_value=True)
    em.capital_engine = MagicMock()
    em.capital_engine.get_available_funds.return_value = 5000.0

    with patch("execution.execution_manager.StrikeSelector.select_strikes") as mock_select:
        mock_select.return_value = [
            ({"symbol": "SENSEX26OCT74300PE", "token": "1001", "strike": 74300.0}, 1, 30.0)
        ]
        # Simulate Trade #2 PUT signal with signal_bar_close = 74358.87
        put_signal = {
            "type": "GAMMA_BURST_ENTRY",
            "signal": "DOWN",
            "conviction": 85.0,
            "spot": 74358.87,
            "sym": "SENSEX",
            "metrics": {
                "pa_setup_name": "topological_prominence_breakout",
                "bar_close": 74358.87,
                "bar_high": 74415.75,
                "bar_low": 74353.36,
                "atr_1m": 52.0,
            },
        }
        attach_scored_contract(em, put_signal, token="1001", strike=74300.0)

        # Case 1: Adverse bounce (live_spot = 74380.0 > 74358.87 + 11.15 noise_buf) -> ABORTED!
        em._process_aegis_signal(put_signal)
        assert em.trade_context is None
        aegis_engine.slot_policy.rollback_admission.assert_called_once()

        # Case 2: Aligned momentum (live_spot = 74355.0 <= 74358.87, delta_3s = -5.0) -> PROCEEDS!
        aegis_engine.slot_policy.rollback_admission.reset_mock()
        aegis_engine.last_spot = 74355.0
        aegis_engine.get_spot_drift.return_value = -5.0
        em.order_lifecycle = MagicMock()
        em.order_lifecycle.execute_entry.return_value = True

        em._process_aegis_signal(put_signal)
        assert em.trade_context is not None
        assert em.trade_context.signal_type == "PUT"
        em.order_lifecycle.execute_entry.assert_called_once()
        aegis_engine.slot_policy.rollback_admission.assert_not_called()


def test_execution_uses_the_scored_contract_without_strike_reselection():
    """
    Execution must use the exact contract scored upstream, not choose a new strike.
    """
    from unittest.mock import MagicMock, patch
    em, ctx, leg = create_test_manager(entry_price=100.0, spot_entry=73400.0, signal_type="CALL")
    em.trade_context = None
    em.trades_today = 0
    em.can_enter = lambda now: True

    t50_epoch = 1700000050.0
    signal = {
        "type": "GAMMA_BURST_ENTRY",
        "signal": "UP",
        "conviction": 85.0,
        "spot": 73405.0,  # moved only 5 pts <= 40 pts
        "timestamp": t50_epoch + 5.0,
        "sym": "SENSEX73400CE",
        "lp": 28.5,
        "metrics": {"atr_1m": 45.0, "bar_close": 73405.0, "pa_setup_name": "volatility_squeeze_breakout"},
    }
    attach_scored_contract(em, signal, token="2001", strike=73400.0)

    with patch("execution.strike_selector.StrikeSelector.select_strikes") as mock_select:
        em.order_lifecycle = MagicMock()
        em.order_lifecycle.execute_entry.return_value = True
        with patch("time.time", return_value=t50_epoch + 5.0):
            em._process_aegis_signal(signal)

        # A new strike must never be selected after scoring.
        mock_select.assert_not_called()
        assert em.trade_context is not None
        assert em.trade_context.signal_type == "CALL"
        assert em.trade_context.legs[0].token == "2001"
        assert em.trade_context.legs[0].symbol == "SENSEX73400CE"


def test_execution_rejects_contract_identity_mismatch():
    em, _, _ = create_test_manager(entry_price=100.0, spot_entry=24000.0, signal_type="CALL")
    em.trade_context = None
    em.can_enter = lambda now: True
    signal = {
        "type": "GAMMA_BURST_ENTRY",
        "signal": "UP",
        "conviction": 85.0,
        "lp": 100.0,
        "spot": 24000.0,
        "sym": "NIFTY24000CE",
        "contract_token": "9001",
        "timestamp": time.time(),
        "metrics": {"strike": 24000.0, "side": "CE"},
    }
    em.aegis_engine.token_map = {"9001": {"side": 0, "strike": 24000.0}}
    em.aegis_engine.t2s = {"9001": "NIFTY24050CE"}
    em._option_cache = {"9001": 100.0}

    em._process_aegis_signal(signal)

    assert em.trade_context is None


def test_premarket_bridge_only_shifts_atm_and_stops_at_open():
    from unittest.mock import MagicMock, patch
    from execution import execution_manager as execution_manager_module

    em, _, _ = create_test_manager()
    em.aegis_engine.shift_atm = MagicMock()
    em.aegis_engine._on_underlying_tick = MagicMock()
    preopen = datetime(2026, 10, 30, 9, 14, 0)

    with patch.object(execution_manager_module, "read_premarket_state", return_value={
        "indicative_spot": 23726.0,
        "gap_pct": 0.2,
    }) as read_state:
        with patch.object(execution_manager_module, "datetime") as mock_datetime:
            mock_datetime.now.return_value = preopen
            em._handle_premarket_bridge()
            em.aegis_engine.shift_atm.assert_called_once_with(0, new_atm=23750)
            em.aegis_engine._on_underlying_tick.assert_not_called()

            mock_datetime.now.return_value = datetime(2026, 10, 30, 9, 15, 0)
            em._handle_premarket_bridge()
            assert read_state.call_count == 1


def test_current_spot_prefers_the_engine_feed_used_for_entry():
    em, _, _ = create_test_manager(spot_entry=23700.0)
    em.aegis_engine.last_spot = 23725.0
    em.aegis_engine._last_spot_ts = time.time()
    em.data_fetcher._spot = 23690.0

    assert em._get_current_spot() == 23725.0


def test_last_mile_in_flight_continuation_and_drawdown_gate():
    """
    UPGRADE 2: Last-Mile Pre-Trade Spot & In-Flight Continuation Gate.
    Verifies:
    1. Peak Drawdown Abortion: New candle spiked up then dumped > 0.18 * ATR from peak.
    2. Opposite Bar Color Abortion: New candle trades below open - 0.10 * ATR.
    3. In-Flight Delta Reversal: Order flow flipped to > 1.25x opposing volume.
    """
    from unittest.mock import MagicMock
    from core.price_action_engine import OHLCBar
    em, ctx, leg = create_test_manager(entry_price=100.0, spot_entry=73400.0, signal_type="CALL")
    em.trade_context = None
    em.trades_today = 0
    em.can_enter = lambda now: True

    aegis_engine = MagicMock()
    aegis_engine.last_spot = 73403.0  # Dumped to 73403
    aegis_engine.get_spot_drift.return_value = -3.0
    aegis_engine.slot_policy = MagicMock()
    em.aegis_engine = aegis_engine

    call_signal = {
        "type": "GAMMA_BURST_ENTRY",
        "signal": "UP",
        "conviction": 85.0,
        "spot": 73410.0,
        "lp": 28.5,
        "sym": "SENSEX74400CE",
        "timestamp": time.time() - 0.5,
        "metrics": {
            "atr_1m": 40.0,
            "bar_close": 73410.0,
            "bar_high": 73415.0,
            "bar_low": 73385.0,
            "pa_setup_name": "volatility_squeeze_breakout",
        },
    }
    attach_scored_contract(em, call_signal, token="2001", strike=74400.0)

    # 1. Peak Drawdown Abortion:
    # New bar opened at 73410, spiked to 73425, then dumped to 73403.
    # Peak = 73425, live_spot = 73403. Drawdown = 22 pts > max_peak_drawdown (0.18 * 40 = 7.2 pts)!
    in_flight_bar = OHLCBar(0, 60, 73410.0, 73425.0, 73400.0, 73403.0, volume=50.0)
    tb_engine = MagicMock()
    tb_engine.current_bar = in_flight_bar
    tb_engine.current_atr_1m = 40.0
    aegis_engine.time_bar_engine = tb_engine
    aegis_engine.spot_history = [
        (call_signal["timestamp"], 73410.0),
        (call_signal["timestamp"] + 0.2, 73425.0),
    ]

    import pandas as pd
    weekly_df = pd.DataFrame([{"strike": 74400.0, "option_type": "CE", "token": "2001", "symbol": "SENSEX74400CE"}])
    em.data_fetcher.get_active_instrument = lambda: ("SENSEX", "SENSEX")
    em.data_fetcher.get_weekly_option_tokens = lambda: weekly_df
    em._option_cache = {"2001": 28.5}

    em._process_aegis_signal(call_signal)
    assert em.trade_context is None
    aegis_engine.slot_policy.rollback_admission.assert_called_once()

    # 2. In-flight Delta Reversal Abortion:
    # Spot is good, but order flow has 2500 PutBuy vs 500 CallBuy (> 1.25x opposing)
    aegis_engine.slot_policy.rollback_admission.reset_mock()
    aegis_engine.last_spot = 73420.0  # Spot fine
    in_flight_bar.high = 73422.0
    in_flight_bar.low = 73410.0
    in_flight_bar.close = 73420.0
    in_flight_bar.open = 73412.0  # Green bar

    pa_engine = MagicMock()
    pa_engine.current_bar = in_flight_bar
    pa_engine.get_in_flight_opt_flow.return_value = {
        "bullish_vol": 500.0,
        "bearish_vol": 2500.0,
        "total_vol": 3000.0,
    }
    aegis_engine.price_action_engine = pa_engine

    em._process_aegis_signal(call_signal)
    assert em.trade_context is None
    aegis_engine.slot_policy.rollback_admission.assert_called_once()



def test_friday_4dte_low_capital_guard():
    """
    UPGRADE: Smart Friday 4-DTE Low-Capital Guard.
    Verifies that on Friday (4 DTE):
    1. If available funds < FRIDAY_MIN_CAPITAL_RS (₹3000), live entry is skipped,
       slot is rolled back, and queue is drained.
    2. If AEGIS_FRIDAY_TRADING_ENABLED == '0', live entry is skipped regardless of capital.
    3. AegisExecutionManager.can_enter respects Friday rules and allows Mon-Thu.
    """
    from unittest.mock import MagicMock, patch
    import os
    import queue
    from execution.execution_manager import AegisExecutionManager

    em, ctx, leg = create_test_manager(entry_price=8.15, spot_entry=22472.0, signal_type="PUT")
    em.trade_context = None
    em.trades_today = 0
    em.signal_queue = queue.Queue()
    em.aegis_engine.slot_policy = MagicMock()
    em.capital_engine = MagicMock()

    # Create dummy signal
    signal = {
        "type": "GAMMA_BURST_ENTRY",
        "signal": "DOWN",
        "conviction": 85.0,
        "lp": 8.15,
        "sym": "NIFTY21700PE",
        "spot": 22472.0,
        "timestamp": time.time(),
        "metrics": {"pa_setup_name": "topological_prominence_breakout", "sizing_tier": "STANDARD_SIZE"},
    }

    friday_noon = datetime(2026, 10, 23, 11, 28, 46)  # Friday (weekday == 4)
    monday_noon = datetime(2026, 10, 26, 11, 28, 46)  # Monday (weekday == 0)

    # 1. can_enter tests
    with patch.dict(os.environ, {"AEGIS_FRIDAY_TRADING_ENABLED": "0"}):
        assert AegisExecutionManager.can_enter(friday_noon, available_funds=50000.0) is False
        assert AegisExecutionManager.can_enter(monday_noon, available_funds=1000.0) is True

    with patch.dict(os.environ, {"AEGIS_FRIDAY_TRADING_ENABLED": "1"}):
        assert AegisExecutionManager.can_enter(friday_noon, available_funds=1500.0) is False
        assert AegisExecutionManager.can_enter(friday_noon, available_funds=3500.0) is True

    # 2. _process_aegis_signal: Low capital on Friday blocks execution
    em.capital_engine.get_available_funds.return_value = 1200.0  # < ₹3,000
    with patch.dict(os.environ, {"AEGIS_FRIDAY_TRADING_ENABLED": "1"}):
        with patch("execution.execution_manager.datetime") as mock_dt:
            mock_dt.now.return_value = friday_noon
            em._process_aegis_signal(signal)

            assert em.trade_context is None
            em.aegis_engine.slot_policy.rollback_admission.assert_called_once()

    # 3. _process_aegis_signal: Trading disabled on Friday blocks execution even with high funds
    em.aegis_engine.slot_policy.rollback_admission.reset_mock()
    em.capital_engine.get_available_funds.return_value = 50000.0
    with patch.dict(os.environ, {"AEGIS_FRIDAY_TRADING_ENABLED": "0"}):
        with patch("execution.execution_manager.datetime") as mock_dt:
            mock_dt.now.return_value = friday_noon
            em._process_aegis_signal(signal)

            assert em.trade_context is None
            em.aegis_engine.slot_policy.rollback_admission.assert_called_once()


def test_catastrophic_disaster_exit_zero_sleep_fast_path():
    """Phase 1: Verify catastrophic exit (DISASTER_SL / PREMIUM HARD SL) executes sub-150ms zero-lag sweep."""
    em, ctx, leg = create_test_manager(entry_price=100.0, spot_entry=24000.0, signal_type="CALL")
    ctx.entry_time = datetime.now() - timedelta(seconds=30)
    em.tp_order_id = "240925000777"
    em.broker.market_data_provider._quote = {"ltp": 91.0, "bp1": 91.0, "sp1": 91.10, "sq1": 500, "bq1": 500}

    modified_orders = []
    placed_orders = []

    class FastMockShoonyaAPI:
        def __init__(self):
            self.modified = False

        def modify_order(self, **kwargs):
            modified_orders.append(kwargs)
            self.modified = True
            return {"stat": "Ok", "result": kwargs.get("orderno")}

        def single_order_history(self, orderno):
            if self.modified:
                return [{"status": "COMPLETE", "flprc": "91.00"}]
            return [{"status": "OPEN", "flprc": "0.00"}]

        def cancel_order(self, orderno):
            return {"stat": "Ok"}

        def place_order(self, **kwargs):
            placed_orders.append(kwargs)
            return {"stat": "Ok", "norenordno": "FAST_999"}

    em.broker._api = FastMockShoonyaAPI()
    em.get_instrument_params = lambda: {"exchange": "NFO", "lot_size": 25}
    em._append_history = lambda r, p: None

    t0 = time.time()
    em._do_chase_exit("Premium Hard SL (-8.0% disaster floor: -9.0% @ 91.00)")
    elapsed_ms = (time.time() - t0) * 1000.0

    # Catastrophic exit must complete in < 250ms (bypassing 2.6s sleeps)
    assert elapsed_ms < 250.0, f"Catastrophic exit took {elapsed_ms:.1f}ms, expected < 250ms"
    assert leg.status == "CLOSED"
    assert len(modified_orders) == 1
    assert modified_orders[0]["orderno"] == "240925000777"


def test_mode_b_universal_candle_color_shield_abort_and_drain():
    """Phase 1: Verify Mode B P1/P2 signals strictly abort if forming candle is opposite color."""
    import queue
    from unittest.mock import MagicMock
    from core.price_action_engine import OHLCBar
    em, ctx, leg = create_test_manager(entry_price=100.0, spot_entry=74000.0, signal_type="CALL")
    em.trade_context = None
    em.trades_today = 0
    em.can_enter = lambda now: True

    aegis_engine = MagicMock()
    aegis_engine.last_spot = 73995.0  # Dropped 5 pts below open (74000)
    aegis_engine.get_spot_drift.return_value = 0.0
    aegis_engine.slot_policy = MagicMock()
    em.aegis_engine = aegis_engine

    # Put a stale signal in the queue to verify queue drain on abort
    em.signal_queue = queue.Queue()
    em.signal_queue.put({"type": "STALE_SIGNAL"})

    # Mode B Pillar 1 CALL signal
    mode_b_p1_signal = {
        "type": "GAMMA_BURST_ENTRY",
        "signal": "UP",
        "conviction": 88.0,
        "spot": 74000.0,
        "lp": 50.0,
        "sym": "SENSEX74000CE",
        "timestamp": time.time() - 0.2,
        "metrics": {
            "atr_1m": 40.0,
            "bar_close": 74000.0,
            "bar_high": 74010.0,
            "bar_low": 73990.0,
            "mode": "MODE_B_MID_BAR_BREAKOUT",
            "pillar": 1,  # Pillar 1 in Mode B previously bypassed color check!
            "pa_setup_name": "momentum_breakout",
        },
    }
    attach_scored_contract(em, mode_b_p1_signal, token="3001", strike=74000.0)

    # Forming bar: open at 74000, current spot at 73995 (< open - 0.05 * 40 = 73998)
    in_flight_bar = OHLCBar(0, 60, 74000.0, 74005.0, 73994.0, 73995.0, volume=100.0)
    tb_engine = MagicMock()
    tb_engine.current_bar = in_flight_bar
    tb_engine.current_atr_1m = 40.0
    aegis_engine.time_bar_engine = tb_engine
    aegis_engine.spot_history = [(mode_b_p1_signal["timestamp"], 74000.0)]

    import pandas as pd
    weekly_df = pd.DataFrame([{"strike": 74000.0, "option_type": "CE", "token": "3001", "symbol": "SENSEX74000CE"}])
    em.data_fetcher.get_active_instrument = lambda: ("SENSEX", "SENSEX")
    em.data_fetcher.get_weekly_option_tokens = lambda: weekly_df
    em._option_cache = {"3001": 50.0}

    em._process_aegis_signal(mode_b_p1_signal)

    # Must abort entry, rollback admission, and drain stale signal queue!
    assert em.trade_context is None
    aegis_engine.slot_policy.rollback_admission.assert_called_once()
    assert em.signal_queue.empty(), "Signal queue must be drained on last-mile color abort"

