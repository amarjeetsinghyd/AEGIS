from AEGIS.core.instrument_repository import InstrumentRepository
repo = InstrumentRepository()
res = repo.get_weekly_option_tokens('SHOONYA', 'NIFTY', 'NFO')
print(f"repo count: {len(res)}")
for r in res[:10]:
    print(r['symbol'], r['strike'], r['token'])
strikes = sorted(set(r['strike'] for r in res))
print(f"Strike range: min={min(strikes)}, max={max(strikes)}, total strikes={len(strikes)}")
