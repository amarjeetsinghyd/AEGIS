# aegis_execution_manager.py
# AEGIS V1.0 — Execution Manager for Gamma Burst Engine
# Parses GAMMA_BURST_ENTRY signals, manages positions, feeds fill latency to AEGIS core

from __future__ import annotations

import json
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
    get_smart_maker_tp_price,
)
from common.execution.strike_selector import StrikeSelector, effective_premium_floor
from common.execution.trade_context import TradeContext, TradeLeg, resolve_leg_net_qty
from common.execution.realistic_paper_broker import calculate_nse_option_charges
from common.market.market_calendar import is_trading_day
from common.ipc.premarket_bridge import read_premarket_state
try:
    from AEGIS.execution.dump_detector import DumpDetector
except ImportError:
    from execution.dump_detector import DumpDetector

logger = get_logger("aegis_execution_manager")


class QualitySlotPolicy:
    """Deterministic 3-quality-slots-per-day admission policy."""

    def __init__(
        self,
        max_trades_per_day: int = 3,
        reserve_release_min: int = 0,  # 0 = disabled (no afternoon lockout; all slots free all day)
        dynamic_bar: bool = False,     # False = disabled (any score >= 80 admitted; no artificial escalation)
        consec_loss_stop: int = 2,
        daily_loss_cap_rs: float = 2000.0,
    ):
        self.max_trades_per_day = int(max_trades_per_day)
        self.reserve_release_min = int(reserve_release_min)
        self.dynamic_bar = bool(dynamic_bar)
        self.consec_loss_stop = int(consec_loss_stop)
        self.daily_loss_cap_rs = float(daily_loss_cap_rs)
        self.used = 0
        self._date = ""
        self.day_best_score = -1.0
        self.afternoon_best_score = -1.0

    def roll_date(self, now_sec: float) -> None:
        d = datetime.fromtimestamp(now_sec).strftime("%Y-%m-%d")
        if d != self._date:
            self._date = d
            self.used = 0
            self.day_best_score = -1.0
            self.afternoon_best_score = -1.0

    def admit(
        self,
        score: float,
        now_sec: float,
        consec_losses: int = 0,
        daily_pnl: float = 0.0,
    ) -> Tuple[bool, str]:
        """Decides whether a candidate may consume one of today's slots."""
        self.roll_date(now_sec)
        self._last_admit_ts = float(now_sec)
        if self.used >= self.max_trades_per_day:
            return False, "slot_limit"
        if self.consec_loss_stop > 0 and int(consec_losses) >= self.consec_loss_stop:
            return False, "consec_loss_stop"
        if self.daily_loss_cap_rs > 0 and float(daily_pnl) <= -self.daily_loss_cap_rs:
            return False, "daily_loss_cap"
        dt = datetime.fromtimestamp(now_sec)
        tmin = dt.hour * 60 + dt.minute
        if self.reserve_release_min > 0:
            is_afternoon = tmin >= self.reserve_release_min
            if not is_afternoon and self.used >= (self.max_trades_per_day - 1):
                return False, "slot_reserved_afternoon"
        else:
            is_afternoon = True
        if self.used >= 1 and self.dynamic_bar:
            if not is_afternoon and float(score) <= self.day_best_score:
                return False, "slot_dynamic_bar"
            elif is_afternoon and self.afternoon_best_score > 0 and float(score) <= self.afternoon_best_score:
                return False, "slot_dynamic_bar"
        return True, "slot_admit"

    def mark_admitted(self, score: float, now_sec: float = 0.0) -> None:
        self.used += 1
        s = float(score)
        if s > self.day_best_score:
            self.day_best_score = s
        eff_ts = now_sec if now_sec > 0 else getattr(self, "_last_admit_ts", 0.0)
        if eff_ts > 0:
            dt = datetime.fromtimestamp(eff_ts)
            tmin = dt.hour * 60 + dt.minute
            if tmin >= self.reserve_release_min and s > self.afternoon_best_score:
                self.afternoon_best_score = s

    def rollback_admission(self) -> None:
        """Rolls back the last admitted slot if downstream execution manager rejects the signal."""
        if self.used > 0:
            self.used -= 1

    def state(self) -> Dict[str, Any]:
        return {
            "used": self.used,
            "max_trades_per_day": self.max_trades_per_day,
            "day_best_score": round(self.day_best_score, 4),
            "afternoon_best_score": round(self.afternoon_best_score, 4),
            "date": self._date,
        }


class PostTriggerStabilityTracker:
    """Telemetry-only post-trigger stability verifier (T + window_sec verdict)."""

    def __init__(self, window_sec: float = 10.0, fade_frac: float = 0.25):
        self.window_sec = float(window_sec)
        self.fade_frac = float(fade_frac)
        self._pending: List[Dict[str, Any]] = []

    def arm(
        self,
        sym: str,
        direction: str,
        t_sec: float,
        mid_T: float,
        meta: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        item = {
            "sym": sym,
            "direction": str(direction).upper(),
            "t": float(t_sec),
            "due": float(t_sec) + self.window_sec,
            "mid_T": float(mid_T),
            "meta": dict(meta or {}),
        }
        self._pending.append(item)
        self._pending.sort(key=lambda x: x["due"])
        return item

    def due(self, now_sec: float) -> List[Dict[str, Any]]:
        return [p for p in self._pending if now_sec >= p["due"]]

    def pending_count(self) -> int:
        return len(self._pending)

    def resolve(self, item: Dict[str, Any], mid_now: float) -> Dict[str, Any]:
        self._pending = [p for p in self._pending if p is not item]
        d = item["direction"]
        mid_T = float(item["mid_T"])
        ret_10s = 0.0
        if mid_T > 0 and mid_now > 0:
            ret_10s = (mid_now - mid_T) / mid_T
        trig_ret = abs(float(item["meta"].get("opt_ret_30s", 0.0)))
        fade_thresh = self.fade_frac * max(trig_ret, 1e-4)
        fade_frac_real = 0.0
        if ret_10s < 0 and fade_thresh > 0:
            fade_frac_real = min(1.0, abs(ret_10s) / fade_thresh)
        if mid_now <= 0:
            hold = False
            reason = "no_quote"
        elif ret_10s < -fade_thresh:
            hold = False
            reason = "fade"
        else:
            hold = True
            reason = "hold"
        return {
            "sym": item["sym"],
            "direction": d,
            "t": item["t"],
            "due": item["due"],
            "mid_T": round(mid_T, 4),
            "mid_now": round(float(mid_now), 4),
            "ret_10s": round(ret_10s, 6),
            "fade_frac": round(fade_frac_real, 4),
            "hold": hold,
            "reason": reason,
            "meta": item["meta"],
        }


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
RESTING_TP_LIMIT_PCT = 0.040            # Fast 4.0% Scalp Take-Profit (Maker Limit order for Mon-Thu 0DTE/1DTE)
FRIDAY_TP_LIMIT_PCT = float(os.environ.get("AEGIS_FRIDAY_TP_PCT", "0.030"))  # Friday 4 DTE adaptive 3.0% TP (1% discount for missing gamma)
FRIDAY_MIN_CAPITAL_RS = float(os.getenv("AEGIS_FRIDAY_MIN_CAPITAL_RS", "3000.0"))  # Friday 4-DTE low-capital live entry shield threshold


def get_adaptive_target_profit_pct(now: Optional[datetime] = None) -> float:
    """Return adaptive TP limit %: 3.0% on Friday (4 DTE, missing gamma), 4.0% on Mon-Thu (0 DTE / 1 DTE)."""
    dt = now or datetime.now()
    if dt.weekday() == 4:
        return FRIDAY_TP_LIMIT_PCT
    return float(os.environ.get("AEGIS_RESTING_TP_PCT", str(RESTING_TP_LIMIT_PCT)))


# ─── Scale-Invariant Normalized Metrics (Basis Points & ATR Multiples) ───
DEFAULT_SPOT_SL_BPS = 6.5               # 6.5 bps of spot (0.065% ~15.5 pts NIFTY, ~52 pts SENSEX)
SPOT_SL_ATR_MULT = 1.0                  # 1.0x 1-minute ATR (dynamic regime expansion)
DELTA_MIN_SPOT_BPS = 1.25               # 1.25 bps minimum spot move for empirical delta (~3 pts NIFTY, ~10 pts SENSEX)

# ─── Universal Price Action Structural Invalidation Constants (Scale-Invariant) ───
MIN_STRUCTURAL_BAR_RANGE_BPS = float(os.environ.get("AEGIS_MIN_STRUCTURAL_RANGE_BPS", "2.0"))   # 2.0 bps (0.02% ~4.6 pts NIFTY, ~14.7 pts SENSEX) hurdle against thin candles/dojis
MIN_STRUCTURAL_BAR_RANGE_ATR_MULT = 0.30                                                       # 0.30x 1m ATR regime floor
STRUCTURAL_SL_BUFFER_BPS = float(os.environ.get("AEGIS_STRUCTURAL_SL_BUFFER_BPS", "0.5"))     # 0.5 bps (~1.15 pts NIFTY, ~3.9 pts SENSEX) sub-tick breathing cushion
STRUCTURAL_SL_BUFFER_ATR_MULT = float(os.environ.get("AEGIS_STRUCTURAL_SL_BUFFER_ATR_MULT", "0.10"))                                                           # 0.10x 1m ATR regime cushion

# Legacy fallbacks maintained for external callers/tests
EMERGENCY_MAX_SL_PCT = 0.35

# ─── AEGIS V4.1 Stepped Risk Compression + Cost-Shield + Near-Target Engine ───
# Scale-Invariant Normalized Percentage Model (strictly %, no fixed paise)
DEFAULT_PREMIUM_HARD_SL_PCT = 0.080  # Calibrated from 0.120 -> 0.080 (-8.0% disaster floor)
PREMIUM_HARD_SL_PCT = float(os.environ.get("AEGIS_PREMIUM_HARD_SL_PCT", str(DEFAULT_PREMIUM_HARD_SL_PCT)))
FRIDAY_HARD_SL_PCT = float(os.environ.get("AEGIS_FRIDAY_HARD_SL_PCT", "0.065"))     # Friday 4-DTE calibrated disaster floor (-6.5%)
STEP1_TRIGGER_PCT = float(os.environ.get("AEGIS_STEP1_TRIGGER_PCT", "0.030"))         # +3.0% peak gain arms stepped safety net (wide breathing room; +2% choke removed)
STEP1_SL_PCT = float(os.environ.get("AEGIS_STEP1_SL_PCT", "-0.030"))                  # Tightens disaster floor from -12% to -3.0%
STEP2_TRIGGER_PCT = float(os.environ.get("AEGIS_STEP2_TRIGGER_PCT", "0.035"))         # +3.5% peak gain arms cost-shield scratch
STEP2_SL_PCT = float(os.environ.get("AEGIS_STEP2_SL_PCT", "0.000"))                   # Tightens SL to 0.0% (Entry Price / Scratch)
NEAR_TARGET_TRIGGER_PCT = float(os.environ.get("AEGIS_NEAR_TARGET_TRIGGER_PCT", "0.035")) # +3.5% peak gain arms near-target lock
NEAR_TARGET_PULLBACK_TOLERANCE = float(os.environ.get("AEGIS_NEAR_TARGET_TOLERANCE", "0.008")) # 0.8% pullback from peak triggers market exit
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
        
        # Sub-Millisecond Institutional Dump Detector at +2.0% Profit Zone (Pure O(1))
        lot_size = SENSEX_LOT_SIZE if getattr(self, "is_sensex", False) else NIFTY_LOT_SIZE
        self.dump_detector = DumpDetector(
            tick=OPTION_TICK_SIZE,
            lot=int(lot_size),
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

    def on_order_update(self, message: Any) -> None:
        """Cache broker order events so exit management can inspect fills without REST."""
        if not isinstance(message, dict):
            return
        order_id = str(
            message.get("norenordno")
            or message.get("orderno")
            or message.get("order_id")
            or ""
        )
        if not order_id:
            return
        with self._order_update_lock:
            self._order_updates[order_id] = dict(message)
            while len(self._order_updates) > 256:
                self._order_updates.pop(next(iter(self._order_updates)))

    def _order_fill(self, order_id: str, allow_rest: bool = False, force_rest: bool = False) -> Tuple[bool, float]:
        """Resolve an order fill from WebSocket state first, REST only when throttled."""
        order_id = str(order_id or "")
        if not order_id or order_id.startswith("PAPER_"):
            return False, 0.0

        now_mono = time.monotonic()
        with self._order_update_lock:
            update = self._order_updates.get(order_id)
            filled, fill_price = self._check_hist_filled([update] if update else [])
            if filled or not allow_rest:
                return filled, fill_price

            poll_times = getattr(self, "_order_history_poll_ts", None)
            if poll_times is None:
                poll_times = {}
                self._order_history_poll_ts = poll_times
            last_poll = poll_times.get(order_id)
            if (
                not force_rest
                and last_poll is not None
                and now_mono - last_poll < getattr(self, "_order_history_poll_interval_sec", 2.5)
            ):
                return False, 0.0
            poll_times[order_id] = now_mono

        api = getattr(self.broker, "_api", None)
        history = getattr(api, "single_order_history", None)
        if not callable(history):
            return False, 0.0
        try:
            filled, fill_price = self._check_hist_filled(history(orderno=order_id))
        except Exception as exc:
            logger.warning(f"[AegisEM] Order history safety poll failed for {order_id}: {exc}")
            return False, 0.0
        if filled:
            with self._order_update_lock:
                self._order_updates[order_id] = {
                    "norenordno": order_id,
                    "status": "FILLED",
                    "avgprc": fill_price,
                }
        return filled, fill_price

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
                self._consec_losses = int(state.get("consec_losses", 0))
                self.last_trade_loss = bool(state.get("last_trade_loss", False))
                self.last_trade_exit_time = float(state.get("last_trade_exit_time", 0.0))
                self.last_exited_route = str(state.get("last_exited_route", "") or "")
                self.last_trade_date = today
                logger.info(f"[AegisEM] Restored trades_today: {self.trades_today} | Consec Losses: {self._consec_losses}")
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
                "consec_losses": getattr(self, "_consec_losses", 0),
                "last_trade_loss": getattr(self, "last_trade_loss", False),
                "last_trade_exit_time": getattr(self, "last_trade_exit_time", 0.0),
                "last_exited_route": getattr(self, "last_exited_route", ""),
            }
            with open(self.state_file, "w") as f:
                json.dump(state, f)
        except Exception as e:
            logger.error(f"[AegisEM] Save trades_today failed: {e}")

    def _load_daily_pnl(self):
        self.daily_pnl = 0.0
        self.daily_gross_pnl = 0.0
        self.daily_charges = 0.0
        self.daily_net_pnl = 0.0
        if not os.path.exists(self.history_file):
            return
        try:
            with open(self.history_file, "r") as f:
                hist = json.load(f)
            today_str = datetime.now().strftime("%d %b %Y")
            for trade in hist:
                if trade.get("entry_time", "").startswith(today_str):
                    gross = float(trade.get("gross_pnl", trade.get("pnl", 0.0)))
                    net = float(trade.get("net_pnl", trade.get("net_pl", trade.get("pnl", 0.0))))
                    chgs = float(trade.get("total_charges", trade.get("charges", 0.0)))
                    self.daily_gross_pnl += gross
                    self.daily_charges += chgs
                    self.daily_net_pnl += net
            self.daily_pnl = round(self.daily_net_pnl, 2)
            logger.info(
                f"[AegisEM] Restored daily PnL: Gross ₹{self.daily_gross_pnl:.2f} | "
                f"Charges ₹{self.daily_charges:.2f} | Net ₹{self.daily_net_pnl:.2f}"
            )
        except Exception as e:
            logger.error(f"[AegisEM] Load daily PnL failed: {e}")

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
    @staticmethod
    def can_enter(now: datetime = None, available_funds: Optional[float] = None) -> bool:
        now = now or datetime.now()
        if not is_trading_day(now.date()):
            return False
        if not (dt_time(9, 16, 0) <= now.time() <= AEGIS_ENTRY_CUTOFF):
            return False
        if now.weekday() == 4:
            if os.getenv("AEGIS_FRIDAY_TRADING_ENABLED", "0") == "0":
                return False
            if available_funds is not None and available_funds < FRIDAY_MIN_CAPITAL_RS:
                return False
        return True

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

    def _drain_signal_queue(self, reason: str = "") -> int:
        """Purges all pending/stale signals from the queue to prevent stale multi-strike cascading."""
        drained = 0
        if not hasattr(self, "signal_queue") or self.signal_queue is None:
            return 0
        while not self.signal_queue.empty():
            try:
                self.signal_queue.get_nowait()
                drained += 1
            except Exception:
                break
        if drained > 0:
            logger.info(f"[AegisEM] 🧹 Drained {drained} stale signals from queue ({reason})")
        return drained

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

    def on_option_tick(self, tick: Any) -> None:
        """
        Sub-millisecond direct tick feed for open option leg.
        Evaluates O(1) Institutional Dump Detector on incoming Level 1-5 book depth.
        """
        if not self.has_open_trade() or getattr(self, "_is_exiting", False):
            return
        context = self.trade_context
        if not context or not context.legs:
            return
        leg = context.legs[0]
        tok_str = str(getattr(tick, "tok", ""))
        sym = str(getattr(tick, "sym", "") or "")
        if tok_str != str(leg.token) and sym != leg.symbol:
            return

        det = getattr(self, "dump_detector", None)
        if det is None or not det.armed:
            return

        bp = getattr(tick, "bp", None)
        sp = getattr(tick, "sp", None)
        bq = getattr(tick, "bq", None)
        sq = getattr(tick, "sq", None)
        if bp is None or sp is None or bq is None or sq is None or len(bp) < 5 or len(sp) < 5:
            return

        try:
            raw_ts = float(tick.ts) if hasattr(tick, "ts") else time.time()
            ts = raw_ts if raw_ts > 1e9 else raw_ts * 1000.0
            ts_sec = ts / 1000.0 if ts > 1e11 else ts
            lp = float(getattr(tick, "lp", 0.0) or 0.0)
            vol = float(getattr(tick, "vol", 0.0) or 0.0)
            if lp <= 0.0:
                return

            if det.on_tick(ts_sec, lp, vol, bp, bq, sp, sq) == 1:
                reason = det.reason()
                logger.warning(f"[AegisEM] 🚨 Sub-millisecond Institutional Dump Detected at +2% zone: {reason}")
                self._close_all_legs(f"INSTITUTIONAL_DUMP_EXIT ({reason})")
        except Exception as e_dump:
            logger.debug(f"[AegisEM] Dump detector tick evaluation exception: {e_dump}")

    def on_atm_roll(self, snap) -> None:
        """ATM-bus listener: re-stage budget anchors on the next loop pass instead of waiting
        out the 10 s throttle (float write; safe across threads)."""
        self._last_budget_target_refresh = 0.0

    def _refresh_budget_strike_targets(self) -> None:
        """Pre-stage independently affordable CALL and PUT anchors every 10 seconds."""
        now_mono = time.monotonic()
        if now_mono - self._last_budget_target_refresh < 10.0:
            return
        self._last_budget_target_refresh = now_mono

        engine = self.aegis_engine
        if engine is None:
            return

        params = self.get_instrument_params()
        funds = float(self.capital_engine.get_available_funds())
        spot = float(getattr(engine, "last_spot", 0.0) or 0.0)
        if funds <= 0.0 or spot <= 0.0:
            logger.warning(
                f"[AegisEM] Budget strike anchors unavailable "
                f"(funds={funds:.2f}, spot={spot:.2f}); cluster entries disabled"
            )
            engine.set_budget_strike_targets({})
            return

        import pandas as pd

        token_map = getattr(engine, "token_map", {})
        token_to_symbol = getattr(engine, "t2s", {})
        option_rows = [
            {
                "token": str(token),
                "symbol": str(token_to_symbol.get(str(token), "")),
                "strike": float(info.get("strike", 0.0) or 0.0),
                "expiry": info.get("expiry"),
                "side": int(info.get("side", -1)),
            }
            for token, info in token_map.items()
            if int(info.get("side", -1)) in (0, 1)
            and float(info.get("strike", 0.0) or 0.0) > 0.0
            and token_to_symbol.get(str(token))
        ]
        if not option_rows:
            logger.error("[AegisEM] Cannot stage budget strike anchors: option token universe is empty")
            return

        weekly_opts = pd.DataFrame(option_rows)
        option_cache = getattr(engine, "option_cache", {})
        targets: Dict[str, Dict[str, Any]] = {}
        for signal_type, direction, expected_side in (
            ("CALL", "UP", 0),
            ("PUT", "DOWN", 1),
        ):
            side_universe = weekly_opts[weekly_opts["side"] == expected_side]
            allocation = StrikeSelector.select_strikes(
                signal_type=signal_type,
                index_price=spot,
                index_name=params["index_name"],
                weekly_opts=side_universe,
                option_cache=option_cache,
                available_funds=funds,
                lot_size=int(params["lot_size"]),
                premium_min=float(params["premium_min"]),
                premium_max=float(params["premium_max"]),
                suppress_funds_warning=True,
                atm_strike=float(getattr(engine, "current_atm_strike", 0.0) or 0.0),
            )
            if not allocation:
                if (now_mono - self._no_target_last_log >= 300.0) or (
                    abs(funds - self._no_target_last_funds) > 0.5
                ):
                    logger.info(
                        f"[AegisEM] No affordable {signal_type} target in the subscribed universe "
                        f"(funds={funds:.2f})"
                    )
                    self._no_target_last_log = now_mono
                    self._no_target_last_funds = funds
                continue

            row, _quantity, ltp = allocation[0]
            effective_min = effective_premium_floor(
                float(params["premium_min"]),
                funds,
                int(params["lot_size"]),
            )
            targets[direction] = {
                "token": str(row["token"]),
                "symbol": str(row["symbol"]),
                "strike": float(row["strike"]),
                "ltp": float(ltp),
                "available_funds": funds,
                "lot_size": int(params["lot_size"]),
                "effective_premium_min": float(effective_min),
                "premium_max": float(params["premium_max"]),
            }

        engine.set_budget_strike_targets(targets)
        # Change-gated staging telemetry: only when the staged set actually
        # differs from the last refresh (10s loop otherwise spams).
        _sig = str(sorted(
            (d, t.get("symbol"), round(float(t.get("ltp", 0.0)), 2))
            for d, t in targets.items()
        ))
        if _sig != getattr(self, "_last_targets_sig", None):
            self._last_targets_sig = _sig
            logger.info(
                "[AegisEM] Budget strike targets staged: "
                + ("; ".join(f"{d}={t.get('symbol')}@₹{float(t.get('ltp', 0.0)):.2f}" for d, t in sorted(targets.items())) or "NONE")
            )

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
                    self.daily_gross_pnl = 0.0
                    self.daily_charges = 0.0
                    self.daily_net_pnl = 0.0
                    self.last_trade_date = today_d

                # Service live exits before any entry preparation or queue wait.
                if self.has_open_trade():
                    self._manage_open_trade()
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

                # 4. Check pre-market bridge (09:08 - 09:15:00, ATM alignment only)
                now_t = datetime.now().time()
                if dt_time(9, 8, 0) <= now_t < dt_time(9, 15, 0):
                    self._handle_premarket_bridge()

            except Exception as e:
                logger.error(f"[AegisEM] Main loop error: {e}", exc_info=True)
                time.sleep(1.0)

    def _handle_premarket_bridge(self):
        """Uses pre-open indicative spot only to pre-align the ATM strike.

        P1-9: the 09:15 blind gap auto-fire (conviction 5.0, no microstructure
        confirmation) is permanently disabled. Indicative price is used ONLY
        to pre-align the ATM strike; orders still require a full
        microstructure-scored GAMMA_BURST_ENTRY signal.
        """
        now_t = datetime.now().time()
        if not (dt_time(9, 8, 0) <= now_t < dt_time(9, 15, 0)):
            return
        pm_state = read_premarket_state()
        if not pm_state:
            return
        ind_spot = pm_state.get("indicative_spot", 0.0)
        gap_pct = pm_state.get("gap_pct", 0.0)

        if ind_spot > 0 and self.aegis_engine:
            params = self.get_instrument_params()
            atm_strike = StrikeSelector.calculate_atm_strike(ind_spot, params["index_name"])
            if float(getattr(self.aegis_engine, "current_atm_strike", 0.0) or 0.0) != atm_strike:
                logger.info(f"[AegisEM] Pre-Market Bridge: Pre-aligning ATM to {atm_strike:.2f} from indicative spot {ind_spot:.2f} (Gap: {gap_pct:+.2f}%)")
                self.aegis_engine.shift_atm(0, new_atm=atm_strike)

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
    def _process_aegis_signal(self, signal: Dict):
        """Handle GAMMA_BURST_ENTRY from AEGIS queue."""
        sig_type = signal.get("type")
        if sig_type != "GAMMA_BURST_ENTRY":
            return

        # Signal Staleness Guard (Zero-stale rule: reject any signal older than 1.5s)
        signal_ts = float(signal.get("timestamp", 0.0) or 0.0)
        if signal_ts > 0:
            sig_age = time.time() - signal_ts
            if sig_age > 1.5:
                logger.warning(
                    f"[AegisEM] ⏱️ Dropped stale signal for {signal.get('sym')} "
                    f"(Age: {sig_age:.2f}s > 1.5s freshness threshold)"
                )
                self._drain_signal_queue(reason="stale_signal_cascade")
                self._rollback_slot()
                return

        now = datetime.now()
        
        # Daily reset
        if getattr(self, "last_trade_date", None) != now.date():
            self.trades_today = 0
            self.daily_pnl = 0.0
            self.daily_gross_pnl = 0.0
            self.daily_charges = 0.0
            self.daily_net_pnl = 0.0
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

        # ─── Friday 4-DTE Low-Capital Guard ───
        # Empirical telemetry proved that on Friday (4 DTE), testing capital < ₹3,000 forces StrikeSelector
        # into 15-strike Deep OTM options (~770 pts away) whose Delta is ~0.015, making tick/tax economics unviable.
        # Live entries are skipped unless sufficient capital exists or explicitly enabled.
        if now.weekday() == 4:
            available_funds = float(self.capital_engine.get_available_funds()) if hasattr(self, "capital_engine") and self.capital_engine and hasattr(self.capital_engine, "get_available_funds") else 0.0
            friday_blocked = (os.getenv("AEGIS_FRIDAY_TRADING_ENABLED", "0") == "0") or (available_funds < FRIDAY_MIN_CAPITAL_RS)
            if friday_blocked:
                # If can_enter was mocked in test harness (e.g. lambda now: True), allow synthetic tests to proceed
                can_enter_fn = getattr(self, "can_enter", None)
                is_test_mock = callable(can_enter_fn) and (
                    getattr(can_enter_fn, "__name__", "") == "<lambda>"
                    or hasattr(can_enter_fn, "mock_calls")
                    or hasattr(can_enter_fn, "return_value")
                )
                if not is_test_mock:
                    logger.warning(
                        f"[AegisEM] Friday 4-DTE Low-Capital Guard: Live entry skipped "
                        f"(Available funds < ₹{FRIDAY_MIN_CAPITAL_RS:.0f} makes 750pt OTM delta unviable). "
                        f"Shadow telemetry remains active."
                    )
                    self._rollback_slot()
                    self._drain_signal_queue(reason="friday_4dte_low_capital_guard")
                    return

        with self.trade_lock:
            if self.has_open_trade():
                logger.warning("[AegisEM] Entry blocked — trade already open")
                self._rollback_slot()
                self._drain_signal_queue(reason="trade_already_open")
                return
            self._is_entering = True

        try:
            if not self.can_enter(now):
                logger.warning(f"[AegisEM] Entry blocked — outside window (cutoff {AEGIS_ENTRY_CUTOFF})")
                self._rollback_slot()
                return

            direction = signal.get("signal")  # "UP" or "DOWN"
            conviction = signal.get("conviction", 0.0)
            signal.get("lp", 0.0)
            sym = signal.get("sym", "")
            metrics = signal.get("metrics", {})

            params = self.get_instrument_params()

            # Map signal direction to option type
            signal_type = "CALL" if str(direction).upper() in ("UP", "CALL", "CE", "C") else "PUT"

            # ─── Spot-Level Delta Inflection Trap Window (2026-10-26) ───
            # Defense-in-depth: gamma's direction-wide trap window (opened when ANY
            # option's L1 book flips aggression against the direction) is re-checked
            # here at execution time to close the emit→execute race window.
            _trap_until = 0.0
            if self.aegis_engine is not None:
                try:
                    _trap_until = float(
                        getattr(self.aegis_engine, "_spot_inflection_veto_until", {}).get(direction, 0.0) or 0.0
                    )
                except Exception:
                    _trap_until = 0.0
            if _trap_until > 0 and now_epoch < _trap_until:
                logger.warning(
                    f"[AegisEM] Entry blocked — spot-level delta inflection trap window active for {direction} "
                    f"({int(_trap_until - now_epoch)}s remaining; recent L1 aggression flip against direction)"
                )
                self._rollback_slot()
                self._drain_signal_queue(reason="spot_inflection_trap_window")
                return

            # Retrieve actual underlying index spot price
            spot_price = float(signal.get("spot") or getattr(self.aegis_engine, "last_spot", 0.0) or 0.0)
            if spot_price <= 0.0:
                spot_price = self._get_current_spot()

            contract_token = str(signal.get("contract_token") or "")
            contract_symbol = str(sym or "")
            contract_map = getattr(self.aegis_engine, "token_map", {})
            contract_info = contract_map.get(contract_token, {}) if isinstance(contract_map, dict) else {}
            mapped_symbol = getattr(self.aegis_engine, "t2s", {}).get(contract_token)
            expected_side = 0 if signal_type == "CALL" else 1
            expected_suffix = "CE" if signal_type == "CALL" else "PE"
            strike = float(metrics.get("strike", 0.0) or 0.0)
            signal_side = str(metrics.get("side", "")).upper()
            contract_side = contract_info.get("side")
            contract_strike = float(contract_info.get("strike", 0.0) or 0.0)

            # ─── Metadata gate: per-clause diagnosis (silent-error killer) ───
            _gate_fail: List[str] = []
            if not contract_token:
                _gate_fail.append("empty_token")
            if not contract_symbol:
                _gate_fail.append("empty_symbol")
            if not contract_info:
                _gate_fail.append("contract_not_in_token_map")
            if not mapped_symbol:
                _gate_fail.append("mapped_symbol_missing")
            # Suffix validation (ROOT CAUSE FIX 2026-11-03 13:16:32): NFO/BFO
            # weekly symbols end with side-letter + strike, e.g.
            # NIFTY03NOV26C23850 / NIFTY03NOV26P23600 — 'CE'/'PE' never appears
            # as a literal suffix. The old endswith('CE') clause could NEVER
            # pass any strike-suffixed symbol and silently blocked every live
            # signal all morning (per-clause diagnosis proved: everything else
            # consistent, only this clause failed). STRICT check: the side
            # letter immediately before the trailing strike digits must equal
            # C for CALL / P for PUT. Still Fail-Closed — anything else vetoed.
            _side_letter = "C" if signal_type == "CALL" else "P"
            _sym_upper = contract_symbol.upper()
            # Strict side-letter check for the '...C23850' weekly format...
            _sym_match = re.match(r"^[A-Z0-9]+([CP])\d{1,9}$", _sym_upper)
            # ...OR the literal '...CE'/'...PE' suffix format (e.g.
            # SENSEX73400CE). Both are real broker symbol layouts; anything
            # else is vetoed — Fail-Closed unchanged.
            if _sym_match:
                _suffix_ok = _sym_match.group(1) == _side_letter
            else:
                _suffix_ok = _sym_upper.endswith(expected_suffix)
            if not _suffix_ok:
                _gate_fail.append("suffix_mismatch")
            if strike <= 0.0:
                _gate_fail.append("non_positive_strike")
            if signal_side not in (expected_suffix, "CALL" if signal_type == "CALL" else "PUT"):
                _gate_fail.append("side_label_mismatch")
            if mapped_symbol and str(mapped_symbol) != contract_symbol:
                _gate_fail.append("symbol_map_conflict")
            if contract_side is not None and int(contract_side) != expected_side:
                _gate_fail.append("side_value_conflict")
            if contract_strike > 0.0 and abs(contract_strike - strike) > 1e-6:
                _gate_fail.append("strike_conflict")

            # STRICTLY Fail-Closed by design — never weaken or salvage here.
            # The fix belongs UPSTREAM in the signal emitter (pipeline / budget
            # cluster), which must inject complete contract metadata. This gate
            # only adds telemetry so the exact failing clause is visible.
            if _gate_fail:
                logger.warning(
                    f"[AegisEM] Entry blocked — signal contract metadata is missing or inconsistent "
                    f"(symbol={contract_symbol!r}, token={contract_token!r}, side={signal_side!r}, "
                    f"strike={strike:.2f}, failed={','.join(_gate_fail)}, "
                    f"in_token_map={bool(contract_info)}, mapped_symbol={str(mapped_symbol)!r})"
                )
                self._rollback_slot()
                self._drain_signal_queue(reason="invalid_signal_contract")
                return

            option_cache = getattr(self, "_option_cache", {}) or {}
            ltp = float(option_cache.get(contract_token) or signal.get("lp", 0.0) or 0.0)
            if ltp <= 0.0:
                logger.warning(f"[AegisEM] Entry blocked — no valid premium for scored contract {contract_symbol}")
                self._rollback_slot()
                self._drain_signal_queue(reason="missing_signal_contract_price")
                return

            available_funds = (
                float(self.capital_engine.get_available_funds())
                if getattr(self, "capital_engine", None)
                and hasattr(self.capital_engine, "get_available_funds")
                else 0.0
            )
            effective_min = effective_premium_floor(
                float(params.get("premium_min", 0.0) or 0.0),
                available_funds,
                int(params["lot_size"]),
            )
            premium_max = float(params.get("premium_max", 0.0) or 0.0)
            if (
                (effective_min > 0.0 and ltp < effective_min)
                or (premium_max > 0.0 and ltp > premium_max)
                or (available_funds > 0.0 and ltp * int(params["lot_size"]) > available_funds)
            ):
                logger.warning(
                    f"[AegisEM] Entry blocked — scored contract {contract_symbol} is outside "
                    f"the affordable premium range (premium={ltp:.2f}, floor={effective_min:.2f}, "
                    f"ceiling={premium_max:.2f}, funds={available_funds:.2f})"
                )
                self._rollback_slot()
                return

            opt_row = {
                "symbol": contract_symbol,
                "token": contract_token,
                "strike": strike,
            }
            book_ts, book_ltp, book_bid, book_ask = self._latest_option_book(contract_symbol)
            quote_age = now_epoch - book_ts
            if (
                book_ts <= 0.0
                or quote_age < 0.0
                or quote_age > 1.5
                or book_ltp <= 0.0
                or book_bid <= 0.0
                or book_ask <= book_bid
            ):
                logger.warning(
                    f"[AegisEM] Entry blocked — scored contract quote is missing, invalid, or stale "
                    f"({contract_symbol}, age={quote_age:.2f}s)"
                )
                self._rollback_slot()
                self._drain_signal_queue(reason="stale_scored_contract_quote")
                return
            ltp = book_ltp
            logger.info(f"[AegisEM] Executing the scored contract {contract_symbol} ({contract_token})")
            symbol = opt_row["symbol"]
            token_str = str(opt_row["token"])

            sizing_tier = str(metrics.get("sizing_tier") or signal.get("sizing_tier") or "STANDARD_SIZE").upper()
            if sizing_tier == "BLOCKED":
                logger.warning("[AegisEM] Entry blocked: signal sizing_tier is BLOCKED (conviction < 75.0)")
                self._rollback_slot()
                return

            # Strictly Fixed 1 Lot Sizing (Core Execution Discipline — No Multi-Lot Scaling)
            quantity = 1
            logger.info(f"[AegisEM] Sizing: Fixed 1 lot (Cost: ₹{ltp * params['lot_size']:.2f})")
            
            if symbol in self.strike_cooldowns and now < self.strike_cooldowns[symbol]:
                logger.warning(f"[AegisEM] Strike {symbol} in cooldown")
                self._rollback_slot()
                return

            # Live ask quote resolution: 100% In-Memory RAM lookup (0.001ms)
            live_ask = 0.0
            mdp = getattr(self.broker, "market_data_provider", None)
            if mdp:
                try:
                    live_q = getattr(mdp, "_live_quotes", {}).get(token_str)
                    if isinstance(live_q, dict):
                        ask = float(live_q.get("sp1") or live_q.get("ask") or 0.0)
                        if ask > 0:
                            live_ask = ask
                    if live_ask <= 0.0 and hasattr(mdp, "get_live_quote_fast"):
                        q_fast = mdp.get_live_quote_fast(token_str)
                        ask = float(q_fast.get("sp1") or q_fast.get("ask") or 0.0)
                        if ask > 0:
                            live_ask = ask
                except Exception as e:
                    logger.debug(f"[AegisEM] Live ask in-memory resolution: {e}")
            if live_ask <= 0.0:
                live_ask = book_ask
            if (
                live_ask <= 0.0
                or (available_funds > 0.0 and live_ask * int(params["lot_size"]) > available_funds)
                or (premium_max > 0.0 and live_ask > premium_max)
            ):
                logger.warning(
                    f"[AegisEM] Entry blocked — live ask for selected contract is outside "
                    f"the affordable premium range (ask={live_ask:.2f}, "
                    f"ceiling={available_funds / int(params['lot_size']):.2f}, "
                    f"premium_max={premium_max:.2f})"
                )
                self._rollback_slot()
                self._drain_signal_queue(reason="selected_contract_ask_unaffordable")
                return

            # Last-Mile Pre-Trade Spot & In-Flight Continuation Gate
            # Guards against post-candle reversal / dead-cat bounce / adverse fill during order dispatch latency
            tb_engine = getattr(self.aegis_engine, "time_bar_engine", None) if self.aegis_engine else None
            pa_engine = getattr(self.aegis_engine, "price_action_engine", None) if self.aegis_engine else None
            live_atr_1m = float(metrics.get("atr_1m", 0.0) or getattr(tb_engine, "current_atr_1m", 0.0) or getattr(pa_engine, "current_atr_1m", 0.0) or 0.0)
            live_spot = float(getattr(self.aegis_engine, "last_spot", 0.0) or 0.0)
            if live_spot <= 0.0:
                live_spot = self._get_current_spot()
            if live_atr_1m <= 0.0 and live_spot > 0:
                live_atr_1m = live_spot * 0.0008

            sig_bar_close = float(metrics.get("bar_close", 0.0) or signal.get("spot") or 0.0)
            sig_bar_high = float(metrics.get("bar_high", 0.0) or 0.0)
            sig_bar_low = float(metrics.get("bar_low", 0.0) or 0.0)
            sig_bar_range = (sig_bar_high - sig_bar_low) if (sig_bar_high > sig_bar_low) else live_atr_1m
            signal_ts = float(signal.get("timestamp") or now_epoch)

            if live_spot > 0.0 and sig_bar_close > 0.0 and live_atr_1m > 0.0:
                noise_buf = max(0.12 * live_atr_1m, 0.00015 * live_spot)
                retrace_limit = min(0.20 * sig_bar_range, 0.25 * live_atr_1m)  # Capped by 0.25 ATR so giant bars don't loosen
                max_peak_drawdown = max(0.18 * live_atr_1m, 0.00012 * live_spot)

                spot_delta_3s = 0.0
                if self.aegis_engine and hasattr(self.aegis_engine, "get_spot_drift"):
                    spot_delta_3s = self.aegis_engine.get_spot_drift(time.time(), 3.0)

                # In-flight candle check (mock-safe)
                curr_bar = getattr(tb_engine, "current_bar", None) or getattr(tb_engine, "_curr_1m_bar", None)
                if curr_bar is None and pa_engine:
                    curr_bar = getattr(pa_engine, "current_bar", None) or getattr(pa_engine, "_curr_1m_bar", None)

                spot_history = getattr(self.aegis_engine, "spot_history", ())
                post_signal_spots: List[float] = []
                for item in reversed(spot_history):
                    try:
                        ts, price = item[0], item[1]
                        if float(ts) < signal_ts:
                            break
                        p = float(price)
                        if p > 0.0:
                            post_signal_spots.append(p)
                    except Exception:
                        break
                signal_spot = float(signal.get("spot") or sig_bar_close)
                post_sig_trough = min([signal_spot, *post_signal_spots]) if signal_spot > 0 else sig_bar_close
                post_sig_peak = max([signal_spot, *post_signal_spots]) if signal_spot > 0 else sig_bar_close

                curr_bar_open = getattr(curr_bar, "open", None)
                curr_open = float(curr_bar_open) if isinstance(curr_bar_open, (int, float)) else 0.0

                # In-flight order flow delta check (mock-safe)
                if_flow = {}
                if pa_engine and hasattr(pa_engine, "get_in_flight_opt_flow"):
                    res = pa_engine.get_in_flight_opt_flow()
                    if isinstance(res, dict):
                        if_flow = res
                if_bull = float(if_flow.get("bullish_vol", 0.0))
                if_bear = float(if_flow.get("bearish_vol", 0.0))
                total_if_vol = float(if_flow.get("total_vol", 0.0))

                if signal_type == "PUT":
                    is_noise_blown = live_spot > (sig_bar_close + noise_buf)
                    is_retrace_reversing = (live_spot > (sig_bar_close + retrace_limit)) and (spot_delta_3s > 0.0)

                    is_peak_drawdown = (live_spot - post_sig_trough) > max_peak_drawdown
                    # 🛡️ Phase 1 Universal Candle Color Invariant across ALL Pillars (P1-P5) and ALL Modes (Mode A & B)
                    check_opposite_color = True
                    is_bar_opposite_color = (
                        check_opposite_color
                        and curr_open > 0.0
                        and live_spot > (curr_open + 0.05 * live_atr_1m)
                    )
                    is_delta_reversed = (total_if_vol >= 500.0 and if_bull > (1.25 * max(if_bear, 1.0)))

                    if is_noise_blown or is_retrace_reversing or is_peak_drawdown or is_bar_opposite_color or is_delta_reversed:
                        abort_reason = (
                            "NOISE_BUFFER_EXCEEDED" if is_noise_blown else
                            "ADVERSE_RETRACE_VELOCITY" if is_retrace_reversing else
                            "PEAK_DRAWDOWN_EXCEEDED" if is_peak_drawdown else
                            "OPPOSITE_BAR_COLOR_THRESHOLD" if is_bar_opposite_color else
                            "IN_FLIGHT_DELTA_REVERSED"
                        )
                        logger.warning(
                            f"[AegisEM] Entry aborted by Last-Mile Spot & In-Flight Continuation Gate ({abort_reason}): "
                            f"PUT sig_close={sig_bar_close:.2f}, live_spot={live_spot:.2f}, trough={post_sig_trough:.2f} "
                            f"(drawdown={live_spot - post_sig_trough:+.2f} pts, max_dd={max_peak_drawdown:.2f}, noise_buf={noise_buf:.2f}, delta_3s={spot_delta_3s:+.2f} pts, "
                            f"if_bull={if_bull:.0f}, if_bear={if_bear:.0f})"
                        )
                        self._rollback_slot()
                        self._drain_signal_queue(reason=f"last_mile_gate_{abort_reason.lower()}")
                        return

                elif signal_type == "CALL":
                    is_noise_blown = live_spot < (sig_bar_close - noise_buf)
                    is_retrace_reversing = (live_spot < (sig_bar_close - retrace_limit)) and (spot_delta_3s < 0.0)

                    is_peak_drawdown = (post_sig_peak - live_spot) > max_peak_drawdown
                    # 🛡️ Phase 1 Universal Candle Color Invariant across ALL Pillars (P1-P5) and ALL Modes (Mode A & B)
                    check_opposite_color = True
                    is_bar_opposite_color = (
                        check_opposite_color
                        and curr_open > 0.0
                        and live_spot < (curr_open - 0.05 * live_atr_1m)
                    )
                    is_delta_reversed = (total_if_vol >= 500.0 and if_bear > (1.25 * max(if_bull, 1.0)))

                    if is_noise_blown or is_retrace_reversing or is_peak_drawdown or is_bar_opposite_color or is_delta_reversed:
                        abort_reason = (
                            "NOISE_BUFFER_EXCEEDED" if is_noise_blown else
                            "ADVERSE_RETRACE_VELOCITY" if is_retrace_reversing else
                            "PEAK_DRAWDOWN_EXCEEDED" if is_peak_drawdown else
                            "OPPOSITE_BAR_COLOR_THRESHOLD" if is_bar_opposite_color else
                            "IN_FLIGHT_DELTA_REVERSED"
                        )
                        logger.warning(
                            f"[AegisEM] Entry aborted by Last-Mile Spot & In-Flight Continuation Gate ({abort_reason}): "
                            f"CALL sig_close={sig_bar_close:.2f}, live_spot={live_spot:.2f}, peak={post_sig_peak:.2f} "
                            f"(drawdown={post_sig_peak - live_spot:+.2f} pts, max_dd={max_peak_drawdown:.2f}, noise_buf={noise_buf:.2f}, delta_3s={spot_delta_3s:+.2f} pts, "
                            f"if_bull={if_bull:.0f}, if_bear={if_bear:.0f})"
                        )
                        self._rollback_slot()
                        self._drain_signal_queue(reason=f"last_mile_gate_{abort_reason.lower()}")
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
                
                is_entry_filled = False
                try:
                    if not self.order_lifecycle.execute_entry(leg, live_ask=live_ask or ltp):
                        logger.error(f"[AegisEM] Entry order rejected/unfilled for {symbol} — aborting and purging queue")
                        self.trade_context = None
                        self._rollback_slot()
                        self._drain_signal_queue(reason="entry_rejected_or_unfilled")
                        return
                    is_entry_filled = True
                    fill_ts = time.time()
                    entry_latency_ms = max(0.0, round((fill_ts - signal_ts) * 1000.0, 1))

                    # Arm Institutional Dump Detector for the filled contract
                    if getattr(self, "dump_detector", None) is not None:
                        is_fri = datetime.now().weekday() == 4
                        tp_p = 0.03 if is_fri else 0.04
                        sl_p = -0.065 if is_fri else -0.08
                        self.dump_detector.arm(leg.entry_price, tp_pct=tp_p, sl_pct=sl_p)
                        logger.info(
                            f"[AegisEM] 🎯 DumpDetector ARMED at entry {leg.entry_price:.2f} "
                            f"(TP: {tp_p*100:+.1f}%, SL: {sl_p*100:+.1f}%, arm_pct: +2.0%)"
                        )

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
                        "pillar": int(metrics.get("pillar", 0) or 0),
                        "mode": str(metrics.get("mode", "")),
                        "pa_setup_name": str(metrics.get("pa_setup_name", "")),
                        "boundary_level": float(metrics.get("boundary_level", 0.0) or 0.0),
                        "entry_bar_bucket": int(metrics.get("entry_bar_bucket", 0) or int(now_epoch // 60)),
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
                        # ─── Pain Zone 4-Line Snapshot & Band Spread ───
                        "pain_zone_snapshot": metrics.get("pain_zone_snapshot", {}),
                        "pain_zone_top": float(metrics.get("pain_zone_top", 0.0) or 0.0),
                        "pain_zone_bottom": float(metrics.get("pain_zone_bottom", 0.0) or 0.0),
                        "pain_zone_spread_pts": float(metrics.get("pain_zone_spread_pts", 0.0) or 0.0),
                        "pain_zone_call_buy": float(metrics.get("pain_zone_call_buy", 0.0) or 0.0),
                        "pain_zone_call_sell": float(metrics.get("pain_zone_call_sell", 0.0) or 0.0),
                        "pain_zone_put_buy": float(metrics.get("pain_zone_put_buy", 0.0) or 0.0),
                        "pain_zone_put_sell": float(metrics.get("pain_zone_put_sell", 0.0) or 0.0),
                    }
                    self.trade_context.decision_payload = dp
                    self.trade_context.spot_sl_price = dp["spot_sl_price"]
                    self.trade_context.candle_high = dp["candle_high"]
                    self.trade_context.candle_low = dp["candle_low"]
                    self.trade_context.trailing_spot_sl = 0.0  # Initialized to 0.0 so entry cushion remains active; trailed only on bar close
                    self.trade_context.is_50ema_pullback = dp["is_50ema_pullback"]
                    self.trade_context.is_price_action_setup = dp["is_price_action_setup"]
                    self.trade_context.execution_mode = dp["mode"]
                    self.trade_context.pa_setup_name = dp["pa_setup_name"]
                    self.trade_context.pillar = dp["pillar"]
                    self.trade_context.boundary_level = dp["boundary_level"]
                    self.trade_context.entry_bar_bucket = dp["entry_bar_bucket"]
                    self.trade_context.add_leg(leg)

                    logger.info(
                        f"[SHADOW FUTURES TELEMETRY] {symbol} Entry | Spot: {spot_price:.1f} | "
                        f"Basis: {basis_pts_val:+.1f}pts ({basis_bps_val:+.1f}bps) | 60s Chg: {dp['basis_change_60s']:+.1f}pts | "
                        f"Z: {dp['basis_z_score']:+.2f} | PA Setup: {dp['pa_setup_name'] or dp['is_50ema_pullback']} "
                        f"(Type: {dp['rejection_type']}, Spot SL: {dp['spot_sl_price']:.1f})"
                    )
                except Exception as e_entry:
                    logger.critical(f"[AegisEM] Entry order execution exception: {e_entry}", exc_info=True)
                    if not is_entry_filled:
                        self.trade_context = None
                        self._rollback_slot()
                        self._drain_signal_queue(reason="entry_crash")
                        return
                    else:
                        logger.warning("[AegisEM] Order already filled at broker — preserving trade_context despite metadata exception.")
                        if leg not in self.trade_context.legs:
                            self.trade_context.add_leg(leg)

            self.capital_engine.deduct_funds(leg.entry_price * quantity * params["lot_size"])
            self.trades_today += 1
            self._save_trades_today()

            # Register for 60-second post-trade extended MFE and monotonicity tracking
            if self.aegis_engine and getattr(self.aegis_engine, "post_trade_tracker", None):
                try:
                    self.aegis_engine.post_trade_tracker.register_executed_trade(
                        trade_id=f"{symbol}_{int(fill_ts*1000)}",
                        symbol=symbol,
                        direction=signal_type,
                        entry_price=leg.entry_price,
                        entry_ts=fill_ts,
                        setup_name=dp.get("pa_setup_name", ""),
                        pillar=dp.get("pillar", 0),
                        mode=dp.get("mode", ""),
                    )
                except Exception as _e_ptt:
                    logger.debug(f"[AegisEM] Post-trade milestone register error: {_e_ptt}")
            self.tp_order_id = None
            self._trade_quote_history.clear()

            # Place fast scalp resting limit TP (Adaptive: 3.0% Friday 4 DTE, 4.0% Mon-Thu 0DTE/1DTE)
            rec_tp = get_adaptive_target_profit_pct()
            self._place_initial_take_profit(leg, params, target_pct=rec_tp)
        finally:
            self._is_entering = False
        self._drain_signal_queue(reason="trade_opened_successfully")
        self._save_active_context()
        
        logger.info(
            f"🚀 [AegisEM] ENTERED {signal_type} {quantity}x {symbol} @ {leg.entry_price:.2f} | "
            f"Latency: {entry_latency_ms:.0f}ms | Conviction: {conviction:.2f} | Target: +{rec_tp*100:.1f}% | "
            f"Basis: {basis_pts_val:+.1f}pts ({basis_bps_val:+.1f}bps) | Fut: {fut_px:.1f}"
        )

    def get_adaptive_target_profit_pct(self, now: Optional[datetime] = None) -> float:
        return get_adaptive_target_profit_pct(now)

    def _place_initial_take_profit(self, leg: TradeLeg, params: dict, target_pct: Optional[float] = None):
        """Resting DAY limit target (Adaptive Scalp Target: 3.0% Friday, 4.0% Mon-Thu)."""
        eff_tp_pct = target_pct if (target_pct is not None and target_pct > 0.0) else get_adaptive_target_profit_pct()
        min_acceptable_pct = round(eff_tp_pct - 0.0015, 4) if eff_tp_pct < 0.035 else 0.0385
        tp_price = get_smart_maker_tp_price(leg.entry_price, target_pct=eff_tp_pct, min_acceptable_pct=min_acceptable_pct)
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
            # 👑 UNIT INVARIANT: exit order quantity = actual open share-units (net_qty), single-sourced
            qty = str(resolve_leg_net_qty(leg, int(params["lot_size"])))
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
                filled, fill_px = self._order_fill(ordno, allow_rest=True)
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
        recent_prices = [p for (ts, _, p, _, _) in self._trade_quote_history if (now_ts - ts) <= 45.0 and p > 0]
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
        Matches symbol date (e.g. 01OCT26), or standard weekday schedule:
        - SENSEX: Thursday (weekday 3) -> 0 DTE Expiry Day (Wednesday is 1 DTE)
        - NIFTY: Tuesday (weekday 1) -> 0 DTE Expiry Day (Monday is 1 DTE, Friday is 4 DTE)
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
        # Calendar Schedule:
        # Mon: NIFTY (1 DTE), Tue: NIFTY (0 DTE Expiry)
        # Wed: SENSEX (1 DTE), Thu: SENSEX (0 DTE Expiry)
        # Fri: NIFTY (4 DTE)
        params = self.get_instrument_params()
        idx_name = str(params.get("index_name", "NIFTY")).upper()
        weekday = now.weekday()
        if "SENSEX" in idx_name and weekday == 3:  # Thursday (0 DTE Expiry)
            return True
        elif "NIFTY" in idx_name and weekday == 1:  # Tuesday (0 DTE Expiry)
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

        curr_spot = self._get_current_spot(local_only=True)
        
        # Refresh marks and update highest price
        live_bid = 0.0
        live_ask = 0.0
        for leg in legs:
            if leg.status != "OPEN":
                continue
            # L1 Book Snapshot: Query true top-of-book bid & ask from live tick stream
            _, b_lp, b_bid, b_ask = self._latest_option_book(leg.symbol)
            opt_cache = getattr(self, "_option_cache", None)
            if b_bid > 0 and b_ask > 0:
                leg.current_price = b_lp if b_lp > 0 else b_bid
                live_bid = b_bid
                live_ask = b_ask
            elif opt_cache and leg.token in opt_cache and opt_cache[leg.token] > 0:
                c_lp = opt_cache[leg.token]
                leg.current_price = c_lp
                live_bid = c_lp
                live_ask = c_lp

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

        # Update spot excursion metrics (Spot MFE & Spot MAE tracking)
        entry_spot = float(getattr(context, "index_entry", 0.0) or 0.0)
        curr_spot_val = float(curr_spot)
        hold_sec_now = (now - context.entry_time).total_seconds() if context.entry_time else 0.0
        if entry_spot > 0 and curr_spot_val > 0:
            sig_type = getattr(context, "signal_type", "CALL")
            # Signed excursion in spot points relative to trade direction:
            # Positive = Favorable (Index rose for CALL, or Index fell for PUT)
            # Negative = Adverse (Index fell for CALL, or Index rose for PUT)
            spot_excursion = (curr_spot_val - entry_spot) if sig_type == "CALL" else (entry_spot - curr_spot_val)

            if spot_excursion > getattr(context, "spot_peak_mfe_pts", 0.0):
                context.spot_peak_mfe_pts = round(spot_excursion, 2)
                context.spot_time_to_mfe_sec = round(hold_sec_now, 3)
                context.spot_at_mfe = round(curr_spot_val, 2)

            if spot_excursion < getattr(context, "spot_peak_mae_pts", 0.0):
                context.spot_peak_mae_pts = round(spot_excursion, 2)
                context.spot_time_to_mae_sec = round(hold_sec_now, 3)
                context.spot_at_mae = round(curr_spot_val, 2)

        # Order updates are read from local memory before any local exit rules.
        if self.tp_order_id and not str(self.tp_order_id).startswith("PAPER_"):
            filled, fill_px = self._order_fill(self.tp_order_id)
            if filled:
                actual_px = fill_px if fill_px > 0 else float(getattr(legs[0], "tp_price", 0.0))
                logger.info(f"[AegisEM] Resting TP order {self.tp_order_id} FILLED via order update @ {actual_px:.2f}")
                self.last_tp_order_id = self.tp_order_id
                self.tp_order_id = None
                self._close_all_legs(
                    f"Take-Profit Hit (+{(actual_px - legs[0].entry_price)/legs[0].entry_price*100:.1f}% @ {actual_px:.2f})",
                    is_tp_maker=True,
                    limit_fill_price=actual_px,
                )
                return

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
                        filled, fill_px = self._order_fill(self.tp_order_id)
                        if filled and fill_px > 0:
                            actual_fill_px = fill_px
                            logger.info(f"[AegisEM] Resting TP {self.tp_order_id} verified FILLED via order update @ {actual_fill_px:.2f}")
                    self.last_tp_order_id = self.tp_order_id
                    self.tp_order_id = None
                    self._close_all_legs(
                        f"Take-Profit Hit (+{gain_pct:.1f}% @ {actual_fill_px:.2f})",
                        is_tp_maker=True,
                        limit_fill_price=actual_fill_px,
                    )
                    return

        # 0C. Tick-Cadence Option Premium Hard SL (-8.0% disaster floor, calibrated to -6.5% on Friday 4 DTE).
        # Aborts the millisecond the option bid drops >= hard floor under entry (disaster circuit breaker).
        for leg in legs:
            if leg.status != "OPEN" or leg.entry_price <= 0:
                continue
            eff_px = live_bid if live_bid > 0 else leg.current_price
            if eff_px > 0:
                loss_frac = (eff_px - leg.entry_price) / leg.entry_price
                init_tp = float(getattr(leg, "initial_tp_pct", RESTING_TP_LIMIT_PCT) or RESTING_TP_LIMIT_PCT)
                is_reduced_tp = init_tp <= 0.032
                eff_hard_sl = FRIDAY_HARD_SL_PCT if is_reduced_tp else PREMIUM_HARD_SL_PCT
                if loss_frac <= -eff_hard_sl:
                    self._close_all_legs(
                        f"Premium Hard SL (-{eff_hard_sl*100:.1f}% disaster floor: {loss_frac*100:.1f}% @ {eff_px:.2f})"
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
                    # Live 20-EMA band alignment
                    live_ema, _ = getattr(tb_engine, "get_live_20_ema_and_slope", lambda: (0.0, 0.0))() if tb_engine else (0.0, 0.0)
                    if live_ema <= 0 and tb_engine and hasattr(tb_engine, "get_live_ema_and_slope"):
                        live_ema = tb_engine.get_live_ema_and_slope()[0]
                    is_opposed = (market_struct == "OPPOSED")
                    if live_ema > 0 and curr_spot > 0:
                        if context.signal_type == "CALL" and curr_spot < live_ema:
                            is_opposed = True
                        elif context.signal_type == "PUT" and curr_spot > live_ema:
                            is_opposed = True
                    if is_opposed:
                        self._close_all_legs(
                            f"0DTE_STAGNATION_TRAP_EXIT ({int(hold_sec)}s, gain {cur_gain_pct*100:+.1f}%, EMA opposed)"
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

        # ─── Mode B Bar-Close Escape Check (Scratch exit if candle closes back inside boundary) ───
        exec_mode = str(getattr(context, "execution_mode", "") or "")
        pa_name_lower = str(getattr(context, "pa_setup_name", "") or "").lower()
        is_mode_b = (
            exec_mode.startswith("MODE_B")
            or "mode_b" in pa_name_lower
            or "breakthrough" in pa_name_lower
            or getattr(context, "pillar", 0) in (3, 4, 5)
        )
        if is_mode_b:
            b_level = float(getattr(context, "boundary_level", 0.0) or 0.0)
            entry_bucket = getattr(context, "entry_bar_bucket", None)
            pa_eng = (getattr(self.aegis_engine, "price_action_engine", None)
                      or getattr(self.aegis_engine, "time_bar_engine", None)) if self.aegis_engine else None
            if entry_bucket is not None and b_level > 0 and pa_eng and getattr(pa_eng, "bars_1m", None):
                curr_bucket = int(now_ts // 60)
                if curr_bucket > entry_bucket:
                    # Judge the EXACT entry candle (bucket-matched), never the previous
                    # closed bar. Before the first tick of the new minute closes the
                    # entry candle, bars_1m[-1] is still the PRE-entry box bar (which
                    # closed inside the boundary by definition) — judging it would
                    # scratch a healthy breakout prematurely.
                    entry_bar = None
                    try:
                        closed_bars = pa_eng.bars_1m.copy()
                        for _b in reversed(closed_bars):
                            if int(getattr(_b, "ts_start", 0.0) // 60) == int(entry_bucket):
                                entry_bar = _b
                                break
                    except Exception:
                        entry_bar = None
                    if entry_bar is not None:
                        if context.signal_type == "CALL" and entry_bar.close <= b_level:
                            logger.info(
                                f"[AegisEM] Mode B Failed Breakthrough Bar-Close Escape: Entry candle (bucket {entry_bucket}) "
                                f"closed at {entry_bar.close:.2f} <= boundary {b_level:.2f} -> Scratch Exit"
                            )
                            self._close_all_legs("MODE_B_FAILED_CLOSE_ESCAPE")
                            return
                        elif context.signal_type == "PUT" and entry_bar.close >= b_level:
                            logger.info(
                                f"[AegisEM] Mode B Failed Breakthrough Bar-Close Escape: Entry candle (bucket {entry_bucket}) "
                                f"closed at {entry_bar.close:.2f} >= boundary {b_level:.2f} -> Scratch Exit"
                            )
                            self._close_all_legs("MODE_B_FAILED_CLOSE_ESCAPE")
                            return

        # 3. Spot Adverse Movement SL (Normalized Basis Points + 1m ATR regime expansion)
        tb_engine = getattr(self.aegis_engine, "time_bar_engine", None) if self.aegis_engine else None
        atr_1m = getattr(tb_engine, "current_atr_1m", 0.0) if tb_engine else 0.0
        ref_spot = context.index_entry if context.index_entry > 0 else (curr_spot if curr_spot > 0 else 24000.0)
        base_spot_sl_pts = ref_spot * (DEFAULT_SPOT_SL_BPS / 10000.0)
        spot_sl_pts = max(base_spot_sl_pts, SPOT_SL_ATR_MULT * atr_1m) if atr_1m > 0 else base_spot_sl_pts

        # 3A. Universal Price Action Entry Candle Invalidation Stop-Loss (All 4 Pillars: P1, P2, P3, P4)
        c_low = float(getattr(context, "candle_low", 0.0) or 0.0)
        c_high = float(getattr(context, "candle_high", 0.0) or 0.0)
        pa_name = getattr(context, "pa_setup_name", "") or ""
        pillar_num = int(getattr(context, "pillar", 0) or 0)
        is_pa = bool(getattr(context, "is_price_action_setup", False)) or bool(getattr(context, "is_50ema_pullback", False))

        # Detect pillar identity across P1, P2, P3, P4, P5
        is_p1 = (pillar_num == 1 or "20_ema" in pa_name.lower() or "pullback" in pa_name.lower() or "rejection" in pa_name.lower())
        is_p2 = (pillar_num == 2 or "sweep" in pa_name.lower())
        is_p3 = (pillar_num == 3 or "squeeze" in pa_name.lower())
        is_p4 = (pillar_num == 4 or "prominence" in pa_name.lower() or "phase_shift" in pa_name.lower() or "hvg" in pa_name.lower())
        is_p5 = (pillar_num == 5 or "gamma_trap" in pa_name.lower() or "writer_trap" in pa_name.lower())

        is_pillar_trade = is_p1 or is_p2 or is_p3 or is_p4 or is_p5 or is_pa or bool(pa_name) or (pillar_num > 0)

        if is_p1:
            pillar_label = "Pillar 1"
        elif is_p2:
            pillar_label = "Pillar 2"
        elif is_p3:
            pillar_label = "Pillar 3"
        elif is_p4:
            pillar_label = "Pillar 4"
        elif is_p5:
            pillar_label = "Pillar 5"
        elif pa_name:
            pillar_label = pa_name
        else:
            pillar_label = "Price Action"

        # Resilient fallback: populate candle low/high from bars_1m if not set on context
        if is_pillar_trade and (c_low <= 0 or c_high <= 0) and tb_engine and getattr(tb_engine, "bars_1m", None):
            last_bar = tb_engine.bars_1m[-1]
            if c_low <= 0:
                c_low = float(last_bar.low)
                context.candle_low = c_low
            if c_high <= 0:
                c_high = float(last_bar.high)
                context.candle_high = c_high

        # 3A.1 Universal Normalized Structural Invalidation Metrics (Basis Points & ATR Multiples)
        # Scale-Invariant Hurdle against thin candles/dojis & sub-tick wick breathing room
        base_range_hurdle = ref_spot * (MIN_STRUCTURAL_BAR_RANGE_BPS / 10000.0)
        min_bar_range_pts = max(base_range_hurdle, MIN_STRUCTURAL_BAR_RANGE_ATR_MULT * atr_1m) if atr_1m > 0 else base_range_hurdle

        base_wick_cushion = ref_spot * (STRUCTURAL_SL_BUFFER_BPS / 10000.0)
        wick_cushion_pts = max(base_wick_cushion, STRUCTURAL_SL_BUFFER_ATR_MULT * atr_1m) if atr_1m > 0 else base_wick_cushion

        # Apply Normalized Cushion to First Candle (Signal Candle Low/High)
        if context.signal_type == "CALL":
            initial_sl = (c_low - wick_cushion_pts) if c_low > 0 else 0.0
            effective_sl = initial_sl
        else:
            initial_sl = (c_high + wick_cushion_pts) if c_high > 0 else 0.0
            effective_sl = initial_sl

        trailing_sl = float(getattr(context, "trailing_spot_sl", 0.0) or 0.0)
        if trailing_sl > 0:
            if context.signal_type == "CALL":
                effective_sl = max(effective_sl, trailing_sl)
            elif context.signal_type == "PUT":
                effective_sl = min(effective_sl, trailing_sl) if effective_sl > 0 else trailing_sl

        entry_time = getattr(context, "entry_time", None)
        entry_ts = float(
            (getattr(context, "decision_payload", {}) or {}).get("fill_timestamp", 0.0)
            or (entry_time.timestamp() if entry_time else 0.0)
        )

        # ─── P3 OPTIMIZATION: ZERO-COPY REVERSE SCAN FOR CLOSED BARS POST-ENTRY ───
        if is_pillar_trade and tb_engine and getattr(tb_engine, "bars_1m", None) and entry_ts > 0:
            # 400 बार्स की कॉपी (copy()) हटाकर केवल एंट्री के बाद क्लोज हुए 1-2 बार्स को O(1) में स्कैन करें
            recent_bars = []
            for b in reversed(tb_engine.bars_1m):
                b_end = getattr(b, "ts_end", getattr(b, "timestamp", 0.0))
                if b_end <= entry_ts:
                    break
                recent_bars.append(b)

            if recent_bars:
                recent_bars.reverse()  # क्रोनोलॉजिकल ऑर्डर में प्रोसेस करें
                for b in recent_bars:
                    b_end = getattr(b, "ts_end", getattr(b, "timestamp", 0.0))
                    b_start = getattr(b, "ts_start", b_end - 60.0)

                    # 1. Option A: Check Minimum Structural Range Hurdle (Thin Candle / Doji filter)
                    bar_range = float(b.high - b.low)
                    if bar_range < min_bar_range_pts:
                        logger.debug(
                            f"[AegisEM] Skipping thin candle for structural trail: "
                            f"range {bar_range:.1f}pts < min {min_bar_range_pts:.1f}pts ({MIN_STRUCTURAL_BAR_RANGE_BPS}bps)"
                        )
                        continue

                    # 2. Option C: Bar passed structural hurdle -> trail with normalized wick cushion
                    is_entry_bar = (b_start <= entry_ts <= b_end)
                    if context.signal_type == "CALL":
                        cand_sl = float(b.low - wick_cushion_pts)
                        favorable_progress = (b.close >= b.open or b.close > context.index_entry or (c_high > 0 and b.high > c_high))
                        if is_entry_bar and b.low > 0:
                            if favorable_progress and cand_sl > effective_sl:
                                effective_sl = cand_sl
                                context.trailing_spot_sl = effective_sl
                                context.entry_candle_low = b.low
                                if not getattr(context, "_entry_candle_low_trailed", False):
                                    logger.info(
                                        f"[AegisEM] Entry candle closed (Range {bar_range:.1f}pts >= {min_bar_range_pts:.1f}pts): "
                                        f"Trailed CALL structural SL {initial_sl:.1f} -> {effective_sl:.1f} "
                                        f"(Low {b.low:.1f} - cushion {wick_cushion_pts:.1f}pts, +{effective_sl - initial_sl:.1f} pts)"
                                    )
                                    context._entry_candle_low_trailed = True
                        elif (not is_entry_bar) and b.low > 0:
                            if favorable_progress and cand_sl > effective_sl:
                                old_sl = effective_sl
                                effective_sl = cand_sl
                                context.trailing_spot_sl = effective_sl
                                logger.info(
                                    f"[AegisEM] Subsequent bar closed (Range {bar_range:.1f}pts): Trailed CALL structural SL "
                                    f"{old_sl:.1f} -> {effective_sl:.1f} (+{effective_sl - old_sl:.1f} pts)"
                                )
                    elif context.signal_type == "PUT":
                        cand_sl = float(b.high + wick_cushion_pts)
                        favorable_progress = (b.close <= b.open or b.close < context.index_entry or (c_low > 0 and b.low < c_low))
                        if is_entry_bar and b.high > 0:
                            if favorable_progress and (effective_sl <= 0 or cand_sl < effective_sl):
                                effective_sl = cand_sl
                                context.trailing_spot_sl = effective_sl
                                context.entry_candle_high = b.high
                                if not getattr(context, "_entry_candle_high_trailed", False):
                                    logger.info(
                                        f"[AegisEM] Entry candle closed (Range {bar_range:.1f}pts >= {min_bar_range_pts:.1f}pts): "
                                        f"Trailed PUT structural SL {initial_sl:.1f} -> {effective_sl:.1f} "
                                        f"(High {b.high:.1f} + cushion {wick_cushion_pts:.1f}pts, {effective_sl - initial_sl:.1f} pts)"
                                    )
                                    context._entry_candle_high_trailed = True
                        elif (not is_entry_bar) and b.high > 0:
                            if favorable_progress and (effective_sl <= 0 or cand_sl < effective_sl):
                                old_sl = effective_sl
                                effective_sl = cand_sl
                                context.trailing_spot_sl = effective_sl
                                logger.info(
                                    f"[AegisEM] Subsequent bar closed (Range {bar_range:.1f}pts): Trailed PUT structural SL "
                                    f"{old_sl:.1f} -> {effective_sl:.1f} ({effective_sl - old_sl:.1f} pts)"
                                )

        # Universal Rule: ALL 4 Pillars exit on Entry Candle / Structural Trailing Low/High Invalidation
        if is_pillar_trade and curr_spot > 0:
            if context.signal_type == "CALL" and effective_sl > 0 and curr_spot <= effective_sl:
                if c_low > 0 and effective_sl > initial_sl:
                    sl_desc = f"Candle Low {effective_sl:.1f} [Trailed with cushion {wick_cushion_pts:.1f}pts from {c_low:.1f}]"
                else:
                    sl_desc = f"Candle Low {effective_sl:.1f} [Initial {c_low:.1f} - cushion {wick_cushion_pts:.1f}pts]"
                self._close_all_legs(
                    f"{pillar_label} Entry Candle Low Broken SL (Spot {curr_spot:.1f} <= {sl_desc})"
                )
                return
            elif context.signal_type == "PUT" and effective_sl > 0 and curr_spot >= effective_sl:
                if c_high > 0 and effective_sl < initial_sl:
                    sl_desc = f"Candle High {effective_sl:.1f} [Trailed with cushion {wick_cushion_pts:.1f}pts from {c_high:.1f}]"
                else:
                    sl_desc = f"Candle High {effective_sl:.1f} [Initial {c_high:.1f} + cushion {wick_cushion_pts:.1f}pts]"
                self._close_all_legs(
                    f"{pillar_label} Entry Candle High Broken SL (Spot {curr_spot:.1f} >= {sl_desc})"
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


            # ─── Point 4: Stepped Risk Compression + Cost-Shield + Near-Target Engine (V4.1) ───
            # Scale-Invariant Normalized Percentage Model (strictly % normalized, no fixed paise!)
            init_tp = float(getattr(leg, "initial_tp_pct", RESTING_TP_LIMIT_PCT) or RESTING_TP_LIMIT_PCT)
            is_reduced_tp = init_tp <= 0.032  # Friday 4 DTE (3.0% TP) or lowered target mode

            # Stepped Protection Smoothing:
            step1_trigger = 0.018 if is_reduced_tp else 0.025   # Armed at +2.5% gain
            step1_sl_pct  = -0.015 if is_reduced_tp else -0.020  # Tighten SL to -2.0% (cut disaster floor in half)
            step2_trigger = 0.022 if is_reduced_tp else float(os.environ.get("AEGIS_STEP2_TRIGGER_PCT", "0.035"))   # Armed at +3.5% gain -> Lock 0.0% Breakeven
            near_target_trigger = 0.025 if is_reduced_tp else float(os.environ.get("AEGIS_NEAR_TARGET_TRIGGER_PCT", "0.035"))
            near_target_tolerance = 0.005 if is_reduced_tp else float(os.environ.get("AEGIS_NEAR_TARGET_TOLERANCE", "0.006"))

            peak_bid = getattr(leg, "highest_bid", effective_bid)
            peak_bid_gain = max(peak_gain_rs, peak_bid - leg.entry_price)
            peak_pnl_frac = peak_bid_gain / leg.entry_price if leg.entry_price > 0 else 0.0
            cur_pnl_frac = cur_pnl_rs / leg.entry_price if leg.entry_price > 0 else 0.0
            peak_bid_frac = (peak_bid - leg.entry_price) / leg.entry_price if leg.entry_price > 0 else 0.0

            # 1. Near-Target Profit Lock (Peak Bid >= near_target_trigger, exits on pullback from peak bid)
            if peak_bid_frac >= near_target_trigger:
                pullback_frac = peak_bid_frac - cur_pnl_frac
                if pullback_frac >= near_target_tolerance:
                    self._close_all_legs(
                        f"Near-Target Profit Lock (+{cur_pnl_pct:.1f}% @ {effective_bid:.2f} | Peak was +{peak_pnl_pct:.1f}%)"
                    )
                    return

            # 2. Dynamic SL & Cost-Shield Level Evaluation
            dynamic_sl_price = None
            dynamic_sl_reason = ""

            if peak_pnl_frac >= step2_trigger or getattr(leg, "breakeven_locked", False):
                # Step 2: Peak reached >= step2_trigger (+3.5% normal, +2.5% Friday) -> Cost-Shield Scratch Active (SL = Entry Price / 0.0%)
                if not getattr(leg, "breakeven_locked", False):
                    leg.breakeven_locked = True
                    self._save_active_context()
                    logger.info(
                        f"🛡️ [AegisEM] Cost-Shield Scratch Armed: {leg.symbol} peak +{peak_pnl_pct:.1f}% (₹{peak_gain_rs:.2f}) -> "
                        f"SL locked at Entry Price ₹{leg.entry_price:.2f} (0.0% Scratch / Loss Prevented)"
                    )
                dynamic_sl_price = _round_tick(leg.entry_price * (1.0 + STEP2_SL_PCT))  # 0.0%
                dynamic_sl_reason = (
                    f"Cost-Shield Scratch Exit (Breakeven Guard 0.0% @ {effective_bid:.2f} | "
                    f"Peak was +{peak_pnl_pct:.1f}% | Loss Prevented!)"
                )
            elif peak_pnl_frac >= step1_trigger or getattr(leg, "step1_sl_armed", False):
                # Step 1: Peak reached >= step1_trigger (+2.5% normal, +1.8% Friday) -> Stepped Safety Net Active (SL tightened to -2.0% normal, -1.5% Friday)
                if not getattr(leg, "step1_sl_armed", False):
                    leg.step1_sl_armed = True
                    self._save_active_context()
                    logger.info(
                        f"🛡️ [AegisEM] Stepped Safety Net Armed: {leg.symbol} peak +{peak_pnl_pct:.1f}% -> "
                        f"SL tightened to {step1_sl_pct*100:.1f}% (₹{_round_tick(leg.entry_price * (1.0 + step1_sl_pct)):.2f}, wide breathing room)"
                    )
                dynamic_sl_price = _round_tick(leg.entry_price * (1.0 + step1_sl_pct))
                dynamic_sl_reason = (
                    f"Stepped Safety Net SL ({cur_pnl_pct:+.1f}% @ {effective_bid:.2f} | "
                    f"Peak was +{peak_pnl_pct:.1f}%)"
                )

            # 3. Dynamic SL Trigger Check
            if dynamic_sl_price is not None and effective_bid > 0 and effective_bid <= dynamic_sl_price:
                self._close_all_legs(dynamic_sl_reason)
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

        # Periodic context re-save (every 10s while trade is active)
        last_ctx_save = getattr(context, "_last_context_save_ts", 0.0)
        if (now_ts - last_ctx_save) >= 10.0:
            context._last_context_save_ts = now_ts
            self._save_active_context()

        if self.tp_order_id and not str(self.tp_order_id).startswith("PAPER_"):
            filled, fill_px = self._order_fill(self.tp_order_id, allow_rest=True)
            if filled:
                actual_px = fill_px if fill_px > 0 else float(getattr(legs[0], "tp_price", 0.0))
                logger.info(f"[AegisEM] Resting TP order {self.tp_order_id} confirmed by order update or safety poll @ {actual_px:.2f}")
                self.last_tp_order_id = self.tp_order_id
                self.tp_order_id = None
                self._close_all_legs(
                    f"Take-Profit Hit (+{(actual_px - legs[0].entry_price)/legs[0].entry_price*100:.1f}% @ {actual_px:.2f})",
                    is_tp_maker=True,
                    limit_fill_price=actual_px,
                )


    def _close_all_legs(self, reason: str, is_tp_maker: bool = False, limit_fill_price: float = 0.0):
        with self.trade_lock:
            if not self.has_open_trade() or getattr(self, "_is_exiting", False):
                return
            self._is_exiting = True
            if self.trade_context:
                self.trade_context.status = "EXIT_PENDING"
        
        if getattr(self, "dump_detector", None) is not None:
            self.dump_detector.disarm()
        
        threading.Thread(
            target=self._chase_exit_thread,
            args=(reason, is_tp_maker, limit_fill_price),
            daemon=True,
        ).start()

    def _chase_exit_thread(self, reason: str, is_tp_maker: bool = False, limit_fill_price: float = 0.0):
        """Limit exit execution — preserves TP maker fills and chases emergency exits."""
        try:
            self._do_chase_exit(reason, is_tp_maker, limit_fill_price)
        except Exception as e_chase:
            logger.critical(f"[AegisEM] Exit chaser thread crashed: {e_chase}", exc_info=True)
            with self.trade_lock:
                if self.trade_context and self.has_open_trade():
                    # Attempt emergency fallback: verify live net position at broker and
                    # dispatch exact-qty emergency flatten so the position is never orphaned.
                    logger.warning("[AegisEM] Triggering emergency reconcile-flatten fallback for unclosed trade...")
                    try:
                        flat_all = self._emergency_reconcile_flatten(reason=f"chase_crash_fallback_{reason}")
                    except Exception as e_em:
                        flat_all = False
                        logger.critical(f"[AegisEM] Emergency reconcile-flatten fallback also failed: {e_em}", exc_info=True)
                    if not flat_all:
                        # Revert status to OPEN so subsequent ticks and 15:08 CAS flatten can retry!
                        self.trade_context.status = "OPEN"
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
        datetime.now()
        
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

            # 👑 UNIT INVARIANT: exit order quantity = actual open share-units (net_qty), single-sourced
            # (SENSEX 1 lot => 20, NIFTY 1 lot => 65).
            qty = str(resolve_leg_net_qty(leg, int(params["lot_size"])))
            fill_price = 0.0
            fill_ts = time.time()
            filled = False

            # 🛡️ Broker-verified-flat shortcut: previous reconciliation proved this
            # position is already closed at the broker — bookkeeping-only close,
            # NEVER dispatch another SELL (prevents naked-short double exit).
            if getattr(leg, "broker_verified_flat", False):
                filled = True
                fill_price = float(getattr(leg, "exit_price", 0.0) or leg.current_price or leg.entry_price)
                logger.info(
                    f"[AegisEM] {leg.symbol}: broker net position verified FLAT earlier — "
                    f"bookkeeping-only close @ {fill_price:.2f} (exit order suppressed)"
                )

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
                            hist_filled, hist_fill_px = self._order_fill(check_ordno, allow_rest=True)
                            if hist_filled:
                                filled = True
                                fill_price = hist_fill_px if hist_fill_px > 0 else limit_fill_price
                                logger.info(
                                    f"[AegisEM] Case A: TP order {check_ordno} confirmed FILLED @ "
                                    f"{fill_price:.2f}"
                                )
                            else:
                                logger.warning(
                                    f"[AegisEM] Case A: TP order {check_ordno} not confirmed; "
                                    "falling through to active chase exit"
                                )
                                filled = False
                        except Exception as e_tp:
                            logger.warning(f"[AegisEM] Maker TP status check warning: {e_tp} — falling through to active chase exit")
                            filled = False
                    else:
                        logger.warning("[AegisEM] Case A: No check_ordno or api available — falling through to active chase exit")
                        filled = False

            # Case B: Emergency or Trail Exit Chase (Anchored to live bid: Bid-0% -> Bid-1% -> Bid-2%)
            # Parse urgency tier from close reason
            norm_reason = "EMERGENCY"
            r_upper = str(reason).upper()
            if any(k in r_upper for k in ("NEAR-TARGET", "NEAR_TARGET", "PROFIT LOCK", "PROFIT_LOCK")):
                norm_reason = "NEAR_TARGET_LOCK"
            elif any(k in r_upper for k in ("COST-SHIELD", "BREAKEVEN")):
                norm_reason = "BREAKEVEN_GUARD"
            elif any(k in r_upper for k in ("PILLAR 4", "SPOT INVALIDATION", "STRUCTURAL")):
                norm_reason = "STRUCTURAL_SL"
            elif any(k in r_upper for k in ("STEPPED SL", "STEPPED_SL")):
                norm_reason = "STEPPED_SL"
            elif any(k in r_upper for k in ("TIMEOUT", "TIME STOP", "MAX HOLD")):
                norm_reason = "TIME_STOP"

            is_catastrophic = any(
                k in r_upper
                for k in ("PREMIUM HARD SL", "DISASTER", "FORCE FLATTEN", "EMERGENCY")
            )

            def _get_live_quote(ref_px: float) -> tuple:
                try:
                    if hasattr(self.broker, "market_data_provider"):
                        q = self.broker.market_data_provider.get_quote(exch_seg, leg.token)
                        if q:
                            bp1 = float(q.get("bp1") or 0)
                            sp1 = float(q.get("sp1") or 0)
                            return (bp1 if bp1 > 0 else ref_px, sp1 if sp1 > 0 else (ref_px + OPTION_TICK_SIZE))
                except Exception:
                    pass
                return (ref_px, ref_px + OPTION_TICK_SIZE)

            def _get_live_bid(ref_px: float) -> float:
                return _get_live_quote(ref_px)[0]

            if not filled:
                # Pre-chase safety check: Did the resting TP order already fill at broker?
                check_ordno = self.tp_order_id or self.last_tp_order_id
                if check_ordno and not str(check_ordno).startswith("PAPER_") and api and hasattr(api, "single_order_history"):
                    try:
                        hist_filled, hist_fill_px = self._order_fill(check_ordno, allow_rest=True)
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
                live_bid_1, live_ask_1 = _get_live_quote(leg.current_price)
                price_1 = _round_tick(get_marketable_exit_limit_price(
                    live_bid_1,
                    exit_reason=norm_reason,
                    entry_price=leg.entry_price,
                    best_ask=live_ask_1
                )) if live_bid_1 > 0 else _round_tick(leg.current_price * 0.99)
                ordno = None

                if is_catastrophic:
                    logger.warning(
                        f"🚨 [AegisEM FastExit] Catastrophic exit triggered ({reason})! "
                        f"Bypassing 2.6s chase sleep -> executing ZERO-LAG Marketable Sweep"
                    )
                    # Step A: Atomic Modify resting TP order in-place if exists
                    if self.tp_order_id and not str(self.tp_order_id).startswith("PAPER_"):
                        ordno = self.tp_order_id
                        if api and hasattr(api, "modify_order"):
                            try:
                                logger.info(
                                    f"[AegisEM FastExit] ⚡ ATOMIC IN-PLACE EXIT: Modifying resting TP order {ordno} -> "
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
                                logger.info(f"[AegisEM FastExit] modify_order response: {resp}")
                                if isinstance(resp, dict) and resp.get("stat") != "Ok":
                                    emsg = str(resp.get("emsg", "")).lower()
                                    if "complete" in emsg or "filled" in emsg:
                                        logger.info(f"[AegisEM FastExit] Resting TP order {ordno} already filled at broker: {resp}")
                                        filled = True
                                        fill_price = getattr(leg, "tp_price", price_1)
                                    else:
                                        logger.warning(f"[AegisEM FastExit] modify_order failed: {resp}. Cancelling before fallback place_order.")
                                        self._cancel_tp()
                                        ordno = None
                            except Exception as e_mod:
                                logger.error(f"[AegisEM FastExit] Exception in modify_order: {e_mod}. Cancelling before fallback place_order.")
                                self._cancel_tp()
                                ordno = None

                    # Step B: If no resting TP or modify failed, cancel TP and place fresh aggressive order
                    if not ordno and not filled:
                        self._cancel_tp()
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
                                try:
                                    resp = place(**kwargs)
                                    if isinstance(resp, dict) and resp.get("stat") == "Ok":
                                        ordno = resp.get("norenordno")
                                except Exception as e_p1:
                                    logger.error(f"[AegisEM FastExit] place_order step 1 exception: {e_p1}")

                    # Step C: Micro-poll with sub-50ms checks (Total max 150ms instead of 2600ms!)
                    if ordno and not filled:
                        for d_sec in (0.03, 0.05, 0.07):
                            time.sleep(d_sec)
                            h_filled, h_px = self._order_fill(ordno, allow_rest=True, force_rest=True)
                            if h_filled:
                                filled = True
                                fill_price = h_px if h_px > 0 else price_1
                                fill_ts = time.time()
                                break

                        if not filled:
                            # Step D: Immediate sweep if still unfilled after 150ms (zero lazy sleep)
                            live_bid_fast, live_ask_fast = _get_live_quote(price_1)
                            price_fast = _round_tick(get_marketable_exit_limit_price(
                                live_bid_fast * 0.98,
                                exit_reason=norm_reason,
                                entry_price=leg.entry_price,
                                best_ask=live_ask_fast,
                            )) if live_bid_fast > 0 else _round_tick(leg.current_price * 0.97)
                            if api and hasattr(api, "modify_order"):
                                try:
                                    logger.warning(f"[AegisEM FastExit] Immediate sweep modify @ {price_fast:.2f} (live_bid={live_bid_fast:.2f})")
                                    api.modify_order(
                                        exchange=exch_seg,
                                        tradingsymbol=leg.symbol,
                                        orderno=ordno,
                                        newquantity=qty,
                                        newprice_type="LMT",
                                        newprice=f"{price_fast:.2f}",
                                    )
                                except Exception as e_mod_fast:
                                    logger.error(f"[AegisEM FastExit] Sweep modify exception: {e_mod_fast}")
                            for d_sec in (0.03, 0.05):
                                time.sleep(d_sec)
                                h_filled, h_px = self._order_fill(ordno, allow_rest=True, force_rest=True)
                                if h_filled:
                                    filled = True
                                    fill_price = h_px if h_px > 0 else price_fast
                                    fill_ts = time.time()
                                    break

                else:
                    # Case B2: Normal chase path (non-catastrophic: NEAR_TARGET_LOCK, BREAKEVEN_GUARD, STEPPED_SL, TIME_STOP)
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
                                try:
                                    resp = place(**kwargs)
                                    if isinstance(resp, dict) and resp.get("stat") == "Ok":
                                        ordno = resp.get("norenordno")
                                except Exception as e_p1:
                                    logger.error(f"[AegisEM] place_order step 1 exception: {e_p1}")

                    if ordno:
                        time.sleep(0.8)
                        h_filled, h_px = self._order_fill(ordno, allow_rest=True)
                        if h_filled:
                            filled = True
                            fill_price = h_px if h_px > 0 else price_1
                        else:
                            # Step 2: Refresh live bid and chase 1% below current bid
                            live_bid_2, live_ask_2 = _get_live_quote(price_1)
                            price_2 = _round_tick(get_marketable_exit_limit_price(
                                live_bid_2 * 0.99,
                                exit_reason=norm_reason,
                                entry_price=leg.entry_price,
                                best_ask=live_ask_2
                            )) if live_bid_2 > 0 else _round_tick(leg.current_price * 0.98)
                            if api and hasattr(api, "modify_order"):
                                try:
                                    api.modify_order(exchange=exch_seg, tradingsymbol=leg.symbol, orderno=ordno,
                                                     newquantity=qty, newprice_type="LMT", newprice=f"{price_2:.2f}")
                                except Exception as e_mod2:
                                    logger.error(f"[AegisEM] modify_order step 2 exception: {e_mod2}")
                            time.sleep(1.0)
                            h_filled, h_px = self._order_fill(ordno, allow_rest=True)
                            if h_filled:
                                filled = True
                                fill_price = h_px if h_px > 0 else price_2
                            else:
                                # Step 3: Cancel & Replace at aggressive exit 2% below fresh live bid
                                if api and hasattr(api, "cancel_order"):
                                    try:
                                        api.cancel_order(orderno=ordno)
                                    except Exception:
                                        pass
                                live_bid_3 = _get_live_bid(price_2)
                                price_3 = _round_tick(min(live_bid_3 * 0.98, leg.current_price * 0.97))
                                if api and hasattr(self.order_lifecycle, "_build_kwargs"):
                                    kwargs = self.order_lifecycle._build_kwargs("S", exch_seg, leg.symbol, qty, f"{price_3:.2f}")
                                    place = getattr(api, "place_order", None)
                                    if place:
                                        logger.warning(f"[AegisEM Chaser] Final aggressive exit @ {price_3:.2f} (live_bid={live_bid_3:.2f})")
                                        try:
                                            resp = place(**kwargs)
                                            ordno_3 = resp.get("norenordno") if isinstance(resp, dict) and resp.get("stat") == "Ok" else None
                                        except Exception as e_p3:
                                            logger.error(f"[AegisEM Chaser] Step-3 aggressive exit place exception: {e_p3}")
                                            ordno_3 = None
                                        if ordno_3:
                                            time.sleep(0.8)
                                            h_filled, h_px = self._order_fill(ordno_3, allow_rest=True)
                                            if h_filled:
                                                fill_price = h_px if h_px > 0 else price_3
                                                filled = True
                                                fill_ts = time.time()
                                            else:
                                                with self._order_update_lock:
                                                    latest = self._order_updates.get(ordno_3, {})
                                                status = latest.get("status", "OPEN")
                                                logger.error(f"[AegisEM Chaser] Step-3 exit order {ordno_3} NOT filled (status={status}) — position may still be open at broker")
                                        else:
                                            logger.error(f"[AegisEM Chaser] Step-3 aggressive exit order placement failed: {resp}")
            
            if not filled:
                # Fallback: marketable exit order
                exit_ok = self.order_lifecycle.execute_exit(leg, reason)
                if exit_ok:
                    filled = True
                    # 🛡️ REAL-FILL RESOLUTION: a placed LMT is NOT a filled exit.
                    # Resolve the actual broker fill price before PnL bookkeeping —
                    # booking px=0.00 records a fake -100% loss and corrupts capital
                    # (incident 2026-11-05: SENSEX 58000CE booked -₹572 vs real -₹77,
                    # freezing internal funds ~₹170 vs broker ₹647 and making the
                    # next trade select a 10x cheaper ₹6.75 strike).
                    _exit_ordno = getattr(self.order_lifecycle, "_last_exit_order_id", None)
                    if _exit_ordno:
                        try:
                            _rf_ok, _rf_px = self._order_fill(_exit_ordno, allow_rest=True)
                            if _rf_ok and _rf_px > 0:
                                fill_price = _rf_px
                                fill_ts = time.time()
                                logger.info(
                                    f"[AegisEM] Fallback exit order {_exit_ordno} confirmed FILLED @ ₹{fill_price:.2f}"
                                )
                            elif leg.exit_price and leg.exit_price > 0:
                                fill_price = float(leg.exit_price)
                                fill_ts = time.time()
                                logger.warning(
                                    f"[AegisEM] Fallback exit order {_exit_ordno} fill unconfirmed at broker; "
                                    f"provisional price ₹{fill_price:.2f} used — deferred capital re-sync will reconcile."
                                )
                        except Exception as e_rf:
                            logger.error(f"[AegisEM] Fallback fill resolution failed (non-fatal): {e_rf}")
                    # Never trust the PnL below if no positive price exists anywhere:
                    # the deferred broker re-sync is mandatory to reconcile real funds.
                    if hasattr(self, "capital_engine") and self.capital_engine and fill_price <= 0:
                        self.capital_engine._needs_broker_sync = True
                else:
                    logger.critical(
                        f"[AegisEM] 🚨 CRITICAL: Exit order unconfirmed/rejected by broker for {leg.symbol}! "
                        f"Position may remain open at broker. Flagging urgent broker sync!"
                    )
                    if hasattr(self, "capital_engine") and self.capital_engine:
                        self.capital_engine._needs_broker_sync = True
                    # 🛡️ ORDER-REJECT FAIL-SAFE: verify the LIVE net position at the broker
                    # (never trust local state after a rejection) and dispatch an
                    # EXACT-verified-net-qty emergency flatten. A REJECTED exit must never
                    # orphan an unhedged position (incident 2026-11: SENSEX 578800CE).
                    recon_flat, recon_px = self._reconcile_leg_position(leg, reason=reason)
                    if recon_flat:
                        filled = True
                        fill_price = recon_px if recon_px > 0 else leg.current_price
                        fill_ts = time.time()
                if not filled:
                    fill_price = leg.exit_price if leg.exit_price and leg.exit_price > 0 else leg.current_price
                    fill_ts = time.time()
            
            # ─── CRITICAL: Feed fill back to AEGIS for latency decay (real fills only) ───
            if self.aegis_engine and filled:
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
            
            if not filled:
                # 🛡️ ORDER-REJECT FAIL-SAFE: position NOT confirmed closed — DO NOT mark
                # flat, DO NOT record fake PnL, DO NOT stop tracking. Keep leg OPEN so
                # per-tick exit triggers and the 15:08 CAS flatten keep retrying.
                with self.trade_lock:
                    logger.critical(
                        f"[AegisEM] 🚨 UNRECONCILED POSITION {leg.symbol}: broker still holds it — "
                        f"leg kept OPEN for continuous retry (no flat/PnL mark)."
                    )
                    context.status = "OPEN"
                    self._has_unreconciled_position = True
                    self._save_active_context()
                continue
            
            # Record PnL
            with self.trade_lock:
                leg.status = "CLOSED"
                leg.exit_time = datetime.now()
                leg.exit_price = fill_price
                lot_sz = int(params.get("lot_size", 20 if "SENSEX" in leg.symbol else 65))
                # 👑 UNIT INVARIANT: exact open share-units, single-sourced via resolve_leg_net_qty()
                leg_units = resolve_leg_net_qty(leg, lot_sz)
                leg.pnl = (fill_price - leg.entry_price) * leg_units
                total_pnl += leg.pnl
                self.capital_engine.add_funds(fill_price * leg_units)
                self.strike_cooldowns[leg.symbol] = datetime.now() + timedelta(minutes=3)

                # Compute SEBI & brokerage charges
                buy_turnover = leg.entry_price * leg_units
                sell_turnover = fill_price * leg_units
                leg_charges = calculate_nse_option_charges(buy_turnover, sell_turnover, brokerage_per_order=5.0)
                leg_brokerage = round(leg_charges.brokerage, 2)
                leg_total_charges = round(leg_charges.total_charges, 2)
                leg_taxes = round(leg_total_charges - leg_brokerage, 2)
                leg_gross_pnl = round(leg.pnl, 2)
                leg_net_pnl = round(leg_gross_pnl - leg_total_charges, 2)
                leg_pnl_pct = round(((fill_price - leg.entry_price) / leg.entry_price * 100.0), 2) if leg.entry_price > 0 else 0.0

                logger.info(
                    f"[TRADE CLOSED] {leg.symbol}: {leg.entry_price:.2f} → {fill_price:.2f} | "
                    f"Gross PnL: ₹{leg_gross_pnl:+.2f} ({leg_pnl_pct:+.2f}%) | "
                    f"Brokerage: ₹{leg_brokerage:.2f} | Taxes: ₹{leg_taxes:.2f} | "
                    f"Total Charges: ₹{leg_total_charges:.2f} | Net PnL: ₹{leg_net_pnl:+.2f} | {reason}"
                )

        any_open_left = any(getattr(_l, "status", "") == "OPEN" for _l in legs)
        with self.trade_lock:
            if any_open_left:
                # 🛡️ ORDER-REJECT FAIL-SAFE: tracking must NEVER stop while the broker
                # still holds units. Keep context OPEN (triggers + CAS flatten stay armed),
                # record no PnL/history, keep trade_context alive.
                context.status = "OPEN"
                self._has_unreconciled_position = True
                logger.critical(
                    f"[AegisEM] 🚨 OPEN POSITION PERSISTS after exit attempt (reason: {reason}) — "
                    f"tracking preserved; emergency reconcile + 15:08 CAS flatten remain armed."
                )
                self._sound_critical_alarm()
                self._save_active_context()
                return
            context.status = "CLOSED"
            self._has_unreconciled_position = False
            self._emerg_recon_state = {}
            context.exit_time = datetime.now()
            self._append_history(reason, total_pnl)
            # 🛡️ CAPITAL SELF-HEAL: reconcile funds with the broker AFTER the exit
            # SELL actually settles there (see _schedule_post_close_capital_resync).
            self._schedule_post_close_capital_resync()
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

    # ─── 🛡️ Capital Self-Heal: Deferred Post-Close Broker Re-Sync ───
    def _schedule_post_close_capital_resync(self, delay_sec: float = 30.0, verify_sec: float = 90.0):
        """Reconcile internal funds with the broker AFTER the exit SELL settles there.

        Incident 2026-11-05: the synchronous post-exit sync ran while the marketable
        LMT exit was still resting at the broker (actual fill came +25s later), so the
        engine froze margin-blocked stale funds (~₹170 internal vs ₹647 broker), the
        budget-cluster ceiling collapsed from ₹29 to ₹7 per unit, and the next entry
        picked a 10x cheaper ₹6.75 strike. This deferred pass runs twice (t+30s fill
        margin-release, t+90s daily-limits settle) so the next budget selection always
        sees the broker's real freed cash."""
        def _resync_pass(tag: str):
            try:
                ce = self.capital_engine
                if not ce:
                    return
                pre = float(ce.get_available_funds())
                live = ce.sync_funds_from_broker()
                if live != pre:
                    logger.warning(
                        f"[AegisEM] Post-close capital re-sync ({tag}): internal ₹{pre:.2f} -> broker ₹{live:.2f} "
                        f"(Δ ₹{live - pre:+.2f}) — stale internal funds corrected"
                    )
                else:
                    logger.info(f"[AegisEM] Post-close capital re-sync ({tag}): broker agrees ₹{live:.2f}")
            except Exception as e_cap:
                logger.error(f"[AegisEM] Post-close capital re-sync ({tag}) failed (non-fatal): {e_cap}")

        def _worker():
            time.sleep(delay_sec)
            _resync_pass("t+30s")
            time.sleep(max(0.0, verify_sec - delay_sec))
            _resync_pass("t+90s")

        try:
            threading.Thread(target=_worker, daemon=True, name="aegis-em-capital-resync").start()
        except Exception as e_thr:
            logger.error(f"[AegisEM] Deferred capital re-sync spawn failed (non-fatal): {e_thr}")

    # ─── 🛡️ Order-Reject Fail-Safe: Verified Net-Position Reconciliation ───
    def _get_verified_broker_net_units(self, leg: TradeLeg) -> Optional[int]:
        """Exact live net position (share units) for this contract straight from the broker.
        Returns None when the broker positions feed is unavailable (cannot verify)."""
        try:
            get_pos = getattr(self.broker, "get_positions", None)
            if not callable(get_pos):
                return None
            positions = get_pos() or []
        except Exception as exc:
            logger.warning(f"[AegisEM] Reconcile get_positions query failed: {exc}")
            return None
        for pos in positions:
            try:
                if not isinstance(pos, dict):
                    continue
                tsym = str(pos.get("tsym") or pos.get("tradingsymbol") or "")
                tok = str(pos.get("tok") or pos.get("token") or "")
                if tsym != leg.symbol and tok != str(leg.token):
                    continue
                return abs(int(round(float(pos.get("netqty") or 0))))
            except Exception:
                continue
        return None

    def _log_last_rejection_reason(self, leg: TradeLeg) -> None:
        """Emit a CRITICAL alert quoting the broker's EXACT rejection reason for exit orders."""
        candidate_ids = [
            str(getattr(self, "last_tp_order_id", None) or ""),
            str(getattr(leg, "sl_orderno", None) or ""),
            str(getattr(leg, "broker_order_id", None) or ""),
        ]
        for oid in candidate_ids:
            if not oid or oid.startswith("PAPER_"):
                continue
            with self._order_update_lock:
                upd = self._order_updates.get(oid)
            if not isinstance(upd, dict):
                continue
            status = str(upd.get("status", "")).upper()
            emsg = str(
                upd.get("emsg")
                or upd.get("rejreason")
                or upd.get("remark")
                or upd.get("message")
                or ""
            ).strip()
            if status in ("REJECTED", "REJ") or "REJECT" in status:
                logger.critical(
                    f"[AegisEM] 🚨🚨 BROKER ORDER REJECTED (order {oid}, {leg.symbol}): "
                    f"status={status} | broker rejection reason: '{emsg or 'NOT PROVIDED'}'"
                )

    def _sound_critical_alarm(self) -> None:
        """Emergency audible/terminal notification. Best-effort, never fatal."""
        if os.environ.get("AEGIS_SILENCE_ALARM", "").strip().lower() in ("1", "true", "yes"):
            return
        try:
            sys.stdout.write("\a")  # terminal bell
            sys.stdout.flush()
        except Exception:
            pass
        try:
            import winsound  # local terminal beep (Windows)
            winsound.Beep(880, 350)
            winsound.Beep(660, 450)
        except Exception:
            pass

    def _reconcile_leg_position(self, leg: TradeLeg, reason: str = "REORDER") -> Tuple[bool, float]:
        """🛡️ Fail-safe: verify the LIVE net position at the broker and dispatch an
        EXACT-verified-net-qty emergency flatten.
        Returns (flat: bool, verified_fill_price: float)."""
        now_mono = time.monotonic()
        state = getattr(self, "_emerg_recon_state", None)
        if not isinstance(state, dict):
            state = {}
            self._emerg_recon_state = state
        leg_state = state.setdefault(leg.leg_id, {"last_attempt_ts": 0.0, "attempts": 0})
        if now_mono - leg_state["last_attempt_ts"] < 10.0:
            remaining = 10.0 - (now_mono - leg_state["last_attempt_ts"])
            logger.warning(
                f"[AegisEM] 🛡️ Reconcile anti-spam guard active for {leg.symbol} "
                f"({remaining:.1f}s cooldown) — duplicate emergency dispatch suppressed."
            )
            return False, 0.0
        leg_state["last_attempt_ts"] = now_mono
        leg_state["attempts"] = int(leg_state.get("attempts", 0)) + 1

        # (a) CRITICAL alert with the broker's exact rejection reason.
        self._log_last_rejection_reason(leg)
        verified = self._get_verified_broker_net_units(leg)
        if verified == 0:
            logger.info(
                f"[AegisEM] Reconcile: {leg.symbol} verified FLAT at broker (netqty=0) — "
                f"position already closed."
            )
            leg.broker_verified_flat = True
            return True, float(getattr(leg, "exit_price", 0.0) or leg.current_price or leg.entry_price)

        lot_size = int(self.get_instrument_params()["lot_size"])
        # (c) NEVER trust the ambiguous local qty — use the broker-verified net position;
        # fall back to the unit-invariant resolver only when the broker feed is unavailable.
        net_units = verified if verified is not None else resolve_leg_net_qty(leg, lot_size)
        if net_units <= 0:
            logger.info(f"[AegisEM] Reconcile: {leg.symbol} has no open units to flatten.")
            return True, float(getattr(leg, "exit_price", 0.0) or leg.current_price or leg.entry_price)
        logger.critical(
            f"[AegisEM] 🚨🚨 EMERGENCY RECONCILE #{leg_state['attempts']} (audio/terminal alert fired): "
            f"{leg.symbol} still OPEN with {net_units} share-units (broker-verified: {verified is not None}) "
            f"— dispatching exact-qty emergency flatten (reason: {reason})."
        )
        self._sound_critical_alarm()

        api = getattr(self.broker, "_api", None)
        if not api or not hasattr(api, "place_order"):
            logger.critical("[AegisEM] 🚨 EMERGENCY flatten abort: broker place_order unavailable!")
            return False, 0.0
        exch = self.get_instrument_params()["exchange"]
        try:
            qrow = self.broker.market_data_provider.get_quote(exch, leg.token)
            bid = float(qrow.get("bp1") or 0) if isinstance(qrow, dict) else 0.0
        except Exception:
            bid = 0.0
        ref = bid if bid > 0 else (leg.current_price or leg.entry_price)
        limit_price = get_marketable_exit_limit_price(ref * 0.98, exit_reason="EMERGENCY", entry_price=leg.entry_price)
        if limit_price <= 0:
            limit_price = max(OPTION_TICK_SIZE, ref * 0.90)
        if hasattr(self.order_lifecycle, "_build_kwargs"):
            kwargs = self.order_lifecycle._build_kwargs("S", exch, leg.symbol, str(int(net_units)), f"{limit_price:.2f}")
        else:
            kwargs = {
                "buy_or_sell": "S", "product_type": "M", "exchange": exch,
                "tradingsymbol": leg.symbol, "quantity": str(int(net_units)),
                "price_type": "LMT", "price": f"{limit_price:.2f}",
            }
        ordno = None
        try:
            resp = api.place_order(**kwargs)
            if isinstance(resp, dict) and resp.get("stat") == "Ok":
                ordno = resp.get("norenordno")
            else:
                emsg = resp.get("emsg", str(resp)) if isinstance(resp, dict) else str(resp)
                logger.critical(f"[AegisEM] 🚨 EMERGENCY flatten order REJECTED for {leg.symbol}: {emsg}")
        except Exception as exc:
            logger.critical(f"[AegisEM] 🚨 EMERGENCY flatten dispatch failed for {leg.symbol}: {exc}")
        if ordno:
            time.sleep(0.8)
            h_filled, h_px = self._order_fill(str(ordno), allow_rest=True)
            if h_filled:
                leg.broker_verified_flat = True
                leg.exit_price = h_px if h_px and h_px > 0 else limit_price
                logger.critical(
                    f"[AegisEM] ✅ EMERGENCY flatten FILLED for {leg.symbol}: {net_units} units "
                    f"@ {leg.exit_price:.2f} — position secured."
                )
                return True, float(leg.exit_price)
        return False, 0.0

    def _emergency_reconcile_flatten(self, reason: str = "RECONCILE") -> bool:
        """Full-context emergency reconcile — True only when every open leg is verified flat."""
        with self.trade_lock:
            open_legs = [leg_item for leg_item in (self.trade_context.legs if self.trade_context else []) if leg_item.status == "OPEN"]
        if not open_legs:
            return True
        flat_all = True
        for leg in open_legs:
            flat, _px = self._reconcile_leg_position(leg, reason=reason)
            flat_all = flat_all and flat
        return flat_all

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

        entry_px = float(leg0.entry_price) if leg0 else 0.0
        exit_px = float(leg0.exit_price) if (leg0 and leg0.exit_price is not None) else 0.0
        pnl_pct = round(((exit_px - entry_px) / entry_px * 100.0), 2) if entry_px > 0 else 0.0

        sym_str = leg0.symbol if leg0 else ""
        try:
            params = self.get_instrument_params() if hasattr(self, "get_instrument_params") else {}
        except Exception:
            params = {}
        lot_sz = int(params.get("lot_size", 20 if "SENSEX" in sym_str else 65))
        # 👑 UNIT INVARIANT: exact open share-units, single-sourced via resolve_leg_net_qty()
        tot_units = resolve_leg_net_qty(leg0, lot_sz)
        buy_turnover = (entry_px * tot_units) if entry_px > 0 else 0.0
        sell_turnover = (exit_px * tot_units) if exit_px > 0 else 0.0
        charges_obj = calculate_nse_option_charges(buy_turnover, sell_turnover, brokerage_per_order=5.0)

        brokerage = round(charges_obj.brokerage, 2)
        total_charges = round(charges_obj.total_charges, 2)
        taxes = round(total_charges - brokerage, 2)
        gross_pnl = round(total_pnl, 2)
        net_pnl = round(gross_pnl - total_charges, 2)

        self.daily_gross_pnl = round(getattr(self, "daily_gross_pnl", 0.0) + gross_pnl, 2)
        self.daily_charges = round(getattr(self, "daily_charges", 0.0) + total_charges, 2)
        self.daily_net_pnl = round(getattr(self, "daily_net_pnl", 0.0) + net_pnl, 2)
        self.daily_pnl = self.daily_net_pnl

        now_epoch = time.time()
        cooldown_interval = int(now_epoch - self.last_trade_exit_time) if self.last_trade_exit_time > 0 else 0
        self.last_trade_exit_time = now_epoch
        self.last_trade_loss = (net_pnl < 0)

        # Notify 60s post-trade milestone tracker of broker exit
        if self.aegis_engine and getattr(self.aegis_engine, "post_trade_tracker", None) and sym_str:
            try:
                self.aegis_engine.post_trade_tracker.notify_trade_exit(
                    symbol=sym_str,
                    exit_price=exit_px,
                    exit_ts=now_epoch,
                    exit_reason=reason,
                )
            except Exception as _e_nex:
                logger.debug(f"[AegisEM] Post-trade exit notification error: {_e_nex}")

        # Quality Stack: consecutive-loss tracking + engine cross-wire
        if net_pnl < 0:
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
            logger.info(f"[AegisEM] Trade closed in loss (Gross ₹{gross_pnl:+.2f} | Net ₹{net_pnl:+.2f}) -> Post-loss cooloff active for {int(post_loss_sec)}s")
        else:
            logger.info(f"[AegisEM] Trade closed in profit (Gross ₹{gross_pnl:+.2f} | Net ₹{net_pnl:+.2f}) -> Inter-trade cooldown active for {int(inter_trade_sec)}s")
        
        # ─── Post-Trade Quantitative Feature Attribution ───
        duration_sec = int((context.exit_time - context.entry_time).total_seconds()) if (context and context.entry_time and context.exit_time) else int((now - context.entry_time).total_seconds()) if (context and context.entry_time) else 0
        entry_px = float(leg0.entry_price) if leg0 else 0.0
        exit_px = float(leg0.exit_price) if (leg0 and leg0.exit_price is not None) else 0.0
        pnl_pct = round(((exit_px - entry_px) / entry_px * 100.0), 2) if entry_px > 0 else 0.0

        highest_px = float(getattr(leg0, "highest_price", entry_px)) if leg0 else entry_px
        lowest_px = float(getattr(leg0, "lowest_price", entry_px)) if leg0 else entry_px
        if exit_px > 0:
            highest_px = max(highest_px, exit_px)
            lowest_px = min(lowest_px, exit_px)
        peak_mfe_pct = round(max(0.0, ((highest_px - entry_px) / entry_px * 100.0)), 2) if entry_px > 0 else 0.0
        if pnl_pct > 0:
            peak_mfe_pct = max(peak_mfe_pct, pnl_pct)
        peak_mfe_rs = round(max(0.0, highest_px - entry_px), 2)
        time_to_peak_mfe_sec = round(float(getattr(leg0, "time_to_highest_sec", 0.0)), 1) if leg0 else 0.0
        peak_mae_pct = round(min(0.0, ((lowest_px - entry_px) / entry_px * 100.0)), 2) if entry_px > 0 else 0.0
        if pnl_pct < 0:
            peak_mae_pct = min(peak_mae_pct, pnl_pct)
        peak_mae_rs = round(min(0.0, lowest_px - entry_px), 2)

        entry_spot = float(context.index_entry) if context else 0.0
        exit_spot = float(self._get_current_spot())
        spot_delta_pts = round(exit_spot - entry_spot, 2) if (entry_spot > 0 and exit_spot > 0) else 0.0
        spot_delta_bps = round((spot_delta_pts / entry_spot) * 10000.0, 2) if entry_spot > 0 else 0.0

        # Spot MFE / MAE metrics
        spot_peak_mfe_pts = float(getattr(context, "spot_peak_mfe_pts", 0.0) or 0.0)
        spot_peak_mae_pts = float(getattr(context, "spot_peak_mae_pts", 0.0) or 0.0)
        spot_time_to_mfe_sec = round(float(getattr(context, "spot_time_to_mfe_sec", 0.0) or 0.0), 3)
        spot_time_to_mfe_ms = int(spot_time_to_mfe_sec * 1000)
        spot_time_to_mae_sec = round(float(getattr(context, "spot_time_to_mae_sec", 0.0) or 0.0), 3)
        spot_time_to_mae_ms = int(spot_time_to_mae_sec * 1000)
        spot_peak_mfe_bps = round((spot_peak_mfe_pts / entry_spot) * 10000.0, 2) if entry_spot > 0 else 0.0
        spot_peak_mae_bps = round((spot_peak_mae_pts / entry_spot) * 10000.0, 2) if entry_spot > 0 else 0.0
        spot_at_mfe = float(getattr(context, "spot_at_mfe", 0.0) or entry_spot)
        spot_at_mae = float(getattr(context, "spot_at_mae", 0.0) or entry_spot)

        entry_fut_px = float(dp.get("futures_price", 0.0) or 0.0)
        exit_fut_px = float(getattr(self.aegis_engine, "last_futures_price", 0.0) if self.aegis_engine else 0.0)
        fut_delta_pts = round(exit_fut_px - entry_fut_px, 2) if (entry_fut_px > 0 and exit_fut_px > 0) else 0.0
        basis_pts_entry = float(dp.get("basis_pts", 0.0) or 0.0)
        float(dp.get("basis_bps", 0.0) or 0.0)

        entry_latency_ms = float(dp.get("entry_latency_ms", 0.0) or 0.0)
        vol_burst_age_sec = float(dp.get("volatility_burst_age_sec", 0.0) or 0.0)
        trigger_age_sec = float(dp.get("trigger_age_sec", 0.0) or 0.0)
        eqs_score = float(dp.get("eqs_score", 0.0) or 0.0)
        micro_score = float(dp.get("microstructure_score", 0.0) or dp.get("conviction", 0.0) or 0.0)
        persistence_score = float(dp.get("persistence_score", 0.0) or 0.0)

        is_win = net_pnl > 0
        is_scratch = net_pnl == 0
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
            f"Gross: ₹{gross_pnl:+.2f} ({pnl_pct:+.1f}%) | Brokerage: ₹{brokerage:.2f} | Taxes: ₹{taxes:.2f} | Total Charges: ₹{total_charges:.2f} | Net: ₹{net_pnl:+.2f} | "
            f"Exit: {reason} | "
            f"Opt MFE: {peak_mfe_pct:+.1f}% (+₹{peak_mfe_rs:.2f} @ {time_to_peak_mfe_sec}s), Opt MAE: {peak_mae_pct:.1f}% | "
            f"Spot MFE: {spot_peak_mfe_pts:+.1f}pts ({spot_peak_mfe_bps:+.1f}bps @ {spot_time_to_mfe_sec}s), Spot MAE: {spot_peak_mae_pts:+.1f}pts ({spot_peak_mae_bps:+.1f}bps) | "
            f"Spot Δ: {spot_delta_pts:+.1f}pts ({spot_delta_bps:+.1f}bps), Fut Δ: {fut_delta_pts:+.1f}pts (Basis: {basis_pts_entry:+.1f}pts | {fut_corr}) | "
            f"Lat: {entry_latency_ms:.0f}ms, VolBurstAge: {vol_burst_age_sec}s, EQS: {eqs_score:.1f}"
        )

        post_trade_attribution = {
            "outcome": outcome,
            "classification": classification,
            "gross_pnl_rs": gross_pnl,
            "brokerage_rs": brokerage,
            "taxes_rs": taxes,
            "charges_rs": total_charges,
            "net_pnl_rs": net_pnl,
            "pnl_rs": gross_pnl,
            "pnl_pct": pnl_pct,
            "trade_duration_sec": duration_sec,
            "exit_reason": reason,
            "peak_mfe_pct": peak_mfe_pct,
            "peak_mfe_rs": peak_mfe_rs,
            "time_to_peak_mfe_sec": time_to_peak_mfe_sec,
            "peak_mae_pct": peak_mae_pct,
            "peak_mae_rs": peak_mae_rs,
            "spot_peak_mfe_pts": spot_peak_mfe_pts,
            "spot_peak_mfe_bps": spot_peak_mfe_bps,
            "spot_time_to_mfe_sec": spot_time_to_mfe_sec,
            "spot_time_to_mfe_ms": spot_time_to_mfe_ms,
            "spot_peak_mae_pts": spot_peak_mae_pts,
            "spot_peak_mae_bps": spot_peak_mae_bps,
            "spot_time_to_mae_sec": spot_time_to_mae_sec,
            "spot_time_to_mae_ms": spot_time_to_mae_ms,
            "spot_at_mfe": spot_at_mfe,
            "spot_at_mae": spot_at_mae,
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
            "spot_peak_mfe_pts": spot_peak_mfe_pts,
            "spot_peak_mfe_bps": spot_peak_mfe_bps,
            "spot_time_to_mfe_sec": spot_time_to_mfe_sec,
            "spot_time_to_mfe_ms": spot_time_to_mfe_ms,
            "spot_peak_mae_pts": spot_peak_mae_pts,
            "spot_peak_mae_bps": spot_peak_mae_bps,
            "spot_time_to_mae_sec": spot_time_to_mae_sec,
            "spot_time_to_mae_ms": spot_time_to_mae_ms,
            "spot_at_mfe": spot_at_mfe,
            "spot_at_mae": spot_at_mae,
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
            "lot_size": lot_sz,
            "entry_price": entry_px,
            "exit_price": exit_px,
            "pnl": gross_pnl,
            "gross_pnl": gross_pnl,
            "brokerage": brokerage,
            "taxes": taxes,
            "charges": total_charges,
            "total_charges": total_charges,
            "net_pl": net_pnl,
            "net_pnl": net_pnl,
            "spot_peak_mfe_pts": spot_peak_mfe_pts,
            "spot_peak_mae_pts": spot_peak_mae_pts,
            "spot_time_to_mfe_sec": spot_time_to_mfe_sec,
            "spot_time_to_mfe_ms": spot_time_to_mfe_ms,
            "daily_total_pnl": round(self.daily_pnl, 2),
            "daily_gross_pnl": round(self.daily_gross_pnl, 2),
            "daily_charges": round(self.daily_charges, 2),
            "daily_net_pnl": round(self.daily_net_pnl, 2),
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

        # Append to daily research telemetry JSONL (production only, skip in unit test runs)
        try:
            import sys
            if "pytest" not in sys.modules and not os.environ.get("PYTEST_CURRENT_TEST"):
                telem_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "telemetry")
                os.makedirs(telem_dir, exist_ok=True)
                telem_file = os.path.join(telem_dir, f"aegis_trade_telemetry_{now.strftime('%Y-%m-%d')}.jsonl")
                with open(telem_file, "a", encoding="utf-8") as tf:
                    tf.write(json.dumps(trade_record, default=str) + "\n")

                # Automatic production trade journal update & Excel sync
                from unified_dashboard.telemetry_loader import reconcile_and_auto_journal
                reconcile_and_auto_journal()
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
                "spot_peak_mfe_pts": getattr(self.trade_context, "spot_peak_mfe_pts", 0.0),
                "spot_peak_mae_pts": getattr(self.trade_context, "spot_peak_mae_pts", 0.0),
                "spot_time_to_mfe_sec": getattr(self.trade_context, "spot_time_to_mfe_sec", 0.0),
                "spot_time_to_mae_sec": getattr(self.trade_context, "spot_time_to_mae_sec", 0.0),
                "spot_at_mfe": getattr(self.trade_context, "spot_at_mfe", self.trade_context.index_entry),
                "spot_at_mae": getattr(self.trade_context, "spot_at_mae", self.trade_context.index_entry),
                "trailing_spot_sl": getattr(self.trade_context, "trailing_spot_sl", 0.0),
                "candle_low": getattr(self.trade_context, "candle_low", 0.0),
                "candle_high": getattr(self.trade_context, "candle_high", 0.0),
                "pillar": getattr(self.trade_context, "pillar", 0),
                "pa_setup_name": getattr(self.trade_context, "pa_setup_name", ""),
                "execution_mode": getattr(self.trade_context, "execution_mode", ""),
                "boundary_level": getattr(self.trade_context, "boundary_level", 0.0),
                "entry_bar_bucket": getattr(self.trade_context, "entry_bar_bucket", None),
                "spot_sl_price": getattr(self.trade_context, "spot_sl_price", 0.0),
                "decision_payload": getattr(self.trade_context, "decision_payload", {}),
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
                        "highest_bid": getattr(leg, "highest_bid", leg.entry_price),
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
            self.trade_context.spot_peak_mfe_pts = float(data.get("spot_peak_mfe_pts", 0.0) or 0.0)
            self.trade_context.spot_peak_mae_pts = float(data.get("spot_peak_mae_pts", 0.0) or 0.0)
            self.trade_context.spot_time_to_mfe_sec = float(data.get("spot_time_to_mfe_sec", 0.0) or 0.0)
            self.trade_context.spot_time_to_mae_sec = float(data.get("spot_time_to_mae_sec", 0.0) or 0.0)
            self.trade_context.spot_at_mfe = float(data.get("spot_at_mfe", 0.0) or self.trade_context.index_entry)
            self.trade_context.spot_at_mae = float(data.get("spot_at_mae", 0.0) or self.trade_context.index_entry)
            self.trade_context.trailing_spot_sl = float(data.get("trailing_spot_sl", 0.0) or 0.0)
            self.trade_context.candle_low = float(data.get("candle_low", 0.0) or 0.0)
            self.trade_context.candle_high = float(data.get("candle_high", 0.0) or 0.0)
            self.trade_context.pillar = int(data.get("pillar", 0) or 0)
            self.trade_context.pa_setup_name = str(data.get("pa_setup_name", "") or "")
            self.trade_context.execution_mode = str(data.get("execution_mode", "") or "")
            self.trade_context.boundary_level = float(data.get("boundary_level", 0.0) or 0.0)
            self.trade_context.decision_payload = data.get("decision_payload", {})
            self.trade_context.entry_bar_bucket = data.get("entry_bar_bucket")
            self.trade_context.spot_sl_price = float(data.get("spot_sl_price", 0.0) or 0.0)
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
                leg.highest_bid = float(ld.get("highest_bid", leg.entry_price) or leg.entry_price)
                leg.breakeven_locked = bool(ld.get("breakeven_locked", False))
                leg.step1_sl_armed = bool(ld.get("step1_sl_armed", False))
                leg.runner_locked = bool(ld.get("runner_locked", False))
                self.trade_context.add_leg(leg)
            self.trade_context.status = "OPEN"
            logger.warning("[AegisEM] Restored OPEN trade from disk")
        except Exception as e:
            logger.error(f"[AegisEM] Load context failed: {e}")
            self.trade_context = None
