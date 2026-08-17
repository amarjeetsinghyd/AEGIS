import json
try:
    with open('/home/opc/stock_order_flow/data/logs/shadow_cascade_2026-08-13.jsonl') as f:
        lines = [json.loads(l) for l in f]

    lines.sort(key=lambda x: x.get('net_vacuum',0), reverse=True)
    print('Top 3 Bullish Vacuum (Asks Pulled - Sellers Panicking):')
    for x in lines[:3]: 
        ts = str(x.get('ts', ''))[11:19]
        print(f"  {ts} | {x.get('sym')} | Vac: {x.get('net_vacuum')} | LP: {x.get('lp')} | Whale: {x.get('whale_concentration')}")

    lines.sort(key=lambda x: x.get('net_vacuum',0))
    print('\nTop 3 Bearish Vacuum (Bids Pulled - Buyers Panicking):')
    for x in lines[:3]: 
        ts = str(x.get('ts', ''))[11:19]
        print(f"  {ts} | {x.get('sym')} | Vac: {x.get('net_vacuum')} | LP: {x.get('lp')} | Whale: {x.get('whale_concentration')}")
except Exception as e:
    print(e)
