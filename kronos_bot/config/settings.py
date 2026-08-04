import os
from dotenv import load_dotenv
from common.config import configure_runtime
from common.runtime.paths import get_project_root

BASE_DIR = str(get_project_root())
load_dotenv(os.path.join(BASE_DIR, ".env"))
load_dotenv(os.path.join(BASE_DIR, "kronos_bot", ".env"), override=True)

RUNTIME_PATHS = configure_runtime("kronos")
DATA_DIR = RUNTIME_PATHS["data_dir"]
LOGS_DIR = RUNTIME_PATHS["logs_dir"]

from common.config import (  # noqa: E402
    NIFTY_PREMIUM_MIN,
    NIFTY_PREMIUM_MAX,
    SENSEX_PREMIUM_MIN,
    SENSEX_PREMIUM_MAX,
    ENTRY_LIMIT_BUFFER_PCT,
    ENTRY_LIMIT_MIN_BUFFER_PTS,
    OPTION_TICK_SIZE,
    INITIAL_PAPER_CAPITAL,
    ENABLE_LIVE_BROKERAGE_EXECUTION,
    OI_REVERSAL_EXIT_ENABLED,
    OI_REVERSAL_EXIT_THRESHOLD,
    OI_REVERSAL_MIN_HOLD_SEC,
    EXIT_LIMIT_GRACE_PERIOD_SEC,
    DEFAULT_PRODUCT_TYPE,
    NIFTY_LOT_SIZE,
    SENSEX_LOT_SIZE,
    BROKER,
)

ACTIVE_BROKER = BROKER
STRIKE_DB_PATH = os.path.join(DATA_DIR, "strike_research.db")
UNDERLYING_PARQUET_FILE = os.path.join(DATA_DIR, "underlying_candles.parquet")

TIME_FRAME = "5m"
PREDICTION_THRESHOLD = 0.0010  # 0.10% — Kronos entry magnitude gate (matches main signal path)
COOLDOWN_MINUTES = 15
UNLIMITED_DAILY_TRADES = True

KRONOS_MODEL_KEY = "NeoQuasar/Kronos-base"
KRONOS_TOKENIZER_KEY = "NeoQuasar/Kronos-Tokenizer-base"
KRONOS_CONTEXT_LENGTH = 512
