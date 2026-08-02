import numpy as np
import pandas as pd
import talib

def calculate_shadow_metrics(df: pd.DataFrame, di_period: int = 14, tsf_period: int = 14) -> dict:
    """
    Calculates DMI (+DI, -DI, ADX) and Time Series Forecast (TSF) metrics for background logging.
    
    Parameters:
        df (pd.DataFrame): DataFrame containing 'high', 'low', 'close' columns.
        di_period (int): Period for DMI (+DI, -DI, ADX) calculation. Default is 14.
        tsf_period (int): Period for Time Series Forecast calculation. Default is 14.
        
    Returns:
        dict: Latest metric values and crossover signals for shadow mode logging.
    """
    # Ensure inputs are contiguous float64 numpy arrays for TA-Lib
    high = np.ascontiguousarray(df['high'].values, dtype=np.float64)
    low = np.ascontiguousarray(df['low'].values, dtype=np.float64)
    close = np.ascontiguousarray(df['close'].values, dtype=np.float64)
    
    # 1. TA-Lib DMI Calculations
    plus_di = talib.PLUS_DI(high, low, close, timeperiod=di_period)
    minus_di = talib.MINUS_DI(high, low, close, timeperiod=di_period)
    adx = talib.ADX(high, low, close, timeperiod=di_period)
    
    # 2. TA-Lib Time Series Forecast (TSF) Calculation
    tsf = talib.TSF(close, timeperiod=tsf_period)
    
    # Extract latest two values for crossover check
    latest_plus_di = float(plus_di[-1])
    latest_minus_di = float(minus_di[-1])
    prev_plus_di = float(plus_di[-2])
    prev_minus_di = float(minus_di[-2])
    
    # Crossover Logic
    di_bullish_cross = bool((prev_plus_di <= prev_minus_di) and (latest_plus_di > latest_minus_di))
    di_bearish_cross = bool((prev_minus_di <= prev_plus_di) and (latest_minus_di > latest_plus_di))
    
    # 3. Compile Analytics Payload
    shadow_data = {
        "timestamp": str(df.index[-1]),
        "current_close": float(close[-1]),
        "tsf_forecast": float(tsf[-1]),
        "tsf_bias": "UP" if tsf[-1] > close[-1] else "DOWN",
        "plus_di": latest_plus_di,
        "minus_di": latest_minus_di,
        "adx": float(adx[-1]),
        "di_bullish_cross": di_bullish_cross,
        "di_bearish_cross": di_bearish_cross
    }
    
    return shadow_data

def log_shadow_metrics(shadow_data: dict, filepath: str = "/dev/shm/kronos_shadow_metrics.json"):
    """
    Writes shadow metrics to Linux Shared Memory (/dev/shm) or log file for real-time tracking.
    """
    import json
    try:
        with open(filepath, "w") as f:
            json.dump(shadow_data, f, indent=4)
    except Exception as e:
        print(f"Shadow logger error: {e}")

# Example Usage Block
if __name__ == "__main__":
    # Mock data structure matching 5-minute candle feeds
    mock_data = {
        "high": np.random.uniform(24000, 24100, 50),
        "low": np.random.uniform(23900, 24000, 50),
        "close": np.random.uniform(23950, 24050, 50)
    }
    df = pd.DataFrame(mock_data)
    
    # Compute metrics
    metrics = calculate_shadow_metrics(df)
    
    # Output result to shared memory
    log_shadow_metrics(metrics)
    print("Shadow metrics logged successfully:")
    print(metrics)