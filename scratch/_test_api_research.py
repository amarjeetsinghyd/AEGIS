import urllib.request, json
url = 'http://127.0.0.1:5000/api/research?symbol=BEL&start=2026-08-17&end=2026-08-17'
try:
    with urllib.request.urlopen(url, timeout=30) as r:
        d = json.loads(r.read())
    print('Keys:', list(d.keys()))
    for k in ['volume_profile', 'bid_depth', 'ask_depth', 'micro_dist', 'auction']:
        if k in d:
            print(f'  {k}: present={d[k] is not None}')
        else:
            print(f'  {k}: MISSING')
    if 'meta' in d:
        print('meta:', d['meta'])
    if 'error' in d:
        print('ERROR:', d['error'])
except Exception as e:
    print('Request failed:', e)
