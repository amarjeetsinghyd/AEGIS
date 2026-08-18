#!/usr/bin/env python3
"""Check what symbols are in the VORTEX daily snapshot."""
import sys
sys.path.insert(0, "/home/opc")
import pandas as pd

df = pd.read_parquet("/home/opc/stock_order_flow/data/analytics/daily/vortex_2026-08-17.parquet")
print("Columns:", list(df.columns))
print("Shape:", df.shape)
print("Symbols:", df["symbol"].tolist() if "symbol" in df.columns else "No symbol column")
print(df.head())