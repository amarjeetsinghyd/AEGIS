import glob
import pandas as pd

files = sorted(glob.glob('/home/opc/AEGIS/data/l2_raw/Nifty/aegis_l2_raw_2026-09-28_part_*.parquet'))
df = pd.read_parquet(files[0])
tok_map = df[['tok', 'sym']].drop_duplicates().set_index('tok')['sym'].to_dict()

target_tokens = [73921, 73900, 73899, 73887, 73888, 73889, 73890, 65899]
for t in target_tokens:
    print(f"{t}: {tok_map.get(t, tok_map.get(str(t), 'Unknown'))}")
