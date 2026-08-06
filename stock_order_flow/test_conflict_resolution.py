"""Tests for SOF 3m bar book + conflict resolution."""
from datetime import datetime

import pandas as pd

from stock_order_flow.bar_book import ThreeMinuteBarBook, floor_3m
from stock_order_flow.evaluator import FootprintEvaluator


def test_floor_3m_aligns_to_grid():
    assert floor_3m(datetime(2026, 8, 6, 9, 15, 40)).minute == 15
    assert floor_3m(datetime(2026, 8, 6, 9, 17, 10)).minute == 15
    assert floor_3m(datetime(2026, 8, 6, 9, 18, 1)).minute == 18


def test_3m_bar_rolls_across_boundary():
    book = ThreeMinuteBarBook()
    tok = "2885"
    book.on_tick(tok, 100.0, ts=datetime(2026, 8, 6, 9, 15, 10))
    book.on_tick(tok, 102.0, ts=datetime(2026, 8, 6, 9, 16, 0))
    book.on_tick(tok, 101.0, ts=datetime(2026, 8, 6, 9, 18, 1), volume_delta=50)
    df = book.completed_df(tok, min_bars=1)
    assert df is not None and len(df) == 1
    assert float(df.iloc[0]["open"]) == 100.0
    assert float(df.iloc[0]["high"]) == 102.0
    assert float(df.iloc[0]["close"]) == 102.0


def _hist(n=20, close=100.0):
    ts = pd.date_range("2026-08-06 09:15", periods=n, freq="3min")
    closes = [close + i * 0.1 for i in range(n)]
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": closes,
            "high": [c + 0.2 for c in closes],
            "low": [c - 0.2 for c in closes],
            "close": closes,
            "volume": [1000] * n,
        }
    )


def test_conflict_without_tsf_history_still_works():
    ev = FootprintEvaluator()
    fp = {
        "total_buy_vol": 1000,
        "total_sell_vol": 200,
        "delta": 800,
        "levels": {100.0: {"buy": 500, "sell": 100}},
        "avg_l2_bid": 100,
        "avg_l2_ask": 500,
    }
    sig, reason = ev.evaluate_boundary(
        "RELIANCE", 100.0, None, fp, now=datetime(2026, 8, 6, 10, 0)
    )
    assert sig == "NEUTRAL"
    assert "ABSORB_CONFLICT" in reason


def test_t1_breakout_becomes_late_buy():
    ev = FootprintEvaluator()
    ev._conflicts["RELIANCE"] = {
        "kind": "ASK_WALL_VS_BUY_DELTA",
        "bar_ts": "2026-08-06 10:00",
        "price": 100.0,
        "poc": 100.0,
        "delta_pct": 0.2,
        "avg_l2_ask": 500,
        "avg_l2_bid": 100,
    }
    fp = {
        "total_buy_vol": 800,
        "total_sell_vol": 400,
        "delta": 400,
        "levels": {100.5: {"buy": 400, "sell": 100}},
        "avg_l2_bid": 200,
        "avg_l2_ask": 150,
    }
    sig, reason = ev.evaluate_boundary(
        "RELIANCE", 100.3, _hist(), fp, now=datetime(2026, 8, 6, 10, 3)
    )
    assert sig == "BUY"
    assert "LATE BREAKOUT BUY" in reason
