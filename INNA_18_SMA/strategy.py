import numpy as np
import pandas as pd
from datetime import datetime
import logging

logger = logging.getLogger("inna_18_sma")

class Inna18SMAStrategy:
    def __init__(self, symbol: str, is_live: bool = False):
        self.symbol = symbol
        self.is_live = is_live
        self.candles = pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        self.in_trade = False
        self.active_sl = 0.0
        self.entry_price = 0.0
        
    def process_new_candle(self, timestamp, open_p, high_p, low_p, close_p, volume_p):
        """
        Receives a completed candle (e.g. 15-minute or Daily).
        Returns an order dict if a signal is generated, else None.
        """
        new_row = {
            'timestamp': timestamp,
            'open': open_p,
            'high': high_p,
            'low': low_p,
            'close': close_p,
            'volume': volume_p
        }
        
        # Append and keep last 50 for indicators
        self.candles = pd.concat([self.candles, pd.DataFrame([new_row])], ignore_index=True)
        if len(self.candles) > 50:
            self.candles = self.candles.iloc[-50:]
            
        df = self.candles.copy()
        
        if len(df) < 21:
            return None
            
        # Indicators
        df["18_SMA"] = df["close"].rolling(window=18).mean()
        df["20_Avg_Volume"] = df["volume"].rolling(window=20).mean()
        
        # Latest values
        curr = df.iloc[-1]
        prev = df.iloc[-2]
        prev2 = df.iloc[-3]
        
        # Rule check
        def fully_above(row):
            return (row['low'] > row['18_SMA']) and (row['close'] > row['18_SMA'])
            
        # Manage active trade
        if self.in_trade:
            # Check Stop Loss
            if curr['low'] < self.active_sl:
                self.in_trade = False
                logger.info(f"[{curr['timestamp']}] STOP LOSS HIT at {self.active_sl}")
                return {"action": "SELL", "reason": "STOP_LOSS", "price": self.active_sl}
                
            # Check Trend Exit (Close < 18 SMA)
            if curr['close'] < curr['18_SMA']:
                self.in_trade = False
                logger.info(f"[{curr['timestamp']}] SMA EXIT at {curr['close']}")
                return {"action": "SELL", "reason": "TREND_BROKEN", "price": curr['close']}
                
            return None
            
        # Look for Entry
        cond_A = fully_above(prev)
        cond_B = fully_above(prev2)
        cond_C = prev['volume'] > prev['20_Avg_Volume']
        
        if cond_A and cond_B and cond_C:
            # Buy Trigger Price is High of prev candle
            trigger_price = prev['high']
            
            # If current price crosses trigger price (assuming market order on close for simplicity in live loop,
            # or ideally place a Stop Limit order at the exchange).
            if curr['high'] > trigger_price:
                self.in_trade = True
                self.entry_price = trigger_price
                # SL is lowest low of last 5 sessions (from prev candle backwards)
                self.active_sl = df.iloc[-6:-1]['low'].min()
                
                logger.info(f"[{curr['timestamp']}] BUY ENTRY at {trigger_price}, SL: {self.active_sl}")
                return {
                    "action": "BUY",
                    "reason": "18_SMA_TREND_PULLBACK",
                    "price": trigger_price,
                    "sl": self.active_sl
                }
                
        return None
