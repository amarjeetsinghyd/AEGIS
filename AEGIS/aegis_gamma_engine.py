# aegis_gamma_engine.py
# AEGIS V2.0 — Empirical Microstructure Impulse Engine
# Reverse-engineered from 812 real >=10% clean option impulse moves (NIFTY & SENSEX)
# Zero-allocation hot path, O(1) continuous confidence scoring (0 to 100).

from __future__ import annotations
import os
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
    from AEGIS.core.microstructure_scorer import MicrostructureScorer
    from AEGIS.core.regime_chop_gate import RegimeChopGate
    from AEGIS.core.time_bar_engine import TimeBarEngine
    from AEGIS.core.directional_persistence import DirectionalPersistenceEngine
    from AEGIS.core.option_quality import OptionQualityEngine
    from AEGIS.core.signal_state_machine import SignalStateMachine, SignalState
    from AEGIS.core.outcome_telemetry import OutcomeTelemetryEngine
    from AEGIS.core.entry_quality_engine import EntryQualityEngine
except ImportError:
    try:
        from core.microstructure_scorer import MicrostructureScorer
        from core.regime_chop_gate import RegimeChopGate
        from core.time_bar_engine import TimeBarEngine
        from core.directional_persistence import DirectionalPersistenceEngine
        from core.option_quality import OptionQualityEngine
        from core.signal_state_machine import SignalStateMachine, SignalState
        from core.outcome_telemetry import OutcomeTelemetryEngine
        from core.entry_quality_engine import EntryQualityEngine
    except ImportError:
        MicrostructureScorer = None
        RegimeChopGate = None
        TimeBarEngine = None
        DirectionalPersistenceEngine = None
        OptionQualityEngine = None
        SignalStateMachine = None
        OutcomeTelemetryEngine = None
        EntryQualityEngine = None

logger = get_logger("aegis_gamma")

# ──────────────────────────────────────────────────────────────
# CONSTANTS & CONFIGURATION
# ──────────────────────────────────────────────────────────────
N_STRIKES = 21          # ATM ±10
ATM_IDX   = N_STRIKES // 2

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
        self.tick_ring: Dict[str, deque] = {}  # pre-sized below after P is loaded
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

        if RegimeChopGate is not None:
            self.chop_gate = RegimeChopGate(strike_step=self.strike_step, cooldown_sec=240.0)
        else:
            self.chop_gate = None

        # State Tracking
        self.last_spot = 0.0
        self.spot_history = deque(maxlen=600)  # (ts_sec, spot_price)
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

        # V3 Horizon-Aligned Modules
        telemetry_dir = os.path.join(PARAM_DIR, "..", "logs", "telemetry") if "PARAM_DIR" in globals() else "AEGIS/logs/telemetry"
        self.time_bar_engine = TimeBarEngine(ema_period=9, atr_period=14) if TimeBarEngine is not None else None
        self.persistence_engine = DirectionalPersistenceEngine(window_sec=60.0, sample_interval_sec=5.0) if DirectionalPersistenceEngine is not None else None
        self.option_quality_engine = OptionQualityEngine(max_spread_pct=2.5, min_top5_depth_qty=500.0) if OptionQualityEngine is not None else None
        self.signal_fsm = SignalStateMachine(max_trigger_freshness_sec=3.0, max_trigger_acceptable_sec=8.0) if SignalStateMachine is not None else None
        self.eqs_engine = EntryQualityEngine(dispatch_threshold=float(self.P.get("eqs_dispatch_threshold", 70.0))) if EntryQualityEngine is not None else None
        self.outcome_telemetry = OutcomeTelemetryEngine(log_dir=telemetry_dir) if OutcomeTelemetryEngine is not None else None

        # Fill & Edge tracking
        self.fill_latency_buf = np.zeros(500, dtype=np.float32)
        self.post_fill_edge_buf = np.zeros(500, dtype=np.float32)
        self.fill_ptr = 0
        self._pending_edge_measures: List[Dict] = []
        # Empirical Veto Gate from 5-day Backtest (Spikes avg 1.895 ticks/s vs controls 0.500)
        self.min_tick_rate_gate = float(self.P.get("min_tick_rate_gate", 1.5))

        # Compatibility attribute (deprecated, kept for dashboard schema — see audit P2.10)
        self.gamma_tensor = np.zeros((2, N_STRIKES), dtype=np.float32)

        # Warm tick_ring & strike map after all symbols known (P1.4/P2.11)
        self._rebuild_strike_to_symbol()
        for sym in set(self.t2s.values()):
            if sym not in self.tick_ring:
                self.tick_ring[sym] = deque(maxlen=100)

        logger.info(
            f"AEGIS Empirical Microstructure Engine initialised | "
            f"Threshold={self.high_conviction_threshold:.1f} | MinTickRate={self.min_tick_rate_gate:.1f} | StrikeStep={self.strike_step}"
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
        self.min_tick_rate_gate = float(self.P.get("min_tick_rate_gate", 1.5))
        if self.scorer is not None:
            weights_file = os.path.join(PARAM_DIR, "microstructure_weights.json")
            self.scorer = MicrostructureScorer(weights_path=weights_file, high_conviction_threshold=self.high_conviction_threshold)
        logger.info(f"Parameters reloaded | New Threshold: {self.high_conviction_threshold} | MinTickRate: {self.min_tick_rate_gate}")

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

            side = info["side"]          # 0=CE, 1=PE
            strike = float(info["strike"])

            # [NEW] O(1) Rolling 2.0s Time-Window Tick Rate Calculation
            if sym not in self.tick_ring:
                self.tick_ring[sym] = deque(maxlen=100)
                
            ring = self.tick_ring[sym]
            ring.append(now_sec)
            while ring and ring[0] < (now_sec - 2.0):
                ring.popleft()
            live_tick_rate = len(ring) / 2.0

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

            tot_bid_depth = b1 + b2 + b3 + b4 + b5
            tot_ask_depth = a1 + a2 + a3 + a4 + a5

            if self.scorer is not None:
                self.scorer.update_tick(sym, now_sec, lp, bp1, sp1, b1, a1, b2, a2, b3, a3, b4, a4, b5, a5)

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
                
                # Check High Conviction Trigger
                if is_high_conv and score >= self.high_conviction_threshold:
                    last_fired = self.cooldowns.get(sym, 0.0)
                    cooldown_dur = float(self.P.get("burst_cooldown_sec", 60.0))
                    if now_sec - last_fired >= cooldown_dur:
                        # [OCCAM GATE] Veto if market is quiet (low intensity)
                        if live_tick_rate < self.min_tick_rate_gate:
                            logger.info(
                                f"[OCCAM VETO] {sym} Score={score:.1f} tick_rate={live_tick_rate:.2f} < {self.min_tick_rate_gate:.1f}"
                            )
                            return

                        # V3 4-Domain Pipeline Evaluation
                        direction = "UP" if side == 0 else "DOWN"  # 0=CE -> UP, 1=PE -> DOWN

                        # Domain 1: Permission (Higher Timeframe Trend & Structure)
                        has_permission = True
                        perm_reason = "permission_ok"
                        perm_metrics = {}
                        if self.time_bar_engine:
                            has_permission, perm_reason, perm_metrics = self.time_bar_engine.get_permission_state(direction)
                            if not has_permission and perm_reason != "warmup_permissive":
                                logger.info(f"[PERMISSION VETO] {sym} ({direction}) blocked by '{perm_reason}'")
                                return

                        # Domain 2: Confirmation (60s Signed Persistence)
                        pers_confirmed = True
                        pers_score = 1.0
                        pers_metrics = {}
                        if self.persistence_engine:
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

                        # Centralized Signal FSM Lifecycle Evaluation
                        fsm_state = None
                        should_fire = False
                        fsm_reason = "direct"
                        if self.signal_fsm:
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
                            should_fire = is_high_conv and score >= self.high_conviction_threshold

                        if should_fire:
                            trigger_age = 0.5
                            if self.signal_fsm:
                                trigger_age = now_sec - self.signal_fsm.get_or_create(sym).last_trigger_ts
                            
                            eqs = 80.0
                            is_eqs_qual = True
                            eqs_breakdown = {}
                            if self.eqs_engine:
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

                            if is_eqs_qual:
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
                                        features_snapshot={**feats, **eqs_breakdown, **perm_metrics},
                                    )
                                if self.signal_fsm:
                                    self.signal_fsm.register_trade_executed(sym, now_sec)

                                self.cooldowns[sym] = now_sec
                                logger.info(
                                    f"[AEGIS V3 FIRED] {sym} ({direction}) | EQS: {eqs:.1f}/100 | MicroScore: {score:.1f} | "
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
                                        "microstructure_score": round(score, 2),
                                        "persistence_score": round(pers_score, 3),
                                        "live_tick_rate": round(live_tick_rate, 2),
                                        "strike": strike,
                                        "side": "CE" if side == 0 else "PE",
                                        **feats,
                                        **eqs_breakdown,
                                        **perm_metrics,
                                    }
                                )

        except Exception as e:
            logger.error(f"AEGIS on_tick_parsed error: {e}", exc_info=True)

    def _on_underlying_tick(self, sym: str, lp: float, now_sec: float):
        """Processes underlying Spot price ticks and manages dynamic ATM strike alignment."""
        if lp <= 0:
            return
        self.last_spot = lp
        self.spot_history.append((now_sec, lp))

        if self.chop_gate:
            self.chop_gate.update(lp, now_sec)

        # 1. Update V3 TimeBarEngine
        if self.time_bar_engine:
            self.time_bar_engine.update_tick(price=lp, ts_sec=now_sec)

        # 2. Update V3 60s Directional Persistence Snapshot every 5s
        if self.persistence_engine and self.persistence_engine.should_sample(now_sec):
            curr_dir = "NEUTRAL"
            drift_5s = 0.0
            if self.time_bar_engine and self.time_bar_engine.bars_1m:
                drift_5s = lp - self.time_bar_engine.bars_1m[-1].open
                if self.time_bar_engine.ema_1m_slope > 0.1:
                    curr_dir = "UP"
                elif self.time_bar_engine.ema_1m_slope < -0.1:
                    curr_dir = "DOWN"
            elif len(self.spot_history) >= 5:
                drift_5s = lp - self.spot_history[-5][1]
                if drift_5s > 0.5:
                    curr_dir = "UP"
                elif drift_5s < -0.5:
                    curr_dir = "DOWN"

            self.persistence_engine.record_snapshot(
                now_sec=now_sec,
                direction=curr_dir,
                micro_score=75.0 if curr_dir != "NEUTRAL" else 50.0,
                spot_drift=drift_5s,
            )

        # Prune older than 300s (5 min)
        cutoff = now_sec - 300.0
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
        """Returns the spot price drift (current_spot - spot_lookback_ago)."""
        if not self.spot_history or self.last_spot <= 0:
            return 0.0
        target_ts = now_sec - lookback_sec
        ref_price = self.spot_history[0][1]
        for ts, price in self.spot_history:
            if ts <= target_ts:
                ref_price = price
            else:
                break
        return self.last_spot - ref_price

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
        # Warm tick_ring for any new symbols without disturbing existing deques
        for sym in self.t2s.values():
            if sym not in self.tick_ring:
                self.tick_ring[sym] = deque(maxlen=100)
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
                sym: Optional[str] = None):
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
        })
        logger.info(f"[AEGIS] Fill recorded: latency={lat_ms:.1f}ms | px={fill_price:.2f}")

    def check_pending_edges(self, now: float, current_spot: float):
        """Records post-fill edge survival."""
        still_pending = []
        for item in self._pending_edge_measures:
            if now >= item["measure_at"]:
                if item["fill_price"] > 0 and current_spot > 0:
                    ret_bps = (current_spot - item["fill_price"]) / item["fill_price"] * 10000.0
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
