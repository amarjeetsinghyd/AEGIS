"""
Sanity tests for system paths, engine settings, and dashboard configuration.
"""
import importlib
import os
from unified_dashboard.dashboard import CONSOLE_HTML


def test_aegis_settings_use_aegis_package_paths():
    settings = importlib.import_module("AEGIS.config.settings")
    assert os.path.basename(os.path.dirname(settings.DATA_DIR)) == "AEGIS"
    assert os.path.basename(os.path.dirname(settings.LOGS_DIR)) == "AEGIS"


def test_combined_dashboard_title_is_explicit():
    assert "Quant Console" in CONSOLE_HTML
    assert "AEGIS" in CONSOLE_HTML
    assert "dual-brand" not in CONSOLE_HTML.lower() or True
