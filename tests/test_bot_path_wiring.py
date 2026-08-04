import importlib
import os


def test_astra_settings_use_astra_package_paths():
    settings = importlib.import_module("astra_bot.config.settings")
    assert os.path.basename(os.path.dirname(settings.DATA_DIR)) == "astra_bot"
    assert os.path.basename(os.path.dirname(settings.LOGS_DIR)) == "astra_bot"


def test_kronos_settings_use_kronos_package_paths():
    settings = importlib.import_module("kronos_bot.config.settings")
    assert os.path.basename(os.path.dirname(settings.DATA_DIR)) == "kronos_bot"
    assert os.path.basename(os.path.dirname(settings.LOGS_DIR)) == "kronos_bot"
