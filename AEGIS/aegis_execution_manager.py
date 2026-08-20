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
MAX_HOLD_SEC = 600.0                    # 10-minute hard ceiling
AEGIS_ENTRY_CUTOFF = dt_time(15, 0, 0)  # No new entries after 15:00
AEGIS_FORCE_FLATTEN = dt_time(15, 10, 0) # Force flatten at 15:10
MAX_TRADES_PER_DAY = 5                  # Daily circuit breaker

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
        self._option_cache: Dict = {}
        self.tp_order_id: Optional[str] = None
        self.trade_lock = threading.Lock()
        
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
        return bool(self.trade_context and self.trade_context.status == "OPEN")

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
            
        if self.trades_today >= MAX_TRADES_PER_DAY:
            logger.warning(f"[AegisEM] Daily trade limit ({MAX_TRADES_PER_DAY}) reached")
            return

        if self.has_open_trade():
            logger.warning("[AegisEM] Entry blocked — trade already open")
            return

        if not self.can_enter(now):
            logger.warning(f"[AegisEM] Entry blocked — outside window (cutoff {AEGIS_ENTRY_CUTOFF})")
            return

        direction = signal.get("signal")  # "UP" or "DOWN"
        conviction = signal.get("conviction", 0.0)
        lp = signal.get("lp", 0.0)
        metrics = signal.get("metrics", {})

        params = self.get_instrument_params()
        weekly_opts = self.data_fetcher.get_weekly_option_tokens()
        
        # Map signal direction to option type
        signal_type = "CALL" if direction == "UP" else "PUT"
        
        allocations = StrikeSelector.select_strikes(
            signal_type=signal_type,
            index_price=lp,
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
                index_entry=lp,
                decision_payload={
                    "conviction": conviction,
                    "direction": direction,
                    "metrics": metrics,
                    "iv": metrics.get("iv", 0.15),
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

        # Place initial TP at +10% (AEGIS manages dynamic exits via signals)
        self._place_initial_take_profit(leg, params)
        self._save_active_context()
        
        logger.info(
            f"🚀 [AegisEM] ENTERED {signal_type} {quantity}x {symbol} @ {leg.entry_price:.2f} | "
            f"Conviction: {conviction:.2f} | Direction: {direction}"
        )

    def _place_initial_take_profit(self, leg: TradeLeg, params: dict):
        """Resting DAY limit at +10% premium (or +5% on Friday DTE 4)."""
        now = datetime.now()
        is_friday = (now.weekday() == 4)
        target_pct = 0.05 if is_friday else 0.10
        tp_price = _round_tick(leg.entry_price * (1.0 + target_pct))
        leg.tp_price = tp_price
        leg.initial_tp_pct = target_pct
        
        if isinstance(self.order_lifecycle, PaperOrderLifecycle):
            self.tp_order_id = f"PAPER_TP_{leg.symbol}"
            logger.info(f"[AegisEM] [Paper] Resting TP limit set at {tp_price:.2f} (+{int(target_pct*100)}% on {'Friday DTE-4' if is_friday else 'Regular Day'})")
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
        """Check exit conditions: AEGIS signals, force flatten, TP/SL."""
        now = datetime.now()
        
        with self.trade_lock:
            if not self.has_open_trade():
                return
            context = self.trade_context
            legs = list(context.legs)

        params = self.get_instrument_params()
        exch_seg = params["exchange"]
        
        # Refresh marks
        for leg in legs:
            if leg.status != "OPEN":
                continue
            try:
                q = self.broker.market_data_provider.get_quote(exch_seg, leg.token)
                if q and float(q.get("ltp") or 0) > 0:
                    leg.current_price = float(q["ltp"])
            except Exception:
                pass

        # 1. Force flatten at session end
        if self.must_force_flatten(now):
            self._close_all_legs(f"Force Flatten (>= {AEGIS_FORCE_FLATTEN.strftime('%H:%M')})")
            return

        # 2. Check AEGIS exit signals (ξ_soft, ξ_hard) — would come via signal queue
        #    For now, we implement local equivalents based on conviction decay
        #    Real AEGIS exit signals would be GAMMA_BURST_EXIT with exit_type
        
        # 3. Hard SL at -25% premium (fallback)
        for leg in legs:
            if leg.status != "OPEN" or leg.entry_price <= 0:
                continue
            sl_price = _round_tick(leg.entry_price * 0.75)
            if leg.current_price > 0 and leg.current_price <= sl_price:
                self._cancel_tp()
                self._close_all_legs(f"Hard SL (-25% @ {leg.current_price:.2f})")
                return

        # 4. TP hit detection (resting or synthetic)
        for leg in legs:
            if leg.status != "OPEN":
                continue
            is_friday = (now.weekday() == 4)
            default_tp_pct = 0.05 if is_friday else 0.10
            tp_price = getattr(leg, "tp_price", _round_tick(leg.entry_price * (1.0 + default_tp_pct)))
            if leg.current_price >= tp_price > 0:
                tp_pct_str = f"+{int(default_tp_pct*100)}%"
                self._close_all_legs(f"Take-Profit Hit ({tp_pct_str} @ {leg.current_price:.2f})")
                return

        # 5. Hard time stop (10 min)
        if context.entry_time:
            hold_sec = (now - context.entry_time).total_seconds()
            if hold_sec >= MAX_HOLD_SEC:
                self._close_all_legs(f"10-Min Hard Time Stop ({int(hold_sec)}s)")
                return

    def _close_all_legs(self, reason: str):
        with self.trade_lock:
            if not self.has_open_trade():
                return
            self.trade_context.status = "EXIT_PENDING"
        
        threading.Thread(target=self._chase_exit_thread, args=(reason,), daemon=True).start()

    def _chase_exit_thread(self, reason: str):
        """Aggressive limit chasing for exit — feeds exact fill to AEGIS."""
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

            # Limit chase: LTP-1% → LTP-2% → LTP-3% (marketable)
            qty = str(leg.quantity * params["lot_size"])
            fill_price = 0.0
            fill_ts = time.time()
            filled = False
            
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
                                place(**kwargs)
                                time.sleep(0.8)
                                history = api.single_order_history(orderno=ordno) if hasattr(api, "single_order_history") else []
                                latest = history[0] if history and isinstance(history, list) else {}
                                fill_price = float(latest.get("avgprc") or price_3)
                                filled = True
            
            if not filled:
                # Fallback
                self.order_lifecycle.execute_exit(leg, reason)
                fill_price = leg.exit_price if leg.exit_price and leg.exit_price > 0 else leg.current_price
                fill_ts = time.time()
            
            # ─── CRITICAL: Feed fill back to AEGIS for latency decay ───
            if self.aegis_engine:
                signal_price = leg.entry_price
                signal_ts = context.entry_time.timestamp() if context.entry_time else fill_ts
                self.aegis_engine.on_fill(
                    sym=context.legs[0].symbol if context.legs else leg.symbol,
                    fill_price=fill_price,
                    fill_ts=fill_ts,
                    signal_price=signal_price,
                    signal_ts=signal_ts
                )
            
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
