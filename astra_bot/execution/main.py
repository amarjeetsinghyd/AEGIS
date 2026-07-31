import os
import sys
import time
from datetime import datetime, time as dt_time
import pandas as pd

# Ensure project root is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from astra_bot.core.logger import get_logger
from astra_bot.core.data_fetcher import DataFetcher
from astra_bot.execution.execution_manager import ExecutionManager
from astra_bot.strategy.kronos_predictor import load_kronos_model
from astra_bot.strategy.signals import generate_kronos_signal
from astra_bot.config.settings import BROKER, COOLDOWN_MINUTES

logger = get_logger("astra_bot_main")

# Market Hours: 09:15 to 15:30 IST
MARKET_OPEN = dt_time(9, 15)
MARKET_CLOSE = dt_time(15, 30)

def is_market_open() -> bool:
    """Checks if the current time is within Indian Market Hours (09:15 - 15:30) on weekdays."""
    now_dt = datetime.now()
    if now_dt.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        return False
    now = now_dt.time()
    return MARKET_OPEN <= now <= MARKET_CLOSE

def wait_until_next_candle(interval_minutes=5):
    """Sleeps until the next interval boundary (e.g., 09:20:00, 09:25:00)."""
    now = datetime.now()
    minutes_to_sleep = interval_minutes - (now.minute % interval_minutes)
    seconds_to_sleep = (minutes_to_sleep * 60) - now.second - (now.microsecond / 1000000.0)
    
    # Pad by 2 seconds to ensure the candle has closed and broker data is generated
    sleep_time = max(0.5, seconds_to_sleep + 2.0)
    logger.info(f"Sleeping for {sleep_time:.2f}s until next candle boundary...")
    time.sleep(sleep_time)

def main():
    logger.info("==============================================")
    logger.info("STARTING KRONOS OPTIONS MOMENTUM BOT (PHASE 1)")
    logger.info("==============================================")
    
    # 1. Initialize broker and execution engine
    try:
        if BROKER.upper() == "SHOONYA":
            from astra_bot.core.shoonya_adapter import ShoonyaAdapter
            broker = ShoonyaAdapter()
        else:
            from astra_bot.core.angel_one_adapter import AngelOneAdapter
            broker = AngelOneAdapter()
        fetcher = DataFetcher(broker)
        trader = ExecutionManager(broker, fetcher)
    except Exception as e:
        logger.critical(f"Initialization failed: {e}")
        sys.exit(1)
        
    # 2. Pre-load the Kronos model to avoid inference latency on the first tick
    logger.info("Pre-loading Kronos Model weights...")
    try:
        load_kronos_model()
    except Exception as e:
        logger.critical(f"Failed to pre-load Kronos Model: {e}")
        sys.exit(1)
        
    # 3. Bootstrap the 5-minute candle sliding window (only if market is currently open)
    if is_market_open():
        logger.info("Bootstrapping 5-minute candle sliding window...")
        try:
            fetcher.bootstrap_candle_window()
        except Exception as e:
            logger.error(f"Failed to bootstrap candle sliding window: {e}")
            sys.exit(1)
    else:
        logger.info("Market is closed. Skipping startup bootstrap. It will bootstrap automatically when the market opens.")
        
    logger.info("Bot is ready and running!")
    
    last_processed_candle_time = None
    last_historical_fetch_time = 0.0
    cached_current_df = pd.DataFrame()
    
    while True:
        try:
            now = datetime.now()
            
            # Check market hours
            if not is_market_open():
                logger.info("Market is closed. Sleeping for 60 seconds...")
                time.sleep(60)
                continue
                
            # Ensure candle window is bootstrapped (if Sunday bootstrap failed)
            if len(fetcher.candle_window) < 200:
                logger.info("Candle sliding window is empty or incomplete. Bootstrapping...")
                if not fetcher.bootstrap_candle_window():
                    logger.warning("Failed to bootstrap candle window. Retrying in 30 seconds...")
                    time.sleep(30)
                    continue
                
            # Fetch active instrument (Nifty/Sensex)
            name, exch_seg = fetcher.get_active_instrument()
            token, _, exchange = fetcher.get_cash_index_token()
            
            # --- 1. MANAGING OPEN POSITIONS (Every 2 seconds) ---
            if trader.trade_context and trader.trade_context.status == "OPEN":
                # Get index LTP
                index_quote = broker.market_data_provider.get_quote(exchange, token)
                index_ltp = index_quote.get("ltp", 0.0)
                
                # Fetch option chain to update option cache
                weekly_opts = fetcher.get_weekly_option_tokens()
                if not weekly_opts.empty:
                    # Query quotes for all open legs to feed trailing SL
                    leg_quotes = {}
                    for leg in trader.trade_context.legs:
                        if leg.status == "OPEN":
                            quote = broker.market_data_provider.get_quote(exch_seg, leg.token)
                            if quote.get("ltp", 0.0) > 0:
                                leg_quotes[leg.token] = quote["ltp"]
                    
                    trader.update_option_cache(leg_quotes)
                    
                # Fetch recent candles for stop loss calculation (rate-limited to once per 30 seconds)
                import time as pytime
                now_time = pytime.time()
                if now_time - last_historical_fetch_time >= 30.0 or cached_current_df.empty:
                    last_historical_fetch_time = now_time
                    cached_current_df = fetcher.get_historical_candles(exchange, token, "FIVE_MINUTE", days_back=1)
                current_df = cached_current_df
                
                # Update trailing stops and manage exits
                trader.manage_open_trade(index_ltp, current_df)
                
                # Sleep briefly
                time.sleep(2)
                continue
                
            # --- 2. EVALUATING NEW SIGNAL (On Candle Close) ---
            # Determine current 5-min candle timestamp
            current_minute = (now.minute // 5) * 5
            current_candle_time = now.replace(minute=current_minute, second=0, microsecond=0)
            
            # If we haven't processed this candle yet
            if last_processed_candle_time is None or current_candle_time > last_processed_candle_time:
                logger.info(f"New 5-minute candle closed at {current_candle_time.strftime('%H:%M:%S')}. Fetching latest closed candle...")
                
                # Fetch the latest candles to retrieve the single newest closed candle
                recent_df = fetcher.get_historical_candles(exchange, token, "FIVE_MINUTE", days_back=1)
                
                if recent_df.empty:
                    logger.warning("Could not fetch index candle from REST API. Retrying in 10 seconds...")
                    time.sleep(10)
                    continue
                    
                recent_df = recent_df.sort_values('timestamp').reset_index(drop=True)
                latest_row = recent_df.iloc[-1]
                latest_candle_time = latest_row['timestamp']
                
                # Verify that it is a new candle and add it to the sliding window
                if last_processed_candle_time is None or latest_candle_time > last_processed_candle_time:
                    logger.info(f"Appending new closed candle at {latest_candle_time} to sliding window...")
                    fetcher.append_live_candle(latest_row.to_dict())
                    
                    # Convert window to DataFrame
                    df = fetcher.get_window_df()
                    
                    if df.empty or len(df) < 10:
                        logger.warning("Not enough sliding window historical data. Retrying in 10 seconds...")
                        time.sleep(10)
                        continue
                        
                    # Generate Kronos prediction signal
                    signal = generate_kronos_signal(df)
                    last_processed_candle_time = latest_candle_time
                
                    if signal["type"] in ["CALL", "PUT"]:
                        logger.info(f"!!! SIGNAL GENERATED: Buy {signal['type']} Option !!!")
                        
                        # Fetch option chain and update cache
                        weekly_opts = fetcher.get_weekly_option_tokens()
                        if not weekly_opts.empty:
                            # Fetch quote for ATM options to populate cache
                            strike_step = 100 if name == "SENSEX" else 50
                            index_close = df.iloc[-1]['close']
                            atm_strike = round(index_close / strike_step) * strike_step
                            
                            # Filter to ATM and near OTM option tokens to pre-fetch quotes
                            opt_pattern = r'C\d+$|CE$' if signal["type"] == "CALL" else r'P\d+$|PE$'
                            filtered_opts = weekly_opts[weekly_opts['symbol'].str.contains(opt_pattern, regex=True)].head(15)
                            
                            opt_quotes = {}
                            for _, row in filtered_opts.iterrows():
                                tok = str(row['token'])
                                q = broker.market_data_provider.get_quote(exch_seg, tok)
                                if q.get("ltp", 0.0) > 0:
                                    opt_quotes[tok] = q["ltp"]
                                    
                            trader.update_option_cache(opt_quotes)
                            
                        # Execute options trade immediately
                        logger.info(f"Triggering Options Execution for {signal['type']} setup...")
                        trader._execute_trade(signal, df)
                    else:
                        chg_pct = signal.get("change_pct", 0.0) * 100
                        cur_close = signal.get("current_close", 0.0)
                        pred_close = signal.get("predicted_close", 0.0)
                        logger.info(f"Signal is NEUTRAL ({chg_pct:+.3f}% | Current: {cur_close:.2f} -> Pred: {pred_close:.2f}). Standing by.")
                else:
                    logger.info(f"REST API latest candle is still at {latest_candle_time}. Retrying in 10 seconds...")
                    time.sleep(10)
                    continue
                
            # Wait until the next 5-minute candle boundary
            wait_until_next_candle(interval_minutes=5)
            
        except KeyboardInterrupt:
            logger.info("Shutting down Bot gracefully...")
            break
        except Exception as e:
            logger.error(f"Error in Bot Main loop: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
