"""Tick compiler: alias-tolerant WS parsing, tick-rule footprints,
end-weighted + persistence-aware L2 depth, dual bar books (3m + 15m)
SYMBOL-keyed via set_symbol_map. market_depth preserves last good values."""
from __future__ import annotations
import threading
from collections import defaultdict
from datetime import datetime
from common.logger import get_logger
from stock_order_flow.bar_book import ThreeMinuteBarBook, BarBook

logger = get_logger("tick_compiler")

# Distance-weighting for L2: best level full weight, decaying outward.
_LEVEL_WEIGHTS = (1.0, 0.5, 1.0 / 3.0, 0.25, 0.2)
# Wall considered "held/replenished" if it stays within 2% of previous tick.
_PERSIST_HOLD = 0.98


class TickCompiler:
    def __init__(self, broker):
        self.broker = broker
        self.footprints = defaultdict(list)
        self.market_depth = {}
        self.lock = threading.Lock()
        self.bar_book = ThreeMinuteBarBook()
        self.bar_book_15 = BarBook(15)
        self._keys_logged = False
        self._t2s = {}
        self._prev_l2w = {}  # token -> (bid_weighted, ask_weighted) for persistence

    def set_symbol_map(self, token_to_symbol: dict):
        self._t2s = {str(k): str(v) for k, v in token_to_symbol.items()}

    def _as_float(self, message: dict, *keys, default=0.0):
        for k in keys:
            v = message.get(k, None)
            if v not in (None, ""):
                try:
                    return float(v)
                except Exception:
                    pass
        return float(default)

    def on_message(self, message: dict):
        try:
            if not self._keys_logged:
                logger.info(f"tick keys sample: {sorted(list(message.keys()))[:20]}")
                self._keys_logged = True
            token = str(message.get("token", "") or "")
            if not token:
                return
            lp = self._as_float(message, "last_traded_price", "lp", "ltp", default=0.0)
            new_vol = self._as_float(message, "volume_trade_for_the_day", "v", default=0.0)
            bp1 = self._as_float(message, "bid", "bp1", default=0.0)
            sp1 = self._as_float(message, "ask", "sp1", default=0.0)
            ts = datetime.now()

            with self.lock:
                prev = self.market_depth.get(token, {"v": 0.0, "lp": 0.0, "bp1": 0.0, "sp1": 0.0})
                old_vol = float(prev.get("v", 0.0))
                vol_delta = new_vol - old_vol if old_vol > 0 and new_vol >= old_vol else 0.0
                # Preserve last good values; depth-only msgs may carry zeros
                self.market_depth[token] = {
                    "lp": lp if lp > 0 else prev.get("lp", 0.0),
                    "v": new_vol if new_vol > 0 else prev.get("v", 0.0),
                    "bp1": bp1 if bp1 > 0 else prev.get("bp1", 0.0),
                    "sp1": sp1 if sp1 > 0 else prev.get("sp1", 0.0),
                }

            # L2 depth: simple sums (legacy) + end-weighted sums (new)
            l2_bid = 0.0
            l2_ask = 0.0
            l2_bid_w = 0.0
            l2_ask_w = 0.0
            have_levels = False
            for i in range(1, 6):
                bq = self._as_float(message, f"bq{i}", default=0.0)
                sq = self._as_float(message, f"sq{i}", default=0.0)
                l2_bid += bq
                l2_ask += sq
                wgt = _LEVEL_WEIGHTS[i - 1]
                l2_bid_w += bq * wgt
                l2_ask_w += sq * wgt
                if bq > 0 or sq > 0:
                    have_levels = True
            if not have_levels or (l2_bid <= 0 and l2_ask <= 0):
                l2_bid = self._as_float(message, "bv", default=0.0)
                l2_ask = self._as_float(message, "sv", default=0.0)
                l2_bid_w = l2_bid
                l2_ask_w = l2_ask

            # Persistence: is each side's wall holding/replenishing tick-over-tick?
            with self.lock:
                prevw = self._prev_l2w.get(token)
                if prevw is not None and (prevw[0] > 0 or prevw[1] > 0):
                    bid_persist = 1.0 if l2_bid_w >= prevw[0] * _PERSIST_HOLD else 0.0
                    ask_persist = 1.0 if l2_ask_w >= prevw[1] * _PERSIST_HOLD else 0.0
                else:
                    bid_persist = 0.5
                    ask_persist = 0.5
                self._prev_l2w[token] = (l2_bid_w, l2_ask_w)

            # Tick-rule classification
            trade_type = None
            if lp > 0:
                if sp1 > 0 and lp >= sp1:
                    trade_type = "BUY"
                elif bp1 > 0 and lp <= bp1:
                    trade_type = "SELL"

            key = self._t2s.get(token, token)
            if lp > 0:
                self.bar_book.on_tick(key, lp, ts, volume_delta=vol_delta)
                self.bar_book_15.on_tick(key, lp, ts, volume_delta=vol_delta)

            if trade_type and vol_delta > 0:
                with self.lock:
                    self.footprints[token].append({
                        "price": lp, "volume": vol_delta, "type": trade_type,
                        "l2_bid": l2_bid, "l2_ask": l2_ask,
                        "l2_bid_w": l2_bid_w, "l2_ask_w": l2_ask_w,
                        "bid_persist": bid_persist, "ask_persist": ask_persist,
                        "ts": ts.isoformat(),
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
                ew_bid_sum = 0.0
                ew_ask_sum = 0.0
                bp_sum = 0.0
                ap_sum = 0.0
                n = 0
                for t in trades:
                    if t["type"] == "BUY":
                        total_buy += float(t["volume"])
                    else:
                        total_sell += float(t["volume"])
                    l2_bid_sum += float(t["l2_bid"])
                    l2_ask_sum += float(t["l2_ask"])
                    ew_bid_sum += float(t["l2_bid_w"])
                    ew_ask_sum += float(t["l2_ask_w"])
                    bp_sum += float(t["bid_persist"])
                    ap_sum += float(t["ask_persist"])
                    n += 1
                out[token] = {
                    "total_buy_vol": total_buy,
                    "total_sell_vol": total_sell,
                    "delta": total_buy - total_sell,
                    "avg_l2_bid": l2_bid_sum / n if n else 0.0,
                    "avg_l2_ask": l2_ask_sum / n if n else 0.0,
                    "ew_l2_bid": ew_bid_sum / n if n else 0.0,
                    "ew_l2_ask": ew_ask_sum / n if n else 0.0,
                    "bid_persistence": bp_sum / n if n else 0.5,
                    "ask_persistence": ap_sum / n if n else 0.5,
                }
            return out

    def get_market_depth_snapshot(self) -> dict:
        with self.lock:
            return {k: dict(v) for k, v in self.market_depth.items()}

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
            logger.error("TICK WIRING FAILED: no add_tick_listener/subscribe found on broker chain!")
            return
        if hasattr(mdp, "add_tick_listener"):
            mdp.add_tick_listener(self.on_message)
        if hasattr(mdp, "subscribe"):
            try:
                mdp.subscribe(tokens, exchange, feed_type="t")
            except TypeError:
                mdp.subscribe(tokens, exchange, "t")
            try:
                mdp.subscribe(tokens, exchange, feed_type="d")
            except TypeError:
                mdp.subscribe(tokens, exchange, "d")
        logger.info(f"Subscribed {len(tokens)} equities on {exchange} (touchline+depth) for 3m+15m WS candles + footprints.")