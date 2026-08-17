import sys, traceback, logging
logging.basicConfig(level=logging.DEBUG)
sys.argv = ['universe_scanner.py', '--now']
sys.path.insert(0, '/home/opc')

from common.broker import ShoonyaAdapter
from common.logger import get_logger
from common.config import configure_runtime, get_data_dir
import stock_order_flow.universe_scanner as us

configure_runtime("stock_order_flow")
logger = get_logger("scan_debug")

print("=== Creating broker ===")
broker = ShoonyaAdapter()
print("=== broker created ===")

sp = getattr(broker, "session_provider", None)
print(f"session_provider: {sp}")

if sp is not None and hasattr(sp, "_restore_or_login"):
    print("=== calling _restore_or_login ===")
    try:
        sp._restore_or_login()
        print("=== login OK ===")
    except Exception as e:
        print(f"LOGIN FAILED: {e}")
        traceback.print_exc()
        sys.exit(1)
else:
    print("no session_provider or no _restore_or_login")

print("=== running scan ===")
try:
    us.run_scan(broker)
    print("=== scan complete ===")
except Exception as e:
    print(f"SCAN FAILED: {e}")
    traceback.print_exc()
