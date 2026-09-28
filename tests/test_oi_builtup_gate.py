import pytest
from AEGIS.core.oi_builtup_gate import OIBuiltupGate


def test_oi_builtup_classification():
    gate = OIBuiltupGate(wall_buffer_pts=50.0, require_builtup_confirmation=True)

    # 1. Long Buildup: Price UP, OI UP
    gate.update_tick("SENSEX2690376500PE", strike=76500, kind=2, lp=50.0, oi=100000.0, ts_sec=100.0)
    gate.update_tick("SENSEX2690376500PE", strike=76500, kind=2, lp=60.0, oi=120000.0, ts_sec=110.0)
    status, dp, doi = gate.get_contract_builtup("SENSEX2690376500PE")
    assert status == "LONG_BUILDUP"
    assert dp == 10.0
    assert doi == 20000.0

    # 2. Short Covering: Price UP, OI DOWN
    gate.update_tick("SENSEX2690376600PE", strike=76600, kind=2, lp=40.0, oi=200000.0, ts_sec=100.0)
    gate.update_tick("SENSEX2690376600PE", strike=76600, kind=2, lp=55.0, oi=170000.0, ts_sec=110.0)
    status, dp, doi = gate.get_contract_builtup("SENSEX2690376600PE")
    assert status == "SHORT_COVERING"
    assert dp == 15.0
    assert doi == -30000.0

    # 3. Short Buildup: Price DOWN, OI UP
    gate.update_tick("SENSEX2690376700PE", strike=76700, kind=2, lp=100.0, oi=50000.0, ts_sec=100.0)
    gate.update_tick("SENSEX2690376700PE", strike=76700, kind=2, lp=80.0, oi=80000.0, ts_sec=110.0)
    status, dp, doi = gate.get_contract_builtup("SENSEX2690376700PE")
    assert status == "SHORT_BUILDUP"
    assert dp == -20.0
    assert doi == 30000.0

    # 4. Long Liquidation: Price DOWN, OI DOWN
    gate.update_tick("SENSEX2690376800CE", strike=76800, kind=1, lp=120.0, oi=300000.0, ts_sec=100.0)
    gate.update_tick("SENSEX2690376800CE", strike=76800, kind=1, lp=90.0, oi=250000.0, ts_sec=110.0)
    status, dp, doi = gate.get_contract_builtup("SENSEX2690376800CE")
    assert status == "LONG_LIQUIDATION"
    assert dp == -30.0
    assert doi == -50000.0


def test_gate_veto_on_decaying_options():
    gate = OIBuiltupGate(wall_buffer_pts=50.0, require_builtup_confirmation=True)

    # Setup contract with Short Buildup (heavy selling)
    gate.update_tick("SENSEX2690376700PE", strike=76700, kind=2, lp=100.0, oi=50000.0, ts_sec=100.0)
    gate.update_tick("SENSEX2690376700PE", strike=76700, kind=2, lp=80.0, oi=80000.0, ts_sec=110.0)

    # Attempt to buy PE when contract is in Short Buildup
    allowed, reason, metrics = gate.evaluate_gate("SENSEX2690376700PE", spot_price=76750.0, direction="PE")
    assert not allowed
    assert reason == "veto_pe_short_buildup"


def test_major_wall_defense_and_squeeze():
    # SENSEX configuration: strike_step = 100.0
    # Zone 1: dist <= 40 pts (D <= 0.40), requires speed <= -1.0% in 3m
    # Zone 2: 40 < dist <= 100 pts (0.40 < D <= 1.00), requires speed <= -0.5% in 3m
    # Zone 3: dist > 100 pts (D > 1.00), clear runway
    gate = OIBuiltupGate(strike_step=100.0, require_builtup_confirmation=True)

    # Setup 76500 PE as the biggest wall with 80 Lakh OI, increasing
    gate.update_tick("SENSEX2690376500PE", strike=76500, kind=2, lp=50.0, oi=7500000.0, ts_sec=100.0)
    gate.update_tick("SENSEX2690376500PE", strike=76500, kind=2, lp=52.0, oi=8000000.0, ts_sec=110.0)

    # Case 1: Spot is 76530 (30 pts from wall, Zone 1: D=0.30 <= 0.40) and wall is firm -> VETO Zone 1!
    allowed, reason, metrics = gate.evaluate_gate("SENSEX2690376500PE", spot_price=76530.0, direction="PE", now_sec=110.0)
    assert not allowed
    assert "veto_firm_pe_wall_zone1_76500" in reason
    assert not metrics["is_squeeze"]

    # Case 2: Spot is 76570 (70 pts from wall, Zone 2: D=0.70) and wall is firm -> VETO Zone 2!
    allowed, reason, metrics = gate.evaluate_gate("SENSEX2690376500PE", spot_price=76570.0, direction="PE", now_sec=110.0)
    assert not allowed
    assert "veto_firm_pe_wall_zone2_76500" in reason
    assert not metrics["is_squeeze"]

    # Case 3: Spot is 76650 (150 pts from wall, Zone 3: D=1.50 > 1.00, plenty of runway) -> PERMITTED!
    allowed, reason, metrics = gate.evaluate_gate("SENSEX2690376500PE", spot_price=76650.0, direction="PE", now_sec=110.0)
    assert allowed
    assert reason == "permitted"

    # Case 4: Zone 2 with Moderate Unwinding (-0.8% speed <= -0.5%) -> SQUEEZE PERMITTED!
    # Drop OI from 8,000,000 to 7,936,000 (-64,000 = -0.8%)
    gate.update_tick("SENSEX2690376500PE", strike=76500, kind=2, lp=55.0, oi=7936000.0, ts_sec=200.0)
    allowed, reason, metrics = gate.evaluate_gate("SENSEX2690376500PE", spot_price=76570.0, direction="PE", now_sec=200.0)
    assert allowed
    assert metrics["is_squeeze"] is True

    # Case 5: Zone 1 with Heavy Unwinding (-1.5% speed <= -1.0%) -> SQUEEZE PERMITTED in Danger Zone!
    # Drop OI from 8,000,000 to 7,880,000 (-120,000 = -1.5%)
    gate.update_tick("SENSEX2690376500PE", strike=76500, kind=2, lp=62.0, oi=7880000.0, ts_sec=210.0)
    allowed, reason, metrics = gate.evaluate_gate("SENSEX2690376500PE", spot_price=76525.0, direction="PE", now_sec=210.0)
    assert allowed
    assert metrics["is_squeeze"] is True

    # Case 6: Wall Penetration (Spot breaks past 76500 to 76490) -> SQUEEZE BREAKOUT!
    allowed, reason, metrics = gate.evaluate_gate("SENSEX2690376500PE", spot_price=76490.0, direction="PE", now_sec=210.0)
    assert allowed
    assert metrics["is_squeeze"] is True


def test_nifty_normalization():
    # NIFTY configuration: strike_step = 50.0
    # Zone 1: dist <= 20 pts (D <= 0.40)
    # Zone 2: 20 < dist <= 50 pts (0.40 < D <= 1.00)
    # Zone 3: dist > 50 pts (D > 1.00)
    gate = OIBuiltupGate(strike_step=50.0, require_builtup_confirmation=True)

    gate.update_tick("NIFTY2690424000PE", strike=24000, kind=2, lp=60.0, oi=20000000.0, ts_sec=100.0)
    gate.update_tick("NIFTY2690424000PE", strike=24000, kind=2, lp=62.0, oi=20500000.0, ts_sec=110.0)

    # 15 pts away -> Zone 1 (15 / 50 = 0.30 <= 0.40) -> VETO Zone 1
    allowed, reason, _ = gate.evaluate_gate("NIFTY2690424000PE", spot_price=24015.0, direction="PE", now_sec=110.0)
    assert not allowed
    assert "veto_firm_pe_wall_zone1" in reason

    # 35 pts away -> Zone 2 (35 / 50 = 0.70) -> VETO Zone 2
    allowed, reason, _ = gate.evaluate_gate("NIFTY2690424000PE", spot_price=24035.0, direction="PE", now_sec=110.0)
    assert not allowed
    assert "veto_firm_pe_wall_zone2" in reason

    # 60 pts away -> Zone 3 (60 / 50 = 1.20 > 1.00) -> PERMITTED
    allowed, reason, _ = gate.evaluate_gate("NIFTY2690424000PE", spot_price=24060.0, direction="PE", now_sec=110.0)
    assert allowed
    assert reason == "permitted"


def test_directional_floor_pe_wall_veto():
    """
    Verifies that when buying PE, the gate checks the immediate floor below spot,
    even if an in-the-money strike above spot has higher absolute OI.
    Reproduction of Sep 08 trade:
    23700 PE has 281L OI (above spot 23670)
    23650 PE has 237L OI (below spot 23670, distance = 20 pts = 0.40 strikes)
    Gate MUST veto PE entry because 23650 PE is an immediate defended floor!
    """
    gate = OIBuiltupGate(strike_step=50.0, require_builtup_confirmation=True)

    # 23700 PE (above spot): 281L OI
    gate.update_tick("NIFTY23700PE", strike=23700, kind=2, lp=55.0, oi=28100000.0, ts_sec=100.0)
    gate.update_tick("NIFTY23700PE", strike=23700, kind=2, lp=58.0, oi=28150000.0, ts_sec=110.0)

    # 23650 PE (below spot): 237L OI
    gate.update_tick("NIFTY23650PE", strike=23650, kind=2, lp=25.0, oi=23700000.0, ts_sec=100.0)
    gate.update_tick("NIFTY23650PE", strike=23650, kind=2, lp=26.0, oi=23720000.0, ts_sec=110.0)

    # Spot is 23670. Buying PE at 23670.
    # Distance to 23650 PE is (23670 - 23650) / 50 = 20 / 50 = 0.40 strikes (Zone 1)
    allowed, reason, metrics = gate.evaluate_gate("NIFTY23600PE", spot_price=23670.0, direction="PE", now_sec=110.0)
    assert not allowed
    assert "veto_firm_pe_wall_zone1_23650" in reason
    assert metrics["dist_to_wall_strikes"] == 0.40


def test_atm_heavy_writing_gate_veto():
    """
    Verifies that aggressive Put/Call writing at ATM/ATM±1 strikes (+50% to +200% OI change)
    triggers an immediate veto even if other filters pass.
    """
    gate = OIBuiltupGate(strike_step=50.0, require_builtup_confirmation=False)

    # 23650 PE opened at 76,000 OI, now surged to 237,000 OI (+211% writing surge!)
    gate.update_tick("NIFTY23650PE", strike=23650, kind=2, lp=25.0, oi=76000.0, ts_sec=100.0)
    gate.update_tick("NIFTY23650PE", strike=23650, kind=2, lp=24.0, oi=237000.0, ts_sec=110.0)

    # Spot is 23670. Candidate is buying 23600 PE.
    allowed, reason, metrics = gate.evaluate_gate("NIFTY23600PE", spot_price=23670.0, direction="PE", now_sec=110.0)
    assert not allowed
    assert "veto_heavy_put_writing_23650" in reason
    assert metrics["heavy_writing_strike"] == 23650.0
    assert metrics["heavy_writing_doi_pct"] > 200.0


def test_atm_heavy_call_writing_gate_veto():
    """
    Verifies that aggressive Call writing at ATM/ATM+1 strikes triggers an immediate veto for CE trades.
    """
    gate = OIBuiltupGate(strike_step=50.0, require_builtup_confirmation=False)

    # 23700 CE opened at 60,000 OI, now surged to 180,000 OI (+200% call writing surge!)
    gate.update_tick("NIFTY23700CE", strike=23700, kind=1, lp=45.0, oi=60000.0, ts_sec=100.0)
    gate.update_tick("NIFTY23700CE", strike=23700, kind=1, lp=42.0, oi=180000.0, ts_sec=110.0)

    # Spot is 23670. Candidate is buying 23700 CE.
    allowed, reason, metrics = gate.evaluate_gate("NIFTY23700CE", spot_price=23670.0, direction="CE", now_sec=110.0)
    assert not allowed
    assert "veto_heavy_call_writing_23700" in reason
    assert metrics["heavy_writing_strike"] == 23700.0
    assert metrics["heavy_writing_doi_pct"] == 200.0


def test_oi_kinematics_fresh_build():
    """OI velocity positive on steady build — no dumping signal."""
    gate = OIBuiltupGate(wall_buffer_pts=50.0, require_builtup_confirmation=False)
    for i, ts in enumerate([0.0, 30.0, 60.0, 90.0, 120.0, 150.0, 180.0]):
        gate.update_tick("NIFTY23700CE", strike=23700, kind=1, lp=45.0,
                         oi=100000.0 + i * 5000.0, ts_sec=ts)
    vel, acc, n = gate.get_oi_kinematics("NIFTY23700CE", 180.0)
    assert n >= 2
    assert vel > 0  # fresh money entering


def test_oi_kinematics_dumping_detected():
    """Sharp 60s OI dump yields velocity below -1.0%/min (veto-grade)."""
    gate = OIBuiltupGate(wall_buffer_pts=50.0, require_builtup_confirmation=False)
    gate.update_tick("NIFTY23700CE", strike=23700, kind=1, lp=45.0, oi=130000.0, ts_sec=0.0)
    gate.update_tick("NIFTY23700CE", strike=23700, kind=1, lp=45.0, oi=130000.0, ts_sec=60.0)
    gate.update_tick("NIFTY23700CE", strike=23700, kind=1, lp=44.0, oi=110000.0, ts_sec=120.0)
    vel, acc, n = gate.get_oi_kinematics("NIFTY23700CE", 120.0)
    assert n >= 2
    assert vel < -1.0  # dumping pace vetoes fresh entries
    assert acc < 0  # accelerating out


def test_oi_kinematics_sparse_fail_open():
    """Fewer than 2 history points => fail-open zeros, never a veto."""
    gate = OIBuiltupGate(wall_buffer_pts=50.0, require_builtup_confirmation=False)
    gate.update_tick("NIFTY23700CE", strike=23700, kind=1, lp=45.0, oi=100000.0, ts_sec=100.0)
    vel, acc, n = gate.get_oi_kinematics("NIFTY23700CE", 100.0)
    assert (vel, acc) == (0.0, 0.0)


def test_wall_snapshot_read_only():
    """Wall snapshot exposes fortress without gating (telemetry only)."""
    gate = OIBuiltupGate(strike_step=50.0, require_builtup_confirmation=True)
    gate.update_tick("NIFTY23650PE", strike=23650, kind=2, lp=25.0, oi=23700000.0, ts_sec=100.0)
    gate.update_tick("NIFTY23700CE", strike=23700, kind=1, lp=45.0, oi=18000000.0, ts_sec=100.0)
    snap = gate.get_wall_snapshot(23670.0)
    assert snap["max_pe_strike"] == 23650.0
    assert snap["max_ce_strike"] == 23700.0
    assert snap["pe_dist_strikes"] == pytest.approx(0.40)
    assert snap["ce_dist_strikes"] == pytest.approx(0.60)
    assert snap["n_tracked"] == 2
    # Empty gate never crashes — zeros for visibility.
    empty = OIBuiltupGate(strike_step=50.0).get_wall_snapshot(23670.0)
    assert empty["max_ce_strike"] == 0.0
    assert empty["n_tracked"] == 0


def test_wall_snapshot_thresholds_untouched():
    """Snapshot must not alter gate verdicts (gates stay strict)."""
    gate = OIBuiltupGate(strike_step=50.0, require_builtup_confirmation=True)
    gate.update_tick("NIFTY23650PE", strike=23650, kind=2, lp=25.0, oi=23700000.0, ts_sec=100.0)
    gate.update_tick("NIFTY23650PE", strike=23650, kind=2, lp=26.0, oi=23720000.0, ts_sec=110.0)
    gate.get_wall_snapshot(23670.0)  # read-only call
    allowed, reason, _ = gate.evaluate_gate("NIFTY23600PE", spot_price=23670.0, direction="PE", now_sec=110.0)
    assert not allowed
    assert "veto_firm_pe_wall_zone1_23650" in reason

