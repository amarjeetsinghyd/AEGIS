import os
import subprocess

KEY_PATH = r"C:\Users\Amarjeet Singh\quant-bot\Oracle SSH keys\ssh-key-2026-07-11.key"
HOST = "opc@137.23.41.38"

check_code = """
import sys
sys.path.insert(0, "/home/opc")
from common.broker.shoonya_adapter import ShoonyaAdapter
a = ShoonyaAdapter()
a.session_provider._restore_or_login()

res_nifty = a._api.searchscrip("NFO", "NIFTY")
print("NIFTY:", res_nifty.get("values")[0].get("ls") if res_nifty and res_nifty.get("values") else "Not found")

res_sensex = a._api.searchscrip("BFO", "SENSEX")
print("SENSEX:", res_sensex.get("values")[0].get("ls") if res_sensex and res_sensex.get("values") else "Not found")

res_banknifty = a._api.searchscrip("NFO", "BANKNIFTY")
print("BANKNIFTY:", res_banknifty.get("values")[0].get("ls") if res_banknifty and res_banknifty.get("values") else "Not found")

res_finnifty = a._api.searchscrip("NFO", "FINNIFTY")
print("FINNIFTY:", res_finnifty.get("values")[0].get("ls") if res_finnifty and res_finnifty.get("values") else "Not found")

res_midcpnifty = a._api.searchscrip("NFO", "MIDCPNIFTY")
print("MIDCPNIFTY:", res_midcpnifty.get("values")[0].get("ls") if res_midcpnifty and res_midcpnifty.get("values") else "Not found")
"""

cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-i", KEY_PATH, HOST, f"python3 -c '{check_code}'"]
res = subprocess.run(cmd, capture_output=True, text=True)
print(res.stdout)
print(res.stderr)
