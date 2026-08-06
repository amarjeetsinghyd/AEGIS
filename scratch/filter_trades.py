import os
import subprocess

KEY_PATH = r"C:\Users\Amarjeet Singh\quant-bot\Oracle SSH keys\ssh-key-2026-07-11.key"
HOST = "opc@137.23.41.38"

check_code = """import json

f = "/home/opc/astra_bot/data/trades/trade_history.json"
try:
    with open(f, 'r') as file:
        d = json.load(file)
    
    initial_len = len(d)
    valid = [t for t in d if t.get('entry_time') != t.get('exit_time')]
    
    with open(f, 'w') as file:
        json.dump(valid, file, indent=2)
        
    print(f"Filtered {initial_len - len(valid)} fake trades from {f}")
except Exception as e:
    print(e)
"""

cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-i", KEY_PATH, HOST, f"python3 -c '{check_code}'"]
res = subprocess.run(cmd, capture_output=True, text=True)
print(res.stdout)
print(res.stderr)
