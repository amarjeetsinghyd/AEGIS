import os
import sys
import pandas as pd
from datetime import datetime, timedelta

# Ensure project root is in sys.path
BASE_DIR = r"C:\Kronos_Testing"
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from astra_bot.core.angel_one_adapter import AngelOneAdapter
from astra_bot.core.data_fetcher import DataFetcher

def download_data():
    broker = AngelOneAdapter()
    fetcher = DataFetcher(broker)
    
    # We want NIFTY Cash Index
    token, name, exchange = fetcher.get_cash_index_token()
    print(f"Fetching {name} Cash Index price data...")
    
    # Fetch 22 days back of ONE_MINUTE data (covers June 26 to July 17)
    days_back = 22
    price_df = fetcher.get_historical_candles(exchange, token, "ONE_MINUTE", days_back=days_back)
    
    if price_df.empty:
        print("Error: Could not fetch Nifty price data.")
        return
        
    print(f"Loaded {len(price_df)} price candles. Start: {price_df['timestamp'].min()}, End: {price_df['timestamp'].max()}")
    
    # Clear date slice logic since we want the whole history
    min_timestamp = price_df['timestamp'].min()
    
    constituents = fetcher.get_active_constituents()
    print(f"Building Synthetic Volume from {len(constituents)} constituents starting exactly at {min_timestamp}...")
    
    # Fetch constituent volumes
    vol_df = fetcher._fetch_constituent_volume(exact_fromdate=min_timestamp)
    
    if vol_df is not None:
        price_df = price_df.set_index('timestamp')
        price_df = price_df.join(vol_df, how='left')
        price_df['volume'] = price_df['synthetic_volume'].fillna(0).astype(int)
        price_df = price_df.drop(columns=['synthetic_volume'])
        price_df = price_df.reset_index()
        print(f"Synthetic Volume injected! Avg volume per candle: {price_df['volume'].mean():.2f}")
    else:
        print("Warning: Could not build synthetic volume.")
        
    csv_path = r"C:\Kronos_Testing\astra_bot\data\scratch\nifty_1m_data.csv"
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    price_df.to_csv(csv_path, index=False)
    print(f"Saved synthetic Nifty 1-minute data to {csv_path}")

if __name__ == "__main__":
    download_data()
