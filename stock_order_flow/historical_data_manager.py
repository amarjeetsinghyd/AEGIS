from __future__ import annotations

import os
from datetime import datetime, timedelta

import pandas as pd

from common.config import get_data_dir
from common.logger import getlogger

logger = getlogger("stockhistory")

HISTORY_DIR = os.path.join(get_data_dir(), "history")
os.makedirs(HISTORY_DIR, exist_ok=True)


def _parse_rows(rows):
    recs = []
    for r in rows or []:
        try:
            recs.append(
                {
                    "timestamp": pd.to_datetime(r.get("time") or r.get("ts") or r.get("timestamp")),
                    "open": float(r.get("into") or r.get("open") or 0),
                    "high": float(r.get("inth") or r.get("high") or 0),
                    "low": float(r.get("intl") or r.get("low") or 0),
                    "close": float(r.get("intc") or r.get("close") or 0),
                    "volume": float(r.get("intv") or r.get("v") or 0),
                }
            )
        except Exception:
            continue
    recs.sort(key=lambda x: x["timestamp"])
    return recs


def fetch_and_seed_history(broker, tokens: list, token_map: dict) -> dict:
    out = {}
    for token in tokens:
        try:
            sym = token_map.get(str(token), token_map.get(token, str(token)))
            path = os.path.join(HISTORY_DIR, f"{sym}.parquet")
            local = None
            if os.path.exists(path):
                try:
                    local = pd.read_parquet(path)
                except Exception:
                    local = None

            last_local_ts = None
            if local is not None and len(local):
                local["timestamp"] = pd.to_datetime(local["timestamp"])
                last_local_ts = local["timestamp"].max()

            start = int((last_local_ts + timedelta(seconds=1)).timestamp()) if last_local_ts is not None else int((datetime.now() - timedelta(days=6)).timestamp())
            end = int(datetime.now().timestamp())
            rows = broker.api.get_time_price_series(exchange="NSE", token=str(token), starttime=start, endtime=end, interval="3")
            recs = _parse_rows(rows)
            df_new = pd.DataFrame(recs)
            if local is not None and len(local):
                df = pd.concat([local, df_new], ignore_index=True)
            else:
                df = df_new
            if df.empty:
                continue
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df = df.sort_values("timestamp").drop_duplicates(subset=["timestamp"], keep="last")
            cutoff = datetime.now() - timedelta(days=6)
            df = df[df["timestamp"] >= cutoff].copy()
            df.to_parquet(path, index=False)
            out[str(token)] = df.reset_index(drop=True)
        except Exception as e:
            logger.debug(f"history fetch failed token={token}: {e}")
    return out
