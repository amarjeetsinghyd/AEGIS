#!/bin/bash
set -u
export PYTHONPATH=/home/opc

echo "===== IMPORT TRACE ====="
python3 - <<'PY'
import traceback
try:
    from astra_bot.execution.order_lifecycle import PaperOrderLifecycle, ShoonyaOrderLifecycle
    print("order_lifecycle OK", ShoonyaOrderLifecycle)
except Exception:
    traceback.print_exc()

try:
    from astra_bot.execution.execution_manager import ExecutionManager
    print("execution_manager OK", ExecutionManager)
except Exception:
    traceback.print_exc()

try:
    from astra_bot.dashboard import update_kdo_live_state
    print("dashboard helper OK")
except Exception as e:
    print("dashboard import:", type(e).__name__, e)

try:
    from kronos_bot.dashboard import update_kdo_live_state
    print("kronos dashboard OK")
except Exception as e:
    print("kronos dashboard import:", type(e).__name__, e)

try:
    import transformers
    print("transformers OK", transformers.__version__)
except Exception as e:
    print("transformers:", type(e).__name__, e)

try:
    from kronos_bot.config.settings import UNDERLYING_PARQUET_FILE, DATA_DIR, ENABLE_LIVE_BROKERAGE_EXECUTION, BROKER
    print("kronos settings", DATA_DIR, UNDERLYING_PARQUET_FILE, ENABLE_LIVE_BROKERAGE_EXECUTION, BROKER)
except Exception:
    traceback.print_exc()
PY

echo "===== CURL LIVE STATE ====="
curl -s http://127.0.0.1:5000/api/live_state | head -c 800
echo
echo "===== PM2 RESTART COUNTS ====="
pm2 jlist | python3 - <<'PY'
import sys, json
data = json.load(sys.stdin)
for p in data:
    env = p.get("pm2_env", {})
    print(f"{p.get('name')}: status={env.get('status')} restarts={env.get('restart_time')} unstable={env.get('unstable_restarts')} uptime_ms={env.get('pm_uptime')} script={env.get('pm_exec_path')}")
PY
