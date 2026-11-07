import os
import time
import pandas as pd
from datetime import datetime, timedelta, date

from common.logger import get_logger
from AEGIS.config.settings import BROKER

ACTIVE_BROKER = (os.getenv("ACTIVE_BROKER") or BROKER).upper()
logger = get_logger("data_fetcher")


def is_call_symbol(sym: str) -> bool:
    """True for CALL symbols across Shoonya formats: CE/CALL words, single-letter
    C suffix (NIFTY29SEP26C22700) or trailing-C stems. Unknown defaults to PUT."""
    s = str(sym).upper().strip()
    if 'CE' in s or 'CALL' in s:
        return True
    if 'PE' in s or 'PUT' in s:
        return False
    stripped = s.rstrip('0123456789')
    return stripped.endswith('C')


def split_option_sides(df):
    """Split an option-universe frame into (ce_df, pe_df) robust to single-letter
    (C/P) and CE/PE symbol formats. Never raises on empty/missing input."""
    if df is None or len(df) == 0 or 'symbol' not in getattr(df, 'columns', []):
        empty = df.iloc[0:0] if df is not None and hasattr(df, 'iloc') else df
        return empty, empty
    mask = df['symbol'].astype(str).apply(is_call_symbol)
    return df[mask].copy(), df[~mask].copy()


def universe_has_both_sides(df) -> bool:
    """True only when the universe carries at least one CE and one PE row."""
    try:
        ce_df, pe_df = split_option_sides(df)
        return len(ce_df) > 0 and len(pe_df) > 0
    except Exception:
        return False


class DataFetcher:
    def __init__(self, broker_gateway):
        self.broker = broker_gateway
        from common.market.instrument_repository import InstrumentRepository
        self.repo = InstrumentRepository()
        self._cached_weekly_opts = None
        self._last_chain_fetch_ts = 0.0
        self._last_atm_cached = 0.0

    def get_index_ltp(self) -> float:
        # WS-driven spot: prefer footprint manager (live provider attribute),
        # fall back to legacy singular name for backward compatibility.
        mdp = getattr(self.broker, "market_data_provider", None)
        if mdp is not None:
            for attr in ("footprint_compiler_manager", "footprint_compiler"):
                fc = getattr(mdp, attr, None)
                if fc is not None:
                    spot = float(getattr(fc, "current_spot", 0.0) or 0.0)
                    if spot > 0:
                        return spot
                
        token, name, exchange = self.get_cash_index_token()
        quote = self.broker.market_data_provider.get_quote(exchange, token)
        return quote.get("ltp", 0.0)

    def get_active_instrument(self):
        """
        Returns the Name and Exchange Segment based on the day of the week.
        Wed (2), Thu (3) -> SENSEX (BFO)
        Mon (0), Tue (1), Fri (4) -> NIFTY (NFO)
        """
        day = datetime.now().weekday()
        if day in [2, 3]:
            return "SENSEX", "BFO"
        else:
            return "NIFTY", "NFO"



    def get_cash_index_token(self):
        name, exch_seg = self.get_active_instrument()
        if name == "NIFTY":
            token = "26000"
            return token, "NIFTY", "NSE"
        else:
            token = "1" if ACTIVE_BROKER == "SHOONYA" else "99919000"
            return token, "SENSEX", "BSE"

    def get_historical_candles(self, exchange, token, interval, days_back=5, minutes_back=None):
        todate = datetime.now()
        if minutes_back is not None:
            fromdate = todate - timedelta(minutes=minutes_back)
        else:
            fromdate = todate - timedelta(days=days_back)
        
        try:
            df = self.broker.historical_provider.get_historical(
                exchange=exchange,
                token=token,
                interval=interval,
                start_time=fromdate,
                end_time=todate
            )
            return df
        except Exception as e:
            logger.error(f"Error fetching candles for token {token}: {e}")
        return pd.DataFrame()

    def get_previous_day_5m_candles(self, exchange: str = None, token: str = None, prev_date: date = None) -> pd.DataFrame:
        """
        Fetches the complete 5-minute historical candle series (~75 candles) for the given
        underlying index from the previous official trading session (09:15:00 to 15:30:00 IST).
        Checks disk cache first to minimize redundant REST calls on bot restarts.
        """
        from common.market.market_calendar import get_previous_trading_day
        from datetime import time as dt_time

        if prev_date is None:
            prev_date = get_previous_trading_day(datetime.now().date())
        
        if token is None or exchange is None:
            default_token, name, default_exch = self.get_cash_index_token()
            token = token or default_token
            exchange = exchange or default_exch
        else:
            name = "NIFTY" if token == "26000" else ("SENSEX" if token in ("1", "99919000") else str(token))

        cache_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "scratch")
        cache_file = os.path.join(cache_dir, f"previous_day_5m_{name}_{prev_date.strftime('%Y%m%d')}.parquet")
        
        if os.path.exists(cache_file):
            try:
                df_cached = pd.read_parquet(cache_file)
                if not df_cached.empty and len(df_cached) >= 30:
                    logger.info(f"[DataFetcher] Loaded {len(df_cached)} previous-day 5m candles for {name} ({prev_date}) from cache: {cache_file}")
                    return df_cached
            except Exception as e_cache:
                logger.debug(f"[DataFetcher] Cache read failed ({e_cache}), falling back to broker API.")

        start_time = datetime.combine(prev_date, dt_time(9, 15, 0))
        end_time = datetime.combine(prev_date, dt_time(15, 30, 0))
        
        # 1. Try via historical_provider
        try:
            hp = getattr(self.broker, "historical_provider", None)
            if hp is not None and hasattr(hp, "get_historical"):
                df = hp.get_historical(
                    exchange=exchange,
                    token=str(token),
                    interval="FIVE_MINUTE",
                    start_time=start_time,
                    end_time=end_time
                )
                if df is not None and not df.empty:
                    os.makedirs(cache_dir, exist_ok=True)
                    try:
                        df.to_parquet(cache_file, index=False)
                    except Exception:
                        pass
                    logger.info(f"[DataFetcher] Fetched {len(df)} 5m candles for {name} ({prev_date}) via historical provider.")
                    return df
        except Exception as e_hp:
            logger.debug(f"[DataFetcher] Historical provider fetch failed for {name}: {e_hp}")

        # 2. Try direct API get_time_price_series
        try:
            api = getattr(self.broker, "_api", None) or self.broker
            if hasattr(api, "get_time_price_series"):
                resp = api.get_time_price_series(
                    exchange=exchange,
                    token=str(token),
                    starttime=int(start_time.timestamp()),
                    endtime=int(end_time.timestamp()),
                    interval=5
                )
                if isinstance(resp, list) and len(resp) > 0:
                    df = pd.DataFrame(resp)
                    if 'time' in df.columns:
                        df['timestamp'] = df['time']
                    elif 'ssboe' in df.columns:
                        df['timestamp'] = df['ssboe']
                    if 'into' in df.columns:
                        df['open'] = df['into']
                    if 'inth' in df.columns:
                        df['high'] = df['inth']
                    if 'intl' in df.columns:
                        df['low'] = df['intl']
                    if 'intc' in df.columns:
                        df['close'] = df['intc']
                    if 'intv' in df.columns:
                        df['volume'] = df['intv']
                    elif 'v' in df.columns:
                        df['volume'] = df['v']
                    
                    for col in ['open', 'high', 'low', 'close', 'volume']:
                        if col in df.columns:
                            df[col] = pd.to_numeric(df[col], errors='coerce')
                    
                    os.makedirs(cache_dir, exist_ok=True)
                    try:
                        df.to_parquet(cache_file, index=False)
                    except Exception:
                        pass
                    logger.info(f"[DataFetcher] Fetched {len(df)} 5m candles for {name} ({prev_date}) via direct API.")
                    return df
        except Exception as e_api:
            logger.debug(f"[DataFetcher] Direct API fetch failed for {name}: {e_api}")

        return pd.DataFrame()

    def get_weekly_option_tokens(self) -> pd.DataFrame:
        name, exch_seg = self.get_active_instrument()
        rows = self.repo.get_weekly_option_tokens(ACTIVE_BROKER, name, exch_seg)
        df = pd.DataFrame(rows) if rows else pd.DataFrame()
        
        # If DB lookup returns empty or incomplete option chain, fetch dynamic chain via searchscrip.
        # Retry once when the chain comes back single-sided (partial searchscrip
        # failure): adopting a CE-only or PE-only universe blinds half the flow.
        for _attempt in (1, 2):
            if not (df.empty or len(df) < 5 or not universe_has_both_sides(df)):
                break
            if _attempt == 2 and not df.empty:
                logger.warning("[DataFetcher] dynamic chain still single-sided after retry — keeping last good universe upstream")
                break
            try:
                index_price = self.get_index_ltp()
                
                if index_price > 0:
                    step = 100 if name == "SENSEX" else 50
                    atm_strike = round(index_price / step) * step

                    # Short-term cache check: return cached chain if ATM is unchanged and within 45s
                    now_ts = time.time()
                    if (
                        self._cached_weekly_opts is not None
                        and not self._cached_weekly_opts.empty
                        and (now_ts - self._last_chain_fetch_ts < 45.0)
                        and abs(atm_strike - self._last_atm_cached) < step
                    ):
                        return self._cached_weekly_opts

                    # Expand to 15 strikes ITM and OTM (total 31 strikes) to support deeper affordable OTM trading
                    depth = int(os.getenv("AEGIS_OPTION_CHAIN_DEPTH", "25"))
                    strike_list = [atm_strike + (i * step) for i in range(-depth, depth + 1)]
                    
                    import re
                    dynamic_opts = []
                    for s_val in strike_list:
                        search_term = f"{name} {int(s_val)}"
                        res = self.broker._api.searchscrip(exchange=exch_seg, searchtext=search_term)
                        if isinstance(res, dict) and res.get('stat') == 'Ok':
                            for item in res.get('values', []):
                                if item.get('instname') == 'OPTIDX' and (item.get('symname') == name or item.get('symname') == 'BSXOPT'):
                                    tsym = item.get('tsym', '')
                                    exd_str = item.get('exd', '')
                                    try:
                                        expiry_dt = datetime.strptime(exd_str, '%d-%b-%Y')
                                    except ValueError:
                                        try:
                                            expiry_dt = datetime.strptime(exd_str, '%d%b%Y')
                                        except ValueError:
                                            continue
                                            
                                    m = re.search(r'[CP](\d+)$', tsym)
                                    strike = float(m.group(1)) if m else float(s_val)
                                    
                                    dynamic_opts.append({
                                        'token': str(item['token']),
                                        'symbol': tsym,
                                        'expiry': exd_str,
                                        'expiry_dt': expiry_dt,
                                        'strike': strike
                                    })
                    
                    if dynamic_opts:
                        dyn_df = pd.DataFrame(dynamic_opts)
                        now = datetime.now()
                        valid_dts = [dt for dt in dyn_df['expiry_dt'] if dt.date() >= now.date()]
                        if valid_dts:
                            nearest_exp = min(valid_dts)
                            df = dyn_df[dyn_df['expiry_dt'] == nearest_exp].drop_duplicates(subset=['token']).reset_index(drop=True)
                            self._cached_weekly_opts = df
                            self._last_chain_fetch_ts = now_ts
                            self._last_atm_cached = atm_strike
            except Exception as e:
                logger.error(f"Error fetching dynamic option chain via searchscrip: {e}")
                
        return df
