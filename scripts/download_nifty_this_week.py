import os
import sys
import json
import time
import datetime
import pandas as pd
from NorenRestApiPy.NorenApi import NorenApi

def main():
    session_file = "/home/opc/data/shoonya_session.json"
    if not os.path.exists(session_file):
        print(f"Error: session file {session_file} does not exist.")
        sys.exit(1)

    with open(session_file, "r") as f:
        session = json.load(f)

    api = NorenApi(host="https://api.shoonya.com/NorenWSTP/", websocket="wss://api.shoonya.com/NorenWSTP/")
    api.set_session(session["userid"], "", session["susertoken"], session["susertoken"])

    end_ts = int(time.time())
    start_ts = end_ts - (6 * 86400)  # last 6 days covers Monday 12 Oct to today 16 Oct

    print(f"Fetching NIFTY 1m candles from {start_ts} to {end_ts}...")
    res = api.get_time_price_series(exchange="NSE", token="26000", starttime=start_ts, endtime=end_ts, interval=1)
    if not isinstance(res, list) or not res:
        res = api.get_time_price_series(exchange="NSE", token="26000", starttime=start_ts, endtime=end_ts, interval="1")

    if not isinstance(res, list) or not res:
        print(f"Error fetching candles: {res}")
        sys.exit(1)

    print(f"Successfully retrieved {len(res)} candles from Shoonya!")

    records = []
    for r in res:
        # Shoonya returns: time (e.g. '16-10-2026 15:29:00'), into, inth, intl, intc, intv, intvwap, ssboe
        t_str = r.get("time", "")
        records.append({
            "time_str": t_str,
            "open": float(r.get("into", r.get("open", 0.0))),
            "high": float(r.get("inth", r.get("high", 0.0))),
            "low": float(r.get("intl", r.get("low", 0.0))),
            "close": float(r.get("intc", r.get("close", 0.0))),
            "volume": float(r.get("intv", r.get("volume", r.get("v", 0.0)))),
            "vwap": float(r.get("intvwap", 0.0)),
            "ssboe": r.get("ssboe", ""),
        })

    df = pd.DataFrame(records)
    # Reverse to chronological order (oldest first)
    df = df.iloc[::-1].reset_index(drop=True)

    # Filter for Monday (12 Oct) and Tuesday (13 Oct)
    # Check date formats in time_str (e.g. '12-10-2026' or '2026-10-12')
    df['date'] = df['time_str'].apply(lambda s: s.split(' ')[0] if ' ' in str(s) else '')

    print(f"\nUnique dates in retrieved candles:")
    print(df['date'].value_counts())

    # Filter exactly for 12 Oct and 13 Oct
    df_12_13 = df[df['date'].isin(['12-10-2026', '13-10-2026', '2026-10-12', '2026-10-13'])].copy().reset_index(drop=True)
    print(f"\nTotal candles for Monday (12 Oct) & Tuesday (13 Oct): {len(df_12_13)}")

    # Cleanliness checks:
    print("\n--- DATA CLEANLINESS REPORT ---")
    zero_close = (df_12_13['close'] <= 0).sum()
    zero_open = (df_12_13['open'] <= 0).sum()
    nan_count = df_12_13[['open', 'high', 'low', 'close']].isna().sum().to_dict()
    print(f"Zero closes: {zero_close}")
    print(f"Zero opens: {zero_open}")
    print(f"NaN values: {nan_count}")
    
    out_csv = "/home/opc/nifty_12_13_oct_1m_clean.csv"
    out_parquet = "/home/opc/nifty_12_13_oct_1m_clean.parquet"
    df_12_13.to_csv(out_csv, index=False)
    df_12_13.to_parquet(out_parquet, index=False)
    print(f"Cleaned dataset saved to {out_csv} and {out_parquet}")

if __name__ == "__main__":
    main()
