import pandas as pd

from astra_bot.strategy.supertrend import filter_entry_signal


def test_put_blocked_when_price_above_supertrend():
    signal, reason = filter_entry_signal("PUT", 110.0, 100.0)
    assert signal == "NEUTRAL"
    assert "PUT blocked" in reason


def test_call_blocked_when_price_below_supertrend():
    signal, reason = filter_entry_signal("CALL", 90.0, 100.0)
    assert signal == "NEUTRAL"
    assert "CALL blocked" in reason


def test_aligned_signal_passes_through():
    signal, reason = filter_entry_signal("CALL", 110.0, 100.0)
    assert signal == "CALL"
    assert reason == "Supertrend aligned"
