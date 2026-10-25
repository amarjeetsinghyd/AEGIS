"""Shared trading defaults + per-process runtime context (DATA_DIR, logs)."""

from __future__ import annotations

import os
from typing import Optional

# Lot sizes — SEBI-mandated as of 2026-07
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

OI_REVERSAL_EXIT_ENABLED = os.getenv("OI_REVERSAL_EXIT_ENABLED", "False").upper() == "TRUE"
OI_REVERSAL_EXIT_THRESHOLD = 50000.0
OI_REVERSAL_MIN_HOLD_SEC = 60.0

# LIVE switch: ONLY via explicit VPS env (AEGIS_LIVE_TRADING=TRUE).
# Default stays False (PAPER) so local runs, tests, and fresh clones can
# never place real orders by accident. Flip ONLY with human approval.
ENABLE_LIVE_BROKERAGE_EXECUTION = os.getenv("AEGIS_LIVE_TRADING", "False").upper() == "TRUE"
BROKER = os.getenv("BROKER", "SHOONYA").upper()

_runtime_name: Optional[str] = None
_data_dir: Optional[str] = None
_logs_dir: Optional[str] = None


def configure_runtime(runtime_name: str) -> dict:
    """Bind process-level paths for the active engine (astra / kronos / hub)."""
    global _runtime_name, _data_dir, _logs_dir
    from common.runtime.paths import get_runtime_paths

    paths = get_runtime_paths(runtime_name)
    _runtime_name = runtime_name
    _data_dir = paths["data_dir"]
    _logs_dir = paths["logs_dir"]
    os.environ["BOT_RUNTIME"] = runtime_name
    os.environ["BOT_DATA_DIR"] = _data_dir
    os.environ["BOT_LOGS_DIR"] = _logs_dir
    return paths


def get_data_dir() -> str:
    if _data_dir:
        return _data_dir
    env = os.environ.get("BOT_DATA_DIR")
    if env:
        return env
    from common.runtime.paths import get_runtime_paths

    # Default fallback = the ACTIVE engine (AEGIS). The legacy "astra" runtime
    # was archived on 2026-10-25; unconfigured processes must never resurrect it.
    name = os.environ.get("BOT_RUNTIME", "aegis")
    return get_runtime_paths(name)["data_dir"]


def get_logs_dir() -> str:
    if _logs_dir:
        return _logs_dir
    env = os.environ.get("BOT_LOGS_DIR")
    if env:
        return env
    from common.runtime.paths import get_runtime_paths

    # Default fallback = the ACTIVE engine (AEGIS).
    name = os.environ.get("BOT_RUNTIME", "aegis")
    return get_runtime_paths(name)["logs_dir"]
