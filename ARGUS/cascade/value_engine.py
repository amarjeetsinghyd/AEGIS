"""Microstructure Cascade — Unified Live Execution Engine (v3.0)
Integrates: Fixed Microstructure Core + MPAR + LRA + DKLA + Unified Conviction Trigger
Optimized for Indian Equities/Options HFT — Zero-Allocation Hot Path
"""
from __future__ import annotations
import os
import sys
from enum import Enum
from dataclasses import dataclass
from typing import Optional

# ─── BOOTSTRAP ───
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import json, time as pytime, math
from collections import deque
from datetime import datetime
import queue
import logging
import numpy as np
from typing import Dict, Deque, List, Tuple, Any, TYPE_CHECKING
from common.config import get_data_dir
from common.logger import get_logger

if TYPE_CHECKING:
    from ARGUS.tick_compiler import ParsedTick

logger = get_logger("cascade_engine")

# ─── PRE-MARKET STATE MACHINE ───
class EnginePhase(Enum):
    AUCTION_ENTRY      = 0  # 09:00-09:04: Universe handoff, absorb only
    AUCTION_FREEZE     = 1  # 09:04-09:08: L2 frozen, capture baseline
    AUCTION_BUFFER     = 2  # 09:08-09:15: L2 frozen, wait for continuous
    TRANSITION_DETECT  = 3  # First continuous tick detected
    CONTINUOUS_LIVE    = 4  # Normal operation

@dataclass(frozen=True)
class BaselineTruth:
    """Immutable baseline captured at 09:08 freeze."""
    bids: Tuple[Tuple[float, int], ...]  # (price, qty) x 5
    asks: Tuple[Tuple[float, int], ...]
    equilibrium_price: float
    equilibrium_qty: int
    mid_price: float
    microprice: float
    book_pressure: float
    lra_ema_bid: Tuple[float, ...]
    lra_ema_ask: Tuple[float, ...]
    dkla_lam_buy: float
    dkla_lam_sell: float
    dkla_asym: float
    captured_at_ns: int

# ──────────────────────────────────────────────────────────────
# ZERO-ALLOCATION HOT PATH HELPERS
# ──────────────────────────────────────────────────────────────

def _walk_book_impact_fast(prices: List[float], qtys: List[float], target_notional: float, mid: float) -> float:
    """O(1) Manual Unrolled Book Walk for 5 Levels. Returns impact in bps.
    Accumulates SHARES filled (not notional*price), then avg_price = target_notional/shares.
    (Old version accumulated cost = notional*price, which is a notional-weighted price, not a
    share-weighted execution price -- close for tight books, systematically biased for wide ones.)"""
    rem = target_notional
    shares = 0.0
    # L1
    lv = prices[0] * qtys[0]
    if lv >= rem: shares += rem / max(prices[0], 1e-9); rem = 0.0
    else: shares += qtys[0]; rem -= lv
    # L2
    lv = prices[1] * qtys[1]
    if lv >= rem: shares += rem / max(prices[1], 1e-9); rem = 0.0
    else: shares += qtys[1]; rem -= lv
    # L3
    lv = prices[2] * qtys[2]
    if lv >= rem: shares += rem / max(prices[2], 1e-9); rem = 0.0
    else: shares += qtys[2]; rem -= lv
    # L4
    lv = prices[3] * qtys[3]
    if lv >= rem: shares += rem / max(prices[3], 1e-9); rem = 0.0
    else: shares += qtys[3]; rem -= lv
    # L5
    lv = prices[4] * qtys[4]
    if lv >= rem: shares += rem / max(prices[4], 1e-9); rem = 0.0
    else: return 9999.0
    if rem > 1e-6 or shares <= 0: return 9999.0
    return ((target_notional / shares) - mid) / mid * 10_000.0

def _ema_update(ema: float, val: float, alpha: float) -> float:
    return ema + alpha * (val - ema)

# ──────────────────────────────────────────────────────────────
# MAIN ENGINE CLASS
# ──────────────────────────────────────────────────────────────

class CascadeValueEngine:
    def __init__(
        self, 
        token_to_symbol: dict, 
        bucket_map: dict = None, 
        vacuum_map: dict = None,
        log_path: str = None,
        dkla_notional: float = 1_00_00_000.0,
        # Trigger Tuning Parameters (Exposed for Config)
        trigger_config: dict = None,
        # Per-Stock Calibration Overrides (Loaded from stock_calibration.json)
        stock_calibration: dict = None
    ):
        self.t2s = {str(k): str(v) for k, v in token_to_symbol.items()}
        self.bucket_map = bucket_map or {}
        self.vacuum_map = vacuum_map or {}
        self.dkla_notional = dkla_notional
        self.stock_calibration = stock_calibration or {}
        
        self.signal_queue = queue.Queue(maxsize=1000)
        
        # ─── Bucket Accumulators ───
        self.cur_buy_val: Dict[str, float] = {}
        self.cur_sell_val: Dict[str, float] = {}
        self.cur_tot_val: Dict[str, float] = {}
        self.cur_tick_count: Dict[str, int] = {}
        self.cur_max_tick_val: Dict[str, float] = {}
        self.cur_net_vacuum: Dict[str, float] = {}
        
        # ─── State ───
        self.last_snap: Dict[str, Dict] = {}
        self.last_depth_state: Dict[str, Dict] = {}
        self.trade_times: Dict[str, Deque[float]] = {}
        self.completion_times: Dict[str, Deque[float]] = {}
        
        # ─── Rolling Windows ───
        self.vpin_q: Dict[str, Deque[float]] = {}
        self.whale_history: Dict[str, Deque[float]] = {}
        self.vacuum_history: Dict[str, Deque[float]] = {}
        self.mlofi_history: Dict[str, Deque[float]] = {}
        self.toxicity_history: Dict[str, Deque[float]] = {}
        
        # ─── MPAR State ───
        self.mp_prev: Dict[str, float] = {}
        self.mp_vel: Dict[str, float] = {}
        self.mp_accel: Dict[str, float] = {}
        
        # ─── LRA State ───
        self.lra_ema: Dict[str, Dict[str, List[float]]] = {}
        self.lra_asym_prev: Dict[str, float] = {}
        
        # ─── DKLA State ───
        self.dkla_asym_prev: Dict[str, float] = {}
        
        # ─── Config ───
        self.CFG = {
            "bucket_turnover": 2_00_00_000.0,
            "vacuum_threshold": 2_500_000.0,
            "glitch_guard_cr": 50.0,
            "min_ticks_per_bucket": 3,
            "whale_window": 50,
            "vacuum_window": 100,
            "mlofi_window": 100,
            "toxicity_window": 200,
            "hawkes_lookback_sec": 600,
            "hawkes_bin_sec": 10,
            "mp_alpha": 0.3,
            "mp_accel_alpha": 0.2,
            "lra_alpha": 0.05,
            "dkla_notional": dkla_notional,
        }
        self._hawkes_bins = int(self.CFG["hawkes_lookback_sec"] / self.CFG["hawkes_bin_sec"])
        
        # ─── SYMBOL INDEX MAPPING (for array-based regime state) ───
        self.sym_to_idx = {sym: i for i, sym in enumerate(self.t2s.values())}
        self.idx_to_sym = {i: sym for sym, i in self.sym_to_idx.items()}
        self._n_symbols = len(self.sym_to_idx)
        
        # ─── PRE-ALLOCATE ALL PER-SYMBOL STATE (Zero hot-path allocation, bounded memory) ───
        self._init_symbol_state()
        
        # ─── REUSABLE HOT-PATH STRUCTURES (No allocation on tick/bucket) ───
        self._curr_bids = {sym: {} for sym in self.t2s.values()}
        self._curr_asks = {sym: {} for sym in self.t2s.values()}
        self._adv_cache = {sym: {} for sym in self.t2s.values()}
        self._hawkes_counts = np.zeros(self._hawkes_bins, dtype=np.int32)
        self._regime_names = ("NOISE", "BURST", "GRIND", "WHALE")
        
        # ─── ADAPTIVE GATING: Regime Classification State (pre-allocated arrays, zero hot-path allocation) ───
        self.regime_beta_ema = np.zeros(self._n_symbols, dtype=np.float32)
        self.regime_W_ema = np.zeros(self._n_symbols, dtype=np.float32)
        self.regime_mom_ema = np.zeros(self._n_symbols, dtype=np.float32)
        self.regime_beta_var = np.zeros(self._n_symbols, dtype=np.float32)
        self.regime_W_var = np.zeros(self._n_symbols, dtype=np.float32)
        self.regime_id = np.zeros(self._n_symbols, dtype=np.int8)  # 0=NOISE, 1=BURST, 2=GRIND, 3=WHALE
        self.regime_conf = np.zeros(self._n_symbols, dtype=np.float32)
        self.regime_mom_history = {sym: deque(maxlen=1200) for sym in self.t2s.values()}  # 15-min LTP history (for 10s, 1m, 5m, 15m multi-horizon alignment)
        self.s_flow_history = {sym: deque(maxlen=30) for sym in self.t2s.values()}         # S_flow history for SVI exit computation
        
        # ─── ADAPTIVE GATING: Bayesian Threshold Posteriors (bucket-level only) ───
        # Beta-Binomial conjugate priors per symbol: branching threshold & whale_z threshold
        self.post_alpha_beta = np.ones(self._n_symbols) * 3.0
        self.post_beta_beta = np.ones(self._n_symbols) * 20.0
        self.post_alpha_W = np.ones(self._n_symbols) * 2.0
        self.post_beta_W = np.ones(self._n_symbols) * 10.0
        self.bucket_count = np.zeros(self._n_symbols, dtype=np.int32)
        
        # Apply symbol-specific Bayesian priors from stock_calibration.json if present
        for sym, cfg in self.stock_calibration.items():
            if sym in self.sym_to_idx:
                idx = self.sym_to_idx[sym]
                if "prior_alpha_beta" in cfg: self.post_alpha_beta[idx] = float(cfg["prior_alpha_beta"])
                if "prior_beta_beta" in cfg: self.post_beta_beta[idx] = float(cfg["prior_beta_beta"])
                if "prior_alpha_W" in cfg: self.post_alpha_W[idx] = float(cfg["prior_alpha_W"])
                if "prior_beta_W" in cfg: self.post_beta_W[idx] = float(cfg["prior_beta_W"])
        
        # ─── ADAPTIVE GATING: False Positive Rate Tracking (for dynamic conviction threshold) ───
        self.recent_fp = {sym: deque(maxlen=20) for sym in self.t2s.values()}
        self.recent_tp = {sym: deque(maxlen=20) for sym in self.t2s.values()}
        
        # ─── TRIGGER CONFIG (The "Holy Grail" Thresholds) ───
        default_trigger = {
            # Core Gates (Must Pass All) - BASE VALUES (adaptive overrides these)
            "min_ticks": 3,
            "min_branching": 0.35,      # Self-exciting flow (base, will be overridden by adaptive)
            "min_whale_z": 1.2,         # Statistical whale presence (base, will be overridden by adaptive)
            "min_tox_pct": 75,          # Toxicity percentile
            
            # Long Conviction Weights (Sum > THRESHOLD triggers)
            "w_mlofi": 2.0,             # Book Pressure vs Flow
            "w_mpar": 2.0,              # Microprice Acceleration (Highest Alpha)
            "w_vac": 2.0,               # Liquidity Vacuum
            "w_lra": 1.5,               # MM Retreat
            "w_dkla": 2.0,              # Impact Asymmetry
            "w_micro": 1.0,             # Microprice Stretch
            "conviction_threshold": 5.5, # Out of ~10.5 max (base, regime-adaptive overrides)
            
            # Dynamic Normalization Clamps
            "mlofi_cap": 3.0,
            "mpar_cap": 3.0,
            "vac_cap": 100.0,           # bps
            "lra_cap": 1.0,
            "dkla_cap": 1.0,
            
            # ─── ADAPTIVE GATING CONFIG — CALIBRATED 2026-08-18 ───
            # Regime classification thresholds (calibrated from 2-day replay)
            "regime_burst_beta_min": 0.15,      # Beta EMA for burst: 0.15+ (was 0.65)
            "regime_burst_W_min": 0.80,         # WhaleZ EMA for burst: 0.80+ (was 0.50)
            "regime_burst_mom_min": 0.20,       # Momentum EMA for burst: 0.20%+ (was 0.25%)
            "regime_grind_beta_min": 0.08,      # Beta EMA for grind: 0.08-0.15 (was 0.25)
            "regime_grind_beta_max": 0.15,
            "regime_grind_W_min": 0.30,         # WhaleZ EMA for grind: 0.30+ (was 0.40)
            "regime_grind_mom_min": 0.15,       # Momentum EMA for grind: 0.15%+ (was 0.35%)
            "regime_whale_W_min": 0.60,         # WhaleZ EMA for whale: 0.60+ (was 1.00)
            
            # Bayesian priors (per symbol overrides allowed via trigger_config)
            "prior_alpha_beta": 3.0, "prior_beta_beta": 20.0,
            "prior_alpha_W": 2.0, "prior_beta_W": 10.0,
            
            # Minimax fallback (CALIBRATION-OPTIMAL from forensic sweep)
            "minimax_beta": 0.10, "minimax_W": 0.85, "minimax_mom": 0.20, "minimax_tox_pct": 60,
            
            # Conviction thresholds per regime (LOWERED for recall)
            "conv_thresh_burst": 5.5, "conv_thresh_grind": 5.0, "conv_thresh_whale": 6.0,
            "conv_thresh_noise": 6.5,  # Raised from 3.0 to 6.5: strict cutoff for NOISE regime (ML-calibrated)
            
            # Interaction weights (BOOSTED structural)
            "interact_mlofi_vac": 0.6, "interact_mpar_lra": 0.5,
            "interact_whale_dkla": 0.5, "interact_beta_mom": 0.3,
            
            # Adaptation controls
            "ema_alpha_regime": 0.15, "ucb_exploration_c": 0.5,
            "min_buckets_for_bayes": 30, "min_buckets_for_omd": 50,
            "fpr_window": 20, "fpr_target": 0.15,
            
            # ─── PRE-MARKET STATE MACHINE CONFIG ───
            "auction_entry_end_ns": 9 * 3600 + 4 * 60,      # 09:04 in seconds from midnight
            "auction_freeze_end_ns": 9 * 3600 + 8 * 60,     # 09:08 in seconds from midnight
            "continuous_open_ns": 9 * 3600 + 15 * 60,       # 09:15 in seconds from midnight
            "auction_freeze_min_ticks": 5,                  # Min ticks to confirm freeze
            "auction_freeze_timeout_sec": 300,              # 5 min max freeze wait
            "baseline_delta_buckets": 3,                    # First 3 buckets use delta-from-baseline
            "min_tick_size": 0.05,                          # NSE min tick for freeze detection
        }
        self.CFG.update(default_trigger)   # apply defaults FIRST
        if trigger_config: self.CFG.update(trigger_config)  # then apply any explicit overrides
        self.TRIG = {k: v for k, v in self.CFG.items() if k in default_trigger}
        
        # Logging
        self.LOG_FILE = log_path or os.path.join(
            get_data_dir(), "logs", f"cascade_live_{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        )
        os.makedirs(os.path.dirname(self.LOG_FILE), exist_ok=True)
    
    def _init_symbol_state(self):
        """Pre-allocate all per-symbol state to fixed keys. No dynamic dict growth."""
        for sym in self.t2s.values():
            self.last_snap[sym] = {}
            self.last_depth_state[sym] = {'bids': {}, 'asks': {}}
            self.trade_times[sym] = deque(maxlen=50000)
            self.completion_times[sym] = deque(maxlen=1000)
            self.vpin_q[sym] = deque(maxlen=20)
            self.whale_history[sym] = deque(maxlen=self.CFG["whale_window"])
            self.vacuum_history[sym] = deque(maxlen=self.CFG["vacuum_window"])
            self.mlofi_history[sym] = deque(maxlen=self.CFG["mlofi_window"])
            self.toxicity_history[sym] = deque(maxlen=self.CFG["toxicity_window"])
            self.mp_prev[sym] = 0.0
            self.mp_vel[sym] = 0.0
            self.mp_accel[sym] = 0.0
            self.lra_ema[sym] = {'bid': [0.0]*5, 'ask': [0.0]*5}
            self.lra_asym_prev[sym] = 0.0
            self.dkla_asym_prev[sym] = 0.0
            self.cur_buy_val[sym] = 0.0
            self.cur_sell_val[sym] = 0.0
            self.cur_tot_val[sym] = 0.0
            self.cur_tick_count[sym] = 0
            self.cur_max_tick_val[sym] = 0.0
            self.cur_net_vacuum[sym] = 0.0
        self._bucket_close_prices = {sym: 0.0 for sym in self.t2s.values()}
        
        # ─── PRE-MARKET STATE MACHINE STATE ───
        self._phase = {sym: EnginePhase.AUCTION_ENTRY for sym in self.t2s.values()}
        self._baseline_truth = {sym: None for sym in self.t2s.values()}
        self._last_l2_snapshot = {sym: {'bids': {}, 'asks': {}} for sym in self.t2s.values()}
        self._auction_tick_count = {sym: 0 for sym in self.t2s.values()}
        self._freeze_confirmed = {sym: False for sym in self.t2s.values()}
        self._baseline_bucket_count = {sym: 0 for sym in self.t2s.values()}
        
        # ─── GLOBAL 1-TRADE LOCK ───
        self._engine_active_trade = False
        self._active_trade_symbol = None
        
        # ─── ML CONVICTION MULTIPLIER ───
        self._ml_predictor = None
        self._ml_feature_computer = None
        self._ml_calibration_dir = None
        self._ml_regime_model_dir = None

    def initialize_ml_predictor(self, calibration_dir: str, regime_model_dir: str) -> None:
        """
        Initialize ML predictor from EOD calibration artifacts.
        Call this after engine creation but before starting live trading.
        
        Args:
            calibration_dir: Path to calibration directory with predictor_global.py
            regime_model_dir: Path to regime model directory
        """
        from pathlib import Path
        from ARGUS.eod.live_inference import CompiledPredictor, LiveFeatureComputer
        
        self._ml_calibration_dir = Path(calibration_dir)
        self._ml_regime_model_dir = Path(regime_model_dir)
        
        if not self._ml_calibration_dir.exists():
            logger.warning(f"ML calibration dir not found: {calibration_dir}")
            return
        
        self._ml_predictor = CompiledPredictor(self._ml_calibration_dir)
        self._ml_feature_computer = LiveFeatureComputer()
        
        logger.info(f"ML predictor initialized: {self._ml_predictor.model_type}")
        if self._ml_predictor.global_meta:
            logger.info(f"  Symbols: {len(self._ml_predictor.global_meta.get('symbol_to_idx', {}))}")
            logger.info(f"  Regimes: {list(self._ml_predictor.global_meta.get('regime_to_idx', {}).keys())}")

    # ═══════════════════════════════════════════════════════════
    # CORE MICROSTRUCTURE CALCULATIONS (FIXED & OPTIMIZED)
    # ═══════════════════════════════════════════════════════════

    def _calculate_vacuum_score(self, sym: str, curr_bids: Dict, curr_asks: Dict, 
                                trade_qty: float, trade_price: float, 
                                touch_bid: float, touch_ask: float) -> float:
        """
        Calculate net vacuum (cancellation pressure) in bps.
        Tracks PURE cancellations only — trades are distinct events and must NOT be
        subtracted from cancellations (the last_state snapshot is pre-trade, so old_qty
        already includes filled quantity; subtracting trade_qty again double-counts).
        """
        vacuum_raw = 0.0
        last_state = self.last_depth_state.get(sym, {'bids': {}, 'asks': {}})
        for price, old_qty in last_state['bids'].items():
            if price in curr_bids:
                dropped = old_qty - curr_bids[price]
                if dropped > 0:
                    # Pure cancellation: price level dropped but NO trade at this price
                    # (trade at this price is a fill, not a cancellation)
                    if not (abs(price - trade_price) < 1e-6 and trade_qty > 0):
                        vacuum_raw -= dropped * price
        for price, old_qty in last_state['asks'].items():
            if price in curr_asks:
                dropped = old_qty - curr_asks[price]
                if dropped > 0:
                    # Pure cancellation: price level dropped but NO trade at this price
                    if not (abs(price - trade_price) < 1e-6 and trade_qty > 0):
                        vacuum_raw += dropped * price
        self.last_depth_state[sym] = {'bids': curr_bids, 'asks': curr_asks}
        total_depth = sum(p*q for p,q in curr_bids.items()) + sum(p*q for p,q in curr_asks.items())
        hist = self.vacuum_history.setdefault(sym, deque(maxlen=self.CFG["vacuum_window"]))
        hist.append(total_depth)
        if hist:
            mean = sum(hist) / len(hist)
        else:
            mean = 1.0
        return (vacuum_raw / max(mean, 1.0)) * 10_000.0

    def _calculate_mlofi(self, sym: str, snap: Dict, flow_imb: float) -> float:
        b_val = snap.get("bp1",0)*snap.get("bq1",0); s_val = snap.get("sp1",0)*snap.get("sq1",0)
        book_num = (b_val - s_val) * 1.0; book_den = (b_val + s_val) * 1.0
        b_val = snap.get("bp2",0)*snap.get("bq2",0); s_val = snap.get("sp2",0)*snap.get("sq2",0)
        book_num += (b_val - s_val) * 0.7408; book_den += (b_val + s_val) * 0.7408
        b_val = snap.get("bp3",0)*snap.get("bq3",0); s_val = snap.get("sp3",0)*snap.get("sq3",0)
        book_num += (b_val - s_val) * 0.5488; book_den += (b_val + s_val) * 0.5488
        b_val = snap.get("bp4",0)*snap.get("bq4",0); s_val = snap.get("sp4",0)*snap.get("sq4",0)
        book_num += (b_val - s_val) * 0.4066; book_den += (b_val + s_val) * 0.4066
        b_val = snap.get("bp5",0)*snap.get("bq5",0); s_val = snap.get("sp5",0)*snap.get("sq5",0)
        book_num += (b_val - s_val) * 0.3012; book_den += (b_val + s_val) * 0.3012
        book_imb = book_num / max(book_den, 1.0)
        mlofi_raw = book_imb - flow_imb
        hist = self.mlofi_history.setdefault(sym, deque(maxlen=self.CFG["mlofi_window"]))
        hist.append(mlofi_raw)
        if len(hist) > 10: return (mlofi_raw - np.mean(hist)) / max(np.std(hist), 1e-6)
        return mlofi_raw

    def _calculate_whale_z(self, sym: str, conc: float) -> float:
        hist = self.whale_history.setdefault(sym, deque(maxlen=self.CFG["whale_window"]))
        hist.append(conc)
        if len(hist) < 10: return 0.0
        return (conc - np.mean(hist)) / max(np.std(hist), 1e-6)

    def _calculate_dkla(self, snap: Dict, mid: float) -> Tuple[float, float, float, float]:
        notional = self.CFG["dkla_notional"]
        ap = [snap.get(f"sp{i}",0.0) for i in range(1,6)]; aq = [snap.get(f"sq{i}",0.0) for i in range(1,6)]
        bp = [snap.get(f"bp{i}",0.0) for i in range(1,6)]; bq = [snap.get(f"bq{i}",0.0) for i in range(1,6)]
        lam_buy = _walk_book_impact_fast(ap, aq, notional, mid)
        lam_sell = _walk_book_impact_fast(bp, bq, notional, mid)
        denom = lam_buy + lam_sell
        asym = (lam_buy - lam_sell) / max(denom, 1e-6) if denom > 0 else 0.0
        eff_lam = max(lam_buy, lam_sell)
        return lam_buy, lam_sell, asym, eff_lam

    def _update_lra(self, sym: str, snap: Dict, curr_bids: Dict, curr_asks: Dict, 
                    trade_price: float, trade_qty: float) -> Tuple[float, float]:
        alpha = self.CFG["lra_alpha"]
        state = self.lra_ema.setdefault(sym, {'bid': [0.0]*5, 'ask': [0.0]*5})
        bid_emas, ask_emas = state['bid'], state['ask']
        last = self.last_depth_state.get(sym, {'bids': {}, 'asks': {}})
        
        for i in range(1, 6):
            p = snap.get(f"bp{i}", 0.0)
            if p > 0:
                oq = last['bids'].get(p, 0); nq = curr_bids.get(p, 0)
                if nq > oq and not (abs(p - trade_price) < 1e-6 and trade_qty > 0):
                    bid_emas[i-1] = _ema_update(bid_emas[i-1], float(nq - oq), alpha)
        for i in range(1, 6):
            p = snap.get(f"sp{i}", 0.0)
            if p > 0:
                oq = last['asks'].get(p, 0); nq = curr_asks.get(p, 0)
                if nq > oq and not (abs(p - trade_price) < 1e-6 and trade_qty > 0):
                    ask_emas[i-1] = _ema_update(ask_emas[i-1], float(nq - oq), alpha)
        
        bid_res = bid_emas[0]*0.7 + bid_emas[1]*0.3
        ask_res = ask_emas[0]*0.7 + ask_emas[1]*0.3
        denom = bid_res + ask_res
        asym = (bid_res - ask_res) / max(denom, 1e-6) if denom > 0 else 0.0
        vel = asym - self.lra_asym_prev.get(sym, 0.0)
        self.lra_asym_prev[sym] = asym
        return asym, vel

    def _calculate_queue_decay(self, sym: str, snap: Dict, curr_bids: Dict[float, int], 
                               curr_asks: Dict[float, int], dVol: float = 0.0, lp: float = 0.0) -> Tuple[float, float]:
        """
        Calculate queue position decay in bps for Bid and Ask.
        """
        bp1, bq1 = snap.get("bp1", 0.0), snap.get("bq1", 0)
        sp1, sq1 = snap.get("sp1", 0.0), snap.get("sq1", 0)
        
        if bq1 <= 0 or bp1 <= 0 or sp1 <= 0 or sq1 <= 0:
            return 0.0, 0.0
        
        last = self.last_depth_state.get(sym, {'bids': {}, 'asks': {}})
        
        # Bid queue consumption = cancellations at bid + trades hitting bid (aggressive sells)
        bid_cancelled = max(0, last['bids'].get(bp1, 0) - curr_bids.get(bp1, 0))
        trade_at_bid = dVol if (dVol > 0 and lp <= bp1) else 0.0
        
        # Ask queue consumption = cancellations at ask + trades hitting ask (aggressive buys)
        ask_cancelled = max(0, last['asks'].get(sp1, 0) - curr_asks.get(sp1, 0))
        trade_at_ask = dVol if (dVol > 0 and lp >= sp1) else 0.0
        
        bid_consumption = bid_cancelled + trade_at_bid
        ask_consumption = ask_cancelled + trade_at_ask
        
        bid_decay_bps = (bid_consumption / max(bq1, 1)) * 100.0
        ask_decay_bps = (ask_consumption / max(sq1, 1)) * 100.0
        
        return bid_decay_bps, ask_decay_bps

    def _estimate_adverse_selection(self, sym: str, side: str, 
                                     vpins: float, oimb: float, 
                                     spread_bps: float, toxicity: float,
                                     bid_queue_decay: float, ask_queue_decay: float,
                                     whale_z: float, branching: float) -> float:
        """
        Estimate P(adverse_selection | signal_side) ∈ [0, 1].
        Adverse selection occurs when order book dynamics OPPOSE the signal direction:
        - For BUY: Sellers dumping, whale selling (WhaleZ < 0), bid collapsing, wide spread.
        - For SELL: Buyers surging, whale buying (WhaleZ > 0), ask collapsing, wide spread.
        Whale in the direction of the trade is ALPHA, NOT toxicity.
        """
        if side == "BUY":
            flow_opposing = max(0.0, min(1.0, (1.0 - oimb))) if oimb < 1.0 else 0.0
            whale_opposing = max(0.0, min(1.0, -whale_z / 2.0)) if whale_z < 0 else 0.0
            queue_risk = min(max(bid_queue_decay / 100.0, 0.0), 1.0)
        else:
            flow_opposing = max(0.0, min(1.0, (oimb - 1.0))) if oimb > 1.0 else 0.0
            whale_opposing = max(0.0, min(1.0, whale_z / 2.0)) if whale_z > 0 else 0.0
            queue_risk = min(max(ask_queue_decay / 100.0, 0.0), 1.0)
            
        spread_risk = min(max((spread_bps - 5.0) / 10.0, 0.0), 1.0)
        branch_risk = min(max((0.15 - branching) / 0.15, 0.0), 1.0)
        
        # Weighted adverse selection probability
        p_adv = (
            0.35 * flow_opposing +
            0.25 * whale_opposing +
            0.20 * spread_risk +
            0.10 * queue_risk +
            0.10 * branch_risk
        )
        return float(min(max(p_adv, 0.0), 1.0))

    # ═══════════════════════════════════════════════════════════════════
    # Q1: MULTI-HORIZON MOMENTUM ALIGNMENT (Continuous, No Hard Gates)
    # ═══════════════════════════════════════════════════════════════════

    def _compute_multi_horizon_alignment(self, sym: str, lp: float, nows: float, 
                                          signal_side: str) -> float:
        """
        Continuous multi-horizon momentum alignment factor M_align ∈ [0, 1].
        
        Replaces hard gate (mom_1m >= 0) with smooth discounting of counter-trend signals
        while preserving explosive V-shape reversals.
        
        Horizons: 10s, 1m, 5m, 15m with weights [0.15, 0.30, 0.35, 0.20]
        Sensitivities: [8.0, 6.0, 4.0, 3.0] (shorter = sharper)
        
        Returns:
            M_align ∈ [0, 1]: 1.0 = perfect alignment, 0.0 = strong counter-trend
        """
        hist = self.regime_mom_history[sym]  # Pre-allocated deque of (ts, price)
        
        # Extract prices at horizons
        horizons = [(10, 'mom_10s'), (60, 'mom_1m'), (300, 'mom_5m'), (900, 'mom_15m')]
        moments = {}
        for window_sec, key in horizons:
            cutoff = nows - window_sec
            old_price = lp
            for ts, price in hist:
                if ts >= cutoff:
                    old_price = price
                    break
            moments[key] = (lp - old_price) / old_price if old_price > 0 else 0.0
        
        # Directional alignment
        direction = 1.0 if signal_side == "BUY" else -1.0
        
        # Weighted tanh sum
        weights = [0.15, 0.30, 0.35, 0.20]
        sensitivities = [8.0, 6.0, 4.0, 3.0]
        keys = ['mom_10s', 'mom_1m', 'mom_5m', 'mom_15m']
        
        aligned_sum = 0.0
        for w, k, key in zip(weights, sensitivities, keys):
            aligned_sum += w * math.tanh(k * moments[key] * direction)
        
        # Sigmoid squashing to [0, 1]
        return 1.0 / (1.0 + math.exp(-aligned_sum))

    # ═══════════════════════════════════════════════════════════════════
    # Q2: DYNAMIC S_FLOW THRESHOLD CALIBRATION
    # ═══════════════════════════════════════════════════════════════════

    def _compute_dynamic_sflow_threshold(self, sym: str, regime: int, nows: float) -> float:
        """
        Dynamic S_flow threshold θ_dynamic = θ_base · V_regime · L_tod · C_symbol
        
        Components:
        1. V_regime: Volatility regime factor (ATR/Spread ratio vs baseline)
        2. L_tod: Time-of-day liquidity profile
        3. C_symbol: Symbol/regime calibration from stock_calibration.json
        
        Returns:
            Dynamic threshold ∈ [0.35, 0.60]
        """
        # 1. Volatility regime factor
        cal = self.stock_calibration.get(sym, {})
        spread_bps = cal.get("spread_median_bps", 5.0)
        atr_15 = cal.get("atr_15_median", 0.003) * 10000  # Convert to bps
        
        if spread_bps > 0:
            ratio = atr_15 / spread_bps
            ratio_baseline = cal.get("atr_spread_ratio_baseline", ratio)
            v_regime = 1.0 + 0.15 * (ratio - ratio_baseline) / max(ratio_baseline, 0.1)
        else:
            v_regime = 1.0
        
        # 2. Time-of-day liquidity factor
        dt = datetime.fromtimestamp(nows)
        hour, minute = dt.hour, dt.minute
        mins_from_open = (hour - 9) * 60 + minute - 15
        
        if mins_from_open < 60:           # 09:15-10:15 Opening Rush
            l_tod = 0.85
        elif 135 <= mins_from_open < 225: # 11:30-13:00 Midday Lull
            l_tod = 1.15
        elif 225 <= mins_from_open < 315: # 13:00-14:30 European Open
            l_tod = 0.95
        elif mins_from_open >= 315:       # 14:30-15:15 EOD Closing
            l_tod = 1.05
        else:
            l_tod = 1.00
        
        # 3. Symbol/regime calibration factor
        reg_name = self._regime_names[regime].lower()
        conv_thresh = cal.get(f"conv_thresh_{reg_name}", 
                              self.CFG.get(f"conv_thresh_{reg_name}", 5.5))
        c_symbol = conv_thresh / 5.5
        
        # Combined dynamic threshold
        theta = 0.45 * v_regime * l_tod * c_symbol
        return max(0.35, min(0.60, theta))

    # ═══════════════════════════════════════════════════════════════════
    # Q3: QUEUE DEPLETION VELOCITY & SPOOFING DETECTION
    # ═══════════════════════════════════════════════════════════════════

    def _calculate_queue_depletion_metrics(self, sym: str, curr_bids: Dict, 
                                            curr_asks: Dict, dVol: float, 
                                            lp: float, dt: float) -> Dict:
        """
        Calculate L2 cancellation velocity, queue depletion speed, and spoofing detection.
        
        Returns dict with:
        - cv_bid, cv_ask: Cancellation velocity (shares/sec) at all 5 levels
        - qds_bid, qds_ask: Queue depletion speed (fraction/sec) at touch
        - sds_bid, sds_ask: Spoofing detection score (cancellations >> trades at touch)
        """
        last = self.last_depth_state[sym]
        
        # Cancellation velocity at each level (all 5 levels)
        cv_bid = 0.0
        cv_ask = 0.0
        for price, old_qty in last['bids'].items():
            new_qty = curr_bids.get(price, 0)
            if old_qty > new_qty:
                cv_bid += (old_qty - new_qty) / max(dt, 1e-6)
        for price, old_qty in last['asks'].items():
            new_qty = curr_asks.get(price, 0)
            if old_qty > new_qty:
                cv_ask += (old_qty - new_qty) / max(dt, 1e-6)
        
        # Queue depletion speed at touch (L1)
        bp1 = next(iter(curr_bids)) if curr_bids else 0
        ap1 = next(iter(curr_asks)) if curr_asks else 0
        bq1 = curr_bids.get(bp1, 1)
        aq1 = curr_asks.get(ap1, 1)
        
        trade_at_bid = dVol if (dVol > 0 and lp <= bp1) else 0
        trade_at_ask = dVol if (dVol > 0 and lp >= ap1) else 0
        
        qds_bid = (trade_at_bid + cv_bid) / max(bq1, 1)
        qds_ask = (trade_at_ask + cv_ask) / max(aq1, 1)
        
        # Spoofing detection: cancellations >> trades at touch
        cv_bid_touch = max(0, last['bids'].get(bp1, 0) - bq1) / max(dt, 1e-6)
        cv_ask_touch = max(0, last['asks'].get(ap1, 0) - aq1) / max(dt, 1e-6)
        
        sds_bid = max(0, (cv_bid_touch - trade_at_bid) / max(cv_bid_touch, 1))
        sds_ask = max(0, (cv_ask_touch - trade_at_ask) / max(cv_ask_touch, 1))
        
        return {
            'cv_bid': cv_bid, 'cv_ask': cv_ask,
            'qds_bid': qds_bid, 'qds_ask': qds_ask,
            'sds_bid': sds_bid, 'sds_ask': sds_ask
        }

    def _estimate_adverse_selection_enhanced(self, sym: str, side: str, 
                                              vpins: float, oimb: float, 
                                              spread_bps: float, toxicity: float,
                                              bid_queue_decay: float, ask_queue_decay: float,
                                              whale_z: float, branching: float,
                                              qds_bid: float = 0.0, qds_ask: float = 0.0,
                                              sds_bid: float = 0.0, sds_ask: float = 0.0,
                                              cv_bid: float = 0.0, cv_ask: float = 0.0,
                                              **kwargs) -> float:
        """
        Enhanced P_adv with queue depletion velocity and spoofing detection.
        
        New components:
        - QueueRisk: min(1.0, QDS_side / 0.5) where QDS_threshold = 0.5/sec
        - SpoofRisk: SDS on the side we're trading (fake wall detection)
        
        Weight redistribution: 0.30 FlowOpp + 0.20 WhaleOpp + 0.15 SpreadRisk 
                              + 0.10 QueueRisk + 0.10 BranchRisk + 0.15 SpoofRisk
        """
        if side == "BUY":
            flow_opposing = max(0.0, min(1.0, 1.0 - oimb)) if oimb < 1.0 else 0.0
            whale_opposing = max(0.0, min(1.0, -whale_z / 2.0)) if whale_z < 0 else 0.0
            queue_risk = min(1.0, qds_bid / 0.5)  # Bid queue depletion speed
            spoof_risk = sds_ask  # Ask spoofing = fake sell wall
        else:
            flow_opposing = max(0.0, min(1.0, oimb - 1.0)) if oimb > 1.0 else 0.0
            whale_opposing = max(0.0, min(1.0, whale_z / 2.0)) if whale_z > 0 else 0.0
            queue_risk = min(1.0, qds_ask / 0.5)  # Ask queue depletion speed
            spoof_risk = sds_bid  # Bid spoofing = fake buy wall
        
        spread_risk = min(max((spread_bps - 5.0) / 10.0, 0.0), 1.0)
        branch_risk = min(max((0.15 - branching) / 0.15, 0.0), 1.0)
        
        p_adv = (
            0.30 * flow_opposing +
            0.20 * whale_opposing +
            0.15 * spread_risk +
            0.10 * queue_risk +
            0.10 * branch_risk +
            0.15 * spoof_risk
        )
        return float(min(max(p_adv, 0.0), 1.0))

    def _update_mpar(self, sym: str, snap: Dict, dt: float) -> Tuple[float, float, float]:
        bp1, bq1 = snap.get("bp1",0.0), snap.get("bq1",0.0)
        ap1, aq1 = snap.get("sp1",0.0), snap.get("sq1",0.0)
        if not (bp1>0 and ap1>0 and bq1>0 and aq1>0): return 0.0, 0.0, 0.0
        mp = (bp1*aq1 + ap1*bq1) / (bq1 + aq1)
        prev_mp = self.mp_prev.get(sym, mp)
        prev_vel = self.mp_vel.get(sym, 0.0)
        vel = _ema_update(prev_vel, (mp - prev_mp)/max(dt,1e-6), self.CFG["mp_alpha"])
        accel = _ema_update(self.mp_accel.get(sym,0.0), (vel - prev_vel)/max(dt,1e-6), self.CFG["mp_accel_alpha"])
        self.mp_prev[sym], self.mp_vel[sym], self.mp_accel[sym] = mp, vel, accel
        # Instant Book Pressure (OIB)
        book_p = (bp1*bq1 - ap1*aq1) / max(bp1*bq1 + ap1*aq1, 1.0)
        align = 1.0 if (accel * book_p) > 0 else -1.0
        return accel, align, mp

    # ═══════════════════════════════════════════════════════════
    # ADAPTIVE GATING: REGIME CLASSIFICATION & DYNAMIC THRESHOLDS
    # ═══════════════════════════════════════════════════════════

    def _update_regime_state(self, sym_idx: int, beta: float, whale_z: float, mom_5m: float) -> None:
        """EMA-based regime classifier: 0=NOISE, 1=BURST, 2=GRIND, 3=WHALE. O(1), zero allocation.
        Kept for telemetry/logging only. Core gating now uses Flow Impetus metric."""
        alpha = self.CFG["ema_alpha_regime"]  # 0.15
        b_ema = self.regime_beta_ema[sym_idx]
        w_ema = self.regime_W_ema[sym_idx]
        m_ema = self.regime_mom_ema[sym_idx]
        b_var = self.regime_beta_var[sym_idx]
        w_var = self.regime_W_var[sym_idx]
        
        # EMA updates
        self.regime_beta_ema[sym_idx] = alpha * beta + (1 - alpha) * b_ema
        self.regime_W_ema[sym_idx] = alpha * whale_z + (1 - alpha) * w_ema
        self.regime_mom_ema[sym_idx] = alpha * mom_5m + (1 - alpha) * m_ema
        
        # Variance for confidence
        self.regime_beta_var[sym_idx] = alpha * (beta - self.regime_beta_ema[sym_idx])**2 + (1 - alpha) * b_var
        self.regime_W_var[sym_idx] = alpha * (whale_z - self.regime_W_ema[sym_idx])**2 + (1 - alpha) * w_var
        
        # Classify (for telemetry only)
        b, w, m = self.regime_beta_ema[sym_idx], self.regime_W_ema[sym_idx], abs(self.regime_mom_ema[sym_idx])
        if b >= self.CFG["regime_burst_beta_min"] and w >= self.CFG["regime_burst_W_min"] and m >= self.CFG["regime_burst_mom_min"]:
            self.regime_id[sym_idx] = 1  # BURST
            self.regime_conf[sym_idx] = 0.9
        elif (self.CFG["regime_grind_beta_min"] <= b < self.CFG["regime_grind_beta_max"] and 
              w >= self.CFG["regime_grind_W_min"] and m >= self.CFG["regime_grind_mom_min"]):
            self.regime_id[sym_idx] = 2  # GRIND
            self.regime_conf[sym_idx] = 0.8
        elif b < self.CFG["regime_grind_beta_min"] and w >= self.CFG["regime_whale_W_min"]:
            self.regime_id[sym_idx] = 3  # WHALE
            self.regime_conf[sym_idx] = 0.7
        else:
            self.regime_id[sym_idx] = 0  # NOISE
            self.regime_conf[sym_idx] = 0.5

    def _get_dynamic_thresholds(self, sym_idx: int) -> tuple:
        """Beta-Binomial posterior mean + UCB exploration. Falls back to minimax when data < 30 buckets."""
        if self.bucket_count[sym_idx] < self.CFG["min_buckets_for_bayes"]:
            return (self.CFG["minimax_beta"], self.CFG["minimax_W"], 
                    self.CFG["minimax_mom"], self.CFG["minimax_tox_pct"])
        
        c = self.CFG["ucb_exploration_c"]
        n_beta = self.post_alpha_beta[sym_idx] + self.post_beta_beta[sym_idx]
        n_W = self.post_alpha_W[sym_idx] + self.post_beta_W[sym_idx]
        
        t_beta = self.post_alpha_beta[sym_idx] / n_beta + c * math.sqrt(math.log(self.bucket_count[sym_idx]) / n_beta)
        t_W = self.post_alpha_W[sym_idx] / n_W + c * math.sqrt(math.log(self.bucket_count[sym_idx]) / n_W)
        
        return (float(max(0.08, min(t_beta, 0.22))), float(max(0.6, min(t_W, 2.5))),
                self.CFG["minimax_mom"], self.CFG["minimax_tox_pct"])

    def _compute_unified_s_flow(self, mpar_accel: float, net_vac: float, whale_z: float, mom_1m: float,
                                       mlofi: float, lra_signal: float, dkla_asym: float, micro_dev: float,
                                       branching: float, regime: int) -> tuple:
        """
        Unified Composite Microstructure Impulse Engine (S_flow).
        
        Single continuous score combining all alpha primitives with temporal alignment awareness.
        No Boolean gates. No phase separation. Replaces dual-gate + 20-term polynomial.
        
        S_flow = Linear_Base + Physics_Interactions
        Linear_Base = w_mpar*MPAR + w_vac*VAC + w_whale*WHALE + w_mom*MOM + w_lra*LRA + w_dkla*DKLA + w_micro*MICRO + w_branch*BRANCH
        Physics_Interactions = w_mlofi_vac*MLOFI*VAC + w_mpar_lra*MPAR*LRA + w_whale_dkla*WHALE*DKLA + w_vac_dkla*VAC*DKLA
        
        Returns: (s_flow_long, s_flow_short) in range [0, 1]
        """
        # Fast scalar clipping (zero allocation, no numpy)
        clip = lambda x, lo, hi: lo if x < lo else hi if x > hi else x
        
        # Normalize all inputs to [0,1] using calibrated caps
        mpar_n   = clip(abs(mpar_accel) / self.CFG["mpar_cap"], 0.0, 1.0)
        vac_n    = clip(abs(net_vac) / self.CFG["vac_cap"], 0.0, 1.0)
        whale_n  = clip(abs(whale_z) / 3.0, 0.0, 1.0)
        mom_n    = clip(abs(mom_1m) / 0.5, 0.0, 1.0)
        lra_n    = clip(abs(lra_signal) / self.CFG["lra_cap"], 0.0, 1.0)
        dkla_n   = clip(abs(dkla_asym) / self.CFG["dkla_cap"], 0.0, 1.0)
        micro_n  = 1.0 if abs(micro_dev) > 0.0002 else 0.0
        branch_n = clip(branching / 0.5, 0.0, 1.0)
        mlofi_n  = clip(abs(mlofi) / self.CFG["mlofi_cap"], 0.0, 1.0)
        
        # Directional signs for long/short
        mpar_sign = 1.0 if mpar_accel >= 0 else -1.0
        vac_sign = 1.0 if net_vac >= 0 else -1.0
        lra_sign = 1.0 if lra_signal >= 0 else -1.0
        dkla_sign = 1.0 if dkla_asym >= 0 else -1.0
        mom_sign = 1.0 if mom_1m >= 0 else -1.0
        mlofi_sign = 1.0 if mlofi >= 0 else -1.0
        
        # Core weights (sum = 1.0 for interpretability)
        w_mpar = 0.30   # Microprice acceleration — highest alpha
        w_vac = 0.20    # Liquidity vacuum — pure cancellation pressure
        w_whale = 0.20  # Whale presence — large lot clustering
        w_mom = 0.10    # 1-min momentum — directional confirmation
        w_lra = 0.08    # MM retreat/advance (lags 1-2 ticks)
        w_dkla = 0.07   # Impact asymmetry (confirms direction)
        w_micro = 0.03  # Microprice stretch (mean reversion risk)
        w_branch = 0.02 # Self-excitation (cluster confirmation)
        
        # Linear base (same for long/short, direction from signs)
        linear_base = (
            w_mpar * mpar_n +
            w_vac * vac_n +
            w_whale * whale_n +
            w_mom * mom_n +
            w_lra * lra_n +
            w_dkla * dkla_n +
            w_micro * micro_n +
            w_branch * branch_n
        )
        
        # Physics-based interactions (4 only - microstructure cascade physics)
        inter_long = 0.0
        inter_short = 0.0
        
        # 1. MLOFI x VACUUM: Book-flow divergence + cancellation pressure
        if mlofi_sign > 0 and vac_sign > 0:
            inter_long += 0.15 * mlofi_n * vac_n
        elif mlofi_sign < 0 and vac_sign < 0:
            inter_short += 0.15 * mlofi_n * vac_n
        
        # 2. MPAR x LRA: Microprice acceleration + MM retreat
        if mpar_sign > 0 and lra_sign > 0:
            inter_long += 0.12 * min(mpar_n, lra_n)
        elif mpar_sign < 0 and lra_sign < 0:
            inter_short += 0.12 * min(mpar_n, lra_n)
        
        # 3. WHALE x DKLA: Large lots + impact asymmetry
        if whale_n > 0 and dkla_sign > 0:
            inter_long += 0.10 * whale_n * dkla_n
        elif whale_n > 0 and dkla_sign < 0:
            inter_short += 0.10 * whale_n * dkla_n
        
        # 4. VACUUM x DKLA: Liquidity removal + impact asymmetry (strong directional)
        if vac_sign > 0 and dkla_sign > 0:
            inter_long += 0.08 * vac_n * dkla_n
        elif vac_sign < 0 and dkla_sign < 0:
            inter_short += 0.08 * vac_n * dkla_n
        
        # Regime modulation (multiplicative, not branching)
        regime_boost = (0.85, 1.00, 0.95, 1.05)[regime]  # NOISE, BURST, GRIND, WHALE
        
        s_flow_long = clip((linear_base + inter_long) * regime_boost, 0.0, 1.0)
        s_flow_short = clip((linear_base + inter_short) * regime_boost, 0.0, 1.0)
        
        return s_flow_long, s_flow_short

    def _update_threshold_posteriors(self, sym_idx: int, signal_fired: bool, move_captured: bool) -> None:
        """Beta-Binomial conjugate update for branching & whale thresholds."""
        if not signal_fired:
            return
        if move_captured:
            self.post_alpha_beta[sym_idx] += 1.0
            self.post_alpha_W[sym_idx] += 1.0
        else:
            self.post_beta_beta[sym_idx] += 1.0
            self.post_beta_W[sym_idx] += 1.0

    def _compute_5m_momentum(self, sym: str, lp: float, nows: float) -> Tuple[float, float]:
        """Compute 5-minute and 1-minute LTP momentum (%). Zero-allocation using pre-allocated deque."""
        hist = self.regime_mom_history[sym]
        hist.append((nows, lp))
        cutoff_5m = nows - 300.0  # 5 minutes
        cutoff_1m = nows - 60.0   # 1 minute
        old_lp_5m = lp
        old_lp_1m = lp
        found_5m = False
        for ts, price in hist:
            if ts >= cutoff_5m and not found_5m:
                old_lp_5m = price
                found_5m = True
            if ts >= cutoff_1m:
                old_lp_1m = price
                break
        m5 = (lp - old_lp_5m) / old_lp_5m * 100.0 if old_lp_5m > 0 else 0.0
        m1 = (lp - old_lp_1m) / old_lp_1m * 100.0 if old_lp_1m > 0 else 0.0
        return m5, m1

    def _compute_interaction_conviction(self, regime: int, mlofi: float, mpar: float, net_vac: float, 
                                        lra_signal: float, dkla_asym: float, micro_dev: float, 
                                        whale_z: float, branching: float, mom_5m: float) -> tuple:
        """Zero-allocation interaction-aware conviction scoring with unified weights.
        Regime parameter kept for API compatibility (telemetry only). Returns (long_score, short_score)."""
        # Main effects (monotonic transforms for long: positive values; for short: negative values)
        # Fast Scalar Clipping replacing np.clip
        mlofi_long = max(0.0, min(mlofi / self.CFG["mlofi_cap"], 1.0))
        mpar_long = max(0.0, min(mpar / self.CFG["mpar_cap"], 1.0))
        vac_long = max(0.0, min(net_vac / self.CFG["vac_cap"], 1.0))
        lra_long = max(0.0, min(lra_signal / self.CFG["lra_cap"], 1.0))
        dkla_long = max(0.0, min(dkla_asym / self.CFG["dkla_cap"], 1.0))
        micro_long = 1.0 if micro_dev > 0.0002 else 0.0
        
        mlofi_short = max(0.0, min(-mlofi / self.CFG["mlofi_cap"], 1.0))
        mpar_short = max(0.0, min(-mpar / self.CFG["mpar_cap"], 1.0))
        vac_short = max(0.0, min(-net_vac / self.CFG["vac_cap"], 1.0))
        lra_short = max(0.0, min(-lra_signal / self.CFG["lra_cap"], 1.0))
        dkla_short = max(0.0, min(-dkla_asym / self.CFG["dkla_cap"], 1.0))
        micro_short = 1.0 if micro_dev < -0.0002 else 0.0
        
        whale_t = max(0.0, min(abs(whale_z) / 3.0, 1.0))
        beta_t = max(0.0, min(branching / 0.5, 1.0))
        mom_t = max(0.0, min(abs(mom_5m) / 0.5, 1.0))
        
        # Unified main effect weights (regime-agnostic, calibrated for 0.40%-0.60% spike capture)
        w_mlofi, w_mpar, w_vac, w_lra, w_dkla, w_micro = 1.8, 2.2, 2.0, 1.5, 1.8, 1.0
        
        # Linear weighted sum (base conviction)
        base_long = (mlofi_long * w_mlofi + mpar_long * w_mpar + vac_long * w_vac +
                     lra_long * w_lra + dkla_long * w_dkla + micro_long * w_micro)
        base_short = (mlofi_short * w_mlofi + mpar_short * w_mpar + vac_short * w_vac +
                      lra_short * w_lra + dkla_short * w_dkla + micro_short * w_micro)
        
        # Pairwise interactions (directional: long uses positive signals, short uses negative)
        inter_long = 0.0
        inter_short = 0.0

        # MLOFI x VACUUM: bid pressure + ask vacuum = long; ask pressure + bid vacuum = short
        if mlofi_long > 0 and vac_long > 0:
            inter_long += self.CFG["interact_mlofi_vac"] * mlofi_long * vac_long
        if mlofi_short > 0 and vac_short > 0:
            inter_short += self.CFG["interact_mlofi_vac"] * mlofi_short * vac_short

        # MPAR x LRA: microprice accel up + MM flee ask = long; accel down + MM flee bid = short
        if mpar_long > 0 and lra_long > 0:
            inter_long += self.CFG["interact_mpar_lra"] * min(mpar_long, lra_long)
        if mpar_short > 0 and lra_short > 0:
            inter_short += self.CFG["interact_mpar_lra"] * min(mpar_short, lra_short)

        # WHALE x DKLA: large lots + buy-side impact = long; large lots + sell-side impact = short
        if whale_t > 0 and dkla_long > 0:
            inter_long += self.CFG["interact_whale_dkla"] * whale_t * dkla_long
        if whale_t > 0 and dkla_short > 0:
            inter_short += self.CFG["interact_whale_dkla"] * whale_t * dkla_short

        # BRANCHING x MOMENTUM: clustering + trend = directional confirmation
        if beta_t > 0 and mom_t > 0:
            if mom_5m > 0:
                inter_long += self.CFG["interact_beta_mom"] * beta_t * mom_t
            else:
                inter_short += self.CFG["interact_beta_mom"] * beta_t * mom_t

        # NEW: VACUUM x DKLA: liquidity removal + impact asymmetry (strong directional signal)
        if vac_long > 0 and dkla_long > 0:
            inter_long += 0.25 * vac_long * dkla_long
        if vac_short > 0 and dkla_short > 0:
            inter_short += 0.25 * vac_short * dkla_short

        # NEW: MLOFI x MICRODEV: book-flow divergence + microprice stretch
        if mlofi_long > 0 and micro_long > 0:
            inter_long += 0.20 * mlofi_long * micro_long
        if mlofi_short > 0 and micro_short > 0:
            inter_short += 0.20 * mlofi_short * micro_short
        
        return base_long + inter_long, base_short + inter_short

    def _get_regime_conviction_threshold(self, regime: int, sym: str) -> float:
        """Regime-adaptive conviction threshold with dynamic FPR adjustment and per-stock calibration."""
        sym_cfg = self.stock_calibration.get(sym, {})
        reg_name = self._regime_names[regime].lower()
        custom_thresh = sym_cfg.get(f"conv_thresh_{reg_name}")
        
        if custom_thresh is not None:
            base_thresh = float(custom_thresh)
        else:
            base_thresh = {1: self.CFG["conv_thresh_burst"], 
                           2: self.CFG["conv_thresh_grind"], 
                           3: self.CFG["conv_thresh_whale"], 
                           0: self.CFG["conv_thresh_noise"]}[regime]
        
        if len(self.recent_fp[sym]) >= self.CFG["fpr_window"]:
            fpr = sum(self.recent_fp[sym]) / len(self.recent_fp[sym])
            base_thresh *= (1.0 + 0.5 * fpr / self.CFG["fpr_target"])
        
        return base_thresh

    def _apply_ml_conviction_multiplier(self, conviction: float, ml_prob: float, regime: int) -> float:
        """
        Apply ML probability as multiplicative conviction multiplier.
        
        Args:
            conviction: Base conviction score from interaction-aware scoring
            ml_prob: ML model probability from EOD calibration (0.0 to 1.0)
            regime: Current regime (0=NOISE, 1=BURST, 2=GRIND, 3=WHALE)
        
        Returns:
            Adjusted conviction score
        
        Rules:
            - Veto: if ml_prob < 0.40, conviction = 0 (hard veto)
            - Boost: if ml_prob > 0.65, conviction *= (1.0 + (ml_prob - 0.65) * 2.0)
            - Neutral: 0.40 <= ml_prob <= 0.65, conviction unchanged
        """
        if ml_prob < 0.40:
            # Hard veto - ML model strongly disagrees
            return 0.0
        
        if ml_prob > 0.65:
            # Boost conviction proportionally to ML confidence
            boost_factor = 1.0 + (ml_prob - 0.65) * 2.0  # 0.65->1.0x, 0.80->1.3x, 0.90->1.5x
            return conviction * boost_factor
        
        # Neutral zone - no adjustment
        return conviction

    def _calculate_kelly_fraction(self, sym: str, side: str, conviction: float, 
                                   ml_prob: Optional[float], regime: int) -> float:
        """
        Calculate Kelly fraction for position sizing based on ML probability and historical metrics.
        
        Kelly Formula: f* = (p * b - q) / b
        where:
        - p = probability of win (from ML model)
        - q = 1 - p = probability of loss
        - b = win/loss ratio (from historical metrics)
        
        For our system:
        - p = ML probability (if available) or conviction-based estimate
        - b = average win / average loss from trade history
        
        Args:
            sym: Symbol
            side: BUY or SELL
            conviction: Conviction score (0-10)
            ml_prob: ML model probability (0.0-1.0) or None
            regime: Current regime
            
        Returns:
            Kelly fraction (0.0 to 1.0)
        """
        # Base win probability from ML or conviction
        if ml_prob is not None:
            p = ml_prob
        else:
            # Estimate from conviction: conviction 5.0 -> ~55% win rate, 10.0 -> ~70%
            p = 0.5 + (conviction / 10.0) * 0.2
        
        # Get historical win/loss ratio from trade history
        # Default to 1.5:1 (typical for microstructure strategies)
        win_loss_ratio = self._get_historical_win_loss_ratio(sym, regime)
        
        # Kelly fraction: f = (p * b - (1-p)) / b
        # where b = win_loss_ratio
        b = win_loss_ratio
        q = 1.0 - p
        
        kelly = (p * b - q) / b
        
        # Apply regime-specific scaling
        regime_scaling = {
            0: 0.5,   # NOISE - reduce size
            1: 1.0,   # BURST - full Kelly
            2: 0.75,  # GRIND - moderate
            3: 1.25,  # WHALE - increase (high conviction regime)
        }
        kelly *= regime_scaling.get(regime, 1.0)
        
        # Apply conviction scaling (higher conviction = more Kelly)
        conviction_scale = 0.5 + (conviction / 10.0) * 0.5  # 0.5x to 1.0x
        kelly *= conviction_scale
        
        # Cap at reasonable bounds
        return max(0.0, min(kelly, 1.0))

    def _get_historical_win_loss_ratio(self, sym: str, regime: int) -> float:
        """
        Get historical win/loss ratio for a symbol/regime.
        Returns default 1.5 if no history available.
        """
        # TODO: Implement actual trade history lookup
        # For now, return regime-specific defaults
        regime_defaults = {
            0: 1.2,   # NOISE - lower win/loss
            1: 1.8,   # BURST - higher win/loss
            2: 1.5,   # GRIND - moderate
            3: 2.0,   # WHALE - highest win/loss
        }
        return regime_defaults.get(regime, 1.5)

    def _get_ml_probability(self, sym: str, regime: int) -> Optional[float]:
        """
        Get ML probability from EOD calibration for a symbol/regime.
        Returns None if ML predictor not available or symbol not in calibration.
        """
        if self._ml_predictor is None or self._ml_feature_computer is None:
            return None
        
        regime_name = self._regime_names[regime]
        
        try:
            # Get features from the live feature computer
            meta = self._ml_predictor.get_metadata(sym, regime_name)
            feature_names = meta.get("feature_names", [])
            
            if not feature_names:
                return None
            
            features = self._ml_feature_computer.get_features(feature_names)
            
            # Get ML probability
            prob = self._ml_predictor.predict(sym, regime_name, features)
            return float(prob)
        except Exception:
            return None

    # ═══════════════════════════════════════════════════════════
    # PRE-MARKET STATE MACHINE HANDLERS
    # ═══════════════════════════════════════════════════════════

    def _seconds_from_midnight(self, ts: float) -> int:
        """Convert timestamp to seconds from midnight (IST)."""
        dt = datetime.fromtimestamp(ts)
        return dt.hour * 3600 + dt.minute * 60 + dt.second

    def _detect_auction_freeze(self, sym: str, curr_bids: Dict, curr_asks: Dict) -> bool:
        """Detect if L2 is frozen (no changes for min_ticks) and spread == min_tick_size."""
        last = self._last_l2_snapshot[sym]
        # Check if L2 unchanged
        if curr_bids == last['bids'] and curr_asks == last['asks']:
            self._auction_tick_count[sym] += 1
        else:
            self._auction_tick_count[sym] = 0
            # Update snapshot
            last['bids'].clear()
            last['bids'].update(curr_bids)
            last['asks'].clear()
            last['asks'].update(curr_asks)
        
        # Check spread == min_tick_size
        bp1 = next(iter(curr_bids)) if curr_bids else 0.0
        ap1 = next(iter(curr_asks)) if curr_asks else 0.0
        spread_ok = (ap1 - bp1) <= self.CFG["min_tick_size"] + 1e-6
        
        return self._auction_tick_count[sym] >= self.CFG["auction_freeze_min_ticks"] and spread_ok

    def _detect_continuous_open(self, sym: str, msg: Dict, curr_bids: Dict, curr_asks: Dict) -> bool:
        """Detect transition to continuous trading: trade print OR L2 mutation OR spread widened."""
        # Trade print detection
        lp = float(msg.get("last_traded_price") or msg.get("lp") or 0)
        if lp > 0:
            return True
        
        # L2 mutation detection
        last = self._last_l2_snapshot[sym]
        if curr_bids != last['bids'] or curr_asks != last['asks']:
            return True
        
        # Spread widened beyond min_tick
        bp1 = next(iter(curr_bids)) if curr_bids else 0.0
        ap1 = next(iter(curr_asks)) if curr_asks else 0.0
        if (ap1 - bp1) > self.CFG["min_tick_size"] + 1e-6:
            return True
        
        return False

    def _capture_baseline(self, sym: str, st: Dict, curr_bids: Dict, curr_asks: Dict, nows: float) -> None:
        """Capture immutable BaselineTruth at 09:08 freeze."""
        # Convert dicts to immutable tuples
        bids_tuple = tuple(sorted(curr_bids.items()))
        asks_tuple = tuple(sorted(curr_asks.items()))
        
        # Compute equilibrium (price with max total qty)
        all_levels = {}
        for p, q in curr_bids.items():
            all_levels[p] = all_levels.get(p, 0) + q
        for p, q in curr_asks.items():
            all_levels[p] = all_levels.get(p, 0) - q
        
        eq_price = max(all_levels.items(), key=lambda x: abs(x[1]))[0] if all_levels else 0.0
        eq_qty = all_levels.get(eq_price, 0)
        
        # Mid & Microprice
        bp1 = next(iter(curr_bids)) if curr_bids else 0.0
        ap1 = next(iter(curr_asks)) if curr_asks else 0.0
        bq1 = curr_bids.get(bp1, 0)
        aq1 = curr_asks.get(ap1, 0)
        mid = (bp1 + ap1) * 0.5 if bp1 > 0 and ap1 > 0 else 0.0
        micro = 0.0
        if (bq1 + aq1) > 0 and bp1 > 0 and ap1 > 0:
            b_val, s_val = bp1 * bq1, ap1 * aq1
            imb = b_val / max(b_val + s_val, 1.0)
            micro = (bp1 + ap1) * 0.5 + (ap1 - bp1) * (imb - 0.5)
        
        # Book Pressure
        book_pressure = (sum(p*q for p,q in curr_bids.items()) - sum(p*q for p,q in curr_asks.items())) / \
                        max(sum(p*q for p,q in curr_bids.items()) + sum(p*q for p,q in curr_asks.items()), 1.0)
        
        # LRA EMA (current state)
        lra_state = self.lra_ema[sym]
        lra_bid = tuple(lra_state['bid'])
        lra_ask = tuple(lra_state['ask'])
        
        # DKLA lambdas
        lam_buy, lam_sell, dkla_asym, _ = self._calculate_dkla(st, mid)
        
        self._baseline_truth[sym] = BaselineTruth(
            bids=bids_tuple,
            asks=asks_tuple,
            equilibrium_price=eq_price,
            equilibrium_qty=eq_qty,
            mid_price=mid,
            microprice=micro,
            book_pressure=book_pressure,
            lra_ema_bid=lra_bid,
            lra_ema_ask=lra_ask,
            dkla_lam_buy=lam_buy,
            dkla_lam_sell=lam_sell,
            dkla_asym=dkla_asym,
            captured_at_ns=nows
        )
        self._freeze_confirmed[sym] = True
        self._baseline_bucket_count[sym] = 0
        
        if logger.isEnabledFor(logging.INFO):
            logger.info(f"[BASELINE_CAPTURED] {sym} | Mid: {mid:.2f} | Micro: {micro:.2f} | "
                       f"EqPrice: {eq_price:.2f} | BookPress: {book_pressure:.3f} | "
                       f"DKLA_Asym: {dkla_asym:.3f} | LRA_Bid: {lra_bid[0]:.1f} LRA_Ask: {lra_ask[0]:.1f}")

    def _activate_continuous(self, sym: str, lp: float, curr_bids: Dict, curr_asks: Dict, nows: float) -> None:
        """Transition to CONTINUOUS_LIVE: compute deltas from baseline and seed adv cache."""
        baseline = self._baseline_truth[sym]
        if baseline is None:
            # Fallback: no baseline captured, use current as baseline
            self._capture_baseline(sym, self.last_snap[sym], curr_bids, curr_asks, nows)
            baseline = self._baseline_truth[sym]
        
        # Compute deltas from baseline
        bp1 = next(iter(curr_bids)) if curr_bids else 0.0
        ap1 = next(iter(curr_asks)) if curr_asks else 0.0
        bq1 = curr_bids.get(bp1, 0)
        aq1 = curr_asks.get(ap1, 0)
        
        # MPAR acceleration delta from baseline
        mp_accel_delta = self.mp_accel.get(sym, 0.0)  # Already computed in _update_mpar
        
        # Vacuum delta from baseline (baseline vacuum = 0 by definition)
        vac_delta = self.cur_net_vacuum.get(sym, 0.0)
        
        # LRA asymmetry delta from baseline
        lra_asym_delta = self.lra_asym_prev.get(sym, 0.0) - baseline.book_pressure
        
        # DKLA asymmetry delta from baseline
        dkla_asym_delta = self.dkla_asym_prev.get(sym, 0.0) - baseline.dkla_asym
        
        # Seed adv cache with deltas for first 3 buckets
        adv = self._adv_cache[sym]
        adv["mpar_accel"] = mp_accel_delta
        adv["vac_bps_tick"] = vac_delta
        adv["lra_asym"] = lra_asym_delta
        adv["dkla_asym"] = dkla_asym_delta
        adv["baseline_delta_active"] = True
        
        self._phase[sym] = EnginePhase.CONTINUOUS_LIVE
        self._baseline_bucket_count[sym] = 0
        
        if logger.isEnabledFor(logging.INFO):
            logger.info(f"[CONTINUOUS_OPEN] {sym} @ {lp:.2f} | MPAR_Accel_Delta: {mp_accel_delta:.4f} | "
                       f"Vac_Delta: {vac_delta:.1f}bps | LRA_Delta: {lra_asym_delta:.3f} | "
                       f"DKLA_Delta: {dkla_asym_delta:.3f}")

    def _route_tick_by_phase(self, sym: str, msg: Dict, st: Dict, curr_bids: Dict, curr_asks: Dict, 
                              lp: float, nows: float, dt: float) -> bool:
        """
        Route tick through pre-market state machine.
        Returns True if tick should proceed to normal hot-path processing.
        Returns False if tick should be absorbed/ignored (auction phases).
        """
        phase = self._phase[sym]
        secs = self._seconds_from_midnight(nows)
        
        # Phase debug logging for first 30 minutes (until 09:45)
        if secs <= 9 * 3600 + 45 * 60 and logger.isEnabledFor(logging.INFO):
            logger.info(f"[PHASE_DEBUG] {sym} | Phase: {phase.name} | SecsFromMidnight: {secs} | "
                       f"AuctionTicks: {self._auction_tick_count[sym]} | FreezeConfirmed: {self._freeze_confirmed[sym]} | "
                       f"BaselineBuckets: {self._baseline_bucket_count[sym]}")
        
        if phase == EnginePhase.AUCTION_ENTRY:
            # 09:00-09:04: Absorb L2 only, update snapshot, skip rolling buffers
            self._last_l2_snapshot[sym]['bids'].clear()
            self._last_l2_snapshot[sym]['bids'].update(curr_bids)
            self._last_l2_snapshot[sym]['asks'].clear()
            self._last_l2_snapshot[sym]['asks'].update(curr_asks)
            
            # Check for transition to AUCTION_FREEZE at 09:04 (auction_entry_end_ns)
            if secs >= self.CFG["auction_entry_end_ns"]:  # 09:04
                self._phase[sym] = EnginePhase.AUCTION_FREEZE
                self._auction_tick_count[sym] = 0
                if logger.isEnabledFor(logging.INFO):
                    logger.info(f"[PHASE_TRANSITION] {sym}: AUCTION_ENTRY -> AUCTION_FREEZE at {secs}s")
            return False  # Skip normal processing
        
        elif phase == EnginePhase.AUCTION_FREEZE:
            # 09:04-09:08: Detect freeze, capture baseline
            if self._detect_auction_freeze(sym, curr_bids, curr_asks):
                self._capture_baseline(sym, st, curr_bids, curr_asks, nows)
                self._phase[sym] = EnginePhase.AUCTION_BUFFER
                if logger.isEnabledFor(logging.INFO):
                    logger.info(f"[PHASE_TRANSITION] {sym}: AUCTION_FREEZE -> AUCTION_BUFFER (freeze confirmed)")
            # Timeout fallback: force transition at 09:08 even if freeze not detected
            elif secs >= self.CFG["auction_freeze_end_ns"]:  # 09:08
                self._capture_baseline(sym, st, curr_bids, curr_asks, nows)
                self._phase[sym] = EnginePhase.AUCTION_BUFFER
                if logger.isEnabledFor(logging.INFO):
                    logger.info(f"[PHASE_TRANSITION] {sym}: AUCTION_FREEZE -> AUCTION_BUFFER (timeout at 09:08)")
            return False  # Skip normal processing
        
        elif phase == EnginePhase.AUCTION_BUFFER:
            # 09:08-09:15: Wait for continuous open detection
            if self._detect_continuous_open(sym, msg, curr_bids, curr_asks):
                self._phase[sym] = EnginePhase.TRANSITION_DETECT
                if logger.isEnabledFor(logging.INFO):
                    logger.info(f"[PHASE_TRANSITION] {sym}: AUCTION_BUFFER -> TRANSITION_DETECT")
            # Timeout fallback: force transition at 09:15 even if continuous not detected
            elif secs >= self.CFG["continuous_open_ns"]:  # 09:15
                self._phase[sym] = EnginePhase.TRANSITION_DETECT
                if logger.isEnabledFor(logging.INFO):
                    logger.info(f"[PHASE_TRANSITION] {sym}: AUCTION_BUFFER -> TRANSITION_DETECT (timeout at 09:15)")
            return False  # Skip normal processing
        
        elif phase == EnginePhase.TRANSITION_DETECT:
            # First continuous tick: activate delta-baseline mode
            self._activate_continuous(sym, lp, curr_bids, curr_asks, nows)
            return True  # Proceed to normal processing (first continuous bucket)
        
        elif phase == EnginePhase.CONTINUOUS_LIVE:
            # Normal operation
            return True
        
        return False

    # ═══════════════════════════════════════════════════════════
    # MAIN TICK LOOP
    # ═══════════════════════════════════════════════════════════

    def on_tick(self, msg: Dict) -> None:
        try:
            tok = str(msg.get("token", ""))
            sym = self.t2s.get(tok)
            if not sym: return
            st = self.last_snap[sym]  # Pre-allocated, no setdefault
            lp = float(msg.get("last_traded_price") or msg.get("lp") or st.get("lp") or 0)
            if lp <= 0: return
            
            nows = pytime.time()
            dt = nows - st.get("last_ts", nows)
            st["last_ts"] = nows
            
            # Volume
            vol = float(msg.get("volume_trade_for_the_day") or msg.get("v") or st.get("vol", 0))
            dVol = max(0.0, vol - st.get("vol", vol))
            st["vol"] = vol; st["lp"] = lp
            if "bts" not in st: st["bts"] = nows
            exch_ts = float(msg.get("timestamp") or msg.get("exch_ts") or msg.get("tt") or nows)
            st["tick_latency"] = max(0.0, nows - exch_ts) if exch_ts > 1e9 else 0.0

            # Parse Depth (Unrolled) - Reuse pre-allocated dicts
            curr_bids = self._curr_bids[sym]
            curr_asks = self._curr_asks[sym]
            curr_bids.clear()
            curr_asks.clear()
            touch_bid = touch_ask = 0.0
            for i in range(1, 6):
                b_p = float(msg.get(f"bp{i}") or st.get(f"bp{i}") or 0)
                b_q = float(msg.get(f"bq{i}") or st.get(f"bq{i}") or 0)
                a_p = float(msg.get(f"sp{i}") or st.get(f"sp{i}") or 0)
                a_q = float(msg.get(f"sq{i}") or st.get(f"sq{i}") or 0)
                st[f"bp{i}"]=b_p; st[f"bq{i}"]=b_q; st[f"sp{i}"]=a_p; st[f"sq{i}"]=a_q
                if i==1: touch_bid, touch_ask = b_p, a_p
                if b_p>0: curr_bids[b_p]=int(b_q)
                if a_p>0: curr_asks[a_p]=int(a_q)

            # ─── PRE-MARKET STATE MACHINE ROUTING ───
            if not self._route_tick_by_phase(sym, msg, st, curr_bids, curr_asks, lp, nows, dt):
                return  # Absorbed by auction phase, skip normal processing

            trade_val = dVol * lp
            if trade_val > self.CFG["glitch_guard_cr"]*1_00_00_000.0: trade_val = self.CFG["glitch_guard_cr"]*1_00_00_000.0
            
            if trade_val > 0:
                bp1, sp1 = st.get("bp1",0.0), st.get("sp1",0.0)
                if bp1>0 and sp1>0:
                    if lp >= sp1: self.cur_buy_val[sym] += trade_val
                    elif lp <= bp1: self.cur_sell_val[sym] += trade_val
                    else:
                        self.cur_buy_val[sym] += trade_val*0.5
                        self.cur_sell_val[sym] += trade_val*0.5
                else:
                    self.cur_buy_val[sym] += trade_val*0.5
                    self.cur_sell_val[sym] += trade_val*0.5
                self.cur_tot_val[sym] += trade_val
                self.cur_tick_count[sym] += 1
                if trade_val > self.cur_max_tick_val[sym]:
                    self.cur_max_tick_val[sym] = trade_val
                self.trade_times[sym].append(nows)

            # ── PER-TICK ADVANCED METRICS ──
            # NOTE: LRA must run BEFORE vacuum, since vacuum overwrites self.last_depth_state[sym]
            # with the CURRENT snapshot, and LRA needs to compare against the PREVIOUS snapshot to
            # detect size adds. Reversing this order silently zeros LRA forever (oq==nq always).
            lra_asym, lra_vel = self._update_lra(sym, st, curr_bids, curr_asks, lp, dVol)
            vac_bps = self._calculate_vacuum_score(sym, curr_bids, curr_asks, dVol, lp, touch_bid, touch_ask) if dVol >= 0 else 0.0
            self.cur_net_vacuum[sym] += vac_bps
            
            # Queue Position Decay (Adverse Selection Signal)
            bid_queue_decay, ask_queue_decay = self._calculate_queue_decay(sym, st, curr_bids, curr_asks, dVol, lp)
            
            # Q3: Queue depletion velocity & spoofing detection
            qd_metrics = self._calculate_queue_depletion_metrics(sym, curr_bids, curr_asks, dVol, lp, dt)
            
            mid = (touch_bid + touch_ask)*0.5 if touch_bid>0 and touch_ask>0 else lp
            lam_buy, lam_sell, dkla_asym, eff_lam = self._calculate_dkla(st, mid)
            mp_accel, mp_align, mp_val = self._update_mpar(sym, st, dt)
            
            # Reuse adv cache dict (no allocation)
            adv = self._adv_cache[sym]
            adv["vac_bps_tick"] = vac_bps
            adv["lam_buy"] = lam_buy
            adv["lam_sell"] = lam_sell
            adv["dkla_asym"] = dkla_asym
            adv["eff_lam"] = eff_lam
            adv["lra_asym"] = lra_asym
            adv["lra_vel"] = lra_vel
            adv["mp_accel"] = mp_accel
            adv["mp_align"] = mp_align
            adv["mp_val"] = mp_val
            adv["bid_queue_decay"] = bid_queue_decay
            adv["ask_queue_decay"] = ask_queue_decay
            # Q3: Queue depletion & spoofing metrics
            adv["qds_bid"] = qd_metrics['qds_bid']
            adv["qds_ask"] = qd_metrics['qds_ask']
            adv["sds_bid"] = qd_metrics['sds_bid']
            adv["sds_ask"] = qd_metrics['sds_ask']
            adv["cv_bid"] = qd_metrics['cv_bid']
            adv["cv_ask"] = qd_metrics['cv_ask']
            st["_adv"] = adv

            # Q4: Push real-time adv metrics to execution manager for dynamic exits
            if hasattr(self, 'execution_manager') and self.execution_manager:
                self.execution_manager.update_adv_metrics(sym, adv)

            # Update last_depth_state in-place (no new dict allocation)
            lds = self.last_depth_state[sym]
            lds['bids'].clear()
            lds['bids'].update(curr_bids)
            lds['asks'].clear()
            lds['asks'].update(curr_asks)

            # Bucket Check
            if self.cur_tot_val[sym] >= self.bucket_map.get(sym, self.CFG["bucket_turnover"]):
                self._process_bucket(sym, lp, nows, dt)
        except Exception as e:
            logger.error(f"on_tick {sym}: {e}", exc_info=True)

    def on_tick_parsed(self, tick: "ParsedTick") -> None:
        """
        Zero-allocation hot path entry point.
        Accepts pre-parsed ParsedTick from TickCompiler, bypassing dict creation.
        """
        try:
            sym = self.t2s.get(tick.tok)
            if not sym: return
            st = self.last_snap[sym]  # Pre-allocated, no setdefault
            lp = tick.lp
            if lp <= 0: return
            
            nows = tick.ts
            dt = nows - st.get("last_ts", nows)
            st["last_ts"] = nows
            
            # Volume
            vol = tick.vol
            dVol = max(0.0, vol - st.get("vol", vol))
            st["vol"] = vol; st["lp"] = lp
            if "bts" not in st: st["bts"] = nows
            # Note: tick_latency not available in zero-copy path (no exch_ts)
            
            # Parse Depth from pre-filled arrays (Unrolled) - Reuse pre-allocated dicts
            curr_bids = self._curr_bids[sym]
            curr_asks = self._curr_asks[sym]
            curr_bids.clear()
            curr_asks.clear()
            touch_bid = touch_ask = 0.0
            for i in range(5):
                b_p = tick.bp[i]
                b_q = tick.bq[i]
                a_p = tick.sp[i]
                a_q = tick.sq[i]
                st[f"bp{i+1}"]=b_p; st[f"bq{i+1}"]=b_q; st[f"sp{i+1}"]=a_p; st[f"sq{i+1}"]=a_q
                if i==0: touch_bid, touch_ask = b_p, a_p
                if b_p>0: curr_bids[b_p]=int(b_q)
                if a_p>0: curr_asks[a_p]=int(a_q)

            # ─── PRE-MARKET STATE MACHINE ROUTING ───
            # Create a minimal msg dict for phase routing
            msg = {"last_traded_price": lp}
            if not self._route_tick_by_phase(sym, msg, st, curr_bids, curr_asks, lp, nows, dt):
                return  # Absorbed by auction phase, skip normal processing

            trade_val = dVol * lp
            if trade_val > self.CFG["glitch_guard_cr"]*1_00_00_000.0: trade_val = self.CFG["glitch_guard_cr"]*1_00_00_000.0
            
            if trade_val > 0:
                bp1, sp1 = st.get("bp1",0.0), st.get("sp1",0.0)
                if bp1>0 and sp1>0:
                    if lp >= sp1: self.cur_buy_val[sym] += trade_val
                    elif lp <= bp1: self.cur_sell_val[sym] += trade_val
                    else:
                        self.cur_buy_val[sym] += trade_val*0.5
                        self.cur_sell_val[sym] += trade_val*0.5
                else:
                    self.cur_buy_val[sym] += trade_val*0.5
                    self.cur_sell_val[sym] += trade_val*0.5
                self.cur_tot_val[sym] += trade_val
                self.cur_tick_count[sym] += 1
                if trade_val > self.cur_max_tick_val[sym]:
                    self.cur_max_tick_val[sym] = trade_val
                self.trade_times[sym].append(nows)

            # ── PER-TICK ADVANCED METRICS ──
            # NOTE: LRA must run BEFORE vacuum, since vacuum overwrites self.last_depth_state[sym]
            # with the CURRENT snapshot, and LRA needs to compare against the PREVIOUS snapshot to
            # detect size adds. Reversing this order silently zeros LRA forever (oq==nq always).
            lra_asym, lra_vel = self._update_lra(sym, st, curr_bids, curr_asks, lp, dVol)
            vac_bps = self._calculate_vacuum_score(sym, curr_bids, curr_asks, dVol, lp, touch_bid, touch_ask) if dVol >= 0 else 0.0
            self.cur_net_vacuum[sym] += vac_bps
            
            # Queue Position Decay (Adverse Selection Signal)
            bid_queue_decay, ask_queue_decay = self._calculate_queue_decay(sym, st, curr_bids, curr_asks, dVol, lp)
            
            # Q3: Queue depletion velocity & spoofing detection
            qd_metrics = self._calculate_queue_depletion_metrics(sym, curr_bids, curr_asks, dVol, lp, dt)
            
            mid = (touch_bid + touch_ask)*0.5 if touch_bid>0 and touch_ask>0 else lp
            lam_buy, lam_sell, dkla_asym, eff_lam = self._calculate_dkla(st, mid)
            mp_accel, mp_align, mp_val = self._update_mpar(sym, st, dt)
            
            # Reuse adv cache dict (no allocation)
            adv = self._adv_cache[sym]
            adv["vac_bps_tick"] = vac_bps
            adv["lam_buy"] = lam_buy
            adv["lam_sell"] = lam_sell
            adv["dkla_asym"] = dkla_asym
            adv["eff_lam"] = eff_lam
            adv["lra_asym"] = lra_asym
            adv["lra_vel"] = lra_vel
            adv["mp_accel"] = mp_accel
            adv["mp_align"] = mp_align
            adv["mp_val"] = mp_val
            adv["bid_queue_decay"] = bid_queue_decay
            adv["ask_queue_decay"] = ask_queue_decay
            # Q3: Queue depletion & spoofing metrics
            adv["qds_bid"] = qd_metrics['qds_bid']
            adv["qds_ask"] = qd_metrics['qds_ask']
            adv["sds_bid"] = qd_metrics['sds_bid']
            adv["sds_ask"] = qd_metrics['sds_ask']
            adv["cv_bid"] = qd_metrics['cv_bid']
            adv["cv_ask"] = qd_metrics['cv_ask']
            # P1 FIX: Real-time TPIN for tick-level toxicity computation in exit brain
            target_bucket = self.bucket_map.get(sym, self.CFG["bucket_turnover"])
            bv = self.cur_buy_val.get(sym, 0.0)
            sv = self.cur_sell_val.get(sym, 0.0)
            adv["tpin"] = (bv - sv) / max(target_bucket, 1.0)
            st["_adv"] = adv

            # Q4: Push real-time adv metrics to execution manager for dynamic exits
            if hasattr(self, 'execution_manager') and self.execution_manager:
                self.execution_manager.update_adv_metrics(sym, adv)

            # Update last_depth_state in-place (no new dict allocation)
            lds = self.last_depth_state[sym]
            lds['bids'].clear()
            lds['bids'].update(curr_bids)
            lds['asks'].clear()
            lds['asks'].update(curr_asks)

            # Bucket Check
            if self.cur_tot_val[sym] >= self.bucket_map.get(sym, self.CFG["bucket_turnover"]):
                self._process_bucket(sym, lp, nows, dt)
        except Exception as e:
            logger.error(f"on_tick_parsed {sym}: {e}", exc_info=True)

    # ═══════════════════════════════════════════════════════════
    # BUCKET PROCESSING & UNIFIED CONVICTION TRIGGER
    # ═══════════════════════════════════════════════════════════

    def _process_bucket(self, sym: str, lp: float, nows: float, dt: float) -> None:
        st = self.last_snap.get(sym, {})
        adv = st.get("_adv", {})
        target_bucket = self.bucket_map.get(sym, self.CFG["bucket_turnover"])
        
        # ── Core Calculations ──
        bv = self.cur_buy_val.get(sym, 0.0); sv = self.cur_sell_val.get(sym, 0.0); tv = self.cur_tot_val.get(sym, 0.0)
        tpin = (bv - sv) / max(target_bucket, 1.0)
        self.vpin_q.setdefault(sym, deque(maxlen=20)).append(abs(tpin))
        avg_tpin = sum(self.vpin_q[sym]) / len(self.vpin_q[sym]) if self.vpin_q[sym] else 0.0
        
        # Hawkes - Reuse pre-allocated array (zero allocation)
        branching = 0.0
        if sym in self.trade_times:
            times = self.trade_times[sym]; cutoff = nows - self.CFG["hawkes_lookback_sec"]
            while times and times[0] < cutoff: times.popleft()
            # P0 FIX: Robust Hawkes guard for sparse/lull periods
            # Require minimum 30 ticks in lookback window AND variance epsilon
            # to prevent unstable branching estimates during low-activity periods
            min_ticks_for_hawkes = 30
            if len(times) >= min_ticks_for_hawkes:
                counts = self._hawkes_counts
                counts.fill(0)
                for t in times:
                    bi = int((t - cutoff) // self.CFG["hawkes_bin_sec"])
                    if 0 <= bi < self._hawkes_bins: counts[bi] += 1
                mc = sum(counts) / self._hawkes_bins
                vc = sum((c - mc) ** 2 for c in counts) / self._hawkes_bins
                # Variance epsilon guard: vc must be > 1e-6 to avoid division by near-zero
                if vc > 1e-6 and mc > 0: 
                    branching = max(0.0, 1.0 - math.sqrt(mc / vc))
                else:
                    branching = 0.0  # Fallback: no self-excitation signal
            else:
                branching = 0.0  # Fallback: insufficient data for reliable estimate
        # Store branching in adv for monkey-patch access (main.py wiring)
        adv["branching"] = branching
        
        # Microprice Dev - Guard division by zero
        bp1, bq1, sp1, sq1 = st.get("bp1",0), st.get("bq1",0), st.get("sp1",0), st.get("sq1",0)
        micro_dev = 0.0
        if (bq1+sq1)>0 and bp1>0 and sp1>0:
            b_val, s_val = bp1*bq1, sp1*sq1
            imb = b_val / max(b_val+s_val,1.0)
            mp = (bp1+sp1)*0.5 + (sp1-bp1)*(imb-0.5)
            if mp != 0.0:
                micro_dev = (lp - mp) / mp
        
        # MLOFI (Fixed)
        mlofi = self._calculate_mlofi(sym, st, tpin)
        
        # Spread/OIMB
        spread = max(0.0, sp1-bp1) if sp1>0 and bp1>0 else 0.0
        b_tot = sum(st.get(f"bq{i}",0) for i in range(1,6))
        s_tot = sum(st.get(f"sq{i}",0) for i in range(1,6))
        oimb = float(b_tot/s_tot) if s_tot>0 else (999.0 if b_tot>0 else 1.0)
        
        # Whale
        tc = max(1, self.cur_tick_count.get(sym,1))
        whale_conc = self.cur_max_tick_val.get(sym,0.0)/max(tv,1.0)
        whale_z = self._calculate_whale_z(sym, whale_conc)
        net_vac = self.cur_net_vacuum.get(sym, 0.0)
        
        # ── ADVANCED AGGREGATES ──
        spread_norm = spread / max(lp, 1e-6)
        
        # ─── DELTA-BASELINE MATH (First 3 buckets after continuous open) ───
        phase = self._phase[sym]
        baseline_delta_active = adv.get("baseline_delta_active", False)
        if baseline_delta_active and self._baseline_bucket_count[sym] < self.CFG["baseline_delta_buckets"]:
            # Use pre-seeded deltas from _activate_continuous
            mp_accel_norm = adv.get("mpar_accel", 0.0) / max(spread_norm * math.sqrt(max(dt,1e-6)), 1e-6)
            net_vac = adv.get("vac_bps_tick", 0.0)  # Delta from baseline
            lra_asym = adv.get("lra_asym", 0.0)     # Delta from baseline
            dkla_asym = adv.get("dkla_asym", 0.0)   # Delta from baseline
            self._baseline_bucket_count[sym] += 1
            if self._baseline_bucket_count[sym] >= self.CFG["baseline_delta_buckets"]:
                adv["baseline_delta_active"] = False
                if logger.isEnabledFor(logging.INFO):
                    logger.info(f"[BASELINE_DELTA_COMPLETE] {sym} | Reverted to absolute math after {self.CFG['baseline_delta_buckets']} buckets")
        else:
            # Normal absolute math
            mp_accel_norm = adv.get("mp_accel",0.0) / max(spread_norm * math.sqrt(max(dt,1e-6)), 1e-6)
        
        divergence = mp_accel_norm - (tpin * spread_norm * 100)
        mpar_score = adv.get("mp_align",0.0) * divergence * abs(mlofi)
        
        # ═══════════════════════════════════════════════
        # UNIFIED S_FLOW IMPULSE ENGINE (replaces dual-gate + 20-term polynomial)
        # ═══════════════════════════════════════════════
        
        # --- REGIME CLASSIFICATION (telemetry only, no branching) ---
        sym_idx = self.sym_to_idx[sym]
        mom_5m, mom_1m = self._compute_5m_momentum(sym, lp, nows)
        self._update_regime_state(sym_idx, branching, whale_z, mom_5m)
        regime = self.regime_id[sym_idx]
        
        # --- TOXICITY TRACKING (for adverse selection) ---
        toxicity = adv.get("eff_lam",0.0) * abs(tpin)
        tox_hist = self.toxicity_history[sym]  # Pre-allocated, no setdefault
        tox_hist.append(toxicity)
        if len(tox_hist) >= 20:
            s_tox = sorted(tox_hist)
            tox_p90 = s_tox[int(0.9 * (len(s_tox) - 1))]
        else:
            tox_p90 = toxicity
        
        # --- LRA SIGNAL (directional, TPIN-gated) ---
        lra_asym = adv.get("lra_asym", 0.0)
        lra_vel = adv.get("lra_vel", 0.0)
        lra_signal = lra_asym if tpin > 0.1 else (-lra_asym if tpin < -0.1 else 0.0)
        dkla_asym = adv.get("dkla_asym", 0.0)
        
        # --- Q1: MULTI-HORIZON MOMENTUM ALIGNMENT (M_align) ---
        # Compute continuous alignment factor for both directions
        m_align_long = self._compute_multi_horizon_alignment(sym, lp, nows, "BUY")
        m_align_short = self._compute_multi_horizon_alignment(sym, lp, nows, "SELL")
        
        # --- UNIFIED S_FLOW COMPUTATION (single call, both directions) ---
        s_flow_long, s_flow_short = self._compute_unified_s_flow(
            mpar_accel=mpar_score,
            net_vac=net_vac,
            whale_z=whale_z,
            mom_1m=mom_1m,
            mlofi=mlofi,
            lra_signal=lra_signal,
            dkla_asym=dkla_asym,
            micro_dev=micro_dev,
            branching=branching,
            regime=regime
        )
        
        # Apply M_align modulation to S_flow (centered at 1.0 for neutral M_align=0.50)
        # Neutral: M_align=0.50 -> multiplier=1.0
        # Aligned: M_align=0.80 -> multiplier=1.3
        # Counter-trend: M_align=0.15 -> multiplier=0.65 (discounted)
        s_flow_long *= (0.5 + m_align_long)
        s_flow_short *= (0.5 + m_align_short)
        
        # Track S_flow history for SVI
        if sym in self.s_flow_history:
            self.s_flow_history[sym].append(float(max(s_flow_long, s_flow_short)))
        
        # --- Q2: DYNAMIC S_FLOW THRESHOLD CALIBRATION ---
        # Replace static 0.45 with regime-aware dynamic threshold
        dynamic_threshold = self._compute_dynamic_sflow_threshold(sym, regime, nows)
        
        # --- SINGLE DECISION RULE (S_flow >= dynamic_threshold, Soft M_align gate) ---
        signal_side = None
        conviction = 0.0
        
        # Dynamic threshold calibrated for 0.40%-0.60% spike capture
        # M_align soft gate >= 0.35 ensures we don't catch severe counter-trend falling knives
        if s_flow_long >= dynamic_threshold and m_align_long >= 0.35 and mom_1m >= 0.0:
            signal_side = "BUY"
            conviction = s_flow_long * 10.0  # Scale to 0-10 for compatibility
        elif s_flow_short >= dynamic_threshold and m_align_short >= 0.35 and mom_1m <= 0.0:
            signal_side = "SELL"
            conviction = s_flow_short * 10.0
        
        # --- Q3: EXTRACT QUEUE DEPLETION & SPOOFING METRICS (Always in scope) ---
        bid_queue_decay = adv.get("bid_queue_decay", 0.0)
        ask_queue_decay = adv.get("ask_queue_decay", 0.0)
        spread_bps = spread / max(lp, 1e-6) * 10000
        qds_bid = adv.get("qds_bid", 0.0)
        qds_ask = adv.get("qds_ask", 0.0)
        sds_bid = adv.get("sds_bid", 0.0)
        sds_ask = adv.get("sds_ask", 0.0)
        cv_bid = adv.get("cv_bid", 0.0)
        cv_ask = adv.get("cv_ask", 0.0)
        adv_sel_prob = 0.0
        
        # --- Q3: ENHANCED ADVERSE SELECTION FILTER (with queue depletion & spoofing) ---
        if signal_side:
            adv_sel_prob = self._estimate_adverse_selection_enhanced(
                sym=sym,
                side=signal_side,
                vpins=avg_tpin,
                oimb=oimb,
                spread_bps=spread_bps,
                toxicity=toxicity,
                bid_queue_decay=bid_queue_decay,
                ask_queue_decay=ask_queue_decay,
                whale_z=whale_z,
                branching=branching,
                qds_bid=qds_bid,
                qds_ask=qds_ask,
                sds_bid=sds_bid,
                sds_ask=sds_ask,
                cv_bid=cv_bid,
                cv_ask=cv_ask
            )
            
            # Log adverse selection probability
            if logger.isEnabledFor(logging.INFO):
                logger.info(
                    "[ADV_SEL] %s | Side: %s | P(toxic): %.3f | VPIN: %.3f | OIB: %.2f | "
                    "Spread: %.1fbps | Tox: %.1f | QueueDecay: B=%.1f A=%.1f | WhaleZ: %.2f | Branch: %.3f | "
                    "QDS: B=%.3f A=%.3f | SDS: B=%.3f A=%.3f | CV: B=%.3f A=%.3f"
                    % (sym, signal_side, adv_sel_prob, avg_tpin, oimb, spread_bps, toxicity,
                       bid_queue_decay, ask_queue_decay, whale_z, branching,
                       qds_bid, qds_ask, sds_bid, sds_ask, cv_bid, cv_ask)
                )
            
            # Veto if adverse selection probability too high
            if adv_sel_prob > 0.60:
                if logger.isEnabledFor(logging.INFO):
                    logger.info(f"[ADV_SEL_VETO] {sym} {signal_side} vetoed | P(toxic)={adv_sel_prob:.3f} > 0.60")
                signal_side = None
                conviction = 0.0
        
        # --- KELLY SIZING (0.05 to 0.25 bounded) ---
        if signal_side:
            kelly_fraction = self._calculate_kelly_fraction(sym, signal_side, conviction, None, regime)
            kelly_fraction = max(0.05, min(kelly_fraction, 0.25))
            
            if logger.isEnabledFor(logging.INFO):
                logger.info(
                    "[KELLY_SIZING] %s | Side: %s | Conviction: %.2f | "
                    "Kelly_Fraction: %.3f | Max_Position_Pct: %.1f%%"
                    % (sym, signal_side, conviction, kelly_fraction, kelly_fraction * 100)
                )
        
        # --- UNIFIED TELEMETRY (replaces ADAPTIVE_GATE + CONVICTION + CONVICTION_DETAIL) ---
        if logger.isEnabledFor(logging.INFO):
            logger.info(
                "[S_FLOW] %s | Phase: %s | Regime: %s (conf=%.1f) | "
                "S_Flow_L: %.3f S_Flow_S: %.3f | "
                "M_Align_L: %.3f M_Align_S: %.3f | Dyn_Thresh: %.3f | "
                "Ticks: %d | Branching: %.3f | WhaleZ: %.2f | "
                "Mom1m: %+.2f%% Mom5m: %+.2f%% | Toxicity: %.1f | "
                "Signal: %s | Conviction: %.2f | AdvSel: %.3f"
                % (sym, phase.name, self._regime_names[regime], self.regime_conf[sym_idx],
                   s_flow_long, s_flow_short,
                   m_align_long, m_align_short, dynamic_threshold,
                   tc, branching, whale_z,
                   mom_1m, mom_5m, toxicity,
                   signal_side or 'NONE', conviction, adv_sel_prob if signal_side else 0.0)
            )
        
        # ── COMPREHENSIVE LOG ENTRY ──
        log_entry = {
            "ts": datetime.now().isoformat(), "sym": sym, "lp": round(lp,2), "bucket_val": round(tv,0),
            "duration_sec": round(nows - st.get("bts", nows), 2), "tick_count": tc,
            # Core
            "tpin": round(tpin,5), "tpin_avg": round(avg_tpin,5), "branching": round(branching,5),
            "mlofi_z": round(mlofi,5), "spread": round(spread,2), "oimb": round(oimb,3),
            "micro_dev": round(micro_dev,6), "whale_conc": round(whale_conc,5), "whale_z": round(whale_z,5),
            "net_vacuum_bps": round(net_vac,2), "tick_latency": round(st.get("tick_latency",0),5),
            # Advanced Raw
            "mpar_score": round(mpar_score,5), "mpar_alignment": round(adv.get("mp_align",0),3),
            "lra_asymmetry": round(lra_asym,5), "lra_velocity": round(lra_vel,5), "lra_signal": round(lra_signal,5),
            "dkla_lambda_buy_bps": round(adv.get("lam_buy",0),2), "dkla_lambda_sell_bps": round(adv.get("lam_sell",0),2),
            "dkla_asymmetry": round(dkla_asym,5), "dkla_toxicity": round(toxicity,2), "dkla_tox_p90": round(tox_p90,2),
            # Q1: Multi-horizon alignment
            "m_align_long": round(m_align_long, 3), "m_align_short": round(m_align_short, 3),
            # Q2: Dynamic threshold
            "dynamic_threshold": round(dynamic_threshold, 3),
            # Q3: Queue depletion & spoofing
            "qds_bid": round(qds_bid, 3), "qds_ask": round(qds_ask, 3),
            "sds_bid": round(sds_bid, 3), "sds_ask": round(sds_ask, 3),
            "cv_bid": round(cv_bid, 3), "cv_ask": round(cv_ask, 3),
            # Unified S_Flow Diagnostics
            "s_flow_long": round(s_flow_long, 3), "s_flow_short": round(s_flow_short, 3),
            "signal_generated": signal_side, "conviction_score": round(conviction, 2)
        }
        
        # Async Log
        try:
            with open(self.LOG_FILE, "a") as f: f.write(json.dumps(log_entry, default=lambda o: bool(o) if isinstance(o, np.bool_) else float(o) if isinstance(o, np.floating) else int(o) if isinstance(o, np.integer) else str(o)) + "\n")
        except IOError: pass
        
        # ── PUSH ACTIONABLE SIGNAL ──
        if signal_side:
            # Calculate Kelly fraction for position sizing (bounded 5% to 25%)
            kelly_fraction = self._calculate_kelly_fraction(sym, signal_side, conviction, None, regime)
            kelly_fraction = max(0.05, min(kelly_fraction, 0.25))
            
            sig_payload = {
                "type": "ENTRY_SIGNAL",
                "sym": sym, "signal": signal_side, "lp": lp,
                "conviction": round(conviction, 2),
                "kelly_fraction": round(kelly_fraction, 3),
                "reason": f"Unified_Conviction_v3",
                "metrics": log_entry
            }
            self.signal_queue.put(sig_payload)
            logger.info(f"SIGNAL {signal_side} {sym} @ {lp:.2f} | Conv: {conviction:.2f} | Kelly: {kelly_fraction:.3f} | Branching: {branching:.2f} WhaleZ: {whale_z:.2f}")
            
            # Q4: Push bucket aggregates to execution manager for dynamic exits
            # Include S_flow history for SVI computation
            s_flow_hist = self.bucket_aggregates.get(sym, {}).get("s_flow_history", [])
            s_flow_hist.append(s_flow_long if signal_side == "BUY" else s_flow_short)
            if len(s_flow_hist) > 50:
                s_flow_hist = s_flow_hist[-50:]
            
            bucket_aggregates = {
                "net_vacuum_bps": net_vac,
                "toxicity_history": list(self.toxicity_history[sym]),
                "branching": branching,
                "whale_z": whale_z,
                "dkla_toxicity": toxicity,
                "s_flow_history": s_flow_hist
            }
            self.bucket_aggregates[sym] = bucket_aggregates
            
            # Notify execution manager
            if hasattr(self, 'execution_manager') and self.execution_manager:
                self.execution_manager.update_bucket_aggregates(sym, bucket_aggregates)
            
            # ULTRA-DETAILED TELEMETRY: Log entry decision with full conviction metrics
            # tox_p90 already computed above
            if logger.isEnabledFor(logging.INFO):
                logger.info(
                    "[ENTRY_DECISION] %s | Side: %s | LP: %.2f | Conviction: %.2f | S_Flow: %.3f | "
                    "M_Align: %.3f | Dyn_Thresh: %.3f | "
                    "TPIN: %.5f | Branching: %.3f | WhaleZ: %.2f | "
                    "Toxicity: %.1f(P90:%.1f) | NetVac: %.1fbps | "
                    "MPAR: %.3f(align:%.1f) | LRA: %.3f | DKLA: %.3f | "
                    "MicroDev: %.6f | QueueDecay: Bid=%.1f Ask=%.1fbps | "
                    "QDS: Bid=%.3f Ask=%.3f | SDS: Bid=%.3f Ask=%.3f | CV: Bid=%.3f Ask=%.3f"
                    % (sym, signal_side, lp, conviction, s_flow_long if signal_side == "BUY" else s_flow_short,
                       m_align_long if signal_side == "BUY" else m_align_short, dynamic_threshold,
                       tpin, branching, whale_z,
                       toxicity, tox_p90, net_vac,
                       mpar_score, adv.get('mp_align',0), lra_signal, dkla_asym,
                       micro_dev, adv.get('bid_queue_decay',0), adv.get('ask_queue_decay',0),
                       qds_bid, qds_ask, sds_bid, sds_ask, cv_bid, cv_ask)
                )
        
        # ── BAYESIAN POSTERIOR UPDATE & BUCKET COUNT (for adaptive thresholds) ──
        # Track whether a signal was generated and whether it would have captured a move
        signal_fired = signal_side is not None
        self.bucket_count[sym_idx] += 1
        
        # For posterior update, we need to know if a move happened in the next 15 min.
        # We approximate by checking if price moved >0.52% from bucket close to next bucket open.
        # This is a simplified proxy - full forward-check requires storing bucket-close prices.
        move_captured = False
        if signal_fired:
            # Store bucket close price for forward check on next bucket
            self._bucket_close_prices[sym] = lp
        elif sym in self._bucket_close_prices:
            # Check if previous signal would have captured a move
            prev_close = self._bucket_close_prices[sym]
            if prev_close > 0:
                move_pct = abs(lp - prev_close) / prev_close * 100.0
                move_captured = move_pct >= 0.52
                # Update posteriors
                self._update_threshold_posteriors(sym_idx, True, move_captured)
                # Track FP/TP for dynamic conviction threshold
                if move_captured:
                    self.recent_tp[sym].append(1)
                    self.recent_fp[sym].append(0)
                else:
                    self.recent_tp[sym].append(0)
                    self.recent_fp[sym].append(1)
            del self._bucket_close_prices[sym]
        
        # ── RESET BUCKET ──
        # Preserve absolute overflow amount, not ratio
        overflow_val = max(0.0, tv - target_bucket)
        if overflow_val > 0:
            # Distribute overflow proportionally to buy/sell
            buy_ratio = bv / max(tv, 1.0)
            sell_ratio = sv / max(tv, 1.0)
            self.cur_buy_val[sym] = overflow_val * buy_ratio
            self.cur_sell_val[sym] = overflow_val * sell_ratio
            self.cur_tot_val[sym] = overflow_val
        else:
            self.cur_buy_val[sym] = 0.0
            self.cur_sell_val[sym] = 0.0
            self.cur_tot_val[sym] = 0.0
        self.cur_tick_count[sym] = 0
        self.cur_max_tick_val[sym] = 0.0
        self.cur_net_vacuum[sym] = 0.0
        st["bts"] = nows
        self.completion_times.setdefault(sym, deque(maxlen=1000)).append(nows)

    # ═══════════════════════════════════════════════════════════
    # MONITORING
    # ═══════════════════════════════════════════════════════════
    def get_market_state_summary(self) -> str:
        if not self.cur_net_vacuum: return "scanning..."
        try:
            hottest = max(self.cur_net_vacuum.keys(), key=lambda s: abs(self.cur_net_vacuum.get(s,0)))
            st = self.last_snap.get(hottest, {}); adv = st.get("_adv", {})
            return (f"{hottest} Bkt:{self.cur_tot_val.get(hottest,0)/self.bucket_map.get(hottest,1)*100:.0f}% "
                    f"Vac:{self.cur_net_vacuum.get(hottest,0):+.0f}bps | "
                    f"MPAR:{adv.get('mp_align',0):+.1f}*{adv.get('mp_accel',0):+.2f} "
                    f"LRA:{adv.get('lra_signal',0):+.2f} "
                    f"DKLA:{adv.get('dkla_asym',0):+.2f} "
                    f"WhaleZ:{self._calculate_whale_z(hottest, self.cur_max_tick_val.get(hottest,0)/max(self.cur_tot_val.get(hottest,1),1)):.1f}")
        except Exception: return "error"

    # ─── GLOBAL TRADE LOCK MANAGEMENT ───
    def set_active_trade(self, sym: str) -> None:
        """Called by execution manager when a position is confirmed open."""
        self._engine_active_trade = True
        self._active_trade_symbol = sym
        if logger.isEnabledFor(logging.INFO):
            logger.info(f"[TRADE_LOCK_ENGAGED] Engine locked on active trade: {sym}")

    def handle_trade_closed(self, sym: str) -> None:
        """
        Release global 1-trade lock when a trade exits or is cancelled.
        Called by execution_manager on TRADE_CLOSED event.
        """
        self._engine_active_trade = False
        self._active_trade_symbol = None
        if logger.isEnabledFor(logging.INFO):
            logger.info(f"[TRADE_LOCK_RELEASED] {sym} | Lock released. Ready for new signals.")

def attach_cascade_engine(broker, token_to_symbol, bucket_map=None, vacuum_map=None, 
                          dkla_notional=1_00_00_000.0, trigger_config=None, stock_calibration=None):
    engine = CascadeValueEngine(token_to_symbol, bucket_map, vacuum_map, 
                                dkla_notional=dkla_notional, trigger_config=trigger_config,
                                stock_calibration=stock_calibration)
    candidates = [broker]
    for attr in ("market_data","mdp","market_data_provider","_mdp","_api","api","session_provider"):
        obj = getattr(broker, attr, None)
        if obj: candidates.append(obj)
    for c in candidates:
        if hasattr(c, "add_tick_listener"):
            c.add_tick_listener(engine.on_tick)
            logger.info("Cascade Engine LIVE Attached (Unified Conviction Trigger Active)")
            return engine
    logger.error("No add_tick_listener found"); return engine