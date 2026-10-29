from datetime import datetime

from AEGIS.core.price_action_engine import OHLCBar, PriceActionEngine


def _rest_candle(ts_start, close=24010.0):
    return {
        "time": datetime.fromtimestamp(ts_start).strftime("%d-%m-%Y %H:%M:%S"),
        "into": close - 1.0,
        "inth": close + 1.0,
        "intl": close - 2.0,
        "intc": close,
        "intv": 12,
    }


def _engine_with_last_closed_bar():
    engine = PriceActionEngine(atr_period=2)
    session_open = datetime.now().replace(hour=9, minute=15, second=0, microsecond=0).timestamp()
    engine._on_1m_bar_close(
        OHLCBar(
            ts_start=session_open,
            ts_end=session_open + 60.0,
            open=24000.0,
            high=24002.0,
            low=23999.0,
            close=24001.0,
            tick_count=10,
        )
    )
    return engine, session_open


def test_backfill_appends_closed_bars_once_without_option_flow():
    engine, start = _engine_with_last_closed_bar()
    current_ts = start + 6 * 60.0 + 15.0
    candles = [_rest_candle(start + offset * 60.0) for offset in range(1, 7)]
    candles.append(_rest_candle(start + 2 * 60.0))

    added = engine.backfill_missing_bars(candles, current_ts)

    assert added == 5
    assert [bar.ts_start for bar in list(engine.bars_1m)[-5:]] == [
        start + offset * 60.0 for offset in range(1, 6)
    ]
    assert all(bar.opt_total_vol == 0.0 for bar in list(engine.bars_1m)[-5:])
    assert all(bar.volume == 12.0 for bar in list(engine.bars_1m)[-5:])
    assert engine._total_bars_seen == 6
    assert engine.backfill_pending is False
    assert engine.backfill_missing_bars(candles, current_ts) == 0


def test_incomplete_backfill_does_not_append_partial_history_and_fails_closed():
    engine, start = _engine_with_last_closed_bar()
    current_ts = start + 6 * 60.0 + 15.0

    added = engine.backfill_missing_bars([_rest_candle(start + 60.0)], current_ts)

    assert added == 0
    assert len(engine.bars_1m) == 1
    assert engine.backfill_pending is True


def test_queued_backfill_is_applied_on_next_spot_tick():
    engine, start = _engine_with_last_closed_bar()
    current_ts = start + 3 * 60.0 + 15.0
    engine.queue_historical_backfill(
        [_rest_candle(start + 60.0), _rest_candle(start + 120.0)],
        current_ts,
    )

    engine.update_tick(24020.0, current_ts)

    assert len(engine.bars_1m) == 3
    assert engine.backfill_pending is False
    assert engine._curr_1m_bar is not None
    assert engine._curr_1m_bar.ts_start == float(int(current_ts // 60) * 60)
