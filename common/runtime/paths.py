from pathlib import Path


def get_project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def get_runtime_paths(runtime_name: str) -> dict:
    runtime_name = (runtime_name or "").strip().lower()

    if runtime_name in {"astra", "kronos", "stock_order_flow"}:
        root_dir = get_project_root() / f"{runtime_name}_bot" if runtime_name != "stock_order_flow" else get_project_root() / runtime_name
    elif runtime_name == "dashboard":
        root_dir = get_project_root() / "unified_dashboard"
    elif runtime_name in {"hub", "shoonya_data_hub", "data_hub"}:
        root_dir = get_project_root() / "shoonya_data_hub"
    else:
        raise ValueError("runtime_name must be 'astra', 'kronos', 'dashboard', or 'hub'")

    data_dir = root_dir / "data"
    logs_dir = root_dir / "logs"

    for path in (data_dir, logs_dir):
        path.mkdir(parents=True, exist_ok=True)

    return {
        "project_root": str(get_project_root()),
        "root_dir": str(root_dir),
        "data_dir": str(data_dir),
        "logs_dir": str(logs_dir),
    }
