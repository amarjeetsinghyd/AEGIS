"""
AEGIS PriceActionEngine — End-to-End Pure Price Action & Microstructure Scalper Engine.

Implements the 4 empirically verified, zero-lag price action scalp pillars:
  Pillar 1: 20 EMA + Wick Rejection (User's System — #1 Performer on NIFTY & SENSEX)
  Pillar 2: Al Brooks 2-Legged Pullback / Micro-Sweep Reclaim (H2/L2 Sweep into 50 EMA)
  Pillar 3: Volatility Squeeze to Expansion Breakout (3-Bar Tight Box Expansion)
  Pillar 4: Break of Structure (BOS Level Breakout above PDH/Pivot or below PDL/Pivot)

Key Architectural Principles:
  - Scale-Invariant: Buffer points and tolerances dynamically normalize to index spot price.
  - Zero Cold-Start Warmup: Historical 1m bars seeded at 09:14 AM via REST API.
  - Watertight: Complete independence from Kaufman KER, Chop Gates, and multi-domain vetoes.
  - Real-Time Dynamic: In-flight EMA and slope updates without mutating closed bar state.
"""

from __future__ import annotations
import math
import logging
import os
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any

import numpy as np
try:
    from AEGIS.core.aegis_v4_core import FastMonotonicStack
except ImportError:
    try:
        from core.aegis_v4_core import FastMonotonicStack
    except ImportError:
        FastMonotonicStack = None

try:
    from common.logger import get_logger
    logger = get_logger("aegis_price_action")
except Exception:
    logger = logging.getLogger("aegis_price_action")


@dataclass
class OHLCBar:
    ts_start: float
    ts_end: float
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    tick_count: int = 0
    # ─── 1-Minute Option Flow (Institutional Delta Footprint) ───
    call_buy_vol: float = 0.0      # Lifted at Ask (Aggressive Call Buying)
    call_sell_vol: float = 0.0     # Hit at Bid (Call Writing / Liquidation)
    put_buy_vol: float = 0.0       # Lifted at Ask (Aggressive Put Buying)
    put_sell_vol: float = 0.0      # Hit at Bid (Put Writing / Liquidation)
    opt_delta_flow: float = 0.0    # Directional Flow: call_buy_vol - put_buy_vol
    opt_total_vol: float = 0.0     # Total Option Traded Volume across ATM window
    opt_pcr: float = 1.0           # Put Volume / Call Volume ratio

    @property
    def ts(self) -> float:
        """Alias ts to ts_end for timestamp compatibility."""
        return self.ts_end


@dataclass
class DowStructureChain:
    """Tracks the 4-point Classical Dow Theory sequence for Pillar 1."""
    is_valid: bool = False
    veto_reason: str = "uninitialized"
    # Indices in bars slice
    idx_1: int = -1       # HH1 (for CALL) or LL1 (for PUT)
    idx_touch1: int = -1  # HL1 (for CALL) or LH1 (for PUT) - 1st Touch (No Entry)
    idx_2: int = -1       # HH2 (for CALL) or LL2 (for PUT)
    idx_touch2: int = -1  # HL2 (for CALL) or LH2 (for PUT) - 2nd Touch (Entry)
    # Price levels
    impulse_1: float = 0.0   # HH1 or LL1 price
    touch_1: float = 0.0     # HL1 or LH1 price
    impulse_2: float = 0.0   # HH2 or LL2 price
    touch_2: float = 0.0     # HL2 or LH2 price
    pullback_bars: int = 0   # Exact candle count from HH2->HL2 or LL2->LH2
    orderly_ratio: float = 0.0
    imp_avg_vol: float = 0.0
    pb_avg_vol: float = 0.0
    entry_vol: float = 0.0
    swing_vdr: float = 1.0
    imp_directional_flow: float = 0.0
    pb_counter_flow: float = 0.0


class PriceActionEngine:
    """
    Dedicated, end-to-end engine for real-time 1m bar aggregation, EMA tracking,
    ATR calculation, intraday VWAP, and the 3 core Price Action scalper setups.
    """

    def __init__(
        self,
        strike_step: float = 50.0,
        max_history_bars: int = 120,
        atr_period: int = 14,
        lot_size: Optional[float] = None,
    ):
        self.strike_step = strike_step
        self.max_history_bars = max_history_bars
        self.atr_period = atr_period

        # Adaptive lot size (SENSEX=20, NIFTY=65)
        if lot_size is not None:
            self.lot_size = float(lot_size)
        elif strike_step == 100.0:
            self.lot_size = 20.0
        else:
            self.lot_size = float(os.getenv("AEGIS_LOT_SIZE", "65.0"))

        # ─── 1-Minute and 5-Minute Bar Building State ───
        self._curr_1m_bar: Optional[OHLCBar] = None
        self._curr_1m_bucket: int = -1
        self.bars_1m: deque[OHLCBar] = deque(maxlen=max_history_bars)
        self._curr_5m_bar: Optional[OHLCBar] = None
        self.bars_5m: deque[OHLCBar] = deque(maxlen=200)


        # ─── Real-Time 1-Minute Option Flow Accumulator (ATM ± 7 Window) ───
        self._opt_flow_1m = {
            "call_buy_vol": 0.0,
            "call_sell_vol": 0.0,
            "put_buy_vol": 0.0,
            "put_sell_vol": 0.0,
            "unclass_vol": 0.0,
        }
        self._opt_last_cum_vol: Dict[str, float] = {}
        self._opt_last_lp: Dict[str, float] = {}
        self._opt_last_frac: Dict[str, float] = {}

        # ─── 4-Line Trapped-Buyer Pain Zone VWAP Accumulators ───
        self._cum_cb_pv: float = 0.0
        self._cum_cb_vol: float = 0.0
        self._cum_cs_pv: float = 0.0
        self._cum_cs_vol: float = 0.0
        self._cum_pb_pv: float = 0.0
        self._cum_pb_vol: float = 0.0
        self._cum_ps_pv: float = 0.0
        self._cum_ps_vol: float = 0.0

        # ─── Price & Tick State ───
        self.last_price: float = 0.0
        self.last_ts: float = 0.0

        # ─── EMA States (Closed Bar State) ───
        self.ema_20_1m: float = 0.0
        self.ema_20_1m_prev: float = 0.0
        self.ema_20_1m_slope: float = 0.0
        # Per-close 20EMA history, index-aligned with bars_1m tail (two-touch confirmation)
        self.ema_20_history: deque[float] = deque(maxlen=max_history_bars)
        self.ema_20_high_1m: float = 0.0
        self.ema_20_low_1m: float = 0.0
        self.ema_20_high_history: deque[float] = deque(maxlen=max_history_bars)
        self.ema_20_low_history: deque[float] = deque(maxlen=max_history_bars)

        self.ema_50_1m: float = 0.0
        self.ema_50_1m_prev: float = 0.0
        self.ema_50_1m_slope: float = 0.0

        # ─── Volatility State ───
        self.atr_history: deque[float] = deque(maxlen=max(100, atr_period * 3))
        self.current_atr_1m: float = 0.0
        self.baseline_atr_1m: float = 0.0
        self.atr_ratio: float = 1.0
        self.slope_veto_bps: float = 0.5

        # ─── Institutional VWAP State ───
        self._vwap_cum_vol_price: float = 0.0
        self._vwap_cum_vol: float = 0.0
        self.current_vwap: float = 0.0
        self._current_vwap_day: int = -1

        # ─── Daily Structural Levels (PDH / PDL / PDC) ───
        self.pdh: float = 0.0
        self.pdl: float = 0.0
        self.pdc: float = 0.0
        self.proximity_threshold_bps: float = float(os.getenv("AEGIS_KEY_LEVEL_PROXIMITY_BPS", "8.0"))
        # ─── HVG Topological Engine (Pillar 4 Legacy/Shadow) ───
        self.hvg_engine: Any = None
        # ─── AEGIS V4.0 Topological 0-D Prominence Stack (Pillar 4 Final) ───
        self.prominence_stack: Any = FastMonotonicStack(capacity=128) if FastMonotonicStack is not None else None
        self._l2_depth_history: Optional[List[Tuple[float, float, float, float]]] = None
        # ─── ML Gatekeeper Flow Recorder Reference ───
        self.ml_gatekeeper: Any = None

    # ═══════════════════════════════════════════════════════════
    # 1. REAL-TIME TICK AGGREGATION & INDICATOR UPDATES
    # ═══════════════════════════════════════════════════════════

    def update_tick(self, price: float, ts_sec: float) -> None:
        """
        Updates the engine with incoming underlying spot index ticks.
        Builds 1-minute OHLC bars on 60-second boundaries and maintains live VWAP.
        """
        if price <= 0 or ts_sec <= 0:
            return

        self.last_price = price
        self.last_ts = ts_sec

        # 1. Intraday VWAP & Option Flow Day Reset
        day_bucket = int(ts_sec // 86400)
        if self._current_vwap_day != day_bucket:
            self._vwap_cum_vol_price = 0.0
            self._vwap_cum_vol = 0.0
            self.current_vwap = 0.0
            self._current_vwap_day = day_bucket
            self._reset_option_day_state()

        # 2. 1-Minute Bar Aggregation
        bucket_1m = int(ts_sec // 60)
        if bucket_1m != self._curr_1m_bucket:
            if self._curr_1m_bar is not None:
                self._on_1m_bar_close(self._curr_1m_bar)
            self._curr_1m_bucket = bucket_1m
            self._curr_1m_bar = OHLCBar(
                ts_start=float(bucket_1m * 60),
                ts_end=float((bucket_1m + 1) * 60),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=1.0,
                tick_count=1,
            )
        else:
            bar = self._curr_1m_bar
            if bar is not None:
                if price > bar.high:
                    bar.high = price
                if price < bar.low:
                    bar.low = price
                bar.close = price
                bar.volume += 1.0
                bar.tick_count += 1

    def _reset_option_day_state(self) -> None:
        """Resets option flow accumulators and tracking state on session rollover."""
        self._opt_last_cum_vol.clear()
        self._opt_last_lp.clear()
        self._opt_last_frac.clear()
        self._cum_cb_pv = 0.0
        self._cum_cb_vol = 0.0
        self._cum_cs_pv = 0.0
        self._cum_cs_vol = 0.0
        self._cum_pb_pv = 0.0
        self._cum_pb_vol = 0.0
        self._cum_ps_pv = 0.0
        self._cum_ps_vol = 0.0

    def _classify_buy_fraction(self, sym: str, lp: float, bp1: float, sp1: float) -> Optional[float]:
        """
        Buyer-initiated fraction (0.0-1.0) using Touch-first classification
        with Lee-Ready Tick Rule fallback. Returns None if unclassifiable.
        """
        valid_quote = bp1 > 0 and sp1 > 0 and sp1 > bp1

        if valid_quote and lp >= sp1:
            frac = 1.0                          # Lifted the ask — pure buy
        elif valid_quote and lp <= bp1:
            frac = 0.0                          # Hit the bid — pure sell
        else:
            # Inside spread OR broken/locked quote -> Tick Rule (Lee-Ready fallback)
            prev_lp = self._opt_last_lp.get(sym)
            if prev_lp is None or prev_lp <= 0:
                self._opt_last_lp[sym] = lp
                return None
            if lp > prev_lp:
                frac = 1.0
            elif lp < prev_lp:
                frac = 0.0
            else:
                frac = self._opt_last_frac.get(sym)   # Zero-tick: carry forward
                if frac is None:
                    self._opt_last_lp[sym] = lp
                    return None

        self._opt_last_lp[sym] = lp
        self._opt_last_frac[sym] = frac
        return frac

    def get_pain_zone_vwaps(self, lot_size: Optional[float] = None, min_bars: Optional[int] = None) -> Dict[str, float]:
        """
        Returns the 4-Line Trapped-Buyer Pain Zone VWAPs and single Pain Zone band.
        Single source of truth — no separate composite counter. The band is:
          zone_top = max(4 lines), zone_bottom = min(4 lines), zone_mid = composite.
        Above zone_top -> only CALLs, below zone_bottom -> only PUTs, inside -> chop.
        """
        eff_lot = float(lot_size) if lot_size is not None else getattr(self, "lot_size", 65.0)
        min_vol = float(os.getenv("AEGIS_PAIN_ZONE_MIN_LOTS", "200")) * eff_lot
        req_bars = int(os.getenv("AEGIS_PAIN_ZONE_MIN_BARS", "30")) if min_bars is None else int(min_bars)
        time_ok = (len(self.bars_1m) >= req_bars) if req_bars > 0 else True

        def _v(pv: float, v: float) -> float:
            return pv / v if (time_ok and v >= min_vol) else 0.0

        cb = _v(self._cum_cb_pv, self._cum_cb_vol)
        cs = _v(self._cum_cs_pv, self._cum_cs_vol)
        pb = _v(self._cum_pb_pv, self._cum_pb_vol)
        ps = _v(self._cum_ps_pv, self._cum_ps_vol)
        lines = [x for x in (cb, cs, pb, ps) if x > 0]
        zone_top = max(lines) if lines else 0.0
        zone_bottom = min(lines) if lines else 0.0
        tot_pv = self._cum_cb_pv + self._cum_cs_pv + self._cum_pb_pv + self._cum_ps_pv
        tot_v = self._cum_cb_vol + self._cum_cs_vol + self._cum_pb_vol + self._cum_ps_vol
        zone_mid = tot_pv / tot_v if (time_ok and tot_v >= min_vol) else 0.0

        return {
            "vwap_call_buy": cb,
            "vwap_call_sell": cs,
            "vwap_put_buy": pb,
            "vwap_put_sell": ps,
            "vwap_composite": zone_mid,
            "zone_top": zone_top,
            "zone_bottom": zone_bottom,
            "zone_mid": zone_mid,
        }

    # ATR buffer multiple for the absolute band veto (V4.1b). The raw zone is
    # razor-thin (~0.2-7 pts on NIFTY): gating on raw edges vetoes ~90% of ticks
    # and whole minutes single-sided on 1-2 pt margins. The buffer keeps the
    # intent (deep trapped zones still veto) while passing buffer-range noise.
    BAND_VETO_ATR_BUFFER_MULT = 0.3

    def band_veto_blocks(self, direction: str, spot: Optional[float] = None) -> Tuple[bool, float, float, float]:
        """Absolute Pain Zone band decision with ATR buffer.

        Returns (blocked, zone_top, zone_bottom, buffer). Warmup (lines at 0)
        or missing spot/ATR never blocks. UP blocked only when spot is
        decisively below the band (spot < top - buf); DOWN only when spot is
        decisively above it (spot > bottom + buf). Inside band+buffer both pass.
        """
        curr_spot = float(spot or self.last_price or 0.0)
        try:
            pz = self.get_pain_zone_vwaps()
        except Exception:
            return False, 0.0, 0.0, 0.0
        top = float(pz.get("zone_top", 0.0) or 0.0)
        bottom = float(pz.get("zone_bottom", 0.0) or 0.0)
        atr = float(getattr(self, "current_atr_1m", 0.0) or 0.0)
        if top <= 0 or bottom <= 0 or curr_spot <= 0 or atr <= 0:
            return False, top, bottom, 0.0
        buf = self.BAND_VETO_ATR_BUFFER_MULT * atr
        is_up = str(direction).upper() in ("UP", "CALL", "CE", "C")
        if is_up:
            return (curr_spot < top - buf), top, bottom, buf
        return (curr_spot > bottom + buf), top, bottom, buf

    def get_flow_symmetry_diagnostic(self) -> Dict[str, Any]:
        """
        EOD Classifier Symmetry Diagnostic per DeepSeek V4 Pro.
        Evaluates overall buyer vs seller balance across Call and Put contracts.
        Ratio = (CallBuy + PutBuy) / Total Volume.
        Acceptable institutional equilibrium is 0.45 - 0.55.
        """
        cb = self._cum_cb_vol
        cs = self._cum_cs_vol
        pb = self._cum_pb_vol
        ps = self._cum_ps_vol
        tot = cb + cs + pb + ps
        buyer_vol = cb + pb
        ratio = (buyer_vol / tot) if tot > 0 else 0.50
        is_balanced = (0.45 <= ratio <= 0.55) if tot >= 5000.0 else True
        alarm = not is_balanced
        return {
            "total_option_vol": tot,
            "call_buy_vol": cb,
            "call_sell_vol": cs,
            "put_buy_vol": pb,
            "put_sell_vol": ps,
            "buyer_ratio": round(ratio, 4),
            "is_balanced": is_balanced,
            "alarm": alarm,
        }


    def evaluate_pain_zone_confluence(
        self,
        pillar: int,
        direction: str,
        spot: Optional[float] = None,
        atr: Optional[float] = None,
        gamma_regime: str = "NEUTRAL",
    ) -> Tuple[bool, float, str, Dict[str, Any]]:
        """
        Evaluates 4-Line Trapped-Buyer Pain Zone Confluence per DeepSeek V4 Pro Institutional Ruleset.
        Returns: (is_allowed: bool, score_delta: float, reason: str, metrics: dict)
        Score Delta:
          +15: 2 fuel lines hit (opposite buyer + writer trapped)
          +10: 1 fuel line hit
            0: neutral (within ±0.3 ATR or warmup < 200 lots)
          -10: 1 veto line hit (0.5 ATR) -> soft veto (requires extra confirmation)
          Hard Block (is_allowed=False): Veto >= 1.0 ATR or explicit fade condition.
        """
        curr_spot = float(spot or self.last_price or 0.0)
        curr_atr = float(atr or self.current_atr_1m or 10.0)
        if curr_atr <= 0:
            curr_atr = 10.0

        pz = self.get_pain_zone_vwaps()
        cb = pz.get("vwap_call_buy", 0.0)
        cs = pz.get("vwap_call_sell", 0.0)
        pb = pz.get("vwap_put_buy", 0.0)
        ps = pz.get("vwap_put_sell", 0.0)

        # Warmup gate check: if any key line is 0, lines are inactive (neutral 0)
        if curr_spot <= 0 or (cb <= 0 and pb <= 0):
            return True, 0.0, "warmup_neutral", {"pain_zone_active": False}

        is_ce = direction.upper() in ("UP", "CALL")
        is_pe = direction.upper() in ("DOWN", "PUT")
        gamma = gamma_regime.upper()

        # Dynamic buffer based on gamma regime
        # Positive gamma (pinning): tighter boundaries. Negative gamma (dealers amplify): wider buffer
        veto_buffer = (0.75 * curr_atr) if gamma == "NEGATIVE" else (0.50 * curr_atr)
        if gamma == "POSITIVE" and pillar in (3, 4):
            veto_buffer = 0.25 * curr_atr

        metrics = {
            "spot": curr_spot,
            "atr": curr_atr,
            "vwap_call_buy": cb,
            "vwap_call_sell": cs,
            "vwap_put_buy": pb,
            "vwap_put_sell": ps,
            "gamma_regime": gamma,
            "pain_zone_active": True,
        }

        # ─── P1: 20 EMA Wick Rejection ───
        if pillar == 1:
            if is_ce:
                # Veto Check 1: spot < CallBuy - 0.5 ATR (Call buyers trapped, will dump on bounce)
                if cb > 0 and curr_spot < (cb - veto_buffer):
                    if curr_spot < (cb - 1.0 * curr_atr):
                        return False, -999.0, "hard_block_call_buyer_pain_severe", metrics
                    return True, -10.0, "veto_call_buyer_dump_risk", metrics
                # Veto Check 2: spot < PutSell - 0.5 ATR (Put writers broken, downside capitulation risk)
                if ps > 0 and curr_spot < (ps - veto_buffer):
                    return True, -10.0, "veto_put_seller_broken_risk", metrics
                
                # Boost conditions:
                fuel_count = 0
                if pb > 0 and curr_spot > pb:  # Pullback trapped Put buyers
                    fuel_count += 1
                if cs > 0 and curr_spot > cs:  # Call writers underwater
                    fuel_count += 1
                
                if fuel_count >= 2:
                    return True, 15.0, "p1_ce_dual_fuel_boost", metrics
                elif fuel_count == 1:
                    return True, 10.0, "p1_ce_single_fuel_boost", metrics
                return True, 0.0, "p1_ce_neutral", metrics

            elif is_pe:
                # Veto Check 1: spot > PutBuy + 0.5 ATR (Put buyers trapped, will dump on dip)
                if pb > 0 and curr_spot > (pb + veto_buffer):
                    if curr_spot > (pb + 1.0 * curr_atr):
                        return False, -999.0, "hard_block_put_buyer_pain_severe", metrics
                    return True, -10.0, "veto_put_buyer_dump_risk", metrics
                # Veto Check 2: spot > CallSell + 0.5 ATR (Call writers broken, upside short squeeze risk)
                if cs > 0 and curr_spot > (cs + veto_buffer):
                    return True, -10.0, "veto_call_seller_broken_risk", metrics

                # Boost conditions:
                fuel_count = 0
                if cb > 0 and curr_spot < cb:  # Call buyers trapped
                    fuel_count += 1
                if ps > 0 and curr_spot < ps:  # Put writers underwater
                    fuel_count += 1

                if fuel_count >= 2:
                    return True, 15.0, "p1_pe_dual_fuel_boost", metrics
                elif fuel_count == 1:
                    return True, 10.0, "p1_pe_single_fuel_boost", metrics
                return True, 0.0, "p1_pe_neutral", metrics


        # ─── P2: 2-Leg Pullback / 50 EMA Sweep ───
        elif pillar == 2:
            if is_ce:
                if cb > 0 and curr_spot < (cb - 1.0 * curr_atr):
                    return False, -999.0, "hard_block_p2_ce_distribution", metrics
                if cb > 0 and curr_spot < (cb - 0.3 * curr_atr):
                    return True, -10.0, "p2_ce_below_call_buy_soft_veto", metrics
                score = 10.0 if (pb > 0 and curr_spot > pb) else 0.0
                return True, score, "p2_ce_confluence", metrics
            elif is_pe:
                if pb > 0 and curr_spot > (pb + 1.0 * curr_atr):
                    return False, -999.0, "hard_block_p2_pe_distribution", metrics
                if pb > 0 and curr_spot > (pb + 0.3 * curr_atr):
                    return True, -10.0, "p2_pe_above_put_buy_soft_veto", metrics
                score = 10.0 if (cb > 0 and curr_spot < cb) else 0.0
                return True, score, "p2_pe_confluence", metrics

        # ─── P3: Squeeze -> Expansion Breakout ───
        elif pillar == 3:
            if is_ce:
                if cb > 0 and curr_spot < cb and gamma == "POSITIVE":
                    return False, -999.0, "fade_p3_ce_under_call_buy_pos_gamma", metrics
                score = 15.0 if (cb > 0 and curr_spot > cb) else 0.0
                return True, score, "p3_ce_expansion", metrics
            elif is_pe:
                if pb > 0 and curr_spot > pb and gamma == "POSITIVE":
                    return False, -999.0, "fade_p3_pe_above_put_buy_pos_gamma", metrics
                score = 15.0 if (pb > 0 and curr_spot < pb) else 0.0
                return True, score, "p3_pe_expansion", metrics

        # ─── P4: BOS (PDH/PDL/Pivot Break) ───
        elif pillar == 4:
            if is_ce:
                if cs > 0 and curr_spot > (cs + 1.0 * curr_atr):
                    return True, 5.0, "p4_ce_late_extension", metrics
                if cs > 0 and curr_spot > cs:
                    return True, 15.0, "p4_ce_max_fuel_writer_capitulation", metrics
                return True, 10.0, "p4_ce_half_size", metrics
            elif is_pe:
                if ps > 0 and curr_spot < (ps - 1.0 * curr_atr):
                    return True, 5.0, "p4_pe_late_extension", metrics
                if ps > 0 and curr_spot < ps:
                    return True, 15.0, "p4_pe_max_fuel_writer_capitulation", metrics
                return True, 10.0, "p4_pe_half_size", metrics

        return True, 0.0, "neutral", metrics

    def update_option_tick(
        self,
        sym: str,
        strike: float,
        side: int,             # 0 = CE, 1 = PE
        lp: float,
        bp1: float,
        sp1: float,
        vol: float,
        atm_strike: Optional[float] = None,
    ) -> None:
        """
        Accumulates option flow (Buyer vs Seller initiated) and updates
        4-Line Trapped-Buyer Pain Zone VWAPs + composite Option VWAP.
        Constrained strictly to ATM ± 7 strikes.
        Uses Hybrid Touch-First with Lee-Ready Tick Rule fallback.
        """
        if vol <= 0 or lp <= 0:
            return

        # 1. Day-cumulative volume differencing + session rollover guard
        prev_vol = self._opt_last_cum_vol.get(sym)
        if prev_vol is None:
            self._opt_last_cum_vol[sym] = vol
            self._opt_last_lp[sym] = lp
            return  # Baseline seed tick

        delta_vol = vol - prev_vol
        self._opt_last_cum_vol[sym] = vol
        if delta_vol < 0:
            delta_vol = vol        # New session: day-cum reset to 0
        if delta_vol <= 0:
            return

        # 2. Window Filter: ATM ± 7 strikes
        ref_atm = atm_strike or (round(self.last_price / self.strike_step) * self.strike_step)
        if ref_atm > 0 and abs(strike - ref_atm) > (7.0 * self.strike_step):
            return

        # 3. Hybrid Classification: Touch-first, Tick-Rule fallback
        buy_frac = self._classify_buy_fraction(sym, lp, bp1, sp1)
        if buy_frac is None:
            self._opt_flow_1m["unclass_vol"] += delta_vol
            return

        buy_vol = delta_vol * buy_frac
        sell_vol = delta_vol - buy_vol

        if side == 0:  # CALL
            self._opt_flow_1m["call_buy_vol"] += buy_vol
            self._opt_flow_1m["call_sell_vol"] += sell_vol
            if self.last_price > 0:
                self._cum_cb_pv += self.last_price * buy_vol
                self._cum_cb_vol += buy_vol
                self._cum_cs_pv += self.last_price * sell_vol
                self._cum_cs_vol += sell_vol
        elif side == 1:  # PUT
            self._opt_flow_1m["put_buy_vol"] += buy_vol
            self._opt_flow_1m["put_sell_vol"] += sell_vol
            if self.last_price > 0:
                self._cum_pb_pv += self.last_price * buy_vol
                self._cum_pb_vol += buy_vol
                self._cum_ps_pv += self.last_price * sell_vol
                self._cum_ps_vol += sell_vol

        # 4. Composite Option-Weighted Spot VWAP Update
        if self.last_price > 0:
            self._vwap_cum_vol_price += self.last_price * delta_vol
            self._vwap_cum_vol += delta_vol
            if self._vwap_cum_vol > 0:
                self.current_vwap = self._vwap_cum_vol_price / self._vwap_cum_vol

    def _on_1m_bar_close(self, bar: OHLCBar) -> None:
        """Processes a finalized 1-minute bar and updates EMAs and ATR."""
        # Attach in-flight 1-minute Option Flow directly to the candle
        cb = self._opt_flow_1m["call_buy_vol"]
        cs = self._opt_flow_1m["call_sell_vol"]
        pb = self._opt_flow_1m["put_buy_vol"]
        ps = self._opt_flow_1m["put_sell_vol"]
        bar.call_buy_vol = cb
        bar.call_sell_vol = cs
        bar.put_buy_vol = pb
        bar.put_sell_vol = ps
        bar.opt_delta_flow = cb - pb
        bar.opt_total_vol = cb + cs + pb + ps
        if bar.opt_total_vol > 0:
            bar.volume = bar.opt_total_vol
        bar.opt_pcr = round((pb + ps) / max(cb + cs, 1.0), 3)

        # Reset accumulator for the next 1-minute bucket
        self._opt_flow_1m = {
            "call_buy_vol": 0.0,
            "call_sell_vol": 0.0,
            "put_buy_vol": 0.0,
            "put_sell_vol": 0.0,
            "unclass_vol": 0.0,
        }

        if bar.opt_total_vol > 0:
            logger.info(
                f"[1M OPTION FLOW] Close: {bar.close:.2f} | "
                f"CallBuy={bar.call_buy_vol:.0f}, CallSell={bar.call_sell_vol:.0f}, "
                f"PutBuy={bar.put_buy_vol:.0f}, PutSell={bar.put_sell_vol:.0f}, "
                f"OFI={bar.opt_delta_flow:+.0f}, PCR={bar.opt_pcr:.2f}, TotalVol={bar.opt_total_vol:.0f}"
            )

        self.bars_1m.append(bar)

        # Update 5-minute bar
        bucket_5m = int(bar.ts_start // 300)
        if self._curr_5m_bar is None or int(self._curr_5m_bar.ts_start // 300) != bucket_5m:
            if self._curr_5m_bar is not None:
                self.bars_5m.append(self._curr_5m_bar)
            self._curr_5m_bar = OHLCBar(
                ts_start=float(bucket_5m * 300),
                ts_end=float((bucket_5m + 1) * 300),
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )
        else:
            b5 = self._curr_5m_bar
            if bar.high > b5.high:
                b5.high = bar.high
            if bar.low < b5.low:
                b5.low = bar.low
            b5.close = bar.close
            b5.volume += bar.volume


        # 1. Update 20 EMA (Close, High, Low Band)
        k20 = 2.0 / (20.0 + 1.0)
        if self.ema_20_1m <= 0:
            self.ema_20_1m = bar.close
            self.ema_20_1m_prev = bar.close
            self.ema_20_1m_slope = 0.0
            self.ema_20_high_1m = bar.high
            self.ema_20_low_1m = bar.low
        else:
            self.ema_20_1m_prev = self.ema_20_1m
            self.ema_20_1m = (bar.close * k20) + (self.ema_20_1m * (1.0 - k20))
            self.ema_20_1m_slope = self.ema_20_1m - self.ema_20_1m_prev
            self.ema_20_high_1m = (bar.high * k20) + (self.ema_20_high_1m * (1.0 - k20))
            self.ema_20_low_1m = (bar.low * k20) + (self.ema_20_low_1m * (1.0 - k20))
        # Index-aligned with bars_1m tail (appended above): enables two-touch confirmation
        self.ema_20_history.append(self.ema_20_1m)
        self.ema_20_high_history.append(self.ema_20_high_1m)
        self.ema_20_low_history.append(self.ema_20_low_1m)

        # 2. Update 50 EMA
        k50 = 2.0 / (50.0 + 1.0)
        if self.ema_50_1m <= 0:
            self.ema_50_1m = bar.close
            self.ema_50_1m_prev = bar.close
            self.ema_50_1m_slope = 0.0
        else:
            self.ema_50_1m_prev = self.ema_50_1m
            self.ema_50_1m = (bar.close * k50) + (self.ema_50_1m * (1.0 - k50))
            self.ema_50_1m_slope = self.ema_50_1m - self.ema_50_1m_prev

        # 2b. Feed 1-Minute ML Gatekeeper Flow Recorder
        if getattr(self, "ml_gatekeeper", None) is not None:
            try:
                open_px = max(bar.open, 1.0)
                ret_1m = (bar.close - bar.open) / open_px * 10000.0
                range_bps = (bar.high - bar.low) / open_px * 10000.0
                body_bps = (bar.close - bar.open) / open_px * 10000.0
                upper_wick_bps = (bar.high - max(bar.open, bar.close)) / open_px * 10000.0
                lower_wick_bps = (min(bar.open, bar.close) - bar.low) / open_px * 10000.0
                ema20_dist = (bar.close - self.ema_20_1m) / self.ema_20_1m * 10000.0 if self.ema_20_1m > 0 else 0.0
                ema5_dist = (bar.close - self.ema_50_1m) / self.ema_50_1m * 10000.0 if self.ema_50_1m > 0 else 0.0

                recent_3 = list(self.bars_1m)[-3:] if len(self.bars_1m) >= 3 else list(self.bars_1m)
                opt_delta_3m = sum(getattr(b, "opt_delta_flow", 0.0) for b in recent_3)

                cb = bar.call_buy_vol
                cs = bar.call_sell_vol
                pb = bar.put_buy_vol
                ps = bar.put_sell_vol

                call_agg = (cb - cs) / max(cb + cs, 1.0)
                put_agg = (pb - ps) / max(pb + ps, 1.0)
                opt_ratio = (cb + 1.0) / (pb + 1.0)

                ml_feats = {
                    "put_aggression": put_agg,
                    "call_buy_vol": cb,
                    "call_sell_vol": cs,
                    "call_aggression": call_agg,
                    "avg_book_imb": 0.0,
                    "upper_wick_bps": upper_wick_bps,
                    "ema20_dist_bps": ema20_dist,
                    "lower_wick_bps": lower_wick_bps,
                    "opt_flow_ratio": opt_ratio,
                    "range_bps": range_bps,
                    "body_bps": body_bps,
                    "put_buy_vol": pb,
                    "put_sell_vol": ps,
                    "opt_delta_3m": opt_delta_3m,
                    "ema5_dist_bps": ema5_dist,
                    "opt_total_vol": bar.opt_total_vol,
                    "opt_pcr": bar.opt_pcr,
                    "opt_delta_flow": bar.opt_delta_flow,
                    "ret_1m_bps": ret_1m,
                }
                idx_name = "SENSEX" if getattr(self, "strike_step", 50) == 100 else "NIFTY"
                self.ml_gatekeeper.record_1m_bar(bar.ts, ml_feats, index_name=idx_name)
            except Exception as e:
                logger.debug(f"[PriceActionEngine] ml_gatekeeper record_1m_bar failed: {e}")

        # 3. Update ATR(14)
        if len(self.bars_1m) >= 2:
            prev_close = self.bars_1m[-2].close
            tr = max(
                bar.high - bar.low,
                abs(bar.high - prev_close),
                abs(bar.low - prev_close),
            )
        else:
            tr = bar.high - bar.low

        self.atr_history.append(tr)
        if len(self.atr_history) >= self.atr_period:
            recent_tr = list(self.atr_history)[-self.atr_period:]
            self.current_atr_1m = sum(recent_tr) / len(recent_tr)
            baseline_tr = list(self.atr_history)[-30:]
            self.baseline_atr_1m = sum(baseline_tr) / len(baseline_tr) if baseline_tr else 0.0
            if self.baseline_atr_1m > 1e-4:
                self.atr_ratio = self.current_atr_1m / self.baseline_atr_1m
            else:
                self.atr_ratio = 1.0
        elif self.atr_history:
            self.current_atr_1m = sum(self.atr_history) / len(self.atr_history)
            self.baseline_atr_1m = self.current_atr_1m
            self.atr_ratio = 1.0

        # 4. Feed Topological HVG Shadow Engine (Pillar 4 Shadow)
        if getattr(self, "hvg_engine", None) is not None:
            try:
                self.hvg_engine.add_bar(
                    open_price=bar.open,
                    high_price=bar.high,
                    low_price=bar.low,
                    close_price=bar.close,
                    atr=self.current_atr_1m,
                    vwap=self.current_vwap,
                    timestamp=bar.ts_start,
                )
            except Exception:
                pass

    # ═══════════════════════════════════════════════════════════
    # 2. ZERO-LAG IN-FLIGHT EMA & DYNAMIC INDICATOR GETTERS
    # ═══════════════════════════════════════════════════════════

    def get_live_20_ema_and_slope(self) -> Tuple[float, float]:
        """
        Computes dynamic 20-EMA and slope on the active in-flight tick price.
        Returns:
            (live_ema_20, live_slope_20_bps)
        """
        ref = self.last_price if self.last_price > 0 else 1.0
        k20 = 2.0 / (20.0 + 1.0)
        if self.ema_20_1m_prev > 0 and self.last_price > 0:
            live_ema_20 = (self.last_price * k20) + (self.ema_20_1m_prev * (1.0 - k20))
            live_slope_20 = live_ema_20 - self.ema_20_1m_prev
        else:
            live_ema_20 = self.ema_20_1m if self.ema_20_1m > 0 else self.last_price
            live_slope_20 = self.ema_20_1m_slope
        live_slope_20_bps = (live_slope_20 / ref) * 10000.0
        return live_ema_20, live_slope_20_bps

    def get_live_50_ema_and_slope(self) -> Tuple[float, float]:
        """
        Computes dynamic 50-EMA and slope on the active in-flight tick price.
        Returns:
            (live_ema_50, live_slope_50_bps)
        """
        ref = self.last_price if self.last_price > 0 else 1.0
        k50 = 2.0 / (50.0 + 1.0)
        if self.ema_50_1m_prev > 0 and self.last_price > 0:
            live_ema_50 = (self.last_price * k50) + (self.ema_50_1m_prev * (1.0 - k50))
            live_slope_50 = live_ema_50 - self.ema_50_1m_prev
        else:
            live_ema_50 = self.ema_50_1m if self.ema_50_1m > 0 else self.last_price
            live_slope_50 = self.ema_50_1m_slope
        live_slope_50_bps = (live_slope_50 / ref) * 10000.0
        return live_ema_50, live_slope_50_bps

    def get_live_ema_and_slope(self) -> Tuple[float, float, float, float]:
        """
        Returns (live_ema_20, live_slope_20_bps, live_ema_50, live_slope_50_bps)
        for backward compatibility with legacy callers expecting 4-tuple EMA & slope metrics.
        """
        live_ema_20, live_slope_20_bps = self.get_live_20_ema_and_slope()
        live_ema_50, live_slope_50_bps = self.get_live_50_ema_and_slope()
        return live_ema_20, live_slope_20_bps, live_ema_50, live_slope_50_bps

    def seed_historical_bars(self, candles: List[Dict[str, Any]]) -> int:
        """
        Pre-warms the engine with historical 1m bars from broker REST API
        to eliminate 9:15 AM cold-start warmup lag and fill data gaps on restart.
        """
        if not candles:
            return 0

        from datetime import datetime as _dt

        def _parse_ts(c_dict: Dict[str, Any]) -> float:
            # 1. ssboe (Shoonya returns ssboe as epoch seconds string)
            ssboe = c_dict.get("ssboe")
            if ssboe is not None:
                try:
                    return float(ssboe)
                except (ValueError, TypeError):
                    pass
            # 2. timestamp / ts
            raw_ts = c_dict.get("timestamp") or c_dict.get("ts")
            if isinstance(raw_ts, (int, float)):
                return float(raw_ts)
            if isinstance(raw_ts, str):
                try:
                    return float(raw_ts)
                except ValueError:
                    try:
                        return _dt.strptime(raw_ts, "%Y-%m-%d %H:%M:%S").timestamp()
                    except Exception:
                        pass
            # 3. time string (Shoonya: 'DD-MM-YYYY HH:MM:SS')
            t_str = c_dict.get("time")
            if isinstance(t_str, (int, float)):
                return float(t_str)
            if isinstance(t_str, str):
                try:
                    return float(t_str)
                except ValueError:
                    for fmt in ("%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y %H:%M:%S"):
                        try:
                            return _dt.strptime(t_str, fmt).timestamp()
                        except Exception:
                            pass
            return 0.0

        parsed_bars = []
        for c in candles:
            try:
                op = float(c.get("open") or c.get("into") or 0.0)
                hi = float(c.get("high") or c.get("inth") or 0.0)
                lo = float(c.get("low") or c.get("intl") or 0.0)
                cl = float(c.get("close") or c.get("intc") or 0.0)
                if op <= 0 or hi <= 0 or lo <= 0 or cl <= 0:
                    continue
                ts = _parse_ts(c)
                vol = float(c.get("volume") or c.get("intv") or 0.0)
                parsed_bars.append((ts, op, hi, lo, cl, vol))
            except Exception:
                continue

        if not parsed_bars:
            return 0

        # Ensure bars are chronological (oldest to newest)
        parsed_bars.sort(key=lambda x: x[0])

        count = 0
        for ts, op, hi, lo, cl, vol in parsed_bars:
            bar = OHLCBar(
                ts_start=ts,
                ts_end=ts + 60.0,
                open=op,
                high=hi,
                low=lo,
                close=cl,
                volume=vol,
                tick_count=10,
            )
            self._on_1m_bar_close(bar)
            count += 1

        if self.bars_1m:
            self.last_price = self.bars_1m[-1].close
        logger.info(f"[PriceActionEngine] Seeded {count} historical bars. Live 20 EMA: {self.ema_20_1m:.1f}, 50 EMA: {self.ema_50_1m:.1f}")
        return count

    def seed_daily_levels(self, pdh: float, pdl: float, pdc: float = 0.0) -> None:
        """Seeds Previous Day High (PDH), Previous Day Low (PDL), and Previous Day Close (PDC)."""
        if pdh > 0:
            self.pdh = float(pdh)
        if pdl > 0:
            self.pdl = float(pdl)
        if pdc > 0:
            self.pdc = float(pdc)
        logger.info(
            f"[PriceActionEngine] Seeded Daily Levels: PDH={self.pdh:.2f} | PDL={self.pdl:.2f} | PDC={self.pdc:.2f} "
            f"(Proximity Threshold: {self.proximity_threshold_bps:.1f} bps)"
        )

    def check_key_level_proximity(self, direction: str, spot_price: float) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Key Level Proximity Veto:
        Prevents buying CALL directly into overhead PDH resistance or
        buying PUT directly into underlying PDL support.
        Threshold is scale-invariant (e.g. 8 bps: ~20 pts on NIFTY, ~60 pts on SENSEX).
        """
        if spot_price <= 0:
            return True, "spot_unavailable", {}

        threshold_pts = (self.proximity_threshold_bps / 10000.0) * spot_price

        if direction.upper() in ("UP", "CALL"):
            # Check if spot is beneath PDH resistance within proximity threshold
            if self.pdh > 0 and spot_price < self.pdh:
                dist = self.pdh - spot_price
                if dist <= threshold_pts:
                    return False, "near_overhead_pdh_resistance", {
                        "pdh": self.pdh,
                        "spot": spot_price,
                        "dist_pts": round(dist, 2),
                        "threshold_pts": round(threshold_pts, 2),
                    }

        elif direction.upper() in ("DOWN", "PUT"):
            # Check if spot is above PDL support within proximity threshold
            if self.pdl > 0 and spot_price > self.pdl:
                dist = spot_price - self.pdl
                if dist <= threshold_pts:
                    return False, "near_underlying_pdl_support", {
                        "pdl": self.pdl,
                        "spot": spot_price,
                        "dist_pts": round(dist, 2),
                        "threshold_pts": round(threshold_pts, 2),
                    }

        return True, "clear_of_key_levels", {}

    # ═══════════════════════════════════════════════════════════
    # 3. THE 3 EMPIRICALLY VERIFIED PRICE ACTION SCALPER PILLARS
    # ═══════════════════════════════════════════════════════════

    def get_20_ema_band_arrays(self, n: int) -> Tuple[List[float], List[float], List[float]]:
        """
        Returns aligned lists of (ema_close, ema_high, ema_low) for the last n closed bars.
        Falls back to derived band from ATR if high/low history is warming up.
        """
        bars = list(self.bars_1m)[-n:]
        e_close = list(self.ema_20_history)[-n:]
        if len(e_close) < len(bars):
            pad_len = len(bars) - len(e_close)
            e_close = [e_close[0] if e_close else self.ema_20_1m] * pad_len + e_close

        has_hl_band = (
            hasattr(self, "ema_20_high_history")
            and len(self.ema_20_high_history) >= len(bars)
            and len(self.ema_20_low_history) >= len(bars)
        )
        if has_hl_band:
            e_high = list(self.ema_20_high_history)[-n:]
            e_low = list(self.ema_20_low_history)[-n:]
        else:
            half_band = max(2.5, 0.35 * (self.current_atr_1m if self.current_atr_1m > 0 else 10.0))
            e_high = [ec + half_band for ec in e_close]
            e_low = [ec - half_band for ec in e_close]

        return e_close, e_high, e_low

    def _validate_dow_hl2_lh2_structure(
        self,
        direction: str,
        touch2_offset: int = 0,
        tolerance_bps: float = 1.5,
        lookback: int = 45,
    ) -> DowStructureChain:
        """
        Strict Classical Dow Theory Validator for Pillar 1:
          - CALL requires: HH1 -> HL1 (1st touch, skipped) -> HH2 (HH2 > HH1) -> 4..12 bar orderly pullback -> HL2 (HL2 > HL1).
          - PUT requires:  LL1 -> LH1 (1st touch, skipped) -> LL2 (LL2 < LL1) -> 4..12 bar orderly pullback -> LH2 (LH2 < LH1).
          - Zero-Tolerance Anti-Breakout Veto: Price must never close across the opposite side of the 20 EMA Band.
        """
        chain = DowStructureChain()
        n = min(lookback, len(self.bars_1m))
        if n < 12:
            chain.veto_reason = "insufficient_bars_for_dow_structure"
            return chain

        bars = list(self.bars_1m)[-n:]
        e_close, e_high, e_low = self.get_20_ema_band_arrays(n)
        atr = self.current_atr_1m if self.current_atr_1m > 0 else (bars[-1].close * 0.001)
        tol_pts = max(1.5, bars[-1].close * (tolerance_bps / 10000.0), 0.12 * atr)
        min_bos_pts = max(1.5, 0.15 * atr)
        min_hl_diff_pts = max(1.0, 0.10 * atr)

        idx_t2 = len(bars) - 1 - touch2_offset
        if idx_t2 < 10:
            chain.veto_reason = "insufficient_history_before_touch2"
            return chain

        is_call = direction.upper() in ("UP", "CALL")

        # ═══════════════════════════════════════════════════════════
        # ═══════════════════════════════════════════════════════════
        # CASE 1: CALL — UPTREND (HH1 -> HL1 -> HH2 -> HL2)
        # ═══════════════════════════════════════════════════════════
        if is_call:
            strict_floor_tol = 0.25 * tol_pts
            # 1. Zero-Tolerance Anti-Breakout Veto (last 10 bars)
            recent_start = max(0, idx_t2 - 10)
            for j in range(recent_start, idx_t2 + 1):
                if bars[j].close < (e_low[j] - strict_floor_tol):
                    chain.veto_reason = "breakout_from_below_veto"
                    return chain

            hl2_price = min(bars[idx_t2].low, bars[-1].low)

            # Continuous uptrend start from idx_t2
            trend_start = idx_t2
            for j in range(idx_t2, -1, -1):
                if bars[j].close < (e_low[j] - strict_floor_tol):
                    break
                trend_start = j

            trend_age = idx_t2 - trend_start
            if trend_age < 10:
                chain.veto_reason = "noise_veto_pullback_too_short"
                return chain

            # 2. Find Global HH2 within 4 to 12 bars prior to idx_t2
            # 2. Find Global HH2 within 5 to 10 bars prior to idx_t2
            search_hh2_start = max(trend_start + 3, idx_t2 - 12)
            if idx_t2 - 5 < search_hh2_start:
                chain.veto_reason = "noise_veto_pullback_too_short"
                return chain

            idx_hh2 = max(range(search_hh2_start, idx_t2), key=lambda j: bars[j].high)
            hh2_price = bars[idx_hh2].high

            if hh2_price <= (e_high[idx_hh2] + 0.20 * atr) or max(bars[idx_hh2].open, bars[idx_hh2].close) < e_high[idx_hh2]:
                chain.veto_reason = "no_established_hh2_above_band"
                return chain

            pullback_bars = idx_t2 - idx_hh2
            if not (5 <= pullback_bars <= 10):
                chain.veto_reason = f"noise_veto_pullback_out_of_bounds ({pullback_bars})"
                return chain

            # 3. Orderly Descending Retracement Check (>= 60%)
            descent_steps = 0
            for j in range(idx_hh2 + 1, idx_t2 + 1):
                if bars[j].close < (e_low[j] - strict_floor_tol):
                    chain.veto_reason = "breakout_from_below_veto"
                    return chain
                if (bars[j].close < bars[j].open) or (bars[j].high <= bars[j - 1].high) or (bars[j].close <= bars[j - 1].close):
                    descent_steps += 1

            orderly_ratio = descent_steps / float(pullback_bars) if pullback_bars > 0 else 0.0
            if orderly_ratio < 0.60:
                chain.veto_reason = f"erratic_pullback_not_orderly ({orderly_ratio:.2f})"
                return chain

            if (hh2_price - hl2_price) < (0.35 * atr):
                chain.veto_reason = "shallow_pullback_insufficient_retracement"
                return chain

            # 4. Find Continuous Uptrend Pair (HH1, HL1) Prior to HH2
            found_pair = False
            idx_hh1, hh1_price, idx_hl1, hl1_price = -1, -1.0, -1, 1e18

            for m in range(trend_start + 1, idx_hh2 - 3):
                if bars[m].high <= e_high[m]:
                    continue
                cand_hh1 = bars[m].high

                trough_idx = min(range(m + 1, idx_hh2), key=lambda x: bars[x].low)
                cand_hl1 = bars[trough_idx].low
                trough_tested = cand_hl1 <= (e_high[trough_idx] + tol_pts)

                if (
                    trough_tested
                    and (cand_hh1 - cand_hl1) >= (0.25 * atr)
                    and (hh2_price >= cand_hh1 + min_bos_pts)
                    and (hl2_price >= cand_hl1 + min_hl_diff_pts)
                ):
                    idx_hh1, hh1_price = m, cand_hh1
                    idx_hl1, hl1_price = trough_idx, cand_hl1
                    found_pair = True
                    break

            if not found_pair:
                chain.veto_reason = "first_touch_hl1_or_hh1_structure_not_met"
                return chain

            # 5. Swing vs Retracement Volume & Flow Validation
            imp_vol_slice = [getattr(b, "volume", 0.0) or 0.0 for b in bars[idx_hl1 + 1 : idx_hh2 + 1]]
            imp_avg_vol = (sum(imp_vol_slice) / len(imp_vol_slice)) if imp_vol_slice else 0.0
            pb_vol_slice = [getattr(b, "volume", 0.0) or 0.0 for b in bars[idx_hh2 + 1 : idx_t2 + 1]]
            pb_avg_vol = (sum(pb_vol_slice) / len(pb_vol_slice)) if pb_vol_slice else 0.0
            entry_vol = getattr(bars[-1], "volume", 0.0) or 0.0

            imp_bull_slice = [
                float(getattr(b, "call_buy_vol", 0.0) or 0.0) + float(getattr(b, "put_sell_vol", 0.0) or 0.0)
                for b in bars[idx_hl1 + 1 : idx_hh2 + 1]
            ]
            imp_bull_flow = (sum(imp_bull_slice) / len(imp_bull_slice)) if imp_bull_slice else 0.0

            pb_bear_slice = [
                float(getattr(b, "put_buy_vol", 0.0) or 0.0) + float(getattr(b, "call_sell_vol", 0.0) or 0.0)
                for b in bars[idx_hh2 + 1 : idx_t2 + 1]
            ]
            pb_bear_flow = (sum(pb_bear_slice) / len(pb_bear_slice)) if pb_bear_slice else 0.0

            imp_directional_flow = imp_bull_flow
            pb_counter_flow = pb_bear_flow
            swing_vdr = round(pb_avg_vol / imp_avg_vol, 2) if imp_avg_vol > 0 else 1.0

            if imp_avg_vol > 10.0:
                if pb_avg_vol > 1.15 * imp_avg_vol:
                    chain.veto_reason = f"volume_dump_pullback_heavy (pb={pb_avg_vol:.0f} > 1.15*imp={imp_avg_vol:.0f})"
                    return chain
                if entry_vol < 0.70 * pb_avg_vol:
                    chain.veto_reason = f"entry_volume_too_thin (entry={entry_vol:.0f} < 0.70*pb={pb_avg_vol:.0f})"
                    return chain

            chain.is_valid = True
            chain.veto_reason = "dow_hl2_confirmed"
            chain.idx_1, chain.idx_touch1, chain.idx_2, chain.idx_touch2 = idx_hh1, idx_hl1, idx_hh2, idx_t2
            chain.impulse_1, chain.touch_1, chain.impulse_2, chain.touch_2 = hh1_price, hl1_price, hh2_price, hl2_price
            chain.pullback_bars = pullback_bars
            chain.orderly_ratio = round(orderly_ratio, 2)
            chain.imp_avg_vol = round(imp_avg_vol, 1)
            chain.pb_avg_vol = round(pb_avg_vol, 1)
            chain.entry_vol = round(entry_vol, 1)
            chain.swing_vdr = swing_vdr
            chain.imp_directional_flow = round(imp_directional_flow, 1)
            chain.pb_counter_flow = round(pb_counter_flow, 1)
            return chain

        # ═══════════════════════════════════════════════════════════
        # CASE 2: PUT — DOWNTREND (LL1 -> LH1 -> LL2 -> LH2)
        # ═══════════════════════════════════════════════════════════
        else:
            strict_roof_tol = 0.25 * tol_pts
            # 1. Zero-Tolerance Anti-Breakout Veto (last 10 bars)
            recent_start = max(0, idx_t2 - 10)
            for j in range(recent_start, idx_t2 + 1):
                if bars[j].close > (e_high[j] + strict_roof_tol):
                    chain.veto_reason = "breakdown_from_above_veto"
                    return chain

            lh2_price = max(bars[idx_t2].high, bars[-1].high)

            # Continuous downtrend start from idx_t2
            trend_start = idx_t2
            for j in range(idx_t2, -1, -1):
                if bars[j].close > (e_high[j] + strict_roof_tol):
                    break
                trend_start = j

            trend_age = idx_t2 - trend_start
            if trend_age < 10:
                chain.veto_reason = "noise_veto_pullback_too_short"
                return chain

            # 2. Find Global LL2 within 5 to 10 bars prior to idx_t2
            search_ll2_start = max(trend_start + 3, idx_t2 - 12)
            if idx_t2 - 5 < search_ll2_start:
                chain.veto_reason = "noise_veto_pullback_too_short"
                return chain

            idx_ll2 = min(range(search_ll2_start, idx_t2), key=lambda j: bars[j].low)
            ll2_price = bars[idx_ll2].low

            if ll2_price >= (e_low[idx_ll2] - 0.20 * atr) or min(bars[idx_ll2].open, bars[idx_ll2].close) > e_low[idx_ll2]:
                chain.veto_reason = "no_established_ll2_below_band"
                return chain

            pullback_bars = idx_t2 - idx_ll2
            if not (5 <= pullback_bars <= 10):
                chain.veto_reason = f"noise_veto_pullback_out_of_bounds ({pullback_bars})"
                return chain

            # 3. Orderly Ascending Retracement Check (>= 60%)
            ascent_steps = 0
            for j in range(idx_ll2 + 1, idx_t2 + 1):
                if bars[j].close > (e_high[j] + strict_roof_tol):
                    chain.veto_reason = "breakdown_from_above_veto"
                    return chain
                if (bars[j].close > bars[j].open) or (bars[j].low >= bars[j - 1].low) or (bars[j].close >= bars[j - 1].close):
                    ascent_steps += 1

            orderly_ratio = ascent_steps / float(pullback_bars) if pullback_bars > 0 else 0.0
            if orderly_ratio < 0.60:
                chain.veto_reason = f"erratic_pullback_not_orderly ({orderly_ratio:.2f})"
                return chain

            if (lh2_price - ll2_price) < (0.35 * atr):
                chain.veto_reason = "shallow_pullback_insufficient_retracement"
                return chain

            # 4. Find Continuous Downtrend Pair (LL1, LH1) Prior to LL2
            found_pair = False
            idx_ll1, ll1_price, idx_lh1, lh1_price = -1, 1e18, -1, -1.0

            for m in range(trend_start + 1, idx_ll2 - 3):
                if bars[m].low >= e_low[m]:
                    continue
                cand_ll1 = bars[m].low

                peak_idx = max(range(m + 1, idx_ll2), key=lambda x: bars[x].high)
                cand_lh1 = bars[peak_idx].high
                peak_tested = cand_lh1 >= (e_low[peak_idx] - tol_pts)

                if (
                    peak_tested
                    and (cand_lh1 - cand_ll1) >= (0.25 * atr)
                    and (ll2_price <= cand_ll1 - min_bos_pts)
                    and (lh2_price <= cand_lh1 - min_hl_diff_pts)
                ):
                    idx_ll1, ll1_price = m, cand_ll1
                    idx_lh1, lh1_price = peak_idx, cand_lh1
                    found_pair = True
                    break

            if not found_pair:
                chain.veto_reason = "first_touch_lh1_or_ll1_structure_not_met"
                return chain

            # 5. Swing vs Retracement Volume & Flow Validation
            imp_vol_slice = [getattr(b, "volume", 0.0) or 0.0 for b in bars[idx_lh1 + 1 : idx_ll2 + 1]]
            imp_avg_vol = (sum(imp_vol_slice) / len(imp_vol_slice)) if imp_vol_slice else 0.0
            pb_vol_slice = [getattr(b, "volume", 0.0) or 0.0 for b in bars[idx_ll2 + 1 : idx_t2 + 1]]
            pb_avg_vol = (sum(pb_vol_slice) / len(pb_vol_slice)) if pb_vol_slice else 0.0
            entry_vol = getattr(bars[-1], "volume", 0.0) or 0.0

            imp_bear_slice = [
                float(getattr(b, "put_buy_vol", 0.0) or 0.0) + float(getattr(b, "call_sell_vol", 0.0) or 0.0)
                for b in bars[idx_lh1 + 1 : idx_ll2 + 1]
            ]
            imp_bear_flow = (sum(imp_bear_slice) / len(imp_bear_slice)) if imp_bear_slice else 0.0

            pb_bull_slice = [
                float(getattr(b, "call_buy_vol", 0.0) or 0.0) + float(getattr(b, "put_sell_vol", 0.0) or 0.0)
                for b in bars[idx_ll2 + 1 : idx_t2 + 1]
            ]
            pb_bull_flow = (sum(pb_bull_slice) / len(pb_bull_slice)) if pb_bull_slice else 0.0

            imp_directional_flow = imp_bear_flow
            pb_counter_flow = pb_bull_flow
            swing_vdr = round(pb_avg_vol / imp_avg_vol, 2) if imp_avg_vol > 0 else 1.0

            if imp_avg_vol > 10.0:
                if pb_avg_vol > 1.15 * imp_avg_vol:
                    chain.veto_reason = f"volume_dump_pullback_heavy (pb={pb_avg_vol:.0f} > 1.15*imp={imp_avg_vol:.0f})"
                    return chain
                if entry_vol < 0.70 * pb_avg_vol:
                    chain.veto_reason = f"entry_volume_too_thin (entry={entry_vol:.0f} < 0.70*pb={pb_avg_vol:.0f})"
                    return chain

            chain.is_valid = True
            chain.veto_reason = "dow_lh2_confirmed"
            chain.idx_1, chain.idx_touch1, chain.idx_2, chain.idx_touch2 = idx_ll1, idx_lh1, idx_ll2, idx_t2
            chain.impulse_1, chain.touch_1, chain.impulse_2, chain.touch_2 = ll1_price, lh1_price, ll2_price, lh2_price
            chain.pullback_bars = pullback_bars
            chain.orderly_ratio = round(orderly_ratio, 2)
            chain.imp_avg_vol = round(imp_avg_vol, 1)
            chain.pb_avg_vol = round(pb_avg_vol, 1)
            chain.entry_vol = round(entry_vol, 1)
            chain.swing_vdr = swing_vdr
            chain.imp_directional_flow = round(imp_directional_flow, 1)
            chain.pb_counter_flow = round(pb_counter_flow, 1)
            return chain

    def _had_prior_ema_touch(
        self,
        direction: str,
        tolerance_bps: float = 1.5,
        lookback: int = 40,
        current_vwap: float = 0.0,
    ) -> bool:
        """Backward compatibility helper: checks if prior touch / Dow structure is valid."""
        dow = self._validate_dow_hl2_lh2_structure(direction, touch2_offset=0, tolerance_bps=tolerance_bps, lookback=lookback)
        if not dow.is_valid:
            dow = self._validate_dow_hl2_lh2_structure(direction, touch2_offset=1, tolerance_bps=tolerance_bps, lookback=lookback)
        return dow.is_valid
    def _check_day_extreme_runway(
        self,
        direction: str,
        current_price: float,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Scale-Invariant Macro Runway & Day Extreme Clearance Rule for Breakout Pillars (P3 & P4).
        Enforces:
        1. True Structural Expansion: If at or breaking Day Extreme, must decisively clear it:
           - PUT: Close <= Day_Low - min_clearance
           - CALL: Close >= Day_High + min_clearance
        2. Internal Breakout Runway: If breaking an internal box/hub before Day Extreme, must have open runway:
           - min_runway_pts = max(1.25 * ATR_{1m}, 0.0008 * Spot)
           - PUT: Close - Day_Low >= min_runway_pts
           - CALL: Day_High - Close >= min_runway_pts
        """
        if self.current_atr_1m <= 0 or current_price <= 0:
            return True, "clear", {}

        if len(self.bars_1m) < 4:
            return True, "clear_insufficient_bars", {}

        curr_bar = self.bars_1m[-1]
        curr_day = int(curr_bar.ts_start // 86400) if curr_bar else self._current_vwap_day
        intraday_prior_bars = [
            b for b in list(self.bars_1m)[:-1]
            if (curr_day <= 0 or int(b.ts_start // 86400) == curr_day)
        ]
        if not intraday_prior_bars and len(self.bars_1m) >= 2:
            intraday_prior_bars = list(self.bars_1m)[:-1]

        if not intraday_prior_bars or len(intraday_prior_bars) < 3:
            return True, "clear_insufficient_prior_bars", {}

        day_high = max(b.high for b in intraday_prior_bars)
        day_low = min(b.low for b in intraday_prior_bars)
        min_runway_pts = max(1.25 * self.current_atr_1m, 0.0008 * current_price)
        min_clearance = max(2.0, (current_price / 24000.0) * 2.0, 0.08 * self.current_atr_1m)

        if direction.upper() in ("DOWN", "PUT"):
            if current_price > day_low:
                dist_to_day_low = current_price - day_low
                if dist_to_day_low < min_runway_pts:
                    return False, "insufficient_runway_to_day_low", {
                        "day_low": round(day_low, 2),
                        "room_pts": round(dist_to_day_low, 2),
                        "threshold_pts": round(min_runway_pts, 2),
                        "atr_1m": round(self.current_atr_1m, 2),
                    }
            elif current_price > (day_low - min_clearance):
                return False, "insufficient_day_low_clearance", {
                    "day_low": round(day_low, 2),
                    "clearance_pts": round(day_low - current_price, 2),
                    "req_clearance_pts": round(min_clearance, 2),
                    "atr_1m": round(self.current_atr_1m, 2),
                }

        elif direction.upper() in ("UP", "CALL"):
            if current_price < day_high:
                dist_to_day_high = day_high - current_price
                if dist_to_day_high < min_runway_pts:
                    return False, "insufficient_runway_to_day_high", {
                        "day_high": round(day_high, 2),
                        "room_pts": round(dist_to_day_high, 2),
                        "threshold_pts": round(min_runway_pts, 2),
                        "atr_1m": round(self.current_atr_1m, 2),
                    }
            elif current_price < (day_high + min_clearance):
                return False, "insufficient_day_high_clearance", {
                    "day_high": round(day_high, 2),
                    "clearance_pts": round(current_price - day_high, 2),
                    "req_clearance_pts": round(min_clearance, 2),
                    "atr_1m": round(self.current_atr_1m, 2),
                }

        return True, "clear_day_extreme_runway", {}

    def _check_structural_obstacle(
        self,
        direction: str,
        current_price: float,
        lookback: int = 120,
        ignore_pivots_below: Optional[float] = None,
        ignore_pivots_above: Optional[float] = None,
        check_day_extremes: bool = False,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Structural Obstacle Shield (HVG Topological Anchors + Multi-Touch Horizontal Clusters).
        Prevents buying PUTs directly into an underlying support wall (e.g. 11:33 & 11:39 double-bottom)
        or buying CALLs directly into an overhead resistance wall.
        Threshold is normalized via localized volatility (0.85x ATR).
        """
        if self.current_atr_1m <= 0 or current_price <= 0:
            return True, "clear", {}

        hurdle_pts = 0.85 * self.current_atr_1m

        # 1. 5m Macro HVG Structural Check (if hvg_engine available)
        if getattr(self, "hvg_engine", None) is not None:
            try:
                ok_5m, reason_5m, det_5m = self.hvg_engine.check_5m_structural_alignment(direction, current_price)
                if not ok_5m:
                    return False, f"hvg_shield_veto ({reason_5m})", det_5m
            except Exception as e:
                logger.debug(f"[StructuralShield] 5m HVG check failed: {e}")

        # 2. 1m Structural Horizontal Pivots / Multi-Touch Clusters
        if len(self.bars_1m) >= 4:
            bars = list(self.bars_1m)[:-1][-lookback:]
            pivot_lows = []
            pivot_highs = []
            prom = 0.10 * self.current_atr_1m
            for i in range(1, len(bars) - 1):
                p_b, c_b, n_b = bars[i - 1], bars[i], bars[i + 1]
                if c_b.low <= (p_b.low - prom) and c_b.low < (n_b.low - prom):
                    pivot_lows.append(c_b.low)
                if c_b.high >= (p_b.high + prom) and c_b.high > (n_b.high + prom):
                    pivot_highs.append(c_b.high)

            if direction.upper() in ("DOWN", "PUT"):
                # Find any established support level below current price or at current price
                # If ignore_pivots_above is specified (e.g. LL1), broken prior lows >= that level are ignored
                supports_below = [
                    pl for pl in pivot_lows
                    if pl <= current_price and (ignore_pivots_above is None or pl < ignore_pivots_above)
                ]
                if supports_below:
                    nearest_support = max(supports_below)
                    dist_to_support = current_price - nearest_support
                    if dist_to_support < hurdle_pts:
                        return False, "trapped_near_support", {
                            "support_level": round(nearest_support, 2),
                            "room_pts": round(dist_to_support, 2),
                            "threshold_pts": round(hurdle_pts, 2),
                            "atr_1m": round(self.current_atr_1m, 2),
                        }

            elif direction.upper() in ("UP", "CALL"):
                # If ignore_pivots_below is specified (e.g. HH1), broken prior highs <= that level are ignored
                resistances_above = [
                    ph for ph in pivot_highs
                    if ph >= current_price and (ignore_pivots_below is None or ph > ignore_pivots_below)
                ]
                if resistances_above:
                    nearest_resistance = min(resistances_above)
                    dist_to_res = nearest_resistance - current_price
                    if dist_to_res < hurdle_pts:
                        return False, "trapped_near_resistance", {
                            "resistance_level": round(nearest_resistance, 2),
                            "room_pts": round(dist_to_res, 2),
                            "threshold_pts": round(hurdle_pts, 2),
                            "atr_1m": round(self.current_atr_1m, 2),
                        }

        # 3. Macro Runway & Day Extreme Clearance Rule (Breakout Setups)
        if check_day_extremes:
            ok_de, r_de, det_de = self._check_day_extreme_runway(direction, current_price)
            if not ok_de:
                return False, r_de, det_de

        return True, "clear_of_structural_obstacles", {}

    def _find_recent_pivots(self, lookback: int = 20) -> Tuple[Optional[float], Optional[float]]:
        """
        Scans up to `lookback` prior closed bars (excluding current bar) for structural pivot extremes:
        - Pivot Low: bar whose low is <= prior bar's low and < next bar's low.
        - Pivot High: bar whose high is >= prior bar's high and > next bar's high.
        Returns:
            (lowest_pivot_low, highest_pivot_high) or (None, None) if no pivots found.
        """
        if len(self.bars_1m) < 4:
            return None, None

        # Take prior closed bars excluding the active/last closed bar
        bars = list(self.bars_1m)[:-1][-lookback:]
        if len(bars) < 3:
            return None, None

        pivot_lows = []
        pivot_highs = []

        for i in range(1, len(bars) - 1):
            prev_b = bars[i - 1]
            curr_b = bars[i]
            next_b = bars[i + 1]

            if curr_b.low <= prev_b.low and curr_b.low < next_b.low:
                pivot_lows.append(curr_b.low)
            if curr_b.high >= prev_b.high and curr_b.high > next_b.high:
                pivot_highs.append(curr_b.high)

        # The most recent pivot is the immediate structural support/resistance obstacle
        nearest_pl = pivot_lows[-1] if pivot_lows else None
        nearest_ph = pivot_highs[-1] if pivot_highs else None
        return nearest_pl, nearest_ph

    def check_pivot_exhaustion(
        self,
        direction: str,
        lookback: int = 30,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Pillar 1 Trend Exhaustion Filter:
        Prevents buying CALL pullbacks into a sequence of descending Lower Highs (distribution / bear flag)
        and prevents buying PUT pullbacks into a sequence of ascending Higher Lows (accumulation / bull flag).

        Scale-invariant: Thresholds are normalized via ATR and index spot price.
        """
        if not os.getenv("AEGIS_PIVOT_EXHAUSTION_FILTER", "true").lower() in ("true", "1", "yes"):
            return False, "filter_disabled", {}

        if len(self.bars_1m) < 6:
            return False, "insufficient_bars", {}

        bar = self.bars_1m[-1]
        prior_bars = list(self.bars_1m)[:-1][-lookback:]
        if len(prior_bars) < 4:
            return False, "insufficient_bars", {}

        ref_spot = self.last_price if self.last_price > 0 else bar.close
        atr = self.current_atr_1m if self.current_atr_1m > 0 else (ref_spot * 0.001)
        buffer_pts = max(5.0, (ref_spot / 24000.0) * 5.0)
        exh_threshold = max(0.20 * atr, 0.50 * buffer_pts)
        min_swing_depth = max(0.15 * atr, 0.40 * buffer_pts)

        live_ema_20, live_slope_20_bps = self.get_live_20_ema_and_slope()

        if direction.upper() in ("UP", "CALL"):
            # 1. Identify distinct swing highs in lookback
            raw_highs: List[Tuple[int, float]] = []
            for i in range(1, len(prior_bars) - 1):
                p_b = prior_bars[i - 1]
                c_b = prior_bars[i]
                n_b = prior_bars[i + 1]
                if (c_b.high >= p_b.high and c_b.high > n_b.high) or (c_b.high > p_b.high and c_b.high >= n_b.high):
                    raw_highs.append((i, c_b.high))

            if len(raw_highs) < 2:
                return False, "insufficient_pivots", {}

            # Filter distinct swing highs that are separated by a pullback of at least min_swing_depth
            distinct_highs: List[Tuple[int, float]] = [raw_highs[0]]
            for idx, h in raw_highs[1:]:
                prev_idx, prev_h = distinct_highs[-1]
                # Check intervening low
                intervening_low = min(b.low for b in prior_bars[prev_idx:idx + 1])
                pullback = min(prev_h, h) - intervening_low
                if pullback >= min_swing_depth and (idx - prev_idx) >= 3:
                    distinct_highs.append((idx, h))
                elif h > prev_h:
                    distinct_highs[-1] = (idx, h)

            if len(distinct_highs) < 2:
                return False, "insufficient_pivots", {}

            # Check the most recent 2 or 3 distinct pivot highs
            recent_highs = [h for _, h in distinct_highs[-3:]]
            h_prev = recent_highs[-2]
            h_last = recent_highs[-1]

            # If current bar has already broken above the last swing high, structure is breaking out (not exhausted)
            if bar.close > h_last:
                return False, "broken_above_last_pivot_high", {}

            # Case A: 3 consecutive lower highs (H1 > H2 > H3)
            is_3_lower_highs = False
            if len(recent_highs) >= 3:
                h_first = recent_highs[-3]
                if (h_first - h_prev) >= (0.10 * atr) and (h_prev - h_last) >= (0.10 * atr):
                    is_3_lower_highs = True

            # Case B: Significant lower high (h_prev - h_last >= exh_threshold)
            drop = h_prev - h_last
            is_significant_drop = drop >= exh_threshold

            # Exhaustion confirms if slope is not strongly bullish (<= 0.20 bps) and making lower highs
            if (is_3_lower_highs or is_significant_drop) and live_slope_20_bps <= 0.20:
                reason = "consecutive_lower_highs" if is_3_lower_highs else "descending_pivot_high_drop"
                return True, reason, {
                    "direction": "CALL",
                    "h_prev": round(h_prev, 2),
                    "h_last": round(h_last, 2),
                    "drop_pts": round(drop, 2),
                    "threshold_pts": round(exh_threshold, 2),
                    "bar_close": round(bar.close, 2),
                    "ema_20_slope_bps": round(live_slope_20_bps, 2),
                    "all_recent_highs": [round(x, 2) for x in recent_highs],
                }

            return False, "higher_highs_or_strong_slope", {}

        elif direction.upper() in ("DOWN", "PUT"):
            # 1. Identify distinct swing lows in lookback
            raw_lows: List[Tuple[int, float]] = []
            for i in range(1, len(prior_bars) - 1):
                p_b = prior_bars[i - 1]
                c_b = prior_bars[i]
                n_b = prior_bars[i + 1]
                if (c_b.low <= p_b.low and c_b.low < n_b.low) or (c_b.low < p_b.low and c_b.low <= n_b.low):
                    raw_lows.append((i, c_b.low))

            if len(raw_lows) < 2:
                return False, "insufficient_pivots", {}

            # Filter distinct swing lows that are separated by a bounce of at least min_swing_depth
            distinct_lows: List[Tuple[int, float]] = [raw_lows[0]]
            for idx, l in raw_lows[1:]:
                prev_idx, prev_l = distinct_lows[-1]
                # Check intervening high
                intervening_high = max(b.high for b in prior_bars[prev_idx:idx + 1])
                bounce = intervening_high - max(prev_l, l)
                if bounce >= min_swing_depth and (idx - prev_idx) >= 3:
                    distinct_lows.append((idx, l))
                elif l < prev_l:
                    distinct_lows[-1] = (idx, l)

            if len(distinct_lows) < 2:
                return False, "insufficient_pivots", {}

            # Check the most recent 2 or 3 distinct pivot lows
            recent_lows = [l for _, l in distinct_lows[-3:]]
            l_prev = recent_lows[-2]
            l_last = recent_lows[-1]

            # If current bar has already broken below the last swing low, structure is breaking down (not exhausted)
            if bar.close < l_last:
                return False, "broken_below_last_pivot_low", {}

            # Case A: 3 consecutive higher lows (L1 < L2 < L3)
            is_3_higher_lows = False
            if len(recent_lows) >= 3:
                l_first = recent_lows[-3]
                if (l_prev - l_first) >= (0.10 * atr) and (l_last - l_prev) >= (0.10 * atr):
                    is_3_higher_lows = True

            # Case B: Significant higher low (l_last - l_prev >= exh_threshold)
            rise = l_last - l_prev
            is_significant_rise = rise >= exh_threshold

            # Exhaustion confirms if slope is not strongly bearish (>= -0.20 bps) and making higher lows
            if (is_3_higher_lows or is_significant_rise) and live_slope_20_bps >= -0.20:
                reason = "consecutive_higher_lows" if is_3_higher_lows else "ascending_pivot_low_rise"
                return True, reason, {
                    "direction": "PUT",
                    "l_prev": round(l_prev, 2),
                    "l_last": round(l_last, 2),
                    "rise_pts": round(rise, 2),
                    "threshold_pts": round(exh_threshold, 2),
                    "bar_close": round(bar.close, 2),
                    "ema_20_slope_bps": round(live_slope_20_bps, 2),
                    "all_recent_lows": [round(x, 2) for x in recent_lows],
                }

            return False, "lower_lows_or_strong_slope", {}

        return False, "unknown_direction", {}

    def check_20_ema_rejection(
        self,
        direction: str,
        tolerance_bps: float = 1.5,
        min_wick_ratio: float = 0.33,
        current_vwap: float = 0.0,
        current_time: float = 0.0,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Pillar 1 (Re-Engineered on Classical Dow Theory):
        Trades strictly the 2nd Pullback Touch (HL2 in Uptrend / LH2 in Downtrend)
        into the 20 EMA High-Low Band after 2 confirmed Higher Highs (or Lower Lows)
        and a 4-to-12 candle orderly retracement.
        """
        live_ema_20, live_slope_20_bps = self.get_live_20_ema_and_slope()
        if live_ema_20 <= 0 or self.last_price <= 0:
            return False, "no_ema20_data", {}

        if len(self.bars_1m) < 12:
            return False, "insufficient_bars", {}

        bar = self.bars_1m[-1]
        e_close_arr, e_high_arr, e_low_arr = self.get_20_ema_band_arrays(2)
        band_high = e_high_arr[-1]
        band_low = e_low_arr[-1]

        atr = self.current_atr_1m if self.current_atr_1m > 0 else (live_ema_20 * 0.001)
        tol_pts = max(1.5, live_ema_20 * (tolerance_bps / 10000.0), 0.12 * atr)
        buffer_pts = max(5.0, (live_ema_20 / 24000.0) * 5.0)
        max_retest_dist = max(12.0, 2.5 * buffer_pts)

        # 1. Bar Close Freshness Gate (12.0s max cutoff)
        if current_time > 0 and bar.ts_end > 0:
            both_epoch = (current_time > 1_000_000_000.0 and bar.ts_end > 1_000_000_000.0)
            both_synthetic = (current_time < 1_000_000_000.0 and bar.ts_end < 1_000_000_000.0)
            if both_epoch or both_synthetic:
                time_since_close = current_time - bar.ts_end
                if time_since_close > 12.0:
                    return False, "stale_bar_entry_window_expired", {
                        "time_since_close": round(time_since_close, 1),
                        "max_allowed_s": 12.0,
                    }

        # Upgrade 3: Live Price & Forming Bar Invalidation Guard (Evaluated Before Slope)
        if self.last_price > 0:
            if direction.upper() in ("UP", "CALL"):
                if self.last_price <= (band_low - 0.5 * tol_pts):
                    return False, "live_price_piercing_below_20_ema", {
                        "last_price": round(self.last_price, 2),
                        "ema_20": round(live_ema_20, 2),
                        "band_low": round(band_low, 2),
                        "tol_pts": round(tol_pts, 2),
                    }
            else:
                if self.last_price >= (band_high + 0.5 * tol_pts):
                    return False, "live_price_piercing_above_20_ema", {
                        "last_price": round(self.last_price, 2),
                        "ema_20": round(live_ema_20, 2),
                        "band_high": round(band_high, 2),
                        "tol_pts": round(tol_pts, 2),
                    }

        total_range = bar.high - bar.low
        if total_range < (0.30 * atr):
            return False, "tiny_range_noise", {}

        body = abs(bar.close - bar.open)
        lower_wick = min(bar.open, bar.close) - bar.low
        upper_wick = bar.high - max(bar.open, bar.close)
        lower_wick_ratio = lower_wick / total_range if total_range > 0 else 0.0
        upper_wick_ratio = upper_wick / total_range if total_range > 0 else 0.0

        # ═══════════════════════════════════════════════════════════
        # CALL EVALUATION (HL2 Green Rejection from 20 EMA Band)
        # ═══════════════════════════════════════════════════════════
        if direction.upper() in ("UP", "CALL"):
            if current_vwap > 0 and bar.close <= current_vwap:
                return False, "below_vwap", {}
            if live_slope_20_bps < -0.10:
                return False, "20_ema_slope_bearish", {}

            close_edge = (bar.close - bar.low) / total_range if total_range > 0 else 0.0
            is_hammer = False
            is_bullish_expansion = False
            if bar.close > bar.open:
                is_hammer = (
                    (lower_wick_ratio >= min_wick_ratio)
                    and (close_edge >= 0.60)
                    and (upper_wick_ratio <= 0.15)
                    and (lower_wick >= 1.5 * upper_wick)
                )
                is_bullish_expansion = (
                    (body >= 0.40 * total_range)
                    and (upper_wick_ratio <= 0.20)
                    and (close_edge >= 0.60)
                )

            is_green_rejection = is_hammer or is_bullish_expansion
            if not is_green_rejection:
                return False, "not_valid_green_rejection_candle", {}
            if bar.close < (band_low - 0.5 * tol_pts) or (bar.close - band_high) > max_retest_dist:
                return False, "retest_distance_out_of_bounds", {}

            # Inside-bar trap protection: unless deep hammer (lower_wick_ratio >= 0.45), must clear prior pullback bar's open/body and high
            prev_bar = self.bars_1m[-2] if len(self.bars_1m) >= 2 else None
            if prev_bar and lower_wick_ratio < 0.45:
                prev_body_top = max(prev_bar.open, prev_bar.close)
                if not (bar.close > prev_body_top and bar.high >= prev_bar.high):
                    return False, "inside_bar_fakeout_pullback_not_reclaimed", {}

            # Check Band Touch: Path A (Bar T touched 20 EMA Band anywhere between High and Low) or Path B (Bar T-1 touched, Bar T confirms)
            touched_T = (bar.low <= (band_high + tol_pts)) and (bar.close >= (band_low - 0.5 * tol_pts))
            prev_ema_high = e_high_arr[-2] if len(e_high_arr) >= 2 else band_high
            touched_T1 = False
            if prev_bar and len(e_high_arr) >= 2:
                touched_T1 = (
                    (prev_bar.low <= (prev_ema_high + tol_pts))
                    and (prev_bar.close >= (e_low_arr[-2] - 0.5 * tol_pts))
                    and (bar.close > prev_bar.high or body >= 0.45 * total_range)
                )

            if not (touched_T or touched_T1):
                return False, "did_not_test_20_ema", {}

            # Validate Full Dow Theory Sequence (HH1 -> HL1 -> HH2 -> 4..12 bar pullback -> HL2)
            dow = self._validate_dow_hl2_lh2_structure("CALL", touch2_offset=0)
            if not dow.is_valid and touched_T1:
                dow = self._validate_dow_hl2_lh2_structure("CALL", touch2_offset=1)

            if not dow.is_valid:
                v_reason = dow.veto_reason
                if v_reason in (
                    "first_touch_hl1_not_found_no_entry_on_hl1",
                    "no_established_hh2_above_band",
                    "no_prior_hh1_before_hl1",
                    "first_touch_hl1_or_hh1_structure_not_met",
                ) or v_reason.startswith("noise_veto_pullback") or v_reason.startswith("erratic_pullback"):
                    v_reason = "single_touch_no_confirmation"
                return False, v_reason, {"dow_veto": dow.veto_reason}

            # Structural Obstacle Shield (ignore broken impulse levels below impulse_1 / HH1)
            ignore_ph = dow.impulse_1 if (dow and dow.impulse_1 > 0) else None
            ok_struct, r_struct, det_struct = self._check_structural_obstacle(
                "CALL", bar.close, ignore_pivots_below=ignore_ph
            )
            if not ok_struct:
                return False, r_struct, det_struct

            if touched_T1 and not (bar.low <= (live_ema_20 + tol_pts)):
                sig_type = "two_bar_retest_bullish_expansion"
                conf_path = "two_bar"
            else:
                sig_type = "hammer_rejection" if is_hammer else "bullish_expansion"
                conf_path = "single_bar"

            sl_price = round(min(bar.low, dow.touch_2) - buffer_pts, 2)
            details = {
                "pa_setup_name": "20_ema_rejection",
                "pillar": 1,
                "rejection_type": sig_type,
                "confirmation_path": conf_path,
                "dow_structure": "dow_hl2_confirmed",
                "ema_20": round(live_ema_20, 2),
                "ema_20_band_high": round(band_high, 2),
                "ema_20_band_low": round(band_low, 2),
                "hh1": round(dow.impulse_1, 2),
                "hl1": round(dow.touch_1, 2),
                "hh2": round(dow.impulse_2, 2),
                "hl2": round(dow.touch_2, 2),
                "pullback_bars": dow.pullback_bars,
                "orderly_ratio": dow.orderly_ratio,
                "imp_avg_vol": dow.imp_avg_vol,
                "pb_avg_vol": dow.pb_avg_vol,
                "entry_vol": dow.entry_vol,
                "swing_vdr": dow.swing_vdr,
                "imp_directional_flow": dow.imp_directional_flow,
                "pb_counter_flow": dow.pb_counter_flow,
                "bar_open": round(bar.open, 2),
                "bar_high": round(bar.high, 2),
                "bar_low": round(bar.low, 2),
                "bar_close": round(bar.close, 2),
                "sl_price": sl_price,
                "buffer_pts": round(buffer_pts, 2),
            }
            return True, f"20_ema_call_hl2_rejection ({sig_type})", details

        # ═══════════════════════════════════════════════════════════
        # PUT EVALUATION (LH2 Red Rejection from 20 EMA Band)
        # ═══════════════════════════════════════════════════════════
        elif direction.upper() in ("DOWN", "PUT"):
            if current_vwap > 0 and bar.close >= current_vwap:
                return False, "above_vwap", {}
            if live_slope_20_bps > 0.10:
                return False, "20_ema_slope_bullish", {}

            close_edge = (bar.high - bar.close) / total_range if total_range > 0 else 0.0
            is_shooting_star = False
            is_bearish_expansion = False
            if bar.close < bar.open:
                is_shooting_star = (
                    (upper_wick_ratio >= min_wick_ratio)
                    and (close_edge >= 0.60)
                    and (lower_wick_ratio <= 0.15)
                    and (upper_wick >= 1.5 * lower_wick)
                )
                is_bearish_expansion = (
                    (body >= 0.40 * total_range)
                    and (lower_wick_ratio <= 0.20)
                    and (close_edge >= 0.60)
                )

            is_red_rejection = is_shooting_star or is_bearish_expansion
            if not is_red_rejection:
                return False, "not_valid_red_rejection_candle", {}
            if bar.close > (band_high + 0.5 * tol_pts) or (band_low - bar.close) > max_retest_dist:
                return False, "retest_distance_out_of_bounds", {}

            # Inside-bar trap protection: unless deep shooting star (upper_wick_ratio >= 0.45), must clear prior pullback bar's open/body and low
            prev_bar = self.bars_1m[-2] if len(self.bars_1m) >= 2 else None
            if prev_bar and upper_wick_ratio < 0.45:
                prev_body_bottom = min(prev_bar.open, prev_bar.close)
                if not (bar.close < prev_body_bottom and bar.low <= prev_bar.low):
                    return False, "inside_bar_fakeout_pullback_not_reclaimed", {}

            touched_T = (bar.high >= (band_low - tol_pts)) and (bar.close <= (band_high + 0.5 * tol_pts))
            prev_ema_low = e_low_arr[-2] if len(e_low_arr) >= 2 else band_low
            touched_T1 = False
            if prev_bar and len(e_high_arr) >= 2:
                touched_T1 = (
                    (prev_bar.high >= (prev_ema_low - tol_pts))
                    and (prev_bar.close <= (e_high_arr[-2] + 0.5 * tol_pts))
                    and (bar.close < prev_bar.low or body >= 0.45 * total_range)
                )

            if not (touched_T or touched_T1):
                return False, "did_not_test_20_ema", {}

            # Validate Full Dow Theory Sequence (LL1 -> LH1 -> LL2 -> 4..12 bar pullback -> LH2)
            dow = self._validate_dow_hl2_lh2_structure("PUT", touch2_offset=0)
            if not dow.is_valid and touched_T1:
                dow = self._validate_dow_hl2_lh2_structure("PUT", touch2_offset=1)

            if not dow.is_valid:
                v_reason = dow.veto_reason
                if v_reason in (
                    "first_touch_lh1_not_found_no_entry_on_lh1",
                    "no_established_ll2_below_band",
                    "no_prior_ll1_before_lh1",
                    "first_touch_lh1_or_ll1_structure_not_met",
                ) or v_reason.startswith("noise_veto_pullback") or v_reason.startswith("erratic_pullback"):
                    v_reason = "single_touch_no_confirmation"
                return False, v_reason, {"dow_veto": dow.veto_reason}

            # Structural Obstacle Shield (ignore broken impulse levels above impulse_1 / LL1)
            ignore_pl = dow.impulse_1 if (dow and dow.impulse_1 > 0) else None
            ok_struct, r_struct, det_struct = self._check_structural_obstacle(
                "PUT", bar.close, ignore_pivots_above=ignore_pl
            )
            if not ok_struct:
                return False, r_struct, det_struct

            if touched_T1 and not (bar.high >= (live_ema_20 - tol_pts)):
                sig_type = "two_bar_retest_bearish_expansion"
                conf_path = "two_bar"
            else:
                sig_type = "pin_bar_rejection" if is_shooting_star else "bearish_expansion"
                conf_path = "single_bar"

            sl_price = round(max(bar.high, dow.touch_2) + buffer_pts, 2)
            details = {
                "pa_setup_name": "20_ema_rejection",
                "pillar": 1,
                "rejection_type": sig_type,
                "confirmation_path": conf_path,
                "dow_structure": "dow_lh2_confirmed",
                "ema_20": round(live_ema_20, 2),
                "ema_20_band_high": round(band_high, 2),
                "ema_20_band_low": round(band_low, 2),
                "ll1": round(dow.impulse_1, 2),
                "lh1": round(dow.touch_1, 2),
                "ll2": round(dow.impulse_2, 2),
                "lh2": round(dow.touch_2, 2),
                "pullback_bars": dow.pullback_bars,
                "orderly_ratio": dow.orderly_ratio,
                "imp_avg_vol": dow.imp_avg_vol,
                "pb_avg_vol": dow.pb_avg_vol,
                "entry_vol": dow.entry_vol,
                "swing_vdr": dow.swing_vdr,
                "imp_directional_flow": dow.imp_directional_flow,
                "pb_counter_flow": dow.pb_counter_flow,
                "bar_open": round(bar.open, 2),
                "bar_high": round(bar.high, 2),
                "bar_low": round(bar.low, 2),
                "bar_close": round(bar.close, 2),
                "sl_price": sl_price,
                "buffer_pts": round(buffer_pts, 2),
            }
            return True, f"20_ema_put_lh2_rejection ({sig_type})", details

        return False, "unknown_direction", {}

    def check_albrooks_2legged_pullback(
        self,
        direction: str,
        current_vwap: float = 0.0,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Pillar 2: Al Brooks 2-Legged Pullback (H2 / L2 Exhaustion Sweep into 50 EMA).
        Flexible pause up to 40 bars with same-side discipline.
        Trend filter: 20 MA must lead 50 MA. Full-candle entries only.
        """
        if len(self.bars_1m) < 3:
            return False, "insufficient_bars", {}

        b1 = self.bars_1m[-1]  # Leg 2 sweep bar (latest closed)
        bar = self.bars_1m[-1]  # Full-candle only, no mid-bar hop.
        if bar is None:
            return False, "no_closed_bar", {}

        live_ema_50, live_slope_50_bps = self.get_live_50_ema_and_slope()
        if live_ema_50 <= 0:
            return False, "no_ema50", {}
        try:
            _e20t, _ = self.get_live_20_ema_and_slope()
        except Exception:
            _e20t = 0.0
        if _e20t > 0:
            if direction.upper() in ("UP", "CALL") and _e20t <= live_ema_50:
                return False, "20_below_50_no_uptrend", {}
            if direction.upper() in ("DOWN", "PUT") and _e20t >= live_ema_50:
                return False, "20_above_50_no_downtrend", {}

        buffer_pts = max(5.0, (live_ema_50 / 24000.0) * 5.0)
        tol_pts = buffer_pts * 1.5

        if direction.upper() in ("DOWN", "PUT"):
            if current_vwap > 0 and bar.close >= current_vwap:
                return False, "above_vwap", {}

            # Leg 2 must reach 50 EMA with bearish reversal.
            tested_50 = abs(b1.high - live_ema_50) <= tol_pts or (b1.high >= (live_ema_50 - tol_pts) and b1.close <= (live_ema_50 + tol_pts))
            if not (tested_50 and bar.close < bar.open):
                return False, "l2_structure_not_met", {}
            # Flexible Leg 1 search: any touch in last 15 bars with same-side pause.
            bars = list(self.bars_1m)
            leg_found = False
            leg1_high = b1.high
            for k in range(6, min(41, len(bars))):
                cand = bars[-(k + 1)]
                c_test = abs(cand.high - live_ema_50) <= tol_pts or (cand.high >= (live_ema_50 - tol_pts) and cand.close <= (live_ema_50 + tol_pts))
                if not c_test:
                    continue
                # Leg 1 must sit below VWAP (bearish falling from VWAP).
                if current_vwap > 0 and cand.high >= current_vwap:
                    continue
                if not (b1.high > cand.high):
                    continue
                # Same-side pause: all bars between Leg 1 and Leg 2 stay below EMA.
                ok = True
                for mid in bars[-k:-1]:
                    if mid.close > (live_ema_50 + 0.5 * tol_pts):
                        ok = False
                        break
                if ok:
                    leg_found = True
                    leg1_high = cand.high
                    break
            # Fallback: first lower-high anywhere (bearish falling from VWAP).
            if not leg_found:
                for k in range(6, min(41, len(bars))):
                    cand = bars[-(k + 1)]
                    if not (cand.high > b1.high and cand.close < cand.open):
                        continue
                    # Below VWAP discipline for entire chain.
                    if current_vwap > 0 and cand.high >= current_vwap:
                        continue
                    ok = True
                    for mid in bars[-k:-1]:
                        if mid.close > (live_ema_50 + 0.5 * tol_pts):
                            ok = False
                            break
                    if ok:
                        leg_found = True
                        leg1_high = cand.high
                        break
            if not leg_found:
                return False, "l2_no_valid_leg1_same_side", {}
            if True:
                sl_price = round(max(bar.high, b1.high) + buffer_pts, 2)
                details = {
                    "pa_setup_name": "albrooks_2legged_pullback",
                    "pillar": 2,
                    "structure": "L2_bearish_exhaustion",
                    "ema_50": round(live_ema_50, 2),
                    "bar_open": round(bar.open, 2),
                    "bar_high": round(bar.high, 2),
                    "bar_low": round(bar.low, 2),
                    "bar_close": round(bar.close, 2),
                    "leg1_high": round(leg1_high, 2),
                    "leg2_high": round(b1.high, 2),
                    "rejection_type": "l2_reversal",
                    "sl_price": sl_price,
                    "buffer_pts": round(buffer_pts, 2),
                }
                return True, "albrooks_l2_put_exhaustion", details
            return False, "l2_structure_not_met", {}

        elif direction.upper() in ("UP", "CALL"):
            if current_vwap > 0 and bar.close <= current_vwap:
                return False, "below_vwap", {}

            # Leg 2 must reach 50 EMA with bullish reversal.
            tested_50 = abs(b1.low - live_ema_50) <= tol_pts or (b1.low <= (live_ema_50 + tol_pts) and b1.close >= (live_ema_50 - tol_pts))
            if not (tested_50 and bar.close > bar.open):
                return False, "h2_structure_not_met", {}
            # Flexible Leg 1 search with same-side pause (bullish stays above EMA).
            bars = list(self.bars_1m)
            leg_found = False
            leg1_low = b1.low
            for k in range(6, min(41, len(bars))):
                cand = bars[-(k + 1)]
                c_test = abs(cand.low - live_ema_50) <= tol_pts or (cand.low <= (live_ema_50 + tol_pts) and cand.close >= (live_ema_50 - tol_pts))
                if not c_test:
                    continue
                # Leg 1 must sit above VWAP (bullish rising from VWAP).
                if current_vwap > 0 and cand.low <= current_vwap:
                    continue
                if not (b1.low < cand.low):
                    continue
                ok = True
                for mid in bars[-k:-1]:
                    if mid.close < (live_ema_50 - 0.5 * tol_pts):
                        ok = False
                        break
                if ok:
                    leg_found = True
                    leg1_low = cand.low
                    break
            # Fallback: first higher-low anywhere (bullish rising from VWAP).
            if not leg_found:
                for k in range(6, min(41, len(bars))):
                    cand = bars[-(k + 1)]
                    if not (cand.low < b1.low and cand.close > cand.open):
                        continue
                    # Above VWAP discipline for entire chain.
                    if current_vwap > 0 and cand.low <= current_vwap:
                        continue
                    ok = True
                    for mid in bars[-k:-1]:
                        if mid.close < (live_ema_50 - 0.5 * tol_pts):
                            ok = False
                            break
                    if ok:
                        leg_found = True
                        leg1_low = cand.low
                        break
            if not leg_found:
                return False, "h2_no_valid_leg1_same_side", {}
            if True:
                sl_price = round(min(bar.low, b1.low) - buffer_pts, 2)
                details = {
                    "pa_setup_name": "albrooks_2legged_pullback",
                    "pillar": 2,
                    "structure": "H2_bullish_exhaustion",
                    "ema_50": round(live_ema_50, 2),
                    "bar_open": round(bar.open, 2),
                    "bar_high": round(bar.high, 2),
                    "bar_low": round(bar.low, 2),
                    "bar_close": round(bar.close, 2),
                    "leg1_low": round(leg1_low, 2),
                    "leg2_low": round(b1.low, 2),
                    "rejection_type": "h2_reversal",
                    "sl_price": sl_price,
                    "buffer_pts": round(buffer_pts, 2),
                }
                return True, "albrooks_h2_call_exhaustion", details
            return False, "h2_structure_not_met", {}

        return False, "unknown_direction", {}

    def check_micro_sweep_reclaim(
        self,
        direction: str,
        current_vwap: float = 0.0,
        lookback_bars: int = 5,
        current_time: float = 0.0,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Pillar 2: Micro-Sweep & Reclaim (Turtle Soup / 5-Bar Liquidity Grab Scalp).
        Sweeps prior 5-bar swing low/high to hunt retail stops, then wick-rejects
        and closes firmly back inside the range in the direction of the dominant trend.
        Full-candle entries only.
        """
        # ─── LUNCH CHOP REGIME VETO (11:15 to 13:00 IST) ───
        # Micro-sweeps during midday lunch consolidation represent low-volume retail wick noise,
        # lacking institutional expansion follow-through.
        if os.getenv("AEGIS_LUNCH_SWEEP_VETO_ENABLED", "1") != "0":
            eval_ts = current_time if current_time > 0 else (
                self.bars_1m[-1].ts_start if (self.bars_1m and getattr(self.bars_1m[-1], "ts_start", 0) > 0) else getattr(self, "last_ts", 0.0)
            )
            if eval_ts > 0:
                from datetime import datetime as _dt, timezone as _tz, timedelta as _td
                _IST = _tz(_td(hours=5, minutes=30))
                dt_ist = _dt.fromtimestamp(eval_ts, _IST)
                time_mins = dt_ist.hour * 60 + dt_ist.minute
                if 675 <= time_mins < 780:
                    return False, "lunch_chop_sweep_veto", {
                        "time_ist": dt_ist.strftime("%H:%M:%S"),
                        "time_mins": time_mins,
                        "verdict": "LUNCH_CHOP_MICRO_SWEEP_BLOCKED",
                    }

        if len(self.bars_1m) < (lookback_bars + 1):
            return False, "insufficient_bars", {}

        bar = self.bars_1m[-1]
        if bar is None:
            return False, "no_closed_bar", {}

        total_range = bar.high - bar.low
        if total_range <= 0:
            return False, "zero_range", {}

        # Minimum range filter on sweep candle (filter micro-tick noise)
        ref_spot = self.last_price if self.last_price > 0 else 24000.0
        _atr_ref = self.current_atr_1m if self.current_atr_1m > 0 else (ref_spot * 0.001)
        if self.current_atr_1m > 0 and total_range < (0.25 * _atr_ref):
            return False, "tiny_range_noise", {}

        # Bar Close Freshness Window (max 15.0s cutoff)
        if current_time > 0 and bar.ts_end > 0:
            both_epoch = (current_time > 1_000_000_000.0 and bar.ts_end > 1_000_000_000.0)
            both_synthetic = (current_time < 1_000_000_000.0 and bar.ts_end < 1_000_000_000.0)
            if both_epoch or both_synthetic:
                time_since_close = current_time - bar.ts_end
                if time_since_close > 15.0:
                    return False, "stale_bar_entry_window_expired", {
                        "time_since_close": round(time_since_close, 1),
                        "max_allowed_s": 15.0,
                    }

        # Lookback prior 5 closed bars (excluding current bar)
        prior_bars = list(self.bars_1m)[-(lookback_bars + 1):-1]
        sw_low = min(b.low for b in prior_bars)
        sw_high = max(b.high for b in prior_bars)

        buffer_pts = max(5.0, (ref_spot / 24000.0) * 5.0)
        min_sweep_depth = max(1.5, (ref_spot / 24000.0) * 1.5, 0.08 * _atr_ref)

        # Live Spot Sanity Guard
        if self.last_price > 0:
            if direction.upper() in ("UP", "CALL") and self.last_price <= sw_low:
                return False, "live_price_below_sweep_low", {
                    "last_price": round(self.last_price, 2),
                    "sw_low": round(sw_low, 2),
                }
            elif direction.upper() in ("DOWN", "PUT") and self.last_price >= sw_high:
                return False, "live_price_above_sweep_high", {
                    "last_price": round(self.last_price, 2),
                    "sw_high": round(sw_high, 2),
                }

        live_ema_20, live_slope_20_bps = self.get_live_20_ema_and_slope()
        live_ema_50, live_slope_50_bps = self.get_live_50_ema_and_slope()

        if direction.upper() in ("UP", "CALL"):
            # Macro trend / VWAP alignment
            if current_vwap > 0 and bar.close <= current_vwap:
                return False, "below_vwap", {}

            # Micro sweep: dipped below prior swing low by at least min_sweep_depth, but closed firmly back above it
            lower_wick = min(bar.open, bar.close) - bar.low
            upper_wick = bar.high - max(bar.open, bar.close)
            wick_ratio = lower_wick / total_range
            upper_wick_ratio = upper_wick / total_range
            close_edge = (bar.close - bar.low) / total_range

            # Enforce clean absorption:
            # 1. Genuine stop hunt: swept below prior swing low by at least min_sweep_depth.
            # 2. Closed firmly back above swing low.
            # 3. Bullish directional close (Close > Open).
            # 4. Lower absorption wick >= 35% of total range.
            # 5. Upper rejection wick strictly capped at <= 15% (zero overhead selling pressure).
            # 6. Strong reclaim into upper body: close_edge >= 0.55.
            # 7. Lower wick clearly dominates upper wick (lower_wick >= 1.25 * upper_wick).
            is_sweep_reclaim = (
                (bar.low <= (sw_low - min_sweep_depth))
                and (bar.close > sw_low)
                and (bar.close > bar.open)
                and (wick_ratio >= 0.35)
                and (upper_wick_ratio <= 0.15)
                and (close_edge >= 0.55)
                and (lower_wick >= 1.25 * upper_wick)
            )

            if is_sweep_reclaim:
                # Structural Obstacle Shield
                ok_struct, r_struct, det_struct = self._check_structural_obstacle("CALL", bar.close)
                if not ok_struct:
                    return False, f"structural_obstacle_{r_struct}", det_struct

                sl_price = round(bar.low - buffer_pts, 2)
                details = {
                    "pa_setup_name": "micro_sweep_reclaim",
                    "pillar": 2,
                    "swing_low": round(sw_low, 2),
                    "sweep_depth": round(sw_low - bar.low, 2),
                    "min_sweep_depth": round(min_sweep_depth, 2),
                    "bar_open": round(bar.open, 2),
                    "bar_high": round(bar.high, 2),
                    "bar_low": round(bar.low, 2),
                    "bar_close": round(bar.close, 2),
                    "wick_ratio": round(wick_ratio, 3),
                    "upper_wick_ratio": round(upper_wick_ratio, 3),
                    "close_edge": round(close_edge, 3),
                    "rejection_type": "liquidity_sweep_call_reclaim",
                    "sl_price": sl_price,
                    "buffer_pts": round(buffer_pts, 2),
                }
                return True, "micro_sweep_call_reclaim", details
            return False, "sweep_structure_not_met", {}

        elif direction.upper() in ("DOWN", "PUT"):
            if current_vwap > 0 and bar.close >= current_vwap:
                return False, "above_vwap", {}

            # Micro sweep: popped above prior swing high by at least min_sweep_depth, but closed firmly back below it
            upper_wick = bar.high - max(bar.open, bar.close)
            lower_wick = min(bar.open, bar.close) - bar.low
            wick_ratio = upper_wick / total_range
            lower_wick_ratio = lower_wick / total_range
            close_edge = (bar.high - bar.close) / total_range

            # Enforce clean absorption:
            # 1. Genuine stop hunt: popped above prior swing high by at least min_sweep_depth.
            # 2. Closed firmly back below swing high.
            # 3. Bearish directional close (Close < Open).
            # 4. Upper absorption wick >= 35% of total range.
            # 5. Lower rejection wick strictly capped at <= 15% (zero underlying buying pressure).
            # 6. Strong reclaim into lower body: close_edge >= 0.55.
            # 7. Upper wick clearly dominates lower wick (upper_wick >= 1.25 * lower_wick).
            is_sweep_reclaim = (
                (bar.high >= (sw_high + min_sweep_depth))
                and (bar.close < sw_high)
                and (bar.close < bar.open)
                and (wick_ratio >= 0.35)
                and (lower_wick_ratio <= 0.15)
                and (close_edge >= 0.55)
                and (upper_wick >= 1.25 * lower_wick)
            )

            if is_sweep_reclaim:
                # Structural Obstacle Shield
                ok_struct, r_struct, det_struct = self._check_structural_obstacle("PUT", bar.close)
                if not ok_struct:
                    return False, f"structural_obstacle_{r_struct}", det_struct

                sl_price = round(bar.high + buffer_pts, 2)
                details = {
                    "pa_setup_name": "micro_sweep_reclaim",
                    "pillar": 2,
                    "swing_high": round(sw_high, 2),
                    "bar_open": round(bar.open, 2),
                    "bar_high": round(bar.high, 2),
                    "bar_low": round(bar.low, 2),
                    "bar_close": round(bar.close, 2),
                    "wick_ratio": round(wick_ratio, 3),
                    "lower_wick_ratio": round(lower_wick_ratio, 3),
                    "close_edge": round(close_edge, 3),
                    "rejection_type": "liquidity_sweep_put_reclaim",
                    "sl_price": sl_price,
                    "buffer_pts": round(buffer_pts, 2),
                }
                return True, "micro_sweep_put_reclaim", details
            return False, "sweep_structure_not_met", {}

        return False, "unknown_direction", {}

    def check_volatility_squeeze_breakout(
        self,
        direction: str,
        current_vwap: float = 0.0,
        current_time: float = 0.0,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Pillar 3: Volatility Squeeze to Expansion Breakout.
        Tight compression across prior 3-4 CLOSED bars (range <= 1.25 of ATR) followed by
        explosive breakout on the closed bar. Full-candle entries only.
        """
        if len(self.bars_1m) < 4:
            return False, "insufficient_bars", {}

        bar = self.bars_1m[-1]
        if bar is None:
            return False, "no_closed_bar", {}

        # Bar Close Freshness Window (max 15.0s cutoff)
        if current_time > 0 and bar.ts_end > 0:
            both_epoch = (current_time > 1_000_000_000.0 and bar.ts_end > 1_000_000_000.0)
            both_synthetic = (current_time < 1_000_000_000.0 and bar.ts_end < 1_000_000_000.0)
            if both_epoch or both_synthetic:
                time_since_close = current_time - bar.ts_end
                if time_since_close > 15.0:
                    return False, "stale_bar_entry_window_expired", {
                        "time_since_close": round(time_since_close, 1),
                        "max_allowed_s": 15.0,
                    }

        # Look back 3 to 4 prior closed bars
        n_box = 4 if len(self.bars_1m) >= 5 else 3
        recent_bars = list(self.bars_1m)[-(n_box + 1):-1]
        box_high = max(b.high for b in recent_bars)
        box_low = min(b.low for b in recent_bars)
        squeeze_range = box_high - box_low

        ref_spot = self.last_price if self.last_price > 0 else 24000.0
        atr = self.current_atr_1m if self.current_atr_1m > 0 else (ref_spot * 0.001)
        buffer_pts = max(5.0, (ref_spot / 24000.0) * 5.0)
        min_box_clearance = max(2.0, (ref_spot / 24000.0) * 2.0, 0.08 * atr)

        # Squeeze compression check (dynamically normalized to ATR)
        if squeeze_range > (1.25 * atr):
            return False, "not_in_squeeze", {}

        # Live Spot Sanity Guard
        if self.last_price > 0:
            if direction.upper() in ("UP", "CALL") and self.last_price < box_high:
                return False, "live_price_below_box_high", {
                    "last_price": round(self.last_price, 2),
                    "box_high": round(box_high, 2),
                }
            elif direction.upper() in ("DOWN", "PUT") and self.last_price > box_low:
                return False, "live_price_above_box_low", {
                    "last_price": round(self.last_price, 2),
                    "box_low": round(box_low, 2),
                }

        if direction.upper() in ("DOWN", "PUT"):
            if current_vwap > 0 and bar.close >= current_vwap:
                return False, "above_vwap", {}

            # Strict EMA Trend Alignment: Cannot short squeeze breakout when 20 EMA > 50 EMA (BULL_CLEAR)
            if self.ema_20_1m > 0 and self.ema_50_1m > 0 and self.ema_20_1m > self.ema_50_1m:
                return False, "bull_overlap_ema_disallowed", {}

            bar_range = bar.high - bar.low
            if bar_range <= 0:
                return False, "zero_range", {}
            bar_body = abs(bar.close - bar.open)
            lower_wick = min(bar.open, bar.close) - bar.low
            lower_wick_ratio = lower_wick / bar_range

            # Downside breakout: must clear box_low by at least min_box_clearance
            if bar.close <= (box_low - min_box_clearance):
                close_edge_put = (bar.high - bar.close) / bar_range
                if not (
                    (bar.close < bar.open)
                    and (bar_body >= 0.35 * bar_range)
                    and (close_edge_put >= 0.55)
                    and (lower_wick_ratio <= 0.18)
                ):
                    return False, "weak_squeeze_breakout_candle", {
                        "bar_open": round(bar.open, 2),
                        "bar_close": round(bar.close, 2),
                        "body_ratio": round(bar_body / bar_range, 3),
                        "close_edge": round(close_edge_put, 3),
                        "lower_wick_ratio": round(lower_wick_ratio, 3),
                    }

                # Structural Obstacle Shield (HVG Topological Anchors + Day Extreme Runway)
                ok_struct, r_struct, det_struct = self._check_structural_obstacle(
                    "PUT", bar.close, check_day_extremes=True
                )
                if not ok_struct:
                    return False, f"structural_obstacle_{r_struct}", det_struct

                sl_price = round(box_high + buffer_pts, 2)
                details = {
                    "pa_setup_name": "volatility_squeeze_breakout",
                    "pillar": 3,
                    "box_high": round(box_high, 2),
                    "box_low": round(box_low, 2),
                    "box_range": round(squeeze_range, 2),
                    "atr_14": round(atr, 2),
                    "min_box_clearance": round(min_box_clearance, 2),
                    "bar_open": round(bar.open, 2),
                    "bar_high": round(bar.high, 2),
                    "bar_low": round(bar.low, 2),
                    "bar_close": round(bar.close, 2),
                    "close_edge": round(close_edge_put, 3),
                    "lower_wick_ratio": round(lower_wick_ratio, 3),
                    "rejection_type": "squeeze_downside_breakout",
                    "sl_price": sl_price,
                    "buffer_pts": round(buffer_pts, 2),
                }
                return True, "volatility_squeeze_downside_breakout", details
            return False, "no_downside_breakout", {}

        elif direction.upper() in ("UP", "CALL"):
            if current_vwap > 0 and bar.close <= current_vwap:
                return False, "below_vwap", {}

            # Strict EMA Trend Alignment: Cannot buy squeeze breakout when 20 EMA < 50 EMA (BEAR_OVERLAP)
            if self.ema_20_1m > 0 and self.ema_50_1m > 0 and self.ema_20_1m < self.ema_50_1m:
                return False, "bear_overlap_ema_disallowed", {}

            bar_range = bar.high - bar.low
            if bar_range <= 0:
                return False, "zero_range", {}
            bar_body = abs(bar.close - bar.open)
            upper_wick = bar.high - max(bar.open, bar.close)
            upper_wick_ratio = upper_wick / bar_range

            # Upside breakout: must clear box_high by at least min_box_clearance
            if bar.close >= (box_high + min_box_clearance):
                close_edge_call = (bar.close - bar.low) / bar_range
                if not (
                    (bar.close > bar.open)
                    and (bar_body >= 0.35 * bar_range)
                    and (close_edge_call >= 0.55)
                    and (upper_wick_ratio <= 0.18)
                ):
                    return False, "weak_squeeze_breakout_candle", {
                        "bar_open": round(bar.open, 2),
                        "bar_close": round(bar.close, 2),
                        "body_ratio": round(bar_body / bar_range, 3),
                        "close_edge": round(close_edge_call, 3),
                        "upper_wick_ratio": round(upper_wick_ratio, 3),
                    }

                # Structural Obstacle Shield (HVG Topological Anchors + Day Extreme Runway)
                ok_struct, r_struct, det_struct = self._check_structural_obstacle(
                    "CALL", bar.close, check_day_extremes=True
                )
                if not ok_struct:
                    return False, f"structural_obstacle_{r_struct}", det_struct

                sl_price = round(box_low - buffer_pts, 2)
                details = {
                    "pa_setup_name": "volatility_squeeze_breakout",
                    "pillar": 3,
                    "box_high": round(box_high, 2),
                    "box_low": round(box_low, 2),
                    "box_range": round(squeeze_range, 2),
                    "atr_14": round(atr, 2),
                    "min_box_clearance": round(min_box_clearance, 2),
                    "bar_open": round(bar.open, 2),
                    "bar_high": round(bar.high, 2),
                    "bar_low": round(bar.low, 2),
                    "bar_close": round(bar.close, 2),
                    "close_edge": round(close_edge_call, 3),
                    "upper_wick_ratio": round(upper_wick_ratio, 3),
                    "rejection_type": "squeeze_upside_breakout",
                    "sl_price": sl_price,
                    "buffer_pts": round(buffer_pts, 2),
                }
                return True, "volatility_squeeze_upside_breakout", details
            return False, "no_upside_breakout", {}

        return False, "unknown_direction", {}

    def check_break_of_structure(
        self,
        direction: str,
        current_vwap: float = 0.0,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        [DISCARDED / RETIRED FROM LIVE TRADING] Legacy Naive 3-Bar BOS Level Breakout.
        Superseded by Pillar 4: Topological HVG Dual-Timeframe Network Phase Shift Breakout.
        Naive 3-bar local pivots lacked topological prominence and produced excessive noise.
        Kept strictly as a standalone method for backward-compatibility with existing tests.
        """
        if len(self.bars_1m) < 4:
            return False, "insufficient_bars", {}

        bar = self.bars_1m[-1]
        if bar is None:
            return False, "no_closed_bar", {}

        prior_bars = list(self.bars_1m)[:-1]
        if not prior_bars:
            return False, "insufficient_bars", {}

        ref_spot = self.last_price if self.last_price > 0 else (bar.close if bar.close > 0 else 24000.0)
        atr = self.current_atr_1m if self.current_atr_1m > 0 else (ref_spot * 0.001)
        buffer_pts = max(5.0, (ref_spot / 24000.0) * 5.0)

        live_ema_20, live_slope_20_bps = self.get_live_20_ema_and_slope()
        live_ema_50, live_slope_50_bps = self.get_live_50_ema_and_slope()

        total_range = bar.high - bar.low
        if total_range <= 0:
            return False, "zero_range", {}
        if total_range < (0.30 * atr):
            return False, "tiny_range_noise", {}

        lookback = min(20, len(prior_bars))
        lookback_bars = prior_bars[-lookback:]

        if direction.upper() in ("UP", "CALL"):
            # 1. Macro trend / VWAP alignment
            if current_vwap > 0 and bar.close <= current_vwap:
                return False, "below_vwap", {}

            # 2. Strict EMA Trend Alignment: Cannot buy breakout when 20 EMA < 50 EMA
            if self.ema_20_1m > 0 and self.ema_50_1m > 0 and self.ema_20_1m < self.ema_50_1m:
                return False, "bear_overlap_ema_disallowed", {}
            if live_slope_20_bps < -0.10:
                return False, "20_ema_slope_bearish", {}

            # 3. Candle quality: bullish impulse, solid body (>= 40%), strong close (upper 35%)
            if bar.close <= bar.open:
                return False, "not_bullish_candle", {}
            body = bar.close - bar.open
            if body < (0.40 * total_range):
                return False, "body_too_small", {}
            if (bar.close - bar.low) / total_range < 0.65:
                return False, "wick_rejection_at_high", {}

            # 4. Identify candidate structural resistance levels
            candidate_levels: List[Tuple[str, float]] = []
            if self.pdh > 0:
                candidate_levels.append(("pdh", self.pdh))

            # Pivot highs in lookback
            if len(lookback_bars) >= 3:
                for i in range(1, len(lookback_bars) - 1):
                    prev_b = lookback_bars[i - 1]
                    curr_b = lookback_bars[i]
                    next_b = lookback_bars[i + 1]
                    if curr_b.high >= prev_b.high and curr_b.high > next_b.high:
                        candidate_levels.append(("pivot_high", curr_b.high))

            # Prior swing high formed before immediate prior bar (at least 2 bars established)
            if len(prior_bars) >= 3:
                prior_swing_h = max(b.high for b in prior_bars[:-1])
                candidate_levels.append(("prior_swing_high", prior_swing_h))

            # 5. Evaluate candidate breakouts
            broken_candidates: List[Tuple[str, float]] = []
            threshold_pts = (self.proximity_threshold_bps / 10000.0) * ref_spot

            prior_margin = max(0.10 * atr, buffer_pts * 0.25)
            open_margin = max(0.25 * atr, buffer_pts * 0.50)
            ext_cap = max(1.25 * atr, buffer_pts * 1.50)

            for name, lvl in candidate_levels:
                if lvl <= 0:
                    continue
                # Current bar must close above the level
                if bar.close <= lvl:
                    continue
                # Prior closed bar must have closed at or below the level (+ small float margin)
                if prior_bars[-1].close > (lvl + prior_margin):
                    continue
                # Breakout candle must have started near or below the level (not an unfillable gap)
                if bar.open > (lvl + open_margin):
                    continue
                # Anti-chasing extension cap: close must not be overextended beyond level
                if (bar.close - lvl) > ext_cap:
                    continue

                # Overhead PDH proximity check: if this broken level is NOT PDH, but PDH is directly overhead
                if name != "pdh" and self.pdh > 0 and bar.close < self.pdh:
                    if (self.pdh - bar.close) <= threshold_pts:
                        continue  # Blocked: breaking intraday pivot directly into unbroken PDH resistance

                broken_candidates.append((name, lvl))

            if not broken_candidates:
                return False, "no_bos_level_broken", {}

            # Pick the highest broken level as the decisive structural resistance
            best_name, broken_lvl = max(broken_candidates, key=lambda x: x[1])
            sl_price = round(min(bar.low - buffer_pts, broken_lvl - buffer_pts), 2)

            details = {
                "pa_setup_name": "break_of_structure",
                "pillar": 4,
                "broken_level": round(broken_lvl, 2),
                "broken_level_type": best_name,
                "bar_open": round(bar.open, 2),
                "bar_high": round(bar.high, 2),
                "bar_low": round(bar.low, 2),
                "bar_close": round(bar.close, 2),
                "total_range": round(total_range, 2),
                "body": round(body, 2),
                "atr_14": round(atr, 2),
                "extension_pts": round(bar.close - broken_lvl, 2),
                "rejection_type": "bos_breakout_confirmation",
                "sl_price": sl_price,
                "buffer_pts": round(buffer_pts, 2),
                "ema_20": round(live_ema_20, 2),
                "ema_50": round(live_ema_50, 2),
            }
            return True, f"bos_call_breakout ({best_name})", details

        elif direction.upper() in ("DOWN", "PUT"):
            # 1. Macro trend / VWAP alignment
            if current_vwap > 0 and bar.close >= current_vwap:
                return False, "above_vwap", {}

            # 2. Strict EMA Trend Alignment: Cannot short breakdown when 20 EMA > 50 EMA
            if self.ema_20_1m > 0 and self.ema_50_1m > 0 and self.ema_20_1m > self.ema_50_1m:
                return False, "bull_overlap_ema_disallowed", {}
            if live_slope_20_bps > 0.10:
                return False, "20_ema_slope_bullish", {}

            # 3. Candle quality: bearish impulse, solid body (>= 40%), strong close (lower 35%)
            if bar.close >= bar.open:
                return False, "not_bearish_candle", {}
            body = bar.open - bar.close
            if body < (0.40 * total_range):
                return False, "body_too_small", {}
            if (bar.high - bar.close) / total_range < 0.65:
                return False, "wick_rejection_at_low", {}

            # 4. Identify candidate structural support levels
            candidate_levels: List[Tuple[str, float]] = []
            if self.pdl > 0:
                candidate_levels.append(("pdl", self.pdl))

            # Pivot lows in lookback
            if len(lookback_bars) >= 3:
                for i in range(1, len(lookback_bars) - 1):
                    prev_b = lookback_bars[i - 1]
                    curr_b = lookback_bars[i]
                    next_b = lookback_bars[i + 1]
                    if curr_b.low <= prev_b.low and curr_b.low < next_b.low:
                        candidate_levels.append(("pivot_low", curr_b.low))

            # Prior swing low formed before immediate prior bar (at least 2 bars established)
            if len(prior_bars) >= 3:
                prior_swing_l = min(b.low for b in prior_bars[:-1])
                candidate_levels.append(("prior_swing_low", prior_swing_l))

            # 5. Evaluate candidate breakdowns
            broken_candidates: List[Tuple[str, float]] = []
            threshold_pts = (self.proximity_threshold_bps / 10000.0) * ref_spot

            prior_margin = max(0.10 * atr, buffer_pts * 0.25)
            open_margin = max(0.25 * atr, buffer_pts * 0.50)
            ext_cap = max(1.25 * atr, buffer_pts * 1.50)

            for name, lvl in candidate_levels:
                if lvl <= 0:
                    continue
                # Current bar must close below the level
                if bar.close >= lvl:
                    continue
                # Prior closed bar must have closed at or above the level (- small float margin)
                if prior_bars[-1].close < (lvl - prior_margin):
                    continue
                # Breakdown candle must have started near or above the level (not an unfillable gap)
                if bar.open < (lvl - open_margin):
                    continue
                # Anti-chasing extension cap: close must not be overextended beyond level
                if (lvl - bar.close) > ext_cap:
                    continue

                # Underlying PDL proximity check: if this broken level is NOT PDL, but PDL is directly below
                if name != "pdl" and self.pdl > 0 and bar.close > self.pdl:
                    if (bar.close - self.pdl) <= threshold_pts:
                        continue  # Blocked: breaking intraday pivot directly into unbroken PDL support

                broken_candidates.append((name, lvl))

            if not broken_candidates:
                return False, "no_bos_level_broken", {}

            # Pick the lowest broken level as the decisive structural support
            best_name, broken_lvl = min(broken_candidates, key=lambda x: x[1])
            sl_price = round(max(bar.high + buffer_pts, broken_lvl + buffer_pts), 2)

            details = {
                "pa_setup_name": "break_of_structure",
                "pillar": 4,
                "broken_level": round(broken_lvl, 2),
                "broken_level_type": best_name,
                "bar_open": round(bar.open, 2),
                "bar_high": round(bar.high, 2),
                "bar_low": round(bar.low, 2),
                "bar_close": round(bar.close, 2),
                "total_range": round(total_range, 2),
                "body": round(body, 2),
                "atr_14": round(atr, 2),
                "extension_pts": round(broken_lvl - bar.close, 2),
                "rejection_type": "bos_breakdown_confirmation",
                "sl_price": sl_price,
                "buffer_pts": round(buffer_pts, 2),
                "ema_20": round(live_ema_20, 2),
                "ema_50": round(live_ema_50, 2),
            }
            return True, f"bos_put_breakdown ({best_name})", details

        return False, "unknown_direction", {}

    def check_topological_prominence_breakout(
        self,
        direction: str,
        current_vwap: float = 0.0,
        current_time: float = 0.0,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Pillar 4 (V4 Final): 0-D Prominence Hub Structural Breakout.
        Evaluates 0-D Prominence (\\hat{\\pi} >= 1.75) via FastMonotonicStack,
        5m structural alignment, and Volume Ratio Gate (VR_t >= 1.4 vs 60m median).
        """
        if len(self.bars_1m) < 5 or self.current_atr_1m <= 1e-6:
            return False, "insufficient_bars", {}

        bar = self.bars_1m[-1]
        if bar is None:
            return False, "no_closed_bar", {}

        # Bar Close Freshness Window (max 15.0s cutoff)
        if current_time > 0 and bar.ts_end > 0:
            both_epoch = (current_time > 1_000_000_000.0 and bar.ts_end > 1_000_000_000.0)
            both_synthetic = (current_time < 1_000_000_000.0 and bar.ts_end < 1_000_000_000.0)
            if both_epoch or both_synthetic:
                time_since_close = current_time - bar.ts_end
                if time_since_close > 15.0:
                    return False, "stale_bar_entry_window_expired", {
                        "time_since_close": round(time_since_close, 1),
                        "max_allowed_s": 15.0,
                    }

        prices_window = np.array([b.close for b in list(self.bars_1m)[-60:]], dtype=np.float64)
        if len(prices_window) < 5:
            return False, "insufficient_window", {}

        hub_idx = len(prices_window) - 2
        if not hasattr(self, "prominence_stack") or self.prominence_stack is None:
            if FastMonotonicStack is not None:
                self.prominence_stack = FastMonotonicStack(capacity=128)

        if self.prominence_stack is None:
            return False, "no_prominence_stack", {}

        pi_hat = self.prominence_stack.compute_prominence(prices_window, hub_idx, self.current_atr_1m)
        if pi_hat < 1.75:
            return False, f"insufficient_prominence_{pi_hat:.2f}", {}

        # Volume Gate: Volume Ratio VR_t >= 1.4 vs 60m median volume
        vols_60 = [float(getattr(b, "volume", 0.0) or 0.0) for b in list(self.bars_1m)[-60:]]
        med_vol = float(np.median(vols_60)) if vols_60 else 1.0
        curr_vol = float(getattr(bar, "volume", 0.0) or 0.0)
        vr_t = curr_vol / med_vol if med_vol > 0 else 1.0
        if vr_t < 1.4:
            return False, f"volume_ratio_gate_failed_{vr_t:.2f}", {}

        # 5m Structural Alignment Gate
        trend_5m_bullish = True
        trend_5m_bearish = True
        if self.bars_5m:
            bar_5m = self.bars_5m[-1]
            trend_5m_bullish = bar_5m.close >= bar_5m.open
            trend_5m_bearish = bar_5m.close <= bar_5m.open

        hub_price = float(prices_window[hub_idx])
        is_bullish = direction.upper() in ("UP", "CALL")
        is_bearish = direction.upper() in ("DOWN", "PUT")

        # Live Spot Sanity Guard
        if self.last_price > 0:
            if is_bullish and self.last_price < hub_price:
                return False, "live_price_below_hub", {
                    "last_price": round(self.last_price, 2),
                    "hub_price": round(hub_price, 2),
                }
            elif is_bearish and self.last_price > hub_price:
                return False, "live_price_above_hub", {
                    "last_price": round(self.last_price, 2),
                    "hub_price": round(hub_price, 2),
                }

        ref_spot = self.last_price if self.last_price > 0 else hub_price
        buffer_pts = max(5.0, (ref_spot / 24000.0) * 5.0)
        _atr_ref = self.current_atr_1m if self.current_atr_1m > 0 else (ref_spot * 0.001)
        min_hub_clearance = max(2.0, (ref_spot / 24000.0) * 2.0, 0.08 * _atr_ref)

        total_range = max(bar.high - bar.low, 0.1)
        body = abs(bar.close - bar.open)
        body_ratio = body / total_range
        upper_wick = bar.high - max(bar.open, bar.close)
        lower_wick = min(bar.open, bar.close) - bar.low
        upper_wick_ratio = upper_wick / total_range
        lower_wick_ratio = lower_wick / total_range
        close_edge_call = (bar.close - bar.low) / total_range
        close_edge_put = (bar.high - bar.close) / total_range

        if is_bullish and trend_5m_bullish:
            # 1. Anti-Exhaustion & Origin Validation:
            bar_range = bar.high - bar.low
            if bar_range > (1.35 * _atr_ref):
                return False, "exhausted_breakout_candle_range", {
                    "bar_range": round(bar_range, 2),
                    "max_allowed_range": round(1.35 * _atr_ref, 2),
                    "atr_ref": round(_atr_ref, 2),
                }

            displacement = abs(bar.close - hub_price)
            if displacement > (1.25 * _atr_ref):
                return False, "overextended_from_hub", {
                    "displacement": round(displacement, 2),
                    "max_allowed": round(1.25 * _atr_ref, 2),
                    "atr_ref": round(_atr_ref, 2),
                }

            if bar.open > (hub_price + 0.25 * _atr_ref):
                return False, "bar_opened_too_far_above_hub", {
                    "bar_open": round(bar.open, 2),
                    "hub_price": round(hub_price, 2),
                    "threshold": round(hub_price + 0.25 * _atr_ref, 2),
                }

            # Enforce solid breakout candle anatomy (No shooting stars / inverted hammer traps):
            is_solid_breakout = (
                bar.close >= (hub_price + min_hub_clearance)
                and bar.close > bar.open
                and body_ratio >= 0.35
                and close_edge_call >= 0.70
                and upper_wick_ratio <= 0.20
            )
            if is_solid_breakout:
                if current_vwap > 0 and bar.close < current_vwap:
                    return False, "counter_to_vwap", {}

                # Structural Obstacle Shield (HVG Topological Anchors + Day Extreme Runway)
                ok_struct, r_struct, det_struct = self._check_structural_obstacle(
                    "CALL", bar.close, check_day_extremes=True
                )
                if not ok_struct:
                    return False, f"structural_obstacle_{r_struct}", det_struct

                sl_price = round(bar.low - buffer_pts, 2)
                return True, "prominence_hub_breakout_up", {
                    "pa_setup_name": "topological_prominence_breakout",
                    "pillar": 4,
                    "hub_price": hub_price,
                    "min_hub_clearance": round(min_hub_clearance, 2),
                    "pi_hat": pi_hat,
                    "vr_t": vr_t,
                    "breakout_direction": "UP",
                    "bar_open": round(bar.open, 2),
                    "bar_close": round(bar.close, 2),
                    "bar_low": round(bar.low, 2),
                    "bar_high": round(bar.high, 2),
                    "body_ratio": round(body_ratio, 3),
                    "upper_wick_ratio": round(upper_wick_ratio, 3),
                    "close_edge": round(close_edge_call, 3),
                    "sl_price": sl_price,
                    "buffer_pts": round(buffer_pts, 2),
                }

        if is_bearish and trend_5m_bearish:
            # 1. Anti-Exhaustion & Origin Validation:
            bar_range = bar.high - bar.low
            if bar_range > (1.35 * _atr_ref):
                return False, "exhausted_breakout_candle_range", {
                    "bar_range": round(bar_range, 2),
                    "max_allowed_range": round(1.35 * _atr_ref, 2),
                    "atr_ref": round(_atr_ref, 2),
                }

            displacement = abs(bar.close - hub_price)
            if displacement > (1.25 * _atr_ref):
                return False, "overextended_from_hub", {
                    "displacement": round(displacement, 2),
                    "max_allowed": round(1.25 * _atr_ref, 2),
                    "atr_ref": round(_atr_ref, 2),
                }

            if bar.open < (hub_price - 0.25 * _atr_ref):
                return False, "bar_opened_too_far_below_hub", {
                    "bar_open": round(bar.open, 2),
                    "hub_price": round(hub_price, 2),
                    "threshold": round(hub_price - 0.25 * _atr_ref, 2),
                }

            # Enforce solid breakout candle anatomy (No hammer / bullish bounce traps):
            is_solid_breakout = (
                bar.close <= (hub_price - min_hub_clearance)
                and bar.close < bar.open
                and body_ratio >= 0.35
                and close_edge_put >= 0.70
                and lower_wick_ratio <= 0.20
            )
            if is_solid_breakout:
                if current_vwap > 0 and bar.close > current_vwap:
                    return False, "counter_to_vwap", {}

                # Structural Obstacle Shield (HVG Topological Anchors + Day Extreme Runway)
                ok_struct, r_struct, det_struct = self._check_structural_obstacle(
                    "PUT", bar.close, check_day_extremes=True
                )
                if not ok_struct:
                    return False, f"structural_obstacle_{r_struct}", det_struct

                sl_price = round(bar.high + buffer_pts, 2)
                return True, "prominence_hub_breakout_down", {
                    "pa_setup_name": "topological_prominence_breakout",
                    "pillar": 4,
                    "hub_price": hub_price,
                    "min_hub_clearance": round(min_hub_clearance, 2),
                    "pi_hat": pi_hat,
                    "vr_t": vr_t,
                    "breakout_direction": "DOWN",
                    "bar_open": round(bar.open, 2),
                    "bar_close": round(bar.close, 2),
                    "bar_low": round(bar.low, 2),
                    "bar_high": round(bar.high, 2),
                    "body_ratio": round(body_ratio, 3),
                    "lower_wick_ratio": round(lower_wick_ratio, 3),
                    "close_edge": round(close_edge_put, 3),
                    "sl_price": sl_price,
                    "buffer_pts": round(buffer_pts, 2),
                }

        return False, "no_breakout_against_hub", {}

    # ═══════════════════════════════════════════════════════════
    # 4. MASTER SIGNAL DISPATCHER
    # ═══════════════════════════════════════════════════════════

    def get_volume_sma(self, lookback: int = 10) -> float:
        """Computes simple moving average of 1m bar volume over lookback periods."""
        if not self.bars_1m:
            return 0.0
        n = min(len(self.bars_1m), max(1, lookback))
        vols = [float(getattr(b, "volume", 0.0) or 0.0) for b in list(self.bars_1m)[-n:]]
        return (sum(vols) / float(n)) if n > 0 else 0.0

    def get_relative_bar_metrics(self, bar: Optional[OHLCBar] = None) -> Dict[str, Any]:
        """Calculates scale-invariant bar geometry, body %, wick %, and volume ratio."""
        b = bar or (self.bars_1m[-1] if self.bars_1m else None) or self._curr_1m_bar
        if not b:
            return {}
        rng = max(b.high - b.low, 0.1)
        body = abs(b.close - b.open)
        body_pct = body / rng
        upper_wick = (b.high - max(b.open, b.close)) / rng
        lower_wick = (min(b.open, b.close) - b.low) / rng
        close_pos = (b.close - b.low) / rng
        atr = max(self.current_atr_1m, 1.0)
        range_atr = rng / atr
        vol_sma = self.get_volume_sma(10)
        b_vol = float(getattr(b, "volume", 1.0) or 1.0)
        vol_ratio = (b_vol / vol_sma) if vol_sma > 0 else 1.0
        return {
            "bar_open": round(b.open, 2),
            "bar_high": round(b.high, 2),
            "bar_low": round(b.low, 2),
            "bar_close": round(b.close, 2),
            "bar_volume": round(b_vol, 2),
            "bar_range": round(rng, 2),
            "body_pct": round(body_pct, 3),
            "upper_wick_pct": round(upper_wick, 3),
            "lower_wick_pct": round(lower_wick, 3),
            "close_pos": round(close_pos, 3),
            "range_atr": round(range_atr, 2),
            "vol_sma10": round(vol_sma, 2),
            "vol_ratio": round(vol_ratio, 2),
        }

    def _enrich_details(self, details: Dict[str, Any]) -> Dict[str, Any]:
        """Enriches setup details dict with relative bar metrics and volume SMA."""
        closed_bar = (self.bars_1m[-1] if self.bars_1m else None) or self._curr_1m_bar
        bar_m = self.get_relative_bar_metrics(bar=closed_bar)
        for k, v in bar_m.items():
            if k not in details:
                details[k] = v
        return details

    def evaluate_setup(
        self,
        direction: str,
        external_vwap: float = 0.0,
        current_time: float = 0.0,
        gamma_regime: str = "NEUTRAL",
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Master Multi-Pillar Price Action Evaluator (V4.0: strict P1 -> P2 -> P3 -> P4
        single-admission ladder, first hit wins — never an AND cascade).
        Evaluates the 4 empirically verified Price Action Scalper Engines:
          Pillar 1: 20 EMA + Wick Rejection (Double-Touch Confluence)
          Pillar 2: Micro-Sweep & Reclaim (Turtle Soup / 5-Bar Liquidity Grab)
          Pillar 3: Volatility Squeeze to Expansion Breakout (3-Bar Tight Box Expansion)
          Pillar 4: 0-D Prominence Hub Breakout (pi-hat >= 1.75, VR >= 1.4, 5m align)
                      + Dual-Timeframe HVG Phase Shift (gated by shadow_only flag)
        Returns:
            (is_triggered, setup_name, details_dict)
        """
        eff_vwap = external_vwap if external_vwap > 0 else self.current_vwap

        # Pillar 1: 20 EMA Pullback Rejection (Double-Touch)
        # V4.1: second-place Pain Zone removed — absolute band veto at tick top is single source.
        ok_20, reason_20, details_20 = self.check_20_ema_rejection(direction, current_vwap=eff_vwap, current_time=current_time)
        if ok_20:
            details_20["pain_zone_score"] = 0.0
            details_20["pain_zone_reason"] = "band_veto_top_only"
            return True, "20_ema_rejection", self._enrich_details(details_20)

        # Pillar 2: Micro-Sweep & Reclaim (Parallel Shadow A/B Tracking)
        if getattr(self, "hvg_engine", None) is not None:
            try:
                self.hvg_engine.check_topological_sweep_reclaim(direction, eff_vwap)
            except Exception as e:
                logger.debug(f"[PriceActionEngine] HVG shadow sweep check failed: {e}")

        ok_sw, reason_sw, details_sw = self.check_micro_sweep_reclaim(direction, eff_vwap, current_time=current_time)
        if ok_sw:
            # V4.1: second-place Pain Zone removed — absolute band veto at tick top is single source.
            details_sw["pain_zone_score"] = 0.0
            details_sw["pain_zone_reason"] = "band_veto_top_only"
            details_sw["strategy_variant"] = "LEGACY_P2"
            bar_ts = self.bars_1m[-1].ts_start if self.bars_1m else self.last_ts
            details_sw["event_id"] = f"LEGACY_P2_{int(bar_ts)}_{direction.upper()}"
            return True, "micro_sweep_reclaim", self._enrich_details(details_sw)

        # V4.0 (2026-09-21): Al Brooks 2-Legged Pullback retired from the live
        # ladder — P1 -> P2 -> P3 -> P4 only. Method kept for offline reference.

        # Pillar 3: Volatility Squeeze Breakout (3-Bar Tight Box Expansion)
        # V4.1: second-place Pain Zone removed — absolute band veto at tick top is single source.
        ok_sq, reason_sq, details_sq = self.check_volatility_squeeze_breakout(direction, eff_vwap, current_time=current_time)
        if ok_sq:
            details_sq["pain_zone_score"] = 0.0
            details_sq["pain_zone_reason"] = "band_veto_top_only"
            return True, "volatility_squeeze_breakout", self._enrich_details(details_sq)

        # Pillar 4 (V4 Final): 0-D Prominence Hub Structural Breakout (\hat{\pi} >= 1.75, VR_t >= 1.4, 5m Alignment)
        # V4.1: second-place Pain Zone removed — absolute band veto at tick top is single source.
        ok_prom, reason_prom, details_prom = self.check_topological_prominence_breakout(direction, eff_vwap, current_time=current_time)
        if ok_prom:
            details_prom["pain_zone_score"] = 0.0
            details_prom["pain_zone_reason"] = "band_veto_top_only"
            bar_ts = self.bars_1m[-1].ts_start if self.bars_1m else self.last_ts
            details_prom["timestamp"] = bar_ts
            details_prom["event_id"] = f"P4_PROM_{int(bar_ts)}_{direction.upper()}"
            return True, "topological_prominence_breakout", self._enrich_details(details_prom)

        # Pillar 4 Fallback: Topological HVG Phase Shift Breakout (Dual-Timeframe 1m + 5m)
        if getattr(self, "hvg_engine", None) is not None:
            try:
                l2_depth = getattr(self, "_l2_depth_history", None)
                ok_hvg, reason_hvg, details_hvg = self.hvg_engine.evaluate_phase_shift(
                    direction=direction,
                    ema_20=self.ema_20_1m,
                    ema_50=self.ema_50_1m,
                    current_vwap=eff_vwap,
                    l2_depth_history=l2_depth,
                )
                if ok_hvg and not getattr(self.hvg_engine, "shadow_only", True):
                    return True, "hvg_network_phase_shift", self._enrich_details(details_hvg)
            except Exception:
                pass

        # V4.0 (2026-09-21): standalone Break of Structure retired from the live
        # ladder — superseded by P4 topological phase shift. Method kept offline.

        return False, "no_price_action_setup", {}

    # Backward compatibility alias for any existing code calling check_price_action_setups
    check_price_action_setups = evaluate_setup

    def get_market_structure(self, direction: str) -> str:
        """
        Evaluates 3-State Market Structure using closed bars and active in-flight bar:
        - 'ALIGNED': Structure confirms the trade direction (HH/HL for UP, LH/LL for DOWN).
        - 'NEUTRAL': Range-bound / consolidation / inside bars.
        - 'OPPOSED': Structure directly opposes trade direction.
        """
        bars = list(self.bars_1m)
        if self._curr_1m_bar is not None:
            bars.append(self._curr_1m_bar)

        if len(bars) < 3:
            return "NEUTRAL"

        b = bars
        h1, h2, h3 = b[-3].high, b[-2].high, b[-1].high
        l1, l2, l3 = b[-3].low, b[-2].low, b[-1].low

        is_higher_highs = (h3 > h2) and (h2 >= h1)
        is_higher_lows = (l3 > l2) and (l2 >= l1)
        is_lower_highs = (h3 < h2) and (h2 <= h1)
        is_lower_lows = (l3 < l2) and (l2 <= l1)

        if direction.upper() in ("UP", "CALL"):
            if is_higher_highs or (is_higher_lows and b[-1].close > b[-2].close):
                return "ALIGNED"
            elif is_lower_highs and is_lower_lows and b[-1].close < b[-2].close:
                return "OPPOSED"
            else:
                return "NEUTRAL"

        elif direction.upper() in ("DOWN", "PUT"):
            if is_lower_lows or (is_lower_highs and b[-1].close < b[-2].close):
                return "ALIGNED"
            elif is_higher_highs and is_higher_lows and b[-1].close > b[-2].close:
                return "OPPOSED"
            else:
                return "NEUTRAL"

        return "NEUTRAL"

    def get_permission_state(self, direction: str) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Backward compatibility permission evaluator.
        Returns:
            (has_permission, reason, metrics)
        """
        live_ema_20, live_slope_20_bps, live_ema_50, live_slope_50_bps = self.get_live_ema_and_slope()
        metrics = {
            "ema_1m_live": round(live_ema_20, 2),
            "ema_3m_live": round(live_ema_50, 2),
            "ema_20_1m_live": round(live_ema_20, 2),
            "ema_50_1m_live": round(live_ema_50, 2),
            "ema_1m_slope": round(self.ema_20_1m_slope, 2),
            "ema_1m_slope_bps": round(live_slope_20_bps, 2),
            "ema_50_1m_slope_bps": round(live_slope_50_bps, 2),
            "atr_ratio": round(self.atr_ratio, 2),
            "current_atr_1m": round(self.current_atr_1m, 2),
            "market_structure": self.get_market_structure(direction),
            "slope_veto_bps": self.slope_veto_bps,
        }

        # Warmup check
        if len(self.bars_1m) < 2:
            return True, "warmup_permissive", metrics

        struct = metrics["market_structure"]
        if struct == "OPPOSED":
            return False, f"market_structure_opposed ({struct})", metrics

        # 50 EMA Trend Permission
        if self.ema_50_1m > 0:
            if direction.upper() in ("UP", "CALL"):
                if live_slope_50_bps < -0.10:
                    return False, f"50m_trend_bearish ({live_slope_50_bps:+.2f}bps)", metrics
            elif direction.upper() in ("DOWN", "PUT"):
                if live_slope_50_bps > 0.10:
                    return False, f"50m_trend_bullish ({live_slope_50_bps:+.2f}bps)", metrics

        return True, "permission_granted", metrics

