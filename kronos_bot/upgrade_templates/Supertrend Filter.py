import numpy as np
import pandas as pd

def calculate_atr(df: pd.DataFrame, period: int = 10) -> pd.DataFrame:
    """
    Calculates the Average True Range (ATR) required for Supertrend and Chandelier calculations.
    """
    df_copy = df.copy()
    df_copy['H-L'] = df_copy['high'] - df_copy['low']
    df_copy['H-PC'] = np.abs(df_copy['high'] - df_copy['close'].shift(1))
    df_copy['L-PC'] = np.abs(df_copy['low'] - df_copy['close'].shift(1))
    
    df_copy['TR'] = df_copy[['H-L', 'H-PC', 'L-PC']].max(axis=1)
    df_copy['ATR'] = df_copy['TR'].rolling(window=period).mean()
    
    return df_copy

def get_supertrend_filter(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    """
    Calculates the Supertrend indicator to act as a trend filter for Kronos entry logic.
    Returns the dataframe with Supertrend value and ST_Trend direction (1 for Uptrend, -1 for Downtrend).
    """
    if 'ATR' not in df.columns:
        df = calculate_atr(df, period=period)
        
    hl2 = (df['high'] + df['low']) / 2.0
    
    df['Basic_UB'] = hl2 + (multiplier * df['ATR'])
    df['Basic_LB'] = hl2 - (multiplier * df['ATR'])
    
    df['Final_UB'] = 0.00
    df['Final_LB'] = 0.00
    df['Supertrend'] = 0.00
    df['ST_Trend'] = 0
    
    for i in range(period, len(df)):
        # Final Upper Band Calculation
        if df['Basic_UB'].iloc[i] < df['Final_UB'].iloc[i - 1] or df['close'].iloc[i - 1] > df['Final_UB'].iloc[i - 1]:
            df.loc[df.index[i], 'Final_UB'] = df['Basic_UB'].iloc[i]
        else:
            df.loc[df.index[i], 'Final_UB'] = df['Final_UB'].iloc[i - 1]
            
        # Final Lower Band Calculation
        if df['Basic_LB'].iloc[i] > df['Final_LB'].iloc[i - 1] or df['close'].iloc[i - 1] < df['Final_LB'].iloc[i - 1]:
            df.loc[df.index[i], 'Final_LB'] = df['Basic_LB'].iloc[i]
        else:
            df.loc[df.index[i], 'Final_LB'] = df['Final_LB'].iloc[i - 1]
            
        # Determine Trend
        if df['Supertrend'].iloc[i - 1] == df['Final_UB'].iloc[i - 1] and df['close'].iloc[i] <= df['Final_UB'].iloc[i]:
            df.loc[df.index[i], 'ST_Trend'] = -1
        elif df['Supertrend'].iloc[i - 1] == df['Final_UB'].iloc[i - 1] and df['close'].iloc[i] > df['Final_UB'].iloc[i]:
            df.loc[df.index[i], 'ST_Trend'] = 1
        elif df['Supertrend'].iloc[i - 1] == df['Final_LB'].iloc[i - 1] and df['close'].iloc[i] >= df['Final_LB'].iloc[i]:
            df.loc[df.index[i], 'ST_Trend'] = 1
        elif df['Supertrend'].iloc[i - 1] == df['Final_LB'].iloc[i - 1] and df['close'].iloc[i] < df['Final_LB'].iloc[i]:
            df.loc[df.index[i], 'ST_Trend'] = -1
            
        # Assign Supertrend Value
        if df['ST_Trend'].iloc[i] == 1:
            df.loc[df.index[i], 'Supertrend'] = df['Final_LB'].iloc[i]
        else:
            df.loc[df.index[i], 'Supertrend'] = df['Final_UB'].iloc[i]
            
    # Clean up intermediate columns to save memory
    df.drop(['H-L', 'H-PC', 'L-PC', 'TR', 'Basic_UB', 'Basic_LB', 'Final_UB', 'Final_LB'], axis=1, inplace=True)
    
    return df