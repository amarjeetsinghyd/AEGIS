import pandas as pd
import glob

# Check Trade 2 spot price trajectory
# In telemetry, entry spot was 74086.03
# Let's inspect spot ticks or 1m candle closes between 12:11 and 12:17

# Search in aegis.log or telemetry for spot around 12:12 - 12:16
with open('/home/opc/AEGIS/logs/aegis.log', 'r') as f:
    lines = [line.strip() for line in f if '12:11:' in line or '12:12:' in line or '12:13:' in line or '12:14:' in line]

print(f"Log lines around Trade 2: {len(lines)}")
for l in lines:
    if 'Spot' in l or 'EMA' in l or 'bar_close' in l or '1m' in l or 'candle' in l:
        print("  ", l[:120])
