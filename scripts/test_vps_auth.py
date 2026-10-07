import os
import sys

sys.path.insert(0, "/home/opc")
from common.broker.shoonya_adapter import ShoonyaAdapter

print("Testing ShoonyaAdapter connection with updated credentials...")
adapter = ShoonyaAdapter()
res = adapter.connect()
print(f"Connection result: {res}")
if adapter.susertoken:
    print(f"SUCCESS! Token acquired: {adapter.susertoken[:12]}...")
else:
    print("FAILED! No susertoken acquired.")
