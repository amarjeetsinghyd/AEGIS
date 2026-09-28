import os
import sys
import glob
import json
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np

IST = timezone(timedelta(hours=5, minutes=30))

def parse_today_1m_data():
    files = sorted(glob.glob('/home/opc/AEGIS/data/l2_raw/Nifty/aegis_l2_raw_2026-09-28_part_*.parquet'))
    print(f"Reading {len(files)} partition files...")

    # We will accumulate all spot ticks and option ticks
    spot_records = []
    opt_records = []

    for f in files:
        df = pd.read_parquet(f, columns=['ts', 'tok', 'sym', 'kind', 'strike', 'lp', 'vol', 'bp1', 'sp1'])
        if df.empty:
            continue
        
        # Spot is kind == 0 or tok == '26000'
        spot_mask = (df['kind'] == 0) | (df['tok'] == '26000')
        spot_df = df[spot_mask]
        if not spot_df.empty:
            spot_records.append(spot_df[['ts', 'lp']])

        opt_mask = df['kind'].isin([1, 2])
        opt_df = df[opt_mask]
        if not opt_df.empty:
            opt_records.append(opt_df[['ts', 'sym', 'kind', 'strike', 'lp', 'vol', 'bp1', 'sp1']])

    print("Concatenating spot ticks...")
    all_spot = pd.concat(spot_records, ignore_index=True)
    all_spot['ts_sec'] = all_spot['ts'] / 1000.0
    all_spot = all_spot.sort_values('ts_sec')

    # Convert to IST datetime
    all_spot['dt'] = pd.to_datetime(all_spot['ts_sec'], unit='s', utc=True).dt.tz_convert(IST)
    
    # Filter strictly to market hours: 09:15:00 to 15:30:00
    market_start = pd.Timestamp("2026-09-28 09:15:00", tz=IST)
    market_end = pd.Timestamp("2026-09-28 15:30:00", tz=IST)
    all_spot = all_spot[(all_spot['dt'] >= market_start) & (all_spot['dt'] < market_end)]
    
    # Assign 1-minute bucket (floor to minute)
    all_spot['minute'] = all_spot['dt'].dt.floor('1min')

    print(f"Total spot ticks in market hours: {len(all_spot)}")

    # 1. Compute Spot 1m OHLC
    ohlc = all_spot.groupby('minute').agg(
        open=('lp', 'first'),
        high=('lp', 'max'),
        low=('lp', 'min'),
        close=('lp', 'last'),
        tick_count=('lp', 'count')
    ).reset_index()

    ohlc['time_str'] = ohlc['minute'].dt.strftime('%H:%M')
    print(f"Total 1-minute candles created: {len(ohlc)}")
    print("Sample candles (first 5):")
    print(ohlc[['time_str', 'open', 'high', 'low', 'close', 'tick_count']].head(5))

    # 2. Process Option Volume Flow
    print("\nProcessing option ticks for CallBuy, CallSell, PutBuy, PutSell...")
    all_opt = pd.concat(opt_records, ignore_index=True)
    all_opt['ts_sec'] = all_opt['ts'] / 1000.0
    all_opt = all_opt.sort_values('ts_sec')
    all_opt['dt'] = pd.to_datetime(all_opt['ts_sec'], unit='s', utc=True).dt.tz_convert(IST)
    all_opt = all_opt[(all_opt['dt'] >= market_start) & (all_opt['dt'] < market_end)]
    all_opt['minute'] = all_opt['dt'].dt.floor('1min')
    print(f"Total option ticks in market hours: {len(all_opt)}")

    # Minute-by-minute spot lookup for ATM ± 7 strike filtering
    spot_lookup = ohlc.set_index('minute')['close'].to_dict()

    # Trackers for volume differencing & Lee-Ready
    last_cum_vol = {}
    last_lp = {}
    last_frac = {}

    # Aggregators per minute
    # minute -> {'call_buy': 0, 'call_sell': 0, 'put_buy': 0, 'put_sell': 0}
    flow_per_minute = {m: {'call_buy': 0.0, 'call_sell': 0.0, 'put_buy': 0.0, 'put_sell': 0.0} for m in ohlc['minute']}

    for row in all_opt.itertuples(index=False):
        sym = row.sym
        vol = float(row.vol)
        lp = float(row.lp)
        minute = row.minute

        if vol <= 0 or lp <= 0 or minute not in flow_per_minute:
            continue

        prev_vol = last_cum_vol.get(sym)
        last_cum_vol[sym] = vol  # Update immediately

        if prev_vol is None:
            last_lp[sym] = lp
            continue  # Seed tick

        delta_vol = vol - prev_vol
        if delta_vol < 0:
            delta_vol = vol
        if delta_vol <= 0:
            continue

        # Filter: ATM ± 7 strikes (350 pts on NIFTY)
        curr_spot = spot_lookup.get(minute, 0.0)
        strike = float(row.strike)
        if curr_spot > 0 and abs(strike - curr_spot) > 350.0:
            continue

        # Hybrid Touch + Tick Rule classification
        bp1 = float(row.bp1) if not np.isnan(row.bp1) else 0.0
        sp1 = float(row.sp1) if not np.isnan(row.sp1) else 0.0
        valid_quote = bp1 > 0 and sp1 > 0 and sp1 > bp1

        if valid_quote and lp >= sp1:
            frac = 1.0  # Buy
        elif valid_quote and lp <= bp1:
            frac = 0.0  # Sell
        else:
            prev_p = last_lp.get(sym)
            if prev_p is None:
                last_lp[sym] = lp
                continue
            if lp > prev_p:
                frac = 1.0
            elif lp < prev_p:
                frac = 0.0
            else:
                frac = last_frac.get(sym)
                if frac is None:
                    last_lp[sym] = lp
                    continue

        last_lp[sym] = lp
        last_frac[sym] = frac

        buy_v = delta_vol * frac
        sell_v = delta_vol - buy_v
        kind = int(row.kind)

        if kind == 1:  # CALL
            flow_per_minute[minute]['call_buy'] += buy_v
            flow_per_minute[minute]['call_sell'] += sell_v
        elif kind == 2:  # PUT
            flow_per_minute[minute]['put_buy'] += buy_v
            flow_per_minute[minute]['put_sell'] += sell_v

    # Combine OHLC with Volume Flow
    ohlc['call_buy_vol'] = ohlc['minute'].map(lambda m: round(flow_per_minute[m]['call_buy'], 0))
    ohlc['call_sell_vol'] = ohlc['minute'].map(lambda m: round(flow_per_minute[m]['call_sell'], 0))
    ohlc['put_buy_vol'] = ohlc['minute'].map(lambda m: round(flow_per_minute[m]['put_buy'], 0))
    ohlc['put_sell_vol'] = ohlc['minute'].map(lambda m: round(flow_per_minute[m]['put_sell'], 0))
    ohlc['total_opt_vol'] = ohlc['call_buy_vol'] + ohlc['call_sell_vol'] + ohlc['put_buy_vol'] + ohlc['put_sell_vol']
    ohlc['ofi'] = ohlc['call_buy_vol'] - ohlc['put_buy_vol']

    # Convert minute to string
    ohlc['time'] = ohlc['time_str']
    cols_to_save = ['time', 'open', 'high', 'low', 'close', 'tick_count', 'call_buy_vol', 'call_sell_vol', 'put_buy_vol', 'put_sell_vol', 'total_opt_vol', 'ofi']
    result_df = ohlc[cols_to_save]

    out_csv = '/home/opc/quant-bot/scripts/nifty_1m_candles_and_volume_flow_2026-09-28.csv'
    out_json = '/home/opc/quant-bot/scripts/nifty_1m_candles_and_volume_flow_2026-09-28.json'
    result_df.to_csv(out_csv, index=False)
    result_df.to_json(out_json, orient='records', indent=2)

    print(f"\nData successfully generated and saved to:")
    print(f"  CSV: {out_csv} ({os.path.getsize(out_csv)} bytes)")
    print(f"  JSON: {out_json} ({os.path.getsize(out_json)} bytes)")
    print(f"Total rows: {len(result_df)}")

    # Print summary highlights
    print("\n--- SAMPLE ROWS (Morning 09:15 - 09:20) ---")
    print(result_df.head(6).to_string(index=False))

    print("\n--- 09:27 MORNING CALL ENTRY ---")
    print(result_df[result_df['time'] == '09:27'].to_string(index=False))

    print("\n--- 14:12 AFTERNOON PUT REJECTION ---")
    print(result_df[(result_df['time'] >= '14:10') & (result_df['time'] <= '14:15')].to_string(index=False))

    print("\n--- TOTAL DAY ACCUMULATED VOLUMES ---")
    print(f"Total CallBuy Volume:  {result_df['call_buy_vol'].sum():,.0f}")
    print(f"Total CallSell Volume: {result_df['call_sell_vol'].sum():,.0f}")
    print(f"Total PutBuy Volume:   {result_df['put_buy_vol'].sum():,.0f}")
    print(f"Total PutSell Volume:  {result_df['put_sell_vol'].sum():,.0f}")
    print(f"Grand Total Option Vol:{result_df['total_opt_vol'].sum():,.0f}")

if __name__ == '__main__':
    parse_today_1m_data()
