import pandas as pd
from kronos_bot.strategy.shadow_indicators import calculate_tsf
from common.logger import get_logger

logger = get_logger("evaluator")

class FootprintEvaluator:
    def __init__(self):
        pass

    def evaluate_5m_boundary(self, symbol, current_price, historical_df, footprint_data):
        """
        Evaluate Order Flow Footprint against TSF Confluence.
        Returns 'BUY', 'SELL', or 'NEUTRAL' and reasoning.
        """
        # 1. TSF Calculation (Using 14 period 5-minute TSF)
        if len(historical_df) < 14:
            return "NEUTRAL", "Not enough data for TSF"
            
        tsf_series = calculate_tsf(historical_df['close'], length=14)
        tsf_pred = float(tsf_series.iloc[-1])
        
        # 2. Footprint Delta & POC
        total_buy = footprint_data.get('total_buy_vol', 0)
        total_sell = footprint_data.get('total_sell_vol', 0)
        delta = footprint_data.get('delta', 0)
        levels = footprint_data.get('levels', {})
        
        if total_buy + total_sell == 0:
            return "NEUTRAL", "No order flow classified on bid/ask"

        # Find Point of Control (Price level with highest total volume)
        poc_price = 0.0
        max_vol = -1
        for price, vol_dict in levels.items():
            level_vol = vol_dict['buy'] + vol_dict['sell']
            if level_vol > max_vol:
                max_vol = level_vol
                poc_price = price

        delta_pct = delta / (total_buy + total_sell)
        
        tsf_delta = (tsf_pred - current_price) / current_price
        
        # 3. Apply L1 Footprint Signal + L2 Spoofing Filter
        avg_l2_bid = footprint_data.get('avg_l2_bid', 0)
        avg_l2_ask = footprint_data.get('avg_l2_ask', 0)

        # Logic: 
        # If Order Flow is strongly buying (Delta > +10%)
        if delta_pct > 0.10:
            if avg_l2_ask > avg_l2_bid * 2.0 and avg_l2_ask > 0:
                reason = f"L2 Absorption Filtered BUY: Massive Ask wall (Asks={avg_l2_ask:.0f} vs Bids={avg_l2_bid:.0f}) | OF Delta +{delta_pct*100:.1f}%"
                return "NEUTRAL", reason
            reason = f"Confluence BUY: OF Delta +{delta_pct*100:.1f}% | POC @ {poc_price} | (Shadow TSF {tsf_delta*100:.2f}%)"
            return "BUY", reason
            
        # If Order Flow is strongly selling (Delta < -10%)
        elif delta_pct < -0.10:
            if avg_l2_bid > avg_l2_ask * 2.0 and avg_l2_bid > 0:
                reason = f"L2 Absorption Filtered SELL: Massive Bid wall (Bids={avg_l2_bid:.0f} vs Asks={avg_l2_ask:.0f}) | OF Delta {delta_pct*100:.1f}%"
                return "NEUTRAL", reason
            reason = f"Confluence SELL: OF Delta {delta_pct*100:.1f}% | POC @ {poc_price} | (Shadow TSF {tsf_delta*100:.2f}%)"
            return "SELL", reason
            
        return "NEUTRAL", f"OF Delta {delta_pct*100:.1f}% (Shadow TSF {tsf_delta*100:.2f}%) - No Confluence"
