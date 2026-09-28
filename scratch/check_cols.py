import pandas as pd
import glob
files = sorted(glob.glob('/home/opc/AEGIS/data/l2_raw/Sensex/aegis_l2_raw_2026-09-24_part_*.parquet'))
df = pd.read_parquet(files[13]) # around part 13/14
print("COLUMNS:", df.columns.tolist())
print("HEAD:\n", df.head(3))
