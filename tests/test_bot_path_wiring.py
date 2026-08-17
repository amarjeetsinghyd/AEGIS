import importlib
import os


def test_aegis_settings_use_aegis_package_paths():
    settings = importlib.import_module("AEGIS.config.settings")
    assert os.path.basename(os.path.dirname(settings.DATA_DIR)) == "AEGIS"
    assert os.path.basename(os.path.dirname(settings.LOGS_DIR)) == "AEGIS"
