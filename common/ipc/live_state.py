"""Cross-process live-state IPC via /dev/shm (no Flask dependency)."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from typing import Any

_lock = threading.Lock()

_SHM = "/dev/shm" if os.path.exists("/dev/shm") else None


def _fallback_dir() -> str:
    from common.runtime.paths import get_project_root

    path = get_project_root() / "data" / "shm"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _shm_path(name: str) -> str:
    base = _SHM or _fallback_dir()
    return os.path.join(base, name)


SHARED_TICK_FILE = _shm_path("shoonya_live_ticks.json")
ASTRA_STATE_FILE = _shm_path("kdo_live_state.json")
KRONOS_STATE_FILE = _shm_path("kronos_live_state.json")
AEGIS_STATE_FILE = _shm_path("aegis_live_state.json")
ARGUS_STATE_FILE = _shm_path("argus_live_state.json")
KILL_SWITCH_FILE = _shm_path("KILL_SWITCH_ACTIVE")

_DEFAULT_ASTRA = {
    "timestamp": "",
    "active_index": "NIFTY",
    "spot_price": 0.0,
    "chain_delta": 0.0,
    "l2_wall_size": 0.0,
    "signal_status": "NEUTRAL",
    "reasoning": "Waiting for live market tick feed...",
    "kinematic_ready": False,
    "rollover_pause": False,
    "option_chain": [],
    "active_trade": None,
    "available_funds": 0.0,
    "engine": "astra",
}

_DEFAULT_KRONOS = {
    "timestamp": "",
    "active_index": "NIFTY",
    "spot_price": 0.0,
    "predicted_close": 0.0,
    "predicted_return_pct": 0.0,
    "signal": "NEUTRAL",
    "signal_status": "NEUTRAL",
    "net_delta_oi": 0.0,
    "last_candle": "",
    "active_trade": None,
    "available_funds": 0.0,
    "reasoning": "Waiting for next 5-minute candle...",
    "engine": "kronos",
}

# AEGIS V2.0 live-state schema (Gamma Burst Engine)
# Keys mirror AegisGammaEngine.get_state_summary() + execution manager state.
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

# ARGUS V3.6 live-state schema (Equities Microstructure Scalper)
_DEFAULT_ARGUS = {
    "timestamp": "",
    "active_symbol": "—",
    "spot_price": 0.0,
    "signal": "WAITING",
    "conviction": 0.0,
    "adv_shield": "CLEAR",
    "regime": "BURST",
    "kyle_lambda": 0.0,
    "branching": 0.0,
    "whale_z": 0.0,
    "universe_count": 15,
    "trades_today": 0,
    "daily_pnl": 0.0,
    "has_open_trade": False,
    "active_trade": None,
    "available_funds": 0.0,
    "params_source": "EOD ML 3-Day",
    "reasoning": "Scanning universe for microstructure conviction…",
    "engine": "argus",
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


def update_astra_live_state(data: dict) -> dict:
    return _update_state(ASTRA_STATE_FILE, _DEFAULT_ASTRA, data)


def get_astra_live_state() -> dict:
    return _read_json(ASTRA_STATE_FILE, _DEFAULT_ASTRA)


def update_kronos_live_state(data: dict) -> dict:
    return _update_state(KRONOS_STATE_FILE, _DEFAULT_KRONOS, data)


def get_kronos_live_state() -> dict:
    return _read_json(KRONOS_STATE_FILE, _DEFAULT_KRONOS)


def update_aegis_live_state(data: dict) -> dict:
    return _update_state(AEGIS_STATE_FILE, _DEFAULT_AEGIS, data)


def get_aegis_live_state() -> dict:
    return _read_json(AEGIS_STATE_FILE, _DEFAULT_AEGIS)


def update_argus_live_state(data: dict) -> dict:
    return _update_state(ARGUS_STATE_FILE, _DEFAULT_ARGUS, data)


def get_argus_live_state() -> dict:
    return _read_json(ARGUS_STATE_FILE, _DEFAULT_ARGUS)


# Back-compat aliases used by Astra KDO loop
update_kdo_live_state = update_astra_live_state
get_kdo_live_state = get_astra_live_state


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
