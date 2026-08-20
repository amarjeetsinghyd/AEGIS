"""
inspect_instruments_db.py — Inspect AEGIS instruments.db and expiry token mapping.
"""
import sqlite3
import pandas as pd

def main():
    con = sqlite3.connect("/home/opc/AEGIS/data/instruments.db")
    tables = pd.read_sql("SELECT name FROM sqlite_master WHERE type='table'", con)
    print("Tables in instruments.db:")
    print(tables)
    
    if "instruments" in tables["name"].values:
        df = pd.read_sql("SELECT * FROM instruments", con)
        print(f"\nTotal instruments: {len(df)}")
        print(df.head(20))
        print("\nInstrument symbol / expiry unique values:")
        if "name" in df.columns:
            print(df["name"].value_counts())
        if "expiry" in df.columns:
            print(df["expiry"].value_counts())
        if "symbol" in df.columns:
            print(df[df["symbol"].str.contains("SENSEX", na=False)].head(20))

if __name__ == "__main__":
    main()
