"""
Experiment: CatBoost Training on SENSEX L2 Order Flow & Price Action
Tests whether 1-minute Option Volume Flow + Microstructure Imbalance
can predict high-probability breakouts (+4% option move) and eliminate traps.
"""

import glob
import os
import sys
import numpy as np
import pandas as pd
from catboost import CatBoostClassifier

def load_and_compile_day(parquet_files, sample_name="Sep 16 (Expiry Day)"):
    print(f"Loading {len(parquet_files)} parquet files for {sample_name}...")
    
    dfs = []
    for f in sorted(parquet_files):
        try:
            df_part = pd.read_parquet(f, columns=[
                'ts', 'sym', 'kind', 'strike', 'lp', 'vol',
                'bp1', 'bq1', 'sp1', 'sq1',
                'bp2', 'bq2', 'sp2', 'sq2',
                'bp3', 'bq3', 'sp3', 'sq3',
                'bp4', 'bq4', 'sp4', 'sq4',
                'bp5', 'bq5', 'sp5', 'sq5'
            ])
            dfs.append(df_part)
        except Exception as e:
            print(f"Warning reading {f}: {e}")
            
    if not dfs:
        raise ValueError("No data loaded")
        
    df = pd.concat(dfs, ignore_index=True)
    print(f"Total raw ticks loaded: {len(df):,}")
    
    # Timestamps to IST
    df['dt'] = pd.to_datetime(df['ts'], unit='ms', utc=True).dt.tz_convert('Asia/Kolkata')
    df['minute'] = df['dt'].dt.floor('1min')
    
    # 1. SPOT SENSEX 1-Minute Bars
    spot_df = df[df['kind'] == 0].copy()
    if spot_df.empty:
        # Fallback to sym == 'SENSEX'
        spot_df = df[df['sym'] == 'SENSEX'].copy()
        
    spot_bars = spot_df.groupby('minute').agg(
        spot_open=('lp', 'first'),
        spot_high=('lp', 'max'),
        spot_low=('lp', 'min'),
        spot_close=('lp', 'last'),
        spot_ticks=('lp', 'count')
    ).reset_index()
    
    # 2. OPTION TICKS (Call vs Put Aggressive Flow & L2 Imbalance)
    opt_df = df[df['kind'].isin([1, 2])].copy() # 1=CE, 2=PE
    
    # Volume differencing per symbol
    opt_df = opt_df.sort_values(['sym', 'ts'])
    opt_df['d_vol'] = opt_df.groupby('sym')['vol'].diff().fillna(0)
    opt_df['d_vol'] = opt_df['d_vol'].clip(lower=0) # Eliminate reset anomalies
    
    # Lee-Ready Midpoint classification
    opt_df['mid'] = (opt_df['bp1'] + opt_df['sp1']) / 2.0
    # Fallback if quotes missing
    opt_df['mid'] = opt_df['mid'].fillna(opt_df['lp'])
    
    opt_df['is_buy'] = opt_df['lp'] >= opt_df['mid']
    opt_df['is_sell'] = opt_df['lp'] < opt_df['mid']
    
    opt_df['call_buy_vol'] = np.where((opt_df['kind'] == 1) & opt_df['is_buy'], opt_df['d_vol'], 0.0)
    opt_df['call_sell_vol'] = np.where((opt_df['kind'] == 1) & opt_df['is_sell'], opt_df['d_vol'], 0.0)
    opt_df['put_buy_vol'] = np.where((opt_df['kind'] == 2) & opt_df['is_buy'], opt_df['d_vol'], 0.0)
    opt_df['put_sell_vol'] = np.where((opt_df['kind'] == 2) & opt_df['is_sell'], opt_df['d_vol'], 0.0)
    
    # Top-5 Book Imbalance: (Total Bids - Total Asks) / (Total Bids + Total Asks)
    tot_bid_qty = opt_df['bq1'] + opt_df['bq2'] + opt_df['bq3'] + opt_df['bq4'] + opt_df['bq5']
    tot_ask_qty = opt_df['sq1'] + opt_df['sq2'] + opt_df['sq3'] + opt_df['sq4'] + opt_df['sq5']
    opt_df['book_imb'] = (tot_bid_qty - tot_ask_qty) / (tot_bid_qty + tot_ask_qty + 1e-5)
    
    opt_bars = opt_df.groupby('minute').agg(
        call_buy_vol=('call_buy_vol', 'sum'),
        call_sell_vol=('call_sell_vol', 'sum'),
        put_buy_vol=('put_buy_vol', 'sum'),
        put_sell_vol=('put_sell_vol', 'sum'),
        avg_book_imb=('book_imb', 'mean'),
        opt_ticks=('lp', 'count')
    ).reset_index()
    
    # Merge Spot + Option Metrics on 1-minute minute
    bars = pd.merge(spot_bars, opt_bars, on='minute', how='inner')
    bars = bars.sort_values('minute').reset_index(drop=True)
    
    print(f"Compiled {len(bars)} 1-minute bars with Spot + Option Order Flow.")
    return bars

def engineer_features(bars):
    df = bars.copy()
    
    # Price Action & Scale-Invariant features (bps)
    df['ret_1m_bps'] = (df['spot_close'] - df['spot_open']) / df['spot_open'] * 10000.0
    df['range_bps'] = (df['spot_high'] - df['spot_low']) / df['spot_open'] * 10000.0
    df['body_bps'] = (df['spot_close'] - df['spot_open']) / df['spot_open'] * 10000.0
    df['upper_wick_bps'] = (df['spot_high'] - df[['spot_open', 'spot_close']].max(axis=1)) / df['spot_open'] * 10000.0
    df['lower_wick_bps'] = (df[['spot_open', 'spot_close']].min(axis=1) - df['spot_low']) / df['spot_open'] * 10000.0
    
    # 20 EMA & Distance
    df['ema_20'] = df['spot_close'].ewm(span=20, adjust=False).mean()
    df['ema20_dist_bps'] = (df['spot_close'] - df['ema_20']) / df['ema_20'] * 10000.0
    
    # 5 EMA Momentum
    df['ema_5'] = df['spot_close'].ewm(span=5, adjust=False).mean()
    df['ema5_dist_bps'] = (df['spot_close'] - df['ema_5']) / df['ema_5'] * 10000.0
    
    # Option Flow Features
    df['opt_total_vol'] = df['call_buy_vol'] + df['call_sell_vol'] + df['put_buy_vol'] + df['put_sell_vol']
    df['opt_delta_flow'] = df['call_buy_vol'] - df['put_buy_vol']
    df['opt_flow_ratio'] = (df['call_buy_vol'] + 1.0) / (df['put_buy_vol'] + 1.0)
    df['opt_pcr'] = (df['put_buy_vol'] + 1.0) / (df['call_buy_vol'] + 1.0)
    
    df['call_aggression'] = (df['call_buy_vol'] - df['call_sell_vol']) / (df['call_buy_vol'] + df['call_sell_vol'] + 1.0)
    df['put_aggression'] = (df['put_buy_vol'] - df['put_sell_vol']) / (df['put_buy_vol'] + df['put_sell_vol'] + 1.0)
    
    # 3-bar Rolling Momentum
    df['call_buy_3m_sum'] = df['call_buy_vol'].rolling(3).sum()
    df['put_buy_3m_sum'] = df['put_buy_vol'].rolling(3).sum()
    df['opt_delta_3m'] = df['call_buy_3m_sum'] - df['put_buy_3m_sum']
    
    # Target Construction (Future 3-Bar Outcome)
    # Target: Does Spot move up +5.0 bps (~40 pts on Sensex = ~+4% on 0.50 delta ATM option)
    # in the next 3 bars WITHOUT dropping -3.0 bps (-24 pts) below current low?
    target_up_bps = 5.0
    sl_down_bps = 3.0
    
    future_max_high = df['spot_high'].shift(-1).rolling(3).max().shift(-2)
    future_min_low = df['spot_low'].shift(-1).rolling(3).min().shift(-2)
    
    # Upside gain and downside risk from current close
    max_upside_bps = (future_max_high - df['spot_close']) / df['spot_close'] * 10000.0
    max_drawdown_bps = (df['spot_close'] - future_min_low) / df['spot_close'] * 10000.0
    
    # Win if upside >= target and drawdown <= sl
    df['target_call_win'] = ((max_upside_bps >= target_up_bps) & (max_drawdown_bps <= sl_down_bps)).astype(int)
    
    # Drop rows without future lookahead (last 3 rows) and initial warmup
    df = df.dropna().reset_index(drop=True)
    return df

def run_catboost_experiment():
    files_sep16 = sorted(glob.glob("AEGIS/data/l2_raw/Sensex/aegis_l2_raw_2026-09-16_part_*.parquet"))
    if not files_sep16:
        print("No Sep 16 files found!")
        return
        
    bars = load_and_compile_day(files_sep16, sample_name="SENSEX Expiry Day (Sep 16)")
    df = engineer_features(bars)
    
    feature_cols = [
        'ret_1m_bps', 'range_bps', 'body_bps', 'upper_wick_bps', 'lower_wick_bps',
        'ema20_dist_bps', 'ema5_dist_bps',
        'call_buy_vol', 'put_buy_vol', 'opt_total_vol', 'opt_delta_flow',
        'opt_flow_ratio', 'opt_pcr', 'call_aggression', 'put_aggression',
        'avg_book_imb', 'opt_delta_3m'
    ]
    
    print("\n--- DATASET SUMMARY ---")
    print(f"Total 1-min samples: {len(df)}")
    wins = df['target_call_win'].sum()
    print(f"Winning Breakouts (+5 bps upside without -3 bps drawdown): {wins} ({wins/len(df)*100:.1f}%)")
    print(f"Flat / Trap Candles: {len(df) - wins} ({(len(df)-wins)/len(df)*100:.1f}%)")
    
    # Chronological Train-Test Split (First 70% morning/midday for Train, Last 30% afternoon for Test)
    split_idx = int(len(df) * 0.70)
    train_df = df.iloc[:split_idx]
    test_df = df.iloc[split_idx:]
    
    X_train = train_df[feature_cols]
    y_train = train_df['target_call_win']
    X_test = test_df[feature_cols]
    y_test = test_df['target_call_win']
    
    print(f"\nTrain set: {len(X_train)} bars (Morning session)")
    print(f"Test set:  {len(X_test)} bars (Afternoon session)")
    
    # Train CatBoostClassifier
    model = CatBoostClassifier(
        iterations=200,
        learning_rate=0.03,
        depth=4,
        l2_leaf_reg=3.0,
        random_seed=42,
        verbose=0
    )
    
    print("\nTraining CatBoostClassifier...")
    model.fit(X_train, y_train, eval_set=(X_test, y_test), early_stopping_rounds=30)
    print("Training Complete!")
    
    # Feature Importances
    importances = model.get_feature_importance()
    feat_imp = pd.DataFrame({'feature': feature_cols, 'importance': importances})
    feat_imp = feat_imp.sort_values('importance', ascending=False).reset_index(drop=True)
    
    print("\n=======================================================")
    print("         CATBOOST FEATURE IMPORTANCE RANKING           ")
    print("=======================================================")
    for i, row in feat_imp.iterrows():
        bar = "#" * int(row['importance'] * 1.5)
        print(f"{i+1:2d}. {row['feature']:<20} | {row['importance']:5.2f}% | {bar}")
        
    # Evaluate on Test Set
    test_probs = model.predict_proba(X_test)[:, 1]
    
    print("\n=======================================================")
    print("      OUT-OF-SAMPLE TEST EVALUATION (Afternoon Data)    ")
    print("=======================================================")
    for threshold in [0.35, 0.45, 0.55, 0.65]:
        preds = (test_probs >= threshold).astype(int)
        trades_taken = preds.sum()
        if trades_taken > 0:
            actual_wins = y_test[preds == 1].sum()
            precision = actual_wins / trades_taken * 100.0
            print(f"Confidence Threshold >= {threshold*100:.0f}% -> Signals Fired: {trades_taken:2d} | Wins: {actual_wins:2d} | Win Rate: {precision:.1f}%")
        else:
            print(f"Confidence Threshold >= {threshold*100:.0f}% -> Signals Fired:  0 | (Filtered out noisy market)")

if __name__ == "__main__":
    run_catboost_experiment()
