from AEGIS.execution.data_fetcher import DataFetcher
from common.broker.shoonya_adapter import ShoonyaAdapter
import contextlib, os

with contextlib.redirect_stdout(open(os.devnull, 'w')):
    broker = ShoonyaAdapter()
    fetcher = DataFetcher(broker)

df = fetcher.get_weekly_option_tokens()
print(f"fetcher.get_weekly_option_tokens() count: {len(df)}")
if not df.empty:
    print("Columns:", df.columns.tolist())
    print("Sample rows:")
    for _, r in df.head(10).iterrows():
        print(f"  token={r.get('token')} | symbol={r.get('symbol')} | strike={r.get('strike')} | expiry={r.get('expiry')}")
    print(f"Strikes: min={df['strike'].min()}, max={df['strike'].max()}, total unique={df['strike'].nunique()}")
