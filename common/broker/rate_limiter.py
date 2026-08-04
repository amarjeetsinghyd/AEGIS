# rate_limiter.py
# Centralized AngelOne SmartAPI Rate Limiter
# QuantOS Runtime — src/core/rate_limiter.py
#
# AngelOne SmartAPI Confirmed Rate Limits (source: smartapi.angelbroking.com/docs/RateLimit)
# ============================================================================
# getCandleData (historical):  3 req/sec | 180 req/min | 5000 req/hour
# getLtpData:                 10 req/sec | 500 req/min | 5000 req/hour
# market/v1/quote:            10 req/sec | 500 req/min | 5000 req/hour  (NOTE: 1 req can fetch 50 symbols)
# placeOrder:                  9 req/sec | 500 req/min | 1000 req/hour  (cumulative with modify+cancel)
# loginByPassword:             1 req/sec
# generateTokens:              1 req/sec
# getOrderBook:                1 req/sec
# getPosition:                 1 req/sec
# getTradeBook:                1 req/sec
#
# WebSocket 2.0 Limits:
# - Max 1000 token_mode subscriptions per session
# - Max 3 concurrent WebSocket connections per client_code
# - Subscribing same token+mode again is gracefully ignored (not counted)
# - Modes: 1=LTP, 2=Quote, 3=SnapQuote, 4=Depth
# - RECOMMENDATION: Subscribe ONE mode per token (use SnapQuote=3 for OHLCV+depth)
#
# JWT Token Lifecycle:
# - Token expires daily at 00:00 IST (midnight) — NOT at a fixed interval
# - Feed token used only for WebSocket
# - Use generateTokens (refreshToken) to get new JWT without full re-login
# - generateTokens: 1 req/sec, 1000 req/hour
#
# getCandleData Constraints:
# - ONE_MINUTE:   max 30 days per request, response capped at 500 rows
# - THREE_MINUTE: max 60 days
# - FIVE_MINUTE:  max 100 days
# - TEN_MINUTE:   max 100 days
# - FIFTEEN_MINUTE: max 200 days
# - ONE_HOUR:     max 400 days
# - ONE_DAY:      max 2000 days
# - Format: fromdate/todate = "YYYY-MM-DD HH:MM"
# - IMPORTANT: For ONE_MINUTE, only fetch <= 30 candles at a time during live
#   to stay within the 500-row cap. Use fromdate = now-30min for live refresh.
#
# Design Principles:
# - Single WebSocket pipeline feeds both trading AND research — NO duplicate REST polling
# - getCandleData called max every 10s in brain_service = ~6/min = well within 180/min
# - Never call REST APIs from research_collector (it taps ZMQ from brain, no direct API)
# - All REST calls go through this rate limiter before executing
# ============================================================================

import time
import threading
from collections import deque
from functools import wraps

try:
    from common.logger import get_logger
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
# SINGLETON LIMITERS — one per API endpoint type
# Import and use these in data_fetcher.py / any code that calls REST APIs
# ============================================================================

# getCandleData: 3/sec, 180/min, 5000/hr
CANDLE_LIMITER = TokenBucketLimiter(
    per_second=3, per_minute=180, per_hour=5000,
    name="getCandleData"
)

# getLtpData: 10/sec, 500/min, 5000/hr
LTP_LIMITER = TokenBucketLimiter(
    per_second=10, per_minute=500, per_hour=5000,
    name="getLtpData"
)

# market/v1/quote: 10/sec, 500/min, 5000/hr
QUOTE_LIMITER = TokenBucketLimiter(
    per_second=10, per_minute=500, per_hour=5000,
    name="marketQuote"
)

# placeOrder + modifyOrder + cancelOrder: CUMULATIVE 9/sec, 500/min, 1000/hr
ORDER_LIMITER = TokenBucketLimiter(
    per_second=9, per_minute=500, per_hour=1000,
    name="order_cumulative"
)

# getOrderBook / getPosition / getTradeBook: 1/sec
ONE_PER_SEC_LIMITER = TokenBucketLimiter(
    per_second=1, per_minute=None, per_hour=None,
    name="one_per_sec"
)

# login: 1/sec
LOGIN_LIMITER = TokenBucketLimiter(
    per_second=1, per_minute=None, per_hour=None,
    name="login"
)

# generateTokens (refresh JWT): 1/sec, 1000/hr
TOKEN_REFRESH_LIMITER = TokenBucketLimiter(
    per_second=1, per_minute=None, per_hour=1000,
    name="generateTokens"
)


# ============================================================================
# Convenience decorator: wraps any API call with the right limiter
# Usage:
#   @rate_limited(CANDLE_LIMITER)
#   def get_candle_data(...):
#       ...
# ============================================================================

def rate_limited(limiter: TokenBucketLimiter, timeout: float = 30.0):
    """Decorator that acquires a rate limit slot before calling the function."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            acquired = limiter.acquire(timeout=timeout)
            if not acquired:
                raise RuntimeError(
                    f"[rate_limited] Could not acquire slot for {func.__name__} "
                    f"within {timeout}s (limiter: {limiter.name})"
                )
            return func(*args, **kwargs)
        return wrapper
    return decorator


# ============================================================================
# Rate-limit-aware retry wrapper with exponential backoff
# Handles 403 (rate limit hit) and transient 5xx errors from AngelOne
# ============================================================================

def call_with_retry(func, *args, limiter: TokenBucketLimiter = None,
                    max_retries: int = 3, base_delay: float = 1.0,
                    **kwargs):
    """
    Calls func(*args, **kwargs) with:
    1. Rate limiter acquisition (if limiter provided)
    2. Exponential backoff retry on failure (403, 5xx, ConnectionError, etc.)

    Returns the function result or raises the last exception after max_retries.

    Usage example in data_fetcher.py:
        result = call_with_retry(
            self.api.getCandleData, payload,
            limiter=CANDLE_LIMITER
        )
    """
    last_exc = None
    for attempt in range(max_retries):
        try:
            if limiter:
                acquired = limiter.acquire()
                if not acquired:
                    limiter_name = getattr(limiter, 'name', 'unknown')
                    raise RuntimeError(
                        f"[call_with_retry] Could not acquire slot for {func.__name__} "
                        f"within timeout (limiter: {limiter_name})"
                    )
            result = func(*args, **kwargs)
            # AngelOne SDK returns dict with 'status': False on error
            if isinstance(result, dict) and result.get('status') is False:
                msg = result.get('message', 'Unknown API error')
                error_code = result.get('errorcode', '')
                normalized_msg = msg.lower()
                rate_limit_error = (
                    'rate' in normalized_msg
                    or 'too many requests' in normalized_msg
                    or 'exceeding access rate' in normalized_msg
                    or error_code in ('AG8001', 'AG8002', 'AB1021', '403')
                )
                if rate_limit_error:
                    wait = base_delay * (2 ** attempt)
                    logger.warning(
                        f"[call_with_retry] Rate limit hit calling {func.__name__}, "
                        f"attempt {attempt+1}/{max_retries}, sleeping {wait:.1f}s"
                    )
                    time.sleep(wait)
                    last_exc = RuntimeError(f"Rate limit: {msg}")
                    continue
                # Other API error — don't retry (bad params, invalid token, etc.)
                logger.error(f"[call_with_retry] API error from {func.__name__}: {msg} (code: {error_code})")
                return result
            return result
        except Exception as e:
            wait = base_delay * (2 ** attempt)
            logger.warning(
                f"[call_with_retry] Exception in {func.__name__} attempt {attempt+1}/{max_retries}: "
                f"{type(e).__name__}: {e}, retrying in {wait:.1f}s"
            )
            time.sleep(wait)
            last_exc = e

    logger.error(f"[call_with_retry] All {max_retries} retries exhausted for {func.__name__}")
    raise last_exc
