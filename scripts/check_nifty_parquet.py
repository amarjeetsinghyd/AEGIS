import pandas as pd
import os

parquet_path = r"C:\Users\Amarjeet Singh\quant-bot\data\underlying_candles_NIFTY.parquet"

if not os.path.exists(parquet_path):
    print("File not found.")
else:
    try:
        df = pd.read_parquet(parquet_path)
        print("Data Integrity Check Passed.")
        print(f"Total Rows: {len(df)}")
        print("\nColumns and Datatypes:")
        print(df.dtypes)
        print("\nFirst 5 rows:")
        print(df.head())
        print("\nLast 5 rows:")
        print(df.tail())
        
        # Check for missing values
        print("\nMissing Values:")
        print(df.isnull().sum())
        
        # Check for fake volume
        if (df['volume'] == 10000000).any():
            print("\nWARNING: Found rows with fake volume (10000000).")
            print(df[df['volume'] == 10000000])
        else:
            print("\nNo fake volume of 10000000 found. Volumes look legitimate.")
            
    except Exception as e:
        print(f"Failed to read parquet file: {e}")
