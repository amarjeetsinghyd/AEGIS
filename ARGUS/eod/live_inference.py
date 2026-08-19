"""
Live inference engine for compiled LightGBM predictors.
Zero-dependency, <5μs latency per prediction.
"""
from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
from typing import Dict, Optional, Callable, List

import numpy as np
from numba import njit


class CompiledPredictor:
    """
    Loads and executes compiled global LightGBM predictor with Platt scaling.
    Hot-path: ~2-3μs including normalization.
    Supports both legacy per-(symbol,regime) and new global pooled models.
    """
    
    def __init__(self, calibration_dir: Path):
        self.calibration_dir = Path(calibration_dir)
        self.global_predictor: Optional[Callable] = None
        self.global_meta: Optional[dict] = None
        self.legacy_predictors: Dict[str, Dict[str, Callable]] = {}
        self.legacy_metadata: Dict[str, Dict[str, dict]] = {}
        self.model_type: str = "unknown"
        self._load_all()
    
    def _load_all(self) -> None:
        """Load all compiled predictors at startup."""
        if not self.calibration_dir.exists():
            return
        
        # Try loading global model first (new architecture)
        global_pred_file = self.calibration_dir / "predictor_global.py"
        global_meta_file = self.calibration_dir / "meta_global.json"
        
        if global_pred_file.exists() and global_meta_file.exists():
            self._load_global_model(global_pred_file, global_meta_file)
            self.model_type = "global_pooled_platt"
            return
        
        # Fallback to legacy per-(symbol,regime) models
        self._load_legacy_models()
        self.model_type = "legacy_per_symbol_regime"
    
    def _load_global_model(self, pred_file: Path, meta_file: Path) -> None:
        """Load global pooled model with Platt scaling."""
        spec = importlib.util.spec_from_file_location("predictor_global", pred_file)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        
        with open(meta_file, "r") as f:
            meta = json.load(f)
        
        self.global_predictor = mod.predict
        self.global_meta = meta
    
    def _load_legacy_models(self) -> None:
        """Load legacy per-(symbol,regime) models."""
        for symbol_dir in self.calibration_dir.iterdir():
            if not symbol_dir.is_dir():
                continue
            symbol = symbol_dir.name
            
            self.legacy_predictors[symbol] = {}
            self.legacy_metadata[symbol] = {}
            
            for pred_file in symbol_dir.glob("predictor_*.py"):
                regime = pred_file.stem.replace("predictor_", "")
                meta_file = symbol_dir / f"meta_{regime}.json"
                
                if not meta_file.exists():
                    continue
                
                spec = importlib.util.spec_from_file_location(
                    f"predictor_{symbol}_{regime}", pred_file
                )
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                
                with open(meta_file, "r") as f:
                    meta = json.load(f)
                
                self.legacy_predictors[symbol][regime] = mod.predict
                self.legacy_metadata[symbol][regime] = meta
    
    def predict(self, symbol: str, regime: str, features: np.ndarray) -> float:
        """
        Run prediction for a symbol in a specific regime.
        
        Args:
            symbol: Stock symbol
            regime: Regime name (e.g., "TRENDING_UP_SURGE")
            features: Raw feature vector (n_features,)
        
        Returns:
            Probability in [0, 1]
        """
        if self.model_type == "global_pooled_platt":
            return self._predict_global(symbol, regime, features)
        else:
            return self._predict_legacy(symbol, regime, features)
    
    def _predict_global(self, symbol: str, regime: str, features: np.ndarray) -> float:
        """Predict using global pooled model."""
        if self.global_predictor is None or self.global_meta is None:
            return 0.0
        
        meta = self.global_meta
        feature_mean = np.array(meta["feature_mean"], dtype=np.float64)
        feature_std = np.array(meta["feature_std"], dtype=np.float64)
        feats_norm = (features - feature_mean) / feature_std
        
        # Call global predictor (handles Platt scaling internally)
        prob = self.global_predictor(feats_norm.tolist(), symbol, regime)
        
        return float(prob)
    
    def _predict_legacy(self, symbol: str, regime: str, features: np.ndarray) -> float:
        """Predict using legacy per-(symbol,regime) model."""
        if symbol not in self.legacy_predictors:
            return 0.0
        
        if regime not in self.legacy_predictors[symbol]:
            regime = next(iter(self.legacy_predictors[symbol]))
        
        meta = self.legacy_metadata[symbol][regime]
        predictor = self.legacy_predictors[symbol][regime]
        
        feature_mean = np.array(meta["feature_mean"], dtype=np.float64)
        feature_std = np.array(meta["feature_std"], dtype=np.float64)
        feats_norm = (features - feature_mean) / feature_std
        
        prob = predictor(feats_norm)
        
        return float(prob)
    
    def should_fire(self, symbol: str, regime: str, features: np.ndarray) -> bool:
        """Check if signal should fire (probability > threshold)."""
        if self.model_type == "global_pooled_platt":
            return self._should_fire_global(symbol, regime, features)
        else:
            return self._should_fire_legacy(symbol, regime, features)
    
    def _should_fire_global(self, symbol: str, regime: str, features: np.ndarray) -> bool:
        if self.global_predictor is None or self.global_meta is None:
            return False
        
        threshold = self.global_meta.get("threshold", 0.5)
        prob = self._predict_global(symbol, regime, features)
        return prob > threshold
    
    def _should_fire_legacy(self, symbol: str, regime: str, features: np.ndarray) -> bool:
        if symbol not in self.legacy_predictors:
            return False
        
        if regime not in self.legacy_predictors[symbol]:
            regime = next(iter(self.legacy_predictors[symbol]))
        
        meta = self.legacy_metadata[symbol][regime]
        threshold = meta.get("threshold", 0.5)
        
        prob = self._predict_legacy(symbol, regime, features)
        return prob > threshold
    
    def get_available_regimes(self, symbol: str) -> list:
        """Get list of available regimes for a symbol."""
        if self.model_type == "global_pooled_platt":
            if self.global_meta:
                return list(self.global_meta.get("regime_to_idx", {}).keys())
            return []
        return list(self.legacy_predictors.get(symbol, {}).keys())
    
    def has_symbol(self, symbol: str) -> bool:
        if self.model_type == "global_pooled_platt":
            if self.global_meta:
                return symbol in self.global_meta.get("symbol_to_idx", {})
            return False
        return symbol in self.legacy_predictors
    
    def get_metadata(self, symbol: str, regime: str) -> dict:
        """Get metadata for a symbol/regime."""
        if self.model_type == "global_pooled_platt":
            return self.global_meta or {}
        if symbol in self.legacy_metadata and regime in self.legacy_metadata[symbol]:
            return self.legacy_metadata[symbol][regime]
        return {}


# =============================================================================
# PRE-ALLOCATED NUMBA BUFFER FOR LIVE INFERENCE
# =============================================================================

# Single contiguous buffer: [prices, volumes, timestamps, bid_px(5), bid_sz(5), ask_px(5), ask_sz(5), buy_vol, sell_vol]
# Total: 1 + 1 + 1 + 5 + 5 + 5 + 5 + 1 + 1 = 25 columns
BUFFER_COLS = 25
PRICE_IDX = 0
VOLUME_IDX = 1
TIMESTAMP_IDX = 2
BID_PX_START = 3
BID_SZ_START = 8
ASK_PX_START = 13
ASK_SZ_START = 18
BUY_VOL_IDX = 23
SELL_VOL_IDX = 24


@njit(cache=True)
def _on_tick_batch(
    raw_buffers: np.ndarray,  # shape (max_history, 25)
    idx: int,
    count: int,
    lp: float,
    vol: float,
    ts: int,
    bp: np.ndarray,  # shape (5,)
    bq: np.ndarray,  # shape (5,)
    sp: np.ndarray,  # shape (5,)
    sq: np.ndarray,  # shape (5,)
) -> tuple:
    """
    Numba-optimized batch tick update.
    Returns updated (idx, count).
    """
    i = idx
    
    # Core fields
    raw_buffers[i, PRICE_IDX] = lp
    raw_buffers[i, VOLUME_IDX] = vol
    raw_buffers[i, TIMESTAMP_IDX] = ts
    
    # L2 data (5 levels)
    for l in range(5):
        raw_buffers[i, BID_PX_START + l] = bp[l]
        raw_buffers[i, BID_SZ_START + l] = bq[l]
        raw_buffers[i, ASK_PX_START + l] = sp[l]
        raw_buffers[i, ASK_SZ_START + l] = sq[l]
    
    # Infer buy/sell volume
    mid = (bp[0] + sp[0]) * 0.5
    if mid > 0:
        if lp >= mid:
            raw_buffers[i, BUY_VOL_IDX] = vol
            raw_buffers[i, SELL_VOL_IDX] = 0.0
        else:
            raw_buffers[i, BUY_VOL_IDX] = 0.0
            raw_buffers[i, SELL_VOL_IDX] = vol
    else:
        raw_buffers[i, BUY_VOL_IDX] = 0.0
        raw_buffers[i, SELL_VOL_IDX] = 0.0
    
    idx = (idx + 1) % raw_buffers.shape[0]
    count = min(count + 1, raw_buffers.shape[0])
    
    return idx, count


@njit(cache=True)
def _get_ordered_slice(raw_buffers: np.ndarray, idx: int, count: int, max_history: int) -> np.ndarray:
    """
    Get ordered slice indices for circular buffer.
    Returns array of indices in chronological order.
    """
    if count < max_history:
        return np.arange(count)
    else:
        # Circular: from idx to end, then 0 to idx
        n = max_history
        result = np.empty(n, dtype=np.int64)
        # First part: idx to n-1
        first_len = n - idx
        result[:first_len] = np.arange(idx, n)
        # Second part: 0 to idx-1
        result[first_len:] = np.arange(idx)
        return result


class LiveFeatureComputer:
    """
    Computes microstructure features incrementally in the live engine.
    Uses pre-allocated Numba buffer for zero-allocation hot path.
    """
    
    def __init__(self, max_history: int = 5000):
        self.max_history = max_history
        # Single contiguous buffer - zero allocation after init
        self.raw_buffers = np.zeros((max_history, BUFFER_COLS), dtype=np.float64)
        self.idx = 0
        self.count = 0
    
    def reset(self) -> None:
        """Clear all buffers."""
        self.raw_buffers.fill(0.0)
        self.idx = 0
        self.count = 0
    
    def on_tick(self, tick: dict) -> None:
        """
        Update buffers with new tick - Numba optimized.
        tick dict keys: lp, vol, ts, bp1..bp5, bq1..bq5, sp1..sp5, sq1..sq5
        """
        # Extract L2 arrays
        bp = np.array([tick.get(f"bp{l+1}", tick.get("bp1", 0.0)) for l in range(5)], dtype=np.float64)
        bq = np.array([tick.get(f"bq{l+1}", tick.get("bq1", 0.0)) for l in range(5)], dtype=np.float64)
        sp = np.array([tick.get(f"sp{l+1}", tick.get("sp1", 0.0)) for l in range(5)], dtype=np.float64)
        sq = np.array([tick.get(f"sq{l+1}", tick.get("sq1", 0.0)) for l in range(5)], dtype=np.float64)
        
        self.idx, self.count = _on_tick_batch(
            self.raw_buffers, self.idx, self.count,
            tick.get("lp", 0.0),
            tick.get("vol", 0.0),
            tick.get("ts", 0),
            bp, bq, sp, sq
        )
    
    def get_features(self, feature_names: List[str]) -> np.ndarray:
        """
        Compute requested features from current buffers.
        Returns array of latest feature values.
        """
        from ARGUS.eod.microstructure import (
            compute_vpin, compute_kyle_lambda, compute_obi_l2,
            compute_microprice_accel, compute_microprice_jerk,
            compute_spread_volatility, compute_effective_spread,
            compute_realized_spread, compute_trade_intensity_hawkes,
            compute_branching_proxy, compute_whale_zscore,
            compute_momentum_5m, compute_volume_rate,
            compute_large_trade_ratio
        )
        
        if self.count < 100:
            return np.zeros(len(feature_names))
        
        # Get ordered slice indices
        sl = _get_ordered_slice(self.raw_buffers, self.idx, self.count, self.max_history)
        
        # Extract columns (views, no copy)
        prices = self.raw_buffers[sl, PRICE_IDX]
        volumes = self.raw_buffers[sl, VOLUME_IDX]
        timestamps = self.raw_buffers[sl, TIMESTAMP_IDX].astype(np.int64)
        bid_px = self.raw_buffers[sl, BID_PX_START:BID_PX_START+5]
        bid_sz = self.raw_buffers[sl, BID_SZ_START:BID_SZ_START+5]
        ask_px = self.raw_buffers[sl, ASK_PX_START:ASK_PX_START+5]
        ask_sz = self.raw_buffers[sl, ASK_SZ_START:ASK_SZ_START+5]
        buy_vol = self.raw_buffers[sl, BUY_VOL_IDX]
        sell_vol = self.raw_buffers[sl, SELL_VOL_IDX]
        
        # Map feature names to functions
        func_map = {
            "vpin": lambda: compute_vpin(buy_vol, sell_vol)[-1],
            "kyle_lambda": lambda: compute_kyle_lambda(prices, volumes)[-1],
            "obi_l2": lambda: compute_obi_l2(bid_px, bid_sz, ask_px, ask_sz)[-1],
            "microprice_accel": lambda: compute_microprice_accel(bid_px, bid_sz, ask_px, ask_sz)[-1],
            "microprice_jerk": lambda: compute_microprice_jerk(bid_px, bid_sz, ask_px, ask_sz)[-1],
            "spread_vol": lambda: compute_spread_volatility(bid_px[:, 0], ask_px[:, 0])[-1],
            "effective_spread": lambda: compute_effective_spread(prices, bid_px[:, 0], ask_px[:, 0])[-1],
            "realized_spread": lambda: compute_realized_spread(prices, bid_px[:, 0], ask_px[:, 0])[-1],
            "hawkes_intensity": lambda: compute_trade_intensity_hawkes(timestamps)[-1],
            "branching_proxy": lambda: compute_branching_proxy(timestamps)[-1],
            "whale_zscore": lambda: compute_whale_zscore(volumes)[-1],
            "momentum_5m": lambda: compute_momentum_5m(prices, timestamps)[-1],
            "volume_rate": lambda: compute_volume_rate(volumes, timestamps)[-1],
            "large_trade_ratio": lambda: compute_large_trade_ratio(volumes)[-1],
        }
        
        features = np.zeros(len(feature_names))
        for i, name in enumerate(feature_names):
            try:
                val = func_map.get(name, lambda: 0.0)()
                features[i] = val if not np.isnan(val) else 0.0
            except Exception:
                features[i] = 0.0
        
        return features


class CalibratedSignalEngine:
    """
    Main entry point for live engine.
    Combines regime prediction + compiled ML inference.
    """
    
    def __init__(self, calibration_dir: str, regime_model_dir: str):
        self.predictor = CompiledPredictor(Path(calibration_dir))
        self.feature_computer = LiveFeatureComputer()
        
        # Load regime model
        from ARGUS.eod.regime import load_regime_model, predict_next_day_regime
        self.gmm, self.hmm, self.scaler, self.regime_features = load_regime_model(
            Path(regime_model_dir)
        )
        self.predict_next_day_regime = predict_next_day_regime
        
        # Current regime per symbol (updated at 15:30)
        self.current_regime: Dict[str, str] = {}
        self.regime_probs: Dict[str, Dict[str, float]] = {}
    
    def update_regimes(self, daily_features: Dict[str, np.ndarray]) -> None:
        """Call at 15:30 IST with latest daily features per symbol."""
        for symbol, feats in daily_features.items():
            probs = self.predict_next_day_regime(
                symbol, feats, self.gmm, self.hmm, self.scaler
            )
            self.regime_probs[symbol] = probs
            # Use most probable regime
            self.current_regime[symbol] = max(probs, key=probs.get)
    
    def on_tick(self, symbol: str, tick: dict) -> Optional[dict]:
        """
        Process tick and return signal if fired.
        Hot-path: ~3-5μs total.
        """
        # Update features
        self.feature_computer.on_tick(tick)
        
        # Get current regime
        regime = self.current_regime.get(symbol, "BALANCED_TREND_GRINDER")
        
        # Check if we have a model for this symbol/regime
        if not self.predictor.has_symbol(symbol):
            return None
        
        if regime not in self.predictor.get_available_regimes(symbol):
            regime = self.predictor.get_available_regimes(symbol)[0]
        
        # Get feature names for this model
        meta = self.predictor.get_metadata(symbol, regime)
        feature_names = meta.get("feature_names", [])
        
        if not feature_names:
            return None
        
        # Compute features
        features = self.feature_computer.get_features(feature_names)
        
        # Predict
        should_fire = self.predictor.should_fire(symbol, regime, features)
        prob = self.predictor.predict(symbol, regime, features)
        
        if should_fire:
            threshold = meta.get("threshold", 0.5)
            return {
                "symbol": symbol,
                "regime": regime,
                "probability": prob,
                "threshold": threshold,
                "features": dict(zip(feature_names, features.tolist())),
            }
        
        return None