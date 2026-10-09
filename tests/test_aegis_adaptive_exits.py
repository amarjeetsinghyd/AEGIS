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

from common.execution.trade_context import TradeContext, TradeLeg
from AEGIS.aegis_execution_manager import (
    AegisExecutionManager,
    DEFAULT_MIN_NOISE_FLOOR_RS,
    NOISE_FLOOR_SPREAD_MULT,
    NOISE_FLOOR_SIGMA_MULT,
    BREAKEVEN_NOISE_MULT,
    BREAKEVEN_TIME_SEC,
    BREAKEVEN_LOCK_BUFFER_RS,
    RUNNER_ARM_NOISE_MULT,
    RUNNER_TRAIL_RATIO,
    RESTING_TP_LIMIT_PCT,
    DEFAULT_SPOT_SL_BPS,
    BREAKEVEN_SPOT_CONFIRM_BPS,
    DELTA_MIN_SPOT_BPS,
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
    def __init__(self):
        self.option_cache = {}
        self.time_bar_engine = None


def create_test_manager(entry_price=19.30, spot_entry=23700.0, signal_type="PUT"):
    broker = DummyBroker(ltp=entry_price, bp1=entry_price - 0.10, sp1=entry_price + 0.10)
    data_fetcher = DummyDataFetcher(spot=spot_entry)
    engine = DummyEngine()
    
    # Instantiate manager without starting background loop
    em = AegisExecutionManager.__new__(AegisExecutionManager)
    em.broker = broker
    em.data_fetcher = data_fetcher
    em.aegis_engine = engine
    em.tp_order_id = None
    import threading
    em.trade_lock = threading.Lock()
    em._trade_quote_history = deque(maxlen=60)
    em._option_cache = {}
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
    leg.runner_locked = False
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
    V4.0 (2026-09-23 update): Entry @ ₹19.30. The tick-cadence Premium Hard SL
    is relaxed to -12.0% disaster floor. Shallow noise dips inside the floor
    (e.g. 18.70 [-3.1%], 18.20 [-5.7%]) must still stay OPEN to breathe.
    Catastrophic drops beyond -12.0% (e.g. 16.80 [-12.9%]) fire immediately.
    """
    em, context, leg = create_test_manager(entry_price=19.30, spot_entry=23700.0, signal_type="PUT")

    # Shallow dip inside the floor: spot intact, premium -3.1% -> stays OPEN
    em.data_fetcher._spot = 23702.0
    em.broker.market_data_provider._quote = {"ltp": 18.70, "bp1": 18.70, "sp1": 18.85}
    em._manage_open_trade()
    assert len(em.exit_reasons) == 0, f"Premature exit occurred: {em.exit_reasons}"
    assert leg.status == "OPEN"

    # Spread/noise dip at -5.7% (18.20): stays OPEN under 12% disaster floor
    em.broker.market_data_provider._quote = {"ltp": 18.20, "bp1": 18.20, "sp1": 18.35}
    em._manage_open_trade()
    assert len(em.exit_reasons) == 0, f"Premature exit occurred: {em.exit_reasons}"
    assert leg.status == "OPEN"

    # Deep crash through the 12% disaster floor: premium -12.95% (16.80) -> Premium Hard SL fires
    em.broker.market_data_provider._quote = {"ltp": 16.80, "bp1": 16.80, "sp1": 16.90}
    em._manage_open_trade()
    assert len(em.exit_reasons) == 1
    assert "Premium Hard SL" in em.exit_reasons[0]


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


def test_dynamic_runner_trailing_and_ema_slope_exit():
    """V4.0 (2026-09-21): runner trailing is disabled (single-target mode) —
    peak gain must NOT arm the runner; 9-EMA slope reversal still exits."""
    em, context, leg = create_test_manager(entry_price=13.00, spot_entry=23700.0, signal_type="PUT")

    # Peak reaches 20.00 (+53.8% gain)
    em.broker.market_data_provider._quote = {"ltp": 20.00, "bp1": 19.95, "sp1": 20.05}
    em._manage_open_trade()

    # Runner must NOT be armed in V4 single-target mode!
    assert not getattr(leg, "runner_locked", False)
    assert leg.highest_price == 20.00
    
    # Configure mock TimeBarEngine with bullish reversal for PUT trade
    class DummyTBEngine:
        def get_live_ema_and_slope(self):
            # Spot crossed above 9-EMA with positive slope (+2.5 bps)
            return 23680.0, 2.5, 23680.0, 2.0
        def get_market_structure(self, direction):
            return "ALIGNED"
            
    em.aegis_engine.time_bar_engine = DummyTBEngine()
    em.data_fetcher._spot = 23685.0  # Spot (23685) > 9-EMA (23680)
    
    # When price is still profitable (19.00), EMA slope flip triggers exit
    em.broker.market_data_provider._quote = {"ltp": 19.00, "bp1": 18.95, "sp1": 19.05}
    em._manage_open_trade()
    
    assert len(em.exit_reasons) == 1
    assert "Trend Reversal Exit" in em.exit_reasons[0]
    assert "crossed above 9-EMA" in em.exit_reasons[0]


def test_unconditional_ema_reversal_exit_on_negative_pnl():
    """
    Trade Reproduction (09:46 IST) under V4.0 (2026-09-21):
    PUT entry at ₹17.40, peak was only ₹18.10 (+4.0%, runner NEVER locked).
    Option dips to ₹17.00 (-2.3% loss, inside the -3.5% premium floor).
    Spot crosses back above 9-EMA with positive slope.
    It MUST exit via Trend Reversal immediately without waiting for timeout!
    (Deeper dips through -3.5% exit via Premium Hard SL first by design.)
    """
    em, context, leg = create_test_manager(entry_price=17.40, spot_entry=23670.0, signal_type="PUT")
    leg.highest_price = 18.10
    leg.runner_locked = False
    leg.breakeven_locked = False

    class DummyTBEngine:
        def get_live_ema_and_slope(self):
            return 23675.0, 1.8, 23675.0, 1.2
        def get_market_structure(self, direction):
            return "ALIGNED"

    em.aegis_engine.time_bar_engine = DummyTBEngine()
    em.data_fetcher._spot = 23681.0  # Spot (23681) crossed above 9-EMA (23675)

    # Option is in shallow loss at ₹17.00 (-2.3%, above the premium floor)
    em.broker.market_data_provider._quote = {"ltp": 17.00, "bp1": 16.95, "sp1": 17.05}
    em._manage_open_trade()

    assert len(em.exit_reasons) == 1
    assert "Trend Reversal Exit" in em.exit_reasons[0]
    assert "crossed above 9-EMA" in em.exit_reasons[0]


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

    with patch("AEGIS.aegis_execution_manager.StrikeSelector.select_strikes") as mock_sel:
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
    from AEGIS.aegis_gamma_engine import AegisGammaEngine
    engine = AegisGammaEngine(
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
    from AEGIS.backtest_engine import SimulatedPositionManager
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
    from AEGIS.backtest_engine import SimulatedPositionManager
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
    from AEGIS.backtest_engine import SimulatedPositionManager
    pos_mgr = SimulatedPositionManager()
    pos_mgr.update_spot(24000.0)

    assert pos_mgr.on_signal(_make_backtest_squeeze_signal(), current_ts_sec=1000.0) is True
    pos_mgr.on_tick("NIFTY26SEP24500CE", 104.5, current_ts_sec=1010.0)
    assert pos_mgr.active_trade is None
    assert "TARGET_HIT (+4.0%)" in pos_mgr.completed_trades[-1].exit_reason


def test_backtest_adaptive_six_min_extension():
    """Backtest time ceiling: flat trade exits at 5m, running winner extends to 6m (live parity)."""
    from AEGIS.backtest_engine import SimulatedPositionManager

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

    # Spot breaks below candle low (72390 <= 72392.5) -> structural exit triggers
    em.data_fetcher._spot = 72390.0
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

    # Spot breaks above candle high (72422 >= 72420.0) -> structural exit triggers
    em2.data_fetcher._spot = 72422.0
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
    
    # Mock time bar engine with 50 EMA opposed (Spot 24000 < 50-EMA 24050 for CALL)
    class MockTBEngine:
        def get_market_structure(self, sig):
            return "NEUTRAL"
        def get_live_50_ema_and_slope(self):
            return (24050.0, -1.0)
        def get_live_ema_and_slope(self):
            return (24000.0, 0.0, 0.0, 0.0)
    em.aegis_engine.time_bar_engine = MockTBEngine()
    
    em._manage_open_trade(now=sim_now)
    assert len(em.exit_reasons) == 1
    assert "0DTE_STAGNATION_TRAP_EXIT" in em.exit_reasons[0]


def test_pillar3_12pct_disaster_sl_allows_small_noise():
    """Verify that a -3.5% noise dip does NOT trigger SL, but >= -12% disaster drop does."""
    em, ctx, leg = create_test_manager(entry_price=25.20, spot_entry=74884.0, signal_type="CALL")
    ctx.entry_time = datetime.now() - timedelta(seconds=20)
    
    # Tick 1: Dip by 88 paise to 24.32 (-3.49% to -3.57%) - normal spread noise
    em.broker.market_data_provider._quote = {"ltp": 24.30, "bp1": 24.30, "sp1": 24.50, "sq1": 500, "bq1": 500}
    em._manage_open_trade()
    # Trade MUST remain OPEN under the -12% disaster floor
    assert len(em.exit_reasons) == 0
    assert leg.status == "OPEN"

    # Tick 2: Severe drop to 22.00 (-12.7%) -> MUST trigger disaster circuit breaker!
    em.broker.market_data_provider._quote = {"ltp": 22.00, "bp1": 22.00, "sp1": 22.10, "sq1": 500, "bq1": 500}
    em._manage_open_trade()
    assert len(em.exit_reasons) == 1
    assert "disaster floor" in em.exit_reasons[0]


def test_pillar2_entry_candle_invalidation_and_no_9ema_choke():
    """Verify that Pillar 2 (Micro-Sweep & Reclaim) is NOT choked by 9-EMA bar close,
    and exits ONLY when spot breaks the Entry Candle Low (CALL) / High (PUT)."""
    from AEGIS.core.time_bar_engine import OHLCBar
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
        def get_live_ema_and_slope(self):
            return (74875.0, 0.0, 74870.0, 0.0)
        def get_live_50_ema_and_slope(self):
            return (74850.0, 0.0)

    em.aegis_engine.time_bar_engine = MockTBEngineWith9EMA()
    em.data_fetcher._spot = 74870.0  # Above candle low (74870.0 > 74865.0)
    em.broker.market_data_provider._quote = {"ltp": 24.80, "bp1": 24.80, "sp1": 24.90, "sq1": 500, "bq1": 500}

    # Step 1: Manage open trade. Even though 1m bar closed below 9-EMA, P2 MUST NOT exit!
    em._manage_open_trade()
    assert len(em.exit_reasons) == 0, "Pillar 2 was prematurely choked by 9-EMA!"

    # Step 2: Spot breaks below Entry Candle Low (74864.0 <= 74865.0) -> Pure PA SL triggers!
    em.data_fetcher._spot = 74864.0
    em._manage_open_trade()
    assert len(em.exit_reasons) == 1
    assert "Pillar 2 Entry Candle Low Broken SL" in em.exit_reasons[0]


def test_pillar1_entry_candle_invalidation_and_no_9ema_choke():
    """Verify that Pillar 1 (20 EMA Pullback) is NOT choked by 9-EMA bar close,
    and exits ONLY when spot breaks the Entry Candle Low (CALL) / High (PUT)."""
    from AEGIS.core.time_bar_engine import OHLCBar
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
        def get_live_ema_and_slope(self):
            return (74875.0, 0.0, 74870.0, 0.0)
        def get_live_50_ema_and_slope(self):
            return (74850.0, 0.0)

    em.aegis_engine.time_bar_engine = MockTBEngineWith9EMA()
    em.data_fetcher._spot = 74870.0  # Above candle low (74870.0 > 74865.0)
    em.broker.market_data_provider._quote = {"ltp": 24.80, "bp1": 24.80, "sp1": 24.90, "sq1": 500, "bq1": 500}

    # Step 1: Manage open trade. Bar closed below 9-EMA, but P1 MUST NOT exit!
    em._manage_open_trade()
    assert len(em.exit_reasons) == 0, "Pillar 1 was prematurely choked by 9-EMA!"

    # Step 2: Spot breaks below Entry Candle Low (74864.0 <= 74865.0) -> Pure PA SL triggers!
    em.data_fetcher._spot = 74864.0
    em._manage_open_trade()
    assert len(em.exit_reasons) == 1
    assert "Pillar 1 Entry Candle Low Broken SL" in em.exit_reasons[0]


def test_pillar3_entry_candle_invalidation_and_no_9ema_choke():
    """Verify that Pillar 3 (Volatility Squeeze Breakout) is NOT choked by 9-EMA bar close,
    and exits ONLY when spot breaks the Entry Candle Low (CALL) / High (PUT)."""
    from AEGIS.core.time_bar_engine import OHLCBar
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
        def get_live_ema_and_slope(self):
            return (74875.0, 0.0, 74870.0, 0.0)
        def get_live_50_ema_and_slope(self):
            return (74850.0, 0.0)

    em.aegis_engine.time_bar_engine = MockTBEngineWith9EMA()
    em.data_fetcher._spot = 74870.0  # Above candle low (74870.0 > 74865.0)
    em.broker.market_data_provider._quote = {"ltp": 24.80, "bp1": 24.80, "sp1": 24.90, "sq1": 500, "bq1": 500}

    # Step 1: Manage open trade. Bar closed below 9-EMA, but P3 MUST NOT exit!
    em._manage_open_trade()
    assert len(em.exit_reasons) == 0, "Pillar 3 was prematurely choked by 9-EMA!"

    # Step 2: Spot breaks below Entry Candle Low (74864.0 <= 74865.0) -> Pure PA SL triggers!
    em.data_fetcher._spot = 74864.0
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










