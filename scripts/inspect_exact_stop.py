import polars as pl
import datetime

df = pl.read_parquet('/home/opc/AEGIS/data/l2_raw/Nifty/aegis_l2_raw_2026-09-28_part_004.parquet')
opt_df = df.filter(~pl.col('tok').cast(pl.Int64).is_in([26000, 68407]))
print("Total opt ticks in part_004:", len(opt_df))
last_10 = opt_df.tail(10)
for r in last_10.iter_rows(named=True):
    ts_val = float(r['ts'])
    while ts_val > 1e11:
        ts_val /= 1000.0
    t_str = datetime.datetime.fromtimestamp(ts_val).strftime('%H:%M:%S.%f')[:-3]
    print(f"{t_str} | tok={r['tok']} | sym={r['sym']} | lp={r['lp']} | vol={r['vol']}")

# Now check non-option ticks right after the last option tick
last_opt_ts = opt_df['ts'][-1]
next_ticks = df.filter(pl.col('ts') >= last_opt_ts).head(15)
print("\nTicks right around/after last opt tick:")
for r in next_ticks.iter_rows(named=True):
    ts_val = float(r['ts'])
    while ts_val > 1e11:
        ts_val /= 1000.0
    t_str = datetime.datetime.fromtimestamp(ts_val).strftime('%H:%M:%S.%f')[:-3]
    print(f"{t_str} | tok={r['tok']} | sym={r['sym']} | lp={r['lp']} | vol={r['vol']}")
