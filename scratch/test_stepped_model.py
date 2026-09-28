import pandas as pd
import glob

files = sorted(glob.glob('/home/opc/AEGIS/data/l2_raw/Sensex/aegis_l2_raw_2026-09-24_part_*.parquet'))

# User's Stepped Percentage Risk Compression Ladder:
# 1. Initial SL = -3.5%
# 2. When Peak >= +2.0% -> SL moves to -1.0% (3.0% room!)
# 3. When Peak >= +3.0% -> SL moves to Flat 0.0% (Cost / Scratch!)
# 4. Target = +4.0% TP

trades_info = [
    {
        "name": "Trade 1 (11:42 PE)",
        "sym": "SENSEX26SEP73600PE",
        "entry_time": "2026-09-24 11:42:06",
        "entry_px": 25.00,
        "max_hold_sec": 300
    },
    {
        "name": "Trade 2 (12:11 CE)",
        "sym": "SENSEX26SEP74600CE",
        "entry_time": "2026-09-24 12:11:32",
        "entry_px": 28.30,
        "max_hold_sec": 300
    },
    {
        "name": "Trade 3 (13:13 CE)",
        "sym": "SENSEX26SEP74600CE",
        "entry_time": "2026-09-24 13:13:05",
        "entry_px": 24.50,
        "max_hold_sec": 300
    }
]

print("="*75)
print("TESTING USER'S STEPPED PERCENTAGE RISK COMPRESSION LADDER")
print("Rule: Peak >= +2% -> SL = -1.0% | Peak >= +3% -> SL = 0.0% (Cost) | TP = +4.0%")
print("="*75)

for tr in trades_info:
    t_entry = pd.to_datetime(tr['entry_time']).tz_localize('Asia/Kolkata')
    t_end = t_entry + pd.Timedelta(seconds=tr['max_hold_sec'])
    ep = tr['entry_px']
    
    dfs = []
    for f in files:
        try:
            df = pd.read_parquet(f, columns=['ts', 'sym', 'lp', 'bp1', 'sp1', 'vol'])
            sub = df[df['sym'] == tr['sym']].copy()
            if len(sub) == 0: continue
            sub['dt'] = pd.to_datetime(sub['ts'], unit='ms', utc=True).dt.tz_convert('Asia/Kolkata')
            sub_w = sub[(sub['dt'] >= t_entry) & (sub['dt'] <= t_end + pd.Timedelta(seconds=30))]
            if len(sub_w) > 0: dfs.append(sub_w)
        except Exception: pass
        
    ticks = pd.concat(dfs).sort_values('dt').drop_duplicates('ts')
    
    current_sl_pct = -0.035 # start at -3.5%
    peak_pct = 0.0
    exit_type = None
    exit_px = None
    exit_time = None
    
    for idx, row in ticks.iterrows():
        dt = row['dt']
        px = row['lp']
        bid = row['bp1'] if row['bp1'] > 0 else px
        gain_pct = (px - ep) / ep
        if gain_pct > peak_pct:
            peak_pct = gain_pct
            
        # Step Ladder
        if peak_pct >= 0.030: # Peak >= +3.0%
            current_sl_pct = 0.000 # Move to Cost / 0.0%
        elif peak_pct >= 0.020: # Peak >= +2.0%
            current_sl_pct = max(current_sl_pct, -0.010) # Move to -1.0%
            
        # Check TP (+4.0%)
        if px >= ep * 1.04 or bid >= ep * 1.04:
            exit_type = "TAKE_PROFIT_HIT (+4.0%)"
            exit_px = ep * 1.04
            exit_time = dt
            break
            
        # Check SL
        sl_px = ep * (1.0 + current_sl_pct)
        if bid <= sl_px or px <= sl_px:
            exit_type = f"STEPPED_SL_HIT (SL: {current_sl_pct*100:+.1f}%)"
            exit_px = bid
            exit_time = dt
            break
            
        if (dt - t_entry).total_seconds() >= tr['max_hold_sec']:
            exit_type = "5MIN_TIMEOUT"
            exit_px = bid
            exit_time = dt
            break
            
    pnl_rs = exit_px - ep
    pnl_pct = (pnl_rs / ep) * 100
    sec = (exit_time - t_entry).total_seconds()
    print(f"\n{tr['name']}: Entry ₹{ep:.2f}")
    print(f"  Result: {exit_type} at {sec:.1f}s")
    print(f"  Exit Price: ₹{exit_px:.2f} | PnL: {pnl_rs:+.2f} ({pnl_pct:+.2f}%)")
    print(f"  Peak Gain reached: +{peak_pct*100:.2f}% | Final SL was: {current_sl_pct*100:+.1f}%")
