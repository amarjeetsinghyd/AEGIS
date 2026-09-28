import polars as pl
df = pl.read_parquet('/home/opc/AEGIS/data/l2_raw/Nifty/aegis_l2_raw_2026-09-21_part_001.parquet')
print("kind value counts:")
for r in df['kind'].value_counts().iter_rows():
    print(f"  kind={r[0]}, count={r[1]}")
print("\nSample rows with sym, kind, strike:")
for r in df.select(['sym', 'kind', 'strike']).head(10).iter_rows():
    print(f"  sym={r[0]}, kind={r[1]}, strike={r[2]}")
