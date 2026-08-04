"""
Kronos DataFetcher — index OHLC + constituent synthetic volume.

Live path:
  - OHLC: REST / live spot at 5m boundary
  - Volume: websocket constituent LTQ accumulator via data hub IPC
    (NIFTY 50 / SENSEX 30). Never use fake 10M filler for production candles.

Backfill / EOD path:
  - OHLC: index REST 5m series
  - Volume: REST 1m constituent volumes summed → resampled to 5m
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

import pandas as pd

from common.ipc import SHARED_TICK_FILE
from common.logger import get_logger
from kronos_bot.config.settings import ACTIVE_BROKER, DATA_DIR, UNDERLYING_PARQUET_FILE

logger = get_logger("data_fetcher")

_CONSTITUENTS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "institutional_memory", "constituents.json"
)


class DataFetcher:
    def __init__(self, broker_gateway, instrument_repo=None):
        self.broker = broker_gateway
        self.repo = instrument_repo
        self.parquet_path = UNDERLYING_PARQUET_FILE

    def get_active_instrument(self) -> Tuple[str, str]:
        today_str = datetime.now().strftime("%A")
        if today_str in ["Wednesday", "Thursday"]:
            return "SENSEX", "BFO"
        return "NIFTY", "NFO"

    def _parquet_path_for(self, target_index: str = None) -> str:
        name = (target_index or self.get_active_instrument()[0]).upper()
        return os.path.join(DATA_DIR, f"underlying_candles_{name}.parquet")

    def get_cash_index_token(self, target_index: str = None) -> Tuple[str, str, str]:
        name = target_index if target_index else self.get_active_instrument()[0]
        if name == "SENSEX":
            return "1", "SENSEX", "BSE"
        return "26000", "NIFTY", "NSE"

    def get_constituents(self, target_index: str = None) -> Dict[str, str]:
        """NIFTY→50 / SENSEX→30 token map from institutional_memory/constituents.json."""
        name = (target_index or self.get_active_instrument()[0]).upper()
        path = os.path.normpath(_CONSTITUENTS_PATH)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if name == "NIFTY" and len(data.get("nifty_50") or {}) >= 45:
                    return dict(data["nifty_50"])
                if name == "SENSEX" and len(data.get("sensex_30") or {}) >= 25:
                    return dict(data["sensex_30"])
            except Exception as e:
                logger.error(f"Failed loading constituents.json: {e}")
        logger.warning(f"No valid constituents for {name}")
        return {}

    def get_index_ltp(self, target_index: str = None) -> float:
        token, _, exch = self.get_cash_index_token(target_index)
        if os.path.exists(SHARED_TICK_FILE):
            try:
                with open(SHARED_TICK_FILE, "r", encoding="utf-8") as f:
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

    def get_hub_synthetic_volume(self, target_index: str = None) -> int:
        """Read closed 5m synthetic constituent volume published by the data hub."""
        name = (target_index or self.get_active_instrument()[0]).upper()
        key = "synthetic_nifty_volume" if name == "NIFTY" else "synthetic_sensex_volume"
        if not os.path.exists(SHARED_TICK_FILE):
            return 0
        try:
            with open(SHARED_TICK_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return int(data.get(key) or data.get("synthetic_constituent_volume") or 0)
        except Exception:
            return 0

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
                                real_opts.append(
                                    {
                                        "token": token,
                                        "symbol": tsym,
                                        "strike": float(target_stk),
                                        "name": name,
                                        "exch_seg": exch_seg,
                                        "exd": str(v.get("exd", "")),
                                        "ls": v.get("ls"),
                                    }
                                )
                if real_opts:
                    df = pd.DataFrame(real_opts).drop_duplicates(subset=["token"])
                    if "exd" in df.columns:
                        df["exd_dt"] = pd.to_datetime(df["exd"], format="%d-%b-%Y", errors="coerce")
                        today = datetime.now().date()
                        df = df[df["exd_dt"].dt.date >= today]
                        df = df.sort_values("exd_dt", ascending=True)
                    logger.info(
                        f"[DataFetcher] Fetched {len(df)} option tokens for {name} (ATM: {atm_strike})."
                    )
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

    def _fetch_constituent_volume_5m(
        self, target_index: str, fromdate: datetime, todate: datetime = None
    ) -> Optional[pd.Series]:
        """
        EOD / historical only: REST **5-minute** volumes for all constituents,
        sum across names → one synthetic_volume series (aligned to 5m bars).
        Do NOT use 1-minute here — Kronos window is 5m.
        """
        todate = todate or datetime.now()
        constituents = self.get_constituents(target_index)
        if not constituents:
            return None

        name = target_index.upper()
        exchange = "NSE" if name == "NIFTY" else "BSE"
        hist = getattr(self.broker, "historical_provider", None)
        if hist is None:
            logger.error("No historical_provider on broker — cannot build synthetic volume")
            return None

        # ~7 calendar-day chunks keep each request under Shoonya row caps (~75 bars/day)
        chunks: list[tuple[datetime, datetime]] = []
        cursor = fromdate.replace(hour=0, minute=0, second=0, microsecond=0)
        while cursor < todate:
            chunk_end = min(cursor + timedelta(days=7), todate)
            chunks.append((cursor, chunk_end))
            cursor = chunk_end

        all_volumes = None
        fetched = 0
        for stock_name, token in constituents.items():
            stock_parts = []
            try:
                for c_start, c_end in chunks:
                    response_df = hist.get_historical(
                        exchange=exchange,
                        token=str(token),
                        interval="FIVE_MINUTE",
                        start_time=c_start,
                        end_time=c_end,
                    )
                    if response_df is not None and not response_df.empty and "volume" in response_df.columns:
                        part = response_df.set_index("timestamp")[["volume"]].copy()
                        part["volume"] = part["volume"].clip(lower=0)
                        stock_parts.append(part)
                    time.sleep(0.2)
                if stock_parts:
                    stock_df = (
                        pd.concat(stock_parts)
                        .groupby(level=0)
                        .sum()
                        .rename(columns={"volume": stock_name})
                    )
                    all_volumes = stock_df if all_volumes is None else all_volumes.join(stock_df, how="outer")
                    fetched += 1
            except Exception as e:
                logger.warning(f"Constituent 5m volume miss {stock_name}/{token}: {e}")
                time.sleep(0.2)

        if all_volumes is None:
            return None
        all_volumes = all_volumes.fillna(0)
        synth = all_volumes.sum(axis=1).clip(lower=0)
        synth.index = pd.to_datetime(synth.index)
        logger.info(
            f"[SyntheticVol] {name}: {fetched}/{len(constituents)} constituents via REST 5m "
            f"(bars={len(synth)}, avg={float(synth.mean()):.0f})"
        )
        return synth

    def rest_backfill_today_candles(self, target_index: str = None) -> pd.DataFrame:
        """
        Index OHLC from REST 5m + synthetic constituent volume from REST 5m (sum of 50/30).
        amount = mean(OHLC) × synthetic_volume. No fake 10M filler. No 1m constituent pulls.
        """
        name = target_index if target_index else self.get_active_instrument()[0]
        token, _, exch = self.get_cash_index_token(name)
        out_path = self._parquet_path_for(name)

        api = None
        if hasattr(self.broker, "session_provider") and hasattr(self.broker.session_provider, "_api"):
            api = self.broker.session_provider._api

        if not api:
            return self.bootstrap_candle_window(name)

        try:
            start_dt = datetime.now() - timedelta(days=14)
            logger.info(
                f"[REST BACKFILL] {name} ({exch}:{token}) OHLC 5m since {start_dt.date()}..."
            )
            res = api.get_time_price_series(
                exchange=exch, token=token, starttime=int(start_dt.timestamp()), interval="5"
            )
            if not isinstance(res, list) or not res:
                logger.error(f"[REST BACKFILL] Empty OHLC for {name}")
                return self.bootstrap_candle_window(name)

            records = []
            for c in reversed(res):
                try:
                    ts = pd.to_datetime(c["time"], format="%d-%m-%Y %H:%M:%S")
                    op = float(c.get("into", 0))
                    hi = float(c.get("inth", 0))
                    lo = float(c.get("intl", 0))
                    cl = float(c.get("intc", 0))
                    if cl > 0:
                        records.append(
                            {
                                "timestamp": ts,
                                "open": op,
                                "high": hi,
                                "low": lo,
                                "close": cl,
                                "volume": 0,
                                "amount": 0.0,
                            }
                        )
                except Exception:
                    continue

            if not records:
                return self.bootstrap_candle_window(name)

            df = (
                pd.DataFrame(records)
                .drop_duplicates(subset=["timestamp"])
                .sort_values("timestamp")
                .reset_index(drop=True)
            )

            vol_from = df["timestamp"].min().to_pydatetime() - timedelta(minutes=5)
            vol_5m = self._fetch_constituent_volume_5m(name, vol_from, datetime.now())
            if vol_5m is not None and len(vol_5m) > 0:
                df = df.set_index("timestamp")
                df["volume"] = vol_5m.reindex(df.index).fillna(0).clip(lower=0).astype(int)
                df = df.reset_index()
                nonzero = int((df["volume"] > 0).sum())
                logger.info(
                    f"[REST BACKFILL] Synthetic 5m volume joined for {name}: "
                    f"{nonzero}/{len(df)} bars non-zero, avg={df['volume'].mean():.0f}"
                )
            else:
                logger.warning(
                    f"[REST BACKFILL] Could not build synthetic 5m volume for {name} — "
                    "leaving volume=0 (no 10M fake filler)."
                )

            mean_px = (df["open"] + df["high"] + df["low"] + df["close"]) / 4.0
            df["amount"] = mean_px * df["volume"].astype(float)

            if len(df) > 512:
                df = df.iloc[-512:].reset_index(drop=True)

            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            df.to_parquet(out_path, index=False)
            if name == self.get_active_instrument()[0]:
                df.to_parquet(self.parquet_path, index=False)
            logger.info(f"[REST BACKFILL] Wrote {len(df)} candles for {name} → {out_path}")
            return df
        except Exception as e:
            logger.error(f"[REST BACKFILL ERROR] {name}: {e}")
            return self.bootstrap_candle_window(name)

    def bootstrap_candle_window(self, target_index: str = None) -> pd.DataFrame:
        path = self._parquet_path_for(target_index)
        for candidate in (path, self.parquet_path):
            if os.path.exists(candidate):
                try:
                    df = pd.read_parquet(candidate)
                    if len(df) > 0:
                        logger.info(
                            f"Bootstrapped {len(df)} candles from {os.path.basename(candidate)}"
                        )
                        return df
                except Exception as e:
                    logger.error(f"Failed to read parquet {candidate}: {e}")
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "amount"])

    def get_window_df(self) -> pd.DataFrame:
        name = self.get_active_instrument()[0]
        df = self.bootstrap_candle_window(name)
        needs_backfill = len(df) < 512
        if not needs_backfill and "volume" in df.columns:
            # Re-backfill if window is still fake 10M filler
            vol = df["volume"].astype(float)
            if len(vol) and (vol == 10_000_000).mean() > 0.8:
                needs_backfill = True
                logger.warning(
                    f"[DataFetcher] Detected fake 10M volume filler on {name} — rebuilding with synthetic volume"
                )
        if needs_backfill:
            logger.info(f"Window size {len(df)} / filler check → backfill {name}")
            df = self.rest_backfill_today_candles(name)
        return df

    def append_live_candle(self, candle_dict: dict):
        name = self.get_active_instrument()[0]
        path = self._parquet_path_for(name)
        df = self.bootstrap_candle_window(name)
        if "timestamp" in df.columns and not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
            df["timestamp"] = pd.to_datetime(df["timestamp"])

        new_row = pd.DataFrame([candle_dict])
        new_row["timestamp"] = pd.to_datetime(new_row["timestamp"])
        if "amount" not in new_row.columns or pd.isna(new_row["amount"]).all():
            mean_px = new_row[["open", "high", "low", "close"]].astype(float).mean(axis=1)
            new_row["amount"] = mean_px * new_row["volume"].astype(float)

        df = pd.concat([df, new_row], ignore_index=True)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
        if len(df) > 512:
            df = df.iloc[-512:].reset_index(drop=True)

        os.makedirs(os.path.dirname(path), exist_ok=True)
        df.to_parquet(path, index=False)
        df.to_parquet(self.parquet_path, index=False)
        logger.info(
            f"[SLIDING WINDOW] {name} appended [{candle_dict['timestamp']}] "
            f"vol={int(candle_dict.get('volume') or 0)} size={len(df)}/512"
        )
