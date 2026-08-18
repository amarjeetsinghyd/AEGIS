#!/usr/bin/env python3
"""Test VORTEX research analytics with range mode."""
import sys
sys.path.insert(0, "/home/opc")

from stock_order_flow.research_analytics import run_research

# Test with a symbol from the universe
result = run_research("RELIANCE", start="2026-08-17", end="2026-08-17")
print("Keys:", list(result.keys()))
print("Meta:", result.get("meta", {}))
print("Volume Profile:", "volume_profile" in result)
print("Bid Depth:", "bid_depth" in result)
print("Ask Depth:", "ask_depth" in result)
print("Microstructure:", "micro_dist" in result)
print("Auction:", "auction" in result)