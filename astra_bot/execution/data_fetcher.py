import os
import json
import time
import requests
import pandas as pd
import collections
from datetime import datetime, timedelta

from astra_bot.config.settings import BROKER
ACTIVE_BROKER = (os.getenv("ACTIVE_BROKER") or BROKER).upper()

from astra_bot.core.logger import get_logger
logger = get_logger("data_fetcher")



# All 50 Nifty 50 Constituents (Source: NSE MW-NIFTY-50-22-Jun-2026.csv)
# Replicates TradingView's volume aggregation for the Cash Index exactly
NIFTY_CONSTITUENTS = {
    "ADANIENT": "25",
    "ADANIPORTS": "15083",
    "APOLLOHOSP": "157",
    "ASIANPAINT": "236",
    "AXISBANK": "5900",
    "BAJAJ-AUTO": "16669",
    "BAJAJFINSV": "16675",
    "BAJFINANCE": "317",
    "BEL": "383",
    "BHARTIARTL": "10604",
    "CIPLA": "694",
    "COALINDIA": "20374",
    "DRREDDY": "881",
    "EICHERMOT": "910",
    "GRASIM": "1232",
    "HCLTECH": "7229",
    "HDFCBANK": "1333",
    "HDFCLIFE": "467",
    "HINDALCO": "1363",
    "HINDUNILVR": "1394",
    "ICICIBANK": "4963",
    "INDIGO": "11195",
    "INFY": "1594",
    "ITC": "1660",
    "JIOFIN": "18143",
    "JSWSTEEL": "11723",
    "KOTAKBANK": "1922",
    "LT": "11483",
    "M&M": "2031",
    "MARUTI": "10999",
    "MAXHEALTH": "22377",
    "NESTLEIND": "17963",
    "NTPC": "11630",
    "ONGC": "2475",
    "POWERGRID": "14977",
    "RELIANCE": "2885",
    "SBILIFE": "21808",
    "SBIN": "3045",
    "SHRIRAMFIN": "4306",
    "SUNPHARMA": "3351",
    "TATACONSUM": "3432",
    "TATASTEEL": "3499",
    "TCS": "11536",
    "TECHM": "13538",
    "TITAN": "3506",
    "TRENT": "1964",
    "ULTRACEMCO": "11532",
    "WIPRO": "3787",
    "INDUSINDBK": "5258",
    "HEROMOTOCO": "1348",
}

# Sensex 30 Constituents (Source: BSE 22-Jun-2026)
# Used on Wednesday/Thursday for synthetic volume
SENSEX_CONSTITUENTS = {
    "ADANIPORTS": "532921",
    "ASIANPAINT": "500820",
    "AXISBANK": "532215",
    "BAJAJFINSV": "532978",
    "BAJFINANCE": "500034",
    "BEL": "500049",
    "BHARTIARTL": "532454",
    "ETERNAL": "543320",
    "HCLTECH": "532281",
    "HDFCBANK": "500180",
    "HINDUNILVR": "500696",
    "ICICIBANK": "532174",
    "INDIGO": "539448",
    "INFY": "500209",
    "ITC": "500875",
    "KOTAKBANK": "500247",
    "LT": "500510",
    "M&M": "500520",
    "MARUTI": "532500",
    "NTPC": "532555",
    "POWERGRID": "532898",
    "RELIANCE": "500325",
    "SBIN": "500112",
    "SUNPHARMA": "524715",
    "TATASTEEL": "500470",
    "TCS": "532540",
    "TECHM": "532755",
    "TITAN": "500114",
    "TRENT": "500251",
    "ULTRACEMCO": "532538",
}

# Cash Index tokens
NIFTY_CASH_TOKEN = "26000"
SENSEX_CASH_TOKEN = "1"

class DataFetcher:
    def __init__(self, broker_gateway):
        self.broker = broker_gateway
        from astra_bot.core.instrument_repository import InstrumentRepository
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
        # Check ring buffer first for WS-driven spot price
        if hasattr(self.broker, "market_data_provider") and hasattr(self.broker.market_data_provider, "footprint_compiler"):
            spot = getattr(self.broker.market_data_provider.footprint_compiler, "current_spot", 0.0)
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
        res = self.repo.get_futures_token(ACTIVE_BROKER, name, exch_seg)
        if not res:
            logger.error(f"Could not find Futures for {name} on {exch_seg}.")
            return None, None, None
        return res[0], res[1], exch_seg

    def get_cash_index_token(self):
        name, exch_seg = self.get_active_instrument()
        if name == "NIFTY":
            token = "26000"
            return token, "NIFTY", "NSE"
        else:
            token = "1" if ACTIVE_BROKER == "SHOONYA" else "99919000"
            return token, "SENSEX", "BSE"

    def get_active_constituents(self):
        """Returns the correct constituent list for the active instrument."""
        name, _ = self.get_active_instrument()
        
        # Try to load dynamic constituents first
        json_path = os.path.join(os.path.dirname(__file__), '../../institutional_memory/constituents.json')
        if os.path.exists(json_path):
            try:
                with open(json_path, 'r') as f:
                    data = json.load(f)
                    
                # Basic validation
                if name == "NIFTY" and "nifty_50" in data and len(data["nifty_50"]) >= 45:
                    return data["nifty_50"]
                elif name == "SENSEX" and "sensex_30" in data and len(data["sensex_30"]) >= 25:
                    return data["sensex_30"]
            except Exception as e:
                logger.error(f"Failed to load dynamic constituents, falling back to hardcoded: {e}")
                
        # Fallback to hardcoded dictionaries
        if name == "NIFTY":
            return NIFTY_CONSTITUENTS
        else:
            return SENSEX_CONSTITUENTS

    def _fetch_constituent_volume(self, days_back=5, minutes_back=None, exact_fromdate=None):
        """
        Fetches 1-min historical data for all constituents of the active index
        and returns a DataFrame with the summed volume.
        """
        constituents = self.get_active_constituents()
        todate = datetime.now()
        if exact_fromdate is not None:
            fromdate = exact_fromdate
        elif minutes_back is not None:
            fromdate = todate - timedelta(minutes=minutes_back)
        else:
            fromdate = todate - timedelta(days=days_back)
        
        name, _ = self.get_active_instrument()
        exchange = "NSE" if name == "NIFTY" else "BSE"
        all_volumes = None
        fetched_count = 0
        
        for stock_name, token in constituents.items():
            try:
                response_df = self.broker.historical_provider.get_historical(
                    exchange=exchange,
                    token=token,
                    interval="ONE_MINUTE",
                    start_time=fromdate,
                    end_time=todate
                )
                
                if not response_df.empty:
                    stock_df = response_df.set_index('timestamp')[['volume']].rename(columns={'volume': stock_name})
                    
                    if all_volumes is None:
                        all_volumes = stock_df
                    else:
                        all_volumes = all_volumes.join(stock_df, how='outer')
                    
                    fetched_count += 1
                
                # Sleep to prevent spamming the provider adapter
                time.sleep(0.4)
                
            except Exception as e:
                logger.warning(f"  Warning: Could not fetch volume for {stock_name}: {e}")
                time.sleep(0.4)
                continue
        
        if all_volumes is not None:
            all_volumes = all_volumes.fillna(0)
            all_volumes['synthetic_volume'] = all_volumes.sum(axis=1)
            logger.info(f"  Synthetic Volume Engine: {fetched_count}/{len(constituents)} constituents loaded.")
            return all_volumes[['synthetic_volume']]
        
        return None

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

    def get_historical_candles_with_synthetic_volume(self, days_back=5):
        """
        Fetches the Cash Index price data (Nifty 50 or Sensex 30) and injects
        synthetic volume from its constituents. This replicates TradingView's
        volume aggregation approach for the Cash Index.
        """
        token, name, exchange = self.get_cash_index_token()
        logger.info(f"Fetching {name} Cash Index price data from {exchange}...")
        price_df = self.get_historical_candles(exchange, token, "ONE_MINUTE", days_back=days_back)
        
        if price_df.empty:
            logger.error(f"ERROR: Could not fetch {name} Cash Index price data.")
            return pd.DataFrame()
        
        # --- PRECISION SCOUTING LOGIC ---
        price_df['date'] = price_df['timestamp'].dt.date
        unique_dates = price_df['date'].unique()
        if len(unique_dates) > 2:
            target_dates = unique_dates[-2:]
            price_df = price_df[price_df['date'].isin(target_dates)]
            
        min_timestamp = price_df['timestamp'].min()
        price_df = price_df.drop(columns=['date'])
        
        logger.info(f"{name} Cash Index: {len(price_df)} candles loaded. Volume is all zeros (expected).")
        constituents = self.get_active_constituents()
        logger.info(f"Building Synthetic Volume from {len(constituents)} Constituents starting exactly at {min_timestamp}...")
        
        vol_df = self._fetch_constituent_volume(exact_fromdate=min_timestamp)
        
        if vol_df is not None:
            # Merge synthetic volume into the price DataFrame
            price_df = price_df.set_index('timestamp')
            price_df = price_df.join(vol_df, how='left')
            price_df['volume'] = price_df['synthetic_volume'].fillna(0).astype(int)
            price_df = price_df.drop(columns=['synthetic_volume'])
            price_df = price_df.reset_index()
            logger.info(f"Synthetic Volume injected! Avg volume per candle: {price_df['volume'].mean():.0f}")
        else:
            logger.warning("WARNING: Could not build synthetic volume. VFI/RVOL will be unreliable.")
        
        return price_df
        
    def get_weekly_option_tokens(self) -> pd.DataFrame:
        name, exch_seg = self.get_active_instrument()
        rows = self.repo.get_weekly_option_tokens(ACTIVE_BROKER, name, exch_seg)
        df = pd.DataFrame(rows) if rows else pd.DataFrame()
        
        # If DB lookup returns empty or incomplete option chain, fetch dynamic chain via searchscrip
        if df.empty or len(df) < 5:
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
