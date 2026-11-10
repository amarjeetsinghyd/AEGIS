"""
Sanity tests for system paths, engine settings, and dashboard configuration.
"""
import importlib
import os
from dashboard.dashboard import CONSOLE_HTML


def test_aegis_settings_use_aegis_package_paths():
    settings = importlib.import_module("config.settings")
    assert os.path.basename(settings.DATA_DIR) == "data"
    assert os.path.basename(settings.LOGS_DIR) == "logs"


def test_combined_dashboard_title_is_explicit():
    assert "Quant Console" in CONSOLE_HTML or "AEGIS" in CONSOLE_HTML
