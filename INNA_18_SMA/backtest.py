import numpy as np
import pandas as pd
import glob
import os

def backtest_inna_18_sma(df):
    """
    df requires columns: 'open', 'high', 'low', 'close', 'volume'
    Timeframe: Intraday (e.g., 5m or 15m)
    """
    if len(df) < 21:
        return df
        
    # 1. Technical Indicators
    df["18_SMA"] = df["close"].rolling(window=18).mean()
    df["20_Avg_Volume"] = df["volume"].rolling(window=20).mean()

    # 2. State Rules (Price location relative to 18 SMA)
    df["close_above_SMA"] = df["close"] > df["18_SMA"]
    df["open_above_SMA"] = df["open"] > df["18_SMA"]
    df["low_above_SMA"] = df["low"] > df["18_SMA"]
    df["high_above_SMA"] = df["high"] > df["18_SMA"]

    # Candle completely above SMA (Body and Wicks)
    df["candle_fully_above"] = (
        df["low_above_SMA"] & df["close_above_SMA"] & df["open_above_SMA"]
    )

    # 3. Signals Engine
    df["Signal"] = 0
    df["Buy_Trigger_Price"] = np.nan
    df["Initial_Stop_Loss"] = np.nan

    for i in range(20, len(df)):
        # Rule: Two consecutive candles completely above 18 SMA
        two_candles_consecutive = (
            df["candle_fully_above"].iloc[i - 1] and df["candle_fully_above"].iloc[i - 2]
        )

        # Rule: Volume Filter (Higher than 20-period average)
        volume_filter = df["volume"].iloc[i - 1] > df["20_Avg_Volume"].iloc[i - 1]

        # Trigger Long Setup Condition
        if two_candles_consecutive and volume_filter:
            # Set entry order to buy at the breakout of the second candle's high
            df.at[df.index[i], "Signal"] = 1
            df.at[df.index[i], "Buy_Trigger_Price"] = df["high"].iloc[i - 1]
            
            # Rule: Stop Loss at recent Swing Low (trailing 5 periods)
            df.at[df.index[i], "Initial_Stop_Loss"] = df["low"].iloc[i - 5 : i].min()

        # Rule: Hard Exit Condition (Any close BELOW the 18 SMA)
        elif df["close"].iloc[i] < df["18_SMA"].iloc[i]:
            df.at[df.index[i], "Signal"] = -1

    return df

def run_backtest():
    print("Loading data...")
    parquet_file = r"C:\Users\Amarjeet Singh\quant-bot\data\underlying_candles_NIFTY.parquet"
    if not os.path.exists(parquet_file):
        print("Data not found")
        return
        
    df = pd.read_parquet(parquet_file)
    print(f"Loaded {len(df)} rows.")
    
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df.set_index('timestamp', inplace=True)
    
    # Data is already 5m, we can optionally resample or use as is
    # Let's resample to 15m for fewer trades, or use 5m directly
    # 5m gives 512 rows, let's use it directly
    df = df.sort_index()
    
    res = backtest_inna_18_sma(df)
    
    # Calculate performance
    in_trade = False
    entry_price = 0
    pnl = 0
    trades = 0
    
    active_sl = 0
    for idx, row in res.iterrows():
        if not in_trade and row['Signal'] == 1:
            in_trade = True
            entry_price = row['Buy_Trigger_Price'] # assuming we get filled at stop limit
            active_sl = row['Initial_Stop_Loss']
            print(f"[{idx}] BUY ENTRY triggered at {entry_price:.2f}, SL: {active_sl:.2f}")
        elif in_trade:
            # Check stop loss first
            if row['low'] < active_sl:
                in_trade = False
                exit_price = active_sl # assuming slippage hits exactly at SL
                trade_pnl = exit_price - entry_price
                pnl += trade_pnl
                trades += 1
                print(f"[{idx}] STOP LOSS HIT at {exit_price:.2f} | PnL: {trade_pnl:.2f}")
            elif row['Signal'] == -1:
                in_trade = False
                exit_price = row['close']
                trade_pnl = exit_price - entry_price
                pnl += trade_pnl
                trades += 1
                print(f"[{idx}] SELL EXIT at {exit_price:.2f} | PnL: {trade_pnl:.2f}")
                
    print(f"Total Trades: {trades}")
    print(f"Total PnL: {pnl:.2f}")

if __name__ == "__main__":
    run_backtest()
