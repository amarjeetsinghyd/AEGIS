"""
Kronos-only execution manager.

MUST NOT use KDO flow-decay / premium hard-SL / OI-reversal exits.
Exit policy (ARCHITECTURE.md §6.2):
  0) Force flatten at session flatten time
  1) +10% premium DAY limit TP (resting)
  2) 5m underlying Chandelier break → marketable LMT exit
  3) 10-minute hard time stop
"""

from __future__ import annotations

import json
import os
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

logger = get_logger("kronos_execution_manager")

# Locked Kronos risk parameters
TP_PCT = 0.10
MAX_HOLD_SEC = 600.0
CHANDELIER_PERIOD = 10
CHANDELIER_MULT = 2.0
KRONOS_ENTRY_CUTOFF = dt_time(14, 50, 0)
# CAS era: continuous cash for F&O underlyings ends 15:15; force-exit options by 15:10.
KRONOS_FORCE_FLATTEN = dt_time(15, 10, 0)


def _round_tick(price: float, tick: float = OPTION_TICK_SIZE) -> float:
    if price <= 0:
        return 0.0
    return round(round(price / tick) * tick, 2)


def _true_range(high, low, prev_close):
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def compute_chandelier_stops(df: pd.DataFrame, period: int = CHANDELIER_PERIOD, mult: float = CHANDELIER_MULT):
    """
    Returns (long_stop, short_stop) for the latest bar using underlying OHLC.
    long_stop  = HH(period) - mult*ATR
    short_stop = LL(period) + mult*ATR
    """
    if df is None or len(df) < period + 2:
        return None, None
    need = {"high", "low", "close"}
    if not need.issubset(df.columns):
        return None, None

    work = df.copy().tail(period + 5)
    closes = work["close"].astype(float).tolist()
    highs = work["high"].astype(float).tolist()
    lows = work["low"].astype(float).tolist()

    trs = []
    for i in range(1, len(work)):
        trs.append(_true_range(highs[i], lows[i], closes[i - 1]))
    if len(trs) < period:
        return None, None
    atr = float(np.mean(trs[-period:]))
    hh = float(max(highs[-period:]))
    ll = float(min(lows[-period:]))
    return hh - mult * atr, ll + mult * atr


class KronosExecutionManager:
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
            logger.info("[KronosEM] LIVE mode — Shoonya LMT / NRML path")
        else:
            self.order_lifecycle = PaperOrderLifecycle()
            logger.info("[KronosEM] PAPER mode")

        self.trade_context: Optional[TradeContext] = None
        self.trades_today = 0
        self.strike_cooldowns = {}
        self._option_cache = {}
        self.tp_order_id = None
        self.chandelier_stop = None

        trades_dir = os.path.join(self.data_dir, "trades")
        os.makedirs(trades_dir, exist_ok=True)
        self.history_file = os.path.join(trades_dir, "trade_history.json")
        self.active_context_file = os.path.join(trades_dir, "active_trade_context.json")
        self._load_active_context()

    # --- calendar gates ---
    @staticmethod
    def can_enter(now: datetime = None) -> bool:
        now = now or datetime.now()
        if not is_trading_day(now.date()):
            return False
        return dt_time(9, 20, 0) <= now.time() <= KRONOS_ENTRY_CUTOFF

    @staticmethod
    def must_force_flatten(now: datetime = None) -> bool:
        now = now or datetime.now()
        return now.time() >= KRONOS_FORCE_FLATTEN

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
            }
        return {
            "premium_min": NIFTY_PREMIUM_MIN,
            "premium_max": NIFTY_PREMIUM_MAX,
            "lot_size": NIFTY_LOT_SIZE,
            "index_name": "NIFTY",
        }

    def execute_entry(self, signal_type: str, index_price: float, decision_payload: dict = None):
        now = datetime.now()
        if self.has_open_trade():
            logger.warning("[KronosEM] Entry blocked — trade already open")
            return
        if not self.can_enter(now):
            logger.warning(f"[KronosEM] Entry blocked — outside Kronos window (cutoff {KRONOS_ENTRY_CUTOFF})")
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
        )
        if not allocations:
            logger.warning("[KronosEM] Entry aborted — no affordable strike")
            return

        opt_row, quantity, ltp = allocations[0]
        symbol = opt_row["symbol"]
        if symbol in self.strike_cooldowns and now < self.strike_cooldowns[symbol]:
            logger.warning(f"[KronosEM] Strike {symbol} in cooldown")
            return

        self.trade_context = TradeContext(
            signal_type=signal_type,
            strategy="KRONOS_AI",
            index_entry=index_price,
            decision_payload=decision_payload or {},
        )
        leg = TradeLeg(str(opt_row["token"]), symbol, float(opt_row["strike"]), signal_type, quantity, ltp)

        if not self.order_lifecycle.execute_entry(leg):
            logger.error(f"[KronosEM] Entry order rejected for {symbol}")
            self.trade_context = None
            return

        self.trade_context.add_leg(leg)
        self.capital_engine.deduct_funds(ltp * quantity * params["lot_size"])
        self.trades_today += 1
        self.tp_order_id = None
        self.chandelier_stop = None

        # Seed chandelier from current window if available
        try:
            window = self.data_fetcher.get_window_df()
            long_sl, short_sl = compute_chandelier_stops(window)
            self.chandelier_stop = long_sl if signal_type.upper() == "CALL" else short_sl
        except Exception as e:
            logger.warning(f"[KronosEM] Chandelier seed failed: {e}")

        self._place_take_profit(leg)
        self._save_active_context()
        logger.info(
            f"[KronosEM] ENTERED {signal_type} {quantity}x {symbol} @ {ltp:.2f} | "
            f"TP={_round_tick(ltp * (1 + TP_PCT)):.2f} | max_hold={int(MAX_HOLD_SEC)}s | "
            f"chandelier={self.chandelier_stop}"
        )

    def _place_take_profit(self, leg: TradeLeg):
        """Resting DAY limit sell at +10% premium. Paper mode marks synthetic TP only."""
        tp_price = _round_tick(leg.entry_price * (1.0 + TP_PCT))
        leg.tp_price = tp_price  # type: ignore[attr-defined]
        if isinstance(self.order_lifecycle, PaperOrderLifecycle):
            self.tp_order_id = f"PAPER_TP_{leg.symbol}"
            logger.info(f"[KronosEM] PAPER TP armed at {tp_price:.2f}")
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
                        logger.info(f"[KronosEM] DAY TP LMT placed {self.tp_order_id} @ {tp_price:.2f}")
                        return
                    logger.error(f"[KronosEM] TP place failed: {resp}")
            logger.warning("[KronosEM] TP resting order not placed — will synthetic-monitor premium ≥ +10%")
        except Exception as e:
            logger.error(f"[KronosEM] TP place exception: {e}")

    def _cancel_tp(self):
        if not self.tp_order_id or str(self.tp_order_id).startswith("PAPER_"):
            self.tp_order_id = None
            return
        try:
            api = getattr(self.broker, "_api", None)
            if api and hasattr(api, "cancel_order"):
                api.cancel_order(orderno=self.tp_order_id)
                logger.info(f"[KronosEM] Cancelled TP order {self.tp_order_id}")
        except Exception as e:
            logger.warning(f"[KronosEM] TP cancel warning: {e}")
        self.tp_order_id = None

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
            tp_price = getattr(leg, "tp_price", _round_tick(leg.entry_price * (1 + TP_PCT)))
            if leg.current_price >= tp_price > 0:
                self._cancel_tp()
                self._close_all_legs(f"Take-Profit Hit (+{TP_PCT*100:.0f}% premium @ {leg.current_price:.2f})")
                return

        # 0) Force flatten
        if self.must_force_flatten(now):
            self._cancel_tp()
            self._close_all_legs(f"Force Flatten (session >= {KRONOS_FORCE_FLATTEN.strftime('%H:%M')})")
            return

        # 2) Chandelier on 5m underlying
        df = underlying_df
        if df is None:
            try:
                df = self.data_fetcher.get_window_df()
            except Exception:
                df = None
        long_sl, short_sl = compute_chandelier_stops(df)
        sig = self.trade_context.signal_type.upper()
        if spot_price <= 0:
            try:
                spot_price = float(self.data_fetcher.get_index_ltp() or 0)
            except Exception:
                spot_price = 0.0

        if sig == "CALL" and long_sl is not None:
            # ratchet up only
            if self.chandelier_stop is None or long_sl > self.chandelier_stop:
                self.chandelier_stop = long_sl
            if spot_price > 0 and spot_price < self.chandelier_stop:
                self._cancel_tp()
                self._close_all_legs(
                    f"Chandelier Exit CALL (spot {spot_price:.2f} < stop {self.chandelier_stop:.2f})"
                )
                return
        elif sig == "PUT" and short_sl is not None:
            if self.chandelier_stop is None or short_sl < self.chandelier_stop:
                self.chandelier_stop = short_sl
            if spot_price > 0 and spot_price > self.chandelier_stop:
                self._cancel_tp()
                self._close_all_legs(
                    f"Chandelier Exit PUT (spot {spot_price:.2f} > stop {self.chandelier_stop:.2f})"
                )
                return

        # 3) Hard time stop
        if hold_sec >= MAX_HOLD_SEC:
            self._cancel_tp()
            self._close_all_legs(f"10-Minute Hard Time Stop ({int(hold_sec)}s)")
            return

    def _close_all_legs(self, reason: str):
        if not self.has_open_trade():
            return
        params = self.get_instrument_params()
        now = datetime.now()
        total_pnl = 0.0
        for leg in self.trade_context.legs:
            if leg.status != "OPEN":
                continue
            if self.order_lifecycle.execute_exit(leg, reason):
                leg.status = "CLOSED"
                leg.exit_time = now
                fill = leg.exit_price if leg.exit_price and leg.exit_price > 0 else leg.current_price
                leg.pnl = (fill - leg.entry_price) * leg.quantity * params["lot_size"]
                total_pnl += leg.pnl
                self.capital_engine.add_funds(fill * leg.quantity * params["lot_size"])
                self.strike_cooldowns[leg.symbol] = now + timedelta(minutes=3)
                logger.info(
                    f"[KronosEM] CLOSED {leg.symbol}: {leg.entry_price:.2f} -> {fill:.2f} | "
                    f"PnL {leg.pnl:.2f} | {reason}"
                )
        self.trade_context.status = "CLOSED"
        self.trade_context.exit_time = now
        self._append_history(reason, total_pnl)
        self.trade_context = None
        self.tp_order_id = None
        self.chandelier_stop = None
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
        hist.append(
            {
                "date": datetime.now().strftime("%d %b %Y %H:%M:%S"),
                "strategy": "KRONOS_AI",
                "signal": self.trade_context.signal_type if self.trade_context else "",
                "symbol": leg0.symbol if leg0 else "",
                "entry_price": leg0.entry_price if leg0 else 0,
                "exit_price": leg0.exit_price if leg0 else 0,
                "pnl": total_pnl,
                "exit_reason": reason,
            }
        )
        try:
            with open(self.history_file, "w", encoding="utf-8") as f:
                json.dump(hist, f, indent=2)
        except Exception as e:
            logger.error(f"[KronosEM] history write failed: {e}")

    def _save_active_context(self):
        try:
            if not self.has_open_trade():
                if os.path.exists(self.active_context_file):
                    os.remove(self.active_context_file)
                return
            payload = {
                "status": "OPEN",
                "signal_type": self.trade_context.signal_type,
                "strategy": "KRONOS_AI",
                "entry_time": self.trade_context.entry_time.isoformat() if self.trade_context.entry_time else None,
                "index_entry": self.trade_context.index_entry,
                "tp_order_id": self.tp_order_id,
                "chandelier_stop": self.chandelier_stop,
                "legs": [
                    {
                        "token": leg.token,
                        "symbol": leg.symbol,
                        "strike": leg.strike,
                        "signal_type": leg.signal_type,
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
            logger.error(f"[KronosEM] save context failed: {e}")

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
                strategy="KRONOS_AI",
                index_entry=float(data.get("index_entry") or 0),
            )
            if data.get("entry_time"):
                self.trade_context.entry_time = datetime.fromisoformat(data["entry_time"])
            self.tp_order_id = data.get("tp_order_id")
            self.chandelier_stop = data.get("chandelier_stop")
            for ld in data.get("legs") or []:
                leg = TradeLeg(
                    ld["token"],
                    ld["symbol"],
                    float(ld["strike"]),
                    ld.get("signal_type", "CALL"),
                    int(ld.get("quantity") or 1),
                    float(ld.get("entry_price") or 0),
                )
                leg.current_price = float(ld.get("current_price") or leg.entry_price)
                leg.status = ld.get("status") or "OPEN"
                if ld.get("tp_price"):
                    leg.tp_price = float(ld["tp_price"])  # type: ignore[attr-defined]
                self.trade_context.add_leg(leg)
            self.trade_context.status = "OPEN"
            logger.warning("[KronosEM] Restored OPEN trade from disk — will enforce flatten/TP/Chandelier")
        except Exception as e:
            logger.error(f"[KronosEM] load context failed: {e}")
            self.trade_context = None
