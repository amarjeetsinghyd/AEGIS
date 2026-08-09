from __future__ import annotations

import os
import sys
import json
import time
from datetime import datetime, time as dttime

BASEDIR = os.path.dirname(os.path.dirname(__file__))
if BASEDIR not in sys.path:
    sys.path.insert(0, BASEDIR)

from common.config import configure_runtime, get_data_dir
from common.broker import ShoonyaAdapter
from common.logger import get_logger
from common.market.market_calendar import is_trading_day, is_market_open, MARKET_CLOSE_TIME, TRADE_ENTRY_CUTOFF_TIME
from stock_order_flow.tick_compiler import TickCompiler
from stock_order_flow.execution_manager import StockExecutionManager
from stock_order_flow.strategy_engine import StockStrategyEngine
from stock_order_flow.historical_data_manager import fetch_and_seed_history
from stock_order_flow.bar_book import resample_bars

logger = get_logger("stockmain")
configure_runtime("stock_order_flow")

BAR_MINUTES = 3
UNIVERSE_PATH = os.path.join(get_data_dir(), "universe.json")

DEFAULT_UNIVERSE = {
    "RELIANCE": {"token": 2885, "search": "RELIANCE-EQ"},
    "HDFCBANK": {"token": 1333, "search": "HDFCBANK-EQ"},
    "INFY": {"token": 1594, "search": "INFY-EQ"},
    "LT": {"token": 11483, "search": "LT-EQ"},
    "TATAMOTORS": {"token": 3456, "search": "TATAMOTORS-EQ"},
    "ICICIBANK": {"token": 4963, "search": "ICICIBANK-EQ"},
    "SBIN": {"token": 3045, "search": "SBIN-EQ"},
    "TCS": {"token": 11536, "search": "TCS-EQ"},
    "AXISBANK": {"token": 5900, "search": "AXISBANK-EQ"},
    "ITC": {"token": 1660, "search": "ITC-EQ"},
    "KOTAKBANK": {"token": 1922, "search": "KOTAKBANK-EQ"},
    "BHARTIARTL": {"token": 10604, "search": "BHARTIARTL-EQ"},
    "BAJFINANCE": {"token": 317, "search": "BAJFINANCE-EQ"},
    "MARUTI": {"token": 10999, "search": "MARUTI-EQ"},
    "MM": {"token": 2031, "search": "MM-EQ"},
}


def load_universe():
    try:
        if os.path.exists(UNIVERSE_PATH):
            with open(UNIVERSE_PATH, "r") as f:
                data = json.load(f)
            stocks = data.get("stocks", [])
            uni = {}
            for s in stocks:
                uni[s["symbol"]] = {"token": str(s["token"]), "search": s.get("search", s["symbol"]) }
            logger.info(f"Universe loaded from universe.json: {len(uni)} stocks, dated {data.get('date')}")
            return uni
    except Exception as e:
        logger.warning(f"universe.json read failed: {e}; using default universe")
    return DEFAULT_UNIVERSE


def seed_books(broker_hist, compiler, token_to_symbol):
    for token_str, df in broker_hist.items():
        sym = token_to_symbol.get(str(token_str), str(token_str))
        compiler.bar_book.seed(sym, df)
        df15 = resample_bars(df, 15)
        if df15 is not None and len(df15):
            compiler.bar_book_15.seed(sym, df15)


def main():
    logger.info("Starting Stock Order Flow Engine 3 FINAL: 5x, ATR15, Chandelier, dynamic universe")
    broker = ShoonyaAdapter()
    if hasattr(broker, "session_provider"):
        broker.session_provider._restore_or_login()

    compiler = TickCompiler(broker)
    trader = StockExecutionManager(broker)
    strategy = StockStrategyEngine()
    universe = load_universe()
    tokens = [meta["token"] for meta in universe.values()]
    token_to_symbol = {meta["token"]: sym for sym, meta in universe.items()}

    compiler.set_symbol_map(token_to_symbol)
    hist_data = fetch_and_seed_history(broker, tokens, token_to_symbol)
    seed_books(hist_data, compiler, token_to_symbol)
    compiler.subscribe(tokens)

    last_processed_bucket = None
    eod_sync_done_for_day = None

    while True:
        try:
            now = datetime.now()

            if now.time() >= dttime(15, 17, 0) and eod_sync_done_for_day != now.date():
                logger.info(f"{now.strftime('%H:%M:%S')} Triggering EOD Gap Fill Sync...")
                eod_hist = fetch_and_seed_history(broker, tokens, token_to_symbol)
                seed_books(eod_hist, compiler, token_to_symbol)
                eod_sync_done_for_day = now.date()
                logger.info("EOD Gap Fill Sync Complete.")

            if not is_trading_day(now.date()):
                time.sleep(60)
                continue
            if not is_market_open(now):
                time.sleep(10)
                continue

            current_prices = {}
            for token in tokens:
                sym = token_to_symbol.get(token)
                md = compiler.market_depth.get(str(token), {})
                if md and md.get("lp", 0) > 0:
                    current_prices[sym] = md["lp"]

            trader.manage_open_trades(current_prices)

            bucket_key = (now.date(), now.hour, now.minute - (now.minute % BAR_MINUTES))
            is_boundary = now.minute % BAR_MINUTES == 0 and now.second >= 2
            if is_boundary and bucket_key != last_processed_bucket and is_market_open(now):
                last_processed_bucket = bucket_key
                if len(trader.open_trades) > 0:
                    logger.info(f"{now.strftime('%H:%M:%S')} Global Mutex Active. Skipping evaluation.")
                    compiler.reset_for_new_candle()
                    continue

                logger.info(f"========== {BAR_MINUTES}m BOUNDARY EVALUATION ==========")
                for token in tokens:
                    sym = token_to_symbol.get(token)
                    compiler.bar_book.force_close_forming(sym, now)
                    compiler.bar_book_15.force_close_forming(sym, now)

                token_footprints = compiler.get_compiled_footprints()
                sym_footprints = {token_to_symbol.get(t, t): fp for t, fp in token_footprints.items()}

                best_sym, signal, reason, metrics = strategy.evaluate_all(sym_footprints, compiler.bar_book, compiler.bar_book_15, now)
                allow_entry = now.time() < TRADE_ENTRY_CUTOFF_TIME
                if best_sym and signal and allow_entry:
                    px = current_prices.get(best_sym, 0.0)
                    if px <= 0:
                        logger.warning(f"No live price for {best_sym}; skipping entry.")
                    else:
                        logger.info(f"🏆 MUTEX WINNER: {best_sym} -> {signal} | {reason}")
                        trader.execute_entry(signal, best_sym, px, reason, metrics=metrics)
                compiler.reset_for_new_candle()

            time.sleep(1)
        except Exception as e:
            logger.error(f"Loop error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
