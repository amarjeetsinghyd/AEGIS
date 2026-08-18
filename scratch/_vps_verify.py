#!/usr/bin/env python3
"""VPS verification + VORTEX analytics runner.
Deployed to VPS and executed remotely to avoid PowerShell/SSH quoting hell.
"""
import sys
import os
import json
from pathlib import Path

BASE = Path("/home/opc")
sys.path.insert(0, str(BASE))

print("=== UNIVERSE.JSON ===")
uni_path = BASE / "stock_order_flow" / "universe.json"
print(f"exists: {uni_path.exists()}")
if uni_path.exists():
    with open(uni_path) as f:
        uni = json.load(f)
    print(f"symbols: {list(uni.keys())[:10]} ... ({len(uni)} total)")

print("\n=== TOKENS CACHE ===")
tc_path = BASE / "stock_order_flow" / "data" / "tokens_cache.json"
print(f"exists: {tc_path.exists()}")

print("\n=== ANALYTICS PERSISTENCE CHECK ===")
try:
    from common.analytics_persistence import list_available_dates
    print(f"VORTEX dates: {list_available_dates('vortex')}")
    print(f"AEGIS dates: {list_available_dates('aegis')}")
except Exception as e:
    print(f"ERROR: {e}")

print("\n=== L2 RAW PARTITIONS (today) ===")
import datetime
today = datetime.datetime.now().strftime("%Y-%m-%d")
vortex_l2 = BASE / "stock_order_flow" / "data" / "l2_raw"
parts = sorted(vortex_l2.glob(f"l2_raw_{today}_part_*.parquet"))
print(f"VORTEX partitions for {today}: {len(parts)} files")
if parts:
    print(f"  first: {parts[0].name}")
    print(f"  last:  {parts[-1].name}")

aegis_l2 = BASE / "AEGIS" / "data" / "l2_raw"
aparts = sorted(aegis_l2.glob(f"aegis_l2_raw_{today}_part_*.parquet"))
print(f"AEGIS partitions for {today}: {len(aparts)} files")
