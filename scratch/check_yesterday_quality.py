import glob
import pandas as pd

files = sorted(glob.glob('/home/opc/AEGIS/data/l2_raw/Nifty/aegis_l2_raw_2026-09-29_part_*.parquet'))
print(f"Total files for 29-Sep: {len(files)}")

total_ticks = 0
opt_ticks = 0
zero_bid_ask_opt = 0

for f in files[::4]: # sample every 4th file to be fast
    df = pd.read_parquet(f)
    total_ticks += len(df)
    opts = df[df['kind'] != 0]
    opt_ticks += len(opts)
    # Check blank quotes
    bad = opts[(opts['bp1'] == 0) & (opts['sp1'] == 0)]
    zero_bid_ask_opt += len(bad)

print(f"Sampled total ticks: {total_ticks}")
print(f"Sampled option ticks: {opt_ticks}")
print(f"Option ticks with 0 bid & 0 ask: {zero_bid_ask_opt}")
bad_pct = (zero_bid_ask_opt / opt_ticks * 100) if opt_ticks > 0 else 0
print(f"Bad quotes percentage: {bad_pct:.2f}%")
