import polars as pl
import pandas as pd
import numpy as np
from datetime import time

def calculate_vwap_expr(price_col='typical_price', out_col='vwap'):
    """
    Returns a Polars expression to calculate Daily anchored VWAP.
    Assumes 'date' and 'valid_vol' exist.
    """
    return (
        (pl.col(price_col) * pl.col("valid_vol")).cum_sum().over("date") /
        pl.col("valid_vol").cum_sum().over("date")
    ).alias(out_col)

def append_all_indicators(df_pandas):
    """
    Appends VWAP, EMA_9, VFI, ATR, ATR Expansion, Compression, and Regime to the DataFrame.
    Internally uses Rust-optimized Polars for a 10x-50x speedup over Pandas.
    """
    if df_pandas.empty or len(df_pandas) < 130:
        return df_pandas # Not enough data
        
    # Extract date and time using Pandas to avoid Polars +05:30 timezone parse errors
    df_pandas['date'] = pd.to_datetime(df_pandas['timestamp']).dt.date
    df_pandas['time'] = pd.to_datetime(df_pandas['timestamp']).dt.time
    
    df = pl.from_pandas(df_pandas)
    
    # Pre-computations
    df = df.with_columns(
        ((pl.col('high') + pl.col('low') + pl.col('close')) / 3).alias('typical_price')
    )
    
    # valid_vol: Filter out pre-market volume (before 9:15 AM)
    df = df.with_columns(
        pl.when(pl.col('time') >= time(9, 15)).then(pl.col('volume')).otherwise(0).alias('valid_vol')
    )
    
    # Calculate VWAPs
    df = df.with_columns([
        calculate_vwap_expr('typical_price', 'vwap'),
        calculate_vwap_expr('high', 'vwap_high'),
        calculate_vwap_expr('low', 'vwap_low')
    ])
    
    # Calculate EMA 9 — TradingView-compatible SMA-seeded method.
    # TradingView seeds EMA with SMA of the first `period` bars, then applies
    # exponential smoothing. Using bar[0] as seed (adjust=False) causes a ~1-2
    # point divergence that never fully closes. This numpy loop is fast enough
    # for our 750-bar window (<1ms extra).
    period_ema = 9
    alpha_ema = 2.0 / (period_ema + 1)  # 0.2
    closes_np = df['close'].to_numpy().astype(float)
    ema_values = np.full(len(closes_np), np.nan)
    if len(closes_np) >= period_ema:
        # Seed: simple average of first `period_ema` bars — same as TradingView
        ema_values[period_ema - 1] = float(np.mean(closes_np[:period_ema]))
        for i in range(period_ema, len(closes_np)):
            ema_values[i] = closes_np[i] * alpha_ema + ema_values[i - 1] * (1.0 - alpha_ema)
    df = df.with_columns(pl.Series('ema_9', ema_values))
    
    # VSA Gatekeeper Metrics
    df = df.with_columns(
        pl.col('volume').rolling_mean(window_size=20, min_samples=1).alias('vol_sma_20'),
        (pl.col('high') - pl.col('low')).replace(0, 0.0001).alias('candle_range'),
        (pl.col('open') - pl.col('close')).abs().alias('real_body')
    ).with_columns(
        (pl.col('volume') / pl.col('vol_sma_20')).alias('rvol'),
        (pl.col('real_body') / pl.col('candle_range')).alias('body_ratio')
    )
    
    # ATR & Compression Features
    high_low = pl.col("high") - pl.col("low")
    high_close = (pl.col("high") - pl.col("close").shift(1)).abs()
    low_close = (pl.col("low") - pl.col("close").shift(1)).abs()
    
    df = df.with_columns(
        pl.max_horizontal([high_low, high_close, low_close]).alias('tr')
    ).with_columns(
        pl.col('tr').rolling_mean(window_size=14, min_samples=1).alias('atr')
    ).with_columns(
        pl.col('atr').rolling_mean(window_size=20, min_samples=1).alias('atr_sma_20')
    ).with_columns(
        (pl.col('atr') / pl.when(pl.col('atr_sma_20') == 0).then(1.0).otherwise(pl.col('atr_sma_20'))).alias('atr_expansion')
    ).with_columns(
        pl.when(pl.col('atr_expansion') < 0.85).then(1.0).otherwise(0.0).alias('compression')
    )
    
    # Market Regime
    df = df.with_columns(
        (pl.col("close") - pl.col("vwap")).alias("vwap_dist")
    ).with_columns(
        (pl.col("vwap_dist") > 0).cast(pl.Int32).rolling_sum(window_size=5).alias('is_above_sum'),
        (pl.col("vwap_dist") < 0).cast(pl.Int32).rolling_sum(window_size=5).alias('is_below_sum')
    ).with_columns(
        pl.when((pl.col('is_above_sum') == 5) | (pl.col('is_below_sum') == 5)).then(1).otherwise(0).alias('market_regime')
    )
    
    # Volume Flow Indicator (VFI)
    period = 130
    coef = 0.2
    vcoef = 2.5
    vfi_smooth = 5
    
    df = df.with_columns(
        (pl.col("typical_price").log() - pl.col("typical_price").shift(1).log()).alias('inter')
    ).with_columns(
        pl.col("inter").rolling_std(window_size=30).alias('vinter')
    ).with_columns(
        (coef * pl.col('vinter') * pl.col('close')).alias('cutoff'),
        pl.col('volume').rolling_mean(window_size=period).shift(1).alias('vave'),
        (pl.col("typical_price") - pl.col("typical_price").shift(1)).alias('mf')
    ).with_columns(
        (pl.col('vave') * vcoef).alias('vmax')
    ).with_columns(
        pl.when(pl.col('volume') < pl.col('vmax')).then(pl.col('volume')).otherwise(pl.col('vmax')).alias('vc')
    ).with_columns(
        pl.when(pl.col('mf') > pl.col('cutoff')).then(pl.col('vc'))
          .when(pl.col('mf') < -pl.col('cutoff')).then(-pl.col('vc'))
          .otherwise(0.0).alias('vcp')
    ).with_columns(
        (pl.col('vcp').rolling_sum(window_size=period) / pl.when(pl.col('vave') == 0).then(None).otherwise(pl.col('vave'))).alias('vfi')
    ).with_columns(
        pl.col('vfi').ewm_mean(alpha=2/(vfi_smooth+1), adjust=False).alias('vfi_ema')
    )
    
    # Drop intermediate logic columns to keep memory clean
    drop_cols = [
        'date', 'time', 'typical_price', 'valid_vol', 'tr', 
        'vwap_dist', 'is_above_sum', 'is_below_sum', 'inter', 
        'vinter', 'cutoff', 'vave', 'mf', 'vmax', 'vc', 'vcp'
    ]
    df = df.drop(drop_cols)
    
    # Convert safely back to Pandas for the Brain Node / Legacy strategy engines
    return df.to_pandas()
