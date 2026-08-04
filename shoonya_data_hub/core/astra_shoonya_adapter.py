import os
import json
import time
import logging

from kronos_bot.core.exceptions import BrokerFeatureUnavailableError
from kronos_bot.core.manifest import BrokerManifest, IdentityCapability, AuthenticationCapability, RateLimitCapability, WebSocketCapability, MarketDataCapability, HistoricalDataCapability, OrderManagementCapability, ProductSupportCapability, ExchangeSupportCapability, FeatureFlagCapability, PerformanceRecommendation
from kronos_bot.core.rate_limiter import TokenBucketLimiter, call_with_retry
from kronos_bot.core.interfaces import IBrokerGateway, ISessionProvider, IMarketDataProvider, IExecutionProvider, IHistoricalProvider, IPortfolioProvider
from kronos_bot.execution.order_lifecycle import OrderLifecycle
from kronos_bot.execution.trade_context import TradeLeg
from kronos_bot.core.logger import get_logger
from kronos_bot.config.settings import DEFAULT_PRODUCT_TYPE, DATA_DIR
logger = get_logger("shoonya_adapter")

try:
    from NorenRestApiPy.NorenApi import NorenApi as NorenApiPy
except Exception as e:
    from kronos_bot.config.settings import ENABLE_LIVE_BROKERAGE_EXECUTION
    if ENABLE_LIVE_BROKERAGE_EXECUTION:
        logger.critical(f"Shoonya SDK import failed in LIVE mode: {e}")
        raise
    
    logger.error(f"Shoonya SDK import failed: {e}. Falling back to mock for PAPER/DEV mode.")
    class _MockNorenApi:
        def __init__(self, *args, **kwargs): pass
        def login(self, *args, **kwargs): return {"status": True, "susertoken": "dummy_token"}
        def set_session(self, *args, **kwargs): pass
        def get_limits(self, *args, **kwargs): return {"status": "ok", "cash": "100000.00"}
        def get_positions(self, *args, **kwargs): return []
        def get_holdings(self, *args, **kwargs): return []
        def get_quotes(self, *args, **kwargs): return {"status": "ok"}
        def get_time_price_series(self, *args, **kwargs): return []
        def get_daily_price_series(self, *args, **kwargs): return []
        def searchscrip(self, *args, **kwargs): return {"values": []}
        def place_order(self, *args, **kwargs): return {"status": "Ok", "norenordno": "dummy_id"}
        def modify_order(self, *args, **kwargs): return {"status": "Ok"}
        def cancel_order(self, *args, **kwargs): return {"status": "Ok"}
        def get_order_book(self, *args, **kwargs): return []
        def get_trade_book(self, *args, **kwargs): return []
    NorenApiPy = _MockNorenApi

SESSION_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "shoonya_session.json")


class ShoonyaSessionProvider(ISessionProvider):
    def __init__(self, api: NorenApiPy) -> None:
        self.logger = logger
        self._api = api
        
        from dotenv import load_dotenv
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".env")
        load_dotenv(dotenv_path=env_path)
        
        self.client_id = os.getenv("SHOONYA_CLIENT_ID", "")
        self.client_secret = os.getenv("SHOONYA_CLIENT_SECRET", "")
        self.userid = os.getenv("SHOONYA_USERNAME", "")
        self.password = os.getenv("SHOONYA_PASSWORD", "")
        self.totp = os.getenv("SHOONYA_TOTP", "")
        self.susertoken = None
        self._restore_or_login()

    def _restore_or_login(self):
        if os.path.exists(SESSION_FILE):
            try:
                with open(SESSION_FILE, "r") as f:
                    data = json.load(f)
                    if data.get("userid") == self.userid and data.get("susertoken"):
                        self.susertoken = data["susertoken"]
                        actid = data.get("actid", self.userid)
                        self._api.set_session(self.userid, self.password, self.susertoken, self.susertoken)
                        self._api.injectOAuthHeader(self.susertoken, self.userid, actid)
                        limits = self._api.get_limits()
                        if isinstance(limits, dict) and limits.get("stat") != "Not_Ok":
                            self.logger.debug(f"Restored and validated Shoonya session for {self.userid}")
                            return
                        else:
                            self.logger.warning(f"Restored session invalid or expired for {self.userid}. Re-authenticating.")
                            self.susertoken = None
            except Exception as e:
                self.logger.error(f"Failed to restore Shoonya session: {e}")

        self._login()

    def _login(self):
        if not self.client_id or not self.userid:
            self.logger.warning("Shoonya credentials missing in .env")
            return
            
        try:
            from kronos_bot.core.shoonya_auth import ShoonyaHeadlessOAuthProvider
            oauth_provider = ShoonyaHeadlessOAuthProvider(
                client_id=self.client_id,
                client_secret=self.client_secret,
                userid=self.userid,
                password=self.password,
                totp_secret=self.totp
            )
            
            limiter = TokenBucketLimiter(per_second=1, name="shoonya_oauth")
            auth_code = call_with_retry(
                lambda: oauth_provider.authenticate(),
                limiter=limiter
            )
            
            if auth_code:
                result = self._api.getAccessToken(auth_code, self.client_secret, self.client_id, self.userid)
                if result is not None:
                    acc_tok, usrid, ref_tok, actid = result
                    self.susertoken = acc_tok
                    self.logger.info("Shoonya getAccessToken successful.")
                    self._api.set_session(self.userid, self.password, self.susertoken, self.susertoken)
                    os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
                    with open(SESSION_FILE, "w") as f:
                        json.dump({
                            "userid": self.userid,
                            "susertoken": self.susertoken,
                            "actid": actid,
                            "updated_at": time.time()
                        }, f)
                    self.logger.info(f"Shoonya OAuth login successful for {self.userid}")
                else:
                    self.logger.error("Failed to retrieve access token via getAccessToken")
            else:
                self.logger.error("Shoonya OAuth login failed: No auth code returned")

        except Exception as e:
            self.logger.error(f"Exception during Shoonya login: {e}")

    def get_session(self) -> dict:
        return {"access_token": self.susertoken}

    def get_quote(self, exchange: str, token: str) -> dict:
        if hasattr(self, "market_data_provider"):
            return self.market_data_provider.get_quote(exchange, token)
        return {}


class ShoonyaMarketDataProvider(IMarketDataProvider):
    QUOTE_LIMITER = TokenBucketLimiter(per_second=10, per_minute=500, name="shoonya_quote")
    HISTORICAL_LIMITER = TokenBucketLimiter(per_second=3, per_minute=180, name="shoonya_historical")

    def __init__(self, api: NorenApiPy):
        self._api = api
        self._live_quotes = {}
        self._subscribed_tokens = set()
        self._ws_started = False
        self._constituent_tokens = set()
        self._accumulated_synthetic_vol = 0
        
        from kronos_bot.core.ring_buffer import RingBufferManager
        self.ring_buffer_manager = RingBufferManager()
        self._index_token = ""
        self._option_ce_tokens = set()
        self._option_pe_tokens = set()
        self._option_oi_start = {}
        self._option_oi_latest = {}

    def get_quotes(self, instrument_ids: list) -> dict:
        res = {}
        for inst in instrument_ids:
            exch, token = inst.split(":") if ":" in inst else ("NSE", inst)
            data = call_with_retry(lambda: self._api.get_quotes(exchange=exch, token=token), limiter=self.QUOTE_LIMITER)
            res[inst] = data
        return res

    def search_instruments(self, query: str) -> list:
        resp = call_with_retry(
            lambda: self._api.searchscrip(exchange="NSE", searchtext=query),
            limiter=self.QUOTE_LIMITER
        )
        return resp.get("values", []) if isinstance(resp, dict) else resp

    def get_historical(self, exchange: str, token: str, starttime=None, endtime=None, interval=1) -> dict:
        if starttime is not None:
            resp = call_with_retry(
                lambda: self._api.get_time_price_series(exchange=exchange, token=token, starttime=starttime, endtime=endtime, interval=interval),
                limiter=self.HISTORICAL_LIMITER
            )
        else:
            resp = call_with_retry(
                lambda: self._api.get_daily_price_series(exchange=exchange, tradingsymbol=token, startdate=str(starttime), enddate=str(endtime)),
                limiter=self.HISTORICAL_LIMITER
            )
        return resp

    def start_live_feed(self, on_tick_callback, on_open_callback=None, on_error_callback=None) -> None:
        def event_handler_quote_update(message):
            if isinstance(message, dict):
                token = message.get("tk")
                if not token:
                    return
                v_total = int(message.get("v", 0) or 0)
                ltq = int(message.get("ltq", 0) or 0)
                if ltq == 0 and v_total > 0:
                    if not hasattr(self, "_last_vol"):
                        self._last_vol = {}
                    prev_v = self._last_vol.get(token, 0)
                    if prev_v > 0 and v_total > prev_v:
                        ltq = v_total - prev_v
                    self._last_vol[token] = v_total

                if str(token) in self._constituent_tokens and ltq > 0:
                    self._accumulated_synthetic_vol += ltq

                if hasattr(self, "ring_buffer_manager"):
                    self.ring_buffer_manager.process_tick(
                        message,
                        getattr(self, "_index_token", ""),
                        getattr(self, "_option_ce_tokens", set()),
                        getattr(self, "_option_pe_tokens", set())
                    )

                s_token = str(token)
                oi_val = message.get("oi") or message.get("poi") or message.get("oiv")
                if oi_val is not None and hasattr(self, "_option_ce_tokens"):
                    if s_token in self._option_ce_tokens or s_token in self._option_pe_tokens:
                        try:
                            oi_int = int(float(oi_val))
                            if oi_int > 0:
                                if s_token not in self._option_oi_start:
                                    self._option_oi_start[s_token] = oi_int
                                self._option_oi_latest[s_token] = oi_int
                        except (ValueError, TypeError):
                            pass

                std_msg = {
                    "token": str(token),
                    "exchange": message.get("e", "NSE"),
                    "last_traded_price": float(message.get("lp", 0) or 0),
                    "last_traded_quantity": int(ltq),
                    "volume_trade_for_the_day": int(v_total),
                    "average_traded_price": float(message.get("ap", 0) or 0),
                    "open": float(message.get("o", 0) or 0),
                    "high": float(message.get("h", 0) or 0),
                    "low": float(message.get("l", 0) or 0),
                    "close": float(message.get("c", 0) or 0),
                    "percent_change": float(message.get("pc", 0) or 0),
                }
                if "bp1" in message:
                    std_msg["bid"] = float(message["bp1"])
                if "sp1" in message:
                    std_msg["ask"] = float(message["sp1"])

                std_msg = {k: v for k, v in std_msg.items() if v != 0 or k in ["last_traded_price"]}
                on_tick_callback(std_msg)

        def event_handler_order_update(message):
            pass

        def open_callback():
            if on_open_callback:
                on_open_callback()

        def error_callback(err):
            if on_error_callback:
                on_error_callback(err)

        def close_callback():
            pass

        self._api.start_websocket(
            order_update_callback=event_handler_order_update,
            subscribe_callback=event_handler_quote_update,
            socket_open_callback=open_callback,
            socket_close_callback=close_callback,
            socket_error_callback=error_callback
        )

    def register_constituent_tokens(self, tokens: list):
        for tok in tokens:
            self._constituent_tokens.add(str(tok))
        logger.info(f"Registered {len(tokens)} constituent tokens for synthetic volume accumulator.")

    def register_option_tokens(self, ce_tokens: list, pe_tokens: list):
        if not hasattr(self, "_option_ce_tokens"):
            self._option_ce_tokens = set()
            self._option_pe_tokens = set()
            self._option_oi_start = {}
            self._option_oi_latest = {}
            self._strike_to_ce_token = {}
            self._strike_to_pe_token = {}
            self._token_to_strike = {}
            
        for tok in ce_tokens:
            self._option_ce_tokens.add(str(tok))
        for tok in pe_tokens:
            self._option_pe_tokens.add(str(tok))
        logger.info(f"Registered {len(ce_tokens)} CE and {len(pe_tokens)} PE option tokens for Net Option Delta OI tracking.")

    def register_option_token_metadata(self, option_df):
        if not hasattr(self, "_strike_to_ce_token"):
            self._strike_to_ce_token = {}
            self._strike_to_pe_token = {}
            self._token_to_strike = {}
            
        if option_df is not None and not option_df.empty:
            for _, row in option_df.iterrows():
                tok = str(row['token'])
                strike = float(row.get('strike', 0.0))
                symbol = str(row.get('symbol', ''))
                
                if strike > 0:
                    self._token_to_strike[tok] = strike
                    if 'CE' in symbol or symbol.endswith('C'):
                        self._strike_to_ce_token[strike] = tok
                    elif 'PE' in symbol or symbol.endswith('P'):
                        self._strike_to_pe_token[strike] = tok

    def get_strike_net_delta_oi(self, strike_price: float) -> float:
        if not hasattr(self, "_option_oi_latest"):
            return 0.0
            
        strike = float(strike_price)
        pe_tok = getattr(self, "_strike_to_pe_token", {}).get(strike)
        ce_tok = getattr(self, "_strike_to_ce_token", {}).get(strike)
        
        pe_delta = 0
        if pe_tok:
            start = self._option_oi_start.get(pe_tok, self._option_oi_latest.get(pe_tok, 0))
            latest = self._option_oi_latest.get(pe_tok, start)
            if start > 0 and latest > 0:
                pe_delta = latest - start
                
        ce_delta = 0
        if ce_tok:
            start = self._option_oi_start.get(ce_tok, self._option_oi_latest.get(ce_tok, 0))
            latest = self._option_oi_latest.get(ce_tok, start)
            if start > 0 and latest > 0:
                ce_delta = latest - start
                
        return float(pe_delta - ce_delta)

    def get_and_reset_synthetic_volume(self) -> int:
        vol = self._accumulated_synthetic_vol
        self._accumulated_synthetic_vol = 0
        return vol

    def get_current_option_net_delta_oi(self) -> tuple[float, float, float]:
        if not hasattr(self, "_option_ce_tokens"):
            return 0.0, 0.0, 0.0

        pe_delta = 0
        for tok in self._option_pe_tokens:
            start = self._option_oi_start.get(tok, self._option_oi_latest.get(tok, 0))
            latest = self._option_oi_latest.get(tok, start)
            if start > 0 and latest > 0:
                pe_delta += (latest - start)

        ce_delta = 0
        for tok in self._option_ce_tokens:
            start = self._option_oi_start.get(tok, self._option_oi_latest.get(tok, 0))
            latest = self._option_oi_latest.get(tok, start)
            if start > 0 and latest > 0:
                ce_delta += (latest - start)

        net_delta_oi = float(pe_delta - ce_delta)
        return net_delta_oi, float(pe_delta), float(ce_delta)

    def get_and_reset_option_net_delta_oi(self) -> tuple[float, float, float]:
        if not hasattr(self, "_option_ce_tokens"):
            return 0.0, 0.0, 0.0

        pe_delta = 0
        for tok in self._option_pe_tokens:
            start = self._option_oi_start.get(tok, self._option_oi_latest.get(tok, 0))
            latest = self._option_oi_latest.get(tok, start)
            if start > 0 and latest > 0:
                pe_delta += (latest - start)

        ce_delta = 0
        for tok in self._option_ce_tokens:
            start = self._option_oi_start.get(tok, self._option_oi_latest.get(tok, 0))
            latest = self._option_oi_latest.get(tok, start)
            if start > 0 and latest > 0:
                ce_delta += (latest - start)

        net_delta_oi = float(pe_delta - ce_delta)
        self._option_oi_start = {tok: self._option_oi_latest[tok] for tok in self._option_oi_latest}

        return net_delta_oi, float(pe_delta), float(ce_delta)

    def subscribe(self, tokens: list, exchange: str) -> None:
        if not tokens: return
        self._ensure_ws_started()
        for t in tokens:
            st = str(t)
            if st in ["26000", "1", "99926000", "99919000"]:
                self._index_token = st
        clean_tokens = [str(t).strip() for t in tokens if str(t).strip()]
        if not clean_tokens:
            logger.warning(f"Skipped Shoonya subscribe: no valid tokens for exchange {exchange}")
            return
        shoonya_tokens = [f"{exchange}|{t}" for t in clean_tokens]
        self._api.subscribe(shoonya_tokens, feed_type='d')
        logger.info(f"Subscribed {len(shoonya_tokens)} {exchange} tokens to Shoonya WebSocket feed")
        
    def unsubscribe(self, tokens: list, exchange: str) -> None:
        if not tokens: return
        shoonya_tokens = [f"{exchange}|{t}" for t in tokens]
        self._api.unsubscribe(shoonya_tokens, feed_type='d')

    def _ensure_ws_started(self):
        if not self._ws_started:
            self._ws_started = True
            try:
                logger.info("Initializing background Shoonya WebSocket feed for caching option LTP...")
                self.start_live_feed(self._on_ws_tick)
            except Exception as e:
                logger.error(f"Failed to start background Shoonya WebSocket: {e}")
                self._ws_started = False

    def _on_ws_tick(self, tick_data: dict):
        raw_token = str(tick_data.get("token") or tick_data.get("tk") or "").strip()
        token = raw_token.split("|")[-1].strip() if "|" in raw_token else raw_token
        if token:
            q_dict = self._live_quotes.setdefault(token, {"ltp": 0.0, "bid": 0.0, "ask": 0.0, "oi": 0.0, "volume": 0.0})
            if not isinstance(q_dict, dict):
                q_dict = {"ltp": float(q_dict) if isinstance(q_dict, (int, float)) else 0.0, "bid": 0.0, "ask": 0.0, "oi": 0.0, "volume": 0.0}
                self._live_quotes[token] = q_dict

            if "lp" in tick_data:
                try: q_dict["ltp"] = float(tick_data["lp"])
                except (ValueError, TypeError): pass
            if "bp1" in tick_data or "b" in tick_data:
                try: q_dict["bid"] = float(tick_data.get("bp1") or tick_data.get("b", 0.0))
                except (ValueError, TypeError): pass
            if "sp1" in tick_data or "a" in tick_data:
                try: q_dict["ask"] = float(tick_data.get("sp1") or tick_data.get("a", 0.0))
                except (ValueError, TypeError): pass
            oi_v = tick_data.get("oi") or tick_data.get("poi") or tick_data.get("oiv")
            if oi_v is not None:
                try: q_dict["oi"] = float(oi_v)
                except (ValueError, TypeError): pass
            if "v" in tick_data:
                try: q_dict["volume"] = float(tick_data["v"])
                except (ValueError, TypeError): pass

    def get_quote(self, exchange: str, token: str) -> dict:
        self._ensure_ws_started()
        if token not in self._subscribed_tokens:
            self._subscribed_tokens.add(token)
            try:
                self.subscribe([token], exchange)
            except Exception:
                pass
            try:
                res = self._api.get_quotes(exchange=exchange, token=token)
                if isinstance(res, dict) and 'lp' in res:
                    q_dict = self._live_quotes.setdefault(token, {"ltp": 0.0, "bid": 0.0, "ask": 0.0, "oi": 0.0, "volume": 0.0})
                    q_dict["ltp"] = float(res.get('lp', 0.0))
                    q_dict["bid"] = float(res.get('bp1', 0.0))
                    q_dict["ask"] = float(res.get('sp1', 0.0))
            except Exception:
                pass
        return self._live_quotes.get(token, {"ltp": 0.0, "bid": 0.0, "ask": 0.0, "oi": 0.0, "volume": 0.0})


class ShoonyaPortfolioProvider(IPortfolioProvider):
    def __init__(self, api: NorenApiPy):
        self._api = api
        self._limiter = TokenBucketLimiter(per_second=5, name="shoonya_portfolio")

    def get_positions(self, *args, **kwargs) -> list:
        return call_with_retry(lambda: self._api.get_positions() or [], limiter=self._limiter)

    def get_holdings(self, *args, **kwargs) -> list:
        return call_with_retry(lambda: self._api.get_holdings() or [], limiter=self._limiter)

    def get_limits(self, *args, **kwargs) -> dict:
        return call_with_retry(lambda: self._api.get_limits() or {}, limiter=self._limiter)

    def get_available_funds(self) -> float:
        limits = self.get_limits()
        if isinstance(limits, dict) and "cash" in limits:
            cash = float(limits.get("cash", 0.0))
            marginused = float(limits.get("marginused", 0.0))
            return max(0.0, cash - marginused)
        return 0.0


class ShoonyaExecutionProvider(IExecutionProvider):
    def __init__(self, api: NorenApiPy):
        self._api = api
        self._order_limiter = TokenBucketLimiter(per_second=2, name="shoonya_order")

    def place_order(self, **order):
        """
        FIXED: Safely translates generic kwargs into whatever specific parameter names 
        the installed version of NorenApiPy.place_order expects (supports both 'buy_or_sell' and 'trantype').
        """
        def _call():
            # Extract common variants passed in order dict
            action = order.get("buy_or_sell") or order.get("trantype") or order.get("transaction_type") or "B"
            prd = order.get("product_type") or order.get("prd") or "N"
            exch = order.get("exchange") or order.get("exch") or "NFO"
            tsym = order.get("tradingsymbol") or order.get("tsym") or order.get("symbol")
            qty = order.get("quantity") or order.get("qty")
            prctyp = order.get("price_type") or order.get("prctyp") or order.get("ordertype") or "LMT"
            prc = order.get("price") or order.get("prc")
            dscqty = order.get("discloseqty") or order.get("dscqty") or 0
            ret = order.get("retention") or order.get("ret") or "DAY"
            trgprc = order.get("trigger_price") or order.get("trgprc")

            # Try calling NorenApiPy with standard signature variants using try-except
            try:
                return self._api.place_order(
                    buy_or_sell=action,
                    product_type=prd,
                    exchange=exch,
                    tradingsymbol=tsym,
                    quantity=qty,
                    discloseqty=dscqty,
                    price_type=prctyp,
                    price=prc,
                    retention=ret
                )
            except TypeError:
                try:
                    # Fallback to positional or alternative keys if SDK signature differs
                    return self._api.place_order(
                        trantype=action,
                        prd=prd,
                        exch=exch,
                        tsym=tsym,
                        qty=qty,
                        dscqty=dscqty,
                        prctyp=prctyp,
                        prc=prc,
                        ret=ret,
                        trigger_price=trgprc
                    )
                except Exception as ex:
                    # Final fallback using generic dict unpacking if accepted
                    return self._api.place_order(**order)

        return call_with_retry(_call, limiter=self._order_limiter)

    def modify_order(self, **kwargs):
        return call_with_retry(lambda: self._api.modify_order(**kwargs), limiter=self._order_limiter)

    def cancel_order(self, orderno: str):
        return call_with_retry(lambda: self._api.cancel_order(orderno=orderno), limiter=self._order_limiter)

    def get_order_book(self):
        return call_with_retry(lambda: self._api.get_order_book() or [], limiter=self._order_limiter)


class ShoonyaHistoricalProvider(IHistoricalProvider):
    def __init__(self, api: NorenApiPy):
        self._api = api
        self._limiter = TokenBucketLimiter(per_second=3, name="shoonya_historical")

    def get_historical(self, exchange: str, token: str, interval: str, start_time, end_time) -> "pd.DataFrame":
        import pandas as pd
        start_ts = int(start_time.timestamp())
        end_ts = int(end_time.timestamp())
        interval_map = {
            "ONE_MINUTE": 1, "THREE_MINUTE": 3, "FIVE_MINUTE": 5, "TEN_MINUTE": 10,
            "FIFTEEN_MINUTE": 15, "THIRTY_MINUTE": 30, "ONE_HOUR": 60, "ONE_DAY": 1440
        }
        shoonya_interval = interval_map.get(interval, 1)
        
        try:
            if shoonya_interval < 1440:
                resp = call_with_retry(
                    lambda: self._api.get_time_price_series(exchange=exchange, token=token, starttime=start_ts, endtime=end_ts, interval=shoonya_interval),
                    limiter=self._limiter
                )
            else:
                resp = call_with_retry(
                    lambda: self._api.get_daily_price_series(exchange=exchange, tradingsymbol=token, startdate=start_time.strftime('%Y-%m-%d'), enddate=end_time.strftime('%Y-%m-%d')),
                    limiter=self._limiter
                )
            
            if isinstance(resp, list) and len(resp) > 0:
                df = pd.DataFrame(resp)
                if 'time' in df.columns: df['timestamp'] = df['time']
                elif 'ssboe' in df.columns: df['timestamp'] = df['ssboe']
                if 'into' in df.columns: df['open'] = df['into']
                if 'inth' in df.columns: df['high'] = df['inth']
                if 'intl' in df.columns: df['low'] = df['intl']
                if 'intc' in df.columns: df['close'] = df['intc']
                if 'intv' in df.columns: df['volume'] = df['intv']
                elif 'v' in df.columns: df['volume'] = df['v']
                
                required_cols = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
                for col in required_cols:
                    if col not in df.columns: df[col] = 0
                if df.columns.duplicated().any():
                    df = df.loc[:, ~df.columns.duplicated()]
                df = df[required_cols]
                
                ts_col = df['timestamp']
                if isinstance(ts_col, pd.DataFrame): ts_col = ts_col.iloc[:, 0]
                if ts_col.dtype == 'O':
                    df['timestamp'] = pd.to_datetime(ts_col, format='mixed', dayfirst=True)
                else:
                    df['timestamp'] = pd.to_datetime(ts_col, unit='s')
                    
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
                return df.sort_values('timestamp').reset_index(drop=True)
        except Exception as e:
            logger.error(f"Historical data error: {e}")
        return pd.DataFrame()


class ShoonyaAdapter(IBrokerGateway):
    def __init__(self) -> None:
        import requests
        original_post, original_get = requests.post, requests.get
        requests.post = lambda *a, **kw: original_post(*a, **{**kw, 'timeout': kw.get('timeout', 15)})
        requests.get = lambda *a, **kw: original_get(*a, **{**kw, 'timeout': kw.get('timeout', 15)})

        self._api = NorenApiPy(host='https://api.shoonya.com/NorenWClientAPI/', websocket='wss://api.shoonya.com/NorenWSAPI/')
        self._session_provider = ShoonyaSessionProvider(self._api)
        self._market_data_provider = ShoonyaMarketDataProvider(self._api)
        self._execution_provider = ShoonyaExecutionProvider(self._api)
        self._portfolio_provider = ShoonyaPortfolioProvider(self._api)
        self._historical_provider = ShoonyaHistoricalProvider(self._api)
        self.order_lifecycle = ShoonyaOrderLifecycle(self._execution_provider)

        self._manifest = BrokerManifest(
            identity=IdentityCapability(broker_name="Shoonya", broker_version="1.0", vendor="Shoonya"),
            authentication=AuthenticationCapability(mechanism="oauth", token_endpoint="/QuickAuth", token_refresh_supported=True),
            rate_limits=RateLimitCapability(rest_requests_per_sec=5.0, order_requests_per_sec=2.0, historical_requests_per_sec=1.0),
            websocket=WebSocketCapability(max_subscriptions=200, tick_format="json", depth_supported=False, heartbeat_interval_seconds=30, auto_reconnect=True),
            market_data=MarketDataCapability(live_quotes=True, ohlc=True, open_interest=False, market_depth=False, greeks=False, streaming_supported=True),
            historical=HistoricalDataCapability(max_lookback_days=1000, max_candles_per_request=1000, supports_adjusted_close=False, supports_tick_history=True),
            order_management=OrderManagementCapability(modify_order=True, cancel_order=True, basket_orders=True, bracket_orders=False, cover_orders=True, gtt_orders=False, supported_order_variants=()),
            product_support=ProductSupportCapability(equities=True, futures=True, options=True, commodities=True, currencies=True, derivatives=True),
            exchange_support=ExchangeSupportCapability(exchanges=("NSE", "NFO", "BSE", "MCX"), primary_exchange="NSE"),
            feature_flags=FeatureFlagCapability(paper_trading=False, replay_mode=False, simulation_mode=False, sandbox=False),
            performance=PerformanceRecommendation(recommended_batch_size=50, recommended_retry_delay_ms=500, recommended_throttling_interval_ms=200, session_timeout_seconds=86400)
        )

    @property
    def session_provider(self) -> ISessionProvider: return self._session_provider
    @property
    def market_data_provider(self) -> IMarketDataProvider: return self._market_data_provider
    @property
    def execution_provider(self) -> IExecutionProvider: return self._execution_provider
    @property
    def historical_provider(self) -> IHistoricalProvider: return self._historical_provider
    @property
    def portfolio_provider(self) -> IPortfolioProvider: return self._portfolio_provider
    @property
    def manifest(self) -> BrokerManifest: return self._manifest
    
    def get_order_lifecycle(self) -> OrderLifecycle: return self.order_lifecycle
    def register_option_tokens(self, ce: list, pe: list): self._market_data_provider.register_option_tokens(ce, pe)
    def register_option_token_metadata(self, df): self._market_data_provider.register_option_token_metadata(df)
    def compile_5min_option_net_delta_oi(self): return self._market_data_provider.get_and_reset_option_net_delta_oi()
    def get_strike_net_delta_oi(self, strike: float): return self._market_data_provider.get_strike_net_delta_oi(strike)
    def get_available_funds(self) -> float: return float(self._portfolio_provider.get_available_funds())