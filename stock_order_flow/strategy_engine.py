"""
Stock Order Flow — Context Gate & Setup Detectors (V4.4 Architecture)
Handles dynamic VWAP per stock, Setups A/B/C/D on 3-minute boundaries.
"""
from datetime import datetime
from common.logger import get_logger

logger = get_logger("stock_strategy")

import pandas as pd
import talib

class StockStructure:
    def __init__(self):
        self.vwap = 0.0
        self.upper = 0.0
        self.lower = 0.0
        self.ema9 = 0.0
        self.ema200 = 0.0
        self.day_high = 0.0
        self.day_low = 0.0

    def update_from_df(self, df: pd.DataFrame):
        """Vectorized calculation using full history"""
        # Calculate EMAs using TA-Lib
        df['ema9'] = talib.EMA(df['close'], timeperiod=9)
        df['ema200'] = talib.EMA(df['close'], timeperiod=200)
        
        # Calculate Daily Anchored VWAP natively (ta VWAP doesn't anchor by day natively)
        # Using typical price
        df['tp'] = (df['high'] + df['low'] + df['close']) / 3.0
        df['date'] = df['timestamp'].dt.date
        df['cum_vol'] = df.groupby('date')['volume'].cumsum()
        df['cum_pv'] = (df['tp'] * df['volume']).groupby(df['date']).cumsum()
        df['vwap'] = df['cum_pv'] / df['cum_vol']
        
        # Upper and lower bands based on VWAP
        df['cum_high_vol'] = (df['high'] * df['volume']).groupby(df['date']).cumsum()
        df['cum_low_vol'] = (df['low'] * df['volume']).groupby(df['date']).cumsum()
        df['upper'] = df['cum_high_vol'] / df['cum_vol']
        df['lower'] = df['cum_low_vol'] / df['cum_vol']
        
        df['day_high'] = df.groupby('date')['high'].cummax()
        df['day_low'] = df.groupby('date')['low'].cummin()
        
        last_row = df.iloc[-1]
        
        self.vwap = float(last_row['vwap'])
        self.upper = float(last_row['upper'])
        self.lower = float(last_row['lower'])
        self.ema9 = float(last_row['ema9'])
        self.ema200 = float(last_row['ema200']) if not pd.isna(last_row['ema200']) else 0.0
        self.day_high = float(last_row['day_high'])
        self.day_low = float(last_row['day_low'])

    def context_state(self, price):
        PROX = price * 0.0020  # 0.20% buffer for stocks
        EXT = price * 0.0015
        if self.upper <= 0 or pd.isna(self.upper): return 'NO_MANS_LAND'
        if (self.lower - PROX) <= price <= (self.upper + PROX): return 'IN_ZONE'
        if abs(price - self.day_high) <= EXT or abs(price - self.day_low) <= EXT: return 'AT_EXTREME'
        return 'NO_MANS_LAND'

class StockStrategyEngine:
    def __init__(self):
        self.structures = {}
        self.veto_until = {}

    def evaluate_all(self, footprints, bar_book, now):
        """Returns (best_symbol, signal_type, reason) or (None, None, None)"""
        candidates = []
        
        for symbol, fp_data in footprints.items():
            df = bar_book.completed_df(symbol, min_bars=2)
            if df is None or len(df) < 2: continue
            
            if symbol not in self.structures:
                self.structures[symbol] = StockStructure()
            
            struct = self.structures[symbol]
            struct.update_from_df(df)  # Vectorized calculation on full history
            
            c = df.iloc[-1]['close']
            ctx = struct.context_state(c)
            
            total_buy = fp_data.get("total_buy_vol", 0)
            total_sell = fp_data.get("total_sell_vol", 0)
            flow = total_buy + total_sell
            norm_delta = (total_buy - total_sell) / flow if flow > 0 else 0.0
            
            avg_ask = fp_data.get("avg_l2_ask", 0)
            avg_bid = fp_data.get("avg_l2_bid", 0)
            
            # Setup C: Veto Shield
            if ctx == 'AT_EXTREME' and abs(norm_delta) > 0.20:
                prog = abs(df.iloc[-1]['close'] - df.iloc[-1]['open']) / c
                if prog < 0.0010: # Less than 0.1% move
                    self.veto_until[symbol] = now.timestamp() + 300
                    logger.info(f"{symbol}: Setup C VETO ACTIVE (Exhaustion)")
                    continue
            
            veto_active = self.veto_until.get(symbol, 0) > now.timestamp()
            
            # Setup B: Absorption (Takes priority, bypasses Veto)
            if ctx == 'IN_ZONE' and abs(norm_delta) > 0.15:
                prog = abs(df.iloc[-1]['close'] - df.iloc[-1]['open']) / c
                if prog < 0.0015: # High effort, low result
                    if norm_delta < 0 and avg_bid > avg_ask * 1.5:
                        candidates.append((0, symbol, "BUY", f"Setup B Absorption: Buy at VWAP. Delta {norm_delta*100:.1f}%"))
                    elif norm_delta > 0 and avg_ask > avg_bid * 1.5:
                        candidates.append((0, symbol, "SELL", f"Setup B Absorption: Sell at VWAP. Delta {norm_delta*100:.1f}%"))
            
            if veto_active: continue
            
            # Setup A: Breakout (Uses EMA200 as trend filter)
            if ctx == 'IN_ZONE' and abs(norm_delta) > 0.10:
                # Up trend definition: Close > EMA9 and Close > EMA200
                up_trend = c > struct.ema9 and c > struct.ema200
                down_trend = c < struct.ema9 and c < struct.ema200
                
                if c > struct.upper and up_trend and norm_delta > 0:
                    if avg_ask < avg_bid * 2: # L2 Guard
                        candidates.append((1, symbol, "BUY", f"Setup A Breakout (Up). Delta {norm_delta*100:.1f}%"))
                elif c < struct.lower and down_trend and norm_delta < 0:
                    if avg_bid < avg_ask * 2: # L2 Guard
                        candidates.append((1, symbol, "SELL", f"Setup A Breakout (Down). Delta {norm_delta*100:.1f}%"))
            
            # Setup D: Stacked Imbalance
            if abs(norm_delta) > 0.40:
                if norm_delta > 0 and df.iloc[-1]['close'] > df.iloc[-1]['open']:
                    candidates.append((2, symbol, "BUY", f"Setup D Imbalance. Delta {norm_delta*100:.1f}%"))
                elif norm_delta < 0 and df.iloc[-1]['close'] < df.iloc[-1]['open']:
                    candidates.append((2, symbol, "SELL", f"Setup D Imbalance. Delta {norm_delta*100:.1f}%"))

        if not candidates:
            return None, None, None
            
        # Mutex Sort: Priority 0 (Setup B) > 1 (Setup A) > 2 (Setup D)
        candidates.sort(key=lambda x: x[0])
        best = candidates[0]
        return best[1], best[2], best[3]