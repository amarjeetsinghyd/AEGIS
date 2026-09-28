import os
import glob
import pandas as pd

files = sorted(glob.glob('/home/opc/AEGIS/data/l2_raw/Nifty/aegis_l2_raw_2026-09-28_part_*.parquet'))
print(f"Total partitions found: {len(files)}")
if files:
    df0 = pd.read_parquet(files[0])
    print("Columns:", df0.columns.tolist())
    print("Shape:", df0.shape)
    print("First 3 rows:")
    print(df0[['ts', 'tok', 'sym', 'kind', 'strike', 'lp', 'vol']].head(5))
    print("Unique kinds:", df0['kind'].unique().tolist())
    print("Underlying rows in part 0:", len(df0[df0['tok'].isin(['26000', '1'])]))
    
    df20 = pd.read_parquet(files[20])
    print("Part 20 shape:", df20.shape)
    print("Part 20 unique tokens:")
    print(df20['tok'].value_counts())
    print("Part 20 unique symbols:")
    print(df20['sym'].value_counts())





