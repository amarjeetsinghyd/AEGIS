import pytest
from datetime import date, datetime, timedelta
from AEGIS.core.price_action_engine import PriceActionEngine, StructuralLevel, OHLCBar
from common.market.market_calendar import get_previous_trading_day, is_exchange_trading_day, NSE_HOLIDAYS_2026


def test_get_previous_trading_day():
    # 2026-10-26 is Monday -> previous trading day should be Friday 2026-10-23
    mon = date(2026, 10, 26)
    assert get_previous_trading_day(mon) == date(2026, 10, 23)

    # 2026-10-27 is Tuesday -> previous trading day should be Monday 2026-10-26
    tue = date(2026, 10, 27)
    assert get_previous_trading_day(tue) == date(2026, 10, 26)

    # 2026-10-28 is Wednesday -> previous trading day should be Tuesday 2026-10-27
    wed = date(2026, 10, 28)
    assert get_previous_trading_day(wed) == date(2026, 10, 27)

    # Dussehra 2026 is on Tuesday 2026-10-20 (in NSE_HOLIDAYS_2026)
    # Wednesday 2026-10-21 -> previous trading day should skip Tuesday and go to Monday 2026-10-19
    wed_after_holiday = date(2026, 10, 21)
    assert get_previous_trading_day(wed_after_holiday) == date(2026, 10, 19)


def test_seed_previous_day_5m_bars():
    pa = PriceActionEngine()

    # Generate 75 5-minute candles (09:15 to 15:30 = 375 minutes = 75 bars)
    base_ts = datetime(2026, 10, 26, 9, 15, 0).timestamp()
    candles_5m = []

    price = 24000.0
    for i in range(75):
        ts = base_ts + (i * 300)
        # Multiple distinct swings:
        # 1. Swing High 1 at i=15 (~24060)
        # 2. Swing Low 1 at i=30 (~23980)
        # 3. Macro Peak (PDH) at i=45 (~24120)
        # 4. Macro Trough (PDL) at i=60 (~23920)
        if 0 <= i <= 15:
            price += 4.0
        elif 16 <= i <= 30:
            price -= 5.0
        elif 31 <= i <= 45:
            price += 9.0
        elif 46 <= i <= 60:
            price -= 13.0
        else:
            price += 5.0

        o = price - 2.0
        h = price + 6.0
        l = price - 6.0
        c = price
        v = 10000.0 + (i * 100)

        candles_5m.append({
            "timestamp": ts,
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": v,
        })

    res = pa.seed_previous_day_5m_bars(candles_5m)

    assert res["levels_discovered"] >= 2
    assert res["pdh"] > 0
    assert res["pdl"] > 0
    assert res["pdc"] > 0
    assert pa.pdh == res["pdh"]
    assert pa.pdl == res["pdl"]
    assert pa.pdc == res["pdc"]

    # Verify levels are registered in session_levels and _previous_day_levels
    assert len(pa._previous_day_levels) >= 2
    for lvl in pa._previous_day_levels:
        assert lvl.is_previous_day is True
        assert lvl.prominence >= 1.25


def test_level_clustering_and_merging():
    # Test Resistance clustering: within cluster_tol, picks highest peak (roof) and sums touches
    l1 = StructuralLevel(level_type="RESISTANCE", price=24050.0, ts_created=100.0, prominence=1.8, bar_idx=0, touch_count=1, volume=5000.0, timeframe="5m")
    l2 = StructuralLevel(level_type="RESISTANCE", price=24055.0, ts_created=200.0, prominence=2.0, bar_idx=0, touch_count=2, volume=6000.0, timeframe="15m")
    l_far = StructuralLevel(level_type="RESISTANCE", price=24150.0, ts_created=300.0, prominence=2.2, bar_idx=0, touch_count=1, volume=4000.0, timeframe="5m")

    cluster_tol = 10.0  # 10 pts
    merged = PriceActionEngine._cluster_and_merge_levels([l1, l2, l_far], cluster_tol)

    assert len(merged) == 2  # l1 and l2 merged into 1, l_far remains separate
    # Find merged cluster
    c_merged = [l for l in merged if l.price < 24100.0][0]
    assert c_merged.price == 24055.0  # highest roof
    assert c_merged.touch_count == 3  # 1 + 2
    assert c_merged.volume == 11000.0  # 5000 + 6000
    assert c_merged.timeframe == "15m"  # 15m confluence dominant
    assert c_merged.prominence == 2.35  # max(1.8, 2.0) + 0.35 boost

    # Test Support clustering: within cluster_tol, picks lowest trough (floor)
    s1 = StructuralLevel(level_type="SUPPORT", price=23955.0, ts_created=100.0, prominence=1.9, bar_idx=0, touch_count=1, volume=4000.0, timeframe="5m")
    s2 = StructuralLevel(level_type="SUPPORT", price=23950.0, ts_created=200.0, prominence=2.1, bar_idx=0, touch_count=1, volume=5000.0, timeframe="5m")

    merged_s = PriceActionEngine._cluster_and_merge_levels([s1, s2], cluster_tol)
    assert len(merged_s) == 1
    assert merged_s[0].price == 23950.0  # lowest floor
    assert merged_s[0].touch_count == 2
    assert merged_s[0].volume == 9000.0


def test_previous_day_levels_persist_across_day_reset():
    pa = PriceActionEngine()

    # Seed mock previous day level
    prev_lvl = StructuralLevel(
        level_type="RESISTANCE",
        price=24100.0,
        ts_created=1000.0,
        prominence=2.5,
        bar_idx=0,
        touch_count=2,
        timeframe="15m",
        is_previous_day=True,
    )
    pa._previous_day_levels = [prev_lvl]
    pa.session_levels.append(prev_lvl)

    # Simulate next day bar
    next_day_ts = datetime(2026, 10, 27, 9, 15, 0).timestamp()
    bar = OHLCBar(
        ts_start=next_day_ts,
        ts_end=next_day_ts + 60.0,
        open=24000.0,
        high=24010.0,
        low=23990.0,
        close=24005.0,
        volume=1000.0,
    )
    pa._on_1m_bar_close(bar)

    # Verify that the unbroken previous-day level is still present in session_levels!
    found = [l for l in pa.session_levels if l.price == 24100.0 and l.level_type == "RESISTANCE"]
    assert len(found) == 1
    assert found[0].is_previous_day is True


def test_mode_b_breakthrough_on_previous_day_level():
    pa = PriceActionEngine()
    now_ts = datetime(2026, 10, 27, 9, 25, 0).timestamp()

    # Seed previous day resistance level
    res_lvl = StructuralLevel(
        level_type="RESISTANCE",
        price=24100.0,
        ts_created=now_ts - 86400.0,
        prominence=2.5,
        bar_idx=0,
        seq_idx=0,
        touch_count=2,
        timeframe="15m",
        is_previous_day=True,
    )
    pa.session_levels.append(res_lvl)
    pa._total_bars_seen = 10
    pa.current_atr_1m = 12.0
    pa.current_vwap = 24050.0

    # Simulate 5m trend bullish
    pa.bars_5m.append(OHLCBar(ts_start=now_ts - 300, ts_end=now_ts, open=24060.0, high=24090.0, low=24055.0, close=24085.0))

    # Form in-flight bar crossing 24100
    pa._curr_1m_bar = OHLCBar(
        ts_start=now_ts,
        ts_end=now_ts + 60.0,
        open=24090.0,
        high=24106.0,
        low=24088.0,
        close=24106.0,
        volume=500,
        tick_count=10,
    )
    pa._opt_flow_1m = {
        "call_buy_vol": 800.0,
        "call_sell_vol": 100.0,
        "put_buy_vol": 100.0,
        "put_sell_vol": 100.0,
    }

    # Single-shot instant breakthrough on breach
    pa.last_price = 24106.0
    ok, reason, details = pa.check_mode_b_breakthrough("UP", current_time=now_ts + 15.0)
    assert ok is True
    assert reason == "sr_breakout"
    assert details["boundary_level"] == 24100.0
    assert res_lvl.is_broken is True


def test_pillar2_sweep_maps_previous_day_level():
    pa = PriceActionEngine()
    now_ts = datetime(2026, 10, 27, 9, 30, 0).timestamp()

    # Seed previous day support level
    sup_lvl = StructuralLevel(
        level_type="SUPPORT",
        price=24000.0,
        ts_created=now_ts - 86400.0,
        prominence=2.5,
        bar_idx=0,
        seq_idx=0,
        touch_count=2,
        timeframe="15m",
        is_previous_day=True,
    )
    pa.session_levels.append(sup_lvl)

    # Bar plunges below 24000 to 23992 and reclaims above 24000 to close at 24004
    sweep_bar = OHLCBar(
        ts_start=now_ts,
        ts_end=now_ts + 60.0,
        open=24005.0,
        high=24010.0,
        low=23992.0,
        close=24004.0,
        volume=1200.0,
    )

    matched = pa._find_swept_sr_level(
        direction="CALL",
        bar=sweep_bar,
        min_sweep_depth=2.0,
        atr=15.0,
    )

    assert matched is not None
    assert matched.price == 24000.0
    assert matched.level_type == "SUPPORT"
    assert matched.is_previous_day is True


def test_interior_cluster_level_vetoed_on_breakout():
    pa = PriceActionEngine()
    now_ts = datetime(2026, 10, 27, 10, 0, 0).timestamp()

    # Create two resistances:
    # 1. Interior cluster at 24080 (only 10 pts below outermost roof 24090; 10 < 1.25 * ATR(15) = 18.75)
    # 2. Outermost roof at 24090
    res_int = StructuralLevel(
        level_type="RESISTANCE",
        price=24080.0,
        ts_created=now_ts - 86400.0,
        prominence=1.5,
        bar_idx=0,
        seq_idx=0,
        touch_count=1,
        timeframe="5m",
        is_previous_day=True,
    )
    res_outer = StructuralLevel(
        level_type="RESISTANCE",
        price=24090.0,
        ts_created=now_ts - 86400.0,
        prominence=2.5,
        bar_idx=0,
        seq_idx=0,
        touch_count=3,
        timeframe="15m",
        is_previous_day=True,
    )

    pa.session_levels = [res_int, res_outer]
    pa.current_atr_1m = 15.0
    pa.pdc = 24000.0
    pa.last_price = 24050.0
    pa._classify_level_roles()

    # Verify classification
    assert res_int.level_role == "INTERIOR_CLUSTER"
    assert res_int.is_tradeable_breakout is False
    assert res_int.breakout_tier == "B_SWEEP_ONLY"

    assert res_outer.level_role == "OUTERMOST_ROOF"
    assert res_outer.is_tradeable_breakout is True
    assert res_outer.breakout_tier == "A_PLUS"

    # Setup environment for Mode B
    pa._total_bars_seen = 10
    pa.current_vwap = 24040.0
    pa.bars_5m.append(OHLCBar(ts_start=now_ts - 300, ts_end=now_ts, open=24040.0, high=24085.0, low=24035.0, close=24082.0))
    pa._opt_flow_1m = {
        "call_buy_vol": 900.0,
        "call_sell_vol": 100.0,
        "put_buy_vol": 50.0,
        "put_sell_vol": 50.0,
    }

    # Case 1: Spot breaks ONLY interior cluster at 24080 (spot = 24084 < 24090)
    pa._curr_1m_bar = OHLCBar(
        ts_start=now_ts,
        ts_end=now_ts + 60.0,
        open=24075.0,
        high=24084.0,
        low=24074.0,
        close=24084.0,
        volume=600,
        tick_count=10,
    )
    pa.last_price = 24084.0
    ok_int, reason_int, det_int = pa.check_mode_b_breakthrough("UP", current_time=now_ts + 20.0)
    assert ok_int is False
    assert reason_int == "no_mode_b_trigger"
    assert det_int == {}

    # Case 2: Spot breaks the OUTERMOST ROOF at 24090 (spot = 24095 > 24090)
    pa._curr_1m_bar = OHLCBar(
        ts_start=now_ts,
        ts_end=now_ts + 60.0,
        open=24075.0,
        high=24095.0,
        low=24074.0,
        close=24095.0,
        volume=1200,
        tick_count=10,
    )
    pa.last_price = 24095.0
    ok2, r2, det2 = pa.check_mode_b_breakthrough("UP", current_time=now_ts + 25.0)
    assert ok2 is True
    assert r2 == "sr_breakout"
    assert det2["boundary_level"] == 24090.0
    assert det2["level_role"] == "OUTERMOST_ROOF"
    assert det2["breakout_tier"] == "A_PLUS"
    assert res_outer.is_broken is True

