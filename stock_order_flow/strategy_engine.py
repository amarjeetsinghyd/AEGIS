"""
Stock Order Flow — Context Gate & Setup Detectors (V4.4 Architecture)
Handles dynamic VWAP per stock, Setups A/B/C/D on 3-minute boundaries.
"""
from datetime import datetime
from common.logger import get_logger

logger = get_logger("stock_strategy")

class StockStructure:
    def __init__(self):
        self.vwap_num = 0.0
        self.vwap_den = 0.0
        self.high_num = 0.0
        self.low_num = 0.0
        self.vwap = 0.0
        self.upper = 0.0
        self.lower = 0.0
        self.ema9 = 0.0
        self.day_high = 0.0
        self.day_low = 0.0

    def update_bar(self, bar):
        h, l, c, v = bar['high'], bar['low'], bar['close'], bar['volume']
        v = v if v > 0 else 1.0
        tp = (h + l + c) / 3.0
        
        self.vwap_num += tp * v
        self.high_num += h * v
        self.low_num += l * v
        self.vwap_den += v
        
        self.vwap = self.vwap_num / self.vwap_den
        self.upper = self.high_num / self.vwap_den
        self.lower = self.low_num / self.vwap_den
        
        k9 = 2.0 / 10.0
        self.ema9 = c if self.ema9 == 0 else (c * k9) + (self.ema9 * (1 - k9))
        
        self.day_high = max(self.day_high, h) if self.day_high > 0 else h
        self.day_low = min(self.day_low, l) if self.day_low > 0 else l

    def context_state(self, price):
        PROX = price * 0.0020  # 0.20% buffer for stocks
        EXT = price * 0.0015
        if self.upper <= 0: return 'NO_MANS_LAND'
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
            
            bars = df.to_dict('records')
            if symbol not in self.structures:
                self.structures[symbol] = StockStructure()
            
            struct = self.structures[symbol]
            struct.update_bar(bars[-1])  # Update with newly closed 3m bar
            
            c = bars[-1]['close']
            ctx = struct.context_state(c)
            
            total_buy = fp_data.get("total_buy_vol", 0)
            total_sell = fp_data.get("total_sell_vol", 0)
            flow = total_buy + total_sell
            norm_delta = (total_buy - total_sell) / flow if flow > 0 else 0.0
            
            avg_ask = fp_data.get("avg_l2_ask", 0)
            avg_bid = fp_data.get("avg_l2_bid", 0)
            
            # Setup C: Veto Shield
            if ctx == 'AT_EXTREME' and abs(norm_delta) > 0.20:
                prog = abs(bars[-1]['close'] - bars[-1]['open']) / c
                if prog < 0.0010: # Less than 0.1% move
                    self.veto_until[symbol] = now.timestamp() + 300
                    logger.info(f"{symbol}: Setup C VETO ACTIVE (Exhaustion)")
                    continue
            
            veto_active = self.veto_until.get(symbol, 0) > now.timestamp()
            
            # Setup B: Absorption (Takes priority, bypasses Veto)
            if ctx == 'IN_ZONE' and abs(norm_delta) > 0.15:
                prog = abs(bars[-1]['close'] - bars[-1]['open']) / c
                if prog < 0.0015: # High effort, low result
                    if norm_delta < 0 and avg_bid > avg_ask * 1.5:
                        candidates.append((0, symbol, "BUY", f"Setup B Absorption: Buy at VWAP. Delta {norm_delta*100:.1f}%"))
                    elif norm_delta > 0 and avg_ask > avg_bid * 1.5:
                        candidates.append((0, symbol, "SELL", f"Setup B Absorption: Sell at VWAP. Delta {norm_delta*100:.1f}%"))
            
            if veto_active: continue
            
            # Setup A: Breakout
            if ctx == 'IN_ZONE' and abs(norm_delta) > 0.10:
                if bars[-1]['close'] > struct.upper and bars[-1]['close'] > struct.ema9 and norm_delta > 0:
                    if avg_ask < avg_bid * 2: # L2 Guard
                        candidates.append((1, symbol, "BUY", f"Setup A Breakout (Up). Delta {norm_delta*100:.1f}%"))
                elif bars[-1]['close'] < struct.lower and bars[-1]['close'] < struct.ema9 and norm_delta < 0:
                    if avg_bid < avg_ask * 2: # L2 Guard
                        candidates.append((1, symbol, "SELL", f"Setup A Breakout (Down). Delta {norm_delta*100:.1f}%"))
            
            # Setup D: Stacked Imbalance
            if abs(norm_delta) > 0.40:
                if norm_delta > 0 and bars[-1]['close'] > bars[-1]['open']:
                    candidates.append((2, symbol, "BUY", f"Setup D Imbalance. Delta {norm_delta*100:.1f}%"))
                elif norm_delta < 0 and bars[-1]['close'] < bars[-1]['open']:
                    candidates.append((2, symbol, "SELL", f"Setup D Imbalance. Delta {norm_delta*100:.1f}%"))

        if not candidates:
            return None, None, None
            
        # Mutex Sort: Priority 0 (Setup B) > 1 (Setup A) > 2 (Setup D)
        candidates.sort(key=lambda x: x[0])
        best = candidates[0]
        return best[1], best[2], best[3]