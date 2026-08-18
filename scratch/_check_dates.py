#!/usr/bin/env python3
import sys
sys.path.insert(0, "/home/opc")
from common.analytics_persistence import list_available_dates
print("VORTEX dates:", list_available_dates("vortex"))
print("AEGIS dates:", list_available_dates("aegis"))