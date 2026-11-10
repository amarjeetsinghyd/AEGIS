"""Adapter that wraps the existing Angel One integration.

The adapter implements the :class:`IBrokerGateway` protocol and delegates
calls to the legacy ``src.core.angel_connection`` module and the
``DataFetcher`` utility.
"""

from typing import Any, List

# Import static manifest models
from broker.manifest import (
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
from market.instrument import UniversalInstrument
from market.instrument_registry import bulk_register

import json
import os
from broker import angel_connection
from config.settings import get_data_dir
DataFetcher = None  # deferred; Angel path optional

from broker.interfaces import (
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
    def __init__(self, api, session_data=None) -> None:
        self._api = api
        self._session_data = session_data or {}
        self._tick_listeners = []
        self._close_listeners = []
        self._pending_tokens = []
        self.ws = None
        self._is_connected = False

    def add_tick_listener(self, listener) -> None:
        if listener not in self._tick_listeners:
            self._tick_listeners.append(listener)

    def remove_tick_listener(self, listener) -> None:
        if listener in self._tick_listeners:
            self._tick_listeners.remove(listener)

    def add_close_listener(self, listener) -> None:
        if listener not in self._close_listeners:
            self._close_listeners.append(listener)

    def start_live_feed(self, on_tick_callback=None, on_open_callback=None, on_error_callback=None) -> None:
        import threading
        from logger import get_logger
        logger = get_logger("angel_adapter")

        if self.ws is not None and self._is_connected:
            return
        from broker import angel_connection

        client_id = os.getenv("ANGEL_CLIENT_ID")
        api_key = os.getenv("ANGEL_API_KEY")
        jwt_token = self._session_data.get("jwtToken")
        feed_token = self._session_data.get("feedToken")

        if not (jwt_token and feed_token):
            try:
                _, s_data = angel_connection.get_angel_connection()
                self._session_data = s_data
                jwt_token = s_data.get("jwtToken")
                feed_token = s_data.get("feedToken")
            except Exception as e_login:
                logger.error(f"[ANGEL_WS] Session refresh failed in start_live_feed: {e_login}")
                return

        self.ws = angel_connection.get_websocket_connection(
            jwt_token, api_key, client_id, feed_token
        )

        def _normalize_angel_tick(msg):
            if not isinstance(msg, dict):
                return msg
            if "last_traded_price" in msg or "token" in msg:
                tok = str(msg.get("token", msg.get("tk", "")))
                buys = msg.get("best_5_buy_data", [])
                sells = msg.get("best_5_sell_data", [])
                raw_lp = float(msg.get("last_traded_price", msg.get("lp", 0)) or 0)
                # Angel SmartWebSocketV2 prices are integers in paise -> scale to Rupees
                norm = {
                    "tk": tok,
                    "token": tok,
                    "lp": raw_lp / 100.0 if raw_lp > 0 else 0.0,
                    "v": float(msg.get("volume_trade_for_the_day", msg.get("vol", 0)) or 0),
                    "e": "NSE" if msg.get("exchange_type") == 1 else "BSE",
                }
                for i in range(5):
                    if i < len(buys) and isinstance(buys[i], dict):
                        norm[f"bp{i+1}"] = float(buys[i].get("price", 0) or 0) / 100.0
                        norm[f"bq{i+1}"] = int(buys[i].get("quantity", 0) or 0)
                    if i < len(sells) and isinstance(sells[i], dict):
                        norm[f"sp{i+1}"] = float(sells[i].get("price", 0) or 0) / 100.0
                        norm[f"sq{i+1}"] = int(sells[i].get("quantity", 0) or 0)
                if "bp1" not in norm:
                    raw_bp1 = float(msg.get("best_bid_price", 0) or 0)
                    raw_sp1 = float(msg.get("best_ask_price", 0) or 0)
                    norm["bp1"] = raw_bp1 / 100.0 if raw_bp1 > 0 else 0.0
                    norm["bq1"] = int(msg.get("best_bid_quantity", 0) or 0)
                    norm["sp1"] = raw_sp1 / 100.0 if raw_sp1 > 0 else 0.0
                    norm["sq1"] = int(msg.get("best_ask_quantity", 0) or 0)
                return norm
            return msg

        def wrapped_on_data(wsapp, message):
            norm_tick = _normalize_angel_tick(message)
            if on_tick_callback:
                try:
                    on_tick_callback(norm_tick)
                except Exception:
                    pass
            for listener in list(self._tick_listeners):
                try:
                    listener(norm_tick)
                except Exception as e_lst:
                    logger.debug(f"[ANGEL_WS] Tick listener error: {e_lst}")

        def wrapped_on_open(wsapp):
            self._is_connected = True
            logger.info("[ANGEL_WS] Connected successfully to SmartWebSocketV2.")
            if on_open_callback:
                try:
                    on_open_callback()
                except Exception:
                    pass
            if self._pending_tokens:
                pending = list(set(self._pending_tokens))
                self._pending_tokens.clear()
                self._raw_subscribe(pending, 1)

        def wrapped_on_error(wsapp, error):
            logger.error(f"[ANGEL_WS] SmartWebSocketV2 error: {error}")
            if on_error_callback:
                try:
                    on_error_callback(error)
                except Exception:
                    pass

        def wrapped_on_close(wsapp):
            self._is_connected = False
            logger.warning("[ANGEL_WS] SmartWebSocketV2 disconnected.")
            for listener in list(self._close_listeners):
                try:
                    listener()
                except Exception:
                    pass

        self.ws.on_open = wrapped_on_open
        self.ws.on_data = wrapped_on_data
        self.ws.on_error = wrapped_on_error
        self.ws.on_close = wrapped_on_close

        def _run():
            try:
                self.ws.connect()
            except Exception as e_ws:
                logger.error(f"[ANGEL_WS] Background connect loop terminated: {e_ws}")

        t = threading.Thread(target=_run, name="angel-ws-runner", daemon=True)
        t.start()

    def _raw_subscribe(self, tokens: list, exch_type: int) -> None:
        if not self.ws or not self._is_connected:
            self._pending_tokens.extend(tokens)
            return
        from logger import get_logger
        logger = get_logger("angel_adapter")
        # Chunk subscriptions into batches of 30 to comply with Angel rate limits
        batch_size = 30
        for i in range(0, len(tokens), batch_size):
            chunk = [str(t) for t in tokens[i:i + batch_size]]
            try:
                self.ws.subscribe("argus_sub", 3, [{"exchangeType": exch_type, "tokens": chunk}])
                logger.info(f"[BROKER] Angel One subscribed {len(chunk)} tokens (Batch {i//batch_size + 1}) in SNAP_QUOTE mode.")
            except Exception as e_sub:
                logger.error(f"[BROKER] Angel One subscribe error: {e_sub}")

    def subscribe(self, tokens: list, exchange: str = "NSE", feed_type: str = "t", **kwargs) -> None:
        exch_type = 1 if str(exchange).upper() == "NSE" else (2 if str(exchange).upper() == "NFO" else 3)
        if not self.ws or not self._is_connected:
            self._pending_tokens.extend(tokens)
            self.start_live_feed()
            return
        self._raw_subscribe(tokens, exch_type)

    def unsubscribe(self, tokens: list, exchange: str) -> None:
        pass

    def get_quote(self, exchange: str, token: str) -> dict:
        try:
            res = self._api.ltpData(exchange, "dummy", token)
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
        from logger import get_logger
        logger = get_logger("angel_adapter")
        
        # Load rate limiter if available
        try:
            from broker.rate_limiter import CANDLE_LIMITER, call_with_retry as _rl_call_with_retry
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
        self._market_data_provider = AngelMarketDataProvider(self._api, session_data=self._session_data)
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
                depth_supported=True,
                heartbeat_interval_seconds=30,
                auto_reconnect=True,
            ),
            market_data=MarketDataCapability(
                live_quotes=True,
                ohlc=True,
                open_interest=False,
                market_depth=True,
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

    def add_tick_listener(self, listener) -> None:
        self._market_data_provider.add_tick_listener(listener)

    def remove_tick_listener(self, listener) -> None:
        self._market_data_provider.remove_tick_listener(listener)

    def add_close_listener(self, listener) -> None:
        self._market_data_provider.add_close_listener(listener)

    def subscribe(self, tokens: list, exchange: str = "NSE", **kwargs) -> None:
        self._market_data_provider.subscribe(tokens, exchange, **kwargs)
