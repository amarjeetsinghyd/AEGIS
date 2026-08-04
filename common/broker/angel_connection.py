import os
import pyotp
import time
import random
from dotenv import load_dotenv
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2
import urllib.request

from common.logger import get_logger
logger = get_logger("angel_connection")


# Load environment variables from .env file
load_dotenv()

# Fetch credentials
api_key = os.getenv("ANGEL_API_KEY")
client_id = os.getenv("ANGEL_CLIENT_ID")
password = os.getenv("ANGEL_PASSWORD")
totp_secret = os.getenv("ANGEL_TOTP_SECRET")

def _is_transient_error(exc: Exception) -> bool:
    """Return True for errors that are expected to disappear after a short wait.

    The Angel SDK may raise exceptions that expose a ``response`` attribute
    with a ``status_code`` (e.g., HTTP 429).  If such an attribute exists we
    inspect it.  As a fallback we look for common substrings in the exception
    message.
    """
    if hasattr(exc, "response"):
        resp = getattr(exc, "response")
        if hasattr(resp, "status_code"):
            code = str(getattr(resp, "status_code"))
            if code == "429" or code.startswith("5"):
                return True
    msg = str(exc).lower()
    if "rate limit" in msg or "429" in msg or "temporarily unavailable" in msg:
        return True
    return False


def get_angel_connection():
    """Initialises and returns the Angel One SmartConnect object.

    Retries are applied **only** to the ``generateSession`` call (or other
    network/API failures).  TOTP generation errors are considered permanent and
    are raised immediately.
    """
    load_dotenv(override=True)
    api_key = os.getenv("ANGEL_API_KEY")
    client_id = os.getenv("ANGEL_CLIENT_ID")
    password = os.getenv("ANGEL_PASSWORD")
    totp_secret = os.getenv("ANGEL_TOTP_SECRET")

    if not all([api_key, client_id, password, totp_secret]):
        raise ValueError(
            "Angel API credentials not fully provided. Please check your .env file or Settings."
        )

    # Initialise the SmartConnect client once.
    smartApi = SmartConnect(api_key=api_key)

    # Generate TOTP – any exception here is permanent and should not be retried.
    try:
        totp = pyotp.TOTP(totp_secret).now()
    except Exception as e:
        raise ValueError(f"Invalid TOTP Secret. Error: {e}")

    max_attempts = 3
    base_delay = 2  # seconds
    attempt = 0
    while True:
        attempt += 1
        try:
            data = smartApi.generateSession(client_id, password, totp)
            if data["status"]:
                return smartApi, data["data"]
            else:
                # SDK returned a structured failure – treat as permanent.
                raise Exception(f"Login failed: {data}")
        except Exception as exc:
            # Decide whether to retry.
            if attempt >= max_attempts or not _is_transient_error(exc):
                raise
            jitter = random.uniform(0, 0.5)
            wait_seconds = base_delay * (2 ** (attempt - 1)) + jitter
            logger.warning(
                f"Transient Angel login error (attempt {attempt}/{max_attempts}): {exc}. "
                f"Next retry in {wait_seconds:.2f}s"
            )
            time.sleep(wait_seconds)

def get_websocket_connection(auth_token, api_key, client_id, feed_token):
    """
    Initializes and returns the Angel One SmartWebSocketV2 object.
    """
    sws = SmartWebSocketV2(auth_token, api_key, client_id, feed_token)
    return sws

if __name__ == "__main__":
    try:
        smartApi, session_data = get_angel_connection()
        
        # A simple API call to check profile, validating the connection
        profile = smartApi.getProfile(session_data['refreshToken'])
        logger.info("Successfully connected to Angel One!")
        logger.info(f"Logged in as: {profile['data']['name']}")
    except Exception as e:
        logger.error(f"Error connecting to Angel One: {e}")
