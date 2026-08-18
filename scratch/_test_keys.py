#!/usr/bin/env python3
"""Test VORTEX research analytics - just check keys."""
import sys
sys.path.insert(0, "/home/opc")

from stock_order_flow.research_analytics import run_research

result = run_research("BEL", start="2026-08-17", end="2026-08-17")
print("Keys:", list(result.keys()))
for k, v in result.items():
    if isinstance(v, dict):
        print(f"  {k}: dict with {len(v)} keys")
    elif isinstance(v, list):
        print(f"  {k}: list of {len(v)} items")
    else:
        print(f"  {k}: {type(v).__name__}")