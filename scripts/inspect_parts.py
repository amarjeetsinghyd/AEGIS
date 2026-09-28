import glob
import os
import datetime
import polars as pl

files = sorted(glob.glob('/home/opc/AEGIS/data/l2_raw/Nifty/aegis_l2_raw_2026-09-28_part_*.parquet'))
print(f"Total files: {len(files)}")
if files:
    df0 = pl.read_parquet(files[0])
    print(f"Columns: {df0.columns}")
for f in files:
    df = pl.read_parquet(f)
    toks = df["tok"].unique().to_list()
    opt_toks = [t for t in toks if t not in ("26000", "68407")]
    ts_val0 = float(df["ts"][0])
    ts_val1 = float(df["ts"][-1])
    while ts_val0 > 1e11:
        ts_val0 /= 1000.0
    while ts_val1 > 1e11:
        ts_val1 /= 1000.0
    t0 = datetime.datetime.fromtimestamp(ts_val0).strftime("%H:%M:%S") if len(df) > 0 else "N/A"
    t1 = datetime.datetime.fromtimestamp(ts_val1).strftime("%H:%M:%S") if len(df) > 0 else "N/A"
    print(f"{os.path.basename(f)} | {t0} - {t1} | total_rows={len(df):6d} | opt_tokens={len(opt_toks)}")
    if "part_004" in f:
        opt_df = df.filter(~pl.col("tok").cast(pl.Int64).is_in([26000, 68407]))
        if len(opt_df) > 0:
            last_opt_ts = float(opt_df["ts"][-1])
            while last_opt_ts > 1e11:
                last_opt_ts /= 1000.0
            print(f"   Part 004 last option tick: {datetime.datetime.fromtimestamp(last_opt_ts).strftime('%H:%M:%S')}, count={len(opt_df)}")
            print("   Tokens in part_004:")
            for sym, strike, cnt in opt_df.group_by(["sym", "strike"]).count().sort("count", descending=True).head(15).iter_rows():
                print(f"      {sym} (strike={strike}): {cnt} ticks")







