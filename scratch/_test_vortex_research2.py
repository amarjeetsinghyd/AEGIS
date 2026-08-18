#!/usr/bin/env python3
"""Test VORTEX research analytics with range mode - full output."""
import sys
sys.path.insert(0, "/home/opc")

from stock_order_flow.research_analytics import run_research

# Test with a symbol from the universe
result = run_research("RELIANCE", start="2026-08-17", end="2026-08-17")
import json
print(json.dumps(result, indent=2, default=str))