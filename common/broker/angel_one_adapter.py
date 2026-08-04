"""Adapter that wraps the existing Angel One integration.

The adapter implements the :class:`IBrokerGateway` protocol and delegates
calls to the legacy ``src.core.angel_connection`` module and the
``DataFetcher`` utility.
"""

from typing import Any, List

# Import static manifest models
from common.broker.manifest import (
    BrokerManifest,
    IdentityCapability,
    AuthenticationCapability,
    RateLimitCapability,
    WebSocketCapability,
    MarketDataCapability,
    HistoricalDataCapability,
    OrderManagementCapability,
    ProductSupportCapability,
    ExchangeSupportCapability,
    FeatureFlagCapability,
    PerformanceRecommendation,
)
from common.market.instrument import UniversalInstrument
from common.market.instrument_registry import bulk_register

import json
import os
from common.broker import angel_connection
from common.config import get_data_dir
DataFetcher = None  # deferred; Angel path optional

from common.broker.interfaces import (
    IBrokerGateway,
    ISessionProvider,
    IMarketDataProvider,
    IExecutionProvider,
    IHistoricalProvider,
    IPortfolioProvider,
)

# Concrete provider implementations ------------------------------------------------

class AngelSessionProvider(ISessionProvider):
    def __init__(self, session_data) -> None:
        self._session_data = session_data

    def get_session(self) -> Any:
        return self._session_data

class AngelMarketDataProvider(IMarketDataProvider):
    def __init__(self, api) -> None:
        self._api = api

    def start_live_feed(self, on_tick_callback, on_open_callback=None, on_error_callback=None) -> None:
        client_id = os.getenv("ANGEL_CLIENT_ID")
        api_key = os.getenv("ANGEL_API_KEY")
        # To get session data without circular dependencies, we rely on the global session manager
        from common.broker.session_manager import LifecycleManager
        sm = LifecycleManager()
        session_data = sm.get_session_data()
        feed_token = session_data.get("feedToken")
        jwt_token = session_data.get("jwtToken")

        self.ws = angel_connection.get_websocket_connection(
            jwt_token, api_key, client_id, feed_token
        )
        
        # Wrap on_data to standard format (Angel returns standard format by default but just pass it through)
        def wrapped_on_data(wsapp, message):
            on_tick_callback(message)

        def wrapped_on_open(wsapp):
            if on_open_callback: on_open_callback()

        def wrapped_on_error(wsapp, error):
            if on_error_callback: on_error_callback(error)

        self.ws.on_open = wrapped_on_open
        self.ws.on_data = wrapped_on_data
        self.ws.on_error = wrapped_on_error
        
        self.ws.connect()

    def subscribe(self, tokens: list, exchange: str) -> None:
        exch_type = 1 if exchange == "NSE" else (2 if exchange == "NFO" else 3)
        self.ws.subscribe("mega_sub", 3, [{"exchangeType": exch_type, "tokens": tokens}])
        
    def unsubscribe(self, tokens: list, exchange: str) -> None:
        pass # Angel One does not have a clean unsubscribe list method via SmartWebSocketV2 without mode switch

    def get_quote(self, exchange: str, token: str) -> dict:
        """Fetch the latest snapshot quote for a given token."""
        try:
            res = self._api.ltpData(exchange, "dummy", token) # Angel LTP API
            if res and res.get('status'):
                return {"ltp": float(res['data']['ltp'])}
        except Exception:
            pass
        return {"ltp": 0.0}

class AngelExecutionProvider(IExecutionProvider):
    def __init__(self, api) -> None:
        self._api = api

    def place_order(self, *args, **kwargs) -> Any:
        # The legacy code uses ``self.api`` directly; expose a thin wrapper.
        return self._api.place_order(*args, **kwargs)

class AngelHistoricalProvider(IHistoricalProvider):
    def __init__(self, api) -> None:
        self._api = api

    def get_historical(self, exchange: str, token: str, interval: str, start_time, end_time) -> "pd.DataFrame":
        import pandas as pd
        import time
        from common.logger import get_logger
        logger = get_logger("angel_adapter")
        
        # Load rate limiter if available
        try:
            from common.broker.rate_limiter import CANDLE_LIMITER, call_with_retry as _rl_call_with_retry
            limiter = CANDLE_LIMITER
        except ImportError:
            limiter = None
            _rl_call_with_retry = None

        historicParam = {
            "exchange": exchange,
            "symboltoken": token,
            "interval": interval,
            "fromdate": start_time.strftime("%Y-%m-%d %H:%M"), 
            "todate": end_time.strftime("%Y-%m-%d %H:%M")
        }

        # Local retry logic for rate limits
        def _call_api_with_retry(api_func, *args, max_retries=5, initial_delay=1.0, **kwargs):
            if limiter is not None and _rl_call_with_retry is not None:
                return _rl_call_with_retry(
                    api_func, *args, limiter=limiter, max_retries=max_retries, base_delay=initial_delay, **kwargs
                )
            
            delay = initial_delay
            for attempt in range(max_retries):
                try:
                    return api_func(*args, **kwargs)
                except Exception as e:
                    err_msg = str(e).lower()
                    if "exceeding access rate" in err_msg or "access denied" in err_msg or "rate limit" in err_msg or "too many requests" in err_msg or "ab1021" in err_msg:
                        if attempt < max_retries - 1:
                            logger.info(f"API Rate Limit hit (attempt {attempt+1}/{max_retries}). Retrying in {delay:.1f}s...")
                            time.sleep(delay)
                            delay *= 2.0
                            continue
                    if attempt < max_retries - 1:
                        logger.info(f"API call failed (attempt {attempt+1}/{max_retries}): {e}. Retrying in {delay:.1f}s...")
                        time.sleep(delay)
                        delay *= 2.0
                    else:
                        raise e
            raise Exception("Max retries exceeded for API call")

        try:
            response = _call_api_with_retry(self._api.getCandleData, historicParam)
            if response and response.get('status') and response.get('data'):
                columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
                df = pd.DataFrame(response['data'], columns=columns)
                df['timestamp'] = pd.to_datetime(df['timestamp'])
                return df
        except Exception as e:
            logger.error(f"Error fetching candles for token {token}: {e}")
            
        return pd.DataFrame()

class AngelPortfolioProvider(IPortfolioProvider):
    def __init__(self, api) -> None:
        self._api = api

    def get_positions(self, *args, **kwargs) -> Any:
        return self._api.get_positions(*args, **kwargs)

# Gateway implementation ----------------------------------------------------------

class AngelOneAdapter(IBrokerGateway):
    def __init__(self) -> None:
        # Initialise the underlying Angel connection once and reuse it.
        self._api, self._session_data = angel_connection.get_angel_connection()
        self._session_provider = AngelSessionProvider(self._session_data)
        self._market_data_provider = AngelMarketDataProvider(self._api)
        self._execution_provider = AngelExecutionProvider(self._api)
        self._historical_provider = AngelHistoricalProvider(self._api)
        self._portfolio_provider = AngelPortfolioProvider(self._api)

        # -------------------------------------------------------------------
        # Static Broker Manifest – describes Angel One capabilities.
        # -------------------------------------------------------------------
        self._manifest = BrokerManifest(
            identity=IdentityCapability(
                broker_name="Angel One",
                broker_version="1.0.0",
                vendor="Angel Broking",
            ),
            authentication=AuthenticationCapability(
                mechanism="api_key",
                token_endpoint=None,
                token_refresh_supported=False,
            ),
            rate_limits=RateLimitCapability(
                rest_requests_per_sec=10.0,
                order_requests_per_sec=5.0,
                historical_requests_per_sec=2.0,
            ),
            websocket=WebSocketCapability(
                max_subscriptions=500,
                tick_format="json",
                depth_supported=False,
                heartbeat_interval_seconds=30,
                auto_reconnect=True,
            ),
            market_data=MarketDataCapability(
                live_quotes=True,
                ohlc=True,
                open_interest=False,
                market_depth=False,
                greeks=False,
                streaming_supported=True,
            ),
            historical=HistoricalDataCapability(
                max_lookback_days=365,
                max_candles_per_request=5000,
                supports_adjusted_close=False,
                supports_tick_history=False,
            ),
            order_management=OrderManagementCapability(
                modify_order=True,
                cancel_order=True,
                basket_orders=False,
                bracket_orders=False,
                cover_orders=False,
                gtt_orders=False,
                supported_order_variants=("MARKET", "LIMIT"),
            ),
            product_support=ProductSupportCapability(
                equities=True,
                futures=True,
                options=True,
                commodities=False,
                currencies=False,
                derivatives=False,
            ),
            exchange_support=ExchangeSupportCapability(
                exchanges=("NSE", "BSE"),
                primary_exchange="NSE",
            ),
            feature_flags=FeatureFlagCapability(
                paper_trading=False,
                replay_mode=False,
                simulation_mode=False,
                sandbox=False,
            ),
            performance=PerformanceRecommendation(
                recommended_batch_size=200,
                recommended_retry_delay_ms=250,
                recommended_throttling_interval_ms=1200,
                session_timeout_seconds=300,
            ),
        )



    @property
    def session_provider(self) -> ISessionProvider:
        return self._session_provider

    @property
    def market_data_provider(self) -> IMarketDataProvider:
        return self._market_data_provider

    @property
    def execution_provider(self) -> IExecutionProvider:
        return self._execution_provider

    @property
    def historical_provider(self) -> IHistoricalProvider:
        return self._historical_provider

    @property
    def portfolio_provider(self) -> IPortfolioProvider:
        return self._portfolio_provider

    @property
    def api(self) -> Any:
        """Expose the underlying Angel API client for legacy callers.

        Some existing services (e.g., PaperTrader) expect direct access to the
        SDK instance.  The adapter provides it via this read‑only property.
        """
        return self._api



    @property
    def manifest(self) -> BrokerManifest:
        """Return the static :class:`BrokerManifest` describing Angel One.

        The registry registers the adapter and reads this property to obtain
        the broker's immutable capability description.
        """
        return self._manifest
