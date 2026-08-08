"""
Historical Data Manager for Stock Order Flow.
Fetches 3-minute OHLCV historical data at boot to warm up EMAs and persist them.
"""
import os
import time
import glob
from datetime import datetime, timedelta
import pandas as pd
from common.logger import get_logger
from stock_order_flow.bar_book import floor_3m

logger = get_logger("historical_data")

HISTORY_DIR = os.path.join(os.path.dirname(__file__), "data", "history")

def _get_start_and_end_dates():
    # We want ~3-4 trading days of data to guarantee we have 250+ bars.
    # To be safe against weekends, we go back 6 calendar days.
    now = datetime.now()
    end = now
    start = now - timedelta(days=6)
    return start.timestamp(), end.timestamp()

def _load_local_parquet(symbol: str) -> pd.DataFrame:
    path = os.path.join(HISTORY_DIR, f"{symbol}_history.parquet")
    if os.path.exists(path):
        try:
            return pd.read_parquet(path)
        except Exception as e:
            logger.error(f"Failed to read parquet for {symbol}: {e}")
    return pd.DataFrame()

def _save_local_parquet(symbol: str, df: pd.DataFrame):
    if not os.path.exists(HISTORY_DIR):
        os.makedirs(HISTORY_DIR, exist_ok=True)
        
    path = os.path.join(HISTORY_DIR, f"{symbol}_history.parquet")
    # Prune to keep only the last 5 days
    cutoff = datetime.now() - timedelta(days=6)
    df = df[df['timestamp'] >= cutoff].copy()
    
    # Sort and drop duplicates just in case
    df = df.sort_values("timestamp").drop_duplicates(subset=["timestamp"], keep="last")
    df.to_parquet(path, index=False)
    
def fetch_and_seed_history(broker, tokens: list, token_map: dict) -> dict:
    """
    Returns a dict mapping symbol -> DataFrame of historical bars.
    Called once at boot to fill gaps.
    """
    logger.info("Starting boot-time historical data gap-fill sync...")
    start_ts, end_ts = _get_start_and_end_dates()
    
    result = {}
    
    for token in tokens:
        symbol = token_map[str(token)]
        local_df = _load_local_parquet(symbol)
        
        last_local_ts = start_ts
        if not local_df.empty and 'timestamp' in local_df.columns:
            last_dt = local_df['timestamp'].max()
            last_local_ts = last_dt.timestamp()
            
        fetch_start = max(last_local_ts, start_ts)
        # Add 1 second to fetch_start to avoid duplicate of the exact last bar
        fetch_start += 1.0
        
        new_df = pd.DataFrame()
        if end_ts - fetch_start > 180: # At least a 3-minute gap
            logger.info(f"Fetching historical REST gap for {symbol} from {datetime.fromtimestamp(fetch_start)}")
            try:
                res = broker._api.get_time_price_series(exchange='NSE', token=str(token), starttime=fetch_start, endtime=end_ts, interval='3')
                if res and isinstance(res, list):
                    rows = []
                    for r in res:
                        if 'time' not in r: continue
                        # time format: '12-07-2023 10:15:00'
                        dt = datetime.strptime(r['time'], "%d-%m-%Y %H:%M:%S")
                        rows.append({
                            'timestamp': dt,
                            'open': float(r['into']),
                            'high': float(r['inth']),
                            'low': float(r['intl']),
                            'close': float(r['intc']),
                            'volume': float(r['intv'])
                        })
                    if rows:
                        new_df = pd.DataFrame(rows)
            except Exception as e:
                logger.error(f"Error fetching history for {symbol}: {e}")
                
            time.sleep(0.5) # Anti-spam delay
            
        # Combine
        combined_df = local_df
        if not new_df.empty:
            combined_df = pd.concat([local_df, new_df], ignore_index=True)
            
        if not combined_df.empty:
            _save_local_parquet(symbol, combined_df)
            result[str(token)] = combined_df
            
    return result
