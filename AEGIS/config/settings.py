import os
from dotenv import load_dotenv
from common.config import configure_runtime
from common.runtime.paths import get_project_root

BASE_DIR = str(get_project_root())
load_dotenv(os.path.join(BASE_DIR, ".env"))

RUNTIME_PATHS = configure_runtime("aegis")
DATA_DIR = RUNTIME_PATHS["data_dir"]
LOGS_DIR = RUNTIME_PATHS["logs_dir"]

# Re-export shared trading constants
from common.config import (  # noqa: E402
    BROKER,
)

ACTIVE_BROKER = BROKER
STRIKE_DB_PATH = os.path.join(DATA_DIR, "strike_research.db")
UNDERLYING_PARQUET_FILE = os.path.join(DATA_DIR, "underlying_candles.parquet")

# AEGIS V3.1 — no Kronos/legacy constants here (see common/config for shared trading constants).
# TIME_FRAME / PREDICTION_THRESHOLD / KRONOS_* were Kronos residue and have been removed (2026-08-29 audit).
# AEGIS uses strike_step 50 (NIFTY) / 100 (SENSEX) resolved at runtime in aegis_main.py:108.
