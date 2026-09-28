import re

log_path = "/home/opc/.pm2/logs/aegis-bot-out.log"

target_times = ["10:57", "12:43", "12:53", "13:05", "13:16", "13:40", "14:07", "14:14", "14:30", "14:46"]

print("=== FORENSIC VETO AUDIT FOR SPECIFIC TIMELINES TODAY ===")

current_target = None
found = {t: [] for t in target_times}

with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
    for line in f:
        if not line.startswith("2026-09-28"):
            continue
        for t in target_times:
            if f" {t}:" in line:
                if any(k in line for k in ["VETO", "QUALITY STACK", "CONVICTION", "OBSTACLE", "SLOT", "ML GATEKEEPER", "ENERGY"]):
                    found[t].append(line.strip())

for t in target_times:
    print(f"\n--- TIME {t} --- (Matches: {len(found[t])})")
    # Show unique messages
    unique = list(dict.fromkeys(found[t]))[:8]
    for u in unique:
        print(u)
