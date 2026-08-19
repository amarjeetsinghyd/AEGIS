"""
Unit tests for EOD training + compilation pipeline.
"""
import json
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ARGUS.eod.train_compile import (
    select_features_mi_lgb,
    train_lightgbm_binary,
    compile_model_to_python,
    save_compiled_predictor,
    load_compiled_predictor,
    RegimeAwareCalibrator,
)


class TestFeatureSelection:
    def test_select_features_mi_lgb(self):
        np.random.seed(42)
        n = 1000
        n_features = 20
        
        X = pd.DataFrame(
            np.random.randn(n, n_features),
            columns=[f"feat_{i}" for i in range(n_features)]
        )
        # Make first 5 features predictive
        y = (X["feat_0"] + X["feat_1"] * 0.5 + np.random.randn(n) * 0.1 > 0).astype(int)
        
        selected = select_features_mi_lgb(X, y, top_k=8)
        
        assert len(selected) == 8
        assert "feat_0" in selected
        assert "feat_1" in selected
    
    def test_select_features_few_samples(self):
        np.random.seed(42)
        X = pd.DataFrame(np.random.randn(100, 5), columns=[f"f{i}" for i in range(5)])
        y = pd.Series(np.random.randint(0, 2, 100))
        
        selected = select_features_mi_lgb(X, y, top_k=10)
        assert len(selected) <= 5


class TestLightGBMTraining:
    def test_train_lightgbm_binary(self):
        np.random.seed(42)
        n = 2000
        X = pd.DataFrame({
            "f1": np.random.randn(n),
            "f2": np.random.randn(n),
            "f3": np.random.randn(n),
        })
        y = (X["f1"] + X["f2"] * 0.5 > 0).astype(int)
        
        model = train_lightgbm_binary(X, y, max_depth=3, n_estimators=20)
        
        assert model is not None
        assert model.n_estimators == 20
        preds = model.predict(X)
        assert preds.shape == (n,)
        assert np.all((preds >= 0) & (preds <= 1))
    
    def test_train_lightgbm_deterministic(self):
        np.random.seed(42)
        X = pd.DataFrame(np.random.randn(500, 3), columns=["a", "b", "c"])
        y = pd.Series(np.random.randint(0, 2, 500))
        
        model1 = train_lightgbm_binary(X, y, max_depth=3, n_estimators=10)
        model2 = train_lightgbm_binary(X, y, max_depth=3, n_estimators=10)
        
        preds1 = model1.predict(X)
        preds2 = model2.predict(X)
        np.testing.assert_allclose(preds1, preds2, rtol=1e-6)


class TestCompilation:
    def test_compile_model_to_python(self):
        np.random.seed(42)
        X = pd.DataFrame(np.random.randn(500, 4), columns=["a", "b", "c", "d"])
        y = pd.Series(np.random.randint(0, 2, 500))
        
        model = train_lightgbm_binary(X, y, max_depth=3, n_estimators=10)
        py_code = compile_model_to_python(model, list(X.columns))
        
        assert isinstance(py_code, str)
        assert "def predict" in py_code
        # m2cgen uses math module, not numpy
        assert "import math" in py_code
        assert "sigmoid" in py_code

    def test_save_load_compiled_predictor(self):
        np.random.seed(42)
        X = pd.DataFrame(np.random.randn(500, 3), columns=["a", "b", "c"])
        y = pd.Series(np.random.randint(0, 2, 500))
        
        model = train_lightgbm_binary(X, y, max_depth=3, n_estimators=10)
        py_code = compile_model_to_python(model, list(X.columns))
        
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            save_compiled_predictor(
                "TEST", "TRENDING_UP_SURGE", py_code,
                list(X.columns),
                X.mean().values,
                X.std().replace(0, 1).values,
                0.55,
                out_dir
            )
            
            # Load back
            predictor_fn, meta = load_compiled_predictor("TEST", "TRENDING_UP_SURGE", out_dir)
            
            assert callable(predictor_fn)
            assert meta["symbol"] == "TEST"
            assert meta["regime"] == "TRENDING_UP_SURGE"
            assert meta["threshold"] == 0.55
            assert len(meta["feature_names"]) == 3
            
            # Test prediction
            test_features = np.array([0.1, -0.2, 0.3])
            prob = predictor_fn(test_features)
            assert 0 <= prob <= 1


class TestRegimeAwareCalibrator:
    def test_calibrator_initialization(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            calibrator = RegimeAwareCalibrator(
                raw_data_dir=Path(tmpdir) / "raw",
                output_dir=Path(tmpdir) / "out",
                n_regimes=4,
                max_depth=3,
                n_estimators=10,
                top_k_features=6,
            )
            
            assert calibrator.n_regimes == 4
            assert calibrator.max_depth == 3
            assert calibrator.n_estimators == 10
            assert calibrator.top_k_features == 6
    
    def test_prepare_tick_arrays(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            calibrator = RegimeAwareCalibrator(
                raw_data_dir=Path(tmpdir),
                output_dir=Path(tmpdir),
            )
            
            # Create mock DataFrame
            n = 100
            df = pd.DataFrame({
                "lp": np.ones(n) * 100 + np.cumsum(np.random.randn(n) * 0.01),
                "vol": np.abs(np.random.randn(n)) * 1000 + 100,
                "ts": np.arange(n, dtype=np.int64) * 1000,
                "bp1": np.ones(n) * 99.95,
                "sp1": np.ones(n) * 100.05,
                "bq1": np.ones(n) * 1000,
                "sq1": np.ones(n) * 1000,
            })
            for l in range(2, 6):
                df[f"bp{l}"] = df["bp1"]
                df[f"sp{l}"] = df["sp1"]
                df[f"bq{l}"] = df["bq1"]
                df[f"sq{l}"] = df["sq1"]
            
            tick_data = calibrator._prepare_tick_arrays(df)
            
            assert "prices" in tick_data
            assert "volumes" in tick_data
            assert "timestamps" in tick_data
            assert "bid_px" in tick_data
            assert tick_data["bid_px"].shape == (n, 5)
            assert "buy_vol" in tick_data
            assert "sell_vol" in tick_data


class TestThresholdOptimization:
    def test_optimize_threshold(self):
        np.random.seed(42)
        X = pd.DataFrame(np.random.randn(1000, 3), columns=["a", "b", "c"])
        y = pd.Series(np.random.randint(0, 2, 1000))
        
        model = train_lightgbm_binary(X, y, max_depth=3, n_estimators=20)
        
        with tempfile.TemporaryDirectory() as tmpdir:
            calibrator = RegimeAwareCalibrator(
                raw_data_dir=Path(tmpdir),
                output_dir=Path(tmpdir),
            )
            
            threshold = calibrator._optimize_threshold(model, X.values, y.values)
            
            assert 0.1 <= threshold <= 0.9


if __name__ == "__main__":
    pytest.main([__file__, "-v"])