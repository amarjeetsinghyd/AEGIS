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


def test_wyckoff_call_liquidation_dump_veto():
    """Vetoes when call sellers are aggressively dumping (CallSell > 1.5 * CallBuy) on a call setup."""
    engine = WyckoffVolumeEngine(mode="ACTIVE")
    bars = [MockBar(open=100.0, high=102.0, low=99.0, close=101.0, volume=1000.0) for _ in range(20)]
    # Healthy pullback approach
    bars.append(MockBar(open=101.0, high=101.5, low=99.5, close=100.0, volume=600.0))
    bars.append(MockBar(open=100.0, high=100.5, low=99.0, close=99.2, volume=500.0))
    bars.append(MockBar(open=99.2, high=99.5, low=98.5, close=98.8, volume=400.0))
    # Rejection candle has 1000 CallBuy, but 1800 CallSell (heavy liquidation dumping)
    bars.append(MockBar(open=98.8, high=100.0, low=98.0, close=99.8, volume=2800.0, call_buy_vol=1000.0, call_sell_vol=1800.0, put_buy_vol=100.0))
    
    passed, reason, metrics = engine.evaluate(pillar=1, direction="UP", bars=bars, atr=10.0)
    assert passed is False
    assert reason == "wyckoff_call_liquidation_dump"
    assert metrics["verdict"] == "CALL_LIQUIDATION_DUMP"


def test_wyckoff_breakout_insufficient_delta_imbalance_veto():
    """Vetoes when breakout candle has high volume but lacks >= 1.5x delta imbalance."""
    engine = WyckoffVolumeEngine(mode="ACTIVE")
    bars = [MockBar(open=100.0, high=102.0, low=99.0, close=101.0, volume=1000.0) for _ in range(20)]
    # Breakout candle has 600 CallBuy vs 550 PutBuy (ratio ~1.09x < 1.5x)
    bars.append(MockBar(open=101.0, high=104.0, low=100.8, close=103.5, volume=1500.0, call_buy_vol=600.0, put_buy_vol=550.0))
    
    passed, reason, metrics = engine.evaluate(pillar=3, direction="UP", bars=bars, atr=10.0)
    assert passed is False
    assert reason == "wyckoff_breakout_insufficient_delta_imbalance"
    assert metrics["verdict"] == "INSUFFICIENT_DELTA_IMBALANCE"


def test_wyckoff_synthetic_net_delta_metrics():
    """Verifies that synthetic_net_delta ((CallBuy + PutSell) - (PutBuy + CallSell)) is correctly computed in metrics."""
    engine = WyckoffVolumeEngine(mode="ACTIVE")
    bars = [MockBar(open=100.0, high=102.0, low=99.0, close=101.0, volume=1000.0) for _ in range(20)]
    # cb=1200, cs=200, pb=200, ps=300
    # Bullish: cb(1200) + ps(300) = 1500
    # Bearish: pb(200) + cs(200) = 400
    # Synth Delta: 1500 - 400 = 1100
    bars.append(MockBar(open=101.0, high=104.0, low=100.8, close=103.5, volume=1900.0,
                        call_buy_vol=1200.0, call_sell_vol=200.0, put_buy_vol=200.0, put_sell_vol=300.0))
    passed, reason, metrics = engine.evaluate(pillar=3, direction="UP", bars=bars, atr=10.0)
    assert passed is True
    assert reason == "wyckoff_breakout_harmony_confirmed"
    assert metrics["synthetic_net_delta"] == 1100.0
    assert metrics["call_buy"] == 1200.0
    assert metrics["call_sell"] == 200.0
    assert metrics["put_buy"] == 200.0
    assert metrics["put_sell"] == 300.0


def test_wyckoff_pullback_no_absorption_nor_dryup_veto():
    """Vetoes when pullback volume didn't dry up (vdr > 0.85, rising) and rejection bar has no absorption wick."""
    engine = WyckoffVolumeEngine(mode="ACTIVE")
    bars = [MockBar(open=100.0, high=102.0, low=99.0, close=101.0, volume=1000.0) for _ in range(20)]
    # Approach bars with rising volume 900 -> 1000 -> 1100 (vdr = 1.0, slope > 0: NOT dry)
    bars.append(MockBar(open=101.0, high=101.5, low=99.5, close=100.0, volume=900.0))
    bars.append(MockBar(open=100.0, high=100.5, low=99.0, close=99.5, volume=1000.0))
    bars.append(MockBar(open=99.5, high=99.8, low=98.5, close=99.0, volume=1100.0))
    # Rejection bar has almost NO lower wick (flat close at low) and low volume -> No absorption!
    bars.append(MockBar(open=99.0, high=99.5, low=97.5, close=97.6, volume=600.0, call_buy_vol=200.0, put_buy_vol=100.0))
    
    passed, reason, metrics = engine.evaluate(pillar=1, direction="UP", bars=bars, atr=10.0)
    assert passed is False
    assert reason == "wyckoff_pullback_no_absorption_nor_dryup"
    assert metrics["verdict"] == "PULLBACK_LACKS_DRYUP_AND_ABSORPTION"


def test_wyckoff_pullback_asymmetric_stopping_volume_pass():
    """Passes when pullback wasn't deeply dry but Bar T exhibits textbook Asymmetric Stopping Absorption."""
    engine = WyckoffVolumeEngine(mode="ACTIVE")
    bars = [MockBar(open=100.0, high=102.0, low=99.0, close=101.0, volume=1000.0) for _ in range(20)]
    # Moderate approach (vdr ~ 0.95, slope negative)
    bars.append(MockBar(open=101.0, high=101.5, low=99.5, close=100.0, volume=1000.0))
    bars.append(MockBar(open=100.0, high=100.5, low=99.0, close=99.5, volume=950.0))
    bars.append(MockBar(open=99.5, high=99.8, low=98.5, close=99.0, volume=900.0))
    # Bar T has heavy opposing put volume absorbed: lower wick = 1.0 on 2.0 spread (wick_ratio=0.50), volume=1200
    bars.append(MockBar(open=99.0, high=100.0, low=98.0, close=99.8, volume=1200.0,
                        call_buy_vol=600.0, call_sell_vol=100.0, put_buy_vol=400.0, put_sell_vol=200.0))
    
    passed, reason, metrics = engine.evaluate(pillar=1, direction="UP", bars=bars, atr=10.0)
    assert passed is True
    assert reason == "wyckoff_pullback_exhaustion_confirmed"
    assert metrics["is_stopping_vol"] is True
    assert metrics["verdict"] == "ASYMMETRIC_STOPPING_ABSORPTION"


def test_wyckoff_pillar_2_spring_wick_absorption():
    """Tests that Pillar 2 Spring requires >= 30% lower wick to confirm liquidity absorption."""
    engine = WyckoffVolumeEngine(mode="ACTIVE")
    bars = [MockBar(open=100.0, high=102.0, low=99.0, close=101.0, volume=1000.0) for _ in range(20)]
    
    # Case A: Weak spring bar with tiny lower wick (open=99.0, close=98.5, low=98.4 -> lower wick = 0.1, spread=2.0 -> wick_ratio=0.05 < 0.30)
    bars_weak = list(bars)
    bars_weak.append(MockBar(open=99.0, high=100.4, low=98.4, close=98.5, volume=1200.0, call_buy_vol=500.0, put_buy_vol=200.0))
    passed_weak, reason_weak, m_weak = engine.evaluate(pillar=2, direction="UP", bars=bars_weak, atr=10.0)
    assert passed_weak is False
    assert reason_weak == "wyckoff_spring_failed_insufficient_wick_absorption"
    assert m_weak["verdict"] == "SPRING_INSUFFICIENT_ABSORPTION_WICK"

    # Case B: Strong spring bar with lower wick = 0.9 on 2.0 spread -> wick_ratio = 0.45 >= 0.30
    bars_strong = list(bars)
    bars_strong.append(MockBar(open=98.9, high=100.0, low=98.0, close=99.8, volume=1500.0, call_buy_vol=800.0, put_buy_vol=200.0))
    passed_strong, reason_strong, m_strong = engine.evaluate(pillar=2, direction="UP", bars=bars_strong, atr=10.0)
    assert passed_strong is True
    assert reason_strong == "wyckoff_spring_reclaim_confirmed"
    assert m_strong["verdict"] == "WYCKOFF_SPRING_ABSORPTION"


def test_wyckoff_breakout_strictly_2x_imbalance():
    """Verifies that breakout requires strictly >= 2.0x delta imbalance (user requirement)."""
    engine = WyckoffVolumeEngine(mode="ACTIVE")
    bars = [MockBar(open=100.0, high=102.0, low=99.0, close=101.0, volume=1000.0) for _ in range(20)]

    # Sub-case 1: 1.8x imbalance (e.g. CallBuy=900, PutBuy=500 -> ratio 1.80x < 2.0x) -> MUST VETO
    bars_18x = list(bars)
    bars_18x.append(MockBar(open=101.0, high=104.0, low=100.8, close=103.5, volume=1500.0, call_buy_vol=900.0, put_buy_vol=500.0))
    passed_18x, reason_18x, m_18x = engine.evaluate(pillar=3, direction="UP", bars=bars_18x, atr=10.0)
    assert passed_18x is False
    assert reason_18x == "wyckoff_breakout_insufficient_delta_imbalance"
    assert m_18x["ratio"] == 1.80

    # Sub-case 2: 2.2x imbalance (e.g. CallBuy=1100, PutBuy=500 -> ratio 2.20x >= 2.0x) -> PASSES
    bars_22x = list(bars)
    bars_22x.append(MockBar(open=101.0, high=104.0, low=100.8, close=103.5, volume=1700.0, call_buy_vol=1100.0, put_buy_vol=500.0))
    passed_22x, reason_22x, _ = engine.evaluate(pillar=3, direction="UP", bars=bars_22x, atr=10.0)
    assert passed_22x is True
    assert reason_22x == "wyckoff_breakout_harmony_confirmed"


def test_wyckoff_breakout_synthetic_delta_negative_veto():
    """Vetoes when direct ratio is >= 2.0x but synthetic net delta is negative due to call liquidation."""
    engine = WyckoffVolumeEngine(mode="ACTIVE")
    bars = [MockBar(open=100.0, high=102.0, low=99.0, close=101.0, volume=1000.0) for _ in range(20)]
    # cb=1200, pb=400 (direct ratio = 3.0x), but call_sell=1400 (liquidation dump)
    # bullish_vol = 1200 + 0 = 1200
    # bearish_vol = 400 + 1400 = 1800
    # synth_delta = 1200 - 1800 = -600 < 0
    # Note: CallSell(1400) > 1.5 * cb(1200) would trigger liquidation dump first.
    # To isolate synthetic delta test: cs=1500, cb=1100 -> cs > 1.5*cb triggers call liquidation.
    # Let cs=1600, cb=1100:
    bars.append(MockBar(open=101.0, high=104.0, low=100.8, close=103.5, volume=2500.0,
                        call_buy_vol=1100.0, call_sell_vol=1600.0, put_buy_vol=400.0))
    passed, reason, metrics = engine.evaluate(pillar=3, direction="UP", bars=bars, atr=10.0)
    assert passed is False
    assert "liquidation" in reason or "synthetic_delta" in reason


def test_wyckoff_pillar_1_dow_swing_volume_confirmed():
    """Verifies that Pillar 1 uses True Swing VDR when Dow swing metrics are passed."""
    engine = WyckoffVolumeEngine(mode="ACTIVE")
    bars = [MockBar(open=100.0, high=102.0, low=99.0, close=101.0, volume=1000.0) for _ in range(20)]
    bars.append(MockBar(open=100.0, high=101.5, low=99.8, close=101.0, volume=9000.0,
                        call_buy_vol=500.0, put_buy_vol=100.0))
    pa_details = {
        "imp_avg_vol": 15000.0,
        "pb_avg_vol": 10000.0,
        "swing_vdr": 0.67,
        "entry_vol": 9000.0,
        "imp_directional_flow": 5000.0,
        "pb_counter_flow": 2000.0,
    }
    passed, reason, metrics = engine.evaluate(pillar=1, direction="UP", bars=bars, atr=10.0, pa_details=pa_details)
    assert passed is True
    assert reason == "wyckoff_dow_swing_volume_confirmed"
    assert metrics["vdr"] == 0.67
    assert metrics["imp_avg_vol"] == 15000.0
    assert metrics["pb_avg_vol"] == 10000.0


def test_wyckoff_pillar_1_dow_swing_dump_veto():
    """Vetoes when Retracement volume dumps and exceeds Impulse Upswing volume (vdr >= 1.25)."""
    engine = WyckoffVolumeEngine(mode="ACTIVE")
    bars = [MockBar(open=100.0, high=102.0, low=99.0, close=101.0, volume=1000.0) for _ in range(20)]
    bars.append(MockBar(open=100.0, high=101.5, low=99.8, close=101.0, volume=12000.0))
    pa_details = {
        "imp_avg_vol": 10000.0,
        "pb_avg_vol": 13000.0,
        "swing_vdr": 1.30,
        "entry_vol": 12000.0,
        "imp_directional_flow": 4000.0,
        "pb_counter_flow": 2000.0,
    }
    passed, reason, metrics = engine.evaluate(pillar=1, direction="UP", bars=bars, atr=10.0, pa_details=pa_details)
    assert passed is False
    assert reason == "wyckoff_dump_pullback_exceeds_upswing"
    assert metrics["vdr"] == 1.30


def test_wyckoff_pillar_1_dow_counter_flow_dominance_veto():
    """Vetoes when Retracement counter option flow dominates Impulse directional flow (> 1.25x)."""
    engine = WyckoffVolumeEngine(mode="ACTIVE")
    bars = [MockBar(open=100.0, high=102.0, low=99.0, close=101.0, volume=1000.0) for _ in range(20)]
    bars.append(MockBar(open=100.0, high=101.5, low=99.8, close=101.0, volume=8000.0))
    pa_details = {
        "imp_avg_vol": 12000.0,
        "pb_avg_vol": 9000.0,
        "swing_vdr": 0.75,
        "entry_vol": 8000.0,
        "imp_directional_flow": 3000.0,
        "pb_counter_flow": 4500.0,  # 4500 > 1.25 * 3000 (3750)
    }
    passed, reason, metrics = engine.evaluate(pillar=1, direction="UP", bars=bars, atr=10.0, pa_details=pa_details)
    assert passed is False
    assert reason == "wyckoff_pullback_counter_flow_dominance"



