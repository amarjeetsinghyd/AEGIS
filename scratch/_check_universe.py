#!/usr/bin/env python3
import json
d = json.load(open('/home/opc/stock_order_flow/universe.json'))
print("type:", type(d))
print("keys:", list(d.keys()))
stocks = d.get('stocks', [])
print("stocks type:", type(stocks))
print("stocks count:", len(stocks))
print("first 3:", stocks[:3] if isinstance(stocks, list) else "not a list")