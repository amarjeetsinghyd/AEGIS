import pandas as pd
from common.broker.shoonya_adapter import ShoonyaAdapter
from astra_bot.execution.data_fetcher import DataFetcher
broker = ShoonyaAdapter()
broker.session_provider._restore_or_login()
fetcher = DataFetcher(broker)
weekly_opts = fetcher.get_weekly_option_tokens()
pe_opts = weekly_opts[weekly_opts['symbol'].str.contains(r'PE|P\d+$')]
print('Options found:', len(pe_opts))
for i, row in pe_opts.head(20).iterrows():
    token = str(row['token'])
    q = broker.market_data_provider.get_quote('BFO', token)
    if isinstance(q, dict):
        print(f"{row.symbol} Strike={row.strike}: LTP={q.get('ltp', 0.0)}")
