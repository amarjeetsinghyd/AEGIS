import pandas as pd
import numpy as np

# Load the audited 1-minute dataset
csv_path = 'scripts/nifty_1m_candles_and_volume_flow_2026-09-21.csv'
df = pd.read_csv(csv_path)

print(f"Loaded {len(df)} 1-minute bars from {csv_path}")

# Typical price or Close
# Let's use Close as the primary benchmark, and we can also inspect typical price
px = df['close']

# 1. Progressive Cumulative Volumes
df['cum_cb_vol'] = df['call_buy_vol'].cumsum()
df['cum_cs_vol'] = df['call_sell_vol'].cumsum()
df['cum_pb_vol'] = df['put_buy_vol'].cumsum()
df['cum_ps_vol'] = df['put_sell_vol'].cumsum()
df['cum_total_vol'] = df['total_opt_vol'].cumsum()

# 2. Progressive Cumulative Price x Volume
df['cum_cb_pv'] = (px * df['call_buy_vol']).cumsum()
df['cum_cs_pv'] = (px * df['call_sell_vol']).cumsum()
df['cum_pb_pv'] = (px * df['put_buy_vol']).cumsum()
df['cum_ps_pv'] = (px * df['put_sell_vol']).cumsum()
df['cum_total_pv'] = (px * df['total_opt_vol']).cumsum()

# 3. Progressive VWAP Lines (rounded to 2 decimal places)
df['vwap_composite'] = (df['cum_total_pv'] / df['cum_total_vol']).round(2)
df['vwap_call_buy'] = (df['cum_cb_pv'] / df['cum_cb_vol']).round(2)
df['vwap_call_sell'] = (df['cum_cs_pv'] / df['cum_cs_vol']).round(2)
df['vwap_put_buy'] = (df['cum_pb_pv'] / df['cum_pb_vol']).round(2)
df['vwap_put_sell'] = (df['cum_ps_pv'] / df['cum_ps_vol']).round(2)

# 4. Trapped-Buyer Pain Zone Spreads (in Spot Points)
# When Spot > vwap_put_buy: Put Buyers are Trapped in Loss (Fuel for UP)
# When Spot < vwap_call_buy: Call Buyers are Trapped in Loss (Fuel for DOWN)
df['spot_vs_cb'] = (df['close'] - df['vwap_call_buy']).round(2)
df['spot_vs_pb'] = (df['close'] - df['vwap_put_buy']).round(2)
df['spot_vs_cs'] = (df['close'] - df['vwap_call_sell']).round(2)
df['spot_vs_ps'] = (df['close'] - df['vwap_put_sell']).round(2)

# Select clean columns for output
output_cols = [
    'time', 'open', 'high', 'low', 'close',
    'vwap_composite', 'vwap_call_buy', 'vwap_call_sell', 'vwap_put_buy', 'vwap_put_sell',
    'call_buy_vol', 'call_sell_vol', 'put_buy_vol', 'put_sell_vol', 'total_opt_vol', 'ofi',
    'spot_vs_cb', 'spot_vs_pb'
]

out_df = df[output_cols]

out_csv = 'scripts/nifty_1m_with_5_vwaps_2026-09-21.csv'
out_json = 'scripts/nifty_1m_with_5_vwaps_2026-09-21.json'

out_df.to_csv(out_csv, index=False)
out_df.to_json(out_json, orient='records', indent=2)

print(f"\nSuccessfully generated 5 VWAP lines across {len(out_df)} bars:")
print(f"  CSV: {out_csv}")
print(f"  JSON: {out_json}")

print("\n--- FIRST 15 MINUTES (09:15 to 09:30) ---")
view_cols = ['time', 'close', 'vwap_composite', 'vwap_call_buy', 'vwap_call_sell', 'vwap_put_buy', 'vwap_put_sell', 'ofi']
print(out_df[view_cols].head(15).to_string(index=False))

print("\n--- MID-DAY (12:00 to 12:10) ---")
print(out_df[(out_df['time'] >= '12:00') & (out_df['time'] <= '12:10')][view_cols].to_string(index=False))

print("\n--- EOD CLOSE (15:20 to 15:30) ---")
print(out_df.tail(10)[view_cols].to_string(index=False))
