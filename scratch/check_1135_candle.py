import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/home/opc")
from common.broker import ShoonyaAdapter
from AEGIS.execution.data_fetcher import DataFetcher

IST = timezone(timedelta(hours=5, minutes=30))

def check_1135():
    broker = ShoonyaAdapter()
    fetcher = DataFetcher(broker)
    df = fetcher.get_historical_candles("BSE", "1", "ONE_MINUTE", days_back=1)
    if df is not None and not df.empty:
        print("Columns in df:", df.columns.tolist())
        # Filter for 11:30 to 11:40
        print("\n--- RECENT 1-MINUTE SENSEX CANDLES AROUND 11:35 ---")
        for _, row in df.tail(25).iterrows():
            t = str(row.get("time") or row.get("timestamp") or row.get("ssboe") or "")
            o = row.get("open") or row.get("into")
            h = row.get("high") or row.get("inth")
            l = row.get("low") or row.get("intl")
            c = row.get("close") or row.get("intc")
            print(f"Time: {t} | Open: {o} | High: {h} | Low: {l} | Close: {c}")
    else:
        print("df is empty or None")

if __name__ == "__main__":
    check_1135()
