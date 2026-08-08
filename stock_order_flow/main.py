"""Stock Order Flow — Engine 3 main loop (paper), 3-minute WS clock."""
from __future__ import annotations
import os, sys, time
from datetime import datetime, time as dt_time

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
if BASE_DIR not in sys.path: sys.path.insert(0, BASE_DIR)
from common.config import configure_runtime
configure_runtime("stock_order_flow")

from common.broker import ShoonyaAdapter
from common.logger import get_logger
from common.market.market_calendar import is_trading_day, is_market_open, MARKET_OPEN_TIME, MARKET_CLOSE_TIME, TRADE_ENTRY_CUTOFF_TIME
from stock_order_flow.tick_compiler import TickCompiler
from stock_order_flow.execution_manager import StockExecutionManager
from stock_order_flow.strategy_engine import StockStrategyEngine # NEW
from stock_order_flow.historical_data_manager import fetch_and_seed_history

logger = get_logger("stock_main")
BAR_MINUTES = 3

EQUITY_UNIVERSE = {
    # Existing 5 Stocks
    "RELIANCE": {"token": "2885", "tsym": "RELIANCE-EQ", "search": ["RELIANCE-EQ", "RELIANCE"]},
    "HDFCBANK": {"token": "1333", "tsym": "HDFCBANK-EQ", "search": ["HDFCBANK-EQ", "HDFCBANK"]},
    "INFY": {"token": "1594", "tsym": "INFY-EQ", "search": ["INFY-EQ", "INFY"]},
    "LT": {"token": "11483", "tsym": "LT-EQ", "search": ["LT-EQ", "LT"]},
    "TATAMOTORS": {
        "token": "3456",
        "tsym": "TMPV-EQ",
        "search": ["Tata Motors", "TMPV-EQ", "TATAMOTORS-EQ"],
        "note": "NSE renamed/split; tracking TMPV-EQ (ex-TATAMOTORS passenger)",
    },
    
    # New 10 High-Liquidity Stocks
    "ICICIBANK": {"token": "4963", "tsym": "ICICIBANK-EQ", "search": ["ICICIBANK-EQ", "ICICIBANK"]},
    "SBIN": {"token": "3045", "tsym": "SBIN-EQ", "search": ["SBIN-EQ", "SBIN"]},
    "TCS": {"token": "11536", "tsym": "TCS-EQ", "search": ["TCS-EQ", "TCS"]},
    "AXISBANK": {"token": "5900", "tsym": "AXISBANK-EQ", "search": ["AXISBANK-EQ", "AXISBANK"]},
    "ITC": {"token": "1660", "tsym": "ITC-EQ", "search": ["ITC-EQ", "ITC"]},
    "KOTAKBANK": {"token": "1922", "tsym": "KOTAKBANK-EQ", "search": ["KOTAKBANK-EQ", "KOTAKBANK"]},
    "BHARTIARTL": {"token": "10604", "tsym": "BHARTIARTL-EQ", "search": ["BHARTIARTL-EQ", "BHARTIARTL"]},
    "BAJFINANCE": {"token": "317", "tsym": "BAJFINANCE-EQ", "search": ["BAJFINANCE-EQ", "BAJFINANCE"]},
    "MARUTI": {"token": "10999", "tsym": "MARUTI-EQ", "search": ["MARUTI-EQ", "MARUTI"]},
    "M&M": {"token": "2031", "tsym": "M&M-EQ", "search": ["M&M-EQ", "M&M"]},
}

def main():
    logger.info("Starting Stock Order Flow Engine 3 — V4.4 Architecture")
    broker = ShoonyaAdapter()
    if hasattr(broker, "session_provider"): broker.session_provider._restore_or_login()
    
    compiler = TickCompiler(broker)
    trader = StockExecutionManager(broker)
    strategy = StockStrategyEngine()

    tokens = [meta["token"] for meta in EQUITY_UNIVERSE.values()]
    token_to_symbol = {meta["token"]: sym for sym, meta in EQUITY_UNIVERSE.items()}
    
    # ---------------------------------------------------------
    # 1. Historical Gap Fill (Boot Sync)
    # ---------------------------------------------------------
    hist_data = fetch_and_seed_history(broker, tokens, token_to_symbol)
    for token_str, df in hist_data.items():
        compiler.bar_book.seed(token_str, df)
    
    compiler.subscribe(tokens)
    
    last_processed_bucket = None
    eod_sync_done_for_day = None

    while True:
        try:
            now = datetime.now()
            
            # EOD Gap Fill Sync Check (at 15:17)
            if now.time() >= dt_time(15, 17, 0) and eod_sync_done_for_day != now.date():
                logger.info(f"[{now.strftime('%H:%M:00')}] Triggering EOD Gap Fill Sync...")
                eod_hist = fetch_and_seed_history(broker, tokens, token_to_symbol)
                for token_str, df in eod_hist.items():
                    compiler.bar_book.seed(token_str, df)
                eod_sync_done_for_day = now.date()
                logger.info("EOD Gap Fill Sync Complete. Data persisted safely.")
            
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
                
                # Check Global Mutex before doing heavy calculations
                if len(trader.open_trades) > 0:
                    logger.info(f"[{now.strftime('%H:%M:00')}] Global Mutex Active. Skipping evaluation.")
                    compiler.reset_for_new_candle()
                    continue
                    
                logger.info(f"========== {BAR_MINUTES}m BOUNDARY EVALUATION ==========")
                
                for token in tokens:
                    compiler.bar_book.force_close_forming(token, now)
                
                # Convert token footprints to symbol footprints for the strategy engine
                token_footprints = compiler.get_compiled_footprints()
                sym_footprints = {token_to_symbol[t]: fp for t, fp in token_footprints.items()}

                # Evaluate all stocks together and get the SINGLE best signal
                best_sym, signal, reason = strategy.evaluate_all(sym_footprints, compiler.bar_book, now)
                
                allow_entry = now.time() < TRADE_ENTRY_CUTOFF_TIME
                if best_sym and signal and allow_entry and best_sym in current_prices and current_prices[best_sym] > 0:
                    logger.info(f"🏆 MUTEX WINNER: {best_sym} -> {signal} | {reason}")
                    trader.execute_entry(signal, best_sym, current_prices[best_sym], reason)
                
                compiler.reset_for_new_candle()

            time.sleep(1)
        except Exception as e:
            logger.error(f"Loop error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()