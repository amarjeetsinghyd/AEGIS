
import sys
sys.path.insert(0, '.')
import pandas as pd
import numpy as np
import lightgbm as lgb
from datetime import timezone, timedelta
from AEGIS.core.quality_stack import QualitySlotPolicy

df = pd.read_parquet('AEGIS/data/ml_features/aegis_pooled_l2_events.parquet')
bst = lgb.Booster(model_file='AEGIS/params/aegis_pooled_brain.txt')
features = ['kyle_lambda_30s', 'momentum_accel_10s', 'option_ret_30s', 'spread_pct']
df['ml_score'] = bst.predict(df[features].values)

ist = timezone(timedelta(hours=5, minutes=30))
df['dt_ist'] = pd.to_datetime(df['ts'], unit='s', utc=True).dt.tz_convert(ist)
df['time_str'] = df['dt_ist'].dt.strftime('%H:%M:%S')

# Fixed Payoff: Target is strictly +4.0% (+0.50 ATR gross, -0.03 friction = +0.47 ATR net)
# Stop loss is ~ -2.5% to -3.0% (-0.35 ATR gross, +0.03 friction = -0.38 ATR net)
WIN_PAYOUT = 0.47
LOSS_COST = 0.38

def run_simulation(data_subset, max_trades=3, dynamic_bar=True, consec_stop=2, threshold=0.3340, entry_cutoff="15:00:00"):
    policy = QualitySlotPolicy(
        max_trades_per_day=max_trades,
        reserve_release_min=885,  # 14:45 IST
        dynamic_bar=dynamic_bar,
        consec_loss_stop=consec_stop,
        daily_loss_cap_rs=2000.0,
    )
    
    trades = []
    for d, grp in data_subset.groupby('date'):
        grp = grp.sort_values('time_str')
        policy.roll_date(grp['ts'].iloc[0])
        consec_losses = 0
        daily_pnl = 0.0
        
        for idx, row in grp.iterrows():
            t = row['time_str']
            if entry_cutoff and t > entry_cutoff:
                continue
            score = row['ml_score']
            if score < threshold:
                continue
            
            admitted, reason = policy.admit(
                score=score,
                now_sec=row['ts'],
                consec_losses=consec_losses,
                daily_pnl=daily_pnl,
            )
            
            if admitted:
                policy.mark_admitted(score, now_sec=row['ts'])
                is_win = int(row['target'])
                pnl = WIN_PAYOUT if is_win == 1 else -LOSS_COST
                daily_pnl += pnl * 1000.0
                trades.append({
                    'date': d,
                    'time': t,
                    'index': row['index'],
                    'direction': row['direction'],
                    'score': score,
                    'is_win': is_win,
                    'pnl': pnl
                })
                if is_win == 1:
                    consec_losses = 0
                else:
                    consec_losses += 1

    tdf = pd.DataFrame(trades)
    n_days = len(data_subset['date'].unique())
    if len(tdf) == 0:
        return {'n': 0, 'wr': 0.0, 'ev_tr': 0.0, 'ev_day': 0.0, 'pf': 0.0, 'trades_day': 0.0, 'tdf': tdf}
    
    n = len(tdf)
    wins = int(tdf['is_win'].sum())
    wr = wins / n
    total_pnl = tdf['pnl'].sum()
    ev_tr = total_pnl / n
    ev_day = total_pnl / n_days
    gross_win = wins * WIN_PAYOUT
    gross_loss = (n - wins) * LOSS_COST
    pf = (gross_win / gross_loss) if gross_loss > 0 else 99.9
    return {
        'n': n,
        'wins': wins,
        'losses': n - wins,
        'wr': wr,
        'ev_tr': ev_tr,
        'ev_day': ev_day,
        'pf': pf,
        'trades_day': n / n_days,
        'tdf': tdf
    }

if __name__ == '__main__':
    # 1. Evaluate on Held-out Untouched TEST FOLD (4 Days: Sep 04, Sep 07, Sep 08, Sep 11)
    test_dates = ['2026-09-04', '2026-09-07', '2026-09-08', '2026-09-11']
    test_data = df[df['date'].isin(test_dates)].copy()

    print('='*105)
    print('TEST FOLD EVALUATION (4 HELD-OUT UNTOUCHED DATES - SEP 4, 7, 8, 11)')
    print('='*105)
    res_test = run_simulation(test_data, max_trades=3, dynamic_bar=True, consec_stop=2, threshold=0.3340, entry_cutoff="15:00:00")
    print(f"Trades: {res_test['n']} | Wins: {res_test['wins']} | Losses: {res_test['losses']} | Win Rate: {res_test['wr']*100:.1f}% | Profit Factor: {res_test['pf']:.2f} | EV/Trade: {res_test['ev_tr']:+.4f}")

    # 2. Evaluate on ALL 11 DATES (Full 540-Event Canonical Dataset)
    print('\n' + '='*105)
    print('FULL 11-DAY CANONICAL DATASET (540 EVENTS ACROSS NIFTY & SENSEX)')
    print('='*105)
    res_full = run_simulation(df, max_trades=3, dynamic_bar=True, consec_stop=2, threshold=0.3340, entry_cutoff="15:00:00")
    print(f"Total Trades: {res_full['n']} | Trades/Day: {res_full['trades_day']:.1f}")
    print(f"Wins: {res_full['wins']} | Losses: {res_full['losses']}")
    print(f"WIN RATE: {res_full['wr']*100:.1f}%")
    print(f"PROFIT FACTOR: {res_full['pf']:.2f}")
    print(f"NET EV / TRADE: {res_full['ev_tr']:+.4f} ATR")
    print(f"NET EV / DAY: {res_full['ev_day']:+.4f} ATR")

    print('\n' + '-'*60)
    print('ALL TRADES TAKEN ACROSS 11 DATES:')
    print('-'*60)
    tdf = res_full['tdf']
    for idx, r in tdf.iterrows():
        res_str = 'WIN (+4.0%)' if r['is_win'] == 1 else 'LOSS (-3.0%)'
        print(f"#{idx+1:02d} | {r['date']} {r['time']} | {r['index']:6s} | {r['direction']:4s} | Score: {r['score']:.4f} | {res_str}")

    print('\n' + '-'*60)
    print('DAY-BY-DAY PERFORMANCE BREAKDOWN:')
    print('-'*60)
    for d, grp in tdf.groupby('date'):
        d_wins = (grp['is_win'] == 1).sum()
        d_losses = (grp['is_win'] == 0).sum()
        d_pnl = grp['pnl'].sum()
        status = 'GREEN' if d_pnl > 0 else ('FLAT' if d_pnl == 0 else 'RED')
        print(f"{d} | Trades: {len(grp)} | Wins: {d_wins} | Losses: {d_losses} | Net PnL: {d_pnl:+.2f} ATR | [{status}]")
