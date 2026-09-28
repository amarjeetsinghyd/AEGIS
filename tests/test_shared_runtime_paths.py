import importlib


def test_shared_runtime_paths_delegate_to_common():
    from common.runtime.paths import get_runtime_paths

    astra = get_runtime_paths("astra")
    kronos = get_runtime_paths("kronos")
    assert astra["data_dir"].endswith("astra_bot\\data") or astra["data_dir"].endswith("astra_bot/data")
    assert kronos["data_dir"].endswith("kronos_bot\\data") or kronos["data_dir"].endswith("kronos_bot/data")


def test_common_ipc_exports():
    from common.ipc import update_astra_live_state, update_kronos_live_state, SHARED_TICK_FILE

    assert SHARED_TICK_FILE
    update_astra_live_state({"signal_status": "TEST"})
    update_kronos_live_state({"signal": "NEUTRAL"})
