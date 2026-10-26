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
from datetime import datetime, time as dt_time, timezone, timedelta

_IST = timezone(timedelta(hours=5, minutes=30))
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


@dataclass
class StructuralLevel:
    """Represents an intraday Support or Resistance level identified by topological prominence or multi-touch."""
    level_type: str        # "SUPPORT" or "RESISTANCE"
    price: float           # Price level
    ts_created: float      # Timestamp when established
    prominence: float      # Prominence score (|depth| / ATR + volume boost)
    bar_idx: int           # Index in bars_1m where formed
    seq_idx: int = 0       # Monotonic sequential bar counter
    touch_count: int = 1   # Number of times price tested this level
    is_broken: bool = False
    ts_broken: float = 0.0
    # ─── Volume & Order Flow Footprint at Anchor / Retests ───
    volume: float = 0.0            # Underlying / bar traded volume at anchor
    call_buy_vol: float = 0.0      # Aggressive Call Buying (lifted at ask)
    call_sell_vol: float = 0.0     # Call Writing / Liquidation (hit at bid)
    put_buy_vol: float = 0.0       # Aggressive Put Buying (lifted at ask)
    put_sell_vol: float = 0.0      # Put Writing / Liquidation (hit at bid)
    opt_delta_flow: float = 0.0    # Directional Flow: call_buy_vol - put_buy_vol
    opt_total_vol: float = 0.0     # Total Option volume across ATM window
    opt_pcr: float = 1.0           # Put / Call volume ratio
    vr_anchor: float = 1.0         # Volume ratio vs 60m median volume at creation
    net_delta_flow: float = 0.0    # Directional Flow alias
    pcr_at_level: float = 1.0      # PCR at level alias
    vol_ratio_at_level: float = 1.0 # VR at level alias


class PriceActionEngine:
    """
    Dedicated, end-to-end engine for real-time 1m bar aggregation, EMA tracking,
    ATR calculation, intraday VWAP, and the 3 core Price Action scalper setups.
    """

    def __init__(
        self,
        strike_step: float = 50.0,
        max_history_bars: int = 400,
        atr_period: int = 14,
        lot_size: Optional[float] = None,
    ):
        self.strike_step = strike_step
        self.max_history_bars = int(os.getenv("AEGIS_MAX_HISTORY_BARS", str(max_history_bars)))
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
        self.bars_1m: deque[OHLCBar] = deque(maxlen=self.max_history_bars)
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
        self._last_global_opt_tick_ts: float = 0.0
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
        self.ema_20_history: deque[float] = deque(maxlen=self.max_history_bars)
        self.ema_20_high_1m: float = 0.0
        self.ema_20_low_1m: float = 0.0
        self.ema_20_high_history: deque[float] = deque(maxlen=self.max_history_bars)
        self.ema_20_low_history: deque[float] = deque(maxlen=self.max_history_bars)

        self.ema_50_1m: float = 0.0
        self.ema_50_1m_prev: float = 0.0
        self.ema_50_1m_slope: float = 0.0

        self.ema_200_1m: float = 0.0
        self.ema_200_1m_prev: float = 0.0
        self.ema_200_1m_slope: float = 0.0
        self.ema_200_history: deque[float] = deque(maxlen=self.max_history_bars)

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

        # ─── Dynamic Session S/R Level Registry (Pillar 4 Multi-Bar Memory) ───
        self.session_levels: List[StructuralLevel] = []
        self._levels_day: int = -1
        self._total_bars_seen: int = 0
        # ─── Daily Structural Levels (PDH / PDL / PDC) ───
        self.pdh: float = 0.0
        self.pdl: float = 0.0
        self.pdc: float = 0.0
        self.proximity_threshold_bps: float = float(os.getenv("AEGIS_KEY_LEVEL_PROXIMITY_BPS", "8.0"))
        # ─── AEGIS V4.0 Topological 0-D Prominence Stack (Pillar 4 Final) ───
        self.prominence_stack: Any = FastMonotonicStack(capacity=128) if FastMonotonicStack is not None else None
        self._l2_depth_history: Optional[List[Tuple[float, float, float, float]]] = None

        # ─── Intra-Bar Mode A & Mode B High-Precision Execution State ───
        self._mode_b_up_ticks: int = 0
        self._mode_b_down_ticks: int = 0
        self._mode_a_armed_setups: Dict[str, Dict[str, Any]] = {}
        self._mode_a_last_trigger_bar_idx: Dict[str, int] = {}
        self._mode_b_last_trigger_bar_idx: Dict[str, int] = {}

    @property
    def current_bar(self) -> Optional[OHLCBar]:
        """Returns the in-flight 1-minute OHLC bar if building."""
        return self._curr_1m_bar

    def get_in_flight_opt_flow(self) -> Dict[str, float]:
        """Returns the current in-flight 1-minute accumulated option flow."""
        cb = float(self._opt_flow_1m.get("call_buy_vol", 0.0))
        cs = float(self._opt_flow_1m.get("call_sell_vol", 0.0))
        pb = float(self._opt_flow_1m.get("put_buy_vol", 0.0))
        ps = float(self._opt_flow_1m.get("put_sell_vol", 0.0))
        bull = cb + ps
        bear = pb + cs
        return {
            "call_buy_vol": cb,
            "call_sell_vol": cs,
            "put_buy_vol": pb,
            "put_sell_vol": ps,
            "bullish_vol": bull,
            "bearish_vol": bear,
            "total_vol": bull + bear,
            "net_delta": bull - bear,
        }

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
        self._last_global_opt_tick_ts = 0.0
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

    def get_pain_zone_band(self, lot_size: Optional[float] = None, min_bars: Optional[int] = None) -> Tuple[float, float]:
        """
        True Pain Zone Band Geometry (4-Line Min-Max Envelope):
          Upper Band = max(vwap_call_buy, vwap_call_sell, vwap_put_buy, vwap_put_sell)
          Lower Band = min(vwap_call_buy, vwap_call_sell, vwap_put_buy, vwap_put_sell)
        Returns (band_upper, band_lower). If warmup / lines <= 0, returns (0.0, 0.0).
        """
        pz = self.get_pain_zone_vwaps(lot_size=lot_size, min_bars=min_bars)
        return float(pz.get("zone_top", 0.0) or 0.0), float(pz.get("zone_bottom", 0.0) or 0.0)

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

    def _pain_zone_vwap_aligned(self, direction: str, price: float, eff_vwap: float = 0.0) -> bool:
        """
        True Pain Zone 4-Line Envelope Alignment (Outside-Band Rule).
        The 4 trapped-party option-flow VWAP lines (vwap_call_buy, vwap_call_sell,
        vwap_put_buy, vwap_put_sell) form a natural cluster band:
            band_upper = max(active_vwaps)
            band_lower = min(active_vwaps)

        Outside-Band Rule (आर-पार का नियम):
          CALL: Spot must be strictly ABOVE the highest active VWAP line (spot > band_upper).
          PUT:  Spot must be strictly BELOW the lowest active VWAP line (spot < band_lower).
          Inside Band (band_lower <= spot <= band_upper): Neutral / No-Trade Zone (vetoed for both).

        No separate 5th composite VWAP is needed when active envelope lines exist.
        Warmup / missing lines pass (fail-open), matching band_veto_blocks semantics.
        If no option-flow lines are active yet but a synthetic/external vwap is provided,
        falls back to single-line alignment (spot > vwap for CALL, spot < vwap for PUT).
        """
        try:
            if price <= 0:
                return True
            is_call = str(direction).upper() in ("UP", "CALL", "CE", "C")
            pz = self.get_pain_zone_vwaps()
            cb = float(pz.get("vwap_call_buy", 0.0) or 0.0)
            cs = float(pz.get("vwap_call_sell", 0.0) or 0.0)
            pb = float(pz.get("vwap_put_buy", 0.0) or 0.0)
            ps = float(pz.get("vwap_put_sell", 0.0) or 0.0)
            active_vwaps = [x for x in (cb, cs, pb, ps) if x > 0]

            if active_vwaps:
                band_upper = max(active_vwaps)
                band_lower = min(active_vwaps)
                if is_call:
                    return price > band_upper
                else:
                    return price < band_lower

            # Fallback when no option-flow VWAP lines active yet (warmup / synthetic test mocks)
            fallback_vwap = float(eff_vwap) if eff_vwap > 0 else float(getattr(self, "current_vwap", 0.0) or 0.0)
            if fallback_vwap > 0:
                if is_call:
                    return price > fallback_vwap
                else:
                    return price < fallback_vwap

            return True
        except Exception:
            return True

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

        import time as _time
        now_ts = self.last_ts if self.last_ts > 0 else _time.time()

        # Global WebSocket Reconnect / Freeze Guard:
        # If the entire options feed went silent between 5.0s and 55.0s, clear cum vols
        # so every symbol cleanly reseeds its baseline on its first post-reconnect tick.
        if self._last_global_opt_tick_ts > 0 and 5.0 < (now_ts - self._last_global_opt_tick_ts) < 55.0:
            self._opt_last_cum_vol.clear()
        self._last_global_opt_tick_ts = now_ts

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
        self._total_bars_seen += 1
        # Attach in-flight 1-minute Option Flow directly to the candle if active
        opt_in_flight_vol = (
            self._opt_flow_1m["call_buy_vol"]
            + self._opt_flow_1m["call_sell_vol"]
            + self._opt_flow_1m["put_buy_vol"]
            + self._opt_flow_1m["put_sell_vol"]
        )
        if opt_in_flight_vol > 0:
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
        elif bar.opt_total_vol <= 0 and (bar.call_buy_vol > 0 or bar.put_buy_vol > 0 or bar.call_sell_vol > 0 or bar.put_sell_vol > 0):
            bar.opt_delta_flow = bar.call_buy_vol - bar.put_buy_vol
            bar.opt_total_vol = bar.call_buy_vol + bar.call_sell_vol + bar.put_buy_vol + bar.put_sell_vol
            bar.opt_pcr = round((bar.put_buy_vol + bar.put_sell_vol) / max(bar.call_buy_vol + bar.call_sell_vol, 1.0), 3)

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

        # 3. Update 200 EMA
        k200 = 2.0 / (200.0 + 1.0)
        if self.ema_200_1m <= 0:
            self.ema_200_1m = bar.close
            self.ema_200_1m_prev = bar.close
            self.ema_200_1m_slope = 0.0
        else:
            self.ema_200_1m_prev = self.ema_200_1m
            self.ema_200_1m = (bar.close * k200) + (self.ema_200_1m * (1.0 - k200))
            self.ema_200_1m_slope = self.ema_200_1m - self.ema_200_1m_prev
        self.ema_200_history.append(self.ema_200_1m)

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

        # 5. Dynamic Session S/R Level Registry Update
        self._update_structural_levels(bar)

        # 6. Mode A Boundary Rejection Arming (Pain Zone & 200 EMA)
        self._evaluate_mode_a_arming(bar)

    def _update_structural_levels(self, bar: OHLCBar) -> None:
        """
        Dynamically discovers, registers, and tracks intraday Support & Resistance levels.
        Maintains an active session diary of structural peaks and troughs.
        """
        curr_day = int(bar.ts_start // 86400) if bar.ts_start > 0 else self._current_vwap_day
        if self._levels_day != curr_day:
            self.session_levels.clear()
            self._levels_day = curr_day
            # Re-seed daily macro anchors (PDH / PDL) for the new session
            if self.pdh > 0:
                self.session_levels.append(
                    StructuralLevel(
                        level_type="RESISTANCE",
                        price=round(self.pdh, 2),
                        ts_created=bar.ts_start,
                        prominence=2.5,
                        bar_idx=0,
                        seq_idx=0,
                        touch_count=2,
                    )
                )
            if self.pdl > 0:
                self.session_levels.append(
                    StructuralLevel(
                        level_type="SUPPORT",
                        price=round(self.pdl, 2),
                        ts_created=bar.ts_start,
                        prominence=2.5,
                        bar_idx=0,
                        seq_idx=0,
                        touch_count=2,
                    )
                )

        atr = self.current_atr_1m if self.current_atr_1m > 0 else (bar.close * 0.001)

        # 1. Unconditional Break Invalidation for completed historical bars
        # Any level formed before cand_hist that cand_hist broke through is marked is_broken = True
        if len(self.bars_1m) >= 2:
            cand_hist = self.bars_1m[-2]
            for lvl in self.session_levels:
                if not lvl.is_broken and lvl.ts_created < cand_hist.ts_start:
                    if lvl.level_type == "SUPPORT" and cand_hist.close < (lvl.price - 0.20 * atr):
                        lvl.is_broken = True
                        lvl.ts_broken = cand_hist.ts_end
                    elif lvl.level_type == "RESISTANCE" and cand_hist.close > (lvl.price + 0.20 * atr):
                        lvl.is_broken = True
                        lvl.ts_broken = cand_hist.ts_end

        # 2. Dynamic Post-Excursion Prominence Update for active unbroken levels
        for lvl in self.session_levels:
            if not lvl.is_broken:
                if lvl.level_type == "SUPPORT":
                    excursion = (bar.high - lvl.price) / max(atr, 1e-4)
                    if excursion > lvl.prominence:
                        lvl.prominence = round(excursion, 2)
                elif lvl.level_type == "RESISTANCE":
                    excursion = (lvl.price - bar.low) / max(atr, 1e-4)
                    if excursion > lvl.prominence:
                        lvl.prominence = round(excursion, 2)

        if len(self.bars_1m) < 3:
            return

        cand = self.bars_1m[-2]
        prev = self.bars_1m[-3]
        curr = bar
        tol = max(0.25 * atr, cand.high * 0.0003)

        # Multi-bar swing depth calculation (looking back up to 5 bars before cand)
        window_left = list(self.bars_1m)[-7:-2] if len(self.bars_1m) >= 7 else list(self.bars_1m)[:-2]
        ref_lows = [b.low for b in window_left] + [curr.low]
        ref_highs = [b.high for b in window_left] + [curr.high]
        depth_high = cand.high - (min(ref_lows) if ref_lows else min(prev.low, curr.low))
        depth_low = (max(ref_highs) if ref_highs else max(prev.high, curr.high)) - cand.low

        # ─── Anchor Volume & Order Flow Footprint ───
        cand_vol = float(getattr(cand, "volume", 0.0) or 0.0)
        cand_cb = float(getattr(cand, "call_buy_vol", 0.0) or 0.0)
        cand_cs = float(getattr(cand, "call_sell_vol", 0.0) or 0.0)
        cand_pb = float(getattr(cand, "put_buy_vol", 0.0) or 0.0)
        cand_ps = float(getattr(cand, "put_sell_vol", 0.0) or 0.0)
        cand_delta = float(getattr(cand, "opt_delta_flow", 0.0) or (cand_cb - cand_pb))
        cand_opt_vol = float(getattr(cand, "opt_total_vol", 0.0) or (cand_cb + cand_cs + cand_pb + cand_ps))
        total_calls = cand_cb + cand_cs
        total_puts = cand_pb + cand_ps
        cand_pcr = float(getattr(cand, "opt_pcr", 1.0) or 1.0)
        if cand_pcr <= 0 and total_calls > 0:
            cand_pcr = total_puts / total_calls

        # 60m Median Volume for Scale-Invariant Volume Ratio (VR)
        vols_60 = [float(getattr(b, "volume", 0.0) or 0.0) for b in list(self.bars_1m)[-60:]]
        if vols_60:
            s_v = sorted(vols_60)
            med_vol = float(s_v[len(s_v)//2])
        else:
            med_vol = 1.0
        vr = cand_vol / max(med_vol, 1.0) if med_vol > 0 else 1.0
        vol_ratio_boost = 0.30 * min(2.5, max(0.0, vr - 1.0))

        # Check if prev bar was a freak 09:15 opening settlement spike
        ref_atr = atr if atr > 0 else (cand.high * 0.001)
        is_prev_freak_open = (
            (prev.high - prev.low <= 5.0)
            and (abs(prev.high - cand.high) > (1.5 * ref_atr) or abs(prev.low - cand.low) > (1.5 * ref_atr))
        )
        is_swing_high = (cand.high >= prev.high and cand.high > curr.high) or (is_prev_freak_open and cand.high > curr.high)
        is_swing_low = (cand.low <= prev.low and cand.low < curr.low) or (is_prev_freak_open and cand.low < curr.low)

        # 3. Swing High / Resistance Peak Detection
        if is_swing_high:
            depth = depth_high
            if depth >= 0.15 * atr:
                matched = False
                for lvl in self.session_levels:
                    if lvl.level_type == "RESISTANCE" and not lvl.is_broken and abs(lvl.price - cand.high) <= tol:
                        lvl.touch_count += 1
                        lvl.volume += cand_vol
                        lvl.call_buy_vol += cand_cb
                        lvl.call_sell_vol += cand_cs
                        lvl.put_buy_vol += cand_pb
                        lvl.put_sell_vol += cand_ps
                        lvl.opt_total_vol += cand_opt_vol
                        lvl.opt_delta_flow = round(lvl.call_buy_vol - lvl.put_buy_vol, 1)
                        lvl.net_delta_flow = lvl.opt_delta_flow
                        tot_c = lvl.call_buy_vol + lvl.call_sell_vol
                        tot_p = lvl.put_buy_vol + lvl.put_sell_vol
                        lvl.opt_pcr = round(tot_p / max(tot_c, 1.0), 2)
                        lvl.pcr_at_level = lvl.opt_pcr
                        lvl.vol_ratio_at_level = round(lvl.volume / max(med_vol, 1.0), 2)
                        retest_boost = 0.35 + (0.15 if vr >= 1.20 else 0.0)
                        lvl.prominence = round(lvl.prominence + retest_boost, 2)
                        lvl.price = max(lvl.price, cand.high)
                        matched = True
                        break
                if not matched:
                    # Institutional Resistance OFI boost: Call writing or negative delta absorption
                    res_ofi_boost = 0.20 if (cand_opt_vol > 0 and (cand_cs > cand_cb or cand_delta < 0)) else 0.0
                    total_prom = round((depth / max(atr, 1e-4)) + vol_ratio_boost + res_ofi_boost, 2)
                    self.session_levels.append(
                        StructuralLevel(
                            level_type="RESISTANCE",
                            price=round(cand.high, 2),
                            ts_created=cand.ts_start,
                            prominence=total_prom,
                            bar_idx=len(self.bars_1m) - 2,
                            seq_idx=max(0, self._total_bars_seen - 2),
                            volume=cand_vol,
                            call_buy_vol=cand_cb,
                            call_sell_vol=cand_cs,
                            put_buy_vol=cand_pb,
                            put_sell_vol=cand_ps,
                            opt_delta_flow=cand_delta,
                            opt_total_vol=cand_opt_vol,
                            opt_pcr=round(cand_pcr, 2),
                            vr_anchor=round(vr, 2),
                            net_delta_flow=cand_delta,
                            pcr_at_level=round(cand_pcr, 2),
                            vol_ratio_at_level=round(vr, 2),
                        )
                    )

        # 4. Swing Low / Support Trough Detection
        if is_swing_low:
            depth = depth_low
            if depth >= 0.15 * atr:
                matched = False
                for lvl in self.session_levels:
                    if lvl.level_type == "SUPPORT" and not lvl.is_broken and abs(lvl.price - cand.low) <= tol:
                        lvl.touch_count += 1
                        lvl.volume += cand_vol
                        lvl.call_buy_vol += cand_cb
                        lvl.call_sell_vol += cand_cs
                        lvl.put_buy_vol += cand_pb
                        lvl.put_sell_vol += cand_ps
                        lvl.opt_total_vol += cand_opt_vol
                        lvl.opt_delta_flow = round(lvl.call_buy_vol - lvl.put_buy_vol, 1)
                        lvl.net_delta_flow = lvl.opt_delta_flow
                        tot_c = lvl.call_buy_vol + lvl.call_sell_vol
                        tot_p = lvl.put_buy_vol + lvl.put_sell_vol
                        lvl.opt_pcr = round(tot_p / max(tot_c, 1.0), 2)
                        lvl.pcr_at_level = lvl.opt_pcr
                        lvl.vol_ratio_at_level = round(lvl.volume / max(med_vol, 1.0), 2)
                        retest_boost = 0.35 + (0.15 if vr >= 1.20 else 0.0)
                        lvl.prominence = round(lvl.prominence + retest_boost, 2)
                        lvl.price = min(lvl.price, cand.low)
                        matched = True
                        break
                if not matched:
                    # Institutional Support OFI boost: Put writing or positive delta absorption
                    sup_ofi_boost = 0.20 if (cand_opt_vol > 0 and (cand_ps > cand_pb or cand_delta > 0)) else 0.0
                    total_prom = round((depth / max(atr, 1e-4)) + vol_ratio_boost + sup_ofi_boost, 2)
                    self.session_levels.append(
                        StructuralLevel(
                            level_type="SUPPORT",
                            price=round(cand.low, 2),
                            ts_created=cand.ts_start,
                            prominence=total_prom,
                            bar_idx=len(self.bars_1m) - 2,
                            seq_idx=max(0, self._total_bars_seen - 2),
                            volume=cand_vol,
                            call_buy_vol=cand_cb,
                            call_sell_vol=cand_cs,
                            put_buy_vol=cand_pb,
                            put_sell_vol=cand_ps,
                            opt_delta_flow=cand_delta,
                            opt_total_vol=cand_opt_vol,
                            opt_pcr=round(cand_pcr, 2),
                            vr_anchor=round(vr, 2),
                            net_delta_flow=cand_delta,
                            pcr_at_level=round(cand_pcr, 2),
                            vol_ratio_at_level=round(vr, 2),
                        )
                    )

    def get_active_sr_registry_snapshot(self) -> List[Dict[str, Any]]:
        """Returns active and recently broken S/R levels for telemetry and dashboard rendering."""
        snapshot = []
        for lvl in self.session_levels:
            snapshot.append({
                "type": lvl.level_type,
                "price": lvl.price,
                "prominence": lvl.prominence,
                "touch_count": lvl.touch_count,
                "is_broken": lvl.is_broken,
                "ts_created": lvl.ts_created,
                "ts_broken": lvl.ts_broken,
                "seq_idx": lvl.seq_idx,
                "volume": round(lvl.volume, 1),
                "vr_anchor": round(lvl.vr_anchor, 2),
                "call_buy_vol": round(lvl.call_buy_vol, 1),
                "call_sell_vol": round(lvl.call_sell_vol, 1),
                "put_buy_vol": round(lvl.put_buy_vol, 1),
                "put_sell_vol": round(lvl.put_sell_vol, 1),
                "opt_delta_flow": round(lvl.opt_delta_flow, 1),
                "opt_pcr": round(lvl.opt_pcr, 2),
                "net_delta_flow": round(lvl.net_delta_flow, 1),
                "pcr_at_level": round(lvl.pcr_at_level, 2),
                "vol_ratio_at_level": round(lvl.vol_ratio_at_level, 2),
            })
        return snapshot

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

    def get_live_200_ema_and_slope(self) -> Tuple[float, float]:
        """
        Computes dynamic 200-EMA and slope on the active in-flight tick price.
        Returns:
            (live_ema_200, live_slope_200_bps)
        """
        ref = self.last_price if self.last_price > 0 else 1.0
        k200 = 2.0 / (200.0 + 1.0)
        if self.ema_200_1m_prev > 0 and self.last_price > 0:
            live_ema_200 = (self.last_price * k200) + (self.ema_200_1m_prev * (1.0 - k200))
            live_slope_200 = live_ema_200 - self.ema_200_1m_prev
        else:
            live_ema_200 = self.ema_200_1m if self.ema_200_1m > 0 else self.last_price
            live_slope_200 = self.ema_200_1m_slope
        live_slope_200_bps = (live_slope_200 / ref) * 10000.0
        return live_ema_200, live_slope_200_bps

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
        logger.info(f"[PriceActionEngine] Seeded {count} historical bars. Live 20 EMA: {self.ema_20_1m:.1f}, 50 EMA: {self.ema_50_1m:.1f}, 200 EMA: {self.ema_200_1m:.1f}")
        return count

    def seed_daily_levels(self, pdh: float, pdl: float, pdc: float = 0.0) -> None:
        """Seeds Previous Day High (PDH), Previous Day Low (PDL), and Previous Day Close (PDC)."""
        if pdh > 0:
            self.pdh = float(pdh)
            # Register PDH as macro RESISTANCE anchor in S/R registry
            if not any(l.level_type == "RESISTANCE" and abs(l.price - self.pdh) < 2.0 for l in self.session_levels):
                self.session_levels.append(
                    StructuralLevel(
                        level_type="RESISTANCE",
                        price=round(self.pdh, 2),
                        ts_created=0.0,
                        prominence=2.5,
                        bar_idx=0,
                        seq_idx=0,
                        touch_count=2,
                    )
                )
        if pdl > 0:
            self.pdl = float(pdl)
            # Register PDL as macro SUPPORT anchor in S/R registry
            if not any(l.level_type == "SUPPORT" and abs(l.price - self.pdl) < 2.0 for l in self.session_levels):
                self.session_levels.append(
                    StructuralLevel(
                        level_type="SUPPORT",
                        price=round(self.pdl, 2),
                        ts_created=0.0,
                        prominence=2.5,
                        bar_idx=0,
                        seq_idx=0,
                        touch_count=2,
                    )
                )
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
        virtual_bar: Optional[OHLCBar] = None,
    ) -> DowStructureChain:
        """
        Strict Classical Dow Theory Validator for Pillar 1:
          - CALL requires: HH1 -> HL1 (1st touch, skipped) -> HH2 (HH2 > HH1) -> 4..12 bar orderly pullback -> HL2 (HL2 > HL1).
          - PUT requires:  LL1 -> LH1 (1st touch, skipped) -> LL2 (LL2 < LL1) -> 4..12 bar orderly pullback -> LH2 (LH2 < LH1).
          - Zero-Tolerance Anti-Breakout Veto: Price must never close OR open across
            the opposite side of the 20 EMA Band (a candle piercing from below/above
            is a breakout, never a pullback rejection).
        Args:
            virtual_bar: Optional in-flight forming bar (Mode A Pre-Close). When provided,
            it is appended as the LAST bar (the touch2 candidate) so the Dow HL2/LH2
            chain is judged on the live candle instead of the previous closed bar.
        """
        chain = DowStructureChain()
        n = min(lookback, len(self.bars_1m))
        if n < 12:
            chain.veto_reason = "insufficient_bars_for_dow_structure"
            return chain

        bars = list(self.bars_1m)[-n:]
        e_close, e_high, e_low = self.get_20_ema_band_arrays(n)
        if virtual_bar is not None:
            bars.append(virtual_bar)
            e_close.append(self.ema_20_1m if getattr(self, "ema_20_1m", 0.0) > 0 else (e_close[-1] if e_close else 0.0))
            e_high.append(self.ema_20_high_1m if getattr(self, "ema_20_high_1m", 0.0) > 0 else (e_high[-1] if e_high else 0.0))
            e_low.append(self.ema_20_low_1m if getattr(self, "ema_20_low_1m", 0.0) > 0 else (e_low[-1] if e_low else 0.0))
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
            # 1. Zero-Tolerance Anti-Breakout Veto (last 10 bars):
            #    A candle that OPENS below the band and closes back above is a
            #    breakout-from-below pierce, not an orderly above-band pullback.
            recent_start = max(0, idx_t2 - 10)
            for j in range(recent_start, idx_t2 + 1):
                if bars[j].close < (e_low[j] - strict_floor_tol) or bars[j].open < (e_low[j] - strict_floor_tol):
                    chain.veto_reason = "breakout_from_below_veto"
                    return chain

            hl2_price = min(bars[idx_t2].low, bars[-1].low)

            # Continuous uptrend start from idx_t2
            trend_start = idx_t2
            for j in range(idx_t2, -1, -1):
                if bars[j].close < (e_low[j] - strict_floor_tol) or bars[j].open < (e_low[j] - strict_floor_tol):
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
                if bars[j].close < (e_low[j] - strict_floor_tol) or bars[j].open < (e_low[j] - strict_floor_tol):
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

            if imp_directional_flow > 0 and pb_counter_flow > 1.15 * imp_directional_flow:
                chain.veto_reason = "wyckoff_pullback_counter_flow_too_heavy"
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
            # 1. Zero-Tolerance Anti-Breakdown Veto (last 10 bars):
            #    A candle that OPENS above the band and closes back below is a
            #    breakdown-from-above pierce, not an orderly below-band pullback.
            recent_start = max(0, idx_t2 - 10)
            for j in range(recent_start, idx_t2 + 1):
                if bars[j].close > (e_high[j] + strict_roof_tol) or bars[j].open > (e_high[j] + strict_roof_tol):
                    chain.veto_reason = "breakdown_from_above_veto"
                    return chain

            lh2_price = max(bars[idx_t2].high, bars[-1].high)

            # Continuous downtrend start from idx_t2
            trend_start = idx_t2
            for j in range(idx_t2, -1, -1):
                if bars[j].close > (e_high[j] + strict_roof_tol) or bars[j].open > (e_high[j] + strict_roof_tol):
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
                if bars[j].close > (e_high[j] + strict_roof_tol) or bars[j].open > (e_high[j] + strict_roof_tol):
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

            if imp_directional_flow > 0 and pb_counter_flow > 1.15 * imp_directional_flow:
                chain.veto_reason = "wyckoff_pullback_counter_flow_too_heavy"
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

        # Filter out 09:15 freak pre-open settlement tick when calculating day extremes
        ref_atr = self.current_atr_1m if self.current_atr_1m > 0 else (current_price * 0.001)
        valid_prior_bars = []
        for idx, b in enumerate(intraday_prior_bars):
            if idx == 0 and len(intraday_prior_bars) > 1:
                next_b = intraday_prior_bars[1]
                is_freak = (
                    (b.high - b.low <= 5.0)
                    and (abs(b.high - next_b.high) > (1.5 * ref_atr) or abs(b.low - next_b.low) > (1.5 * ref_atr))
                )
                if is_freak:
                    continue
            valid_prior_bars.append(b)

        bars_for_extremes = valid_prior_bars if valid_prior_bars else intraday_prior_bars
        day_high = max(b.high for b in bars_for_extremes)
        day_low = min(b.low for b in bars_for_extremes)
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

        # 1m Structural Horizontal Pivots / Multi-Touch Clusters
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

    def _evaluate_wyckoff_pillar_flow(
        self,
        pillar: int,
        direction: str,
        bar: Optional[OHLCBar] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Regime-Aware, Pillar-Specific Wyckoff & Institutional Option Flow Delta Gate.
        
        Evaluates Order Flow Footprint (Call Buy, Call Sell, Put Buy, Put Sell, Net Delta)
        specifically tailored to each Pillar's market phase.

        CRITICAL BACKWARD-COMPATIBILITY:
        Whenever bar.opt_total_vol <= 0 (synthetic bars / offline tests without option ticks),
        neutral bypass is granted so tests without option data pass cleanly.
        """
        if bar is None:
            return True, "no_bar_bypass", {}

        extra = extra or {}
        vols_60 = [float(getattr(b, "volume", 0.0) or 0.0) for b in list(self.bars_1m)[-60:]]
        if vols_60:
            s_v = sorted(vols_60)
            med_vol = float(s_v[len(s_v)//2])
        else:
            med_vol = 1.0
        bar_vol = float(getattr(bar, "volume", 0.0) or 0.0)
        vr_t = bar_vol / max(med_vol, 1.0) if med_vol > 0 else 1.0

        opt_vol = float(getattr(bar, "opt_total_vol", 0.0) or 0.0)
        if opt_vol <= 0.0:
            # Synthetic / test bar bypass
            return True, "opt_flow_synthetic_bypass", {"vr_t": round(vr_t, 2)}

        cb = float(getattr(bar, "call_buy_vol", 0.0) or 0.0)
        cs = float(getattr(bar, "call_sell_vol", 0.0) or 0.0)
        pb = float(getattr(bar, "put_buy_vol", 0.0) or 0.0)
        ps = float(getattr(bar, "put_sell_vol", 0.0) or 0.0)

        bull_flow = cb + ps
        bear_flow = pb + cs
        net_inst_delta = bull_flow - bear_flow
        bull_ratio = bull_flow / max(bear_flow, 1.0)
        bear_ratio = bear_flow / max(bull_flow, 1.0)
        is_call = direction.upper() in ("UP", "CALL")

        flow_metrics = {
            "bull_flow": round(bull_flow, 1),
            "bear_flow": round(bear_flow, 1),
            "net_inst_delta": round(net_inst_delta, 1),
            "bull_ratio": round(bull_ratio, 2),
            "bear_ratio": round(bear_ratio, 2),
            "vr_t": round(vr_t, 2),
            "call_buy_vol": round(cb, 1),
            "call_sell_vol": round(cs, 1),
            "put_buy_vol": round(pb, 1),
            "put_sell_vol": round(ps, 1),
        }

        # ─── PILLAR 1: 20 EMA Rejection (Wyckoff Pullback & Re-accumulation/Distribution) ───
        if pillar == 1:
            is_wick_rejection = bool(extra.get("is_wick_rejection", False))
            if is_call:
                req_ratio = 0.85 if is_wick_rejection else 1.05
                if bull_ratio < req_ratio:
                    return False, "wyckoff_p1_bearish_delta_divergence", {
                        **flow_metrics,
                        "required_bull_ratio": req_ratio,
                        "is_wick_rejection": is_wick_rejection,
                    }
            else:
                req_ratio = 0.85 if is_wick_rejection else 1.05
                if bear_ratio < req_ratio:
                    return False, "wyckoff_p1_bullish_delta_divergence", {
                        **flow_metrics,
                        "required_bear_ratio": req_ratio,
                        "is_wick_rejection": is_wick_rejection,
                    }

        # ─── PILLAR 2: Micro-Sweep Reclaim (Wyckoff Spring / Upthrust Absorption) ───
        elif pillar == 2:
            if is_call:
                # CALL Spring: Veto if heavy opposing supply (bear_ratio >= 1.15) OR high volume with weak buyers & low put writing
                put_sell_pct = ps / max(opt_vol, 1.0)
                if bear_ratio >= 1.15 or (vr_t >= 1.20 and bull_ratio < 0.95 and put_sell_pct < 0.30):
                    return False, "wyckoff_p2_failed_spring_heavy_supply", {
                        **flow_metrics,
                        "put_sell_pct": round(put_sell_pct, 3),
                        "verdict": "FAILED_SPRING_SUPPLY_OVERWHELMED",
                    }
            else:
                # PUT Upthrust: Veto if heavy opposing demand (bull_ratio >= 1.15) OR high volume with weak sellers & low call writing
                call_sell_pct = cs / max(opt_vol, 1.0)
                if bull_ratio >= 1.15 or (vr_t >= 1.20 and bear_ratio < 0.95 and call_sell_pct < 0.30):
                    return False, "wyckoff_p2_failed_upthrust_heavy_demand", {
                        **flow_metrics,
                        "call_sell_pct": round(call_sell_pct, 3),
                        "verdict": "FAILED_UPTHRUST_DEMAND_OVERWHELMED",
                    }

        # ─── PILLAR 3: Volatility Squeeze Breakout (Wyckoff Initiative SOS / SOW) ───
        elif pillar == 3:
            if is_call:
                if bull_ratio < 1.15 or cb < pb:
                    return False, "wyckoff_p3_call_negative_delta_trap", {
                        **flow_metrics,
                        "required_bull_ratio": 1.15,
                        "verdict": "BULL_TRAP_NEGATIVE_DELTA",
                    }
            else:
                if bear_ratio < 1.15 or pb < cb:
                    return False, "wyckoff_p3_put_positive_delta_trap", {
                        **flow_metrics,
                        "required_bear_ratio": 1.15,
                        "verdict": "BEAR_TRAP_POSITIVE_DELTA",
                    }

        # ─── PILLAR 4: Prominence & S/R Registry Breakout (Wyckoff Effort vs Result & Stopping Volume) ───
        elif pillar == 4:
            if is_call:
                # CALL Breakout requires genuine BullFlow dominance (bull_ratio >= 1.18)
                if bear_flow > bull_flow or bull_ratio < 1.18:
                    return False, "wyckoff_p4_stopping_volume_bearish_supply", {
                        **flow_metrics,
                        "required_bull_ratio": 1.18,
                        "verdict": "STOPPING_VOLUME_BEARISH_SUPPLY_ABSORPTION",
                    }
            else:
                # PUT Breakdown requires genuine BearFlow dominance (bear_ratio >= 1.18)
                # If bull_flow > bear_flow (e.g. Put Writing dominance +1.29M like 21 Oct 09:56), veto immediately!
                if bull_flow > bear_flow or bear_ratio < 1.18:
                    return False, "wyckoff_p4_stopping_volume_bullish_absorption", {
                        **flow_metrics,
                        "required_bear_ratio": 1.18,
                        "verdict": "STOPPING_VOLUME_BULLISH_PUT_WRITING_ABSORPTION",
                    }

        return True, "wyckoff_flow_confirmed", flow_metrics

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

            # Breakout-from-Below Open Veto: a rejection candle must OPEN at/above the
            # lower band edge (inside the band or on the correct side). A candle that
            # opens below the band and pierces up through it is a breakout, never a
            # 20 EMA pullback rejection.
            if bar.open < (band_low - 0.5 * tol_pts):
                return False, "breakout_from_below_open_veto", {
                    "bar_open": round(bar.open, 2),
                    "band_low": round(band_low, 2),
                    "tol_pts": round(tol_pts, 2),
                }

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

            # Wyckoff Institutional Option Flow Delta Gate
            ok_wflow, r_wflow, det_wflow = self._evaluate_wyckoff_pillar_flow(
                pillar=1,
                direction="CALL",
                bar=bar,
                extra={"is_wick_rejection": is_hammer},
            )
            if not ok_wflow:
                return False, r_wflow, det_wflow

            sl_price = round(min(bar.low, dow.touch_2) - buffer_pts, 2)
            cb_b = float(getattr(bar, "call_buy_vol", 0.0) or 0.0)
            cs_b = float(getattr(bar, "call_sell_vol", 0.0) or 0.0)
            pb_b = float(getattr(bar, "put_buy_vol", 0.0) or 0.0)
            ps_b = float(getattr(bar, "put_sell_vol", 0.0) or 0.0)
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
                "n1_footprint": {
                    "n1_vol": round(float(getattr(bar, "volume", 0.0) or 0.0), 1),
                    "n1_net_delta": round((cb_b + ps_b) - (pb_b + cs_b), 1),
                    "n1_bulls": round(cb_b + ps_b, 1),
                    "n1_bears": round(pb_b + cs_b, 1),
                },
            }
            details.update(det_wflow)
            return True, f"20_ema_call_hl2_rejection ({sig_type})", details

        # ═══════════════════════════════════════════════════════════
        # PUT EVALUATION (LH2 Red Rejection from 20 EMA Band)
        # ═══════════════════════════════════════════════════════════
        elif direction.upper() in ("DOWN", "PUT"):
            if current_vwap > 0 and bar.close >= current_vwap:
                return False, "above_vwap", {}
            if live_slope_20_bps > 0.10:
                return False, "20_ema_slope_bullish", {}

            # Breakdown-from-Above Open Veto: a rejection candle must OPEN at/below the
            # upper band edge (inside the band or on the correct side). A candle that
            # opens above the band and pierces down through it is a breakdown, never a
            # 20 EMA pullback rejection.
            if bar.open > (band_high + 0.5 * tol_pts):
                return False, "breakdown_from_above_open_veto", {
                    "bar_open": round(bar.open, 2),
                    "band_high": round(band_high, 2),
                    "tol_pts": round(tol_pts, 2),
                }

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

            # Wyckoff Institutional Option Flow Delta Gate
            ok_wflow, r_wflow, det_wflow = self._evaluate_wyckoff_pillar_flow(
                pillar=1,
                direction="PUT",
                bar=bar,
                extra={"is_wick_rejection": is_shooting_star},
            )
            if not ok_wflow:
                return False, r_wflow, det_wflow

            sl_price = round(max(bar.high, dow.touch_2) + buffer_pts, 2)
            cb_b = float(getattr(bar, "call_buy_vol", 0.0) or 0.0)
            cs_b = float(getattr(bar, "call_sell_vol", 0.0) or 0.0)
            pb_b = float(getattr(bar, "put_buy_vol", 0.0) or 0.0)
            ps_b = float(getattr(bar, "put_sell_vol", 0.0) or 0.0)
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
                "n1_footprint": {
                    "n1_vol": round(float(getattr(bar, "volume", 0.0) or 0.0), 1),
                    "n1_net_delta": round((cb_b + ps_b) - (pb_b + cs_b), 1),
                    "n1_bulls": round(cb_b + ps_b, 1),
                    "n1_bears": round(pb_b + cs_b, 1),
                },
            }
            details.update(det_wflow)
            return True, f"20_ema_put_lh2_rejection ({sig_type})", details

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

                # Wyckoff Institutional Option Flow Delta Gate (Spring Absorption)
                ok_wflow, r_wflow, det_wflow = self._evaluate_wyckoff_pillar_flow(
                    pillar=2,
                    direction="CALL",
                    bar=bar,
                )
                if not ok_wflow:
                    return False, r_wflow, det_wflow

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
                details.update(det_wflow)
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

                # Wyckoff Institutional Option Flow Delta Gate (Upthrust Absorption)
                ok_wflow, r_wflow, det_wflow = self._evaluate_wyckoff_pillar_flow(
                    pillar=2,
                    direction="PUT",
                    bar=bar,
                )
                if not ok_wflow:
                    return False, r_wflow, det_wflow

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
                details.update(det_wflow)
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

        # Wyckoff Box Volume Expansion Gate
        box_vols = [float(getattr(b, "volume", 0.0) or 0.0) for b in recent_bars]
        box_avg_vol = (sum(box_vols) / len(box_vols)) if box_vols else 1.0
        vols_60 = [float(getattr(b, "volume", 0.0) or 0.0) for b in list(self.bars_1m)[-60:]]
        if vols_60:
            s_v = sorted(vols_60)
            med_vol = float(s_v[len(s_v)//2])
        else:
            med_vol = 1.0
        vr_t = float(bar.volume) / med_vol if med_vol > 0 else 1.0
        if bar.volume > 10.0 and box_avg_vol > 10.0:
            if not (bar.volume >= 1.15 * box_avg_vol or vr_t >= 1.15):
                return False, "wyckoff_p3_low_volume_squeeze_breakout", {
                    "bar_volume": round(bar.volume, 1),
                    "box_avg_vol": round(box_avg_vol, 1),
                    "vr_t": round(vr_t, 2),
                }

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
                # Wyckoff Institutional Option Flow Delta Gate (Initiative SOW)
                ok_wflow, r_wflow, det_wflow = self._evaluate_wyckoff_pillar_flow(
                    pillar=3,
                    direction="PUT",
                    bar=bar,
                )
                if not ok_wflow:
                    return False, r_wflow, det_wflow

                # Climax Exhaustion Cap: Breakout candle cannot be an overextended blowout bar
                if bar_range > (1.35 * atr):
                    return False, "exhausted_squeeze_breakout_candle_range", {
                        "bar_range": round(bar_range, 2),
                        "atr": round(atr, 2),
                        "max_allowed": round(1.35 * atr, 2),
                    }

                # Box Displacement Cap: Cannot enter if already overextended beyond the squeeze box
                if (box_low - bar.close) > (1.20 * atr):
                    return False, "overextended_from_squeeze_box", {
                        "displacement": round(box_low - bar.close, 2),
                        "max_allowed": round(1.20 * atr, 2),
                        "atr": round(atr, 2),
                    }

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
                details.update(det_wflow)
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
                # Wyckoff Institutional Option Flow Delta Gate (Initiative SOS)
                ok_wflow, r_wflow, det_wflow = self._evaluate_wyckoff_pillar_flow(
                    pillar=3,
                    direction="CALL",
                    bar=bar,
                )
                if not ok_wflow:
                    return False, r_wflow, det_wflow

                # Climax Exhaustion Cap: Breakout candle cannot be an overextended blowout bar
                if bar_range > (1.35 * atr):
                    return False, "exhausted_squeeze_breakout_candle_range", {
                        "bar_range": round(bar_range, 2),
                        "atr": round(atr, 2),
                        "max_allowed": round(1.35 * atr, 2),
                    }

                # Box Displacement Cap: Cannot enter if already overextended beyond the squeeze box
                if (bar.close - box_high) > (1.20 * atr):
                    return False, "overextended_from_squeeze_box", {
                        "displacement": round(bar.close - box_high, 2),
                        "max_allowed": round(1.20 * atr, 2),
                        "atr": round(atr, 2),
                    }

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
                details.update(det_wflow)
                return True, "volatility_squeeze_upside_breakout", details
            return False, "no_upside_breakout", {}

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

        is_bullish = direction.upper() in ("UP", "CALL")
        is_bearish = direction.upper() in ("DOWN", "PUT")
        ref_spot = self.last_price if self.last_price > 0 else bar.close
        buffer_pts = max(5.0, (ref_spot / 24000.0) * 5.0)
        _atr_ref = self.current_atr_1m if self.current_atr_1m > 0 else (ref_spot * 0.001)
        min_hub_clearance = max(2.0, (ref_spot / 24000.0) * 2.0, 0.08 * _atr_ref)

        # 5m Structural Alignment Gate
        trend_5m_bullish = True
        trend_5m_bearish = True
        if self.bars_5m:
            bar_5m = self.bars_5m[-1]
            trend_5m_bullish = bar_5m.close >= bar_5m.open
            trend_5m_bearish = bar_5m.close <= bar_5m.open

        # Volume Gate: Volume Ratio VR_t vs 60m median volume
        vols_60 = [float(getattr(b, "volume", 0.0) or 0.0) for b in list(self.bars_1m)[-60:]]
        if vols_60:
            s_v = sorted(vols_60)
            med_vol = float(s_v[len(s_v)//2])
        else:
            med_vol = 1.0
        curr_vol = float(getattr(bar, "volume", 0.0) or 0.0)
        vr_t = curr_vol / med_vol if med_vol > 0 else 1.0

        total_range = max(bar.high - bar.low, 0.1)
        body = abs(bar.close - bar.open)
        body_ratio = body / total_range
        upper_wick = bar.high - max(bar.open, bar.close)
        lower_wick = min(bar.open, bar.close) - bar.low
        upper_wick_ratio = upper_wick / total_range
        lower_wick_ratio = lower_wick / total_range
        close_edge_call = (bar.close - bar.low) / total_range
        close_edge_put = (bar.high - bar.close) / total_range

        # ─── Priority 1: Dynamic Session S/R Level Registry Breakout ───
        if is_bearish and trend_5m_bearish:
            cand_supports = [
                lvl for lvl in self.session_levels
                if lvl.level_type == "SUPPORT"
                and not lvl.is_broken
                and lvl.prominence >= 1.25
                and ((self._total_bars_seen - lvl.seq_idx) >= 3 or (bar.ts_start - lvl.ts_created) >= 180.0)
            ]
            cand_supports.sort(key=lambda lvl: (lvl.touch_count * 0.5 + lvl.prominence), reverse=True)
            for s_lvl in cand_supports:
                displacement = s_lvl.price - bar.close
                bar_range = bar.high - bar.low
                origin_dist = abs(bar.open - s_lvl.price)
                if (
                    bar.close <= (s_lvl.price - min_hub_clearance)
                    and origin_dist <= (0.35 * _atr_ref)
                    and bar.close < bar.open
                    and body_ratio >= 0.35
                    and close_edge_put >= 0.68
                    and lower_wick_ratio <= 0.18
                    and bar_range <= (1.35 * _atr_ref)
                    and displacement <= (1.25 * _atr_ref)
                    and vr_t >= 1.20
                ):
                    if current_vwap > 0 and bar.close > current_vwap:
                        continue

                    # Macro Runway Guard (Day Extreme Clearance + ignore self-broken level)
                    ok_struct, r_struct, det_struct = self._check_structural_obstacle(
                        "PUT", bar.close, ignore_pivots_above=(s_lvl.price - 0.10 * _atr_ref), check_day_extremes=True
                    )
                    if not ok_struct:
                        return False, f"structural_obstacle_{r_struct}", det_struct

                    s_lvl.is_broken = True
                    s_lvl.ts_broken = bar.ts_end
                    sl_price = round(s_lvl.price + buffer_pts, 2)
                    age_bars = (self._total_bars_seen - s_lvl.seq_idx) if self._total_bars_seen >= s_lvl.seq_idx else (len(self.bars_1m) - 1 - s_lvl.bar_idx)

                    # Wyckoff Effort vs Result & Stopping Volume Shield
                    ok_wflow, r_wflow, det_wflow = self._evaluate_wyckoff_pillar_flow(
                        pillar=4,
                        direction="PUT",
                        bar=bar,
                    )
                    if not ok_wflow:
                        return False, r_wflow, det_wflow

                    details = {
                        "pa_setup_name": "topological_prominence_breakout",
                        "pillar": 4,
                        "hub_price": s_lvl.price,
                        "min_hub_clearance": round(min_hub_clearance, 2),
                        "pi_hat": s_lvl.prominence,
                        "vr_t": vr_t,
                        "breakout_direction": "DOWN",
                        "anchor_age_bars": age_bars,
                        "anchor_touch_count": s_lvl.touch_count,
                        "anchor_volume": round(s_lvl.volume, 1),
                        "anchor_vr": round(s_lvl.vr_anchor, 2),
                        "anchor_delta_flow": round(s_lvl.opt_delta_flow, 1),
                        "anchor_pcr": round(s_lvl.opt_pcr, 2),
                        "anchor_call_sell_vol": round(s_lvl.call_sell_vol, 1),
                        "anchor_put_sell_vol": round(s_lvl.put_sell_vol, 1),
                        "level_source": "sr_registry",
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
                    details.update(det_wflow)
                    return True, "prominence_hub_breakout_down", details

        elif is_bullish and trend_5m_bullish:
            cand_resistances = [
                lvl for lvl in self.session_levels
                if lvl.level_type == "RESISTANCE"
                and not lvl.is_broken
                and lvl.prominence >= 1.25
                and ((self._total_bars_seen - lvl.seq_idx) >= 3 or (bar.ts_start - lvl.ts_created) >= 180.0)
            ]
            cand_resistances.sort(key=lambda lvl: (lvl.touch_count * 0.5 + lvl.prominence), reverse=True)
            for r_lvl in cand_resistances:
                displacement = bar.close - r_lvl.price
                bar_range = bar.high - bar.low
                origin_dist = abs(bar.open - r_lvl.price)
                if (
                    bar.close >= (r_lvl.price + min_hub_clearance)
                    and origin_dist <= (0.35 * _atr_ref)
                    and bar.close > bar.open
                    and body_ratio >= 0.35
                    and close_edge_call >= 0.68
                    and upper_wick_ratio <= 0.18
                    and bar_range <= (1.35 * _atr_ref)
                    and displacement <= (1.25 * _atr_ref)
                    and vr_t >= 1.20
                ):
                    if current_vwap > 0 and bar.close < current_vwap:
                        continue

                    ok_struct, r_struct, det_struct = self._check_structural_obstacle(
                        "CALL", bar.close, ignore_pivots_below=(r_lvl.price + 0.10 * _atr_ref), check_day_extremes=True
                    )
                    if not ok_struct:
                        return False, f"structural_obstacle_{r_struct}", det_struct

                    r_lvl.is_broken = True
                    r_lvl.ts_broken = bar.ts_end
                    sl_price = round(r_lvl.price - buffer_pts, 2)
                    age_bars = (self._total_bars_seen - r_lvl.seq_idx) if self._total_bars_seen >= r_lvl.seq_idx else (len(self.bars_1m) - 1 - r_lvl.bar_idx)

                    # Wyckoff Effort vs Result & Stopping Volume Shield
                    ok_wflow, r_wflow, det_wflow = self._evaluate_wyckoff_pillar_flow(
                        pillar=4,
                        direction="CALL",
                        bar=bar,
                    )
                    if not ok_wflow:
                        return False, r_wflow, det_wflow

                    details = {
                        "pa_setup_name": "topological_prominence_breakout",
                        "pillar": 4,
                        "hub_price": r_lvl.price,
                        "min_hub_clearance": round(min_hub_clearance, 2),
                        "pi_hat": r_lvl.prominence,
                        "vr_t": vr_t,
                        "breakout_direction": "UP",
                        "anchor_age_bars": age_bars,
                        "anchor_touch_count": r_lvl.touch_count,
                        "anchor_volume": round(r_lvl.volume, 1),
                        "anchor_vr": round(r_lvl.vr_anchor, 2),
                        "anchor_delta_flow": round(r_lvl.opt_delta_flow, 1),
                        "anchor_pcr": round(r_lvl.opt_pcr, 2),
                        "anchor_call_sell_vol": round(r_lvl.call_sell_vol, 1),
                        "anchor_put_sell_vol": round(r_lvl.put_sell_vol, 1),
                        "level_source": "sr_registry",
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
                    details.update(det_wflow)
                    return True, "prominence_hub_breakout_up", details

        # ─── Priority 2: Fallback to FastMonotonicStack (T-2 Hub) ───
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

        if vr_t < 1.4:
            return False, f"volume_ratio_gate_failed_{vr_t:.2f}", {}

        hub_price = float(prices_window[hub_idx])

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

                # Wyckoff Effort vs Result & Stopping Volume Shield
                ok_wflow, r_wflow, det_wflow = self._evaluate_wyckoff_pillar_flow(
                    pillar=4,
                    direction="CALL",
                    bar=bar,
                )
                if not ok_wflow:
                    return False, r_wflow, det_wflow

                sl_price = round(bar.low - buffer_pts, 2)
                details = {
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
                details.update(det_wflow)
                return True, "prominence_hub_breakout_up", details

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

                # Wyckoff Effort vs Result & Stopping Volume Shield
                ok_wflow, r_wflow, det_wflow = self._evaluate_wyckoff_pillar_flow(
                    pillar=4,
                    direction="PUT",
                    bar=bar,
                )
                if not ok_wflow:
                    return False, r_wflow, det_wflow

                sl_price = round(bar.high + buffer_pts, 2)
                details = {
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
                details.update(det_wflow)
                return True, "prominence_hub_breakout_down", details

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
        cb = float(getattr(b, "call_buy_vol", 0.0) or 0.0)
        cs = float(getattr(b, "call_sell_vol", 0.0) or 0.0)
        pb = float(getattr(b, "put_buy_vol", 0.0) or 0.0)
        ps = float(getattr(b, "put_sell_vol", 0.0) or 0.0)
        opt_vol = float(getattr(b, "opt_total_vol", 0.0) or (cb + cs + pb + ps))
        opt_delta = float(getattr(b, "opt_delta_flow", 0.0) or (cb - pb))
        opt_pcr = float(getattr(b, "opt_pcr", 1.0) or 1.0)
        # In-Flight Forming Bar Patch: option flow is attached to the candle ONLY at
        # _on_1m_bar_close; while the bar is forming, the live numbers live in
        # self._opt_flow_1m. Overlay them so Mode A / Mode B signals never carry
        # zero option flow into conviction scoring and execution telemetry.
        if b is self._curr_1m_bar:
            _flow = self._opt_flow_1m or {}
            _cb = float(_flow.get("call_buy_vol", 0.0) or 0.0)
            _cs = float(_flow.get("call_sell_vol", 0.0) or 0.0)
            _pb = float(_flow.get("put_buy_vol", 0.0) or 0.0)
            _ps = float(_flow.get("put_sell_vol", 0.0) or 0.0)
            if (_cb + _cs + _pb + _ps) > 0:
                cb, cs, pb, ps = _cb, _cs, _pb, _ps
                opt_vol = _cb + _cs + _pb + _ps
                opt_delta = _cb - _pb
                opt_pcr = (_pb + _ps) / max(_cb + _cs, 1.0)
        bull_flow = cb + ps
        bear_flow = pb + cs

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
            "call_buy_vol": round(cb, 1),
            "call_sell_vol": round(cs, 1),
            "put_buy_vol": round(pb, 1),
            "put_sell_vol": round(ps, 1),
            "opt_total_vol": round(opt_vol, 1),
            "opt_delta_flow": round(opt_delta, 1),
            "opt_pcr": round(opt_pcr, 2),
            "bull_flow": round(bull_flow, 1),
            "bear_flow": round(bear_flow, 1),
        }

    def _enrich_details(self, details: Dict[str, Any]) -> Dict[str, Any]:
        """Enriches setup details dict with relative bar metrics and volume SMA."""
        mode_val = str(details.get("mode", "")).upper()
        if mode_val.startswith("MODE_") and self._curr_1m_bar is not None:
            active_bar = self._curr_1m_bar
        else:
            active_bar = (self.bars_1m[-1] if self.bars_1m else None) or self._curr_1m_bar
        bar_m = self.get_relative_bar_metrics(bar=active_bar)
        for k, v in bar_m.items():
            if k not in details:
                details[k] = v
        return details

    def _evaluate_mode_a_arming(self, bar: OHLCBar) -> None:
        """
        Evaluates boundary rejection on latest closed 1m candle and arms Mode A.
        Strictly wired to Pillar 1 (20 EMA Pullback Rejection) & Pillar 2 (Micro-Sweep).
        Never arms on breakouts from below (CALL) or breakouts from above (PUT)!
        """
        if len(self.bars_1m) < 12:
            return
        bar_idx = self._total_bars_seen
        atr = self.current_atr_1m if self.current_atr_1m > 0 else 10.0
        c_range = max(bar.high - bar.low, 0.5)
        lower_wick = min(bar.open, bar.close) - bar.low
        upper_wick = bar.high - max(bar.open, bar.close)
        lower_wick_ratio = lower_wick / c_range
        upper_wick_ratio = upper_wick / c_range

        live_ema_20, live_slope_20_bps = self.get_live_20_ema_and_slope()
        e_close_arr, e_high_arr, e_low_arr = self.get_20_ema_band_arrays(2)
        band_high = e_high_arr[-1]
        band_low = e_low_arr[-1]
        tol_pts = max(1.5, live_ema_20 * (1.5 / 10000.0), 0.12 * atr)

        # ─── 1. Pillar 1: CALL 20 EMA Pullback Rejection Arming ───
        last_p1_up_idx = self._mode_a_last_trigger_bar_idx.get("P1_EMA20_UP", -100)
        if (bar_idx - last_p1_up_idx) >= 3:
            uptrend_ok = (live_slope_20_bps >= -0.10) and (self.ema_20_1m >= self.ema_50_1m or self.ema_50_1m <= 0)
            vwap_ok = (self.current_vwap <= 0 or bar.close > self.current_vwap)
            # Breakout-from-below VETO: Price must have opened at/above band and prior bars must have been above band!
            prior_bars = list(self.bars_1m)[-4:-1]
            came_from_above = (bar.open >= band_low - 0.25 * atr) and any(b.high >= band_high for b in prior_bars)
            # Band Touch
            band_tested = (bar.low <= band_high + tol_pts) and (bar.close >= band_low - 0.25 * atr)
            # Rejection Geometry: Real lower wick (>= 0.20), closing in top half (NO 'or close > open'!)
            rej_geom = (lower_wick_ratio >= 0.20) and (bar.close >= bar.low + 0.40 * c_range)
            dow = self._validate_dow_hl2_lh2_structure("CALL", touch2_offset=0)
            ok_struct, _, _ = self._check_structural_obstacle("CALL", bar.close, check_day_extremes=True)

            if uptrend_ok and vwap_ok and came_from_above and band_tested and rej_geom and dow.is_valid and ok_struct and self._check_wyckoff_harmony("UP", bar):
                self._mode_a_armed_setups["PILLAR_1_EMA_UP"] = {
                    "direction": "UP",
                    "pillar": 1,
                    "pa_setup_name": "20_ema_rejection",
                    "anchor_high": bar.high,
                    "anchor_low": bar.low,
                    "boundary_level": band_high,
                    "armed_bar_idx": bar_idx,
                    "armed_ts": self.last_ts,
                }
                self._mode_a_last_trigger_bar_idx["P1_EMA20_UP"] = bar_idx
                logger.info(f"[PriceActionEngine] [MODE_A_ARMED] Pillar 1 20 EMA CALL Pullback Rejection Armed | AnchorHigh: {bar.high:.2f}")

        # ─── 2. Pillar 1: PUT 20 EMA Pullback Rejection Arming ───
        last_p1_down_idx = self._mode_a_last_trigger_bar_idx.get("P1_EMA20_DOWN", -100)
        if (bar_idx - last_p1_down_idx) >= 3:
            downtrend_ok = (live_slope_20_bps <= 0.10) and (self.ema_20_1m <= self.ema_50_1m or self.ema_50_1m <= 0)
            vwap_ok = (self.current_vwap <= 0 or bar.close < self.current_vwap)
            prior_bars = list(self.bars_1m)[-4:-1]
            came_from_below = (bar.open <= band_high + 0.25 * atr) and any(b.low <= band_low for b in prior_bars)
            band_tested = (bar.high >= band_low - tol_pts) and (bar.close <= band_high + 0.25 * atr)
            rej_geom = (upper_wick_ratio >= 0.20) and (bar.close <= bar.high - 0.40 * c_range)
            dow = self._validate_dow_hl2_lh2_structure("PUT", touch2_offset=0)
            ok_struct, _, _ = self._check_structural_obstacle("PUT", bar.close, check_day_extremes=True)

            if downtrend_ok and vwap_ok and came_from_below and band_tested and rej_geom and dow.is_valid and ok_struct and self._check_wyckoff_harmony("DOWN", bar):
                self._mode_a_armed_setups["PILLAR_1_EMA_DOWN"] = {
                    "direction": "DOWN",
                    "pillar": 1,
                    "pa_setup_name": "20_ema_rejection",
                    "anchor_high": bar.high,
                    "anchor_low": bar.low,
                    "boundary_level": band_low,
                    "armed_bar_idx": bar_idx,
                    "armed_ts": self.last_ts,
                }
                self._mode_a_last_trigger_bar_idx["P1_EMA20_DOWN"] = bar_idx
                logger.info(f"[PriceActionEngine] [MODE_A_ARMED] Pillar 1 20 EMA PUT Pullback Rejection Armed | AnchorLow: {bar.low:.2f}")

        # ─── 3. Pillar 2: CALL Micro-Sweep & Reclaim Arming ───
        last_p2_up_idx = self._mode_a_last_trigger_bar_idx.get("P2_SWEEP_UP", -100)
        if len(self.bars_1m) >= 7 and (bar_idx - last_p2_up_idx) >= 3:
            is_lunch_chop = False
            if bar.ts_start > 1_000_000_000.0:
                try:
                    dt_bar = datetime.fromtimestamp(bar.ts_start, _IST)
                except Exception:
                    dt_bar = None
                if dt_bar is not None:
                    is_lunch_chop = (dt_time(11, 15, 0) <= dt_bar.time() <= dt_time(13, 0, 0))
            if not is_lunch_chop:
                prior_5 = list(self.bars_1m)[-6:-1]
                swept_low = min(b.low for b in prior_5)
                # Parity with check_micro_sweep_reclaim: minimum sweep depth
                ref_spot_2 = self.last_price if self.last_price > 0 else (bar.close if bar.close > 0 else 24000.0)
                min_sweep_depth = max(1.5, (ref_spot_2 / 24000.0) * 1.5, 0.08 * atr)
                swept_and_reclaimed = (bar.low <= (swept_low - min_sweep_depth)) and (bar.close > swept_low)
                sweep_geom = (
                    (lower_wick_ratio >= 0.35)
                    and (upper_wick_ratio <= 0.15)
                    and (lower_wick >= 1.25 * upper_wick)
                    and (bar.close >= bar.low + 0.55 * c_range)
                )
                # Parity with check_micro_sweep_reclaim: Pain Zone VWAP alignment
                # (composite + direction-selected trapped-buyer line) + obstacle shield
                vwap_ok_p2 = self._pain_zone_vwap_aligned("CALL", bar.close)
                if swept_and_reclaimed and sweep_geom and vwap_ok_p2 and self._check_wyckoff_harmony("UP", bar):
                    ok_struct_p2, _, _ = self._check_structural_obstacle("CALL", bar.close)
                    if ok_struct_p2:
                        self._mode_a_armed_setups["PILLAR_2_SWEEP_UP"] = {
                            "direction": "UP",
                            "pillar": 2,
                            "pa_setup_name": "micro_sweep_reclaim",
                            "anchor_high": bar.high,
                            "anchor_low": bar.low,
                            "boundary_level": swept_low,
                            "armed_bar_idx": bar_idx,
                            "armed_ts": self.last_ts,
                        }
                        self._mode_a_last_trigger_bar_idx["P2_SWEEP_UP"] = bar_idx
                        logger.info(f"[PriceActionEngine] [MODE_A_ARMED] Pillar 2 Micro-Sweep CALL Armed | SweptLow: {swept_low:.2f} | AnchorHigh: {bar.high:.2f}")

        # ─── 4. Pillar 2: PUT Micro-Sweep & Reclaim Arming ───
        last_p2_down_idx = self._mode_a_last_trigger_bar_idx.get("P2_SWEEP_DOWN", -100)
        if len(self.bars_1m) >= 7 and (bar_idx - last_p2_down_idx) >= 3:
            is_lunch_chop = False
            if bar.ts_start > 1_000_000_000.0:
                try:
                    dt_bar = datetime.fromtimestamp(bar.ts_start, _IST)
                except Exception:
                    dt_bar = None
                if dt_bar is not None:
                    is_lunch_chop = (dt_time(11, 15, 0) <= dt_bar.time() <= dt_time(13, 0, 0))
            if not is_lunch_chop:
                prior_5 = list(self.bars_1m)[-6:-1]
                swept_high = max(b.high for b in prior_5)
                # Parity with check_micro_sweep_reclaim: minimum sweep depth
                ref_spot_2d = self.last_price if self.last_price > 0 else (bar.close if bar.close > 0 else 24000.0)
                min_sweep_depth_dn = max(1.5, (ref_spot_2d / 24000.0) * 1.5, 0.08 * atr)
                swept_and_reclaimed = (bar.high >= (swept_high + min_sweep_depth_dn)) and (bar.close < swept_high)
                sweep_geom = (
                    (upper_wick_ratio >= 0.35)
                    and (lower_wick_ratio <= 0.15)
                    and (upper_wick >= 1.25 * lower_wick)
                    and (bar.close <= bar.high - 0.55 * c_range)
                )
                # Parity with check_micro_sweep_reclaim: Pain Zone VWAP alignment
                # (composite + direction-selected trapped-buyer line) + obstacle shield
                vwap_ok_p2d = self._pain_zone_vwap_aligned("PUT", bar.close)
                if swept_and_reclaimed and sweep_geom and vwap_ok_p2d and self._check_wyckoff_harmony("DOWN", bar):
                    ok_struct_p2d, _, _ = self._check_structural_obstacle("PUT", bar.close)
                    if ok_struct_p2d:
                        self._mode_a_armed_setups["PILLAR_2_SWEEP_DOWN"] = {
                            "direction": "DOWN",
                            "pillar": 2,
                            "pa_setup_name": "micro_sweep_reclaim",
                            "anchor_high": bar.high,
                            "anchor_low": bar.low,
                            "boundary_level": swept_high,
                            "armed_bar_idx": bar_idx,
                            "armed_ts": self.last_ts,
                        }
                        self._mode_a_last_trigger_bar_idx["P2_SWEEP_DOWN"] = bar_idx
                        logger.info(f"[PriceActionEngine] [MODE_A_ARMED] Pillar 2 Micro-Sweep PUT Armed | SweptHigh: {swept_high:.2f} | AnchorLow: {bar.low:.2f}")

    def _check_wyckoff_harmony(self, direction: str, bar: Any) -> bool:
        """Verifies institutional order flow delta harmony (no opposing liquidation dump)."""
        cb = float(getattr(bar, "call_buy_vol", 0.0) or 0.0)
        pb = float(getattr(bar, "put_buy_vol", 0.0) or 0.0)
        if cb > 1000.0 or pb > 1000.0:
            if direction == "UP" and pb > 2.5 * max(cb, 1.0):
                return False
            elif direction == "DOWN" and cb > 2.5 * max(pb, 1.0):
                return False
        return True

    def _delta_harmony_ok(self, direction: str) -> bool:
        """In-flight OFI delta harmony gate (2026-10-26).

        Single source of truth for the institutional flow-harmony requirement
        (spec §3), shared by the T:50s pre-close path and the Mode A T+0s
        pierce path. Mode B ladders keep their inline 1.30x variant.
        Gracefully bypasses when no option flow is classified (synthetic tests,
        warmup) per the pre-flight audit rule: bull+bear <= 0 => True.
        """
        cb = float(self._opt_flow_1m.get("call_buy_vol", 0.0) or 0.0)
        cs = float(self._opt_flow_1m.get("call_sell_vol", 0.0) or 0.0)
        pb = float(self._opt_flow_1m.get("put_buy_vol", 0.0) or 0.0)
        ps = float(self._opt_flow_1m.get("put_sell_vol", 0.0) or 0.0)
        bull_flow = cb + ps
        bear_flow = pb + cs
        if bull_flow + bear_flow <= 0:
            return True
        if direction == "UP":
            return bull_flow >= 1.25 * bear_flow and cb >= cs
        if direction == "DOWN":
            return bear_flow >= 1.25 * bull_flow and pb >= ps
        return True

    def check_mode_a_pierce(
        self,
        direction: str,
        external_vwap: float = 0.0,
        current_time: float = 0.0,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Mode A: Live In-Flight Intra-Bar Piercing Trigger on Armed Setups.
        Active during T:00s - T:20s of Bar N+1.
        """
        if not self._mode_a_armed_setups:
            return False, "no_mode_a_armed", {}

        now_sec = current_time if current_time > 0 else self.last_ts
        sec_in_min = int(now_sec % 60)
        atr = self.current_atr_1m if self.current_atr_1m > 0 else 10.0
        spot = self.last_price
        # Live Pain Zone VWAP re-check at pierce time (the composite option-flow
        # VWAP and trapped-buyer lines can drift between arming and pierce)
        eff_vwap_mp = external_vwap if external_vwap > 0 else self.current_vwap

        for b_name, setup in list(self._mode_a_armed_setups.items()):
            s_dir = setup.get("direction", "")
            if s_dir != direction:
                continue

            # Never pierce against the Pain Zone VWAP alignment (parity with all other routes)
            if s_dir == "UP" and not self._pain_zone_vwap_aligned("CALL", spot, eff_vwap_mp):
                continue
            if s_dir == "DOWN" and not self._pain_zone_vwap_aligned("PUT", spot, eff_vwap_mp):
                continue

            anchor_h = setup.get("anchor_high", 0.0)
            anchor_l = setup.get("anchor_low", 0.0)
            pillar_num = setup.get("pillar", 1)
            setup_name = setup.get("pa_setup_name", "20_ema_rejection")

            # Priority Window: Disarm after 20s if not pierced
            if sec_in_min > 20:
                del self._mode_a_armed_setups[b_name]
                continue

            triggered = False
            # Pierce Quality Buffer (2026-10-26): marginal single-tick pierces are
            # the classic bull/bear-trap signature (trap case 2026-10-26 10:17:
            # +0.75pt pierce immediately reversed). Scale-invariant excursion
            # beyond the anchor is now required before Mode A consumes a setup.
            pierce_buffer = max(0.10 * atr, 0.10)
            if s_dir == "UP" and spot >= (anchor_h + pierce_buffer):
                triggered = True
            elif s_dir == "DOWN" and spot <= (anchor_l - pierce_buffer):
                triggered = True

            if triggered:
                # OFI Delta Harmony Gate (2026-10-26): Mode A pierces now require
                # in-flight institutional flow harmony (parity with Mode B ladders
                # and the T:50s pre-close path). Consumes the setup on failure —
                # the pierce fired but the order flow betrayed it.
                if not self._delta_harmony_ok(direction):
                    del self._mode_a_armed_setups[b_name]
                    logger.info(
                        f"[PriceActionEngine] [MODE_A_HARMONY_VETO] Pillar {pillar_num} {setup_name} {direction} "
                        f"pierce at spot={spot:.2f} blocked: delta_harmony_fail (in-flight OFI against direction)"
                    )
                    return False, "delta_harmony_fail", {}
                del self._mode_a_armed_setups[b_name]
                b_open = round(self._curr_1m_bar.open, 2) if self._curr_1m_bar is not None else round(anchor_l if direction == "UP" else anchor_h, 2)
                logger.info(f"[PriceActionEngine] [MODE_A_PIERCE] Pillar {pillar_num} {setup_name} {direction} Triggered at spot={spot:.2f} (T+{sec_in_min}s, buffer={pierce_buffer:.2f}pts)")
                return True, setup_name, {
                    "pillar": pillar_num,
                    "pa_setup_name": setup_name,
                    "mode": "MODE_A_PIERCE",
                    "boundary": b_name,
                    "boundary_level": round(setup.get("boundary_level", spot), 2),
                    "sl_price": round(anchor_l - 0.15 * atr if direction == "UP" else anchor_h + 0.15 * atr, 2),
                    "trigger_sec": sec_in_min,
                    "bar_open": b_open,
                    "bar_high": anchor_h,
                    "bar_low": anchor_l,
                    "bar_close": round(spot, 2),
                    "entry_bar_bucket": int(now_sec // 60),
                }

        return False, "mode_a_not_pierced", {}

    def check_mode_a_pre_close(
        self,
        direction: str,
        external_vwap: float = 0.0,
        current_time: float = 0.0,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Mode A: Live In-Flight Pre-Close Early Entry (T:50s - T:58s) for Pillar 1 & Pillar 2.
        Triggers before candle close if rejection geometry and live delta harmony are confirmed.
        """
        now_sec = current_time if current_time > 0 else self.last_ts
        sec_in_min = int(now_sec % 60)
        if not (50 <= sec_in_min <= 58):
            return False, "outside_preclose_window", {}

        curr_bar = self._curr_1m_bar
        if curr_bar is None or curr_bar.tick_count < 6 or self.current_atr_1m <= 0:
            return False, "insufficient_ticks_or_atr", {}

        eff_vwap = external_vwap if external_vwap > 0 else self.current_vwap
        atr = self.current_atr_1m
        spot = self.last_price
        curr_rng = max(curr_bar.high - curr_bar.low, 0.5)

        # OFI Delta Harmony Gate (shared helper — single source of truth)
        if not self._delta_harmony_ok(direction):
            return False, "delta_harmony_fail", {}

        bar_idx = self._total_bars_seen

        # ─── Pillar 1 Pre-Close (20 EMA Rejection) ───
        live_ema_20, live_slope_20_bps = self.get_live_20_ema_and_slope()
        e_close_arr, e_high_arr, e_low_arr = self.get_20_ema_band_arrays(2)
        band_high = e_high_arr[-1]
        band_low = e_low_arr[-1]
        tol_pts = max(1.5, live_ema_20 * (1.5 / 10000.0), 0.12 * atr)

        if direction == "UP":
            last_p1 = self._mode_a_last_trigger_bar_idx.get("P1_EMA20_UP", -100)
            if (bar_idx - last_p1) >= 3:
                uptrend_ok = (live_slope_20_bps >= -0.10) and (self.ema_20_1m >= self.ema_50_1m or self.ema_50_1m <= 0)
                vwap_ok = (self.current_vwap <= 0 or spot > self.current_vwap)
                came_from_above = curr_bar.open >= (band_low - 0.25 * atr)
                band_tested = (curr_bar.low <= band_high + tol_pts) and (spot >= band_low - 0.25 * atr)
                lower_wick = min(curr_bar.open, spot) - curr_bar.low
                lower_wick_ratio = lower_wick / curr_rng
                close_edge = (spot - curr_bar.low) / curr_rng
                # Judge the Dow HL2 chain on the LIVE forming candle (virtual last bar),
                # not on the previous closed bar which is not the touch2 candidate.
                forming_snapshot = OHLCBar(
                    ts_start=curr_bar.ts_start,
                    ts_end=curr_bar.ts_end,
                    open=curr_bar.open,
                    high=curr_bar.high,
                    low=curr_bar.low,
                    close=spot,
                    volume=float(getattr(curr_bar, "volume", 1.0) or 1.0),
                    tick_count=getattr(curr_bar, "tick_count", 0),
                )
                dow = self._validate_dow_hl2_lh2_structure("CALL", touch2_offset=0, virtual_bar=forming_snapshot)
                ok_struct, _, _ = self._check_structural_obstacle("CALL", spot)
                if uptrend_ok and vwap_ok and came_from_above and band_tested and lower_wick_ratio >= 0.20 and close_edge >= 0.65 and (dow.is_valid or len(self.bars_1m) < 16) and ok_struct:
                    self._mode_a_last_trigger_bar_idx["P1_EMA20_UP"] = bar_idx
                    logger.info(f"[PriceActionEngine] [MODE_A_PRECLOSE] Pillar 1 20 EMA CALL Rejection Triggered at spot={spot:.2f} (T+{sec_in_min}s)")
                    return True, "20_ema_rejection", {
                        "pillar": 1,
                        "pa_setup_name": "20_ema_rejection",
                        "mode": "MODE_A_PRECLOSE",
                        "boundary": "20_EMA_BAND",
                        "boundary_level": round(band_high, 2),
                        "sl_price": round(curr_bar.low - 0.15 * atr, 2),
                        "trigger_sec": sec_in_min,
                        "bar_open": round(curr_bar.open, 2),
                        "bar_high": round(curr_bar.high, 2),
                        "bar_low": round(curr_bar.low, 2),
                        "bar_close": round(spot, 2),
                        "entry_bar_bucket": int(now_sec // 60),
                    }
        elif direction == "DOWN":
            last_p1 = self._mode_a_last_trigger_bar_idx.get("P1_EMA20_DOWN", -100)
            if (bar_idx - last_p1) >= 3:
                downtrend_ok = (live_slope_20_bps <= 0.10) and (self.ema_20_1m <= self.ema_50_1m or self.ema_50_1m <= 0)
                vwap_ok = (self.current_vwap <= 0 or spot < self.current_vwap)
                came_from_below = curr_bar.open <= (band_high + 0.25 * atr)
                band_tested = (curr_bar.high >= band_low - tol_pts) and (spot <= band_high + 0.25 * atr)
                upper_wick = curr_bar.high - max(curr_bar.open, spot)
                upper_wick_ratio = upper_wick / curr_rng
                close_edge = (curr_bar.high - spot) / curr_rng
                # Judge the Dow LH2 chain on the LIVE forming candle (virtual last bar),
                # not on the previous closed bar which is not the touch2 candidate.
                forming_snapshot_dn = OHLCBar(
                    ts_start=curr_bar.ts_start,
                    ts_end=curr_bar.ts_end,
                    open=curr_bar.open,
                    high=curr_bar.high,
                    low=curr_bar.low,
                    close=spot,
                    volume=float(getattr(curr_bar, "volume", 1.0) or 1.0),
                    tick_count=getattr(curr_bar, "tick_count", 0),
                )
                dow = self._validate_dow_hl2_lh2_structure("PUT", touch2_offset=0, virtual_bar=forming_snapshot_dn)
                ok_struct, _, _ = self._check_structural_obstacle("PUT", spot)
                if downtrend_ok and vwap_ok and came_from_below and band_tested and upper_wick_ratio >= 0.20 and close_edge >= 0.65 and (dow.is_valid or len(self.bars_1m) < 16) and ok_struct:
                    self._mode_a_last_trigger_bar_idx["P1_EMA20_DOWN"] = bar_idx
                    logger.info(f"[PriceActionEngine] [MODE_A_PRECLOSE] Pillar 1 20 EMA PUT Rejection Triggered at spot={spot:.2f} (T+{sec_in_min}s)")
                    return True, "20_ema_rejection", {
                        "pillar": 1,
                        "pa_setup_name": "20_ema_rejection",
                        "mode": "MODE_A_PRECLOSE",
                        "boundary": "20_EMA_BAND",
                        "boundary_level": round(band_low, 2),
                        "sl_price": round(curr_bar.high + 0.15 * atr, 2),
                        "trigger_sec": sec_in_min,
                        "bar_open": round(curr_bar.open, 2),
                        "bar_high": round(curr_bar.high, 2),
                        "bar_low": round(curr_bar.low, 2),
                        "bar_close": round(spot, 2),
                        "entry_bar_bucket": int(now_sec // 60),
                    }

        # ─── Pillar 2 Pre-Close (Micro-Sweep & Reclaim) ───
        if len(self.bars_1m) >= 6:
            is_lunch_chop = False
            if now_sec > 1_000_000_000.0:
                try:
                    dt_now = datetime.fromtimestamp(now_sec, _IST)
                except Exception:
                    dt_now = None
                if dt_now is not None:
                    is_lunch_chop = (dt_time(11, 15, 0) <= dt_now.time() <= dt_time(13, 0, 0))
            if not is_lunch_chop:
                prior_5 = list(self.bars_1m)[-5:]
                if direction == "UP":
                    last_p2 = self._mode_a_last_trigger_bar_idx.get("P2_SWEEP_UP", -100)
                    if (bar_idx - last_p2) >= 3:
                        swept_low = min(b.low for b in prior_5)
                        # Parity with check_micro_sweep_reclaim: minimum sweep depth
                        ref_spot_pc = self.last_price if self.last_price > 0 else (spot if spot > 0 else 24000.0)
                        min_sweep_depth_pc = max(1.5, (ref_spot_pc / 24000.0) * 1.5, 0.08 * atr)
                        swept_and_reclaimed = (curr_bar.low <= (swept_low - min_sweep_depth_pc)) and (spot > swept_low)
                        lower_wick = min(curr_bar.open, spot) - curr_bar.low
                        lower_wick_ratio = lower_wick / curr_rng
                        upper_wick_pc = curr_bar.high - max(curr_bar.open, spot)
                        upper_wick_ratio_pc = upper_wick_pc / curr_rng
                        close_edge = (spot - curr_bar.low) / curr_rng
                        sweep_geom_ok = (
                            (lower_wick_ratio >= 0.35)
                            and (upper_wick_ratio_pc <= 0.15)
                            and (lower_wick >= 1.25 * upper_wick_pc)
                            and (close_edge >= 0.65)
                        )
                        vwap_ok_pc = self._pain_zone_vwap_aligned("CALL", spot, eff_vwap)
                        if swept_and_reclaimed and sweep_geom_ok and vwap_ok_pc:
                            ok_struct_pc, _, _ = self._check_structural_obstacle("CALL", spot)
                            if ok_struct_pc:
                                self._mode_a_last_trigger_bar_idx["P2_SWEEP_UP"] = bar_idx
                                logger.info(f"[PriceActionEngine] [MODE_A_PRECLOSE] Pillar 2 Micro-Sweep CALL Triggered at spot={spot:.2f} (T+{sec_in_min}s)")
                                return True, "micro_sweep_reclaim", {
                                    "pillar": 2,
                                    "pa_setup_name": "micro_sweep_reclaim",
                                    "mode": "MODE_A_PRECLOSE",
                                    "boundary": "MICRO_SWEEP_LOW",
                                    "boundary_level": round(swept_low, 2),
                                    "sl_price": round(curr_bar.low - 0.15 * atr, 2),
                                    "trigger_sec": sec_in_min,
                                    "bar_open": round(curr_bar.open, 2),
                                    "bar_high": round(curr_bar.high, 2),
                                    "bar_low": round(curr_bar.low, 2),
                                    "bar_close": round(spot, 2),
                                    "entry_bar_bucket": int(now_sec // 60),
                                }
                elif direction == "DOWN":
                    last_p2 = self._mode_a_last_trigger_bar_idx.get("P2_SWEEP_DOWN", -100)
                    if (bar_idx - last_p2) >= 3:
                        swept_high = max(b.high for b in prior_5)
                        # Parity with check_micro_sweep_reclaim: minimum sweep depth
                        ref_spot_pcd = self.last_price if self.last_price > 0 else (spot if spot > 0 else 24000.0)
                        min_sweep_depth_pcd = max(1.5, (ref_spot_pcd / 24000.0) * 1.5, 0.08 * atr)
                        swept_and_reclaimed = (curr_bar.high >= (swept_high + min_sweep_depth_pcd)) and (spot < swept_high)
                        upper_wick = curr_bar.high - max(curr_bar.open, spot)
                        upper_wick_ratio = upper_wick / curr_rng
                        lower_wick_pcd = min(curr_bar.open, spot) - curr_bar.low
                        lower_wick_ratio_pcd = lower_wick_pcd / curr_rng
                        close_edge = (curr_bar.high - spot) / curr_rng
                        sweep_geom_ok = (
                            (upper_wick_ratio >= 0.35)
                            and (lower_wick_ratio_pcd <= 0.15)
                            and (upper_wick >= 1.25 * lower_wick_pcd)
                            and (close_edge >= 0.65)
                        )
                        vwap_ok_pcd = self._pain_zone_vwap_aligned("PUT", spot, eff_vwap)
                        if swept_and_reclaimed and sweep_geom_ok and vwap_ok_pcd:
                            ok_struct_pcd, _, _ = self._check_structural_obstacle("PUT", spot)
                            if ok_struct_pcd:
                                self._mode_a_last_trigger_bar_idx["P2_SWEEP_DOWN"] = bar_idx
                                logger.info(f"[PriceActionEngine] [MODE_A_PRECLOSE] Pillar 2 Micro-Sweep PUT Triggered at spot={spot:.2f} (T+{sec_in_min}s)")
                                return True, "micro_sweep_reclaim", {
                                    "pillar": 2,
                                    "pa_setup_name": "micro_sweep_reclaim",
                                    "mode": "MODE_A_PRECLOSE",
                                    "boundary": "MICRO_SWEEP_HIGH",
                                    "boundary_level": round(swept_high, 2),
                                    "sl_price": round(curr_bar.high + 0.15 * atr, 2),
                                    "trigger_sec": sec_in_min,
                                    "bar_open": round(curr_bar.open, 2),
                                    "bar_high": round(curr_bar.high, 2),
                                    "bar_low": round(curr_bar.low, 2),
                                    "bar_close": round(spot, 2),
                                    "entry_bar_bucket": int(now_sec // 60),
                                }

        return False, "mode_a_preclose_not_met", {}


    def check_mode_b_breakthrough(
        self,
        direction: str,
        external_vwap: float = 0.0,
        current_time: float = 0.0,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Mode B: Live In-Flight Intra-Bar Breakthrough Surge.
        Triggers strictly on:
          1. Pillar 3: Volatility Squeeze Box Breakout (3-4 bar tight compression).
          2. Pillar 4: Dynamic S/R Registry Breakout (with Day Extreme Runway Shield).
        Active mid-bar between T:08s and T:48s.
        """
        now_sec = current_time if current_time > 0 else self.last_ts
        sec_in_min = int(now_sec % 60)
        if not (8 <= sec_in_min <= 48):
            return False, "outside_mode_b_window", {}

        curr_bar = self._curr_1m_bar
        if curr_bar is None or curr_bar.tick_count < 6 or self.current_atr_1m <= 0:
            return False, "insufficient_ticks_or_atr", {}

        atr = self.current_atr_1m
        spot = self.last_price
        curr_rng = curr_bar.high - curr_bar.low

        # Activity Floor Filter: at least 0.20x ATR to avoid single-tick micro noise
        if curr_rng < (0.20 * atr):
            return False, "range_sub_threshold", {}

        cb = float(self._opt_flow_1m.get("call_buy_vol", 0.0))
        cs = float(self._opt_flow_1m.get("call_sell_vol", 0.0))
        pb = float(self._opt_flow_1m.get("put_buy_vol", 0.0))
        ps = float(self._opt_flow_1m.get("put_sell_vol", 0.0))
        bull_flow = cb + ps
        bear_flow = pb + cs
        tick_rate = curr_bar.tick_count / max(float(sec_in_min), 1.0)

        # Require active volume delta or tick rate >= 1.5
        vols_60 = [float(getattr(b, "volume", 0.0) or 0.0) for b in list(self.bars_1m)[-60:]]
        inflight_opt_vol = cb + cs + pb + ps
        if vols_60 and inflight_opt_vol > 0:
            s_v = sorted(vols_60)
            med_vol = float(s_v[len(s_v)//2])
            expected_opt_vol = max(1.0, med_vol * (float(sec_in_min) / 60.0))
            opt_pace_ok = (inflight_opt_vol / expected_opt_vol) >= 1.15
        else:
            opt_pace_ok = True

        if not (opt_pace_ok or tick_rate >= 1.5):
            return False, "volume_velocity_sub_threshold", {}

        # ─── Pain Zone VWAP Alignment Gate (Parity with main Pillar 3/4 ladders) ───
        # Mode B must never fire a breakout against the composite option-flow VWAP
        # or the direction-selected trapped-buyer line (index has no native volume;
        # these are the 4-stream Pain Zone substitutes for an index VWAP).
        eff_vwap_mb = external_vwap if external_vwap > 0 else self.current_vwap
        if direction == "UP" and not self._pain_zone_vwap_aligned("CALL", spot, eff_vwap_mb):
            return False, "below_vwap", {}
        if direction == "DOWN" and not self._pain_zone_vwap_aligned("PUT", spot, eff_vwap_mb):
            return False, "above_vwap", {}

        bar_idx = self._total_bars_seen

        # ─── 1. PILLAR 3: Squeeze Box Breakout (Mode B) ───
        min_clearance = max(2.0, (spot / 24000.0) * 2.0, 0.08 * atr)
        n_box = 4 if len(self.bars_1m) >= 5 else 3
        if len(self.bars_1m) >= n_box:
            recent_bars = list(self.bars_1m)[-n_box:]
            box_high = max(b.high for b in recent_bars)
            box_low = min(b.low for b in recent_bars)
            box_rng = box_high - box_low
            last_p3 = self._mode_b_last_trigger_bar_idx.get("P3_BOX_BREAKTHROUGH", -100)
            if box_rng <= (1.25 * atr) and (bar_idx - last_p3) >= 3:
                if direction == "UP":
                    if curr_bar.open <= box_high and spot >= (box_high + min_clearance):
                        body_ratio = (spot - curr_bar.open) / max(curr_rng, 0.1)
                        upper_wick = curr_bar.high - spot
                        trend_ok = not (self.ema_20_1m < self.ema_50_1m and self.ema_20_1m > 0 and self.ema_50_1m > 0)
                        ok_struct, _, _ = self._check_structural_obstacle("CALL", spot, check_day_extremes=True)
                        if body_ratio >= 0.65 and upper_wick <= (0.05 * atr) and trend_ok and ok_struct:
                            if (bull_flow + bear_flow <= 0) or (bull_flow >= 1.30 * bear_flow and cb > cs):
                                last_spot_cnt = getattr(self, "_mode_b_p3_up_last_tick", -1)
                                last_spot_time = getattr(self, "_mode_b_p3_up_last_time", 0.0)
                                if curr_bar.tick_count != last_spot_cnt or (now_sec - last_spot_time) >= 0.25:
                                    self._mode_b_p3_up_ticks = getattr(self, "_mode_b_p3_up_ticks", 0) + 1
                                    self._mode_b_p3_up_last_tick = curr_bar.tick_count
                                    self._mode_b_p3_up_last_time = now_sec
                                if self._mode_b_p3_up_ticks >= 2:
                                    self._mode_b_p3_up_ticks = 0
                                    self._mode_b_last_trigger_bar_idx["P3_BOX_BREAKTHROUGH"] = bar_idx
                                    logger.info(f"[PriceActionEngine] [MODE_B_P3] Squeeze Box CALL Breakout at spot={spot:.2f} > box_high={box_high:.2f} (T+{sec_in_min}s)")
                                    return True, "volatility_squeeze_breakout", {
                                        "pillar": 3,
                                        "pa_setup_name": "volatility_squeeze_breakout",
                                        "mode": "MODE_B_INFLIGHT_BREAKTHROUGH",
                                        "box_high": round(box_high, 2),
                                        "box_low": round(box_low, 2),
                                        "boundary_level": round(box_high, 2),
                                        "sl_price": round(curr_bar.low - 0.15 * atr, 2),
                                        "trigger_sec": sec_in_min,
                                        "bar_open": round(curr_bar.open, 2),
                                        "bar_high": round(curr_bar.high, 2),
                                        "bar_low": round(curr_bar.low, 2),
                                        "bar_close": round(spot, 2),
                                        "entry_bar_bucket": int(now_sec // 60),
                                    }
                                return False, "awaiting_second_tick", {}
                    self._mode_b_p3_up_ticks = 0
                elif direction == "DOWN":
                    if curr_bar.open >= box_low and spot <= (box_low - min_clearance):
                        body_ratio = (curr_bar.open - spot) / max(curr_rng, 0.1)
                        lower_wick = spot - curr_bar.low
                        trend_ok = not (self.ema_20_1m > self.ema_50_1m and self.ema_20_1m > 0 and self.ema_50_1m > 0)
                        ok_struct, _, _ = self._check_structural_obstacle("PUT", spot, check_day_extremes=True)
                        if body_ratio >= 0.65 and lower_wick <= (0.05 * atr) and trend_ok and ok_struct:
                            if (bull_flow + bear_flow <= 0) or (bear_flow >= 1.30 * bull_flow and pb > ps):
                                last_spot_cnt = getattr(self, "_mode_b_p3_down_last_tick", -1)
                                last_spot_time = getattr(self, "_mode_b_p3_down_last_time", 0.0)
                                if curr_bar.tick_count != last_spot_cnt or (now_sec - last_spot_time) >= 0.25:
                                    self._mode_b_p3_down_ticks = getattr(self, "_mode_b_p3_down_ticks", 0) + 1
                                    self._mode_b_p3_down_last_tick = curr_bar.tick_count
                                    self._mode_b_p3_down_last_time = now_sec
                                if self._mode_b_p3_down_ticks >= 2:
                                    self._mode_b_p3_down_ticks = 0
                                    self._mode_b_last_trigger_bar_idx["P3_BOX_BREAKTHROUGH"] = bar_idx
                                    logger.info(f"[PriceActionEngine] [MODE_B_P3] Squeeze Box PUT Breakout at spot={spot:.2f} < box_low={box_low:.2f} (T+{sec_in_min}s)")
                                    return True, "volatility_squeeze_breakout", {
                                        "pillar": 3,
                                        "pa_setup_name": "volatility_squeeze_breakout",
                                        "mode": "MODE_B_INFLIGHT_BREAKTHROUGH",
                                        "box_high": round(box_high, 2),
                                        "box_low": round(box_low, 2),
                                        "boundary_level": round(box_low, 2),
                                        "sl_price": round(curr_bar.high + 0.15 * atr, 2),
                                        "trigger_sec": sec_in_min,
                                        "bar_open": round(curr_bar.open, 2),
                                        "bar_high": round(curr_bar.high, 2),
                                        "bar_low": round(curr_bar.low, 2),
                                        "bar_close": round(spot, 2),
                                        "entry_bar_bucket": int(now_sec // 60),
                                    }
                                return False, "awaiting_second_tick", {}
                    self._mode_b_p3_down_ticks = 0

        # ─── 2. PILLAR 4: Dynamic Session S/R Registry & T-2 Prominence Hub Breakout (Mode B) ───
        # 5m Structural Alignment Gate (parity with check_topological_prominence_breakout)
        trend_5m_bullish_mb = True
        trend_5m_bearish_mb = True
        if self.bars_5m:
            bar_5m_mb = self.bars_5m[-1]
            trend_5m_bullish_mb = bar_5m_mb.close >= bar_5m_mb.open
            trend_5m_bearish_mb = bar_5m_mb.close <= bar_5m_mb.open

        last_p4 = self._mode_b_last_trigger_bar_idx.get("P4_SR_BREAKTHROUGH", -100)
        if (bar_idx - last_p4) >= 3:
            if direction == "UP":
                candidates = [
                    lvl for lvl in self.session_levels
                    if lvl.level_type == "RESISTANCE"
                    and not lvl.is_broken
                    and lvl.prominence >= 1.25
                    and ((self._total_bars_seen - lvl.seq_idx) >= 3 or (now_sec - lvl.ts_created) >= 180.0)
                ]
                for hub in getattr(self, "_topological_hubs", []):
                    if hub.get("hub_type") == "PEAK" and not hub.get("is_broken", False) and hub.get("pi_hat", 0.0) >= 1.75:
                        if not any(abs(c.price - hub["price"]) < 2.0 for c in candidates):
                            candidates.append(StructuralLevel(
                                level_type="RESISTANCE",
                                price=hub["price"],
                                ts_created=hub.get("ts_created", now_sec),
                                prominence=hub.get("pi_hat", 1.75),
                                bar_idx=hub.get("bar_idx", 0),
                                touch_count=1
                            ))
                candidates.sort(key=lambda l: (l.touch_count * 0.5 + l.prominence), reverse=True)
                matched_any = False
                for r_lvl in candidates:
                    if curr_bar.open <= r_lvl.price and spot >= (r_lvl.price + min_clearance):
                        body_ratio = (spot - curr_bar.open) / max(curr_rng, 0.1)
                        upper_wick = curr_bar.high - spot
                        trend_ok = not (self.ema_20_1m < self.ema_50_1m and self.ema_20_1m > 0 and self.ema_50_1m > 0)
                        ok_struct, _, _ = self._check_structural_obstacle("CALL", spot, ignore_pivots_below=r_lvl.price, check_day_extremes=True)
                        if body_ratio >= 0.65 and upper_wick <= (0.05 * atr) and trend_ok and trend_5m_bullish_mb and ok_struct:
                            if (bull_flow + bear_flow <= 0) or (bull_flow >= 1.30 * bear_flow and cb > cs):
                                matched_any = True
                                last_spot_cnt = getattr(self, "_mode_b_p4_up_last_tick", -1)
                                last_spot_time = getattr(self, "_mode_b_p4_up_last_time", 0.0)
                                if curr_bar.tick_count != last_spot_cnt or (now_sec - last_spot_time) >= 0.25:
                                    self._mode_b_p4_up_ticks = getattr(self, "_mode_b_p4_up_ticks", 0) + 1
                                    self._mode_b_p4_up_last_tick = curr_bar.tick_count
                                    self._mode_b_p4_up_last_time = now_sec
                                if self._mode_b_p4_up_ticks >= 2:
                                    self._mode_b_p4_up_ticks = 0
                                    self._mode_b_last_trigger_bar_idx["P4_SR_BREAKTHROUGH"] = bar_idx
                                    r_lvl.is_broken = True
                                    r_lvl.ts_broken = now_sec
                                    logger.info(f"[PriceActionEngine] [MODE_B_P4] Dynamic S/R CALL Breakout at spot={spot:.2f} > res={r_lvl.price:.2f} (T+{sec_in_min}s)")
                                    return True, "topological_prominence_breakout", {
                                        "pillar": 4,
                                        "pa_setup_name": "topological_prominence_breakout",
                                        "mode": "MODE_B_INFLIGHT_BREAKTHROUGH",
                                        "boundary_level": round(r_lvl.price, 2),
                                        "sl_price": round(curr_bar.low - 0.15 * atr, 2),
                                        "trigger_sec": sec_in_min,
                                        "bar_open": round(curr_bar.open, 2),
                                        "bar_high": round(curr_bar.high, 2),
                                        "bar_low": round(curr_bar.low, 2),
                                        "bar_close": round(spot, 2),
                                        "entry_bar_bucket": int(now_sec // 60),
                                    }
                                return False, "awaiting_second_tick", {}
                if not matched_any:
                    self._mode_b_p4_up_ticks = 0
            elif direction == "DOWN":
                candidates = [
                    lvl for lvl in self.session_levels
                    if lvl.level_type == "SUPPORT"
                    and not lvl.is_broken
                    and lvl.prominence >= 1.25
                    and ((self._total_bars_seen - lvl.seq_idx) >= 3 or (now_sec - lvl.ts_created) >= 180.0)
                ]
                for hub in getattr(self, "_topological_hubs", []):
                    if hub.get("hub_type") == "TROUGH" and not hub.get("is_broken", False) and hub.get("pi_hat", 0.0) >= 1.75:
                        if not any(abs(c.price - hub["price"]) < 2.0 for c in candidates):
                            candidates.append(StructuralLevel(
                                level_type="SUPPORT",
                                price=hub["price"],
                                ts_created=hub.get("ts_created", now_sec),
                                prominence=hub.get("pi_hat", 1.75),
                                bar_idx=hub.get("bar_idx", 0),
                                touch_count=1
                            ))
                candidates.sort(key=lambda l: (l.touch_count * 0.5 + l.prominence), reverse=True)
                matched_any = False
                for s_lvl in candidates:
                    if curr_bar.open >= s_lvl.price and spot <= (s_lvl.price - min_clearance):
                        body_ratio = (curr_bar.open - spot) / max(curr_rng, 0.1)
                        lower_wick = spot - curr_bar.low
                        trend_ok = not (self.ema_20_1m > self.ema_50_1m and self.ema_20_1m > 0 and self.ema_50_1m > 0)
                        ok_struct, _, _ = self._check_structural_obstacle("PUT", spot, ignore_pivots_above=s_lvl.price, check_day_extremes=True)
                        if body_ratio >= 0.65 and lower_wick <= (0.05 * atr) and trend_ok and trend_5m_bearish_mb and ok_struct:
                            if (bull_flow + bear_flow <= 0) or (bear_flow >= 1.30 * bull_flow and pb > ps):
                                matched_any = True
                                last_spot_cnt = getattr(self, "_mode_b_p4_down_last_tick", -1)
                                last_spot_time = getattr(self, "_mode_b_p4_down_last_time", 0.0)
                                if curr_bar.tick_count != last_spot_cnt or (now_sec - last_spot_time) >= 0.25:
                                    self._mode_b_p4_down_ticks = getattr(self, "_mode_b_p4_down_ticks", 0) + 1
                                    self._mode_b_p4_down_last_tick = curr_bar.tick_count
                                    self._mode_b_p4_down_last_time = now_sec
                                if self._mode_b_p4_down_ticks >= 2:
                                    self._mode_b_p4_down_ticks = 0
                                    self._mode_b_last_trigger_bar_idx["P4_SR_BREAKTHROUGH"] = bar_idx
                                    s_lvl.is_broken = True
                                    s_lvl.ts_broken = now_sec
                                    logger.info(f"[PriceActionEngine] [MODE_B_P4] Dynamic S/R PUT Breakout at spot={spot:.2f} < sup={s_lvl.price:.2f} (T+{sec_in_min}s)")
                                    return True, "topological_prominence_breakout", {
                                        "pillar": 4,
                                        "pa_setup_name": "topological_prominence_breakout",
                                        "mode": "MODE_B_INFLIGHT_BREAKTHROUGH",
                                        "boundary_level": round(s_lvl.price, 2),
                                        "sl_price": round(curr_bar.high + 0.15 * atr, 2),
                                        "trigger_sec": sec_in_min,
                                        "bar_open": round(curr_bar.open, 2),
                                        "bar_high": round(curr_bar.high, 2),
                                        "bar_low": round(curr_bar.low, 2),
                                        "bar_close": round(spot, 2),
                                        "entry_bar_bucket": int(now_sec // 60),
                                    }
                                return False, "awaiting_second_tick", {}
                if not matched_any:
                    self._mode_b_p4_down_ticks = 0

        return False, "no_mode_b_trigger", {}

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

        # ─── Priority Intra-Bar Triggers (Mode A Pierce & Mode B Breakthrough) ───
        ok_mb, reason_mb, details_mb = self.check_mode_b_breakthrough(direction, eff_vwap, current_time=current_time)
        if ok_mb:
            setup_name = details_mb.get("pa_setup_name", reason_mb)
            return True, setup_name, self._enrich_details(details_mb)

        ok_ma, reason_ma, details_ma = self.check_mode_a_pierce(direction, eff_vwap, current_time=current_time)
        if ok_ma:
            setup_name = details_ma.get("pa_setup_name", reason_ma)
            return True, setup_name, self._enrich_details(details_ma)

        ok_mp, reason_mp, details_mp = self.check_mode_a_pre_close(direction, eff_vwap, current_time=current_time)
        if ok_mp:
            setup_name = details_mp.get("pa_setup_name", reason_mp)
            return True, setup_name, self._enrich_details(details_mp)

        # Pillar 1: 20 EMA Pullback Rejection (Double-Touch)
        # V4.1: second-place Pain Zone removed — absolute band veto at tick top is single source.
        ok_20, reason_20, details_20 = self.check_20_ema_rejection(direction, current_vwap=eff_vwap, current_time=current_time)
        if ok_20:
            details_20["pain_zone_score"] = 0.0
            details_20["pain_zone_reason"] = "band_veto_top_only"
            return True, "20_ema_rejection", self._enrich_details(details_20)

        # Pillar 2: Micro-Sweep & Reclaim
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

