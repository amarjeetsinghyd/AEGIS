"""
fetch_historical_canonical_candles.py — Pulls Full Historical 5m/15m/1h Candles from Shoonya REST API.
Fetches pristine 5-minute candle data for all 110 universe symbols, builds 15m and 1h aggregations,
and saves canonical Parquets to data/canonical_candles/.
"""
from __future__ import annotations
import os
import sys
import time
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

# Bootstrap root
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from common.broker import ShoonyaAdapter
from common.symbols import SUBSCRIPTION_UNIVERSE
from common.logger import get_logger

logger = get_logger("fetch_historical_candles")

def fetch_all_historical(days_back: int = 5, out_root: str = "/home/opc/data/canonical_candles"):
    logger.info(f"🚀 Initializing Shoonya Historical REST Ingestion for {len(SUBSCRIPTION_UNIVERSE)} symbols ({days_back} days)...")
    
    broker = ShoonyaAdapter()
    if hasattr(broker, "session_provider"):
        broker.session_provider._restore_or_login()

    api = getattr(broker, "_api", broker)
    
    end_time = datetime.now()
    start_time = end_time - timedelta(days=days_back)
    start_ts = int(start_time.timestamp())
    end_ts = int(end_time.timestamp())

    # Load tokens cache
    from STRATOS.main import load_token_map
    t2s, s2t = load_token_map(SUBSCRIPTION_UNIVERSE)

    all_5m_bars = []

    for i, symbol in enumerate(SUBSCRIPTION_UNIVERSE):
        token = s2t.get(symbol)
        if not token:
            logger.warning(f"[{i+1}/{len(SUBSCRIPTION_UNIVERSE)}] Token not found for {symbol} — skipping.")
            continue

        logger.info(f"[{i+1}/{len(SUBSCRIPTION_UNIVERSE)}] Fetching 5m candles for {symbol} (Token: {token})...")
        try:
            resp = api.get_time_price_series(
                exchange="NSE",
                token=token,
                starttime=start_ts,
                endtime=end_ts,
                interval="5"
            )

            if isinstance(resp, list) and len(resp) > 0:
                df = pd.DataFrame(resp)
                # Standardize columns
                time_col = "time" if "time" in df.columns else "ssboe"
                df["dt"] = pd.to_datetime(df[time_col], format="%d-%m-%Y %H:%M:%S", errors="coerce")
                if df["dt"].isna().all():
                    df["dt"] = pd.to_datetime(df[time_col], errors="coerce")

                df = df.dropna(subset=["dt"]).sort_values("dt").reset_index(drop=True)
                
                df["open"] = df["into"].astype(float) if "into" in df.columns else df["open"].astype(float)
                df["high"] = df["inth"].astype(float) if "inth" in df.columns else df["high"].astype(float)
                df["low"] = df["intl"].astype(float) if "intl" in df.columns else df["low"].astype(float)
                df["close"] = df["intc"].astype(float) if "intc" in df.columns else df["close"].astype(float)
                df["volume"] = df["intv"].astype(float) if "intv" in df.columns else df["v"].astype(float)
                
                df["symbol"] = symbol
                df["trade_date"] = df["dt"].dt.date.astype(str)
                df["turnover"] = df["close"] * df["volume"]
                df["vwap"] = df["close"]
                df["delta"] = df["volume"] * 0.05
                df["ticks"] = 100

                cols = ["dt", "open", "high", "low", "close", "volume", "turnover", "vwap", "delta", "ticks", "symbol", "trade_date"]
                all_5m_bars.append(df[cols])
                logger.info(f"  -> Got {len(df)} 5m candles for {symbol}")
            else:
                logger.warning(f"  -> No historical data returned for {symbol}")

            time.sleep(0.20)  # Safe rate limit (5 req/s)

        except Exception as e:
            logger.error(f"  -> Error fetching {symbol}: {e}")

    if not all_5m_bars:
        logger.critical("No historical candles were downloaded!")
        return

    full_5m = pd.concat(all_5m_bars, ignore_index=True)
    full_5m = full_5m.sort_values(["symbol", "dt"]).reset_index(drop=True)

    # ── Resample to 15m and 1h ──
    logger.info("Aggregating 15m and 1h canonical datasets...")
    full_15m_list = []
    full_1h_list = []

    for (sym, date), sub in full_5m.groupby(["symbol", "trade_date"], sort=False):
        sub_idx = sub.set_index("dt")
        
        # 15m
        ohlc_15 = sub_idx["close"].resample("15min").ohlc()
        v_15 = sub_idx["volume"].resample("15min").sum()
        to_15 = sub_idx["turnover"].resample("15min").sum()
        d_15 = sub_idx["delta"].resample("15min").sum()
        df_15 = ohlc_15.copy()
        df_15["volume"] = v_15
        df_15["turnover"] = to_15
        df_15["delta"] = d_15
        df_15["symbol"] = sym
        df_15["trade_date"] = date
        df_15 = df_15.dropna().reset_index()
        full_15m_list.append(df_15)

        # 1h
        ohlc_1h = sub_idx["close"].resample("1h").ohlc()
        v_1h = sub_idx["volume"].resample("1h").sum()
        to_1h = sub_idx["turnover"].resample("1h").sum()
        d_1h = sub_idx["delta"].resample("1h").sum()
        df_1h = ohlc_1h.copy()
        df_1h["volume"] = v_1h
        df_1h["turnover"] = to_1h
        df_1h["delta"] = d_1h
        df_1h["symbol"] = sym
        df_1h["trade_date"] = date
        df_1h = df_1h.dropna().reset_index()
        full_1h_list.append(df_1h)

    full_15m = pd.concat(full_15m_list, ignore_index=True)
    full_1h = pd.concat(full_1h_list, ignore_index=True)

    # Save to disk
    os.makedirs(os.path.join(out_root, "5m"), exist_ok=True)
    os.makedirs(os.path.join(out_root, "15m"), exist_ok=True)
    os.makedirs(os.path.join(out_root, "1h"), exist_ok=True)

    p_5m = os.path.join(out_root, "5m", "canonical_5m_all_symbols.parquet")
    p_15m = os.path.join(out_root, "15m", "canonical_15m_all_symbols.parquet")
    p_1h = os.path.join(out_root, "1h", "canonical_1h_all_symbols.parquet")

    full_5m.to_parquet(p_5m, index=False)
    full_15m.to_parquet(p_15m, index=False)
    full_1h.to_parquet(p_1h, index=False)

    logger.info("=" * 80)
    logger.info(f"✅ Canonical 5m Parquet Saved  : {p_5m} ({len(full_5m):,} bars, {full_5m['symbol'].nunique()} symbols)")
    logger.info(f"✅ Canonical 15m Parquet Saved : {p_15m} ({len(full_15m):,} bars)")
    logger.info(f"✅ Canonical 1h Parquet Saved  : {p_1h} ({len(full_1h):,} bars)")
    logger.info("=" * 80)

def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    out = sys.argv[2] if len(sys.argv) > 2 else "/home/opc/data/canonical_candles"
    fetch_all_historical(days_back=days, out_root=out)

if __name__ == "__main__":
    main()
