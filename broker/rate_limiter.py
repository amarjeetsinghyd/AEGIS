# rate_limiter.py
# Centralized rate limiter for Shoonya broker REST calls.
# Token-bucket sliding-window limiter shared across the AEGIS codebase.

import time
import threading
from collections import deque

try:
    from logger import get_logger
    logger = get_logger("rate_limiter")
except ImportError:
    import logging
    logger = logging.getLogger("rate_limiter")
    logging.basicConfig(level=logging.INFO)


class TokenBucketLimiter:
    """
    Thread-safe token bucket rate limiter.
    Tracks both per-second AND per-minute limits simultaneously.
    Blocks (sleeps) the calling thread if rate would be exceeded.
    """

    def __init__(self, per_second: int, per_minute: int = None, per_hour: int = None, name: str = ""):
        self.per_second = per_second
        self.per_minute = per_minute
        self.per_hour = per_hour
        self.name = name
        self._lock = threading.Lock()
        # Sliding window timestamps for per-second
        self._second_window: deque = deque()
        # Sliding window timestamps for per-minute
        self._minute_window: deque = deque()
        # Sliding window timestamps for per-hour
        self._hour_window: deque = deque()

    def acquire(self, timeout: float = 30.0) -> bool:
        """
        Block until a request slot is available within all applicable limits.
        Returns True if acquired, False if timeout exceeded.
        Raises RuntimeError if waited longer than timeout.
        """
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                # Prune stale entries
                self._prune(now)

                sec_ok = len(self._second_window) < self.per_second
                min_ok = (self.per_minute is None) or (len(self._minute_window) < self.per_minute)
                hr_ok = (self.per_hour is None) or (len(self._hour_window) < self.per_hour)

                if sec_ok and min_ok and hr_ok:
                    self._second_window.append(now)
                    if self.per_minute is not None:
                        self._minute_window.append(now)
                    if self.per_hour is not None:
                        self._hour_window.append(now)
                    return True

                # Calculate next available slot
                wait = self._next_wait(now)

            if time.monotonic() + wait > deadline:
                logger.warning(f"[RateLimiter:{self.name}] Timeout waiting for slot after {timeout}s")
                return False

            if wait > 0:
                logger.debug(f"[RateLimiter:{self.name}] Rate limited, sleeping {wait:.3f}s")
                time.sleep(min(wait, 0.1))  # Sleep in small increments, recheck

    def _prune(self, now: float):
        while self._second_window and now - self._second_window[0] >= 1.0:
            self._second_window.popleft()
        while self._minute_window and now - self._minute_window[0] >= 60.0:
            self._minute_window.popleft()
        while self._hour_window and now - self._hour_window[0] >= 3600.0:
            self._hour_window.popleft()

    def _next_wait(self, now: float) -> float:
        waits = []
        if self._second_window and len(self._second_window) >= self.per_second:
            waits.append(1.0 - (now - self._second_window[0]) + 0.001)
        if self.per_minute and self._minute_window and len(self._minute_window) >= self.per_minute:
            waits.append(60.0 - (now - self._minute_window[0]) + 0.001)
        if self.per_hour and self._hour_window and len(self._hour_window) >= self.per_hour:
            waits.append(3600.0 - (now - self._hour_window[0]) + 0.001)
        return max(waits) if waits else 0.0

    def __repr__(self):
        return f"TokenBucketLimiter(name={self.name}, {self.per_second}/s, {self.per_minute}/min, {self.per_hour}/hr)"


# ============================================================================
# Rate-limit-aware retry wrapper with exponential backoff
# Handles 403 (rate limit hit) and transient 5xx errors
# ============================================================================

def call_with_retry(func, *args, limiter: TokenBucketLimiter = None,
                    max_retries: int = 3, base_delay: float = 1.0,
                    **kwargs):
    """
    Calls func(*args, **kwargs) with:
    1. Rate limiter acquisition (if limiter provided)
    2. Exponential backoff retry on failure (403, 5xx, ConnectionError, etc.)

    Returns the function result or raises the last exception after max_retries.

    Usage example:
        result = call_with_retry(
            some_api_call, payload,
            limiter=some_limiter
        )
    """
    last_exc = None
    # `functools.partial` / callable objects have no __name__ -> never AttributeError
    func_name = getattr(func, "__name__", None) or repr(func)
    for attempt in range(max_retries):
        try:
            if limiter:
                acquired = limiter.acquire()
                if not acquired:
                    limiter_name = getattr(limiter, 'name', 'unknown')
                    raise RuntimeError(
                        f"[call_with_retry] Could not acquire slot for {func_name} "
                        f"within timeout (limiter: {limiter_name})"
                    )
            result = func(*args, **kwargs)
            # Shoonya returns {"stat": "Not_Ok", "emsg": "..."} instead of raising.
            # Without this branch every Shoonya rejection (and rate limit) looked
            # like a SUCCESS to the retry wrapper and was never retried.
            if isinstance(result, dict) and str(result.get("stat", "")).lower() == "not_ok":
                msg = str(result.get("emsg", "Unknown Shoonya API error"))
                norm_msg = msg.lower()
                rate_limit_error = (
                    "rate" in norm_msg
                    or "too many" in norm_msg
                    or "limit" in norm_msg
                    or "429" in norm_msg
                )
                if rate_limit_error:
                    wait = base_delay * (2 ** attempt)
                    logger.warning(
                        f"[call_with_retry] Shoonya rate limit hit calling {func_name} "
                        f"({msg}), attempt {attempt+1}/{max_retries}, sleeping {wait:.1f}s"
                    )
                    time.sleep(wait)
                    last_exc = RuntimeError(f"Shoonya rate limit: {msg}")
                    continue
                # Other Shoonya rejection (bad token, RMS block, invalid qty) —
                # never retry, hand the raw dict back to the caller.
                logger.error(f"[call_with_retry] Shoonya API error from {func_name}: {msg}")
                return result
            return result
        except Exception as e:
            wait = base_delay * (2 ** attempt)
            logger.warning(
                f"[call_with_retry] Exception in {func_name} attempt {attempt+1}/{max_retries}: "
                f"{type(e).__name__}: {e}, retrying in {wait:.1f}s"
            )
            time.sleep(wait)
            last_exc = e

    logger.error(f"[call_with_retry] All {max_retries} retries exhausted for {func_name}")
    raise last_exc
