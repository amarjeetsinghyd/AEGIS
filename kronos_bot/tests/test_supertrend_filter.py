"""Tests for Supertrend filter + K-ST-FINAL gate modes + 1m aggregator."""
from datetime import datetime
from unittest.mock import MagicMock

import pandas as pd

from kronos_bot.strategy.supertrend import filter_entry_signal, supertrend_bias
from kronos_bot.core.minute_bars import MinuteBarAggregator
from kronos_bot.strategy.signal_generator import KronosSignalGenerator


def _fake_window(n: int = 20, close: float = 100.0) -> pd.DataFrame:
    ts = pd.date_range("2026-08-06 09:15", periods=n, freq="5min")
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 1000.0,
            "amount": close * 1000.0,
        }
    )


def _uptrend_1m(n: int = 40, start: float = 100.0) -> pd.DataFrame:
    """Rising 1m bars so Supertrend(10,3) ends UPTREND."""
    rows = []
    px = start
    t0 = datetime(2026, 8, 6, 9, 15)
    for i in range(n):
        o, c = px, px + 0.5
        rows.append(
            {
                "timestamp": t0 + pd.Timedelta(minutes=i),
                "open": o,
                "high": c + 0.1,
                "low": o - 0.05,
                "close": c,
            }
        )
        px = c
    return pd.DataFrame(rows)


def _patch_predictor(gen: KronosSignalGenerator, pred_close: float):
    mock_pred = MagicMock()
    mock_pred.predict.return_value = pd.DataFrame({"close": [pred_close]})
    gen._predictor = mock_pred
    gen._load_predictor = MagicMock()


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


def test_morning_bypass_returns_raw_call_without_st():
    gen = KronosSignalGenerator(device="cpu")
    _patch_predictor(gen, pred_close=100.5)  # +0.5% → CALL
    # Counter-trend 1m frame would block if gate were on — must still pass in bypass
    res = gen.predict(
        _fake_window(close=100.0),
        minute_df=_uptrend_1m(),  # unused during bypass
        now=datetime(2026, 8, 6, 9, 30, 0),
    )
    assert res["gate_mode"] == "ST_BYPASS"
    assert res["supertrend_tf"] == "bypass"
    assert res["raw_signal"] == "CALL"
    assert res["signal"] == "CALL"
    assert res["supertrend"] is None


def test_after_0945_put_blocked_on_1m_uptrend():
    gen = KronosSignalGenerator(device="cpu")
    _patch_predictor(gen, pred_close=99.0)  # −1% → PUT
    minute_df = _uptrend_1m()
    res = gen.predict(
        _fake_window(close=100.0),
        minute_df=minute_df,
        now=datetime(2026, 8, 6, 10, 0, 0),
    )
    assert res["gate_mode"] == "ST_1M"
    assert res["supertrend_tf"] == "1m"
    assert res["raw_signal"] == "PUT"
    assert res["signal"] == "NEUTRAL"
    assert res["supertrend_bias"] == "UPTREND"


def test_minute_aggregator_rolls_bar(tmp_path):
    agg = MinuteBarAggregator(data_dir=str(tmp_path), index_name="NIFTY")
    agg.reset_for_session(datetime(2026, 8, 6).date())
    t0 = datetime(2026, 8, 6, 10, 0, 10)
    agg.on_tick(100.0, t0)
    agg.on_tick(102.0, t0.replace(second=20))
    closed = agg.on_tick(101.0, t0.replace(minute=1, second=1))
    assert closed is not None
    assert closed["open"] == 100.0
    assert closed["high"] == 102.0
    assert closed["low"] == 100.0
    assert closed["close"] == 102.0
    df = agg.completed_df()
    assert len(df) == 1
    assert float(df.iloc[0]["high"]) == 102.0


def test_gate_mode_constants_in_signal_module():
    from kronos_bot.strategy import signal_generator as sg
    assert sg._ST_GATE_START.hour == 9 and sg._ST_GATE_START.minute == 45
