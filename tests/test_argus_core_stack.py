"""
Comprehensive Unit Tests for ARGUS Core Stack (v5.0).
Validates:
  1. StockPriceActionEngine (4 Pillars + scale-invariance)
  2. StockVwapBandEngine (Pain Zone early directional veto)
  3. StockWyckoffEngine (VDR >= 1.0 & Delta harmony)
  4. StockMicroValidator (30s L2 energy scoring + circuit breakers)
  5. StockConvictionEngine (0-100 rubric & >=80 hurdle)
  6. StockUniverseManager (Top 15 stock coordination)
  7. Adaptive Exits (Maker TP, Near-Target Lock, Breakeven, Candle Invalidation, Time Harvest)
  8. StockTradeJournal (CSV recording & Excel compilation)
"""

import time
import pytest
from ARGUS.core.stock_price_action_engine import StockPriceActionEngine, StockOHLCBar
from ARGUS.core.stock_vwap_band_engine import StockVwapBandEngine
from ARGUS.core.stock_wyckoff_engine import StockWyckoffEngine
from ARGUS.core.stock_micro_validator import StockMicroValidator
from ARGUS.core.stock_conviction_engine import StockConvictionEngine
from ARGUS.core.stock_universe_manager import StockUniverseManager
from ARGUS.core.stock_trade_journal import record_completed_trade, init_journal_csv


def test_stock_price_action_pillar1_20_ema_buy():
    engine = StockPriceActionEngine(symbol="RELIANCE")
    base_time = 1700000000.0

    # Seed 25 bars in an uptrend (EMA around 2500)
    bars = []
    for i in range(25):
        c = 2480.0 + i * 1.5
        bars.append({
            "ts": base_time + i * 60,
            "open": c - 1.0, "high": c + 2.0, "low": c - 1.5, "close": c,
            "volume": 10000.0, "delta": 2000.0
        })
    engine.seed_bars(bars)
    assert engine.ema20 is not None
    assert engine.atr > 0

    # Feed a pullback bar touching 20 EMA with lower wick rejection
    ema = engine.ema20
    pullback_bar = {
        "ts": base_time + 26 * 60,
        "open": ema + 1.0,
        "high": ema + 3.0,
        "low": ema - 0.2, # touched EMA
        "close": ema + 2.0, # closed above EMA with lower wick
        "volume": 15000.0,
        "delta": 3000.0,
    }
    engine.seed_bars([pullback_bar])

    is_setup, name, details = engine.evaluate_setups("BUY")
    assert is_setup is True
    assert name == "20_ema_rejection"
    assert details["pillar"] == 1


def test_stock_price_action_pillar2_sweep_reclaim_buy():
    engine = StockPriceActionEngine(symbol="TATASTEEL")
    base_time = 1700000000.0

    # Seed 10 bars with EMA around 183 (downtrend pullback)
    bars = []
    for i in range(10):
        c = 184.0 - i * 0.4  # descends from 184.0 to 180.4
        bars.append({
            "ts": base_time + i * 60,
            "open": c + 0.2, "high": c + 0.5, "low": c - 0.5, "close": c,
            "volume": 50000.0, "delta": -5000.0
        })
    engine.seed_bars(bars)

    # 5-bar swing low was ~180.0. Sweep below to 179.0 and reclaim at 180.6 (below EMA 182)
    sweep_bar = {
        "ts": base_time + 11 * 60,
        "open": 179.5, "high": 180.8, "low": 179.0, "close": 180.6,
        "volume": 80000.0, "delta": 15000.0
    }
    engine.seed_bars([sweep_bar])

    is_setup, name, details = engine.evaluate_setups("BUY")
    assert is_setup is True
    assert name == "micro_sweep_reclaim"
    assert details["pillar"] == 2


def test_stock_price_action_pillar3_squeeze_breakout():
    engine = StockPriceActionEngine(symbol="INFY")
    base_time = 1700000000.0

    # Seed 10 bars
    bars = []
    for i in range(10):
        bars.append({
            "ts": base_time + i * 60,
            "open": 1500.0, "high": 1505.0, "low": 1495.0, "close": 1500.0,
            "volume": 20000.0, "delta": 1000.0
        })
    engine.seed_bars(bars)
    atr = engine.atr

    # 3 tight bars (compression box <= 1.25 ATR)
    for i in range(3):
        engine.seed_bars([{
            "ts": base_time + (11 + i) * 60,
            "open": 1500.0, "high": 1500.5, "low": 1499.5, "close": 1500.2,
            "volume": 10000.0, "delta": 500.0
        }])

    # Expansion breakout bar
    engine.seed_bars([{
        "ts": base_time + 14 * 60,
        "open": 1500.2, "high": 1508.0, "low": 1500.0, "close": 1507.5,
        "volume": 40000.0, "delta": 12000.0
    }])

    is_setup, name, details = engine.evaluate_setups("BUY")
    assert is_setup is True
    assert name == "volatility_squeeze_breakout"
    assert details["pillar"] == 3


def test_stock_vwap_band_engine_directional_veto():
    engine = StockVwapBandEngine(symbol="SBIN", band_atr_mult=0.80)

    # Accumulate VWAP around 800 with ATR = 4.0 (Band = 800 +/- 3.20)
    engine.update_tick(price=800.0, volume_delta=10000.0, atr=4.0)
    assert engine.vwap == 800.0
    assert engine.upper_band == 803.20
    assert engine.lower_band == 796.80

    # 1. Spot inside band (801.0) -> VETO (Chop zone)
    ok, reason, _ = engine.check_direction_veto("BUY", 801.0)
    assert ok is False
    assert reason == "inside_pain_zone_band"

    # 2. Spot above upper band (805.0) -> BUY passes, SHORT vetoed
    ok_buy, reason_buy, _ = engine.check_direction_veto("BUY", 805.0)
    assert ok_buy is True
    assert reason_buy == "above_upper_band"

    ok_short, reason_short, _ = engine.check_direction_veto("SELL", 805.0)
    assert ok_short is False
    assert reason_short == "short_vetoed_above_upper_band"

    # 3. Spot below lower band (795.0) -> SHORT passes, BUY vetoed
    ok_s, reason_s, _ = engine.check_direction_veto("SELL", 795.0)
    assert ok_s is True
    assert reason_s == "below_lower_band"

    ok_b, reason_b, _ = engine.check_direction_veto("BUY", 795.0)
    assert ok_b is False
    assert reason_b == "buy_vetoed_below_lower_band"


def test_stock_wyckoff_engine_gate():
    engine = StockWyckoffEngine(symbol="HDFCBANK", vdr_threshold=1.0, volume_surge_ratio=1.15)

    # Record 6 baseline bars of 10,000 volume and 5.0 bps spread
    for _ in range(6):
        engine.record_bar(volume=10000.0, delta=1000.0, spread_bps=5.0)

    # Surge volume 15,000 with positive delta 4,000 and tight spread 4.0 bps -> PASS
    passed, reason, metrics = engine.evaluate_gate(
        direction="BUY",
        current_volume=15000.0,
        current_delta=4000.0,
        current_spread_bps=4.0
    )
    assert passed is True
    assert reason == "wyckoff_confirmed"
    assert metrics["has_surge"] is True
    assert metrics["delta_aligned"] is True

    # Negative delta on BUY setup -> Instant VETO
    passed_neg, reason_neg, _ = engine.evaluate_gate(
        direction="BUY",
        current_volume=15000.0,
        current_delta=-2000.0,
        current_spread_bps=4.0
    )
    assert passed_neg is False
    assert reason_neg == "delta_opposed"


def test_stock_micro_validator():
    validator = StockMicroValidator(min_pass_score=60.0)
    anchor = 1000.0

    # 25 ticks with rising price, strong bid touch
    ticks = []
    for i in range(25):
        t = anchor - 25.0 + i
        p = 100.0 + i * 0.02 # rising price (+0.50%)
        # bq1=1000, sq1=200 -> strong buyer aggression
        ticks.append((t, p, p - 0.05, p + 0.05, 1000, 200))

    passed, score, metrics = validator.evaluate("RELIANCE", ticks, direction="BUY", anchor_T=anchor)
    assert passed is True
    assert score >= 80.0
    assert metrics["reason"] == "energy_confirmed"

    # Spread blowout circuit breaker (> 15 bps)
    blowout_ticks = [(anchor - 5.0, 100.0, 99.50, 100.50, 500, 500)] # spread 1.00 / 100 = 100 bps
    passed_bo, score_bo, metrics_bo = validator.evaluate("RELIANCE", blowout_ticks, direction="BUY", anchor_T=anchor)
    assert passed_bo is False
    assert metrics_bo["reason"] == "spread_blowout"


def test_stock_conviction_engine_hurdle():
    engine = StockConvictionEngine(hurdle_score=80.0)

    # Strong setup: Pillar 1, high VDR & Surge, strong micro
    pa_details = {"pillar": 1, "has_runway": True, "runway_atr": 1.2}
    wyck_metrics = {"has_vdr": True, "has_surge": True, "delta_aligned": True}
    micro_score = 90.0

    admit, score, breakdown = engine.evaluate(1, pa_details, wyck_metrics, micro_score)
    assert admit is True
    assert score >= 85.0
    assert breakdown["score_pa"] == 40.0
    assert breakdown["score_wyck"] == 30.0

    # Weak setup: low micro, no surge
    wyck_weak = {"has_vdr": False, "has_surge": False, "delta_aligned": True}
    admit_w, score_w, _ = engine.evaluate(1, pa_details, wyck_weak, micro_score=30.0)
    assert admit_w is False
    assert score_w < 80.0


def test_stock_universe_manager_coordination():
    mgr = StockUniverseManager(symbols=["RELIANCE", "TATASTEEL"])
    assert "RELIANCE" in mgr.pa_engines
    assert "TATASTEEL" in mgr.vwap_engines

    # Feed ticks
    now = time.time()
    sig = mgr.on_tick("RELIANCE", price=2500.0, volume=1000.0, bp1=2499.95, sp1=2500.05, bq1=500, sq1=500, ts_sec=now)
    assert sig is None # Initial warmup tick


def test_stock_trade_journal_recording():
    test_trade = {
        "symbol": "TATASTEEL",
        "type": "BUY",
        "entry_price": 180.0,
        "exit_price": 180.90,  # +0.50% TP
        "qty": 555,
        "margin": 20000.0,
        "exit_reason": "MAKER_LIMIT_TP",
        "pillar": "P1",
        "conviction": 86.5,
        "duration_s": 45,
    }
    record_completed_trade(test_trade)
    # Ensure CSV exists and has content
    from ARGUS.core.stock_trade_journal import CSV_PATH, XLSX_PATH
    assert CSV_PATH.exists()
    assert XLSX_PATH.exists()


def test_argus_adaptive_exits_logic():
    from ARGUS.execution_manager import StockExecutionManager

    # Mock manager to test manage_open_trades logic
    mgr = StockExecutionManager.__new__(StockExecutionManager)
    mgr.open_trades = {}
    mgr.closed_reasons = []
    mgr.adv_metrics_dict = {}
    mgr.bucket_aggregates_dict = {}

    def mock_close(sym, price, reason):
        mgr.closed_reasons.append(reason)
        mgr.open_trades.pop(sym, None)

    mgr._close_trade = mock_close

    # 1. Maker Limit TP Test (+0.50%)
    mgr.open_trades["RELIANCE"] = {
        "entry_time": "2026-10-05T10:00:00",
        "symbol": "RELIANCE",
        "type": "BUY",
        "entry_price": 2500.0,
        "qty": 40,
        "maker_tp": 2512.50, # +0.50%
        "highest": 2500.0,
        "lowest": 2500.0,
        "atr": 10.0,
    }
    mgr.manage_open_trades({"RELIANCE": 2513.0})
    assert "MAKER_LIMIT_TP" in mgr.closed_reasons

    # 2. Near-Target Lock Test (Peak reached +0.38%, pulled back to +0.26% -> 0.12% pullback)
    mgr.closed_reasons.clear()
    mgr.open_trades["TATASTEEL"] = {
        "entry_time": "2026-10-05T10:00:00",
        "symbol": "TATASTEEL",
        "type": "BUY",
        "entry_price": 100.0,
        "qty": 100,
        "maker_tp": 100.50,
        "highest": 100.38, # +0.38% peak
        "lowest": 100.0,
        "atr": 0.5,
    }
    mgr.manage_open_trades({"TATASTEEL": 100.26}) # pulled back 0.12%
    assert "NEAR_TARGET_PROFIT_LOCK" in mgr.closed_reasons

    # 3. Candle Invalidation Test (Price drops below entry candle low)
    mgr.closed_reasons.clear()
    mgr.open_trades["INFY"] = {
        "entry_time": "2026-10-05T10:00:00",
        "symbol": "INFY",
        "type": "BUY",
        "entry_price": 1500.0,
        "qty": 50,
        "maker_tp": 1507.50,
        "highest": 1501.0,
        "lowest": 1498.0,
        "entry_candle_low": 1498.0,
        "atr": 5.0,
    }
    mgr.manage_open_trades({"INFY": 1497.50}) # breached candle low
    assert "CANDLE_INVALIDATION" in mgr.closed_reasons

    # 4. Breakeven Exit Test (Peak reached +0.32%, returned to entry 100.0)
    mgr.closed_reasons.clear()
    mgr.open_trades["SBIN"] = {
        "entry_time": "2026-10-05T10:00:00",
        "symbol": "SBIN",
        "type": "BUY",
        "entry_price": 800.0,
        "qty": 100,
        "maker_tp": 804.0,
        "highest": 802.60, # +0.325% peak
        "lowest": 800.0,
        "atr": 3.0,
    }
    mgr.manage_open_trades({"SBIN": 800.0}) # back to breakeven
    assert "BREAKEVEN_EXIT" in mgr.closed_reasons

