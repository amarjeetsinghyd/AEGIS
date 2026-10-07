#!/usr/bin/env python3
"""
Empirical Correlation & Predictive Power Audit:
Simple OFI (CallBuy - PutBuy) vs Synthetic Net Delta ((CallBuy + PutSell) - (PutBuy + CallSell))

Tests on real 1-minute option flow logs from AEGIS production logs.
"""

import re
import math
import sys
import numpy as np

LOG_PATH = sys.argv[1] if len(sys.argv) > 1 else "/home/opc/AEGIS/logs/aegis.log"

# Regex pattern for 1M OPTION FLOW
# 2026-09-30 11:21:00 - aegis_price_action - INFO - [1M OPTION FLOW] Close: 72643.45 | CallBuy=21993420, PutBuy=1346160, OFI=+20647260, PCR=0.12, TotalVol=25500980
PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?\[1M OPTION FLOW\] Close:\s*([\d\.]+)\s*\|\s*CallBuy=([\d\.]+),\s*PutBuy=([\d\.]+),\s*OFI=([+-]?[\d\.]+),\s*PCR=([\d\.]+),\s*TotalVol=([\d\.]+)"
)

def parse_logs(log_path, target_date=None):
    records = []
    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if "[1M OPTION FLOW]" not in line:
                continue
            if target_date and target_date not in line:
                continue
            m = PATTERN.search(line)
            if not m:
                continue
            ts_str, close_str, cb_str, pb_str, ofi_str, pcr_str, tot_str = m.groups()
            close_px = float(close_str)
            cb = float(cb_str)
            pb = float(pb_str)
            pcr = float(pcr_str)
            tot_vol = float(tot_str)
            
            if tot_vol <= 0 or close_px <= 0:
                continue
                
            # Decompose CallSell and PutSell from TotalVol and PCR
            # C + P = TotalVol
            # P / C = PCR => C = TotalVol / (1 + PCR)
            c_vol = tot_vol / (1.0 + pcr) if pcr >= 0 else tot_vol
            p_vol = tot_vol - c_vol
            cs = max(0.0, c_vol - cb)
            ps = max(0.0, p_vol - pb)
            
            # Synthetic Volumes
            bull_vol = cb + ps
            bear_vol = pb + cs
            simple_ofi = cb - pb
            synth_delta = bull_vol - bear_vol
            
            records.append({
                "ts": ts_str,
                "close": close_px,
                "cb": cb,
                "pb": pb,
                "cs": cs,
                "ps": ps,
                "tot_vol": tot_vol,
                "bull_vol": bull_vol,
                "bear_vol": bear_vol,
                "simple_ofi": simple_ofi,
                "synth_delta": synth_delta,
            })
    return records

def compute_stats(records):
    if len(records) < 10:
        return None
    
    n = len(records)
    cb_arr = np.array([r["cb"] for r in records])
    pb_arr = np.array([r["pb"] for r in records])
    cs_arr = np.array([r["cs"] for r in records])
    ps_arr = np.array([r["ps"] for r in records])
    
    simple_ofi_arr = np.array([r["simple_ofi"] for r in records])
    synth_delta_arr = np.array([r["synth_delta"] for r in records])
    closes = np.array([r["close"] for r in records])
    
    # Calculate forward returns
    ret_1m = np.zeros(n - 1)
    ret_3m = np.zeros(n - 3) if n > 3 else np.array([])
    
    for i in range(n - 1):
        ret_1m[i] = closes[i + 1] - closes[i]
    for i in range(n - 3):
        ret_3m[i] = closes[i + 3] - closes[i]
        
    def corr(x, y):
        if len(x) != len(y) or len(x) == 0:
            return 0.0
        std_x = np.std(x)
        std_y = np.std(y)
        if std_x == 0 or std_y == 0:
            return 0.0
        return float(np.corrcoef(x, y)[0, 1])

    # 1. Internal Flow Correlations
    corr_cb_ps = corr(cb_arr, ps_arr)
    corr_pb_cs = corr(pb_arr, cs_arr)
    corr_bull_bear = corr(cb_arr + ps_arr, pb_arr + cs_arr)
    
    # 2. Predictive Power (Correlation with Forward Spot Move)
    s_ofi_1m = simple_ofi_arr[:-1]
    sy_del_1m = synth_delta_arr[:-1]
    
    corr_simple_1m = corr(s_ofi_1m, ret_1m)
    corr_synth_1m = corr(sy_del_1m, ret_1m)
    
    corr_simple_3m = corr(simple_ofi_arr[:-3], ret_3m) if len(ret_3m) > 0 else 0.0
    corr_synth_3m = corr(synth_delta_arr[:-3], ret_3m) if len(ret_3m) > 0 else 0.0
    
    # 3. Directional Hit Rate (Sign matching)
    valid_1m = (s_ofi_1m != 0) & (ret_1m != 0)
    hit_simple_1m = np.mean(np.sign(s_ofi_1m[valid_1m]) == np.sign(ret_1m[valid_1m])) if np.sum(valid_1m) > 0 else 0.0
    
    valid_sy_1m = (sy_del_1m != 0) & (ret_1m != 0)
    hit_synth_1m = np.mean(np.sign(sy_del_1m[valid_sy_1m]) == np.sign(ret_1m[valid_sy_1m])) if np.sum(valid_sy_1m) > 0 else 0.0
    
    return {
        "bars": n,
        "corr_cb_ps": corr_cb_ps,
        "corr_pb_cs": corr_pb_cs,
        "corr_bull_bear": corr_bull_bear,
        "corr_simple_1m": corr_simple_1m,
        "corr_synth_1m": corr_synth_1m,
        "corr_simple_3m": corr_simple_3m,
        "corr_synth_3m": corr_synth_3m,
        "hit_simple_1m": hit_simple_1m,
        "hit_synth_1m": hit_synth_1m,
    }

def main():
    print(f"Loading data from: {LOG_PATH}")
    dates = ["2026-09-29", "2026-09-30"]
    
    for d in dates:
        recs = parse_logs(LOG_PATH, target_date=d)
        print(f"\n==========================================")
        print(f"Results for Date: {d} (Total 1M Bars: {len(recs)})")
        print(f"==========================================")
        stats = compute_stats(recs)
        if not stats:
            print("Insufficient records.")
            continue
        print(f"1. Flow Component Harmony:")
        print(f"   - Corr(CallBuy, PutSell) [Bullish Harmony]:  {stats['corr_cb_ps']:+.4f}")
        print(f"   - Corr(PutBuy, CallSell) [Bearish Harmony]:  {stats['corr_pb_cs']:+.4f}")
        print(f"   - Corr(Total Bull, Total Bear):              {stats['corr_bull_bear']:+.4f}")
        print(f"\n2. Predictive Power with Forward Spot Move (1-Min Return):")
        print(f"   - Simple OFI (CallBuy - PutBuy):             {stats['corr_simple_1m']:+.4f}")
        print(f"   - Synthetic Net Delta (Full 4-Way):          {stats['corr_synth_1m']:+.4f}")
        print(f"   - Directional Hit Rate (Simple OFI):         {stats['hit_simple_1m']*100:.2f}%")
        print(f"   - Directional Hit Rate (Synthetic Delta):    {stats['hit_synth_1m']*100:.2f}%")
        print(f"\n3. Predictive Power with Forward Spot Move (3-Min Return):")
        print(f"   - Simple OFI (3-Min):                        {stats['corr_simple_3m']:+.4f}")
        print(f"   - Synthetic Net Delta (3-Min):               {stats['corr_synth_3m']:+.4f}")

    # All dates combined
    all_recs = parse_logs(LOG_PATH)
    print(f"\n==========================================")
    print(f"COMBINED AUDIT (Total Bars: {len(all_recs)})")
    print(f"==========================================")
    stats_all = compute_stats(all_recs)
    if stats_all:
        print(f"1. Flow Component Harmony:")
        print(f"   - Corr(CallBuy, PutSell):                    {stats_all['corr_cb_ps']:+.4f}")
        print(f"   - Corr(PutBuy, CallSell):                    {stats_all['corr_pb_cs']:+.4f}")
        print(f"\n2. Predictive Power (1-Min Spot Return):")
        print(f"   - Simple OFI Corr:                           {stats_all['corr_simple_1m']:+.4f}")
        print(f"   - Synthetic Delta Corr:                      {stats_all['corr_synth_1m']:+.4f}")
        print(f"   - Hit Rate (Simple OFI):                     {stats_all['hit_simple_1m']*100:.2f}%")
        print(f"   - Hit Rate (Synthetic Delta):                {stats_all['hit_synth_1m']*100:.2f}%")
        print(f"\n3. Predictive Power (3-Min Spot Return):")
        print(f"   - Simple OFI (3-Min):                        {stats_all['corr_simple_3m']:+.4f}")
        print(f"   - Synthetic Net Delta (3-Min):               {stats_all['corr_synth_3m']:+.4f}")

if __name__ == "__main__":
    main()
