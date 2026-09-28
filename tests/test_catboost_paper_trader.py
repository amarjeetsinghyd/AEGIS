"""
Unit tests for CatBoost Standalone Paper Trader & ML Gatekeeper Integration.
"""

import time
import os
import shutil
import pytest
from AEGIS.core.catboost_paper_trader import CatBoostPaperTrader, ActivePaperTrade
from AEGIS.core.ml_gatekeeper import MLGatekeeper
from AEGIS.core.price_action_engine import OHLCBar


def test_ohlcbar_ts_property():
    bar = OHLCBar(
        ts_start=100.0,
        ts_end=160.0,
        open=74000.0,
        high=74020.0,
        low=73990.0,
        close=74015.0,
    )
    assert bar.ts == 160.0
    assert bar.ts_end == 160.0


def test_catboost_paper_trader_entry_and_exit(tmp_path):
    test_dir = str(tmp_path / "telemetry")
    trader = CatBoostPaperTrader(log_dir=test_dir, tp_pct=0.04, sl_pct=0.03, max_hold_sec=300.0, min_score=0.40)

    strike_to_symbol = {(0, 74000.0): "SENSEX26SEP74000CE", (1, 74000.0): "SENSEX26SEP74000PE"}
    option_cache = {"SENSEX26SEP74000CE": 25.0, "SENSEX26SEP74000PE": 30.0}

    now = 1000.0
    features = {"opt_delta_flow": 5000.0, "ret_1m_bps": 5.0}

    # 1. Score below threshold -> No Trade
    trade = trader.on_1m_bar(
        bar_ts=now,
        score=0.35,
        features=features,
        day_median=0.30,
        spot_px=74010.0,
        atm_strike=74000.0,
        strike_to_symbol=strike_to_symbol,
        option_cache=option_cache,
    )
    assert trade is None
    assert trader.active_trade is None

    # 2. Score >= threshold (0.45 >= 0.40 and >= day_median 0.30) -> Enters Paper Trade!
    trade = trader.on_1m_bar(
        bar_ts=now,
        score=0.45,
        features=features,
        day_median=0.30,
        spot_px=74010.0,
        atm_strike=74000.0,
        strike_to_symbol=strike_to_symbol,
        option_cache=option_cache,
    )
    assert trade is not None
    assert trade.symbol == "SENSEX26SEP74000CE"
    assert trade.entry_px == 25.0
    assert trade.target_px == 26.0  # +4%
    assert trade.sl_px == 24.25     # -3%
    assert trader.active_trade is not None

    # 3. Tick update: Price moves to 25.5 (+2.0%) -> In-flight update
    exit_res = trader.on_option_tick("SENSEX26SEP74000CE", 25.5, now + 10.0)
    assert exit_res is None
    assert trader.active_trade.mfe_pct == 2.0

    # 4. Tick update: Price reaches 26.1 (+4.4%) -> Target Hit!
    exit_res = trader.on_option_tick("SENSEX26SEP74000CE", 26.1, now + 25.0)
    assert exit_res is not None
    assert exit_res["exit_reason"].startswith("TARGET_HIT")
    assert exit_res["pnl_pct"] == 4.4
    assert trader.active_trade is None
    assert trader.wins == 1
    assert trader.total_trades == 1


def test_catboost_paper_trader_stop_loss(tmp_path):
    test_dir = str(tmp_path / "telemetry")
    trader = CatBoostPaperTrader(log_dir=test_dir, tp_pct=0.04, sl_pct=0.03, max_hold_sec=300.0, min_score=0.40)

    strike_to_symbol = {(1, 74000.0): "SENSEX26SEP74000PE"}
    option_cache = {"SENSEX26SEP74000PE": 50.0}

    now = 2000.0
    features = {"opt_delta_flow": -5000.0, "ret_1m_bps": -5.0}

    # Score 0.48 -> Enter PE
    trade = trader.on_1m_bar(
        bar_ts=now,
        score=0.48,
        features=features,
        day_median=0.35,
        spot_px=73990.0,
        atm_strike=74000.0,
        strike_to_symbol=strike_to_symbol,
        option_cache=option_cache,
    )
    assert trade is not None
    assert trade.direction == "DOWN"
    assert trade.symbol == "SENSEX26SEP74000PE"

    # Price drops to 48.0 (-4.0% <= -3.0% SL) -> Stopped out!
    exit_res = trader.on_option_tick("SENSEX26SEP74000PE", 48.0, now + 15.0)
    assert exit_res is not None
    assert "STOP_LOSS_HIT" in exit_res["exit_reason"]
    assert trader.losses == 1
    assert trader.active_trade is None


def test_ml_gatekeeper_triggers_paper_trader():
    gk = MLGatekeeper()
    assert gk.paper_trader is not None

    gk.strike_to_symbol = {(0, 74000.0): "SENSEX26SEP74000CE"}
    gk.option_cache = {"SENSEX26SEP74000CE": 20.0}
    gk.get_spot = lambda: 74005.0
    gk.get_atm = lambda: 74000.0

    sample_feats = {
        "put_aggression": -0.5,
        "call_buy_vol": 500000.0,
        "call_aggression": 0.8,
        "avg_book_imb": 0.3,
        "upper_wick_bps": 1.0,
        "ema20_dist_bps": 8.0,
        "lower_wick_bps": 0.5,
        "opt_flow_ratio": 3.0,
        "range_bps": 12.0,
        "body_bps": 10.0,
        "put_buy_vol": 50000.0,
        "opt_delta_3m": 450000.0,
        "ema5_dist_bps": 5.0,
        "opt_total_vol": 600000.0,
        "opt_pcr": 0.2,
        "opt_delta_flow": 450000.0,
        "ret_1m_bps": 8.0,
    }

    now = time.time()
    score = gk.record_1m_bar(now, sample_feats, index_name="SENSEX")
    assert len(gk.recent_scores) == 1
    assert score > 0
