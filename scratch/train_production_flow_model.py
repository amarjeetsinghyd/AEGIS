"""
Production Flow Model Trainer for AEGIS ML Gatekeeper
Trains both LightGBM and CatBoost models on SENSEX and NIFTY L2 order flow data.
Exports:
- AEGIS/params/aegis_flow_brain.txt (LightGBM Booster format)
- AEGIS/params/aegis_catboost_brain.cbm (CatBoost Binary format)
- AEGIS/params/aegis_ml_brain_config.json
"""

import os
import glob
import json
import numpy as np
import pandas as pd
from catboost import CatBoostClassifier
import lightgbm as lgb

FEATURE_COLS = [
    'put_aggression',
    'call_buy_vol',
    'call_aggression',
    'avg_book_imb',
    'upper_wick_bps',
    'ema20_dist_bps',
    'lower_wick_bps',
    'opt_flow_ratio',
    'range_bps',
    'body_bps',
    'put_buy_vol',
    'opt_delta_3m',
    'ema5_dist_bps',
    'opt_total_vol',
    'opt_pcr',
    'opt_delta_flow',
    'ret_1m_bps'
]

def load_and_compile_parquet_list(files, name=""):
    print(f"Loading {len(files)} files for {name}...")
    dfs = []
    for f in sorted(files):
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
            continue
            
    if not dfs:
        return pd.DataFrame()
        
    df = pd.concat(dfs, ignore_index=True)
    df['dt'] = pd.to_datetime(df['ts'], unit='ms', utc=True).dt.tz_convert('Asia/Kolkata')
    df['minute'] = df['dt'].dt.floor('1min')
    
    # Spot 1-minute bars
    spot_df = df[df['kind'] == 0].copy()
    if spot_df.empty:
        spot_df = df[df['sym'].str.contains('NIFTY|SENSEX', regex=True) & (df['kind'] == 0)].copy()
    if spot_df.empty:
        spot_df = df[df['sym'].isin(['NIFTY', 'SENSEX'])].copy()
        
    spot_bars = spot_df.groupby('minute').agg(
        spot_open=('lp', 'first'),
        spot_high=('lp', 'max'),
        spot_low=('lp', 'min'),
        spot_close=('lp', 'last'),
        spot_ticks=('lp', 'count')
    ).reset_index()
    
    # Option Ticks
    opt_df = df[df['kind'].isin([1, 2])].copy()
    if opt_df.empty:
        return pd.DataFrame()
        
    opt_df = opt_df.sort_values(['sym', 'ts'])
    opt_df['d_vol'] = opt_df.groupby('sym')['vol'].diff().fillna(0).clip(lower=0)
    
    opt_df['mid'] = ((opt_df['bp1'] + opt_df['sp1']) / 2.0).fillna(opt_df['lp'])
    opt_df['is_buy'] = opt_df['lp'] >= opt_df['mid']
    opt_df['is_sell'] = opt_df['lp'] < opt_df['mid']
    
    opt_df['call_buy_vol'] = np.where((opt_df['kind'] == 1) & opt_df['is_buy'], opt_df['d_vol'], 0.0)
    opt_df['call_sell_vol'] = np.where((opt_df['kind'] == 1) & opt_df['is_sell'], opt_df['d_vol'], 0.0)
    opt_df['put_buy_vol'] = np.where((opt_df['kind'] == 2) & opt_df['is_buy'], opt_df['d_vol'], 0.0)
    opt_df['put_sell_vol'] = np.where((opt_df['kind'] == 2) & opt_df['is_sell'], opt_df['d_vol'], 0.0)
    
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
    
    bars = pd.merge(spot_bars, opt_bars, on='minute', how='inner')
    bars = bars.sort_values('minute').reset_index(drop=True)
    return bars

def engineer_features(bars):
    df = bars.copy()
    
    # Scale-invariant price features (bps)
    df['ret_1m_bps'] = (df['spot_close'] - df['spot_open']) / df['spot_open'] * 10000.0
    df['range_bps'] = (df['spot_high'] - df['spot_low']) / df['spot_open'] * 10000.0
    df['body_bps'] = (df['spot_close'] - df['spot_open']) / df['spot_open'] * 10000.0
    df['upper_wick_bps'] = (df['spot_high'] - df[['spot_open', 'spot_close']].max(axis=1)) / df['spot_open'] * 10000.0
    df['lower_wick_bps'] = (df[['spot_open', 'spot_close']].min(axis=1) - df['spot_low']) / df['spot_open'] * 10000.0
    
    df['ema_20'] = df['spot_close'].ewm(span=20, adjust=False).mean()
    df['ema20_dist_bps'] = (df['spot_close'] - df['ema_20']) / df['ema_20'] * 10000.0
    
    df['ema_5'] = df['spot_close'].ewm(span=5, adjust=False).mean()
    df['ema5_dist_bps'] = (df['spot_close'] - df['ema_5']) / df['ema_5'] * 10000.0
    
    # Option Flow
    df['opt_total_vol'] = df['call_buy_vol'] + df['call_sell_vol'] + df['put_buy_vol'] + df['put_sell_vol']
    df['opt_delta_flow'] = df['call_buy_vol'] - df['put_buy_vol']
    df['opt_flow_ratio'] = (df['call_buy_vol'] + 1.0) / (df['put_buy_vol'] + 1.0)
    df['opt_pcr'] = (df['put_buy_vol'] + 1.0) / (df['call_buy_vol'] + 1.0)
    
    df['call_aggression'] = (df['call_buy_vol'] - df['call_sell_vol']) / (df['call_buy_vol'] + df['call_sell_vol'] + 1.0)
    df['put_aggression'] = (df['put_buy_vol'] - df['put_sell_vol']) / (df['put_buy_vol'] + df['put_sell_vol'] + 1.0)
    
    df['call_buy_3m_sum'] = df['call_buy_vol'].rolling(3).sum()
    df['put_buy_3m_sum'] = df['put_buy_vol'].rolling(3).sum()
    df['opt_delta_3m'] = df['call_buy_3m_sum'] - df['put_buy_3m_sum']
    
    # Target: 3-bar future upside >= 5.0 bps without drawdown > 3.0 bps
    target_up_bps = 5.0
    sl_down_bps = 3.0
    future_max_high = df['spot_high'].shift(-1).rolling(3).max().shift(-2)
    future_min_low = df['spot_low'].shift(-1).rolling(3).min().shift(-2)
    
    max_upside = (future_max_high - df['spot_close']) / df['spot_close'] * 10000.0
    max_drawdown = (df['spot_close'] - future_min_low) / df['spot_close'] * 10000.0
    
    df['target'] = ((max_upside >= target_up_bps) & (max_drawdown <= sl_down_bps)).astype(int)
    return df.dropna().reset_index(drop=True)

def train_and_export():
    dataset_dfs = []
    
    # SENSEX Sep 16
    sensex_16 = sorted(glob.glob("AEGIS/data/l2_raw/Sensex/aegis_l2_raw_2026-09-16_part_*.parquet"))
    if sensex_16:
        b16 = load_and_compile_parquet_list(sensex_16, "SENSEX Sep 16")
        if not b16.empty:
            dataset_dfs.append(engineer_features(b16))
            
    # SENSEX Sep 23
    sensex_23 = sorted(glob.glob("AEGIS/data/l2_raw/Sensex/aegis_l2_raw_2026-09-23_part_*.parquet"))
    if sensex_23:
        b23 = load_and_compile_parquet_list(sensex_23, "SENSEX Sep 23")
        if not b23.empty:
            dataset_dfs.append(engineer_features(b23))
            
    # NIFTY Aug 18
    nifty_18 = sorted(glob.glob("AEGIS/data/l2_raw/Nifty/aegis_l2_raw_2026-08-18_part_*.parquet"))[:12]
    if nifty_18:
        bn18 = load_and_compile_parquet_list(nifty_18, "NIFTY Aug 18")
        if not bn18.empty:
            dataset_dfs.append(engineer_features(bn18))

    full_df = pd.concat(dataset_dfs, ignore_index=True)
    print(f"\nTotal Compiled Training Samples: {len(full_df)} bars")
    print(f"Positive Targets (Wins): {full_df['target'].sum()} ({full_df['target'].mean()*100:.1f}%)")
    
    X = full_df[FEATURE_COLS]
    y = full_df['target']
    
    # 1. Train LightGBM Booster
    lgb_train = lgb.Dataset(X, label=y)
    lgb_params = {
        'objective': 'binary',
        'metric': 'binary_logloss',
        'boosting_type': 'gbdt',
        'num_leaves': 15,
        'learning_rate': 0.03,
        'feature_fraction': 0.85,
        'min_data_in_leaf': 10,
        'verbose': -1,
        'seed': 42
    }
    booster = lgb.train(lgb_params, lgb_train, num_boost_round=150)
    
    # Save LightGBM text model
    params_dir = "AEGIS/params"
    os.makedirs(params_dir, exist_ok=True)
    lgb_path = os.path.join(params_dir, "aegis_flow_brain.txt")
    booster.save_model(lgb_path)
    print(f"Saved LightGBM Flow Brain: {lgb_path}")
    
    # 2. Train CatBoostClassifier
    cb_model = CatBoostClassifier(
        iterations=200,
        learning_rate=0.03,
        depth=4,
        l2_leaf_reg=3.0,
        random_seed=42,
        verbose=0
    )
    cb_model.fit(X, y)
    cb_path = os.path.join(params_dir, "aegis_catboost_brain.cbm")
    cb_model.save_model(cb_path)
    print(f"Saved CatBoost Flow Brain: {cb_path}")
    
    # 3. Save Config with 40% optimal threshold
    cfg = {
        "model_version": "v3.9_flow_gatekeeper",
        "features": FEATURE_COLS,
        "optimal_threshold": 0.40,
        "rolling_window_minutes": 5,
        "description": "Dual-Track 1-minute Option Order Flow + Microstructure Confluence Brain"
    }
    cfg_path = os.path.join(params_dir, "aegis_ml_brain_config.json")
    with open(cfg_path, "w") as f:
        json.dump(cfg, f, indent=4)
    print(f"Saved Config: {cfg_path}")
    print("\nTraining and Export Complete!")

if __name__ == "__main__":
    train_and_export()
