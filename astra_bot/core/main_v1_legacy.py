import os
import sys
import time
from datetime import datetime, timedelta, time as dt_time
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

from astra_bot.config.market_calendar import (
    is_market_open,
    is_pre_market,
    is_trading_day,
    sleep_until_next_premarket
)

logger = get_logger("astra_bot_main")

# --- Signal color helpers (console only; file log stays plain) ---
_ANSI = {"NEUTRAL": "\033[33m", "CALL": "\033[32m", "PUT": "\033[31m", "RESET": "\033[0m"}

def log_signal(signal_type: str, message: str):
    """Log a trading signal with color on console; plain in file."""
    color = _ANSI.get(signal_type.upper(), "")
    logger.info(f"{color}{message}{_ANSI['RESET']}")

def _fmt_vol(v: int) -> str:
    """Format volume TradingView-style: 5.02M, 418.3K, or raw int."""
    if v >= 1_000_000:
        return f"{v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"{v / 1_000:.1f}K"
    return str(int(v))

def perform_premarket_handshake(broker, fetcher):
    """
    Executes the 08:55 AM pre-market setup:
    1. Updates constituent token mappings dynamically via update_constituents.py.
    2. Subscribes WebSocket feed to constituent tokens for live synthetic volume.
    3. Re-authenticates broker OAuth session tokens.
    4. Pre-loads Kronos model weights into RAM and pre-warms PyTorch.
    5. Bootstraps 5-minute candle sliding window from Parquet cache / API.
    """
    logger.info("==========================================================")
    logger.info("08:55 AM IST PRE-MARKET WAKEUP: Initiating Handshake & Setup")
    logger.info("==========================================================")
    
    # 1. Update dynamic constituents and subscribe WebSocket feed
    try:
        from astra_bot.scripts.update_constituents import update_constituents
        logger.info("Running pre-market constituent token update...")
        update_constituents()
        
        constituents = fetcher.get_active_constituents()
        if constituents and hasattr(broker.market_data_provider, "register_constituent_tokens"):
            tokens = list(constituents.values())
            broker.market_data_provider.register_constituent_tokens(tokens)
            broker.market_data_provider._ensure_ws_started()
            broker.market_data_provider.subscribe(tokens, "NSE")
            logger.info(f"Subscribed {len(tokens)} constituent tokens to WebSocket for synthetic volume tracking.")
    except Exception as e:
        logger.error(f"Pre-market constituent setup warning: {e}")

    # 2. Session renewal
    try:
        if hasattr(broker, "session_provider") and hasattr(broker.session_provider, "_restore_or_login"):
            logger.info("Refreshing broker authentication session tokens...")
            broker.session_provider._restore_or_login()
    except Exception as e:
        logger.error(f"Pre-market session renewal warning: {e}")
        
    # 3. Pre-load model weights
    try:
        logger.info("Pre-loading Kronos Model weights into RAM...")
        load_kronos_model()
    except Exception as e:
        logger.error(f"Pre-market model loading error: {e}")
        
    # 4. Bootstrap candle window
    try:
        logger.info("Bootstrapping 5-minute candle sliding window...")
        fetcher.bootstrap_candle_window()
    except Exception as e:
        logger.error(f"Pre-market candle window bootstrap warning: {e}")
        
    logger.info("Pre-market setup complete! Standing by until market opens at 09:15:00 AM IST...")

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
        
    # 3. Handle startup timing state
    now_dt = datetime.now()
    if is_market_open(now_dt):
        logger.info("Market is open. Bootstrapping candle window...")
        fetcher.bootstrap_candle_window()
        try:
            constituents = fetcher.get_active_constituents()
            if constituents and hasattr(broker.market_data_provider, "register_constituent_tokens"):
                tokens = list(constituents.values())
                broker.market_data_provider.register_constituent_tokens(tokens)
                broker.market_data_provider.subscribe(tokens, "NSE")
                logger.info(f"Subscribed {len(tokens)} constituent tokens to WebSocket for synthetic volume tracking.")
        except Exception as e:
            logger.error(f"Startup constituent subscription warning: {e}")
    elif is_pre_market(now_dt):
        perform_premarket_handshake(broker, fetcher)
    else:
        logger.info("Market is currently closed. Will enter deep sleep until pre-market.")
        
    logger.info("Bot is ready and running!")
    
    last_processed_candle_time = None
    last_historical_fetch_time = 0.0
    cached_current_df = pd.DataFrame()
    pending_synthetic_vol = 0
    compiled_candle_time = None
    
    while True:
        try:
            now = datetime.now()
            
            # Check market hours
            if not is_market_open(now):
                sleep_until_next_premarket()
                # Woke up at 08:55 AM pre-market on next trading day!
                perform_premarket_handshake(broker, fetcher)
                
                # Wait until 09:15:02 AM IST to start trading loop
                market_open_dt = datetime.combine(datetime.now().date(), dt_time(9, 15, 2))
                sleep_to_open = (market_open_dt - datetime.now()).total_seconds()
                if sleep_to_open > 0:
                    logger.info(f"Waiting for market open ({sleep_to_open:.1f}s until 09:15:02 AM IST)...")
                    time.sleep(sleep_to_open)
                continue
                
            # Ensure candle window is bootstrapped (if window is incomplete)
            if len(fetcher.candle_window) < 10:
                logger.info("Candle sliding window is incomplete. Bootstrapping...")
                if not fetcher.bootstrap_candle_window():
                    logger.warning("Failed to bootstrap candle window. Retrying in 30 seconds...")
                    time.sleep(30)
                    continue
                
            # Fetch active instrument (Nifty/Sensex)
            name, exch_seg = fetcher.get_active_instrument()
            token, _, exchange = fetcher.get_cash_index_token()
            
            # --- 1. OPEN TRADE MANAGEMENT & MONITORING ---
            if trader.trade_context is not None and trader.trade_context.has_open_trade():
                # Fetch index LTP for exit evaluation
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
            # At 09:30:00, the candle that just closed on exchange is timestamped 09:25:00 (09:30 - 5 minutes)
            current_minute = (now.minute // 5) * 5
            current_boundary_time = now.replace(minute=current_minute, second=0, microsecond=0)
            target_closed_candle_time = current_boundary_time - timedelta(minutes=5)
            
            # If we haven't processed this closed candle yet
            if last_processed_candle_time is None or target_closed_candle_time > last_processed_candle_time:
                # STEP A: Compile & Log WebSocket Synthetic Volume FIRST (strictly ONCE per candle boundary)
                if compiled_candle_time != target_closed_candle_time:
                    compiled_candle_time = target_closed_candle_time
                    if hasattr(broker.market_data_provider, "get_and_reset_synthetic_volume"):
                        pending_synthetic_vol = broker.market_data_provider.get_and_reset_synthetic_volume()
                        logger.info(f"[SYNTHETIC VOLUME] Compiled live 5-minute volume for closed candle [{target_closed_candle_time.strftime('%H:%M:%S')}]: {_fmt_vol(pending_synthetic_vol)} shares.")
                
                # STEP B: Fetch latest price candle from REST API SECOND
                recent_df = fetcher.get_historical_candles(exchange, token, "FIVE_MINUTE", days_back=1)
                
                if recent_df.empty:
                    logger.warning("Could not fetch index candle from REST API. Retrying in 10 seconds...")
                    time.sleep(10)
                    continue
                    
                recent_df = recent_df.sort_values('timestamp').reset_index(drop=True)
                latest_row = recent_df.iloc[-1]
                latest_candle_time = latest_row['timestamp']
                
                # Verify that the REST API candle matches or exceeds our target closed candle
                if last_processed_candle_time is None or latest_candle_time > last_processed_candle_time:
                    candle_dict = latest_row.to_dict()
                    if pending_synthetic_vol > 0:
                        candle_dict['volume'] = pending_synthetic_vol
                        pending_synthetic_vol = 0 # reset pending
                        
                    logger.info(f"Combining Price & Volume into sliding window for candle [{latest_candle_time}] (Close: {candle_dict.get('close', 0):.2f} | Vol: {candle_dict.get('volume', 0):,})...")
                    fetcher.append_live_candle(candle_dict)
                    
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
                        log_signal(signal["type"], f"!!! SIGNAL GENERATED: Buy {signal['type']} Option !!!")
                        
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
                        log_signal(signal["type"], f"Triggering Options Execution for {signal['type']} setup...")
                        trader._execute_trade(signal, df)
                    else:
                        chg_pct = signal.get("change_pct", 0.0) * 100
                        cur_close = signal.get("current_close", 0.0)
                        pred_close = signal.get("predicted_close", 0.0)
                        log_signal("NEUTRAL", f"Signal is NEUTRAL ({chg_pct:+.3f}% | Current: {cur_close:.2f} -> Pred: {pred_close:.2f}). Standing by.")
                else:
                    logger.info(f"REST API latest candle is still at {latest_candle_time}. Retrying in 10 seconds...")
                    time.sleep(10)
                    continue
                
            # After market closes (loop exits is_market_open), run EOD REST backfill
            if not is_market_open(datetime.now()):
                try:
                    inactive_index = "SENSEX" if name == "NIFTY" else "NIFTY"
                    logger.info(f"Market closed. Running EOD REST backfill for active ({name}) and inactive ({inactive_index}) indices...")
                    fetcher.rest_backfill_today_candles(name)           # Fix any NaN / corruption in active index
                    fetcher.rest_backfill_today_candles(inactive_index) # Fill today's candles for inactive index
                except Exception as e:
                    logger.error(f"EOD backfill error: {e}")

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
