#!/usr/bin/env python3
"""Test VORTEX research analytics with a valid symbol."""
import sys
sys.path.insert(0, "/home/opc")

from stock_order_flow.research_analytics import run_research

# Test with a symbol from the snapshot
result = run_research("BEL", start="2026-08-17", end="2026-08-17")
import json
print(json.dumps(result, indent=2, default=str))