import pandas as pd
import glob

files = sorted(glob.glob('/home/opc/AEGIS/data/l2_raw/Sensex/aegis_l2_raw_2026-09-24_part_*.parquet'))

def analyze_trade(sym, start_str, end_str, entry_time_str, entry_px, exit_time_str, exit_px, title):
    print(f"\n{'='*70}")
    print(f"FORENSIC TICK ANALYSIS: {title}")
    print(f"Symbol: {sym} | Entry: ₹{entry_px} @ {entry_time_str} | Bot Exit: ₹{exit_px} @ {exit_time_str}")
    print(f"Observation Window: {start_str} to {end_str}")
    print(f"{'='*70}")
    
    t_start = pd.to_datetime(start_str).tz_localize('Asia/Kolkata')
    t_end = pd.to_datetime(end_str).tz_localize('Asia/Kolkata')
    t_entry = pd.to_datetime(entry_time_str).tz_localize('Asia/Kolkata')
    t_exit = pd.to_datetime(exit_time_str).tz_localize('Asia/Kolkata')
    
    dfs = []
    for f in files:
        try:
            df = pd.read_parquet(f, columns=['ts', 'sym', 'lp', 'bp1', 'sp1', 'vol'])
            sub = df[df['sym'] == sym].copy()
            if len(sub) == 0: continue
            
            sub['dt'] = pd.to_datetime(sub['ts'], unit='ms', utc=True).dt.tz_convert('Asia/Kolkata')
            sub_window = sub[(sub['dt'] >= t_start) & (sub['dt'] <= t_end)]
            if len(sub_window) > 0:
                dfs.append(sub_window)
        except Exception as e:
            pass
            
    if not dfs:
        print(f"No ticks found for {sym} in {start_str} to {end_str}")
        return
        
    full = pd.concat(dfs).sort_values('dt').drop_duplicates('ts')
    print(f"Total ticks recorded in window: {len(full)}")
    
    # 1. During Bot Trade Window (Entry to Bot Exit)
    trade_window = full[(full['dt'] >= t_entry) & (full['dt'] <= t_exit)]
    if len(trade_window) > 0:
        bot_mfe = trade_window['lp'].max()
        bot_mae = trade_window['lp'].min()
        print(f"--- DURING BOT TRADE (Hold: {(t_exit - t_entry).total_seconds():.0f}s) ---")
        print(f"  Entry: ₹{entry_px} -> Bot Exit: ₹{exit_px} (Gain: +₹{exit_px - entry_px:.2f} / +{(exit_px - entry_px)/entry_px*100:.2f}%)")
        print(f"  Max Price reached before exit: ₹{bot_mfe:.2f} (+₹{bot_mfe - entry_px:.2f} / +{(bot_mfe - entry_px)/entry_px*100:.2f}%)")
        print(f"  Min Price reached before exit: ₹{bot_mae:.2f} (-₹{entry_px - bot_mae:.2f} / -{(entry_px - bot_mae)/entry_px*100:.2f}%)")
    
    # 2. After Exit: Post-Exit Trajectory (Next 1 min, 3 min, 5 min, 10 min)
    post_window = full[full['dt'] >= t_entry]
    
    # Max price within 1 min, 3 min, 5 min of entry
    w_1m = full[(full['dt'] >= t_entry) & (full['dt'] <= t_entry + pd.Timedelta(seconds=60))]
    w_3m = full[(full['dt'] >= t_entry) & (full['dt'] <= t_entry + pd.Timedelta(minutes=3))]
    w_5m = full[(full['dt'] >= t_entry) & (full['dt'] <= t_entry + pd.Timedelta(minutes=5))]
    w_10m = full[(full['dt'] >= t_entry) & (full['dt'] <= t_entry + pd.Timedelta(minutes=10))]
    
    print("\n--- POST-ENTRY TRAJECTORY (What happened after entry?) ---")
    if len(w_1m) > 0:
        max_1m = w_1m['lp'].max()
        min_1m = w_1m['lp'].min()
        t_max_1m = w_1m[w_1m['lp'] == max_1m]['dt'].iloc[0].strftime('%H:%M:%S')
        print(f"  Within 1 Min:  High: ₹{max_1m:.2f} (+{(max_1m - entry_px)/entry_px*100:.2f}%) @ {t_max_1m} | Low: ₹{min_1m:.2f} ({(min_1m - entry_px)/entry_px*100:.2f}%)")
    if len(w_3m) > 0:
        max_3m = w_3m['lp'].max()
        min_3m = w_3m['lp'].min()
        t_max_3m = w_3m[w_3m['lp'] == max_3m]['dt'].iloc[0].strftime('%H:%M:%S')
        t_min_3m = w_3m[w_3m['lp'] == min_3m]['dt'].iloc[0].strftime('%H:%M:%S')
        print(f"  Within 3 Mins: High: ₹{max_3m:.2f} (+{(max_3m - entry_px)/entry_px*100:.2f}%) @ {t_max_3m} | Low: ₹{min_3m:.2f} ({(min_3m - entry_px)/entry_px*100:.2f}%) @ {t_min_3m}")
    if len(w_5m) > 0:
        max_5m = w_5m['lp'].max()
        min_5m = w_5m['lp'].min()
        t_max_5m = w_5m[w_5m['lp'] == max_5m]['dt'].iloc[0].strftime('%H:%M:%S')
        t_min_5m = w_5m[w_5m['lp'] == min_5m]['dt'].iloc[0].strftime('%H:%M:%S')
        print(f"  Within 5 Mins: High: ₹{max_5m:.2f} (+{(max_5m - entry_px)/entry_px*100:.2f}%) @ {t_max_5m} | Low: ₹{min_5m:.2f} ({(min_5m - entry_px)/entry_px*100:.2f}%) @ {t_min_5m}")
    if len(w_10m) > 0:
        max_10m = w_10m['lp'].max()
        min_10m = w_10m['lp'].min()
        t_max_10m = w_10m[w_10m['lp'] == max_10m]['dt'].iloc[0].strftime('%H:%M:%S')
        t_min_10m = w_10m[w_10m['lp'] == min_10m]['dt'].iloc[0].strftime('%H:%M:%S')
        print(f"  Within 10 Mins:High: ₹{max_10m:.2f} (+{(max_10m - entry_px)/entry_px*100:.2f}%) @ {t_max_10m} | Low: ₹{min_10m:.2f} ({(min_10m - entry_px)/entry_px*100:.2f}%) @ {t_min_10m}")

    # Resampled 5-second candles for first 3 minutes
    full_sub = full[(full['dt'] >= t_entry - pd.Timedelta(seconds=10)) & (full['dt'] <= t_entry + pd.Timedelta(minutes=3))].copy()
    full_sub.set_index('dt', inplace=True)
    c5s = full_sub['lp'].resample('5s').ohlc().dropna()
    print("\n--- 5-SECOND CANDLES AROUND TRADE ---")
    for ts, r in c5s.iterrows():
        star = " <-- ENTRY" if (ts <= t_entry <= ts + pd.Timedelta(seconds=5)) else (" <-- BOT EXIT" if (ts <= t_exit <= ts + pd.Timedelta(seconds=5)) else "")
        print(f"  {ts.strftime('%H:%M:%S')} | O:{r['open']:.2f} H:{r['high']:.2f} L:{r['low']:.2f} C:{r['close']:.2f}{star}")

# Trade 1
analyze_trade('SENSEX26SEP73600PE', '2026-09-24 11:41:50', '2026-09-24 11:53:00', '2026-09-24 11:42:06', 25.00, '2026-09-24 11:42:31', 25.65, 'Trade 1: SENSEX 73600 PE')

# Trade 2
analyze_trade('SENSEX26SEP74600CE', '2026-09-24 12:11:15', '2026-09-24 12:22:00', '2026-09-24 12:11:32', 28.30, '2026-09-24 12:11:58', 28.70, 'Trade 2: SENSEX 74600 CE')

# Trade 3
analyze_trade('SENSEX26SEP74600CE', '2026-09-24 13:12:50', '2026-09-24 13:24:00', '2026-09-24 13:13:05', 24.50, '2026-09-24 13:13:13', 24.95, 'Trade 3: SENSEX 74600 CE')
