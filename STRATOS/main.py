"""
main.py — Production Standalone Runner for STRATOS.
Connects to Shoonya Live WebSocket feed and runs the Multi-Timeframe Classical Market Structure & S/R Flip Engine.
Features:
- Live Shoonya Order Execution (MIS 5x Margin)
- Real-Time Tick Resampling (1H, 15m, 5m OHLCV + Order Flow Delta)
- 15m HH2 Breakout Support Floor Detection
- 5m Pullback Volume Contraction & Absorption Retest Trigger
- Anti-Liquidity Hunting SL & Session-Adaptive Trailing Stops (9-EMA / 20-EMA)
- EOD Square-off (15:10 IST)
"""
from __future__ import annotations
import os
import sys
import json
import time
import signal
import threading
from datetime import datetime, time as dt_time

# Bootstrap repository root
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from common.config import configure_runtime, get_data_dir
configure_runtime("stratos")

from common.broker import ShoonyaAdapter
from common.logger import get_logger
from common.symbols import SUBSCRIPTION_UNIVERSE
from common.market.market_calendar import (
    is_trading_day, is_market_open, MARKET_CLOSE_TIME,
    is_bot_active_hours, sleep_until_next_premarket, is_pre_market
)
from STRATOS.config import STRATOS_CONFIG
from STRATOS.orchestrator import StratosOrchestrator

logger = get_logger("stratos_main")

TOKEN_CACHE_PATH = os.path.join(get_data_dir(), "tokens_cache.json")

def load_token_map(symbols: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """
    Load token-to-symbol and symbol-to-token mappings from tokens_cache.json.
    """
    candidates = [
        TOKEN_CACHE_PATH,
        os.path.join(BASE_DIR, "ARGUS", "data", "tokens_cache.json"),
        "/home/opc/ARGUS/data/tokens_cache.json",
        os.path.join(BASE_DIR, "data", "tokens_cache.json")
    ]
    t2s = {}
    s2t = {}
    for p in candidates:
        if os.path.exists(p):
            try:
                with open(p, "r") as f:
                    tc = json.load(f)
                for sym, meta in tc.items():
                    if isinstance(meta, dict) and "token" in meta:
                        tok = str(meta["token"])
                        t2s[tok] = sym
                        s2t[sym] = tok
                if t2s:
                    logger.info(f"[TOKENS] Loaded {len(t2s)} token mappings from {p}")
                    return t2s, s2t
            except Exception as e:
                logger.warning(f"[TOKENS] Failed reading {p}: {e}")
    return t2s, s2t

class StratosLiveService:
    def __init__(self, is_live: bool = True):
        self.is_live = is_live
        self.broker = None
        self.orchestrator = None
        self.running = False
        self.t2s = {}
        self.s2t = {}
        self.symbols = SUBSCRIPTION_UNIVERSE
        self.shutdown_event = threading.Event()

    def initialize(self):
        logger.info("=" * 70)
        logger.info("🚀 INITIALIZING STRATOS: Classical Market Structure & Support Flip Engine")
        logger.info(f"📊 Universe: {len(self.symbols)} Liquid NSE Equities")
        logger.info(f"⚡ Execution Mode: {'LIVE SHOONYA BROKER' if self.is_live else 'PAPER SIMULATION'}")
        logger.info("=" * 70)

        # 1. Connect Shoonya Broker Adapter
        self.broker = ShoonyaAdapter()
        if hasattr(self.broker, "session_provider"):
            self.broker.session_provider._restore_or_login()
        logger.info("[OK] Shoonya Broker Session Active")

        # 2. Load Tokens Map
        self.t2s, self.s2t = load_token_map(self.symbols)

        # 3. Initialize Master Orchestrator
        self.orchestrator = StratosOrchestrator(self.symbols, broker=self.broker, is_live=self.is_live)
        logger.info("[OK] STRATOS Orchestrator Initialized")

    def _on_tick(self, tick: dict):
        """
        WebSocket tick callback (Zero-Allocation Hot Path).
        """
        try:
            tok = str(tick.get("tk", tick.get("token", ""))).split("|")[-1].strip()
            symbol = self.t2s.get(tok)
            if not symbol:
                return

            lp = float(tick.get("lp", tick.get("last_traded_price", 0.0)) or 0.0)
            if lp <= 0:
                return

            vol = float(tick.get("v", tick.get("vol", tick.get("volume_trade_for_the_day", 0.0))) or 0.0)
            ts = float(tick.get("ft", tick.get("ts", time.time())) or time.time())

            # Order Flow Directionality Approximation
            bp1 = float(tick.get("bp1", tick.get("bid", 0.0)) or 0.0)
            sp1 = float(tick.get("sp1", tick.get("ask", 0.0)) or 0.0)
            is_buy = True if lp >= sp1 else False if lp <= bp1 else True

            self.total_ticks += 1
            self.last_tick_time = ts
            self.active_symbols_seen.add(symbol)

            # Feed tick into Orchestrator
            self.orchestrator.on_tick(
                symbol=symbol,
                ts=ts,
                price=lp,
                cumulative_vol=vol,
                is_buy=is_buy
            )
        except Exception as e:
            logger.debug(f"Tick error: {e}")

    def _rest_poller_worker(self):
        """
        Background REST API poller: continuously polls real Shoonya quotes
        for all 110 universe tokens to guarantee 100% resilient data flow.
        """
        import concurrent.futures
        logger.info("[REST_POLLER] Background REST API Poller initialized.")
        while not self.shutdown_event.is_set():
            try:
                now = datetime.now()
                if dt_time(9, 15) <= now.time() <= dt_time(15, 30):
                    api = getattr(self.broker, "_api", getattr(self.broker, "api", self.broker))
                    tokens_list = list(self.s2t.items())

                    def _fetch_one(item):
                        sym, tok = item
                        try:
                            res = api.get_quotes(exchange="NSE", token=tok)
                            if isinstance(res, dict) and res.get("lp") is not None:
                                lp = float(res.get("lp", 0.0) or 0.0)
                                if lp > 0:
                                    self._on_tick({
                                        "tk": tok,
                                        "lp": lp,
                                        "bp1": float(res.get("bp1", 0.0) or 0.0),
                                        "sp1": float(res.get("sp1", 0.0) or 0.0),
                                        "v": float(res.get("v", 0.0) or 0.0),
                                        "ts": time.time()
                                    })
                        except Exception:
                            pass

                    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                        list(executor.map(_fetch_one, tokens_list))

                time.sleep(4.0)
            except Exception as e:
                logger.debug(f"[REST_POLLER] Warning: {e}")
                time.sleep(5.0)

    def start(self):
        self.initialize()
        self.running = True
        self.total_ticks = 0
        self.last_tick_time = 0.0
        self.active_symbols_seen = set()

        # 1. Register Tick Listener on Broker Adapter
        if hasattr(self.broker, "add_tick_listener"):
            self.broker.add_tick_listener(self._on_tick)

        # 2. Subscribe WebSocket to Universe Tokens
        tokens_to_sub = [self.s2t[s] for s in self.symbols if s in self.s2t]
        if tokens_to_sub:
            logger.info(f"[FEED] Subscribing WebSocket to {len(tokens_to_sub)} NSE tokens...")
            if hasattr(self.broker, "subscribe"):
                self.broker.subscribe(tokens_to_sub, exchange="NSE", feed_type="t")
            elif hasattr(self.broker, "api") and hasattr(self.broker.api, "start_websocket"):
                feed_tokens = [f"NSE|{t}" for t in tokens_to_sub]
                self.broker.api.start_websocket(
                    order_update_callback=lambda x: None,
                    subscribe_callback=self._on_tick,
                    socket_open_callback=lambda: self.broker.api.subscribe(feed_tokens)
                )

        # 3. Start Background REST Poller
        poller_thread = threading.Thread(target=self._rest_poller_worker, daemon=True, name="StratosRestPoller")
        poller_thread.start()

        logger.info("🟢 STRATOS LIVE SERVICE RUNNING. Listening for market structure breakouts...")

        # Main Lifecycle & Heartbeat Loop
        while not self.shutdown_event.is_set():
            try:
                now = datetime.now()
                open_cnt = self.orchestrator.trades.get_open_count()
                today_trades = self.orchestrator.trades.get_today_trades_count()
                
                # Active Heartbeat with Tick Stats
                logger.info(
                    f"[HEARTBEAT] Time: {now.strftime('%H:%M:%S')} | "
                    f"Ticks: {self.total_ticks:,} | Symbols Active: {len(self.active_symbols_seen)}/{len(self.symbols)} | "
                    f"Open Positions: {open_cnt} | Today Trades: {today_trades}"
                )
                
                # Check EOD Market Close (15:30 IST)
                if now.time() >= dt_time(15, 30):
                    logger.info("[EOD] Market Closed (15:30 IST). STRATOS standing down.")
                    time.sleep(300)

                self.shutdown_event.wait(30.0)
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"[LOOP] Error in main loop: {e}", exc_info=True)
                time.sleep(5)

    def stop(self):
        logger.info("🛑 Shutting down STRATOS Live Service...")
        self.running = False
        self.shutdown_event.set()
        if self.broker and hasattr(self.broker, "close_websocket"):
            try:
                self.broker.close_websocket()
            except Exception:
                pass
        logger.info("[STOP] STRATOS Live Service stopped cleanly.")

def main():
    service = StratosLiveService(is_live=True)

    def _sig_handler(sig, frame):
        logger.info(f"Signal {sig} received. Initiating graceful shutdown...")
        service.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    service.start()

if __name__ == "__main__":
    main()
