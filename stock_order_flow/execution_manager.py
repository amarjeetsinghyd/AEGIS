"""Paper execution for Stock Order Flow — MIS-style 5x margin, dashboard-compatible history."""
from __future__ import annotations

import json
import os
from datetime import datetime, time

from common.config import get_data_dir, INITIAL_PAPER_CAPITAL
from common.execution.capital_engine import CapitalEngine
from common.logger import get_logger

logger = get_logger("stock_execution")

JOURNAL_DIR = os.path.join(get_data_dir(), "decisions")
TRADES_DIR = os.path.join(get_data_dir(), "trades")
os.makedirs(JOURNAL_DIR, exist_ok=True)
os.makedirs(TRADES_DIR, exist_ok=True)
JOURNAL_PATH = os.path.join(JOURNAL_DIR, "stock_order_flow_journal.jsonl")
HISTORY_PATH = os.path.join(TRADES_DIR, "trade_history.json")
POST_TRACKING_PATH = os.path.join(TRADES_DIR, "post_trade_tracking.json")
OPEN_PATH = os.path.join(TRADES_DIR, "open_positions.json")

# MIS-style: up to this many concurrent names; margin split across remaining slots
MAX_OPEN_POSITIONS = 1
LEVERAGE = 5.0
TP_PCT = 0.005
SL_PCT = 0.005


class StockExecutionManager:
    """
    Paper MIS mimic:
      - Available cash is margin.
      - Notional = margin × 5 (leverage).
      - New entries only consume a fair share of remaining cash so other names can trade.
    """

    def __init__(self, broker):
        self.broker = broker
        self.open_trades = {}
        self.post_trades = []
        self.leverage = LEVERAGE
        self.capital_engine = CapitalEngine(mode="PAPER", data_dir=get_data_dir())
        self._load_open()
        self._load_post()

    def _load_open(self):
        try:
            if os.path.exists(OPEN_PATH):
                with open(OPEN_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self.open_trades = data
                    logger.info(f"Restored {len(self.open_trades)} open SOF positions from disk")
        except Exception as e:
            logger.warning(f"open_positions load failed: {e}")

    def _load_post(self):
        try:
            if os.path.exists(POST_TRACKING_PATH):
                with open(POST_TRACKING_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    self.post_trades = data
        except Exception:
            pass

    def _save_open(self):
        try:
            with open(OPEN_PATH, "w", encoding="utf-8") as f:
                json.dump(self.open_trades, f, indent=2, default=str)
        except Exception as e:
            logger.warning(f"open_positions save failed: {e}")

    def _margin_budget(self) -> float:
        """Equal-split remaining free cash across free slots (MIS diversification)."""
        funds = float(self.capital_engine.get_available_funds())
        open_n = len(self.open_trades)
        slots_left = max(1, MAX_OPEN_POSITIONS - open_n)
        return max(0.0, funds / slots_left)

    def execute_entry(self, signal, symbol, spot_price, reason):
        # 1. GLOBAL CONCURRENCY LOCK (One trade at a time)
        if len(self.open_trades) >= 1 and symbol not in self.open_trades:
            logger.info(f"Concurrency Lock Active: Ignoring new {signal} signal for {symbol} because we are already holding an open trade.")
            return

        now_time = datetime.now().time()
        if now_time < time(9, 15) or now_time >= time(15, 0):
            logger.info(f"Skipping entry for {symbol}: outside 09:15-15:00 window")
            return

        if spot_price is None or float(spot_price) <= 0:
            logger.warning(f"Invalid spot for {symbol}: {spot_price}")
            return
        spot_price = float(spot_price)

        # Opposite signal while long/short → Ignore and let SL/TP handle the exit
        if symbol in self.open_trades:
            existing = self.open_trades[symbol]
            if existing.get("type") != signal:
                logger.info(f"Concurrency Lock Active: Ignoring reverse {signal} signal for {symbol}. Waiting for natural TP/SL exit.")
            else:
                logger.info(f"Skipping entry for {symbol}, same-side trade already open.")
            return

        if len(self.open_trades) >= MAX_OPEN_POSITIONS:
            logger.info(f"Skipping {symbol}: max open positions ({MAX_OPEN_POSITIONS}) reached")
            return

        margin_budget = self._margin_budget()
        if margin_budget < 50:
            logger.warning(
                f"Insufficient margin budget for {symbol}: "
                f"free=₹{self.capital_engine.get_available_funds():.2f} budget=₹{margin_budget:.2f}"
            )
            return

        # MIS: notional = margin × leverage; qty from notional
        notional_budget = margin_budget * self.leverage
        qty = int(notional_budget / spot_price)
        if qty <= 0:
            logger.warning(
                f"Insufficient buying power for {symbol} at {spot_price} "
                f"(margin_budget=₹{margin_budget:.2f}, {self.leverage:.0f}x → "
                f"notional=₹{notional_budget:.2f})"
            )
            return

        margin = (qty * spot_price) / self.leverage
        free = float(self.capital_engine.get_available_funds())
        if margin > free:
            qty = int((free * self.leverage) / spot_price)
            if qty <= 0:
                logger.warning(f"Insufficient free cash for {symbol}")
                return
            margin = (qty * spot_price) / self.leverage

        self.capital_engine.deduct_funds(margin)

        trade = {
            "entry_time": datetime.now().isoformat(),
            "symbol": symbol,
            "type": signal,
            "signal": signal,
            "entry_price": spot_price,
            "qty": qty,
            "margin": round(margin, 2),
            "notional": round(qty * spot_price, 2),
            "leverage": self.leverage,
            "reason": reason,
        }
        self.open_trades[symbol] = trade
        self._save_open()

        logger.info(
            f"PAPER MIS ENTRY: {signal} {qty}x {symbol} @ {spot_price:.2f} | "
            f"margin=₹{margin:.2f} notional=₹{qty*spot_price:.2f} "
            f"({self.leverage:.0f}x) | free→₹{self.capital_engine.get_available_funds():.2f} | {reason}"
        )
        self._append_journal({"event": "ENTRY", **trade})

    def manage_open_trades(self, current_prices):
        self._manage_post_trades(current_prices)
        now_time = datetime.now().time()
        is_eod_flatten = now_time >= time(15, 10)

        for symbol, trade in list(self.open_trades.items()):
            if symbol not in current_prices:
                continue
            current_price = float(current_prices[symbol])
            entry = float(trade["entry_price"])

            if trade["type"] == "BUY":
                pnl_pct = (current_price - entry) / entry
            else:
                pnl_pct = (entry - current_price) / entry

            exit_reason = None
            if is_eod_flatten:
                exit_reason = "EOD_FLATTEN"
            elif pnl_pct >= TP_PCT:
                exit_reason = "TAKE_PROFIT"
            elif pnl_pct <= -SL_PCT:
                exit_reason = "STOP_LOSS"

            if exit_reason:
                self._close_trade(symbol, current_price, exit_reason)

    def _close_trade(self, symbol: str, current_price: float, exit_reason: str):
        trade = self.open_trades.get(symbol)
        if not trade:
            return
        current_price = float(current_price)
        entry = float(trade["entry_price"])
        qty = int(trade["qty"])
        leverage = float(trade.get("leverage") or self.leverage)
        margin = float(trade.get("margin") or ((qty * entry) / leverage))

        if trade["type"] == "BUY":
            pnl_pct = (current_price - entry) / entry
            abs_pnl = qty * (current_price - entry)
        else:
            pnl_pct = (entry - current_price) / entry
            abs_pnl = qty * (entry - current_price)

        # abs_pnl is already on 5x notional (qty sized with leverage). RoM shows MIS effect.
        rom_pct = (abs_pnl / margin) if margin > 0 else 0.0

        trade["exit_time"] = datetime.now().isoformat()
        trade["exit_price"] = current_price
        trade["pnl_pct"] = pnl_pct
        trade["net_pl"] = round(abs_pnl, 2)
        trade["return_on_margin_pct"] = rom_pct
        trade["exit_reason"] = exit_reason
        trade["signal"] = trade.get("type")

        self.capital_engine.add_funds(margin + abs_pnl)

        logger.info(
            f"PAPER MIS EXIT: {symbol} {exit_reason} @ {current_price:.2f} | "
            f"qty={qty} pnl=₹{abs_pnl:.2f} (spot {pnl_pct*100:.2f}% | "
            f"RoM {rom_pct*100:.2f}% on ₹{margin:.2f} margin @ {leverage:.0f}x) | "
            f"free→₹{self.capital_engine.get_available_funds():.2f}"
        )
        self._append_journal({"event": "EXIT", **trade})
        self._append_trade_history(trade)

        post_trade = dict(trade)
        post_trade["max_price_after"] = current_price
        post_trade["min_price_after"] = current_price
        self.post_trades.append(post_trade)
        self._save_post_trades()

        del self.open_trades[symbol]
        self._save_open()

    def _manage_post_trades(self, current_prices):
        updated = False
        for t in self.post_trades:
            symbol = t["symbol"]
            if symbol not in current_prices:
                continue
            cp = float(current_prices[symbol])
            if cp > t.get("max_price_after", cp):
                t["max_price_after"] = cp
                updated = True
            if cp < t.get("min_price_after", cp):
                t["min_price_after"] = cp
                updated = True
        if updated:
            self._save_post_trades()

    def _save_post_trades(self):
        try:
            with open(POST_TRACKING_PATH, "w", encoding="utf-8") as f:
                json.dump(self.post_trades, f, indent=2, default=str)
        except Exception:
            pass

    def _append_journal(self, row):
        try:
            with open(JOURNAL_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, default=str) + "\n")
        except Exception as e:
            logger.error(f"Journal write error: {e}")

    def _append_trade_history(self, trade):
        """Dashboard expects entry_price / exit_price / net_pl (₹)."""
        try:
            history = []
            if os.path.exists(HISTORY_PATH):
                with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                    history = json.load(f)
                    if not isinstance(history, list):
                        history = []
            pnl_pct = float(trade.get("pnl_pct") or 0.0)
            rom = trade.get("return_on_margin_pct")
            if rom is None:
                m = float(trade.get("margin") or 0)
                rom = (float(trade.get("net_pl") or 0) / m) if m > 0 else 0.0
            lev = float(trade.get("leverage") or LEVERAGE)
            formatted_trade = {
                "date": trade["entry_time"],
                "entry_time": trade["entry_time"],
                "exit_time": trade["exit_time"],
                "symbol": trade["symbol"],
                "signal": trade.get("type") or trade.get("signal"),
                "strategy": "order_flow",
                "entry_price": trade["entry_price"],
                "exit_price": trade["exit_price"],
                "qty": trade["qty"],
                "margin": trade.get("margin"),
                "notional": trade.get("notional") or (trade["qty"] * trade["entry_price"]),
                "leverage": lev,
                "net_pl": trade.get("net_pl"),
                "pnl": trade.get("net_pl"),  # rupees for dashboard normalizer
                "pnl_pct": pnl_pct,
                "return_on_margin_pct": rom,
                "reason": trade.get("exit_reason"),
                "exit_reason": trade.get("exit_reason"),
                # Spot move vs leveraged return on margin (MIS 5x)
                "result": (
                    f"{pnl_pct*100:+.2f}% spot | {float(rom)*100:+.2f}% RoM ({lev:.0f}x) | "
                    f"₹{float(trade.get('net_pl') or 0):.2f}"
                ),
                "entry_reason": trade.get("reason"),
            }
            history.append(formatted_trade)
            with open(HISTORY_PATH, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Trade history write error: {e}")
