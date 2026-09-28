import re
from collections import Counter
c = Counter()
with open('/home/opc/AEGIS/logs/aegis.log', 'r', encoding='utf-8', errors='ignore') as f:
    for line in f:
        if line.startswith('2026-09-28 ') and 'VETO' in line:
            m = re.search(r'\[([^\]]*VETO[^\]]*)\]', line)
            if m:
                c[m.group(1)] += 1
print("=== ALL VETOES ON 2026-09-28 ===")
for k, v in c.most_common():
    print(f"{v:6d} : {k}")
