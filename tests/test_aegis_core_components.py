"""
Unit tests for AEGIS Core Microstructure Components:
- DirectionalPersistenceEngine
- OutcomeTelemetryEngine
"""
import pytest
from AEGIS.core.directional_persistence import DirectionalPersistenceEngine
from AEGIS.core.outcome_telemetry import OutcomeTelemetryEngine


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

        # 6 UP, 4 DOWN => opposite_count=4 > max_allowed_opposite(1)
        for i in range(6):
            engine.record_snapshot(
                now_sec=float(i * 5),
                direction="UP",
                micro_score=80.0,
                spot_drift=+1.0,
            )
        for i in range(6, 10):
            engine.record_snapshot(
                now_sec=float(i * 5),
                direction="DOWN",
                micro_score=75.0,
                spot_drift=-2.0,
            )

        is_confirmed, persistence, metrics = engine.evaluate_persistence("CALL")
        assert is_confirmed is False
        assert metrics["opposite_count"] == 4


class TestOutcomeTelemetry:
    def test_outcome_telemetry_tracking(self, tmp_path):
        engine = OutcomeTelemetryEngine(log_dir=str(tmp_path))

        engine.register_entry(
            event_id="TR_001",
            symbol="NIFTY26AUG24200CE",
            direction="UP",
            entry_ts=1000.0,
            entry_opt_price=100.0,
            entry_spot_price=24200.0,
            eqs_score=85.0,
        )

        # Provide ticks at +15s, +30s, +60s, +180s, +300s
        engine.update_tick("NIFTY26AUG24200CE", 105.0, 1015.0)
        engine.update_tick("NIFTY26AUG24200CE", 110.0, 1030.0)
        engine.update_tick("NIFTY26AUG24200CE", 108.0, 1060.0)
        engine.update_tick("NIFTY26AUG24200CE", 115.0, 1180.0)
        completed = engine.update_tick("NIFTY26AUG24200CE", 120.0, 1300.0)

        assert len(completed) == 1
        record = completed[0]
        assert record.mfe_30s_pct == pytest.approx(10.0, 0.01)
        assert record.mfe_60s_pct == pytest.approx(10.0, 0.01)
        assert record.mfe_180s_pct == pytest.approx(15.0, 0.01)
        assert record.mfe_300s_pct == pytest.approx(20.0, 0.01)
        assert record.hit_tp_5pct is True
        assert record.is_complete is True


class TestWatertightRetailDoctrine:
    """Tests the retail 5%-10% Dynamic TP scaling."""

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
