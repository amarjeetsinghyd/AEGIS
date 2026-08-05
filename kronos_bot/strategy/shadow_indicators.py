import pandas as pd
import numpy as np

def calculate_slope(series: pd.Series, length: int = 14) -> pd.Series:
    res = np.full_like(series, np.nan, dtype=float)
    y_vals = series.values
    x = np.arange(length)
    x_mean = np.mean(x)
    dx = x - x_mean
    sum_dx2 = np.sum(dx**2)
    
    for i in range(length - 1, len(y_vals)):
        y = y_vals[i-length+1 : i+1]
        slope = np.sum(dx * (y - np.mean(y))) / sum_dx2
        res[i] = slope
    return pd.Series(res, index=series.index)

def calculate_tsf(series: pd.Series, length: int = 14) -> pd.Series:
    res = np.full_like(series, np.nan, dtype=float)
    y_vals = series.values
    x = np.arange(length)
    x_mean = np.mean(x)
    dx = x - x_mean
    sum_dx2 = np.sum(dx**2)
    
    for i in range(length - 1, len(y_vals)):
        y = y_vals[i-length+1 : i+1]
        y_mean = np.mean(y)
        slope = np.sum(dx * (y - y_mean)) / sum_dx2
        intercept = y_mean - slope * x_mean
        # Forecast exactly at next bar (x = length)
        res[i] = slope * length + intercept
    return pd.Series(res, index=series.index)

def calculate_atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    high = df['high']
    low = df['low']
    close_prev = df['close'].shift(1)
    
    tr1 = high - low
    tr2 = (high - close_prev).abs()
    tr3 = (low - close_prev).abs()
    
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    # Wilder's Smoothing for ATR
    atr = np.full_like(tr.values, np.nan, dtype=float)
    if len(tr) >= length:
        atr[length-1] = np.mean(tr.values[:length])
        for i in range(length, len(tr)):
            atr[i] = (atr[i-1] * (length - 1) + tr.values[i]) / length
    return pd.Series(atr, index=df.index)

def calculate_adx(df: pd.DataFrame, length: int = 14) -> pd.Series:
    up_move = df['high'] - df['high'].shift(1)
    down_move = df['low'].shift(1) - df['low']
    
    pos_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    neg_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    
    tr = pd.concat([
        df['high'] - df['low'], 
        (df['high'] - df['close'].shift(1)).abs(), 
        (df['low'] - df['close'].shift(1)).abs()
    ], axis=1).max(axis=1).values
    
    def smooth(series_vals):
        res = np.full_like(series_vals, np.nan, dtype=float)
        if len(series_vals) >= length:
            res[length-1] = np.sum(series_vals[1:length+1]) # TR uses simple sum first
            for i in range(length, len(series_vals)):
                res[i] = res[i-1] - (res[i-1] / length) + series_vals[i]
        return res

    tr_smoothed = smooth(tr)
    pos_dm_smoothed = smooth(pos_dm)
    neg_dm_smoothed = smooth(neg_dm)
    
    with np.errstate(divide='ignore', invalid='ignore'):
        pos_di = 100 * pos_dm_smoothed / tr_smoothed
        neg_di = 100 * neg_dm_smoothed / tr_smoothed
        dx = 100 * np.abs(pos_di - neg_di) / (pos_di + neg_di)
    
    adx = np.full_like(dx, np.nan, dtype=float)
    valid_dx = np.where(~np.isnan(dx))[0]
    if len(valid_dx) >= length:
        start_idx = valid_dx[0] + length - 1
        if start_idx < len(dx):
            adx[start_idx] = np.mean(dx[valid_dx[0]:start_idx+1])
            for i in range(start_idx + 1, len(dx)):
                adx[i] = (adx[i-1] * (length - 1) + dx[i]) / length
                
    return pd.Series(adx, index=df.index)

def calculate_cmf(df: pd.DataFrame, length: int = 14) -> pd.Series:
    mfv = ((df['close'] - df['low']) - (df['high'] - df['close'])) / (df['high'] - df['low'])
    mfv = mfv.fillna(0.0) * df['volume']
    
    cmf = mfv.rolling(length).sum() / df['volume'].rolling(length).sum()
    return cmf
