"""
AEGIS Pillar 1 (20 EMA Rejection) Veto Measurement & Counterfactual A/B Analysis
Dataset: 2026-09-21 (Clean 1-minute NIFTY Spot + 4-Flow Options)

Objective:
1. Identify all 20 EMA pullback touches / candidates.
2. Audit Current Vetoes:
   - 1.25x VDR (Wyckoff Dump)
   - 2.2x Opposing Delta (Touch Bar PutBuy > 2.2*CallBuy or vice versa)
3. Audit DeepSeek Proposed Upgrades:
   - Normalized 4-flow Imbalance (I_norm 5m <= -0.30 for Call, >= +0.30 for Put)
   - Regime-Adaptive VDR Z-score (Z_vol >= 1.5)
4. Measure Ground-Truth Forward 5-Minute Outcome:
   - Forward MFE (Max Favorable Excursion pts)
   - Forward MAE (Max Adverse Excursion pts)
   - Forward Outcome: WIN (+8 pts / +4% opt equiv before -14 pts SL) vs LOSS
5. Did the vetoes kill winning trades (false vetoes) or save money from dumps (true vetoes)?
"""

import pandas as pd
import numpy as np

def run_audit():
    df = pd.read_csv('scripts/nifty_1m_with_5_vwaps_2026-09-21.csv')
    n = len(df)
    
    # 1. Calculate 20 EMA on Close
    df['ema_20'] = df['close'].ewm(span=20, adjust=False).mean()
    df['ema_20_slope'] = df['ema_20'].diff()
    
    # 2. 14-period ATR
    df['tr'] = np.maximum(df['high'] - df['low'], 
               np.maximum(abs(df['high'] - df['close'].shift(1)), 
                          abs(df['low'] - df['close'].shift(1))))
    df['atr'] = df['tr'].rolling(14, min_periods=5).mean().fillna(10.0)
    
    # 3. Baseline Volume (20-bar SMA of total option volume)
    df['v_base'] = df['total_opt_vol'].rolling(20, min_periods=5).mean()
    
    # 4. Approach VDR (prior 3 bars average / v_base)
    # prior bars are t-3, t-2, t-1
    df['v_approach'] = (df['total_opt_vol'].shift(1) + df['total_opt_vol'].shift(2) + df['total_opt_vol'].shift(3)) / 3.0
    df['vdr'] = df['v_approach'] / (df['v_base'].shift(1) + 1e-6)
    df['vol_slope'] = df['total_opt_vol'].shift(1) - df['total_opt_vol'].shift(3)
    
    # 5. DeepSeek Upgrade 2: Rolling 90-min VDR Z-score
    df['vdr_mean_90'] = df['vdr'].rolling(90, min_periods=20).mean()
    df['vdr_std_90'] = df['vdr'].rolling(90, min_periods=20).std()
    df['z_vol'] = (df['vdr'] - df['vdr_mean_90']) / (df['vdr_std_90'] + 1e-6)
    
    # 6. DeepSeek Upgrade 1: Normalized 5-min 4-Flow Imbalance (I_norm)
    # ΔF = (CB + PS) - (PB + CS)
    df['bull_flow'] = df['call_buy_vol'] + df['put_sell_vol']
    df['bear_flow'] = df['put_buy_vol'] + df['call_sell_vol']
    df['tot_4flow'] = df['bull_flow'] + df['bear_flow']
    
    df['delta_5m'] = (df['bull_flow'].rolling(5).sum()) - (df['bear_flow'].rolling(5).sum())
    df['tot_4flow_5m'] = df['tot_4flow'].rolling(5).sum()
    df['i_norm_5m'] = df['delta_5m'] / (df['tot_4flow_5m'] + 1e-6)
    
    # Tolerance for 20 EMA touch (1.5 bps)
    tol = df['ema_20'] * (1.5 / 10000.0)
    
    candidates = []
    
    for i in range(25, n - 6):
        row = df.iloc[i]
        t = row['time']
        close_p = row['close']
        open_p = row['open']
        high_p = row['high']
        low_p = row['low']
        ema = row['ema_20']
        slope = row['ema_20_slope']
        t_tol = tol.iloc[i]
        
        # --- Check CALL Pullback candidate ---
        # Trend is up: ema slope > 0. Price dips to touch 20 EMA, then closes above
        is_call_touch = (slope > 0) and (low_p <= ema + t_tol) and (close_p >= ema - 0.5 * t_tol) and (close_p >= open_p)
        
        # --- Check PUT Pullback candidate ---
        # Trend is down: ema slope < 0. Price bounces to touch 20 EMA, then closes below
        is_put_touch = (slope < 0) and (high_p >= ema - t_tol) and (close_p <= ema + 0.5 * t_tol) and (close_p <= open_p)
        
        direction = None
        if is_call_touch:
            direction = "CALL"
        elif is_put_touch:
            direction = "PUT"
            
        if direction is None:
            continue
            
        # Current Vetoes
        vdr = row['vdr']
        v_slope = row['vol_slope']
        cur_vdr_veto = (vdr >= 1.25) and (v_slope > 0)
        
        cb = row['call_buy_vol']
        pb = row['put_buy_vol']
        if direction == "CALL":
            cur_delta_veto = pb > (2.2 * max(cb, 1.0))
        else:
            cur_delta_veto = cb > (2.2 * max(pb, 1.0))
            
        cur_any_veto = cur_vdr_veto or cur_delta_veto
        
        # DeepSeek Proposed Vetoes
        z_vol = row['z_vol']
        prop_zvol_veto = (z_vol >= 1.5) and (v_slope > 0)
        
        i_norm = row['i_norm_5m']
        vol_5m = row['tot_4flow_5m']
        has_min_lots = vol_5m >= 5000  # min floor
        
        if direction == "CALL":
            prop_inorm_veto = (i_norm <= -0.30) and has_min_lots
        else:
            prop_inorm_veto = (i_norm >= +0.30) and has_min_lots
            
        prop_any_veto = prop_zvol_veto or prop_inorm_veto
        
        # Ground Truth Forward Outcome over next 5 bars (T+1 to T+5)
        fwd_slice = df.iloc[i+1 : i+6]
        fwd_highs = fwd_slice['high'].values
        fwd_lows = fwd_slice['low'].values
        fwd_closes = fwd_slice['close'].values
        
        if direction == "CALL":
            mfe = max(fwd_highs) - close_p
            mae = close_p - min(fwd_lows)
            fwd_close_delta = fwd_closes[-1] - close_p
            
            # Hit TP (+8 pts) before SL (-14 pts)
            hit_tp = False
            hit_sl = False
            for step_h, step_l in zip(fwd_highs, fwd_lows):
                if (step_h - close_p) >= 8.0:
                    hit_tp = True
                    break
                if (close_p - step_l) >= 14.0:
                    hit_sl = True
                    break
            win = hit_tp and not hit_sl
        else: # PUT
            mfe = close_p - min(fwd_lows)
            mae = max(fwd_highs) - close_p
            fwd_close_delta = close_p - fwd_closes[-1]
            
            hit_tp = False
            hit_sl = False
            for step_h, step_l in zip(fwd_highs, fwd_lows):
                if (close_p - step_l) >= 8.0:
                    hit_tp = True
                    break
                if (step_h - close_p) >= 14.0:
                    hit_sl = True
                    break
            win = hit_tp and not hit_sl

        candidates.append({
            "idx": i,
            "time": t,
            "direction": direction,
            "close": close_p,
            "ema_20": round(ema, 2),
            "vdr": round(vdr, 2),
            "vol_slope": round(v_slope, 1),
            "cur_vdr_veto": cur_vdr_veto,
            "cb_vol": cb,
            "pb_vol": pb,
            "ratio_cb_pb": round(pb / max(cb, 1.0) if direction=="CALL" else cb / max(pb, 1.0), 2),
            "cur_delta_veto": cur_delta_veto,
            "cur_any_veto": cur_any_veto,
            "z_vol": round(z_vol, 2) if not np.isnan(z_vol) else 0.0,
            "prop_zvol_veto": prop_zvol_veto,
            "i_norm_5m": round(i_norm, 3) if not np.isnan(i_norm) else 0.0,
            "prop_inorm_veto": prop_inorm_veto,
            "prop_any_veto": prop_any_veto,
            "mfe_5m": round(mfe, 2),
            "mae_5m": round(mae, 2),
            "fwd_5m_delta": round(fwd_close_delta, 2),
            "is_win": win,
            "is_loss": hit_sl,
        })
        
    res_df = pd.DataFrame(candidates)
    return res_df

if __name__ == '__main__':
    res = run_audit()
    print(f"Total 20 EMA Pullback Setups Detected: {len(res)}")
    print("\n--- BREAKDOWN OF CURRENT VETOES ---")
    print("Cur VDR Vetoes (1.25x):", res['cur_vdr_veto'].sum())
    print("Cur 2.2x Delta Vetoes:", res['cur_delta_veto'].sum())
    print("Cur Any Vetoes:", res['cur_any_veto'].sum())
    print("Cur Passed Setups:", (~res['cur_any_veto']).sum())
    
    print("\n--- BREAKDOWN OF PROPOSED UPGRADES (DeepSeek) ---")
    print("Proposed Z_vol Vetoes (Z >= 1.5):", res['prop_zvol_veto'].sum())
    print("Proposed I_norm Vetoes (|I| >= 0.30):", res['prop_inorm_veto'].sum())
    print("Proposed Any Vetoes:", res['prop_any_veto'].sum())
    print("Proposed Passed Setups:", (~res['prop_any_veto']).sum())
    
    print("\n--- COUNTERFACTUAL AUDIT: DID CURRENT 2.2x VETO KILL WINS OR SAVE LOSSES? ---")
    vetoed_by_delta = res[res['cur_delta_veto']]
    print(f"Total Setups Vetoed by 2.2x Delta: {len(vetoed_by_delta)}")
    for _, r in vetoed_by_delta.iterrows():
        print(f"Time: {r['time']} | Dir: {r['direction']} | Close: {r['close']} | Ratio: {r['ratio_cb_pb']}x | MFE: +{r['mfe_5m']} pts | MAE: -{r['mae_5m']} pts | Win: {r['is_win']} | Loss: {r['is_loss']}")
        
    print("\n--- PERFORMANCE COMPARISON (A/B) ---")
    cur_passed = res[~res['cur_any_veto']]
    prop_passed = res[~res['prop_any_veto']]
    
    print(f"Current System Passed Setups: {len(cur_passed)} | Wins: {cur_passed['is_win'].sum()} | Losses: {cur_passed['is_loss'].sum()} | Win Rate: {cur_passed['is_win'].sum()/max(len(cur_passed),1)*100:.1f}% | Avg Forward Delta: {cur_passed['fwd_5m_delta'].mean():+.2f} pts")
    print(f"Proposed System Passed Setups: {len(prop_passed)} | Wins: {prop_passed['is_win'].sum()} | Losses: {prop_passed['is_loss'].sum()} | Win Rate: {prop_passed['is_win'].sum()/max(len(prop_passed),1)*100:.1f}% | Avg Forward Delta: {prop_passed['fwd_5m_delta'].mean():+.2f} pts")
    
    res.to_csv('scripts/pillar1_veto_audit_results_2026-09-21.csv', index=False)
    print("\nSaved full audit to scripts/pillar1_veto_audit_results_2026-09-21.csv")
