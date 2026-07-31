import os
import sys
import time
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

# Ensure project root is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from astra_bot.core.logger import get_logger
from astra_bot.config.settings import BROKER
from astra_bot.core.data_fetcher import DataFetcher
from astra_bot.strategy.kronos_predictor import load_kronos_model
from astra_bot.strategy.signals import generate_kronos_signal

logger = get_logger("historical_simulation")

def run_simulation(days_back=13, momentum_threshold=0.0025):
    logger.info("==========================================================")
    logger.info(f"STARTING KRONOS HISTORICAL ACCURACY BACKTESTER ({days_back} DAYS)")
    logger.info("==========================================================")
    
    # 1. Initialize API Adapter and load model
    try:
        if BROKER.upper() == "SHOONYA":
            from astra_bot.core.shoonya_adapter import ShoonyaAdapter
            broker = ShoonyaAdapter()
        else:
            from astra_bot.core.angel_one_adapter import AngelOneAdapter
            broker = AngelOneAdapter()
        fetcher = DataFetcher(broker)
    except Exception as e:
        logger.critical(f"Initialization failed: {e}")
        return
        
    logger.info("Loading Kronos Model...")
    load_kronos_model()
    
    # 2. Fetch cash index token (NSE spot Nifty)
    token, name, exchange = fetcher.get_cash_index_token()
    
    # Auto-scaling date search to guarantee enough candles for bootstrapping the first sliding window
    # We fetch days_back + 10 to ensure we have a rich history for June/July
    days_back_search = days_back + 10
    df = pd.DataFrame()
    for attempt in range(5):
        logger.info(f"Fetching historical 5-minute candles for {name} (days_back={days_back_search})...")
        df = fetcher.get_historical_candles(exchange, token, "FIVE_MINUTE", days_back=days_back_search)
        if not df.empty:
            logger.info(f"Fetched {len(df)} candles.")
            if len(df) >= 512 + 75:
                break
        days_back_search += 5
        
    if df.empty or len(df) < 512:
        logger.error(f"Not enough historical data to run simulation. Loaded {len(df)} candles, need at least 512.")
        return
        
    logger.info(f"Loaded {len(df)} candles. Sorting chronologically...")
    df = df.sort_values('timestamp').reset_index(drop=True)
    
    # Target period boundary: July 6, 2026
    target_start_date = pd.Timestamp("2026-07-06")
    
    # Bootstrap initial sliding window (first 512 candles)
    fetcher.candle_window.clear()
    for i in range(512):
        fetcher.candle_window.append(df.iloc[i].to_dict())
        
    predictions_count = 0
    correct_directions = 0
    absolute_errors = []
    
    total_momentum_signals = 0
    correct_momentum_signals = 0
    
    logger.info("Running sequential fast predictions from July 6 onwards...")
    start_time = time.time()
    
    for step in range(512, len(df)):
        current_candle = df.iloc[step]
        candle_time = current_candle['timestamp']
        close_price = current_candle['close']
        
        # Filter: Only run predictions for candles starting from July 6, 2026
        c_time_naive = pd.Timestamp(candle_time).tz_localize(None)
        if c_time_naive < target_start_date:
            # Slide window and continue without predicting
            fetcher.append_live_candle(current_candle.to_dict())
            continue
            
        # Check if candle is within trading hours (09:15 to 15:30)
        c_time = candle_time.time()
        if not (datetime.strptime("09:15", "%H:%M").time() <= c_time <= datetime.strptime("15:30", "%H:%M").time()):
            fetcher.append_live_candle(current_candle.to_dict())
            continue
            
        # Verify if we have a future candle to compare against
        if step + 1 >= len(df):
            fetcher.append_live_candle(current_candle.to_dict())
            continue
            
        actual_next_candle = df.iloc[step + 1]
        actual_next_close = actual_next_candle['close']
        
        # Get current sliding window df and run Kronos forecast
        window_df = fetcher.get_window_df()
        signal = generate_kronos_signal(window_df)
        
        predicted_close = signal["predicted_close"]
        predicted_change = predicted_close - close_price
        predicted_change_pct = predicted_change / close_price
        
        actual_change = actual_next_close - close_price
        actual_change_pct = actual_change / close_price
        
        # Directional Hit check
        is_correct_dir = False
        if (predicted_change > 0 and actual_change > 0) or (predicted_change < 0 and actual_change < 0):
            is_correct_dir = True
            correct_directions += 1
            
        predictions_count += 1
        
        # Absolute forecast error in points
        abs_error = abs(predicted_close - actual_next_close)
        absolute_errors.append(abs_error)
        
        # Momentum check
        is_momentum_signal = abs(predicted_change_pct) >= momentum_threshold
        if is_momentum_signal:
            total_momentum_signals += 1
            if is_correct_dir:
                correct_momentum_signals += 1
                
        # Log individual row details
        status_str = "WIN" if is_correct_dir else "LOSS"
        momentum_tag = " [MOMENTUM]" if is_momentum_signal else ""
        
        logger.info(
            f"[{candle_time}] "
            f"Close: {close_price:.2f} | "
            f"Pred Close: {predicted_close:.2f} ({predicted_change_pct*100:+.3f}%){momentum_tag} | "
            f"Actual Close: {actual_next_close:.2f} ({actual_change_pct*100:+.3f}%) | "
            f"Err: {abs_error:.2f} pts | Result: {status_str}"
        )
        
        # Slide window
        fetcher.append_live_candle(current_candle.to_dict())
        
    inference_duration = time.time() - start_time
    logger.info(f"Sequential prediction completed! Time taken: {inference_duration:.2f} seconds ({inference_duration / predictions_count:.3f}s per candle).")
    
    # --- 6. Print Consolidated Backtest Report ---
    logger.info("\n")
    logger.info("==========================================================")
    logger.info("           KRONOS AI DIRECT VALIDATION REPORT             ")
    logger.info("==========================================================")
    logger.info(f"Target Period:        July 6, 2026 to July 17, 2026")
    logger.info(f"Total Candles Tested: {predictions_count}")
    
    dir_acc = (correct_directions / predictions_count * 100) if predictions_count > 0 else 0.0
    logger.info(f"Direction Hit Rate:   {dir_acc:.2f}% ({correct_directions}/{predictions_count})")
    
    mean_abs_error = np.mean(absolute_errors) if absolute_errors else 0.0
    logger.info(f"Mean Absolute Error:  {mean_abs_error:.2f} Index Points")
    
    logger.info("----------------------------------------------------------")
    logger.info(f"Momentum Signal Threshold: {momentum_threshold * 100:.2f}%")
    logger.info(f"Total Momentum Signals:   {total_momentum_signals}")
    mom_acc = (correct_momentum_signals / total_momentum_signals * 100) if total_momentum_signals > 0 else 0.0
    logger.info(f"Momentum Signal Win Rate: {mom_acc:.2f}% ({correct_momentum_signals}/{total_momentum_signals})")
    logger.info("==========================================================")

if __name__ == "__main__":
    # Run backtest for last 13 calendar days (covers July 6 to July 17)
    run_simulation(days_back=13)
