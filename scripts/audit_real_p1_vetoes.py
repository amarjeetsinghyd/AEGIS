"""
Real P1 Candidate Veto & Ground-Truth Forward Outcome Audit
Date: 2026-09-21
"""

import sys, os
sys.path.insert(0, os.getcwd())
import pandas as pd
import numpy as np
from AEGIS.core.price_action_engine import PriceActionEngine, OHLCBar
from AEGIS.core.wyckoff_volume_engine import WyckoffVolumeEngine

df = pd.read_csv('scripts/nifty_1m_with_5_vwaps_2026-09-21.csv')
n = len(df)

# Rolling 90-min VDR & Z_vol calculation
df['v_base'] = df['total_opt_vol'].rolling(20, min_periods=5).mean()
df['v_approach'] = (df['total_opt_vol'].shift(1) + df['total_opt_vol'].shift(2) + df['total_opt_vol'].shift(3)) / 3.0
df['vdr'] = df['v_approach'] / (df['v_base'].shift(1) + 1e-6)
df['vdr_mean_90'] = df['vdr'].rolling(90, min_periods=20).mean()
df['vdr_std_90'] = df['vdr'].rolling(90, min_periods=20).std()
df['z_vol'] = (df['vdr'] - df['vdr_mean_90']) / (df['vdr_std_90'] + 1e-6)

# Rolling 5-min 4-Flow Imbalance (I_norm)
df['bull_flow'] = df['call_buy_vol'] + df['put_sell_vol']
df['bear_flow'] = df['put_buy_vol'] + df['call_sell_vol']
df['tot_4flow'] = df['bull_flow'] + df['bear_flow']
df['delta_5m'] = (df['bull_flow'].rolling(5).sum()) - (df['bear_flow'].rolling(5).sum())
df['tot_4flow_5m'] = df['tot_4flow'].rolling(5).sum()
df['i_norm_5m'] = df['delta_5m'] / (df['tot_4flow_5m'] + 1e-6)

pae = PriceActionEngine(strike_step=50.0)
wyckoff = WyckoffVolumeEngine(mode="ACTIVE")

results = []

for i, row in df.iterrows():
    ts = 1726910100.0 + i * 60.0
    bar = OHLCBar(
        ts_start=ts - 60.0,
        ts_end=ts,
        open=row['open'],
        high=row['high'],
        low=row['low'],
        close=row['close'],
        volume=row['total_opt_vol'],
        call_buy_vol=row['call_buy_vol'],
        call_sell_vol=row['call_sell_vol'],
        put_buy_vol=row['put_buy_vol'],
        put_sell_vol=row['put_sell_vol'],
        opt_delta_flow=row['call_buy_vol'] - row['put_buy_vol'],
        opt_total_vol=row['total_opt_vol']
    )
    pae.bars_1m.append(bar)
    pae.last_price = row['close']
    pae.current_vwap = row['vwap_composite']
    
    if len(pae.bars_1m) >= 20:
        closes = [b.close for b in pae.bars_1m]
        pae.ema_20_1m = pd.Series(closes).ewm(span=20, adjust=False).mean().iloc[-1]
        pae.ema_20_history.append(pae.ema_20_1m)
        pae.current_atr_1m = 10.0
        
        for dir_name in ['CALL', 'PUT']:
            ok_p1, r_p1, d_p1 = pae.check_20_ema_rejection(dir_name, current_vwap=row['vwap_composite'])
            if ok_p1 and i <= n - 6:
                # 1. Current Wyckoff Evaluation
                w_pass, w_reason, w_metrics = wyckoff.evaluate(
                    pillar=1,
                    direction=dir_name,
                    bars=list(pae.bars_1m),
                    atr=10.0,
                    pa_details=d_p1
                )
                
                # Check individual current vetoes:
                cb = row['call_buy_vol']
                pb = row['put_buy_vol']
                vdr = row['vdr']
                v_slope = df['total_opt_vol'].iloc[i-1] - df['total_opt_vol'].iloc[i-3] if i >= 3 else 0.0
                
                cur_vdr_veto = (vdr >= 1.25) and (v_slope > 0)
                cur_delta_veto = (pb > 2.2 * max(cb, 1.0)) if dir_name == "CALL" else (cb > 2.2 * max(pb, 1.0))
                
                # 2. DeepSeek Proposed Upgrades
                z_vol = row['z_vol']
                prop_zvol_veto = (z_vol >= 1.5) and (v_slope > 0)
                i_norm = row['i_norm_5m']
                vol_5m = row['tot_4flow_5m']
                has_min_floor = vol_5m >= 5000
                prop_inorm_veto = ((i_norm <= -0.30) if dir_name == "CALL" else (i_norm >= +0.30)) and has_min_floor
                prop_pass = not (prop_zvol_veto or prop_inorm_veto)
                
                # 3. Ground Truth Forward 5m Outcome
                close_p = row['close']
                fwd = df.iloc[i+1 : i+6]
                fwd_highs = fwd['high'].values
                fwd_lows = fwd['low'].values
                fwd_closes = fwd['close'].values
                
                if dir_name == "CALL":
                    mfe = max(fwd_highs) - close_p
                    mae = close_p - min(fwd_lows)
                    fwd_delta = fwd_closes[-1] - close_p
                    hit_tp = any((h - close_p) >= 8.0 for h in fwd_highs)
                    hit_sl = any((close_p - l) >= 14.0 for l in fwd_lows)
                else:
                    mfe = close_p - min(fwd_lows)
                    mae = max(fwd_highs) - close_p
                    fwd_delta = close_p - fwd_closes[-1]
                    hit_tp = any((close_p - l) >= 8.0 for l in fwd_lows)
                    hit_sl = any((h - close_p) >= 14.0 for h in fwd_highs)
                    
                is_win = hit_tp and not hit_sl
                is_loss = hit_sl
                
                results.append({
                    "time": row['time'],
                    "direction": dir_name,
                    "close": close_p,
                    "rejection_type": d_p1.get("rejection_type"),
                    "vdr": round(vdr, 2),
                    "cb": cb,
                    "pb": pb,
                    "ratio": round(pb / max(cb, 1.0) if dir_name == "CALL" else cb / max(pb, 1.0), 2),
                    "cur_pass": w_pass,
                    "cur_reason": w_reason,
                    "cur_vdr_veto": cur_vdr_veto,
                    "cur_delta_veto": cur_delta_veto,
                    "z_vol": round(z_vol, 2) if not np.isnan(z_vol) else 0.0,
                    "i_norm_5m": round(i_norm, 3) if not np.isnan(i_norm) else 0.0,
                    "prop_pass": prop_pass,
                    "prop_zvol_veto": prop_zvol_veto,
                    "prop_inorm_veto": prop_inorm_veto,
                    "mfe": round(mfe, 2),
                    "mae": round(mae, 2),
                    "fwd_delta": round(fwd_delta, 2),
                    "win": is_win,
                    "loss": is_loss
                })

res_df = pd.DataFrame(results)
print(f"=== PILLAR 1 CANDIDATES EVALUATED: {len(res_df)} ===")
print("\n--- CURRENT SYSTEM ---")
print("Passed:", res_df['cur_pass'].sum())
print("Vetoed (Total):", (~res_df['cur_pass']).sum())
print("Vetoed by VDR 1.25x:", res_df['cur_vdr_veto'].sum())
print("Vetoed by 2.2x Delta:", res_df['cur_delta_veto'].sum())

print("\n--- DEEPSEEK PROPOSED UPGRADES ---")
print("Passed:", res_df['prop_pass'].sum())
print("Vetoed (Total):", (~res_df['prop_pass']).sum())
print("Vetoed by Z_vol >= 1.5:", res_df['prop_zvol_veto'].sum())
print("Vetoed by I_norm 5m (|I|>=0.30):", res_df['prop_inorm_veto'].sum())

print("\n--- DETAILED AUDIT OF EVERY VETOED SETUP ---")
vetoed = res_df[(~res_df['cur_pass']) | (~res_df['prop_pass'])]
if len(vetoed) == 0:
    print("None were vetoed on this date.")
else:
    for _, r in vetoed.iterrows():
        print(f"Time: {r['time']} | {r['direction']} | Close: {r['close']} | "
              f"CurPass: {r['cur_pass']} ({r['cur_reason']}) | "
              f"PropPass: {r['prop_pass']} (Z={r['z_vol']}, I={r['i_norm_5m']}) | "
              f"MFE: +{r['mfe']} pts | MAE: -{r['mae']} pts | Win: {r['win']} | Loss: {r['loss']}")

print("\n--- PERFORMANCE COMPARISON ---")
cur_wins = res_df[res_df['cur_pass']]['win'].sum()
cur_losses = res_df[res_df['cur_pass']]['loss'].sum()
cur_n = res_df['cur_pass'].sum()
print(f"CURRENT RULES:  N={cur_n} | Wins={cur_wins} | Losses={cur_losses} | WinRate={cur_wins/max(cur_n,1)*100:.1f}% | Avg Delta={res_df[res_df['cur_pass']]['fwd_delta'].mean():+.2f} pts")

prop_wins = res_df[res_df['prop_pass']]['win'].sum()
prop_losses = res_df[res_df['prop_pass']]['loss'].sum()
prop_n = res_df['prop_pass'].sum()
print(f"PROPOSED RULES: N={prop_n} | Wins={prop_wins} | Losses={prop_losses} | WinRate={prop_wins/max(prop_n,1)*100:.1f}% | Avg Delta={res_df[res_df['prop_pass']]['fwd_delta'].mean():+.2f} pts")

res_df.to_csv('scripts/real_p1_veto_audit_2026-09-21.csv', index=False)
