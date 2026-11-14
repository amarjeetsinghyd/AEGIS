# aegis_execution_manager.py
# AEGIS V1.0 — Execution Manager (Zero-Regression Mixin Orchestrator)
# Parses GAMMA_BURST_ENTRY signals, manages positions, feeds fill latency to AEGIS core

from __future__ import annotations

import json
import logging
import os
import queue
import re
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta, time as dt_time
from typing import Optional, Dict, Any, List, Tuple

import numpy as np

from logger import get_logger
from config.settings import (
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
from execution.capital_engine import CapitalEngine
from execution.order_lifecycle import (
    PaperOrderLifecycle,
    ShoonyaOrderLifecycle,
    get_marketable_entry_limit_price,
    get_marketable_exit_limit_price,
    get_smart_maker_tp_price,
)
from execution.strike_selector import StrikeSelector, effective_premium_floor
from execution.trade_context import TradeContext, TradeLeg, resolve_leg_net_qty
from execution.realistic_paper_broker import calculate_nse_option_charges
from market.market_calendar import is_trading_day
try:
    from execution.dump_detector import DumpDetector
except ImportError:
    from execution.dump_detector import DumpDetector

# ─── Re-export Slot & Risk Constants, Policies & Helpers for Backward Compatibility ───
from execution.slot_and_risk_constants import (
    QualitySlotPolicy,
    PostTriggerStabilityTracker,
    MAX_HOLD_SEC,
    MAX_HOLD_EXTENDED_SEC,
    HOLD_EXTEND_MIN_PROFIT_PCT,
    _parse_time_env,
    AEGIS_ENTRY_CUTOFF,
    AEGIS_FORCE_FLATTEN,
    MAX_TRADES_PER_DAY,
    DEFAULT_MIN_NOISE_FLOOR_RS,
    NOISE_FLOOR_SPREAD_MULT,
    NOISE_FLOOR_SIGMA_MULT,
    NOISE_FLOOR_MIN_PCT,
    RESTING_TP_LIMIT_PCT,
    FRIDAY_TP_LIMIT_PCT,
    FRIDAY_MIN_CAPITAL_RS,
    get_adaptive_target_profit_pct,
    DEFAULT_SPOT_SL_BPS,
    SPOT_SL_ATR_MULT,
    DELTA_MIN_SPOT_BPS,
    MIN_STRUCTURAL_BAR_RANGE_BPS,
    MIN_STRUCTURAL_BAR_RANGE_ATR_MULT,
    STRUCTURAL_SL_BUFFER_BPS,
    STRUCTURAL_SL_BUFFER_ATR_MULT,
    EMERGENCY_MAX_SL_PCT,
    ENABLE_PREMIUM_DISASTER_FLOOR,
    DEFAULT_PREMIUM_HARD_SL_PCT,
    PREMIUM_HARD_SL_PCT,
    FRIDAY_HARD_SL_PCT,
    STEP1_TRIGGER_PCT,
    STEP1_SL_PCT,
    STEP2_TRIGGER_PCT,
    STEP2_SL_PCT,
    NEAR_TARGET_TRIGGER_PCT,
    NEAR_TARGET_PULLBACK_TOLERANCE,
    _round_tick,
)

# ─── Import Mixin Classes ───
from execution.entry_engine import EntryEngineMixin
from execution.trade_monitor import TradeMonitorMixin
from execution.exit_chaser import ExitChaserMixin
from execution.trade_persistence import TradePersistenceMixin

logger = get_logger("aegis_execution_manager")


class AegisExecutionManager(
    EntryEngineMixin,
    TradeMonitorMixin,
    ExitChaserMixin,
    TradePersistenceMixin,
):
    """
    Execution layer for AEGIS Production v1.0.
    - Enters on AEGIS signals (5 Macro Price Action Pillars + Mode B Mid-Bar Breakthrough)
    - Execution: Strict 1-lot Maker TP (+4.0% Mon-Thu, +3.0% Fri), Universal Spot Invalidation, Atomic 0.75s fill window
    - Risk Caps: Max 3 trades/day, Hard Disaster Floor (-8.0%), 15:08:00 IST CAS force flatten
    """

    def __init__(
        self,
        broker_gateway,
        data_fetcher,
        aegis_engine,                   # Reference to AegisPipelineOrchestrator for fill feedback
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
        self._order_update_lock = threading.Lock()
        self._order_updates: Dict[str, Dict[str, Any]] = {}
        self._order_history_poll_ts: Dict[str, float] = {}
        self._order_history_poll_interval_sec = 2.5
        self.trade_lock = threading.Lock()
        self._trade_quote_history = deque(maxlen=60)
        self._last_quote_hist_ts = 0.0
        
        # Sub-Millisecond Institutional Dump Detector at +2.0% Profit Zone (Pure O(1))
        # LOT-AWARE: NIFTY 65 / SENSEX 20 must come from the ACTIVE instrument —
        # the legacy `getattr(self, "is_sensex", False)` flag was never set, so
        # SENSEX ran with v0=130 instead of 40 (3x too insensitive on Wed/Thu).
        self.dump_detector = DumpDetector(
            tick=OPTION_TICK_SIZE,
            lot=int(self._active_lot_size()),
            tp_pct=0.04,
            sl_pct=-0.08,
            arm_pct=0.02,
            pull_ticks=2.0,
            theta_min=0.50,
        )

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
        self.daily_gross_pnl = 0.0
        self.daily_net_pnl = 0.0
        self.daily_charges = 0.0
        
        # Order-Reject Fail-Safe state (verified net-position reconciliation)
        self._emerg_recon_state = {}          # per-leg reconcile attempt bookkeeping
        self._has_unreconciled_position = False  # True while broker holds a not-verified-flat position
        
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
        self._last_budget_target_refresh = 0.0
        # Spam guard: budget-target 'No affordable' state logs at most once per
        # 5 min (or when funds move) instead of every 10s refresh cycle.
        self._no_target_last_log = 0.0
        self._no_target_last_funds = -1.0
        market_data = getattr(self.broker, "market_data_provider", None)
        if market_data is not None and hasattr(market_data, "add_order_listener"):
            market_data.add_order_listener(self.on_order_update)


    @property
    def _pa_engine(self):
        """Returns the active price action engine, supporting both modern price_action_engine
        and backward-compatible test mock time_bar_engine."""
        eng = self.aegis_engine
        if not eng:
            return None
        pa = getattr(eng, "price_action_engine", None)
        tb = getattr(eng, "time_bar_engine", None)
        # Check pa first (production standard), then fallback to tb (legacy test mock)
        if pa is not None:
            pa_dict = getattr(pa, "__dict__", {})
            if any(k in pa_dict for k in ("bars_1m", "current_bar", "_curr_1m_bar", "current_atr_1m", "get_in_flight_opt_flow")):
                return pa
        if tb is not None:
            tb_dict = getattr(tb, "__dict__", {})
            if any(k in tb_dict for k in ("bars_1m", "current_bar", "_curr_1m_bar", "current_atr_1m", "get_in_flight_opt_flow")):
                return tb
        return pa or tb


    def get_slot_state(self) -> dict:
        """Quality Stack telemetry: current slot/safety state for dashboard & tests."""
        return {
            "trades_today": int(getattr(self, "trades_today", 0)),
            "max_trades_per_day": MAX_TRADES_PER_DAY,
            "daily_pnl": round(float(getattr(self, "daily_pnl", 0.0)), 2),
            "daily_gross_pnl": round(float(getattr(self, "daily_gross_pnl", 0.0)), 2),
            "daily_charges": round(float(getattr(self, "daily_charges", 0.0)), 2),
            "daily_net_pnl": round(float(getattr(self, "daily_net_pnl", 0.0)), 2),
            "consec_losses": int(getattr(self, "_consec_losses", 0)),
            "consec_loss_stop": int(getattr(self, "consec_loss_stop", 2)),
            "daily_loss_cap_rs": float(getattr(self, "daily_loss_cap_rs", 2000.0)),
            "last_trade_loss": bool(getattr(self, "last_trade_loss", False)),
        }

    # ─── Calendar Gates ───

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


    def _active_lot_size(self, symbol: str = "") -> int:
        """Lot size for the ACTIVE instrument (NIFTY 65 / SENSEX 20).

        Single source: `get_instrument_params()`. Falls back to symbol-name
        sniffing and finally to the NIFTY lot, so a hot-path call can never
        raise. Never hardcode 65/20 at call sites.
        """
        try:
            if symbol:
                return SENSEX_LOT_SIZE if "SENSEX" in str(symbol).upper() else NIFTY_LOT_SIZE
            return int(self.get_instrument_params()["lot_size"])
        except Exception:
            return SENSEX_LOT_SIZE if "SENSEX" in str(symbol).upper() else NIFTY_LOT_SIZE


    def _order_api(self) -> Any:
        """Order-placement seam: prefer the rate-limited ShoonyaExecutionProvider.

        Production (`ShoonyaAdapter`) exposes `execution_provider`, whose
        place/modify/cancel all funnel through `call_with_retry(limiter=
        _order_limiter)`. Raw `broker._api` keeps working as a fallback for
        mock/unit-test gateways that only expose `_api`.
        """
        provider = getattr(self.broker, "execution_provider", None)
        if provider is not None:
            return provider
        return getattr(self.broker, "_api", None)


    def _latest_option_book(self, symbol: str) -> Tuple[float, float, float, float]:
        """Latest in-memory L1 book snapshot for an option symbol, read O(1) from
        the gamma engine's rolling 30s tick history. Returns (ts, lp, bid, ask);
        all zeros when the symbol has no live tick history (fail-open by design).
        Row layout in gamma history_30s: (now_sec, lp, bp1, sp1, b1, a1).
        """
        try:
            eng = self.aegis_engine
            if eng is not None:
                hist = getattr(eng, "history_30s", None)
                if hist:
                    rows = hist.get(symbol)
                    if rows:
                        row = rows[-1]
                        return float(row[0]), float(row[1]), float(row[2]), float(row[3])
        except Exception:
            pass
        return 0.0, 0.0, 0.0, 0.0


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
                    self.daily_gross_pnl = 0.0
                    self.daily_charges = 0.0
                    self.daily_net_pnl = 0.0
                    self.last_trade_date = today_d

                # Service live exits before any entry preparation or queue wait.
                if self.has_open_trade():
                    self._manage_open_trade()
                    time.sleep(0.02)
                else:
                    # Check signal queue first (non-blocking) so waiting live signals are never delayed
                    try:
                        signal = self.signal_queue.get(block=False)
                        self._process_aegis_signal(signal)
                    except queue.Empty:
                        self._refresh_budget_strike_targets()
                        try:
                            signal = self.signal_queue.get(timeout=0.05)
                            self._process_aegis_signal(signal)
                        except queue.Empty:
                            pass

                # 3. Check pending edge measures for AEGIS latency tracking
                # (P0-3: engine resolves the *option premium* LTP internally via
                # option_cache — never pass index spot here.)
                if self.aegis_engine:
                    self.aegis_engine.check_pending_edges(time.time())

            except Exception as e:
                logger.error(f"[AegisEM] Main loop error: {e}", exc_info=True)
                time.sleep(1.0)


    def _get_current_spot(self, local_only: bool = False) -> float:
        """Prefer the engine's live spot feed; use the provider only when it is unavailable."""
        engine = self.aegis_engine
        engine_spot = float(getattr(engine, "last_spot", 0.0) or 0.0) if engine else 0.0
        if engine_spot > 0.0:
            engine_ts = getattr(engine, "_last_spot_ts", 0.0)
            try:
                engine_ts = float(engine_ts or 0.0)
                spot_age = time.time() - engine_ts
            except (TypeError, ValueError):
                engine_ts = 0.0
                spot_age = 0.0
            if engine_ts <= 0.0 or 0.0 <= spot_age <= 5.0:
                return engine_spot
            if local_only:
                return 0.0
        if local_only:
            return 0.0
        try:
            return self.data_fetcher.get_index_ltp()
        except Exception:
            return 0.0

    # ─── Signal Processing ───

    def get_adaptive_target_profit_pct(self, now: Optional[datetime] = None) -> float:
        return get_adaptive_target_profit_pct(now)

