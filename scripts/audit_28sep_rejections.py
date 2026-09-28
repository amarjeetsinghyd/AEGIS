"""
Reconstruct today's (2026-09-28) 1-minute Spot bars and audit PriceActionEngine 20 EMA Rejection
specifically at user-mentioned times:
- 09:59
- 10:37
- 11:37
- 12:23 / 12:24
- 13:22
- 14:11 to 14:13
- 14:31
"""

import sys, os, glob
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np

sys.path.insert(0, '/home/opc')

from AEGIS.core.price_action_engine import PriceActionEngine, OHLCBar

IST = timezone(timedelta(hours=5, minutes=30))

files = sorted(glob.glob('/home/opc/AEGIS/data/l2_raw/Nifty/aegis_l2_raw_2026-09-28_part_*.parquet'))
print(f"Loading {len(files)} parquet files...")

spot_dfs = []
for f in files:
    try:
        df = pd.read_parquet(f, columns=['ts', 'tok', 'kind', 'lp'])
        # Filter for Spot: kind == 0 or tok == '26000'
        s_df = df[(df['kind'] == 0) | (df['tok'] == '26000') | (df['tok'] == 26000)]
        if not s_df.empty:
            spot_dfs.append(s_df[['ts', 'lp']])
    except Exception as e:
        pass

all_spot = pd.concat(spot_dfs, ignore_index=True)
all_spot['ts_sec'] = all_spot['ts'] / 1000.0
all_spot = all_spot.sort_values('ts_sec').reset_index(drop=True)
all_spot['dt'] = pd.to_datetime(all_spot['ts_sec'], unit='s', utc=True).dt.tz_convert(IST)

# Filter strictly to market hours: 09:15:00 to 15:30:00
market_start = pd.Timestamp("2026-09-28 09:15:00", tz=IST)
market_end = pd.Timestamp("2026-09-28 15:30:00", tz=IST)
all_spot = all_spot[(all_spot['dt'] >= market_start) & (all_spot['dt'] < market_end)]
all_spot['minute'] = all_spot['dt'].dt.floor('1min')

print(f"Total spot ticks loaded: {len(all_spot)}")

# Group into 1m bars
bars_1m = all_spot.groupby('minute').agg(
    open=('lp', 'first'),
    high=('lp', 'max'),
    low=('lp', 'min'),
    close=('lp', 'last'),
    ts_start=('ts_sec', 'first'),
    ts_end=('ts_sec', 'last'),
    ticks=('lp', 'count')
).reset_index()

bars_1m['time'] = bars_1m['minute'].dt.strftime('%H:%M')
print(f"Total 1m bars constructed: {len(bars_1m)}")

pae = PriceActionEngine(strike_step=50.0)
target_times = ['09:59', '10:37', '11:37', '12:23', '12:24', '13:22', '14:11', '14:12', '14:13', '14:31']

print("\n" + "="*85)
print(f"{'TIME':6} | {'SPOT':8} | {'EMA20':8} | {'DIR':4} | {'P1 STATUS':8} | {'REASON / DETAILS'}")
print("="*85)

for i, row in bars_1m.iterrows():
    t_str = row['time']
    bar = OHLCBar(
        ts_start=row['ts_start'],
        ts_end=row['ts_end'],
        open=row['open'],
        high=row['high'],
        low=row['low'],
        close=row['close'],
        volume=1000.0,
        call_buy_vol=500.0,
        put_buy_vol=500.0
    )
    pae.bars_1m.append(bar)
    pae.last_price = row['close']
    
    # Compute live EMA 20
    if len(pae.bars_1m) >= 20:
        closes = [b.close for b in pae.bars_1m]
        pae.ema_20_1m = pd.Series(closes).ewm(span=20, adjust=False).mean().iloc[-1]
        pae.ema_20_history.append(pae.ema_20_1m)
        pae.current_atr_1m = 8.0 # typical ATR today
        
        ok_call, r_call, d_call = pae.check_20_ema_rejection('CALL')
        ok_put, r_put, d_put = pae.check_20_ema_rejection('PUT')
        
        if t_str in target_times:
            # Print CALL
            print(f"{t_str:6} | {row['close']:8.2f} | {pae.ema_20_1m:8.2f} | CALL | {'PASS' if ok_call else 'REJECT':8} | {r_call} {d_call.get('rejection_type', '')}")
            # Print PUT
            print(f"{t_str:6} | {row['close']:8.2f} | {pae.ema_20_1m:8.2f} | PUT  | {'PASS' if ok_put else 'REJECT':8} | {r_put} {d_put.get('rejection_type', '')}")
            print("-" * 85)
