import os
import glob
import pandas as pd
import numpy as np

def main():
    nifty_l2_dir = "/home/opc/AEGIS/data/l2_raw/Nifty"
    files_12 = sorted(glob.glob(f"{nifty_l2_dir}/aegis_l2_raw_2026-10-12_part_*.parquet"))
    files_13 = sorted(glob.glob(f"{nifty_l2_dir}/aegis_l2_raw_2026-10-13_part_*.parquet"))
    
    print(f"Found {len(files_12)} parts for 12 Oct (Monday) and {len(files_13)} parts for 13 Oct (Tuesday)")
    if not files_12 and not files_13:
        print("No files found!")
        return

    # Check first file schema
    sample_df = pd.read_parquet(files_12[0])
    print("Columns:", sample_df.columns.tolist())
    print("Tokens in sample:", sample_df['token'].unique().tolist()[:10] if 'token' in sample_df else "No token column")
    print("Sample rows:")
    print(sample_df.head(2))

    # Look for NIFTY spot token 26000 or symbol containing NIFTY
    token_col = 'token' if 'token' in sample_df.columns else ('Token' if 'Token' in sample_df.columns else None)
    price_col = 'lp' if 'lp' in sample_df.columns else ('ltp' if 'ltp' in sample_df.columns else ('price' if 'price' in sample_df.columns else 'close'))
    ts_col = 'ts' if 'ts' in sample_df.columns else ('timestamp' if 'timestamp' in sample_df.columns else 'time')

    print(f"Using columns: token={token_col}, price={price_col}, ts={ts_col}")

    # Extract all rows for Nifty index
    all_files = files_12 + files_13
    nifty_ticks = []

    for f in all_files:
        try:
            df = pd.read_parquet(f)
            # Filter for Nifty spot token
            # Nifty spot token is usually '26000' or 26000
            if token_col:
                sub = df[df[token_col].astype(str) == '26000']
                if sub.empty:
                    # Check if symbol column exists
                    if 'symbol' in df.columns:
                        sub = df[df['symbol'].str.contains('NIFTY 50|NIFTY', case=False, na=False) & ~df['symbol'].str.contains('CE|PE|FUT', case=False, na=False)]
                if not sub.empty:
                    nifty_ticks.append(sub[[ts_col, price_col]])
        except Exception as e:
            print(f"Error reading {f}: {e}")

    if not nifty_ticks:
        print("Could not find token 26000 in L2 raw! Checking available tokens across sample files:")
        for f in files_12[:3]:
            df = pd.read_parquet(f)
            print(f"{f}: {df[token_col].unique().tolist()[:10]}")
            if 'symbol' in df.columns:
                print(f"Symbols: {df['symbol'].unique().tolist()[:10]}")
        return

    full_ticks = pd.concat(nifty_ticks, ignore_index=True)
    print(f"Total Nifty ticks extracted: {len(full_ticks)}")

    # Convert ts to datetime IST
    # ts is usually epoch in seconds or milliseconds
    ts_val = full_ticks[ts_col].iloc[0]
    unit = 'ms' if ts_val > 1e11 else 's'
    full_ticks['dt'] = pd.to_datetime(full_ticks[ts_col], unit=unit, utc=True).dt.tz_convert('Asia/Kolkata')
    full_ticks['price'] = pd.to_numeric(full_ticks[price_col], errors='coerce')
    full_ticks = full_ticks.dropna(subset=['price', 'dt']).sort_values('dt').reset_index(drop=True)

    # Resample to 1-minute OHLC
    full_ticks.set_index('dt', inplace=True)
    ohlc = full_ticks['price'].resample('1min').ohlc()
    ohlc['volume'] = full_ticks['price'].resample('1min').count()  # tick count as volume proxy

    # Forward fill or clean small internal gaps
    ohlc = ohlc.dropna(subset=['close']).reset_index()
    ohlc['date'] = ohlc['dt'].dt.strftime('%Y-%m-%d')
    ohlc['time_str'] = ohlc['dt'].dt.strftime('%H:%M')

    # Filter market hours 09:15 to 15:30
    ohlc = ohlc[(ohlc['time_str'] >= '09:15') & (ohlc['time_str'] <= '15:30')].copy()

    # Cleanliness check
    print("\n=== DATA CLEANLINESS REPORT ===")
    print(f"Total 1m candles extracted: {len(ohlc)}")
    for d, grp in ohlc.groupby('date'):
        print(f"Date {d}: {len(grp)} candles (Expected ~375 per full day) | Range: {grp['time_str'].min()} to {grp['time_str'].max()} | Close Range: {grp['close'].min():.2f} - {grp['close'].max():.2f}")
        zero_cnt = (grp['close'] <= 0).sum()
        print(f"  Zero close: {zero_cnt}, NaNs: {grp['close'].isna().sum()}")

    out_file = "/home/opc/nifty_12_13_oct_1m_clean.parquet"
    ohlc.to_parquet(out_file, index=False)
    print(f"\nSaved clean 1m candles to {out_file}")

if __name__ == "__main__":
    main()
