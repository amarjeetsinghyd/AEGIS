"""LIVE execution for Stock Order Flow — Strict Global Lock, MIS MKT Orders.
Phase-2: accepts metrics dict (CVD/VP/context) and stores it as ml_features
inside every trade record — the dataset for future XGBoost confidence model."""
from __future__ import annotations
import json
import os
import time as pytime
from datetime import datetime, time
from common.config import get_data_dir
from common.execution.capital_engine import CapitalEngine
from common.logger import get_logger

logger = get_logger("stock_execution")

JOURNAL_DIR = os.path.join(get_data_dir(), "decisions")
TRADES_DIR = os.path.join(get_data_dir(), "trades")
os.makedirs(JOURNAL_DIR, exist_ok=True)
os.makedirs(TRADES_DIR, exist_ok=True)

OPEN_PATH = os.path.join(TRADES_DIR, "open_positions.json")
HISTORY_PATH = os.path.join(TRADES_DIR, "trade_history.json")

MAX_OPEN_POSITIONS = 1
LEVERAGE = 5.0
TP_PCT = 0.010
SL_PCT = 0.008  # AUDIT E2: noise-safe
MAX_HOLD_MINUTES = 60


class StockExecutionManager:
    def __init__(self, broker):
        self.broker = broker
        self.api = self.broker._api if hasattr(self.broker, "_api") else self.broker
        self.open_trades = {}
        self.leverage = LEVERAGE
        self.capital_engine = CapitalEngine(mode="LIVE", portfolio_provider=self.broker, data_dir=get_data_dir())
        self._load_open()
        self._day_key = None      # AUDIT E3
        self._day_loss = 0.0      # AUDIT E3

    def _load_open(self):
        try:
            if os.path.exists(OPEN_PATH):
                with open(OPEN_PATH, "r") as f:
                    self.open_trades = json.load(f)
        except Exception as e:
            logger.warning(f"open_positions load failed: {e}")

    def _save_open(self):
        try:
            with open(OPEN_PATH, "w") as f:
                json.dump(self.open_trades, f, indent=2, default=str)
        except Exception as e:
            logger.warning(f"open_positions save failed: {e}")

    def _save_history(self, trade):
        try:
            _today = datetime.now().date()
            if self._day_key != _today:
                self._day_key = _today
                self._day_loss = 0.0
            self._day_loss += float(trade.get("net_pl") or 0.0)  # AUDIT E4
            history = []
            if os.path.exists(HISTORY_PATH):
                with open(HISTORY_PATH, "r") as f:
                    history = json.load(f)
            history.append(trade)
            with open(HISTORY_PATH, "w") as f:
                json.dump(history, f, indent=2, default=str)
        except Exception as e:
            logger.warning(f"trade_history save failed: {e}")

    def execute_entry(self, signal, symbol, spot_price, reason, metrics=None):
        if len(self.open_trades) >= MAX_OPEN_POSITIONS:
            logger.info(f"Global Lock Active: Ignoring {signal} for {symbol}.")
            return
        now_time = datetime.now().time()
        if now_time < time(9, 15) or now_time >= time(15, 0):
            return

        margin_budget = float(self.capital_engine.get_available_funds())
        if margin_budget < 50:
            logger.warning("Insufficient margin budget.")
            return

        _today = datetime.now().date()
        if self._day_key != _today:
            self._day_key = _today
            self._day_loss = 0.0
        if self._day_loss <= -0.02 * max(margin_budget, 1.0):
            logger.warning(f"Daily loss cap hit ({self._day_loss:.2f}) — no new entries today.")
            return  # AUDIT E5

        notional_budget = min(margin_budget * self.leverage, margin_budget * 2.0)  # AUDIT E1
        qty = int(notional_budget / spot_price)
        if qty <= 0:
            return

        action = "B" if signal == "BUY" else "S"
        kwargs = {
            "buy_or_sell": action, "product_type": "I", "exchange": "NSE",
            "tradingsymbol": symbol, "quantity": qty, "price_type": "MKT",
            "price": 0, "trigger_price": None, "discloseqty": 0, "retention": "DAY"
        }
        try:
            logger.info(f"[StockExecution] Placing LIVE {action} MKT for {qty}x {symbol}...")
            resp = self.api.place_order(**kwargs) if hasattr(self.api, "place_order") else None
            if resp and isinstance(resp, dict) and resp.get("stat") == "Ok" and resp.get("norenordno"):
                ordno = resp["norenordno"]
                avg_prc = spot_price
                fill_confirmed = False
                for _ in range(3):
                    pytime.sleep(1.0)
                    history = self.api.single_order_history(orderno=ordno) if hasattr(self.api, "single_order_history") else []
                    if history and isinstance(history, list) and history[0].get("status") in ["FILLED", "COMPLETE"]:
                        fill_confirmed = True
                        avg_prc = float(history[0].get("avgprc", spot_price))
                        break
                if fill_confirmed:
                    logger.info(f"[StockExecution] ✅ Entry FILLED @ Avg Price: {avg_prc:.2f}")
                else:
                    logger.warning(f"[StockExecution] ⚠️ Fill unconfirmed, using spot.")

                margin = (qty * avg_prc) / self.leverage
                trade = {
                    "entry_time": datetime.now().isoformat(), "symbol": symbol, "type": signal,
                    "entry_price": avg_prc, "qty": qty, "margin": round(margin, 2),
                    "leverage": self.leverage, "reason": reason, "broker_order_id": ordno,
                    # Phase-2: ML feature snapshot at entry (future training label source)
                    "ml_features": metrics or {},
                }
                self.open_trades[symbol] = trade
                self._save_open()
            else:
                err_msg = resp.get("emsg", "Unknown") if isinstance(resp, dict) else str(resp)
                logger.error(f"[StockExecution] 🚨 API REJECTION: {err_msg}")
        except Exception as e:
            logger.error(f"[StockExecution] Exception in execute_entry: {e}")

    def manage_open_trades(self, current_prices):
        now_time = datetime.now().time()
        is_eod_flatten = now_time >= time(15, 10)
        for symbol, trade in list(self.open_trades.items()):
            if symbol not in current_prices:
                continue
            current_price = float(current_prices[symbol])
            entry = float(trade["entry_price"])
            pnl_pct = (current_price - entry) / entry if trade["type"] == "BUY" else (entry - current_price) / entry
            try:
                minutes_held = (datetime.now() - datetime.fromisoformat(trade["entry_time"])).total_seconds() / 60.0
            except Exception:
                minutes_held = 0.0
            exit_reason = None
            if is_eod_flatten:
                exit_reason = "EOD_FLATTEN"
            elif pnl_pct >= TP_PCT:
                exit_reason = "TAKE_PROFIT"
            elif pnl_pct <= -SL_PCT:
                exit_reason = "STOP_LOSS"
            elif minutes_held >= MAX_HOLD_MINUTES:
                exit_reason = "TIME_STOP"
            if exit_reason:
                self._close_trade(symbol, current_price, exit_reason)

    def _close_trade(self, symbol, current_price, exit_reason):
        trade = self.open_trades.get(symbol)
        if not trade:
            return
        qty = int(trade["qty"])
        action = "S" if trade["type"] == "BUY" else "B"
        kwargs = {
            "buy_or_sell": action, "product_type": "I", "exchange": "NSE",
            "tradingsymbol": symbol, "quantity": qty, "price_type": "MKT",
            "price": 0, "trigger_price": None, "discloseqty": 0, "retention": "DAY"
        }
        try:
            logger.info(f"[StockExecution] Exiting {qty}x {symbol} ({exit_reason})...")
            resp = self.api.place_order(**kwargs) if hasattr(self.api, "place_order") else None
            if resp and isinstance(resp, dict) and resp.get("stat") == "Ok" and resp.get("norenordno"):
                ordno = resp["norenordno"]
                avg_prc = current_price
                for _ in range(3):
                    pytime.sleep(1.0)
                    history = self.api.single_order_history(orderno=ordno) if hasattr(self.api, "single_order_history") else []
                    if history and isinstance(history, list) and history[0].get("status") in ["FILLED", "COMPLETE"]:
                        avg_prc = float(history[0].get("avgprc", current_price))
                        break
                entry = float(trade["entry_price"])
                abs_pnl = qty * (avg_prc - entry) if trade["type"] == "BUY" else qty * (entry - avg_prc)
                logger.info(f"[StockExecution] CLOSED: {symbol} {exit_reason} | PnL=₹{abs_pnl:.2f}")
                trade["exit_time"] = datetime.now().isoformat()
                trade["exit_price"] = avg_prc
                trade["net_pl"] = round(abs_pnl, 2)
                trade["exit_reason"] = exit_reason
                trade["strategy"] = "stock_order_flow"
                trade["exit_broker_order_id"] = ordno
                self._save_history(trade)
                del self.open_trades[symbol]
                self._save_open()
            else:
                err_msg = resp.get("emsg", "Unknown") if isinstance(resp, dict) else str(resp)
                logger.error(f"[StockExecution] 🚨 EXIT REJECTION: {err_msg}")
        except Exception as e:
            logger.error(f"[StockExecution] Exception in _close_trade: {e}")