#!/usr/bin/env python3
"""Run VORTEX daily aggregation for today."""
import sys
sys.path.insert(0, "/home/opc")

from common.analytics_persistence import run_daily_aggregation

result = run_daily_aggregation(target_date="2026-08-17", engines=["vortex"])
print(result)