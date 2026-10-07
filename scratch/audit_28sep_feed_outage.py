import re

log_path = '/home/opc/.pm2/logs/aegis-bot-out.log'

target_windows = [
    '2026-09-28 10:56', '2026-09-28 10:57', '2026-09-28 10:58',
    '2026-09-28 12:23', '2026-09-28 12:24', '2026-09-28 12:25',
    '2026-09-28 14:13', '2026-09-28 14:14', '2026-09-28 14:15',
    '2026-09-28 14:29', '2026-09-28 14:30', '2026-09-28 14:31',
    '2026-09-28 15:05', '2026-09-28 15:06', '2026-09-28 15:07',
]

with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
    for line in f:
        if any(w in line for w in target_windows):
            if any(k in line for k in ['VETO', 'DROP', 'ORDER', 'TRIGGER', 'trade', 'blocked', 'signal', 'ENTRY', 'DEPTH', 'PA:', 'EXECUTION', 'INVALID', 'zero']):
                print(line.strip()[:180])
