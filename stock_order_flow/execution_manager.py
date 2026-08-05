import os
import json
from datetime import datetime, time
from common.logger import get_logger
from common.config import get_data_dir
from common.execution.capital_engine import CapitalEngine

logger = get_logger("stock_execution")

JOURNAL_DIR = os.path.join(get_data_dir(), "decisions")
TRADES_DIR = os.path.join(get_data_dir(), "trades")
os.makedirs(JOURNAL_DIR, exist_ok=True)
os.makedirs(TRADES_DIR, exist_ok=True)
JOURNAL_PATH = os.path.join(JOURNAL_DIR, "stock_order_flow_journal.jsonl")
HISTORY_PATH = os.path.join(TRADES_DIR, "trade_history.json")
POST_TRACKING_PATH = os.path.join(TRADES_DIR, "post_trade_tracking.json")

class StockExecutionManager:
    """Simulates paper trading execution with 5x leverage."""
    def __init__(self, broker):
        self.broker = broker
        self.open_trades = {}
        self.post_trades = []
        self.leverage = 5.0
        self.capital_engine = CapitalEngine(mode="PAPER")

    def execute_entry(self, signal, symbol, spot_price, reason):
        now_time = datetime.now().time()
        if now_time < time(9, 15) or now_time >= time(15, 0):
            logger.debug(f"Skipping entry for {symbol}: outside 09:15-15:00 window")
            return

        if symbol in self.open_trades:
            logger.info(f"Skipping entry for {symbol}, trade already open.")
            return

        funds = self.capital_engine.get_available_funds()
        qty = int((funds * self.leverage) / spot_price)
        if qty == 0:
            logger.warning(f"Insufficient buying power for {symbol} at {spot_price}")
            return
            
        margin = (qty * spot_price) / self.leverage
        self.capital_engine.deduct_funds(margin)

        trade = {
            "entry_time": datetime.now().isoformat(),
            "symbol": symbol,
            "type": signal,
            "entry_price": spot_price,
            "qty": qty,
            "reason": reason
        }
        self.open_trades[symbol] = trade
        
        logger.info(f"PAPER TRADE EXECUTED: {signal} {qty}x {symbol} @ {spot_price}. Reason: {reason}")
        self._append_journal({"event": "ENTRY", **trade})


    def manage_open_trades(self, current_prices):
        self._manage_post_trades(current_prices)
        """Manage Take Profit (0.5%) and Stop Loss (0.5%)"""
        now_time = datetime.now().time()
        is_eod_flatten = now_time >= time(15, 10)

        for symbol, trade in list(self.open_trades.items()):
            if symbol not in current_prices:
                continue
                
            current_price = current_prices[symbol]
            entry = trade["entry_price"]
            
            pnl_pct = 0.0
            if trade["type"] == "BUY":
                pnl_pct = (current_price - entry) / entry
            elif trade["type"] == "SELL":
                pnl_pct = (entry - current_price) / entry
                
            exit_reason = None
            if is_eod_flatten:
                exit_reason = "EOD_FLATTEN"
            elif pnl_pct >= 0.005:
                exit_reason = "TAKE_PROFIT"
            elif pnl_pct <= -0.0050:
                exit_reason = "STOP_LOSS"
                
            if exit_reason:
                trade["exit_time"] = datetime.now().isoformat()
                trade["exit_price"] = current_price
                trade["pnl_pct"] = pnl_pct
                trade["exit_reason"] = exit_reason
                
                margin = (trade["qty"] * trade["entry_price"]) / self.leverage
                abs_pnl = trade["qty"] * (current_price - trade["entry_price"]) if trade["type"] == "BUY" else trade["qty"] * (trade["entry_price"] - current_price)
                self.capital_engine.add_funds(margin + abs_pnl)
                
                logger.info(f"PAPER TRADE CLOSED: {symbol} {exit_reason} at {current_price} | PNL: {pnl_pct*100:.2f}%")
                self._append_journal({"event": "EXIT", **trade})
                self._append_trade_history(trade)
                
                # Start tracking
                post_trade = trade.copy()
                post_trade["max_price_after"] = current_price
                post_trade["min_price_after"] = current_price
                self.post_trades.append(post_trade)
                self._save_post_trades()
                
                del self.open_trades[symbol]



    def _manage_post_trades(self, current_prices):
        updated = False
        for t in self.post_trades:
            symbol = t["symbol"]
            if symbol in current_prices:
                cp = current_prices[symbol]
                if cp > t["max_price_after"]:
                    t["max_price_after"] = cp
                    updated = True
                if cp < t["min_price_after"]:
                    t["min_price_after"] = cp
                    updated = True
        
        if updated:
            self._save_post_trades()

    def _save_post_trades(self):
        try:
            with open(POST_TRACKING_PATH, "w", encoding="utf-8") as f:
                json.dump(self.post_trades, f, indent=2)
        except Exception:
            pass

    def _append_journal(self, row):
        try:
            with open(JOURNAL_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, default=str) + "\n")
        except Exception as e:
            logger.error(f"Journal write error: {e}")

    def _append_trade_history(self, trade):
        try:
            history = []
            if os.path.exists(HISTORY_PATH):
                with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                    history = json.load(f)
            # Format trade for dashboard compatibility
            formatted_trade = {
                "date": trade["entry_time"],
                "exit_time": trade["exit_time"],
                "symbol": trade["symbol"],
                "signal": trade["type"],
                "strategy": "order_flow",
                "reason": trade["exit_reason"],
                "result": f"{trade['pnl_pct']*100:.2f}%"
            }
            history.append(formatted_trade)
            with open(HISTORY_PATH, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=2)
        except Exception as e:
            logger.error(f"Trade history write error: {e}")
