"""LIVE execution — v10.0 "Microstructure Decay Dynamic Exits"
Integrates: CascadeValueEngine (MPAR, DKLA, LRA, Unified Conviction Trigger)
            + Advanced Microstructure Decay Exit Architecture
v9.7 base: 5x leverage, ATR15 TP/SL + Chandelier trail, zone-capped TP,
           5% daily loss cap, calibrated costs, robust API resolution,
           Noren tsym handling, tick-aligned marketable LMT orders,
           TRADED status recognition, rejection logging, pre-square-off (14:55),
           retry cap + broker position reconciliation, MAE/MFE attribution,
           slippage/latency logging, consecutive-loss circuit breaker (3 SLs),
           margin safety 0.75 with auto-retry, TICK_SIZE 0.10 universal,
           BREAKEVEN LOCK (0.5*ATR profit -> stop floored at entry).
v10.0: REPLACED rigid 0.3%/0.15% exits with dynamic microstructure decay logic:
       MPAR deceleration, DKLA asymmetry flip, Vacuum exhaustion/reversal,
       LRA velocity reversal, confirmed by toxicity spike + whale exit + branching collapse.
       All order-punching/API/Shoonya mechanics PRESERVED 100%."""
from __future__ import annotations

import os
import sys
import logging

# ─── BOOTSTRAP ───
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import json
import math
import time as pytime
import threading
import queue
import copy
from datetime import datetime, time
import concurrent.futures
from typing import Dict, Any, Tuple, List, Deque
from collections import deque
import numpy as np

from common.config import get_data_dir
from common.execution.capital_engine import CapitalEngine
from common.logger import get_logger

logger = get_logger("stock_execution")

TRADES_DIR = os.path.join(get_data_dir(), "trades")
os.makedirs(TRADES_DIR, exist_ok=True)
OPEN_PATH = os.path.join(TRADES_DIR, "open_positions.json")
HISTORY_PATH = os.path.join(TRADES_DIR, "trade_history.json")

PAPER_MODE = True                # ARGUS running in shadow/paper mode per user directive
MAX_OPEN_POSITIONS = 1
MAX_TRADES_PER_DAY = 5           # Hard safety limit for testing: max 5 trades per day
LEVERAGE = 5.0
MARGIN_SAFETY = 1.0          # Set to 100% per user request
TP_ATR_MULT = 1.5            # Calibrated for 0.4% - 0.6% micro-trend capture
SL_ATR_MULT = 0.8            # Initial ATR stop loss (tight risk control)
CHANDELIER_ATR_MULT = 0.8    # Tight dynamic trailing stop to lock in sudden spike peaks
BREAKEVEN_ATR_FRACTION = 0.4 # Lock breakeven early at 40% of ATR move to protect profits
# MN-6: gap-open detection before breakeven floor lock. A single-tick move larger
# than GAP_ATR_MULT * ATR past the entry is a discontinuous gap, not a trend advance
# — locking the stop at breakeven on a gap would invert it exactly at entry right as
# price gaps through it. Such ticks skip the breakeven lock (the stop stays at its
# prior chandelier/init value) so the gap is treated as an exit signal, not a floor.
GAP_ATR_MULT = 1.5
FALLBACK_TP_PCT = 0.005      # 0.50% target move (0.4% - 0.6% capture objective)
FALLBACK_SL_PCT = 0.0025     # 0.25% stop loss (2:1 reward-to-risk ratio)
DAILY_LOSS_CAP_PCT = 0.05
MAX_HOLD_MINUTES = 35
MKT_SLACK_PCT = 0.010
TICK_SIZE = 0.10
EOD_FLATTEN_TIME = time(14, 55)
MAX_EXIT_ATTEMPTS = 3
CONSEC_SL_LIMIT = 3

# ═══════════════════════════════════════════════════════════════════
# DYNAMIC EXIT ENGINE — Microstructure Decay Detection (EMBEDDED)
# ═══════════════════════════════════════════════════════════════════

EXIT_CFG = {
    # MPAR Deceleration
    "mpar_accel_flip_mult": 2.0,          # Require 2x adverse acceleration flip
    "mpar_accel_collapse_pct": 0.50,      
    "mpar_min_hold_seconds": 60,          # Give position at least 60s to develop
    # DKLA Asymmetry Flip
    "dkla_flip_threshold": 0.0,
    "dkla_flip_confirm_bps": 10.0,        # Require stronger confirmation
    "dkla_min_hold_seconds": 60,          
    # Vacuum Exhaustion/Reversal
    "vac_reversal_bps": 30.0,             
    "vac_exhaustion_z": 3.0,              
    "vac_min_hold_seconds": 60,           
    # LRA Velocity Reversal
    "lra_vel_flip_threshold": 0.0,
    "lra_vel_magnitude_min": 0.25,        
    "lra_min_hold_seconds": 60,           
    # Confirmation Layer (require >= 2 of 3, with hold timer)
    "min_hold_seconds": 60,               # Minimum hold before confirmation layer exit
    "cooldown_seconds": 180,              # 3-minute per-symbol cooldown after exit
    "tox_spike_pct": 95,
    "whale_exit_z": -1.5,                 # More negative - require stronger whale exit
    "branching_collapse": 0.10,           # Lowered from 0.15 - more sensitive to collapse
    # Hard Guards
    "max_hold_minutes": MAX_HOLD_MINUTES,
    "breakeven_atr_frac": BREAKEVEN_ATR_FRACTION,
    "eod_flatten_time": "14:55",
}

# ═══════════════════════════════════════════════════════════════════
# KYLE'S λ LIMIT PRICE ENGINE — Optimal Limit Order Placement
# ═══════════════════════════════════════════════════════════════════

def calculate_kyle_limit_price(
    side: str,
    mid_price: float,
    lam_buy: float,
    lam_sell: float,
    target_notional: float,
    conviction: float,
    max_slippage_bps: float = 5.0,
    # P1 FIX: Queue position risk & spread tightness parameters
    bid_queue_decay: float = 0.0,
    ask_queue_decay: float = 0.0,
    spread_bps: float = 0.0,
) -> float:
    """
    Calculate optimal limit price using Kyle's λ (market impact coefficient).
    
    Kyle's model: optimal limit price = mid ± (target_notional * λ / 2)
    where λ is the price impact per unit notional (in bps per notional).
    
    For BUY:  limit = mid + (target_notional * lam_buy / 2)  -> pay up to λ/2 impact
    For SELL: limit = mid - (target_notional * lam_sell / 2) -> receive down to λ/2 impact
    
    The conviction parameter scales the aggressiveness:
    - High conviction (>0.7): Use full Kyle λ pricing (capture spread as maker)
    - Medium conviction (0.5-0.7): Use 50% of Kyle λ
    - Low conviction (<0.5): Fall back to marketable limit (current behavior)
    
    P1 FIX: Queue position risk & spread tightness adjustments:
    - High queue decay (fast consumption) = widen limit to avoid adverse selection
    - Tight spread = more aggressive (smaller queue risk premium)
    - Wide spread = less aggressive (larger queue risk premium)
    
    Args:
        side: "BUY" or "SELL"
        mid_price: Current mid price
        lam_buy: Kyle's λ for buy side (bps per notional) from DKLA
        lam_sell: Kyle's λ for sell side (bps per notional) from DKLA
        target_notional: Target notional for the order (e.g., 1 Cr)
        conviction: Signal conviction score (0-10+)
        max_slippage_bps: Maximum slippage tolerance in bps
        bid_queue_decay: Bid queue consumption rate in bps (from adv_metrics)
        ask_queue_decay: Ask queue consumption rate in bps (from adv_metrics)
        spread_bps: Current bid-ask spread in bps
    
    Returns:
        Optimal limit price aligned to tick size
    """
    # Normalize conviction to [0, 1] range (typical conviction range 0-10)
    conv_norm = min(max(conviction / 10.0, 0.0), 1.0)
    
    # Scale factor based on conviction
    if conv_norm >= 0.7:
        kyle_scale = 1.0  # Full Kyle pricing - be maker
    elif conv_norm >= 0.5:
        kyle_scale = 0.5  # Half Kyle pricing
    else:
        kyle_scale = 0.0  # Marketable limit (current behavior)
    
    # P1 FIX: Queue position risk adjustment
    # High queue decay = liquidity vanishing fast = widen limit to avoid being picked off
    # Use the queue decay for the side we're trading on
    if side == "BUY":
        queue_decay = ask_queue_decay  # We're buying at ask, so ask queue decay matters
        lam = lam_buy
    else:
        queue_decay = bid_queue_decay  # We're selling at bid, so bid queue decay matters
        lam = lam_sell
    
    # Queue risk premium: scale by queue decay (bps)
    # High decay (>50 bps) adds up to 2 bps premium to limit price
    queue_risk_premium_bps = min(queue_decay * 0.04, 2.0)  # Cap at 2 bps
    
    # P1 FIX: Spread tightness adjustment
    # Tight spread (< 2 bps) = more aggressive (reduce premium)
    # Wide spread (> 10 bps) = less aggressive (increase premium)
    if spread_bps > 0:
        if spread_bps < 2.0:
            spread_adjustment = -0.5  # Tight spread: be more aggressive
        elif spread_bps > 10.0:
            spread_adjustment = 1.0   # Wide spread: be less aggressive
        else:
            spread_adjustment = 0.0
    else:
        spread_adjustment = 0.0
    
    # Kyle optimal: mid ± (notional * λ / 2) * scale
    # λ is in bps, so convert: λ_bps * notional / 10000 = price impact
    impact = (target_notional * lam / 2.0) / 10000.0 * kyle_scale
    
    if side == "BUY":
        limit_price = mid_price + impact
        # Add queue risk premium (widen limit = pay more to ensure fill)
        limit_price += mid_price * (queue_risk_premium_bps + spread_adjustment) / 10000.0
    else:
        limit_price = mid_price - impact
        # Subtract queue risk premium (widen limit = receive less to ensure fill)
        limit_price -= mid_price * (queue_risk_premium_bps + spread_adjustment) / 10000.0
    
    # Cap at max slippage
    max_impact = mid_price * max_slippage_bps / 10000.0
    if side == "BUY":
        limit_price = min(limit_price, mid_price + max_impact)
    else:
        limit_price = max(limit_price, mid_price - max_impact)
    
    # Align to tick size
    steps = limit_price / TICK_SIZE
    if side == "BUY":
        aligned = math.ceil(steps - 1e-9) * TICK_SIZE
    else:
        aligned = math.floor(steps + 1e-9) * TICK_SIZE
    
    return round(aligned, 2)


class ExitState:
    __slots__ = (
        "entry_time", "entry_accel", "peak_accel_mag", "peak_vacuum",
        "peak_vacuum_z", "entry_dkla_asym", "entry_lra_vel",
        "confirmed_decay_count", "breakeven_locked",
        # Q4: Peak-lock trackers (only increase, never decrease)
        "locked_peak_accel", "locked_peak_vacuum", "locked_peak_vacuum_z"
    )
    def __init__(self):
        self.entry_time = 0.0
        self.entry_accel = 0.0
        self.peak_accel_mag = 0.0
        self.peak_vacuum = 0.0
        self.peak_vacuum_z = 0.0
        self.entry_dkla_asym = 0.0
        self.entry_lra_vel = 0.0
        self.confirmed_decay_count = 0
        self.breakeven_locked = False
        # Q4: Peak-lock trackers
        self.locked_peak_accel = 0.0
        self.locked_peak_vacuum = 0.0
        self.locked_peak_vacuum_z = 0.0

def evaluate_exit(
    position_direction: str,
    entry_price: float,
    current_lp: float,
    adv_metrics: Dict[str, Any],
    bucket_aggregates: Dict[str, Any],
    trade_state: ExitState,
    atr: float = 0.0,
    current_time: float = None,
) -> Tuple[bool, str]:
    """
    Returns (should_exit: bool, reason: str).
    Reason codes: "MPAR_DECEL", "DKLA_FLIP", "VAC_REVERSAL", "LRA_VEL_FLIP",
                  "TOX_SPIKE", "WHALE_EXIT", "BRANCHING_COLLAPSE",
                  "BREAKEVEN_LOCK", "TIME_STOP", "EOD_FLATTEN", "NONE"
    
    Q4 OPTIMIZATION: SVI-based adaptive sensitivity + peak-lock mechanism
    - SVI (S_flow Volatility Index) computed from recent S_flow variance
    - Adaptive thresholds: base_threshold * (1 + SVI * sensitivity_factor)
    - Peak-lock: once peak_accel_mag/peak_vacuum set, they only increase (never decrease)
    - Prevents premature exits during normal volatility expansion
    """
    now = current_time or pytime.time()
    side = 1 if position_direction == "BUY" else -1

    # Extract current microstructure metrics from adv_metrics (real-time tick data)
    mp_accel     = adv_metrics.get("mp_accel", 0.0)
    mp_align     = adv_metrics.get("mp_align", 0.0)
    dkla_asym    = adv_metrics.get("dkla_asym", 0.0)
    lam_buy      = adv_metrics.get("lam_buy", 0.0)
    lam_sell     = adv_metrics.get("lam_sell", 0.0)
    lra_vel      = adv_metrics.get("lra_vel", 0.0)
    lra_asym     = adv_metrics.get("lra_asym", 0.0)
    vac_bps_tick = adv_metrics.get("vac_bps_tick", 0.0)
    
    # Extract bucket-level aggregates from bucket_aggregates
    net_vacuum   = bucket_aggregates.get("net_vacuum_bps", 0.0)
    toxicity_history = bucket_aggregates.get("toxicity_history", [])
    branching_ratio = bucket_aggregates.get("branching", 1.0)
    whale_z = bucket_aggregates.get("whale_z", 0.0)
    dkla_toxicity = bucket_aggregates.get("dkla_toxicity", 0.0)
    
    # Q4: SVI (S_flow Volatility Index) - compute from recent S_flow variance
    s_flow_history = bucket_aggregates.get("s_flow_history", [])
    svi = 0.0
    if s_flow_history and len(s_flow_history) >= 10:
        import numpy as np
        s_flow_vals = np.array(s_flow_history[-20:])  # Last 20 buckets
        svi = float(np.std(s_flow_vals) / max(np.mean(np.abs(s_flow_vals)), 1e-6))
        svi = min(svi, 2.0)  # Cap SVI at 2.0

    # Initialize state on first call
    if trade_state.entry_time == 0.0:
        trade_state.entry_time = now
        trade_state.entry_accel = mp_accel
        trade_state.peak_accel_mag = abs(mp_accel)
        trade_state.entry_dkla_asym = dkla_asym
        trade_state.entry_lra_vel = lra_vel
        trade_state.peak_vacuum = net_vacuum * side
        trade_state.peak_vacuum_z = 0.0
        # Q4: Peak-lock trackers (only increase, never decrease)
        trade_state.locked_peak_accel = abs(mp_accel)
        trade_state.locked_peak_vacuum = net_vacuum * side
        trade_state.locked_peak_vacuum_z = 0.0
        return False, "NONE"

    # Q4: PEAK-LOCK MECHANISM - peaks only increase, never decrease
    # This prevents premature exits during normal volatility expansion
    trade_state.locked_peak_accel = max(trade_state.locked_peak_accel, abs(mp_accel))
    trade_state.locked_peak_vacuum = max(trade_state.locked_peak_vacuum, net_vacuum * side)
    
    # Also update legacy peak trackers for backward compatibility
    trade_state.peak_accel_mag = max(trade_state.peak_accel_mag, abs(mp_accel))
    trade_state.peak_vacuum = max(trade_state.peak_vacuum, net_vacuum * side)

    # Compute Vacuum Z-Score
    vac_z = 0.0
    if toxicity_history and len(toxicity_history) > 20:
        import numpy as np
        vac_mean = np.mean(toxicity_history)
        vac_std = max(np.std(toxicity_history), 1e-6)
        vac_z = (net_vacuum - vac_mean) / vac_std
        trade_state.peak_vacuum_z = max(trade_state.peak_vacuum_z, vac_z * side)
        trade_state.locked_peak_vacuum_z = max(trade_state.locked_peak_vacuum_z, vac_z * side)

    # ─── HARD GUARDS ───
    minutes_held = (now - trade_state.entry_time) / 60.0
    if minutes_held >= EXIT_CFG["max_hold_minutes"]:
        return True, "TIME_STOP"

    eod_h, eod_m = map(int, EXIT_CFG["eod_flatten_time"].split(":"))
    if datetime.now().time() >= time(eod_h, eod_m):
        return True, "EOD_FLATTEN"

    # Breakeven Lock
    if atr > 0 and not trade_state.breakeven_locked:
        pnl = (current_lp - entry_price) * side
        if pnl >= EXIT_CFG["breakeven_atr_frac"] * atr:
            trade_state.breakeven_locked = True

    # Q4: ADAPTIVE THRESHOLDS based on SVI
    # Higher SVI = more volatile regime = wider thresholds to avoid whipsaws
    svi_sensitivity = 0.3  # How much SVI widens thresholds
    adaptive_mult = 1.0 + svi * svi_sensitivity
    
    mpar_accel_flip_mult = EXIT_CFG["mpar_accel_flip_mult"] * adaptive_mult
    mpar_accel_collapse_pct = EXIT_CFG["mpar_accel_collapse_pct"] / adaptive_mult  # Tighter collapse in high SVI
    dkla_flip_confirm_bps = EXIT_CFG["dkla_flip_confirm_bps"] * adaptive_mult
    vac_reversal_bps = EXIT_CFG["vac_reversal_bps"] * adaptive_mult
    vac_exhaustion_z = EXIT_CFG["vac_exhaustion_z"] * adaptive_mult
    lra_vel_magnitude_min = EXIT_CFG["lra_vel_magnitude_min"] * adaptive_mult
    tox_spike_pct = min(99, EXIT_CFG["tox_spike_pct"] + int(svi * 2))  # Higher percentile in high SVI
    whale_exit_z = EXIT_CFG["whale_exit_z"] * adaptive_mult
    branching_collapse = EXIT_CFG["branching_collapse"] / adaptive_mult  # More sensitive in high SVI

    # ─── DECAY PILLAR 1: MPAR DECELERATION (Directional Flip Only) ───
    seconds_held = now - trade_state.entry_time
    if seconds_held >= EXIT_CFG["mpar_min_hold_seconds"]:
        accel_flipped = (mp_accel * side < 0) and (trade_state.entry_accel * side > 0)
        # Only exit on MPAR if acceleration has actively reversed against trade direction
        # with significant magnitude (not just because entry impulse settled into steady velocity)
        if accel_flipped and abs(mp_accel) > mpar_accel_flip_mult * abs(trade_state.entry_accel):
            return True, "MPAR_DECEL"

    # ─── DECAY PILLAR 2: DKLA ASYMMETRY FLIP (adaptive) ───
    if seconds_held >= EXIT_CFG["dkla_min_hold_seconds"]:
        dkla_flipped = (dkla_asym * side < 0) and (trade_state.entry_dkla_asym * side > 0)
        dkla_magnitude = abs(lam_buy - lam_sell)
        if dkla_flipped and dkla_magnitude > dkla_flip_confirm_bps:
            return True, "DKLA_FLIP"

    # ─── DECAY PILLAR 3: VACUUM EXHAUSTION/REVERSAL (with peak-lock) ───
    if seconds_held >= EXIT_CFG["vac_min_hold_seconds"]:
        # Q4: Use locked peak vacuum for reversal detection
        vac_reversed = (trade_state.locked_peak_vacuum - net_vacuum * side) > vac_reversal_bps
        vac_exhausted = (trade_state.locked_peak_vacuum_z > vac_exhaustion_z) and \
                        (trade_state.locked_peak_vacuum_z - vac_z * side > 1.0)
        if vac_reversed or vac_exhausted:
            return True, "VAC_REVERSAL"

    # ─── DECAY PILLAR 4: LRA VELOCITY REVERSAL (adaptive) ───
    if seconds_held >= EXIT_CFG["lra_min_hold_seconds"]:
        lra_flipped = (lra_vel * side < 0) and (trade_state.entry_lra_vel * side > 0)
        if lra_flipped and abs(lra_vel) > lra_vel_magnitude_min:
            return True, "LRA_VEL_FLIP"

    # ─── CONFIRMATION LAYER (Require >= 2 of 3, adaptive, min hold enforced) ───
    if seconds_held >= EXIT_CFG.get("min_hold_seconds", 60):
        confirm_signals = 0
        confirm_reasons = []

        if toxicity_history and len(toxicity_history) > 20:
            import numpy as np
            tox_p95 = np.percentile(toxicity_history, tox_spike_pct)
            current_tox = bucket_aggregates.get("dkla_toxicity", 0.0)
            if current_tox > tox_p95:
                confirm_signals += 1
                confirm_reasons.append("TOX_SPIKE")

        if whale_z < whale_exit_z:
            confirm_signals += 1
            confirm_reasons.append("WHALE_EXIT")

        if branching_ratio < branching_collapse:
            confirm_signals += 1
            confirm_reasons.append("BRANCHING_COLLAPSE")

        if confirm_signals >= 2:
            return True, "+".join(confirm_reasons)

    return False, "NONE"

# ═══════════════════════════════════════════════════════════════════
# EXECUTION MANAGER (FULLY INTEGRATED v10.0)
# ═══════════════════════════════════════════════════════════════════

def _resolve_api(broker):
    candidates = []
    sp = getattr(broker, "session_provider", None)
    for base in (broker, sp):
        if base is None:
            continue
        for attr in ("_api", "api"):
            cand = getattr(base, attr, None)
            if cand is not None:
                candidates.append(cand)
    candidates.append(broker)
    for cand in candidates:
        if hasattr(cand, "place_order") or hasattr(cand, "placeorder"):
            return cand
    return candidates[0]

class StockExecutionManager:
    def __init__(self, broker):
        self.broker = broker
        self.api = _resolve_api(broker)
        logger.info(f"[StockExecution] API resolved: {type(self.api).__name__}")
        
        self.open_trades = {}
        self.leverage = LEVERAGE
        self.max_open_positions = MAX_OPEN_POSITIONS  # Read by main loop entry gate
        self._tsym = {}
        self.capital_engine = CapitalEngine(mode="LIVE", portfolio_provider=self.broker, data_dir=get_data_dir())
        
        # ─── Microstructure Metrics Feed (from CascadeValueEngine) ───
        self.adv_metrics_dict = {}          # symbol -> latest st["_adv"] dict
        self.bucket_aggregates_dict = {}    # symbol -> latest bucket aggregates
        
        self._load_open()
        self._day_key = None
        self._day_start_margin = 0.0
        self._day_loss = 0.0
        self._consec_sl = 0
        self._day_paused = False
        self._last_rej = ""
        self.symbol_last_exit_time = {}     # symbol -> timestamp of last exit (per-symbol cooldown)
        
        self.cmd_queue = queue.Queue(maxsize=10)
        self.prices_dict = {}
        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)

    # ─── PUBLIC: Called by CascadeValueEngine.on_tick() ───
    def update_adv_metrics(self, symbol: str, adv: dict):
        """Real-time per-tick microstructure metrics from Value Engine."""
        self.adv_metrics_dict[symbol] = adv

    # ─── PUBLIC: Called by CascadeValueEngine._process_bucket() ───
    def update_bucket_aggregates(self, symbol: str, aggregates: dict):
        """Bucket-level aggregates (net_vacuum_bps, branching, whale_z, toxicity_history, etc.)"""
        self.bucket_aggregates_dict[symbol] = aggregates

    def start(self):
        self.worker_thread.start()
        logger.info("[StockExecution] ExecutionThread started in background.")

    def update_prices(self, prices: dict):
        self.prices_dict = dict(prices)

    def enqueue_entry(self, signal, best_sym, lp, reason, metrics):
        try:
            self.cmd_queue.put_nowait(("ENTRY", (signal, best_sym, lp, reason, metrics), pytime.time()))
        except queue.Full:
            logger.warning(f"[StockExecution] Queue FULL! Dropping {signal} on {best_sym}")

    def _worker_loop(self):
        while True:
            try:
                if not self.cmd_queue.empty():
                    q_size = self.cmd_queue.qsize()
                    if q_size > 1:
                        logger.warning(f"[StockExecution] ⚠️ Queue backing up! Size: {q_size} (Possible API Lag)")
                
                while not self.cmd_queue.empty():
                    cmd, args, enqueued_at = self.cmd_queue.get_nowait()
                    if pytime.time() - enqueued_at > 2.0:
                        logger.warning(f"[StockExecution] Dropping stale entry from queue (lag: {pytime.time() - enqueued_at:.1f}s)")
                        continue
                    if cmd == "ENTRY":
                        self.execute_entry(*args)
                
                if self.open_trades and self.prices_dict:
                    self.manage_open_trades(self.prices_dict)
                
                pytime.sleep(0.05)
            except Exception as e:
                logger.error(f"[StockExecution] Worker thread error: {e}")
                pytime.sleep(1.0)

    def set_tsym_map(self, symbol_to_tsym: dict):
        self._tsym = {str(k): str(v) for k, v in symbol_to_tsym.items()}

    def _load_open(self):
        try:
            if os.path.exists(OPEN_PATH):
                with open(OPEN_PATH, "r") as f:
                    self.open_trades = json.load(f)
                
                valid_trades = {}
                for sym, t in self.open_trades.items():
                    tsym = t.get("tsym", sym)
                    net_qty = self._position_qty(tsym)
                    if net_qty is None:
                        valid_trades[sym] = t
                        continue
                    if net_qty == 0:
                        logger.warning(f"[StockExecution] Ghost trade detected for {tsym} (Broker qty = 0). Dropping.")
                    else:
                        if t["qty"] != abs(net_qty):
                            logger.warning(f"[StockExecution] Trade qty mismatch for {tsym}. Booking {net_qty} instead of {t['qty']}.")
                            t["qty"] = int(abs(net_qty))
                        valid_trades[sym] = t
                self.open_trades = valid_trades
                self._save_open()
        except Exception as e:
            logger.warning(f"open_positions load failed: {e}")

    def _save_open(self):
        try:
            with open(OPEN_PATH, "w") as f:
                json.dump(self.open_trades, f, indent=2, default=str)
        except Exception as e:
            logger.warning(f"open_positions save failed: {e}")

    def _save_history(self, trade):
        try:
            history = []
            if os.path.exists(HISTORY_PATH):
                with open(HISTORY_PATH, "r") as f:
                    history = json.load(f)
            history.append(trade)
            with open(HISTORY_PATH, "w") as f:
                json.dump(history, f, indent=2, default=str)
        except Exception as e:
            logger.warning(f"trade_history save failed: {e}")

    def get_today_trades_count(self) -> int:
        """Count completed + active trades executed today."""
        count = 0
        today_str = datetime.now().date().isoformat()
        try:
            if os.path.exists(HISTORY_PATH):
                with open(HISTORY_PATH, "r") as f:
                    history = json.load(f)
                    for t in history:
                        entry_time = str(t.get("entry_time", ""))
                        if entry_time.startswith(today_str):
                            count += 1
        except Exception:
            pass
        return count + len(self.open_trades)

    def _ensure_day_snapshot(self):
        _today = datetime.now().date()
        if self._day_key != _today:
            self._day_key = _today
            self._day_loss = 0.0
            self._consec_sl = 0
            self._day_paused = False
            current_funds = float(self.capital_engine.get_available_funds())
            self._day_start_margin = max(current_funds, 1.0)
            logger.info(f"[StockExecution] Day snapshot: start_margin = {self._day_start_margin:.2f}")

    def _run_with_timeout(self, func, *args, timeout=3.0, **kwargs):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(func, *args, **kwargs)
            try:
                return future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                logger.error(f"[StockExecution] 🚨 SDK Timeout ({timeout}s) on {func.__name__}")
                return None
            except Exception as e:
                logger.error(f"[StockExecution] 🚨 SDK Error on {func.__name__}: {e}")
                return None

    def _place_order(self, kwargs: dict):
        if PAPER_MODE:
            sim_ordno = f"SIM_{int(pytime.time() * 1000)}"
            logger.info(f"[StockExecution] 📝 [PAPER MODE] Simulated Order for {kwargs.get('tradingsymbol')} {kwargs.get('buy_or_sell')} {kwargs.get('quantity')}x @ {kwargs.get('price')} (ID: {sim_ordno})")
            return {"stat": "Ok", "norenordno": sim_ordno}
        for fn_name in ("place_order", "placeorder"):
            fn = getattr(self.api, fn_name, None)
            if fn:
                return self._run_with_timeout(fn, **kwargs)
        return None

    def _cancel_order(self, ordno: str):
        for fn_name in ("cancel_order", "cancelorder"):
            fn = getattr(self.api, fn_name, None)
            if fn:
                resp = self._run_with_timeout(fn, orderno=ordno)
                if not resp:
                    resp = self._run_with_timeout(fn, ordno)
                logger.warning(f"[StockExecution] Cancel sent for {ordno}: {resp}")
                return resp
        return None

    def _order_history(self, ordno: str):
        for fn_name in ("single_order_history", "singleorderhistory"):
            fn = getattr(self.api, fn_name, None)
            if fn:
                resp = self._run_with_timeout(fn, orderno=ordno)
                if not resp:
                    resp = self._run_with_timeout(fn, ordno)
                return resp
        return None

    def _position_qty(self, tsym: str):
        for fn_name in ("get_positions", "positions", "getposition"):
            fn = getattr(self.api, fn_name, None)
            if not fn:
                continue
            rows = self._run_with_timeout(fn)
            if isinstance(rows, list):
                net = 0.0
                for r in rows:
                    if str(r.get("tsym", "")) == tsym and str(r.get("exch", "")) == "NSE":
                        try:
                            net += float(r.get("netqty", 0))
                        except Exception:
                            pass
                return net
        return None

    def _confirm_fill(self, ordno: str, fallback_price: float):
        if str(ordno).startswith("SIM_"):
            return True, fallback_price, 10
        self._last_rej = ""
        avg_prc = fallback_price
        last_row = None
        for attempt in range(6):
            pytime.sleep(1.0)
            hist = self._order_history(ordno)
            row = None
            if isinstance(hist, list) and len(hist) > 0:
                row = hist[0]
            elif isinstance(hist, dict):
                row = hist
            if row:
                last_row = row
                status = str(row.get("status", "")).upper()
                if status in {"FILLED", "COMPLETE", "COMPLETED", "TRADED"}:
                    try:
                        p = (row.get("flprc") or row.get("avgprc") or row.get("prc") or fallback_price)
                        p = float(p)
                        fill_qty = int(row.get("fillshares") or row.get("fillqty") or 0)
                        if p > 0:
                            logger.info(f"[StockExecution] Order {ordno} TRADED | fill_price={p:.2f} | qty={fill_qty} (attempt {attempt+1})")
                            return True, p, fill_qty
                    except Exception:
                        pass
                elif status in {"REJECTED", "CANCELED", "CANCELLED"}:
                    rej = row.get("rejreason") or row.get("exch_rej_reason") or ""
                    self._last_rej = str(rej)
                    logger.error(f"[StockExecution] Order {ordno} {status} | reason: {rej} | full: {row}")
                    return False, fallback_price, 0
        if last_row:
            logger.error(f"[StockExecution] No fill confirmation for {ordno}; last: {last_row}")
        return False, avg_prc, 0

    def _resolve_tsym(self, symbol: str) -> str:
        return self._tsym.get(symbol, f"{symbol}-EQ")

    @classmethod
    def _marketable_limit(cls, action: str, spot_price: float) -> float:
        raw = spot_price * (1.0 + MKT_SLACK_PCT) if action == "B" else spot_price * (1.0 - MKT_SLACK_PCT)
        steps = raw / TICK_SIZE
        aligned = math.ceil(steps - 1e-9) * TICK_SIZE if action == "B" else math.floor(steps + 1e-9) * TICK_SIZE
        return round(aligned, 2)

    @staticmethod
    def _calc_txn_cost(entry: float, exit_price: float, qty: int, side: str) -> float:
        avg_notional = qty * ((entry + exit_price) / 2.0)
        return round(avg_notional * 0.001096, 2)

    @staticmethod
    def _slip_bps(fill: float, ref: float, side: str) -> float:
        if ref <= 0:
            return 0.0
        diff = (fill - ref) / ref if side == "BUY" else (ref - fill) / ref
        return round(diff * 1e4, 2)

    def _book_close(self, trade, symbol, price, reason, keep_open=False):
        entry = float(trade["entry_price"])
        qty = int(trade["qty"])
        side = trade["type"]
        gross_pnl = qty * (price - entry) if side == "BUY" else qty * (entry - price)
        txn_cost = self._calc_txn_cost(entry, price, qty, side)
        net_pnl = gross_pnl - txn_cost
        
        if reason == "STOP_LOSS" and net_pnl < 0:
            self._consec_sl += 1
            if self._consec_sl >= CONSEC_SL_LIMIT and not self._day_paused:
                self._day_paused = True
                logger.warning(f"[StockExecution] 🛑 CIRCUIT BREAKER: {self._consec_sl} consecutive SLs — entries paused for the day.")
        else:
            self._consec_sl = 0
        
        highest = float(trade.get("highest", entry))
        lowest = float(trade.get("lowest", entry))
        if side == "BUY":
            mfe = qty * (highest - entry)
            mae = qty * (lowest - entry)
        else:
            mfe = qty * (entry - lowest)
            mae = qty * (entry - highest)
        
        logger.info(f"[StockExecution] CLOSED: {symbol} {reason} | Gross={gross_pnl:.2f}, Cost={txn_cost:.2f}, Net={net_pnl:.2f}")
        trade.update({
            "exit_time": datetime.now().isoformat(), "exit_price": price,
            "gross_pl": round(gross_pnl, 2), "txn_cost": round(txn_cost, 2),
            "net_pl": round(net_pnl, 2), "exit_reason": reason,
            "mfe": round(mfe, 2), "mae": round(mae, 2),
            "strategy": "ARGUS",
        })
        self._day_loss += net_pnl
        self.symbol_last_exit_time[symbol] = pytime.time()
        self._save_history(trade)
        if not keep_open:
            self.open_trades.pop(symbol, None)
        self._save_open()
        
        # ─── RELEASE GLOBAL TRADE LOCK ───
        # Notify CascadeValueEngine that trade is closed so it can accept new signals
        try:
            from ARGUS.cascade.value_engine import CascadeValueEngine
            # Find the engine instance (it's attached to the broker or accessible via module)
            # We'll use a module-level reference or pass it during wiring
            if hasattr(self, '_cascade_engine') and self._cascade_engine:
                self._cascade_engine.handle_trade_closed(symbol)
        except Exception:
            pass  # Silent fail - lock release is best-effort

    def execute_entry(self, signal, symbol, spot_price, reason, metrics=None):
        if len(self.open_trades) >= MAX_OPEN_POSITIONS:
            logger.info(f"Global Lock Active: Ignoring {signal} for {symbol}.")
            return
        now_time = datetime.now().time()
        if now_time < time(9, 15) or now_time >= time(14, 45):
            return
        self._ensure_day_snapshot()
        if self._day_paused:
            logger.warning("[StockExecution] Circuit breaker active — skipping entry.")
            return
        today_trades = self.get_today_trades_count()
        if today_trades >= MAX_TRADES_PER_DAY:
            logger.warning(f"[StockExecution] 🛑 Daily trade limit reached ({today_trades}/{MAX_TRADES_PER_DAY}) — no new entries today.")
            return
        
        # ─── PER-SYMBOL COOLDOWN GATE (3 Minutes) ───
        cooldown_sec = float(EXIT_CFG.get("cooldown_seconds", 180))
        last_exit = self.symbol_last_exit_time.get(symbol, 0.0)
        if pytime.time() - last_exit < cooldown_sec:
            rem = int(cooldown_sec - (pytime.time() - last_exit))
            logger.info(f"[StockExecution] ⏳ Cooldown active for {symbol} ({rem}s remaining) — skipping entry.")
            return
        margin_budget = float(self.capital_engine.get_available_funds())
        if margin_budget < 50:
            logger.warning("Insufficient margin budget.")
            return
        loss_cap_limit = -DAILY_LOSS_CAP_PCT * self._day_start_margin
        if self._day_loss <= loss_cap_limit:
            logger.warning(f"Daily loss cap hit ({self._day_loss:.2f} <= {loss_cap_limit:.2f}) — no new entries today.")
            return
        notional_budget = margin_budget * self.leverage
        
        # ─── KELLY SIZING FROM ML PROB ───
        # Get Kelly fraction from metrics (passed from value_engine signal)
        kelly_fraction = float(metrics.get("kelly_fraction", 1.0)) if metrics else 1.0
        # Cap Kelly fraction at reasonable bounds
        kelly_fraction = max(0.0, min(kelly_fraction, 1.0))
        
        # Apply Kelly sizing to notional budget
        kelly_notional = notional_budget * kelly_fraction
        qty = int((kelly_notional * MARGIN_SAFETY) / max(spot_price, 0.01))
        if qty <= 0:
            return
        
        if logger.isEnabledFor(logging.INFO):
            logger.info(
                "[KELLY_EXEC] %s | Margin: %.0f | Leverage: %.1f | Notional: %.0f | "
                "Kelly: %.3f | Kelly_Notional: %.0f | Qty: %d"
                % (symbol, margin_budget, self.leverage, notional_budget, 
                   kelly_fraction, kelly_notional, qty)
            )
        atr = float(metrics.get("atr", 0.0)) if metrics else 0.0
        if atr > 0:
            logger.info(f"ATR15 exits: TP={atr*TP_ATR_MULT:.2f}, InitSL={atr*SL_ATR_MULT:.2f}, Chandelier={atr*CHANDELIER_ATR_MULT:.2f}")
        else:
            logger.warning("ATR not available — using fixed % fallback exits")
        tp_dist = atr * TP_ATR_MULT if atr > 0 else 0.0
        if metrics:
            d_opp = float(metrics.get("dist_supply_above", 0.0)) if signal == "BUY" else float(metrics.get("dist_demand_below", 0.0))
            if d_opp > 0:
                tp_dist = min(tp_dist, d_opp * 0.9) if tp_dist > 0 else d_opp * 0.9
        action = "B" if signal == "BUY" else "S"
        tsym = self._resolve_tsym(symbol)
        
        # ─── KYLE'S λ LIMIT PRICE ENGINE ───
        # Get DKLA lambdas from entry-time microstructure metrics
        entry_adv = self.adv_metrics_dict.get(symbol, {})
        lam_buy = entry_adv.get("lam_buy", 0.0)
        lam_sell = entry_adv.get("lam_sell", 0.0)
        mid_price = entry_adv.get("mp_val", spot_price)  # Microprice if available, else spot
        
        # P1 FIX: Get queue decay and spread for queue risk adjustment
        bid_queue_decay = entry_adv.get("bid_queue_decay", 0.0)
        ask_queue_decay = entry_adv.get("ask_queue_decay", 0.0)
        # Calculate spread in bps
        bp1 = entry_adv.get("bp1", 0.0)
        sp1 = entry_adv.get("sp1", 0.0)
        spread_bps = ((sp1 - bp1) / max(mid_price, 1e-6)) * 10000.0 if bp1 > 0 and sp1 > 0 else 0.0
        
        # Get conviction from metrics (passed from value_engine)
        conviction = float(metrics.get("conviction", 5.0)) if metrics else 5.0
        
        # Target notional for this order
        target_notional = qty * spot_price
        
        # Calculate optimal limit price using Kyle's λ
        limit_price = calculate_kyle_limit_price(
            side=signal,
            mid_price=mid_price,
            lam_buy=lam_buy,
            lam_sell=lam_sell,
            target_notional=target_notional,
            conviction=conviction,
            max_slippage_bps=5.0,
            bid_queue_decay=bid_queue_decay,
            ask_queue_decay=ask_queue_decay,
            spread_bps=spread_bps,
        )
        
        # Log Kyle pricing decision
        if logger.isEnabledFor(logging.INFO):
            kyle_impact_bps = (target_notional * (lam_buy if signal == "BUY" else lam_sell) / 2.0) / 10000.0
            queue_decay_used = ask_queue_decay if signal == "BUY" else bid_queue_decay
            queue_risk_bps = min(queue_decay_used * 0.04, 2.0)
            logger.info(
                "[KYLE_LIMIT] %s | Side: %s | Mid: %.2f | λ_buy: %.1fbps | λ_sell: %.1fbps | "
                "Conviction: %.1f | KyleImpact: %.1fbps | QueueDecay: %.1fbps | QueueRisk: %.1fbps | "
                "SpreadBps: %.1f | Limit: %.2f | Marketable: %.2f"
                % (symbol, signal, mid_price, lam_buy, lam_sell, conviction, 
                   kyle_impact_bps, queue_decay_used, queue_risk_bps,
                   spread_bps, limit_price, self._marketable_limit(action, spot_price))
            )
        
        attempt_qty = qty
        for attempt in range(2):
            kwargs = {
                "buy_or_sell": action, "product_type": "I", "exchange": "NSE",
                "tradingsymbol": tsym, "quantity": attempt_qty, "price_type": "LMT",
                "price": limit_price, "trigger_price": None, "discloseqty": 0, "retention": "DAY",
            }
            try:
                logger.info(f"[StockExecution] Placing LIVE {action} MKT-able LMT for {attempt_qty}x {symbol} @ {limit_price:.2f} (tsym={tsym}, tp_dist={tp_dist:.2f})...")
                t0 = pytime.time()
                resp = self._place_order(kwargs)
                if resp and isinstance(resp, dict) and "Invalid Trading Symbol" in str(resp.get("emsg", "")):
                    alt = symbol if tsym != symbol else f"{symbol}-EQ"
                    logger.warning(f"[StockExecution] tsym '{tsym}' rejected — retrying with '{alt}'")
                    kwargs["tradingsymbol"] = alt
                    resp2 = self._place_order(kwargs)
                    if resp2 and isinstance(resp2, dict) and str(resp2.get("stat", "")).upper() == "OK":
                        tsym = alt
                        resp = resp2
                if resp and isinstance(resp, dict) and str(resp.get("stat", "")).upper() == "OK" and resp.get("norenordno"):
                    ordno = str(resp["norenordno"])
                    filled, avg_prc, fillqty = self._confirm_fill(ordno, spot_price)
                    t1 = pytime.time()
                    if filled and fillqty > 0:
                        logger.info(f"[StockExecution] ✅ Entry FILLED @ {avg_prc:.2f} (qty={fillqty})")
                        margin = (fillqty * avg_prc) / self.leverage
                        
                        # ─── CAPTURE ENTRY-TIME MICROSTRUCTURE VALUES FOR DYNAMIC EXITS ───
                        entry_adv = self.adv_metrics_dict.get(symbol, {})
                        entry_bucket = self.bucket_aggregates_dict.get(symbol, {})
                        exit_state = ExitState()
                        exit_state.entry_time = pytime.time()
                        exit_state.entry_accel = entry_adv.get("mp_accel", 0.0)
                        exit_state.peak_accel_mag = abs(exit_state.entry_accel)
                        exit_state.entry_dkla_asym = entry_adv.get("dkla_asym", 0.0)
                        exit_state.entry_lra_vel = entry_adv.get("lra_vel", 0.0)
                        net_vac = entry_bucket.get("net_vacuum_bps", 0.0)
                        side_mult = 1 if signal == "BUY" else -1
                        exit_state.peak_vacuum = net_vac * side_mult
                        exit_state.peak_vacuum_z = 0.0
                        exit_state.breakeven_locked = False
                        exit_state.confirmed_decay_count = 0
                        
                        trade = {
                            "entry_time": datetime.now().isoformat(), "symbol": symbol, "tsym": tsym,
                            "type": signal, "entry_price": avg_prc, "qty": fillqty, "margin": round(margin, 2),
                            "leverage": self.leverage, "reason": reason, "broker_order_id": ordno,
                            "atr": round(atr, 4), "tp_dist": round(tp_dist, 4),
                            "highest": avg_prc, "lowest": avg_prc,
                            "entry_latency_s": round(t1 - t0, 2),
                            "entry_slip_bps": self._slip_bps(avg_prc, spot_price, signal),
                            "ml_features": metrics or {},
                            "exit_state": exit_state,
                        }
                        self.open_trades[symbol] = trade
                        self._save_open()
                        if hasattr(self, '_cascade_engine') and self._cascade_engine:
                            self._cascade_engine.set_active_trade(symbol)
                        
                        if fillqty < attempt_qty:
                            self._cancel_order(ordno)
                            logger.warning(f"[StockExecution] ⚠️ Partial entry fill ({fillqty}/{attempt_qty}). Remainder cancelled.")
                        return
                    
                    self._cancel_order(ordno)
                    net_qty = self._position_qty(tsym)
                    if net_qty and abs(net_qty) > 0:
                        logger.warning(f"[StockExecution] 🚨 Orphan fill detected during non-fill! Booking {abs(net_qty)} shares.")
                        margin = (abs(net_qty) * avg_prc) / self.leverage
                        
                        # ─── CAPTURE ENTRY-TIME MICROSTRUCTURE VALUES FOR DYNAMIC EXITS (ORPHAN RECOVERY) ───
                        entry_adv = self.adv_metrics_dict.get(symbol, {})
                        entry_bucket = self.bucket_aggregates_dict.get(symbol, {})
                        exit_state = ExitState()
                        exit_state.entry_time = pytime.time()
                        exit_state.entry_accel = entry_adv.get("mp_accel", 0.0)
                        exit_state.peak_accel_mag = abs(exit_state.entry_accel)
                        exit_state.entry_dkla_asym = entry_adv.get("dkla_asym", 0.0)
                        exit_state.entry_lra_vel = entry_adv.get("lra_vel", 0.0)
                        net_vac = entry_bucket.get("net_vacuum_bps", 0.0)
                        side_mult = 1 if signal == "BUY" else -1
                        exit_state.peak_vacuum = net_vac * side_mult
                        exit_state.peak_vacuum_z = 0.0
                        exit_state.breakeven_locked = False
                        exit_state.confirmed_decay_count = 0
                        
                        trade = {
                            "entry_time": datetime.now().isoformat(), "symbol": symbol, "tsym": tsym,
                            "type": signal, "entry_price": avg_prc, "qty": int(abs(net_qty)), "margin": round(margin, 2),
                            "leverage": self.leverage, "reason": "Orphan_Recovery", "broker_order_id": ordno,
                            "atr": round(atr, 4), "tp_dist": round(tp_dist, 4),
                            "highest": avg_prc, "lowest": avg_prc,
                            "entry_latency_s": round(t1 - t0, 2),
                            "entry_slip_bps": self._slip_bps(avg_prc, spot_price, signal),
                            "ml_features": metrics or {},
                            "exit_state": exit_state,
                        }
                        self.open_trades[symbol] = trade
                        self._save_open()
                        if hasattr(self, '_cascade_engine') and self._cascade_engine:
                            self._cascade_engine.set_active_trade(symbol)
                        return
                    
                    if "margin" in self._last_rej.lower() and attempt == 0:
                        attempt_qty = max(1, int(attempt_qty * 0.95))
                        logger.warning(f"[StockExecution] ⚠️ Margin shortfall — retrying with qty {attempt_qty}")
                        continue
                    logger.warning("[StockExecution] ⚠️ Entry not confirmed — no position booked.")
                    return
                else:
                    logger.error(f"[StockExecution] 🚨 API REJECTION: {resp}")
                    return
            except Exception as e:
                logger.error(f"[StockExecution] Exception in execute_entry: {e}")
                return

    # ══════════════════════════════════════════════════════════════════
    # MANAGE OPEN TRADES — NOW WITH DYNAMIC MICROSTRUCTURE DECAY EXITS
    # ══════════════════════════════════════════════════════════════════
    def manage_open_trades(self, current_prices):
        now_time = datetime.now().time()
        is_eod_flatten = now_time >= EOD_FLATTEN_TIME
        
        for symbol, trade in list(self.open_trades.items()):
            if symbol not in current_prices:
                if not is_eod_flatten:
                    continue
                current_price = float(trade.get("highest", trade.get("entry_price")))
            else:
                current_price = float(current_prices[symbol])
            
            entry = float(trade["entry_price"])
            atr = float(trade.get("atr", 0.0))
            side = trade["type"]
            
            # ─── GET MICROSTRUCTURE METRICS ───
            adv = self.adv_metrics_dict.get(symbol, {})
            bucket_aggs = self.bucket_aggregates_dict.get(symbol, {})
            
            # ─── ENSURE EXIT STATE EXISTS ───
            # After a restart, exit_state loads from JSON as a str (default=str in
            # _save_open) — rebuild it so the trade stays manageable. Entry-time
            # microstructure is lost on restart; evaluate_exit re-initializes on
            # first call from live metrics.
            if "exit_state" not in trade or not isinstance(trade["exit_state"], ExitState):
                trade["exit_state"] = ExitState()
            exit_state = trade["exit_state"]
            
            # ─── EVALUATE DYNAMIC EXIT ───
            should_exit, reason = evaluate_exit(
                position_direction=side,
                entry_price=entry,
                current_lp=current_price,
                adv_metrics=adv,
                bucket_aggregates=bucket_aggs,
                trade_state=exit_state,
                atr=atr,
                current_time=pytime.time(),
            )
            
            # ─── LEGACY ATR/CHANDELIER/BREAKEVEN LOGIC (FALLBACK ONLY) ───
            legacy_exit = None
            if not should_exit and not is_eod_flatten:
                try:
                    minutes_held = (datetime.now() - datetime.fromisoformat(trade["entry_time"])).total_seconds() / 60.0
                except Exception:
                    minutes_held = 0.0
                tp_dist = float(trade.get("tp_dist", 0.0))
                if tp_dist <= 0:
                    tp_dist = atr * TP_ATR_MULT if atr > 0 else 0.0
                
                if tp_dist > 0:
                    if side == "BUY" and current_price >= entry + tp_dist:
                        legacy_exit = "TAKE_PROFIT"
                    elif side == "SELL" and current_price <= entry - tp_dist:
                        legacy_exit = "TAKE_PROFIT"
                elif atr <= 0:
                    pnl_pct = (current_price - entry) / entry if side == "BUY" else (entry - current_price) / entry
                    if pnl_pct >= FALLBACK_TP_PCT:
                        legacy_exit = "TAKE_PROFIT"
                
                if legacy_exit is None and atr > 0:
                    if side == "BUY":
                        prev_highest = float(trade.get("highest", entry))
                        trade["highest"] = max(prev_highest, current_price)
                        chan_stop = float(trade["highest"]) - atr * CHANDELIER_ATR_MULT
                        init_stop = entry - atr * SL_ATR_MULT
                        stop_price = max(init_stop, chan_stop)
                        be_lock = False
                        # MN-6 gap detection: a single-tick move > GAP_ATR_MULT*ATR past
                        # the prior high is a discontinuous gap, not a trend advance. Locking
                        # the stop at breakeven on a gap-open would invert it exactly at entry
                        # as price gaps through it, so skip the floor on gap ticks.
                        gap_open = (current_price - prev_highest) > GAP_ATR_MULT * atr
                        if float(trade["highest"]) >= entry + BREAKEVEN_ATR_FRACTION * atr and not gap_open:
                            if entry > stop_price:
                                stop_price = entry
                                be_lock = True
                        elif gap_open and float(trade["highest"]) >= entry + BREAKEVEN_ATR_FRACTION * atr:
                            logger.info(f"[MN-6] Gap-open detected ({current_price - prev_highest:.2f} > {GAP_ATR_MULT*atr:.2f}) — breakeven floor lock skipped for {symbol}.")
                        if current_price <= stop_price:
                            legacy_exit = "BREAKEVEN_EXIT" if be_lock else ("CHANDELIER_EXIT" if chan_stop > init_stop else "STOP_LOSS")
                    else:
                        prev_lowest = float(trade.get("lowest", entry))
                        trade["lowest"] = min(prev_lowest, current_price)
                        chan_stop = float(trade["lowest"]) + atr * CHANDELIER_ATR_MULT
                        init_stop = entry + atr * SL_ATR_MULT
                        stop_price = min(init_stop, chan_stop)
                        be_lock = False
                        gap_open = (prev_lowest - current_price) > GAP_ATR_MULT * atr
                        if float(trade["lowest"]) <= entry - BREAKEVEN_ATR_FRACTION * atr and not gap_open:
                            # For a SHORT the stop sits ABOVE the trade; breakeven tightens any
                            # loss-allowing stop (stop above entry) down to entry.
                            if entry < stop_price:
                                stop_price = entry
                                be_lock = True
                        elif gap_open and float(trade["lowest"]) <= entry - BREAKEVEN_ATR_FRACTION * atr:
                            logger.info(f"[MN-6] Gap-down detected ({prev_lowest - current_price:.2f} > {GAP_ATR_MULT*atr:.2f}) — breakeven floor lock skipped for {symbol}.")
                        if current_price >= stop_price:
                            legacy_exit = "BREAKEVEN_EXIT" if be_lock else ("CHANDELIER_EXIT" if chan_stop < init_stop else "STOP_LOSS")
                elif legacy_exit is None and atr <= 0:
                    pnl_pct = (current_price - entry) / entry if side == "BUY" else (entry - current_price) / entry
                    if pnl_pct <= -FALLBACK_SL_PCT:
                        legacy_exit = "STOP_LOSS"
                
                if legacy_exit is None and minutes_held >= MAX_HOLD_MINUTES:
                    legacy_exit = "TIME_STOP"
            
            exit_reason = reason if should_exit else legacy_exit
            if exit_reason is None and is_eod_flatten:
                exit_reason = "EOD_FLATTEN"
            
            if exit_reason:
                self._close_trade(symbol, current_price, exit_reason)

    def _close_trade(self, symbol, current_price, exit_reason):
        trade = self.open_trades.get(symbol)
        if not trade:
            return
        attempts = int(trade.get("exit_attempts", 0)) + 1
        trade["exit_attempts"] = attempts
        tsym = str(trade.get("tsym", self._resolve_tsym(symbol)))
        
        if attempts > MAX_EXIT_ATTEMPTS:
            net = self._position_qty(tsym)
            if net == 0.0:
                logger.warning(f"[StockExecution] Reconcile: broker flat on {symbol} — booking BROKER_SQUAREOFF.")
                self._book_close(trade, symbol, current_price, "BROKER_SQUAREOFF")
                return
        
        qty = int(trade["qty"])
        action = "S" if trade["type"] == "BUY" else "B"
        limit_price = self._marketable_limit(action, current_price)
        kwargs = {
            "buy_or_sell": action, "product_type": "I", "exchange": "NSE",
            "tradingsymbol": tsym, "quantity": qty, "price_type": "LMT",
            "price": limit_price, "trigger_price": None, "discloseqty": 0, "retention": "DAY",
        }
        try:
            logger.info(f"[StockExecution] Exiting {qty}x {symbol} ({exit_reason}) @ {limit_price:.2f}...")
            t0 = pytime.time()
            resp = self._place_order(kwargs)
            emsg = str(resp.get("emsg", "")) if isinstance(resp, dict) else ""
            if "square off" in emsg.lower():
                net = self._position_qty(tsym)
                if net == 0.0:
                    self._book_close(trade, symbol, current_price, "BROKER_SQUAREOFF")
                    return
            if resp and isinstance(resp, dict) and str(resp.get("stat", "")).upper() == "OK" and resp.get("norenordno"):
                ordno = str(resp["norenordno"])
                filled, avg_prc, fillqty = self._confirm_fill(ordno, current_price)
                t1 = pytime.time()
                if not filled or fillqty == 0:
                    logger.warning("[StockExecution] ⚠️ Exit not confirmed — will retry next cycle.")
                    self._cancel_order(ordno)
                    net_qty = self._position_qty(tsym)
                    if net_qty is not None:
                        actual_qty = int(abs(net_qty))
                        if actual_qty != trade["qty"]:
                            logger.warning(f"[StockExecution] 🚨 Exit reconciliation: Broker qty is {actual_qty}, expected {trade['qty']}. Updating.")
                            if actual_qty == 0:
                                self._book_close(trade, symbol, current_price, exit_reason)
                            else:
                                trade["qty"] = actual_qty
                                self._save_open()
                    return
                
                if fillqty < qty:
                    logger.warning(f"[StockExecution] Partial exit fill: {fillqty}/{qty}. Modifying trade quantity.")
                    self._cancel_order(ordno)
                    trade["qty"] -= fillqty
                    closed_trade = copy.deepcopy(trade)
                    closed_trade["qty"] = fillqty
                    closed_trade["exit_broker_order_id"] = ordno
                    closed_trade.pop("exit_attempts", None)
                    closed_trade["exit_latency_s"] = round(t1 - t0, 2)
                    closed_trade["exit_slip_bps"] = self._slip_bps(avg_prc, current_price, trade["type"])
                    self._book_close(closed_trade, symbol, avg_prc, exit_reason, keep_open=True)
                    self._save_open()
                    return
                
                trade["exit_broker_order_id"] = ordno
                trade.pop("exit_attempts", None)
                trade["exit_latency_s"] = round(t1 - t0, 2)
                trade["exit_slip_bps"] = self._slip_bps(avg_prc, current_price, trade["type"])
                self._book_close(trade, symbol, avg_prc, exit_reason)
            else:
                logger.error(f"[StockExecution] 🚨 EXIT REJECTION: {resp}")
        except Exception as e:
            logger.error(f"[StockExecution] Exception in _close_trade: {e}")

    # ══════════════════════════════════════════════════════════════════
    # MONITORING / STATUS
    # ══════════════════════════════════════════════════════════════════
    def get_open_positions_summary(self) -> str:
        if not self.open_trades:
            return "No open positions"
        lines = []
        for sym, t in self.open_trades.items():
            lp = self.prices_dict.get(sym, t.get("entry_price", 0))
            pnl = (lp - t["entry_price"]) * t["qty"] if t["type"] == "BUY" else (t["entry_price"] - lp) * t["qty"]
            exit_state = t.get("exit_state")
            decay_info = ""
            if exit_state and exit_state.entry_time > 0:
                peak_accel = exit_state.peak_accel_mag
                peak_vac = exit_state.peak_vacuum
                be_locked = exit_state.breakeven_locked
                decay_info = f" | PeakAccel:{peak_accel:.3f} PeakVac:{peak_vac:.0f}bps BE:{'Y' if be_locked else 'N'}"
            lines.append(f"{sym} {t['type']} {t['qty']}@{t['entry_price']:.2f} -> {lp:.2f} (PnL:{pnl:.0f}){decay_info}")
        return "\n".join(lines)

def attach_execution_manager(broker) -> StockExecutionManager:
    """Factory function to create and start the Execution Manager."""
    mgr = StockExecutionManager(broker)
    mgr.start()
    return mgr
