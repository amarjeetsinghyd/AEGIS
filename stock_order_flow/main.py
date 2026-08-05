import os
import sys
import time
from datetime import datetime, timedelta

# Add quant-bot base to sys path
BASE_DIR = os.path.dirname(os.path.dirname(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from common.broker import ShoonyaAdapter
from common.logger import get_logger
from common.config import configure_runtime
configure_runtime("stock_order_flow")

from stock_order_flow.tick_compiler import TickCompiler
from stock_order_flow.evaluator import FootprintEvaluator
from stock_order_flow.execution_manager import StockExecutionManager

logger = get_logger("stock_main")

STOCKS = ["RELIANCE", "HDFCBANK", "INFY", "LT", "TATAMOTORS"]

def main():
    logger.info("Starting Stock Order Flow Engine (Engine 3) - PAPER TRADING MODE")
    try:
        broker = ShoonyaAdapter()
        compiler = TickCompiler(broker)
        evaluator = FootprintEvaluator()
        trader = StockExecutionManager(broker)
    except Exception as e:
        logger.error(f"Initialization failed: {e}")
        sys.exit(1)

    # 1. Resolve tokens
    tokens = []
    token_to_symbol = {}
    for sym in STOCKS:
        try:
            res = broker.market_data_provider.search_instruments(query=f'{sym}-EQ')
            if res:
                token = res[0]['token']
                tokens.append(token)
                token_to_symbol[token] = sym
                logger.info(f"Resolved {sym} -> {token}")
        except Exception as e:
            logger.error(f"Could not resolve {sym}: {e}")

    if not tokens:
        logger.error("No valid tokens found. Exiting.")
        sys.exit(1)

    # 2. Start WebSocket
    compiler.subscribe(tokens)
    
    last_processed_minute = -1
    
    # 3. Main Loop
    while True:
        try:
            now = datetime.now()
            
            # Manage Open Trades every second
            current_prices = {}
            for t in tokens:
                if t in compiler.market_depth and compiler.market_depth[t]['lp'] > 0:
                    current_prices[token_to_symbol[t]] = compiler.market_depth[t]['lp']
            
            trader.manage_open_trades(current_prices)
            
            # 5-Minute Boundary Execution
            is_boundary = (now.minute % 5 == 0) and (now.second >= 2)
            if is_boundary and now.minute != last_processed_minute:
                last_processed_minute = now.minute
                logger.info(f"========== 5 MINUTE BOUNDARY {now.strftime('%H:%M:00')} ==========")
                
                footprints = compiler.get_compiled_footprints()
                
                for token, sym in token_to_symbol.items():
                    # We need 5m historical data for TSF
                    try:
                        # Fetch last 14 candles of 5m from REST for TSF
                        hist = broker.historical_provider.get_historical(
                            exchange="NSE",
                            token=str(token),
                            interval="FIVE_MINUTE",
                            start_time=now - timedelta(minutes=75), # 15 candles
                            end_time=now
                        )
                        if hist is None or hist.empty:
                            logger.warning(f"No historical data for {sym}")
                            continue
                            
                        # Evaluate footprint + TSF
                        fp_data = footprints.get(token, {})
                        current_price = current_prices.get(sym, float(hist.iloc[-1]['close']))
                        
                        signal, reason = evaluator.evaluate_5m_boundary(
                            symbol=sym,
                            current_price=current_price,
                            historical_df=hist,
                            footprint_data=fp_data
                        )
                        
                        if signal in ["BUY", "SELL"]:
                            logger.info(f"🔥 {sym} SIGNAL: {signal} - {reason}")
                            trader.execute_entry(signal, sym, current_price, reason)
                        else:
                            logger.info(f"{sym}: {reason}")
                            
                    except Exception as e:
                        logger.error(f"Error evaluating {sym}: {e}")

                # Reset footprint matrix for the next 5m candle
                compiler.reset_for_new_candle()
                
            time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Stopping Engine 3...")
            break
        except Exception as e:
            logger.error(f"Loop error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
