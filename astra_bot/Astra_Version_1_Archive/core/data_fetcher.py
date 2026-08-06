import os
import sys
import json
import logging
import pandas as pd
from datetime import datetime, timedelta
from typing import Tuple, List, Dict, Any

from common.logger import get_logger
from astra_bot.config.settings import DATA_DIR, ACTIVE_BROKER, UNDERLYING_PARQUET_FILE
from common.ipc import SHARED_TICK_FILE

logger = get_logger("data_fetcher")

class DataFetcher:
    def __init__(self, broker_gateway, instrument_repo=None):
        self.broker = broker_gateway
        self.repo = instrument_repo
        self.parquet_path = UNDERLYING_PARQUET_FILE

    def get_active_instrument(self) -> Tuple[str, str]:
        today_str = datetime.now().strftime("%A")
        if today_str in ["Wednesday", "Thursday"]:
            return "SENSEX", "BFO"
        else:
            return "NIFTY", "NFO"

    def get_cash_index_token(self, target_index: str = None) -> Tuple[str, str, str]:
        name = target_index if target_index else self.get_active_instrument()[0]
        if name == "SENSEX":
            return "1", "SENSEX", "BSE"
        else:
            return "26000", "NIFTY", "NSE"

    def get_index_ltp(self, target_index: str = None) -> float:
        token, _, exch = self.get_cash_index_token(target_index)
        if os.path.exists(SHARED_TICK_FILE):
            try:
                with open(SHARED_TICK_FILE, "r") as f:
                    data = json.load(f)
                quotes = data.get("quotes", {})
                q = quotes.get(token, {})
                if isinstance(q, dict):
                    ltp = float(q.get("lp") or q.get("ltp") or q.get("close") or 0.0)
                    if ltp > 0:
                        return ltp
            except Exception:
                pass
        
        if hasattr(self.broker, "market_data_provider"):
            try:
                quote = self.broker.market_data_provider.get_quote(exch, token)
                if isinstance(quote, dict) and float(quote.get("ltp", 0)) > 0:
                    return float(quote["ltp"])
            except Exception:
                pass
        return 0.0

    def get_weekly_option_tokens(self, target_index: str = None) -> pd.DataFrame:
        name = target_index if target_index else self.get_active_instrument()[0]
        exch_seg = "BFO" if name == "SENSEX" else "NFO"
        df = pd.DataFrame()
        
        api = None
        if hasattr(self.broker, "session_provider") and hasattr(self.broker.session_provider, "_api"):
            api = self.broker.session_provider._api

        if api:
            try:
                spot = self.get_index_ltp(name)
                if spot <= 0:
                    spot = 77600.0 if name == "SENSEX" else 24200.0
                step = 100 if name == "SENSEX" else 50
                atm_strike = round(spot / step) * step
                
                real_opts = []
                for offset in range(-30, 31):
                    target_stk = int(atm_strike + (offset * step))
                    res = api.searchscrip(exchange=exch_seg, searchtext=f"{name} {target_stk}")
                    if isinstance(res, dict) and "values" in res:
                        for v in res["values"]:
                            tsym = str(v.get("tsym", ""))
                            token = str(v.get("token", ""))
                            optt = str(v.get("optt", "")).upper()
                            if token.isdigit() and (optt in ["CE", "PE"] or "CE" in tsym or "PE" in tsym):
                                real_opts.append({
                                    "token": token,
                                    "symbol": tsym,
                                    "strike": float(target_stk),
                                    "name": name,
                                    "exch_seg": exch_seg,
                                    "exd": str(v.get("exd", "")),
                                    "ls": v.get("ls")
                                })
                if real_opts:
                    df = pd.DataFrame(real_opts).drop_duplicates(subset=["token"])
                    if "exd" in df.columns:
                        df["exd_dt"] = pd.to_datetime(df["exd"], format="%d-%b-%Y", errors="coerce")
                        from datetime import datetime
                        today = datetime.now().date()
                        df = df[df["exd_dt"].dt.date >= today]
                        df = df.sort_values("exd_dt", ascending=True)
                    logger.info(f"[DataFetcher] Fetched {len(df)} real active option tokens from Shoonya searchscrip (ATM: {atm_strike}).")
                    return df
            except Exception as e:
                logger.warning(f"Shoonya searchscrip error: {e}")

        if df.empty and self.repo:
            try:
                rows = self.repo.get_weekly_option_tokens(ACTIVE_BROKER, name, exch_seg)
                if rows:
                    df = pd.DataFrame(rows)
            except Exception as e:
                logger.warning(f"Repo weekly options query warning: {e}")

        return df

    def rest_backfill_today_candles(self, target_index: str = None) -> pd.DataFrame:
        name = target_index if target_index else self.get_active_instrument()[0]
        token, _, exch = self.get_cash_index_token()
        
        api = None
        if hasattr(self.broker, "session_provider") and hasattr(self.broker.session_provider, "_api"):
            api = self.broker.session_provider._api

        if api:
            try:
                start_dt = datetime.now() - timedelta(days=7)
                logger.info(f"[REST BACKFILL] Requesting 5-minute historical candles from Shoonya REST API for {name} ({exch}:{token}) since {start_dt.strftime('%Y-%m-%d')}...")
                res = api.get_time_price_series(exchange=exch, token=token, starttime=int(start_dt.timestamp()), interval="5")
                
                if isinstance(res, list) and len(res) > 0:
                    records = []
                    for c in reversed(res):  # Convert from newest-first to chronological
                        try:
                            ts = pd.to_datetime(c["time"], format="%d-%m-%Y %H:%M:%S")
                            op = float(c.get("into", 0))
                            hi = float(c.get("inth", 0))
                            lo = float(c.get("intl", 0))
                            cl = float(c.get("intc", 0))
                            vol = int(float(c.get("v") or c.get("intv") or 10000000))
                            if vol <= 0:
                                vol = 10000000
                            amt = cl * vol
                            if cl > 0:
                                records.append({
                                    "timestamp": ts,
                                    "open": op,
                                    "high": hi,
                                    "low": lo,
                                    "close": cl,
                                    "volume": vol,
                                    "amount": amt
                                })
                        except Exception:
                            continue

                    if records:
                        df_backfill = pd.DataFrame(records).drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
                        
                        # --- SEBI CAS FILTER: Ignore fake auction spikes after 15:10 ---
                        from datetime import time as dt_time
                        cutoff_time = dt_time(15, 10, 0)
                        df_backfill = df_backfill[df_backfill["timestamp"].dt.time <= cutoff_time].copy()
                        
                        if len(df_backfill) > 512:
                            df_backfill = df_backfill.iloc[-512:].reset_index(drop=True)
                            
                        os.makedirs(os.path.dirname(self.parquet_path), exist_ok=True)
                        df_backfill.to_parquet(self.parquet_path, index=False)
                        logger.info(f"[REST BACKFILL] Successfully backfilled {len(df_backfill)} 5-minute candles into {self.parquet_path}.")
                        return df_backfill
            except Exception as e:
                logger.error(f"[REST BACKFILL ERROR] Failed to fetch historical candles: {e}")

        return self.bootstrap_candle_window()

    def bootstrap_candle_window(self) -> pd.DataFrame:
        if os.path.exists(self.parquet_path):
            try:
                df = pd.read_parquet(self.parquet_path)
                if len(df) > 0:
                    logger.info(f"Successfully bootstrapped sliding window with {len(df)} candles from disk.")
                    return df
            except Exception as e:
                logger.error(f"Failed to read parquet: {e}")
        
        df = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "amount"])
        return df

    def get_window_df(self) -> pd.DataFrame:
        df = self.bootstrap_candle_window()
        if len(df) < 50:
            logger.info(f"Sliding window size ({len(df)}) is below minimum threshold (50). Triggering automatic REST API backfill...")
            df = self.rest_backfill_today_candles()
        return df

    def append_live_candle(self, candle_dict: dict):
        # --- SEBI CAS FILTER: Ignore fake auction spikes after 15:10 ---
        from datetime import time as dt_time
        try:
            c_ts = pd.to_datetime(candle_dict.get('timestamp'))
            if c_ts and hasattr(c_ts, 'time') and c_ts.time() > dt_time(15, 10, 0):
                logger.warning(f"Ignoring live candle at {c_ts.time()} (SEBI CAS spike filter)")
                return
        except Exception:
            pass
            
        df = self.bootstrap_candle_window()
        # Ensure timestamp column in existing df is datetime64
        if "timestamp" in df.columns and not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            
        new_row = pd.DataFrame([candle_dict])
        new_row["timestamp"] = pd.to_datetime(new_row["timestamp"])
        
        df = pd.concat([df, new_row], ignore_index=True)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        
        df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
        if len(df) > 512:
            df = df.iloc[-512:].reset_index(drop=True)
            
        os.makedirs(os.path.dirname(self.parquet_path), exist_ok=True)
        df.to_parquet(self.parquet_path, index=False)
        logger.info(f"[SLIDING WINDOW] Appended candle [{candle_dict['timestamp']}] to window (Total Size: {len(df)}/512 candles).")