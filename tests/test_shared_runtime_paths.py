def test_shared_runtime_paths_delegate_to_common():
    from config.paths import get_runtime_paths

    aegis = get_runtime_paths("aegis")
    assert aegis["data_dir"].endswith("data")
    assert aegis["logs_dir"].endswith("logs")


def test_common_ipc_exports():
    from ipc.live_state import update_aegis_live_state, SHARED_TICK_FILE

    assert SHARED_TICK_FILE
    res = update_aegis_live_state({"signal_status": "TEST"})
    assert res.get("signal_status") == "TEST"
