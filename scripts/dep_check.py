import sys, traceback
sys.path.insert(0, '/home/opc')

steps = [
    ("talib", "import talib"),
    ("numpy", "import numpy as np"),
    ("ShoonyaAdapter", "from common.broker import ShoonyaAdapter"),
    ("configure_runtime", "from common.config import configure_runtime, get_data_dir; configure_runtime('stock_order_flow'); print('data_dir:', get_data_dir())"),
    ("historical_data_manager", "from stock_order_flow.historical_data_manager import fetch_and_seed_history"),
    ("universe_scanner import", "import stock_order_flow.universe_scanner"),
]

for name, code in steps:
    try:
        exec(code)
        print(f"OK: {name}")
    except Exception as e:
        print(f"FAIL: {name} => {e}")
        traceback.print_exc()
        break
