"""
trade_manager.py — Time-Adaptive Trailing Engine & Trade Lifecycle Manager.
- Morning (09:15 - 13:30 IST): Trail on 5-Minute 20 EMA (Wide multi-hour trend ride).
- Afternoon (13:30 - 15:15 IST): Trail on 5-Minute 9 EMA (Tight profit lock).
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
        self.open_trades = {}  # symbol -> trade state dict
        self.daily_closed_trades = []

    def register_trade(self, symbol: str, trade_dict: dict):
        self.open_trades[symbol] = trade_dict

    def get_open_count(self) -> int:
        return len(self.open_trades)

    def get_today_trades_count(self) -> int:
        return len(self.open_trades) + len(self.daily_closed_trades)

    def evaluate_exits(self, symbol: str, cur_price: float, arrays_5m: dict, cur_time: time = None) -> dict | None:
        """
        Evaluate Time-Adaptive Trailing Stops and Target Exits.
        """
        trade = self.open_trades.get(symbol)
        if not trade:
            return None

        if cur_time is None:
            cur_time = datetime.now().time()

        entry_p = trade["entry_price"]
        init_sl = trade["stop_loss"]
        tp1_p = trade["tp1_price"]
        tp2_p = trade["tp2_price"]
        qty = trade["qty"]

        # Track high watermark
        if cur_price > trade.get("highest_price", entry_p):
            trade["highest_price"] = cur_price

        # ── 1. Hard Initial Stop Loss Check ──
        if cur_price <= init_sl:
            return {
                "action": "CLOSE", "symbol": symbol, "exit_price": cur_price,
                "reason": f"STOP_LOSS_HIT ({cur_price:.2f} <= {init_sl:.2f})",
                "pnl": (cur_price - entry_p) * qty, "pnl_pct": ((cur_price - entry_p) / entry_p) * 100.0
            }

        # ── 2. Time-Adaptive EMA Trailing Stop Check (on 5m bar close) ──
        if arrays_5m and arrays_5m["count"] >= 20:
            closes = arrays_5m["close"]
            
            # Select Trailing EMA based on session time
            if cur_time < self.cfg["session_split_time"]:
                # Morning Session: 20 EMA
                trailing_period = self.cfg["morning_trailing_ema"]
                session_name = "MORNING_20_EMA"
            else:
                # Afternoon Session: 9 EMA
                trailing_period = self.cfg["afternoon_trailing_ema"]
                session_name = "AFTERNOON_9_EMA"

            ema_vals = compute_ema(closes, trailing_period)
            if len(ema_vals) > 0:
                cur_ema = ema_vals[-1]
                # If price was in profit (passed TP1 or +0.40%) and now closes below trailing EMA
                if cur_price >= entry_p * 1.004 and closes[-1] < cur_ema:
                    return {
                        "action": "CLOSE", "symbol": symbol, "exit_price": cur_price,
                        "reason": f"TRAILING_EXIT_{session_name} (Close {closes[-1]:.2f} < EMA {cur_ema:.2f})",
                        "pnl": (cur_price - entry_p) * qty, "pnl_pct": ((cur_price - entry_p) / entry_p) * 100.0
                    }

        # ── 3. EOD Force Square-off Check (15:10 IST) ──
        if cur_time >= self.cfg["eod_squareoff_time"]:
            return {
                "action": "CLOSE", "symbol": symbol, "exit_price": cur_price,
                "reason": "EOD_SQUAREOFF_15:10",
                "pnl": (cur_price - entry_p) * qty, "pnl_pct": ((cur_price - entry_p) / entry_p) * 100.0
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
