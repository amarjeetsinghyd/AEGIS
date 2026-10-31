"""AEGIS feed — Shoonya WS tick parsing (alias-tolerant, stateful L1-L5 merge), session bar books (3m/15m),
rolling per-symbol microstructure stats, and historical/instrument data fetching.

Consolidated from tick_compiler.py + bar_book.py + data_fetcher.py. Hot path (TickCompiler.on_message, BarBook.on_tick)
is allocation-free in steady state: constant key tuples, in-place market_depth update, deque(maxlen) stats window,
integer session/bucket math, slice-assigned pooled ParsedTick, a single clock read per message.
"""
from __future__ import annotations
import threading
import pandas as pd
import os
import time
import json
import requests
import collections
from collections import defaultdict, deque
from datetime import datetime, time as dtime, timedelta, date
from common.logger import get_logger
from AEGIS.config.settings import BROKER

# ── constant key tuples (hoisted out of the per-tick path) ──
_LP_KEYS = ("last_traded_price", "lp", "ltp")
_VOL_KEYS = ("volume_trade_for_the_day", "v")
_BID_KEYS = ("bid", "bp1")
_ASK_KEYS = ("ask", "sp1")
_BQ1_KEYS = ("bq1",)
_SQ1_KEYS = ("sq1",)
_LEVEL_KEYS = tuple((f"bp{i}", f"bq{i}", f"sp{i}", f"sq{i}") for i in range(1, 6))


def _num(message: dict, keys) -> float:
    """== TickCompiler._as_float(message, *keys, default=0.0), without the varargs tuple."""
    for k in keys:
        v = message.get(k, None)
        if v not in (None, ""):
            try:
                return float(v)
            except Exception:
                pass
    return 0.0


_log_bars = get_logger("sofbarbook")

SESSION_START = dtime(9, 15)

SESSION_END = dtime(15, 15)

def floor_3m(ts: datetime) -> datetime:
    m = ts.minute - (ts.minute % 3)
    return ts.replace(minute=m, second=0, microsecond=0)

class BarBook:
    def __init__(self, minutes: int = 3):
        self.minutes = minutes
        self.completed = defaultdict(list)
        self.forming = {}
        self._fk = {}            # token -> integer bucket key of the forming bar (perf index; not part of the bar dicts)
        self.lock = threading.Lock()

    def floor_bar(self, ts: datetime) -> datetime:
        m = ts.minute - (ts.minute % self.minutes)
        return ts.replace(minute=m, second=0, microsecond=0)

    @staticmethod
    def in_session(ts: datetime) -> bool:
        return SESSION_START <= ts.time() <= SESSION_END

    def seed(self, token: str, df: pd.DataFrame):
        if df is None or len(df) == 0:
            return
        with self.lock:
            existing = {b["timestamp"] for b in self.completed[token]}
            for r in df.itertuples(index=False):
                ts = pd.to_datetime(r.timestamp).to_pydatetime()
                if not self.in_session(ts) or ts in existing:
                    continue
                self.completed[token].append(
                    {
                        "timestamp": ts,
                        "open": float(r.open),
                        "high": float(r.high),
                        "low": float(r.low),
                        "close": float(r.close),
                        "volume": float(getattr(r, "volume", 0.0)),
                    }
                )
                existing.add(ts)
            self.completed[token].sort(key=lambda b: b["timestamp"])
            if len(self.completed[token]) > 2000:
                self.completed[token] = self.completed[token][-2000:]
            _log_bars.info(f"{self.minutes}m BARS: Seeded {len(self.completed[token])} completed bars for key={token}")

    def on_tick(self, token: str, ltp: float, ts: datetime, volume_delta: float = 0.0):
        if ltp <= 0:
            return
        # Hot path: identical to SESSION_START <= ts.time() <= SESSION_END (inclusive, microsecond-exact) but
        # integer-only. 33300 s = 09:15:00, 54900 s = 15:15:00.
        hh, mm = ts.hour, ts.minute
        sod = hh * 3600 + mm * 60 + ts.second
        if sod < 33300 or sod > 54900 or (sod == 54900 and ts.microsecond):
            return
        k = hh * 60 + (mm - mm % self.minutes)          # == floor_bar() identity, without building a datetime per tick
        with self.lock:
            f = self.forming.get(token)
            if f is not None:
                k0 = self._fk.get(token)
                same = (k0 == k) if k0 is not None else (f["timestamp"] == self.floor_bar(ts))
            else:
                same = False
            if not same:
                if f is not None:
                    self.completed[token].append(f)
                    if len(self.completed[token]) > 2000:
                        self.completed[token] = self.completed[token][-2000:]
                f = {
                    "timestamp": self.floor_bar(ts),
                    "open": ltp,
                    "high": ltp,
                    "low": ltp,
                    "close": ltp,
                    "volume": 0.0,
                }
                self.forming[token] = f
                self._fk[token] = k
            else:
                if ltp > f["high"]:
                    f["high"] = ltp
                if ltp < f["low"]:
                    f["low"] = ltp
                f["close"] = ltp
            if volume_delta and volume_delta > 0:
                f["volume"] += volume_delta

    def force_close_forming(self, token: str, now: datetime):
        with self.lock:
            f = self.forming.get(token)
            if f is None:
                return
            if f["timestamp"] < self.floor_bar(now):
                self.completed[token].append(f)
                if len(self.completed[token]) > 2000:
                    self.completed[token] = self.completed[token][-2000:]
                self.forming.pop(token, None)
                self._fk.pop(token, None)

    def completed_df(self, token: str, min_bars: int = 2):
        with self.lock:
            bars = list(self.completed[token])
        if len(bars) < min_bars:
            return None
        df = pd.DataFrame(bars)
        if df.empty:
            return None
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df.sort_values("timestamp").reset_index(drop=True)

class ThreeMinuteBarBook(BarBook):
    def __init__(self):
        super().__init__(3)

def resample_bars(df: pd.DataFrame, minutes: int = 15) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return None
    d = df.copy()
    d["timestamp"] = pd.to_datetime(d["timestamp"])
    d = d.set_index("timestamp")
    r = d.resample(f"{minutes}min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["close"])
    return r.reset_index()

_log_tick = get_logger("tick_compiler")

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
        self.bar_book_15 = BarBook(15)
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
        # ─── TIER 2 INTRADAY FILTER (15-min rolling stats) ───
        self._tier2_symbols = set()  # top 30 from 15-min filter
        self._engine_symbols = set()  # top 15 for adaptive engine
        self._stats_15min = defaultdict(lambda: {
            'ticks': 0, 'volume': 0, 'trade_value': 0.0,
            'spread_sum': 0.0, 'spread_cnt': 0,
            'mid_prices': deque(maxlen=500), 'oib_sum': 0.0, 'oib_cnt': 0,   # deque(maxlen): O(1) vs list.pop(0) O(n) per tick
        })
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
                _log_tick.info(f"tick keys sample: {sorted(list(message.keys()))[:20]}")
                self._keys_logged = True
            token = str(message.get("token", "") or "")
            if not token:
                return
            lp = _num(message, _LP_KEYS)
            new_vol = _num(message, _VOL_KEYS)
            bp1 = _num(message, _BID_KEYS)
            sp1 = _num(message, _ASK_KEYS)
            ts = datetime.now()
            now_t = time.time()                         # ONE clock read per message (was three)

            with self.lock:
                md = self.market_depth.get(token)       # no per-tick default-dict / replacement-dict allocation
                if md is None:
                    p_lp = p_v = p_b = p_s = 0.0
                else:
                    p_lp, p_v, p_b, p_s = md["lp"], md["v"], md["bp1"], md["sp1"]
                old_vol = float(p_v)
                vol_delta = new_vol - old_vol if old_vol > 0 and new_vol >= old_vol else 0.0
                # Preserve last good values; depth-only msgs may carry zeros. Monotonic volume prevents regression.
                n_lp = lp if lp > 0 else p_lp
                n_v = new_vol if new_vol >= old_vol else old_vol
                n_b = bp1 if bp1 > 0 else p_b
                n_s = sp1 if sp1 > 0 else p_s
                if md is None:
                    self.market_depth[token] = {"lp": n_lp, "v": n_v, "bp1": n_b, "sp1": n_s, "ts": now_t}
                else:
                    md["lp"] = n_lp; md["v"] = n_v; md["bp1"] = n_b; md["sp1"] = n_s; md["ts"] = now_t

            key = self._t2s.get(token, token)
            if lp > 0:
                self.bar_book.on_tick(key, lp, ts, volume_delta=vol_delta)
                self.bar_book_15.on_tick(key, lp, ts, volume_delta=vol_delta)

            # ── TIER 2 INTRADAY FILTER / 5-MIN MACRO STATS (per-symbol rolling stats) ───
            if bp1 > 0 and sp1 > 0:
                st = self._stats_15min[key]
                st['ticks'] += 1
                if vol_delta > 0:
                    st['volume'] += vol_delta
                    st['trade_value'] += vol_delta * lp
                st['spread_sum'] += (sp1 - bp1)
                st['spread_cnt'] += 1
                st['mid_prices'].append((bp1 + sp1) * 0.5)
                # OIB at L1
                bq1 = _num(message, _BQ1_KEYS)
                sq1 = _num(message, _SQ1_KEYS)
                if bq1 + sq1 > 0:
                    st['oib_sum'] += (bq1 - sq1) / (bq1 + sq1)
                    st['oib_cnt'] += 1

            # ── STATEFUL MERGED BOOK UPDATE (ELIMINATES PARTIAL-TICK ZERO-WIPING) ──
            st = self._book_state.get(token)
            if st is None:
                st = self._book_state[token] = {
                    "lp": 0.0, "vol": 0.0,
                    "bp": [0.0] * 5, "bq": [0] * 5,
                    "sp": [0.0] * 5, "sq": [0] * 5,
                }
            if lp > 0:
                st["lp"] = lp
            if new_vol > 0 and new_vol >= st["vol"]:
                st["vol"] = new_vol

            # Touchline (L1) or Depth (L1-L5) updates merge without zero-wiping existing levels.
            # Constant key tuples: no per-tick f-string construction (was 20 per message).
            st_bp, st_bq, st_sp, st_sq = st["bp"], st["bq"], st["sp"], st["sq"]
            get = message.get
            for i, (kbp, kbq, ksp, ksq) in enumerate(_LEVEL_KEYS):
                raw = get(kbp)
                if raw is not None and str(raw).strip() != "":
                    try:
                        v = float(raw)
                        if v >= 0: st_bp[i] = v
                    except (ValueError, TypeError):
                        pass
                raw = get(kbq)
                if raw is not None and str(raw).strip() != "":
                    try:
                        v = int(float(raw))
                        if v >= 0: st_bq[i] = v
                    except (ValueError, TypeError):
                        pass
                raw = get(ksp)
                if raw is not None and str(raw).strip() != "":
                    try:
                        v = float(raw)
                        if v >= 0: st_sp[i] = v
                    except (ValueError, TypeError):
                        pass
                raw = get(ksq)
                if raw is not None and str(raw).strip() != "":
                    try:
                        v = int(float(raw))
                        if v >= 0: st_sq[i] = v
                    except (ValueError, TypeError):
                        pass

            if bp1 > 0:
                st_bp[0] = bp1
            if sp1 > 0:
                st_sp[0] = sp1

            # ── UNIFIED ZERO-COPY DISPATCH TO ENGINE (ALL SYMBOLS DIRECT) ──
            if self.engine and key in self._tick_pool:
                tick = self._tick_pool[key]
                tick.tok = token
                tick.lp = st["lp"] if st["lp"] > 0 else lp
                tick.vol = st["vol"] if st["vol"] > 0 else new_vol
                tick.ts = now_t
                tick.bp[:] = st_bp                       # C-level 5-element copies; no per-element Python loop
                tick.bq[:] = st_bq
                tick.sp[:] = st_sp
                tick.sq[:] = st_sq
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
                    _log_tick.debug(f"universe_manager tick dispatch error: {e_sig}")
        except Exception as e:
            _log_tick.debug(f"on_message error: {e}")

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
            _log_tick.error("TICK WIRING FAILED: no add_tick_listener/subscribe found on broker chain!")
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
                _log_tick.warning(f"Subscribe deferred ({exchange} {len(tokens)} tokens): {e_sub}")
        _log_tick.info(f"Subscribed {len(tokens)} equities on {exchange} (touchline+depth) for 3m+15m WS candles.")

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
                        _log_tick.info("WS closed outside active market hours (08:55-15:17) — skipping reconnect.")
                        return
                except Exception:
                    # If the calendar import fails we err on the side of NOT
                    # hammering the broker on a weekend; bail out.
                    _log_tick.warning("is_exchange_trading_day / is_bot_active_hours unavailable — skipping WS reconnect re-issue.")
                    return
                mdp = self._resolve_mdp()
                if mdp is None:
                    _log_tick.warning("Reconnect cannot re-issue subscribe: no MDP resolved.")
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
                    _log_tick.info("WS reconnect skipped: no subscribed tokens remembered.")
                    return
                try:
                    if hasattr(mdp, "subscribe"):
                        for exch, toks in grouped.items():
                            if not toks:
                                continue
                            try:
                                TickCompiler._subscribe_feeds(mdp, toks, exch)
                            except Exception as e_sub:
                                _log_tick.warning(f"Reconnect subscribe deferred ({exch}): {e_sub}")
                    total = sum(len(v) for v in grouped.values())
                    _log_tick.info(f"WS reconnect re-subscribed {total} tokens across {len(grouped)} exchanges after {backoff:.1f}s backoff.")
                    return  # re-issue succeeded — close listener will fire again on the next drop
                except Exception as exc:
                    _log_tick.warning(f"WS reconnect re-subscribe failed ({exc}); backing off {min(backoff*2, 30.0):.1f}s.")
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
        _log_tick.info(f"[TIER2] Updated Tier 2 symbols: {len(self._tier2_symbols)} symbols")

    def set_engine_symbols(self, symbols: set):
        """Set top 15 symbols for adaptive engine dispatch."""
        self._engine_symbols = set(symbols) if symbols else set()
        _log_tick.info(f"[TIER2] Updated Engine symbols: {len(self._engine_symbols)} symbols")

    def get_15min_stats(self, sym: str) -> dict | None:
        """Get 15-min rolling stats for a symbol."""
        st = self._stats_15min.get(sym)
        if not st or st['ticks'] == 0:
            return None
        elapsed = time.time() - self._stats_window_start
        if elapsed <= 0:
            elapsed = 300.0  # 5 min rolling
        ticks_per_min = st['ticks'] / (elapsed / 60.0)
        vol_per_min = st['volume'] / (elapsed / 60.0)
        trade_value_per_min = st['trade_value'] / (elapsed / 60.0)
        mid_prices = st['mid_prices']
        last_mid = mid_prices[-1] if mid_prices else 1.0
        avg_spread_bps = (st['spread_sum'] / max(st['spread_cnt'], 1)) / max(last_mid, 1.0) * 10000 if st['spread_cnt'] > 0 else 0
        microprice_vol = 0.0
        ret_5m = 0.0
        if len(mid_prices) > 5:
            import numpy as np
            microprice_vol = float(np.std(mid_prices) / max(np.mean(mid_prices), 1) * 100)
            if mid_prices[0] > 0:
                ret_5m = float((mid_prices[-1] - mid_prices[0]) / mid_prices[0] * 100.0)
        
        # 5-minute VWAP & Distance from VWAP
        vwap_5m = st['trade_value'] / max(st['volume'], 1.0) if st['volume'] > 0 else last_mid
        vwap_dist = float((last_mid - vwap_5m) / vwap_5m * 100.0) if vwap_5m > 0 and last_mid > 0 else 0.0
        
        oib = st['oib_sum'] / max(st['oib_cnt'], 1) if st['oib_cnt'] > 0 else 0.0
        return {
            'ticks_per_min': ticks_per_min,
            'vol_per_min': vol_per_min,
            'avg_spread_bps': avg_spread_bps,
            'trade_value_per_min': trade_value_per_min,
            'microprice_vol': microprice_vol,
            'ret_15m': ret_5m,
            'ret_5m': ret_5m,
            'vwap_dist': vwap_dist,
            'oib': oib,
        }

    def get_5min_stats(self, symbol: str) -> dict:
        return self.get_15min_stats(symbol)

    def reset_15min_stats(self):
        """Reset 5-min stats for new window."""
        self._stats_15min.clear()
        self._stats_window_start = time.time()
        _log_tick.info("[MACRO_5M] Reset 5-min stats for new window")

    def reset_5min_stats(self):
        self.reset_15min_stats()

    def reset_session(self):
        """Reset session-level state for market open / day rollover.
        Clears merged book state and depth to eliminate yesterday's cumulative volume carry-over."""
        with self.lock:
            self._book_state.clear()
            self.market_depth.clear()
            if hasattr(self.bar_book, "forming"):
                self.bar_book.forming.clear()
            if hasattr(self.bar_book_15, "forming"):
                self.bar_book_15.forming.clear()
            self._stats_15min.clear()
            self._stats_window_start = time.time()
        _log_tick.info("[TickCompiler] Session state reset completed (clean slate for day rollover).")

    def stop(self) -> None:
        """Detaches engine and clears active subscription tracking for clean shutdown."""
        self.engine = None
        self._subscribed_tokens = {}
        _log_tick.info("[TickCompiler] Stopped and detached from engine.")

ACTIVE_BROKER = (os.getenv("ACTIVE_BROKER") or BROKER).upper()

_log_fetch = get_logger("data_fetcher")

def is_call_symbol(sym: str) -> bool:
    """True for CALL symbols across Shoonya formats: CE/CALL words, single-letter
    C suffix (NIFTY29SEP26C22700) or trailing-C stems. Unknown defaults to PUT."""
    s = str(sym).upper().strip()
    if 'CE' in s or 'CALL' in s:
        return True
    if 'PE' in s or 'PUT' in s:
        return False
    stripped = s.rstrip('0123456789')
    return stripped.endswith('C')

def split_option_sides(df):
    """Split an option-universe frame into (ce_df, pe_df) robust to single-letter
    (C/P) and CE/PE symbol formats. Never raises on empty/missing input."""
    if df is None or len(df) == 0 or 'symbol' not in getattr(df, 'columns', []):
        empty = df.iloc[0:0] if df is not None and hasattr(df, 'iloc') else df
        return empty, empty
    mask = df['symbol'].astype(str).apply(is_call_symbol)
    return df[mask].copy(), df[~mask].copy()

def universe_has_both_sides(df) -> bool:
    """True only when the universe carries at least one CE and one PE row."""
    try:
        ce_df, pe_df = split_option_sides(df)
        return len(ce_df) > 0 and len(pe_df) > 0
    except Exception:
        return False

# Cash Index tokens
NIFTY_CASH_TOKEN = "26000"

SENSEX_CASH_TOKEN = "1"

class DataFetcher:
    def __init__(self, broker_gateway):
        self.broker = broker_gateway
        from .infra import InstrumentRepository
        self.repo = InstrumentRepository()
        self.candle_window = collections.deque(maxlen=512)
        self._cached_weekly_opts = None
        self._last_chain_fetch_ts = 0.0
        self._last_atm_cached = 0.0

    def bootstrap_candle_window(self):
        """
        Fetches the last 7 trading days of Nifty 50 (or SENSEX) 5-minute candles
        via the broker's REST API at market opening to seed the sliding window.
        Attempts to load from disk cache first if available.
        """
        token, name, exchange = self.get_cash_index_token()
        candle_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "scratch", f"underlying_candles_{name}.parquet")
        
        # Try loading from disk cache first
        if os.path.exists(candle_file):
            try:
                df = pd.read_parquet(candle_file)
                if not df.empty and len(df) >= 200:
                    _log_fetch.info(f"Loading {name} candle sliding window from disk cache {candle_file}...")
                    df = df.tail(512)
                    self.candle_window.clear()
                    for _, row in df.iterrows():
                        self.candle_window.append(row.to_dict())
                    _log_fetch.info(f"Successfully bootstrapped sliding window with {len(self.candle_window)} candles from disk.")
                    return True
            except Exception as e:
                _log_fetch.warning(f"Failed to load candle cache from disk: {e}. Falling back to API bootstrap.")

        _log_fetch.info(f"Bootstrapping {name} candle sliding window (last 7 trading days) via Broker REST API...")
        df = self.get_historical_candles(exchange, token, "FIVE_MINUTE", days_back=12)
        if df.empty:
            _log_fetch.error(f"Failed to bootstrap candle window for {name}. REST API returned empty DataFrame.")
            return False
        df = df.tail(512)
        self.candle_window.clear()
        for _, row in df.iterrows():
            self.candle_window.append(row.to_dict())
        
        # Save to disk cache
        try:
            os.makedirs(os.path.dirname(candle_file), exist_ok=True)
            df.to_parquet(candle_file, index=False)
            _log_fetch.info(f"Saved {len(df)} bootstrapped candles to disk cache: {candle_file}")
        except Exception as e:
            _log_fetch.error(f"Failed to save candle cache to disk: {e}")
            
        _log_fetch.info(f"Successfully bootstrapped sliding window with {len(self.candle_window)} candles for {name}.")
        return True

    def append_live_candle(self, candle_dict: dict):
        self.candle_window.append(candle_dict)
        ts = candle_dict.get('timestamp', 'N/A')
        o = float(candle_dict.get('open', 0.0))
        h = float(candle_dict.get('high', 0.0))
        l = float(candle_dict.get('low', 0.0))
        c = float(candle_dict.get('close', 0.0))
        v = candle_dict.get('volume', 0)
        
        _log_fetch.info(f"[REST API CANDLE] Time: {ts} | Open: {o:.2f} | High: {h:.2f} | Low: {l:.2f} | Close: {c:.2f} | Vol: {v}")
        _log_fetch.info(f"[SLIDING WINDOW] Appended candle [{ts}] to window (Total Size: {len(self.candle_window)}/512 candles).")
        
        # Save updated deque to disk cache
        try:
            _, name, _ = self.get_cash_index_token()
            candle_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "scratch", f"underlying_candles_{name}.parquet")
            df_to_save = pd.DataFrame(list(self.candle_window))
            os.makedirs(os.path.dirname(candle_file), exist_ok=True)
            df_to_save.to_parquet(candle_file, index=False)
        except Exception as e:
            _log_fetch.error(f"Failed to save live candle cache to disk: {e}")

    def get_window_df(self) -> pd.DataFrame:
        df = pd.DataFrame(list(self.candle_window))
        if not df.empty:
            df['timestamp'] = pd.to_datetime(df['timestamp'])
        return df

    def get_index_ltp(self) -> float:
        # WS-driven spot: prefer footprint manager (live provider attribute),
        # fall back to legacy singular name for backward compatibility.
        mdp = getattr(self.broker, "market_data_provider", None)
        if mdp is not None:
            for attr in ("footprint_compiler_manager", "footprint_compiler"):
                fc = getattr(mdp, attr, None)
                if fc is not None:
                    spot = float(getattr(fc, "current_spot", 0.0) or 0.0)
                    if spot > 0:
                        return spot
                
        token, name, exchange = self.get_cash_index_token()
        quote = self.broker.market_data_provider.get_quote(exchange, token)
        return quote.get("ltp", 0.0)

    def get_active_instrument(self):
        """
        Returns the Name and Exchange Segment based on the day of the week.
        Wed (2), Thu (3) -> SENSEX (BFO)
        Mon (0), Tue (1), Fri (4) -> NIFTY (NFO)
        """
        day = datetime.now().weekday()
        if day in [2, 3]:
            return "SENSEX", "BFO"
        else:
            return "NIFTY", "NFO"



    def get_cash_index_token(self):
        name, exch_seg = self.get_active_instrument()
        if name == "NIFTY":
            token = "26000"
            return token, "NIFTY", "NSE"
        else:
            token = "1" if ACTIVE_BROKER == "SHOONYA" else "99919000"
            return token, "SENSEX", "BSE"

    def get_historical_candles(self, exchange, token, interval, days_back=5, minutes_back=None):
        todate = datetime.now()
        if minutes_back is not None:
            fromdate = todate - timedelta(minutes=minutes_back)
        else:
            fromdate = todate - timedelta(days=days_back)
        
        try:
            df = self.broker.historical_provider.get_historical(
                exchange=exchange,
                token=token,
                interval=interval,
                start_time=fromdate,
                end_time=todate
            )
            return df
        except Exception as e:
            _log_fetch.error(f"Error fetching candles for token {token}: {e}")
        return pd.DataFrame()

    def get_previous_day_5m_candles(self, exchange: str = None, token: str = None, prev_date: date = None) -> pd.DataFrame:
        """
        Fetches the complete 5-minute historical candle series (~75 candles) for the given
        underlying index from the previous official trading session (09:15:00 to 15:30:00 IST).
        Checks disk cache first to minimize redundant REST calls on bot restarts.
        """
        from common.market.market_calendar import get_previous_trading_day
        from datetime import time as dt_time

        if prev_date is None:
            prev_date = get_previous_trading_day(datetime.now().date())
        
        if token is None or exchange is None:
            default_token, name, default_exch = self.get_cash_index_token()
            token = token or default_token
            exchange = exchange or default_exch
        else:
            name = "NIFTY" if token == "26000" else ("SENSEX" if token in ("1", "99919000") else str(token))

        cache_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "scratch")
        cache_file = os.path.join(cache_dir, f"previous_day_5m_{name}_{prev_date.strftime('%Y%m%d')}.parquet")
        
        if os.path.exists(cache_file):
            try:
                df_cached = pd.read_parquet(cache_file)
                if not df_cached.empty and len(df_cached) >= 30:
                    _log_fetch.info(f"[DataFetcher] Loaded {len(df_cached)} previous-day 5m candles for {name} ({prev_date}) from cache: {cache_file}")
                    return df_cached
            except Exception as e_cache:
                _log_fetch.debug(f"[DataFetcher] Cache read failed ({e_cache}), falling back to broker API.")

        start_time = datetime.combine(prev_date, dt_time(9, 15, 0))
        end_time = datetime.combine(prev_date, dt_time(15, 30, 0))
        
        # 1. Try via historical_provider
        try:
            hp = getattr(self.broker, "historical_provider", None)
            if hp is not None and hasattr(hp, "get_historical"):
                df = hp.get_historical(
                    exchange=exchange,
                    token=str(token),
                    interval="FIVE_MINUTE",
                    start_time=start_time,
                    end_time=end_time
                )
                if df is not None and not df.empty:
                    os.makedirs(cache_dir, exist_ok=True)
                    try:
                        df.to_parquet(cache_file, index=False)
                    except Exception:
                        pass
                    _log_fetch.info(f"[DataFetcher] Fetched {len(df)} 5m candles for {name} ({prev_date}) via historical provider.")
                    return df
        except Exception as e_hp:
            _log_fetch.debug(f"[DataFetcher] Historical provider fetch failed for {name}: {e_hp}")

        # 2. Try direct API get_time_price_series
        try:
            api = getattr(self.broker, "_api", None) or self.broker
            if hasattr(api, "get_time_price_series"):
                resp = api.get_time_price_series(
                    exchange=exchange,
                    token=str(token),
                    starttime=int(start_time.timestamp()),
                    endtime=int(end_time.timestamp()),
                    interval=5
                )
                if isinstance(resp, list) and len(resp) > 0:
                    df = pd.DataFrame(resp)
                    if 'time' in df.columns: df['timestamp'] = df['time']
                    elif 'ssboe' in df.columns: df['timestamp'] = df['ssboe']
                    if 'into' in df.columns: df['open'] = df['into']
                    if 'inth' in df.columns: df['high'] = df['inth']
                    if 'intl' in df.columns: df['low'] = df['intl']
                    if 'intc' in df.columns: df['close'] = df['intc']
                    if 'intv' in df.columns: df['volume'] = df['intv']
                    elif 'v' in df.columns: df['volume'] = df['v']
                    
                    for col in ['open', 'high', 'low', 'close', 'volume']:
                        if col in df.columns:
                            df[col] = pd.to_numeric(df[col], errors='coerce')
                    
                    os.makedirs(cache_dir, exist_ok=True)
                    try:
                        df.to_parquet(cache_file, index=False)
                    except Exception:
                        pass
                    _log_fetch.info(f"[DataFetcher] Fetched {len(df)} 5m candles for {name} ({prev_date}) via direct API.")
                    return df
        except Exception as e_api:
            _log_fetch.debug(f"[DataFetcher] Direct API fetch failed for {name}: {e_api}")

        return pd.DataFrame()

    def get_weekly_option_tokens(self) -> pd.DataFrame:
        name, exch_seg = self.get_active_instrument()
        rows = self.repo.get_weekly_option_tokens(ACTIVE_BROKER, name, exch_seg)
        df = pd.DataFrame(rows) if rows else pd.DataFrame()
        
        # If DB lookup returns empty or incomplete option chain, fetch dynamic chain via searchscrip.
        # Retry once when the chain comes back single-sided (partial searchscrip
        # failure): adopting a CE-only or PE-only universe blinds half the flow.
        for _attempt in (1, 2):
            if not (df.empty or len(df) < 5 or not universe_has_both_sides(df)):
                break
            if _attempt == 2 and not df.empty:
                _log_fetch.warning("[DataFetcher] dynamic chain still single-sided after retry — keeping last good universe upstream")
                break
            try:
                index_price = self.get_index_ltp()
                
                if index_price > 0:
                    step = 100 if name == "SENSEX" else 50
                    atm_strike = round(index_price / step) * step

                    # Short-term cache check: return cached chain if ATM is unchanged and within 45s
                    now_ts = time.time()
                    if (
                        self._cached_weekly_opts is not None
                        and not self._cached_weekly_opts.empty
                        and (now_ts - self._last_chain_fetch_ts < 45.0)
                        and abs(atm_strike - self._last_atm_cached) < step
                    ):
                        return self._cached_weekly_opts

                    # Expand to 15 strikes ITM and OTM (total 31 strikes) to support deeper affordable OTM trading
                    depth = int(os.getenv("AEGIS_OPTION_CHAIN_DEPTH", "25"))
                    strike_list = [atm_strike + (i * step) for i in range(-depth, depth + 1)]
                    
                    import re
                    dynamic_opts = []
                    for s_val in strike_list:
                        search_term = f"{name} {int(s_val)}"
                        res = self.broker._api.searchscrip(exchange=exch_seg, searchtext=search_term)
                        if isinstance(res, dict) and res.get('stat') == 'Ok':
                            for item in res.get('values', []):
                                if item.get('instname') == 'OPTIDX' and (item.get('symname') == name or item.get('symname') == 'BSXOPT'):
                                    tsym = item.get('tsym', '')
                                    exd_str = item.get('exd', '')
                                    try:
                                        expiry_dt = datetime.strptime(exd_str, '%d-%b-%Y')
                                    except ValueError:
                                        try:
                                            expiry_dt = datetime.strptime(exd_str, '%d%b%Y')
                                        except ValueError:
                                            continue
                                            
                                    m = re.search(r'[CP](\d+)$', tsym)
                                    strike = float(m.group(1)) if m else float(s_val)
                                    
                                    dynamic_opts.append({
                                        'token': str(item['token']),
                                        'symbol': tsym,
                                        'expiry': exd_str,
                                        'expiry_dt': expiry_dt,
                                        'strike': strike
                                    })
                    
                    if dynamic_opts:
                        dyn_df = pd.DataFrame(dynamic_opts)
                        now = datetime.now()
                        valid_dts = [dt for dt in dyn_df['expiry_dt'] if dt.date() >= now.date()]
                        if valid_dts:
                            nearest_exp = min(valid_dts)
                            df = dyn_df[dyn_df['expiry_dt'] == nearest_exp].drop_duplicates(subset=['token']).reset_index(drop=True)
                            self._cached_weekly_opts = df
                            self._last_chain_fetch_ts = now_ts
                            self._last_atm_cached = atm_strike
            except Exception as e:
                _log_fetch.error(f"Error fetching dynamic option chain via searchscrip: {e}")
                
        return df
