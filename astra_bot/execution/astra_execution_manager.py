"""
Astra V2 Option Order Flow execution manager.

MUST NOT use KDO flow-decay / OI-reversal exits.
Exit policy:
  0) Force flatten at session flatten time (≥ 15:10)
  1) +10% premium DAY limit TP (resting)
  2) −25% premium hard SL (fallback when Chandelier is unreliable)
  3) 5m underlying Chandelier break → marketable LMT exit
  4) 10-minute hard time stop
"""

from __future__ import annotations

import json
import os
import queue
import threading
from datetime import datetime, timedelta, time as dt_time
from typing import Optional

import numpy as np
import pandas as pd

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

logger = get_logger("astra_execution_manager")

# Locked Astra risk parameters
MAX_HOLD_SEC = 600.0
ASTRA_ENTRY_CUTOFF = dt_time(15, 0, 0)
ASTRA_FORCE_FLATTEN = dt_time(15, 10, 0)

def _round_tick(price: float, tick: float = OPTION_TICK_SIZE) -> float:
    if price <= 0:
        return 0.0
    return round(round(price / tick) * tick, 2)


class AstraExecutionManager:
    """Entry/exit lifecycle dedicated to Kronos ML signals."""

    def __init__(self, broker_gateway, data_fetcher):
        self.broker = broker_gateway
        self.data_fetcher = data_fetcher
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
            logger.info("[AstraEM] LIVE mode — Shoonya LMT / NRML path")
        else:
            self.order_lifecycle = PaperOrderLifecycle()
            logger.info("[AstraEM] PAPER mode")

        self.trade_context: Optional[TradeContext] = None
        self.trades_today = 0
        self.last_trade_date = None
        self.max_trades_per_day = 100
        self.strike_cooldowns = {}
        self._option_cache = {}
        self.tp_order_id = None
        self.exit_signal_queue = queue.Queue()
        self.trade_lock = threading.Lock()
        
        self.daily_pnl = 0.0
        self.daily_loss_cap = -5000.0
        
        trades_dir = os.path.join(self.data_dir, "trades")
        os.makedirs(trades_dir, exist_ok=True)
        self.history_file = os.path.join(trades_dir, "trade_history.json")
        self.active_context_file = os.path.join(trades_dir, "astra_active_trade_context.json")
        self._load_active_context()
        self._load_daily_pnl()

    def _load_daily_pnl(self):
        self.daily_pnl = 0.0
        if not os.path.exists(self.history_file):
            return
        try:
            with open(self.history_file, "r", encoding="utf-8") as f:
                hist = json.load(f)
            
            today_str = datetime.now().strftime("%d %b %Y")
            for trade in hist:
                trade_date = trade.get("entry_time", "")
                if trade_date.startswith(today_str):
                    self.daily_pnl += trade.get("pnl", 0.0)
            logger.info(f"[AstraEM] Restored daily PnL from history: ₹{self.daily_pnl:.2f}")
        except Exception as e:
            logger.error(f"[AstraEM] failed to load daily PnL: {e}")

    # --- calendar gates ---
    @staticmethod
    def can_enter(now: datetime = None) -> bool:
        now = now or datetime.now()
        if not is_trading_day(now.date()):
            return False
        return dt_time(9, 16, 0) <= now.time() <= ASTRA_ENTRY_CUTOFF

    @staticmethod
    def must_force_flatten(now: datetime = None) -> bool:
        now = now or datetime.now()
        return now.time() >= ASTRA_FORCE_FLATTEN

    def has_open_trade(self) -> bool:
        return bool(self.trade_context and self.trade_context.status == "OPEN")

    @property
    def tp_pct(self):
        return 0.05 if datetime.now().weekday() == 4 else 0.10

    @property
    def hard_sl_pct(self):
        return 0.10 if datetime.now().weekday() == 4 else 0.15

    def _effective_sl_price(self, leg) -> float:
        base_sl = _round_tick(leg.entry_price * (1 - self.hard_sl_pct))
        lock_level = leg.entry_price * (1 + 0.5 * self.tp_pct)   # half of TP
        if leg.current_price >= lock_level > 0:
            return max(base_sl, _round_tick(leg.entry_price * 1.01))   # floor at +1%
        return base_sl

    def get_instrument_params(self) -> dict:
        name, _ = self.data_fetcher.get_active_instrument()
        if name == "SENSEX":
            return {
                "premium_min": SENSEX_PREMIUM_MIN,
                "premium_max": SENSEX_PREMIUM_MAX,
                "lot_size": SENSEX_LOT_SIZE,
                "index_name": "SENSEX",
            }
        return {
            "premium_min": NIFTY_PREMIUM_MIN,
            "premium_max": NIFTY_PREMIUM_MAX,
            "lot_size": NIFTY_LOT_SIZE,
            "index_name": "NIFTY",
        }

    def execute_entry(self, signal_type: str, index_price: float, decision_payload: dict = None, target_token: str = None):
        now = datetime.now()
        
        # Circuit Breaker Reset
        if self.last_trade_date != now.date():
            self.trades_today = 0
            self.daily_pnl = 0.0
            self.last_trade_date = now.date()
            
        if self.trades_today >= self.max_trades_per_day:
            logger.warning(f"[AstraEM] Daily trade limit ({self.max_trades_per_day}) reached. Entry blocked.")
            return
            
        if self.daily_pnl <= self.daily_loss_cap:
            logger.warning(f"[AstraEM] Daily loss cap hit (₹{self.daily_pnl:.0f}). Entries blocked.")
            return

        if self.has_open_trade():
            logger.warning("[AstraEM] Entry blocked — trade already open")
            return
        if not self.can_enter(now):
            logger.warning(f"[AstraEM] Entry blocked — outside Astra window (cutoff {ASTRA_ENTRY_CUTOFF})")
            return

        params = self.get_instrument_params()
        weekly_opts = self.data_fetcher.get_weekly_option_tokens()
        allocations = StrikeSelector.select_strikes(
            signal_type=signal_type,
            index_price=index_price,
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
            logger.warning(
                f"[AstraEM] Entry aborted — no affordable strike "
                f"({params['index_name']} funds=₹{self.capital_engine.get_available_funds():.2f} "
                f"lot={params['lot_size']} max_ltp≈₹{self.capital_engine.get_available_funds()/max(params['lot_size'],1):.2f})"
            )
            try:
                from common.ipc import update_kronos_live_state
                update_kronos_live_state({
                    "signal": signal_type,
                    "signal_status": signal_type,
                    "reasoning": (
                        f"{signal_type} signal but NO AFFORDABLE STRIKE "
                        f"(funds ₹{self.capital_engine.get_available_funds():.0f}, "
                        f"lot {params['lot_size']} → need premium ≤ "
                        f"₹{self.capital_engine.get_available_funds()/max(params['lot_size'],1):.0f})"
                    ),
                })
            except Exception:
                pass
            return

        opt_row, quantity, ltp = allocations[0]
        
        # Target Token override if available and affordable
        if target_token:
            for alloc in allocations:
                if str(alloc[0]["token"]) == str(target_token):
                    opt_row, quantity, ltp = alloc
                    break
                    
        symbol = opt_row["symbol"]
        token_str = str(opt_row["token"])
        
        if symbol in self.strike_cooldowns and now < self.strike_cooldowns[symbol]:
            logger.warning(f"[AstraEM] Strike {symbol} in cooldown")
            return
            
        live_ask = 0.0
        # Spread Check (P1)
        if hasattr(self.broker, "market_data_provider"):
            try:
                exch_seg = "BFO" if "SENSEX" in params["index_name"] else "NFO"
                q = self.broker.market_data_provider.get_quote(exch_seg, token_str)
                if q:
                    ask = float(q.get("sp1") or 0)
                    bid = float(q.get("bp1") or 0)
                    live_ask = ask
                    if ask > 0 and bid > 0:
                        mid = (ask + bid) / 2
                        spread = (ask - bid) / mid
                        if spread > 0.02: # 2% spread limit
                            logger.warning(f"[AstraEM] Entry aborted - Spread too high: {spread*100:.1f}% for {symbol}")
                            return
                        
                        # ✅ NEW: Liquidity Gate (sq1)
                        sq1 = int(float(q.get("sq1") or 0))
                        if sq1 < 3: # Less than 3 lots available at the Ask
                            logger.warning(f"[AstraEM] Entry aborted - Ask Size (sq1={sq1}) too thin for {symbol}")
                            return
            except Exception as e:
                logger.error(f"[AstraEM] Spread check failed or timed out: {e}")
                return

        with self.trade_lock:
            self.trade_context = TradeContext(
                signal_type=signal_type,
                strategy="ASTRA_V2",
                index_entry=index_price,
                decision_payload=decision_payload or {},
            )
            leg = TradeLeg(token_str, symbol, float(opt_row["strike"]), signal_type, quantity, ltp)
    
            if not self.order_lifecycle.execute_entry(leg, live_ask=live_ask or ltp):
                logger.error(f"[AstraEM] Entry order rejected for {symbol}")
                self.trade_context = None
                return
    
            self.trade_context.add_leg(leg)
            
        self.capital_engine.deduct_funds(leg.entry_price * quantity * params["lot_size"])
        self.trades_today += 1
        self.tp_order_id = None

        self._place_take_profit(leg)
        self._save_active_context()
        logger.info(
            f"[AstraEM] ENTERED {signal_type} {quantity}x {symbol} @ {leg.entry_price:.2f} | "
            f"TP={leg.tp_price:.2f} | SL={_round_tick(leg.entry_price * (1 - self.hard_sl_pct)):.2f} | max_hold={int(MAX_HOLD_SEC)}s"
        )

    def _place_take_profit(self, leg: TradeLeg):
        """Resting DAY limit sell at +10% premium. Paper mode marks synthetic TP only."""
        tp_price = _round_tick(leg.entry_price * (1.0 + self.tp_pct))
        leg.tp_price = tp_price  # type: ignore[attr-defined]
        if isinstance(self.order_lifecycle, PaperOrderLifecycle):
            self.tp_order_id = f"PAPER_TP_{leg.symbol}"
            logger.info(f"[AstraEM] PAPER TP armed at {tp_price:.2f}")
            return

        # Live: place DAY LMT sell via lifecycle kwargs builder if available
        try:
            exch = getattr(leg, "exchange", "BFO" if "SENSEX" in leg.symbol else "NFO")
            params = self.get_instrument_params()
            qty = str(leg.quantity * params["lot_size"])
            if hasattr(self.order_lifecycle, "_build_kwargs"):
                kwargs = self.order_lifecycle._build_kwargs("S", exch, leg.symbol, qty, f"{tp_price:.2f}")
                api = self.broker if hasattr(self.broker, "place_order") else getattr(self.broker, "_api", None)
                place = getattr(self.broker, "place_order", None) or getattr(api, "place_order", None)
                if place:
                    resp = place(**kwargs)
                    if isinstance(resp, dict) and resp.get("stat") == "Ok" and resp.get("norenordno"):
                        self.tp_order_id = resp["norenordno"]
                        logger.info(f"[AstraEM] DAY TP LMT placed {self.tp_order_id} @ {tp_price:.2f}")
                        return
                    logger.error(f"[AstraEM] TP place failed: {resp}")
            logger.warning("[AstraEM] TP resting order not placed — will synthetic-monitor premium ≥ +10%")
        except Exception as e:
            logger.error(f"[AstraEM] TP place exception: {e}")

    def _cancel_tp(self):
        if not self.tp_order_id or str(self.tp_order_id).startswith("PAPER_"):
            self.tp_order_id = None
            return
        try:
            api = getattr(self.broker, "_api", None)
            if api and hasattr(api, "cancel_order"):
                api.cancel_order(orderno=self.tp_order_id)
                logger.info(f"[AstraEM] Cancelled TP order {self.tp_order_id}")
        except Exception as e:
            logger.warning(f"[AstraEM] TP cancel warning: {e}")
        self.tp_order_id = None

    def process_tick_for_exit(self, token: str, current_ltp: float):
        """WS Callback to monitor exit conditions tick-by-tick (Non-blocking)."""
        with self.trade_lock:
            if not self.has_open_trade() or not self.trade_context:
                return
                
            legs_snapshot = list(self.trade_context.legs)
            
        for leg in legs_snapshot:
            if leg.status != "OPEN" or leg.token != token:
                continue
                
            tp_price = getattr(leg, "tp_price", _round_tick(leg.entry_price * (1 + self.tp_pct)))
            sl_price = self._effective_sl_price(leg)
            
            if current_ltp >= tp_price > 0:
                self.exit_signal_queue.put(f"Take-Profit Hit (+{self.tp_pct*100:.0f}% premium @ {current_ltp:.2f})")
            elif current_ltp <= sl_price and sl_price > 0:
                if sl_price > leg.entry_price:
                    self.exit_signal_queue.put(f"Profit-Lock SL (+1% floor @ {current_ltp:.2f})")
                else:
                    self.exit_signal_queue.put(f"Premium Hard SL (−{self.hard_sl_pct*100:.0f}% @ {current_ltp:.2f})")

    def process_exit_queue(self):
        """Polls the exit queue to perform the REST API exit synchronously in the main thread."""
        try:
            # Non-blocking get
            reason = self.exit_signal_queue.get_nowait()
            if self.has_open_trade():
                logger.warning(f"[AstraEM] Executing WS Exit Signal: {reason}")
                self._close_all_legs(reason)
        except queue.Empty:
            pass

    def manage_open_trade(self, underlying_df: pd.DataFrame = None, spot_price: float = 0.0):
        if not self.has_open_trade():
            return

        now = datetime.now()
        hold_sec = (now - self.trade_context.entry_time).total_seconds() if self.trade_context.entry_time else 0.0
        name, exch_seg = self.data_fetcher.get_active_instrument()
        params = self.get_instrument_params()

        # Refresh option mark
        for leg in self.trade_context.legs:
            if leg.status != "OPEN":
                continue
            try:
                q = self.broker.market_data_provider.get_quote(exch_seg, leg.token)
                if q and float(q.get("ltp") or 0) > 0:
                    leg.current_price = float(q["ltp"])
            except Exception:
                pass

            # Synthetic / live TP fill detection
            tp_price = getattr(leg, "tp_price", _round_tick(leg.entry_price * (1 + self.tp_pct)))
            if leg.current_price >= tp_price > 0:
                self._close_all_legs(f"Take-Profit Hit (+{self.tp_pct*100:.0f}% premium @ {leg.current_price:.2f})")
                return

        # 0) Force flatten
        if self.must_force_flatten(now):
            self._close_all_legs(f"Force Flatten (session >= {ASTRA_FORCE_FLATTEN.strftime('%H:%M')})")
            return

        # 1b) Premium hard SL / Profit-Lock SL
        for leg in self.trade_context.legs:
            if leg.status != "OPEN":
                continue
            if leg.entry_price <= 0 or leg.current_price <= 0:
                continue
            
            sl_price = self._effective_sl_price(leg)
            if leg.current_price <= sl_price:
                if sl_price > leg.entry_price:
                    self._cancel_tp()
                    self._close_all_legs(f"Profit-Lock SL (+1% floor @ {leg.current_price:.2f})")
                else:
                    dd = (leg.current_price - leg.entry_price) / leg.entry_price
                    self._close_all_legs(
                        f"Premium Hard SL (−{self.hard_sl_pct*100:.0f}% @ {leg.current_price:.2f}; "
                        f"entry {leg.entry_price:.2f}, dd={dd*100:.1f}%)"
                    )
                return

        # 2) No-Effect Early Stop (5 mins) & Hard Time Stop (10 mins)
        if hold_sec >= 300:
            leg0 = next((l for l in self.trade_context.legs if l.status == "OPEN"), None)
            if leg0 and leg0.entry_price > 0:
                chg = (leg0.current_price - leg0.entry_price) / leg0.entry_price
                if chg < 0.01:
                    self._cancel_tp()
                    self._close_all_legs(f"No-Effect Stop ({int(hold_sec)}s, {chg*100:+.1f}%)")
                    return

        if hold_sec >= MAX_HOLD_SEC:
            self._close_all_legs(f"10-Minute Hard Time Stop ({int(hold_sec)}s)")
            return

    def _close_all_legs(self, reason: str):
        with self.trade_lock:
            if not self.has_open_trade():
                return
            
            # Immediately mark as EXIT_PENDING to prevent double closes
            self.trade_context.status = "EXIT_PENDING"
            
        import threading
        threading.Thread(target=self._chase_exit_thread, args=(reason,), daemon=True).start()

    def _chase_exit_thread(self, reason: str):
        params = self.get_instrument_params()
        
        with self.trade_lock:
            legs_snapshot = list(self.trade_context.legs)
            context = self.trade_context
        now = datetime.now()
        total_pnl = 0.0
        
        filled_qty = 0
        # Bug 22: Partial Fills Check
        if self.tp_order_id and not str(self.tp_order_id).startswith("PAPER_"):
            try:
                api = getattr(self.broker, "_api", None)
                if api and hasattr(api, "single_order_history"):
                    history = api.single_order_history(orderno=self.tp_order_id)
                    if history and isinstance(history, list):
                        latest = history[0]
                        status = latest.get("status")
                        filled_qty = int(float(latest.get("fillshares", 0)))
                        
                        for leg in self.trade_context.legs:
                            if leg.status == "OPEN":
                                total_leg_qty_shares = leg.quantity * params["lot_size"]
                                if status in ["FILLED", "COMPLETE"] or filled_qty >= total_leg_qty_shares:
                                    reason = f"TP Fully Filled (+{self.tp_pct*100:.0f}%)"
                                    leg.quantity = 0
                                elif filled_qty > 0:
                                    leg.quantity -= (filled_qty // params["lot_size"])
                                    logger.info(f"[AstraEM] TP partially filled. Adjusted leg qty to {leg.quantity}.")
            except Exception as e:
                logger.error(f"[AstraEM] TP order history check failed: {e}")

        for leg in self.trade_context.legs:
            if leg.status != "OPEN":
                continue
            
            if leg.quantity <= 0:
                # Fully filled by resting TP
                leg.status = "CLOSED"
                leg.exit_time = now
                leg.exit_price = getattr(leg, "tp_price", leg.current_price)
                leg.pnl = (leg.exit_price - leg.entry_price) * filled_qty # using raw filled shares
                total_pnl += leg.pnl
                self.capital_engine.add_funds(leg.exit_price * filled_qty)
                logger.info(f"[AstraEM] CLOSED {leg.symbol} via resting TP | PnL {leg.pnl:.2f}")
                continue
                
            # Anchor to live bid before chasing
            try:
                q = self.broker.market_data_provider.get_quote(exch_seg, leg.token)
                if q and float(q.get("bp1") or 0) > 0:
                    leg.current_price = float(q["bp1"])
            except Exception:
                pass

            # Limit Chasing Implementation
            import time
            api = getattr(self.broker, "_api", None)
            exch_seg = "BFO" if "SENSEX" in leg.symbol else "NFO"
            qty = str(leg.quantity * params["lot_size"])
            
            # Step 1: Initial Order (LTP - 1%)
            price_1 = _round_tick(leg.current_price * 0.99)
            
            ordno = None
            if self.tp_order_id and not str(self.tp_order_id).startswith("PAPER_"):
                ordno = self.tp_order_id
                logger.info(f"[AstraEM Chaser] Modifying resting TP order {ordno} to LMT Exit @ {price_1:.2f} (LTP-1%)")
                api.modify_order(exchange=exch_seg, tradingsymbol=leg.symbol, orderno=ordno, newquantity=qty, newprice_type="LMT", newprice=f"{price_1:.2f}") if hasattr(api, "modify_order") else {}
            else:
                kwargs = self.order_lifecycle._build_kwargs("S", exch_seg, leg.symbol, qty, f"{price_1:.2f}") if hasattr(self.order_lifecycle, "_build_kwargs") else None
                place = getattr(api, "place_order", None)
                if place and kwargs:
                    logger.info(f"[AstraEM Chaser] Placing Initial LMT Exit @ {price_1:.2f} (LTP-1%)")
                    resp = place(**kwargs)
                    if isinstance(resp, dict) and resp.get("stat") == "Ok":
                        ordno = resp.get("norenordno")
                    
            if ordno:
                time.sleep(1.0)
                history = api.single_order_history(orderno=ordno) if hasattr(api, "single_order_history") else []
                status = history[0].get("status") if history and isinstance(history, list) else "OPEN"
                
                # Step 2: Modify (LTP - 2%)
                if status not in ["FILLED", "COMPLETE", "REJECTED"]:
                    price_2 = _round_tick(leg.current_price * 0.98)
                    logger.info(f"[AstraEM Chaser] Order {ordno} OPEN. Modifying to {price_2:.2f} (LTP-2%)")
                    mod_resp = api.modify_order(exchange=exch_seg, tradingsymbol=leg.symbol, orderno=ordno, newquantity=qty, newprice_type="LMT", newprice=f"{price_2:.2f}") if hasattr(api, "modify_order") else {}
                    
                    time.sleep(1.5)
                    history = api.single_order_history(orderno=ordno) if hasattr(api, "single_order_history") else []
                    status = history[0].get("status") if history and isinstance(history, list) else "OPEN"
                    
                    # Step 3: Cancel & Replace (LTP - 3%)
                    if status not in ["FILLED", "COMPLETE", "REJECTED"]:
                        logger.warning(f"[AstraEM Chaser] Modify failed to fill. Cancelling {ordno} & Replacing (LTP-3%)")
                        if hasattr(api, "cancel_order"):
                            api.cancel_order(orderno=ordno)
                        
                        price_3 = _round_tick(leg.current_price * 0.97)
                        kwargs_3 = self.order_lifecycle._build_kwargs("S", exch_seg, leg.symbol, qty, f"{price_3:.2f}")
                        logger.info(f"[AstraEM Chaser] Placing final aggressive LMT Exit @ {price_3:.2f}")
                        place(**kwargs_3)
                        fill = price_3
                    else:
                        fill = price_2
                else:
                    fill = price_1
            else:
                # Fallback to local order lifecycle execution if API place failed
                self.order_lifecycle.execute_exit(leg, reason)
                fill = leg.exit_price if leg.exit_price and leg.exit_price > 0 else leg.current_price
            
            with self.trade_lock:
                leg.status = "CLOSED"
                leg.exit_time = datetime.now()
                leg.exit_price = fill
                leg.pnl = (fill - leg.entry_price) * leg.quantity * params["lot_size"]
                total_pnl += leg.pnl
                self.capital_engine.add_funds(fill * leg.quantity * params["lot_size"])
                self.strike_cooldowns[leg.symbol] = datetime.now() + timedelta(minutes=3)
                logger.info(
                    f"[AstraEM] CLOSED {leg.symbol}: {leg.entry_price:.2f} -> {fill:.2f} | "
                    f"PnL {leg.pnl:.2f} | {reason}"
                )
                
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
                with open(self.history_file, "r", encoding="utf-8") as f:
                    hist = json.load(f)
            except Exception:
                hist = []
        leg0 = self.trade_context.legs[0] if self.trade_context and self.trade_context.legs else None
        fill = 0.0
        if leg0:
            fill = float(leg0.exit_price or leg0.current_price or 0.0)
            leg0.exit_reason = reason
            if not leg0.exit_price:
                leg0.exit_price = fill
        entry_dt_str = self.trade_context.entry_time.strftime("%d %b %Y %H:%M:%S") if self.trade_context and self.trade_context.entry_time else datetime.now().strftime("%d %b %Y %H:%M:%S")
        exit_dt_str = datetime.now().strftime("%d %b %Y %H:%M:%S")

        self.daily_pnl += total_pnl
        # Dual keys: Kronos uses pnl/exit_reason; dashboard also reads net_pl/reason
        hist.append(
            {
                "date": entry_dt_str,
                "entry_time": entry_dt_str,
                "exit_time": exit_dt_str,
                "strategy": "ASTRA_V2",
                "signal": self.trade_context.signal_type if self.trade_context else "",
                "signal_type": self.trade_context.signal_type if self.trade_context else "",
                "symbol": leg0.symbol if leg0 else "",
                "entry_price": leg0.entry_price if leg0 else 0,
                "exit_price": fill if leg0 else 0,
                "pnl": total_pnl,
                "net_pl": total_pnl,
                "daily_total_pnl": self.daily_pnl,
                "exit_reason": reason,
                "reason": reason,
                "decision_payload": self.trade_context.decision_payload if self.trade_context else None
            }
        )
        try:
            with open(self.history_file, "w", encoding="utf-8") as f:
                json.dump(hist, f, indent=2)
        except Exception as e:
            logger.error(f"[AstraEM] history write failed: {e}")

    def _save_active_context(self):
        try:
            if not self.has_open_trade():
                if os.path.exists(self.active_context_file):
                    os.remove(self.active_context_file)
                return
            payload = {
                "status": "OPEN",
                "signal_type": self.trade_context.signal_type,
                "strategy": "ASTRA_V2",
                "entry_time": self.trade_context.entry_time.isoformat() if self.trade_context.entry_time else None,
                "index_entry": self.trade_context.index_entry,
                "tp_order_id": self.tp_order_id,
                "legs": [
                    {
                        "token": leg.token,
                        "symbol": leg.symbol,
                        "strike": leg.strike,
                        # TradeLeg uses option_type (CALL/PUT), not signal_type
                        "option_type": getattr(leg, "option_type", None)
                        or self.trade_context.signal_type,
                        "quantity": leg.quantity,
                        "entry_price": leg.entry_price,
                        "current_price": leg.current_price,
                        "status": leg.status,
                        "tp_price": getattr(leg, "tp_price", None),
                    }
                    for leg in self.trade_context.legs
                ],
            }
            with open(self.active_context_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            logger.error(f"[AstraEM] save context failed: {e}")

    def _load_active_context(self):
        if not os.path.exists(self.active_context_file):
            return
        try:
            with open(self.active_context_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("status") != "OPEN":
                return
            self.trade_context = TradeContext(
                signal_type=data.get("signal_type", "CALL"),
                strategy="ASTRA_V2",
                index_entry=float(data.get("index_entry") or 0),
            )
            if data.get("entry_time"):
                self.trade_context.entry_time = datetime.fromisoformat(data["entry_time"])
            self.tp_order_id = data.get("tp_order_id")
            for ld in data.get("legs") or []:
                opt_type = (
                    ld.get("option_type")
                    or ld.get("signal_type")
                    or data.get("signal_type")
                    or "CALL"
                )
                leg = TradeLeg(
                    ld["token"],
                    ld["symbol"],
                    float(ld["strike"]),
                    opt_type,
                    int(ld.get("quantity") or 1),
                    float(ld.get("entry_price") or 0),
                )
                leg.current_price = float(ld.get("current_price") or leg.entry_price)
                leg.status = ld.get("status") or "OPEN"
                if ld.get("tp_price"):
                    leg.tp_price = float(ld["tp_price"])  # type: ignore[attr-defined]
                self.trade_context.add_leg(leg)
            self.trade_context.status = "OPEN"
            logger.warning("[AstraEM] Restored OPEN trade from disk — will enforce flatten/TP/Chandelier")
        except Exception as e:
            logger.error(f"[AstraEM] load context failed: {e}")
            self.trade_context = None
