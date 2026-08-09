from __future__ import annotations

import json
import os
import time as pytime
from datetime import datetime, time

from common.config import get_data_dir
from common.execution.capital_engine import CapitalEngine
from common.logger import getlogger

logger = getlogger("stockexecution")

TRADES_DIR = os.path.join(get_data_dir(), "trades")
os.makedirs(TRADES_DIR, exist_ok=True)
OPEN_PATH = os.path.join(TRADES_DIR, "open_positions.json")
HISTORY_PATH = os.path.join(TRADES_DIR, "trade_history.json")

MAX_OPEN_POSITIONS = 1
LEVERAGE = 5.0
BROKERAGE_PCT = 0.0003
STT_PCT = 0.00025
SLIPPAGE_PCT = 0.0005
TOTAL_COST_PCT = BROKERAGE_PCT + STT_PCT + SLIPPAGE_PCT
TP_ATR_MULT = 1.0
SL_ATR_MULT = 1.0
CHANDELIER_ATR_MULT = 1.0
FALLBACK_TP_PCT = 0.010
FALLBACK_SL_PCT = 0.008
DAILY_LOSS_CAP_PCT = 0.05
MAX_HOLD_MINUTES = 60


class StockExecutionManager:
    def __init__(self, broker):
        self.broker = broker
        self.api = self.broker.api if hasattr(self.broker, "api") else self.broker
        self.open_trades = {}
        self.leverage = LEVERAGE
        self.capital_engine = CapitalEngine(mode="LIVE", portfolio_provider=self.broker, data_dir=get_data_dir())
        self.load_open()
        self._day_key = None
        self._day_start_margin = 0.0
        self._day_loss = 0.0

    def load_open(self):
        try:
            if os.path.exists(OPEN_PATH):
                with open(OPEN_PATH, "r") as f:
                    self.open_trades = json.load(f)
        except Exception as e:
            logger.warning(f"open_positions load failed: {e}")

    def save_open(self):
        try:
            with open(OPEN_PATH, "w") as f:
                json.dump(self.open_trades, f, indent=2, default=str)
        except Exception as e:
            logger.warning(f"open_positions save failed: {e}")

    def save_history(self, trade):
        try:
            history = []
            if os.path.exists(HISTORY_PATH):
                with open(HISTORY_PATH, "r") as f:
                    history = json.load(f)
            history.append(trade)
            with open(HISTORY_PATH, "w") as f:
                json.dump(history, f, indent=2, default=str)
        except Exception as e:
            logger.warning(f"trade_history save failed: {e}")

    def _ensure_day_snapshot(self):
        today = datetime.now().date()
        if self._day_key != today:
            self._day_key = today
            self._day_loss = 0.0
            current_funds = float(self.capital_engine.get_available_funds())
            self._day_start_margin = max(current_funds, 1.0)
            logger.info(f"[StockExecution] Day snapshot start_margin={self._day_start_margin:.2f}")

    def _place_order(self, kwargs: dict):
        if hasattr(self.api, "place_order"):
            return self.api.place_order(**kwargs)
        if hasattr(self.api, "placeorder"):
            return self.api.placeorder(**kwargs)
        return None

    def _order_history(self, ordno: str):
        if hasattr(self.api, "single_order_history"):
            return self.api.single_order_history(ordno)
        if hasattr(self.api, "singleorderhistory"):
            return self.api.singleorderhistory(ordno)
        return None

    def _confirm_fill(self, ordno: str, fallback_price: float):
        avgprc = fallback_price
        for _ in range(3):
            pytime.sleep(1.0)
            hist = self._order_history(ordno)
            if hist and isinstance(hist, list):
                row = hist[0]
                status = str(row.get("status", "")).upper()
                if status in {"FILLED", "COMPLETE", "COMPLETED"}:
                    try:
                        avgprc = float(row.get("avgprc", fallback_price))
                    except Exception:
                        avgprc = fallback_price
                    return True, avgprc
        return False, avgprc

    def execute_entry(self, signal, symbol, spot_price, reason, metrics=None):
        if len(self.open_trades) >= MAX_OPEN_POSITIONS:
            logger.info(f"[StockExecution] Global Lock Active. Ignoring signal for {symbol}.")
            return
        now_t = datetime.now().time()
        if now_t < time(9, 15) or now_t >= time(15, 0):
            return

        self._ensure_day_snapshot()
        margin_budget = float(self.capital_engine.get_available_funds())
        if margin_budget < 50:
            logger.warning("Insufficient margin budget.")
            return

        loss_cap_limit = -DAILY_LOSS_CAP_PCT * self._day_start_margin
        if self._day_loss <= loss_cap_limit:
            logger.warning(f"Daily loss cap hit {self._day_loss:.2f}/{loss_cap_limit:.2f}. No new entries today.")
            return

        notional_budget = margin_budget * self.leverage
        qty = int(notional_budget / max(spot_price, 0.01))
        if qty <= 0:
            return

        atr = float(metrics.get("atr", 0.0)) if metrics else 0.0
        if atr > 0:
            logger.info(f"[StockExecution] ATR15 exits TP={atr*TP_ATR_MULT:.2f}, InitSL={atr*SL_ATR_MULT:.2f}, Chandelier={atr*CHANDELIER_ATR_MULT:.2f}")
        else:
            logger.warning("ATR not available; using fixed fallback exits.")

        action = "B" if signal == "BUY" else "S"
        kwargs = {
            "buy_or_sell": action,
            "product_type": "I",
            "exchange": "NSE",
            "tradingsymbol": symbol,
            "quantity": qty,
            "price_type": "MKT",
            "price": 0,
            "trigger_price": None,
            "discloseqty": 0,
            "retention": "DAY",
        }

        try:
            logger.info(f"[StockExecution] Placing LIVE {action} MKT for {qty}x {symbol}...")
            resp = self._place_order(kwargs)
            if resp and isinstance(resp, dict) and str(resp.get("stat", "")).upper() == "OK":
                ordno = str(resp.get("norenordno", ""))
                filled, avgprc = self._confirm_fill(ordno, spot_price)
                if filled:
                    logger.info(f"[StockExecution] Entry FILLED @ {avgprc:.2f}")
                else:
                    logger.warning("[StockExecution] Fill unconfirmed, using spot price.")
                    avgprc = spot_price

                margin_used = qty * avgprc / self.leverage
                trade = {
                    "entry_time": datetime.now().isoformat(),
                    "symbol": symbol,
                    "type": signal,
                    "entry_price": avgprc,
                    "qty": qty,
                    "margin": round(margin_used, 2),
                    "leverage": self.leverage,
                    "reason": reason,
                    "broker_order_id": ordno,
                    "atr": round(atr, 4),
                    "highest": avgprc,
                    "lowest": avgprc,
                    "ml_features": metrics or {},
                }
                self.open_trades[symbol] = trade
                self.save_open()
            else:
                errmsg = resp.get("emsg", "Unknown") if isinstance(resp, dict) else str(resp)
                logger.error(f"[StockExecution] API REJECTION: {errmsg}")
        except Exception as e:
            logger.error(f"[StockExecution] Exception in execute_entry: {e}")

    def _close_trade(self, symbol, current_price, exit_reason):
        trade = self.open_trades.get(symbol)
        if not trade:
            return
        qty = int(trade["qty"])
        action = "S" if trade["type"] == "BUY" else "B"
        kwargs = {
            "buy_or_sell": action,
            "product_type": "I",
            "exchange": "NSE",
            "tradingsymbol": symbol,
            "quantity": qty,
            "price_type": "MKT",
            "price": 0,
            "trigger_price": None,
            "discloseqty": 0,
            "retention": "DAY",
        }

        try:
            logger.info(f"[StockExecution] Exiting {qty}x {symbol} reason={exit_reason}...")
            resp = self._place_order(kwargs)
            if resp and isinstance(resp, dict) and str(resp.get("stat", "")).upper() == "OK":
                ordno = str(resp.get("norenordno", ""))
                filled, avgprc = self._confirm_fill(ordno, current_price)
                if not filled:
                    avgprc = current_price
                entry = float(trade["entry_price"])
                gross_pnl = qty * (avgprc - entry) if trade["type"] == "BUY" else qty * (entry - avgprc)
                notional = qty * (entry + avgprc) / 2.0
                txncost = notional * TOTAL_COST_PCT
                net_pnl = gross_pnl - txncost
                logger.info(f"[StockExecution] CLOSED {symbol} {exit_reason}. Gross={gross_pnl:.2f}, Cost={txncost:.2f}, Net={net_pnl:.2f}")

                trade.update({
                    "exit_time": datetime.now().isoformat(),
                    "exit_price": avgprc,
                    "gross_pnl": round(gross_pnl, 2),
                    "txncost": round(txncost, 2),
                    "net_pnl": round(net_pnl, 2),
                    "exit_reason": exit_reason,
                    "strategy": "stock_order_flow",
                    "exit_broker_order_id": ordno,
                })
                self._day_loss += net_pnl
                self.save_history(trade)
                del self.open_trades[symbol]
                self.save_open()
            else:
                errmsg = resp.get("emsg", "Unknown") if isinstance(resp, dict) else str(resp)
                logger.error(f"[StockExecution] EXIT REJECTION: {errmsg}")
        except Exception as e:
            logger.error(f"[StockExecution] Exception in close_trade: {e}")

    def manage_open_trades(self, current_prices: dict):
        now_t = datetime.now().time()
        is_eod_flatten = now_t >= time(15, 10)
        self._ensure_day_snapshot()

        for symbol, trade in list(self.open_trades.items()):
            if symbol not in current_prices:
                continue
            current_price = float(current_prices[symbol])
            entry = float(trade["entry_price"])
            atr = float(trade.get("atr", 0.0))
            try:
                minutes_held = (datetime.now() - datetime.fromisoformat(trade["entry_time"])).total_seconds() / 60.0
            except Exception:
                minutes_held = 0.0

            exit_reason = None
            if is_eod_flatten:
                exit_reason = "EOD_FLATTEN"
            elif atr > 0:
                if trade["type"] == "BUY":
                    trade["highest"] = max(float(trade.get("highest", entry)), current_price)
                    chan_stop = float(trade["highest"]) - atr * CHANDELIER_ATR_MULT
                    init_stop = entry - atr * SL_ATR_MULT
                    stop_price = max(init_stop, chan_stop)
                    if current_price >= entry + atr * TP_ATR_MULT:
                        exit_reason = "TAKE_PROFIT"
                    elif current_price <= stop_price:
                        exit_reason = "CHANDELIER_EXIT" if chan_stop > init_stop else "STOP_LOSS"
                else:
                    trade["lowest"] = min(float(trade.get("lowest", entry)), current_price)
                    chan_stop = float(trade["lowest"]) + atr * CHANDELIER_ATR_MULT
                    init_stop = entry + atr * SL_ATR_MULT
                    stop_price = min(init_stop, chan_stop)
                    if current_price <= entry - atr * TP_ATR_MULT:
                        exit_reason = "TAKE_PROFIT"
                    elif current_price >= stop_price:
                        exit_reason = "CHANDELIER_EXIT" if chan_stop < init_stop else "STOP_LOSS"
            else:
                pnl_pct = (current_price - entry) / entry if trade["type"] == "BUY" else (entry - current_price) / entry
                if pnl_pct >= FALLBACK_TP_PCT:
                    exit_reason = "TAKE_PROFIT"
                elif pnl_pct <= -FALLBACK_SL_PCT:
                    exit_reason = "STOP_LOSS"

            if exit_reason is None and minutes_held >= MAX_HOLD_MINUTES:
                exit_reason = "TIME_STOP"

            if exit_reason:
                self._close_trade(symbol, current_price, exit_reason)
