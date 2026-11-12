"""
Targeted tests for Task 2, 3, and 6:
- Task 2: Master VWAP gate placed strictly above Pillar 5.
- Task 3: Same-Candle Double Trigger Prevention via _last_committed_bar_idx.
- Task 6: Pillar 3 5m Trend Filter and Dynamic ATR Box Pierce.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
from core.price_action_engine import PriceActionEngine, OHLCBar


def _make_bar(open_p, high_p, low_p, close_p, vol=1000.0, ts_start=1000.0):
    return OHLCBar(
        ts_start=float(ts_start),
        ts_end=float(ts_start + 60.0),
        open=float(open_p),
        high=float(high_p),
        low=float(low_p),
        close=float(close_p),
        volume=float(vol),
    )


def test_task_2_vwap_gate_blocks_pillar_5():
    """
    Task 2: Master VWAP Gate is an absolute pre-condition for ALL pillars including Pillar 5.
    If spot is below session VWAP, UP breakthrough must immediately return (False, 'below_vwap', {}).
    """
    pae = PriceActionEngine(strike_step=50.0)
    pae.current_atr_1m = 15.0
    pae.current_vwap = 24100.0
    pae.last_price = 24050.0  # Spot is below VWAP 24100.0
    now = 1792730040.0
    pae.last_ts = now + 15

    pae._curr_1m_bar = _make_bar(24040.0, 24060.0, 24035.0, 24050.0, vol=50000.0, ts_start=now)
    pae.bars_1m.append(_make_bar(24020.0, 24045.0, 24015.0, 24040.0, vol=40000.0, ts_start=now - 60))
    pae._total_bars_seen = 10

    # UP breakthrough must fail at master VWAP gate before even evaluating Pillar 5
    ok, reason, details = pae.check_mode_b_breakthrough("UP", current_time=pae.last_ts)
    assert ok is False
    assert reason == "below_vwap"


def test_task_3_same_candle_double_trigger_lockout():
    """
    Task 3: When a signal is committed on bar N, _last_committed_bar_idx locks out
    subsequent triggers on bar N across Mode A and Mode B for that direction.
    """
    pae = PriceActionEngine(strike_step=50.0)
    pae.current_atr_1m = 10.0
    pae._total_bars_seen = 25
    now = 1792730040.0
    pae.last_ts = now + 15

    # Downstream execution commits a setup on bar 25 for UP
    pae.commit_setup({"direction": "UP", "entry_bar_bucket": 25})

    # Both Mode B and Mode A checks on bar 25 must be locked out
    ok_b, reason_b, _ = pae.check_mode_b_breakthrough("UP", current_time=pae.last_ts)
    assert ok_b is False
    assert reason_b == "same_candle_already_fired"

    ok_p1, reason_p1, _ = pae.check_20_ema_rejection("UP")
    assert ok_p1 is False
    assert reason_p1 == "same_candle_already_fired"

    ok_p2, reason_p2, _ = pae.check_micro_sweep_reclaim("UP")
    assert ok_p2 is False
    assert reason_p2 == "same_candle_already_fired"


def test_task_6_pillar_3_box_pierce_and_5m_trend_filter():
    """
    Task 6: Pillar 3 Volatility Squeeze requires:
    1. Scale-invariant pierce = max(1.5, 0.20 * atr)
    2. 5m trend alignment (b5.close >= b5.open for UP, b5.close <= b5.open for DOWN)
    """
    pae = PriceActionEngine(strike_step=50.0)
    pae.current_atr_1m = 15.0  # min pierce = max(1.5, 0.20 * 15) = 3.0 pts
    now = 1792730040.0
    pae.last_ts = now + 15
    pae._total_bars_seen = 20

    # Seed 5m bar with BEARISH candle (open 24100, close 24080)
    pae.bars_5m.append(_make_bar(24100.0, 24105.0, 24075.0, 24080.0, vol=100000.0))

    # Mock squeeze box with box_high=24090.0
    pae._get_squeeze_box_cached = MagicMock(return_value=(True, {"box_high": 24090.0, "box_low": 24070.0, "bar_count": 5}))
    pae._pain_zone_vwap_aligned = MagicMock(return_value=True)
    pae._check_structural_obstacle = MagicMock(return_value=(True, "", {}))

    # Spot crosses box_high by 3.5 pts (spot=24093.5), but 5m trend is BEARISH -> Blocked!
    pae._curr_1m_bar = _make_bar(24085.0, 24095.0, 24084.0, 24093.5, vol=20000.0, ts_start=now)
    pae.last_price = 24093.5

    ok_trend_blocked, _, _ = pae.check_mode_b_breakthrough("UP", current_time=pae.last_ts)
    assert ok_trend_blocked is False

    # Flip 5m bar to BULLISH candle (open 24080, close 24100) -> Allowed!
    pae.bars_5m[-1] = _make_bar(24080.0, 24105.0, 24075.0, 24100.0, vol=100000.0)
    ok_approved, reason, det = pae.check_mode_b_breakthrough("UP", current_time=pae.last_ts)
    assert ok_approved is True
    assert det["pillar"] == 3
    assert det["direction"] == "UP"
