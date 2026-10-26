import pytest
from datetime import datetime
from unittest.mock import MagicMock
from queue import Queue
from AEGIS.core.price_action_engine import PriceActionEngine, OHLCBar
from AEGIS.aegis_execution_manager import AegisExecutionManager, TradeContext, TradeLeg

def test_trailing_spot_sl_cushion_preserved_on_entry():
    """Verify that on trade entry, trailing_spot_sl does not overwrite the wick cushion."""
    broker = MagicMock()
    data_fetcher = MagicMock()
    data_fetcher.get_active_instrument.return_value = ("NIFTY", "NFO")
    aegis_engine = MagicMock()
    sig_q = Queue()
    em = AegisExecutionManager(broker, data_fetcher, aegis_engine, sig_q)

    ctx = TradeContext(
        signal_type="CALL",
        strategy="AEGIS_GAMMA_BURST",
        index_entry=22466.65,
    )
    ctx.entry_time = datetime.now()
    ctx.candle_low = 22460.85
    ctx.candle_high = 22467.80
    ctx.pillar = 1
    ctx.pa_setup_name = "20_ema_rejection"
    ctx.trailing_spot_sl = 0.0  # Proper entry state!

    leg = TradeLeg("23300", "NIFTY27OCT26C23300", 23300.0, "CALL", 65, 8.15)
    leg.current_price = 8.15
    ctx.add_leg(leg)
    em.trade_context = ctx

    class MockTB:
        def __init__(self):
            self.bars_1m = [
                OHLCBar(ts_start=datetime.now().timestamp(), ts_end=datetime.now().timestamp() + 60,
                        open=22463.15, high=22467.80, low=22460.85, close=22466.00)
            ]
    class MockEngine:
        time_bar_engine = MockTB()
    em.aegis_engine = MockEngine()

    data_fetcher._spot = 22460.0  # Dips below candle_low 22460.85 by 0.85 pts, but inside cushion (cushion = 16.29 * 0.15 = 2.44 pts -> SL = 22458.41)!
    data_fetcher.get_index_ltp = MagicMock(return_value=22460.0)
    em.data_fetcher = data_fetcher

    # Manage trade: spot at 22460.0 MUST NOT trigger exit because 22460.0 > 22458.41!
    em._manage_open_trade()
    assert em.has_open_trade() is True, "Trade was prematurely stopped out because cushion was wiped out!"

def test_mode_a_breakout_from_below_veto():
    """Verify that price coming from below 20 EMA cannot arm a Mode A CALL rejection."""
    pa = PriceActionEngine()
    pa.current_atr_1m = 15.0
    now = 1700000000.0

    # Feed bars below 20 EMA (downtrend / consolidation below 22460)
    for i in range(25):
        b = OHLCBar(
            ts_start=now + i*60,
            ts_end=now + (i+1)*60,
            open=22420.0 + i*0.5,
            high=22425.0 + i*0.5,
            low=22418.0 + i*0.5,
            close=22422.0 + i*0.5,
            volume=50000.0,
        )
        pa.bars_1m.append(b)

    # Now a breakout bar comes from below and crosses 20 EMA upwards
    breakout_bar = OHLCBar(
        ts_start=now + 26*60,
        ts_end=now + 27*60,
        open=22435.0,  # Below 20 EMA (around 22450)
        high=22465.0,
        low=22434.0,
        close=22463.0,  # Closes above/in 20 EMA
        volume=100000.0,
    )
    pa.bars_1m.append(breakout_bar)
    pa._evaluate_mode_a_arming(breakout_bar)

    # Verify that Mode A did NOT arm a CALL rejection on this breakout from below!
    armed = pa._mode_a_armed_setups.get("PILLAR_1_20_EMA")
    assert armed is None or armed.get("direction") != "UP", "Falsely armed CALL rejection on a breakout from below!"


def test_pillar_2_micro_sweep_mode_a_arming_and_piercing():
    """Verify that a valid 5-bar micro-sweep & reclaim arms Mode A and triggers pierce."""
    pa = PriceActionEngine()
    pa.current_atr_1m = 10.0
    now = 1792730040.0  # Exactly divisible by 60 for clean T:00s - T:20s testing

    # 1. Feed 12 base bars forming a clear swing low at 22450.0
    for i in range(12):
        b = OHLCBar(
            ts_start=now + i * 60,
            ts_end=now + (i + 1) * 60,
            open=22460.0,
            high=22465.0,
            low=22450.0 + i * 1.0,
            close=22458.0,
            volume=50000.0,
        )
        pa.bars_1m.append(b)

    # 2. Sweep bar: sweeps prior 5-bar low (22450.0 + 7.0 = 22457.0) down to 22445.0, but reclaims and closes at 22458.0
    # Prior 5 bars lows are 22457.0, 22458.0, 22459.0, 22460.0, 22461.0 (min = 22457.0)
    sweep_bar = OHLCBar(
        ts_start=now + 12 * 60,
        ts_end=now + 13 * 60,
        open=22458.0,
        high=22462.0,
        low=22445.0,  # Swept 22457.0
        close=22460.0, # Reclaimed > 22457.0
        volume=80000.0,
    )
    pa.bars_1m.append(sweep_bar)
    pa._evaluate_mode_a_arming(sweep_bar)

    assert "PILLAR_2_SWEEP_UP" in pa._mode_a_armed_setups
    setup = pa._mode_a_armed_setups["PILLAR_2_SWEEP_UP"]
    assert setup["pillar"] == 2
    assert setup["pa_setup_name"] == "micro_sweep_reclaim"
    assert setup["anchor_high"] == 22462.0

    # 3. Bar N+1 at T+10s: marginal single-tick pierce (0.5pt < 0.10xATR buffer)
    #    must NOT consume the setup — that is the trap filter (2026-10-26).
    pa.last_price = 22462.5
    ok, reason, _ = pa.check_mode_a_pierce("UP", current_time=now + 13 * 60 + 10)
    assert ok is False, "Marginal sub-buffer pierce must be filtered (trap signature)"
    assert "PILLAR_2_SWEEP_UP" in pa._mode_a_armed_setups, "Setup must stay armed after sub-buffer touch"

    # 4. Same bar at T+15s: spot extends 1.5pts beyond anchor (> buffer) -> Triggers Mode A Pierce!
    pa.last_price = 22463.5
    ok, reason, details = pa.check_mode_a_pierce("UP", current_time=now + 13 * 60 + 15)
    assert ok is True
    assert details["mode"] == "MODE_A_PIERCE"
    assert details["pillar"] == 2
    assert details["pa_setup_name"] == "micro_sweep_reclaim"


def _arm_micro_sweep_pillar_2(pa, now):
    """Arms a Pillar 2 micro-sweep CALL setup (shared fixture for Mode A gates)."""
    for i in range(12):
        b = OHLCBar(
            ts_start=now + i * 60,
            ts_end=now + (i + 1) * 60,
            open=22460.0,
            high=22465.0,
            low=22450.0 + i * 1.0,
            close=22458.0,
            volume=50000.0,
        )
        pa.bars_1m.append(b)
    sweep_bar = OHLCBar(
        ts_start=now + 12 * 60,
        ts_end=now + 13 * 60,
        open=22458.0,
        high=22462.0,
        low=22445.0,
        close=22460.0,
        volume=80000.0,
    )
    pa.bars_1m.append(sweep_bar)
    pa._evaluate_mode_a_arming(sweep_bar)
    assert "PILLAR_2_SWEEP_UP" in pa._mode_a_armed_setups


def test_mode_a_pierce_blocked_by_delta_harmony():
    """2026-10-26: a valid buffered pierce with in-flight OFI against the direction
    is blocked (delta_harmony_fail) — parity with Mode B ladders and T:50s pre-close."""
    pa = PriceActionEngine()
    pa.current_atr_1m = 10.0
    now = 1792730040.0
    _arm_micro_sweep_pillar_2(pa, now)

    # In-flight flow: bull=1500, bear=7000 -> bull < 1.25x bear AND call_sell > call_buy
    pa._opt_flow_1m = {
        "call_buy_vol": 1000.0,
        "call_sell_vol": 2000.0,
        "put_buy_vol": 5000.0,
        "put_sell_vol": 500.0,
        "unclass_vol": 0.0,
    }
    pa.last_price = 22464.0  # 2.0pts beyond anchor — clears the pierce buffer
    ok, reason, _ = pa.check_mode_a_pierce("UP", current_time=now + 13 * 60 + 10)
    assert ok is False
    assert reason == "delta_harmony_fail"


def test_mode_a_pierce_passes_with_harmonious_flow():
    """2026-10-26: harmonious in-flight flow (bull >= 1.25x bear, cb >= cs) lets a
    buffered pierce fire — the gate must never blanket-block valid springs."""
    pa = PriceActionEngine()
    pa.current_atr_1m = 10.0
    now = 1792730040.0
    _arm_micro_sweep_pillar_2(pa, now)

    # In-flight flow: bull=7000, bear=1000 -> 7.0x harmony, cb >= cs
    pa._opt_flow_1m = {
        "call_buy_vol": 6000.0,
        "call_sell_vol": 500.0,
        "put_buy_vol": 500.0,
        "put_sell_vol": 1000.0,
        "unclass_vol": 0.0,
    }
    pa.last_price = 22464.0
    ok, reason, details = pa.check_mode_a_pierce("UP", current_time=now + 13 * 60 + 10)
    assert ok is True
    assert details["mode"] == "MODE_A_PIERCE"


def test_mode_a_pierce_harmony_bypass_on_zero_flow():
    """2026-10-26: zero classified option flow (synthetic tests / warmup) must
    gracefully bypass the harmony gate per the pre-flight audit rule."""
    pa = PriceActionEngine()
    pa.current_atr_1m = 10.0
    now = 1792730040.0
    _arm_micro_sweep_pillar_2(pa, now)

    pa._opt_flow_1m = {"call_buy_vol": 0.0, "call_sell_vol": 0.0, "put_buy_vol": 0.0, "put_sell_vol": 0.0, "unclass_vol": 0.0}
    pa.last_price = 22464.0
    ok, reason, details = pa.check_mode_a_pierce("UP", current_time=now + 13 * 60 + 10)
    assert ok is True
    assert details["mode"] == "MODE_A_PIERCE"


def test_mode_b_breakthrough_pillar_3_and_pillar_4():
    """Verify that Mode B triggers mid-bar (T:08s-T:48s) on Pillar 3 (Box) and Pillar 4 (S/R)."""
    pa = PriceActionEngine()
    pa.current_atr_1m = 10.0
    now = 1792730000.0

    # Feed 5 consolidation bars forming a tight squeeze box (22450 - 22458, rng = 8 <= 1.25*ATR)
    for i in range(5):
        b = OHLCBar(
            ts_start=now + i * 60,
            ts_end=now + (i + 1) * 60,
            open=22452.0,
            high=22458.0,
            low=22450.0,
            close=22455.0,
            volume=50000.0,
        )
        pa.bars_1m.append(b)

    pa.ema_20_1m = 22455.0
    pa.ema_50_1m = 22450.0

    # Forming bar at T:25s breaking out of box_high (22458.0)
    curr_bar = OHLCBar(
        ts_start=now + 5 * 60,
        ts_end=now + 6 * 60,
        open=22454.0,  # Open inside box
        high=22463.0,
        low=22454.0,
        close=22463.0,
        volume=30000.0,
        tick_count=20,
    )
    pa._curr_1m_bar = curr_bar
    pa.last_price = 22462.5  # Spot broken out > 22458 + 2.0
    pa._opt_flow_1m = {
        "call_buy_vol": 100000.0,
        "call_sell_vol": 20000.0,
        "put_buy_vol": 10000.0,
        "put_sell_vol": 5000.0,
    }

    # Tick 1: Awaiting second tick
    ok1, reason1, _ = pa.check_mode_b_breakthrough("UP", current_time=now + 5 * 60 + 25)
    assert ok1 is False
    assert reason1 == "awaiting_second_tick"

    # Tick 2: Consecutive persistence -> Trigger Pillar 3 Volatility Squeeze Breakout!
    ok2, reason2, details2 = pa.check_mode_b_breakthrough("UP", current_time=now + 5 * 60 + 26)
    assert ok2 is True
    assert details2["pillar"] == 3
    assert details2["pa_setup_name"] == "volatility_squeeze_breakout"
    assert details2["mode"] == "MODE_B_INFLIGHT_BREAKTHROUGH"


def test_mode_a_pre_close_early_entry():
    """Verify that Mode A Pre-Close triggers at T:50s-T:58s before candle close."""
    pa = PriceActionEngine()
    pa.current_atr_1m = 10.0
    now = 1792730040.0  # Divisible by 60

    # Feed 15 base uptrend bars
    for i in range(15):
        b = OHLCBar(
            ts_start=now + i * 60,
            ts_end=now + (i + 1) * 60,
            open=22450.0 + i * 1.0,
            high=22455.0 + i * 1.0,
            low=22448.0 + i * 1.0,
            close=22454.0 + i * 1.0,
            volume=50000.0,
        )
        pa.bars_1m.append(b)

    pa.ema_20_1m = 22460.0
    pa.ema_50_1m = 22450.0

    # Forming bar at T:52s touching 20 EMA and showing strong rejection with delta
    forming_bar = OHLCBar(
        ts_start=now + 15 * 60,
        ts_end=now + 16 * 60,
        open=22462.0,
        high=22468.0,
        low=22458.0,  # Tested 20 EMA
        close=22467.0,
        volume=60000.0,
        tick_count=25,
    )
    pa._curr_1m_bar = forming_bar
    pa.last_price = 22467.0  # Spot near high (close_edge >= 0.65)
    pa._opt_flow_1m = {
        "call_buy_vol": 120000.0,
        "call_sell_vol": 30000.0,
        "put_buy_vol": 15000.0,
        "put_sell_vol": 10000.0,
    }

    ok, reason, details = pa.check_mode_a_pre_close("UP", current_time=now + 15 * 60 + 52)
    assert ok is True
    assert details["mode"] == "MODE_A_PRECLOSE"
    assert details["pillar"] == 1
    assert details["pa_setup_name"] == "20_ema_rejection"
    assert details["trigger_sec"] == 52


def test_mode_b_p4_triggers_with_larger_origin_dist_and_expansion():
    """Verify that Mode B P4 triggers cleanly when open is 6pts below resistance and range expands."""
    from AEGIS.core.price_action_engine import StructuralLevel
    pa = PriceActionEngine()
    pa.current_atr_1m = 10.0
    now = 1792730040.0

    # Feed 15 base bars (wide range so P3 squeeze box is bypassed)
    for i in range(15):
        b = OHLCBar(
            ts_start=now + i * 60,
            ts_end=now + (i + 1) * 60,
            open=22445.0 + i * 1.0,
            high=22450.0 + i * 1.0,  # Max high = 22464.0 (below resistance 22465.0)
            low=22435.0 + i * 1.0,   # Wide 15pt range (> 1.25x ATR)
            close=22448.0 + i * 1.0,
            volume=50000.0,
        )
        pa.bars_1m.append(b)

    pa.ema_20_1m = 22460.0
    pa.ema_50_1m = 22450.0
    pa.day_high = 22550.0  # Clear runway to Day High

    # Add a resistance level at 22465.0
    res = StructuralLevel(
        level_type="RESISTANCE",
        price=22465.0,
        ts_created=now + 5 * 60,
        prominence=2.5,
        bar_idx=5,
        seq_idx=5,
        touch_count=2,
    )
    pa.session_levels.append(res)
    pa._total_bars_seen = 15

    # Forming bar at T:25s:
    # Opens at 22459.0 (6.0 pts below resistance, which would fail old 0.35 * ATR = 3.5 pts!)
    # Surges to 22475.0 (Range = 16.0 pts > 1.20 * ATR = 12.0 pts!)
    forming_bar = OHLCBar(
        ts_start=now + 15 * 60,
        ts_end=now + 16 * 60,
        open=22459.0,
        high=22475.0,
        low=22459.0,
        close=22474.8,
        volume=80000.0,
        tick_count=20,
    )
    pa._curr_1m_bar = forming_bar
    pa.last_price = 22474.8  # Well above 22465.0 + min_clearance and upper_wick <= 0.05 * atr
    pa._opt_flow_1m = {
        "call_buy_vol": 150000.0,
        "call_sell_vol": 30000.0,
        "put_buy_vol": 10000.0,
        "put_sell_vol": 10000.0,
    }

    # Tick 1:
    ok1, reason1, _ = pa.check_mode_b_breakthrough("UP", current_time=now + 15 * 60 + 25)
    assert ok1 is False
    assert reason1 == "awaiting_second_tick"

    # Tick 2: (0.3s later)
    forming_bar.tick_count += 1
    ok2, reason2, details = pa.check_mode_b_breakthrough("UP", current_time=now + 15 * 60 + 25.3)
    assert ok2 is True
    assert details["pillar"] == 4
    assert details["pa_setup_name"] == "topological_prominence_breakout"
    assert details["mode"] == "MODE_B_INFLIGHT_BREAKTHROUGH"
    assert details["boundary_level"] == 22465.0


def test_compute_n1_footprint_and_20_ema_shadow_telemetry():
    """Verify compute_n1_footprint calculates volume dry-up, synthetic net delta, and stopping volume."""
    pa = PriceActionEngine()
    now = datetime.now().timestamp()
    
    # Seed 20 historical bars with baseline volume 10,000
    for i in range(20):
        b = OHLCBar(
            ts_start=now - (20 - i) * 60,
            ts_end=now - (19 - i) * 60,
            open=22400.0,
            high=22410.0,
            low=22395.0,
            close=22405.0,
            volume=10000.0,
            tick_count=50,
        )
        pa.bars_1m.append(b)

    # Test Case 1: Volume Dry-Up (vol = 5000 < 0.80 * 10000 = 8000)
    dry_bar = OHLCBar(
        ts_start=now,
        ts_end=now + 60,
        open=22400.0,
        high=22405.0,
        low=22398.0,
        close=22402.0,
        volume=5000.0,
        tick_count=20,
    )
    dry_bar.call_buy_vol = 3000.0
    dry_bar.call_sell_vol = 500.0
    dry_bar.put_buy_vol = 400.0
    dry_bar.put_sell_vol = 1000.0
    pa.bars_1m.append(dry_bar)

    fp = pa.compute_n1_footprint(dry_bar, atr=10.0)
    assert fp["n1_vol"] == 5000.0
    assert fp["n1_baseline_vol"] == 10000.0
    assert fp["n1_vol_dryup"] is True
    # Net Delta: (CB 3000 + PS 1000) - (PB 400 + CS 500) = 4000 - 900 = 3100
    assert fp["n1_net_delta"] == 3100.0
    assert fp["n1_stopping_vol"] is False

    # Test Case 2: Stopping Volume (vol = 15000 >= 1.25 * 10000, range = 5 <= 0.80 * 10 = 8)
    stopping_bar = OHLCBar(
        ts_start=now + 60,
        ts_end=now + 120,
        open=22400.0,
        high=22404.0,
        low=22399.0,
        close=22402.0,
        volume=15000.0,
        tick_count=80,
    )
    pa.bars_1m.append(stopping_bar)
    fp2 = pa.compute_n1_footprint(stopping_bar, atr=10.0)
    assert fp2["n1_stopping_vol"] is True
    assert fp2["n1_vol_dryup"] is False


def test_mode_b_no_upper_cap_triggers_late_in_candle():
    """Verify that Mode B triggers without an upper cap (e.g. at T:53s) while preserving T < 8s noise guard."""
    pa = PriceActionEngine()
    pa.current_atr_1m = 10.0
    now = 1792730040.0  # Divisible by 60

    # 4 tight bars forming a squeeze box between 22450 and 22458 (Range = 8.0 pts <= 1.25x ATR)
    for i in range(4):
        b = OHLCBar(
            ts_start=now + i * 60,
            ts_end=now + (i + 1) * 60,
            open=22452.0,
            high=22458.0,
            low=22450.0,
            close=22455.0,
            volume=20000.0,
            tick_count=40,
        )
        pa.bars_1m.append(b)

    # Forming bar at T:52s (previously blocked by <= 48s cap)
    curr_bar = OHLCBar(
        ts_start=now + 4 * 60,
        ts_end=now + 5 * 60,
        open=22455.0,
        high=22463.0,
        low=22454.0,
        close=22462.5,
        volume=30000.0,
        tick_count=35,
    )
    pa._curr_1m_bar = curr_bar
    pa.last_price = 22462.5
    pa._opt_flow_1m = {
        "call_buy_vol": 100000.0,
        "call_sell_vol": 20000.0,
        "put_buy_vol": 10000.0,
        "put_sell_vol": 5000.0,
    }

    # Early guard check: At T:05s, outside_mode_b_window is enforced
    ok_early, reason_early, _ = pa.check_mode_b_breakthrough("UP", current_time=now + 4 * 60 + 5)
    assert ok_early is False
    assert reason_early == "outside_mode_b_window"

    # Late breakthrough check: At T:52s, it awaits 2nd tick
    ok1, reason1, _ = pa.check_mode_b_breakthrough("UP", current_time=now + 4 * 60 + 52)
    assert ok1 is False
    assert reason1 == "awaiting_second_tick"

    # At T:53s, 2nd tick triggers Mode B successfully!
    curr_bar.tick_count += 1
    ok2, reason2, details2 = pa.check_mode_b_breakthrough("UP", current_time=now + 4 * 60 + 53)
    assert ok2 is True
    assert details2["pillar"] == 3
    assert details2["pa_setup_name"] == "volatility_squeeze_breakout"
    assert details2["mode"] == "MODE_B_INFLIGHT_BREAKTHROUGH"
    assert details2["trigger_sec"] == 53


def test_check_n1_wyckoff_gate_logic():
    """Verify check_n1_wyckoff_gate passes normal & high-conviction rejections and vetoes dumps & swamps."""
    pa = PriceActionEngine()

    # 1. Volume Dry-Up Pass (< 0.80x)
    fp_dry = {
        "n1_vol": 6000.0,
        "n1_baseline_vol": 10000.0,
        "n1_vol_dryup": True,
        "n1_stopping_vol": False,
        "n1_bulls": 2000.0,
        "n1_bears": 1000.0,
        "n1_net_delta": 1000.0,
    }
    ok, r = pa.check_n1_wyckoff_gate("CALL", fp_dry)
    assert ok is True
    assert r == "wyckoff_n1_gate_passed"

    # 2. Normal Baseline Volume Pass (0.95x, between 0.80 and 1.24x)
    fp_norm = {
        "n1_vol": 9500.0,
        "n1_baseline_vol": 10000.0,
        "n1_vol_dryup": False,
        "n1_stopping_vol": False,
        "n1_bulls": 1500.0,
        "n1_bears": 1200.0,
        "n1_net_delta": 300.0,
    }
    ok, r = pa.check_n1_wyckoff_gate("CALL", fp_norm)
    assert ok is True
    assert r == "wyckoff_n1_gate_passed"

    # 3. Stopping Volume Absorption Pass (>= 1.25x with absorption)
    fp_stopping = {
        "n1_vol": 14000.0,
        "n1_baseline_vol": 10000.0,
        "n1_vol_dryup": False,
        "n1_stopping_vol": True,
        "n1_bulls": 5000.0,
        "n1_bears": 2000.0,
        "n1_net_delta": 3000.0,
    }
    ok, r = pa.check_n1_wyckoff_gate("CALL", fp_stopping)
    assert ok is True
    assert r == "wyckoff_n1_gate_passed"

    # 4. Heavy Volume Dump Veto (>= 1.25x without stopping volume / absorption)
    fp_dump = {
        "n1_vol": 15000.0,
        "n1_baseline_vol": 10000.0,
        "n1_vol_dryup": False,
        "n1_stopping_vol": False,
        "n1_bulls": 1000.0,
        "n1_bears": 1000.0,
        "n1_net_delta": 0.0,
    }
    ok, r = pa.check_n1_wyckoff_gate("CALL", fp_dump)
    assert ok is False
    assert r == "wyckoff_n1_unabsorbed_volume_dump"

    # 5. Opposing Delta Swamp Veto on CALL (bears swamping bulls: net_delta < 0, bears > 1.5 * bulls)
    fp_swamp_call = {
        "n1_vol": 9000.0,
        "n1_baseline_vol": 10000.0,
        "n1_vol_dryup": False,
        "n1_stopping_vol": False,
        "n1_bulls": 1000.0,
        "n1_bears": 3000.0,  # 3x bulls
        "n1_net_delta": -2000.0,
    }
    ok, r = pa.check_n1_wyckoff_gate("CALL", fp_swamp_call)
    assert ok is False
    assert r == "wyckoff_n1_opposing_put_delta_swamp"

    # 6. Opposing Delta Swamp Veto on PUT (bulls swamping bears: net_delta > 0, bulls > 1.5 * bears)
    fp_swamp_put = {
        "n1_vol": 9000.0,
        "n1_baseline_vol": 10000.0,
        "n1_vol_dryup": False,
        "n1_stopping_vol": False,
        "n1_bulls": 3500.0,  # 3.5x bears
        "n1_bears": 1000.0,
        "n1_net_delta": 2500.0,
    }
    ok, r = pa.check_n1_wyckoff_gate("PUT", fp_swamp_put)
    assert ok is False
    assert r == "wyckoff_n1_opposing_call_delta_swamp"


def test_micro_sweep_sr_registry_mapping():
    """Verify that Pillar 2 micro-sweep maps to active StructuralLevel Support/Resistance from the Registry."""
    from AEGIS.core.price_action_engine import StructuralLevel
    pa = PriceActionEngine()
    pa.current_atr_1m = 10.0
    now = 1792730040.0

    # 1. Register a multi-touch Structural Support at 22480.0
    support = StructuralLevel(
        level_type="SUPPORT",
        price=22480.0,
        ts_created=now - 600,
        prominence=2.5,
        bar_idx=5,
        seq_idx=5,
        touch_count=2,
    )
    pa.session_levels.append(support)
    pa._total_bars_seen = 15

    # Feed 10 base bars
    for i in range(10):
        pa.bars_1m.append(OHLCBar(
            ts_start=now - (10 - i) * 60,
            ts_end=now - (9 - i) * 60,
            open=22485.0,
            high=22490.0,
            low=22482.0,
            close=22486.0,
            volume=10000.0,
            tick_count=50,
        ))

    # Sweep Bar: opens at 22484.0, plunges to 22474.0 (pierces 22480 by 6 pts > min_sweep),
    # then closes at 22486.0 (> 22480 support, 83% lower wick)
    sweep_bar = OHLCBar(
        ts_start=now,
        ts_end=now + 60,
        open=22484.0,
        high=22488.0,
        low=22474.0,
        close=22486.0,
        volume=15000.0,
        tick_count=60,
    )
    pa.bars_1m.append(sweep_bar)
    pa.last_price = 22486.0

    # Execute Pillar 2 check
    ok, reason, details = pa.check_micro_sweep_reclaim("CALL", current_vwap=22470.0, current_time=now + 65.0)
    assert ok is True
    assert details["pillar"] == 2
    assert details["pa_setup_name"] == "micro_sweep_reclaim"
    assert details["sweep_source"] == "SR_REGISTRY"
    assert details["swept_level"] == 22480.0
    assert details["level_touches"] == 2
    assert details["level_prominence"] == 2.5




