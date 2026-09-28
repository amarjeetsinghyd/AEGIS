import glob
import os
import datetime
import polars as pl
import pandas as pd
import numpy as np

files = sorted(glob.glob("/home/opc/AEGIS/data/l2_raw/Nifty/aegis_l2_raw_2026-09-21_part_*.parquet"))
print(f"Reading {len(files)} files for 2026-09-21...")

# We only need ts, tok, sym, lp, vol, bp1, sp1
dfs = []
for f in files:
    df = pl.read_parquet(f, columns=["ts", "tok", "sym", "lp", "vol", "bp1", "sp1"])
    dfs.append(df)

full_df = pl.concat(dfs)
print(f"Total rows concatenated: {len(full_df):,}")

# Cast columns
full_df = full_df.with_columns([
    pl.col("tok").cast(pl.Int64),
    pl.col("lp").cast(pl.Float64),
    pl.col("vol").cast(pl.Float64),
    pl.col("bp1").cast(pl.Float64),
    pl.col("sp1").cast(pl.Float64),
])

# Normalize timestamp to seconds
ts_s = full_df["ts"].to_numpy().astype(np.float64)
# If ms
mask_ms = ts_s > 1e11
ts_s[mask_ms] /= 1000.0

full_df = full_df.with_columns(pl.Series("ts_sec", ts_s))
full_df = full_df.sort("ts_sec")

# Filter market hours 09:15 to 15:30
# Find start of day timestamp
t_min = full_df["ts_sec"].min()
dt_min = datetime.datetime.fromtimestamp(t_min)
market_start = datetime.datetime(dt_min.year, dt_min.month, dt_min.day, 9, 15, 0).timestamp()
market_end = datetime.datetime(dt_min.year, dt_min.month, dt_min.day, 15, 30, 0).timestamp()

mkt_df = full_df.filter((pl.col("ts_sec") >= market_start) & (pl.col("ts_sec") <= market_end))
print(f"Market hours rows: {len(mkt_df):,}")

# Spot ticks
spot_df = mkt_df.filter(pl.col("tok") == 26000)
print(f"Spot rows: {len(spot_df):,}")

# Option ticks
opt_df = mkt_df.filter(~pl.col("tok").is_in([26000, 68407]))
print(f"Option rows: {len(opt_df):,}")

# Assign 1-minute bucket (0 to 374)
mkt_df = mkt_df.with_columns(
    ((pl.col("ts_sec") - market_start) // 60).cast(pl.Int32).alias("min_idx")
)

# Aggregate spot per minute
spot_agg = mkt_df.filter(pl.col("tok") == 26000).group_by("min_idx").agg([
    pl.col("lp").first().alias("open"),
    pl.col("lp").max().alias("high"),
    pl.col("lp").min().alias("low"),
    pl.col("lp").last().alias("close"),
    pl.len().alias("spot_ticks"),
]).sort("min_idx")

# Aggregate option ticks count per minute
opt_agg = mkt_df.filter(~pl.col("tok").is_in([26000, 68407])).group_by("min_idx").agg([
    pl.len().alias("opt_ticks"),
    pl.col("tok").n_unique().alias("unique_opt_toks"),
]).sort("min_idx")

merged = spot_agg.join(opt_agg, on="min_idx", how="outer").sort("min_idx")
merged = merged.fill_null(0)

total_mins = len(merged)
zero_opt_mins = len(merged.filter(pl.col("opt_ticks") == 0))
ok_opt_mins = len(merged.filter(pl.col("opt_ticks") > 0))

print("\n================ 1-MINUTE GRANULARITY AUDIT ================")
print(f"Total 1m bars in session: {total_mins} (out of 375 expected)")
print(f"Bars with OPTION ticks > 0: {ok_opt_mins} ({ok_opt_mins/375*100:.1f}%)")
print(f"Bars with ZERO option ticks: {zero_opt_mins} ({zero_opt_mins/375*100:.1f}%)")

zero_blocks = []
in_block = False
b_start = 0
for r in merged.iter_rows(named=True):
    m_idx = r["min_idx"]
    opt_cnt = r["opt_ticks"]
    t_str = (datetime.datetime.fromtimestamp(market_start + m_idx * 60)).strftime("%H:%M")
    if opt_cnt == 0:
        if not in_block:
            in_block = True
            b_start = (m_idx, t_str)
    else:
        if in_block:
            in_block = False
            zero_blocks.append((b_start[1], t_str, m_idx - b_start[0]))
if in_block:
    zero_blocks.append((b_start[1], "15:30", 375 - b_start[0]))

print(f"\nZero-Option Windows ({len(zero_blocks)} found):")
for zb in zero_blocks:
    print(f"   Gap from {zb[0]} to {zb[1]} ({zb[2]} minutes)")

print("\nSample first 15 minutes of session:")
for r in merged.filter(pl.col("spot_ticks") > 0).head(15).iter_rows(named=True):
    t_str = (datetime.datetime.fromtimestamp(market_start + r["min_idx"] * 60)).strftime("%H:%M")
    print(f"   {t_str} | Spot: O={r['open']:.2f}, H={r['high']:.2f}, L={r['low']:.2f}, C={r['close']:.2f} | spot_ticks={r['spot_ticks']} | opt_ticks={r['opt_ticks']} ({r['unique_opt_toks']} tokens)")

