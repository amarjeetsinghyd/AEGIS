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
