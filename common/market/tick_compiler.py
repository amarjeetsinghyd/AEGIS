"""Tick compiler: alias-tolerant WS parsing, stateful L1-L5 depth,
dual bar books (3m + 15m)
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
from common.market.bar_book import ThreeMinuteBarBook, BarBook

logger = get_logger("tick_compiler")

class ParsedTick:
    """Zero-allocation parsed tick for hot path. Reused per symbol."""
    __slots__ = ('tok', 'lp', 'vol', 'bp', 'bq', 'sp', 'sq', 'ts', 'oi', '_iceberg_processed')
    def __init__(self):
        self.tok = ""
        self.lp = 0.0
        self.vol = 0.0
        self.bp = [0.0]*5
        self.bq = [0]*5
        self.sp = [0.0]*5
        self.sq = [0]*5
        self.ts = 0.0
        self.oi = 0.0
        self._iceberg_processed = False


class TickCompiler:
    def __init__(self, broker):
        self.broker = broker
        self.market_depth = {}
        self.lock = threading.Lock()
        self.bar_book = ThreeMinuteBarBook()
        self._keys_logged = False
        self._t2s = {}
        self._tick_pool = {}  # sym -> ParsedTick (reused)
        self.engine = None  # Set via set_engine()
        self._book_state = {}  # token -> dict holding stateful merged L1-L5 order book and last good lp/vol
        # ─── MN-4: WebSocket reconnection bookkeeping ───
        # Reconnect is listener-owned (see ShoonyaMarketDataProvider.add_close_listener).
        # The backoff thread is OFF the hot path — on_message is never blocked.
        # Weekend/holiday WS drops are expected (AGENTS.md SHOONYA) and skipped via
        # is_trading_day before any re-subscribe attempt.
        # Exchange-aware: tokens are tracked per-exchange so BSE spot (1) and
        # BFO options are never re-subscribed as NSE on reconnect.
        self._subscribed_tokens = {}
        self._subscribed_exchange = "NSE"
        self._wired_listeners = False
        self._reconnect_lock = threading.Lock()
        self._reconnecting = False
        self._stats_window_start = time.time()

    def set_symbol_map(self, token_to_symbol: dict):
        # Refresh-safe: merges new tokens (09:15 universe expansion) without
        # dropping pool entries. Never shrinks pool to avoid hot-path churn.
        merged = {str(k): str(v) for k, v in token_to_symbol.items()}
        self._t2s.update(merged)
        # Initialize tick pool for zero-copy path
        for sym in merged.values():
            if sym not in self._tick_pool:
                self._tick_pool[sym] = ParsedTick()

    @property
    def token_to_symbol(self):
        return self._t2s

    def set_engine(self, engine):
        """Set the engine for zero-copy tick dispatch."""
        self.engine = engine

    def set_universe_manager(self, universe_manager):
        """Set the StockUniverseManager for Top 15 equities coordination."""
        self.universe_manager = universe_manager

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
                # Preserve last good values; depth-only msgs may carry zeros. Monotonic volume prevents regression.
                self.market_depth[token] = {
                    "lp": lp if lp > 0 else prev.get("lp", 0.0),
                    "v": new_vol if new_vol >= old_vol else old_vol,
                    "bp1": bp1 if bp1 > 0 else prev.get("bp1", 0.0),
                    "sp1": sp1 if sp1 > 0 else prev.get("sp1", 0.0),
                    "ts": time.time()
                }

            key = self._t2s.get(token, token)
            if lp > 0:
                self.bar_book.on_tick(key, lp, ts, volume_delta=vol_delta)

            # ── STATEFUL MERGED BOOK UPDATE (ELIMINATES PARTIAL-TICK ZERO-WIPING) ──
            if token not in self._book_state:
                self._book_state[token] = {
                    "lp": 0.0, "vol": 0.0,
                    "bp": [0.0] * 5, "bq": [0] * 5,
                    "sp": [0.0] * 5, "sq": [0] * 5,
                }
            st = self._book_state[token]

            # 1. Update last traded price and cumulative volume only when present and positive
            if lp > 0:
                st["lp"] = lp
            if new_vol > 0 and new_vol >= st["vol"]:
                st["vol"] = new_vol

            # 2. Update order book levels only when explicitly present in message
            # Touchline (L1) or Depth (L1-L5) updates merge seamlessly without zero-wiping existing levels
            for i in range(1, 6):
                raw_bp = message.get(f"bp{i}")
                if raw_bp is not None and str(raw_bp).strip() != "":
                    try:
                        v_bp = float(raw_bp)
                        if v_bp >= 0: st["bp"][i - 1] = v_bp
                    except (ValueError, TypeError):
                        pass
                raw_bq = message.get(f"bq{i}")
                if raw_bq is not None and str(raw_bq).strip() != "":
                    try:
                        v_bq = int(float(raw_bq))
                        if v_bq >= 0: st["bq"][i - 1] = v_bq
                    except (ValueError, TypeError):
                        pass
                raw_sp = message.get(f"sp{i}")
                if raw_sp is not None and str(raw_sp).strip() != "":
                    try:
                        v_sp = float(raw_sp)
                        if v_sp >= 0: st["sp"][i - 1] = v_sp
                    except (ValueError, TypeError):
                        pass
                raw_sq = message.get(f"sq{i}")
                if raw_sq is not None and str(raw_sq).strip() != "":
                    try:
                        v_sq = int(float(raw_sq))
                        if v_sq >= 0: st["sq"][i - 1] = v_sq
                    except (ValueError, TypeError):
                        pass

            if bp1 > 0:
                st["bp"][0] = bp1
            if sp1 > 0:
                st["sp"][0] = sp1

            # ── UNIFIED ZERO-COPY DISPATCH TO ENGINE (ALL 110 SYMBOLS DIRECT) ──
            # Parse directly into pre-allocated ParsedTick using clean statefully merged book
            if self.engine and key in self._tick_pool:
                tick = self._tick_pool[key]
                tick.tok = token
                # Merged state: use last known good lp and vol so depth-only messages don't drop
                tick.lp = st["lp"] if st["lp"] > 0 else lp
                tick.vol = st["vol"] if st["vol"] > 0 else new_vol
                tick.ts = time.time()
                for i in range(5):
                    tick.bp[i] = st["bp"][i]
                    tick.bq[i] = st["bq"][i]
                    tick.sp[i] = st["sp"][i]
                    tick.sq[i] = st["sq"][i]
                # Non-blocking dispatch - engine processes in same thread
                self.engine.on_tick_parsed(tick)

            # ── DISPATCH TO STOCK UNIVERSE MANAGER (TOP 15 STOCKS) ──
            if hasattr(self, "universe_manager") and self.universe_manager:
                try:
                    bp1_val = self._as_float(message, "bp1", default=0.0)
                    sp1_val = self._as_float(message, "sp1", default=0.0)
                    bq1_val = int(self._as_float(message, "bq1", default=100))
                    sq1_val = int(self._as_float(message, "sq1", default=100))
                    sig = self.universe_manager.on_tick(
                        symbol=key,
                        price=lp,
                        volume=new_vol,
                        bp1=bp1_val,
                        sp1=sp1_val,
                        bq1=bq1_val,
                        sq1=sq1_val,
                        ts_sec=time.time(),
                    )
                    if sig and self.engine and hasattr(self.engine, "signal_queue"):
                        self.engine.signal_queue.put(sig)
                except Exception as e_sig:
                    logger.debug(f"universe_manager tick dispatch error: {e_sig}")
        except Exception as e:
            logger.debug(f"on_message error: {e}")

    def get_market_depth_snapshot(self) -> dict:
        with self.lock:
            return {k: dict(v) for k, v in self.market_depth.items()}

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
        # Exchange-aware accumulate: same token on different exchange stays distinct.
        # Old single-exchange overwrite caused BSE:1 / BFO options to be
        # re-subscribed as NSE on reconnect → Spot 0.
        if not isinstance(self._subscribed_tokens, dict):
            legacy = list(self._subscribed_tokens) if isinstance(self._subscribed_tokens, list) else []
            self._subscribed_tokens = {}
            for t in legacy:
                self._subscribed_tokens[str(t)] = self._subscribed_exchange
        exch = str(exchange or "NSE").upper()
        for t in (tokens or []):
            self._subscribed_tokens[str(t)] = exch
        self._subscribed_exchange = exch
        if not self._wired_listeners:
            if hasattr(mdp, "add_tick_listener"):
                mdp.add_tick_listener(self.on_message)
            # MN-4: register as a WS-close listener so the broker hands us the
            # socket_close_callback without us owning the WS lifecycle. Guarded so
            # brokers without close-listener support silently skip reconnect wiring.
            if hasattr(mdp, "add_close_listener"):
                mdp.add_close_listener(self.on_ws_close)
            self._wired_listeners = True
        if hasattr(mdp, "subscribe"):
            try:
                self._subscribe_feeds(mdp, tokens, exchange)
            except Exception as e_sub:
                logger.warning(f"Subscribe deferred ({exchange} {len(tokens)} tokens): {e_sub}")
        logger.info(f"Subscribed {len(tokens)} equities on {exchange} (touchline+depth) for 3m+15m WS candles.")

    @staticmethod
    def _subscribe_feeds(mdp, tokens, exchange) -> None:
        """Broker-tolerant subscribe: Shoonya (feed_type) and Angel (tokens, exchange)."""
        try:
            mdp.subscribe(tokens, exchange, feed_type="t")
        except TypeError:
            try:
                mdp.subscribe(tokens, exchange, "t")
            except TypeError:
                mdp.subscribe(tokens, exchange)
                return
        try:
            mdp.subscribe(tokens, exchange, feed_type="d")
        except TypeError:
            try:
                mdp.subscribe(tokens, exchange, "d")
            except TypeError:
                pass

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
                    from common.market.market_calendar import is_exchange_trading_day, is_bot_active_hours
                    from datetime import datetime as _dt
                    now_dt = _dt.now()
                    if not is_exchange_trading_day(now_dt.date()) or not is_bot_active_hours(now_dt):
                        logger.info("WS closed outside active market hours (08:55-15:17) — skipping reconnect.")
                        return
                except Exception:
                    # If the calendar import fails we err on the side of NOT
                    # hammering the broker on a weekend; bail out.
                    logger.warning("is_exchange_trading_day / is_bot_active_hours unavailable — skipping WS reconnect re-issue.")
                    return
                mdp = self._resolve_mdp()
                if mdp is None:
                    logger.warning("Reconnect cannot re-issue subscribe: no MDP resolved.")
                    return
                # Group by exchange so BSE spot and BFO options keep their segment.
                # Snapshot tokens under list() to eliminate RuntimeError on concurrent subscribe()
                grouped = {}
                if isinstance(self._subscribed_tokens, dict):
                    for tok, exch in list(self._subscribed_tokens.items()):
                        grouped.setdefault(str(exch or "NSE").upper(), []).append(tok)
                else:
                    grouped[str(self._subscribed_exchange or "NSE").upper()] = list(self._subscribed_tokens or [])
                if not grouped or all(len(v) == 0 for v in grouped.values()):
                    logger.info("WS reconnect skipped: no subscribed tokens remembered.")
                    return
                try:
                    if hasattr(mdp, "subscribe"):
                        for exch, toks in grouped.items():
                            if not toks:
                                continue
                            try:
                                TickCompiler._subscribe_feeds(mdp, toks, exch)
                            except Exception as e_sub:
                                logger.warning(f"Reconnect subscribe deferred ({exch}): {e_sub}")
                    total = sum(len(v) for v in grouped.values())
                    logger.info(f"WS reconnect re-subscribed {total} tokens across {len(grouped)} exchanges after {backoff:.1f}s backoff.")
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
        """Legacy stub for 15-min stats (purged from AEGIS v1.1)."""
        return None

    def get_5min_stats(self, symbol: str) -> dict | None:
        return None

    def reset_15min_stats(self):
        pass

    def reset_5min_stats(self):
        pass

    def reset_session(self):
        """Reset session-level state for market open / day rollover.
        Clears merged book state and depth to eliminate yesterday's cumulative volume carry-over."""
        with self.lock:
            self._book_state.clear()
            self.market_depth.clear()
            if hasattr(self.bar_book, "forming"):
                self.bar_book.forming.clear()
            self._stats_window_start = time.time()
        logger.info("[TickCompiler] Session state reset completed (clean slate for day rollover).")

    def stop(self) -> None:
        """Detaches engine and clears active subscription tracking for clean shutdown."""
        self.engine = None
        self._subscribed_tokens = {}
        logger.info("[TickCompiler] Stopped and detached from engine.")