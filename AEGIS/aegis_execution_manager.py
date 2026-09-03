# aegis_execution_manager.py
# AEGIS V2.0 — Execution Manager for Gamma Burst Engine
# Parses GAMMA_BURST_ENTRY signals, manages positions, feeds fill latency to AEGIS core

from __future__ import annotations

import json
import os
import queue
import threading
import time
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
MAX_HOLD_SEC = 300.0                    # 5-minute hard ceiling
AEGIS_ENTRY_CUTOFF = dt_time(15, 0, 0)  # No new entries after 15:00
AEGIS_FORCE_FLATTEN = dt_time(15, 10, 0) # Force flatten at 15:10
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "3")) # Hard safety cap: max 3 trades per day

# ─── Watertight Retail Target, Breakeven Guard & Runner Lock Constants ───
TARGET_PROFIT_PCT = 0.040               # +4.0% Target Profit (Resting DAY Limit target)
STOP_LOSS_PCT = 0.035                   # -3.5% Hard Stop Loss
BREAKEVEN_TRIGGER_PCT = 0.020           # Arm Breakeven Guard when peak >= +2.0%
BREAKEVEN_LOCK_PCT = 0.005              # Lock +0.5% (covers all statutory fees + brokerage)
RUNNER_TRIGGER_PCT = 0.070              # Arm Runner Lock when peak >= +7.0%
RUNNER_FLOOR_PCT = 0.050                # Minimum +5.0% floor on runner pullbacks
RUNNER_TRAIL_PCT = 0.020                # Trail 2.0% below peak once runner armed

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
        self.trade_lock = threading.Lock()
        
        # Cooldown & Inter-trade Intervals
        self.last_trade_exit_time: float = 0.0
        self.last_trade_loss: bool = False
        self.inter_trade_cooldown_sec: float = 1200.0  # 20 minutes minimum between any trade exits
        self.post_loss_cooloff_sec: float = 1200.0     # 20 minutes mandatory freeze after a loss
        
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
        return bool(self.trade_context and str(self.trade_context.status).upper() in ("OPEN", "PENDING", "EXIT_PENDING", "SUBMITTED"))

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
                if self.aegis_engine:
                    self.aegis_engine.check_pending_edges(time.time(), self._get_current_spot())

                # 4. Check pre-market bridge (09:08 - 09:15:30)
                now_t = datetime.now().time()
                if dt_time(9, 8, 0) <= now_t <= dt_time(9, 15, 30):
                    self._handle_premarket_bridge()

            except Exception as e:
                logger.error(f"[AegisEM] Main loop error: {e}", exc_info=True)
                time.sleep(1.0)

    def _handle_premarket_bridge(self):
        """Processes 09:08-09:14 Pre-Market equilibrium from ARGUS and arms opening trade."""
        pm_state = read_premarket_state()
        if not pm_state:
            return
        ind_spot = pm_state.get("indicative_spot", 0.0)
        gap_pct = pm_state.get("gap_pct", 0.0)
        direction = pm_state.get("direction", "NEUTRAL")
        
        if ind_spot > 0 and self.aegis_engine:
            if self.aegis_engine.last_spot != ind_spot:
                logger.info(f"[AegisEM] Pre-Market Bridge: Pre-aligning ATM to {ind_spot:.2f} (Gap: {gap_pct:+.2f}%)")
                self.aegis_engine._on_underlying_tick("INDEX", ind_spot, time.time())

        # If strong gap bias at market open (09:15:00 - 09:15:20) and no open trade, fire opening trade
        now_t = datetime.now().time()
        if dt_time(9, 15, 0) <= now_t <= dt_time(9, 15, 20) and not self.has_open_trade() and self.trades_today == 0:
            if abs(gap_pct) >= 0.25 and direction in ("UP", "DOWN"):
                if not getattr(self, "_opening_order_fired", False):
                    self._opening_order_fired = True
                    logger.info(f"[AegisEM] 🔥 Triggering Opening Momentum Trade based on 09:08 Gap ({gap_pct:+.2f}% {direction})!")
                    self._process_aegis_signal({
                        "type": "GAMMA_BURST_ENTRY",
                        "signal": direction,
                        "lp": ind_spot if ind_spot > 0 else self._get_current_spot(),
                        "conviction": 5.0,
                        "metrics": {"source": "09:08_PREMARKET_BRIDGE", "gap_pct": gap_pct}
                    })

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
        if self.last_trade_date != now.date():
            self.trades_today = 0
            self.daily_pnl = 0.0
            self.last_trade_date = now.date()
        # Daily trade limit (Strict 3 trades max per day)
        max_daily_trades = MAX_TRADES_PER_DAY
        if self.trades_today >= max_daily_trades:
            logger.warning(f"[AegisEM] Daily trade limit ({max_daily_trades}) reached")
            return

        # Inter-trade Cooldown (Minimum 120s pause between trades to prevent whip capture)
        now_epoch = time.time()
        if self.last_trade_exit_time > 0 and now_epoch < (self.last_trade_exit_time + self.inter_trade_cooldown_sec):
            rem = int((self.last_trade_exit_time + self.inter_trade_cooldown_sec) - now_epoch)
            logger.warning(f"[AegisEM] Entry blocked — inter-trade cooldown active ({rem}s remaining)")
            return

        # Post-Loss Freeze (Mandatory 5-minute freeze after a loss to allow market turbulence to settle)
        if self.last_trade_loss and self.last_trade_exit_time > 0 and now_epoch < (self.last_trade_exit_time + self.post_loss_cooloff_sec):
            rem = int((self.last_trade_exit_time + self.post_loss_cooloff_sec) - now_epoch)
            logger.warning(f"[AegisEM] Entry blocked — post-loss cooloff active ({rem}s remaining)")
            return

        with self.trade_lock:
            if self.has_open_trade():
                logger.warning("[AegisEM] Entry blocked — trade already open")
                return
            self._is_entering = True

        try:
            if not self.can_enter(now):
                logger.warning(f"[AegisEM] Entry blocked — outside window (cutoff {AEGIS_ENTRY_CUTOFF})")
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
                return

            opt_row, quantity, ltp = allocations[0]
            symbol = opt_row["symbol"]
            token_str = str(opt_row["token"])
            
            if symbol in self.strike_cooldowns and now < self.strike_cooldowns[symbol]:
                logger.warning(f"[AegisEM] Strike {symbol} in cooldown")
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
                                return
                            sq1 = int(float(q.get("sq1") or 0))
                            if sq1 < 3:
                                logger.warning(f"[AegisEM] Ask size too thin: sq1={sq1}")
                                return
                except Exception as e:
                    logger.error(f"[AegisEM] Pre-trade check failed: {e}")
                    return

            # Execute entry
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
                        "signal_ts": float(signal.get("timestamp") or 0.0),
                    },
                )
                leg = TradeLeg(token_str, symbol, float(opt_row["strike"]), signal_type, quantity, ltp)
                
                if not self.order_lifecycle.execute_entry(leg, live_ask=live_ask or ltp):
                    logger.error(f"[AegisEM] Entry order rejected for {symbol}")
                    self.trade_context = None
                    return
                
                self.trade_context.add_leg(leg)

            self.capital_engine.deduct_funds(leg.entry_price * quantity * params["lot_size"])
            self.trades_today += 1
            self._save_trades_today()
            self.tp_order_id = None

            # Place initial dynamic TP (scales from 5.5% to 10.0%)
            rec_tp = float(metrics.get("recommended_tp_pct") or TARGET_PROFIT_PCT)
            self._place_initial_take_profit(leg, params, target_pct=rec_tp)
        finally:
            self._is_entering = False
        self._save_active_context()
        
        logger.info(
            f"🚀 [AegisEM] ENTERED {signal_type} {quantity}x {symbol} @ {leg.entry_price:.2f} | "
            f"Conviction: {conviction:.2f} | Target: +{rec_tp*100:.1f}% | Direction: {direction}"
        )

    def _place_initial_take_profit(self, leg: TradeLeg, params: dict, target_pct: Optional[float] = None):
        """Resting DAY limit target (Dynamic 5.5% to 10.0%)."""
        eff_tp_pct = target_pct if target_pct and target_pct > 0 else TARGET_PROFIT_PCT
        tp_price = _round_tick(leg.entry_price * (1.0 + eff_tp_pct))
        leg.tp_price = tp_price
        leg.initial_tp_pct = eff_tp_pct
        leg.highest_price = leg.entry_price
        leg.breakeven_locked = False
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
                        logger.info(f"[AegisEM] DAY TP placed {self.tp_order_id} @ {tp_price:.2f}")
        except Exception as e:
            logger.error(f"[AegisEM] TP place failed: {e}")

    def _cancel_tp(self):
        if not self.tp_order_id or str(self.tp_order_id).startswith("PAPER_"):
            self.tp_order_id = None
            return
        try:
            api = getattr(self.broker, "_api", None)
            if api and hasattr(api, "cancel_order"):
                api.cancel_order(orderno=self.tp_order_id)
                logger.info(f"[AegisEM] Cancelled TP order {self.tp_order_id}")
        except Exception as e:
            logger.warning(f"[AegisEM] TP cancel warning: {e}")
        self.tp_order_id = None

    # ─── Open Trade Management ───
    def _manage_open_trade(self):
        """Check exit conditions: AEGIS signals, force flatten, TP/SL, Trailing SL."""
        now = datetime.now()
        
        with self.trade_lock:
            if not self.has_open_trade():
                return
            context = self.trade_context
            legs = list(context.legs)

        params = self.get_instrument_params()
        exch_seg = params["exchange"]
        
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

            # Update highest price reached during the trade
            if leg.current_price > getattr(leg, "highest_price", leg.entry_price):
                leg.highest_price = leg.current_price

        # 1. Force flatten at session end
        if self.must_force_flatten(now):
            self._close_all_legs(f"Force Flatten (>= {AEGIS_FORCE_FLATTEN.strftime('%H:%M')})")
            return

        # 2. Dynamic Take-Profit Hit (Passive Maker Limit Fill — Zero Chaser Discount)
        for leg in legs:
            if leg.status != "OPEN":
                continue
            tp_price = getattr(leg, "tp_price", _round_tick(leg.entry_price * (1.0 + TARGET_PROFIT_PCT)))
            effective_exit_bid = live_bid if live_bid > 0 else leg.current_price
            if effective_exit_bid >= tp_price > 0:
                tp_pct = (tp_price - leg.entry_price) / leg.entry_price * 100.0
                self._close_all_legs(
                    f"Take-Profit Hit (+{tp_pct:.1f}% @ {tp_price:.2f})",
                    is_tp_maker=True,
                    limit_fill_price=tp_price,
                )
                return

        # 3. Runner Trailing Lock (Arm at peak >= +7.0%, lock +5.0% minimum floor, trail 2.0% below peak)
        for leg in legs:
            if leg.status != "OPEN" or leg.entry_price <= 0:
                continue
            highest = getattr(leg, "highest_price", leg.entry_price)
            peak_pnl_pct = (highest - leg.entry_price) / leg.entry_price
            curr_pnl_pct = (leg.current_price - leg.entry_price) / leg.entry_price

            if peak_pnl_pct >= RUNNER_TRIGGER_PCT:
                leg.runner_locked = True
                runner_floor_price = _round_tick(leg.entry_price * (1.0 + RUNNER_FLOOR_PCT))
                runner_trail_price = _round_tick(highest * (1.0 - RUNNER_TRAIL_PCT))
                effective_runner_sl = max(runner_floor_price, runner_trail_price)
                effective_bid = live_bid if live_bid > 0 else leg.current_price

                if effective_bid > 0 and effective_bid <= effective_runner_sl:
                    self._cancel_tp()
                    self._close_all_legs(f"Runner Lock Exit (+{curr_pnl_pct*100:.1f}% @ {effective_bid:.2f})")
                    return

        # 4. Breakeven Guard (Arm at peak >= +3.0%, ratchet SL to Entry + 0.5% zero-friction floor)
        for leg in legs:
            if leg.status != "OPEN" or leg.entry_price <= 0:
                continue
            highest = getattr(leg, "highest_price", leg.entry_price)
            peak_pnl_pct = (highest - leg.entry_price) / leg.entry_price
            effective_bid = live_bid if live_bid > 0 else leg.current_price

            if peak_pnl_pct >= BREAKEVEN_TRIGGER_PCT:
                leg.breakeven_locked = True

            if getattr(leg, "breakeven_locked", False):
                be_sl_price = _round_tick(leg.entry_price * (1.0 + BREAKEVEN_LOCK_PCT))
                if effective_bid > 0 and effective_bid <= be_sl_price:
                    self._cancel_tp()
                    curr_pnl_pct = (effective_bid - leg.entry_price) / leg.entry_price
                    self._close_all_legs(f"Breakeven Guard Cut (+{curr_pnl_pct*100:.1f}% @ {effective_bid:.2f})")
                    return

        # 5. Hard SL (-3.5%)
        for leg in legs:
            if leg.status != "OPEN" or leg.entry_price <= 0:
                continue
            sl_price = _round_tick(leg.entry_price * (1.0 - STOP_LOSS_PCT))
            effective_bid = live_bid if live_bid > 0 else leg.current_price
            if effective_bid > 0 and effective_bid <= sl_price:
                self._cancel_tp()
                self._close_all_legs(f"Hard SL (-{STOP_LOSS_PCT*100:.1f}% @ {effective_bid:.2f})")
                return

        # 6. Spot Adverse Movement SL (+/- 25 pts for Nifty, +/- 50 pts for Sensex)
        curr_spot = self._get_current_spot()
        # params has key 'index_name' (NIFTY/SENSEX), not 'instrument' — fixed 2026-08-29 audit P1.3
        spot_sl_pts = 25.0 if "NIFTY" in params.get("index_name", "NIFTY").upper() else 50.0
        if curr_spot > 0 and context.index_entry > 0:
            if context.signal_type == "CALL" and curr_spot <= (context.index_entry - spot_sl_pts):
                self._cancel_tp()
                self._close_all_legs(f"Spot Adverse Breakdown SL ({curr_spot:.1f} vs entry {context.index_entry:.1f}, -{spot_sl_pts:.0f}pts)")
                return
            elif context.signal_type == "PUT" and curr_spot >= (context.index_entry + spot_sl_pts):
                self._cancel_tp()
                self._close_all_legs(f"Spot Adverse Rally SL ({curr_spot:.1f} vs entry {context.index_entry:.1f}, +{spot_sl_pts:.0f}pts)")
                return

        # 7. Hard time stop (5 min)
        if context.entry_time:
            hold_sec = (now - context.entry_time).total_seconds()
            if hold_sec >= MAX_HOLD_SEC:
                self._close_all_legs(f"5-Min Hard Time Stop ({int(hold_sec)}s)")
                return

    def _close_all_legs(self, reason: str, is_tp_maker: bool = False, limit_fill_price: float = 0.0):
        with self.trade_lock:
            if not self.has_open_trade():
                return
            self.trade_context.status = "EXIT_PENDING"
        
        threading.Thread(
            target=self._chase_exit_thread,
            args=(reason, is_tp_maker, limit_fill_price),
            daemon=True,
        ).start()

    def _chase_exit_thread(self, reason: str, is_tp_maker: bool = False, limit_fill_price: float = 0.0):
        """Limit exit execution — preserves TP maker fills and chases emergency exits."""
        params = self.get_instrument_params()
        exch_seg = params["exchange"]
        
        with self.trade_lock:
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
            if is_tp_maker and limit_fill_price > 0:
                if str(getattr(self, "tp_order_id", "")).startswith("PAPER_"):
                    filled = True
                    fill_price = limit_fill_price
                elif self.tp_order_id and api:
                    try:
                        time.sleep(0.5)
                        if hasattr(api, "single_order_history"):
                            hist = api.single_order_history(orderno=self.tp_order_id)
                            if isinstance(hist, list) and hist:
                                latest = hist[-1]
                                if latest.get("status") in ["FILLED", "COMPLETE"]:
                                    filled = True
                                    fill_price = float(latest.get("avgprc") or limit_fill_price)
                    except Exception as e_tp:
                        logger.warning(f"[AegisEM] Maker TP status check warning: {e_tp}")
                
                if not filled and str(getattr(self, "tp_order_id", "")).startswith("PAPER_"):
                    filled = True
                    fill_price = limit_fill_price

            # Case B: Emergency or Trail Exit Chase (LTP-1% -> LTP-2% -> LTP-3%)
            if not filled:
                # Step 1: LTP - 1%
                price_1 = _round_tick(leg.current_price * 0.99)
                ordno = None
                
                if self.tp_order_id and not str(self.tp_order_id).startswith("PAPER_"):
                    ordno = self.tp_order_id
                    if api and hasattr(api, "modify_order"):
                        api.modify_order(exchange=exch_seg, tradingsymbol=leg.symbol, orderno=ordno,
                                         newquantity=qty, newprice_type="LMT", newprice=f"{price_1:.2f}")
                else:
                    if api and hasattr(self.order_lifecycle, "_build_kwargs"):
                        kwargs = self.order_lifecycle._build_kwargs("S", exch_seg, leg.symbol, qty, f"{price_1:.2f}")
                        place = getattr(api, "place_order", None)
                        if place:
                            resp = place(**kwargs)
                            if isinstance(resp, dict) and resp.get("stat") == "Ok":
                                ordno = resp.get("norenordno")
                
                if ordno:
                    time.sleep(0.8)
                    history = api.single_order_history(orderno=ordno) if hasattr(api, "single_order_history") else []
                    latest = history[0] if history and isinstance(history, list) else {}
                    status = latest.get("status", "OPEN")
                    
                    if status in ["FILLED", "COMPLETE"]:
                        filled = True
                        fill_price = float(latest.get("avgprc") or price_1)
                    else:
                        # Step 2: LTP - 2%
                        price_2 = _round_tick(leg.current_price * 0.98)
                        if api and hasattr(api, "modify_order"):
                            api.modify_order(exchange=exch_seg, tradingsymbol=leg.symbol, orderno=ordno,
                                             newquantity=qty, newprice_type="LMT", newprice=f"{price_2:.2f}")
                        time.sleep(1.0)
                        history = api.single_order_history(orderno=ordno) if hasattr(api, "single_order_history") else []
                        latest = history[0] if history and isinstance(history, list) else {}
                        status = latest.get("status", "OPEN")
                        
                        if status in ["FILLED", "COMPLETE"]:
                            filled = True
                            fill_price = float(latest.get("avgprc") or price_2)
                        else:
                            # Step 3: Cancel & Replace at LTP - 3%
                            if api and hasattr(api, "cancel_order"):
                                api.cancel_order(orderno=ordno)
                            price_3 = _round_tick(leg.current_price * 0.97)
                            if api and hasattr(self.order_lifecycle, "_build_kwargs"):
                                kwargs = self.order_lifecycle._build_kwargs("S", exch_seg, leg.symbol, qty, f"{price_3:.2f}")
                                place = getattr(api, "place_order", None)
                                if place:
                                    logger.warning(f"[AegisEM Chaser] Final aggressive exit @ {price_3:.2f}")
                                    resp = place(**kwargs)
                                    ordno_3 = resp.get("norenordno") if isinstance(resp, dict) and resp.get("stat") == "Ok" else None
                                    if ordno_3:
                                        time.sleep(0.8)
                                        history = api.single_order_history(orderno=ordno_3) if hasattr(api, "single_order_history") else []
                                        latest = history[0] if history and isinstance(history, list) else {}
                                        status = latest.get("status", "OPEN")
                                        if status in ["FILLED", "COMPLETE"]:
                                            fill_price = float(latest.get("avgprc") or price_3)
                                            filled = True
                                            fill_ts = time.time()
                                        else:
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
            self._save_active_context()

    def _append_history(self, reason: str, total_pnl: float):
        hist = []
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, "r") as f:
                    hist = json.load(f)
            except Exception:
                hist = []
        
        leg0 = self.trade_context.legs[0] if self.trade_context and self.trade_context.legs else None
        entry_dt_str = self.trade_context.entry_time.strftime("%d %b %Y %H:%M:%S") if self.trade_context and self.trade_context.entry_time else datetime.now().strftime("%d %b %Y %H:%M:%S")
        exit_dt_str = datetime.now().strftime("%d %b %Y %H:%M:%S")
        
        self.daily_pnl += total_pnl
        self.last_trade_exit_time = time.time()
        self.last_trade_loss = (total_pnl < 0)
        if self.last_trade_loss:
            logger.info(f"[AegisEM] Trade closed in loss (₹{total_pnl:.2f}) -> Post-loss cooloff active for {int(self.post_loss_cooloff_sec)}s")
        else:
            logger.info(f"[AegisEM] Trade closed in profit (₹{total_pnl:.2f}) -> Inter-trade cooldown active for {int(self.inter_trade_cooldown_sec)}s")
        
        hist.append({
            "date": entry_dt_str,
            "entry_time": entry_dt_str,
            "exit_time": exit_dt_str,
            "strategy": "AEGIS_GAMMA_BURST",
            "signal": self.trade_context.signal_type if self.trade_context else "",
            "signal_type": self.trade_context.signal_type if self.trade_context else "",
            "symbol": leg0.symbol if leg0 else "",
            "entry_price": leg0.entry_price if leg0 else 0,
            "exit_price": leg0.exit_price if leg0 else 0,
            "pnl": total_pnl,
            "net_pl": total_pnl,
            "daily_total_pnl": self.daily_pnl,
            "exit_reason": reason,
            "reason": reason,
            "decision_payload": self.trade_context.decision_payload if self.trade_context else None
        })
        try:
            with open(self.history_file, "w") as f:
                json.dump(hist, f, indent=2)
        except Exception as e:
            logger.error(f"[AegisEM] History write failed: {e}")

    def _save_active_context(self):
        try:
            if not self.has_open_trade():
                if os.path.exists(self.active_context_file):
                    os.remove(self.active_context_file)
                return
            payload = {
                "status": "OPEN",
                "signal_type": self.trade_context.signal_type,
                "strategy": "AEGIS_GAMMA_BURST",
                "entry_time": self.trade_context.entry_time.isoformat() if self.trade_context.entry_time else None,
                "index_entry": self.trade_context.index_entry,
                "tp_order_id": self.tp_order_id,
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
                self.trade_context.entry_time = datetime.fromisoformat(data["entry_time"])
            self.tp_order_id = data.get("tp_order_id")
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
                self.trade_context.add_leg(leg)
            self.trade_context.status = "OPEN"
            logger.warning("[AegisEM] Restored OPEN trade from disk")
        except Exception as e:
            logger.error(f"[AegisEM] Load context failed: {e}")
            self.trade_context = None
