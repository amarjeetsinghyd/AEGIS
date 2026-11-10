"""AEGIS Inter-Process Communication (IPC)."""
from ipc.live_state import (
    update_aegis_live_state,
    get_aegis_live_state,
    is_kill_switch_active,
    set_kill_switch,
    SHARED_TICK_FILE,
    AEGIS_STATE_FILE,
    KILL_SWITCH_FILE,
)

__all__ = [
    "update_aegis_live_state",
    "get_aegis_live_state",
    "is_kill_switch_active",
    "set_kill_switch",
    "SHARED_TICK_FILE",
    "AEGIS_STATE_FILE",
    "KILL_SWITCH_FILE",
]
