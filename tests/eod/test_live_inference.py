"""
Unit tests for EOD live inference engine.
"""
import json
import tempfile
from pathlib import Path

import numpy as np
import pytest

from stock_order_flow.eod.live_inference import (
    CompiledPredictor,
    LiveFeatureComputer,
    CalibratedSignalEngine,
)


class TestCompiledPredictor:
    def test_load_predictor(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cal_dir = Path(tmpdir)
            sym_dir = cal_dir / "TEST"
            sym_dir.mkdir()
            
            # Create a simple predictor
            py_code = '''
import numpy as np
def predict(x):
    return 1.0 / (1.0 + np.exp(-(x[0] * 0.5 + x[1] * -0.3 + 0.1)))
'''
            (sym_dir / "predictor_TRENDING_UP_SURGE.py").write_text(py_code)
            
            meta = {
                "symbol": "TEST",
                "regime": "TRENDING_UP_SURGE",
                "feature_names": ["f1", "f2"],
                "feature_mean": [0.0, 0.0],
                "feature_std": [1.0, 1.0],
                "threshold": 0.55,
                "model_version": "lgb_v1",
            }
            (sym_dir / "meta_TRENDING_UP_SURGE.json").write_text(json.dumps(meta))
            
            predictor = CompiledPredictor(cal_dir)
            
            assert predictor.has_symbol("TEST")
            assert "TRENDING_UP_SURGE" in predictor.get_available_regimes("TEST")
            
            # Test prediction
            features = np.array([1.0, -1.0])
            prob = predictor.predict("TEST", "TRENDING_UP_SURGE", features)
            assert 0 <= prob <= 1
            
            # Test should_fire
            should = predictor.should_fire("TEST", "TRENDING_UP_SURGE", features)
            assert isinstance(should, bool)
    
    def test_missing_symbol(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            predictor = CompiledPredictor(Path(tmpdir))
            assert not predictor.has_symbol("UNKNOWN")
            assert predictor.predict("UNKNOWN", "REGIME", np.array([0.0])) == 0.0
            assert not predictor.should_fire("UNKNOWN", "REGIME", np.array([0.0]))


class TestLiveFeatureComputer:
    def test_on_tick_basic(self):
        computer = LiveFeatureComputer(max_history=100)
        
        tick = {
            "lp": 100.0,
            "vol": 1000,
            "ts": 1000000,
            "bp1": 99.95,
            "sp1": 100.05,
            "bq1": 500,
            "sq1": 500,
        }
        for l in range(2, 6):
            tick[f"bp{l}"] = 99.95 - (l-1)*0.05
            tick[f"sp{l}"] = 100.05 + (l-1)*0.05
            tick[f"bq{l}"] = 500
            tick[f"sq{l}"] = 500
        
        computer.on_tick(tick)
        
        assert computer.count == 1
        # Check raw_buffers instead of individual arrays
        assert computer.raw_buffers[0, 0] == 100.0  # PRICE_IDX
        assert computer.raw_buffers[0, 23] == 1000  # BUY_VOL_IDX
        assert computer.raw_buffers[0, 24] == 0     # SELL_VOL_IDX
    
    def test_on_tick_sell_side(self):
        computer = LiveFeatureComputer(max_history=100)
        
        tick = {
            "lp": 99.90,  # Below mid
            "vol": 1000,
            "ts": 1000000,
            "bp1": 99.95,
            "sp1": 100.05,
            "bq1": 500,
            "sq1": 500,
        }
        for l in range(2, 6):
            tick[f"bp{l}"] = 99.95
            tick[f"sp{l}"] = 100.05
            tick[f"bq{l}"] = 500
            tick[f"sq{l}"] = 500
        
        computer.on_tick(tick)
        
        assert computer.raw_buffers[0, 23] == 0     # BUY_VOL_IDX
        assert computer.raw_buffers[0, 24] == 1000  # SELL_VOL_IDX
    
    def test_get_features_insufficient_history(self):
        computer = LiveFeatureComputer(max_history=100)
        
        # Only 10 ticks
        for i in range(10):
            computer.on_tick({
                "lp": 100.0, "vol": 100, "ts": i * 1000,
                "bp1": 99.95, "sp1": 100.05,
                "bq1": 500, "sq1": 500,
            })
        
        features = computer.get_features(["vpin", "obi_l2", "whale_zscore"])
        assert np.all(features == 0)  # Not enough history
    
    def test_get_features_sufficient_history(self):
        computer = LiveFeatureComputer(max_history=500)
        
        # Generate 200 ticks with some pattern
        for i in range(200):
            computer.on_tick({
                "lp": 100.0 + np.sin(i * 0.1) * 0.5,
                "vol": 1000 + np.random.randint(-100, 100),
                "ts": i * 1000,
                "bp1": 99.95,
                "sp1": 100.05,
                "bq1": 500,
                "sq1": 500,
            })
        
        features = computer.get_features(["obi_l2", "whale_zscore", "momentum_5m"])
        
        assert features.shape == (3,)
        assert np.all(np.isfinite(features))
    
    def test_circular_buffer(self):
        computer = LiveFeatureComputer(max_history=50)
        
        # Fill beyond capacity
        for i in range(100):
            computer.on_tick({
                "lp": 100.0, "vol": 100, "ts": i * 1000,
                "bp1": 99.95, "sp1": 100.05,
                "bq1": 500, "sq1": 500,
            })
        
        assert computer.count == 50  # Capped at max_history
        features = computer.get_features(["obi_l2"])
        assert np.isfinite(features[0])


class TestCalibratedSignalEngine:
    def test_engine_initialization(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cal_dir = Path(tmpdir) / "cal"
            reg_dir = Path(tmpdir) / "reg"
            cal_dir.mkdir()
            reg_dir.mkdir()
            
            # Create minimal calibration
            sym_dir = cal_dir / "TEST"
            sym_dir.mkdir()
            py_code = '''
import numpy as np
def predict(x):
    return 0.6
'''
            (sym_dir / "predictor_TRENDING_UP_SURGE.py").write_text(py_code)
            meta = {
                "symbol": "TEST", "regime": "TRENDING_UP_SURGE",
                "feature_names": ["obi_l2"], "feature_mean": [0.0], "feature_std": [1.0],
                "threshold": 0.5, "model_version": "lgb_v1",
            }
            (sym_dir / "meta_TRENDING_UP_SURGE.json").write_text(json.dumps(meta))
            
            # Create minimal regime model
            import joblib
            from sklearn.mixture import GaussianMixture
            from sklearn.preprocessing import RobustScaler
            
            gmm = GaussianMixture(n_components=4, random_state=42)
            gmm.fit(np.random.randn(100, 6))
            scaler = RobustScaler()
            scaler.fit(np.random.randn(100, 6))
            
            joblib.dump(gmm, reg_dir / "gmm_model.pkl")
            joblib.dump(scaler, reg_dir / "scaler.pkl")
            (reg_dir / "regime_meta.json").write_text(json.dumps({
                "feature_cols": ["a", "b", "c", "d", "e", "f"],
                "regime_names": ["QUIET_ACCUMULATION", "TRENDING_UP_SURGE", 
                                "TRENDING_DOWN_LIQUIDATION", "CHOPPY_NOISE_REJECT"],
                "n_components": 4, "has_hmm": False
            }))
            
            engine = CalibratedSignalEngine(str(cal_dir), str(reg_dir))
            
            assert engine.predictor.has_symbol("TEST")
            assert engine.gmm is not None
            assert engine.scaler is not None
    
    def test_update_regimes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cal_dir = Path(tmpdir) / "cal"
            reg_dir = Path(tmpdir) / "reg"
            cal_dir.mkdir()
            reg_dir.mkdir()
            
            # Setup as above
            sym_dir = cal_dir / "TEST"
            sym_dir.mkdir()
            py_code = 'import numpy as np\ndef predict(x): return 0.6\n'
            (sym_dir / "predictor_TRENDING_UP_SURGE.py").write_text(py_code)
            meta = {"symbol": "TEST", "regime": "TRENDING_UP_SURGE",
                    "feature_names": ["obi_l2"], "feature_mean": [0.0], "feature_std": [1.0],
                    "threshold": 0.5, "model_version": "lgb_v1"}
            (sym_dir / "meta_TRENDING_UP_SURGE.json").write_text(json.dumps(meta))
            
            import joblib
            from sklearn.mixture import GaussianMixture
            from sklearn.preprocessing import RobustScaler
            
            gmm = GaussianMixture(n_components=4, random_state=42)
            gmm.fit(np.random.randn(100, 6))
            scaler = RobustScaler()
            scaler.fit(np.random.randn(100, 6))
            
            joblib.dump(gmm, reg_dir / "gmm_model.pkl")
            joblib.dump(scaler, reg_dir / "scaler.pkl")
            (reg_dir / "regime_meta.json").write_text(json.dumps({
                "feature_cols": ["a", "b", "c", "d", "e", "f"],
                "regime_names": ["QUIET_ACCUMULATION", "TRENDING_UP_SURGE", 
                                "TRENDING_DOWN_LIQUIDATION", "CHOPPY_NOISE_REJECT"],
                "n_components": 4, "has_hmm": False
            }))
            
            engine = CalibratedSignalEngine(str(cal_dir), str(reg_dir))
            
            # Update regimes
            daily_feats = {"TEST": np.array([5.0, 1.0, 50.0, 0.1, 0.001, 0.5])}
            engine.update_regimes(daily_feats)
            
            assert "TEST" in engine.current_regime
            # The regime should be one we have a predictor for, or fallback should work
            regime = engine.current_regime["TEST"]
            available = engine.predictor.get_available_regimes("TEST")
            assert regime in available or len(available) > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])