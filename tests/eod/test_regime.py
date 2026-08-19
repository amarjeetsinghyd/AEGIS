"""
Unit tests for EOD regime clustering (GMM + HMM).
"""
import numpy as np
import pandas as pd
import pytest

from ARGUS.eod.regime import (
    fit_gmm_hmm,
    predict_next_day_regime,
    get_regime_params,
    REGIME_NAMES,
    FEATURE_COLS,
)


class TestRegimeClustering:
    def test_fit_gmm_basic(self):
        # Create synthetic daily data
        np.random.seed(42)
        n_samples = 100
        daily_df = pd.DataFrame({
            "symbol": np.random.choice(["RELIANCE", "TCS", "INFY"], n_samples),
            "date": pd.date_range("2024-01-01", periods=n_samples, freq="D").astype(str),
            "avg_spread_bps": np.random.uniform(2, 20, n_samples),
            "spread_vol": np.random.uniform(0.5, 5, n_samples),
            "trade_intensity": np.random.uniform(10, 200, n_samples),
            "obi_mean": np.random.uniform(-0.5, 0.5, n_samples),
            "kyle_lambda": np.random.uniform(0, 0.01, n_samples),
            "day_return_pct": np.random.uniform(-2, 2, n_samples),
        })
        
        gmm, hmm_model, scaler, feature_cols = fit_gmm_hmm(daily_df, n_regimes=4)
        
        assert gmm is not None
        assert gmm.n_components == 4
        assert scaler is not None
        assert feature_cols == FEATURE_COLS
    
    def test_fit_gmm_auto_components(self):
        np.random.seed(42)
        n_samples = 50
        daily_df = pd.DataFrame({
            "symbol": ["RELIANCE"] * n_samples,
            "date": pd.date_range("2024-01-01", periods=n_samples, freq="D").astype(str),
            "avg_spread_bps": np.random.uniform(2, 20, n_samples),
            "spread_vol": np.random.uniform(0.5, 5, n_samples),
            "trade_intensity": np.random.uniform(10, 200, n_samples),
            "obi_mean": np.random.uniform(-0.5, 0.5, n_samples),
            "kyle_lambda": np.random.uniform(0, 0.01, n_samples),
            "day_return_pct": np.random.uniform(-2, 2, n_samples),
        })
        
        gmm, hmm_model, scaler, feature_cols = fit_gmm_hmm(daily_df, n_regimes=None)
        
        assert gmm is not None
        assert 2 <= gmm.n_components <= 6
    
    def test_predict_next_day_regime(self):
        np.random.seed(42)
        n_samples = 100
        daily_df = pd.DataFrame({
            "symbol": ["RELIANCE"] * n_samples,
            "date": pd.date_range("2024-01-01", periods=n_samples, freq="D").astype(str),
            "avg_spread_bps": np.random.uniform(2, 20, n_samples),
            "spread_vol": np.random.uniform(0.5, 5, n_samples),
            "trade_intensity": np.random.uniform(10, 200, n_samples),
            "obi_mean": np.random.uniform(-0.5, 0.5, n_samples),
            "kyle_lambda": np.random.uniform(0, 0.01, n_samples),
            "day_return_pct": np.random.uniform(-2, 2, n_samples),
        })
        
        gmm, hmm_model, scaler, feature_cols = fit_gmm_hmm(daily_df, n_regimes=4)
        
        # Predict for latest day - need 2D array for score_samples
        latest = daily_df.iloc[-1][FEATURE_COLS].values.reshape(1, -1)
        probs = predict_next_day_regime("RELIANCE", latest, gmm, hmm_model, scaler)
        
        assert isinstance(probs, dict)
        assert len(probs) == 4
        assert abs(sum(probs.values()) - 1.0) < 1e-6
        assert all(0 <= p <= 1 for p in probs.values())
        assert all(name in REGIME_NAMES for name in probs.keys())


class TestRegimeParams:
    def test_get_regime_params_all(self):
        for regime in REGIME_NAMES:
            params = get_regime_params(regime)
            assert "conv_burst" in params
            assert "conv_grind" in params
            assert "min_ticks" in params
            assert "whale_mult" in params
            assert "branching_mult" in params
            assert params["conv_burst"] > params["conv_grind"]
    
    def test_get_regime_params_fallback(self):
        params = get_regime_params("UNKNOWN_REGIME")
        assert params == get_regime_params("BALANCED_TREND_GRINDER")
    
    def test_choppy_noise_reject_strict(self):
        params = get_regime_params("CHOPPY_NOISE_REJECT")
        assert params["conv_burst"] >= 6.0
        assert params["min_ticks"] >= 4
    
    def test_trending_surge_fast(self):
        params = get_regime_params("TRENDING_UP_SURGE")
        assert params["conv_burst"] <= 5.0
        assert params["min_ticks"] <= 2


class TestRegimeMapping:
    def test_regime_names_order(self):
        assert REGIME_NAMES[0] == "QUIET_ACCUMULATION"
        assert REGIME_NAMES[1] == "TRENDING_UP_SURGE"
        assert REGIME_NAMES[2] == "TRENDING_DOWN_LIQUIDATION"
        assert REGIME_NAMES[3] == "CHOPPY_NOISE_REJECT"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])