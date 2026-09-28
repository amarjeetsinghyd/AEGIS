import glob
import os
import datetime
import polars as pl
import sys

date_str = sys.argv[1] if len(sys.argv) > 1 else "2026-09-21"
files = sorted(glob.glob(f"/home/opc/AEGIS/data/l2_raw/Nifty/aegis_l2_raw_{date_str}_part_*.parquet"))

print(f"==================================================")
print(f"AUDITING DATA QUALITY FOR {date_str} (NIFTY)")
print(f"Found {len(files)} partition files")
print(f"==================================================")

if not files:
    print(f"No files found for {date_str}!")
    sys.exit(1)

total_rows_all = 0
total_opt_rows_all = 0
total_spot_rows_all = 0
total_fut_rows_all = 0

problematic_parts = []

for f in files:
    base = os.path.basename(f)
    df = pl.read_parquet(f)
    n = len(df)
    total_rows_all += n
    
    if n == 0:
        print(f"WARNING: {base} is EMPTY!")
        problematic_parts.append((base, "EMPTY"))
        continue
        
    ts_val0 = float(df["ts"][0])
    ts_val1 = float(df["ts"][-1])
    while ts_val0 > 1e11: ts_val0 /= 1000.0
    while ts_val1 > 1e11: ts_val1 /= 1000.0
    t0 = datetime.datetime.fromtimestamp(ts_val0).strftime("%H:%M:%S")
    t1 = datetime.datetime.fromtimestamp(ts_val1).strftime("%H:%M:%S")
    
    toks = df["tok"].cast(pl.Int64).unique().to_list()
    spot_rows = len(df.filter(pl.col("tok").cast(pl.Int64) == 26000))
    fut_rows = len(df.filter(pl.col("tok").cast(pl.Int64) == 68407))
    opt_df = df.filter(~pl.col("tok").cast(pl.Int64).is_in([26000, 68407]))
    opt_rows = len(opt_df)
    opt_tok_count = len(opt_df["tok"].unique()) if opt_rows > 0 else 0
    
    total_spot_rows_all += spot_rows
    total_fut_rows_all += fut_rows
    total_opt_rows_all += opt_rows
    
    status = "OK"
    if opt_rows == 0:
        status = "ZERO_OPTIONS"
        problematic_parts.append((base, "ZERO_OPTIONS", t0, t1))
    elif opt_tok_count < 10:
        status = f"FEW_OPTIONS({opt_tok_count})"
        problematic_parts.append((base, status, t0, t1))
        
    print(f"{base} | {t0} - {t1} | rows={n:6d} | spot={spot_rows:5d} | fut={fut_rows:5d} | opt_rows={opt_rows:6d} ({opt_tok_count:2d} tokens) | {status}")

print(f"\n---------------- SUMMARY FOR {date_str} ----------------")
print(f"Total Partitions: {len(files)}")
print(f"Total Rows: {total_rows_all:,}")
print(f"Total Spot Rows: {total_spot_rows_all:,}")
print(f"Total Futures Rows: {total_fut_rows_all:,}")
print(f"Total Options Rows: {total_opt_rows_all:,}")
print(f"Problematic Partitions: {len(problematic_parts)}")
for p in problematic_parts:
    print(f"   --> {p}")
