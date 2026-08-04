import numpy as np
import pandas as pd

def get_chandelier_exits(df: pd.DataFrame, period: int = 22, multiplier: float = 3.0) -> pd.DataFrame:
    """
    Calculates Chandelier-based Trailing Stop Loss and Custom Dynamic Targets.
    Requires 'ATR' column to be present in the DataFrame.
    """
    # Ensure ATR exists; if not, calculate a basic version or raise an error.
    # It is expected that the calculate_atr() function from the supertrend module is run first.
    if 'ATR' not in df.columns:
        raise ValueError("ATR column is missing. Please run calculate_atr() before applying Chandelier logic.")
        
    # Calculate Extreme Points over the rolling period
    df['Highest_High'] = df['high'].rolling(window=period).max()
    df['Lowest_Low'] = df['low'].rolling(window=period).min()
    
    # 1. Chandelier Trailing Stop Loss (Standard Logic)
    # Long SL hangs from the highest high. Short SL rests on the lowest low.
    df['Chandelier_Long_SL'] = df['Highest_High'] - (df['ATR'] * multiplier)
    df['Chandelier_Short_SL'] = df['Lowest_Low'] + (df['ATR'] * multiplier)
    
    # 2. Custom Chandelier Target (Reverse Logic)
    # Long Target is projected upwards from the lowest low. Short Target is projected downwards from the highest high.
    df['Chandelier_Long_Target'] = df['Lowest_Low'] + (df['ATR'] * multiplier)
    df['Chandelier_Short_Target'] = df['Highest_High'] - (df['ATR'] * multiplier)
    
    # Clean up intermediate columns
    df.drop(['Highest_High', 'Lowest_Low'], axis=1, inplace=True)
    
    return df