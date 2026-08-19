"""Tick compiler: alias-tolerant WS parsing, tick-rule footprints,
end-weighted + persistence-aware L2 depth, dual bar books (3m + 15m)
SYMBOL-keyed via set_symbol_map. market_depth preserves last good values."""
from __future__ import annotations
import os
import sys

# ─── BOOTSTRAP ───
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import threading
import time
from collections import defaultdict
from datetime import datetime
from common.logger import get_logger
from ARGUS.bar_book import ThreeMinuteBarBook, BarBook

logger = get_logger("tick_compiler")

# Distance-weighting for L2: best level full weight, decaying outward.
_LEVEL_WEIGHTS = (1.0, 0.5, 1.0 / 3.0, 0.25, 0.2)
# Wall considered "held/replenished" if it stays within 2% of previous tick.
_PERSIST_HOLD = 0.98


class ParsedTick:
    """Zero-allocation parsed tick for hot path. Reused per symbol."""
    __slots__ = ('tok', 'lp', 'vol', 'bp', 'bq', 'sp', 'sq', 'ts')
    def __init__(self):
        self.tok = ""
        self.lp = 0.0
        self.vol = 0.0
        self.bp = [0.0]*5
        self.bq = [0]*5
        self.sp = [0.0]*5
        self.sq = [0]*5
        self.ts = 0.0


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
        self._tick_pool = {}  # sym -> ParsedTick (reused)
        self.engine = None  # Set via set_engine()
        # ─── MN-4: WebSocket reconnection bookkeeping ───
        # Reconnect is listener-owned (see ShoonyaMarketDataProvider.add_close_listener).
        # The backoff thread is OFF the hot path — on_message is never blocked.
        # Weekend/holiday WS drops are expected (AGENTS.md SHOONYA) and skipped via
        # is_trading_day before any re-subscribe attempt.
        self._subscribed_tokens = []
        self._subscribed_exchange = "NSE"
        self._reconnect_lock = threading.Lock()
        self._reconnecting = False
        # ─── TIER 2 INTRADAY FILTER (15-min rolling stats) ───
        self._tier2_symbols = set()  # top 30 from 15-min filter
        self._engine_symbols = set()  # top 15 for adaptive engine
        self._stats_15min = defaultdict(lambda: {
            'ticks': 0, 'volume': 0, 'trade_value': 0.0,
            'spread_sum': 0.0, 'spread_cnt': 0,
            'mid_prices': [], 'oib_sum': 0.0, 'oib_cnt': 0,
        })
        self._stats_window_start = time.time()

    def set_symbol_map(self, token_to_symbol: dict):
        self._t2s = {str(k): str(v) for k, v in token_to_symbol.items()}
        # Initialize tick pool for zero-copy path
        for sym in self._t2s.values():
            if sym not in self._tick_pool:
                self._tick_pool[sym] = ParsedTick()

    def set_engine(self, engine):
        """Set the engine for zero-copy tick dispatch."""
        self.engine = engine

    @property
    def token_to_symbol(self):
        """Expose the token-to-symbol mapping for external access."""
        return self._t2s

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

            import time
            with self.lock:
                prev = self.market_depth.get(token, {"v": 0.0, "lp": 0.0, "bp1": 0.0, "sp1": 0.0})
                old_vol = float(prev.get("v", 0.0))
                vol_delta = new_vol - old_vol if old_vol > 0 and new_vol >= old_vol else 0.0
                # Preserve last good values; depth-only msgs may carry zeros. Atomically swap dict.
                self.market_depth[token] = {
                    "lp": lp if lp > 0 else prev.get("lp", 0.0),
                    "v": new_vol if new_vol > 0 else prev.get("v", 0.0),
                    "bp1": bp1 if bp1 > 0 else prev.get("bp1", 0.0),
                    "sp1": sp1 if sp1 > 0 else prev.get("sp1", 0.0),
                    "ts": time.time()
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

            # ── TIER 2 INTRADAY FILTER (15-min rolling stats) ───
            # Collect per-symbol stats for 15-min filter (only if symbol in tier2 or engine set)
            # Only compute if we have meaningful depth
            if bp1 > 0 and sp1 > 0:
                st = self._stats_15min[key]
                st['ticks'] += 1
                if vol_delta > 0:
                    st['volume'] += vol_delta
                    st['trade_value'] += vol_delta * lp
                st['spread_sum'] += (sp1 - bp1)
                st['spread_cnt'] += 1
                mid = (bp1 + sp1) * 0.5
                st['mid_prices'].append(mid)
                if len(st['mid_prices']) > 500:
                    st['mid_prices'].pop(0)
                # OIB at L1
                bq1 = self._as_float(message, "bq1", default=0.0)
                sq1 = self._as_float(message, "sq1", default=0.0)
                if bq1 + sq1 > 0:
                    st['oib_sum'] += (bq1 - sq1) / (bq1 + sq1)
                    st['oib_cnt'] += 1

            # ── TIER 2 GATE: Skip symbols not in top 30 ───
            if self._tier2_symbols and key not in self._tier2_symbols:
                return  # Skip all parsing/math for bottom 80 symbols

            # ── ENGINE GATE: Skip symbols not in top 15 for adaptive engine ───
            if self._engine_symbols and self.engine and key not in self._engine_symbols:
                # Still allow footprints/bar_books but skip engine dispatch
                pass
            else:
                # ── ZERO-COPY DISPATCH TO ENGINE ──
                # Parse directly into pre-allocated ParsedTick, bypass dict creation
                if self.engine and key in self._tick_pool:
                    tick = self._tick_pool[key]
                    tick.tok = token
                    tick.lp = lp
                    tick.vol = new_vol
                    tick.ts = time.time()
                    for i in range(5):
                        tick.bp[i] = self._as_float(message, f"bp{i+1}", default=0.0)
                        tick.bq[i] = int(self._as_float(message, f"bq{i+1}", default=0.0))
                        tick.sp[i] = self._as_float(message, f"sp{i+1}", default=0.0)
                        tick.sq[i] = int(self._as_float(message, f"sq{i+1}", default=0.0))
                    # Non-blocking dispatch - engine processes in same thread
                    self.engine.on_tick_parsed(tick)
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
        # MN-4: remember what we subscribed so a mid-session WS close can re-issue it.
        self._subscribed_tokens = list([str(t) for t in tokens])
        self._subscribed_exchange = exchange
        if hasattr(mdp, "add_tick_listener"):
            mdp.add_tick_listener(self.on_message)
        # MN-4: register as a WS-close listener so the broker hands us the
        # socket_close_callback without us owning the WS lifecycle. Guarded so
        # brokers without close-listener support silently skip reconnect wiring.
        if hasattr(mdp, "add_close_listener"):
            mdp.add_close_listener(self.on_ws_close)
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

    # ─── MN-4: WebSocket reconnection with exponential backoff ───
    # on_ws_close is called by the broker's socket_close_callback (NorenApiPy).
    # We do NOT immediately re-subscribe in-line (the WS object is gone); instead
    # a short background thread re-subscribes after backoff. Only ONE reconnect
    # attempt is in flight at a time (_reconnecting single-flight). Weekend and
    # NSE-holiday closes are expected (AGENTS.md SHOONYA) and skipped — no
    # backoff burn, no log spam on a Saturday.
    def on_ws_close(self):
        with self._reconnect_lock:
            if self._reconnecting:
                return  # already retrying — let the in-flight loop handle this close
            self._reconnecting = True
        threading.Thread(target=self._reconnect_loop, name="tick-compiler-reconnect", daemon=True).start()

    def _reconnect_loop(self):
        """Exponential-backoff re-subscribe after a mid-session WS drop.
        Trading-day-gated (weekend/holiday closes are skipped). Stdlib-only."""
        try:
            backoff = 1.0
            while True:
                time.sleep(backoff)
                try:
                    from common.market.market_calendar import is_trading_day
                    from datetime import datetime as _dt
                    if not is_trading_day(_dt.now().date()):
                        logger.info("WS closed on a non-trading day (weekend/holiday) — no reconnect attempt.")
                        return
                except Exception:
                    # If the calendar import fails we err on the side of NOT
                    # hammering the broker on a weekend; bail out.
                    logger.warning("is_trading_day unavailable — skipping WS reconnect re-issue.")
                    return
                mdp = self._resolve_mdp()
                if mdp is None:
                    logger.warning("Reconnect cannot re-issue subscribe: no MDP resolved.")
                    return
                tokens = self._subscribed_tokens
                exchange = self._subscribed_exchange
                try:
                    if hasattr(mdp, "subscribe"):
                        try:
                            mdp.subscribe(tokens, exchange, feed_type="t")
                        except TypeError:
                            mdp.subscribe(tokens, exchange, "t")
                        try:
                            mdp.subscribe(tokens, exchange, feed_type="d")
                        except TypeError:
                            mdp.subscribe(tokens, exchange, "d")
                    logger.info(f"WS reconnect re-subscribed {len(tokens)} tokens on {exchange} after {backoff:.1f}s backoff.")
                    return  # re-issue succeeded — close listener will fire again on the next drop
                except Exception as exc:
                    logger.warning(f"WS reconnect re-subscribe failed ({exc}); backing off {min(backoff*2, 30.0):.1f}s.")
                    backoff = min(backoff * 2, 30.0)  # cap: 1, 2, 4, 8, 16, 30s ...
        finally:
            with self._reconnect_lock:
                self._reconnecting = False

    def _resolve_mdp(self):
        for name in ("market_data", "mdp", "market_data_provider", "_mdp"):
            obj = getattr(self.broker, name, None)
            if obj is not None and (hasattr(obj, "add_tick_listener") or hasattr(obj, "subscribe")):
                return obj
        if hasattr(self.broker, "add_tick_listener") or hasattr(self.broker, "subscribe"):
            return self.broker
        return None

    # ─── TIER 2 INTRADAY FILTER API ───
    def set_tier2_symbols(self, symbols: set):
        """Set top 30 symbols for Tier 2 pre-filter (15-min rolling filter)."""
        self._tier2_symbols = set(symbols) if symbols else set()
        logger.info(f"[TIER2] Updated Tier 2 symbols: {len(self._tier2_symbols)} symbols")

    def set_engine_symbols(self, symbols: set):
        """Set top 15 symbols for adaptive engine dispatch."""
        self._engine_symbols = set(symbols) if symbols else set()
        logger.info(f"[TIER2] Updated Engine symbols: {len(self._engine_symbols)} symbols")

    def get_15min_stats(self, sym: str) -> dict | None:
        """Get 15-min rolling stats for a symbol."""
        st = self._stats_15min.get(sym)
        if not st or st['ticks'] == 0:
            return None
        elapsed = time.time() - self._stats_window_start
        if elapsed <= 0:
            elapsed = 900.0  # 15 min
        ticks_per_min = st['ticks'] / (elapsed / 60.0)
        vol_per_min = st['volume'] / (elapsed / 60.0)
        avg_spread_bps = (st['spread_sum'] / max(st['spread_cnt'], 1)) / max(st.get('mid', 1), 1) * 10000 if st['spread_cnt'] > 0 else 0
        mid_prices = st['mid_prices']
        microprice_vol = 0.0
        ret_15m = 0.0
        last_mid = mid_prices[-1] if mid_prices else 0.0
        if len(mid_prices) > 10:
            import numpy as np
            microprice_vol = float(np.std(mid_prices) / max(np.mean(mid_prices), 1) * 100)
            if mid_prices[0] > 0:
                ret_15m = float((mid_prices[-1] - mid_prices[0]) / mid_prices[0] * 100.0)
        
        # 15-minute VWAP & Distance from VWAP
        vwap_15m = st['trade_value'] / max(st['volume'], 1.0) if st['volume'] > 0 else last_mid
        vwap_dist = float((last_mid - vwap_15m) / vwap_15m * 100.0) if vwap_15m > 0 and last_mid > 0 else 0.0
        
        oib = st['oib_sum'] / max(st['oib_cnt'], 1) if st['oib_cnt'] > 0 else 0.0
        return {
            'ticks_per_min': ticks_per_min,
            'vol_per_min': vol_per_min,
            'avg_spread_bps': avg_spread_bps,
            'trade_value_per_min': trade_value_per_min,
            'microprice_vol': microprice_vol,
            'ret_15m': ret_15m,
            'vwap_dist': vwap_dist,
            'oib': oib,
        }

    def reset_15min_stats(self):
        """Reset 15-min stats for new window."""
        self._stats_15min.clear()
        self._stats_window_start = time.time()
        logger.info("[TIER2] Reset 15-min stats for new window")