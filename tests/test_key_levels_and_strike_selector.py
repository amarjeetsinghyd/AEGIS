import pytest
import pandas as pd
from datetime import datetime
from common.execution.strike_selector import StrikeSelector
from AEGIS.core.price_action_engine import PriceActionEngine, OHLCBar


def test_strike_selector_picks_highest_affordable_without_testing_band():
    """Verify StrikeSelector picks the highest affordable strike (closest to ATM, highest delta)."""
    # Account has Rs 787 funds, lot size 20 => max affordable premium Rs 39.35
    weekly_opts = pd.DataFrame([
        {"symbol": "SENSEX2691774400CE", "token": "101", "strike": 74400.0},
        {"symbol": "SENSEX2691775100CE", "token": "102", "strike": 75100.0},
        {"symbol": "SENSEX2691775400CE", "token": "103", "strike": 75400.0},
    ])
    option_cache = {
        "101": 200.0,  # Rs 4000 for 1 lot -> Unaffordable
        "102": 29.45,  # Rs 589 for 1 lot -> Affordable (<= Rs 787)
        "103": 12.10,  # Rs 242 for 1 lot -> Affordable, but lower delta
    }
    alloc = StrikeSelector.select_strikes(
        signal_type="CALL",
        index_price=74380.0,
        index_name="SENSEX",
        weekly_opts=weekly_opts,
        option_cache=option_cache,
        available_funds=787.0,
        lot_size=20,
    )
    assert len(alloc) == 1
    chosen_row, qty, ltp = alloc[0]
    # Must select 75100 CE (Rs 29.45) rather than forcing down to Rs 12.10
    assert chosen_row["symbol"] == "SENSEX2691775100CE"
    assert ltp == 29.45


def test_strike_selector_0dte_depth_guard():
    """Verify that on 0DTE (expiry day), deep OTM strikes (> 5 strikes away) are capped."""
    # Build 10 OTM strikes
    opts = []
    cache = {}
    for i in range(10):
        strike_val = 74400.0 + (i * 100)
        tok = str(200 + i)
        opts.append({"symbol": f"SENSEX26917{int(strike_val)}CE", "token": tok, "strike": strike_val})
        cache[tok] = 10.0  # all cheap

    weekly_opts = pd.DataFrame(opts)

    alloc = StrikeSelector.select_strikes(
        signal_type="CALL",
        index_price=74380.0,
        index_name="SENSEX",
        weekly_opts=weekly_opts,
        option_cache=cache,
        available_funds=1000.0,
        lot_size=20,
        is_zero_dte=True,  # 0DTE active!
    )
    assert len(alloc) == 1
    chosen_row, qty, ltp = alloc[0]
    # Under 0DTE guard (max_depth=5), max allowed strike is 74400 + 400 = 74800 CE
    assert chosen_row["strike"] <= 74800.0


def test_key_level_proximity_veto_near_pdh():
    """Verify that PriceActionEngine blocks CALLs when spot is directly beneath PDH."""
    engine = PriceActionEngine()
    engine.seed_daily_levels(pdh=74420.0, pdl=73800.0, pdc=74100.0)
    engine.proximity_threshold_bps = 8.0  # 8 bps on 74,400 is ~59.5 pts

    # Spot at 74400 is only 20 pts below PDH (20 <= 59.5 pts) -> MUST BLOCK!
    ok, reason, details = engine.check_key_level_proximity("UP", spot_price=74400.0)
    assert ok is False
    assert reason == "near_overhead_pdh_resistance"
    assert details["dist_pts"] == 20.0

    # Spot at 74300 is 120 pts below PDH (> 59.5 pts) -> MUST ALLOW!
    ok, reason, _ = engine.check_key_level_proximity("UP", spot_price=74300.0)
    assert ok is True
    assert reason == "clear_of_key_levels"

    # Spot at 74430 has broken ABOVE PDH -> MUST ALLOW!
    ok, reason, _ = engine.check_key_level_proximity("UP", spot_price=74430.0)
    assert ok is True
    assert reason == "clear_of_key_levels"


def test_key_level_proximity_veto_near_pdl():
    """Verify that PriceActionEngine blocks PUTs when spot is directly above PDL."""
    engine = PriceActionEngine()
    engine.seed_daily_levels(pdh=75000.0, pdl=74000.0, pdc=74500.0)
    engine.proximity_threshold_bps = 8.0  # 8 bps on 74,000 is ~59.2 pts

    # Spot at 74020 is only 20 pts above PDL -> MUST BLOCK!
    ok, reason, details = engine.check_key_level_proximity("DOWN", spot_price=74020.0)
    assert ok is False
    assert reason == "near_underlying_pdl_support"
    assert details["dist_pts"] == 20.0

    # Spot at 74200 is 200 pts above PDL -> MUST ALLOW!
    ok, reason, _ = engine.check_key_level_proximity("DOWN", spot_price=74200.0)
    assert ok is True
    assert reason == "clear_of_key_levels"


def test_strike_selector_selects_otm8_when_closer_unaffordable():
    """Verify that on low capital (e.g. Rs 605.83, ceiling Rs 30.29 on lot 20),
    StrikeSelector scans up to OTM 10 and selects OTM 8 (74000 PE @ 24.95) when OTM 1-7 are > Rs 32.
    """
    # ATM 74800, PUT strikes: 74700 (OTM 1) down to 73800 (OTM 10)
    opts = []
    cache = {}
    # ATM
    opts.append({"symbol": "SENSEX26SEP74800PE", "token": "74800", "strike": 74800.0})
    cache["74800"] = 190.45
    # OTM 1 to 7: all unaffordable for Rs 605.83 / lot 20 (> Rs 30.29)
    unaffordable_ltps = [149.90, 116.15, 90.45, 69.05, 53.25, 40.60, 32.00]
    for i, ltp_val in enumerate(unaffordable_ltps, start=1):
        stk = 74800.0 - (i * 100)
        tok = str(int(stk))
        opts.append({"symbol": f"SENSEX26SEP{tok}PE", "token": tok, "strike": stk})
        cache[tok] = ltp_val
    # OTM 8 (74000 PE): Rs 24.95 (1 lot = Rs 499.00 <= Rs 605.83 -> Affordable!)
    opts.append({"symbol": "SENSEX26SEP74000PE", "token": "74000", "strike": 74000.0})
    cache["74000"] = 24.95
    # OTM 9 (73900 PE): Rs 19.40 (Affordable, but lower delta)
    opts.append({"symbol": "SENSEX26SEP73900PE", "token": "73900", "strike": 73900.0})
    cache["73900"] = 19.40

    weekly_opts = pd.DataFrame(opts)

    alloc = StrikeSelector.select_strikes(
        signal_type="PUT",
        index_price=74813.0,
        index_name="SENSEX",
        weekly_opts=weekly_opts,
        option_cache=cache,
        available_funds=605.83,
        lot_size=20,
    )
    assert len(alloc) == 1
    chosen_row, qty, ltp = alloc[0]
    assert chosen_row["symbol"] == "SENSEX26SEP74000PE"
    assert chosen_row["strike"] == 74000.0
    assert ltp == 24.95
    assert ltp * 20 <= 605.83


def test_strike_selector_selects_otm12_for_tiny_funds_sensex():
    """Verify that with tiny funds (e.g. Rs 588.36, ceiling Rs 29.418 on lot 20),
    StrikeSelector scans up to 15 OTM strikes and selects 73900 CE (OTM 12) @ 24.50
    when OTM 1 to 11 (72800 to 73800) are all unaffordable (> Rs 30.00).
    """
    # ATM 72700, CALL strikes: 72800 (OTM 1) up to 74200 (OTM 15)
    opts = []
    cache = {}
    # ATM
    opts.append({"symbol": "SENSEX26O0172700CE", "token": "72700", "strike": 72700.0})
    cache["72700"] = 350.0

    # OTM 1 to 11 (72800 to 73800): all unaffordable for Rs 588.36 / lot 20 (> Rs 29.418)
    for i in range(1, 12):
        stk = 72700.0 + (i * 100)
        tok = str(int(stk))
        opts.append({"symbol": f"SENSEX26O01{tok}CE", "token": tok, "strike": stk})
        cache[tok] = 180.0 - (i * 12.0)  # e.g. 73800 = 48.0 > 29.418

    # OTM 12 (73900 CE): Rs 24.50 (1 lot = Rs 490.00 <= Rs 588.36 -> Affordable!)
    opts.append({"symbol": "SENSEX26O0173900CE", "token": "73900", "strike": 73900.0})
    cache["73900"] = 24.50

    # OTM 13 (74000 CE): Rs 14.20 (Affordable, but lower delta than 73900)
    opts.append({"symbol": "SENSEX26O0174000CE", "token": "74000", "strike": 74000.0})
    cache["74000"] = 14.20

    weekly_opts = pd.DataFrame(opts)

    alloc = StrikeSelector.select_strikes(
        signal_type="CALL",
        index_price=72690.0,
        index_name="SENSEX",
        weekly_opts=weekly_opts,
        option_cache=cache,
        available_funds=588.36,
        lot_size=20,
        is_zero_dte=True,
    )
    assert len(alloc) == 1
    chosen_row, qty, ltp = alloc[0]
    assert chosen_row["symbol"] == "SENSEX26O0173900CE"
    assert chosen_row["strike"] == 73900.0
    assert ltp == 24.50
    assert ltp * 20 <= 588.36


