# aegis_gamma_engine.py
# AEGIS V2.0 — Empirical Microstructure Impulse Engine
# Reverse-engineered from 812 real >=10% clean option impulse moves (NIFTY & SENSEX)
# Zero-allocation hot path, O(1) continuous confidence scoring (0 to 100).

from __future__ import annotations
import os
import queue
import sys
import json
import math
import time
import logging
from collections import deque
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Any

import numpy as np

# ──────────────────────────────────────────────────────────────
# BOOTSTRAP & LOGGER
# ──────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from common.logger import get_logger
from common.config import get_data_dir

try:
    from AEGIS.core.price_action_engine import PriceActionEngine
    from AEGIS.core.microstructure_scorer import MicrostructureScorer
    from AEGIS.core.regime_chop_gate import RegimeChopGate
    from AEGIS.core.time_bar_engine import TimeBarEngine
    from AEGIS.core.directional_persistence import DirectionalPersistenceEngine
    from AEGIS.core.option_quality import OptionQualityEngine
    from AEGIS.core.signal_state_machine import SignalStateMachine, SignalState
    from AEGIS.core.outcome_telemetry import OutcomeTelemetryEngine
    from AEGIS.core.entry_quality_engine import EntryQualityEngine
    from AEGIS.core.oi_builtup_gate import OIBuiltupGate
    from AEGIS.core.index_vwap_engine import IndexVWAPEngine
except ImportError:
    try:
        from core.price_action_engine import PriceActionEngine
        from core.microstructure_scorer import MicrostructureScorer
        from core.regime_chop_gate import RegimeChopGate
        from core.time_bar_engine import TimeBarEngine
        from core.directional_persistence import DirectionalPersistenceEngine
        from core.option_quality import OptionQualityEngine
        from core.signal_state_machine import SignalStateMachine, SignalState
        from core.outcome_telemetry import OutcomeTelemetryEngine
        from core.entry_quality_engine import EntryQualityEngine
        from core.oi_builtup_gate import OIBuiltupGate
        from core.index_vwap_engine import IndexVWAPEngine
    except ImportError:
        PriceActionEngine = None
        MicrostructureScorer = None
        RegimeChopGate = None
        TimeBarEngine = None
        DirectionalPersistenceEngine = None
        OptionQualityEngine = None
        SignalStateMachine = None
        OutcomeTelemetryEngine = None
        EntryQualityEngine = None
        OIBuiltupGate = None
        IndexVWAPEngine = None

logger = get_logger("aegis_gamma")

# ──────────────────────────────────────────────────────────────
# CONSTANTS & CONFIGURATION
# ──────────────────────────────────────────────────────────────
N_STRIKES = 21          # ATM ±10
ATM_IDX   = N_STRIKES // 2

# Spot-feed hygiene (verified 2026-09-09: a 7.40 print on 75k SENSEX corrupted
# the 1m bar low + ATR). A single index print this far from the last good tick
# is a feed artifact, never a real move — drop it before it pollutes bars/ATR.
MAX_SINGLE_TICK_JUMP_FRAC = 0.005  # 0.5% (~375 pts SENSEX, ~120 pts NIFTY)
SPOT_RESYNC_GAP_SEC = 60.0         # after a feed gap this long, accept next tick unconditionally

PARAM_DIR  = os.path.join(get_data_dir(), "params")
PARAM_FILE = os.path.join(PARAM_DIR, "microstructure_weights.json")

# ──────────────────────────────────────────────────────────────
# AEGIS ENGINE CLASS (PURE EMPIRICAL MICROSTRUCTURE)
# ──────────────────────────────────────────────────────────────
class AegisGammaEngine:
    """
    AEGIS Empirical Microstructure Engine.
    Detects institutional quote acceleration, deep order book vacuums, and cross-strike
    gamma coherence to trigger high-conviction >=10% option impulse trades.
    """

    def __init__(
        self,
        token_to_symbol: Dict[str, str],
        token_map: Dict[str, Dict],          # token -> {'side':0/1, 'strike':float, 'expiry':datetime}
        bucket_map: Dict[str, float] = None,
        strike_step: float = 100.0,
        underlying_tokens: set = None,
        signal_queue: Any = None,            # queue.Queue for AegisExecutionManager
        log_path: str = None,
    ):
        self.t2s = {str(k): str(v) for k, v in token_to_symbol.items()}
        self.token_map = {str(k): v for k, v in token_map.items()}
        self.bucket_map = bucket_map or {}
        self.strike_step = strike_step
        self.underlying_tokens = underlying_tokens or {"26000", "1", "99919000", "99926000"}
        self.signal_queue = signal_queue

        # Build fast (side, strike) -> symbol lookup for cross-strike coherence
        self.strike_to_symbol: Dict[Tuple[int, float], str] = {}
        self.tick_ring: Dict[str, deque] = {}  # rolling 30.0s tick arrival timestamps
        self.history_30s: Dict[str, deque] = {} # rolling 30.0s (ts, lp, bp1, sp1, b1, a1)
        # Rebuild is defined below as _rebuild_strike_to_symbol(); warm tick_ring after t2s known

        # ─── Load or default parameters ───
        self.P = self._load_or_default_params()
        self.high_conviction_threshold = float(self.P.get("high_conviction_threshold", 75.0))

        # ─── Empirical Microstructure Scorer ───
        weights_file = os.path.join(PARAM_DIR, "microstructure_weights.json")
        if MicrostructureScorer is not None:
            self.scorer = MicrostructureScorer(weights_path=weights_file, high_conviction_threshold=self.high_conviction_threshold)
        else:
            self.scorer = None

        # Decoupled legacy chop gate: wiring cut per user instruction
        self.chop_gate = None

        # State Tracking
        self.last_spot = 0.0
        self._last_spot_ts = 0.0  # ts of last ACCEPTED spot tick (bad-tick guard re-sync)
        self.spot_history = deque(maxlen=1800)  # (ts_sec, spot_price) rolling 15+ minutes
        self.session_high = 0.0
        self.session_low = float("inf")
        self.spot_ema_fast = 0.0
        self.spot_ema_slow = 0.0
        self.current_atm_strike = 0.0
        self.last_atm_iv = 0.15
        self.cooldowns: Dict[str, float] = {}  # sym -> last_signal_ts
        self._pending: Dict[str, Dict] = {}
        self.primed_strikes: Dict[str, Dict] = {}  # sym -> {"ts": float, "direction": str, "count": int}
        self.required_bursts = int(self.P.get("required_bursts", 3))  # 3-Burst Triple Tap
        self.min_burst_gap_sec = float(self.P.get("min_burst_gap_sec", 5.0))  # ≥ 5.0s between distinct bursts
        self.max_burst_memory_sec = float(self.P.get("max_burst_memory_sec", 180.0))
        self.spot_1m_drift_gate = float(self.P.get("spot_1m_drift_gate", 1.5))
        self.option_cache: Dict[str, float] = {}
        self.last_known_depth: Dict[str, Tuple[float, float]] = {}  # sym -> (tot_bid_depth, tot_ask_depth)

        # V3 Horizon-Aligned Modules (Adaptive for NIFTY vs SENSEX)
        telemetry_dir = os.path.join(PARAM_DIR, "..", "logs", "telemetry") if "PARAM_DIR" in globals() else "AEGIS/logs/telemetry"
        # Dedicated End-to-End Price Action Engine (replaces legacy TimeBarEngine)
        self.price_action_engine = PriceActionEngine(strike_step=self.strike_step, atr_period=14) if PriceActionEngine is not None else None
        # Alias time_bar_engine to price_action_engine for 100% backward compatibility
        self.time_bar_engine = self.price_action_engine
        self.persistence_engine = DirectionalPersistenceEngine(window_sec=30.0, sample_interval_sec=5.0) if DirectionalPersistenceEngine is not None else None
        
        # SENSEX has lot size 20 (vs Nifty 65) -> depth threshold adaptive: 150 for Sensex, 400 for Nifty
        is_sensex = (float(self.strike_step) == 100.0) or ("SENSEX" in str(getattr(self, "index_name", "")).upper()) or any("SENSEX" in str(s).upper() for s in self.t2s.values())
        min_depth = 50.0 if is_sensex else 150.0
        self.option_quality_engine = OptionQualityEngine(max_spread_pct=2.5, min_top5_depth_qty=min_depth) if OptionQualityEngine is not None else None
        self.signal_fsm = SignalStateMachine(max_trigger_freshness_sec=45.0, max_trigger_acceptable_sec=45.0, prime_timeout_sec=60.0) if SignalStateMachine is not None else None
        self.eqs_engine = EntryQualityEngine(dispatch_threshold=float(self.P.get("eqs_dispatch_threshold", 70.0))) if EntryQualityEngine is not None else None
        self.outcome_telemetry = OutcomeTelemetryEngine(log_dir=telemetry_dir) if OutcomeTelemetryEngine is not None else None
        self.oi_gate = OIBuiltupGate(
            strike_step=self.strike_step,
            unwind_window_sec=180.0,
            zone1_strike_dist=0.40,
            zone2_strike_dist=1.00,
            zone1_unwind_speed_pct=-1.0,
            zone2_unwind_speed_pct=-0.5,
            require_builtup_confirmation=True,
            enabled=True,
        ) if OIBuiltupGate is not None else None

        self.vwap_engine = IndexVWAPEngine(
            index_name="SENSEX" if self.strike_step >= 100.0 else "NIFTY",
            strike_step=self.strike_step,
            max_extension_bps=float(self.P.get("max_vwap_extension_bps", 25.0)),
            poll_interval_sec=60.0,
        ) if IndexVWAPEngine is not None else None

        # Fill & Edge tracking
        self.fill_latency_buf = np.zeros(500, dtype=np.float32)
        self.post_fill_edge_buf = np.zeros(500, dtype=np.float32)
        self.fill_ptr = 0
        self._pending_edge_measures: List[Dict] = []
        
        # Adaptive Veto Gate & Horizon Alignment Parameters
        self.moderate_conviction_threshold = float(self.P.get("moderate_conviction_threshold", 60.0))
        self.min_tick_rate_gate = float(self.P.get("min_tick_rate_gate", 0.05))
        self.impulse_validity_window_sec = float(self.P.get("impulse_validity_window_sec", 60.0))  # 60s micro impulse memory
        self.volatility_memory_window_sec = float(self.P.get("volatility_memory_window_sec", 300.0))  # 5-min macro volatility memory
        self.last_volatility_burst_ts: float = -1.0e18
        self.macro_drift_target_bps = float(self.P.get("macro_drift_target_bps", 1.0))
        self.impulse_memory: Dict[str, Tuple[float, float, float]] = {}  # sym -> (ts, score, lp)

        # Active Index Futures Tracking & Lead-Lag Basis Engine
        self.last_futures_price: float = 0.0
        self.last_basis_pts: float = 0.0
        self.last_basis_bps: float = 0.0
        self.futures_volume: float = 0.0
        self.futures_history: deque = deque(maxlen=300)  # (now_sec, fut_lp, basis_pts)

        # Compatibility attribute (deprecated, kept for dashboard schema — see audit P2.10)
        self.gamma_tensor = np.zeros((2, N_STRIKES), dtype=np.float32)

        # Warm tick_ring & history_30s after all symbols known (P1.4/P2.11)
        self._rebuild_strike_to_symbol()
        for sym in set(self.t2s.values()):
            if sym not in self.tick_ring:
                self.tick_ring[sym] = deque(maxlen=300)
            if sym not in self.history_30s:
                self.history_30s[sym] = deque(maxlen=300)

        logger.info(
            f"AEGIS Empirical Microstructure Engine initialised | "
            f"HighThreshold={self.high_conviction_threshold:.1f} | ModThreshold={self.moderate_conviction_threshold:.1f} | "
            f"MinTickRate={self.min_tick_rate_gate:.2f} | MacroDriftTarget={self.macro_drift_target_bps:.1f}bps | "
            f"MinDepth={min_depth:.0f} | StrikeStep={self.strike_step}"
        )

    def _load_or_default_params(self) -> Dict:
        """Load calibrated empirical microstructure weights."""
        if os.path.exists(PARAM_FILE):
            try:
                with open(PARAM_FILE, "r") as f:
                    params = json.load(f)
                logger.info(f"Loaded calibrated empirical params from {PARAM_FILE}")
                return params
            except Exception as e:
                logger.warning(f"Failed to load {PARAM_FILE}: {e}")

        priors = {
            "model_name": "AEGIS_Empirical_Microstructure_Scorer_v1",
            "high_conviction_threshold": 75.0,
            "moderate_conviction_threshold": 60.0,
            "cost_bps": 2.0,
            "hold_time_sec": 30.0,
            "burst_cooldown_sec": 60.0
        }
        return priors

    def reload_params(self):
        """Hot-reload params from disk."""
        self.P = self._load_or_default_params()
        self.high_conviction_threshold = float(self.P.get("high_conviction_threshold", 75.0))
        self.moderate_conviction_threshold = float(self.P.get("moderate_conviction_threshold", 60.0))
        self.min_tick_rate_gate = float(self.P.get("min_tick_rate_gate", 0.05))
        self.impulse_validity_window_sec = float(self.P.get("impulse_validity_window_sec", 60.0))
        self.volatility_memory_window_sec = float(self.P.get("volatility_memory_window_sec", 300.0))
        self.macro_drift_target_bps = float(self.P.get("macro_drift_target_bps", 1.0))
        if self.scorer is not None:
            weights_file = os.path.join(PARAM_DIR, "microstructure_weights.json")
            self.scorer = MicrostructureScorer(weights_path=weights_file, high_conviction_threshold=self.high_conviction_threshold)
        logger.info(
            f"Parameters reloaded | HighThresh: {self.high_conviction_threshold} | "
            f"ModThresh: {self.moderate_conviction_threshold} | MinTickRate: {self.min_tick_rate_gate} | "
            f"MacroDriftTarget: {self.macro_drift_target_bps}bps"
        )

    # ═══════════════════════════════════════════════════════════
    # HOT PATH: PER-TICK PROCESSING (O(1), ZERO ALLOC)
    # ═══════════════════════════════════════════════════════════
    def on_tick_parsed(self, tick) -> None:
        """Processes zero-copy incoming tick from market WebSocket."""
        try:
            tok_str = str(tick.tok)
            sym = self.t2s.get(tok_str) or getattr(tick, "sym", None)
            if not sym:
                return

            now = float(tick.ts) if float(tick.ts) > 1e9 else float(tick.ts) * 1000.0
            now_sec = now / 1000.0 if now > 1e11 else now
            lp = float(tick.lp)
            if lp <= 0:
                return

            # ─── Underlying Cash Index Tick ───
            if tok_str in self.underlying_tokens:
                self._on_underlying_tick(sym, lp, now_sec)
                return

            # ─── Option Contract Tick ───
            self.option_cache[tok_str] = lp
            if self.outcome_telemetry:
                self.outcome_telemetry.update_tick(sym, lp, now_sec)

            info = self.token_map.get(tok_str)
            if not info:
                return

            # Route futures token to live Basis and Lead-Lag tracking (do not score as option)
            if info.get("is_futures"):
                self._on_futures_tick(sym, lp, tick, now_sec)
                return

            side = info["side"]          # 0=CE, 1=PE
            strike = float(info["strike"])

            # [NEW] O(1) Rolling 30.0s Time-Window Tick Rate & L1 History
            if sym not in self.tick_ring:
                self.tick_ring[sym] = deque(maxlen=300)
            if sym not in self.history_30s:
                self.history_30s[sym] = deque(maxlen=300)
                
            ring = self.tick_ring[sym]
            ring.append(now_sec)
            cutoff_30s = now_sec - 30.0
            while ring and ring[0] < cutoff_30s:
                ring.popleft()
            live_tick_rate = len(ring) / 30.0

            # Extract Level 1-5 Depth - Scalar O(1) Math (Invariant I1)
            if hasattr(tick, "bp") and hasattr(tick, "sp") and len(tick.bp) >= 5 and len(tick.sp) >= 5:
                bp1 = float(tick.bp[0])
                sp1 = float(tick.sp[0])
                b1 = float(tick.bq[0])
                a1 = float(tick.sq[0])
                b2 = float(tick.bq[1])
                a2 = float(tick.sq[1])
                b3 = float(tick.bq[2])
                a3 = float(tick.sq[2])
                b4 = float(tick.bq[3])
                a4 = float(tick.sq[3])
                b5 = float(tick.bq[4])
                a5 = float(tick.sq[4])
            else:
                bp1 = float(getattr(tick, "bp1", 0.0) or 0.0)
                sp1 = float(getattr(tick, "sp1", 0.0) or 0.0)
                b1 = float(getattr(tick, "bq1", 0.0) or 0.0)
                a1 = float(getattr(tick, "sq1", 0.0) or 0.0)
                b2 = float(getattr(tick, "bq2", 0.0) or 0.0)
                a2 = float(getattr(tick, "sq2", 0.0) or 0.0)
                b3 = float(getattr(tick, "bq3", 0.0) or 0.0)
                a3 = float(getattr(tick, "sq3", 0.0) or 0.0)
                b4 = float(getattr(tick, "bq4", 0.0) or 0.0)
                a4 = float(getattr(tick, "sq4", 0.0) or 0.0)
                b5 = float(getattr(tick, "bq5", 0.0) or 0.0)
                a5 = float(getattr(tick, "sq5", 0.0) or 0.0)

            hist_30s = self.history_30s[sym]
            hist_30s.append((now_sec, lp, bp1, sp1, b1, a1))
            while hist_30s and hist_30s[0][0] < cutoff_30s:
                hist_30s.popleft()

            if self.oi_gate and strike > 0 and side in (0, 1):
                kind = 1 if side == 0 else 2
                oi_val = float(getattr(tick, "oi", 0.0) or 0.0)
                self.oi_gate.update_tick(sym, strike, kind, lp, oi_val, now_sec)

            curr_b_depth = b1 + b2 + b3 + b4 + b5
            curr_a_depth = a1 + a2 + a3 + a4 + a5
            if (curr_b_depth + curr_a_depth) > 0:
                tot_bid_depth = curr_b_depth
                tot_ask_depth = curr_a_depth
                self.last_known_depth[sym] = (tot_bid_depth, tot_ask_depth)
            else:
                tot_bid_depth, tot_ask_depth = self.last_known_depth.get(sym, (0.0, 0.0))

            if self.scorer is not None:
                self.scorer.update_tick(sym, now_sec, lp, bp1, sp1, b1, a1, b2, a2, b3, a3, b4, a4, b5, a5)

                direction = "UP" if side == 0 else "DOWN"  # 0=CE -> UP, 1=PE -> DOWN

                # ─── PILLAR 1: VWAP One-Way Directional Regime (Route 2 Price Action) ───
                # Fast pre-filtering: Spot < VWAP suppresses CE (UP), Spot > VWAP suppresses PE (DOWN)
                if self.vwap_engine and self.vwap_engine.current_vwap > 0 and self.last_spot > 0:
                    vwap_val = self.vwap_engine.current_vwap
                    if direction == "UP" and self.last_spot < vwap_val:
                        return
                    elif direction == "DOWN" and self.last_spot > vwap_val:
                        return

                # Compute adjacent OTM returns (OTM 1 to 4) and opposite side return
                otm_rets = {}
                for k in (1, 2, 3, 4):
                    otm_k_strike = strike + k * self.strike_step if side == 0 else strike - k * self.strike_step
                    otm_sym = self.strike_to_symbol.get((side, otm_k_strike))
                    if otm_sym and otm_sym in self.scorer.history:
                        buf_otm = self.scorer.history[otm_sym]
                        if len(buf_otm) >= 2 and buf_otm[0][1] > 0:
                            otm_rets[k] = (buf_otm[-1][1] - buf_otm[0][1]) / buf_otm[0][1]

                opp_sym = self.strike_to_symbol.get((1 - side, strike))
                opp_ret = 0.0
                if opp_sym and opp_sym in self.scorer.history:
                    buf_opp = self.scorer.history[opp_sym]
                    if len(buf_opp) >= 2 and buf_opp[0][1] > 0:
                        opp_ret = (buf_opp[-1][1] - buf_opp[0][1]) / buf_opp[0][1]

                # Compute real-time empirical confidence score
                score, is_high_conv, feats = self.scorer.compute_score(
                    sym, 
                    now_sec,
                    adjacent_otm_returns=otm_rets,
                    opp_side_return=opp_ret
                )
                
                # Record high conviction into impulse memory
                if score >= self.high_conviction_threshold:
                    self.impulse_memory[sym] = (now_sec, score, lp)

                # Check if this contract has an active impulse memory from the last 60 seconds
                has_active_impulse = False
                imp_score = score
                if sym in self.impulse_memory:
                    imp_ts, imp_s, imp_lp = self.impulse_memory[sym]
                    if (now_sec - imp_ts) <= self.impulse_validity_window_sec:
                        has_active_impulse = True
                        imp_score = max(score, imp_s)

                # ─── THE 3 ACTIVE PRICE ACTION & MICROSTRUCTURE SCALPER ENGINES ───
                # Pillar 1: 20 EMA + Wick Rejection (User's System - Top Performer)
                # Pillar 2: Al Brooks 2-Legged Pullback (H2 / L2 Exhaustion at 50 EMA)
                # Pillar 3: Volatility Squeeze to Expansion Breakout (100% WR Scalper)
                is_pa_setup = False
                pa_setup_name = ""
                pa_details = {}
                if self.price_action_engine:
                    current_vwap = getattr(self.vwap_engine, "current_vwap", 0.0) if self.vwap_engine else 0.0
                    is_pa_setup, pa_setup_name, pa_details = self.price_action_engine.evaluate_setup(direction, current_vwap)

                # Legacy alias for backward compatibility with telemetry/logs
                is_50ema_pullback = (pa_setup_name == "albrooks_2legged_pullback")

                # Fast bypass: if not a price action setup, and score below 50.0 without active impulse, return
                if not is_pa_setup and score < 50.0 and not has_active_impulse:
                    return

                # Adaptive Conviction Threshold based on Spot Drift confirmation or Price Action trigger
                drift_60_bps = self.get_spot_drift_bps(now_sec, 60.0) if len(self.spot_history) >= 2 else 0.0
                is_trend_confirmed = (direction == "UP" and drift_60_bps >= self.macro_drift_target_bps) or \
                                     (direction == "DOWN" and drift_60_bps <= -self.macro_drift_target_bps) or \
                                     is_pa_setup

                effective_threshold = self.moderate_conviction_threshold if is_trend_confirmed else self.high_conviction_threshold
                
                # Trigger is eligible if:
                # - Score meets adaptive threshold, OR
                # - Active high-conviction impulse occurred within 60s and score >= 55.0, OR
                # - One of the 3 empirically verified Price Action Setups triggered!
                is_trigger_eligible = (score >= effective_threshold) or \
                                      (has_active_impulse and score >= 55.0) or \
                                      is_pa_setup

                # Check Trigger Eligibility
                if is_trigger_eligible:
                    last_fired = self.cooldowns.get(sym, 0.0)
                    cooldown_dur = float(self.P.get("burst_cooldown_sec", 60.0))
                    if now_sec - last_fired >= cooldown_dur:
                        # [OCCAM GATE] Veto if market is quiet (low intensity)
                        if not is_pa_setup and live_tick_rate < self.min_tick_rate_gate:
                            logger.info(
                                f"[OCCAM VETO] {sym} Score={score:.1f} tick_rate={live_tick_rate:.2f} < {self.min_tick_rate_gate:.1f}"
                            )
                            return

                        # [REGIME CHOP GATE] Veto if market is in low-efficiency oscillation or dead compression
                        # Watertight Guard: Price action pullbacks naturally cause low KER, and Squeeze causes RangeComp veto.
                        # Both are explicitly bypassed when an active Price Action setup is detected.
                        if not is_pa_setup and self.chop_gate:
                            chop_ok, chop_reason, chop_metrics = self.chop_gate.get_regime_state(now_sec)
                            if not chop_ok and chop_reason != "warmup":
                                logger.info(
                                    f"[REGIME CHOP VETO] {sym} ({direction}) blocked by '{chop_reason}' (KER={chop_metrics.get('ker', 0.0):.3f}, RangeComp={chop_metrics.get('range_ratio', 0.0):.3f})"
                                )
                                return

                        # [MACRO ALIGNMENT GATE] Agile 60-Second Spot Drift Confirmation (>= macro_drift_target_bps)
                        # Watertight Guard: Bypassed during Price Action setups so counter-trend pullback doesn't veto
                        if not is_pa_setup and len(self.spot_history) >= 2 and self.last_spot > 0:
                            drift_60_pts = self.get_spot_drift(now_sec, 60.0)
                            if direction == "UP" and drift_60_bps < self.macro_drift_target_bps:
                                logger.info(
                                    f"[MACRO ALIGN VETO] {sym} (UP) blocked: 60s spot drift below momentum target ({drift_60_bps:+.1f}bps/{drift_60_pts:+.1f}pts < {self.macro_drift_target_bps:.1f}bps)"
                                )
                                return
                            elif direction == "DOWN" and drift_60_bps > -self.macro_drift_target_bps:
                                logger.info(
                                    f"[MACRO ALIGN VETO] {sym} (DOWN) blocked: 60s spot drift above momentum target ({drift_60_bps:+.1f}bps/{drift_60_pts:+.1f}pts > -{self.macro_drift_target_bps:.1f}bps)"
                                )
                                return

                        # [ANTI-EXHAUSTION GATE] Multi-Window Scale-Invariant Exhaustion (5m & 15m)
                        # Watertight Guard: Price Action setups have retraced and reset exhaustion
                        if not is_pa_setup and len(self.spot_history) >= 2 and self.last_spot > 0:
                            drift_5m_bps = self.get_spot_drift_bps(now_sec, 300.0)
                            drift_15m_bps = self.get_spot_drift_bps(now_sec, 900.0)
                            max_exhaust_5m_bps = float(self.P.get("max_exhaust_5m_bps", 15.0))
                            max_exhaust_15m_bps = float(self.P.get("max_exhaust_15m_bps", 25.0))

                            if direction == "DOWN":
                                if drift_5m_bps < -max_exhaust_5m_bps:
                                    logger.info(
                                        f"[EXHAUSTION VETO] {sym} (DOWN) blocked: 5m spot drop {drift_5m_bps:.1f}bps exceeds limit (-{max_exhaust_5m_bps:.1f}bps)"
                                    )
                                    return
                                if drift_15m_bps < -max_exhaust_15m_bps:
                                    logger.info(
                                        f"[EXHAUSTION VETO] {sym} (DOWN) blocked: 15m spot drop {drift_15m_bps:.1f}bps exceeds limit (-{max_exhaust_15m_bps:.1f}bps)"
                                    )
                                    return
                            elif direction == "UP":
                                if drift_5m_bps > max_exhaust_5m_bps:
                                    logger.info(
                                        f"[EXHAUSTION VETO] {sym} (UP) blocked: 5m spot surge +{drift_5m_bps:.1f}bps exceeds limit (+{max_exhaust_5m_bps:.1f}bps)"
                                    )
                                    return
                                if drift_15m_bps > max_exhaust_15m_bps:
                                    logger.info(
                                        f"[EXHAUSTION VETO] {sym} (UP) blocked: 15m spot surge +{drift_15m_bps:.1f}bps exceeds limit (+{max_exhaust_15m_bps:.1f}bps)"
                                    )
                                    return

                        # [SESSION EXTREME PROXIMITY GATE]
                        # Vetoes entering PUT directly on unconfirmed session low / floor, or CALL directly on session ceiling
                        # Watertight Guard: Bypassed for Price Action setups so squeeze breakouts at extremes aren't vetoed
                        if not is_pa_setup and self.session_high > 0 and self.session_low < float("inf") and self.last_spot > 0:
                            dist_to_low = self.last_spot - self.session_low
                            dist_to_high = self.session_high - self.last_spot
                            session_drop_bps = ((self.session_high - self.last_spot) / self.session_high) * 10000.0
                            session_rally_bps = ((self.last_spot - self.session_low) / self.session_low) * 10000.0

                            if direction == "DOWN" and session_drop_bps >= 25.0 and dist_to_low <= (0.25 * self.strike_step):
                                logger.info(
                                    f"[SESSION FLOOR VETO] {sym} (DOWN) blocked: session drop {session_drop_bps:.1f}bps, sitting on session low floor (dist={dist_to_low:.1f}pts)"
                                )
                                return
                            elif direction == "UP" and session_rally_bps >= 25.0 and dist_to_high <= (0.25 * self.strike_step):
                                logger.info(
                                    f"[SESSION CEILING VETO] {sym} (UP) blocked: session rally +{session_rally_bps:.1f}bps, sitting on session high ceiling (dist={dist_to_high:.1f}pts)"
                                )
                                return

                        # [VWAP OVER-EXTENSION GATE]
                        # Vetoes entering into an overextended move far away from VWAP (> 25 bps)
                        # Watertight Guard: Bypassed when Price Action setup is active (built-in VWAP alignment)
                        if not is_pa_setup and self.vwap_engine and self.vwap_engine.current_vwap > 0:
                            vwap_m = self.vwap_engine.get_vwap_metrics(self.last_spot)
                            if direction == "DOWN" and vwap_m.get("is_overextended_down"):
                                logger.info(
                                    f"[VWAP EXTENSION VETO] {sym} (DOWN) blocked: Spot overextended below VWAP ({vwap_m['dist_bps']:.1f}bps < -{self.vwap_engine.max_extension_bps:.1f}bps)"
                                )
                                return
                            elif direction == "UP" and vwap_m.get("is_overextended_up"):
                                logger.info(
                                    f"[VWAP EXTENSION VETO] {sym} (UP) blocked: Spot overextended above VWAP (+{vwap_m['dist_bps']:.1f}bps > +{self.vwap_engine.max_extension_bps:.1f}bps)"
                                )
                                return

                        # Domain 1: Permission (Higher Timeframe Trend & Structure)
                        has_permission = True
                        perm_reason = "permission_ok"
                        perm_metrics = {}
                        if self.time_bar_engine:
                            if is_pa_setup:
                                # Price action setup has verified trend alignment and rejection geometry
                                has_permission = True
                                perm_reason = f"price_action_{pa_setup_name}"
                            else:
                                has_permission, perm_reason, perm_metrics = self.time_bar_engine.get_permission_state(direction)
                                if not has_permission and perm_reason != "warmup_permissive":
                                    logger.info(f"[PERMISSION VETO] {sym} ({direction}) blocked by '{perm_reason}'")
                                    return

                        # Domain 1.5: Volatility Expansion Gate with 5-Minute Impulse Memory
                        # Watertight Guard: Bypassed for Price Action setups (especially Squeeze which is compression)
                        if not is_pa_setup:
                            atr_ratio = float(perm_metrics.get("atr_ratio", 1.0))
                            has_vol_memory = (now_sec - self.last_volatility_burst_ts) <= self.volatility_memory_window_sec
                            if atr_ratio < 0.75 and not has_vol_memory and perm_reason != "warmup_permissive":
                                logger.info(
                                    f"[VOLATILITY CHOP VETO] {sym} ({direction}) blocked: ATR ratio severely depressed "
                                    f"({atr_ratio:.2f} < 0.75) and no active 5m volatility memory"
                                )
                                return

                        # Domain 2: Confirmation (60s Signed Persistence)
                        pers_confirmed = True
                        pers_score = 1.0
                        pers_metrics = {}
                        if not is_pa_setup and self.persistence_engine:
                            pers_confirmed, pers_score, pers_metrics = self.persistence_engine.evaluate_persistence(direction)

                        # Domain 3 & 4: Option Tradability & Quality
                        opt_qualified = True
                        opt_reason = "option_ok"
                        opt_metrics = {}
                        if self.option_quality_engine:
                            opt_qualified, opt_reason, opt_metrics = self.option_quality_engine.qualify_option(
                                sym=sym,
                                lp=lp,
                                bp1=bp1,
                                sp1=sp1,
                                top5_bid_qty=tot_bid_depth,
                                top5_ask_qty=tot_ask_depth,
                            )
                            if not opt_qualified:
                                logger.info(f"[OPTION QUALITY VETO] {sym} blocked: {opt_reason}")
                                return

                        # Domain 7: Real-Time Open Interest (OI) Built-up & Wall Defense Gate
                        if self.oi_gate:
                            oi_allowed, oi_reason, oi_metrics = self.oi_gate.evaluate_gate(
                                sym=sym,
                                spot_price=self.last_spot,
                                direction=direction,
                                now_sec=now_sec,
                            )
                            if not oi_allowed:
                                logger.info(f"[OI GATE VETO] {sym} ({direction}) blocked: {oi_reason}")
                                return

                        # Centralized Signal FSM Lifecycle Evaluation
                        fsm_state = None
                        should_fire = False
                        fsm_reason = "direct"
                        if is_pa_setup:
                            # ─── MICROSTRUCTURE VALIDATOR (Secondary Protective Shield) ───
                            # User Directive: Min 30s persistence, balanced secondary parameters.
                            # Primary anchor is Macro Price Action; microstructure ensures liquid entry & non-inverted book.
                            
                            # 1. 30s Rolling Persistence Gate
                            if self.persistence_engine:
                                pers_ok, pers_score, pers_m = self.persistence_engine.evaluate_persistence(
                                    direction,
                                    lookback_sec=30.0,
                                    min_threshold=0.55,
                                    max_allowed_opposite=1,
                                )
                                # Veto only if persistence is explicitly opposed (not during warmup)
                                if not pers_ok and pers_m.get("reason") not in ("warmup_zero_obs_permissive", "warmup_aligned_permissive"):
                                    logger.info(
                                        f"[MICRO VALIDATOR VETO] {sym} ({direction}) {pa_setup_name} blocked by persistence: {pers_m.get('reason')} (Dp={pers_score:.2f})"
                                    )
                                    return

                            # 2. Depth Alignment (Sanity check: ensure option book is not completely blocked by opposing wall)
                            tot_depth = tot_bid_depth + tot_ask_depth
                            if tot_depth > 0:
                                top5_imb = (tot_bid_depth - tot_ask_depth) / float(tot_depth)
                                if top5_imb < -0.40:
                                    logger.info(f"[MICRO VALIDATOR VETO] {sym} {pa_setup_name} blocked: severe opposing book wall (top5_imb={top5_imb:.2f} < -0.40)")
                                    return

                            # 3. Friction Gate (Spread % & Edge-to-Cost ETF)
                            mid_px = (bp1 + sp1) / 2.0 if (bp1 > 0 and sp1 > 0) else lp
                            spread_pct = ((sp1 - bp1) / mid_px * 100.0) if (mid_px > 0 and bp1 > 0 and sp1 > 0) else 0.5
                            max_allowed_spread = 1.0  # Liquid ATM contract max spread
                            if spread_pct > max_allowed_spread:
                                logger.info(f"[MICRO VALIDATOR VETO] {sym} {pa_setup_name} blocked: spread {spread_pct:.2f}% > {max_allowed_spread:.2f}%")
                                return

                            roundtrip_friction = spread_pct + 0.30
                            etf = 4.0 / roundtrip_friction if roundtrip_friction > 0 else 5.0
                            if etf < 2.5:
                                logger.info(f"[MICRO VALIDATOR VETO] {sym} {pa_setup_name} blocked: ETF {etf:.2f} < 2.5 (friction={roundtrip_friction:.2f}%)")
                                return

                            should_fire = True
                            fsm_reason = f"price_action_{pa_setup_name}"
                        elif self.signal_fsm:
                            fsm_state, should_fire, fsm_reason = self.signal_fsm.update_trigger(
                                sym=sym,
                                now_sec=now_sec,
                                direction=direction,
                                micro_score=score,
                                has_permission=has_permission,
                                persistence_confirmed=pers_confirmed,
                                option_qualified=opt_qualified,
                            )
                            if not should_fire:
                                if fsm_state == SignalState.PRIMED and fsm_reason.startswith("primed"):
                                    logger.info(f"[AEGIS V3 PRIMED] {sym} ({direction}) | Score: {score:.1f}/100 | FSM: PRIMED | Awaiting persistence & confirmation...")
                                return
                        else:
                            should_fire = is_trigger_eligible

                        if should_fire:
                            trigger_age = 0.5
                            if self.signal_fsm and not is_pa_setup:
                                trigger_age = now_sec - self.signal_fsm.get_or_create(sym).last_trigger_ts
                            
                            eqs = 85.0
                            is_eqs_qual = True
                            eqs_breakdown = {}
                            if not is_pa_setup and self.eqs_engine:
                                eqs, is_eqs_qual, eqs_breakdown = self.eqs_engine.compute_eqs(
                                    ema_1m_slope=perm_metrics.get("ema_1m_slope", 0.0),
                                    ema_3m_slope=perm_metrics.get("ema_3m_slope", 0.0),
                                    atr_ratio=perm_metrics.get("atr_ratio", 1.0),
                                    market_structure=perm_metrics.get("market_structure", "NEUTRAL"),
                                    direction=direction,
                                    persistence_score=pers_score,
                                    opposite_count=pers_metrics.get("opposite_count", 0),
                                    micro_score=score,
                                    trigger_age_sec=trigger_age,
                                    spread_pct=opt_metrics.get("spread_pct", 0.5),
                                    total_depth_qty=tot_bid_depth + tot_ask_depth,
                                )
                            elif is_pa_setup:
                                eqs = 85.0
                                is_eqs_qual = True
                                eqs_breakdown = {
                                    "eqs": 85.0,
                                    "pa_setup": pa_setup_name,
                                    "pillar": pa_details.get("pillar", 1),
                                }

                            if is_eqs_qual:
                                # Fast Scalp Take-Profit Target (4.0% default)
                                recommended_tp_pct = 0.040

                                # Extract rich timing, volatility, and futures flow telemetry
                                vol_burst_age = round(now_sec - self.last_volatility_burst_ts, 2) if getattr(self, "last_volatility_burst_ts", 0.0) > 0 else 0.0
                                atr_1m_val = getattr(self.time_bar_engine, "current_atr_1m", 0.0) if self.time_bar_engine else 0.0
                                spread_val = opt_metrics.get("spread_pct", 0.0) if isinstance(opt_metrics, dict) else 0.0
                                tot_depth_val = tot_bid_depth + tot_ask_depth
                                fut_basis_m = self.get_futures_basis_metrics(now_sec, 60.0)

                                telemetry_metrics = {
                                    "impulse_timestamp": now_sec,
                                    "impulse_time_iso": datetime.fromtimestamp(now_sec).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                                    "volatility_burst_age_sec": vol_burst_age,
                                    "trigger_age_sec": round(trigger_age, 2),
                                    "atr_1m": round(atr_1m_val, 2),
                                    "spread_pct": round(spread_val, 3),
                                    "total_depth_qty": tot_depth_val,
                                    "futures_price": self.last_futures_price,
                                    "basis_pts": round(self.last_basis_pts, 2),
                                    "basis_bps": round(self.last_basis_bps, 2),
                                    "futures_volume": self.futures_volume,
                                    "is_50ema_pullback": is_50ema_pullback,
                                    "is_price_action_setup": is_pa_setup,
                                    "pa_setup_name": pa_setup_name,
                                    **fut_basis_m,
                                    **pa_details,
                                }

                                event_id = f"{sym}_{int(now_sec*1000)}"
                                if self.outcome_telemetry:
                                    self.outcome_telemetry.register_entry(
                                        event_id=event_id,
                                        symbol=sym,
                                        direction=direction,
                                        entry_ts=now_sec,
                                        entry_opt_price=lp,
                                        entry_spot_price=self.last_spot,
                                        eqs_score=eqs,
                                        features_snapshot={
                                            **feats,
                                            **eqs_breakdown,
                                            **perm_metrics,
                                            **telemetry_metrics,
                                            "recommended_tp_pct": recommended_tp_pct,
                                        },
                                    )
                                if self.signal_fsm:
                                    self.signal_fsm.register_trade_executed(sym, now_sec)

                                self.cooldowns[sym] = now_sec
                                logger.info(
                                    f"[AEGIS V3 FIRED] {sym} ({direction}) | EQS: {eqs:.1f}/100 | Target: +{recommended_tp_pct*100:.1f}% | "
                                    f"Basis: {self.last_basis_pts:+.1f}pts ({self.last_basis_bps:+.1f}bps) | FutPx: {self.last_futures_price:.1f} | "
                                    f"Persist: {pers_score:.2f} | Struct: {perm_metrics.get('market_structure', 'N/A')} | "
                                    f"ATR_Ratio: {perm_metrics.get('atr_ratio', 1.0):.2f} | Spread: {opt_metrics.get('spread_pct', 0.5):.2f}%"
                                )
                                self._emit_signal(
                                    sym=sym,
                                    lp=lp,
                                    direction=direction,
                                    conviction=score,
                                    gates={
                                        "permission_granted": has_permission,
                                        "persistence_confirmed": pers_confirmed,
                                        "option_qualified": opt_qualified,
                                        "eqs_qualified": is_eqs_qual,
                                    },
                                    metrics={
                                        "eqs_score": eqs,
                                        "recommended_tp_pct": recommended_tp_pct,
                                        "microstructure_score": round(score, 2),
                                        "persistence_score": round(pers_score, 3),
                                        "live_tick_rate": round(live_tick_rate, 2),
                                        "strike": strike,
                                        "side": "CE" if side == 0 else "PE",
                                        **telemetry_metrics,
                                        **feats,
                                        **eqs_breakdown,
                                        **perm_metrics,
                                    }
                                )

        except Exception as e:
            logger.error(f"AEGIS on_tick_parsed error: {e}", exc_info=True)

    def _on_futures_tick(self, sym: str, lp: float, tick: Any, now_sec: float):
        """Processes active index futures ticks for Basis, volume, and lead-lag analysis."""
        if lp <= 0:
            return
        self.last_futures_price = lp
        vol = float(getattr(tick, "vol", 0.0) or 0.0)
        self.futures_volume = vol
        if self.last_spot > 0:
            self.last_basis_pts = lp - self.last_spot
            self.last_basis_bps = (self.last_basis_pts / self.last_spot) * 10000.0
            self.futures_history.append((now_sec, lp, self.last_basis_pts))

    @staticmethod
    def is_plausible_spot_tick(lp: float, ref_lp: float, gap_sec: float) -> bool:
        """Single-tick plausibility gate for the index feed (unit-testable).

        Rejects one-print wonders (bad ticks) while never blocking genuine flow:
        - non-finite / <= 0 prints are always rejected;
        - with no reference yet, anything positive is accepted;
        - after a feed gap (> SPOT_RESYNC_GAP_SEC: restart, wake, reconnect),
          the next tick re-syncs unconditionally (covers overnight gaps);
        - otherwise the tick must land within MAX_SINGLE_TICK_JUMP_FRAC of ref.
        """
        if not math.isfinite(lp) or lp <= 0:
            return False
        if not math.isfinite(ref_lp) or ref_lp <= 0:
            return True
        if gap_sec is None or not math.isfinite(gap_sec) or gap_sec < 0:
            gap_sec = 0.0
        if gap_sec > SPOT_RESYNC_GAP_SEC:
            return True
        return abs(lp - ref_lp) / ref_lp <= MAX_SINGLE_TICK_JUMP_FRAC

    def _on_underlying_tick(self, sym: str, lp: float, now_sec: float):
        """Processes underlying Spot price ticks and manages dynamic ATM strike alignment."""
        if lp <= 0:
            return

        # Spot sanity filter: prevent cross-index contamination (e.g. SENSEX 75k polluting NIFTY 24k)
        if self.strike_step <= 50.0 and lp > 45000.0:
            return  # SENSEX tick received in NIFTY engine
        if self.strike_step >= 100.0 and lp < 45000.0:
            return  # NIFTY tick received in SENSEX engine

        # Bad-tick guard: drop implausible single prints BEFORE they pollute
        # spot_history, PA bars, ATR, session extremes, and persistence snapshots.
        gap_sec = (now_sec - self._last_spot_ts) if self._last_spot_ts > 0 else float("inf")
        if not AegisGammaEngine.is_plausible_spot_tick(lp, self.last_spot, gap_sec):
            logger.warning(
                f"[SPOT BAD-TICK] Dropped implausible index print {lp:.2f} "
                f"vs last {self.last_spot:.2f} (gap {gap_sec:.1f}s)"
            )
            return
        self._last_spot_ts = now_sec

        self.last_spot = lp
        self.spot_history.append((now_sec, lp))

        if self.session_high == 0.0 or lp > self.session_high:
            self.session_high = lp
        if self.session_low == float("inf") or lp < self.session_low:
            self.session_low = lp

        if self.chop_gate:
            self.chop_gate.update(lp, now_sec, evaluate=False)

        if self.vwap_engine:
            self.vwap_engine.update_spot(lp, now_sec)

        # 1. Update V3 TimeBarEngine & 5-minute Volatility Impulse Memory
        if self.time_bar_engine:
            self.time_bar_engine.update_tick(price=lp, ts_sec=now_sec)
            if self.time_bar_engine.atr_ratio >= 1.0 or self.time_bar_engine.current_atr_1m >= (0.00025 * lp):
                self.last_volatility_burst_ts = now_sec

        # 2. Update V3 60s Directional Persistence Snapshot every 5s
        if self.persistence_engine and self.persistence_engine.should_sample(now_sec):
            # True in-flight 5-second drift from spot history
            drift_5s = 0.0
            if len(self.spot_history) >= 2:
                p_5s = self.spot_history[0][1]
                target_ts = now_sec - 5.0
                for ts_h, px_h in reversed(self.spot_history):
                    if ts_h <= target_ts:
                        p_5s = px_h
                        break
                drift_5s = lp - p_5s

            # In-flight directional determination using live EMA slope & live drift
            curr_dir = "NEUTRAL"
            if self.time_bar_engine:
                _, live_slope_1m_bps, _, _ = self.time_bar_engine.get_live_ema_and_slope()
                if live_slope_1m_bps > 0.5 and drift_5s > 0.2:
                    curr_dir = "UP"
                elif live_slope_1m_bps < -0.5 and drift_5s < -0.2:
                    curr_dir = "DOWN"
            elif abs(drift_5s) >= 0.5:
                curr_dir = "UP" if drift_5s > 0 else "DOWN"

            self.persistence_engine.record_snapshot(
                now_sec=now_sec,
                direction=curr_dir,
                micro_score=75.0 if curr_dir != "NEUTRAL" else 50.0,
                spot_drift=drift_5s,
            )

        # Prune older than 900s (15 min)
        cutoff = now_sec - 900.0
        while self.spot_history and self.spot_history[0][0] < cutoff:
            self.spot_history.popleft()

        # Update fast (1-min) and slow (5-min) Spot EMAs
        if self.spot_ema_fast == 0.0:
            self.spot_ema_fast = lp
            self.spot_ema_slow = lp
        else:
            self.spot_ema_fast = 0.05 * lp + 0.95 * self.spot_ema_fast
            self.spot_ema_slow = 0.01 * lp + 0.99 * self.spot_ema_slow

        new_atm = round(lp / self.strike_step) * self.strike_step
        if self.current_atm_strike == 0.0:
            self.current_atm_strike = new_atm
        elif new_atm != self.current_atm_strike:
            shift = int(round((new_atm - self.current_atm_strike) / self.strike_step))
            if shift != 0:
                self.current_atm_strike = new_atm
                logger.info(f"[AEGIS] Dynamic ATM roll: shift={shift:+d} | New ATM={self.current_atm_strike:.0f}")

    def get_spot_drift(self, now_sec: float, lookback_sec: float = 60.0) -> float:
        """Returns the spot price drift in points (current_spot - spot_lookback_ago)."""
        if not self.spot_history or self.last_spot <= 0:
            return 0.0
        if (now_sec - self.spot_history[-1][0]) > 60.0:
            return 0.0
        target_ts = now_sec - lookback_sec
        ref_price = self.spot_history[0][1]
        for ts, price in self.spot_history:
            if ts <= target_ts:
                ref_price = price
            else:
                break
        return self.last_spot - ref_price

    def get_spot_drift_bps(self, now_sec: float, lookback_sec: float = 60.0) -> float:
        """Returns the scale-invariant spot price drift in basis points (bps)."""
        if not self.spot_history or self.last_spot <= 0:
            return 0.0
        if (now_sec - self.spot_history[-1][0]) > 60.0:
            return 0.0
        target_ts = now_sec - lookback_sec
        ref_price = self.spot_history[0][1]
        for ts, price in self.spot_history:
            if ts <= target_ts:
                ref_price = price
            else:
                break
        if ref_price <= 0 or abs(self.last_spot - ref_price) > 5000:
            # Token switch / uninitialized history jump protection
            return 0.0
        return ((self.last_spot - ref_price) / ref_price) * 10000.0

    def get_spot_30s_high_low(self, now_sec: float) -> Tuple[float, float]:
        """Returns the (spot_high_30s, spot_low_30s) across rolling 30.0s window."""
        if not self.spot_history or self.last_spot <= 0:
            return self.last_spot, self.last_spot
        cutoff = now_sec - 30.0
        pts = [p for t, p in self.spot_history if t >= cutoff]
        if not pts:
            return self.last_spot, self.last_spot
        return max(pts), min(pts)

    def get_futures_basis_metrics(self, now_sec: float, lookback_sec: float = 60.0) -> Dict[str, float]:
        """Returns 60s pre-trade Futures Basis metrics for shadow forensics telemetry."""
        if not self.futures_history or self.last_spot <= 0:
            return {
                "basis_current": round(self.last_basis_pts, 2),
                "basis_60s_ago": round(self.last_basis_pts, 2),
                "basis_change_60s": 0.0,
                "basis_change_60s_bps": 0.0,
                "basis_z_score": 0.0,
            }

        target_ts = now_sec - lookback_sec
        ref_basis = self.futures_history[0][2]
        basis_vals = []
        for ts, px, b_pts in self.futures_history:
            basis_vals.append(b_pts)
            if ts <= target_ts:
                ref_basis = b_pts

        current_basis = self.last_basis_pts
        delta_basis = current_basis - ref_basis
        delta_bps = (delta_basis / self.last_spot) * 10000.0 if self.last_spot > 0 else 0.0

        z = 0.0
        if len(basis_vals) >= 10:
            mean_b = sum(basis_vals) / len(basis_vals)
            var_b = sum((x - mean_b) ** 2 for x in basis_vals) / len(basis_vals)
            std_b = var_b ** 0.5
            if std_b > 0.01:
                z = (current_basis - mean_b) / std_b

        return {
            "basis_current": round(current_basis, 2),
            "basis_60s_ago": round(ref_basis, 2),
            "basis_change_60s": round(delta_basis, 2),
            "basis_change_60s_bps": round(delta_bps, 2),
            "basis_z_score": round(z, 2),
        }

    def _rebuild_strike_to_symbol(self):
        """Rebuild (side,strike)→symbol after token-map refresh (fixes P1.4 universe drift)."""
        self.strike_to_symbol.clear()
        for tok, info in self.token_map.items():
            sym_name = self.t2s.get(str(tok))
            if sym_name and "side" in info and "strike" in info:
                try:
                    self.strike_to_symbol[(int(info["side"]), float(info["strike"]))] = sym_name
                except Exception:
                    continue

    def refresh_token_maps(self, token_to_symbol: Dict[str, str], token_map: Dict[str, Dict]):
        """Hot-refresh after expiry roll / ATM rebuild (called by aegis_main on 1-min boundary)."""
        self.t2s = {str(k): str(v) for k, v in token_to_symbol.items()}
        self.token_map = {str(k): v for k, v in token_map.items()}
        self._rebuild_strike_to_symbol()
        # Warm tick_ring & history_30s for any new symbols without disturbing existing deques
        for sym in self.t2s.values():
            if sym not in self.tick_ring:
                self.tick_ring[sym] = deque(maxlen=300)
            if sym not in self.history_30s:
                self.history_30s[sym] = deque(maxlen=300)
        logger.info(f"[AEGIS] Token maps refreshed — {len(self.token_map)} tokens, {len(self.strike_to_symbol)} strikes")

    def shift_atm(self, shift: int, new_atm: float = 0.0):
        """External shift method."""
        if new_atm > 0:
            self.current_atm_strike = new_atm

    def _emit_signal(self, sym: str, lp: float, direction: str, conviction: float, gates: Dict, metrics: Dict):
        """Emits signal to AegisExecutionManager queue."""
        if not self.signal_queue:
            logger.warning("[AEGIS] Signal queue is not connected — signal dropped")
            return
        payload = {
            "type": "GAMMA_BURST_ENTRY",
            "sym": sym,
            "signal": direction,
            "lp": lp,
            "spot": self.last_spot,
            "conviction": conviction,
            "gates": gates,
            "metrics": metrics,
            "timestamp": time.time(),
        }
        try:
            self.signal_queue.put_nowait(payload)
        except queue.Full:
            logger.warning(f"[AEGIS] Signal queue full ({self.signal_queue.qsize()}/{self.signal_queue.maxsize}) — dropping {sym} {direction} {conviction:.1f}")
            return
        logger.info(f"[AEGIS SIGNAL EMITTED] {direction} {sym} @ {lp:.2f} (Score={conviction:.1f})")

    # ═══════════════════════════════════════════════════════════
    # EXECUTION FEEDBACK LOOP
    # ═══════════════════════════════════════════════════════════
    def on_fill(self, fill_ts: float, fill_price: float, signal_ts: float, signal_price: float,
                sym: Optional[str] = None, token: Optional[str] = None):
        """Records signal→fill latency and schedules post-fill edge measurement."""
        lat_ms = max(0.0, (fill_ts - signal_ts) * 1000.0)
        idx = self.fill_ptr % 500
        self.fill_latency_buf[idx] = lat_ms
        self.fill_ptr += 1
        self._pending_edge_measures.append({
            "fill_ts": fill_ts,
            "fill_price": fill_price,
            "measure_at": fill_ts + 30.0,
            "buf_idx": idx,
            "sym": sym,
            "token": str(token) if token is not None else None,
        })
        logger.info(f"[AEGIS] Fill recorded: latency={lat_ms:.1f}ms | px={fill_price:.2f}")

    def _resolve_option_ltp(self, sym: Optional[str], token: Optional[str]) -> float:
        """Resolves current option premium LTP from option_cache (token-first, sym fallback)."""
        if token is not None and str(token) in self.option_cache:
            px = float(self.option_cache[str(token)])
            if px > 0:
                return px
        if sym:
            for t, sn in self.t2s.items():
                if sn == sym and t in self.option_cache:
                    px = float(self.option_cache[t])
                    if px > 0:
                        return px
        return 0.0

    def check_pending_edges(self, now: float, current_opt_price: float = 0.0,
                            sym: Optional[str] = None, token: Optional[str] = None):
        """Records post-fill edge survival vs the *option premium* (not index spot).

        Resolution order per pending fill: explicit token/sym args → stored
        token/sym via option_cache → explicit current_opt_price fallback
        (accepted only if within 200% of fill_price, so a stale index-spot
        value can never poison the metric again — see P0-3).
        """
        still_pending = []
        for item in self._pending_edge_measures:
            if now >= item["measure_at"]:
                fill_px = float(item["fill_price"])
                px = self._resolve_option_ltp(sym or item.get("sym"), token or item.get("token"))
                if px <= 0 and current_opt_price > 0 and fill_px > 0:
                    # Guard: reject index-spot scale values masquerading as option LTP
                    if abs(current_opt_price - fill_px) / fill_px <= 2.0:
                        px = float(current_opt_price)
                if fill_px > 0 and px > 0:
                    ret_bps = (px - fill_px) / fill_px * 10000.0
                    self.post_fill_edge_buf[item["buf_idx"]] = ret_bps
            else:
                still_pending.append(item)
        self._pending_edge_measures = still_pending

    def _hawkes_branching(self) -> float:
        return 0.0

    def get_state_summary(self) -> Dict[str, Any]:
        """Provides snapshot metrics for monitoring / UI dashboard."""
        return {
            "spot": self.last_spot,
            "atm_strike": self.current_atm_strike,
            "atm_iv": self.last_atm_iv,
            "gamma_sum": 0.0,
            "hawkes_B": 0.0,
            "fills_recorded": self.fill_ptr,
            "pending_signals": len(self._pending),
            "scorer_active": self.scorer is not None,
            "high_conviction_threshold": self.high_conviction_threshold,
            "futures_price": self.last_futures_price,
            "basis_pts": self.last_basis_pts,
            "basis_bps": self.last_basis_bps,
            "futures_volume": self.futures_volume,
        }


def create_aegis_engine(
    token_to_symbol: Dict[str, str],
    token_map: Dict[str, Dict],
    bucket_map: Dict[str, float] = None,
    strike_step: float = 100.0,
    underlying_tokens: set = None,
    signal_queue: Any = None,
    log_path: str = None,
) -> AegisGammaEngine:
    return AegisGammaEngine(
        token_to_symbol=token_to_symbol,
        token_map=token_map,
        bucket_map=bucket_map,
        strike_step=strike_step,
        underlying_tokens=underlying_tokens,
        signal_queue=signal_queue,
        log_path=log_path,
    )
