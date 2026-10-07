"""
AEGIS ML Gatekeeper Engine — Ultra-Fast LightGBM & CatBoost Confluence Validator.

Features:
1. Sub-millisecond C-API text models (aegis_flow_brain.txt, aegis_pooled_brain.txt).
2. Evaluates 1-minute Option Order Flow (Call/Put Aggressive Buy/Sell) + Microstructure Imbalance.
3. Maintains rolling 5-minute memory deque: checks if score was >= 40% at any point in the last 5 minutes.
4. Independent 1-minute score recorder and Confluence Gatekeeper for 4-Pillars setups.
"""

from __future__ import annotations
import os
import time
import json
import logging
from collections import deque
from typing import Dict, Tuple, Optional, Any, List
import numpy as np

try:
    import lightgbm as lgb
except ImportError:
    lgb = None

try:
    from catboost import CatBoostClassifier
except ImportError:
    CatBoostClassifier = None

try:
    from AEGIS.core.catboost_paper_trader import CatBoostPaperTrader
except ImportError:
    try:
        from core.catboost_paper_trader import CatBoostPaperTrader
    except ImportError:
        CatBoostPaperTrader = None

try:
    from common.logger import get_logger
    logger = get_logger("aegis_ml_gatekeeper")
except Exception:
    logger = logging.getLogger("aegis_ml_gatekeeper")

# Legacy 4-feature order (preserved for backward compatibility)
FEATURE_ORDER = [
    "kyle_lambda_30s",
    "momentum_accel_10s",
    "option_ret_30s",
    "spread_pct",
]

# 1-Minute Option Flow + Microstructure Alpha Features (New Institutional Brain)
FLOW_FEATURE_ORDER = [
    "put_aggression",
    "call_buy_vol",
    "call_aggression",
    "avg_book_imb",
    "upper_wick_bps",
    "ema20_dist_bps",
    "lower_wick_bps",
    "opt_flow_ratio",
    "range_bps",
    "body_bps",
    "put_buy_vol",
    "opt_delta_3m",
    "ema5_dist_bps",
    "opt_total_vol",
    "opt_pcr",
    "opt_delta_flow",
    "ret_1m_bps",
]

DEFAULT_THRESHOLDS = {
    "POOLED": 0.3340,
    "NIFTY": 0.3679,
    "SENSEX": 0.3778,
    "FLOW": 0.4000,
}


class MLGatekeeper:
    """
    Sub-millisecond LightGBM & CatBoost Validator for AEGIS Price Action Triggers.
    Evaluates 1-minute Option Order Flow + Microstructure Imbalance.
    Maintains a rolling 5-minute memory buffer for trade confluence.
    """

    def __init__(
        self,
        pooled_model_path: Optional[str] = None,
        nifty_model_path: Optional[str] = None,
        sensex_model_path: Optional[str] = None,
        flow_model_path: Optional[str] = None,
        config_path: Optional[str] = None,
        model_mode: str = "POOLED",
        enabled: bool = True,
    ):
        self.enabled = enabled and ((lgb is not None) or (CatBoostClassifier is not None))
        self.model_mode = model_mode.upper()  # "POOLED", "INDEX_SPECIFIC", or "FLOW"
        self.models: Dict[str, Any] = {}
        self.flow_model: Optional[Any] = None
        self.flow_model_type: str = "NONE"
        self.thresholds: Dict[str, float] = DEFAULT_THRESHOLDS.copy()

        # Rolling 5-minute memory buffer: stores dicts of {"ts": float, "score": float, "features": dict}
        self.recent_scores: deque = deque(maxlen=20)
        self.day_scores: list = []
        self.use_adaptive_median: bool = os.getenv("AEGIS_ML_USE_ADAPTIVE_MEDIAN", "true").lower() in ("true", "1", "yes")
        self.adaptive_min_floor: float = float(os.getenv("AEGIS_ML_ADAPTIVE_MIN_FLOOR", "0.15"))
        self.confluence_threshold: float = float(os.getenv("AEGIS_ML_CONFLUENCE_THRESHOLD", "0.40"))
        self.rolling_window_sec: int = int(os.getenv("AEGIS_ML_ROLLING_WINDOW_SEC", "300"))

        # Autonomous CatBoost Paper Trader (Zero-Risk Standalone ML Loop)
        self.paper_trader = CatBoostPaperTrader() if CatBoostPaperTrader is not None else None
        self.strike_to_symbol: Dict[Tuple[int, float], str] = {}
        self.option_cache: Dict[str, float] = {}
        self.get_spot = lambda: 0.0
        self.get_atm = lambda: 0.0

        # Resolve paths
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        pooled_path = pooled_model_path or os.path.join(base_dir, "params", "aegis_pooled_brain.txt")
        nifty_path = nifty_model_path or os.path.join(base_dir, "params", "aegis_nifty_brain.txt")
        sensex_path = sensex_model_path or os.path.join(base_dir, "params", "aegis_sensex_brain.txt")
        flow_path = flow_model_path or os.path.join(base_dir, "params", "aegis_flow_brain.txt")
        catboost_path = os.path.join(base_dir, "params", "aegis_catboost_brain.cbm")
        cfg_path = config_path or os.path.join(base_dir, "params", "aegis_ml_brain_config.json")

        # Load config thresholds if present
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, "r") as f:
                    cfg = json.load(f)
                for k in ["POOLED", "NIFTY", "SENSEX", "FLOW"]:
                    if k in cfg and "optimal_threshold" in cfg[k]:
                        self.thresholds[k] = float(cfg[k]["optimal_threshold"])
                if "optimal_threshold" in cfg:
                    self.confluence_threshold = float(cfg["optimal_threshold"])
                    self.thresholds["FLOW"] = self.confluence_threshold
                if "rolling_window_minutes" in cfg:
                    self.rolling_window_sec = int(cfg["rolling_window_minutes"]) * 60
            except Exception as e:
                logger.warning(f"Failed to load ML brain config {cfg_path}: {e}")

        # Load Flow Brain (Primary 1-minute Option Order Flow model)
        if self.enabled:
            # 1. Try CatBoost flow model first (Preferred primary brain per trader instruction)
            if CatBoostClassifier is not None and os.path.exists(catboost_path):
                try:
                    cb = CatBoostClassifier()
                    cb.load_model(catboost_path)
                    self.flow_model = cb
                    self.flow_model_type = "CATBOOST"
                    logger.info(f"Loaded CatBoost Flow Brain from {catboost_path} (threshold={self.confluence_threshold:.4f})")
                except Exception as e:
                    logger.warning(f"Failed to load CatBoost flow model {catboost_path}: {e}")

            # 2. Fallback to LightGBM flow model if CatBoost not available
            if self.flow_model is None and lgb is not None and os.path.exists(flow_path):
                try:
                    self.flow_model = lgb.Booster(model_file=flow_path)
                    self.flow_model_type = "LIGHTGBM"
                    logger.info(f"Loaded LightGBM Flow Brain from {flow_path} (threshold={self.confluence_threshold:.4f})")
                except Exception as e:
                    logger.warning(f"Failed to load LightGBM flow model {flow_path}: {e}")

            # 3. Load legacy boosters as fallbacks
            if lgb is not None:
                for key, path in [("POOLED", pooled_path), ("NIFTY", nifty_path), ("SENSEX", sensex_path)]:
                    if os.path.exists(path):
                        try:
                            self.models[key] = lgb.Booster(model_file=path)
                            logger.info(f"Loaded {key} ML Gatekeeper Brain from {path} (threshold={self.thresholds.get(key, 0.5):.4f})")
                        except Exception as e:
                            logger.error(f"Failed to load {key} ML model: {e}")

        # Pre-allocated reusable buffers (zero-allocation hot path)
        self._feat_buf = np.zeros((1, len(FEATURE_ORDER)), dtype=np.float64)
        self._flow_buf = np.zeros((1, len(FLOW_FEATURE_ORDER)), dtype=np.float64)

    def get_day_median_score(self) -> float:
        """
        Calculates the expanding median (P50) score of all 1-minute bars evaluated today.
        Returns safety floor (adaptive_min_floor) if insufficient bars (< 5).
        """
        if not self.day_scores or len(self.day_scores) < 5:
            return self.adaptive_min_floor
        med = float(np.median(self.day_scores))
        return max(self.adaptive_min_floor, med)

    def record_1m_bar(self, timestamp: float, features: Dict[str, Any], index_name: str = "NIFTY") -> float:
        """
        Calculates the ML score for a completed 1-minute canonical bar
        and stores it in the rolling 5-minute memory deque.
        """
        score = self.predict_flow_score(features)
        self.day_scores.append(float(score))
        
        entry = {
            "ts": float(timestamp),
            "score": float(score),
            "features": features,
            "index_name": index_name
        }
        self.recent_scores.append(entry)
        
        # Calculate recent max in last rolling window
        now_ts = float(timestamp)
        cutoff = now_ts - self.rolling_window_sec
        recent_in_window = [s["score"] for s in self.recent_scores if s["ts"] >= cutoff]
        max_5m = max(recent_in_window) if recent_in_window else score
        day_median = self.get_day_median_score()
        status = "HOT 🔥" if score >= self.confluence_threshold else "WARM" if score >= day_median else "COLD"
        
        cb_val = float(features.get('call_buy_vol', 0) or 0)
        cs_val = float(features.get('call_sell_vol', 0) or 0)
        pb_val = float(features.get('put_buy_vol', 0) or 0)
        ps_val = float(features.get('put_sell_vol', 0) or 0)
        logger.info(
            f"[CATBOOST 1M FLOW] {index_name} | Score: {score:.1%} | Max 5m: {max_5m:.1%} | "
            f"Day Median: {day_median:.1%} | Call: {cb_val:,.0f}B/{cs_val:,.0f}S | "
            f"Put: {pb_val:,.0f}B/{ps_val:,.0f}S | {status}"
        )

        # Standalone Autonomous CatBoost Paper Trader Evaluation
        if self.paper_trader is not None:
            try:
                spot_px = self.get_spot() if callable(self.get_spot) else 0.0
                atm_k = self.get_atm() if callable(self.get_atm) else 0.0
                self.paper_trader.on_1m_bar(
                    bar_ts=float(timestamp),
                    score=float(score),
                    features=features,
                    day_median=float(day_median),
                    spot_px=float(spot_px),
                    atm_strike=float(atm_k),
                    strike_to_symbol=self.strike_to_symbol,
                    option_cache=self.option_cache,
                )
            except Exception as pe:
                logger.warning(f"Error in CatBoost Paper Trader: {pe}")

        return score

    def predict_flow_score(self, features: Dict[str, Any]) -> float:
        """
        Computes flow model probability using FLOW_FEATURE_ORDER.
        """
        if not self.enabled or self.flow_model is None:
            # Fallback to legacy evaluate if flow model not available
            _, score, _ = self.evaluate(features)
            return score

        buf = np.empty((1, len(FLOW_FEATURE_ORDER)), dtype=np.float64)
        for i, fname in enumerate(FLOW_FEATURE_ORDER):
            val = features.get(fname)
            if val is None:
                buf[0, i] = np.nan
            else:
                try:
                    buf[0, i] = float(val)
                except (ValueError, TypeError):
                    buf[0, i] = np.nan

        try:
            if self.flow_model_type == "LIGHTGBM":
                return float(self.flow_model.predict(buf)[0])
            elif self.flow_model_type == "CATBOOST":
                return float(self.flow_model.predict_proba(buf)[:, 1][0])
            else:
                return 0.50
        except Exception as e:
            logger.warning(f"Error predicting flow score: {e}")
            return 0.50

    def check_rolling_confluence(
        self,
        threshold: Optional[float] = None,
        window_sec: Optional[int] = None,
        current_ts: Optional[float] = None,
        use_adaptive: Optional[bool] = None,
    ) -> Tuple[bool, float, Dict[str, Any]]:
        """
        Checks if the ML score was >= threshold at ANY point in the last window_sec.
        Supports Adaptive Daily Median (P50) thresholding when use_adaptive=True and threshold is None.
        
        Returns:
            (passed: bool, max_score: float, details: dict)
        """
        if not self.enabled:
            return True, 1.0, {"reason": "ML_DISABLED", "max_score": 1.0, "window_sec": window_sec or 300}

        is_adaptive = self.use_adaptive_median if use_adaptive is None else use_adaptive
        day_median = self.get_day_median_score()

        if threshold is not None:
            th = threshold
        elif is_adaptive:
            th = day_median
        else:
            th = self.confluence_threshold

        win = window_sec if window_sec is not None else self.rolling_window_sec
        now_ts = current_ts if current_ts is not None else time.time()
        cutoff = now_ts - win

        scores_in_window = [s for s in self.recent_scores if s["ts"] >= cutoff]

        if not scores_in_window:
            # Cold boot or first bar of day: allow entry with baseline threshold so trades aren't choked
            return True, th, {
                "reason": "COLD_BOOT_PASS",
                "max_score": th,
                "threshold": round(th, 4),
                "day_median": round(day_median, 4),
                "is_adaptive": is_adaptive,
                "window_sec": win,
                "scores_evaluated": 0
            }

        best_entry = max(scores_in_window, key=lambda s: s["score"])
        max_score = best_entry["score"]
        peak_age_sec = now_ts - best_entry["ts"]
        passed = max_score >= th

        details = {
            "passed": passed,
            "max_score": round(max_score, 4),
            "threshold": round(th, 4),
            "day_median": round(day_median, 4),
            "is_adaptive": is_adaptive,
            "window_sec": win,
            "peak_age_sec": round(peak_age_sec, 1),
            "scores_evaluated": len(scores_in_window),
            "total_day_bars": len(self.day_scores),
            "reason": (
                f"CONFLUENCE_PASSED (Peak Score: {max_score:.1%} >= {th:.1%}{' [Day Median]' if is_adaptive and threshold is None else ' [Adaptive Conviction Hurdle]' if threshold is not None else ''} {peak_age_sec:.0f}s ago)"
                if passed else
                f"CONFLUENCE_VETO (Peak Score: {max_score:.1%} < {th:.1%}{' [Day Median]' if is_adaptive and threshold is None else ' [Adaptive Conviction Hurdle]' if threshold is not None else ''} in last {win//60}m)"
            )
        }
        return passed, max_score, details

    def evaluate(self, features: Dict[str, Any], index_name: str = "NIFTY") -> Tuple[bool, float, str]:
        """
        Evaluates a candidate Price Action setup.
        Supports both new Flow Brain (if flow features present) and legacy 4-feature brain.
        
        Returns:
            (passed: bool, score: float, reason: str)
        """
        if not self.enabled:
            return True, 1.0, "ML_GATEKEEPER_DISABLED"

        # Check if caller passed Flow features (e.g. put_aggression or call_buy_vol)
        if self.flow_model is not None and ("put_aggression" in features or "call_buy_vol" in features):
            prob = self.predict_flow_score(features)
            thresh = self.thresholds.get("FLOW", self.confluence_threshold)
            passed = prob >= thresh
            reason = (
                f"ML_PASSED: FLOW score={prob:.4f} >= {thresh:.4f}"
                if passed else
                f"ML_VETO: FLOW score={prob:.4f} < {thresh:.4f} (low setup conviction)"
            )
            return passed, prob, reason

        idx = "SENSEX" if "SENSEX" in index_name.upper() else "NIFTY"

        # Auto-select model based on mode (POOLED is primary institutional brain)
        if self.model_mode == "POOLED" and "POOLED" in self.models:
            model = self.models["POOLED"]
            thresh = self.thresholds.get("POOLED", 0.3340)
            model_tag = "POOLED"
        else:
            model = self.models.get(idx)
            thresh = self.thresholds.get(idx, 0.3679)
            model_tag = idx

        if model is None:
            if "POOLED" in self.models:
                model = self.models["POOLED"]
                thresh = self.thresholds.get("POOLED", 0.3340)
                model_tag = "POOLED"
            elif idx in self.models:
                model = self.models[idx]
                thresh = self.thresholds.get(idx, 0.3679)
                model_tag = idx
            else:
                return True, 1.0, f"ML_GATEKEEPER_NO_MODEL_{model_tag}"

        if "index_id" not in features:
            features["index_id"] = 1 if idx == "SENSEX" else 0

        for i, fname in enumerate(FEATURE_ORDER):
            val = features.get(fname)
            if val is None:
                self._feat_buf[0, i] = np.nan
            else:
                try:
                    self._feat_buf[0, i] = float(val)
                except (ValueError, TypeError):
                    self._feat_buf[0, i] = np.nan

        prob = float(model.predict(self._feat_buf)[0])

        passed = prob >= thresh
        if passed:
            reason = f"ML_PASSED: {model_tag} score={prob:.4f} >= {thresh:.4f}"
        else:
            reason = f"ML_VETO: {model_tag} score={prob:.4f} < {thresh:.4f} (low setup conviction)"

        return passed, prob, reason
