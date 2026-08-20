import glob
import os
import pandas as pd

def main():
    root = "/home/opc/ARGUS/data/l2_raw"
    files = sorted(glob.glob(os.path.join(root, "**", "*.parquet"), recursive=True))
    print(f"Total parquet files found: {len(files)}")
    
    date_summary = {}

    for f in files:
        try:
            sample = pd.read_parquet(f)
            ts_col = "exch_ts" if "exch_ts" in sample.columns else "ts"
            tok_col = "tok" if "tok" in sample.columns else "token"
            
            ts_vals = sample[ts_col]
            min_ts = ts_vals.min()
            max_ts = ts_vals.max()
            if min_ts > 1e11:
                min_ts /= 1000.0
                max_ts /= 1000.0
            
            min_dt = pd.to_datetime(min_ts, unit="s", utc=True).tz_convert("Asia/Kolkata")
            max_dt = pd.to_datetime(max_ts, unit="s", utc=True).tz_convert("Asia/Kolkata")
            date_str = str(min_dt.date())
            
            if date_str not in date_summary:
                date_summary[date_str] = {
                    "files": 0, "ticks": 0, "min_time": min_dt, "max_time": max_dt, "tokens": set()
                }
            
            date_summary[date_str]["files"] += 1
            date_summary[date_str]["ticks"] += len(sample)
            if min_dt < date_summary[date_str]["min_time"]:
                date_summary[date_str]["min_time"] = min_dt
            if max_dt > date_summary[date_str]["max_time"]:
                date_summary[date_str]["max_time"] = max_dt
            date_summary[date_str]["tokens"].update(sample[tok_col].unique())
            
        except Exception as e:
            print(f"Error reading {f}: {e}")

    print("\n" + "=" * 90)
    print("PARQUET DATASET TIMESTAMPS SUMMARY BY DATE")
    print("=" * 90)
    for d, s in sorted(date_summary.items()):
        print(f"Date: {d:<12} | Files: {s['files']:<3} | Ticks: {s['ticks']:>10,} | Tokens: {len(s['tokens']):<3} | Start: {s['min_time'].strftime('%H:%M:%S')} | End: {s['max_time'].strftime('%H:%M:%S')}")

if __name__ == "__main__":
    main()
