import glob, json
from collections import defaultdict

all_stats = defaultdict(lambda: {'total': 0, 'wins': 0, 'losses': 0})
files = sorted(glob.glob('scratch/aegis_boundary_shadow_trades_*.jsonl'))
for fpath in files:
    with open(fpath, 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            t = json.loads(line)
            b = t.get('boundary', 'UNKNOWN')
            all_stats[b]['total'] += 1
            if t.get('outcome') == 'WINNER':
                all_stats[b]['wins'] += 1
            elif t.get('outcome') == 'LOSER':
                all_stats[b]['losses'] += 1

print("=== ALL-TIME Cumulative Boundary Shadow Performance ===")
for b, s in all_stats.items():
    tot = s['total']
    wr = (s['wins'] / tot * 100.0) if tot else 0.0
    print(f"Boundary: {b:<25} | Total: {tot:>2} | Wins: {s['wins']:>2} | Losses: {s['losses']:>2} | WinRate: {wr:>5.1f}%")
