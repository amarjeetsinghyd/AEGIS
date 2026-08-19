"""
LightGBM training + m2cgen compilation pipeline for EOD calibration.
Trains a Global Pooled Model + Per-Symbol Platt Scaling for robust calibration.
"""
from __future__ import annotations
import json
import os
import warnings
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

import lightgbm as lgb
import m2cgen as m2c
import numpy as np
import pandas as pd
from m2cgen.assemblers import LightGBMModelAssembler
from sklearn.feature_selection import mutual_info_classif
from sklearn.linear_model import LogisticRegression

warnings.filterwarnings("ignore")


def select_features_mi_lgb(X: pd.DataFrame, y: pd.Series, top_k: int = 12) -> List[str]:
    """
    Two-stage feature selection:
    1. Mutual Information filter (non-linear, fast)
    2. LightGBM gain importance (model-aware)
    
    Returns top_k feature names.
    """
    # Stage 1: MI filter - get 2x candidates
    mi_scores = mutual_info_classif(X.fillna(0), y, random_state=42, n_neighbors=5)
    mi_ranking = pd.Series(mi_scores, index=X.columns).nlargest(top_k * 2).index.tolist()
    
    if len(mi_ranking) <= top_k:
        return mi_ranking
    
    # Stage 2: LightGBM on reduced set
    dtrain = lgb.Dataset(X[mi_ranking], label=y, free_raw_data=False)
    params = {
        "objective": "binary",
        "metric": "auc",
        "verbosity": -1,
        "deterministic": True,
        "force_row_wise": True,
        "num_threads": 1,
    }
    model = lgb.train(params, dtrain, num_boost_round=50)
    
    gain = pd.Series(model.feature_importance("gain"), index=mi_ranking)
    return gain.nlargest(top_k).index.tolist()


def train_lightgbm_binary(X: pd.DataFrame, y: pd.Series,
                           max_depth: int = 5,
                           n_estimators: int = 100,
                           learning_rate: float = 0.05,
                           min_data_in_leaf: int = 500) -> lgb.LGBMClassifier:
    """
    Train a LightGBM binary classifier with deterministic settings.
    Uses sklearn API (LGBMClassifier) for m2cgen compatibility.
    """
    model = lgb.LGBMClassifier(
        objective="binary",
        metric="f1",
        max_depth=max_depth,
        num_leaves=2**max_depth - 1,
        min_data_in_leaf=min_data_in_leaf,
        learning_rate=learning_rate,
        feature_pre_filter=False,
        verbosity=-1,
        deterministic=True,
        force_row_wise=True,
        n_jobs=1,
        n_estimators=n_estimators,
        bagging_fraction=0.8,
        bagging_freq=5,
        feature_fraction=0.8,
        random_state=42,
    )
    model.fit(X, y)
    return model


def compile_model_to_python(model: lgb.LGBMClassifier, 
                             feature_names: List[str],
                             quantize: bool = True) -> str:
    """
    Compile LightGBM model to pure Python using m2cgen.
    Returns Python source code as string.
    """
    # m2cgen supports LGBMClassifier directly
    py_code = m2c.export_to_python(model, function_name="predict")
    return py_code


def save_compiled_predictor(symbol: str, regime: str, 
                             py_code: str,
                             feature_names: List[str],
                             feature_mean: np.ndarray,
                             feature_std: np.ndarray,
                             threshold: float,
                             out_dir: Path) -> None:
    """
    Save compiled predictor + metadata for a (symbol, regime) pair.
    """
    symbol_dir = out_dir / symbol
    symbol_dir.mkdir(parents=True, exist_ok=True)
    
    # Save predictor function
    predictor_file = symbol_dir / f"predictor_{regime}.py"
    predictor_file.write_text(py_code)
    
    # Save metadata
    meta = {
        "symbol": symbol,
        "regime": regime,
        "feature_names": feature_names,
        "feature_mean": feature_mean.tolist(),
        "feature_std": feature_std.tolist(),
        "threshold": threshold,
        "model_version": "lgb_v1",
    }
    meta_file = symbol_dir / f"meta_{regime}.json"
    meta_file.write_text(json.dumps(meta, indent=2))


def load_compiled_predictor(symbol: str, regime: str, 
                             calibration_dir: Path) -> Tuple[callable, dict]:
    """
    Load compiled predictor function and metadata at runtime.
    """
    import importlib.util
    
    symbol_dir = calibration_dir / symbol
    predictor_file = symbol_dir / f"predictor_{regime}.py"
    meta_file = symbol_dir / f"meta_{regime}.json"
    
    if not predictor_file.exists() or not meta_file.exists():
        raise FileNotFoundError(f"Predictor not found for {symbol}/{regime}")
    
    # Dynamic import
    spec = importlib.util.spec_from_file_location(
        f"predictor_{symbol}_{regime}", predictor_file
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    
    with open(meta_file, "r") as f:
        meta = json.load(f)
    
    # Wrap predictor to return positive class probability only
    original_predict = mod.predict
    def predict_proba(features):
        result = original_predict(features)
        if isinstance(result, (list, tuple)) and len(result) == 2:
            return float(result[1])  # positive class probability
        return float(result)
    
    return predict_proba, meta


class RegimeAwareCalibrator:
    """
    End-to-end EOD calibrator:
    1. Compute microstructure features
    2. GMM + HMM regime clustering
    3. Per-(symbol, regime) feature selection + LightGBM training
    4. Compile to pure Python predictors
    """
    
    def __init__(self, 
                 raw_data_dir: Path,
                 output_dir: Path,
                 n_regimes: int = 4,
                 max_depth: int = 5,
                 n_estimators: int = 100,
                 top_k_features: int = 12):
        self.raw_data_dir = Path(raw_data_dir)
        self.output_dir = Path(output_dir)
        self.n_regimes = n_regimes
        self.max_depth = max_depth
        self.n_estimators = n_estimators
        self.top_k_features = top_k_features
        
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.regime_names = [
            "QUIET_ACCUMULATION",
            "TRENDING_UP_SURGE", 
            "TRENDING_DOWN_LIQUIDATION",
            "CHOPPY_NOISE_REJECT"
        ]
    
    def run(self, lookback_days: int = 3) -> Dict:
        """
        Run full EOD calibration pipeline (Global Pooled + Platt Scaling).
        Returns summary dict.
        """
        from stock_order_flow.eod.microstructure import compute_all_features
        from stock_order_flow.eod.regime import fit_gmm_hmm, predict_next_day_regime
        
        # 1. Load and prepare data
        print("Loading raw L2 data...")
        tick_data_by_symbol = self._load_tick_data(lookback_days)
        
        # 2. Compute features for all symbols
        print("Computing microstructure features...")
        features_by_symbol = {}
        targets_by_symbol = {}
        daily_summaries = []
        
        for symbol, df in tick_data_by_symbol.items():
            if len(df) < 5000:
                continue
            
            # Prepare tick arrays
            tick_data = self._prepare_tick_arrays(df)
            
            # Compute all features
            features = compute_all_features(tick_data)
            feature_df = pd.DataFrame(features)
            
            # Compute targets (forward 10m excursion >= 0.45%)
            max_up, max_dn = self._compute_targets(tick_data)
            is_long = (max_up >= 0.45) & (max_dn <= 0.20)
            is_short = (max_dn >= 0.45) & (max_up <= 0.20)
            target = (is_long | is_short).astype(int)
            
            # Daily summaries for regime clustering
            daily = self._compute_daily_summary(symbol, df, tick_data)
            daily_summaries.append(daily)
            
            features_by_symbol[symbol] = feature_df
            targets_by_symbol[symbol] = target
        
        # 3. Fit GMM + HMM on daily summaries
        print("Fitting GMM + HMM regime model...")
        daily_df = pd.concat(daily_summaries, ignore_index=True)
        gmm, hmm_model, scaler, feature_cols = fit_gmm_hmm(daily_df, self.n_regimes)
        
        # 4. Assign regimes to each symbol-day
        daily_df["regime"] = self._assign_regimes(daily_df, gmm, scaler, feature_cols)
        
        # 5. Prepare pooled training data
        print("Preparing pooled training data...")
        all_features_list = []
        all_targets_list = []
        all_symbol_ids = []
        all_regime_ids = []
        
        # Create symbol/regime to index mappings
        unique_symbols = sorted(features_by_symbol.keys())
        unique_regimes = self.regime_names
        symbol_to_idx = {s: i for i, s in enumerate(unique_symbols)}
        regime_to_idx = {r: i for i, r in enumerate(unique_regimes)}
        
        for symbol in features_by_symbol:
            sym_daily = daily_df[daily_df["symbol"] == symbol]
            if len(sym_daily) == 0:
                continue
            
            X = features_by_symbol[symbol]
            y = targets_by_symbol[symbol]
            
            if len(X) < 5000 or y.sum() < 50:
                continue
            
            # Feature selection on pooled data (use first symbol's features as reference)
            # We'll do global feature selection after pooling
            all_features_list.append(X)
            all_targets_list.append(y)
            all_symbol_ids.extend([symbol_to_idx[symbol]] * len(X))
            all_regime_ids.extend([regime_to_idx.get(r, 0) for r in sym_daily.sort_values("date")["regime"].values])
        
        if not all_features_list:
            print("No valid symbols for training")
            return {}
        
        # Pool all data
        pooled_features = pd.concat(all_features_list, ignore_index=True)
        pooled_targets = pd.concat(all_targets_list, ignore_index=True)
        pooled_symbol_ids = np.array(all_symbol_ids)
        pooled_regime_ids = np.array(all_regime_ids)
        
        # Global feature selection on pooled data
        print("Running global feature selection...")
        selected_features = select_features_mi_lgb(pooled_features, pooled_targets, self.top_k_features)
        pooled_features_sel = pooled_features[selected_features]
        
        # Normalize pooled features
        feature_mean = pooled_features_sel.mean().values
        feature_std = pooled_features_sel.std().replace(0, 1).values
        pooled_features_norm = (pooled_features_sel - feature_mean) / feature_std
        
        # 6. Train global pooled model
        print("Training global pooled model...")
        global_model = train_global_pooled_model(
            pooled_features_norm, pooled_targets,
            pooled_symbol_ids, pooled_regime_ids,
            max_depth=self.max_depth,
            n_estimators=self.n_estimators
        )
        
        # 7. Get global predictions for Platt scaling
        print("Computing global predictions for Platt scaling...")
        global_probs = global_model.predict_proba(pooled_features_norm)[:, 1]
        
        # 8. Fit per-symbol Platt scaling
        print("Fitting per-symbol Platt scaling...")
        platt_scalers = fit_per_symbol_platt(
            global_probs, pooled_targets.values, pooled_symbol_ids
        )
        
        # 9. Compute calibrated probabilities and optimize global threshold
        calibrated_probs = np.array([
            apply_platt_scaling(p, unique_symbols[sid], platt_scalers)
            for p, sid in zip(global_probs, pooled_symbol_ids)
        ])
        
        # Optimize threshold on calibrated probabilities
        from sklearn.metrics import f1_score
        best_f1, best_thresh = 0, 0.5
        for thresh in np.arange(0.1, 0.9, 0.02):
            preds = (calibrated_probs >= thresh).astype(int)
            f1 = f1_score(pooled_targets, preds, zero_division=0)
            if f1 > best_f1:
                best_f1, best_thresh = f1, thresh
        threshold = float(best_thresh)
        
        # 10. Compile global model + Platt scaling to Python
        print("Compiling global model to Python...")
        py_code = compile_global_model_to_python(
            global_model, selected_features, platt_scalers,
            symbol_to_idx, regime_to_idx
        )
        
        # 11. Save global predictor
        print("Saving global predictor...")
        save_global_predictor(
            py_code, selected_features, feature_mean, feature_std,
            threshold, platt_scalers, symbol_to_idx, regime_to_idx,
            self.output_dir
        )
        
        # 12. Save calibration index
        calibration_summary = {}
        for symbol in unique_symbols:
            sym_mask = pooled_symbol_ids == symbol_to_idx[symbol]
            n_samples = sym_mask.sum()
            n_positive = pooled_targets[sym_mask].sum()
            calibration_summary[symbol] = {
                "n_features": len(selected_features),
                "features": selected_features,
                "threshold": threshold,
                "n_samples": int(n_samples),
                "n_positive": int(n_positive),
                "platt_alpha": platt_scalers.get(symbol, (0.0, 1.0))[0],
                "platt_beta": platt_scalers.get(symbol, (0.0, 1.0))[1],
            }
        
        index = {
            "version": "5.0",
            "model_type": "global_pooled_platt",
            "regime_model": {
                "gmm_components": self.n_regimes,
                "regime_names": self.regime_names,
            },
            "symbols": calibration_summary,
            "global_threshold": threshold,
            "global_f1": best_f1,
        }
        (self.output_dir / "calibration_index.json").write_text(json.dumps(index, indent=2))
        
        print(f"Calibration complete: {len(calibration_summary)} symbols, global F1={best_f1:.4f}")
        return index
    
    def _load_tick_data(self, lookback_days: int) -> Dict[str, pd.DataFrame]:
        """Load parquet files from last N trading days."""
        import glob
        from datetime import datetime
        
        date_dirs = sorted([
            d for d in os.listdir(self.raw_data_dir) 
            if os.path.isdir(self.raw_data_dir / d) and len(d) == 10
        ])[-lookback_days:]
        
        all_dfs = []
        for dt_str in date_dirs:
            files = glob.glob(str(self.raw_data_dir / dt_str / "*.parquet"))
            for f in files:
                df = pd.read_parquet(f)
                df["date"] = dt_str
                all_dfs.append(df)
        
        if not all_dfs:
            return {}
        
        full = pd.concat(all_dfs, ignore_index=True)
        
        # Group by symbol (assuming token->symbol mapping available)
        # For now, group by token and map later
        return {tok: grp for tok, grp in full.groupby("tok")}
    
    def _prepare_tick_arrays(self, df: pd.DataFrame) -> dict:
        """Convert DataFrame to arrays for Numba kernels."""
        df = df.sort_values("ts").reset_index(drop=True)
        
        # Basic arrays
        n = len(df)
        tick_data = {
            "prices": df["lp"].values.astype(np.float64),
            "volumes": df["vol"].values.astype(np.float64),
            "timestamps": df["ts"].values.astype(np.int64),
        }
        
        # L2 arrays (assume columns bp1..bp5, bq1..bq5, sp1..sp5, sq1..sq5)
        n_levels = 5
        bid_px = np.zeros((n, n_levels), dtype=np.float64)
        bid_sz = np.zeros((n, n_levels), dtype=np.float64)
        ask_px = np.zeros((n, n_levels), dtype=np.float64)
        ask_sz = np.zeros((n, n_levels), dtype=np.float64)
        
        for l in range(1, n_levels + 1):
            bid_px[:, l-1] = df.get(f"bp{l}", df["bp1"]).values.astype(np.float64)
            bid_sz[:, l-1] = df.get(f"bq{l}", df["bq1"]).values.astype(np.float64)
            ask_px[:, l-1] = df.get(f"sp{l}", df["sp1"]).values.astype(np.float64)
            ask_sz[:, l-1] = df.get(f"sq{l}", df["sq1"]).values.astype(np.float64)
        
        tick_data["bid_px"] = bid_px
        tick_data["bid_sz"] = bid_sz
        tick_data["ask_px"] = ask_px
        tick_data["ask_sz"] = ask_sz
        
        # Buy/sell volume (infer from price vs mid)
        mid = (bid_px[:, 0] + ask_px[:, 0]) / 2.0
        buy_vol = np.where(tick_data["prices"] >= mid, tick_data["volumes"], 0.0)
        sell_vol = np.where(tick_data["prices"] < mid, tick_data["volumes"], 0.0)
        tick_data["buy_vol"] = buy_vol
        tick_data["sell_vol"] = sell_vol
        
        return tick_data
    
    def _compute_targets(self, tick_data: dict) -> Tuple[np.ndarray, np.ndarray]:
        """Compute forward 10m max up/down excursions."""
        from stock_order_flow.eod.microstructure import compute_forward_excursions
        return compute_forward_excursions(
            tick_data["prices"], 
            tick_data["timestamps"],
            horizon_ms=600000
        )
    
    def _compute_daily_summary(self, symbol: str, df: pd.DataFrame, 
                                tick_data: dict) -> pd.DataFrame:
        """Compute daily summary features for regime clustering."""
        from stock_order_flow.eod.microstructure import (
            compute_vpin, compute_kyle_lambda, compute_obi_l2,
            compute_trade_intensity_hawkes, compute_spread_volatility
        )
        
        summaries = []
        for date, g in df.groupby("date"):
            if len(g) < 500:
                continue
            
            # Get indices for this day
            idx = g.index.values
            start, end = idx[0], idx[-1] + 1
            
            # Slice arrays
            prices = tick_data["prices"][start:end]
            volumes = tick_data["volumes"][start:end]
            timestamps = tick_data["timestamps"][start:end]
            bid_px = tick_data["bid_px"][start:end]
            bid_sz = tick_data["bid_sz"][start:end]
            ask_px = tick_data["ask_px"][start:end]
            ask_sz = tick_data["ask_sz"][start:end]
            buy_vol = tick_data["buy_vol"][start:end]
            sell_vol = tick_data["sell_vol"][start:end]
            
            mid = (bid_px[:, 0] + ask_px[:, 0]) / 2.0
            mid[mid == 0] = 1.0
            spread_bps = (ask_px[:, 0] - bid_px[:, 0]) / mid * 10000.0
            
            # Features
            vpin = compute_vpin(buy_vol, sell_vol).mean()
            kyle = compute_kyle_lambda(prices, volumes).mean()
            obi = compute_obi_l2(bid_px, bid_sz, ask_px, ask_sz).mean()
            intensity = compute_trade_intensity_hawkes(timestamps).mean()
            spread_vol = compute_spread_volatility(bid_px[:, 0], ask_px[:, 0]).mean()
            ret_day = (prices[-1] - prices[0]) / prices[0] * 100.0 if prices[0] > 0 else 0.0
            
            summaries.append({
                "symbol": symbol,
                "date": date,
                "avg_spread_bps": float(np.mean(spread_bps)),
                "spread_vol": float(spread_vol) if not np.isnan(spread_vol) else 0.0,
                "trade_intensity": float(intensity),
                "obi_mean": float(obi),
                "kyle_lambda": float(kyle) if not np.isnan(kyle) else 0.0,
                "day_return_pct": float(ret_day),
                "tick_count": len(g),
            })
        
        return pd.DataFrame(summaries)
    
    def _assign_regimes(self, daily_df: pd.DataFrame, gmm, scaler, feature_cols) -> np.ndarray:
        """Assign regime labels using GMM + heuristic mapping."""
        X = daily_df[feature_cols].values
        X_scaled = scaler.transform(X)
        clusters = gmm.predict(X_scaled)
        
        # Map clusters to regime names using heuristics
        cluster_means = daily_df.groupby(clusters)[feature_cols].mean()
        regime_map = {}
        
        for c in cluster_means.index:
            row = cluster_means.loc[c]
            if row["avg_spread_bps"] > cluster_means["avg_spread_bps"].median() and \
               row["trade_intensity"] < cluster_means["trade_intensity"].median():
                regime_map[c] = "CHOPPY_NOISE_REJECT"
            elif row["day_return_pct"] > 0.30:
                regime_map[c] = "TRENDING_UP_SURGE"
            elif row["day_return_pct"] < -0.30:
                regime_map[c] = "TRENDING_DOWN_LIQUIDATION"
            else:
                regime_map[c] = "QUIET_ACCUMULATION"
        
        return np.array([regime_map.get(c, "QUIET_ACCUMULATION") for c in clusters])
    
    def _optimize_threshold(self, model: lgb.LGBMClassifier, X: np.ndarray, y: np.ndarray) -> float:
        """Find probability threshold maximizing F1."""
        from sklearn.metrics import f1_score
        
        probs = model.predict_proba(X)[:, 1]
        best_f1, best_thresh = 0, 0.5
        for thresh in np.arange(0.1, 0.9, 0.02):
            preds = (probs >= thresh).astype(int)
            f1 = f1_score(y, preds, zero_division=0)
            if f1 > best_f1:
                best_f1, best_thresh = f1, thresh
        return float(best_thresh)


# =============================================================================
# GLOBAL POOLED MODEL + PLATT SCALING (New Architecture)
# =============================================================================

def train_global_pooled_model(
    all_features: pd.DataFrame,
    all_targets: pd.Series,
    symbol_ids: np.ndarray,
    regime_ids: np.ndarray,
    max_depth: int = 6,
    n_estimators: int = 200,
    min_data_in_leaf: int = 1000
) -> lgb.LGBMClassifier:
    """
    Train a single global LightGBM model on all symbols/regimes.
    Uses symbol_id and regime_id as categorical features.
    """
    X = all_features.copy()
    X['symbol_id'] = pd.Categorical(symbol_ids)
    X['regime_id'] = pd.Categorical(regime_ids)
    
    model = lgb.LGBMClassifier(
        objective="binary",
        metric="f1",
        max_depth=max_depth,
        num_leaves=2**max_depth - 1,
        min_data_in_leaf=min_data_in_leaf,
        learning_rate=0.05,
        feature_pre_filter=False,
        verbosity=-1,
        deterministic=True,
        force_row_wise=True,
        n_jobs=1,
        n_estimators=n_estimators,
        bagging_fraction=0.8,
        bagging_freq=5,
        feature_fraction=0.8,
        random_state=42,
        categorical_feature=['symbol_id', 'regime_id'],
    )
    model.fit(X, all_targets)
    return model


def fit_per_symbol_platt(
    global_probs: np.ndarray,
    targets: np.ndarray,
    symbol_ids: np.ndarray,
    min_samples_for_calibration: int = 200
) -> Dict[str, Tuple[float, float]]:
    """
    Fit per-symbol Platt scaling (logistic regression) on global model probabilities.
    Returns dict: symbol -> (alpha, beta) where calibrated_p = sigmoid(alpha + beta * global_p)
    Uses Bayesian shrinkage: C = 1/n_samples for automatic regularization.
    """
    scalers = {}
    unique_symbols = np.unique(symbol_ids)
    
    for sym in unique_symbols:
        mask = symbol_ids == sym
        n_samples = mask.sum()
        
        if n_samples < min_samples_for_calibration:
            # Too little data → use identity calibration (no change)
            scalers[sym] = (0.0, 1.0)
        else:
            # Fit logistic regression with shrinkage
            # C = 1/n provides Bayesian-like regularization
            lr = LogisticRegression(
                C=1.0 / n_samples,
                penalty='l2',
                solver='lbfgs',
                max_iter=1000,
                random_state=42
            )
            lr.fit(global_probs[mask].reshape(-1, 1), targets[mask])
            alpha = float(lr.intercept_[0])
            beta = float(lr.coef_[0, 0])
            scalers[sym] = (alpha, beta)
    
    return scalers


def apply_platt_scaling(
    global_prob: float,
    symbol: str,
    platt_scalers: Dict[str, Tuple[float, float]]
) -> float:
    """Apply per-symbol Platt scaling to global probability."""
    alpha, beta = platt_scalers.get(symbol, (0.0, 1.0))
    logit = alpha + beta * global_prob
    # Sigmoid
    if logit >= 0:
        return 1.0 / (1.0 + np.exp(-logit))
    else:
        z = np.exp(logit)
        return z / (1.0 + z)


def compile_global_model_to_python(
    model: lgb.LGBMClassifier,
    feature_names: List[str],
    platt_scalers: Dict[str, Tuple[float, float]],
    symbol_to_idx: Dict[str, int],
    regime_to_idx: Dict[str, int]
) -> str:
    """
    Compile global model + Platt scaling to pure Python.
    Returns a single predictor function that handles all symbols/regimes.
    """
    # Export global model
    global_code = m2c.export_to_python(model, function_name="predict_global")
    
    # Build symbol/regime lookup tables
    symbol_list = [k for k, v in sorted(symbol_to_idx.items(), key=lambda x: x[1])]
    regime_list = [k for k, v in sorted(regime_to_idx.items(), key=lambda x: x[1])]
    
    # Generate Platt scaling lookup
    platt_lines = []
    for sym in symbol_list:
        alpha, beta = platt_scalers.get(sym, (0.0, 1.0))
        platt_lines.append(f'    "{sym}": ({alpha}, {beta}),')
    platt_dict = "{\n" + "\n".join(platt_lines) + "\n}"
    
    # Feature names as list literal
    feature_names_repr = repr(feature_names)
    
    # Generate complete predictor
    predictor_code = f'''
{global_code}

# Platt scaling parameters per symbol
PLATT_SCALERS = {platt_dict}

# Symbol/regime to categorical index
SYMBOL_TO_IDX = {symbol_to_idx}
REGIME_TO_IDX = {regime_to_idx}

# Feature names in order
FEATURE_NAMES = {feature_names_repr}

def predict(features: list, symbol: str, regime: str) -> float:
    """
    Predict breakout probability for a symbol/regime.
    
    Args:
        features: List of feature values (in order of FEATURE_NAMES)
        symbol: Stock symbol
        regime: Regime name
    
    Returns:
        Calibrated probability in [0, 1]
    """
    # Build feature vector with categorical features
    feat_dict = dict(zip(FEATURE_NAMES, features))
    feat_dict['symbol_id'] = SYMBOL_TO_IDX.get(symbol, 0)
    feat_dict['regime_id'] = REGIME_TO_IDX.get(regime, 0)
    
    # Convert to array in correct order
    feature_order = FEATURE_NAMES + ['symbol_id', 'regime_id']
    x = [feat_dict[f] for f in feature_order]
    
    # Get global probability
    global_probs = predict_global([x])
    global_p = global_probs[0][1]  # positive class
    
    # Apply Platt scaling
    alpha, beta = PLATT_SCALERS.get(symbol, (0.0, 1.0))
    logit = alpha + beta * global_p
    if logit >= 0:
        return 1.0 / (1.0 + __import__('math').exp(-logit))
    else:
        z = __import__('math').exp(logit)
        return z / (1.0 + z)
'''
    return predictor_code


def save_global_predictor(
    py_code: str,
    feature_names: List[str],
    feature_mean: np.ndarray,
    feature_std: np.ndarray,
    threshold: float,
    platt_scalers: Dict[str, Tuple[float, float]],
    symbol_to_idx: Dict[str, int],
    regime_to_idx: Dict[str, int],
    out_dir: Path
) -> None:
    """Save global compiled predictor + metadata."""
    out_dir.mkdir(parents=True, exist_ok=True)
    
    # Save predictor function
    predictor_file = out_dir / "predictor_global.py"
    predictor_file.write_text(py_code)
    
    # Save metadata
    meta = {
        "model_type": "global_pooled_platt",
        "feature_names": feature_names,
        "feature_mean": feature_mean.tolist(),
        "feature_std": feature_std.tolist(),
        "threshold": threshold,
        "platt_scalers": {k: list(v) for k, v in platt_scalers.items()},
        "symbol_to_idx": symbol_to_idx,
        "regime_to_idx": regime_to_idx,
        "model_version": "lgb_global_v1",
    }
    meta_file = out_dir / "meta_global.json"
    meta_file.write_text(json.dumps(meta, indent=2))


def load_global_predictor(calibration_dir: Path) -> Tuple[callable, dict]:
    """Load global compiled predictor function and metadata."""
    import importlib.util
    
    predictor_file = calibration_dir / "predictor_global.py"
    meta_file = calibration_dir / "meta_global.json"
    
    if not predictor_file.exists() or not meta_file.exists():
        raise FileNotFoundError(f"Global predictor not found in {calibration_dir}")
    
    spec = importlib.util.spec_from_file_location(
        "predictor_global", predictor_file
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    
    with open(meta_file, "r") as f:
        meta = json.load(f)
    
    return mod.predict, meta


def run_calibration(raw_data_dir: str, output_dir: str, **kwargs) -> Dict:
    """Entry point for eod_calibrator.py integration."""
    calibrator = RegimeAwareCalibrator(
        raw_data_dir=Path(raw_data_dir),
        output_dir=Path(output_dir),
        **kwargs
    )
    return calibrator.run()