
import pandas as pd
import numpy as np
import lightgbm as lgb
from datetime import datetime

df = pd.read_parquet('AEGIS/data/ml_features/aegis_pooled_l2_events.parquet')
bst = lgb.Booster(model_file='AEGIS/params/aegis_pooled_brain.txt')
features = ['kyle_lambda_30s', 'momentum_accel_10s', 'option_ret_30s', 'spread_pct']
df['ml_score'] = bst.predict(df[features].values)

# Fixed Payoff: Target is strictly +4.0% (+0.50 ATR gross, -0.03 friction = +0.47 ATR net)
# Stop loss is ~ -2.5% to -3.0% (-0.35 ATR gross, +0.03 friction = -0.38 ATR net)
WIN_PAYOUT = 0.47
LOSS_COST = 0.38

def run_simulation(data_subset, max_trades=3, dynamic_bar=True, consec_stop=2, threshold=0.3340):
    trades = []
    for d, grp in data_subset.groupby('date'):
        used = 0
        best_score = -1.0
        consec_losses = 0
        for idx, row in grp.iterrows():
            score = row['ml_score']
            if score < threshold:
                continue
            if max_trades > 0 and used >= max_trades:
                continue
            if consec_stop > 0 and consec_losses >= consec_stop:
                continue
            if dynamic_bar and used >= 1 and score <= best_score:
                continue
            
            used += 1
            if score > best_score:
                best_score = score
            is_win = int(row['target'])
            pnl = WIN_PAYOUT if is_win == 1 else -LOSS_COST
            trades.append({
                'date': d,
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

# 1. Evaluate on Held-out Untouched TEST FOLD (4 Days: Sep 04, Sep 07, Sep 08, Sep 11)
test_dates = ['2026-09-04', '2026-09-07', '2026-09-08', '2026-09-11']
test_data = df[df['date'].isin(test_dates)].copy()

print('========================================================================================')
print('TEST FOLD EVALUATION (4 HELD-OUT UNTOUCHED DATES - SEP 4, 7, 8, 11)')
print('========================================================================================')
policies = [
    ('Raw Price Action (Baseline - No ML)', 999, False, 0, 0.0),
    ('ML Gatekeeper Only (>= 0.3340, No Trade Limit)', 999, False, 0, 0.3340),
    ('ML + Max 3 Trades/Day (Dynamic Bar + 2-Loss Stop)', 3, True, 2, 0.3340),
    ('ML + Max 2 Trades/Day (Dynamic Bar + 1-Loss Stop)', 2, True, 1, 0.3340),
    ('Single Bullet Mode (Max 1 Trade/Day, Top Setup)', 1, False, 0, 0.3340),
    ('Stricter ML Threshold (>= 0.3400) + Max 2 Trades/Day', 2, True, 1, 0.3400),
]

print('%-45s | %-6s | %-10s | %-8s | %-10s | %-8s' % ('Policy', 'Trades', 'Trades/Day', 'Win Rate', 'EV/Trade', 'Profit Factor'))
print('-'*105)
for name, max_t, dyn, c_stop, th in policies:
    res = run_simulation(test_data, max_trades=max_t, dynamic_bar=dyn, consec_stop=c_stop, threshold=th)
    print('%-45s | %-6d | %-10.1f | %-7.1f%% | %+9.4f | %-8.2f' % (name, res['n'], res['trades_day'], res['wr']*100, res['ev_tr'], res['pf']))

# 2. Evaluate on ALL 11 DATES (Full 540-Event Canonical Dataset)
print('
========================================================================================')
print('FULL 11-DAY CANONICAL DATASET (540 EVENTS ACROSS NIFTY & SENSEX)')
print('========================================================================================')
print('%-45s | %-6s | %-10s | %-8s | %-10s | %-8s' % ('Policy', 'Trades', 'Trades/Day', 'Win Rate', 'EV/Trade', 'Profit Factor'))
print('-'*105)
for name, max_t, dyn, c_stop, th in policies:
    res = run_simulation(df, max_trades=max_t, dynamic_bar=dyn, consec_stop=c_stop, threshold=th)
    print('%-45s | %-6d | %-10.1f | %-7.1f%% | %+9.4f | %-8.2f' % (name, res['n'], res['trades_day'], res['wr']*100, res['ev_tr'], res['pf']))
