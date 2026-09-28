import pandas as pd
import glob
import json

files = sorted(glob.glob('/home/opc/AEGIS/data/l2_raw/Sensex/aegis_l2_raw_2026-09-24_part_*.parquet'))

# We want to simulate the 4 Pillars for the 3 trades:
# Rule 1: TP (+4.0% Limit Order)
# Rule 2: Spot Invalidation (9-EMA / 20-EMA / 1m Bar Structure)
# Rule 3: Emergency Hard SL (-12.0%)
# Rule 4: 5-Minute Timeout (Exit at Market after 300s)

# Load 1m spot bars from today's telemetry or calculate from ticks
# In telemetry, we have entry_spot, spot_sl_price, etc.

trades_info = [
    {
        "name": "Trade 1 (11:42 PE)",
        "sym": "SENSEX26SEP73600PE",
        "side": "PUT",
        "entry_time": "2026-09-24 11:42:06",
        "entry_px": 25.00,
        "entry_spot": 74100.90,
        "spot_sl": 74125.0, # Spot SL for PUT (rally above EMA)
        "tp_pct": 0.04,     # +4.0% -> 26.00
        "tp_px": 26.00,
        "emergency_sl_pct": -0.12, # -12% -> 22.00
        "emergency_sl_px": 22.00,
        "max_hold_sec": 300
    },
    {
        "name": "Trade 2 (12:11 CE)",
        "sym": "SENSEX26SEP74600CE",
        "side": "CALL",
        "entry_time": "2026-09-24 12:11:32",
        "entry_px": 28.30,
        "entry_spot": 74086.03,
        "spot_sl": 74060.0,
        "tp_pct": 0.04,     # +4.0% -> 29.45
        "tp_px": 29.45,
        "emergency_sl_pct": -0.12, # -12% -> 24.90
        "emergency_sl_px": 24.90,
        "max_hold_sec": 300
    },
    {
        "name": "Trade 3 (13:13 CE)",
        "sym": "SENSEX26SEP74600CE",
        "side": "CALL",
        "entry_time": "2026-09-24 13:13:05",
        "entry_px": 24.50,
        "entry_spot": 73999.29,
        "spot_sl": 73971.31,
        "tp_pct": 0.04,     # +4.0% -> 25.50 (24.5 * 1.04 = 25.48)
        "tp_px": 25.48,
        "emergency_sl_pct": -0.12, # -12% -> 21.56
        "emergency_sl_px": 21.56,
        "max_hold_sec": 300
    }
]

print("="*75)
print("SIMULATION: 4-PILLAR EXITS (TP +4% vs Spot Invalidation vs Emer SL -12% vs 5m Timeout)")
print("="*75)

for tr in trades_info:
    t_entry = pd.to_datetime(tr['entry_time']).tz_localize('Asia/Kolkata')
    t_end = t_entry + pd.Timedelta(seconds=tr['max_hold_sec'])
    
    # Load ticks for this option
    dfs = []
    for f in files:
        try:
            df = pd.read_parquet(f, columns=['ts', 'sym', 'lp', 'bp1', 'sp1', 'vol'])
            sub = df[df['sym'] == tr['sym']].copy()
            if len(sub) == 0: continue
            sub['dt'] = pd.to_datetime(sub['ts'], unit='ms', utc=True).dt.tz_convert('Asia/Kolkata')
            sub_w = sub[(sub['dt'] >= t_entry) & (sub['dt'] <= t_end + pd.Timedelta(seconds=30))]
            if len(sub_w) > 0:
                dfs.append(sub_w)
        except Exception:
            pass
            
    ticks = pd.concat(dfs).sort_values('dt').drop_duplicates('ts')
    
    tp_hit = False
    tp_time = None
    tp_val = None
    
    emer_hit = False
    emer_time = None
    emer_val = None
    
    # Iterate through ticks chronologically
    for idx, row in ticks.iterrows():
        dt = row['dt']
        sec = (dt - t_entry).total_seconds()
        px = row['lp']
        bid = row['bp1']
        ask = row['sp1']
        
        # Check TP (+4% hit if ask or ltp touches/crosses tp_px)
        if px >= tr['tp_px'] or bid >= tr['tp_px']:
            tp_hit = True
            tp_time = dt
            tp_val = px
            break
            
        # Check Emergency SL (-12% hit if bid drops <= emergency_sl_px)
        if bid > 0 and bid <= tr['emergency_sl_px']:
            emer_hit = True
            emer_time = dt
            emer_val = bid
            break
        elif px <= tr['emergency_sl_px']:
            emer_hit = True
            emer_time = dt
            emer_val = px
            break
            
        if sec >= tr['max_hold_sec']:
            break
            
    timeout_tick = ticks[ticks['dt'] <= t_end].iloc[-1] if len(ticks[ticks['dt'] <= t_end]) > 0 else ticks.iloc[-1]
    
    print(f"\n--- {tr['name']} ---")
    print(f"Entry: ₹{tr['entry_px']:.2f} @ {tr['entry_time']}")
    print(f"Target (+4.0% TP): ₹{tr['tp_px']:.2f} | Emergency SL (-12%): ₹{tr['emergency_sl_px']:.2f} | Timeout: {t_end.strftime('%H:%M:%S')}")
    
    if tp_hit:
        sec_to_tp = (tp_time - t_entry).total_seconds()
        print(f"🏆 RESULT: TAKE-PROFIT HIT (+4.0%)!")
        print(f"   Filled at: ₹{tr['tp_px']:.2f} (LTP reached ₹{tp_val:.2f})")
        print(f"   Time to TP: {sec_to_tp:.1f} seconds (at {tp_time.strftime('%H:%M:%S')})")
        print(f"   Points Gained: +₹{tr['tp_px'] - tr['entry_px']:.2f} (+4.00%)")
    elif emer_hit:
        sec_to_sl = (emer_time - t_entry).total_seconds()
        print(f"❌ RESULT: EMERGENCY STOP-LOSS HIT (-12.0%)!")
        print(f"   Executed at: ₹{emer_val:.2f} at {sec_to_sl:.1f}s (at {emer_time.strftime('%H:%M:%S')})")
        print(f"   Points Loss: -₹{tr['entry_px'] - emer_val:.2f} (-12.0%)")
    else:
        # Check what price was at 5-minute timeout
        to_px = timeout_tick['lp']
        to_pnl_rs = to_px - tr['entry_px']
        to_pnl_pct = (to_pnl_rs / tr['entry_px']) * 100
        sec_held = (timeout_tick['dt'] - t_entry).total_seconds()
        print(f"⏱️ RESULT: 5-MINUTE TIMEOUT (Neither TP nor -12% SL hit in 300s)")
        print(f"   Price at 5 mins ({timeout_tick['dt'].strftime('%H:%M:%S')}): ₹{to_px:.2f}")
        print(f"   PnL at Timeout: {to_pnl_rs:+.2f} ({to_pnl_pct:+.2f}%)")
        
    # Also print Peak MFE and Peak MAE within the 5 minutes
    ticks_5m = ticks[ticks['dt'] <= t_end]
    if len(ticks_5m) > 0:
        max_px = ticks_5m['lp'].max()
        min_px = ticks_5m['lp'].min()
        t_max = ticks_5m[ticks_5m['lp'] == max_px]['dt'].iloc[0].strftime('%H:%M:%S')
        t_min = ticks_5m[ticks_5m['lp'] == min_px]['dt'].iloc[0].strftime('%H:%M:%S')
        print(f"   [5-Min Range]: Highest = ₹{max_px:.2f} (+{(max_px - tr['entry_px'])/tr['entry_px']*100:.1f}%) @ {t_max} | Lowest = ₹{min_px:.2f} ({(min_px - tr['entry_px'])/tr['entry_px']*100:.1f}%) @ {t_min}")
