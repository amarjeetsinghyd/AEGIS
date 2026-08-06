import time
import collections
import threading
from datetime import datetime
from collections import defaultdict

from common.logger import get_logger

logger = get_logger("footprint_compiler")


class TickDeduplicator:
    """
    O(1) Tick Deduplicator.
    Prevents processing duplicate WebSocket ticks using a bounded sliding set.
    """
    def __init__(self, max_size=2000):
        self.seen_set = set()
        self.seen_queue = collections.deque(maxlen=max_size)

    def is_duplicate(self, symbol: str, timestamp: str, volume: float) -> bool:
        tick_id = f"{symbol}_{timestamp}_{volume}"
        if tick_id in self.seen_set:
            return True
        if len(self.seen_queue) == self.seen_queue.maxlen:
            oldest = self.seen_queue.popleft()
            self.seen_set.discard(oldest)
        self.seen_queue.append(tick_id)
        self.seen_set.add(tick_id)
        return False

    def clear(self):
        self.seen_set.clear()
        self.seen_queue.clear()


class FootprintCompiler:
    """
    Dynamic 14-Token Option Chain Ring Buffer & Tick Compiler.
    Compiles 1-minute L1 Footprints and L2 Depth for ATM±3 strikes.
    NO FLUSH on strike shift to maintain contiguous data.
    """
    def __init__(self, tick_callback=None):
        self.tick_callback = tick_callback
        self.deduplicator = TickDeduplicator()
        self.l2_history = defaultdict(lambda: collections.deque(maxlen=10))
        
        self.current_spot = 0.0
        self.last_atm_strike = None
        
        self.current_minute = None
        self.footprints = defaultdict(list)
        self.closed_footprints = defaultdict(list)
        self.market_depth = {}
        self.lock = threading.Lock()
        
    def process_tick(self, message: dict, index_token: str, ce_tokens: set = None, pe_tokens: set = None):
        """Processes incoming raw WebSocket tick message."""
        if not isinstance(message, dict):
            return

        token = str(message.get("tk", ""))
        if not token:
            return

        ts = str(message.get("t", message.get("ft", time.time())))
        vol = float(message.get("v") or message.get("ltq") or 0.0)
        price = float(message.get("lp") or message.get("c") or 0.0)

        # Spot Tick
        if token == index_token and price > 0:
            self.current_spot = price
            return # Spot ticks don't have L1/L2 order flow

        with self.lock:
            try:
                tick_min = int(float(ts)) // 60
            except:
                tick_min = int(time.time()) // 60
                
            if self.current_minute is None:
                self.current_minute = tick_min
                
            if tick_min > self.current_minute:
                self.closed_footprints = self.footprints.copy()
                self.footprints.clear()
                self.current_minute = tick_min

            # O(1) Deduplication
            if self.deduplicator.is_duplicate(token, ts, vol):
                return

            if token not in self.market_depth:
                self.market_depth[token] = {"lp": 0.0, "v": 0, "bp1": 0.0, "sp1": 0.0, "bq1": 0, "sq1": 0, "last_depth_ts": 0.0, "ofi": 0.0}

            depth = self.market_depth[token]
            prev_bq1 = depth.get("bq1", 0)
            prev_sq1 = depth.get("sq1", 0)
            prev_bp1 = depth.get("bp1", 0.0)
            prev_sp1 = depth.get("sp1", 0.0)

            # Update Depth State
            if price > 0:
                depth["lp"] = price
            if "bp1" in message:
                try: depth["bp1"] = float(message["bp1"] or 0)
                except: pass
            if "sp1" in message:
                try: depth["sp1"] = float(message["sp1"] or 0)
                except: pass
                
            # Cache all 5 levels of L2 Depth when a depth message arrives
            has_depth_update = False
            for i in range(1, 6):
                if f"bq{i}" in message:
                    try: 
                        depth[f"bq{i}"] = int(float(message.get(f"bq{i}", 0) or 0))
                        has_depth_update = True
                    except: pass
                if f"sq{i}" in message:
                    try: 
                        depth[f"sq{i}"] = int(float(message.get(f"sq{i}", 0) or 0))
                        has_depth_update = True
                    except: pass
                    
            if has_depth_update:
                depth["last_depth_ts"] = time.time()
                self.l2_history[token].append({
                    "bq1": depth.get("bq1", 0),
                    "sq1": depth.get("sq1", 0)
                })
                
                # Update OFI (Order Flow Imbalance)
                bq1 = depth.get("bq1", 0)
                sq1 = depth.get("sq1", 0)
                bp1 = depth.get("bp1", 0.0)
                sp1 = depth.get("sp1", 0.0)
                
                if bp1 == prev_bp1:
                    depth["ofi"] += bq1 - prev_bq1
                else:
                    depth["ofi"] += bq1 if bp1 > prev_bp1 else -bq1

                if sp1 == prev_sp1:
                    depth["ofi"] -= sq1 - prev_sq1
                else:
                    depth["ofi"] -= sq1 if sp1 < prev_sp1 else -sq1

            vol_delta = 0.0
            new_vol = 0
            if "v" in message:
                try: new_vol = int(float(message.get("v") or 0))
                except: new_vol = 0
                
            old_vol = int(depth.get("v") or 0)

            # First tick loss fix: If old_vol is 0, try to use ltq, else we just use the new_vol as baseline
            if old_vol == 0 and new_vol > 0:
                ltq = int(float(message.get("ltq") or 0))
                if ltq > 0:
                    vol_delta = float(ltq)
                else:
                    vol_delta = float(new_vol)
            elif old_vol > 0 and new_vol > old_vol:
                vol_delta = float(new_vol - old_vol)
                
            if vol_delta > 0:
                lp = depth["lp"]
                bp1 = depth["bp1"]
                sp1 = depth["sp1"]

                # Infer Aggression
                trade_type = "NEUTRAL"
                if lp >= sp1 and sp1 > 0:
                    trade_type = "BUY"
                elif lp <= bp1 and bp1 > 0:
                    trade_type = "SELL"

                # Calculate Average L2 Depth FROM CACHE (not from raw trade message)
                l2_bid_qty = 0
                l2_ask_qty = 0
                for i in range(1, 6):
                    l2_bid_qty += depth.get(f"bq{i}", 0)
                    l2_ask_qty += depth.get(f"sq{i}", 0)

                if trade_type != "NEUTRAL" and vol_delta > 0:
                    trade_vol = vol_delta if trade_type == "BUY" else -vol_delta
                    depth["ofi"] += trade_vol
                    
                    self.footprints[token].append({
                        "price": lp,
                        "volume": vol_delta,
                        "type": trade_type,
                        "l2_bid": l2_bid_qty,
                        "l2_ask": l2_ask_qty,
                    })

            if new_vol > 0:
                depth["v"] = new_vol

            # Fire tick callback for instant SL/TP exit evaluation (HFT non-blocking)
            if self.tick_callback and price > 0:
                self.tick_callback(token, price)

    def get_compiled_footprints(self, ce_tokens: set, pe_tokens: set):
        """Compiles the footprints for the currently tracked ATM±3 tokens."""
        with self.lock:
            compiled = {}
            target_tokens = set(str(t) for t in ce_tokens).union(set(str(t) for t in pe_tokens))
            
            # Use closed_footprints to prevent race condition with new-minute ticks
            for token, trades in self.closed_footprints.items():
                if token not in target_tokens:
                    continue # Ignore footprints from out-of-window strikes
                    
                total_buy = 0
                total_sell = 0
                total_l2_bid = 0
                total_l2_ask = 0
                tick_count = len(trades)

                for trade in trades:
                    vol = trade["volume"]
                    total_l2_bid += trade.get("l2_bid", 0)
                    total_l2_ask += trade.get("l2_ask", 0)
                    if trade["type"] == "BUY":
                        total_buy += vol
                    else:
                        total_sell += vol

                depth_cache = self.market_depth.get(token, {})
                last_l2_ask = sum(depth_cache.get(f"sq{i}", 0) for i in range(1, 6))
                last_l2_bid = sum(depth_cache.get(f"bq{i}", 0) for i in range(1, 6))

                # Spoofing Filter Logic
                history = list(self.l2_history[token])
                ask_spoof = False
                bid_spoof = False
                if len(history) >= 3:
                    ask_sizes = [h["sq1"] for h in history if h["sq1"] > 0]
                    if ask_sizes:
                        avg_ask = sum(ask_sizes) / len(ask_sizes)
                        if depth_cache.get("sq1", 0) < avg_ask * 0.2:
                            ask_spoof = True
                    bid_sizes = [h["bq1"] for h in history if h["bq1"] > 0]
                    if bid_sizes:
                        avg_bid = sum(bid_sizes) / len(bid_sizes)
                        if depth_cache.get("bq1", 0) < avg_bid * 0.2:
                            bid_spoof = True

                compiled[token] = {
                    "option_type": "CE" if token in set(str(t) for t in ce_tokens) else "PE",
                    "total_buy_vol": total_buy,
                    "total_sell_vol": total_sell,
                    "delta": total_buy - total_sell,
                    "avg_l2_bid": total_l2_bid / tick_count if tick_count else 0,
                    "avg_l2_ask": total_l2_ask / tick_count if tick_count else 0,
                    "last_l2_ask": last_l2_ask,
                    "last_l2_bid": last_l2_bid,
                    "last_depth_ts": depth_cache.get("last_depth_ts", 0.0),
                    "ofi": depth_cache.get("ofi", 0.0),
                    "ask_wall_spoof": ask_spoof,
                    "bid_wall_spoof": bid_spoof,
                }
            
            # Cleanup target tokens
            keys_to_remove = [t for t in self.footprints.keys() if t not in target_tokens]
            for k in keys_to_remove:
                del self.footprints[k]
                if k in self.market_depth:
                    del self.market_depth[k]
                if k in self.l2_history:
                    del self.l2_history[k]
                    
            return compiled

    # Note: reset_for_new_candle is no longer needed as clearing is atomic above.

    def trigger_strike_rollover(self, new_spot: float, strike_step: float = 50.0):
        """
        STRIKE ROLLOVER (NO FLUSH):
        Detects ATM shift, updates reference, but NEVER flushes the buffer.
        """
        new_atm = round(new_spot / strike_step) * strike_step
        if self.last_atm_strike is not None and new_atm != self.last_atm_strike:
            logger.info(f"[STRIKE ROLLOVER DETECTED] Spot moved from {self.last_atm_strike:.0f} to ATM {new_atm:.0f}!")
            # Note: We do NOT flush self.footprints here. 
            # `get_compiled_footprints` filters by the active 14 tokens seamlessly.
            
        self.last_atm_strike = new_atm
