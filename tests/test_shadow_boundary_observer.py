import pytest
import time
from unittest.mock import MagicMock
from AEGIS.core.shadow_boundary_observer import ShadowBoundaryObserver, ShadowTrade

def test_shadow_boundary_observer_init():
    mock_pae = MagicMock()
    mock_pae.get_pain_zone_vwaps.return_value = {"zone_top": 22520.0, "zone_bottom": 22510.0}
    mock_pae.ema_200_1m = 22490.0
    mock_pae.current_atr_1m = 10.0
    mock_pae.bars_1m = []
    
    observer = ShadowBoundaryObserver(
        price_action_engine=mock_pae,
        strike_step=50.0,
        log_dir="scratch"
    )
    assert observer is not None
    assert observer.strike_step == 50.0

def test_otm5_strike_selection():
    mock_pae = MagicMock()
    observer = ShadowBoundaryObserver(price_action_engine=mock_pae, strike_step=50.0, log_dir="scratch")
    observer.current_atm_strike = 22500.0
    observer.strike_to_symbol = {
        (0, 22750.0): "NIFTY_22750_CE",
        (1, 22250.0): "NIFTY_22250_PE",
    }
    
    # CALL OTM 5: ATM (22500) + 5*50 = 22750 CE
    sym_call, strike_call, side_call = observer.select_otm5_symbol("UP", 22500.0)
    assert sym_call == "NIFTY_22750_CE"
    assert strike_call == 22750.0
    assert side_call == "CE"
    
    # PUT OTM 5: ATM (22500) - 5*50 = 22250 PE
    sym_put, strike_put, side_put = observer.select_otm5_symbol("DOWN", 22500.0)
    assert sym_put == "NIFTY_22250_PE"
    assert strike_put == 22250.0
    assert side_put == "PE"

def test_shadow_trade_lifecycle():
    mock_pae = MagicMock()
    observer = ShadowBoundaryObserver(price_action_engine=mock_pae, strike_step=50.0, log_dir="scratch")
    now = time.time()
    
    trade = ShadowTrade(
        trade_id="TEST_001",
        boundary="PAIN_ZONE_BAND",
        direction="UP",
        entry_ts=now,
        entry_time_str="12:00:00",
        spot_entry=22500.0,
        spot_anchor_high=22499.0,
        spot_anchor_low=22490.0,
        spot_sl_price=22488.0,
        opt_symbol="NIFTY_22750_CE",
        opt_strike=22750.0,
        opt_side="CE",
        opt_entry=50.0,
        opt_tp_price=52.0,  # +4%
        opt_sl_price=44.0,  # -12%
    )
    observer.active_trades.append(trade)
    
    # Advance 35s, option price moves to 51.0 (+2.0%)
    observer.on_option_tick("NIFTY_22750_CE", 51.0, now + 35.0)
    assert trade.captured_30s is True
    assert trade.opt_mfe_30s_pct == pytest.approx(2.0)
    assert trade.is_active is True
    
    # Advance 45s, option price hits 52.5 (+5.0% -> TP Hit!)
    observer.on_option_tick("NIFTY_22750_CE", 52.5, now + 45.0)
    assert trade.is_active is False
    assert trade.outcome == "WINNER"
    assert trade.exit_reason == "TAKE_PROFIT_HIT"


def test_ema200_levels_and_live_slope():
    from AEGIS.core.price_action_engine import PriceActionEngine
    pa = PriceActionEngine()
    assert pa.ema_200_1m == 0.0
    
    # Seed 20 historical bars
    bars = []
    base_ts = 1700000000.0
    for i in range(25):
        bars.append({
            "ts": base_ts + i * 60,
            "open": 22500.0 + i,
            "high": 22505.0 + i,
            "low": 22495.0 + i,
            "close": 22502.0 + i,
            "volume": 1000.0,
        })
    pa.seed_historical_bars(bars)
    
    assert pa.ema_200_1m > 0.0
    live_ema_200, slope_bps = pa.get_live_200_ema_and_slope()
    assert live_ema_200 > 0.0
    
    # Test ShadowBoundaryObserver EMA 200 retrieval
    observer = ShadowBoundaryObserver(price_action_engine=pa, strike_step=50.0, log_dir="scratch")
    top, bottom = observer._get_ema200_levels(atr=10.0)
    assert top > bottom
    assert top == pytest.approx(pa.ema_200_1m + 2.5)
    assert bottom == pytest.approx(pa.ema_200_1m - 2.5)
