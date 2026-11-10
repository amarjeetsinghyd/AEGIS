"""Cross-process live-state IPC via /dev/shm or local fallback."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from typing import Any

_lock = threading.Lock()
_SHM = "/dev/shm" if os.path.exists("/dev/shm") else None


def _fallback_dir() -> str:
    from config.paths import get_project_root

    path = get_project_root() / "data" / "shm"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _shm_path(name: str) -> str:
    base = _SHM or _fallback_dir()
    return os.path.join(base, name)


SHARED_TICK_FILE = _shm_path("shoonya_live_ticks.json")
AEGIS_STATE_FILE = _shm_path("aegis_live_state.json")
KILL_SWITCH_FILE = _shm_path("KILL_SWITCH_ACTIVE")

# AEGIS V1.0 live-state schema
_DEFAULT_AEGIS = {
    "timestamp": "",
    "active_index": "NIFTY",
    "spot_price": 0.0,
    "atm_strike": 0.0,
    "atm_iv": 0.0,
    "gamma_sum": 0.0,
    "speed_sum": 0.0,
    "hawkes_B": 0.0,
    "conviction_eff": 0.0,
    "params_source": "priors",
    "engine": "aegis",
}


def _atomic_write(path: str, payload: dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


def _read_json(path: str, default: dict) -> dict:
    if not os.path.exists(path):
        return dict(default)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return dict(default)


def _update_state(path: str, base: dict, data: dict) -> dict:
    with _lock:
        current = _read_json(path, base)
        current.update(data or {})
        current["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            _atomic_write(path, current)
        except Exception:
            pass
        return current


def update_aegis_live_state(data: dict) -> dict:
    return _update_state(AEGIS_STATE_FILE, _DEFAULT_AEGIS, data)


def get_aegis_live_state() -> dict:
    return _read_json(AEGIS_STATE_FILE, _DEFAULT_AEGIS)


def is_kill_switch_active(data_dir: str | None = None) -> bool:
    if os.path.exists(KILL_SWITCH_FILE):
        return True
    if data_dir and os.path.exists(os.path.join(data_dir, "KILL_SWITCH_ACTIVE")):
        return True
    return False


def set_kill_switch(active: bool, data_dir: str | None = None) -> None:
    if active:
        open(KILL_SWITCH_FILE, "a", encoding="utf-8").close()
        if data_dir:
            open(os.path.join(data_dir, "KILL_SWITCH_ACTIVE"), "a", encoding="utf-8").close()
    else:
        for path in (KILL_SWITCH_FILE, os.path.join(data_dir or "", "KILL_SWITCH_ACTIVE")):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass
