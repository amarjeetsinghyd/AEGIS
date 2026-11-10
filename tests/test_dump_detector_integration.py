import time
import pytest
from types import SimpleNamespace
from execution.execution_manager import AegisExecutionManager
from execution.trade_context import TradeContext, TradeLeg


class MockBroker:
    pass


class MockFetcher:
    pass


class MockEngine:
    pass


def test_dump_detector_initialization():
    em = AegisExecutionManager.__new__(AegisExecutionManager)
    em.is_sensex = False
    from execution.dump_detector import DumpDetector
    em.dump_detector = DumpDetector(tick=0.05, lot=65, tp_pct=0.04, sl_pct=-0.08, arm_pct=0.02)
    assert em.dump_detector is not None
    assert em.dump_detector.armed is False
    assert em.dump_detector.lot == 65 if hasattr(em.dump_detector, "lot") else True


def test_dump_detector_arming_and_disarming():
    em = AegisExecutionManager.__new__(AegisExecutionManager)
    from execution.dump_detector import DumpDetector
    em.dump_detector = DumpDetector(tick=0.05, lot=65, tp_pct=0.04, sl_pct=-0.08, arm_pct=0.02)
    em.dump_detector.arm(100.0, tp_pct=0.04, sl_pct=-0.08)
    assert em.dump_detector.armed is True
    assert em.dump_detector.entry == 100.0
    em.dump_detector.disarm()
    assert em.dump_detector.armed is False


def test_dump_detector_on_option_tick_triggers_emergency_exit():
    em = AegisExecutionManager.__new__(AegisExecutionManager)
    from execution.dump_detector import DumpDetector
    em.dump_detector = DumpDetector(tick=0.05, lot=65, tp_pct=0.04, sl_pct=-0.08, arm_pct=0.02, pull_ticks=2.0)
    
    # Setup open trade
    em._is_exiting = False
    leg = TradeLeg(token="12345", symbol="NIFTY26OCT24000CE", strike=24000.0, option_type="CALL", quantity=65, entry_price=100.0)
    em.trade_context = TradeContext(signal_type="CALL", strategy="AEGIS", index_entry=24000.0)
    em.trade_context.add_leg(leg)
    em.trade_context.status = "OPEN"
    
    exits_called = []
    em._close_all_legs = lambda reason, **kwargs: exits_called.append(reason)
    
    # Arm detector at 100.0
    em.dump_detector.arm(100.0)
    
    # Feed warm ticks around 100.0
    ts = 1000.0
    base_bp = [100.0, 99.95, 99.90, 99.85, 99.80]
    base_bq = [500, 400, 300, 200, 100]
    base_sp = [100.05, 100.10, 100.15, 100.20, 100.25]
    base_sq = [500, 400, 300, 200, 100]
    
    tick = SimpleNamespace(tok="12345", sym="NIFTY26OCT24000CE", ts=ts, lp=100.0, vol=50000.0, bp=base_bp, bq=base_bq, sp=base_sp, sq=base_sq)
    em.on_option_tick(tick)
    assert len(exits_called) == 0
    
    # Price rises to +2.5% (102.50) -> zone arms
    rally_bp = [102.50, 102.45, 102.40, 102.35, 102.30]
    rally_sp = [102.55, 102.60, 102.65, 102.70, 102.75]
    tick_rally = SimpleNamespace(tok="12345", sym="NIFTY26OCT24000CE", ts=ts + 1.0, lp=102.50, vol=52000.0, bp=rally_bp, bq=base_bq, sp=rally_sp, sq=base_sq)
    em.on_option_tick(tick_rally)
    assert em.dump_detector.zone is True
    assert len(exits_called) == 0
    
    # Now simulate an aggressive dump: bid collapses (drops 4 ticks to 102.30), heavy selling, ask stacking
    dump_bp = [102.30, 102.25, 102.20, 102.15, 102.10]
    dump_bq = [50, 40, 30, 20, 10]           # collapsed bids (90% drop)
    dump_sp = [102.35, 102.40, 102.45, 102.50, 102.55]
    dump_sq = [2500, 2000, 1500, 1000, 800]  # massive ask wall
    
    # Tick 1: heavy sell volume hitting bid
    tick_dump1 = SimpleNamespace(tok="12345", sym="NIFTY26OCT24000CE", ts=ts + 1.5, lp=102.30, vol=60000.0, bp=dump_bp, bq=dump_bq, sp=dump_sp, sq=dump_sq)
    em.on_option_tick(tick_dump1)
    
    # Tick 2: sustained persistence past 300ms
    tick_dump2 = SimpleNamespace(tok="12345", sym="NIFTY26OCT24000CE", ts=ts + 2.0, lp=102.25, vol=65000.0, bp=dump_bp, bq=dump_bq, sp=dump_sp, sq=dump_sq)
    em.on_option_tick(tick_dump2)
    
    # Assert exit was triggered with INSTITUTIONAL_DUMP_EXIT
    assert len(exits_called) == 1
    assert "INSTITUTIONAL_DUMP_EXIT" in exits_called[0]
