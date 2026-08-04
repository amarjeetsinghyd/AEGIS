#!/bin/bash
set -e
cd /home/opc
echo "=== EXTRACT ==="
tar -xf /tmp/deploy_bundle.tar -C /home/opc
touch /home/opc/astra_bot/__init__.py /home/opc/kronos_bot/__init__.py /home/opc/shoonya_data_hub/__init__.py /home/opc/unified_dashboard/__init__.py || true
rm -f astra_bot/dashboard.py astra_bot/dashboard_disabled.py kronos_bot/dashboard.py kronos_bot/dashboard_disabled.py || true
rm -f astra_bot/microstructure_pre_refactor.py astra_bot/microstructure_merge.py || true
rm -f kronos_bot/microstructure_pre_refactor.py kronos_bot/microstructure_merge.py || true
ls -la common | head -20
ls -la unified_dashboard
echo "=== IMPORT CHECK ==="
PYTHONPATH=/home/opc python3 <<'PY'
import sys, traceback, inspect
sys.path.insert(0, '/home/opc')
from common.ipc import update_astra_live_state, update_kronos_live_state, SHARED_TICK_FILE
from common.broker.shoonya_adapter import ShoonyaAdapter
from common.execution.execution_manager import ExecutionManager
from kronos_bot.strategy.signal_generator import KronosSignalGenerator
src = inspect.getsource(KronosSignalGenerator._load_predictor)
assert 'transformers' not in src
assert 'Kronos.from_pretrained' in src
from unified_dashboard.dashboard import app
print('VPS IMPORTS OK', SHARED_TICK_FILE, app.name)
update_astra_live_state({'signal_status': 'BOOT'})
update_kronos_live_state({'signal': 'NEUTRAL'})
PY
echo "=== PM2 ==="
pm2 delete all || true
pm2 start /home/opc/ecosystem.config.js
sleep 5
pm2 list
echo "=== DASHBOARD ==="
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:5000/ || true
curl -s http://127.0.0.1:5000/api/health || true
echo
echo "=== SHM ==="
ls -la /dev/shm
echo "=== LOGS TAIL ==="
pm2 logs astra-bot --lines 15 --nostream || true
pm2 logs kronos-bot --lines 15 --nostream || true
pm2 logs shoonya-data-hub --lines 15 --nostream || true
pm2 logs unified-dashboard --lines 10 --nostream || true
