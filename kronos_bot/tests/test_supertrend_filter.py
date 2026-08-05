import pandas as pd

from kronos_bot.strategy.supertrend import filter_entry_signal, supertrend_bias


def test_put_blocked_when_price_above_supertrend():
    signal, reason = filter_entry_signal("PUT", 110.0, 100.0)
    assert signal == "NEUTRAL"
    assert "PUT blocked" in reason
    assert "UPTREND" in reason


def test_call_blocked_when_price_below_supertrend():
    signal, reason = filter_entry_signal("CALL", 90.0, 100.0)
    assert signal == "NEUTRAL"
    assert "CALL blocked" in reason
    assert "DOWNTREND" in reason


def test_aligned_signal_passes_through():
    signal, reason = filter_entry_signal("CALL", 110.0, 100.0)
    assert signal == "CALL"
    assert "Supertrend aligned" in reason
    assert "UPTREND" in reason


def test_supertrend_bias_labels():
    assert supertrend_bias(24614.25, 24550.71) == "UPTREND"
    assert supertrend_bias(24500.0, 24550.71) == "DOWNTREND"
