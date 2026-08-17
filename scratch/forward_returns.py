import json
from datetime import datetime, timedelta

log_path = '/home/opc/stock_order_flow/data/logs/shadow_cascade_2026-08-13.jsonl'
events = [
    # Bullish
    {"type": "Bullish", "time": "2026-08-13T13:49:28", "sym": "SBIN", "lp": 1079.7},
    {"type": "Bullish", "time": "2026-08-13T13:49:42", "sym": "SBIN", "lp": 1079.9},
    {"type": "Bullish", "time": "2026-08-13T09:21:52", "sym": "BOSCHLTD", "lp": 46990.0},
    # Bearish
    {"type": "Bearish", "time": "2026-08-13T10:29:06", "sym": "SBIN", "lp": 1080.4},
    {"type": "Bearish", "time": "2026-08-13T13:02:48", "sym": "MARUTI", "lp": 13905.0},
    {"type": "Bearish", "time": "2026-08-13T13:05:31", "sym": "MARUTI", "lp": 13900.0}
]

# Parse target times
for ev in events:
    t = datetime.strptime(ev["time"], "%Y-%m-%dT%H:%M:%S")
    ev["t_obj"] = t
    ev["t1"] = t + timedelta(minutes=1)
    ev["t5"] = t + timedelta(minutes=5)
    ev["lp1"] = None
    ev["lp5"] = None

# Scan file
try:
    with open(log_path, 'r') as f:
        for line in f:
            data = json.loads(line)
            ts_str = data.get("ts", "")[:19]
            sym = data.get("sym")
            lp = data.get("lp")
            if not ts_str or not sym or not lp:
                continue
            
            try:
                curr_t = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S")
            except:
                continue
            
            for ev in events:
                if ev["sym"] == sym:
                    # Look for closest time after t1
                    if ev["lp1"] is None and curr_t >= ev["t1"]:
                        ev["lp1"] = lp
                    # Look for closest time after t5
                    if ev["lp5"] is None and curr_t >= ev["t5"]:
                        ev["lp5"] = lp

    print("--- Forward Returns Analysis ---")
    for ev in events:
        p0 = ev["lp"]
        p1 = ev["lp1"] if ev["lp1"] else p0
        p5 = ev["lp5"] if ev["lp5"] else p0
        
        diff1 = p1 - p0
        diff5 = p5 - p0
        
        print(f"{ev['type']} | {ev['time'][11:]} | {ev['sym']} | LP: {p0}")
        print(f"  -> +1m: {p1} ({diff1:+.1f}) | +5m: {p5} ({diff5:+.1f})")

except Exception as e:
    print(f"Error: {e}")
