import time
import threading
from collections import defaultdict
from common.logger import get_logger

logger = get_logger("tick_compiler")

class TickCompiler:
    def __init__(self, broker):
        self.broker = broker
        # Map token -> list of footprint updates for current 5m candle
        self.footprints = defaultdict(list)
        # Store last volume to compute delta volume
        self.last_volume = {}
        # Store current bp1, sp1, lp for each token
        self.market_depth = {}
        
        self.lock = threading.Lock()

    def reset_for_new_candle(self):
        """Called every 5 minutes to clear the accumulated footprint."""
        with self.lock:
            self.footprints.clear()
            logger.info("Footprint matrix reset for new 5-minute candle.")

    def on_order_update(self, message):
        pass

    def on_message(self, message):
        """Process incoming websocket ticks for the 5 selected stocks."""
        if 'token' not in message:
            return

        token = message['token']
        
        with self.lock:
            if token not in self.market_depth:
                self.market_depth[token] = {'lp': 0.0, 'v': 0, 'bp1': 0.0, 'sp1': 0.0}
            
            depth = self.market_depth[token]
            
            # Update fields if present in message
            if 'last_traded_price' in message: depth['lp'] = float(message['last_traded_price'])
            if 'bid' in message: depth['bp1'] = float(message['bid'])
            if 'ask' in message: depth['sp1'] = float(message['ask'])
            
            if 'volume_trade_for_the_day' in message:
                new_vol = int(message['volume_trade_for_the_day'])
                old_vol = depth['v']
                
                if old_vol > 0 and new_vol > old_vol:
                    tick_vol = new_vol - old_vol
                    lp = depth['lp']
                    bp1 = depth['bp1']
                    sp1 = depth['sp1']
                    
                    # Classify aggressive buyer vs seller
                    trade_type = "NEUTRAL"
                    if lp >= sp1 and sp1 > 0:
                        trade_type = "BUY"
                    elif lp <= bp1 and bp1 > 0:
                        trade_type = "SELL"
                        
                    if trade_type != "NEUTRAL":
                        self.footprints[token].append({
                            'price': lp,
                            'volume': tick_vol,
                            'type': trade_type
                        })
                
                depth['v'] = new_vol

    def get_compiled_footprints(self):
        """Returns a compiled dictionary of buy/sell volume at each price level."""
        with self.lock:
            compiled = {}
            for token, trades in self.footprints.items():
                levels = defaultdict(lambda: {'buy': 0, 'sell': 0})
                total_buy = 0
                total_sell = 0
                
                for trade in trades:
                    price = trade['price']
                    vol = trade['volume']
                    if trade['type'] == "BUY":
                        levels[price]['buy'] += vol
                        total_buy += vol
                    else:
                        levels[price]['sell'] += vol
                        total_sell += vol
                
                compiled[token] = {
                    'levels': dict(levels),
                    'total_buy_vol': total_buy,
                    'total_sell_vol': total_sell,
                    'delta': total_buy - total_sell
                }
            return compiled

    def subscribe(self, tokens, exchange='NSE'):
        """Subscribe to the selected stocks on WebSocket."""
        try:
            self.broker.market_data_provider.start_live_feed(
                on_tick_callback=self.on_message,
                on_open_callback=lambda: self.broker.market_data_provider.subscribe(tokens, exchange)
            )
            logger.info(f"WebSocket started for {len(tokens)} stock(s).")
        except Exception as e:
            logger.error(f"WebSocket start failed: {e}")
