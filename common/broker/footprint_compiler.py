import time
import collections
from common.logger import get_logger

logger = get_logger("footprint_compiler")


class TickDeduplicator:
    """
    O(1) deduplicator for websocket ticks.
    """
    def __init__(self, max_size=4000):
        self.seen_set = set()
        self.seen_queue = collections.deque(maxlen=max_size)

    def is_duplicate(self, symbol: str, timestamp: str, oi: float, volume: float) -> bool:
        tick_id = f"{symbol}_{timestamp}_{oi}_{volume}"
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
    Continuity-preserving micro-bucket ring buffer.

    Key changes vs old architecture:
    - Never flushes the ring buffer on ATM rollover.
    - Never enforces a blind 30s pause.
    - Rebases CE/PE OI levels when the active basket changes.
    - Exposes rollover transition state for the UI and engine.
    """
    def __init__(self, bucket_seconds: float = 5.0, buffer_size: int = 12, rollover_grace_seconds: float = 10.0):
        self.bucket_seconds = float(bucket_seconds)
        self.buffer = collections.deque(maxlen=int(buffer_size))
        self.deduplicator = TickDeduplicator()

        self.current_bucket_start = time.time()
        self.current_spot = 0.0
        self.current_option_prices = {}
        self.latest_oi = {}

        self._ce_tokens = set()
        self._pe_tokens = set()
        self._basket_signature = (frozenset(), frozenset())

        self._calls_level_offset = 0.0
        self._puts_level_offset = 0.0
        self._last_cont_calls_oi = None
        self._last_cont_puts_oi = None

        self.last_atm_strike = None
        self.last_rollover_at = 0.0
        self.last_rollover_from = None
        self.last_rollover_to = None
        self.rollover_grace_seconds = float(rollover_grace_seconds)

        self.pause_until = 0.0

    def _active_token_count(self) -> int:
        return len(self._ce_tokens) + len(self._pe_tokens)

    def _compute_coverage(self) -> float:
        total = self._active_token_count()
        if total <= 0:
            return 0.0
        covered = 0
        for tok in self._ce_tokens:
            if tok in self.latest_oi:
                covered += 1
        for tok in self._pe_tokens:
            if tok in self.latest_oi:
                covered += 1
        return covered / float(total)

    def _set_active_token_sets(self, ce_tokens: set, pe_tokens: set):
        new_ce = set(str(t) for t in ce_tokens)
        new_pe = set(str(t) for t in pe_tokens)
        new_signature = (frozenset(new_ce), frozenset(new_pe))

        if new_signature == self._basket_signature:
            self._ce_tokens = new_ce
            self._pe_tokens = new_pe
            return

        prev_calls = self._last_cont_calls_oi
        prev_puts = self._last_cont_puts_oi

        raw_calls = sum(self.latest_oi.get(tok, 0.0) for tok in new_ce)
        raw_puts = sum(self.latest_oi.get(tok, 0.0) for tok in new_pe)

        if prev_calls is not None:
            self._calls_level_offset = prev_calls - raw_calls
        if prev_puts is not None:
            self._puts_level_offset = prev_puts - raw_puts

        self._ce_tokens = new_ce
        self._pe_tokens = new_pe
        self._basket_signature = new_signature

    def _compute_continuous_oi(self):
        raw_calls = sum(self.latest_oi.get(tok, 0.0) for tok in self._ce_tokens)
        raw_puts = sum(self.latest_oi.get(tok, 0.0) for tok in self._pe_tokens)

        cont_calls = raw_calls + self._calls_level_offset
        cont_puts = raw_puts + self._puts_level_offset

        self._last_cont_calls_oi = cont_calls
        self._last_cont_puts_oi = cont_puts
        return raw_calls, raw_puts, cont_calls, cont_puts

    def process_tick(self, message: dict, index_token: str, ce_tokens: set, pe_tokens: set):
        if not isinstance(message, dict):
            return

        token = str(message.get("tk", ""))
        if not token:
            return

        ts = str(message.get("t", message.get("ft", time.time())))
        oi = float(message.get("oi") or message.get("poi") or 0.0)
        vol = float(message.get("v") or message.get("ltq") or 0.0)
        price = float(message.get("lp") or message.get("c") or 0.0)

        if (token == index_token or token == "26000") and price > 0:
            self.current_spot = price

        if token != index_token and token != "26000":
            if self.deduplicator.is_duplicate(token, ts, oi, vol):
                return

        if price > 0:
            self.current_option_prices[token] = price

        if oi > 0:
            self.latest_oi[token] = oi

        self._set_active_token_sets(ce_tokens, pe_tokens)

        now = time.time()
        if now - self.current_bucket_start >= self.bucket_seconds:
            self._flush_micro_bucket(now)

    def _flush_micro_bucket(self, now: float):
        raw_calls_oi, raw_puts_oi, calls_oi, puts_oi = self._compute_continuous_oi()
        net_delta_oi = puts_oi - calls_oi
        coverage = self._compute_coverage()

        snapshot = {
            "timestamp": now,
            "spot_price": self.current_spot,
            "calls_delta_oi": float(calls_oi),
            "puts_delta_oi": float(puts_oi),
            "net_delta_oi": float(net_delta_oi),
            "raw_calls_oi": float(raw_calls_oi),
            "raw_puts_oi": float(raw_puts_oi),
            "raw_net_delta_oi": float(raw_puts_oi - raw_calls_oi),
            "coverage": float(coverage),
            "num_active_tokens": int(self._active_token_count()),
            "option_prices": dict(self.current_option_prices),
            "option_ois": dict(self.latest_oi),
            "atm_strike": self.last_atm_strike,
            "rollover_active": self.is_rollover_transition_active(),
            "rollover_from": self.last_rollover_from,
            "rollover_to": self.last_rollover_to,
        }

        self.buffer.append(snapshot)
        self.current_bucket_start = now

    def trigger_strike_rollover_pause(self, new_spot: float, strike_step: float = 50.0):
        """
        Compatibility method retained for main.py.
        New behavior:
        - mark rollover transition
        - preserve buffer continuity
        - do not pause
        - do not flush
        """
        new_atm = round(new_spot / strike_step) * strike_step
        if self.last_atm_strike is not None and new_atm != self.last_atm_strike:
            self.last_rollover_from = self.last_atm_strike
            self.last_rollover_to = new_atm
            self.last_rollover_at = time.time()
            logger.info(
                f"[STRIKE ROLLOVER DETECTED] Spot moved from {self.last_atm_strike:.0f} to ATM {new_atm:.0f}"
            )
            logger.info("   -> Preserving ring buffer and rebasing OI continuity offsets (no flush, no blind pause).")

        self.last_atm_strike = new_atm
        self.pause_until = 0.0

    def has_full_window(self) -> bool:
        return len(self.buffer) >= self.buffer.maxlen

    def is_rollover_transition_active(self) -> bool:
        if self.last_rollover_at <= 0:
            return False
        return (time.time() - self.last_rollover_at) <= self.rollover_grace_seconds

    def is_kinematic_ready(self) -> bool:
        return self.has_full_window()

    def get_latest_snapshots(self) -> list:
        return list(self.buffer)
