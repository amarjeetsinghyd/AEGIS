"""Stock Order Flow — Engine 3 main loop (LIVE), 3-minute WS clock.
FINAL v4: symbol-keyed books + tsym map + StructureMapEngine wiring
(15m bars filtered to today for OR15/OR30 freeze)."""
from __future__ import annotations
import os, sys, json, time
from datetime import datetime, time as dt_time
import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from common.config import configure_runtime, get_data_dir
configure_runtime("stock_order_flow")

from common.broker import ShoonyaAdapter
from common.logger import get_logger
from common.market.market_calendar import (
    is_trading_day, is_market_open, MARKET_CLOSE_TIME,
    is_bot_active_hours, sleep_until_next_premarket, is_pre_market
)
from stock_order_flow.tick_compiler import TickCompiler
from stock_order_flow.execution_manager import StockExecutionManager
from stock_order_flow.strategy_engine import StockStrategyEngine
from stock_order_flow.structure_map import StructureMapEngine
from stock_order_flow.historical_data_manager import fetch_and_seed_history
from stock_order_flow.bar_book import resample_bars

logger = get_logger("stock_main")

BAR_MINUTES = 3
UNIVERSE_PATH = os.path.join(get_data_dir(), "universe.json")

DEFAULT_UNIVERSE = {
    "RELIANCE": {"token": "2885", "search": ["RELIANCE-EQ", "RELIANCE"]},
    "HDFCBANK": {"token": "1333", "search": ["HDFCBANK-EQ", "HDFCBANK"]},
    "INFY": {"token": "1594", "search": ["INFY-EQ", "INFY"]},
    "LT": {"token": "11483", "search": ["LT-EQ", "LT"]},
    "TATAMOTORS": {"token": "3456", "search": ["Tata Motors", "TMPV-EQ", "TATAMOTORS-EQ"]},
    "ICICIBANK": {"token": "4963", "search": ["ICICIBANK-EQ", "ICICIBANK"]},
    "SBIN": {"token": "3045", "search": ["SBIN-EQ", "SBIN"]},
    "TCS": {"token": "11536", "search": ["TCS-EQ", "TCS"]},
    "AXISBANK": {"token": "5900", "search": ["AXISBANK-EQ", "AXISBANK"]},
    "ITC": {"token": "1660", "search": ["ITC-EQ", "ITC"]},
    "KOTAKBANK": {"token": "1922", "search": ["KOTAKBANK-EQ", "KOTAKBANK"]},
    "BHARTIARTL": {"token": "10604", "search": ["BHARTIARTL-EQ", "BHARTIARTL"]},
    "BAJFINANCE": {"token": "317", "search": ["BAJFINANCE-EQ", "BAJFINANCE"]},
    "MARUTI": {"token": "10999", "search": ["MARUTI-EQ", "MARUTI"]},
    "M&M": {"token": "2031", "search": ["M&M-EQ", "M&M"]},
}


def load_universe():
    try:
        if os.path.exists(UNIVERSE_PATH):
            with open(UNIVERSE_PATH, "r") as f:
                data = json.load(f)
            stocks = data.get("stocks", [])
            if stocks:
                uni = {}
                for s in stocks:
                    uni[s["symbol"]] = {"token": str(s["token"]), "search": s.get("search", [])}
                logger.info(f"Universe loaded from universe.json ({len(uni)} stocks, dated {data.get('date')})")
                return uni
    except Exception as e:
        logger.warning(f"universe.json read failed ({e}) — using default universe")
    return DEFAULT_UNIVERSE


def build_tsym_map(universe: dict) -> dict:
    m = {}
    for sym, meta in universe.items():
        search = meta.get("search", [])
        if isinstance(search, list) and search:
            m[sym] = str(search[0])
        elif isinstance(search, str) and search:
            m[sym] = search
        else:
            m[sym] = f"{sym}-EQ"
    return m


def compute_prior(df):
    try:
        if df is None or len(df) == 0:
            return None
        d = df.copy()
        d["timestamp"] = pd.to_datetime(d["timestamp"])
        d["date"] = d["timestamp"].dt.date
        last_date = d["date"].max()
        prev = d[d["date"] < last_date]
        if len(prev) == 0:
            return None
        pday = prev[prev["date"] == prev["date"].max()]
        if len(pday) == 0:
            return None
        h = float(pday["high"].max())
        l = float(pday["low"].min())
        c = float(pday["close"].iloc[-1])
        tp = (pday["high"] + pday["low"] + pday["close"]) / 3.0
        v = pday["volume"].replace(0, 1e-9)
        vwap = float((tp * v).sum() / v.sum())
        return {"h": h, "l": l, "c": c, "vwap": vwap}
    except Exception:
        return None


def seed_books(broker_hist, compiler, token_to_symbol, smap):
    for token_str, df in broker_hist.items():
        sym = token_to_symbol.get(str(token_str), str(token_str))
        compiler.bar_book.seed(sym, df)
        df15 = resample_bars(df, 15)
        if df15 is not None and len(df15):
            compiler.bar_book_15.seed(sym, df15)
        prior = compute_prior(df)
        if prior:
            smap.set_prior(sym, prior["h"], prior["l"], prior["c"], prior["vwap"])


def main():
    logger.info("Starting Stock Order Flow Engine 3 — FINAL v4 (zone-aware)")
    now = datetime.now()
    if not is_bot_active_hours(now) and not is_pre_market(now):
        logger.info("Bot started outside active hours. Entering deep sleep until premarket...")
        sleep_until_next_premarket()

    broker = ShoonyaAdapter()
    if hasattr(broker, "session_provider"):
        broker.session_provider._restore_or_login()
    compiler = TickCompiler(broker)
    trader = StockExecutionManager(broker)
    strategy = StockStrategyEngine()
    smap = StructureMapEngine()

    universe = load_universe()
    tokens = [meta["token"] for meta in universe.values()]
    token_to_symbol = {meta["token"]: sym for sym, meta in universe.items()}

    compiler.set_symbol_map(token_to_symbol)
    trader.set_tsym_map(build_tsym_map(universe))

    from stock_order_flow.l2_archiver import attach_archiver
    archiver = attach_archiver(broker, token_to_symbol)

    from stock_order_flow.cascade.value_engine import attach_cascade_engine
    cascade_engine = attach_cascade_engine(broker, token_to_symbol)

    hist_data = fetch_and_seed_history(broker, tokens, token_to_symbol)
    seed_books(hist_data, compiler, token_to_symbol, smap)
    compiler.subscribe(tokens)

    last_processed_bucket = None
    eod_sync_done_for_day = None

    while True:
        try:
            now = datetime.now()

            if not is_bot_active_hours(now):
                logger.info("Bot active runtime closed. Entering deep sleep...")
                sleep_until_next_premarket()
                if hasattr(broker, "session_provider"):
                    broker.session_provider._restore_or_login()
                continue
                
            if is_pre_market(now):
                time.sleep(1)
                continue

            if now.time() >= dt_time(15, 17, 0) and eod_sync_done_for_day != now.date():
                logger.info(f"[{now.strftime('%H:%M:00')}] Triggering EOD Gap Fill Sync...")
                eod_hist = fetch_and_seed_history(broker, tokens, token_to_symbol)
                seed_books(eod_hist, compiler, token_to_symbol, smap)
                eod_sync_done_for_day = now.date()
                logger.info("EOD Gap Fill Sync Complete.")

            if not is_trading_day(now.date()) or now.time() > MARKET_CLOSE_TIME:
                time.sleep(60)
                continue

            current_prices = {}
            for t in tokens:
                sym = token_to_symbol[t]
                if t in compiler.market_depth and compiler.market_depth[t]["lp"] > 0:
                    current_prices[sym] = compiler.market_depth[t]["lp"]

            trader.manage_open_trades(current_prices)

            bucket_key = (now.date(), now.hour, now.minute - (now.minute % BAR_MINUTES))
            is_boundary = (now.minute % BAR_MINUTES == 0) and (now.second >= 2)

            if is_boundary and bucket_key != last_processed_bucket and is_market_open(now):
                last_processed_bucket = bucket_key

                if len(trader.open_trades) > 0:
                    logger.info(f"[{now.strftime('%H:%M:00')}] Global Mutex Active. Skipping evaluation.")
                    compiler.reset_for_new_candle()
                    continue

                logger.info(f"========== {BAR_MINUTES}m BOUNDARY EVALUATION ==========")
                for token in tokens:
                    sym = token_to_symbol[token]
                    compiler.bar_book.force_close_forming(sym, now)
                    compiler.bar_book_15.force_close_forming(sym, now)

                # update spatial map from TODAY's 15m structure only
                today_ts = pd.Timestamp(now.date())
                for token in tokens:
                    sym = token_to_symbol[token]
                    df15 = compiler.bar_book_15.completed_df(sym, min_bars=1)
                    if df15 is not None and len(df15):
                        df15 = df15[pd.to_datetime(df15["timestamp"]) >= today_ts]
                    bars = df15.to_dict("records") if df15 is not None and len(df15) else []
                    atr15 = strategy._atr15(compiler.bar_book_15, sym)
                    smap.update(sym, bars, atr15, now)

                token_footprints = compiler.get_compiled_footprints()
                sym_footprints = {token_to_symbol[t]: fp for t, fp in token_footprints.items()}

                best_sym, signal, reason, metrics = strategy.evaluate_all(
                    sym_footprints, compiler.bar_book, compiler.bar_book_15, now, smap=smap)
                allow_entry = now.time() < dt_time(14, 45)
                if best_sym and signal and allow_entry:
                    px = current_prices.get(best_sym, 0.0)
                    if px <= 0:
                        logger.warning(f"No live price for {best_sym} — skipping entry")
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