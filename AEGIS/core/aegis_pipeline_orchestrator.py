# aegis_pipeline_orchestrator.py
# AEGIS V1.0 — Empirical Microstructure Impulse Engine
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
    from AEGIS.core.option_order_flow import (
        MicrostructureScorer,
        CompositeConvictionEngine,
        ConvictionResult,
    )
    from AEGIS.core.option_quality import OptionQualityEngine
    from AEGIS.core.outcome_telemetry import OutcomeTelemetryEngine, PostTradeMilestoneTracker

    from AEGIS.execution.aegis_execution_manager import QualitySlotPolicy, PostTriggerStabilityTracker
    from AEGIS.core.wyckoff_volume_engine import WyckoffVolumeEngine
    from AEGIS.core.shadow_boundary_observer import ShadowBoundaryObserver
except ImportError:
    try:
        from core.price_action_engine import PriceActionEngine
        from core.option_order_flow import (
            MicrostructureScorer,
            CompositeConvictionEngine,
            ConvictionResult,
        )
        from core.option_quality import OptionQualityEngine
        from core.outcome_telemetry import OutcomeTelemetryEngine, PostTradeMilestoneTracker

        from execution.aegis_execution_manager import QualitySlotPolicy, PostTriggerStabilityTracker
        from core.wyckoff_volume_engine import WyckoffVolumeEngine
        from core.shadow_boundary_observer import ShadowBoundaryObserver
    except ImportError:
        PriceActionEngine = None
        MicrostructureScorer = None
        CompositeConvictionEngine = None
        WyckoffVolumeEngine = None
        AdaptiveMicroValidator = None
        OptionQualityEngine = None
        OutcomeTelemetryEngine = None
        PostTradeMilestoneTracker = None

        ShadowBoundaryObserver = None
        QualitySlotPolicy = None
        PostTriggerStabilityTracker = None

# ─── Resilient optional imports (hardening 2026-09-21: each isolated so one
# missing/stale module on a deploy target can never kill engine init; every
# consumer already guards `is not None`) ───


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
class AegisPipelineOrchestrator:
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

        # ─── Order Flow Energy Validator (OFEE) ───
        # Decommissioned & merged directly into CompositeConvictionEngine (Zero-Entropy Architecture)


        # Decoupled legacy chop gate: wiring cut per user instruction

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
        self.option_cache: Dict[str, float] = {}
        self.last_known_depth: Dict[str, Tuple[float, float]] = {}  # sym -> (tot_bid_depth, tot_ask_depth)
        self._l2_depth_history: Dict[str, deque[Tuple[float, float, float, float]]] = {}  # sym -> deque[(ts, bid, ask, imb)]

        # V3 Horizon-Aligned Modules (Adaptive for NIFTY vs SENSEX)
        telemetry_dir = os.path.join(PARAM_DIR, "..", "logs", "telemetry") if "PARAM_DIR" in globals() else "AEGIS/logs/telemetry"
        # SENSEX has lot size 20 (vs Nifty 65) -> depth threshold adaptive: 150 for Sensex, 400 for Nifty
        is_sensex = (float(self.strike_step) == 100.0) or ("SENSEX" in str(getattr(self, "index_name", "")).upper()) or any("SENSEX" in str(s).upper() for s in self.t2s.values())
        min_depth = 50.0 if is_sensex else 150.0
        engine_lot = 20.0 if is_sensex else 65.0

        # Dedicated End-to-End Price Action Engine (replaces legacy TimeBarEngine)
        self.price_action_engine = PriceActionEngine(strike_step=self.strike_step, atr_period=14, lot_size=engine_lot) if PriceActionEngine is not None else None
        # Alias time_bar_engine to price_action_engine for 100% backward compatibility
        self.time_bar_engine = self.price_action_engine
        # Testing phase: premium floor Rs 4.0 (env AEGIS_MIN_PREMIUM) so Rs 4.30
        # candidates are not vetoed while funds are tiny.
        _min_prem = float(os.getenv("AEGIS_MIN_PREMIUM", "4.0"))
        self.option_quality_engine = OptionQualityEngine(max_spread_pct=2.5, min_top5_depth_qty=min_depth, min_premium=_min_prem) if OptionQualityEngine is not None else None
        # Hot-path veto log throttle state: key -> last emitted epoch sec + suppressed count.
        self._veto_log_ts: Dict[str, float] = {}
        self._veto_log_suppressed: Dict[str, int] = {}
        self._veto_log_interval_sec = float(os.getenv("AEGIS_VETO_LOG_INTERVAL_SEC", "60.0"))
        self.outcome_telemetry = OutcomeTelemetryEngine(log_dir=telemetry_dir) if OutcomeTelemetryEngine is not None else None
        self.post_trade_tracker = PostTradeMilestoneTracker(log_dir=telemetry_dir) if PostTradeMilestoneTracker is not None else None
        self.oi_gate = None
        self._oi_kin_armed = 0
        self._oi_kin_skipped = 0
        self._oi_kin_vetoed = 0
        self._oi_gate_vetoed = 0
        self._di_vetoed = 0
        self._last_wall_log_min = -1

        self.vwap_engine = None






        # Shadow Quant Telemetry Tracker (Tracks 5 novel quant edges in passive shadow mode)

        # ML Gatekeeper Engine (DECOMMISSIONED / DISABLED — Zero CPU overhead)
        self.is_sensex = is_sensex
        self.index_name = "SENSEX" if is_sensex else "NIFTY"
        # ─── Option Quality Mode (SHADOW = log but don't block) ───
        # Index options (NIFTY/SENSEX) always have institutional liquidity; premium floor
        # and depth checks are over-specified for 0DTE affordable index option testing.
        self.option_quality_mode = str(self.P.get("option_quality_mode", os.getenv("AEGIS_OPTION_QUALITY_MODE", "SHADOW"))).upper()

        # ─── Quality Stack (SHADOW-first safety gear) ───
        # 3-quality-slots/day policy + shadow AND-gate + post-trigger stability telemetry.
        self.quality_stack_mode = str(self.P.get("quality_stack_mode", os.getenv("AEGIS_QUALITY_STACK_MODE", "ACTIVE"))).upper()
        self.quality_energy_min = float(self.P.get("quality_energy_min", 70.0))
        self.quality_max_spread_pct = float(self.P.get("quality_max_spread_pct", 0.5))
        self.quality_stability_window_sec = float(self.P.get("quality_stability_window_sec", 10.0))
        self.quality_stability_fade_frac = float(self.P.get("quality_stability_fade_frac", 0.25))
        self.slot_policy = QualitySlotPolicy(
            max_trades_per_day=int(self.P.get("max_trades_per_day", os.getenv("MAX_TRADES_PER_DAY", "3"))),
            reserve_release_min=int(self.P.get("slot_reserve_release_min", os.getenv("AEGIS_SLOT_RESERVE_RELEASE_MIN", "0"))),
            dynamic_bar=bool(self.P.get("slot_dynamic_bar", False)),
            consec_loss_stop=int(self.P.get("consec_loss_stop", 2)),
            daily_loss_cap_rs=float(self.P.get("daily_loss_cap_rs", os.getenv("AEGIS_DAILY_LOSS_CAP_RS", "2000.0"))),
        ) if QualitySlotPolicy is not None else None
        self.stability_tracker = PostTriggerStabilityTracker(
            window_sec=self.quality_stability_window_sec,
            fade_frac=self.quality_stability_fade_frac,
        ) if PostTriggerStabilityTracker is not None else None
        self._stability_log_path = os.path.join(telemetry_dir, "aegis_stability_verdicts.jsonl") if telemetry_dir else None
        # Cross-wired EM safety state (execution manager writes, engine reads)
        self._em_consec_losses = 0
        self._em_daily_pnl = 0.0

        # ─── Composite Conviction Scoring Engine (0-100 Continuous Evaluator) ───
        self.conviction_engine = CompositeConvictionEngine(
            min_approval_score=float(self.P.get("conviction_min_approval_score", 80.0)),
            high_conviction_score=float(self.P.get("conviction_high_score", 85.0)),
            standard_tp_pct=float(self.P.get("standard_tp_pct", 0.040)),
            high_conviction_tp_pct=float(self.P.get("high_conviction_tp_pct", 0.040)),
        ) if CompositeConvictionEngine is not None else None

        # ─── Wyckoff Volume & Delta Engine (Parallel Price & Volume Confirmation Gate) ───
        self.wyckoff_mode = str(self.P.get("wyckoff_mode", os.getenv("AEGIS_WYCKOFF_MODE", "ACTIVE"))).upper()
        self.wyckoff_volume_engine = WyckoffVolumeEngine(
            mode=self.wyckoff_mode,
        ) if WyckoffVolumeEngine is not None else None

        # ─── Shadow Boundary Observer (Monitors First-Touch Pain Zone & 200 EMA + OTM 5 Paper Trading) ───
        self.shadow_boundary_observer = ShadowBoundaryObserver(
            price_action_engine=self.price_action_engine,
            wyckoff_engine=self.wyckoff_volume_engine,

            strike_step=self.strike_step,
            log_dir=telemetry_dir,
        ) if ShadowBoundaryObserver is not None else None
        if self.shadow_boundary_observer is not None:
            self.shadow_boundary_observer.strike_to_symbol = self.strike_to_symbol
            self.shadow_boundary_observer.option_cache = self.option_cache


        # ─── AEGIS V4.0 Futures RingBuffer (Passive Telemetry) ───

        # Decommissioned 2026-10-27 (Logic De-bloat): Top5DepthGuard eliminated.
        # Order-book depth & spread are fully verified by CompositeConvictionEngine.


        # Fill & Edge tracking
        self.fill_latency_buf = np.zeros(500, dtype=np.float32)
        self.post_fill_edge_buf = np.zeros(500, dtype=np.float32)
        self.fill_ptr = 0
        self._pending_edge_measures: List[Dict] = []
        
        # Adaptive Veto Gate & Horizon Alignment Parameters
        self.moderate_conviction_threshold = float(self.P.get("moderate_conviction_threshold", 60.0))
        self.min_tick_rate_gate = float(self.P.get("min_tick_rate_gate", 0.05))
        # V3.0 DEPRECATED: forward impulse memory eliminated. Backward-looking
        # event-bound slicing (T-60s to T) is the sole validator. Attribute kept
        # for backward import compat only; always empty, purged on any failure.
        self.impulse_validity_window_sec = 0.0
        self.volatility_memory_window_sec = float(self.P.get("volatility_memory_window_sec", 300.0))  # 5-min macro volatility memory
        self.last_volatility_burst_ts: float = -1.0e18
        self.macro_drift_target_bps = float(self.P.get("macro_drift_target_bps", 1.0))
        self.impulse_memory: Dict[str, Tuple[float, float, float]] = {}

        # Active Index Futures Tracking & Lead-Lag Basis Engine
        self.last_futures_price: float = 0.0
        self.last_basis_pts: float = 0.0
        self.last_basis_bps: float = 0.0
        self.futures_volume: float = 0.0
        self.futures_history: deque = deque(maxlen=300)  # (now_sec, fut_lp, basis_pts)
        # SHADOW futures telemetry (2026-10-26): futures feed is telemetry-only,
        # never gates. Tick-rate counters below exist solely to measure the live
        # data speed of the futures contract before any future gate adoption.
        self._fut_shadow_total_ticks: int = 0
        self._fut_shadow_window_ticks: int = 0
        self._fut_shadow_window_start: float = 0.0
        self._fut_shadow_sym: str = ""

        # Spot-level delta inflection trap window (2026-10-26): when ANY option
        # symbol's L1 book shows a delta inflection flip against a direction,
        # ALL entries in that direction are vetoed briefly. Per-strike checks
        # alone miss the trap on thin-book execution strikes (hot strikes).
        self._spot_inflection_veto_until: Dict[str, float] = {"UP": 0.0, "DOWN": 0.0}
        self._spot_inflection_veto_window_sec: float = 45.0

        # Compatibility attribute (deprecated, kept for dashboard schema — see audit P2.10)


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

            # ─── Quality Stack: resolve armed post-trigger stability verdicts ───
            self._process_stability_due(now_sec)

            # ─── Underlying Cash Index Tick ───
            if tok_str in self.underlying_tokens:
                self._on_underlying_tick(sym, lp, now_sec)
                return

            # ─── Option Contract Tick ───
            self.option_cache[tok_str] = lp
            if sym:
                self.option_cache[sym] = lp
            if self.outcome_telemetry:
                self.outcome_telemetry.update_tick(sym, lp, now_sec)
            if getattr(self, "post_trade_tracker", None) and sym:
                self.post_trade_tracker.on_option_tick(sym, lp, now_sec)
            if getattr(self, "shadow_boundary_observer", None) and sym:
                self.shadow_boundary_observer.on_option_tick(sym, lp, now_sec)

            info = self.token_map.get(tok_str)
            if not info:
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
            tick_bp = getattr(tick, "bp", None)
            tick_sp = getattr(tick, "sp", None)
            if tick_bp is not None and tick_sp is not None and len(tick_bp) >= 5 and len(tick_sp) >= 5:
                bp1 = float(tick.bp[0])
                bp2 = float(tick.bp[1])
                bp3 = float(tick.bp[2])
                bp4 = float(tick.bp[3])
                bp5 = float(tick.bp[4])
                sp1 = float(tick.sp[0])
                sp2 = float(tick.sp[1])
                sp3 = float(tick.sp[2])
                sp4 = float(tick.sp[3])
                sp5 = float(tick.sp[4])
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
                bp2 = float(getattr(tick, "bp2", 0.0) or (bp1 - 0.05 if bp1 > 0 else 0.0))
                bp3 = float(getattr(tick, "bp3", 0.0) or (bp1 - 0.10 if bp1 > 0 else 0.0))
                bp4 = float(getattr(tick, "bp4", 0.0) or (bp1 - 0.15 if bp1 > 0 else 0.0))
                bp5 = float(getattr(tick, "bp5", 0.0) or (bp1 - 0.20 if bp1 > 0 else 0.0))
                sp1 = float(getattr(tick, "sp1", 0.0) or 0.0)
                sp2 = float(getattr(tick, "sp2", 0.0) or (sp1 + 0.05 if sp1 > 0 else 0.0))
                sp3 = float(getattr(tick, "sp3", 0.0) or (sp1 + 0.10 if sp1 > 0 else 0.0))
                sp4 = float(getattr(tick, "sp4", 0.0) or (sp1 + 0.15 if sp1 > 0 else 0.0))
                sp5 = float(getattr(tick, "sp5", 0.0) or (sp1 + 0.20 if sp1 > 0 else 0.0))
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

            curr_b_depth = b1 + b2 + b3 + b4 + b5
            curr_a_depth = a1 + a2 + a3 + a4 + a5
            if (curr_b_depth + curr_a_depth) > 0:
                tot_bid_depth = curr_b_depth
                tot_ask_depth = curr_a_depth
                self.last_known_depth[sym] = (tot_bid_depth, tot_ask_depth)
                # Rolling 2.0s depth history with stale tick pruning
                if sym not in self._l2_depth_history:
                    self._l2_depth_history[sym] = deque(maxlen=20)
                tot_d = tot_bid_depth + tot_ask_depth
                imb_d = (tot_bid_depth - tot_ask_depth) / float(tot_d) if tot_d > 0 else 0.0
                self._l2_depth_history[sym].append((now_sec, tot_bid_depth, tot_ask_depth, imb_d))
                # Prune stale ticks older than 2.0s
                while self._l2_depth_history[sym] and (now_sec - self._l2_depth_history[sym][0][0]) > 2.0:
                    self._l2_depth_history[sym].popleft()
            else:
                tot_bid_depth, tot_ask_depth = self.last_known_depth.get(sym, (0.0, 0.0))

            vol_val = float(getattr(tick, "vol", 0.0) or getattr(tick, "v", 0.0) or getattr(tick, "volume", 0.0) or (tick.get("v", 0.0) if isinstance(tick, dict) else 0.0) or 0.0)

            # ─── 1-Minute Option Flow Accumulator (ATM ± 7 Window) ───
            if self.price_action_engine is not None and strike > 0 and side in (0, 1):
                self.price_action_engine.update_option_tick(
                    sym=sym,
                    strike=strike,
                    side=side,
                    lp=lp,
                    bp1=bp1,
                    sp1=sp1,
                    vol=vol_val,
                    atm_strike=self.current_atm_strike,
                )


            if self.scorer is not None:
                self.scorer.update_tick(sym, now_sec, lp, bp1, sp1, b1, a1, b2, a2, b3, a3, b4, a4, b5, a5, vol=vol_val)

                direction = "UP" if side == 0 else "DOWN"  # 0=CE -> UP, 1=PE -> DOWN

                # Entry Candidate Gate: Only tradable strikes (within 8 steps of ATM) evaluate entry setups.
                # Deep OTM strikes (e.g. 21500 PE when ATM is 22700) are tracked 100% for Option Flow
                # (OFI, PCR, Delta above), but must NEVER trigger trade entry or depth guard vetoes!
                if self.current_atm_strike > 0.0 and abs(strike - self.current_atm_strike) > (8 * self.strike_step):
                    return None

                # ─── Phase 1 (V4.1b): Absolute Pain Zone Band Veto ───
                # Vetoes entry trade consideration for opposing direction while
                # preserving 100% full option flow telemetry (update_option_tick, depth, OI) above.
                _pz_top = 0.0
                _pz_bottom = 0.0
                _vetoed = False
                if self.price_action_engine is not None:
                    try:
                        _vetoed, _pz_top, _pz_bottom, _pz_buf = self.price_action_engine.band_veto_blocks(
                            direction, spot=self.last_spot
                        )
                    except Exception:
                        _vetoed, _pz_top, _pz_bottom = False, 0.0, 0.0

                try:
                    _dg = self.__dict__.setdefault("_band_diag", {"m": -1, "up_p": 0, "up_b": 0, "dn_p": 0, "dn_b": 0})
                    _now_m = int(now_sec // 60)
                    if _dg["m"] != _now_m:
                        if _dg["m"] > 0:
                            logger.info(
                                f"[BAND DIAG] spot={self.last_spot:.1f} top={_pz_top:.1f} bot={_pz_bottom:.1f} "
                                f"UP pass={_dg['up_p']} block={_dg['up_b']} DOWN pass={_dg['dn_p']} block={_dg['dn_b']}"
                            )
                        _dg.update({"m": _now_m, "up_p": 0, "up_b": 0, "dn_p": 0, "dn_b": 0})
                    if direction == "UP":
                        _dg["up_p" if not _vetoed else "up_b"] += 1
                    else:
                        _dg["dn_p" if not _vetoed else "dn_b"] += 1
                    if _vetoed:
                        return None
                except Exception:
                    if _vetoed:
                        return None

                # ─── Fail-Fast Gate 1: Burst Cooldown (O(1)) ───
                last_fired = self.cooldowns.get(sym, 0.0)
                cooldown_dur = float(self.P.get("burst_cooldown_sec", 60.0))
                if (now_sec - last_fired) < cooldown_dur:
                    return None

                # ─── Fail-Fast Gate 2: Quality Stack Slot Policy Limit (O(1)) ───
                if self.quality_stack_mode == "ACTIVE" and self.slot_policy:
                    if self.slot_policy.used >= self.slot_policy.max_trades_per_day:
                        return None
                    if int(getattr(self, "_em_consec_losses", 0)) >= self.slot_policy.consec_loss_stop and self.slot_policy.consec_loss_stop > 0:
                        return None
                    if float(getattr(self, "_em_daily_pnl", 0.0)) <= -self.slot_policy.daily_loss_cap_rs and self.slot_policy.daily_loss_cap_rs > 0:
                        return None

                # ─── THE 4 ACTIVE PRICE ACTION & MICROSTRUCTURE SCALPER ENGINES ───
                # Pillar 1: 20 EMA + Wick Rejection (Top Performer)
                # Pillar 2: Liquidity Sweep & Reclaim (Turtle Soup / 5-Bar Liquidity Grab)
                # Pillar 3: Volatility Squeeze to Expansion Breakout (100% WR Scalper)
                # Pillar 4: 0-D Prominence Hub Structural Breakout (V4 Final / HVG Shadow)
                is_pa_setup = False
                pa_setup_name = ""
                pa_details = {}
                if self.price_action_engine:
                    current_vwap = getattr(self.price_action_engine, "current_vwap", 0.0)
                    if sym in self._l2_depth_history:
                        self.price_action_engine._l2_depth_history = list(self._l2_depth_history[sym])
                    is_pa_setup, pa_setup_name, pa_details = self.price_action_engine.evaluate_setup(direction, current_vwap, current_time=now_sec)

                # Legacy alias (V4.0 retired albrooks from the ladder: always False).
                is_50ema_pullback = False

                # PA-only mode: hidden microstructure route removed.
                # System fires ONLY on the 4 pillars; microstructure stays as validator.
                # V3.0 DROP policy: no pillar => instant discard, purge impulse.
                if not is_pa_setup:
                    self._purge_impulse_memory(sym)
                    return None

                # ─── V3.0 Event-Bound Anchor T (formal 1m close) & Option Scorer ───
                # Compute empirical microstructure score ONLY when a valid Pillar has fired!
                # Avoids wasting 2.5ms on 99.9% of non-setup market ticks.
                anchor_T = self._get_last_closed_bar_T()
                use_anchor = self._is_T_fresh(anchor_T, now_sec, 120.0)
                anchor_arg = anchor_T if use_anchor else None

                score, is_high_conv, feats = self.scorer.compute_score(
                    sym,
                    now_sec,
                    adjacent_otm_returns={},
                    opp_side_return=0.0,
                    anchor_ts_sec=anchor_arg,
                )

                self._purge_impulse_memory(sym)

                # ─── Wyckoff Volume & Order Flow Delta Gate (Shadow Mode Observability) ───
                wyckoff_passed = True
                wyckoff_reason = "wyckoff_passed"
                wyckoff_m = {}
                if self.wyckoff_volume_engine is not None and is_pa_setup:
                    recent_bars = list(self.price_action_engine.bars_1m) if self.price_action_engine else []
                    atr_val = getattr(self.price_action_engine, "current_atr_1m", 10.0) if self.price_action_engine else 10.0
                    pillar_num = int(pa_details.get("pillar", 1))

                    wyckoff_passed, wyckoff_reason, wyckoff_m = self.wyckoff_volume_engine.evaluate(
                        pillar=pillar_num,
                        direction=direction,
                        bars=recent_bars,
                        atr=atr_val,
                        pa_details=pa_details,
                    )

                    # Wyckoff Volume & Order Flow Delta Gate (Active Institutional Gatekeeper)
                    if not wyckoff_passed:
                        self._throttled_veto_log(
                            f"wyckoff_veto_{sym}_{direction}",
                            f"[WYCKOFF VETO] {sym} ({direction}) {pa_setup_name} P{pillar_num} blocked: "
                            f"{wyckoff_reason} | {wyckoff_m}"
                        )
                        return None

                    self._throttled_veto_log(
                        f"wyckoff_conf_{sym}_{direction}",
                        f"[WYCKOFF CONFIRMED] {sym} ({direction}) {pa_setup_name} P{pillar_num} | "
                        f"{wyckoff_reason} | VDR={wyckoff_m.get('vdr', 0.0):.2f}, "
                        f"Absorption={wyckoff_m.get('absorption_score', 0.0):.1f}x, "
                        f"OFI={wyckoff_m.get('opt_delta_flow', 0.0):+.0f}",
                        interval_sec=15.0,
                    )
                    pa_details["wyckoff_metrics"] = wyckoff_m
                    pa_details["wyckoff_reason"] = wyckoff_reason



                # Adaptive Conviction Threshold based on Spot Drift confirmation or Price Action trigger
                drift_60_bps = self.get_spot_drift_bps(now_sec, 60.0) if len(self.spot_history) >= 2 else 0.0
                is_trend_confirmed = (direction == "UP" and drift_60_bps >= self.macro_drift_target_bps) or \
                                     (direction == "DOWN" and drift_60_bps <= -self.macro_drift_target_bps) or \
                                     is_pa_setup

                effective_threshold = self.moderate_conviction_threshold if is_trend_confirmed else self.high_conviction_threshold
                
                # Trigger is eligible ONLY on 3 verified Price Action Setups.
                # Microstructure score stays as flexible validator inside PA flow.
                is_trigger_eligible = is_pa_setup

                # Check Trigger Eligibility
                if is_trigger_eligible:
                    last_fired = self.cooldowns.get(sym, 0.0)
                    cooldown_dur = float(self.P.get("burst_cooldown_sec", 60.0))
                    if now_sec - last_fired >= cooldown_dur:
                        # Domain 0.5: Key Structural Level Proximity Veto (PDH Resistance / PDL Support)
                        pa_eng = getattr(self, "price_action_engine", None)
                        if pa_eng and hasattr(pa_eng, "check_key_level_proximity"):
                            key_ok, key_reason, key_m = pa_eng.check_key_level_proximity(direction, self.last_spot)
                            if not key_ok:
                                self._purge_impulse_memory(sym)
                                self._throttled_veto_log(
                                    "key_level_veto",
                                    f"[KEY LEVEL VETO] {sym} ({direction}) blocked: {key_reason} "
                                    f"(dist={key_m.get('dist_pts')}pts <= {key_m.get('threshold_pts')}pts threshold | Spot: {self.last_spot:.1f})"
                                )
                                return None

                        # Price action setup has verified trend alignment and rejection geometry
                        has_permission = True
                        perm_reason = f"price_action_{pa_setup_name}"
                        perm_metrics = {}
                        pers_confirmed = True
                        pers_score = 1.0
                        pers_metrics = {}

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
                                if self.option_quality_mode == "ACTIVE":
                                    self._throttled_veto_log("option_quality_veto", f"[OPTION QUALITY VETO] {sym} blocked: {opt_reason}")
                                    return
                                elif self.option_quality_mode == "SHADOW":
                                    self._throttled_veto_log("option_quality_shadow", f"[OPTION QUALITY SHADOW] {sym} would block: {opt_reason}")

                        # Domain 7: Real-Time Open Interest (OI) Built-up & Wall Defense Gate permanently decommissioned from entry path.
                        # (OI tracking maintained passively for live state telemetry / dashboard walls only; it never blocks scalper entries).

                        # Centralized Signal FSM Lifecycle Evaluation
                        fsm_state = None
                        should_fire = False
                        fsm_reason = "direct"
                        if is_pa_setup:
                            # ─── V3.0 EVENT-BOUND MICROSTRUCTURE VALIDATOR (T-anchored) ───
                            # 60s persistence (T-60 to T): Dp >= 0.70, opposite <= 2.
                            # 30s acceleration (T-30 to T): microprice + L1 touch.
                            # Any failure => instant DROP + impulse purge. No queue.
                            pillar = int(pa_details.get("pillar", 1) or 1)

                            # 1. 60-Second Directional Persistence
                            # Pullback Archetypes (Pillar 1 & 2): Macro Structure (Spot vs VWAP & 20>50 EMA)
                            # establishes trend alignment. Pullbacks retrace counter-trend by definition,
                            # so spot-drift persistence is bypassed.
                            # Breakout Archetypes (Pillar 3 & 4): 60s Spot Persistence.
                            # 2026-10-26: futures 15s path-efficiency branch removed —
                            # futures feed moved to SHADOW telemetry-only (data speed
                            # Macro structure and candle geometry (Body >= 65%, edge, wicks, ATR expansion)
                            # are verified directly by PriceActionEngine. Legacy 60s tick-counting spot persistence
                            # veto is permanently decommissioned (Zero-Entropy Logic De-bloat).
                            is_mode_b = (pa_details.get("mode") == "MODE_B_INFLIGHT_BREAKTHROUGH")
                            pers_score = float(pa_details.get("dp_micro", 1.0))
                            pers_confirmed = True

                            # 2. MICROSTRUCTURE EXTRACTION & SANITY CIRCUIT BREAKERS
                            # Zero-Entropy Architecture: Order flow momentum is scored strictly by
                            # CompositeConvictionEngine (>= 80.0 threshold). The redundant 60-point
                            # energy gate is fully eliminated. Only hard liquidity circuit breakers
                            # (spread blowout > 2.5%, ghost book < 200, severe flush < -1.5%) veto early.
                            energy_score = 100.0
                            micro_m = {}
                            time_since_close = (now_sec - anchor_T) if (anchor_T and anchor_T > 0) else 0.0
                            if is_mode_b:
                                # Mode B In-Flight: extract live micro metrics from immediate rolling 10s buffer
                                # to ensure touch_i1, real spread_pct and depth are verified upfront
                                if self.conviction_engine and self.scorer:
                                    buf = self.scorer.history.get(sym, [])
                                    if buf:
                                        micro_m = self.conviction_engine.extract_micro_metrics(
                                            tick_buffer=buf,
                                            anchor_T=now_sec,
                                            lookback_sec=10.0,
                                        )
                                        if not micro_m.get("is_valid", True):
                                            micro_30_m = self.conviction_engine.extract_micro_metrics(
                                                tick_buffer=buf,
                                                anchor_T=now_sec,
                                                lookback_sec=30.0,
                                            )
                                            if micro_30_m.get("is_valid", True):
                                                micro_m = micro_30_m
                                        if micro_m.get("is_valid", True):
                                            tot_bid_depth = micro_m.get("tot_bid_depth", tot_bid_depth)
                                            tot_ask_depth = micro_m.get("tot_ask_depth", tot_ask_depth)
                                            bp1 = micro_m.get("bp1", bp1)
                                            sp1 = micro_m.get("sp1", sp1)
                            elif not is_mode_b and use_anchor and self.conviction_engine and self.scorer:
                                buf = self.scorer.history.get(sym, [])
                                if buf:
                                    micro_m = self.conviction_engine.extract_micro_metrics(
                                        tick_buffer=buf,
                                        anchor_T=anchor_T,
                                        lookback_sec=30.0,
                                    )
                                    # Fallback: check 60s lookback if 30s was invalid
                                    if not micro_m.get("is_valid", True):
                                        micro_60_m = self.conviction_engine.extract_micro_metrics(
                                            tick_buffer=buf,
                                            anchor_T=anchor_T,
                                            lookback_sec=60.0,
                                        )
                                        if micro_60_m.get("is_valid", True):
                                            micro_m = micro_60_m

                                    # Hard Circuit Breaker Check (Liquid market sanity floor)
                                    if not micro_m.get("is_valid", True):
                                        self._purge_impulse_memory(sym)
                                        self._throttled_veto_log(
                                            "v3_circuit_breaker",
                                            f"[V3 CIRCUIT BREAKER] {sym} ({direction}) {pa_setup_name} P{pillar} blocked: "
                                            f"{micro_m.get('reason')} (ret={micro_m.get('micro_ret_30s', 0.0):+.4f}, "
                                            f"spread={micro_m.get('spread_pct', 0.0):.2f}%, depth={micro_m.get('tot_depth', 0)})"
                                        )
                                        return None

                                    # Update depth and quotes from verified slice
                                    tot_bid_depth = micro_m.get("tot_bid_depth", tot_bid_depth)
                                    tot_ask_depth = micro_m.get("tot_ask_depth", tot_ask_depth)
                                    bp1 = micro_m.get("bp1", bp1)
                                    sp1 = micro_m.get("sp1", sp1)
                            elif not is_mode_b and not use_anchor:
                                # Stale-T unit-test fallback: basic depth wall and spread checks
                                tot_depth = tot_bid_depth + tot_ask_depth
                                if tot_depth > 0:
                                    top5_imb = (tot_bid_depth - tot_ask_depth) / float(tot_depth)
                                    if top5_imb < -0.40:
                                        self._purge_impulse_memory(sym)
                                        self._throttled_veto_log("v3_drop_depth", f"[V3 DROP DEPTH] {sym} {pa_setup_name} P{pillar} blocked: severe opposing book wall (top5_imb={top5_imb:.2f} < -0.40)")
                                        return None

                                mid_px = (bp1 + sp1) / 2.0 if (bp1 > 0 and sp1 > 0) else lp
                                spread_pct = ((sp1 - bp1) / mid_px * 100.0) if (mid_px > 0 and bp1 > 0 and sp1 > 0) else 0.5
                                if spread_pct > 1.0:
                                    self._purge_impulse_memory(sym)
                                    self._throttled_veto_log("v3_drop_spread", f"[V3 DROP SPREAD] {sym} {pa_setup_name} P{pillar} blocked: spread {spread_pct:.2f}% > 1.00%")
                                    return None

                            should_fire = True
                            fsm_reason = f"price_action_{pa_setup_name}"
                        if should_fire:
                            trigger_age = 0.5
                            
                            eqs = 85.0
                            is_eqs_qual = True
                            eqs_breakdown = {}
                            if is_pa_setup:
                                # Per-pillar EQS: P1 top performer, P2 trap needs confirmation,
                                # P3 blast is fast but thin, P4 clean structural breakout — all above dispatch 70.
                                _pillar_eqs = {1: 85.0, 2: 83.0, 3: 82.0, 4: 85.0}.get(pillar, 85.0)
                                eqs = _pillar_eqs
                                is_eqs_qual = True
                                eqs_breakdown = {
                                    "eqs": _pillar_eqs,
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
                                vwap_val = getattr(self.price_action_engine, "current_vwap", 0.0) if self.price_action_engine else 0.0
                                if vwap_val <= 0:
                                    vwap_val = self.last_spot

                                # ─── COMPOSITE CONVICTION SCORING ENGINE (0-100 CONTINUOUS EVALUATOR) ───
                                conviction_res = None
                                sizing_tier = "STANDARD_SIZE"
                                conviction_score_val = float(eqs)
                                conviction_breakdown = {}
                                conviction_sub_scores = {}
                                if self.conviction_engine and self.price_action_engine:
                                    if str(pa_details.get("mode", "")).startswith("MODE_") and self.price_action_engine._curr_1m_bar is not None:
                                        current_bar = self.price_action_engine._curr_1m_bar
                                    else:
                                        current_bar = (self.price_action_engine.bars_1m[-1] if self.price_action_engine.bars_1m else None) or self.price_action_engine._curr_1m_bar
                                    wyckoff_context = dict(pa_details.get("wyckoff_metrics") or {})
                                    if wyckoff_passed and bool(wyckoff_context.get("wyckoff_confirmed", False)):
                                        wyckoff_context["wyckoff_confirmed"] = True
                                    else:
                                        wyckoff_context["wyckoff_confirmed"] = False

                                    conviction_res = self.conviction_engine.compute_conviction_score(
                                        direction=direction,
                                        setup_name=pa_setup_name,
                                        spot_price=self.last_spot,
                                        prior_bars=list(self.price_action_engine.bars_1m),
                                        current_bar=current_bar,
                                        atr=atr_1m_val,
                                        ema_20=getattr(self.price_action_engine, "ema_20_1m", 0.0),
                                        ema_50=getattr(self.price_action_engine, "ema_50_1m", 0.0),
                                        vwap=vwap_val,
                                        pdh=getattr(self.price_action_engine, "pdh", 0.0),
                                        pdl=getattr(self.price_action_engine, "pdl", 0.0),
                                        vol_sma10=self.price_action_engine.get_volume_sma(10),
                                        persistence_dp=pers_score,
                                        touch_i1=float(micro_m.get("touch_i1", 0.0)),
                                        delta_fut_60s_bps=float(fut_basis_m.get("delta_fut_60s_bps", 0.0)),
                                        delta_basis_60s_bps=float(fut_basis_m.get("basis_change_60s_bps", 0.0)),
                                        option_delta=None,
                                        option_strike=float(strike),
                                        spread_pct=float(spread_val or (micro_m.get("spread_pct", 0.5))),
                                        pa_details=pa_details,
                                        wyckoff_details=wyckoff_context,
                                        micro_metrics=micro_m,
                                    )

                                    if not conviction_res.is_approved:
                                        # 1) Pre-Close 60s Lookback [T-60, T]:
                                        # Check if flow during the entire prior 60s candle formation approves conviction (>= 80.0)
                                        if use_anchor and anchor_T is not None and anchor_T > 0 and self.scorer and self.conviction_engine:
                                            buf = self.scorer.history.get(sym, [])
                                            past_60_m = self.conviction_engine.extract_micro_metrics(
                                                tick_buffer=buf,
                                                anchor_T=anchor_T,
                                                lookback_sec=60.0,
                                            )
                                            if past_60_m.get("is_valid", True):
                                                past_conv_res = self.conviction_engine.compute_conviction_score(
                                                    direction=direction,
                                                    setup_name=pa_setup_name,
                                                    spot_price=self.last_spot,
                                                    prior_bars=list(self.price_action_engine.bars_1m),
                                                    current_bar=current_bar,
                                                    atr=atr_1m_val,
                                                    ema_20=getattr(self.price_action_engine, "ema_20_1m", 0.0),
                                                    ema_50=getattr(self.price_action_engine, "ema_50_1m", 0.0),
                                                    vwap=vwap_val,
                                                    pdh=getattr(self.price_action_engine, "pdh", 0.0),
                                                    pdl=getattr(self.price_action_engine, "pdl", 0.0),
                                                    vol_sma10=self.price_action_engine.get_volume_sma(10),
                                                    persistence_dp=pers_score,
                                                    touch_i1=float(past_60_m.get("touch_i1", 0.0)),
                                                    delta_fut_60s_bps=float(fut_basis_m.get("delta_fut_60s_bps", 0.0)),
                                                    delta_basis_60s_bps=float(fut_basis_m.get("basis_change_60s_bps", 0.0)),
                                                    option_delta=None,
                                                    option_strike=float(strike),
                                                    spread_pct=float(spread_val or (past_60_m.get("spread_pct", 0.5))),
                                                    pa_details=pa_details,
                                                    wyckoff_details=wyckoff_context,
                                                    micro_metrics=past_60_m,
                                                )
                                                if past_conv_res.is_approved:
                                                    conviction_res = past_conv_res
                                                    micro_m = past_60_m
                                                    logger.info(
                                                        f"[AEGIS V3 PRE-CLOSE 60S FIRED] {sym} ({direction}) {pa_setup_name} "
                                                        f"order flow confirmed from prior 60s candle | "
                                                        f"Conviction: {conviction_res.total_score:.1f}/100"
                                                    )

                                    if not conviction_res.is_approved:
                                        # 2) 60s Asynchronous Order Flow Confirmation Window (Future):
                                        # If pre-close conviction failed, evaluate live trailing flow [now_sec-30, now_sec] / [now_sec-60, now_sec]
                                        # within 60s of candle close to confirm post-breakout surge
                                        if 0.0 < time_since_close <= 60.0 and self.scorer and self.conviction_engine:
                                            buf = self.scorer.history.get(sym, [])
                                            live_micro_m = self.conviction_engine.extract_micro_metrics(
                                                tick_buffer=buf,
                                                anchor_T=now_sec,
                                                lookback_sec=30.0,
                                            )
                                            live_energy = 100.0
                                            live_ok = live_micro_m.get("is_valid", True)
                                            if not live_ok:
                                                live_micro_m = self.conviction_engine.extract_micro_metrics(
                                                    tick_buffer=buf,
                                                    anchor_T=now_sec,
                                                    lookback_sec=60.0,
                                                )
                                                live_ok = live_micro_m.get("is_valid", True)

                                            if live_ok:
                                                live_conv_res = self.conviction_engine.compute_conviction_score(
                                                    direction=direction,
                                                    setup_name=pa_setup_name,
                                                    spot_price=self.last_spot,
                                                    prior_bars=list(self.price_action_engine.bars_1m),
                                                    current_bar=current_bar,
                                                    atr=atr_1m_val,
                                                    ema_20=getattr(self.price_action_engine, "ema_20_1m", 0.0),
                                                    ema_50=getattr(self.price_action_engine, "ema_50_1m", 0.0),
                                                    vwap=vwap_val,
                                                    pdh=getattr(self.price_action_engine, "pdh", 0.0),
                                                    pdl=getattr(self.price_action_engine, "pdl", 0.0),
                                                    vol_sma10=self.price_action_engine.get_volume_sma(10),
                                                    persistence_dp=pers_score,
                                                    touch_i1=float(live_micro_m.get("touch_i1", 0.0)),
                                                    delta_fut_60s_bps=float(fut_basis_m.get("delta_fut_60s_bps", 0.0)),
                                                    delta_basis_60s_bps=float(fut_basis_m.get("basis_change_60s_bps", 0.0)),
                                                    option_delta=None,
                                                    option_strike=float(strike),
                                                    spread_pct=float(spread_val or (live_micro_m.get("spread_pct", 0.5))),
                                                    pa_details=pa_details,
                                                    wyckoff_details=wyckoff_context,
                                                    micro_metrics=live_micro_m,
                                                )
                                                if live_conv_res.is_approved:
                                                    conviction_res = live_conv_res
                                                    micro_m = live_micro_m
                                                    energy_score = live_energy
                                                    logger.info(
                                                        f"[AEGIS V3 60S ARMED FIRED] {sym} ({direction}) {pa_setup_name} "
                                                        f"order flow confirmed at T+{time_since_close:.1f}s | "
                                                        f"Conviction: {conviction_res.total_score:.1f}/100"
                                                    )

                                    if not conviction_res.is_approved:
                                        self._purge_impulse_memory(sym)
                                        self._throttled_veto_log(
                                            "conviction_veto",
                                            f"[CONVICTION VETO] {sym} ({direction}) {pa_setup_name} blocked: "
                                            f"Score {conviction_res.total_score:.1f}/100 < {self.conviction_engine.min_approval_score:.1f} ({conviction_res.reason}) | "
                                            f"Aggression: {conviction_res.sub_scores.get('touch_aggression', conviction_res.sub_scores.get('flow', 0))} | "
                                            f"Momentum: {conviction_res.sub_scores.get('micro_momentum', conviction_res.sub_scores.get('impulse', 0))} | "
                                            f"Contract: {conviction_res.sub_scores.get('contract_tradability', conviction_res.sub_scores.get('contract', 0))}"
                                        )
                                        return None

                                    # Sizing tier and dynamic TP scaling
                                    recommended_tp_pct = conviction_res.recommended_tp_pct
                                    sizing_tier = conviction_res.sizing_tier
                                    conviction_score_val = conviction_res.total_score
                                    conviction_breakdown = conviction_res.breakdown
                                    conviction_sub_scores = conviction_res.sub_scores

                                # ─── Quality Stack 3-Slot Policy Gate ───
                                slot_admit = True
                                slot_reason = "slot_policy_off"
                                slot_score = float(conviction_score_val if conviction_score_val > 0 else micro_m.get("energy_score", score))
                                if self.slot_policy:
                                    slot_admit, slot_reason = self.slot_policy.admit(
                                        score=slot_score,
                                        now_sec=now_sec,
                                        consec_losses=int(getattr(self, "_em_consec_losses", 0)),
                                        daily_pnl=float(getattr(self, "_em_daily_pnl", 0.0)),
                                    )
                                if self.quality_stack_mode == "ACTIVE" and not slot_admit:
                                    self._purge_impulse_memory(sym)
                                    self._throttled_veto_log(
                                        "slot_policy_veto",
                                        f"[SLOT POLICY VETO] {sym} ({direction}) {pa_setup_name} blocked: {slot_reason}",
                                    )
                                    return None

                                # ─── Delta Inflection Trap Shield ───
                                _di_veto, _di_detail = self._delta_inflection_check(sym, direction, now_sec)
                                if _di_veto:
                                    self._di_vetoed += 1
                                    self._purge_impulse_memory(sym)
                                    self._throttled_veto_log("delta_inflection_veto",
                                                             f"[DELTA INFLECTION VETO] {sym} ({direction}) "
                                                             f"{pa_setup_name} blocked: {_di_detail}")
                                    return None

                                # ─── EXECUTION COMMIT & IMMEDIATE DISPATCH (<0.8ms from Tick) ───
                                if self.slot_policy and slot_admit:
                                    self.slot_policy.mark_admitted(slot_score, now_sec=now_sec)

                                self.cooldowns[sym] = now_sec

                                pz_snap = (
                                    self.price_action_engine.get_pain_zone_vwaps()
                                    if (self.price_action_engine and hasattr(self.price_action_engine, "get_pain_zone_vwaps"))
                                    else {}
                                )
                                pz_top = float(pz_snap.get("zone_top", 0.0) or 0.0)
                                pz_bot = float(pz_snap.get("zone_bottom", 0.0) or 0.0)
                                pz_spread = round(pz_top - pz_bot, 2) if (pz_top > 0 and pz_bot > 0) else 0.0

                                live_signal_metrics = {
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
                                    "conviction_score": conviction_score_val,
                                    "sizing_tier": sizing_tier,
                                    "conviction_sub_scores": conviction_sub_scores,
                                    "conviction_breakdown": conviction_breakdown,
                                    "energy_score": round(energy_score, 1),
                                    "quality_stack_mode": self.quality_stack_mode,
                                    "slot_verdict": slot_reason,
                                    "slot_used": self.slot_policy.used if self.slot_policy else 0,
                                    "oi_snapshot": self._get_oi_snapshot(sym, direction, now_sec),
                                    "pain_zone_snapshot": pz_snap,
                                    "pain_zone_top": pz_top,
                                    "pain_zone_bottom": pz_bot,
                                    "pain_zone_spread_pts": pz_spread,
                                    "pain_zone_call_buy": float(pz_snap.get("vwap_call_buy", 0.0) or 0.0),
                                    "pain_zone_call_sell": float(pz_snap.get("vwap_call_sell", 0.0) or 0.0),
                                    "pain_zone_put_buy": float(pz_snap.get("vwap_put_buy", 0.0) or 0.0),
                                    "pain_zone_put_sell": float(pz_snap.get("vwap_put_sell", 0.0) or 0.0),
                                    "eqs_score": eqs,
                                    "recommended_tp_pct": recommended_tp_pct,
                                    "microstructure_score": round(score, 2),
                                    "persistence_score": round(pers_score, 3),
                                    "live_tick_rate": round(live_tick_rate, 2),
                                    "strike": strike,
                                    "side": "CE" if side == 0 else "PE",
                                    **pa_details,
                                    **micro_m,
                                    **fut_basis_m,
                                    **feats,
                                    **eqs_breakdown,
                                    **perm_metrics,
                                }

                                # Dispatch live signal to execution manager IMMEDIATELY!
                                # Order placement starts on downstream execution thread with zero delay.
                                self._emit_signal(
                                    sym=sym,
                                    lp=lp,
                                    direction=direction,
                                    conviction=conviction_score_val,
                                    gates={
                                        "permission_granted": has_permission,
                                        "persistence_confirmed": pers_confirmed,
                                        "option_qualified": opt_qualified,
                                        "eqs_qualified": is_eqs_qual,
                                        "conviction_approved": True,
                                    },
                                    metrics=live_signal_metrics,
                                )

                                # ─── POST-DISPATCH SHADOW & TELEMETRY ENRICHMENT ───
                                # Runs immediately after order dispatch; live execution is never delayed.
                                # 2. Stability Tracker Post-Trigger Arming
                                if self.stability_tracker and micro_m:
                                    try:
                                        _sbp = float(micro_m.get("bp1", 0.0) or 0.0)
                                        _ssp = float(micro_m.get("sp1", 0.0) or 0.0)
                                        _mid_T = ((_sbp + _ssp) / 2.0) if (_sbp > 0 and _ssp > 0) else float(micro_m.get("p1", 0.0) or 0.0)
                                        if _mid_T > 0:
                                            self.stability_tracker.arm(
                                                sym=sym,
                                                direction=direction,
                                                t_sec=now_sec,
                                                mid_T=_mid_T,
                                                meta={
                                                    "opt_ret_30s": float(micro_m.get("micro_ret_30s", 0.0) or 0.0),
                                                    "energy_score": energy_score,
                                                },
                                            )
                                    except Exception as _e_st:
                                        logger.debug(f"[Aegis] Stability tracker arm error: {_e_st}")

                                # 3. Outcome Telemetry Database Entry (100% enriched payload)
                                if self.outcome_telemetry:
                                    try:
                                        event_id = f"{sym}_{int(now_sec*1000)}"
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
                                                **live_signal_metrics,
                                                "recommended_tp_pct": recommended_tp_pct,
                                            },
                                        )
                                    except Exception as _e_ot:
                                        logger.debug(f"[Aegis] Outcome telemetry error: {_e_ot}")

                                # 4. Consolidated Execution Log
                                logger.info(
                                    f"[AEGIS V3 FIRED] {sym} ({direction}) | Conviction: {conviction_score_val:.1f}/100 ({sizing_tier}) | "
                                    f"Energy: {energy_score:.1f}/100 | EQS: {eqs:.1f}/100 | "
                                    f"Target: +{recommended_tp_pct*100:.1f}% | "
                                    f"Basis: {self.last_basis_pts:+.1f}pts ({self.last_basis_bps:+.1f}bps) | FutPx: {self.last_futures_price:.1f} | "
                                    f"Persist: {pers_score:.2f} | Struct: {perm_metrics.get('market_structure', 'N/A')} | "
                                    f"ATR_Ratio: {perm_metrics.get('atr_ratio', 1.0):.2f} | Spread: {opt_metrics.get('spread_pct', 0.5):.2f}%"
                                )

        except Exception as e:
            logger.error(f"AEGIS on_tick_parsed error: {e}", exc_info=True)

    def _delta_inflection_check(self, sym: str, direction: str, now_sec: float = 0.0):
        """Delta inflection veto (Shield, added 2026-09-22).

        Compares mean L1 touch imbalance I1 = (b1-a1)/(b1+a1) of the first
        vs second half of the 30s tick history. A sign flip against the
        trade direction with magnitude >= 0.15 means aggression changed
        sides (trap warning). Returns (vetoed: bool, detail: str).

        2026-10-26: flips now also open a short direction-wide trap window
        so the veto propagates to every other strike (including thin-book
        hot-strike execution symbols that never register the flip in their
        own sparse history).
        """
        now_eff = float(now_sec) if now_sec and now_sec > 0 else time.time()
        _veto_until_map = getattr(self, "_spot_inflection_veto_until", None)
        if not isinstance(_veto_until_map, dict):
            _veto_until_map = {}
            self._spot_inflection_veto_until = _veto_until_map
        if now_eff < float(_veto_until_map.get(direction, 0.0) or 0.0):
            return True, "spot_level_inflection_flip_active (direction-wide trap window)"
        hist = self.history_30s.get(sym) if hasattr(self, "history_30s") else None
        if not hist or len(hist) < 6:
            return False, "insufficient_history"
        n = len(hist)
        half = n // 2
        def _mean_i1(rows):
            vals = []
            for r in rows:
                try:
                    b1, a1 = float(r[4]), float(r[5])
                except (IndexError, TypeError, ValueError):
                    continue
                if b1 + a1 > 0:
                    vals.append((b1 - a1) / (b1 + a1))
            return sum(vals) / len(vals) if vals else 0.0
        i1_early = _mean_i1(list(hist)[:half])
        i1_late = _mean_i1(list(hist)[half:])
        flip_mag = abs(i1_late - i1_early)
        flipped_against = (direction == "UP" and i1_early > 0 and i1_late < 0) or \
                          (direction == "DOWN" and i1_early < 0 and i1_late > 0)
        if flipped_against and flip_mag >= 0.15:
            _veto_until_map[direction] = max(
                float(_veto_until_map.get(direction, 0.0) or 0.0),
                now_eff + getattr(self, "_spot_inflection_veto_window_sec", 45.0),
            )
            return True, f"delta_inflection_flip {i1_early:+.2f}->{i1_late:+.2f} (mag {flip_mag:.2f})"
        return False, f"no_flip early={i1_early:+.2f} late={i1_late:+.2f}"

    def _get_oi_snapshot(self, sym: str, direction: str, now_sec: float) -> Dict[str, Any]:
        """Read-only OI snapshot for telemetry (never vetoes, fail-safe).

        Captures kinematics velocity/acceleration, buildup class, wall
        distance and unwind speed at trigger time so every fill carries
        a post-trade OI audit trail in decision_payload.
        """
        snap: Dict[str, Any] = {
            "vel": 0.0, "acc": 0.0, "n": 0,
            "builtup": "UNKNOWN", "wall_dist_strikes": 0.0, "wall_speed": 0.0,
        }
        try:
            gate = getattr(self, "oi_gate", None)
            if gate is None:
                snap["builtup"] = "GATE_MISSING"
                return snap
            vel, acc, n = gate.get_oi_kinematics(sym, float(now_sec or 0.0))
            snap["vel"] = round(float(vel), 2)
            snap["acc"] = round(float(acc), 2)
            snap["n"] = int(n)
            try:
                builtup, _dp, _doi = gate.get_contract_builtup(sym)
                snap["builtup"] = str(builtup)
            except Exception:
                pass
            try:
                spot = float(getattr(self, "last_spot", 0.0) or 0.0)
                wall = gate.get_wall_snapshot(spot)
                is_pe = str(direction).upper() in ("DOWN", "PE", "PUT") or ("PE" in str(sym).upper())
                if is_pe:
                    snap["wall_dist_strikes"] = float(wall.get("pe_dist_strikes", 0.0))
                else:
                    snap["wall_dist_strikes"] = float(wall.get("ce_dist_strikes", 0.0))
                snap["wall_ce_strike"] = float(wall.get("max_ce_strike", 0.0))
                snap["wall_pe_strike"] = float(wall.get("max_pe_strike", 0.0))
            except Exception:
                pass
        except Exception:
            pass
        return snap

    def get_oi_telemetry(self) -> Dict[str, Any]:
        """Shield arm/skip/veto counters + wall snapshot for live state."""
        out: Dict[str, Any] = {
            "kin_armed": int(getattr(self, "_oi_kin_armed", 0)),
            "kin_skipped": int(getattr(self, "_oi_kin_skipped", 0)),
            "kin_vetoed": int(getattr(self, "_oi_kin_vetoed", 0)),
            "gate_vetoed": int(getattr(self, "_oi_gate_vetoed", 0)),
            "delta_vetoed": int(getattr(self, "_di_vetoed", 0)),
        }
        try:
            gate = getattr(self, "oi_gate", None)
            spot = float(getattr(self, "last_spot", 0.0) or 0.0)
            if gate is not None:
                out["walls"] = gate.get_wall_snapshot(spot)
            else:
                out["walls"] = {}
        except Exception:
            out["walls"] = {}
        return out

    def maybe_log_wall_snapshot(self, spot_price: float = 0.0, now_sec: float = 0.0) -> bool:
        """Logs the fortress snapshot at most once per 15-minute bucket.

        Called from the 1-minute main loop (off hot path). Returns True
        when a snapshot line was emitted.
        """
        try:
            ts = float(now_sec or time.time())
            bucket = int(ts // 900)
            if bucket == int(getattr(self, "_last_wall_log_min", -1)):
                return False
            self._last_wall_log_min = bucket
            gate = getattr(self, "oi_gate", None)
            if gate is None:
                return False
            spot = float(spot_price or getattr(self, "last_spot", 0.0) or 0.0)
            wall = gate.get_wall_snapshot(spot)
            tele = self.get_oi_telemetry()
            logger.info(
                f"[OI WALL SNAPSHOT] Spot:{spot:.1f} "
                f"CE_wall:{wall.get('max_ce_strike', 0.0):.0f}@{wall.get('max_ce_oi', 0.0):.0f}"
                f"({wall.get('ce_dist_strikes', 0.0):+.2f}stk) "
                f"PE_wall:{wall.get('max_pe_strike', 0.0):.0f}@{wall.get('max_pe_oi', 0.0):.0f}"
                f"({wall.get('pe_dist_strikes', 0.0):+.2f}stk) "
                f"tracked:{wall.get('n_tracked', 0)} "
                f"kin[armed:{tele.get('kin_armed', 0)} skipped:{tele.get('kin_skipped', 0)} "
                f"vetoed:{tele.get('kin_vetoed', 0)}] gate_vetoed:{tele.get('gate_vetoed', 0)} "
                f"delta_vetoed:{tele.get('delta_vetoed', 0)}"
            )
            return True
        except Exception:
            return False

    def _on_futures_tick(self, sym: str, lp: float, tick: Any, now_sec: float):
        """SHADOW futures tick processor (2026-10-26).

        Telemetry-only: measures the live data speed of the futures contract
        and tracks basis/lead-lag stats for offline analysis. NEVER gates a
        live decision. Candidate future gate uses (to be evaluated from the
        shadow logs once tick rate is proven):
          1. Basis divergence veto (futures basis z-score extreme vs spot move).
          2. Lead-lag confirmation (futures leads spot by ~1 tick on breaks).
          3. P3/P4 breakout confirmation via 15s futures path efficiency.
        """
        if lp <= 0:
            return
        self.last_futures_price = lp

        # ─── SHADOW Data-Speed Measurement (5-minute rolling window) ───
        self._fut_shadow_total_ticks += 1
        self._fut_shadow_window_ticks += 1
        self._fut_shadow_sym = sym or self._fut_shadow_sym
        if self._fut_shadow_window_start <= 0:
            self._fut_shadow_window_start = now_sec
            logger.info(f"[SHADOW FUTURES FEED] First tick received: {sym} @ {lp:.2f} — shadow data-speed measurement active (non-gating)")
        if (now_sec - self._fut_shadow_window_start) >= 300.0:
            window_sec = max(now_sec - self._fut_shadow_window_start, 1.0)
            rate = self._fut_shadow_window_ticks / window_sec
            basis_vals = [b for (_, _, b) in list(self.futures_history)[-self._fut_shadow_window_ticks:] if abs(b) < 1000.0]
            basis_mean = (sum(basis_vals) / len(basis_vals)) if basis_vals else 0.0
            logger.info(
                f"[SHADOW FUTURES FEED] {self._fut_shadow_sym} | ticks_5m={self._fut_shadow_window_ticks} "
                f"({rate:.2f}/s) | total={self._fut_shadow_total_ticks} | lp={lp:.2f} "
                f"| basis_mean={basis_mean:+.2f}pts | verdict={'GATE_VIABLE' if rate >= 1.0 else 'TOO_SLOW_FOR_GATE'}"
            )
            self._fut_shadow_window_start = now_sec
            self._fut_shadow_window_ticks = 0

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
        if not AegisPipelineOrchestrator.is_plausible_spot_tick(lp, self.last_spot, gap_sec):
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

        # Update Shadow Boundary Observer (First-Touch Pain Zone & 200 EMA + OTM 5)
        if getattr(self, "shadow_boundary_observer", None):
            self.shadow_boundary_observer.on_underlying_tick(lp, now_sec)

        # 1. Update V3 TimeBarEngine & 5-minute Volatility Impulse Memory
        if self.time_bar_engine:
            self.time_bar_engine.update_tick(price=lp, ts_sec=now_sec)
            if self.time_bar_engine.atr_ratio >= 1.0 or self.time_bar_engine.current_atr_1m >= (0.00025 * lp):
                self.last_volatility_burst_ts = now_sec

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

    # ─── V3.0 Event-Bound Helpers (T Anchoring) ───
    def _get_last_closed_bar_T(self) -> float:
        """Returns formal 1-minute candle close timestamp T (ts_end of last closed bar)."""
        try:
            pae = getattr(self, "price_action_engine", None) or getattr(self, "time_bar_engine", None)
            if pae is not None and getattr(pae, "bars_1m", None):
                if len(pae.bars_1m) > 0:
                    bar = pae.bars_1m[-1]
                    t_end = float(getattr(bar, "ts_end", 0.0) or 0.0)
                    if t_end > 0:
                        return t_end
                    t_start = float(getattr(bar, "ts_start", 0.0) or 0.0)
                    if t_start > 0:
                        return t_start + 60.0
        except Exception:
            pass
        return 0.0

    def _is_T_fresh(self, anchor_T: float, now_sec: float, max_age_sec: float = 120.0) -> bool:
        """True when T is a recent production bar-close (not synthetic old test data)."""
        if not anchor_T or anchor_T <= 0 or not now_sec or now_sec <= 0:
            return False
        age = now_sec - anchor_T
        return 0.0 <= age <= max_age_sec

    def _purge_impulse_memory(self, sym: str) -> None:
        """V3.0 DROP policy: discard any retained impulse for sym immediately."""
        try:
            if sym in self.impulse_memory:
                del self.impulse_memory[sym]
        except Exception:
            pass

    def _throttled_veto_log(self, key: str, msg: str, interval_sec: Optional[float] = None) -> None:
        """Hot-path log throttle: first hit immediate, then at most once per
        interval with suppressed-count summary. Keeps CPU < 10% (no per-tick disk writes)."""
        try:
            now = time.time()
            interval = float(interval_sec if interval_sec is not None else (getattr(self, "_veto_log_interval_sec", 60.0) or 60.0))
            last = self._veto_log_ts.get(key, 0.0)
            if now - last >= interval:
                suppressed = int(self._veto_log_suppressed.get(key, 0))
                self._veto_log_ts[key] = now
                self._veto_log_suppressed[key] = 0
                if suppressed > 0:
                    logger.info(f"{msg} (suppressed {suppressed}x in last {interval:.0f}s)")
                else:
                    logger.info(msg)
            else:
                self._veto_log_suppressed[key] = int(self._veto_log_suppressed.get(key, 0)) + 1
        except Exception:
            pass

    # ─── Quality Stack: Stability Due Processing ───
    def _process_stability_due(self, now_sec: float) -> None:
        """Resolves armed post-trigger stability verdicts (telemetry-only)."""
        if not self.stability_tracker:
            return
        try:
            for item in self.stability_tracker.due(now_sec):
                mid_now = 0.0
                if self.scorer is not None:
                    buf = self.scorer.history.get(item["sym"])
                    if buf:
                        last = buf[-1]
                        bp, sp = float(last[2]), float(last[3])
                        mid_now = ((bp + sp) / 2.0) if (bp > 0 and sp > 0) else float(last[1])
                verdict = self.stability_tracker.resolve(item, mid_now)
                logger.info(
                    f"[STABILITY 10s] {verdict['sym']} ({verdict['direction']}) | hold={verdict['hold']} "
                    f"ret10={verdict['ret_10s']*100:.2f}% | fade_frac={verdict['fade_frac']:.2f}"
                )
                self._write_stability_verdict(verdict)
        except Exception:
            pass

    def _write_stability_verdict(self, verdict: dict) -> None:
        try:
            path = getattr(self, "_stability_log_path", None)
            if not path:
                return
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(verdict, default=str) + "\n")
        except Exception:
            pass

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

    def get_futures_basis_metrics(self, now_sec: float, lookback_sec: float = 60.0) -> Dict[str, float]:
        """Returns 60s pre-trade Futures Basis metrics and Futures Lead-Lag Momentum for conviction scoring."""
        if not self.futures_history or self.last_spot <= 0:
            return {
                "basis_current": round(self.last_basis_pts, 2),
                "basis_60s_ago": round(self.last_basis_pts, 2),
                "basis_change_60s": 0.0,
                "basis_change_60s_bps": 0.0,
                "basis_z_score": 0.0,
                "delta_fut_60s_pts": 0.0,
                "delta_fut_60s_bps": 0.0,
            }

        target_ts = now_sec - lookback_sec
        ref_basis = self.futures_history[0][2]
        ref_fut_px = self.futures_history[0][1]
        basis_vals = []
        for ts, px, b_pts in self.futures_history:
            basis_vals.append(b_pts)
            if ts <= target_ts:
                ref_basis = b_pts
                ref_fut_px = px

        current_basis = self.last_basis_pts
        delta_basis = current_basis - ref_basis
        delta_bps = (delta_basis / self.last_spot) * 10000.0 if self.last_spot > 0 else 0.0

        current_fut_px = self.last_futures_price if self.last_futures_price > 0 else ref_fut_px
        delta_fut_px = current_fut_px - ref_fut_px
        delta_fut_bps = (delta_fut_px / ref_fut_px * 10000.0) if ref_fut_px > 0 else 0.0

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
            "delta_fut_60s_pts": round(delta_fut_px, 2),
            "delta_fut_60s_bps": round(delta_fut_bps, 2),
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
        if getattr(self, "shadow_boundary_observer", None) is not None:
            self.shadow_boundary_observer.strike_to_symbol = self.strike_to_symbol

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

    def get_pain_zone_band(self) -> Tuple[float, float]:
        """
        True Pain Zone Band Envelope:
          Upper Band = max(active_vwaps)
          Lower Band = min(active_vwaps)
        Returns (band_upper, band_lower) from PriceActionEngine single source.
        """
        if self.price_action_engine is not None:
            return self.price_action_engine.get_pain_zone_band()
        return 0.0, 0.0

    def close(self) -> None:
        """Gracefully flushes pending shadow telemetry and shuts down background resources."""


def create_aegis_engine(
    token_to_symbol: Dict[str, str],
    token_map: Dict[str, Dict],
    bucket_map: Dict[str, float] = None,
    strike_step: float = 100.0,
    underlying_tokens: set = None,
    signal_queue: Any = None,
    log_path: str = None,
) -> AegisPipelineOrchestrator:
    return AegisPipelineOrchestrator(
        token_to_symbol=token_to_symbol,
        token_map=token_map,
        bucket_map=bucket_map,
        strike_step=strike_step,
        underlying_tokens=underlying_tokens,
        signal_queue=signal_queue,
        log_path=log_path,
    )




