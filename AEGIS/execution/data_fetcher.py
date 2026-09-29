import os
import json
import time
import requests
import pandas as pd
import collections
from datetime import datetime, timedelta

from AEGIS.config.settings import BROKER
ACTIVE_BROKER = (os.getenv("ACTIVE_BROKER") or BROKER).upper()

from common.logger import get_logger
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



# Cash Index tokens
NIFTY_CASH_TOKEN = "26000"
SENSEX_CASH_TOKEN = "1"

class DataFetcher:
    def __init__(self, broker_gateway):
        self.broker = broker_gateway
        from AEGIS.core.instrument_repository import InstrumentRepository
        self.repo = InstrumentRepository()
        self.candle_window = collections.deque(maxlen=512)

    def bootstrap_candle_window(self):
        """
        Fetches the last 7 trading days of Nifty 50 (or SENSEX) 5-minute candles
        via the broker's REST API at market opening to seed the sliding window.
        Attempts to load from disk cache first if available.
        """
        token, name, exchange = self.get_cash_index_token()
        candle_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "scratch", f"underlying_candles_{name}.parquet")
        
        # Try loading from disk cache first
        if os.path.exists(candle_file):
            try:
                df = pd.read_parquet(candle_file)
                if not df.empty and len(df) >= 200:
                    logger.info(f"Loading {name} candle sliding window from disk cache {candle_file}...")
                    df = df.tail(512)
                    self.candle_window.clear()
                    for _, row in df.iterrows():
                        self.candle_window.append(row.to_dict())
                    logger.info(f"Successfully bootstrapped sliding window with {len(self.candle_window)} candles from disk.")
                    return True
            except Exception as e:
                logger.warning(f"Failed to load candle cache from disk: {e}. Falling back to API bootstrap.")

        logger.info(f"Bootstrapping {name} candle sliding window (last 7 trading days) via Broker REST API...")
        df = self.get_historical_candles(exchange, token, "FIVE_MINUTE", days_back=12)
        if df.empty:
            logger.error(f"Failed to bootstrap candle window for {name}. REST API returned empty DataFrame.")
            return False
        df = df.tail(512)
        self.candle_window.clear()
        for _, row in df.iterrows():
            self.candle_window.append(row.to_dict())
        
        # Save to disk cache
        try:
            os.makedirs(os.path.dirname(candle_file), exist_ok=True)
            df.to_parquet(candle_file, index=False)
            logger.info(f"Saved {len(df)} bootstrapped candles to disk cache: {candle_file}")
        except Exception as e:
            logger.error(f"Failed to save candle cache to disk: {e}")
            
        logger.info(f"Successfully bootstrapped sliding window with {len(self.candle_window)} candles for {name}.")
        return True

    def append_live_candle(self, candle_dict: dict):
        self.candle_window.append(candle_dict)
        ts = candle_dict.get('timestamp', 'N/A')
        o = float(candle_dict.get('open', 0.0))
        h = float(candle_dict.get('high', 0.0))
        l = float(candle_dict.get('low', 0.0))
        c = float(candle_dict.get('close', 0.0))
        v = candle_dict.get('volume', 0)
        
        logger.info(f"[REST API CANDLE] Time: {ts} | Open: {o:.2f} | High: {h:.2f} | Low: {l:.2f} | Close: {c:.2f} | Vol: {v}")
        logger.info(f"[SLIDING WINDOW] Appended candle [{ts}] to window (Total Size: {len(self.candle_window)}/512 candles).")
        
        # Save updated deque to disk cache
        try:
            _, name, _ = self.get_cash_index_token()
            candle_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "scratch", f"underlying_candles_{name}.parquet")
            df_to_save = pd.DataFrame(list(self.candle_window))
            os.makedirs(os.path.dirname(candle_file), exist_ok=True)
            df_to_save.to_parquet(candle_file, index=False)
        except Exception as e:
            logger.error(f"Failed to save live candle cache to disk: {e}")

    def get_window_df(self) -> pd.DataFrame:
        df = pd.DataFrame(list(self.candle_window))
        if not df.empty:
            df['timestamp'] = pd.to_datetime(df['timestamp'])
        return df

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

    def get_current_futures_token(self):
        name, exch_seg = self.get_active_instrument()
        try:
            res = self.repo.get_futures_token(ACTIVE_BROKER, name, exch_seg)
            if res:
                return res[0], res[1], exch_seg
        except Exception as e:
            logger.warning(f"Error querying futures token from repo: {e}")

        # Calibrated fallback for current active monthly futures (NIFTY & SENSEX)
        if name == "NIFTY":
            return "68407", "NIFTY29SEP26F", "NFO"
        elif name == "SENSEX":
            return "844615", "SENSEX26SEPFUT", "BFO"

        logger.error(f"Could not find Futures for {name} on {exch_seg}.")
        return None, None, None

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
                    # Expand to 10 strikes ITM and OTM to support deeper OTM trading
                    strike_list = [atm_strike + (i * step) for i in range(-10, 11)]
                    
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
            except Exception as e:
                logger.error(f"Error fetching dynamic option chain via searchscrip: {e}")
                
        return df
