"""
Audit today's specific timestamps in /home/opc/AEGIS/logs/aegis.log:
Timestamps:
- 09:59
- 10:37
- 11:37
- 12:23
- 13:22
- 14:11 to 14:13
- 14:31
"""

import sys

target_times = [
    '09:58', '09:59', '10:00',
    '10:36', '10:37', '10:38',
    '11:36', '11:37', '11:38',
    '12:22', '12:23', '12:24',
    '13:21', '13:22', '13:23',
    '14:10', '14:11', '14:12', '14:13', '14:14',
    '14:30', '14:31', '14:32'
]

log_path = '/home/opc/AEGIS/logs/aegis.log'

print(f"Auditing {log_path} for target timestamps...")

matched_lines = []
with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
    for line in f:
        if not line.startswith('2026-09-28 '):
            continue
        ts_prefix = line[11:16]
        if ts_prefix in target_times:
            # We want to see:
            # 1. Heartbeat line: [HH:MM] | Spot: ... | EMA:20@... | PA: ...
            # 2. Any VETO line (WYCKOFF, PAIN ZONE, PIERCE, ML, etc.)
            # 3. Any 1M OPTION FLOW line
            # 4. Any CONFIRMED / TRIGGER / FIRED / REJECTED line
            if any(k in line for k in [
                '[PAIN ZONE', '[WYCKOFF', 'VETO', '20_ema', 'rejection',
                'wyckoff', 'pierced', 'slope', 'did_not_test', 'PA:',
                '[1M OPTION FLOW]', '[CATBOOST', 'DROPPED', 'WARMUP'
            ]):
                matched_lines.append(line.strip())

print(f"Total matched lines: {len(matched_lines)}")

# Group by time window
current_window = None
for l in matched_lines:
    t_min = l[11:16]
    # Simplify output
    print(l)
