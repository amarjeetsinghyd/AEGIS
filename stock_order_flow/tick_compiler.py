"""Compile L1 footprint + L2 depth + WS 3m OHLC from Shoonya ticks."""
from __future__ import annotations

import threading
from collections import defaultdict
from datetime import datetime

from common.logger import get_logger
from stock_order_flow.bar_book import ThreeMinuteBarBook

logger = get_logger("tick_compiler")


class TickCompiler:
    def __init__(self, broker):
        self.broker = broker
        self.footprints = defaultdict(list)
        self.market_depth = {}
        self.lock = threading.Lock()
        self._tokens = []
        self.bar_book = ThreeMinuteBarBook()

    def reset_for_new_candle(self):
        """Clear footprint matrix after a completed 3m boundary (OHLC book kept)."""
        with self.lock:
            self.footprints.clear()
            logger.info("Footprint matrix reset for new 3-minute candle.")

    def on_message(self, message):
        """Process normalized WS ticks (touchline and/or depth)."""
        if not isinstance(message, dict) or "token" not in message:
            return

        token = str(message["token"])
        now = datetime.now()
        with self.lock:
            if token not in self.market_depth:
                self.market_depth[token] = {"lp": 0.0, "v": 0, "bp1": 0.0, "sp1": 0.0}

            depth = self.market_depth[token]

            if "last_traded_price" in message:
                try:
                    lp = float(message["last_traded_price"] or 0)
                    if lp > 0:
                        depth["lp"] = lp
                except (TypeError, ValueError):
                    pass
            if "bid" in message:
                try:
                    depth["bp1"] = float(message["bid"] or 0)
                except (TypeError, ValueError):
                    pass
            if "ask" in message:
                try:
                    depth["sp1"] = float(message["ask"] or 0)
                except (TypeError, ValueError):
                    pass
            if "bp1" in message:
                try:
                    depth["bp1"] = float(message["bp1"] or 0)
                except (TypeError, ValueError):
                    pass
            if "sp1" in message:
                try:
                    depth["sp1"] = float(message["sp1"] or 0)
                except (TypeError, ValueError):
                    pass

            vol_delta = 0.0
            if "volume_trade_for_the_day" in message:
                try:
                    new_vol = int(float(message.get("volume_trade_for_the_day") or 0))
                except (TypeError, ValueError):
                    new_vol = 0
                old_vol = int(depth.get("v") or 0)

                if old_vol > 0 and new_vol > old_vol:
                    vol_delta = float(new_vol - old_vol)
                    lp = depth["lp"]
                    bp1 = depth["bp1"]
                    sp1 = depth["sp1"]

                    trade_type = "NEUTRAL"
                    if lp >= sp1 and sp1 > 0:
                        trade_type = "BUY"
                    elif lp <= bp1 and bp1 > 0:
                        trade_type = "SELL"

                    l2_bid_qty = 0
                    l2_ask_qty = 0
                    for i in range(1, 6):
                        try:
                            l2_bid_qty += int(float(message.get(f"bq{i}", 0) or 0))
                            l2_ask_qty += int(float(message.get(f"sq{i}", 0) or 0))
                        except (TypeError, ValueError):
                            pass

                    if trade_type != "NEUTRAL" and vol_delta > 0:
                        self.footprints[token].append(
                            {
                                "price": lp,
                                "volume": vol_delta,
                                "type": trade_type,
                                "l2_bid": l2_bid_qty,
                                "l2_ask": l2_ask_qty,
                            }
                        )

                if new_vol > 0:
                    depth["v"] = new_vol

            # Canonical 3m OHLC from WS (even on depth-only ticks with LTP)
            if depth["lp"] > 0:
                self.bar_book.on_tick(token, depth["lp"], ts=now, volume_delta=vol_delta)

    def get_compiled_footprints(self):
        with self.lock:
            compiled = {}
            for token, trades in self.footprints.items():
                levels = defaultdict(lambda: {"buy": 0, "sell": 0})
                total_buy = 0
                total_sell = 0
                total_l2_bid = 0
                total_l2_ask = 0
                tick_count = len(trades)

                for trade in trades:
                    price = trade["price"]
                    vol = trade["volume"]
                    total_l2_bid += trade.get("l2_bid", 0)
                    total_l2_ask += trade.get("l2_ask", 0)
                    if trade["type"] == "BUY":
                        levels[price]["buy"] += vol
                        total_buy += vol
                    else:
                        levels[price]["sell"] += vol
                        total_sell += vol

                compiled[token] = {
                    "levels": dict(levels),
                    "total_buy_vol": total_buy,
                    "total_sell_vol": total_sell,
                    "delta": total_buy - total_sell,
                    "avg_l2_bid": total_l2_bid / tick_count if tick_count else 0,
                    "avg_l2_ask": total_l2_ask / tick_count if tick_count else 0,
                }
            return compiled

    def subscribe(self, tokens, exchange="NSE"):
        self._tokens = [str(t) for t in tokens]
        mdp = self.broker.market_data_provider
        try:
            mdp.add_tick_listener(self.on_message)
            mdp._ensure_ws_started()
            mdp.subscribe(self._tokens, exchange, feed_type="t")
            mdp.subscribe(self._tokens, exchange, feed_type="d")
            logger.info(
                f"Subscribed {len(self._tokens)} equities on {exchange} "
                f"(touchline+depth) for 3m WS candles + footprint."
            )
        except Exception as e:
            logger.error(f"WebSocket subscribe failed: {e}")
