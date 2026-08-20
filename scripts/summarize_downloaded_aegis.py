"""
summarize_downloaded_aegis.py — Summarize all downloaded AEGIS L2 Options files.
"""
import glob
import os
import sys
import pandas as pd

def main():
    root = r"c:\Users\Amarjeet Singh\quant-bot\AEGIS\data\l2_raw"
    files = sorted(glob.glob(os.path.join(root, "*.parquet")))
    print(f"Total downloaded AEGIS files: {len(files)}")
    
    date_summary = {}
    for f in files:
        fname = os.path.basename(f)
        parts = fname.split("_")
        date_str = parts[3] if len(parts) >= 4 else "unknown"
        df = pd.read_parquet(f)
        
        tok_col = 'tok' if 'tok' in df.columns else 'token'
        tokens = df[tok_col].unique() if tok_col in df.columns else []
        sym_col = 'sym' if 'sym' in df.columns else 'symbol'
        symbols = df[sym_col].unique() if sym_col in df.columns else []
        
        if date_str not in date_summary:
            date_summary[date_str] = {"files": 0, "rows": 0, "size_mb": 0.0, "tokens": set(), "symbols": set()}
        date_summary[date_str]["files"] += 1
        date_summary[date_str]["rows"] += len(df)
        date_summary[date_str]["size_mb"] += os.path.getsize(f) / (1024 * 1024)
        date_summary[date_str]["tokens"].update(tokens)
        date_summary[date_str]["symbols"].update(symbols)

    print("\n" + "=" * 80)
    print("LOCAL AEGIS L2 ARCHIVE INVENTORY SUMMARY")
    print("=" * 80)
    for d, info in sorted(date_summary.items()):
        syms = [str(s) for s in list(info['symbols'])[:5] if s is not None]
        print(f"Date: {d} | Files: {info['files']:2d} | Rows: {info['rows']:10,d} | Size: {info['size_mb']:5.2f} MB | Tokens: {len(info['tokens']):3d} | Symbols: {syms}")
    print("=" * 80)

if __name__ == "__main__":
    main()
