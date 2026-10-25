import glob
import pandas as pd

f = "/home/opc/AEGIS/data/l2_raw/Nifty/aegis_l2_raw_2026-10-12_part_000.parquet"
df = pd.read_parquet(f)
print("Unique symbols in part 000:")
print(df['sym'].unique().tolist())
print("\nUnique kinds:", df['kind'].unique().tolist() if 'kind' in df else "no kind")
