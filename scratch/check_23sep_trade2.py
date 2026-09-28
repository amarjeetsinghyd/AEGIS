import pandas as pd

df = pd.read_parquet('/home/opc/AEGIS/data/l2_raw/Sensex/aegis_l2_raw_2026-09-23_part_023.parquet')
df['dt'] = pd.to_datetime(df['ts'], unit='ms', utc=True).dt.tz_convert('Asia/Kolkata')
df['time'] = df['dt'].dt.strftime('%H:%M:%S')

# 1. Spot Index SENSEX between 14:24 and 14:32
spot = df[df['kind'] == 0]
if spot.empty:
    spot = df[df['sym'] == 'SENSEX']
spot_w = spot[(spot['time'] >= '14:24:50') & (spot['time'] <= '14:32:00')]
print("=== SENSEX SPOT 1-MIN CANDLES (14:25 - 14:31) ===")
spot_m = spot_w.groupby(spot_w['dt'].dt.floor('1min')).agg(
    open=('lp', 'first'), high=('lp', 'max'), low=('lp', 'min'), close=('lp', 'last')
)
print(spot_m.to_string())

# 2. Option SENSEX26SEP75600CE between 14:24:50 and 14:32:00
opt = df[df['sym'] == 'SENSEX26SEP75600CE'].copy()
if not opt.empty:
    opt_w = opt[(opt['time'] >= '14:24:50') & (opt['time'] <= '14:32:00')].copy()
    print("\n=== SENSEX26SEP75600CE TICKS (14:24:50 to 14:26:00) ===")
    opt_sub = opt_w[(opt_w['time'] >= '14:25:00') & (opt_w['time'] <= '14:25:40')]
    print(opt_sub[['time', 'lp', 'bp1', 'sp1', 'vol']].drop_duplicates(subset=['time', 'lp']).to_string())
    
    print("\n=== SENSEX26SEP75600CE 1-MIN SUMMARY (14:25 to 14:31) ===")
    opt_m = opt_w.groupby(opt_w['dt'].dt.floor('1min')).agg(
        open=('lp', 'first'), high=('lp', 'max'), low=('lp', 'min'), close=('lp', 'last'), vol=('vol', 'last')
    )
    print(opt_m.to_string())
else:
    print("SENSEX26SEP75600CE not in partition 23")
