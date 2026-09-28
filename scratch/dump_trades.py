import json
import sys

for dt in ['2026-09-21', '2026-09-23', '2026-09-24']:
    fn = f'scratch/aegis_trade_telemetry_{dt}.jsonl'
    print('========================================')
    print('DATE:', dt)
    print('========================================')
    try:
        with open(fn, 'r', encoding='utf-8') as f:
            for i, line in enumerate(f):
                if not line.strip(): continue
                d = json.loads(line)
                sym = d.get('symbol')
                if sym in ['NIFTY24000CE', 'NIFTY_CALL']:
                    continue
                dp = d.get('decision_payload', {})
                pta = d.get('post_trade_attribution', {})
                tel = d.get('telemetry', {})
                
                trade_id = d.get('trade_id')
                sym = d.get('symbol')
                side = dp.get('metrics', {}).get('side') or d.get('direction')
                it = dp.get('impulse_time_iso') or tel.get('impulse_time')
                ft = dp.get('fill_time_iso') or tel.get('fill_time')
                ep = d.get('entry_price')
                xp = d.get('exit_price')
                pnl_rs = pta.get('pnl_rs') if pta.get('pnl_rs') is not None else d.get('pnl')
                pnl_pct = pta.get('pnl_pct') if pta.get('pnl_pct') is not None else d.get('pnl_pct')
                dur = pta.get('trade_duration_sec') or tel.get('trade_duration_sec')
                reason = pta.get('exit_reason') or d.get('exit_reason')
                mfe = pta.get('peak_mfe_pct')
                mfe_rs = pta.get('peak_mfe_rs')
                mfe_t = pta.get('time_to_peak_mfe_sec')
                mae = pta.get('peak_mae_pct')
                mae_rs = pta.get('peak_mae_rs')
                setup = dp.get('pa_setup_name') or dp.get('metrics', {}).get('pa_setup_name')
                rej = dp.get('rejection_type')
                eqs = dp.get('eqs_score')
                conv = dp.get('conviction_score') or dp.get('conviction')
                diag = pta.get('diagnostic_summary')
                spot_move = tel.get('spot_move_pts')
                espot = tel.get('entry_spot')
                xspot = tel.get('exit_spot')
                
                def clean(s):
                    return str(s).replace('\u20b9', 'Rs.').replace('\u0394', 'Delta')
                
                print(f"--- TRADE {i+1} ---")
                print(f"Trade ID: {trade_id}")
                print(f"Symbol: {sym} | Side: {side}")
                print(f"Impulse Time: {it} | Fill Time: {ft}")
                print(f"Entry: Rs {ep} -> Exit: Rs {xp} (Pts: {xp - ep:+.2f})")
                print(f"PnL: Rs {pnl_rs} ({pnl_pct}%) | Dur: {dur}s")
                print(f"Exit Reason: {clean(reason)}")
                print(f"Peak MFE: {mfe}% (Rs {mfe_rs}) @ {mfe_t}s | Peak MAE: {mae}% (Rs {mae_rs})")
                print(f"Setup: {setup} ({rej}) | Conviction: {conv} | EQS: {eqs}")
                print(f"Spot: {espot} -> {xspot} (Move: {spot_move} pts)")
                print(f"Diagnostic: {clean(diag)}")
                print()
    except Exception as e:
        print(f"Error reading {fn}: {e}")
