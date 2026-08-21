"""
Unit and Integration Tests for STRATOS: Pure Turnover-Bucket Market Structure Engine.
"""
import pytest
import numpy as np
from datetime import time
from STRATOS.turnover_bucket_engine import TurnoverBucketEngine, SymbolBucketAccumulator
from STRATOS.structure_engine import MarketStructureEngine
from STRATOS.pullback_analyzer import PullbackAnalyzer
from STRATOS.flip_trigger import FlipTriggerEngine
from STRATOS.anti_hunt_risk import AntiHuntRiskEngine
from STRATOS.trade_manager import ActiveTradeManager
from STRATOS.orchestrator import StratosOrchestrator

def test_turnover_bucket_accumulator():
    # ₹20,000 threshold for testing
    acc = SymbolBucketAccumulator(target_turnover_inr=20_000.0, max_history=50)
    t0 = 1700000000.0
    
    completed = []
    # Send ticks: price = 1000.0, volume = 5.0 (Turnover per tick = 5000)
    for i in range(10):
        t = t0 + i * 2.0
        price = 1000.0 + i
        cum_vol = (i + 1) * 5.0
        b = acc.on_tick(t, price, cum_vol, is_buy=True)
        if b:
            completed.append(b)
            
    assert len(completed) >= 2
    assert completed[0].turnover >= 20_000.0
    assert completed[0].open == 1000.0
    assert completed[0].close > 1000.0
    assert completed[0].delta > 0

def test_structure_engine_turnover_breakout():
    engine = MarketStructureEngine()
    
    highs = np.array([95, 100, 96, 92, 90, 93, 105, 110, 106, 97, 95, 99, 112, 115, 116], dtype=np.float64)
    lows = np.array([88, 92, 89, 87, 85, 88, 98, 102, 98, 93, 91, 94, 108, 110, 111], dtype=np.float64)
    closes = np.array([94, 98, 91, 90, 89, 92, 104, 108, 99, 94, 93, 98, 111, 115, 116], dtype=np.float64)
    vols = np.full(len(closes), 50000.0)
    deltas = np.full(len(closes), 10000.0)

    arrays = {"high": highs, "low": lows, "close": closes, "volume": vols, "delta": deltas}
    
    res = engine.update_structure("TCS", arrays)
    assert res["status"] in ["AWAITING_LONG_RETEST", "AWAITING_RETEST", "NO_CLEAN_BREAKOUT"]

def test_pullback_analyzer_turnover():
    analyzer = PullbackAnalyzer()
    
    # Low-volume shallow pullback
    highs = np.array([105, 104, 103, 102, 101], dtype=np.float64)
    lows = np.array([102, 101, 100.5, 100.1, 100.0], dtype=np.float64)
    closes = np.array([103, 102, 101, 100.5, 100.2], dtype=np.float64)
    volumes = np.array([2000, 1800, 1500, 1200, 1000], dtype=np.float64) # Drying volume
    deltas = np.array([-100, -80, -50, -20, 10], dtype=np.float64)
    
    arrays = {"high": highs, "low": lows, "close": closes, "volume": volumes, "delta": deltas}
    res = analyzer.analyze_pullback_quality(arrays, support_level=100.0, breakout_vol=10000.0)
    
    assert res["valid"] is True
    assert res["vol_ratio"] < 0.85

def test_flip_trigger_rejection_wick():
    trigger = FlipTriggerEngine()
    
    opens = np.array([100.8])
    highs = np.array([101.5])
    lows = np.array([99.8])
    closes = np.array([101.2])
    volumes = np.array([5000.0])
    deltas = np.array([2500.0]) # Positive buyer delta
    
    arrays = {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes, "delta": deltas}
    res = trigger.evaluate_trigger(arrays, support_level=100.0)
    assert res["triggered"] is True
    assert res["pattern"] == "LOWER_REJECTION_WICK"

def test_anti_hunt_risk():
    risk = AntiHuntRiskEngine()
    highs = np.linspace(100, 105, 20)
    lows = np.linspace(98, 103, 20)
    closes = np.linspace(99, 104, 20)
    arrays = {"high": highs, "low": lows, "close": closes}
    
    levels = risk.calculate_trade_levels(entry_price=104.0, support_level=100.0, rejection_low=99.8, arrays=arrays)
    assert levels["stop_loss"] < 99.8 # Strictly below lowest wick with breathing room buffer
    assert levels["tp1_price"] > 104.0
    assert levels["reward_risk"] == 2.0

def test_trade_manager_adaptive_trailing():
    mgr = ActiveTradeManager()
    trade = {
        "symbol": "INFY", "entry_price": 100.0, "stop_loss": 98.0,
        "tp1_price": 104.0, "tp2_price": 106.0, "qty": 50, "highest_price": 100.0
    }
    mgr.register_trade("INFY", trade)
    
    # Simulate SL hit
    res = mgr.evaluate_exits("INFY", cur_price=97.5, arrays=None)
    assert res is not None
    assert "STOP_LOSS_HIT" in res["reason"]
