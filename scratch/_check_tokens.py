#!/usr/bin/env python3
import json
d = json.load(open('/home/opc/stock_order_flow/data/tokens_cache.json'))
print("type:", type(d))
print("keys count:", len(d) if isinstance(d, dict) else "not a dict")
if isinstance(d, dict):
    print("first 3:", list(d.items())[:3])