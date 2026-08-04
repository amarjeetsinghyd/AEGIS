#!/bin/bash
pm2 restart unified-dashboard
sleep 2
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:5000/
curl -s http://127.0.0.1:5000/api/live_state > /tmp/live.json
python3 - <<'PY'
import json
d=json.load(open('/tmp/live.json'))
print('default', d.get('default_log_engine'))
print('kronos_lines', len(d.get('kronos_logs') or []))
print('astra_lines', len(d.get('astra_logs') or []))
print('has_combined', 'logs' in d)
PY
