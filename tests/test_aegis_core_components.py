"""
Unit tests for AEGIS Core Microstructure Components:
- DirectionalPersistenceEngine
- EntryQualityEngine (EQS)
- OutcomeTelemetryEngine
- SignalStateMachine (FSM)
- TimeBarEngine & Market Structure
- RegimeChopGate
"""
import pytest
from AEGIS.core.directional_persistence import DirectionalPersistenceEngine
from AEGIS.core.entry_quality_engine import EntryQualityEngine
from AEGIS.core.outcome_telemetry import OutcomeTelemetryEngine
from AEGIS.core.signal_state_machine import SignalStateMachine, SignalState
from AEGIS.core.time_bar_engine import TimeBarEngine, OHLCBar
from AEGIS.core.regime_chop_gate import RegimeChopGate


class TestDirectionalPersistence:
    def test_directional_persistence_confirmed(self):
        engine = DirectionalPersistenceEngine(
            window_sec=60.0,
            sample_interval_sec=5.0,
            target_persistence_threshold=0.70,
            max_allowed_opposite=1,
        )

        for i in range(10):
            engine.record_snapshot(
                now_sec=float(i * 5),
                direction="UP",
                micro_score=82.0,
                spot_drift=+2.0,
            )
        for i in range(10, 12):
            engine.record_snapshot(
                now_sec=float(i * 5),
                direction="NEUTRAL",
                micro_score=60.0,
                spot_drift=0.0,
            )

        is_confirmed, persistence, metrics = engine.evaluate_persistence("CALL")
        assert is_confirmed is True
        assert persistence == pytest.approx(10.0 / 12.0, 0.01)
        assert metrics["opposite_count"] == 0

    def test_directional_persistence_opposite_invalidation(self):
        engine = DirectionalPersistenceEngine(
            window_sec=60.0,
            sample_interval_sec=5.0,
            target_persistence_threshold=0.70,
            max_allowed_opposite=1,
        )

        for i in range(8):
            engine.record_snapshot(float(i * 5), "UP", 80.0, +2.0)
        for i in range(8, 11):
            engine.record_snapshot(float(i * 5), "DOWN", 80.0, -2.0)
        engine.record_snapshot(55.0, "NEUTRAL", 60.0, 0.0)

        is_confirmed, persistence, metrics = engine.evaluate_persistence("CALL")
        assert is_confirmed is False
        assert metrics["opposite_count"] == 3
        assert "excessive_opposite" in metrics["reason"]


class TestEntryQualityEngine:
    def test_eqs_computation_aligned(self):
        eqs_engine = EntryQualityEngine(dispatch_threshold=75.0)

        score, is_qual, breakdown = eqs_engine.compute_eqs(
            ema_1m_slope=1.5,
            ema_3m_slope=1.2,
            atr_ratio=1.25,
            market_structure="ALIGNED",
            direction="UP",
            persistence_score=0.85,
            opposite_count=0,
            micro_score=85.0,
            trigger_age_sec=1.5,
            spread_pct=0.8,
            total_depth_qty=3000.0,
        )

        assert is_qual is True
        assert score >= 75.0
        assert breakdown["permission_pts"] > 20.0
        assert breakdown["persistence_pts"] > 20.0
        assert breakdown["trigger_pts"] > 20.0

    def test_eqs_opposed_structure_veto(self):
        eqs_engine = EntryQualityEngine(dispatch_threshold=75.0)

        score, is_qual, breakdown = eqs_engine.compute_eqs(
            ema_1m_slope=1.5,
            ema_3m_slope=1.2,
            atr_ratio=1.25,
            market_structure="OPPOSED",
            direction="UP",
            persistence_score=0.85,
            opposite_count=0,
            micro_score=85.0,
            trigger_age_sec=1.5,
            spread_pct=0.8,
            total_depth_qty=3000.0,
        )

        assert is_qual is False
        assert score == 0.0
        assert "opposed" in breakdown["reason"]


class TestOutcomeTelemetry:
    def test_outcome_telemetry_tracking(self, tmp_path):
        log_dir = str(tmp_path / "telemetry")
        engine = OutcomeTelemetryEngine(log_dir=log_dir)

        engine.register_entry(
            event_id="evt_001",
            symbol="NIFTY01SEP26C24000",
            direction="UP",
            entry_ts=100.0,
            entry_opt_price=100.0,
            entry_spot_price=24000.0,
            eqs_score=82.5,
        )

        engine.update_tick("NIFTY01SEP26C24000", 103.0, 110.0)
        engine.update_tick("NIFTY01SEP26C24000", 106.0, 120.0)
        engine.update_tick("NIFTY01SEP26C24000", 108.0, 390.0)
        completed = engine.update_tick("NIFTY01SEP26C24000", 108.0, 410.0)

        assert len(completed) == 1
        rec = completed[0]
        assert rec.hit_tp_5pct is True
        assert rec.time_to_tp_5pct_sec == 20.0
        assert rec.mfe_300s_pct == pytest.approx(8.0, 0.01)
        assert rec.is_complete is True


class TestSignalStateMachine:
    def test_fsm_lifecycle_transitions(self):
        fsm = SignalStateMachine(
            max_trigger_freshness_sec=3.0,
            max_trigger_acceptable_sec=8.0,
            cooldown_duration_sec=120.0,
        )
        sym = "NIFTY01SEP26C24100"

        # Step 1: Score < 78 does not prime
        st, fire, reason = fsm.update_trigger(
            sym=sym,
            now_sec=100.0,
            direction="UP",
            micro_score=72.0,
            has_permission=True,
            persistence_confirmed=False,
            option_qualified=True,
        )
        assert st == SignalState.OBSERVE
        assert fire is False

        # Step 2: High score primes
        st, fire, reason = fsm.update_trigger(
            sym=sym,
            now_sec=105.0,
            direction="UP",
            micro_score=81.0,
            has_permission=True,
            persistence_confirmed=False,
            option_qualified=True,
        )
        assert st == SignalState.PRIMED
        assert fire is False

        # Step 3: Persistence confirmed + fresh trigger -> FIRED
        st, fire, reason = fsm.update_trigger(
            sym=sym,
            now_sec=106.0,
            direction="UP",
            micro_score=80.0,
            has_permission=True,
            persistence_confirmed=True,
            option_qualified=True,
        )
        assert st == SignalState.FIRED
        assert fire is True

        # Step 4: After firing -> COOLDOWN
        st, fire, reason = fsm.update_trigger(
            sym=sym,
            now_sec=107.0,
            direction="UP",
            micro_score=85.0,
            has_permission=True,
            persistence_confirmed=True,
            option_qualified=True,
        )
        assert st == SignalState.COOLDOWN
        assert fire is False


class TestTimeBarEngine:
    def test_time_bar_aggregation(self):
        engine = TimeBarEngine(ema_period=9, atr_period=14)

        for s in range(0, 60, 5):
            engine.update_tick(price=24000.0 + s * 0.1, ts_sec=float(s))

        assert len(engine.bars_1m) == 0

        n1, n3 = engine.update_tick(price=24010.0, ts_sec=60.0)
        assert n1 is True
        assert len(engine.bars_1m) == 1
        bar0 = engine.bars_1m[0]
        assert bar0.open == 24000.0
        assert bar0.close == 24000.0 + 55 * 0.1

        for s in range(65, 120, 5):
            engine.update_tick(price=24010.0 + (s - 60) * 0.2, ts_sec=float(s))

        n1, n3 = engine.update_tick(price=24030.0, ts_sec=120.0)
        assert n1 is True
        assert len(engine.bars_1m) == 2
        assert engine.ema_1m > 0
        assert engine.ema_1m_slope > 0

    def test_market_structure_detection(self):
        engine = TimeBarEngine()

        b1 = OHLCBar(0, 60, 24000, 24010, 23990, 24005)
        b2 = OHLCBar(60, 120, 24005, 24020, 24000, 24015)
        b3 = OHLCBar(120, 180, 24015, 24030, 24010, 24025)
        b4 = OHLCBar(180, 240, 24025, 24040, 24020, 24035)

        for bar in [b1, b2, b3, b4]:
            engine._on_1m_bar_close(bar)

        assert engine.get_market_structure("CALL") == "ALIGNED"
        assert engine.get_market_structure("PUT") == "OPPOSED"

        has_perm, reason, metrics = engine.get_permission_state("PUT")
        assert has_perm is False
        assert "opposed" in reason.lower()


class TestRegimeChopGate:
    def test_regime_chop_gate_basic(self):
        gate = RegimeChopGate(ker_window_sec=30.0, range_ref_window_sec=300.0, cooldown_sec=60.0)
        can_trade, reason, metrics = gate.update(24150.0, 100.0)
        assert not can_trade
        assert reason == "warmup"

    def test_regime_chop_gate_trending_vs_choppy(self):
        gate = RegimeChopGate(ker_window_sec=30.0, range_ref_window_sec=300.0, ker_veto_threshold=0.30, range_compression_veto=0.10)
        t = 100.0
        price = 24150.0
        for i in range(100):
            t += 0.5
            price = 24150.0 + (1.0 if i % 2 == 0 else -1.0)
            can_trade, reason, metrics = gate.update(price, t)

        assert metrics["ker"] < 0.20
        assert not can_trade
        assert reason in ("low_efficiency_chop", "range_compressed", "mean_reversion_signature")

        gate_trend = RegimeChopGate(ker_window_sec=30.0, range_ref_window_sec=300.0, range_compression_veto=0.0)
        t = 100.0
        price = 24150.0
        for i in range(60):
            t += 0.5
            price += 1.0
            can_trade, reason, metrics = gate_trend.update(price, t)

        assert metrics["ker"] > 0.90
        assert can_trade
        assert reason == "regime_ok"

    def test_regime_chop_gate_cooldown(self):
        gate = RegimeChopGate(cooldown_sec=120.0)
        gate.register_trade_close(500.0)

        for i in range(10):
            can_trade, reason, m = gate.update(24150.0 + i, 550.0 + i)
        assert not can_trade
        assert reason == "post_trade_cooldown"


class TestWatertightRetailDoctrine:
    """Tests the retail 5%-10% Dynamic TP, Breakeven Guard, and Runner Lock."""

    def test_dynamic_tp_calculation_scaling(self):
        # ATR=1.0, EQS=75 -> 5.0% + 2.0%*(5/30) = 5.33% -> clamped to min 5.5%
        dtp_1 = round(min(0.10, max(0.055, 0.050 * 1.0 + 0.020 * max(0.0, (75.0 - 70.0) / 30.0))), 4)
        assert dtp_1 == 0.055

        # ATR=1.25, EQS=82 -> 5.0%*1.25 + 2.0%*(12/30) = 6.25% + 0.8% = 7.05%
        dtp_2 = round(min(0.10, max(0.055, 0.050 * 1.25 + 0.020 * max(0.0, (82.0 - 70.0) / 30.0))), 4)
        assert dtp_2 == 0.0705

        # ATR=1.6, EQS=95 -> 5.0%*1.6 + 2.0%*(25/30) = 8.0% + 1.67% = 9.67%
        dtp_3 = round(min(0.10, max(0.055, 0.050 * 1.6 + 0.020 * max(0.0, (95.0 - 70.0) / 30.0))), 4)
        assert dtp_3 == 0.0967

        # Hyper-volatility clamped to max 10.0%
        dtp_4 = round(min(0.10, max(0.055, 0.050 * 2.5 + 0.020 * max(0.0, (100.0 - 70.0) / 30.0))), 4)
        assert dtp_4 == 0.10

    def test_fsm_trigger_refresh_after_persistence(self):
        fsm = SignalStateMachine(max_trigger_freshness_sec=3.0, prime_timeout_sec=60.0)
        sym = "NIFTY26AUG24200CE"

        # T=100s: Primes
        st, fire, _ = fsm.update_trigger(sym, 100.0, "UP", 79.0, True, False, True)
        assert st == SignalState.PRIMED
        assert not fire

        # T=104s: Persistence confirms (>3s from prime) -> transitions to CONFIRMED
        st, fire, reason = fsm.update_trigger(sym, 104.0, "UP", 65.0, True, True, True)
        assert st == SignalState.CONFIRMED
        assert not fire
        assert reason == "persistence_confirmed"

        # T=106s: Fresh strong burst arrives -> MUST FIRE IMMEDIATELY!
        st, fire, reason = fsm.update_trigger(sym, 106.0, "UP", 82.0, True, True, True)
        assert st == SignalState.FIRED
        assert fire is True
        assert "fresh_trigger_fired" in reason

    def test_breakeven_guard_and_runner_parameters(self):
        from AEGIS.aegis_execution_manager import (
            TARGET_PROFIT_PCT,
            STOP_LOSS_PCT,
            BREAKEVEN_TRIGGER_PCT,
            BREAKEVEN_LOCK_PCT,
            RUNNER_TRIGGER_PCT,
            RUNNER_FLOOR_PCT,
        )
        # Verify calibrated parameters are mathematically sound
        assert TARGET_PROFIT_PCT >= 0.055
        assert STOP_LOSS_PCT == 0.035
        assert BREAKEVEN_TRIGGER_PCT == 0.030
        assert BREAKEVEN_LOCK_PCT == 0.005  # Covers ~0.35% roundtrip charges
        assert RUNNER_TRIGGER_PCT == 0.070
        assert RUNNER_FLOOR_PCT == 0.050    # Locks minimum +5.0% on runner pullback

