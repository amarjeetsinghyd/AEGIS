import time
import collections
from kronos_bot.core.logger import get_logger

logger = get_logger("ring_buffer")


class TickDeduplicator:
    """
    O(1) Tick Deduplicator.
    Prevents processing duplicate WebSocket ticks using a bounded sliding set.
    Hash Key Format: f"{symbol}_{timestamp}_{oi}_{volume}"
    """
    def __init__(self, max_size=2000):
        self.seen_set = set()
        self.seen_queue = collections.deque(maxlen=max_size)

    def is_duplicate(self, symbol: str, timestamp: str, oi: float, volume: float) -> bool:
        tick_id = f"{symbol}_{timestamp}_{oi}_{volume}"
        if tick_id in self.seen_set:
            return True
        
        # Add to set and bounded queue
        if len(self.seen_queue) == self.seen_queue.maxlen:
            oldest = self.seen_queue.popleft()
            self.seen_set.discard(oldest)
            
        self.seen_queue.append(tick_id)
        self.seen_set.add(tick_id)
        return False

    def clear(self):
        self.seen_set.clear()
        self.seen_queue.clear()


class RingBufferManager:
    """
    60-Second RAM Ring Buffer & Kinematic Micro-Bucketer.
    - Micro-buckets ticks into 5-second snapshots.
    - Stores exactly 12 x 5-second snapshots in collections.deque(maxlen=12) (60-second sliding window).
    - Enforces Strike Rollover Kinematic Pause & Buffer Flush on strike shifts.
    """
    def __init__(self, bucket_seconds: float = 5.0, buffer_size: int = 12):
        self.bucket_seconds = bucket_seconds
        self.buffer = collections.deque(maxlen=buffer_size)
        self.deduplicator = TickDeduplicator()
        
        self.current_bucket_start = time.time()
        self.current_spot = 0.0
        self.current_option_prices = {}
        self.current_calls_oi = 0.0
        self.current_puts_oi = 0.0
        
        # Strike Rollover Tracking
        self.last_atm_strike = None
        self.pause_until = 0.0

    def process_tick(self, message: dict, index_token: str, ce_tokens: set, pe_tokens: set):
        """Processes incoming raw WebSocket tick message."""
        if not isinstance(message, dict):
            return

        token = str(message.get("tk", ""))
        if not token:
            return

        ts = str(message.get("t", message.get("ft", time.time())))
        oi = float(message.get("oi") or message.get("poi") or 0.0)
        vol = float(message.get("v") or message.get("ltq") or 0.0)
        price = float(message.get("lp") or message.get("c") or 0.0)

        # Spot Tick
        if (token == index_token or token == "26000") and price > 0:
            self.current_spot = price

        # O(1) Deduplication
        if token != index_token and token != "26000":
            if self.deduplicator.is_duplicate(token, ts, oi, vol):
                return

        # Spot Tick
        if (token == index_token or token == "26000") and price > 0:
            self.current_spot = price

        # Option Tick
        if price > 0:
            self.current_option_prices[token] = price

        if oi > 0:
            if not hasattr(self, "latest_oi"):
                self.latest_oi = {}
            self.latest_oi[str(token)] = oi

        self._ce_tokens = set(str(t) for t in ce_tokens)
        self._pe_tokens = set(str(t) for t in pe_tokens)

        # Check if 5-second micro-bucket window has completed
        now = time.time()
        if now - self.current_bucket_start >= self.bucket_seconds:
            self._flush_micro_bucket(now)

    def _flush_micro_bucket(self, now: float):
        """Closes 5-second micro-bucket and appends to 60-second Ring Buffer."""
        latest_oi = getattr(self, "latest_oi", {})
        calls_oi = sum(latest_oi.get(tok, 0.0) for tok in getattr(self, "_ce_tokens", set()))
        puts_oi = sum(latest_oi.get(tok, 0.0) for tok in getattr(self, "_pe_tokens", set()))
        net_delta_oi = puts_oi - calls_oi

        snapshot = {
            "timestamp": now,
            "spot_price": self.current_spot,
            "calls_delta_oi": calls_oi,
            "puts_delta_oi": puts_oi,
            "net_delta_oi": net_delta_oi,
            "option_prices": dict(self.current_option_prices),
            "option_ois": dict(getattr(self, "latest_oi", {}))
        }
        
        self.buffer.append(snapshot)
        self.current_bucket_start = now

    def trigger_strike_rollover_pause(self, new_spot: float, strike_step: float = 50.0):
        """
        STRIKE ROLLOVER KINEMATIC PAUSE (CRITICAL FIX 2):
        Flushes 30s Ring Buffer and enforces 30-second Kinematic Pause on strike resubscription.
        """
        new_atm = round(new_spot / strike_step) * strike_step
        if self.last_atm_strike is not None and new_atm != self.last_atm_strike:
            logger.info(f"[STRIKE ROLLOVER DETECTED] Spot moved from {self.last_atm_strike:.0f} to ATM {new_atm:.0f}!")
            logger.info("   -> Flushing 60s Ring Buffer & Enforcing 30-Second Kinematic Pause...")
            self.buffer.clear()
            self.deduplicator.clear()
            if hasattr(self, "latest_oi"):
                self.latest_oi.clear()
            self.pause_until = time.time() + 30.0
            
        self.last_atm_strike = new_atm

    def is_kinematic_ready(self) -> bool:
        """
        Returns True ONLY if:
        1. 30-second Kinematic Pause has expired.
        2. Ring Buffer is fully populated with 12 x 5s micro-buckets (60s sliding window).
        """
        now = time.time()
        if now < self.pause_until:
            return False
        return len(self.buffer) >= self.buffer.maxlen

    def get_latest_snapshots(self) -> list:
        """Returns the list of 12 snapshots in the Ring Buffer."""
        return list(self.buffer)