import glob
import os
import pandas as pd
import numpy as np

def main():
    nifty_l2_dir = "/home/opc/AEGIS/data/l2_raw/Nifty"
    files_12 = sorted(glob.glob(f"{nifty_l2_dir}/aegis_l2_raw_2026-10-12_part_*.parquet"))
    files_13 = sorted(glob.glob(f"{nifty_l2_dir}/aegis_l2_raw_2026-10-13_part_*.parquet"))
    all_files = files_12 + files_13

    print(f"Reading {len(all_files)} L2 Archiver files for 12 Oct (Monday) and 13 Oct (Tuesday)...")
    
    ticks = []
    for f in all_files:
        try:
            df = pd.read_parquet(f, columns=['ts', 'tok', 'sym', 'lp'])
            sub = df[(df['tok'] == '26000') | (df['sym'] == 'NIFTY')]
            if not sub.empty:
                ticks.append(sub[['ts', 'lp']])
        except Exception as e:
            print(f"Error in {f}: {e}")

    if not ticks:
        print("No Nifty ticks found!")
        return

    full_ticks = pd.concat(ticks, ignore_index=True)
    print(f"Total raw NIFTY spot ticks extracted from L2 Archiver: {len(full_ticks)}")

    # ts is in milliseconds
    full_ticks['dt'] = pd.to_datetime(full_ticks['ts'], unit='ms', utc=True).dt.tz_convert('Asia/Kolkata')
    full_ticks['price'] = pd.to_numeric(full_ticks['lp'], errors='coerce')
    full_ticks = full_ticks.dropna(subset=['price', 'dt']).sort_values('dt').reset_index(drop=True)

    # Resample to 1-minute OHLC
    full_ticks.set_index('dt', inplace=True)
    ohlc = full_ticks['price'].resample('1min').ohlc()
    ohlc['tick_count'] = full_ticks['price'].resample('1min').count()

    # Drop minutes where market is closed (between 15:30 and 09:15 next day)
    ohlc = ohlc.dropna(subset=['close']).reset_index()
    ohlc['date'] = ohlc['dt'].dt.strftime('%Y-%m-%d')
    ohlc['time_str'] = ohlc['dt'].dt.strftime('%H:%M')

    # Filter to exact market hours: 09:15 to 15:30
    ohlc = ohlc[(ohlc['time_str'] >= '09:15') & (ohlc['time_str'] <= '15:30')].copy().reset_index(drop=True)

    print("\n" + "="*50)
    print("📊 DATA CLEANLINESS & INTEGRITY AUDIT")
    print("="*50)
    print(f"Total 1m candles: {len(ohlc)}")
    for d, grp in ohlc.groupby('date'):
        print(f"\nDate: {d} ({'Monday' if '12' in d else 'Tuesday'})")
        print(f"  Total candles: {len(grp)} (Market duration: {grp['time_str'].min()} to {grp['time_str'].max()})")
        print(f"  Price range: Low ₹{grp['low'].min():.2f} - High ₹{grp['high'].max():.2f}")
        zero_cnt = (grp[['open', 'high', 'low', 'close']] <= 0).sum().to_dict()
        nan_cnt = grp[['open', 'high', 'low', 'close']].isna().sum().to_dict()
        print(f"  Zero values: {zero_cnt}")
        print(f"  NaN values:  {nan_cnt}")
        # Check consecutive minute continuity
        grp_times = grp['time_str'].tolist()
        expected_minutes = 376  # 09:15 to 15:30 inclusive
        print(f"  Continuity: {len(grp_times)} / {expected_minutes} minutes present ({len(grp_times)/expected_minutes*100:.1f}%)")

    out_parquet = "/home/opc/nifty_12_13_oct_1m_clean.parquet"
    out_csv = "/home/opc/nifty_12_13_oct_1m_clean.csv"
    ohlc.to_parquet(out_parquet, index=False)
    ohlc.to_csv(out_csv, index=False)
    print(f"\nCleaned 1m dataset saved to:\n  - {out_parquet}\n  - {out_csv}")

if __name__ == "__main__":
    main()
