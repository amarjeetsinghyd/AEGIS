"""
trade_manager.py — Two-Way Adaptive Trailing Engine & Trade Lifecycle Manager for STRATOS.
- Long: Stop below SL, trail on rising 20/9 EMA.
- Short: Stop above SL, trail on falling 20/9 EMA.
"""
from __future__ import annotations
from datetime import datetime, time
import numpy as np
from STRATOS.config import STRATOS_CONFIG

def compute_ema(prices: np.ndarray, period: int) -> np.ndarray:
    n = len(prices)
    if n < period:
        return np.array([])
    alpha = 2.0 / (period + 1.0)
    ema = np.empty(n, dtype=np.float64)
    ema[0] = prices[0]
    for i in range(1, n):
        ema[i] = ema[i-1] + alpha * (prices[i] - ema[i-1])
    return ema

class ActiveTradeManager:
    def __init__(self, cfg: dict = None):
        self.cfg = cfg or STRATOS_CONFIG
        self.open_trades = {}
        self.daily_closed_trades = []

    def register_trade(self, symbol: str, trade_dict: dict):
        self.open_trades[symbol] = trade_dict

    def get_open_count(self) -> int:
        return len(self.open_trades)

    def get_today_trades_count(self) -> int:
        return len(self.open_trades) + len(self.daily_closed_trades)

    def evaluate_exits(self, symbol: str, cur_price: float, arrays: dict = None, cur_time: time = None) -> dict | None:
        trade = self.open_trades.get(symbol)
        if not trade:
            return None

        if cur_time is None:
            cur_time = datetime.now().time()

        side = trade.get("side", "BUY")
        entry_p = trade["entry_price"]
        init_sl = trade["stop_loss"]
        tp1_p = trade.get("tp1_price", 0.0)
        tp2_p = trade.get("tp2_price", 0.0)
        qty = trade["qty"]

        # ── 1. BUY (Long) Management ──
        if side == "BUY":
            if cur_price > trade.get("highest_price", entry_p):
                trade["highest_price"] = cur_price

            # Hard SL
            if cur_price <= init_sl:
                return {
                    "action": "CLOSE", "symbol": symbol, "exit_price": cur_price,
                    "reason": f"STOP_LOSS_HIT ({cur_price:.2f} <= {init_sl:.2f})",
                    "pnl": (cur_price - entry_p) * qty, "pnl_pct": ((cur_price - entry_p) / entry_p) * 100.0
                }

            # TP2 Target
            if tp2_p > 0 and cur_price >= tp2_p:
                return {
                    "action": "CLOSE", "symbol": symbol, "exit_price": cur_price,
                    "reason": f"TARGET_2_HIT ({cur_price:.2f} >= {tp2_p:.2f})",
                    "pnl": (cur_price - entry_p) * qty, "pnl_pct": ((cur_price - entry_p) / entry_p) * 100.0
                }

            # Trailing Stop
            if arrays and len(arrays.get("close", [])) >= 10:
                closes = arrays["close"]
                trailing_period = self.cfg.get("morning_trailing_ema", 20) if cur_time < self.cfg.get("session_split_time", time(13, 30)) else self.cfg.get("afternoon_trailing_ema", 9)
                sess_name = "MORNING_20_EMA" if cur_time < self.cfg.get("session_split_time", time(13, 30)) else "AFTERNOON_9_EMA"

                ema_vals = compute_ema(closes, trailing_period)
                if len(ema_vals) > 0:
                    cur_ema = ema_vals[-1]
                    if cur_price >= entry_p * 1.0035 and closes[-1] < cur_ema:
                        return {
                            "action": "CLOSE", "symbol": symbol, "exit_price": cur_price,
                            "reason": f"TRAILING_EXIT_{sess_name}",
                            "pnl": (cur_price - entry_p) * qty, "pnl_pct": ((cur_price - entry_p) / entry_p) * 100.0
                        }

            # EOD Square-off
            if cur_time >= self.cfg.get("eod_squareoff_time", time(15, 10)):
                return {
                    "action": "CLOSE", "symbol": symbol, "exit_price": cur_price,
                    "reason": "EOD_SQUAREOFF_15:10",
                    "pnl": (cur_price - entry_p) * qty, "pnl_pct": ((cur_price - entry_p) / entry_p) * 100.0
                }

        # ── 2. SELL (Short) Management ──
        elif side == "SELL":
            if cur_price < trade.get("lowest_price", entry_p):
                trade["lowest_price"] = cur_price

            # Hard SL
            if cur_price >= init_sl:
                return {
                    "action": "CLOSE", "symbol": symbol, "exit_price": cur_price,
                    "reason": f"STOP_LOSS_HIT ({cur_price:.2f} >= {init_sl:.2f})",
                    "pnl": (entry_p - cur_price) * qty, "pnl_pct": ((entry_p - cur_price) / entry_p) * 100.0
                }

            # TP2 Target
            if tp2_p > 0 and cur_price <= tp2_p:
                return {
                    "action": "CLOSE", "symbol": symbol, "exit_price": cur_price,
                    "reason": f"TARGET_2_HIT ({cur_price:.2f} <= {tp2_p:.2f})",
                    "pnl": (entry_p - cur_price) * qty, "pnl_pct": ((entry_p - cur_price) / entry_p) * 100.0
                }

            # Trailing Stop
            if arrays and len(arrays.get("close", [])) >= 10:
                closes = arrays["close"]
                trailing_period = self.cfg.get("morning_trailing_ema", 20) if cur_time < self.cfg.get("session_split_time", time(13, 30)) else self.cfg.get("afternoon_trailing_ema", 9)
                sess_name = "MORNING_20_EMA" if cur_time < self.cfg.get("session_split_time", time(13, 30)) else "AFTERNOON_9_EMA"

                ema_vals = compute_ema(closes, trailing_period)
                if len(ema_vals) > 0:
                    cur_ema = ema_vals[-1]
                    if cur_price <= entry_p * 0.9965 and closes[-1] > cur_ema:
                        return {
                            "action": "CLOSE", "symbol": symbol, "exit_price": cur_price,
                            "reason": f"TRAILING_EXIT_{sess_name}",
                            "pnl": (entry_p - cur_price) * qty, "pnl_pct": ((entry_p - cur_price) / entry_p) * 100.0
                        }

            # EOD Square-off
            if cur_time >= self.cfg.get("eod_squareoff_time", time(15, 10)):
                return {
                    "action": "CLOSE", "symbol": symbol, "exit_price": cur_price,
                    "reason": "EOD_SQUAREOFF_15:10",
                    "pnl": (entry_p - cur_price) * qty, "pnl_pct": ((entry_p - cur_price) / entry_p) * 100.0
                }

        return None

    def close_trade(self, symbol: str, exit_dict: dict):
        if symbol in self.open_trades:
            trade = self.open_trades.pop(symbol)
            trade.update(exit_dict)
            trade["close_time"] = datetime.now().isoformat()
            self.daily_closed_trades.append(trade)
            return trade
        return None
