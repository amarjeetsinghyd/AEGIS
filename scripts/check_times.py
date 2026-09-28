import polars as pl
import datetime
# read from merged or run quick tail
import glob
files = sorted(glob.glob("/home/opc/AEGIS/data/l2_raw/Nifty/aegis_l2_raw_2026-09-21_part_*.parquet"))
df_last = pl.read_parquet(files[-1])
ts_end = float(df_last["ts"][-1])
while ts_end > 1e11: ts_end /= 1000.0
print("Last file end time:", datetime.datetime.fromtimestamp(ts_end).strftime('%H:%M:%S'))
df_first = pl.read_parquet(files[0])
ts_start = float(df_first["ts"][0])
while ts_start > 1e11: ts_start /= 1000.0
print("First file start time:", datetime.datetime.fromtimestamp(ts_start).strftime('%H:%M:%S'))
