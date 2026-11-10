import pytest
from unittest.mock import MagicMock
from core.price_action_engine import PriceActionEngine, OHLCBar, StructuralLevel
from core.shadow_boundary_observer import ShadowBoundaryObserver, ShadowTrade


def test_shadow_observer_initialization():
    observer = ShadowBoundaryObserver(strike_step=50.0, log_dir="scratch")
    assert observer.strike_step == 50.0
    assert observer.active_trades == []
    assert observer.armed_setups == {}


def test_select_otm5_symbol():
    observer = ShadowBoundaryObserver(strike_step=50.0, log_dir="scratch")
    observer.current_atm_strike = 22500.0
    observer.strike_to_symbol = {
        (0, 22750.0): "NIFTY_22750_CE",
        (1, 22250.0): "NIFTY_22250_PE",
    }
    
    # CALL -> ATM + 5*50 = 22750 CE
    sym_call, k_call, side_call = observer.select_otm5_symbol("UP", 22510.0)
    assert sym_call == "NIFTY_22750_CE"
    assert k_call == 22750.0
    assert side_call == "CE"
    
    # PUT -> ATM - 5*50 = 22250 PE
    sym_put, k_put, side_put = observer.select_otm5_symbol("DOWN", 22490.0)
    assert sym_put == "NIFTY_22250_PE"
    assert k_put == 22250.0
    assert side_put == "PE"


def test_option_tick_updates_mfe_and_hits_tp():
    observer = ShadowBoundaryObserver(strike_step=50.0, log_dir="scratch")
    trade = ShadowTrade(
        trade_id="TEST_001",
        boundary="PAIN_ZONE_BAND",
        direction="UP",
        entry_ts=1000.0,
        entry_time_str="10:00:00",
        spot_entry=22500.0,
        spot_anchor_high=22505.0,
        spot_anchor_low=22485.0,
        spot_sl_price=22483.0,
        opt_symbol="NIFTY_22750_CE",
        opt_strike=22750.0,
        opt_side="CE",
        opt_entry=50.0,
        opt_tp_price=52.0,  # +4%
        opt_sl_price=44.0,  # -12%
    )
    observer.active_trades.append(trade)
    
    # 1. Option tick moves up to 51.0 (+2%)
    observer.on_option_tick("NIFTY_22750_CE", 51.0, 1010.0)
    assert trade.is_active is True
    assert trade.opt_peak_mfe_pct == pytest.approx(2.0)
    
    # 2. Option tick hits 52.1 (+4.2% -> TP Hit)
    observer.on_option_tick("NIFTY_22750_CE", 52.1, 1015.0)
    assert trade.is_active is False
    assert trade.outcome == "WINNER"
    assert trade.exit_reason == "TAKE_PROFIT_HIT"
    assert trade.duration_sec == pytest.approx(15.0)


def test_ema200_levels_and_live_slope():
    pa = PriceActionEngine(strike_step=50.0)
    pa.ema_200_1m = 22500.0
    pa.current_atr_1m = 10.0
    
    observer = ShadowBoundaryObserver(price_action_engine=pa, strike_step=50.0, log_dir="scratch")
    top, bottom = observer._get_ema200_levels(pa.current_atr_1m)
    assert top > bottom
    assert top == pytest.approx(pa.ema_200_1m + 2.5)
    assert bottom == pytest.approx(pa.ema_200_1m - 2.5)


def test_inflight_breakthrough_surge():
    mock_pae = MagicMock()
    # 13:43 prior red bar touching Pain Zone at 22510.0
    prior_bar = MagicMock()
    prior_bar.open = 22513.0
    prior_bar.high = 22514.0
    prior_bar.low = 22508.0
    prior_bar.close = 22509.0
    prior_bar.volume = 1000.0
    prior_bar.call_buy_vol = 1000.0
    prior_bar.put_buy_vol = 1000.0

    # 13:44 forming green bar breaking 13:43 High
    mock_bar = MagicMock()
    mock_bar.tick_count = 80
    mock_bar.open = 22509.5
    mock_bar.high = 22519.0
    mock_bar.low = 22509.0
    mock_bar.close = 22518.8
    mock_bar.volume = 1000.0
    mock_bar.call_buy_vol = 5000.0
    mock_bar.put_buy_vol = 1000.0
    mock_pae._curr_1m_bar = mock_bar
    mock_pae._opt_flow_1m = {
        "call_buy_vol": 5000.0,
        "call_sell_vol": 100.0,
        "put_buy_vol": 1000.0,
        "put_sell_vol": 100.0,
    }
    mock_pae.get_pain_zone_vwaps.return_value = {"zone_top": 22510.0, "zone_bottom": 22490.0}
    mock_pae.bars_1m = [prior_bar] * 25
    mock_pae.current_atr_1m = 10.0
    mock_pae.ema_200_1m = 22480.0

    observer = ShadowBoundaryObserver(price_action_engine=mock_pae, strike_step=50.0, log_dir="scratch")
    observer.current_atm_strike = 22500.0
    observer.strike_to_symbol = {
        (0, 22750.0): "NIFTY_22750_CE",
        (1, 22250.0): "NIFTY_22250_PE",
    }
    observer.option_cache = {"NIFTY_22750_CE": 45.0}

    # At T+25s, spot is 22518.8, which is >= prior_bar.high (22514.0)
    now_sec = 1700000000.0 + 25.0  # sec_in_min = 45 (between 8 and 48)
    # Single-shot instant entry on first tick breach (zero debounce)
    observer._check_inflight_breakthrough(spot=22518.8, now_sec=now_sec)
    assert len(observer.active_trades) == 1
    trade = observer.active_trades[0]
    assert trade.boundary == "PAIN_ZONE_REJECTION_BREAKOUT"
    assert trade.direction == "UP"
    assert trade.spot_entry == 22518.8
    assert trade.opt_symbol == "NIFTY_22750_CE"


def test_mode_b_breakthrough_in_price_action_engine():
    pa = PriceActionEngine(strike_step=50.0)
    pa.current_atr_1m = 10.0
    pa.last_price = 22518.8
    pa.last_ts = 1700000000.0 + 25.0

    # Seed 25 prior bars so bars_1m has history
    prior_bar = OHLCBar(
        ts_start=1700000000.0 - 60.0,
        ts_end=1700000000.0,
        open=22500.0,
        high=22510.0,
        low=22490.0,
        close=22505.0,
        volume=100.0,
    )
    pa.bars_1m = [prior_bar] * 25
    pa._total_bars_seen = 25

    # Register Pillar 4 Dynamic S/R Level (Resistance at 22510.0)
    pa.session_levels.append(
        StructuralLevel(
            level_type="RESISTANCE",
            price=22510.0,
            ts_created=1700000000.0 - 300.0,
            prominence=1.85,
            seq_idx=0,
            bar_idx=0,
            touch_count=2,
        )
    )

    # Forming bar
    curr_bar = OHLCBar(
        ts_start=1700000000.0,
        ts_end=1700000000.0 + 60.0,
        open=22510.0,
        high=22519.0,
        low=22509.0,
        close=22518.8,
        volume=1000.0,
        tick_count=8,
    )
    pa._curr_1m_bar = curr_bar

    # Single-shot instant breakthrough on first tick breach (zero debounce)
    ok1, reason1, details1 = pa.check_mode_b_breakthrough("UP", current_time=pa.last_ts)
    assert ok1 is True
    assert reason1 in ("sr_breakout", "topological_prominence_breakout")
    assert details1["boundary_level"] == 22510.0
    assert details1["trigger_sec"] == 45


def test_shadow_inflight_breakthrough_large_expansion_above_120_atr():
    """Verify that ShadowBoundaryObserver fires on Mode B breakouts even when prior/forming candles are wild/large."""
    mock_pae = MagicMock()
    # Prior bar had massive 25pt range (testing 200 EMA at 22480.0)
    prior_bar = MagicMock()
    prior_bar.open = 22500.0
    prior_bar.high = 22502.0
    prior_bar.low = 22478.0  # Pierced 200 EMA at 22480
    prior_bar.close = 22481.0
    prior_bar.volume = 3000.0
    prior_bar.call_buy_vol = 1000.0
    prior_bar.put_buy_vol = 2000.0

    # Next bar (13:44 style) is even larger (surges to 22525, breaking prior 22502 High)
    mock_bar = MagicMock()
    mock_bar.tick_count = 50
    mock_bar.open = 22481.0
    mock_bar.high = 22524.2
    mock_bar.low = 22480.0
    mock_bar.close = 22524.0
    mock_bar.volume = 5000.0
    mock_bar.call_buy_vol = 15000.0
    mock_bar.put_buy_vol = 500.0
    mock_pae._curr_1m_bar = mock_bar
    mock_pae._opt_flow_1m = {
        "call_buy_vol": 15000.0,
        "call_sell_vol": 200.0,
        "put_buy_vol": 500.0,
        "put_sell_vol": 200.0,
    }
    mock_pae.get_pain_zone_vwaps.return_value = {"zone_top": 0.0, "zone_bottom": 0.0}
    mock_pae.bars_1m = [prior_bar] * 25
    mock_pae.current_atr_1m = 10.0
    mock_pae.ema_200_1m = 22480.0

    observer = ShadowBoundaryObserver(price_action_engine=mock_pae, strike_step=50.0, log_dir="scratch")
    observer.current_atm_strike = 22500.0
    observer.strike_to_symbol = {(0, 22750.0): "NIFTY_22750_CE"}
    observer.option_cache = {"NIFTY_22750_CE": 45.0}

    now_sec = 1700000000.0 + 10.0  # (1700000000 + 10) % 60 = 30s
    # Single-shot instant entry on first tick breach (zero debounce)
    observer._check_inflight_breakthrough(spot=22524.0, now_sec=now_sec)
    assert len(observer.active_trades) == 1
    trade = observer.active_trades[0]
    assert trade.boundary == "EMA_200_REJECTION_BREAKOUT"
    assert trade.direction == "UP"
    # Scale-invariant cushion: max(2.0, (22524.0/24000)*2.0, 0.10*10) = 2.0 pts
    assert trade.spot_sl_price == pytest.approx(prior_bar.low - 2.0)


def test_rejection_breakout_strict_1bar_expiry():
    """Verify that if the immediate next bar does not break prior bar's extreme, setup expires."""
    mock_pae = MagicMock()
    prior_bar = MagicMock()
    prior_bar.open = 22513.0
    prior_bar.high = 22514.0
    prior_bar.low = 22508.0
    prior_bar.close = 22509.0  # Tested Pain Zone
    prior_bar.volume = 1000.0

    # Bar N: spot moves sideways and never breaks 22514.0 + min_clearance
    sideways_bar = MagicMock()
    sideways_bar.tick_count = 50
    sideways_bar.open = 22509.0
    sideways_bar.high = 22512.0  # Failed to break 22514.0!
    sideways_bar.low = 22508.0
    sideways_bar.close = 22511.0
    mock_pae._curr_1m_bar = sideways_bar
    mock_pae._opt_flow_1m = {"call_buy_vol": 5000.0, "put_buy_vol": 100.0}
    mock_pae.get_pain_zone_vwaps.return_value = {"zone_top": 0.0, "zone_bottom": 0.0}
    mock_pae.bars_1m = [prior_bar] * 25
    mock_pae.current_atr_1m = 10.0
    mock_pae.ema_200_1m = 22400.0

    observer = ShadowBoundaryObserver(price_action_engine=mock_pae, strike_step=50.0, log_dir="scratch")
    observer.current_atm_strike = 22500.0
    observer.strike_to_symbol = {(0, 22750.0): "NIFTY_22750_CE"}
    observer.option_cache = {"NIFTY_22750_CE": 45.0}

    now_sec = 1700000000.0 + 10.0
    observer._check_inflight_breakthrough(spot=22511.0, now_sec=now_sec)
    assert len(observer.active_trades) == 0

    # Bar N closes sideways without triggering. Now Bar N+1 begins:
    # sideways_bar is now in bars_1m. But sideways_bar did NOT test Pain Zone low (low was 22508, close 22511).
    unrelated_prior = MagicMock()
    unrelated_prior.open = 22520.0
    unrelated_prior.high = 22525.0
    unrelated_prior.low = 22518.0  # Nowhere near Pain Zone (22510)
    unrelated_prior.close = 22522.0
    mock_pae.bars_1m.append(unrelated_prior)

    # Next bar surges, but since immediate prior bar did NOT test Pain Zone, it must NOT trigger!
    surge_bar = MagicMock()
    surge_bar.tick_count = 50
    surge_bar.open = 22522.0
    surge_bar.high = 22540.0
    surge_bar.low = 22522.0
    surge_bar.close = 22538.0
    mock_pae._curr_1m_bar = surge_bar

    observer._check_inflight_breakthrough(spot=22538.0, now_sec=now_sec + 60.0)
    observer._check_inflight_breakthrough(spot=22538.0, now_sec=now_sec + 60.2)
    assert len(observer.active_trades) == 0  # Expired, clean rejection!


def test_shadow_ema20_rejection_and_n1_footprint():
    """Verify that ShadowBoundaryObserver tracks 20 EMA Rejection Breakouts with N-1 Wyckoff Footprint."""
    mock_pae = MagicMock()
    # Baseline history with 10,000 volume average
    base_bars = []
    for _ in range(20):
        b = MagicMock()
        b.volume = 10000.0
        base_bars.append(b)

    # N-1 testing candle touching 20 EMA at 22500.0
    # Shows Volume Dry-Up (3000 < 0.80 * 10000) and Positive Net Delta (Call Buy 2000 + Put Sell 1000 > Put Buy 500)
    prior_bar = MagicMock()
    prior_bar.open = 22505.0
    prior_bar.high = 22506.0
    prior_bar.low = 22498.0  # Tested 20 EMA at 22500
    prior_bar.close = 22501.0
    prior_bar.volume = 3000.0
    prior_bar.call_buy_vol = 2000.0
    prior_bar.call_sell_vol = 200.0
    prior_bar.put_buy_vol = 500.0
    prior_bar.put_sell_vol = 1000.0
    base_bars.append(prior_bar)

    # Next bar (N) breaks prior bar High (22506.0 + clearance = 22508.0)
    forming_bar = MagicMock()
    forming_bar.tick_count = 50
    forming_bar.open = 22501.0
    forming_bar.high = 22515.2
    forming_bar.low = 22500.5
    forming_bar.close = 22515.0
    forming_bar.volume = 5000.0
    forming_bar.call_buy_vol = 8000.0
    forming_bar.put_buy_vol = 500.0

    mock_pae.bars_1m = base_bars
    mock_pae._curr_1m_bar = forming_bar
    mock_pae._opt_flow_1m = {"call_buy_vol": 8000.0, "put_buy_vol": 500.0}
    mock_pae.get_pain_zone_vwaps.return_value = {"zone_top": 0.0, "zone_bottom": 0.0}
    mock_pae.current_atr_1m = 10.0
    mock_pae.ema_200_1m = 22500.0
    mock_pae.ema_20_1m = 22500.0

    observer = ShadowBoundaryObserver(price_action_engine=mock_pae, strike_step=50.0, log_dir="scratch")
    observer.current_atm_strike = 22500.0
    observer.strike_to_symbol = {(0, 22750.0): "NIFTY_22750_CE"}
    observer.option_cache = {"NIFTY_22750_CE": 45.0}

    now_sec = 1700000000.0 + 10.0  # sec_in_min = 30s
    # Single-shot instant entry on first tick breach (zero debounce)
    observer._check_inflight_breakthrough(spot=22515.0, now_sec=now_sec)
    assert len(observer.active_trades) == 1
    trade = observer.active_trades[0]
    assert trade.boundary == "EMA_200_REJECTION_BREAKOUT"
    assert trade.direction == "UP"

    # Verify N-1 Wyckoff Footprint Telemetry
    assert trade.n1_vol == pytest.approx(3000.0)
    assert trade.n1_vol_dryup is True
    # Net Delta = (CB 2000 + PS 1000) - (PB 500 + CS 200) = 3000 - 700 = +2300
    assert trade.n1_net_delta == pytest.approx(2300.0)


def test_shadow_breakthrough_late_in_candle_no_upper_cap():
    """Verify shadow breakthrough triggers at T:54s (no 48s upper cap) and respects T < 8s noise guard."""
    mock_pae = MagicMock()
    mock_pae.current_atr_1m = 10.0
    mock_pae.ema_200_1m = 22500.0
    mock_pae.ema_20_1m = 22500.0
    mock_pae.get_pain_zone_vwaps.return_value = {"zone_top": 0.0, "zone_bottom": 0.0}

    prior_bar = MagicMock()
    prior_bar.high = 22506.0
    prior_bar.low = 22495.0
    prior_bar.close = 22504.0
    prior_bar.open = 22498.0
    prior_bar.volume = 3000.0
    prior_bar.call_buy_vol = 2000.0
    prior_bar.call_sell_vol = 200.0
    prior_bar.put_buy_vol = 500.0
    prior_bar.put_sell_vol = 1000.0

    mock_pae.bars_1m = [prior_bar]

    forming_bar = MagicMock()
    forming_bar.tick_count = 50
    forming_bar.open = 22501.0
    forming_bar.high = 22515.2
    forming_bar.low = 22500.5
    forming_bar.close = 22515.0
    forming_bar.volume = 5000.0

    mock_pae._curr_1m_bar = forming_bar
    mock_pae._opt_flow_1m = {"call_buy_vol": 8000.0, "put_buy_vol": 500.0}

    observer = ShadowBoundaryObserver(price_action_engine=mock_pae, strike_step=50.0, log_dir="scratch")
    observer.current_atm_strike = 22500.0
    observer.strike_to_symbol = {(0, 22750.0): "NIFTY_22750_CE"}
    observer.option_cache = {"NIFTY_22750_CE": 45.0}

    # At T:01s (early rollover guard window: sec_in_min = 1 < 2), breakthrough is ignored
    now_base = 1700000040.0
    observer._check_inflight_breakthrough(spot=22515.0, now_sec=now_base + 1.0)
    assert len(observer.active_trades) == 0

    # At T:54s (late candle: sec_in_min = 54, single-shot first tick trigger)
    late_sec = now_base + 54.0
    observer._check_inflight_breakthrough(spot=22515.0, now_sec=late_sec)
    assert len(observer.active_trades) == 1
    assert observer.active_trades[0].boundary == "EMA_200_REJECTION_BREAKOUT"


def test_pain_zone_direct_breakout_call():
    """Verify in-flight Mode B breakout when spot breaks above Pain Zone Top (CALL)."""
    mock_pae = MagicMock()
    bar_n2 = MagicMock()
    bar_n2.open = 22496.0
    bar_n2.high = 22502.0
    bar_n2.low = 22494.0
    bar_n2.close = 22499.0
    bar_n2.volume = 1000.0

    prior_bar = MagicMock()
    prior_bar.open = 22500.0
    prior_bar.high = 22506.0  # > 22502 (HH)
    prior_bar.low = 22498.0   # > 22494 (HL)
    prior_bar.close = 22504.0
    prior_bar.volume = 1000.0

    forming_bar = MagicMock()
    forming_bar.tick_count = 30
    forming_bar.open = 22505.0
    forming_bar.high = 22513.0
    forming_bar.low = 22504.0  # >= 22498 (HL)
    forming_bar.close = 22512.5
    forming_bar.volume = 2000.0

    mock_pae.bars_1m = [bar_n2] * 20 + [bar_n2, prior_bar]
    mock_pae._curr_1m_bar = forming_bar
    mock_pae._opt_flow_1m = {"call_buy_vol": 5000.0, "put_buy_vol": 500.0}
    mock_pae.get_pain_zone_vwaps.return_value = {"zone_top": 22510.0, "zone_bottom": 22490.0}
    mock_pae.current_atr_1m = 10.0
    mock_pae.ema_200_1m = 22450.0
    mock_pae.ema_20_1m = 22450.0

    observer = ShadowBoundaryObserver(price_action_engine=mock_pae, strike_step=50.0, log_dir="scratch")
    observer.current_atm_strike = 22500.0
    observer.strike_to_symbol = {(0, 22750.0): "NIFTY_22750_CE", (1, 22250.0): "NIFTY_22250_PE"}
    observer.option_cache = {"NIFTY_22750_CE": 45.0, "NIFTY_22250_PE": 45.0}

    now_sec = 1700000000.0 + 20.0
    # Spot 22512.5 breaks above zone_top 22510.0
    observer._check_inflight_breakthrough(spot=22512.5, now_sec=now_sec)
    assert len(observer.active_trades) == 1
    trade = observer.active_trades[0]
    assert trade.boundary == "PAIN_ZONE_BREAKOUT"
    assert trade.direction == "UP"
    assert trade.spot_entry == 22512.5
    assert trade.opt_symbol == "NIFTY_22750_CE"


def test_pain_zone_direct_breakdown_put():
    """Verify in-flight Mode B breakdown when spot breaks below Pain Zone Bottom (PUT)."""
    mock_pae = MagicMock()
    bar_n2 = MagicMock()
    bar_n2.open = 22506.0
    bar_n2.high = 22508.0
    bar_n2.low = 22500.0
    bar_n2.close = 22503.0
    bar_n2.volume = 1000.0

    prior_bar = MagicMock()
    prior_bar.open = 22500.0
    prior_bar.high = 22502.0  # < 22508 (LH)
    prior_bar.low = 22494.0   # < 22500 (LL)
    prior_bar.close = 22496.0
    prior_bar.volume = 1000.0

    forming_bar = MagicMock()
    forming_bar.tick_count = 30
    forming_bar.open = 22495.0
    forming_bar.high = 22496.0  # <= 22502 (LH)
    forming_bar.low = 22487.0
    forming_bar.close = 22488.0
    forming_bar.volume = 2000.0

    mock_pae.bars_1m = [bar_n2] * 20 + [bar_n2, prior_bar]
    mock_pae._curr_1m_bar = forming_bar
    mock_pae._opt_flow_1m = {"call_buy_vol": 500.0, "put_buy_vol": 5000.0}
    mock_pae.get_pain_zone_vwaps.return_value = {"zone_top": 22510.0, "zone_bottom": 22490.0}
    mock_pae.current_atr_1m = 10.0
    mock_pae.ema_200_1m = 22450.0
    mock_pae.ema_20_1m = 22450.0

    observer = ShadowBoundaryObserver(price_action_engine=mock_pae, strike_step=50.0, log_dir="scratch")
    observer.current_atm_strike = 22500.0
    observer.strike_to_symbol = {(0, 22750.0): "NIFTY_22750_CE", (1, 22250.0): "NIFTY_22250_PE"}
    observer.option_cache = {"NIFTY_22750_CE": 45.0, "NIFTY_22250_PE": 45.0}

    now_sec = 1700000000.0 + 20.0
    # Spot 22488.0 breaks below zone_bottom 22490.0 (lowest line)
    observer._check_inflight_breakthrough(spot=22488.0, now_sec=now_sec)
    assert len(observer.active_trades) == 1
    trade = observer.active_trades[0]
    assert trade.boundary == "PAIN_ZONE_BREAKOUT"
    assert trade.direction == "DOWN"
    assert trade.spot_entry == 22488.0
    assert trade.opt_symbol == "NIFTY_22250_PE"


def test_ema200_direct_breakout_call():
    """Verify in-flight Mode B breakout when spot crosses 200 EMA to the upside (CALL)."""
    mock_pae = MagicMock()
    bar_n2 = MagicMock()
    bar_n2.open = 22464.0
    bar_n2.high = 22472.0
    bar_n2.low = 22462.0
    bar_n2.close = 22469.0
    bar_n2.volume = 1000.0

    prior_bar = MagicMock()
    prior_bar.open = 22470.0
    prior_bar.high = 22478.0  # > 22472 (HH)
    prior_bar.low = 22468.0   # > 22462 (HL)
    prior_bar.close = 22476.0
    prior_bar.volume = 1000.0

    forming_bar = MagicMock()
    forming_bar.tick_count = 30
    forming_bar.open = 22477.0
    forming_bar.high = 22483.0
    forming_bar.low = 22476.0  # >= 22468 (HL)
    forming_bar.close = 22482.0
    forming_bar.volume = 2000.0

    mock_pae.bars_1m = [bar_n2] * 20 + [bar_n2, prior_bar]
    mock_pae._curr_1m_bar = forming_bar
    mock_pae._opt_flow_1m = {"call_buy_vol": 6000.0, "put_buy_vol": 400.0}
    mock_pae.get_pain_zone_vwaps.return_value = {"zone_top": 0.0, "zone_bottom": 0.0}
    mock_pae.current_atr_1m = 10.0
    mock_pae.ema_200_1m = 22480.0

    observer = ShadowBoundaryObserver(price_action_engine=mock_pae, strike_step=50.0, log_dir="scratch")
    observer.current_atm_strike = 22500.0
    observer.strike_to_symbol = {(0, 22750.0): "NIFTY_22750_CE", (1, 22250.0): "NIFTY_22250_PE"}
    observer.option_cache = {"NIFTY_22750_CE": 45.0, "NIFTY_22250_PE": 45.0}

    now_sec = 1700000000.0 + 15.0
    # Spot 22482.0 crosses above 200 EMA (22480.0)
    observer._check_inflight_breakthrough(spot=22482.0, now_sec=now_sec)
    assert len(observer.active_trades) == 1
    trade = observer.active_trades[0]
    assert trade.boundary == "EMA_200_BREAKOUT"
    assert trade.direction == "UP"
    assert trade.spot_entry == 22482.0
    assert trade.opt_symbol == "NIFTY_22750_CE"


def test_ema200_direct_breakdown_put():
    """Verify in-flight Mode B breakdown when spot breaks below 200 EMA low (PUT)."""
    mock_pae = MagicMock()
    bar_n2 = MagicMock()
    bar_n2.open = 22490.0
    bar_n2.high = 22493.0
    bar_n2.low = 22486.0
    bar_n2.close = 22488.0
    bar_n2.volume = 1000.0

    prior_bar = MagicMock()
    prior_bar.open = 22485.0
    prior_bar.high = 22488.0  # < 22493 (LH)
    prior_bar.low = 22481.0   # < 22486 (LL)
    prior_bar.close = 22483.0
    prior_bar.volume = 1000.0

    forming_bar = MagicMock()
    forming_bar.tick_count = 30
    forming_bar.open = 22481.0
    forming_bar.high = 22482.0  # <= 22488 (LH)
    forming_bar.low = 22475.0
    forming_bar.close = 22475.5
    forming_bar.volume = 2000.0

    mock_pae.bars_1m = [bar_n2] * 20 + [bar_n2, prior_bar]
    mock_pae._curr_1m_bar = forming_bar
    mock_pae._opt_flow_1m = {"call_buy_vol": 300.0, "put_buy_vol": 6000.0}
    mock_pae.get_pain_zone_vwaps.return_value = {"zone_top": 0.0, "zone_bottom": 0.0}
    mock_pae.current_atr_1m = 10.0
    mock_pae.ema_200_1m = 22480.0  # 200 EMA low = 22480 - 0.25*10 = 22477.5
    mock_pae.ema_20_1m = 22450.0

    observer = ShadowBoundaryObserver(price_action_engine=mock_pae, strike_step=50.0, log_dir="scratch")
    observer.current_atm_strike = 22500.0
    observer.strike_to_symbol = {(0, 22750.0): "NIFTY_22750_CE", (1, 22250.0): "NIFTY_22250_PE"}
    observer.option_cache = {"NIFTY_22750_CE": 45.0, "NIFTY_22250_PE": 45.0}

    now_sec = 1700000000.0 + 15.0
    # Spot 22475.5 breaks below 200 EMA Low (22477.5)
    observer._check_inflight_breakthrough(spot=22475.5, now_sec=now_sec)
    assert len(observer.active_trades) == 1
    trade = observer.active_trades[0]
    assert trade.boundary == "EMA_200_BREAKOUT"
    assert trade.direction == "DOWN"
    assert trade.spot_entry == 22475.5
    assert trade.opt_symbol == "NIFTY_22250_PE"


def test_pain_zone_direct_breakout_cooldown_3bars():
    """Verify that after a Pain Zone direct breakout triggers, consecutive ticks/bars within 3 bars are ignored."""
    mock_pae = MagicMock()
    bar_n2 = MagicMock()
    bar_n2.open = 22496.0
    bar_n2.high = 22502.0
    bar_n2.low = 22494.0
    bar_n2.close = 22499.0
    bar_n2.volume = 1000.0

    prior_bar = MagicMock()
    prior_bar.open = 22500.0
    prior_bar.high = 22506.0  # > 22502 (HH)
    prior_bar.low = 22498.0   # > 22494 (HL)
    prior_bar.close = 22504.0
    prior_bar.volume = 1000.0

    forming_bar = MagicMock()
    forming_bar.tick_count = 30
    forming_bar.open = 22505.0
    forming_bar.high = 22513.0
    forming_bar.low = 22504.0  # >= 22498 (HL)
    forming_bar.close = 22512.5
    forming_bar.volume = 2000.0

    mock_pae.bars_1m = [bar_n2] * 20 + [bar_n2, prior_bar]
    mock_pae._curr_1m_bar = forming_bar
    mock_pae._opt_flow_1m = {"call_buy_vol": 5000.0, "put_buy_vol": 500.0}
    mock_pae.get_pain_zone_vwaps.return_value = {"zone_top": 22510.0, "zone_bottom": 22490.0}
    mock_pae.current_atr_1m = 10.0
    mock_pae.ema_200_1m = 22450.0
    mock_pae.ema_20_1m = 22450.0

    observer = ShadowBoundaryObserver(price_action_engine=mock_pae, strike_step=50.0, log_dir="scratch")
    observer.current_atm_strike = 22500.0
    observer.strike_to_symbol = {(0, 22750.0): "NIFTY_22750_CE"}
    observer.option_cache = {"NIFTY_22750_CE": 45.0}

    now_sec = 1700000000.0 + 20.0
    # First trigger at bar_idx 25
    observer._check_inflight_breakthrough(spot=22512.5, now_sec=now_sec)
    assert len(observer.active_trades) == 1

    # Second tick on same bar: must NOT duplicate trigger
    observer._check_inflight_breakthrough(spot=22514.0, now_sec=now_sec + 2.0)
    assert len(observer.active_trades) == 1

    # Bar 26 (+1 bar): still in cooldown
    mock_pae.bars_1m.append(forming_bar)
    observer._check_inflight_breakthrough(spot=22515.0, now_sec=now_sec + 60.0)
    assert len(observer.active_trades) == 1


def test_dow_thrust_vetoes_chop_breakout_call():
    """Verify that when price is oscillating in a range (no Dow HH/HL ascending sequence), CALL breakout is vetoed."""
    mock_pae = MagicMock()
    # Bar N-2 has higher high (range chop, inside bar)
    bar_n2 = MagicMock()
    bar_n2.open = 22500.0
    bar_n2.high = 22508.0
    bar_n2.low = 22495.0
    bar_n2.close = 22500.0
    bar_n2.volume = 1000.0

    # Bar N-1 makes a Lower High (22505 < 22508) - fails Dow HH
    prior_bar = MagicMock()
    prior_bar.open = 22500.0
    prior_bar.high = 22505.0  # < 22508.0 (No Higher High!)
    prior_bar.low = 22497.0
    prior_bar.close = 22504.0
    prior_bar.volume = 1000.0

    # Forming bar suddenly surges across Pain Zone Top (22510.0)
    forming_bar = MagicMock()
    forming_bar.tick_count = 30
    forming_bar.open = 22505.0
    forming_bar.high = 22513.0
    forming_bar.low = 22504.0
    forming_bar.close = 22512.5
    forming_bar.volume = 2000.0

    mock_pae.bars_1m = [bar_n2] * 20 + [bar_n2, prior_bar]
    mock_pae._curr_1m_bar = forming_bar
    mock_pae._opt_flow_1m = {"call_buy_vol": 5000.0, "put_buy_vol": 500.0}
    mock_pae.get_pain_zone_vwaps.return_value = {"zone_top": 22510.0, "zone_bottom": 22490.0}
    mock_pae.current_atr_1m = 10.0
    mock_pae.ema_200_1m = 22450.0
    mock_pae.ema_20_1m = 22450.0

    observer = ShadowBoundaryObserver(price_action_engine=mock_pae, strike_step=50.0, log_dir="scratch")
    observer.current_atm_strike = 22500.0
    observer.strike_to_symbol = {(0, 22750.0): "NIFTY_22750_CE"}
    observer.option_cache = {"NIFTY_22750_CE": 45.0}

    now_sec = 1700000000.0 + 20.0
    # Spot 22512.5 crosses zone_top 22510.0, but Dow thrust fails -> VETOED!
    observer._check_inflight_breakthrough(spot=22512.5, now_sec=now_sec)
    assert len(observer.active_trades) == 0


def test_dow_thrust_vetoes_chop_breakdown_put():
    """Verify that when price is oscillating in a range (no Dow LH/LL descending sequence), PUT breakdown is vetoed."""
    mock_pae = MagicMock()
    # Bar N-2 has lower high
    bar_n2 = MagicMock()
    bar_n2.open = 22500.0
    bar_n2.high = 22502.0
    bar_n2.low = 22492.0
    bar_n2.close = 22495.0
    bar_n2.volume = 1000.0

    # Bar N-1 makes a Higher Low (22496 > 22492) - fails Dow LL
    prior_bar = MagicMock()
    prior_bar.open = 22496.0
    prior_bar.high = 22501.0
    prior_bar.low = 22496.0  # > 22492.0 (No Lower Low!)
    prior_bar.close = 22498.0
    prior_bar.volume = 1000.0

    # Forming bar suddenly drops across Pain Zone Bottom (22490.0)
    forming_bar = MagicMock()
    forming_bar.tick_count = 30
    forming_bar.open = 22495.0
    forming_bar.high = 22496.0
    forming_bar.low = 22487.0
    forming_bar.close = 22488.0
    forming_bar.volume = 2000.0

    mock_pae.bars_1m = [bar_n2] * 20 + [bar_n2, prior_bar]
    mock_pae._curr_1m_bar = forming_bar
    mock_pae._opt_flow_1m = {"call_buy_vol": 500.0, "put_buy_vol": 5000.0}
    mock_pae.get_pain_zone_vwaps.return_value = {"zone_top": 22510.0, "zone_bottom": 22490.0}
    mock_pae.current_atr_1m = 10.0
    mock_pae.ema_200_1m = 22450.0
    mock_pae.ema_20_1m = 22450.0

    observer = ShadowBoundaryObserver(price_action_engine=mock_pae, strike_step=50.0, log_dir="scratch")
    observer.current_atm_strike = 22500.0
    observer.strike_to_symbol = {(1, 22250.0): "NIFTY_22250_PE"}
    observer.option_cache = {"NIFTY_22250_PE": 45.0}

    now_sec = 1700000000.0 + 20.0
    # Spot 22488.0 crosses zone_bottom 22490.0, but Dow thrust fails -> VETOED!
    observer._check_inflight_breakthrough(spot=22488.0, now_sec=now_sec)
    assert len(observer.active_trades) == 0






def test_pain_zone_mid_breakout_call():
    """Verify in-flight Mode B breakthrough when spot crosses Pain Zone Mid (CALL)."""
    mock_pae = MagicMock()
    bar_n2 = MagicMock()
    bar_n2.open = 22490.0
    bar_n2.high = 22495.0
    bar_n2.low = 22485.0
    bar_n2.close = 22492.0
    bar_n2.volume = 1000.0

    prior_bar = MagicMock()
    prior_bar.open = 22493.0
    prior_bar.high = 22502.0  # > 22495 (HH)
    prior_bar.low = 22490.0   # > 22485 (HL)
    prior_bar.close = 22500.0
    prior_bar.volume = 1500.0

    forming_bar = MagicMock()
    forming_bar.tick_count = 25
    forming_bar.open = 22499.0
    forming_bar.high = 22506.0
    forming_bar.low = 22498.0  # >= 22490 (HL)
    forming_bar.close = 22505.0
    forming_bar.volume = 2000.0

    mock_pae.bars_1m = [bar_n2] * 20 + [bar_n2, prior_bar]
    mock_pae._curr_1m_bar = forming_bar
    mock_pae._opt_flow_1m = {"call_buy_vol": 5000.0, "put_buy_vol": 500.0}
    # zone_top is 22515 (not broken yet), but zone_mid is 22502 (broken by spot 22505!)
    mock_pae.get_pain_zone_vwaps.return_value = {
        "zone_top": 22515.0,
        "zone_bottom": 22485.0,
        "zone_mid": 22502.0,
    }
    mock_pae.current_atr_1m = 10.0
    mock_pae.ema_200_1m = 22450.0
    mock_pae.ema_20_1m = 22450.0

    observer = ShadowBoundaryObserver(price_action_engine=mock_pae, strike_step=50.0, log_dir="scratch")
    observer.current_atm_strike = 22500.0
    observer.strike_to_symbol = {(0, 22750.0): "NIFTY_22750_CE"}
    observer.option_cache = {"NIFTY_22750_CE": 50.0}

    now_sec = 1700000000.0 + 15.0
    # Spot is 22505.0 > zone_mid 22502.0 (but < zone_top 22515.0)
    observer._check_inflight_breakthrough(spot=22505.0, now_sec=now_sec)
    assert len(observer.active_trades) == 1
    trade = observer.active_trades[0]
    assert trade.boundary == "PAIN_ZONE_MID_BREAKOUT"
    assert trade.direction == "UP"
    assert trade.spot_entry == 22505.0
    assert trade.pz_mid == 22502.0



def test_call_writer_trap_breakout():
    """Verify in-flight Mode B breakthrough when spot crosses Call Sell VWAP (Call Writers Trapped)."""
    mock_pae = MagicMock()
    bar_n2 = MagicMock()
    bar_n2.open = 22490.0
    bar_n2.high = 22495.0
    bar_n2.low = 22485.0
    bar_n2.close = 22492.0
    bar_n2.volume = 1000.0

    prior_bar = MagicMock()
    prior_bar.open = 22493.0
    prior_bar.high = 22502.0
    prior_bar.low = 22490.0
    prior_bar.close = 22500.0
    prior_bar.volume = 1500.0

    forming_bar = MagicMock()
    forming_bar.tick_count = 25
    forming_bar.open = 22499.0
    forming_bar.high = 22506.0
    forming_bar.low = 22498.0
    forming_bar.close = 22505.0
    forming_bar.volume = 2000.0

    mock_pae.bars_1m = [bar_n2] * 20 + [bar_n2, prior_bar]
    mock_pae._curr_1m_bar = forming_bar
    mock_pae._opt_flow_1m = {"call_buy_vol": 5000.0, "put_buy_vol": 500.0}
    # zone_top is 22525 and zone_mid is 22515 (neither broken by 22505!), but Call Sell VWAP is 22502!
    mock_pae.get_pain_zone_vwaps.return_value = {
        "zone_top": 22525.0,
        "zone_bottom": 22475.0,
        "zone_mid": 22515.0,
        "vwap_call_sell": 22502.0,
        "vwap_put_sell": 22480.0,
    }
    mock_pae.current_atr_1m = 10.0
    mock_pae.ema_200_1m = 22450.0
    mock_pae.ema_20_1m = 22450.0

    observer = ShadowBoundaryObserver(price_action_engine=mock_pae, strike_step=50.0, log_dir="scratch")
    observer.current_atm_strike = 22500.0
    observer.strike_to_symbol = {(0, 22750.0): "NIFTY_22750_CE"}
    observer.option_cache = {"NIFTY_22750_CE": 50.0}

    now_sec = 1700000000.0 + 15.0
    # Spot is 22505.0 > vwap_call_sell 22502.0
    observer._check_inflight_breakthrough(spot=22505.0, now_sec=now_sec)
    assert len(observer.active_trades) == 1
    trade = observer.active_trades[0]
    assert trade.boundary == "CALL_WRITER_TRAP_BREAKOUT"
    assert trade.direction == "UP"
    assert trade.spot_entry == 22505.0
    assert trade.pz_call_sell == 22502.0


def test_put_writer_trap_breakout():
    """Verify in-flight Mode B breakdown when spot breaks below Put Sell VWAP (Put Writers Trapped)."""
    mock_pae = MagicMock()
    bar_n2 = MagicMock()
    bar_n2.open = 22510.0
    bar_n2.high = 22515.0
    bar_n2.low = 22505.0
    bar_n2.close = 22508.0
    bar_n2.volume = 1000.0

    prior_bar = MagicMock()
    prior_bar.open = 22507.0
    prior_bar.high = 22508.0  # < 22515 (LH)
    prior_bar.low = 22498.0   # < 22505 (LL)
    prior_bar.close = 22500.0
    prior_bar.volume = 1500.0

    forming_bar = MagicMock()
    forming_bar.tick_count = 25
    forming_bar.open = 22501.0
    forming_bar.high = 22502.0  # <= 22508 (LH)
    forming_bar.low = 22494.0   # < 22498 (LL)
    forming_bar.close = 22495.0
    forming_bar.volume = 2000.0

    mock_pae.bars_1m = [bar_n2] * 20 + [bar_n2, prior_bar]
    mock_pae._curr_1m_bar = forming_bar
    mock_pae._opt_flow_1m = {"put_buy_vol": 5000.0, "call_buy_vol": 500.0}
    # zone_bottom is 22475 and zone_mid is 22485 (neither broken by 22495!), but Put Sell VWAP is 22498!
    mock_pae.get_pain_zone_vwaps.return_value = {
        "zone_top": 22525.0,
        "zone_bottom": 22475.0,
        "zone_mid": 22485.0,
        "vwap_call_sell": 22520.0,
        "vwap_put_sell": 22498.0,
    }
    mock_pae.current_atr_1m = 10.0
    mock_pae.ema_200_1m = 22550.0
    mock_pae.ema_20_1m = 22550.0

    observer = ShadowBoundaryObserver(price_action_engine=mock_pae, strike_step=50.0, log_dir="scratch")
    observer.current_atm_strike = 22500.0
    observer.strike_to_symbol = {(1, 22250.0): "NIFTY_22250_PE"}
    observer.option_cache = {"NIFTY_22250_PE": 50.0}

    now_sec = 1700000000.0 + 15.0
    # Spot is 22495.0 < vwap_put_sell 22498.0
    observer._check_inflight_breakthrough(spot=22495.0, now_sec=now_sec)
    assert len(observer.active_trades) == 1
    trade = observer.active_trades[0]
    assert trade.boundary == "PUT_WRITER_TRAP_BREAKOUT"
    assert trade.direction == "DOWN"
    assert trade.spot_entry == 22495.0
    assert trade.pz_put_sell == 22498.0
