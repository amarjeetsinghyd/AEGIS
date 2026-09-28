import pytest
from dataclasses import dataclass
from typing import Optional
from AEGIS.core.wyckoff_volume_engine import WyckoffVolumeEngine


@dataclass
class MockBar:
    open: float
    high: float
    low: float
    close: float
    volume: float = 1000.0
    call_buy_vol: float = 0.0
    call_sell_vol: float = 0.0
    put_buy_vol: float = 0.0
    put_sell_vol: float = 0.0


def test_wyckoff_healthy_pullback_exhaustion():
    engine = WyckoffVolumeEngine(mode="ACTIVE")
    # 20 baseline bars with 1000 volume each
    bars = [MockBar(open=100.0, high=102.0, low=99.0, close=101.0, volume=1000.0) for _ in range(20)]
    
    # Healthy Pullback: 3 falling approach bars with DRYING UP volume (700 -> 500 -> 350)
    bars.append(MockBar(open=101.0, high=101.5, low=99.5, close=100.0, volume=700.0))
    bars.append(MockBar(open=100.0, high=100.5, low=99.0, close=99.2, volume=500.0))
    bars.append(MockBar(open=99.2, high=99.5, low=98.5, close=98.8, volume=350.0))
    
    # Touch & Rejection Bar T: lower wick, volume pop to 1200, strong close
    bars.append(MockBar(open=98.8, high=100.0, low=98.0, close=99.8, volume=1200.0, call_buy_vol=500.0, put_buy_vol=100.0))
    
    passed, reason, metrics = engine.evaluate(
        pillar=1,
        direction="UP",
        bars=bars,
        atr=10.0,
    )
    
    assert passed is True
    assert reason == "wyckoff_pullback_exhaustion_confirmed"
    assert metrics["is_dry_approach"] is True
    assert metrics["vdr"] < 0.85


def test_wyckoff_institutional_dump_veto():
    engine = WyckoffVolumeEngine(mode="ACTIVE")
    # 20 baseline bars with 1000 volume each
    bars = [MockBar(open=100.0, high=102.0, low=99.0, close=101.0, volume=1000.0) for _ in range(20)]
    
    # Institutional Dump: 3 falling approach bars with RISING HEAVY volume (1200 -> 1500 -> 1800)
    bars.append(MockBar(open=101.0, high=101.5, low=99.5, close=100.0, volume=1200.0))
    bars.append(MockBar(open=100.0, high=100.5, low=98.5, close=99.0, volume=1500.0))
    bars.append(MockBar(open=99.0, high=99.5, low=97.0, close=97.5, volume=1800.0))
    
    # Bar T
    bars.append(MockBar(open=97.5, high=98.0, low=96.5, close=97.0, volume=2000.0))
    
    passed, reason, metrics = engine.evaluate(
        pillar=1,
        direction="UP",
        bars=bars,
        atr=10.0,
    )
    
    assert passed is False
    assert "dump" in reason
    assert metrics["verdict"] == "INSTITUTIONAL_DUMP"


def test_wyckoff_pullback_opposing_put_delta_surge_veto():
    engine = WyckoffVolumeEngine(mode="ACTIVE")
    bars = [MockBar(open=100.0, high=102.0, low=99.0, close=101.0, volume=1000.0) for _ in range(20)]
    
    # Normal pullback volume
    bars.append(MockBar(open=101.0, high=101.5, low=99.5, close=100.0, volume=600.0))
    bars.append(MockBar(open=100.0, high=100.5, low=99.0, close=99.2, volume=500.0))
    bars.append(MockBar(open=99.2, high=99.5, low=98.5, close=98.8, volume=400.0))
    
    # Bar T has massive Put Aggression (1500 put buys vs 100 call buys -> 15x ratio)
    bars.append(MockBar(open=98.8, high=99.5, low=98.0, close=99.0, volume=800.0, call_buy_vol=100.0, put_buy_vol=1500.0))
    
    passed, reason, metrics = engine.evaluate(
        pillar=1,
        direction="UP",
        bars=bars,
        atr=10.0,
    )
    
    assert passed is False
    assert reason == "wyckoff_opposing_put_delta_surge"


def test_wyckoff_squeeze_breakout_harmony_and_divergence():
    engine = WyckoffVolumeEngine(mode="ACTIVE")
    bars = [MockBar(open=100.0, high=102.0, low=99.0, close=101.0, volume=1000.0) for _ in range(20)]
    
    # Scenario A: Genuine Breakout with Delta Harmony (Call Buy 1200 vs Put Buy 200)
    bars_harmony = list(bars)
    bars_harmony.append(MockBar(open=101.0, high=104.0, low=100.8, close=103.5, volume=1500.0, call_buy_vol=1200.0, put_buy_vol=200.0))
    passed, reason, _ = engine.evaluate(pillar=3, direction="UP", bars=bars_harmony, atr=10.0)
    assert passed is True
    assert reason == "wyckoff_breakout_harmony_confirmed"
    
    # Scenario B: Bull Trap with Delta Divergence (Price breaks up, but Put Buy 1600 vs Call Buy 300)
    bars_trap = list(bars)
    bars_trap.append(MockBar(open=101.0, high=104.0, low=100.8, close=103.5, volume=1500.0, call_buy_vol=300.0, put_buy_vol=1600.0))
    passed_trap, reason_trap, m_trap = engine.evaluate(pillar=3, direction="UP", bars=bars_trap, atr=10.0)
    assert passed_trap is False
    assert reason_trap == "wyckoff_breakout_delta_divergence_bear_swamp"
    assert m_trap["verdict"] == "BULL_TRAP_DELTA_DIVERGENCE"


def test_wyckoff_synthetic_zero_volume_pass():
    engine = WyckoffVolumeEngine(mode="ACTIVE")
    bars = [MockBar(open=100.0, high=102.0, low=99.0, close=101.0, volume=0.0) for _ in range(10)]
    passed, reason, _ = engine.evaluate(pillar=1, direction="UP", bars=bars, atr=10.0)
    assert passed is True
    assert reason == "synthetic_zero_volume_pass"
