import json
import os
import time
import subprocess

context_file = "/home/opc/astra_bot/data/trades/astra_active_trade_context.json"

while True:
    is_open = False
    if os.path.exists(context_file):
        try:
            with open(context_file, "r") as f:
                data = json.load(f)
            if data.get("status") == "OPEN":
                is_open = True
        except Exception:
            pass
            
    if not is_open:
        print("Trade is closed or no active trade. Stopping astra-bot...")
        subprocess.run(["pm2", "stop", "astra-bot"])
        print("Bot stopped.")
        break
        
    time.sleep(5)
