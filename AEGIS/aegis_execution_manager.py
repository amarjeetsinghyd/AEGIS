# aegis_execution_manager.py
# AEGIS V2.0 — Execution Manager for Gamma Burst Engine
# Parses GAMMA_BURST_ENTRY signals, manages positions, feeds fill latency to AEGIS core

from __future__ import annotations

import json
import os
import queue
import threading
import time
from collections import deque
from datetime import datetime, timedelta, time as dt_time
from typing import Optional, Dict, Any, List

import numpy as np

from common.logger import get_logger
from common.config import (
    get_data_dir,
    ENABLE_LIVE_BROKERAGE_EXECUTION,
    NIFTY_LOT_SIZE,
    SENSEX_LOT_SIZE,
    NIFTY_PREMIUM_MIN,
    NIFTY_PREMIUM_MAX,
    SENSEX_PREMIUM_MIN,
    SENSEX_PREMIUM_MAX,
    OPTION_TICK_SIZE,
)
from common.execution.capital_engine import CapitalEngine
from common.execution.order_lifecycle import (
    PaperOrderLifecycle,
    ShoonyaOrderLifecycle,
    get_marketable_exit_limit_price,
)
from common.execution.strike_selector import StrikeSelector
from common.execution.trade_context import TradeContext, TradeLeg
from common.market.market_calendar import is_trading_day
from common.ipc.premarket_bridge import read_premarket_state

logger = get_logger("aegis_execution_manager")

# ─── AEGIS Risk Constants ───
MAX_HOLD_SEC = 300.0                    # 5-minute baseline hard ceiling
MAX_HOLD_EXTENDED_SEC = 360.0           # 6-minute conditional extension for running winners
HOLD_EXTEND_MIN_PROFIT_PCT = 0.020      # +2.0% minimum gain required to qualify for 6-min hold
def _parse_time_env(env_name: str, default_h: int, default_m: int, default_s: int = 0) -> dt_time:
    raw = os.getenv(env_name, "").strip()
    if not raw:
        return dt_time(default_h, default_m, default_s)
    try:
        parts = [int(p) for p in raw.split(":")]
        return dt_time(*parts)
    except Exception:
        return dt_time(default_h, default_m, default_s)

AEGIS_ENTRY_CUTOFF = _parse_time_env("AEGIS_ENTRY_CUTOFF", 15, 0, 0)   # No new entries after 15:00 (>=8 min before 15:08 flatten)
AEGIS_FORCE_FLATTEN = _parse_time_env("AEGIS_FORCE_FLATTEN", 15, 8, 0) # Force flatten at 15:08 (CAS / broker square-off shield)
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "3")) # Hard safety cap: max 3 trades per day

# ─── Adaptive Quantitative Exit Constants (Claude's 4-Pillar Framework) ───
DEFAULT_MIN_NOISE_FLOOR_RS = 1.50       # Minimum rupee noise floor (₹1.50 = 30 ticks of 5p)
NOISE_FLOOR_SPREAD_MULT = 2.5           # k1: 2.5x bid-ask spread
NOISE_FLOOR_SIGMA_MULT = 2.0            # k2: 2.0x rolling 30-45s premium stdev
NOISE_FLOOR_MIN_PCT = 0.05              # 5% of option entry price as proportional noise hurdle
BREAKEVEN_NOISE_MULT = 2.5              # Move must exceed 2.5x noise floor to arm Breakeven
BREAKEVEN_TIME_SEC = 60.0               # Minimum hold time (60s) or spot confirmation for BE
BREAKEVEN_LOCK_BUFFER_RS = 0.50         # Minimum green lock buffer (₹0.50)
RUNNER_ARM_NOISE_MULT = 3.0             # Arm Runner when peak gain >= 3.0x noise floor (or +15%)
RUNNER_TRAIL_RATIO = 0.20               # Dynamic trail 20% of peak gain
EMERGENCY_MAX_SL_PCT = 0.35             # Emergency hard cap: max -35% SL
RESTING_TP_LIMIT_PCT = 0.040            # Fast 4.0% Scalp Take-Profit (Maker Limit order)

# ─── Scale-Invariant Normalized Metrics (Basis Points & ATR Multiples) ───
DEFAULT_SPOT_SL_BPS = 6.5               # 6.5 bps of spot (0.065% ~15.5 pts NIFTY, ~52 pts SENSEX)
SPOT_SL_ATR_MULT = 1.0                  # 1.0x 1-minute ATR (dynamic regime expansion)
BREAKEVEN_SPOT_CONFIRM_BPS = 3.5        # 3.5 bps of spot (~8.3 pts NIFTY, ~28 pts SENSEX)
BREAKEVEN_SPOT_ATR_MULT = 0.5           # 0.5x 1-minute ATR
DELTA_MIN_SPOT_BPS = 1.25               # 1.25 bps minimum spot move for empirical delta (~3 pts NIFTY, ~10 pts SENSEX)

# Legacy fallbacks maintained for external callers/tests
TARGET_PROFIT_PCT = RESTING_TP_LIMIT_PCT
STOP_LOSS_PCT = 0.050

# ─── AEGIS V4.1 Stepped Risk Compression + Cost-Shield + Near-Target Engine ───
# Scale-Invariant Normalized Percentage Model (strictly %, no fixed paise)
PREMIUM_HARD_SL_PCT = float(os.environ.get("AEGIS_PREMIUM_HARD_SL_PCT", "0.120"))   # Tick-cadence option premium disaster circuit breaker (-12.0%)
V4_SINGLE_TARGET_MODE = True  # Runner trailing disabled; 100% qty TP/SL/time exits
STEP1_TRIGGER_PCT = float(os.environ.get("AEGIS_STEP1_TRIGGER_PCT", "0.020"))         # +2.0% peak gain arms stepped safety net
STEP1_SL_PCT = float(os.environ.get("AEGIS_STEP1_SL_PCT", "-0.030"))                  # Tightens disaster floor from -12% to -3.0%
STEP2_TRIGGER_PCT = float(os.environ.get("AEGIS_STEP2_TRIGGER_PCT", "0.030"))         # +3.0% peak gain arms cost-shield scratch
STEP2_SL_PCT = float(os.environ.get("AEGIS_STEP2_SL_PCT", "0.000"))                   # Tightens SL to 0.0% (Entry Price / Scratch)
NEAR_TARGET_TRIGGER_PCT = float(os.environ.get("AEGIS_NEAR_TARGET_TRIGGER_PCT", "0.035")) # +3.5% peak gain arms near-target lock
NEAR_TARGET_PULLBACK_TOLERANCE = float(os.environ.get("AEGIS_NEAR_TARGET_TOLERANCE", "0.005")) # 0.5% pullback from peak triggers market exit
BREAKEVEN_TRIGGER_PCT = 0.030
BREAKEVEN_LOCK_PCT = 0.005
RUNNER_TRIGGER_PCT = 0.070
RUNNER_FLOOR_PCT = 0.050
RUNNER_TRAIL_PCT = 0.020

def _round_tick(price: float, tick: float = OPTION_TICK_SIZE) -> float:
    if price <= 0:
        return 0.0
    return round(round(price / tick) * tick, 2)


class AegisExecutionManager:
    """
    Execution layer for AEGIS Gamma Burst signals.
    - Enters on GAMMA_BURST_ENTRY (direction + conviction)
    - Exits on AEGIS exit signals (ξ_soft → 50% reduce, ξ_hard → flatten)
    - Force-flattens at session end
    - Feeds exact fill timestamps/prices back to AEGIS engine for latency decay
    """

    def __init__(
        self,
        broker_gateway,
        data_fetcher,
        aegis_engine,                   # Reference to AegisGammaEngine for fill feedback
        signal_queue: queue.Queue,      # AEGIS → Execution signal queue
    ):
        self.broker = broker_gateway
        self.data_fetcher = data_fetcher
        self.aegis_engine = aegis_engine
        self.signal_queue = signal_queue

        self.data_dir = get_data_dir()
        mode = "LIVE" if ENABLE_LIVE_BROKERAGE_EXECUTION else "PAPER"
        self.capital_engine = CapitalEngine(mode=mode, portfolio_provider=self.broker, data_dir=self.data_dir)
        
        if ENABLE_LIVE_BROKERAGE_EXECUTION:
            if hasattr(broker_gateway, "get_order_lifecycle"):
                self.order_lifecycle = broker_gateway.get_order_lifecycle()
            else:
                self.order_lifecycle = ShoonyaOrderLifecycle(
                    broker_gateway.execution_provider if hasattr(broker_gateway, "execution_provider") else broker_gateway
                )
            logger.info("[AegisEM] LIVE mode — Shoonya LMT / NRML path")
        else:
            self.order_lifecycle = PaperOrderLifecycle()
            logger.info("[AegisEM] PAPER mode")

        # State
        self.trade_context: Optional[TradeContext] = None
        self.trades_today = 0
        self.last_trade_date = None
        self.strike_cooldowns: Dict[str, datetime] = {}
        self._option_cache: Dict = getattr(aegis_engine, "option_cache", {}) if aegis_engine else {}
        self.tp_order_id: Optional[str] = None
        self.last_tp_order_id: Optional[str] = None
        self.trade_lock = threading.Lock()
        self._trade_quote_history = deque(maxlen=60)
        
        # Cooldown & Inter-trade Intervals
        self.last_trade_exit_time: float = 0.0
        self.last_trade_loss: bool = False
        self.last_exited_route: str = ""
        self.consecutive_route_cooldown_sec: float = float(os.getenv("AEGIS_CONSECUTIVE_ROUTE_COOLDOWN_SEC", "180.0"))  # 3 mins for same setup
        self.inter_trade_cooldown_sec: float = float(os.getenv("AEGIS_INTER_TRADE_COOLDOWN_SEC", "60.0"))               # 1 min general cooldown
        self.post_loss_cooloff_sec: float = float(os.getenv("AEGIS_POST_LOSS_COOLOFF_SEC", "120.0"))                    # 2 mins cooloff after loss

        # Quality Stack safety gear: consecutive-loss stop + daily loss cap
        self.consec_loss_stop: int = int(os.getenv("AEGIS_CONSEC_LOSS_STOP", "2"))
        self.daily_loss_cap_rs: float = float(os.getenv("AEGIS_DAILY_LOSS_CAP_RS", "2000.0"))
        self._consec_losses: int = 0
        
        # PnL tracking
        self.daily_pnl = 0.0
        
        # Persistence
        trades_dir = os.path.join(self.data_dir, "trades")
        os.makedirs(trades_dir, exist_ok=True)
        self.history_file = os.path.join(trades_dir, "trade_history.json")
        self.active_context_file = os.path.join(trades_dir, "aegis_active_trade_context.json")
        self.state_file = os.path.join(trades_dir, "aegis_state.json")
        
        self._load_active_context()
        self._load_daily_pnl()
        self._load_trades_today()

        # Main loop polling
        self._running = False
        self._main_thread: Optional[threading.Thread] = None

    # ─── Persistence ───
    def _load_trades_today(self):
        today = datetime.now().date()
        if not os.path.exists(self.state_file):
            self.trades_today = 0
            self.last_trade_date = today
            return
        try:
            with open(self.state_file, "r") as f:
                state = json.load(f)
            state_date = datetime.fromisoformat(state.get("date", "1970-01-01")).date()
            if state_date == today:
                self.trades_today = int(state.get("trades_today", 0))
                self.last_trade_date = today
                logger.info(f"[AegisEM] Restored trades_today: {self.trades_today}")
            else:
                self.trades_today = 0
                self.last_trade_date = today
        except Exception as e:
            logger.error(f"[AegisEM] Load trades_today failed: {e}")
            self.trades_today = 0
            self.last_trade_date = today

    def _save_trades_today(self):
        try:
            state = {
                "date": (self.last_trade_date or datetime.now().date()).isoformat(),
                "trades_today": self.trades_today,
            }
            with open(self.state_file, "w") as f:
                json.dump(state, f)
        except Exception as e:
            logger.error(f"[AegisEM] Save trades_today failed: {e}")

    def _load_daily_pnl(self):
        self.daily_pnl = 0.0
        if not os.path.exists(self.history_file):
            return
        try:
            with open(self.history_file, "r") as f:
                hist = json.load(f)
            today_str = datetime.now().strftime("%d %b %Y")
            for trade in hist:
                if trade.get("entry_time", "").startswith(today_str):
                    self.daily_pnl += trade.get("pnl", 0.0)
            logger.info(f"[AegisEM] Restored daily PnL: ₹{self.daily_pnl:.2f}")
        except Exception as e:
            logger.error(f"[AegisEM] Load daily PnL failed: {e}")

    def get_slot_state(self) -> dict:
        """Quality Stack telemetry: current slot/safety state for dashboard & tests."""
        return {
            "trades_today": int(getattr(self, "trades_today", 0)),
            "max_trades_per_day": MAX_TRADES_PER_DAY,
            "daily_pnl": round(float(getattr(self, "daily_pnl", 0.0)), 2),
            "consec_losses": int(getattr(self, "_consec_losses", 0)),
            "consec_loss_stop": int(getattr(self, "consec_loss_stop", 2)),
            "daily_loss_cap_rs": float(getattr(self, "daily_loss_cap_rs", 2000.0)),
            "last_trade_loss": bool(getattr(self, "last_trade_loss", False)),
        }

    # ─── Calendar Gates ───
    @staticmethod
    def can_enter(now: datetime = None) -> bool:
        now = now or datetime.now()
        if not is_trading_day(now.date()):
            return False
        return dt_time(9, 16, 0) <= now.time() <= AEGIS_ENTRY_CUTOFF

    @staticmethod
    def must_force_flatten(now: datetime = None) -> bool:
        now = now or datetime.now()
        return now.time() >= AEGIS_FORCE_FLATTEN

    def has_open_trade(self) -> bool:
        if getattr(self, "_is_entering", False):
            return True
        if getattr(self, "_is_exiting", False):
            return True
        return bool(self.trade_context and str(self.trade_context.status).upper() in ("OPEN", "PENDING", "SUBMITTED", "EXIT_PENDING"))

    def _rollback_slot(self) -> None:
        """Rollback slot usage in quality slot policy if entry is blocked downstream."""
        sp = getattr(self.aegis_engine, "slot_policy", None)
        if sp and hasattr(sp, "rollback_admission"):
            sp.rollback_admission()

    def get_instrument_params(self) -> dict:
        name, _ = self.data_fetcher.get_active_instrument()
        if name == "SENSEX":
            return {
                "premium_min": SENSEX_PREMIUM_MIN,
                "premium_max": SENSEX_PREMIUM_MAX,
                "lot_size": SENSEX_LOT_SIZE,
                "index_name": "SENSEX",
                "exchange": "BFO",
            }
        return {
            "premium_min": NIFTY_PREMIUM_MIN,
            "premium_max": NIFTY_PREMIUM_MAX,
            "lot_size": NIFTY_LOT_SIZE,
            "index_name": "NIFTY",
            "exchange": "NFO",
        }

    # ─── Main Loop ───
    def start(self):
        """Start the execution manager's main processing loop."""
        if self._running:
            return
        self._running = True
        self._main_thread = threading.Thread(target=self._run_loop, daemon=True)
        self._main_thread.start()
        logger.info("[AegisEM] Execution manager started")

    def stop(self):
        self._running = False
        if self._main_thread:
            self._main_thread.join(timeout=2.0)
        logger.info("[AegisEM] Execution manager stopped")

    def _run_loop(self):
        """Main processing loop: drains signal queue, manages open trades."""
        while self._running:
            try:
                # 0. Daily reset check
                today_d = datetime.now().date()
                if self.last_trade_date != today_d:
                    self.trades_today = 0
                    self.daily_pnl = 0.0
                    self.last_trade_date = today_d

                # 1. Process AEGIS signals (blocking with timeout)
                try:
                    signal = self.signal_queue.get(timeout=0.5)
                    self._process_aegis_signal(signal)
                except queue.Empty:
                    pass

                # 2. Manage open trade (exits, force flatten)
                if self.has_open_trade():
                    self._manage_open_trade()

                # 3. Check pending edge measures for AEGIS latency tracking
                # (P0-3: engine resolves the *option premium* LTP internally via
                # option_cache — never pass index spot here.)
                if self.aegis_engine:
                    self.aegis_engine.check_pending_edges(time.time())

                # 4. Check pre-market bridge (09:08 - 09:15:30)
                now_t = datetime.now().time()
                if dt_time(9, 8, 0) <= now_t <= dt_time(9, 15, 30):
                    self._handle_premarket_bridge()

            except Exception as e:
                logger.error(f"[AegisEM] Main loop error: {e}", exc_info=True)
                time.sleep(1.0)

    def _handle_premarket_bridge(self):
        """Processes 09:08-09:14 Pre-Market equilibrium from ARGUS (ATM pre-alignment only).

        P1-9: the 09:15 blind gap auto-fire (conviction 5.0, no microstructure
        confirmation) is permanently disabled. Indicative price is used ONLY
        to pre-align the ATM strike; orders still require a full
        microstructure-scored GAMMA_BURST_ENTRY signal.
        """
        pm_state = read_premarket_state()
        if not pm_state:
            return
        ind_spot = pm_state.get("indicative_spot", 0.0)
        gap_pct = pm_state.get("gap_pct", 0.0)

        if ind_spot > 0 and self.aegis_engine:
            if self.aegis_engine.last_spot != ind_spot:
                logger.info(f"[AegisEM] Pre-Market Bridge: Pre-aligning ATM to {ind_spot:.2f} (Gap: {gap_pct:+.2f}%)")
                self.aegis_engine._on_underlying_tick("INDEX", ind_spot, time.time())

    def _get_current_spot(self) -> float:
        """Fetch current spot price for AEGIS edge measurement."""
        try:
            return self.data_fetcher.get_index_ltp()
        except Exception:
            return 0.0

    # ─── Signal Processing ───
    def _process_aegis_signal(self, signal: Dict):
        """Handle GAMMA_BURST_ENTRY from AEGIS queue."""
        sig_type = signal.get("type")
        if sig_type != "GAMMA_BURST_ENTRY":
            return

        now = datetime.now()
        
        # Daily reset
        if getattr(self, "last_trade_date", None) != now.date():
            self.trades_today = 0
            self.daily_pnl = 0.0
            self.last_trade_date = now.date()
            self._consec_losses = 0
        # Daily trade limit (Strict 3 trades max per day)
        max_daily_trades = MAX_TRADES_PER_DAY
        if self.trades_today >= max_daily_trades:
            logger.warning(f"[AegisEM] Daily trade limit ({max_daily_trades}) reached")
            return

        # Quality Stack: consecutive-loss stop (blocks entries after N straight losses)
        consec_stop = int(getattr(self, "consec_loss_stop", 2))
        consec_losses = int(getattr(self, "_consec_losses", 0))
        if consec_stop > 0 and consec_losses >= consec_stop:
            logger.warning(
                f"[AegisEM] Entry blocked — consecutive-loss stop ({consec_losses} >= {consec_stop})"
            )
            return

        # Quality Stack: daily loss cap (blocks entries once day's PnL crosses -cap)
        daily_cap = float(getattr(self, "daily_loss_cap_rs", 2000.0))
        daily_pnl = float(getattr(self, "daily_pnl", 0.0))
        if daily_cap > 0 and daily_pnl <= -daily_cap:
            logger.warning(
                f"[AegisEM] Entry blocked — daily loss cap hit (PnL ₹{daily_pnl:.2f} <= -₹{daily_cap:.0f})"
            )
            return

        now_epoch = time.time()

        # Momentum-chain route guard: same setup can repeat on 3rd/4th high,
        # but not within the consecutive-route cooldown (default 180s).
        # Old strict rule blocked all back-to-back repeats unconditionally —
        # that killed falling-knife chains (2nd high trade, then 4th high trade).
        incoming_route = str(signal.get("metrics", {}).get("pa_setup_name") or "")
        if (
            incoming_route
            and self.last_exited_route
            and incoming_route == self.last_exited_route
            and self.last_trade_exit_time > 0
            and now_epoch < (self.last_trade_exit_time + self.consecutive_route_cooldown_sec)
        ):
            rem = int((self.last_trade_exit_time + self.consecutive_route_cooldown_sec) - now_epoch)
            logger.warning(
                f"[AegisEM] Entry blocked — same-route cooldown active for '{incoming_route}' ({rem}s remaining). "
                f"Chain allowed after cooldown (3rd/4th high)."
            )
            self._rollback_slot()
            return

        # Inter-trade Cooldown (General pause between trade exits to prevent whip capture)
        if self.last_trade_exit_time > 0 and now_epoch < (self.last_trade_exit_time + self.inter_trade_cooldown_sec):
            rem = int((self.last_trade_exit_time + self.inter_trade_cooldown_sec) - now_epoch)
            logger.warning(f"[AegisEM] Entry blocked — inter-trade cooldown active ({rem}s remaining)")
            self._rollback_slot()
            return

        # Post-Loss Freeze (Mandatory 5-minute freeze after a loss to allow market turbulence to settle)
        if self.last_trade_loss and self.last_trade_exit_time > 0 and now_epoch < (self.last_trade_exit_time + self.post_loss_cooloff_sec):
            rem = int((self.last_trade_exit_time + self.post_loss_cooloff_sec) - now_epoch)
            logger.warning(f"[AegisEM] Entry blocked — post-loss cooloff active ({rem}s remaining)")
            self._rollback_slot()
            return

        with self.trade_lock:
            if self.has_open_trade():
                logger.warning("[AegisEM] Entry blocked — trade already open")
                self._rollback_slot()
                return
            self._is_entering = True

        try:
            if not self.can_enter(now):
                logger.warning(f"[AegisEM] Entry blocked — outside window (cutoff {AEGIS_ENTRY_CUTOFF})")
                self._rollback_slot()
                return

            direction = signal.get("signal")  # "UP" or "DOWN"
            conviction = signal.get("conviction", 0.0)
            opt_lp = signal.get("lp", 0.0)
            sym = signal.get("sym", "")
            metrics = signal.get("metrics", {})

            params = self.get_instrument_params()
            weekly_opts = self.data_fetcher.get_weekly_option_tokens()
            
            # Map signal direction to option type
            signal_type = "CALL" if direction == "UP" else "PUT"
            
            # Retrieve actual underlying index spot price
            spot_price = float(signal.get("spot") or getattr(self.aegis_engine, "last_spot", 0.0) or 0.0)
            if spot_price <= 0.0:
                spot_price = self._get_current_spot()
            
            allocations = StrikeSelector.select_strikes(
                signal_type=signal_type,
                index_price=spot_price,
                index_name=params["index_name"],
                weekly_opts=weekly_opts,
                option_cache=self._option_cache,
                available_funds=self.capital_engine.get_available_funds(),
                lot_size=params["lot_size"],
                market_data_provider=getattr(self.broker, "market_data_provider", None),
                premium_min=params.get("premium_min"),
                premium_max=params.get("premium_max"),
            )
            
            if not allocations:
                logger.warning(f"[AegisEM] No affordable strike for {signal_type}")
                self._rollback_slot()
                return

            opt_row, raw_quantity, ltp = allocations[0]
            symbol = opt_row["symbol"]
            token_str = str(opt_row["token"])

            sizing_tier = str(metrics.get("sizing_tier") or signal.get("sizing_tier") or "STANDARD_SIZE").upper()
            if sizing_tier == "BLOCKED":
                logger.warning(f"[AegisEM] Entry blocked: signal sizing_tier is BLOCKED (conviction < 75.0)")
                self._rollback_slot()
                return

            # Strictly Fixed 1 Lot Sizing (Core Execution Discipline — No Multi-Lot Scaling)
            quantity = 1
            logger.info(f"[AegisEM] Sizing: Fixed 1 lot (Cost: ₹{ltp * params['lot_size']:.2f})")
            
            if symbol in self.strike_cooldowns and now < self.strike_cooldowns[symbol]:
                logger.warning(f"[AegisEM] Strike {symbol} in cooldown")
                self._rollback_slot()
                return

            # Spread & liquidity check
            live_ask = 0.0
            if hasattr(self.broker, "market_data_provider"):
                try:
                    exch_seg = params["exchange"]
                    q = self.broker.market_data_provider.get_quote(exch_seg, token_str)
                    if q:
                        ask = float(q.get("sp1") or 0)
                        bid = float(q.get("bp1") or 0)
                        live_ask = ask
                        if ask > 0 and bid > 0:
                            mid = (ask + bid) / 2
                            spread = (ask - bid) / mid
                            if spread > 0.02:
                                logger.warning(f"[AegisEM] Spread too high: {spread*100:.1f}%")
                                self._rollback_slot()
                                return
                            sq1 = int(float(q.get("sq1") or 0))
                            if sq1 < 3:
                                logger.warning(f"[AegisEM] Ask size too thin: sq1={sq1}")
                                self._rollback_slot()
                                return
                except Exception as e:
                    logger.error(f"[AegisEM] Pre-trade check failed: {e}")
                    self._rollback_slot()
                    return

            # Execute entry
            signal_ts = float(signal.get("timestamp") or metrics.get("impulse_timestamp") or time.time())
            with self.trade_lock:
                self.trade_context = TradeContext(
                    signal_type=signal_type,
                    strategy="AEGIS_GAMMA_BURST",
                    index_entry=spot_price,
                    decision_payload={
                        "conviction": conviction,
                        "direction": direction,
                        "metrics": metrics,
                        "iv": metrics.get("iv", 0.15),
                        "signal_ts": signal_ts,
                    },
                )
                leg = TradeLeg(token_str, symbol, float(opt_row["strike"]), signal_type, quantity, ltp)
                
                try:
                    if not self.order_lifecycle.execute_entry(leg, live_ask=live_ask or ltp):
                        logger.error(f"[AegisEM] Entry order rejected for {symbol}")
                        self.trade_context = None
                        self._rollback_slot()
                        return
                    
                    fill_ts = time.time()
                    entry_latency_ms = max(0.0, round((fill_ts - signal_ts) * 1000.0, 1))

                    # Enrich decision_payload with exact execution timestamps & microstructure context
                    fut_px = float(metrics.get("futures_price", 0.0) or getattr(self.aegis_engine, "last_futures_price", 0.0))
                    basis_pts_val = float(metrics.get("basis_pts", 0.0) or getattr(self.aegis_engine, "last_basis_pts", 0.0))
                    basis_bps_val = float(metrics.get("basis_bps", 0.0) or getattr(self.aegis_engine, "last_basis_bps", 0.0))
                    fut_vol_val = float(metrics.get("futures_volume", 0.0) or getattr(self.aegis_engine, "futures_volume", 0.0))

                    dp = {
                        "conviction": conviction,
                        "sizing_tier": sizing_tier,
                        "conviction_score": float(metrics.get("conviction_score", conviction or 0.0)),
                        "conviction_sub_scores": metrics.get("conviction_sub_scores", {}),
                        "direction": direction,
                        "metrics": metrics,
                        "iv": metrics.get("iv", 0.15),
                        "signal_ts": signal_ts,
                        "impulse_timestamp": float(metrics.get("impulse_timestamp") or signal_ts),
                        "impulse_time_iso": metrics.get("impulse_time_iso", datetime.fromtimestamp(signal_ts).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]),
                        "fill_timestamp": fill_ts,
                        "fill_time_iso": datetime.fromtimestamp(fill_ts).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                        "entry_latency_ms": entry_latency_ms,
                        "entry_spot_price": spot_price,
                        "futures_price": fut_px,
                        "basis_pts": basis_pts_val,
                        "basis_bps": basis_bps_val,
                        "futures_volume": fut_vol_val,
                        "volatility_burst_age_sec": float(metrics.get("volatility_burst_age_sec", 0.0)),
                        "trigger_age_sec": float(metrics.get("trigger_age_sec", 0.0)),
                        "atr_1m": float(metrics.get("atr_1m", 0.0)),
                        "eqs_score": float(metrics.get("eqs_score", 0.0)),
                        "microstructure_score": float(metrics.get("microstructure_score", conviction)),
                        "persistence_score": float(metrics.get("persistence_score", 0.0)),
                        "market_structure": metrics.get("market_structure", "NEUTRAL"),
                        "spread_pct": float(metrics.get("spread_pct", 0.0)),
                        "total_depth_qty": float(metrics.get("total_depth_qty", 0.0)),
                        "basis_60s_ago": float(metrics.get("basis_60s_ago", 0.0)),
                        "basis_change_60s": float(metrics.get("basis_change_60s", 0.0)),
                        "basis_change_60s_bps": float(metrics.get("basis_change_60s_bps", 0.0)),
                        "basis_z_score": float(metrics.get("basis_z_score", 0.0)),
                        "is_50ema_pullback": bool(metrics.get("is_50ema_pullback", False)),
                        "is_price_action_setup": bool(metrics.get("is_price_action_setup", False)),
                        "pa_setup_name": str(metrics.get("pa_setup_name", "")),
                        "rejection_type": metrics.get("rejection_type", "none"),
                        "spot_sl_price": float(metrics.get("sl_price", 0.0) or 0.0),
                        "candle_high": float(metrics.get("bar_high", 0.0) or 0.0),
                        "candle_low": float(metrics.get("bar_low", 0.0) or 0.0),
                        # ─── OI shield audit trail (visibility only — gates untouched) ───
                        "oi_snapshot": metrics.get("oi_snapshot", {}),
                        "oi_vel": float((metrics.get("oi_snapshot", {}) or {}).get("vel", 0.0)),
                        "oi_acc": float((metrics.get("oi_snapshot", {}) or {}).get("acc", 0.0)),
                        "oi_n": int((metrics.get("oi_snapshot", {}) or {}).get("n", 0)),
                        "oi_builtup": str((metrics.get("oi_snapshot", {}) or {}).get("builtup", "UNKNOWN")),
                        "oi_wall_dist_strikes": float((metrics.get("oi_snapshot", {}) or {}).get("wall_dist_strikes", 0.0)),
                    }
                    self.trade_context.decision_payload = dp
                    self.trade_context.spot_sl_price = dp["spot_sl_price"]
                    self.trade_context.candle_high = dp["candle_high"]
                    self.trade_context.candle_low = dp["candle_low"]
                    self.trade_context.is_50ema_pullback = dp["is_50ema_pullback"]
                    self.trade_context.is_price_action_setup = dp["is_price_action_setup"]
                    self.trade_context.pa_setup_name = dp["pa_setup_name"]
                    self.trade_context.add_leg(leg)

                    logger.info(
                        f"[SHADOW FUTURES TELEMETRY] {symbol} Entry | Spot: {spot_price:.1f} | "
                        f"Basis: {basis_pts_val:+.1f}pts ({basis_bps_val:+.1f}bps) | 60s Chg: {dp['basis_change_60s']:+.1f}pts | "
                        f"Z: {dp['basis_z_score']:+.2f} | PA Setup: {dp['pa_setup_name'] or dp['is_50ema_pullback']} "
                        f"(Type: {dp['rejection_type']}, Spot SL: {dp['spot_sl_price']:.1f})"
                    )
                except Exception as e_entry:
                    logger.critical(f"[AegisEM] Entry order execution crashed: {e_entry}", exc_info=True)
                    self.trade_context = None
                    return

            self.capital_engine.deduct_funds(leg.entry_price * quantity * params["lot_size"])
            self.trades_today += 1
            self._save_trades_today()
            self.tp_order_id = None
            self._trade_quote_history.clear()

            # Place fast scalp resting limit TP (Constant 4.0% Maker limit target)
            rec_tp = RESTING_TP_LIMIT_PCT
            self._place_initial_take_profit(leg, params, target_pct=rec_tp)
        finally:
            self._is_entering = False
        self._save_active_context()
        
        logger.info(
            f"🚀 [AegisEM] ENTERED {signal_type} {quantity}x {symbol} @ {leg.entry_price:.2f} | "
            f"Latency: {entry_latency_ms:.0f}ms | Conviction: {conviction:.2f} | Target: +{rec_tp*100:.1f}% | "
            f"Basis: {basis_pts_val:+.1f}pts ({basis_bps_val:+.1f}bps) | Fut: {fut_px:.1f}"
        )

    def _place_initial_take_profit(self, leg: TradeLeg, params: dict, target_pct: Optional[float] = None):
        """Resting DAY limit target (Fast 4.0% Scalp Target)."""
        eff_tp_pct = target_pct if (target_pct is not None and target_pct > 0.0) else RESTING_TP_LIMIT_PCT
        tp_price = _round_tick(leg.entry_price * (1.0 + eff_tp_pct))
        leg.tp_price = tp_price
        leg.initial_tp_pct = eff_tp_pct
        leg.highest_price = leg.entry_price
        leg.breakeven_locked = False
        leg.step1_sl_armed = False
        leg.runner_locked = False
        
        if isinstance(self.order_lifecycle, PaperOrderLifecycle):
            self.tp_order_id = f"PAPER_TP_{leg.symbol}"
            logger.info(f"[AegisEM] [Paper] Resting TP limit set at {tp_price:.2f} (+{eff_tp_pct*100:.1f}%)")
            return

        try:
            exch = params["exchange"]
            qty = str(leg.quantity * params["lot_size"])
            if hasattr(self.order_lifecycle, "_build_kwargs"):
                kwargs = self.order_lifecycle._build_kwargs("S", exch, leg.symbol, qty, f"{tp_price:.2f}")
                api = self.broker if hasattr(self.broker, "place_order") else getattr(self.broker, "_api", None)
                place = getattr(self.broker, "place_order", None) or getattr(api, "place_order", None)
                if place:
                    resp = place(**kwargs)
                    if isinstance(resp, dict) and resp.get("stat") == "Ok" and resp.get("norenordno"):
                        self.tp_order_id = resp["norenordno"]
                        self.last_tp_order_id = self.tp_order_id
                        logger.info(f"[AegisEM] DAY TP placed {self.tp_order_id} @ {tp_price:.2f}")
        except Exception as e:
            logger.error(f"[AegisEM] TP place failed: {e}")

    @staticmethod
    def _check_hist_filled(hist: Any) -> Tuple[bool, float]:
        """
        Safely inspects Shoonya single_order_history list (returned newest-first).
        Checks all events in history: if any is FILLED/COMPLETE or rpt == 'Fill',
        returns (True, fill_price).
        """
        if not isinstance(hist, list) or not hist:
            return False, 0.0
        for h in hist:
            if isinstance(h, dict):
                status = str(h.get("status", "")).upper()
                rpt = str(h.get("rpt", "")).upper()
                if status in ("FILLED", "COMPLETE") or rpt == "FILL":
                    px = float(h.get("flprc") or h.get("avgprc") or 0.0)
                    return True, px
        return False, 0.0

    def _cancel_tp(self):
        if not self.tp_order_id or str(self.tp_order_id).startswith("PAPER_"):
            self.tp_order_id = None
            return
        ordno = self.tp_order_id
        self.last_tp_order_id = ordno
        try:
            api = getattr(self.broker, "_api", None)
            if api and hasattr(api, "single_order_history"):
                # Fast check if already filled before cancelling
                hist = api.single_order_history(orderno=ordno)
                filled, fill_px = self._check_hist_filled(hist)
                if filled:
                    logger.info(f"[AegisEM] Resting TP order {ordno} already FILLED at broker @ {fill_px:.2f}! Skipping cancel.")
                    self.tp_order_id = None
                    return
            if api and hasattr(api, "cancel_order"):
                resp = api.cancel_order(orderno=ordno)
                logger.info(f"[AegisEM] Cancelled TP order {ordno} (resp: {resp})")
        except Exception as e:
            logger.warning(f"[AegisEM] TP cancel warning: {e}")
        self.tp_order_id = None

    # ─── Adaptive Quantitative Microstructure Helpers ───
    def _compute_noise_floor(self, leg: TradeLeg, live_bid: float, live_ask: float) -> float:
        """
        Calculates dynamic noise floor (in Rupees) based on bid-ask spread and realized micro-volatility:
        noise_floor_rs = max(k1 * spread, k2 * stdev_30s, min_floor)
        where min_floor scales proportionally with option entry premium (min 5% or ₹1.50).
        """
        now_ts = time.time()
        spread = max(live_ask - live_bid, 0.15) if (live_ask > 0 and live_bid > 0 and live_ask >= live_bid) else 0.15
        
        # Realized micro-volatility over last 30-45s
        recent_prices = [p for (ts, s, p, b, a) in self._trade_quote_history if (now_ts - ts) <= 45.0 and p > 0]
        if len(recent_prices) >= 4:
            stdev_prem = float(np.std(recent_prices))
        else:
            stdev_prem = spread * 0.5
            
        proportional_floor = leg.entry_price * NOISE_FLOOR_MIN_PCT if leg.entry_price > 0 else DEFAULT_MIN_NOISE_FLOOR_RS
        effective_min_floor = max(DEFAULT_MIN_NOISE_FLOOR_RS, proportional_floor)

        noise_floor = max(
            NOISE_FLOOR_SPREAD_MULT * spread,
            NOISE_FLOOR_SIGMA_MULT * stdev_prem,
            effective_min_floor,
        )
        return round(noise_floor, 2)

    def _compute_realized_delta(self, context: TradeContext, leg: TradeLeg, curr_spot: float) -> float:
        """
        Empirical / Realized Local Delta (Δpremium / Δspot) without heavy Black-Scholes.
        Uses live observed relationship or fast moneyness approximation.
        Normalizes minimum spot move in basis points (DELTA_MIN_SPOT_BPS).
        """
        spot_entry = context.index_entry
        prem_entry = leg.entry_price
        delta_spot = abs(curr_spot - spot_entry)
        delta_prem = abs(leg.current_price - prem_entry)

        # Normalized minimum spot move hurdle (in bps: 1.25 bps ~ 3 pts NIFTY, ~10 pts SENSEX)
        ref_spot = spot_entry if spot_entry > 0 else (curr_spot if curr_spot > 0 else 24000.0)
        min_spot_move = max(1.0, ref_spot * (DELTA_MIN_SPOT_BPS / 10000.0))

        if delta_spot >= min_spot_move:
            emp_delta = delta_prem / delta_spot
            return max(0.25, min(0.70, emp_delta))

        # Fast theoretical fallback based on Moneyness (Normalized by strike step)
        params = self.get_instrument_params()
        strike_step = 50.0 if "NIFTY" in params.get("index_name", "NIFTY").upper() else 100.0
        sig_type = str(context.signal_type).upper()
        if sig_type in ("CALL", "BUY_CALL"):
            m = (spot_entry - leg.strike) / strike_step
        else:
            m = (leg.strike - spot_entry) / strike_step

        if m >= 0.5:
            return 0.55  # ITM
        elif m >= -0.5:
            return 0.45  # ATM
        elif m >= -1.5:
            return 0.35  # 1-strike OTM
        else:
            return 0.25  # 2-strike OTM

    def _is_active_instrument_expiry(self, now: Optional[datetime] = None, leg: Optional[TradeLeg] = None) -> bool:
        """
        Determines if today is Expiry Day for the active instrument or contract.
        Matches symbol date (e.g. 16SEP26), or standard weekday schedule:
        - SENSEX: Wednesday (weekday 2)
        - NIFTY: Thursday (weekday 3)
        """
        if now is None:
            now = datetime.now()
        
        # Method 1: Check contract symbol string for today's date stamp (e.g. '16SEP')
        if leg and leg.symbol:
            day_str = now.strftime("%d%b").upper()
            day_str_alt = now.strftime("%d%b%y").upper()
            sym_upper = leg.symbol.upper()
            if day_str in sym_upper or day_str_alt in sym_upper:
                return True

        # Method 2: Check active instrument standard weekday
        params = self.get_instrument_params()
        idx_name = str(params.get("index_name", "NIFTY")).upper()
        weekday = now.weekday()
        if "SENSEX" in idx_name and weekday == 2:  # Wednesday
            return True
        elif "NIFTY" in idx_name and weekday == 3:  # Thursday
            return True

        return False

    # ─── Open Trade Management ───
    def _manage_open_trade(self, now: Optional[datetime] = None):
        """Check exit conditions: AEGIS signals, force flatten, TP/SL, Trailing SL."""
        if now is None:
            now = datetime.now()
        now_ts = now.timestamp()
        
        with self.trade_lock:
            if not self.has_open_trade() or getattr(self, "_is_exiting", False):
                return
            if not self.trade_context or str(self.trade_context.status).upper() != "OPEN":
                return
            context = self.trade_context
            legs = list(context.legs)

        params = self.get_instrument_params()
        exch_seg = params["exchange"]
        curr_spot = self._get_current_spot()
        
        # Refresh marks and update highest price
        live_bid = 0.0
        live_ask = 0.0
        for leg in legs:
            if leg.status != "OPEN":
                continue
            opt_cache = getattr(self, "_option_cache", None)
            if opt_cache and leg.token in opt_cache:
                c_lp = opt_cache[leg.token]
                if c_lp > 0:
                    leg.current_price = c_lp
                    live_bid = c_lp
                    live_ask = c_lp
            else:
                try:
                    q = self.broker.market_data_provider.get_quote(exch_seg, leg.token)
                    if q:
                        if float(q.get("ltp") or 0) > 0:
                            leg.current_price = float(q["ltp"])
                        live_bid = float(q.get("bp1") or leg.current_price)
                        live_ask = float(q.get("sp1") or leg.current_price)
                except Exception:
                    live_bid = leg.current_price

            # Update excursion metrics (MFE & MAE tracking)
            hold_sec_now = (now - context.entry_time).total_seconds() if context.entry_time else 0.0
            if leg.current_price > getattr(leg, "highest_price", leg.entry_price):
                leg.highest_price = leg.current_price
                leg.time_to_highest_sec = hold_sec_now
            if not hasattr(leg, "lowest_price") or leg.current_price < leg.lowest_price:
                leg.lowest_price = leg.current_price
                leg.time_to_lowest_sec = hold_sec_now
            eff_bid_now = live_bid if live_bid > 0 else leg.current_price
            if eff_bid_now > getattr(leg, "highest_bid", 0.0):
                leg.highest_bid = eff_bid_now

            # Record in rolling trade history
            self._trade_quote_history.append((now_ts, curr_spot, leg.current_price, live_bid, live_ask))

        # 0. Check if resting TP order was ALREADY filled at the broker!
        if self.tp_order_id and not str(self.tp_order_id).startswith("PAPER_"):
            api = getattr(self.broker, "_api", None)
            if api and hasattr(api, "single_order_history"):
                try:
                    hist = api.single_order_history(orderno=self.tp_order_id)
                    filled, fill_px = self._check_hist_filled(hist)
                    if filled:
                        actual_px = fill_px if fill_px > 0 else float(getattr(legs[0], "tp_price", 0.0))
                        logger.info(f"🎉 [AegisEM] Resting TP order {self.tp_order_id} was FILLED at broker @ {actual_px:.2f}!")
                        self.last_tp_order_id = self.tp_order_id
                        self.tp_order_id = None
                        self._close_all_legs(
                            f"Take-Profit Hit (+{(actual_px - legs[0].entry_price)/legs[0].entry_price*100:.1f}% @ {actual_px:.2f})",
                            is_tp_maker=True,
                            limit_fill_price=actual_px,
                        )
                        return
                except Exception as e_tp_check:
                    logger.warning(f"[AegisEM] Error checking TP order status: {e_tp_check}")

        # 0B. Local Take-Profit Hit Check (Paper + Live backstop — never depends on
        # broker resting-order fill polling alone). Fires the instant bid touch/crosses
        # leg.tp_price, so a +4% day-move can no longer ride to the time stop.
        for leg in legs:
            if leg.status != "OPEN":
                continue
            tp_price = float(getattr(leg, "tp_price", 0.0) or 0.0)
            if tp_price > 0 and leg.entry_price > 0:
                eff_bid = live_bid if live_bid > 0 else leg.current_price
                if eff_bid > 0 and eff_bid >= tp_price:
                    gain_pct = ((tp_price - leg.entry_price) / leg.entry_price) * 100.0
                    logger.info(
                        f"[AegisEM] Local TP hit: {leg.symbol} bid {eff_bid:.2f} >= tp {tp_price:.2f} (+{gain_pct:.1f}%)"
                    )
                    actual_fill_px = tp_price
                    if self.tp_order_id and not str(self.tp_order_id).startswith("PAPER_"):
                        api = getattr(self.broker, "_api", None)
                        if api and hasattr(api, "single_order_history"):
                            try:
                                hist = api.single_order_history(orderno=self.tp_order_id)
                                filled, fill_px = self._check_hist_filled(hist)
                                if filled and fill_px > 0:
                                    actual_fill_px = fill_px
                                    logger.info(f"🎉 [AegisEM] Resting TP {self.tp_order_id} verified FILLED @ {actual_fill_px:.2f}")
                            except Exception as e_tp:
                                logger.debug(f"[AegisEM] Error checking TP order in 0B: {e_tp}")
                    self.last_tp_order_id = self.tp_order_id
                    self.tp_order_id = None
                    self._close_all_legs(
                        f"Take-Profit Hit (+{gain_pct:.1f}% @ {actual_fill_px:.2f})",
                        is_tp_maker=True,
                        limit_fill_price=actual_fill_px,
                    )
                    return

        # 0C. Tick-Cadence Option Premium Hard SL (-12.0% disaster floor).
        # Aborts the millisecond the option bid drops >= 12.0% under entry (disaster circuit breaker).
        for leg in legs:
            if leg.status != "OPEN" or leg.entry_price <= 0:
                continue
            eff_px = live_bid if live_bid > 0 else leg.current_price
            if eff_px > 0:
                loss_frac = (eff_px - leg.entry_price) / leg.entry_price
                if loss_frac <= -PREMIUM_HARD_SL_PCT:
                    self._close_all_legs(
                        f"Premium Hard SL (-{PREMIUM_HARD_SL_PCT*100:.1f}% disaster floor: {loss_frac*100:.1f}% @ {eff_px:.2f})"
                    )
                    return

        # 1. Force flatten at session end
        if self.must_force_flatten(now):
            self._close_all_legs(f"Force Flatten (>= {AEGIS_FORCE_FLATTEN.strftime('%H:%M')})")
            return

        # 2. Dynamic Time Stop (Point 2: 0DTE Dynamic Time-Decay Shield)
        if context.entry_time:
            hold_sec = (now - context.entry_time).total_seconds()
            leg0 = legs[0] if legs else None
            eff_bid = live_bid if live_bid > 0 else (leg0.current_price if leg0 else 0.0)
            cur_gain_pct = ((eff_bid - leg0.entry_price) / leg0.entry_price) if (leg0 and leg0.entry_price > 0) else 0.0

            # Dynamic Max Hold Time Configuration:
            # - Non-expiry: 300s baseline (extendable to 360s for running winners)
            # - Expiry Days (Before 12:30 PM): 240s (4 min strict)
            # - Expiry Days (At or After 12:30 PM): 180s (3 min strict theta compression)
            is_expiry = self._is_active_instrument_expiry(now, leg0)
            is_post_1230 = now.time() >= dt_time(12, 30)
            if is_expiry:
                dynamic_max_hold = 180.0 if is_post_1230 else 240.0
            else:
                dynamic_max_hold = MAX_HOLD_SEC  # 300.0s

            # Point 2: Conditional Stagnation Trap Check at 90s (Expiry session after 12:30 PM)
            if is_expiry and is_post_1230 and hold_sec >= 90.0:
                if cur_gain_pct < 0.005:  # Gain < +0.5%
                    tb_engine = getattr(self.aegis_engine, "time_bar_engine", None)
                    market_struct = tb_engine.get_market_structure(context.signal_type) if (tb_engine and hasattr(tb_engine, "get_market_structure")) else "NEUTRAL"
                    live_50, _ = getattr(tb_engine, "get_live_50_ema_and_slope", lambda: (0.0, 0.0))() if tb_engine else (0.0, 0.0)
                    is_opposed = (market_struct == "OPPOSED")
                    if live_50 > 0 and curr_spot > 0:
                        if context.signal_type == "CALL" and curr_spot < live_50:
                            is_opposed = True
                        elif context.signal_type == "PUT" and curr_spot > live_50:
                            is_opposed = True
                    if is_opposed:
                        self._close_all_legs(
                            f"0DTE_STAGNATION_TRAP_EXIT ({int(hold_sec)}s, gain {cur_gain_pct*100:+.1f}%, 50-EMA opposed)"
                        )
                        return

            # Check eligibility for 6-minute extension (Only non-expiry days or strong running momentum):
            tb_engine = getattr(self.aegis_engine, "time_bar_engine", None) if self.aegis_engine else None
            market_struct = tb_engine.get_market_structure(context.signal_type) if (tb_engine and hasattr(tb_engine, "get_market_structure")) else "NEUTRAL"

            is_profit_running = False
            if leg0 and leg0.entry_price > 0:
                is_profit_running = (
                    cur_gain_pct >= HOLD_EXTEND_MIN_PROFIT_PCT
                    or getattr(leg0, "breakeven_locked", False)
                    or getattr(leg0, "runner_locked", False)
                )

            can_extend_to_6min = is_profit_running and (market_struct != "OPPOSED") and not (is_expiry and is_post_1230)

            if hold_sec >= MAX_HOLD_EXTENDED_SEC:
                self._close_all_legs(f"6-Min Extended Time Stop ({int(hold_sec)}s)")
                return
            elif hold_sec >= dynamic_max_hold:
                if can_extend_to_6min and hold_sec < MAX_HOLD_EXTENDED_SEC:
                    if not getattr(context, "_extended_to_6m_logged", False):
                        gain_pct_disp = cur_gain_pct * 100.0
                        logger.info(
                            f"[AegisEM] Trade running in profit (+{gain_pct_disp:.1f}%) with favorable structure "
                            f"at {int(dynamic_max_hold)}s -> Extending time ceiling to 360s"
                        )
                        context._extended_to_6m_logged = True
                else:
                    if is_expiry:
                        expiry_tag = f"0DTE {'Afternoon' if is_post_1230 else 'Morning'} "
                        self._close_all_legs(f"{expiry_tag}Time Stop ({int(hold_sec)}s, ceiling {int(dynamic_max_hold)}s)")
                    else:
                        self._close_all_legs(f"5-Min Hard Time Stop ({int(hold_sec)}s)")
                    return

        # 3. Spot Adverse Movement SL (Normalized Basis Points + 1m ATR regime expansion)
        tb_engine = getattr(self.aegis_engine, "time_bar_engine", None) if self.aegis_engine else None
        atr_1m = getattr(tb_engine, "current_atr_1m", 0.0) if tb_engine else 0.0
        ref_spot = context.index_entry if context.index_entry > 0 else (curr_spot if curr_spot > 0 else 24000.0)
        base_spot_sl_pts = ref_spot * (DEFAULT_SPOT_SL_BPS / 10000.0)
        spot_sl_pts = max(base_spot_sl_pts, SPOT_SL_ATR_MULT * atr_1m) if atr_1m > 0 else base_spot_sl_pts

        # 3A. Precise Price Action Wick Invalidation Stop-Loss (User Rule: Candle High/Low +/- 5 pts buffer)
        spot_wick_sl = float(getattr(context, "spot_sl_price", 0.0) or 0.0)
        if spot_wick_sl > 0 and curr_spot > 0:
            pa_name = getattr(context, "pa_setup_name", "") or "50 EMA Pullback"
            if context.signal_type == "PUT" and curr_spot >= spot_wick_sl:
                c_high = getattr(context, "candle_high", 0.0)
                self._close_all_legs(
                    f"{pa_name} Wick SL Hit (Spot {curr_spot:.1f} >= SL {spot_wick_sl:.1f} [Candle High {c_high:.1f} + buffer])"
                )
                return
            elif context.signal_type == "CALL" and curr_spot <= spot_wick_sl:
                c_low = getattr(context, "candle_low", 0.0)
                self._close_all_legs(
                    f"{pa_name} Wick SL Hit (Spot {curr_spot:.1f} <= SL {spot_wick_sl:.1f} [Candle Low {c_low:.1f} - buffer])"
                )
                return

        # 3B. Spot Adverse Movement SL (Normalized Basis Points + 1m ATR regime expansion)
        if curr_spot > 0 and context.index_entry > 0:
            if context.signal_type == "CALL" and curr_spot <= (context.index_entry - spot_sl_pts):
                self._close_all_legs(f"Spot Adverse Breakdown SL ({curr_spot:.1f} vs entry {context.index_entry:.1f}, -{spot_sl_pts:.1f}pts [{(spot_sl_pts/ref_spot)*10000:.1f}bps])")
                return
            elif context.signal_type == "PUT" and curr_spot >= (context.index_entry + spot_sl_pts):
                self._close_all_legs(f"Spot Adverse Rally SL ({curr_spot:.1f} vs entry {context.index_entry:.1f}, +{spot_sl_pts:.1f}pts [{(spot_sl_pts/ref_spot)*10000:.1f}bps])")
                return

        # 3C. Unconditional Spot Adverse Trend Invalidation Exit
        # Cuts trades immediately upon trend reversal or counter-trend swing violation
        if tb_engine and curr_spot > 0:
            if hasattr(tb_engine, "get_live_ema_and_slope"):
                live_ema_1m, live_slope_1m_bps, _, _ = tb_engine.get_live_ema_and_slope()
            else:
                live_ema_1m, live_slope_1m_bps = 0.0, 0.0
            live_ema_50, live_slope_50_bps = getattr(tb_engine, "get_live_50_ema_and_slope", lambda: (0.0, 0.0))()
            market_struct = tb_engine.get_market_structure(context.signal_type) if hasattr(tb_engine, "get_market_structure") else "NEUTRAL"

            trend_flipped = False
            flip_reason = ""

            # Completed 1m Candle Invalidation Exit (Pillar 2: 9 EMA Structural Exit)
            bars_1m = getattr(tb_engine, "bars_1m", None)
            if bars_1m and context.entry_time:
                last_bar = bars_1m[-1]
                bar_ts = getattr(last_bar, "ts_end", getattr(last_bar, "timestamp", 0.0))
                if bar_ts >= context.entry_time.timestamp():
                    if context.signal_type == "CALL" and last_bar.close < live_ema_1m:
                        trend_flipped = True
                        flip_reason = f"Spot 1m Bar Closed Below 9-EMA ({last_bar.close:.1f} < {live_ema_1m:.1f})"
                    elif context.signal_type == "PUT" and last_bar.close > live_ema_1m:
                        trend_flipped = True
                        flip_reason = f"Spot 1m Bar Closed Above 9-EMA ({last_bar.close:.1f} > {live_ema_1m:.1f})"

            if not trend_flipped:
                if context.signal_type == "PUT":
                    if live_ema_50 > 0 and curr_spot > (live_ema_50 + 5.0) and live_slope_50_bps > 0.5:
                        trend_flipped = True
                        flip_reason = f"Spot broke above 50-EMA ({curr_spot:.1f} > {live_ema_50:.1f}, slope +{live_slope_50_bps:.1f}bps)"
                    elif curr_spot > live_ema_1m and live_slope_1m_bps > 1.0:
                        trend_flipped = True
                        flip_reason = f"Spot crossed above 9-EMA ({curr_spot:.1f} > {live_ema_1m:.1f}, slope +{live_slope_1m_bps:.1f}bps)"
                    elif market_struct == "OPPOSED":
                        trend_flipped = True
                        flip_reason = "1m Market Structure OPPOSED (Higher Highs/Lows)"
                elif context.signal_type == "CALL":
                    if live_ema_50 > 0 and curr_spot < (live_ema_50 - 5.0) and live_slope_50_bps < -0.5:
                        trend_flipped = True
                        flip_reason = f"Spot broke below 50-EMA ({curr_spot:.1f} < {live_ema_50:.1f}, slope {live_slope_50_bps:.1f}bps)"
                    elif curr_spot < live_ema_1m and live_slope_1m_bps < -1.0:
                        trend_flipped = True
                        flip_reason = f"Spot crossed below 9-EMA ({curr_spot:.1f} < {live_ema_1m:.1f}, slope {live_slope_1m_bps:.1f}bps)"
                    elif market_struct == "OPPOSED":
                        trend_flipped = True
                        flip_reason = "1m Market Structure OPPOSED (Lower Highs/Lows)"

            if trend_flipped:
                pnl_str = ""
                if legs and legs[0].entry_price > 0:
                    eff_bid = live_bid if live_bid > 0 else legs[0].current_price
                    pnl_pct = ((eff_bid - legs[0].entry_price) / legs[0].entry_price) * 100.0
                    pnl_str = f" | {pnl_pct:+.1f}% @ {eff_bid:.2f}"
                self._close_all_legs(f"Trend Reversal Exit ({flip_reason}{pnl_str})")
                return

        # ─── Adaptive Quantitative Microstructure Exits ───
        for leg in legs:
            if leg.status != "OPEN" or leg.entry_price <= 0:
                continue

            highest = getattr(leg, "highest_price", leg.entry_price)
            peak_gain_rs = highest - leg.entry_price
            effective_bid = live_bid if live_bid > 0 else leg.current_price
            cur_pnl_rs = effective_bid - leg.entry_price
            cur_pnl_pct = (cur_pnl_rs / leg.entry_price) * 100.0
            peak_pnl_pct = (peak_gain_rs / leg.entry_price) * 100.0
            noise_floor = self._compute_noise_floor(leg, live_bid, live_ask)
            hold_sec = (now - context.entry_time).total_seconds() if context.entry_time else 0.0

            # ─── Point 4 Gate 2: 3-Minute Harvest Exit (+1.5% to +2.5%) ───
            # If trade has spent >= 180s and profit is hovering in +1.5% to +2.5%,
            # lock in gains immediately to prevent late-bar theta melt and reversals.
            if hold_sec >= 180.0 and 1.5 <= cur_pnl_pct <= 2.5:
                self._close_all_legs(
                    f"TIME_HARVEST_LOCKED_GAIN (+{cur_pnl_pct:.1f}% after {int(hold_sec)}s @ {effective_bid:.2f})"
                )
                return

            # ─── Point 4: Stepped Risk Compression + Cost-Shield + Near-Target Engine (V4.1) ───
            # Scale-Invariant Normalized Percentage Model (strictly % normalized, no fixed paise!)
            peak_bid = getattr(leg, "highest_bid", effective_bid)
            peak_bid_gain = max(peak_gain_rs, peak_bid - leg.entry_price)
            peak_pnl_frac = peak_bid_gain / leg.entry_price if leg.entry_price > 0 else 0.0
            cur_pnl_frac = cur_pnl_rs / leg.entry_price if leg.entry_price > 0 else 0.0
            peak_bid_frac = (peak_bid - leg.entry_price) / leg.entry_price if leg.entry_price > 0 else 0.0

            # 1. Near-Target Profit Lock (Peak Bid >= +3.5%, exits on >= 0.5% pullback from peak bid)
            if peak_bid_frac >= NEAR_TARGET_TRIGGER_PCT:
                pullback_frac = peak_bid_frac - cur_pnl_frac
                if pullback_frac >= NEAR_TARGET_PULLBACK_TOLERANCE:
                    self._close_all_legs(
                        f"Near-Target Profit Lock (+{cur_pnl_pct:.1f}% @ {effective_bid:.2f} | Peak was +{peak_pnl_pct:.1f}%)"
                    )
                    return

            # 2. Dynamic SL & Cost-Shield Level Evaluation
            dynamic_sl_price = None
            dynamic_sl_reason = ""

            if peak_pnl_frac >= STEP2_TRIGGER_PCT or getattr(leg, "breakeven_locked", False):
                # Step 2: Peak reached >= +3.0% -> Cost-Shield Scratch Active (SL = Entry Price / 0.0%)
                if not getattr(leg, "breakeven_locked", False):
                    leg.breakeven_locked = True
                    logger.info(
                        f"🛡️ [AegisEM] Cost-Shield Scratch Armed: {leg.symbol} peak +{peak_pnl_pct:.1f}% (₹{peak_gain_rs:.2f}) -> "
                        f"SL locked at Entry Price ₹{leg.entry_price:.2f} (0.0% Scratch / Loss Prevented)"
                    )
                dynamic_sl_price = _round_tick(leg.entry_price * (1.0 + STEP2_SL_PCT))  # 0.0%
                dynamic_sl_reason = (
                    f"Cost-Shield Scratch Exit (Breakeven Guard 0.0% @ {effective_bid:.2f} | "
                    f"Peak was +{peak_pnl_pct:.1f}% | Loss Prevented!)"
                )
            elif peak_pnl_frac >= STEP1_TRIGGER_PCT or getattr(leg, "step1_sl_armed", False):
                # Step 1: Peak reached >= +2.0% -> Stepped Safety Net Active (SL tightened from -12% to -3.0%)
                if not getattr(leg, "step1_sl_armed", False):
                    leg.step1_sl_armed = True
                    logger.info(
                        f"🛡️ [AegisEM] Stepped Safety Net Armed: {leg.symbol} peak +{peak_pnl_pct:.1f}% -> "
                        f"SL tightened to -3.0% (₹{_round_tick(leg.entry_price * (1.0 + STEP1_SL_PCT)):.2f}, 5% cushion)"
                    )
                dynamic_sl_price = _round_tick(leg.entry_price * (1.0 + STEP1_SL_PCT))  # -3.0%
                dynamic_sl_reason = (
                    f"Stepped Safety Net SL ({cur_pnl_pct:+.1f}% @ {effective_bid:.2f} | "
                    f"Peak was +{peak_pnl_pct:.1f}%)"
                )

            # 3. Dynamic SL Trigger Check
            if dynamic_sl_price is not None and effective_bid > 0 and effective_bid <= dynamic_sl_price:
                self._close_all_legs(dynamic_sl_reason)
                return

            # 5. Dynamic Runner Trailing (legacy — disabled in V4 single-target mode)
            if V4_SINGLE_TARGET_MODE:
                runner_hurdle_met = False
            else:
                runner_hurdle_met = (peak_gain_rs >= RUNNER_ARM_NOISE_MULT * noise_floor) or ((peak_gain_rs / leg.entry_price) >= 0.15)
            if runner_hurdle_met:
                leg.runner_locked = True

            if getattr(leg, "runner_locked", False):
                trail_dist = max(noise_floor * 1.5, RUNNER_TRAIL_RATIO * peak_gain_rs)
                runner_sl_price = _round_tick(highest - trail_dist)
                runner_sl_price = max(runner_sl_price, _round_tick(leg.entry_price + BREAKEVEN_LOCK_BUFFER_RS))

                # Dynamic trailing floor
                if effective_bid > 0 and effective_bid <= runner_sl_price:
                    pnl_rs = effective_bid - leg.entry_price
                    pnl_pct = (pnl_rs / leg.entry_price) * 100.0
                    self._close_all_legs(f"Runner Dynamic Trail Exit (+{pnl_pct:.1f}% @ {effective_bid:.2f})")
                    return

            # 6. Spot-Driven Invalidation Stop-Loss (Derived from Spot SL pts & Realized Delta)
            realized_delta = self._compute_realized_delta(context, leg, curr_spot)
            opt_sl_dist = max(noise_floor, realized_delta * spot_sl_pts)
            # Emergency max SL cap (35%)
            max_sl_dist = leg.entry_price * EMERGENCY_MAX_SL_PCT
            effective_sl_dist = min(opt_sl_dist, max_sl_dist)
            effective_sl_price = _round_tick(leg.entry_price - effective_sl_dist)

            if effective_bid > 0 and effective_bid <= effective_sl_price:
                loss_rs = leg.entry_price - effective_bid
                loss_pct = (loss_rs / leg.entry_price) * 100.0
                self._close_all_legs(
                    f"Adaptive Spot Invalidation SL (-{loss_pct:.1f}% @ {effective_bid:.2f}, -₹{loss_rs:.2f} | "
                    f"Δ={realized_delta:.2f}, Floor=₹{noise_floor:.2f})"
                )
                return


    def _close_all_legs(self, reason: str, is_tp_maker: bool = False, limit_fill_price: float = 0.0):
        with self.trade_lock:
            if not self.has_open_trade() or getattr(self, "_is_exiting", False):
                return
            self._is_exiting = True
            if self.trade_context:
                self.trade_context.status = "EXIT_PENDING"
        
        threading.Thread(
            target=self._chase_exit_thread,
            args=(reason, is_tp_maker, limit_fill_price),
            daemon=True,
        ).start()

    def _chase_exit_thread(self, reason: str, is_tp_maker: bool = False, limit_fill_price: float = 0.0):
        """Limit exit execution — preserves TP maker fills and chases emergency exits."""
        try:
            self._do_chase_exit(reason, is_tp_maker, limit_fill_price)
        finally:
            with self.trade_lock:
                self._is_exiting = False

    def _do_chase_exit(self, reason: str, is_tp_maker: bool = False, limit_fill_price: float = 0.0):
        params = self.get_instrument_params()
        exch_seg = params["exchange"]
        
        with self.trade_lock:
            if not self.trade_context:
                return
            legs = list(self.trade_context.legs)
            context = self.trade_context
        
        api = getattr(self.broker, "_api", None)
        total_pnl = 0.0
        now = datetime.now()
        
        for leg in legs:
            if leg.status != "OPEN":
                continue
            
            if leg.quantity <= 0:
                continue

            # Anchor to live bid
            try:
                q = self.broker.market_data_provider.get_quote(exch_seg, leg.token)
                if q and float(q.get("bp1") or 0) > 0:
                    leg.current_price = float(q["bp1"])
            except Exception:
                pass

            qty = str(leg.quantity * params["lot_size"])
            fill_price = 0.0
            fill_ts = time.time()
            filled = False

            # Case A: Passive Take-Profit Maker Fill (Preserve 100% of profit without discount!)
            # NOTE: tp_order_id may already be None here (cleared in Section 0B / _cancel_tp)
            # but last_tp_order_id holds the actual broker orderno we must verify against.
            if is_tp_maker and limit_fill_price > 0:
                if str(getattr(self, "tp_order_id", "")).startswith("PAPER_") or \
                   str(getattr(self, "last_tp_order_id", "")).startswith("PAPER_"):
                    filled = True
                    fill_price = limit_fill_price
                    logger.info(f"[AegisEM] Paper TP fill accepted @ {fill_price:.2f}")
                else:
                    # Check live order — prefer current tp_order_id, fall back to last_tp_order_id
                    check_ordno = self.tp_order_id or self.last_tp_order_id
                    if check_ordno and api:
                        try:
                            time.sleep(0.5)
                            if hasattr(api, "single_order_history"):
                                hist = api.single_order_history(orderno=check_ordno)
                                hist_filled, hist_fill_px = self._check_hist_filled(hist)
                                if hist_filled:
                                    filled = True
                                    fill_price = hist_fill_px if hist_fill_px > 0 else limit_fill_price
                                    logger.info(
                                        f"🎉 [AegisEM] Case A: TP order {check_ordno} CONFIRMED FILLED @ "
                                        f"{fill_price:.2f} (limit_fill_price={limit_fill_price:.2f})"
                                    )
                                else:
                                    logger.info(
                                        f"[AegisEM] Case A: TP order {check_ordno} hist check unconfirmed "
                                        f"— accepting limit_fill_price={limit_fill_price:.2f} as fill reference"
                                    )
                                    filled = True
                                    fill_price = limit_fill_price
                        except Exception as e_tp:
                            logger.warning(f"[AegisEM] Maker TP status check warning: {e_tp}")
                    
                    if not filled:
                        # Bid >= TP price already confirmed in Section 0B — treat as maker fill
                        logger.info(
                            f"[AegisEM] Case A fallback: no ordno available but bid >= tp_price, "
                            f"accepting limit_fill_price={limit_fill_price:.2f}"
                        )
                        filled = True
                        fill_price = limit_fill_price

            # Case B: Emergency or Trail Exit Chase (Anchored to live bid: Bid-0% -> Bid-1% -> Bid-2%)
            def _get_live_bid(ref_px: float) -> float:
                try:
                    if hasattr(self.broker, "market_data_provider"):
                        q = self.broker.market_data_provider.get_quote(exch_seg, leg.token)
                        if q and float(q.get("bp1") or 0) > 0:
                            return float(q["bp1"])
                except Exception:
                    pass
                return ref_px

            if not filled:
                # Pre-chase safety check: Did the resting TP order already fill at broker?
                check_ordno = self.tp_order_id or self.last_tp_order_id
                if check_ordno and not str(check_ordno).startswith("PAPER_") and api and hasattr(api, "single_order_history"):
                    try:
                        hist = api.single_order_history(orderno=check_ordno)
                        hist_filled, hist_fill_px = self._check_hist_filled(hist)
                        if hist_filled:
                            filled = True
                            fill_price = hist_fill_px if hist_fill_px > 0 else (getattr(leg, "tp_price", 0.0) or leg.current_price)
                            logger.info(
                                f"🎉 [AegisEM] Resting TP order {check_ordno} was already FILLED at broker @ {fill_price:.2f}! "
                                f"Skipping chase exit (position already flat)."
                            )
                    except Exception as e_hist:
                        logger.warning(f"[AegisEM] Pre-chase TP check warning: {e_hist}")

            if not filled:
                # Step 1: Anchor to fresh live bid (marketable fill)
                live_bid_1 = _get_live_bid(leg.current_price)
                price_1 = _round_tick(get_marketable_exit_limit_price(live_bid_1)) if live_bid_1 > 0 else _round_tick(leg.current_price * 0.99)
                ordno = None
                
                if self.tp_order_id and not str(self.tp_order_id).startswith("PAPER_"):
                    ordno = self.tp_order_id
                    if api and hasattr(api, "modify_order"):
                        try:
                            logger.info(
                                f"[AegisEM] ⚡ ATOMIC IN-PLACE EXIT: Modifying resting TP order {ordno} -> "
                                f"LMT @ {price_1:.2f} (live_bid={live_bid_1:.2f}) for instant fill"
                            )
                            resp = api.modify_order(
                                exchange=exch_seg,
                                tradingsymbol=leg.symbol,
                                orderno=ordno,
                                newquantity=qty,
                                newprice_type="LMT",
                                newprice=f"{price_1:.2f}",
                            )
                            logger.info(f"[AegisEM] modify_order response: {resp}")
                            if isinstance(resp, dict) and resp.get("stat") != "Ok":
                                emsg = str(resp.get("emsg", "")).lower()
                                if "complete" in emsg or "filled" in emsg:
                                    logger.info(f"[AegisEM] Resting TP order {ordno} already filled at broker: {resp}")
                                    filled = True
                                    fill_price = getattr(leg, "tp_price", price_1)
                                else:
                                    logger.warning(f"[AegisEM] modify_order failed: {resp}. Cancelling before fallback place_order.")
                                    self._cancel_tp()
                                    ordno = None
                        except Exception as e_mod:
                            logger.error(f"[AegisEM] Exception in modify_order: {e_mod}. Cancelling before fallback place_order.")
                            self._cancel_tp()
                            ordno = None
                
                if not ordno and not filled:
                    if api:
                        if hasattr(self.order_lifecycle, "_build_kwargs"):
                            kwargs = self.order_lifecycle._build_kwargs("S", exch_seg, leg.symbol, qty, f"{price_1:.2f}")
                        else:
                            kwargs = {
                                "buy_or_sell": "S",
                                "product_type": "M",
                                "exchange": exch_seg,
                                "tradingsymbol": leg.symbol,
                                "quantity": qty,
                                "price_type": "LMT",
                                "price": f"{price_1:.2f}",
                            }
                        place = getattr(api, "place_order", None)
                        if place:
                            resp = place(**kwargs)
                            if isinstance(resp, dict) and resp.get("stat") == "Ok":
                                ordno = resp.get("norenordno")
                
                if ordno:
                    time.sleep(0.8)
                    history = api.single_order_history(orderno=ordno) if hasattr(api, "single_order_history") else []
                    h_filled, h_px = self._check_hist_filled(history)
                    if h_filled:
                        filled = True
                        fill_price = h_px if h_px > 0 else price_1
                    else:
                        # Step 2: Refresh live bid and chase 1% below current bid
                        live_bid_2 = _get_live_bid(price_1)
                        price_2 = _round_tick(get_marketable_exit_limit_price(live_bid_2 * 0.99)) if live_bid_2 > 0 else _round_tick(leg.current_price * 0.98)
                        if api and hasattr(api, "modify_order"):
                            api.modify_order(exchange=exch_seg, tradingsymbol=leg.symbol, orderno=ordno,
                                             newquantity=qty, newprice_type="LMT", newprice=f"{price_2:.2f}")
                        time.sleep(1.0)
                        history = api.single_order_history(orderno=ordno) if hasattr(api, "single_order_history") else []
                        h_filled, h_px = self._check_hist_filled(history)
                        if h_filled:
                            filled = True
                            fill_price = h_px if h_px > 0 else price_2
                        else:
                            # Step 3: Cancel & Replace at aggressive exit 2% below fresh live bid
                            if api and hasattr(api, "cancel_order"):
                                api.cancel_order(orderno=ordno)
                            live_bid_3 = _get_live_bid(price_2)
                            price_3 = _round_tick(min(live_bid_3 * 0.98, leg.current_price * 0.97))
                            if api and hasattr(self.order_lifecycle, "_build_kwargs"):
                                kwargs = self.order_lifecycle._build_kwargs("S", exch_seg, leg.symbol, qty, f"{price_3:.2f}")
                                place = getattr(api, "place_order", None)
                                if place:
                                    logger.warning(f"[AegisEM Chaser] Final aggressive exit @ {price_3:.2f} (live_bid={live_bid_3:.2f})")
                                    resp = place(**kwargs)
                                    ordno_3 = resp.get("norenordno") if isinstance(resp, dict) and resp.get("stat") == "Ok" else None
                                    if ordno_3:
                                        time.sleep(0.8)
                                        history = api.single_order_history(orderno=ordno_3) if hasattr(api, "single_order_history") else []
                                        h_filled, h_px = self._check_hist_filled(history)
                                        if h_filled:
                                            fill_price = h_px if h_px > 0 else price_3
                                            filled = True
                                            fill_ts = time.time()
                                        else:
                                            latest = history[0] if history and isinstance(history, list) and isinstance(history[0], dict) else {}
                                            status = latest.get("status", "OPEN")
                                            logger.error(f"[AegisEM Chaser] Step-3 exit order {ordno_3} NOT filled (status={status}) — position may still be open at broker")
                                    else:
                                        logger.error(f"[AegisEM Chaser] Step-3 aggressive exit order placement failed: {resp}")
            
            if not filled:
                # Fallback
                self.order_lifecycle.execute_exit(leg, reason)
                fill_price = leg.exit_price if leg.exit_price and leg.exit_price > 0 else leg.current_price
                fill_ts = time.time()
            
            # ─── CRITICAL: Feed fill back to AEGIS for latency decay ───
            if self.aegis_engine:
                signal_price = leg.entry_price
                dp = getattr(context, "decision_payload", None) or {}
                signal_ts = float(dp.get("signal_ts") or 0.0)
                if signal_ts <= 0:
                    signal_ts = context.entry_time.timestamp() if context.entry_time else fill_ts
                try:
                    self.aegis_engine.on_fill(
                        sym=context.legs[0].symbol if context.legs else leg.symbol,
                        token=leg.token,
                        fill_price=fill_price,
                        fill_ts=fill_ts,
                        signal_price=signal_price,
                        signal_ts=signal_ts
                    )
                except Exception as e:
                    # Feedback must never break trade bookkeeping below
                    logger.error(f"[AegisEM] on_fill feedback failed (non-fatal): {e}")
            
            # Record PnL
            with self.trade_lock:
                leg.status = "CLOSED"
                leg.exit_time = datetime.now()
                leg.exit_price = fill_price
                leg.pnl = (fill_price - leg.entry_price) * leg.quantity * params["lot_size"]
                total_pnl += leg.pnl
                self.capital_engine.add_funds(fill_price * leg.quantity * params["lot_size"])
                self.strike_cooldowns[leg.symbol] = datetime.now() + timedelta(minutes=3)
                logger.info(f"[AegisEM] CLOSED {leg.symbol}: {leg.entry_price:.2f} → {fill_price:.2f} | PnL {leg.pnl:.2f} | {reason}")

        with self.trade_lock:
            context.status = "CLOSED"
            context.exit_time = datetime.now()
            self._append_history(reason, total_pnl)
            if self.aegis_engine and getattr(self.aegis_engine, "chop_gate", None):
                try:
                    self.aegis_engine.chop_gate.register_trade_close(time.time())
                except Exception:
                    pass
            if self.trade_context == context:
                self.trade_context = None
            self.tp_order_id = None
            self.last_tp_order_id = None
            self._save_active_context()

    def _append_history(self, reason: str, total_pnl: float):
        hist = []
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, "r") as f:
                    hist = json.load(f)
            except Exception:
                hist = []
        
        context = self.trade_context
        leg0 = context.legs[0] if context and context.legs else None
        now = datetime.now()
        entry_dt_str = context.entry_time.strftime("%d %b %Y %H:%M:%S") if context and context.entry_time else now.strftime("%d %b %Y %H:%M:%S")
        exit_dt_str = now.strftime("%d %b %Y %H:%M:%S")

        # Ghost-trade dedup guard: same (entry_time, symbol) already recorded -> skip BEFORE
        # any state mutation (daily_pnl / cooldown / history / telemetry) to avoid double count.
        dup_sym = leg0.symbol if leg0 else ""
        for existing in hist:
            if existing.get("entry_time") == entry_dt_str and existing.get("symbol") == dup_sym:
                logger.warning(
                    f"[AegisEM] Duplicate close detected ({entry_dt_str} {dup_sym}) — "
                    f"history append skipped (ghost-trade guard)"
                )
                return

        self.daily_pnl += total_pnl
        now_epoch = time.time()
        cooldown_interval = int(now_epoch - self.last_trade_exit_time) if self.last_trade_exit_time > 0 else 0
        self.last_trade_exit_time = now_epoch
        self.last_trade_loss = (total_pnl < 0)

        # Quality Stack: consecutive-loss tracking + engine cross-wire
        if total_pnl < 0:
            self._consec_losses = int(getattr(self, "_consec_losses", 0)) + 1
        else:
            self._consec_losses = 0
        if self.aegis_engine is not None:
            try:
                self.aegis_engine._em_consec_losses = self._consec_losses
                self.aegis_engine._em_daily_pnl = self.daily_pnl
            except Exception:
                pass
        dp = dict(context.decision_payload) if context and context.decision_payload else {}
        self.last_exited_route = str(getattr(context, "pa_setup_name", "") or dp.get("pa_setup_name", "") or "")
        if self.last_exited_route:
            logger.info(
                f"[AegisEM] Exited route '{self.last_exited_route}' -> Strict non-consecutive lockout armed (setup blocked until alternate route triggers)"
            )
        post_loss_sec = getattr(self, "post_loss_cooloff_sec", 120.0)
        inter_trade_sec = getattr(self, "inter_trade_cooldown_sec", 60.0)
        if self.last_trade_loss:
            logger.info(f"[AegisEM] Trade closed in loss (₹{total_pnl:.2f}) -> Post-loss cooloff active for {int(post_loss_sec)}s")
        else:
            logger.info(f"[AegisEM] Trade closed in profit (₹{total_pnl:.2f}) -> Inter-trade cooldown active for {int(inter_trade_sec)}s")
        
        # ─── Post-Trade Quantitative Feature Attribution ───
        duration_sec = int((context.exit_time - context.entry_time).total_seconds()) if (context and context.entry_time and context.exit_time) else int((now - context.entry_time).total_seconds()) if (context and context.entry_time) else 0
        entry_px = float(leg0.entry_price) if leg0 else 0.0
        exit_px = float(leg0.exit_price) if (leg0 and leg0.exit_price is not None) else 0.0
        pnl_pct = round(((exit_px - entry_px) / entry_px * 100.0), 2) if entry_px > 0 else 0.0

        highest_px = float(getattr(leg0, "highest_price", entry_px)) if leg0 else entry_px
        lowest_px = float(getattr(leg0, "lowest_price", entry_px)) if leg0 else entry_px
        peak_mfe_pct = round(max(0.0, ((highest_px - entry_px) / entry_px * 100.0)), 2) if entry_px > 0 else 0.0
        peak_mfe_rs = round(max(0.0, highest_px - entry_px), 2)
        time_to_peak_mfe_sec = round(float(getattr(leg0, "time_to_highest_sec", 0.0)), 1) if leg0 else 0.0
        peak_mae_pct = round(min(0.0, ((lowest_px - entry_px) / entry_px * 100.0)), 2) if entry_px > 0 else 0.0
        peak_mae_rs = round(min(0.0, lowest_px - entry_px), 2)

        entry_spot = float(context.index_entry) if context else 0.0
        exit_spot = float(self._get_current_spot())
        spot_delta_pts = round(exit_spot - entry_spot, 2) if (entry_spot > 0 and exit_spot > 0) else 0.0
        spot_delta_bps = round((spot_delta_pts / entry_spot) * 10000.0, 2) if entry_spot > 0 else 0.0

        entry_fut_px = float(dp.get("futures_price", 0.0) or 0.0)
        exit_fut_px = float(getattr(self.aegis_engine, "last_futures_price", 0.0) if self.aegis_engine else 0.0)
        fut_delta_pts = round(exit_fut_px - entry_fut_px, 2) if (entry_fut_px > 0 and exit_fut_px > 0) else 0.0
        basis_pts_entry = float(dp.get("basis_pts", 0.0) or 0.0)
        basis_bps_entry = float(dp.get("basis_bps", 0.0) or 0.0)

        entry_latency_ms = float(dp.get("entry_latency_ms", 0.0) or 0.0)
        vol_burst_age_sec = float(dp.get("volatility_burst_age_sec", 0.0) or 0.0)
        trigger_age_sec = float(dp.get("trigger_age_sec", 0.0) or 0.0)
        eqs_score = float(dp.get("eqs_score", 0.0) or 0.0)
        micro_score = float(dp.get("microstructure_score", 0.0) or dp.get("conviction", 0.0) or 0.0)
        persistence_score = float(dp.get("persistence_score", 0.0) or 0.0)

        is_win = total_pnl > 0
        is_scratch = total_pnl == 0
        outcome = "WINNER" if is_win else ("SCRATCH" if is_scratch else "LOSER")

        # Determine classification tag
        if "Take-Profit" in reason or "TP" in reason:
            classification = "TAKE_PROFIT_LIMIT"
        elif "Extended Time" in reason:
            classification = "EXTENDED_6MIN_TIME_EXIT"
        elif "Time Stop" in reason:
            classification = "MOMENTUM_STALL_TIME_EXIT" if is_win else "TIME_STOP_LOSS"
        elif "Trend Reversal" in reason:
            classification = "TREND_REVERSAL_EXIT"
        elif "Breakeven" in reason:
            classification = "BREAKEVEN_GUARD"
        elif "Runner" in reason:
            classification = "RUNNER_DYNAMIC_TRAIL"
        elif "SL" in reason:
            classification = "STOP_LOSS_HIT"
        elif "Force Flatten" in reason:
            classification = "FORCE_FLATTEN"
        else:
            classification = "OTHER"

        # Futures correlation assessment
        fut_corr = "NEUTRAL"
        if entry_fut_px > 0 and exit_fut_px > 0:
            signal_dir = context.signal_type if context else "CALL"
            favored_fut = (fut_delta_pts > 0 and signal_dir == "CALL") or (fut_delta_pts < 0 and signal_dir == "PUT")
            favored_spot = (spot_delta_pts > 0 and signal_dir == "CALL") or (spot_delta_pts < 0 and signal_dir == "PUT")
            if favored_fut and favored_spot:
                fut_corr = "CONFIRMED_DIRECTION"
            elif favored_fut and spot_delta_pts == 0:
                fut_corr = "CONFIRMED_DIRECTION"
            elif favored_fut and not favored_spot:
                fut_corr = "FUTURES_LEAD_SPOT_LAG"
            elif not favored_fut and favored_spot:
                fut_corr = "SPOT_DRIVEN_FUTURES_DIVERGENCE"
            elif not favored_fut and not favored_spot and (fut_delta_pts != 0 or spot_delta_pts != 0):
                fut_corr = "OPPOSED"

        sig_type = context.signal_type if context else "TRADE"
        sym_str = leg0.symbol if leg0 else "OPTION"
        diag_summary = (
            f"{outcome} {sig_type} on {sym_str} ({duration_sec}s) | "
            f"PnL: ₹{total_pnl:+.2f} ({pnl_pct:+.1f}%) | Exit: {reason} | "
            f"MFE: {peak_mfe_pct:+.1f}% (+₹{peak_mfe_rs:.2f} @ {time_to_peak_mfe_sec}s), MAE: {peak_mae_pct:.1f}% | "
            f"Spot Δ: {spot_delta_pts:+.1f}pts ({spot_delta_bps:+.1f}bps), Fut Δ: {fut_delta_pts:+.1f}pts (Basis: {basis_pts_entry:+.1f}pts | {fut_corr}) | "
            f"Lat: {entry_latency_ms:.0f}ms, VolBurstAge: {vol_burst_age_sec}s, EQS: {eqs_score:.1f}"
        )

        post_trade_attribution = {
            "outcome": outcome,
            "classification": classification,
            "pnl_rs": round(total_pnl, 2),
            "pnl_pct": pnl_pct,
            "trade_duration_sec": duration_sec,
            "exit_reason": reason,
            "peak_mfe_pct": peak_mfe_pct,
            "peak_mfe_rs": peak_mfe_rs,
            "time_to_peak_mfe_sec": time_to_peak_mfe_sec,
            "peak_mae_pct": peak_mae_pct,
            "peak_mae_rs": peak_mae_rs,
            "spot_move_pts": spot_delta_pts,
            "spot_move_bps": spot_delta_bps,
            "futures_move_pts": fut_delta_pts,
            "futures_basis_pts": basis_pts_entry,
            "futures_correlation": fut_corr,
            "entry_latency_ms": entry_latency_ms,
            "volatility_burst_age_sec": vol_burst_age_sec,
            "trigger_age_sec": trigger_age_sec,
            "time_since_last_trade_sec": cooldown_interval,
            "eqs_score": eqs_score,
            "microstructure_score": micro_score,
            "persistence_score": persistence_score,
            "diagnostic_summary": diag_summary,
        }

        trade_telemetry = {
            "impulse_time": dp.get("impulse_time_iso", ""),
            "fill_time": dp.get("fill_time_iso", ""),
            "entry_latency_ms": entry_latency_ms,
            "trade_duration_sec": duration_sec,
            "entry_spot": entry_spot,
            "exit_spot": exit_spot,
            "spot_move_pts": spot_delta_pts,
            "entry_futures": entry_fut_px,
            "exit_futures": exit_fut_px,
            "futures_move_pts": fut_delta_pts,
            "basis_pts_entry": basis_pts_entry,
            "peak_mfe_pct": peak_mfe_pct,
            "peak_mfe_rs": peak_mfe_rs,
            "time_to_peak_mfe_sec": time_to_peak_mfe_sec,
            "peak_mae_pct": peak_mae_pct,
            "peak_mae_rs": peak_mae_rs,
        }

        trade_record = {
            "date": entry_dt_str,
            "entry_time": entry_dt_str,
            "exit_time": exit_dt_str,
            "strategy": "AEGIS_GAMMA_BURST",
            "signal": context.signal_type if context else "",
            "signal_type": context.signal_type if context else "",
            "symbol": leg0.symbol if leg0 else "",
            "quantity": leg0.quantity if leg0 else 1,
            "entry_price": entry_px,
            "exit_price": exit_px,
            "pnl": total_pnl,
            "net_pl": total_pnl,
            "daily_total_pnl": self.daily_pnl,
            "exit_reason": reason,
            "reason": reason,
            "feature_vector": dp,
            "telemetry": trade_telemetry,
            "post_trade_attribution": post_trade_attribution,
            "decision_payload": dp,
        }
        hist.append(trade_record)

        try:
            with open(self.history_file, "w") as f:
                json.dump(hist, f, indent=2)
        except Exception as e:
            logger.error(f"[AegisEM] History write failed: {e}")

        # Append to daily research telemetry JSONL
        try:
            telem_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "telemetry")
            os.makedirs(telem_dir, exist_ok=True)
            telem_file = os.path.join(telem_dir, f"aegis_trade_telemetry_{now.strftime('%Y-%m-%d')}.jsonl")
            with open(telem_file, "a", encoding="utf-8") as tf:
                tf.write(json.dumps(trade_record, default=str) + "\n")
        except Exception as e_telem:
            logger.warning(f"[AegisEM] Telemetry write warning: {e_telem}")

        logger.info(f"📊 [AegisEM Post-Trade] {diag_summary}")

    def _save_active_context(self):
        try:
            if not self.trade_context or str(getattr(self.trade_context, "status", "")).upper() not in ("OPEN", "PENDING", "SUBMITTED", "EXIT_PENDING"):
                if os.path.exists(self.active_context_file):
                    try:
                        os.remove(self.active_context_file)
                    except Exception:
                        pass
                return
            payload = {
                "status": "OPEN",
                "signal_type": self.trade_context.signal_type,
                "strategy": "AEGIS_GAMMA_BURST",
                "entry_time": self.trade_context.entry_time.isoformat() if self.trade_context.entry_time else None,
                "index_entry": self.trade_context.index_entry,
                "tp_order_id": self.tp_order_id,
                "last_tp_order_id": self.last_tp_order_id,
                "legs": [
                    {
                        "token": leg.token,
                        "symbol": leg.symbol,
                        "strike": leg.strike,
                        "option_type": getattr(leg, "option_type", None) or self.trade_context.signal_type,
                        "quantity": leg.quantity,
                        "entry_price": leg.entry_price,
                        "current_price": leg.current_price,
                        "status": leg.status,
                        "initial_tp_pct": getattr(leg, "initial_tp_pct", 0.10),
                        "tp_pct": getattr(leg, "tp_pct", 0.10),
                        "tp_price": getattr(leg, "tp_price", None),
                        "highest_price": getattr(leg, "highest_price", leg.entry_price),
                        "breakeven_locked": getattr(leg, "breakeven_locked", False),
                        "step1_sl_armed": getattr(leg, "step1_sl_armed", False),
                        "runner_locked": getattr(leg, "runner_locked", False),
                    }
                    for leg in self.trade_context.legs
                ],
            }
            with open(self.active_context_file, "w") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            logger.error(f"[AegisEM] Save context failed: {e}")

    def _load_active_context(self):
        if not os.path.exists(self.active_context_file):
            return
        try:
            with open(self.active_context_file, "r") as f:
                data = json.load(f)
            if data.get("status") != "OPEN":
                return
            self.trade_context = TradeContext(
                signal_type=data.get("signal_type", "CALL"),
                strategy="AEGIS_GAMMA_BURST",
                index_entry=float(data.get("index_entry") or 0),
            )
            if data.get("entry_time"):
                entry_dt = datetime.fromisoformat(data["entry_time"])
                age_sec = (datetime.now() - entry_dt).total_seconds()
                if age_sec > MAX_HOLD_EXTENDED_SEC:
                    logger.warning(
                        f"[AegisEM] Stale active context found ({age_sec:.0f}s old > "
                        f"{MAX_HOLD_EXTENDED_SEC:.0f}s). Discarding ghost trade."
                    )
                    if os.path.exists(self.active_context_file):
                        try:
                            os.remove(self.active_context_file)
                        except Exception:
                            pass
                    self.trade_context = None
                    return
                self.trade_context.entry_time = entry_dt
            self.tp_order_id = data.get("tp_order_id")
            self.last_tp_order_id = data.get("last_tp_order_id")
            for ld in data.get("legs") or []:
                opt_type = ld.get("option_type") or ld.get("signal_type") or data.get("signal_type") or "CALL"
                leg = TradeLeg(ld["token"], ld["symbol"], float(ld["strike"]), opt_type,
                               int(ld.get("quantity") or 1), float(ld.get("entry_price") or 0))
                leg.current_price = float(ld.get("current_price") or leg.entry_price)
                leg.status = ld.get("status") or "OPEN"
                leg.initial_tp_pct = float(ld.get("initial_tp_pct") or 0.10)
                leg.tp_pct = float(ld.get("tp_pct") or leg.initial_tp_pct)
                if ld.get("tp_price"):
                    leg.tp_price = float(ld["tp_price"])
                leg.highest_price = float(ld.get("highest_price") or leg.entry_price)
                leg.breakeven_locked = bool(ld.get("breakeven_locked", False))
                leg.step1_sl_armed = bool(ld.get("step1_sl_armed", False))
                leg.runner_locked = bool(ld.get("runner_locked", False))
                self.trade_context.add_leg(leg)
            self.trade_context.status = "OPEN"
            logger.warning("[AegisEM] Restored OPEN trade from disk")
        except Exception as e:
            logger.error(f"[AegisEM] Load context failed: {e}")
            self.trade_context = None
