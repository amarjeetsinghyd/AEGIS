"""AEGIS Canonical Configuration & Settings."""
from __future__ import annotations

import os
from dotenv import load_dotenv
from config.paths import get_project_root, get_runtime_paths

BASE_DIR = str(get_project_root())
load_dotenv(os.path.join(BASE_DIR, ".env"))

RUNTIME_PATHS = get_runtime_paths("aegis")
DATA_DIR = RUNTIME_PATHS["data_dir"]
LOGS_DIR = RUNTIME_PATHS["logs_dir"]

# Index lot sizes & parameters
NIFTY_LOT_SIZE = 65
SENSEX_LOT_SIZE = 20

NIFTY_PREMIUM_MIN = 15
NIFTY_PREMIUM_MAX = 120
SENSEX_PREMIUM_MIN = 20
SENSEX_PREMIUM_MAX = 200

ENTRY_LIMIT_BUFFER_PCT = 0.025
ENTRY_LIMIT_MIN_BUFFER_PTS = 3.0
OPTION_TICK_SIZE = 0.05

INITIAL_PAPER_CAPITAL = 2500.0
EXIT_LIMIT_GRACE_PERIOD_SEC = 5.0
DEFAULT_PRODUCT_TYPE = "N"

ENABLE_LIVE_BROKERAGE_EXECUTION = (
    os.getenv("AEGIS_LIVE_TRADING", "").upper() == "TRUE"
    or os.getenv("ENABLE_LIVE_BROKERAGE_EXECUTION", "False").upper() == "TRUE"
)
BROKER = os.getenv("BROKER", "SHOONYA").upper()
ACTIVE_BROKER = BROKER

STRIKE_DB_PATH = os.path.join(DATA_DIR, "strike_research.db")
UNDERLYING_PARQUET_FILE = os.path.join(DATA_DIR, "underlying_candles.parquet")


def configure_runtime(runtime_name: str = "aegis") -> dict:
    paths = get_runtime_paths(runtime_name)
    os.environ["BOT_RUNTIME"] = runtime_name
    os.environ["BOT_DATA_DIR"] = paths["data_dir"]
    os.environ["BOT_LOGS_DIR"] = paths["logs_dir"]
    return paths


def get_data_dir() -> str:
    return os.environ.get("BOT_DATA_DIR", DATA_DIR)


def get_logs_dir() -> str:
    return os.environ.get("BOT_LOGS_DIR", LOGS_DIR)
