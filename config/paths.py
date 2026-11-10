"""Canonical Project and Runtime Paths for AEGIS."""
from pathlib import Path


def get_project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def get_runtime_paths(runtime_name: str = "aegis") -> dict:
    root = get_project_root()
    data_dir = root / "data"
    logs_dir = root / "logs"
    for p in (data_dir, logs_dir):
        p.mkdir(parents=True, exist_ok=True)
    return {
        "project_root": str(root),
        "root_dir": str(root),
        "data_dir": str(data_dir),
        "logs_dir": str(logs_dir),
    }
