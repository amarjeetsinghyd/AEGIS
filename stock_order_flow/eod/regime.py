"""
GMM + HMM regime clustering for next-day regime prediction.
"""
from __future__ import annotations
import warnings
from pathlib import Path
from typing import Dict, List, Tuple, Optional

import numpy as np
import pandas as pd
from sklearn.mixture import GaussianMixture
from sklearn.preprocessing import RobustScaler

try:
    from hmmlearn import hmm
    HMM_AVAILABLE = True
except ImportError:
    HMM_AVAILABLE = False
    hmm = None

warnings.filterwarnings("ignore")


REGIME_NAMES = [
    "QUIET_ACCUMULATION",
    "TRENDING_UP_SURGE",
    "TRENDING_DOWN_LIQUIDATION", 
    "CHOPPY_NOISE_REJECT"
]

FEATURE_COLS = [
    "avg_spread_bps",
    "spread_vol",
    "trade_intensity",
    "obi_mean",
    "kyle_lambda",
    "day_return_pct",
]


def fit_gmm_hmm(daily_df: pd.DataFrame, 
                n_regimes: int = 4,
                random_state: int = 42) -> Tuple:
    """
    Fit GMM + HMM on daily microstructure summaries.
    
    Returns:
        (gmm, hmm_model, scaler, feature_cols)
    """
    # Prepare features
    X = daily_df[FEATURE_COLS].values
    scaler = RobustScaler()
    X_scaled = scaler.fit_transform(X)
    
    # Fit GMM - use BIC to select n_components if not specified
    if n_regimes is None:
        best_gmm, best_bic = None, np.inf
        for k in range(2, min(7, len(daily_df))):
            gmm = GaussianMixture(
                n_components=k, 
                covariance_type="full",
                random_state=random_state,
                n_init=10
            )
            gmm.fit(X_scaled)
            if gmm.bic(X_scaled) < best_bic:
                best_bic, best_gmm = gmm.bic(X_scaled), gmm
        gmm = best_gmm
        n_regimes = gmm.n_components
    else:
        gmm = GaussianMixture(
            n_components=min(n_regimes, len(daily_df)),
            covariance_type="full",
            random_state=random_state,
            n_init=10
        )
        gmm.fit(X_scaled)
    
    # Fit HMM for temporal persistence (if available)
    hmm_model = None
    if HMM_AVAILABLE and len(daily_df) > n_regimes * 2:
        hmm_model = fit_hmm_on_daily(daily_df, gmm, scaler)
    
    return gmm, hmm_model, scaler, FEATURE_COLS


def fit_hmm_on_daily(daily_df: pd.DataFrame,
                      gmm: GaussianMixture,
                      scaler: RobustScaler) -> hmm.GaussianHMM:
    """
    Fit HMM on regime sequences per symbol.
    Uses original scaled features as observations (not emission probabilities).
    """
    # Sort by symbol then date
    daily_df = daily_df.sort_values(["symbol", "date"]).reset_index(drop=True)
    
    # Get scaled features for HMM
    X = daily_df[FEATURE_COLS].values
    X_scaled = scaler.transform(X)
    
    # Group by symbol
    sequences = []
    lengths = []
    for symbol in daily_df["symbol"].unique():
        mask = daily_df["symbol"] == symbol
        seq = X_scaled[mask]
        if len(seq) > 1:
            sequences.append(seq)
            lengths.append(len(seq))
    
    if not sequences:
        return None
    
    # Fit HMM
    n_components = gmm.n_components
    hmm_model = hmm.GaussianHMM(
        n_components=n_components,
        covariance_type="diag",
        n_iter=100,
        random_state=42,
        init_params="stmc"
    )
    
    # Concatenate sequences
    X_hmm = np.vstack(sequences)
    try:
        hmm_model.fit(X_hmm, lengths=lengths)
    except Exception:
        return None
    
    return hmm_model


def predict_next_day_regime(symbol: str,
                            latest_daily_features: np.ndarray,
                            gmm: GaussianMixture,
                            hmm_model: Optional[hmm.GaussianHMM],
                            scaler: RobustScaler) -> Dict[str, float]:
    """
    Predict next-day regime probabilities for a symbol.
    
    Args:
        symbol: Stock symbol
        latest_daily_features: (6,) array of latest daily features
        gmm: Fitted GaussianMixture
        hmm_model: Fitted HMM (or None)
        scaler: Fitted RobustScaler
    
    Returns:
        Dict mapping regime_name -> probability
    """
    # GMM emission probabilities
    X_scaled = scaler.transform(latest_daily_features.reshape(1, -1))
    emission_probs = gmm.predict_proba(X_scaled)[0]  # (n_regimes,)
    
    if hmm_model is None:
        # No temporal model - use GMM only
        combined = emission_probs
    else:
        # HMM one-step-ahead prediction
        # Get filtered state for latest observation (2D input for score_samples)
        logprob, posteriors = hmm_model.score_samples(X_scaled)
        last_posterior = posteriors[0]  # P(z_t | x_{1:t})
        
        # One-step transition
        next_probs = last_posterior @ hmm_model.transmat_
        
        # Combine: geometric mean of emission + transition
        combined = np.sqrt(emission_probs * next_probs)
        combined = combined / combined.sum()
    
    return {name: float(p) for name, p in zip(REGIME_NAMES[:len(combined)], combined)}


def get_regime_params(regime: str) -> Dict:
    """
    Get live engine parameters for a regime.
    These replace the hardcoded values in eod_calibrator.py.
    """
    params = {
        "CHOPPY_NOISE_REJECT": {
            "conv_burst": 6.2,
            "conv_grind": 5.8,
            "min_ticks": 4,
            "whale_mult": 1.2,
            "branching_mult": 1.1,
        },
        "TRENDING_UP_SURGE": {
            "conv_burst": 4.8,
            "conv_grind": 4.5,
            "min_ticks": 2,
            "whale_mult": 0.9,
            "branching_mult": 0.9,
        },
        "TRENDING_DOWN_LIQUIDATION": {
            "conv_burst": 4.8,
            "conv_grind": 4.5,
            "min_ticks": 2,
            "whale_mult": 0.9,
            "branching_mult": 0.9,
        },
        "QUIET_ACCUMULATION": {
            "conv_burst": 5.2,
            "conv_grind": 4.8,
            "min_ticks": 3,
            "whale_mult": 1.0,
            "branching_mult": 1.0,
        },
        "BALANCED_TREND_GRINDER": {  # fallback
            "conv_burst": 5.5,
            "conv_grind": 5.0,
            "min_ticks": 3,
            "whale_mult": 1.0,
            "branching_mult": 1.0,
        },
    }
    return params.get(regime, params["BALANCED_TREND_GRINDER"])


def save_regime_model(gmm: GaussianMixture,
                       hmm_model: Optional[hmm.GaussianHMM],
                       scaler: RobustScaler,
                       feature_cols: List[str],
                       out_dir: Path) -> None:
    """Save regime model artifacts for live inference."""
    import joblib
    
    out_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(gmm, out_dir / "gmm_model.pkl")
    joblib.dump(scaler, out_dir / "scaler.pkl")
    if hmm_model:
        joblib.dump(hmm_model, out_dir / "hmm_model.pkl")
    
    meta = {
        "feature_cols": feature_cols,
        "regime_names": REGIME_NAMES[:gmm.n_components],
        "n_components": gmm.n_components,
        "has_hmm": hmm_model is not None,
    }
    (out_dir / "regime_meta.json").write_text(json.dumps(meta, indent=2))


def load_regime_model(model_dir: Path) -> Tuple:
    """Load regime model artifacts for live inference."""
    import joblib
    import json
    
    gmm = joblib.load(model_dir / "gmm_model.pkl")
    scaler = joblib.load(model_dir / "scaler.pkl")
    
    hmm_model = None
    if (model_dir / "hmm_model.pkl").exists():
        hmm_model = joblib.load(model_dir / "hmm_model.pkl")
    
    with open(model_dir / "regime_meta.json", "r") as f:
        meta = json.load(f)
    
    return gmm, hmm_model, scaler, meta["feature_cols"]


# Add missing import
import json