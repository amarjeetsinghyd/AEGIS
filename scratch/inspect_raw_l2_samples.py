import glob
import pandas as pd
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

files = sorted(glob.glob('/home/opc/AEGIS/data/l2_raw/Nifty/aegis_l2_raw_2026-09-28_part_*.parquet'))
print(f"Total raw L2 parquet files: {len(files)}")

if not files:
    exit(0)

# Check columns
df_0 = pd.read_parquet(files[0])
print("Columns:", list(df_0.columns))
print(df_0.head(2))

# Search for ticks around 10:57, 14:30
# 10:57 IST = 05:27 UTC = 1759037220 (approx)
target_ts_ranges = [
    # 10:57:00 IST -> (2026-09-28 10:56:50 to 10:57:15)
    (pd.Timestamp("2026-09-28 10:56:50", tz=IST).timestamp() * 1000, pd.Timestamp("2026-09-28 10:57:15", tz=IST).timestamp() * 1000, "10:57"),
    # 14:30:00 IST -> (2026-09-28 14:29:50 to 14:30:15)
    (pd.Timestamp("2026-09-28 14:29:50", tz=IST).timestamp() * 1000, pd.Timestamp("2026-09-28 14:30:15", tz=IST).timestamp() * 1000, "14:30"),
]

for t_start, t_end, label in target_ts_ranges:
    print(f"\n--- Checking raw ticks for window {label} IST ---")
    matches = []
    for f in files:
        df = pd.read_parquet(f)
        sub = df[(df['ts'] >= t_start) & (df['ts'] <= t_end)]
        if not sub.empty:
            matches.append(sub)
    if matches:
        res = pd.concat(matches, ignore_index=True)
        print(f"Total ticks in window: {len(res)}")
        # Check tokens present
        print("Tokens count:\n", res['tok'].value_counts().head(10))
        # Print sample raw ticks
        sample_cols = [c for c in ['ts', 'tok', 'lp', 'bp1', 'sp1', 'bq1', 'sq1', 'v'] if c in res.columns]
        print(res[sample_cols].head(10).to_string())
    else:
        print(f"ZERO ticks found in parquet for window {label}!")
