import pandas as pd

f = "/home/opc/AEGIS/data/l2_raw/Nifty/aegis_l2_raw_2026-10-12_part_000.parquet"
df = pd.read_parquet(f)
sub = df[df['sym'] == 'NIFTY']
print("NIFTY Spot rows in part 000:", len(sub))
print(sub.head(5)[['ts', 'tok', 'sym', 'kind', 'lp', 'vol']])
