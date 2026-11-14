"""
Group 3: Wyckoff Fallback Return, Scale-Invariance, TradeLeg PnL & WS Reconnect Patch

Covers:
1. WyckoffVolumeEngine.evaluate() never returns None (even without MODE_B tag in pa_details).
2. SENSEX-scale (80000) level dedup in StructuralRegistryMixin (bps-normalised tolerance).
   Lot-scaled Pillar 2 volume in WyckoffVolumeEngine (8.0 * eff_lot).
3. TradeLeg.close_leg PnL in true share units (65 NIFTY / 20 SENSEX).
   ShoonyaOrderLifecycle.execute_exit does NOT mark CLOSED prior to fill confirmation.
"""
import pytest

from core.wyckoff_volume_engine import WyckoffVolumeEngine
from core.price_action_engine import PriceActionEngine, OHLCBar
from core.structural_registry import StructuralLevel
from execution.trade_context import TradeLeg
from execution.order_lifecycle import ShoonyaOrderLifecycle


# ─────────────────────────────────────────────────────────────────────────────
# Fix 1: WyckoffVolumeEngine.evaluate() never returns None
# ─────────────────────────────────────────────────────────────────────────────

def test_wyckoff_evaluate_never_returns_none_without_mode_b_tag():
    """evaluate() must never return None even when pa_details has no MODE_B tag.
    Pure Live In-Flight Mode B is the sole entry mode — route unconditionally."""
    wy = WyckoffVolumeEngine(mode="ACTIVE")

    # No MODE_B tag, zero flow (ACTIVE mode) -> veto tuple, never None
    result = wy.evaluate(
        pillar=1,
        direction="UP",
        bars=[],
        atr=10.0,
        pa_details={"pa_setup_name": "some_setup", "mode": "MODE_A"},
    )
    assert result is not None
    passed, reason, metrics = result
    assert isinstance(passed, bool)
    assert isinstance(reason, str)
    assert isinstance(metrics, dict)
    assert passed is False  # zero flow in ACTIVE mode -> veto
    assert reason == "insufficient_intrabar_flow"

    # Sufficient flow -> in_flight_validated set, never None
    result_ok = wy.evaluate(
        pillar=1,
        direction="UP",
        bars=[],
        atr=10.0,
        pa_details={
            "pa_setup_name": "20_ema_rejection",
            "mode": "MODE_B_INFLIGHT_BREAKTHROUGH",
            "opt_total_vol": 3000.0,
            "opt_delta_flow": 2500.0,
            "bull_flow": 2750.0,
            "bear_flow": 250.0,
        },
    )
    assert result_ok is not None
    passed_ok, reason_ok, metrics_ok = result_ok
    assert passed_ok is True
    assert metrics_ok.get("in_flight_validated") is True

    # DISABLED mode still short-circuits cleanly
    wy_disabled = WyckoffVolumeEngine(mode="DISABLED")
    result_d = wy_disabled.evaluate(
        pillar=1, direction="UP", bars=[], atr=10.0,
        pa_details={"pa_setup_name": "x", "mode": "MODE_B"},
    )
    assert result_d == (True, "wyckoff_disabled", {})


# ─────────────────────────────────────────────────────────────────────────────
# Fix 2: Scale-invariance — SENSEX dedup + lot-scaled Pillar 2 volume
# ─────────────────────────────────────────────────────────────────────────────

def _make_bar(open_p, high_p, low_p, close_p, vol=1000.0, ts_start=1000.0):
    return OHLCBar(
        ts_start=float(ts_start),
        ts_end=float(ts_start + 60.0),
        open=float(open_p),
        high=float(high_p),
        low=float(low_p),
        close=float(close_p),
        volume=float(vol),
    )


def test_structural_registry_sensex_scale_dedup():
    """SENSEX-scale (80000) level dedup uses bps-normalised tolerance (~6.4 pts).
    max(0.5, 80000 * 0.00008) = 6.4 -> levels within 6.4 pts are duplicates."""
    pae = PriceActionEngine(strike_step=100.0)
    pae.current_atr_1m = 10.0
    pae.last_price = 80000.0
    pae.pdc = 80000.0
    now = 1792730040.0

    tol = pae._prox_tol()
    assert tol == pytest.approx(6.4), f"Expected ~6.4 pts for SENSEX, got {tol}"

    lvl1 = StructuralLevel(
        level_type="RESISTANCE", price=80100.0, ts_created=now,
        prominence=2.0, bar_idx=0, seq_idx=0,
    )
    pae.session_levels = [lvl1]

    # 5 pts apart -> within 6.4 pts tolerance -> treated as duplicate
    lvl2 = StructuralLevel(
        level_type="RESISTANCE", price=80105.0, ts_created=now,
        prominence=2.0, bar_idx=0, seq_idx=0,
    )
    is_dup = any(
        l.level_type == lvl2.level_type and abs(l.price - lvl2.price) < pae._prox_tol()
        for l in pae.session_levels
    )
    assert is_dup is True, "5 pts apart on SENSEX should be deduped (< 6.4 pts tol)"

    # 10 pts apart -> outside tolerance -> NOT a duplicate
    lvl3 = StructuralLevel(
        level_type="RESISTANCE", price=80110.0, ts_created=now,
        prominence=2.0, bar_idx=0, seq_idx=0,
    )
    is_dup3 = any(
        l.level_type == lvl3.level_type and abs(l.price - lvl3.price) < pae._prox_tol()
        for l in pae.session_levels
    )
    assert is_dup3 is False, "10 pts apart on SENSEX should NOT be deduped (> 6.4 pts tol)"

    # NIFTY cross-check: max(0.5, 24000 * 0.00008) = 1.92 -> ~2 pts
    pae_nifty = PriceActionEngine(strike_step=50.0)
    pae_nifty.last_price = 24000.0
    assert pae_nifty._prox_tol() == pytest.approx(1.92)


def test_wyckoff_pillar2_lot_scaled_volume_threshold():
    """Pillar 2 active sweep volume threshold is 8.0 * eff_lot (520 NIFTY / 160 SENSEX)."""
    # NIFTY: eff_lot=65 -> threshold = 8.0 * 65 = 520
    wy_nifty = WyckoffVolumeEngine(mode="ACTIVE")
    passed, _, _ = wy_nifty.evaluate(
        pillar=2,
        direction="UP",
        bars=[],
        atr=10.0,
        pa_details={
            "pillar": 2,
            "pa_setup_name": "sweep_reclaim",
            "mode": "MODE_B_INFLIGHT_BREAKTHROUGH",
            "lot_size": 65.0,
            "bull_flow": 300.0,
            "bear_flow": 200.0,   # total = 500 < 520 -> below active sweep floor
        },
    )
    assert passed is True, f"NIFTY 500 vol (< 520 threshold) should pass, got reason"

    # SENSEX: eff_lot=20 -> threshold = 8.0 * 20 = 160
    wy_sensex = WyckoffVolumeEngine(mode="ACTIVE")
    passed_s, _, _ = wy_sensex.evaluate(
        pillar=2,
        direction="UP",
        bars=[],
        atr=10.0,
        pa_details={
            "pillar": 2,
            "pa_setup_name": "sweep_reclaim",
            "mode": "MODE_B_INFLIGHT_BREAKTHROUGH",
            "lot_size": 20.0,
            "bull_flow": 100.0,
            "bear_flow": 50.0,    # total = 150 < 160 -> below active sweep floor
        },
    )
    assert passed_s is True, f"SENSEX 150 vol (< 160 threshold) should pass"

    # Above-threshold opposing dominance should still veto (sanity that threshold is active)
    wy_nifty2 = WyckoffVolumeEngine(mode="ACTIVE")
    passed_veto, reason_veto, _ = wy_nifty2.evaluate(
        pillar=2,
        direction="UP",
        bars=[],
        atr=10.0,
        pa_details={
            "pillar": 2,
            "pa_setup_name": "sweep_reclaim",
            "mode": "MODE_B_INFLIGHT_BREAKTHROUGH",
            "lot_size": 65.0,
            "bull_flow": 100.0,
            "bear_flow": 500.0,   # total = 600 >= 520 -> active; bear 500 > 2.2*100=220 -> veto
        },
    )
    assert passed_veto is False
    assert "opposing_put_dominance" in reason_veto


# ─────────────────────────────────────────────────────────────────────────────
# Fix 3: TradeLeg.close_leg PnL in share units; execute_exit not CLOSED pre-fill
# ─────────────────────────────────────────────────────────────────────────────

def test_tradeleg_close_leg_pnl_share_units():
    """close_leg computes PnL in true share units (65 NIFTY / 20 SENSEX), not lots."""
    # NIFTY: quantity=1 lot, lot_size=65 -> net_qty=65
    leg = TradeLeg("TOKEN", "NIFTY24500CE", 24500.0, "CALL", 1, 37.30)
    leg.close_leg(38.00, "TP", lot_size=65)
    assert leg.status == "CLOSED"
    assert leg.exit_price == 38.00
    assert leg.pnl == pytest.approx((38.00 - 37.30) * 65)  # 0.70 * 65 = 45.50

    # SENSEX: quantity=1 lot, lot_size=20 -> net_qty=20
    leg2 = TradeLeg("TOKEN", "SENSEX57800CE", 57800.0, "PUT", 1, 24.70)
    leg2.close_leg(24.55, "SL", lot_size=20)
    assert leg2.status == "CLOSED"
    assert leg2.pnl == pytest.approx((24.55 - 24.70) * 20)  # -0.15 * 20 = -3.00

    # Default lot_size=1 keeps canonical lots behaviour (back-compat)
    leg3 = TradeLeg("TOKEN", "NIFTY24500CE", 24500.0, "CALL", 1, 37.30)
    leg3.close_leg(38.00, "TP")  # lot_size defaults to 1
    assert leg3.pnl == pytest.approx((38.00 - 37.30) * 1)  # 0.70 * 1 lot


class DummyBrokerForExit:
    """Minimal broker that accepts the exit order but never confirms a fill."""
    def place_order(self, **kwargs):
        return {"stat": "Ok", "norenordno": "ORD123"}


def test_shoonya_execute_exit_does_not_mark_closed_before_fill():
    """execute_exit places the order but does NOT mark leg CLOSED until fill confirmed."""
    leg = TradeLeg("TOKEN", "NIFTY24500CE", 24500.0, "CALL", 1, 37.30)
    lifecycle = ShoonyaOrderLifecycle(DummyBrokerForExit())

    result = lifecycle.execute_exit(leg, "TP", exit_price=38.00)
    assert result is True
    # Leg must remain OPEN — broker fill not yet confirmed
    assert leg.status == "OPEN", "Leg must not be CLOSED before live fill confirmation"
    assert leg.exit_price is None
    assert leg.pnl == 0.0
    assert lifecycle._last_exit_order_id == "ORD123"
