"""Tick compiler: alias-tolerant WS parsing, tick-rule footprints, L2 depth,
and dual bar books (3m + 15m) fed from the same ticks.
Bar books are keyed by SYMBOL via set_symbol_map(); market_depth & footprints
stay token-keyed (main translates)."""
from __future__ import annotations
import threading
from collections import defaultdict
from datetime import datetime
from common.logger import get_logger
from stock_order_flow.bar_book import ThreeMinuteBarBook, BarBook

logger = get_logger("tick_compiler")


class TickCompiler:
    def __init__(self, broker):
        self.broker = broker
        self.footprints = defaultdict(list)
        self.market_depth = {}
        self.lock = threading.Lock()
        self.bar_book = ThreeMinuteBarBook()
        self.bar_book_15 = BarBook(15)
        self._keys_logged = False
        self._t2s = {}   # token -> symbol map (set by main)

    def set_symbol_map(self, token_to_symbol: dict):
        self._t2s = {str(k): str(v) for k, v in token_to_symbol.items()}

    def on_message(self, message: dict):
        try:
            if not self._keys_logged:
                logger.info(f"tick keys sample: {sorted(message.keys())}")
                self._keys_logged = True

            token = str(message.get("token", "") or "")
            if not token:
                return

            lp = float(message.get("last_traded_price") or message.get("lp") or 0) or 0.0
            new_vol = float(message.get("volume_trade_for_the_day") or message.get("v") or 0) or 0.0
            bp1 = float(message.get("bid") or message.get("bp1") or 0) or 0.0
            sp1 = float(message.get("ask") or message.get("sp1") or 0) or 0.0
            ts = datetime.now()

            with self.lock:
                prev = self.market_depth.get(token, {"lp": 0.0, "v": 0.0})
                old_vol = float(prev.get("v", 0.0))
                self.market_depth[token] = {"lp": lp, "v": new_vol, "bp1": bp1, "sp1": sp1}
            vol_delta = (new_vol - old_vol) if (old_vol > 0 and new_vol > old_vol) else 0.0

            # L2 depth (5-level sums, bv/sv fallback)
            l2_bid = 0.0
            l2_ask = 0.0
            for i in range(1, 6):
                l2_bid += float(message.get(f"bq{i}") or 0) or 0.0
                l2_ask += float(message.get(f"sq{i}") or 0) or 0.0
            if l2_bid <= 0:
                l2_bid = float(message.get("bv") or 0) or 0.0
            if l2_ask <= 0:
                l2_ask = float(message.get("sv") or 0) or 0.0

            # Tick-rule classification
            trade_type = None
            if lp > 0:
                if sp1 > 0 and lp >= sp1:
                    trade_type = "BUY"
                elif bp1 > 0 and lp <= bp1:
                    trade_type = "SELL"

            # Feed BOTH bar books from the same tick (SYMBOL-keyed)
            if lp > 0:
                key = self._t2s.get(token, token)
                self.bar_book.on_tick(key, lp, ts, volume_delta=vol_delta)
                self.bar_book_15.on_tick(key, lp, ts, volume_delta=vol_delta)

            if trade_type and vol_delta > 0:
                with self.lock:
                    self.footprints[token].append({
                        "price": lp, "volume": vol_delta, "type": trade_type,
                        "l2_bid": l2_bid, "l2_ask": l2_ask,
                    })
        except Exception as e:
            logger.debug(f"on_message error: {e}")

    def get_compiled_footprints(self) -> dict:
        with self.lock:
            out = {}
            for token, trades in self.footprints.items():
                total_buy = 0.0
                total_sell = 0.0
                l2_bid_sum = 0.0
                l2_ask_sum = 0.0
                n = 0
                for t in trades:
                    if t["type"] == "BUY":
                        total_buy += t["volume"]
                    else:
                        total_sell += t["volume"]
                    l2_bid_sum += t["l2_bid"]
                    l2_ask_sum += t["l2_ask"]
                    n += 1
                out[token] = {
                    "total_buy_vol": total_buy,
                    "total_sell_vol": total_sell,
                    "delta": total_buy - total_sell,
                    "avg_l2_bid": (l2_bid_sum / n) if n else 0.0,
                    "avg_l2_ask": (l2_ask_sum / n) if n else 0.0,
                }
            return out

    def reset_for_new_candle(self):
        with self.lock:
            self.footprints.clear()

    def subscribe(self, tokens: list, exchange: str = "NSE"):
        mdp = None
        for name in ("market_data", "mdp", "market_data_provider", "_mdp"):
            obj = getattr(self.broker, name, None)
            if obj is not None and (hasattr(obj, "add_tick_listener") or hasattr(obj, "subscribe")):
                mdp = obj
                break
        if mdp is None and (hasattr(self.broker, "add_tick_listener") or hasattr(self.broker, "subscribe")):
            mdp = self.broker
        if mdp is None:
            logger.error("🚨 TICK WIRING FAILED: no add_tick_listener/subscribe found on broker chain!")
            return
        if hasattr(mdp, "add_tick_listener"):
            mdp.add_tick_listener(self.on_message)
        if hasattr(mdp, "subscribe"):
            mdp.subscribe(tokens, exchange, feed_type="t")
            mdp.subscribe(tokens, exchange, feed_type="d")
        logger.info(f"Subscribed {len(tokens)} equities on {exchange} (touchline+depth) for 3m+15m WS candles + footprint.")