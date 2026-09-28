"""
Structured audit for specific user timestamps:
- 09:59
- 10:37
- 11:37
- 12:23
- 13:22
- 14:11 to 14:13
- 14:31
"""

target_windows = [
    ("09:58", "10:01"),
    ("10:36", "10:39"),
    ("11:36", "11:39"),
    ("12:22", "12:25"),
    ("13:21", "13:24"),
    ("14:10", "14:15"),
    ("14:30", "14:33"),
]

log_path = '/home/opc/AEGIS/logs/aegis.log'

for w_start, w_end in target_windows:
    print(f"\n==================== WINDOW: {w_start} to {w_end} ====================")
    with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            if not line.startswith('2026-09-28 '):
                continue
            ts = line[11:16]
            if w_start <= ts <= w_end:
                # Filter for key events
                if any(k in line for k in [
                    '[WYCKOFF', 'VETO', 'PAIN ZONE', 'PIERCE', '20_ema', 
                    '[CATBOOST', 'PA:', '[1M OPTION FLOW]', 'DROPPED',
                    'wyckoff_dump', 'opposing'
                ]):
                    # simplify log line
                    print(line.strip())
