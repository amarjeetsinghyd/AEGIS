#!/usr/bin/env python3
import sys
import json
import subprocess

result = subprocess.run([
    sys.executable, "/home/opc/stock_order_flow/research_analytics.py", 
    "RELIANCE", "--date", "2026-08-17"
], capture_output=True, text=True, cwd="/home/opc", timeout=120)

if result.returncode != 0:
    print(f"ERROR: {result.stderr}")
    sys.exit(1)

data = json.loads(result.stdout)
print("Keys:", list(data.keys()))
print("Meta:", data.get("meta", {}))
print("Volume Profile:", "volume_profile" in data)
print("Bid Depth:", "bid_depth" in data)
print("Ask Depth:", "ask_depth" in data)
print("Microstructure:", "microstructure" in data)
print("Auction Imbalance:", "auction_imbalance" in data)