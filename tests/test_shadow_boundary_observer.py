import pytest
from unittest.mock import MagicMock
from AEGIS.core.price_action_engine import PriceActionEngine, OHLCBar, StructuralLevel
from AEGIS.core.shadow_boundary_observer import ShadowBoundaryObserver, ShadowTrade


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
    mock_bar = MagicMock()
    mock_bar.tick_count = 80
    mock_bar.open = 22510.0
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
    mock_pae.bars_1m = [mock_bar] * 25
    mock_pae.current_atr_1m = 10.0
    mock_pae.ema_200_1m = 22480.0

    observer = ShadowBoundaryObserver(price_action_engine=mock_pae, strike_step=50.0, log_dir="scratch")
    observer.current_atm_strike = 22500.0
    observer.strike_to_symbol = {
        (0, 22750.0): "NIFTY_22750_CE",
        (1, 22250.0): "NIFTY_22250_PE",
    }
    observer.option_cache = {"NIFTY_22750_CE": 45.0}

    # At T+25s, spot is 22518.8, which is > pz_top (22510) + 0.15*ATR (1.5) = 22511.5
    # Range is 10.0 (1.0x ATR), body_ratio is 0.88 >= 0.65, upper_wick = 0.2 <= 0.05*ATR
    now_sec = 1700000000.0 + 25.0  # sec_in_min = 25
    # First tick -> arms persistence (count = 1)
    observer._check_inflight_breakthrough(spot=22518.8, now_sec=now_sec)
    assert len(observer.active_trades) == 0

    # Second tick -> fires entry!
    observer._check_inflight_breakthrough(spot=22518.8, now_sec=now_sec + 0.1)
    assert len(observer.active_trades) == 1
    trade = observer.active_trades[0]
    assert trade.boundary == "PAIN_ZONE_BREAKTHROUGH"
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

    # Tick 1: returns False (awaiting second tick)
    ok1, reason1, _ = pa.check_mode_b_breakthrough("UP", current_time=pa.last_ts)
    assert ok1 is False
    assert reason1 == "awaiting_second_tick"

    # Tick 2: returns True (breakthrough confirmed on Pillar 4 S/R breakout)
    ok2, reason2, details = pa.check_mode_b_breakthrough("UP", current_time=pa.last_ts + 0.3)
    assert ok2 is True
    assert reason2 == "topological_prominence_breakout"
    assert details["boundary_level"] == 22510.0
    assert details["trigger_sec"] == 45


def test_shadow_inflight_breakthrough_large_expansion_above_120_atr():
    """Verify that ShadowBoundaryObserver fires on Mode B breakouts even when candle expands > 1.20x ATR."""
    mock_pae = MagicMock()
    mock_bar = MagicMock()
    mock_bar.tick_count = 50
    # Candle opens at 22505, surges to 22522 (range = 17.0 pts > 1.20 * ATR = 12.0)
    mock_bar.open = 22505.0
    mock_bar.high = 22522.0
    mock_bar.low = 22505.0
    mock_bar.close = 22521.8
    mock_bar.volume = 2000.0
    mock_bar.call_buy_vol = 10000.0
    mock_bar.put_buy_vol = 500.0
    mock_pae._curr_1m_bar = mock_bar
    mock_pae._opt_flow_1m = {
        "call_buy_vol": 10000.0,
        "call_sell_vol": 200.0,
        "put_buy_vol": 500.0,
        "put_sell_vol": 200.0,
    }
    mock_pae.get_pain_zone_vwaps.return_value = {"zone_top": 22510.0, "zone_bottom": 22490.0}
    mock_pae.bars_1m = [mock_bar] * 25
    mock_pae.current_atr_1m = 10.0
    mock_pae.ema_200_1m = 22480.0

    observer = ShadowBoundaryObserver(price_action_engine=mock_pae, strike_step=50.0, log_dir="scratch")
    observer.current_atm_strike = 22500.0
    observer.strike_to_symbol = {(0, 22750.0): "NIFTY_22750_CE"}
    observer.option_cache = {"NIFTY_22750_CE": 45.0}

    now_sec = 1700000000.0 + 10.0  # (1700000000 + 10) % 60 = 30s
    # Tick 1 -> persistence count 1
    observer._check_inflight_breakthrough(spot=22521.8, now_sec=now_sec)
    assert len(observer.active_trades) == 0

    # Tick 2 -> fires trade despite range (17.0) being 1.7x ATR
    observer._check_inflight_breakthrough(spot=22521.8, now_sec=now_sec + 0.2)
    assert len(observer.active_trades) == 1
    trade = observer.active_trades[0]
    assert trade.boundary == "PAIN_ZONE_BREAKTHROUGH"
    assert trade.direction == "UP"
    # Scale-invariant cushion: max(2.0, (22521.8/24000)*2.0, 0.10*10) = 2.0 pts
    assert trade.spot_sl_price == pytest.approx(mock_bar.low - 2.0)

