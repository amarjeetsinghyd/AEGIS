import os
import glob
import time
import pandas as pd
import numpy as np

def run_reconstruction():
    nifty_l2_dir = "/home/opc/AEGIS/data/l2_raw/Nifty"
    
    dates = ["2026-10-12", "2026-10-13"]
    
    for d in dates:
        files = sorted(glob.glob(f"{nifty_l2_dir}/aegis_l2_raw_{d}_part_*.parquet"))
        print(f"\nProcessing {len(files)} L2 Archiver parts for {d}...")
        
        # State variables matching AEGIS PriceActionEngine
        spot_price = 0.0
        strike_step = 50.0
        eff_lot = 65.0
        min_vol = 200.0 * eff_lot  # 13,000 lots minimum threshold
        
        opt_last_cum_vol = {}
        opt_last_lp = {}
        opt_last_frac = {}
        
        cum_cb_pv = 0.0
        cum_cb_vol = 0.0
        cum_cs_pv = 0.0
        cum_cs_vol = 0.0
        cum_pb_pv = 0.0
        cum_pb_vol = 0.0
        cum_ps_pv = 0.0
        cum_ps_vol = 0.0
        
        # 1-minute snapshots
        minute_records = []
        curr_minute_bucket = -1
        
        t0 = time.time()
        total_rows = 0
        
        for f in files:
            try:
                df = pd.read_parquet(f, columns=['ts', 'tok', 'sym', 'kind', 'strike', 'lp', 'vol', 'bp1', 'sp1'])
                total_rows += len(df)
            except Exception as e:
                print(f"Error reading {f}: {e}")
                continue
                
            for row in df.itertuples(index=False):
                ts = row.ts
                tok = row.tok
                sym = row.sym
                kind = row.kind
                strike = row.strike
                lp = row.lp
                vol = row.vol
                bp1 = row.bp1
                sp1 = row.sp1
                
                # ts in ms -> sec
                ts_sec = ts / 1000.0
                bucket_1m = int(ts_sec // 60)
                
                # Check 1-minute boundary snapshot
                if bucket_1m != curr_minute_bucket:
                    if curr_minute_bucket != -1 and spot_price > 0:
                        cb = (cum_cb_pv / cum_cb_vol) if cum_cb_vol >= min_vol else 0.0
                        cs = (cum_cs_pv / cum_cs_vol) if cum_cs_vol >= min_vol else 0.0
                        pb = (cum_pb_pv / cum_pb_vol) if cum_pb_vol >= min_vol else 0.0
                        ps = (cum_ps_pv / cum_ps_vol) if cum_ps_vol >= min_vol else 0.0
                        
                        lines = [x for x in (cb, cs, pb, ps) if x > 0]
                        z_top = max(lines) if lines else 0.0
                        z_bottom = min(lines) if lines else 0.0
                        tot_pv = cum_cb_pv + cum_cs_pv + cum_pb_pv + cum_ps_pv
                        tot_v = cum_cb_vol + cum_cs_vol + cum_pb_vol + cum_ps_vol
                        z_mid = (tot_pv / tot_v) if tot_v >= min_vol else 0.0
                        
                        dt = pd.to_datetime(curr_minute_bucket * 60, unit='s', utc=True).tz_convert('Asia/Kolkata')
                        time_str = dt.strftime('%H:%M')
                        
                        if '09:15' <= time_str <= '15:30':
                            minute_records.append({
                                'date': d,
                                'time': time_str,
                                'ts_bucket': curr_minute_bucket,
                                'spot_price': spot_price,
                                'vwap_call_buy': cb,
                                'vwap_call_sell': cs,
                                'vwap_put_buy': pb,
                                'vwap_put_sell': ps,
                                'zone_top': z_top,
                                'zone_bottom': z_bottom,
                                'zone_mid': z_mid,
                                'tot_option_vol': tot_v
                            })
                    curr_minute_bucket = bucket_1m
                    
                # Update spot price
                if tok == '26000' or sym == 'NIFTY' or kind == 0:
                    if lp > 0:
                        spot_price = lp
                    continue
                    
                # Option flow logic (kind == 1 for CE, kind == 2 for PE)
                if kind in (1, 2) and spot_price > 0:
                    # ATM +/- 7 strikes window filter
                    ref_atm = round(spot_price / strike_step) * strike_step
                    if ref_atm > 0 and abs(strike - ref_atm) > (7.0 * strike_step):
                        continue
                        
                    # Volume differencing
                    prev_vol = opt_last_cum_vol.get(sym)
                    if prev_vol is None:
                        opt_last_cum_vol[sym] = vol
                        opt_last_lp[sym] = lp
                        continue
                        
                    delta_vol = vol - prev_vol
                    opt_last_cum_vol[sym] = vol
                    if delta_vol <= 0:
                        continue
                        
                    # Classification (Lee-Ready + Touch-first)
                    valid_quote = (bp1 > 0) and (sp1 > 0) and (sp1 > bp1)
                    if valid_quote and lp >= sp1:
                        frac = 1.0
                    elif valid_quote and lp <= bp1:
                        frac = 0.0
                    else:
                        prev_lp = opt_last_lp.get(sym)
                        if prev_lp is None or prev_lp <= 0:
                            opt_last_lp[sym] = lp
                            continue
                        if lp > prev_lp:
                            frac = 1.0
                        elif lp < prev_lp:
                            frac = 0.0
                        else:
                            frac = opt_last_frac.get(sym)
                            if frac is None:
                                opt_last_lp[sym] = lp
                                continue
                                
                    opt_last_lp[sym] = lp
                    opt_last_frac[sym] = frac
                    
                    buy_vol = delta_vol * frac
                    sell_vol = delta_vol - buy_vol
                    
                    if kind == 1: # CALL
                        cum_cb_pv += spot_price * buy_vol
                        cum_cb_vol += buy_vol
                        cum_cs_pv += spot_price * sell_vol
                        cum_cs_vol += sell_vol
                    elif kind == 2: # PUT
                        cum_pb_pv += spot_price * buy_vol
                        cum_pb_vol += buy_vol
                        cum_ps_pv += spot_price * sell_vol
                        cum_ps_vol += sell_vol

        print(f"Finished {d} in {round(time.time() - t0, 1)}s. Total rows processed: {total_rows}")
        snap_df = pd.DataFrame(minute_records)
        out_file = f"/home/opc/real_pain_zone_{d}.parquet"
        out_csv = f"/home/opc/real_pain_zone_{d}.csv"
        snap_df.to_parquet(out_file, index=False)
        snap_df.to_csv(out_csv, index=False)
        print(f"Saved {len(snap_df)} minutes to {out_file} and {out_csv}")
        if not snap_df.empty:
            print("\nSample Real 4-Line Pain Zone Data:")
            print(snap_df[['time', 'spot_price', 'vwap_call_buy', 'vwap_call_sell', 'vwap_put_buy', 'vwap_put_sell', 'zone_top', 'zone_bottom']].head(10))

if __name__ == '__main__':
    run_reconstruction()
