import polars as pl, glob, sys
from datetime import datetime

DATA_DIR = "/home/opc/AEGIS/data/l2_raw/Nifty"
DATE = "2026-09-22"

# First: understand schema
f = sorted(glob.glob(f"{DATA_DIR}/aegis_l2_raw_{DATE}_part_*.parquet"))[20]
df = pl.read_parquet(f)
print("=== SCHEMA ===")
print("Columns:", df.columns)
print("Dtypes:", df.dtypes)
print("Rows:", len(df))
print("Sample row:")
print(df.head(2).to_pandas().T)
print()

# Find timestamp col and LP col
ts_col = next((c for c in df.columns if c.lower() in ("ts","timestamp","time","t")), None)
lp_col = next((c for c in df.columns if c.lower() in ("lp","ltp","last","last_price","close")), None)
sym_col = next((c for c in df.columns if c.lower() in ("sym","symbol","tsym","tk","token","name")), None)

print(f"ts_col={ts_col}, lp_col={lp_col}, sym_col={sym_col}")

if ts_col:
    sample_ts = df[ts_col][0]
    print(f"Sample TS value: {sample_ts}")
    # Try to parse
    try:
        print(f"  As datetime (if epoch sec): {datetime.fromtimestamp(float(sample_ts))}")
    except:
        pass

# Show unique symbols in this part (look for PE options)
if sym_col:
    syms = df[sym_col].unique().to_list()
    pe_syms = [s for s in syms if "P" in str(s) and "22SEP" in str(s)]
    print(f"\nPE symbols in part_020: {pe_syms[:10]}")
