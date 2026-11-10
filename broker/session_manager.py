# session_manager.py
# AngelOne SmartAPI Session Lifecycle Manager  [ANGEL-ONLY LEGACY MODULE]
# QuantOS Runtime — src/core/session_manager.py
#
# NOTE: This module is ONLY used by the Angel One broker adapter (angel_one_adapter.py).
# For Shoonya, session management is handled by ShoonyaSessionProvider in shoonya_adapter.py.
# This file is preserved for backward compatibility if Angel One is re-enabled.
#
# Responsibilities:
#   1. Single shared login for the entire process (no duplicate logins)
#   2. Automatic JWT refresh using refreshToken (1 req/sec, 1000/hr)
#      before the daily midnight (00:00 IST) token flush
#   3. Automatic re-login if refresh fails (e.g. refresh token also expired)
#   4. Thread-safe access to current jwt_token and feed_token
#   5. Broadcasts refresh events so feed_service/brain_service can update
#      their AngelOne SDK instances without restarting
#
# Key Facts (from AngelOne SmartAPI docs):
#   - JWT token expires at 00:00 IST every day (midnight)
#   - refreshToken endpoint: 1 req/sec, 1000 req/hour
#   - loginByPassword endpoint: 1 req/sec (use sparingly)
#   - Feed token is ONLY for WebSocket — cannot be used for REST
#   - After midnight flush, full re-login required to get new feed token
#   - Session flushed at 5 AM some sources say — we refresh at 23:50 IST to be safe

import os
import time
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo  # Python 3.9+

try:
    from logger import get_logger
    logger = get_logger("session_manager")
except ImportError:
    import logging
    logger = logging.getLogger("session_manager")
    logging.basicConfig(level=logging.INFO)

try:
    from broker.rate_limiter import LOGIN_LIMITER, TOKEN_REFRESH_LIMITER, call_with_retry
except ImportError:
    LOGIN_LIMITER = None
    TOKEN_REFRESH_LIMITER = None
    def call_with_retry(func, *args, limiter=None, max_retries=3, base_delay=1.0, **kwargs):
        return func(*args, **kwargs)

IST = ZoneInfo("Asia/Kolkata")

# Refresh 10 minutes before midnight IST to ensure tokens are valid at market open next day
# and to handle any clock skew
REFRESH_BEFORE_MIDNIGHT_MINUTES = 10


class SessionManager:
    """
    Singleton-pattern session manager.
    Maintains a single authenticated AngelOne session across the entire process.

    Usage:
        from broker.session_manager import get_session
        session = get_session()          # Get the active session
        api = session.api                # SmartConnect instance
        jwt = session.jwt_token          # Current JWT token
        feed = session.feed_token        # Current feed token (WebSocket)
        session.register_refresh_cb(cb)  # Called after every successful token refresh
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        self.api = None
        self.jwt_token = None
        self.refresh_token = None
        self.feed_token = None
        self.client_id = os.getenv("ANGEL_CLIENT_ID", "")
        self.api_key = os.getenv("ANGEL_API_KEY", "")
        self._refresh_callbacks = []  # list of callables to invoke after token refresh
        self._token_lock = threading.RLock()
        self._refresher_thread = None
        self._running = False

    def login(self):
        """
        Full login via loginByPassword.
        Should only be called once at startup or after refresh failure.
        Respects LOGIN_LIMITER (1 req/sec).
        """
        from broker.angel_connection import get_angel_connection
        logger.info("[SessionManager] Performing full login...")
        api, session_data = call_with_retry(
            get_angel_connection,
            limiter=LOGIN_LIMITER,
            max_retries=3,
            base_delay=2.0
        )
        with self._token_lock:
            self.api = api
            self.jwt_token = session_data.get("jwtToken", "")
            self.refresh_token = session_data.get("refreshToken", "")
            self.feed_token = session_data.get("feedToken", "")
        logger.info("[SessionManager] Login successful. JWT and feed tokens acquired.")
        return self.api, session_data

    def refresh(self):
        """
        Refresh JWT using refreshToken endpoint (cheaper than full re-login).
        Falls back to full re-login if refresh fails.
        Respects TOKEN_REFRESH_LIMITER (1 req/sec, 1000/hr).
        Returns True on success, False on failure.
        """
        if not self.api or not self.refresh_token:
            logger.warning("[SessionManager] No existing session to refresh — doing full login.")
            try:
                self.login()
                return True
            except Exception as e:
                logger.error(f"[SessionManager] Full re-login failed: {e}")
                return False

        try:
            logger.info("[SessionManager] Refreshing JWT token via generateTokens...")
            if TOKEN_REFRESH_LIMITER:
                TOKEN_REFRESH_LIMITER.acquire()

            result = self.api.generateToken(self.refresh_token)
            if not result or result.get('status') is False:
                raise RuntimeError(f"generateToken failed: {result}")

            data = result.get('data', {})
            with self._token_lock:
                self.jwt_token = data.get('jwtToken', self.jwt_token)
                self.refresh_token = data.get('refreshToken', self.refresh_token)
                # feed_token is NOT updated by generateToken — only full login provides it
                # WebSocket must reconnect with the ORIGINAL feed_token or restart after midnight

            logger.info("[SessionManager] JWT token refreshed successfully.")
            self._notify_refresh_callbacks()
            return True

        except Exception as e:
            logger.error(f"[SessionManager] Token refresh failed: {e}. Attempting full re-login...")
            try:
                self.login()
                self._notify_refresh_callbacks()
                return True
            except Exception as re_err:
                logger.critical(f"[SessionManager] Full re-login also failed: {re_err}")
                return False

    def register_refresh_cb(self, callback):
        """
        Register a callback invoked after every successful token refresh.
        Signature: callback(jwt_token: str, feed_token: str)
        Useful for feed_service to update its WS auth without restarting.
        """
        self._refresh_callbacks.append(callback)

    def _notify_refresh_callbacks(self):
        with self._token_lock:
            jwt = self.jwt_token
            feed = self.feed_token
        for cb in self._refresh_callbacks:
            try:
                cb(jwt, feed)
            except Exception as e:
                logger.error(f"[SessionManager] Refresh callback error: {e}")

    def start_auto_refresh(self):
        """
        Starts a background thread that refreshes the JWT before midnight IST.
        Checks every minute. Refreshes at 23:50 IST to be safe before 00:00 flush.
        """
        if self._refresher_thread and self._refresher_thread.is_alive():
            return
        self._running = True
        self._refresher_thread = threading.Thread(
            target=self._auto_refresh_loop,
            daemon=True,
            name="session_auto_refresh"
        )
        self._refresher_thread.start()
        logger.info("[SessionManager] Auto-refresh thread started (refreshes at 23:50 IST).")

    def stop_auto_refresh(self):
        self._running = False

    def _auto_refresh_loop(self):
        """
        Background thread:
        - Wakes up every 60 seconds
        - Checks if current IST time is between 23:50 and 23:59
        - If yes and not already refreshed today, performs token refresh
        """
        last_refresh_date = None

        while self._running:
            time.sleep(60)
            try:
                now_ist = datetime.now(IST)

                # Refresh window: 23:50 to 23:59 IST
                in_refresh_window = (now_ist.hour == 23 and now_ist.minute >= REFRESH_BEFORE_MIDNIGHT_MINUTES)

                if in_refresh_window and last_refresh_date != now_ist.date():
                    logger.info(f"[SessionManager] Pre-midnight refresh window — refreshing JWT at {now_ist.strftime('%H:%M IST')}...")
                    success = self.refresh()
                    if success:
                        last_refresh_date = now_ist.date()
                        logger.info("[SessionManager] Pre-midnight refresh complete. Session valid for next trading day.")
                    else:
                        logger.critical("[SessionManager] Pre-midnight refresh FAILED. Manual intervention may be needed.")

                # Emergency catch: if it's 09:00-09:15 IST and token looks stale
                # (could happen if system was off during midnight)
                if now_ist.hour == 9 and 0 <= now_ist.minute <= 14:
                    if last_refresh_date != now_ist.date():
                        logger.warning("[SessionManager] Token may be stale (morning of new day). Forcing refresh...")
                        success = self.refresh()
                        if success:
                            last_refresh_date = now_ist.date()

            except Exception as e:
                logger.error(f"[SessionManager] Auto-refresh loop error: {e}")

    def get_tokens(self):
        """Returns (jwt_token, feed_token) thread-safely."""
        with self._token_lock:
            return self.jwt_token, self.feed_token

    def is_alive(self) -> bool:
        """Returns True if session appears valid (has jwt token)."""
        with self._token_lock:
            return bool(self.jwt_token)


# ============================================================================
# Module-level singleton accessor
# ============================================================================

_session_instance = None
_session_init_lock = threading.Lock()


def get_session(auto_login: bool = True) -> SessionManager:
    """
    Returns the singleton SessionManager.
    If auto_login=True and not yet logged in, performs login automatically.
    """
    global _session_instance
    with _session_init_lock:
        if _session_instance is None:
            _session_instance = SessionManager()
            if auto_login and not _session_instance.is_alive():
                _session_instance.login()
                _session_instance.start_auto_refresh()
    return _session_instance
