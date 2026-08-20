"""
Unit and Integration Tests for STRATOS: Classical Market Structure and Support Flip Engine.
"""
import pytest
import numpy as np
from datetime import time
from STRATOS.candle_resampler import MultiTimeframeResampler, SymbolCandleSeries
from STRATOS.structure_engine import MarketStructureEngine
from STRATOS.pullback_analyzer import PullbackAnalyzer
from STRATOS.flip_trigger import FlipTriggerEngine, is_bullish_engulfing, is_bullish_hammer
from STRATOS.anti_hunt_risk import AntiHuntRiskEngine
from STRATOS.trade_manager import ActiveTradeManager
from STRATOS.orchestrator import StratosOrchestrator

def test_candle_resampler():
    resampler = MultiTimeframeResampler(["RELIANCE"])
    t0 = 1700000000.0
    
    # Send 10 ticks over 10 minutes (600 seconds)
    for i in range(10):
        t = t0 + i * 60.0
        price = 2500.0 + i * 2.0
        vol = (i + 1) * 100.0
        events = resampler.on_tick("RELIANCE", t, price, vol, True)
        
    arr_5m = resampler.get_arrays("RELIANCE", "5m")
    assert arr_5m is not None
    assert arr_5m["count"] >= 2
    assert arr_5m["close"][-1] > 2500.0

def test_structure_engine_breakout():
    engine = MarketStructureEngine()
    
    highs = np.array([95, 100, 96, 92, 90, 93, 105, 110, 106, 97, 95, 99, 112, 115, 116], dtype=np.float64)
    lows = np.array([88, 92, 89, 87, 85, 88, 98, 102, 98, 93, 91, 94, 108, 110, 111], dtype=np.float64)
    closes = np.array([94, 98, 91, 90, 89, 92, 104, 108, 99, 94, 93, 98, 111, 115, 116], dtype=np.float64)
    vols = np.full(len(closes), 50000.0)

    arrays_15m = {"high": highs, "low": lows, "close": closes, "volume": vols, "count": len(closes)}
    arrays_5m = {"high": highs, "low": lows, "close": closes, "volume": vols, "count": len(closes)}
    
    res = engine.update_structure("TCS", arrays_15m, arrays_5m)
    assert res["status"] in ["AWAITING_RETEST", "NO_CLEAN_BREAKOUT"]

def test_pullback_analyzer():
    analyzer = PullbackAnalyzer()
    
    # Low-volume shallow pullback
    highs = np.array([105, 104, 103, 102, 101], dtype=np.float64)
    lows = np.array([102, 101, 100.5, 100.1, 100.0], dtype=np.float64)
    closes = np.array([103, 102, 101, 100.5, 100.2], dtype=np.float64)
    volumes = np.array([2000, 1800, 1500, 1200, 1000], dtype=np.float64) # Drying volume
    
    arrays_5m = {"high": highs, "low": lows, "close": closes, "volume": volumes, "count": 5}
    res = analyzer.analyze_pullback_quality(arrays_5m, support_level=100.0, breakout_vol=10000.0)
    
    assert res["valid"] is True
    assert res["vol_ratio"] < 0.70

def test_candlestick_patterns():
    # Test Bullish Engulfing
    opens = np.array([102.0, 100.0])
    closes = np.array([100.0, 103.0])
    assert is_bullish_engulfing(opens, closes) is True
    
    # Test Bullish Hammer
    opens_h = np.array([101.0])
    highs_h = np.array([101.2])
    lows_h = np.array([98.0])   # Long lower shadow
    closes_h = np.array([101.1])
    assert is_bullish_hammer(opens_h, highs_h, lows_h, closes_h) is True

def test_anti_hunt_risk():
    risk = AntiHuntRiskEngine()
    highs = np.linspace(100, 105, 20)
    lows = np.linspace(98, 103, 20)
    closes = np.linspace(99, 104, 20)
    arrays_5m = {"high": highs, "low": lows, "close": closes}
    
    levels = risk.calculate_trade_levels(entry_price=104.0, support_level=100.0, rejection_low=99.8, arrays_5m=arrays_5m)
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
    res = mgr.evaluate_exits("INFY", cur_price=97.5, arrays_5m=None)
    assert res is not None
    assert "STOP_LOSS_HIT" in res["reason"]
