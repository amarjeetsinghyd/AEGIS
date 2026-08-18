import sys, json
sys.path.insert(0, '/home/opc/stock_order_flow')
from research_analytics import run_research

# Test range mode with today's date
r = run_research('RELIANCE', start='2026-08-17', end='2026-08-17')
print('Keys:', list(r.keys()))
expected = ['volume_profile', 'bid_depth', 'ask_depth', 'micro_dist', 'auction']
for k in expected:
    if k in r:
        print(f'  {k}: present={r[k] is not None}')
    else:
        print(f'  {k}: MISSING')
if 'error' in r:
    print('ERROR:', r['error'])
if 'meta' in r:
    print('meta:', r['meta'])
