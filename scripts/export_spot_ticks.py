import glob
import pandas as pd

def main():
    nifty_l2_dir = "/home/opc/AEGIS/data/l2_raw/Nifty"
    files_12 = sorted(glob.glob(f"{nifty_l2_dir}/aegis_l2_raw_2026-10-12_part_*.parquet"))
    files_13 = sorted(glob.glob(f"{nifty_l2_dir}/aegis_l2_raw_2026-10-13_part_*.parquet"))
    all_files = files_12 + files_13
    
    ticks = []
    for f in all_files:
        try:
            df = pd.read_parquet(f, columns=['ts', 'tok', 'sym', 'lp'])
            sub = df[(df['tok'] == '26000') | (df['sym'] == 'NIFTY')]
            if not sub.empty:
                ticks.append(sub[['ts', 'lp']])
        except Exception as e:
            pass
            
    full_ticks = pd.concat(ticks, ignore_index=True)
    full_ticks['lp'] = pd.to_numeric(full_ticks['lp'], errors='coerce')
    full_ticks = full_ticks.dropna().sort_values('ts').reset_index(drop=True)
    
    out_file = "/home/opc/nifty_spot_ticks_12_13_oct.parquet"
    full_ticks.to_parquet(out_file, index=False)
    print(f"Saved {len(full_ticks)} raw spot ticks to {out_file}")

if __name__ == '__main__':
    main()
