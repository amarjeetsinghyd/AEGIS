import re

p = "C:/Users/AMARJE~1/AppData/Local/Temp/opencode/aegis-fullday.log"
lines = open(p, encoding="utf-8", errors="ignore").readlines()
print("total lines:", len(lines))

# focus window 14:20-15:00 on Sep 22
win = [l for l in lines if "2026-09-22 14:2" in l or "2026-09-22 14:3" in l
       or "2026-09-22 14:4" in l or "2026-09-22 14:5" in l]
print("window lines:", len(win))

print("=== ALL VETO/ENERGY lines in window ===")
for l in win:
    if "VETO" in l or "DROP ENERGY" in l or "SIGNAL EMITTED" in l or "Entry FILLED" in l:
        print(l.strip()[:400])
