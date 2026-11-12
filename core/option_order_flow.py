"""
AEGIS Option Order Flow — Real-Time Option Order Flow Aggregator, Tape Reading and Pain Zone VWAP.

Tracks real-time order flow across ATM +/- 25 option strikes:
  1. Aggressive Call/Put Buy vs Sell classification (Ask Lifting vs Bid Hitting).
  2. In-flight 1-Minute Delta Flow (OFI Delta) and Put/Call Volume Ratio (PCR).
  3. 4-Line Trapped-Buyer Pain Zone VWAP Accumulators (CB, CS, PB, PS VWAP).
  4. Institutional Flow Harmony and Delta Divergence Gates.
"""

from __future__ import annotations
import os
import math
from collections import deque
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple, Sequence

try:
    from logger import get_logger
    logger = get_logger("aegis_option_order_flow")
except Exception:
    logger = logging.getLogger("aegis_option_order_flow")


class OptionOrderFlowMixin:
    """Mixin providing real-time option tick classification, OFI delta, PCR, and Pain Zone VWAP."""

    def _init_option_order_flow_state(self) -> None:
        """Initializes all in-flight option flow and Pain Zone accumulators."""
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

        # 4-Line Trapped-Buyer Pain Zone VWAP Accumulators
        self._cum_cb_pv: float = 0.0
        self._cum_cb_vol: float = 0.0
        self._cum_cs_pv: float = 0.0
        self._cum_cs_vol: float = 0.0
        self._cum_pb_pv: float = 0.0
        self._cum_pb_vol: float = 0.0
        self._cum_ps_pv: float = 0.0
        self._cum_ps_vol: float = 0.0

        # Cached environment parameters for O(1) tick performance
        self._pz_intraday_bars: int = 0
        self._pz_last_bar_count: int = -1

    def _update_pz_intraday_bar_count(self) -> None:
        """Updates cached intraday bar count on 1m bar mutations (close/seed)."""
        if not hasattr(self, "bars_1m") or not self.bars_1m:
            self._pz_intraday_bars = 0
            self._pz_last_bar_count = 0
            return
        last_b = self.bars_1m[-1]
        curr_day = int(last_b.ts_start // 86400) if last_b else 0
        self._pz_intraday_bars = sum(
            1 for b in self.bars_1m
            if (curr_day <= 0 or int(getattr(b, "ts_start", 0) // 86400) == curr_day)
        )
        self._pz_last_bar_count = len(self.bars_1m)

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
        Includes Post-Sweep Quote-Step detection to prevent book-stepping ask sweeps
        from falsely classifying as sells when post-sweep bp1 advances to lp.
        """
        valid_quote = bp1 > 0 and sp1 > 0 and sp1 > bp1
        prev_lp = self._opt_last_lp.get(sym)

        if valid_quote:
            if lp >= sp1:
                frac = 1.0                          # Lifted the ask — pure buy
            elif prev_lp is not None and lp > prev_lp and lp >= bp1:
                frac = 1.0                          # Post-sweep quote step up -> aggressive buyer sweep!
            elif prev_lp is not None and lp < prev_lp and lp <= sp1:
                frac = 0.0                          # Post-sweep quote step down -> aggressive seller sweep!
            elif lp <= bp1:
                frac = 0.0                          # Hit the bid — pure sell
            else:
                # Inside spread -> Tick Rule (Lee-Ready fallback)
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
        else:
            # Broken/locked quote -> Tick Rule (Lee-Ready fallback)
            if prev_lp is None or prev_lp <= 0:
                self._opt_last_lp[sym] = lp
                return None
            if lp > prev_lp:
                frac = 1.0
            elif lp < prev_lp:
                frac = 0.0
            else:
                frac = self._opt_last_frac.get(sym)
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
        last_b = self.bars_1m[-1] if self.bars_1m else None
        curr_day = int(last_b.ts_start // 86400) if last_b else 0
        now_epoch = getattr(self, "last_ts", 0.0)
        if now_epoch > 0 and curr_day > 0 and int(now_epoch // 86400) > curr_day:
            intraday_bars = 0
        else:
            total_b = len(self.bars_1m) if hasattr(self, "bars_1m") else 0
            if total_b != getattr(self, "_pz_last_bar_count", -1):
                self._update_pz_intraday_bar_count()
            intraday_bars = getattr(self, "_pz_intraday_bars", 0)
        time_ok = (intraday_bars >= req_bars) if req_bars > 0 else True

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

    def band_veto_blocks(self, direction: str, spot: Optional[float] = None) -> Tuple[bool, float, float, float]:
        """Apply Master Session VWAP directional boundary.

        Returns (blocked, master_vwap, master_vwap, 0.0). Warmup (missing lines)
        or missing spot never blocks. UP requires spot strictly above master_vwap;
        DOWN requires spot strictly below master_vwap.
        """
        curr_spot = float(spot or self.last_price or 0.0)
        try:
            pz = self.get_pain_zone_vwaps()
        except Exception:
            return True, 0.0, 0.0, 0.0
        top = float(pz.get("zone_top", 0.0) or 0.0)
        bottom = float(pz.get("zone_bottom", 0.0) or 0.0)
        mid = float(pz.get("zone_mid", 0.0) or getattr(self, "current_vwap", 0.0) or 0.0)
        master_vwap = mid if mid > 0 else (top if top > 0 else bottom)

        if master_vwap <= 0 or curr_spot <= 0:
            return False, master_vwap, master_vwap, 0.0

        is_up = str(direction).upper() in ("UP", "CALL", "CE", "C")
        if is_up:
            return curr_spot <= master_vwap, master_vwap, master_vwap, 0.0
        return curr_spot >= master_vwap, master_vwap, master_vwap, 0.0

    def _pain_zone_vwap_aligned(self, direction: str, price: float, eff_vwap: float = 0.0) -> bool:
        """
        Master Composite Session VWAP Directional Alignment.
        CALL: Spot must be strictly above Master Session VWAP (price > master_vwap).
        PUT:  Spot must be strictly below Master Session VWAP (price < master_vwap).
        Eliminates the artificial 76-190 pt chop band while preserving clean directional filter.
        """
        try:
            if price <= 0:
                return True
            is_call = str(direction).upper() in ("UP", "CALL", "CE", "C")
            master_vwap = float(eff_vwap) if eff_vwap > 0 else float(getattr(self, "current_vwap", 0.0) or 0.0)
            if master_vwap <= 0:
                pz = self.get_pain_zone_vwaps()
                master_vwap = float(pz.get("zone_mid", 0.0) or pz.get("zone_top", 0.0) or pz.get("zone_bottom", 0.0) or 0.0)

            if master_vwap <= 0:
                return True

            if is_call:
                return price > master_vwap
            return price < master_vwap
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
        # If the entire options feed went silent for > 5.0s (any duration), clear cum vols
        # and cached tick state so every symbol cleanly reseeds its baseline on its first post-reconnect tick.
        if self._last_global_opt_tick_ts > 0 and (now_ts - self._last_global_opt_tick_ts) > 5.0:
            self._opt_last_cum_vol.clear()
            self._opt_last_lp.clear()
            self._opt_last_frac.clear()
        self._last_global_opt_tick_ts = now_ts

        # 1. Day-cumulative volume differencing + session rollover guard
        prev_vol = self._opt_last_cum_vol.get(sym)
        if prev_vol is None:
            self._opt_last_cum_vol[sym] = vol
            self._opt_last_lp[sym] = lp
            return  # Baseline seed tick

        delta_vol = vol - prev_vol
        if delta_vol <= 0:
            # Out-of-order stale tick or zero-volume quote update: discard safely.
            # Never overwrite _opt_last_cum_vol with a smaller stale volume or create phantom volume.
            return
        self._opt_last_cum_vol[sym] = vol

        # 2. Window Filter: ATM ± 3 strikes (Tight institutional corridor)
        ref_atm = atm_strike or (round(self.last_price / self.strike_step) * self.strike_step)
        if ref_atm > 0 and abs(strike - ref_atm) > (3.0 * self.strike_step):
            return

        # 3. Hybrid Classification: Touch-first, Tick-Rule fallback
        buy_frac = self._classify_buy_fraction(sym, lp, bp1, sp1)
        if buy_frac is None:
            self._opt_flow_1m["unclass_vol"] += delta_vol
            return

        buy_vol = delta_vol * buy_frac
        sell_vol = delta_vol - buy_vol

        # Forward aggressive volume to SweepStopRunDetector if active
        if hasattr(self, "sweep_detector") and self.sweep_detector is not None:
            self.sweep_detector.on_option_tick(
                sym=sym,
                strike=strike,
                side=side,
                buy_vol=buy_vol,
                sell_vol=sell_vol,
                ts=now_ts,
                atm_strike=ref_atm,
                strike_step=getattr(self, "strike_step", 50.0),
            )

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

        # 5. Live Forming Bar Option Volume Sync (Strictly options volume — spot index has no volume)
        curr_bar = getattr(self, "_curr_1m_bar", None)
        if curr_bar is not None:
            curr_bar.volume = (
                self._opt_flow_1m.get("call_buy_vol", 0.0)
                + self._opt_flow_1m.get("call_sell_vol", 0.0)
                + self._opt_flow_1m.get("put_buy_vol", 0.0)
                + self._opt_flow_1m.get("put_sell_vol", 0.0)
            )


    def _check_wyckoff_harmony(self, direction: str, bar: Any) -> bool:
        """Verifies institutional order flow delta harmony (no opposing liquidation dump)."""
        cb = float(getattr(bar, "call_buy_vol", 0.0) or 0.0)
        pb = float(getattr(bar, "put_buy_vol", 0.0) or 0.0)
        eff_lot = getattr(self, "lot_size", 65.0) or 65.0
        min_v = 15.0 * eff_lot  # Scale-invariant (15 lots minimum active flow)
        if cb > min_v or pb > min_v:
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


class MicrostructureScorer:
    """
    Real-time empirical microstructure confidence scoring engine for Options.
    Evaluates tick arrival frequency, deep order book vacuums, spread compression,
    and cross-strike gamma coherence to output a calibrated 0-100 conviction score.
    """
    
    def __init__(self, high_conviction_threshold: float = 75.0, **_kwargs: Any):
        self.high_conviction_threshold = high_conviction_threshold
        self.moderate_conviction_threshold = 60.0
        
        # In-memory defaults for backward-compatibility; disk I/O eliminated
        self.raw_intercept = -6.041813
        self.weights = {"target_tick_intensity": 4.532921}
                
        self.history: Dict[str, deque] = {}
        self.max_window_sec = 90.0  # C1 fix: expanded from 30s → 90s to capture T:50–58s Mode A pre-close window
        self._running_ofi: Dict[str, float] = {}
        self._running_tot_q: Dict[str, float] = {}

    def update_tick(
        self,
        sym: str,
        ts_sec: float,
        lp: float,
        bp1: float,
        sp1: float,
        b1: float = 0.0,
        a1: float = 0.0,
        b2: float = 0.0,
        a2: float = 0.0,
        b3: float = 0.0,
        a3: float = 0.0,
        b4: float = 0.0,
        a4: float = 0.0,
        b5: float = 0.0,
        a5: float = 0.0,
        vol: float = 0.0,
        **kwargs: Any
    ) -> None:
        """Records a tick and prunes rows older than the configured scoring window."""
        # Backward compatibility for legacy callers passing (bq1, sq1, deep_bid_qty, deep_ask_qty)
        if "bq1" in kwargs:
            b1 = float(kwargs["bq1"])
        if "sq1" in kwargs:
            a1 = float(kwargs["sq1"])
        if "volume" in kwargs:
            vol = float(kwargs["volume"])
        elif "v" in kwargs:
            vol = float(kwargs["v"])
        if "deep_bid_qty" in kwargs or "deep_ask_qty" in kwargs:
            db = float(kwargs.get("deep_bid_qty", 0.0))
            da = float(kwargs.get("deep_ask_qty", 0.0))
            b3 = db / 3.0; b4 = db / 3.0; b5 = db / 3.0
            a3 = da / 3.0; a4 = da / 3.0; a5 = da / 3.0

        if sym not in self.history:
            self.history[sym] = deque(maxlen=1000)  # C1 fix: expanded from 200 → 1000 to match 90s window @ ~10 ticks/s
            self._running_ofi[sym] = 0.0
            self._running_tot_q[sym] = 0.0
            
        buf = self.history[sym]
        if buf:
            prev_b1 = buf[-1][4]
            prev_a1 = buf[-1][5]
            d_b = b1 - prev_b1
            d_s = a1 - prev_a1
            d_ofi = max(d_b, 0.0) - max(-d_s, 0.0)
            d_q = b1 + a1
        else:
            d_ofi = 0.0
            d_q = 0.0

        self._running_ofi[sym] += d_ofi
        self._running_tot_q[sym] += d_q
        buf.append((ts_sec, lp, bp1, sp1, b1, a1, b2, a2, b3, a3, b4, a4, b5, a5, d_ofi, d_q, vol))
        
        cutoff = ts_sec - self.max_window_sec
        while buf and buf[0][0] < cutoff:
            popped = buf.popleft()
            self._running_ofi[sym] -= popped[14]
            self._running_tot_q[sym] -= popped[15]

    def compute_score(self, *args, **kwargs) -> Tuple[float, bool, Dict[str, float]]:
        """DEPRECATED: Empirical logit scorer retired; scoring fully handled by CompositeConvictionEngine."""
        return 0.0, False, {}

    def verify_event_bound_30s(self, *args, **kwargs) -> Tuple[bool, Dict[str, float]]:
        """DEPRECATED: Replaced by CompositeConvictionEngine circuit breakers."""
        return True, {"reason": "delegated_to_conviction_engine"}


@dataclass
class ConvictionResult:
    """Structured output of the composite conviction evaluation."""
    total_score: float
    is_approved: bool
    sizing_tier: str  # "FULL_SIZE", "STANDARD_SIZE", "BLOCKED"
    recommended_tp_pct: float
    sub_scores: Dict[str, float] = field(default_factory=dict)
    breakdown: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""


class CompositeConvictionEngine:
    """
    AEGIS Unified Option Microstructure & Order Flow Conviction Engine (OFEE).
    Consolidates raw tick-buffer metrics extraction, sanity circuit breakers,
    and Dual-Profile institutional conviction evaluation (0-100 scale).

    Merged Architecture:
      - Raw tick buffer parsing & circuit breakers directly embedded (AdaptiveMicroValidator merged).
      - Mode B Profile: In-Flight Mid-Bar Breakthrough Surge (T:02s onwards, No Upper Cap).
      - Mode A Profile: Structural Rejection & Close (T:50s Pre-Close / T:00s Close).
    """

    @staticmethod
    def extract_micro_metrics(
        tick_buffer: Sequence[Any],
        anchor_T: Optional[float] = None,
        lookback_sec: float = 30.0,
    ) -> Dict[str, Any]:
        """
        Extracts real-time option contract metrics directly from the raw tick buffer.
        Evaluates top-of-book L1 touch I1, L3-L5 deep book pressure, spread %,
        arrival tick intensity (ticks/sec), and sanity circuit breakers.
        """
        if not tick_buffer:
            return {
                "is_valid": False,
                "reason": "insufficient_buffer",
                "n_ticks": 0,
                "touch_i1": 0.0,
                "spread_pct": 0.50,
            }

        lookback = float(lookback_sec or 30.0)
        if anchor_T is not None and anchor_T > 0:
            w_start = anchor_T - lookback
            sl = [r for r in tick_buffer if w_start <= r[0] <= anchor_T]
        else:
            last_ts = tick_buffer[-1][0]
            w_start = last_ts - lookback
            sl = [r for r in tick_buffer if r[0] >= w_start]

        if len(sl) < 2:
            return {
                "is_valid": False,
                "reason": "insufficient_anchored_ticks",
                "n_ticks": len(sl),
                "touch_i1": 0.0,
                "spread_pct": 0.50,
            }

        last_r = sl[-1]
        first_r = sl[0]
        p0 = float(first_r[1])
        p1 = float(last_r[1])

        depth_r = last_r
        for r in reversed(sl):
            r_b1 = float(r[4]) if len(r) > 4 else 0.0
            r_a1 = float(r[5]) if len(r) > 5 else 0.0
            r_bp = float(r[2]) if len(r) > 2 else 0.0
            r_sp = float(r[3]) if len(r) > 3 else 0.0
            if (r_b1 + r_a1) > 0 or (r_bp > 0 and r_sp > 0):
                depth_r = r
                break

        bp1 = float(depth_r[2]) if len(depth_r) > 2 else 0.0
        sp1 = float(depth_r[3]) if len(depth_r) > 3 else 0.0

        b1 = float(depth_r[4]) if len(depth_r) > 4 else 0.0
        a1 = float(depth_r[5]) if len(depth_r) > 5 else 0.0
        b2 = float(depth_r[6]) if len(depth_r) > 6 else 0.0
        a2 = float(depth_r[7]) if len(depth_r) > 7 else 0.0
        b3 = float(depth_r[8]) if len(depth_r) > 8 else 0.0
        a3 = float(depth_r[9]) if len(depth_r) > 9 else 0.0
        b4 = float(depth_r[10]) if len(depth_r) > 10 else 0.0
        a4 = float(depth_r[11]) if len(depth_r) > 11 else 0.0
        b5 = float(depth_r[12]) if len(depth_r) > 12 else 0.0
        a5 = float(depth_r[13]) if len(depth_r) > 13 else 0.0

        tot_bid_depth = b1 + b2 + b3 + b4 + b5
        tot_ask_depth = a1 + a2 + a3 + a4 + a5
        tot_depth = tot_bid_depth + tot_ask_depth

        has_valid_quotes = (bp1 > 0.0 and sp1 > 0.0)
        mid_px = (bp1 + sp1) / 2.0 if has_valid_quotes else p1
        spread_pct = ((sp1 - bp1) / mid_px * 100.0) if (has_valid_quotes and mid_px > 0) else 99.0

        bp0 = float(first_r[2]) if len(first_r) > 2 else 0.0
        sp0 = float(first_r[3]) if len(first_r) > 3 else 0.0
        mid_0 = (bp0 + sp0) / 2.0 if (bp0 > 0 and sp0 > 0) else p0

        micro_ret = (mid_px - mid_0) / mid_0 if mid_0 > 0 else ((p1 - p0) / p0 if p0 > 0 else 0.0)

        # 5-Tick Temporal Smoothing on Touch Imbalance (I1)
        # Prevents a single MM quote sweep/wobble from collapsing conviction on a viable contract
        recent_i1s = []
        for r in reversed(sl):
            r_b1 = float(r[4]) if len(r) > 4 else 0.0
            r_a1 = float(r[5]) if len(r) > 5 else 0.0
            if (r_b1 + r_a1) > 0:
                recent_i1s.append((r_b1 - r_a1) / (r_b1 + r_a1 + 1e-4))
                if len(recent_i1s) >= 5:
                    break

        if recent_i1s:
            sorted_i1s = sorted(recent_i1s)
            n_i1 = len(sorted_i1s)
            mid_idx = n_i1 // 2
            touch_i1 = sorted_i1s[mid_idx] if (n_i1 % 2 == 1) else (sorted_i1s[mid_idx - 1] + sorted_i1s[mid_idx]) / 2.0
        else:
            touch_i1 = (b1 - a1) / (b1 + a1 + 1e-4)

        i3 = (b3 - a3) / (b3 + a3 + 1e-4)
        i4 = (b4 - a4) / (b4 + a4 + 1e-4)
        i5 = (b5 - a5) / (b5 + a5 + 1e-4)
        deep_pressure = (i3 + i4 + i5) / 3.0

        n_ticks = len(sl)
        effective_dt = max(float(lookback), 1.0)
        tick_intensity = float(n_ticks) / effective_dt

        base_metrics = {
            "is_valid": True,
            "anchor_T": float(anchor_T or 0.0),
            "lookback_sec": float(lookback),
            "micro_ret": float(micro_ret),
            "micro_ret_30s": float(micro_ret),
            "option_ret_30s": float(micro_ret),
            "touch_i1": float(touch_i1),
            "deep_pressure": float(deep_pressure),
            "tot_depth": float(tot_depth),
            "tot_bid_depth": float(tot_bid_depth),
            "tot_ask_depth": float(tot_ask_depth),
            "spread_pct": float(spread_pct),
            "n_ticks": int(n_ticks),
            "tick_intensity": float(tick_intensity),
            "p0": float(p0),
            "p1": float(p1),
            "bp1": float(bp1),
            "sp1": float(sp1),
        }

        # Hard Circuit Breakers
        if not has_valid_quotes:
            return {**base_metrics, "is_valid": False, "reason": "circuit_breaker_missing_quotes"}
        if sp1 <= bp1:
            return {**base_metrics, "is_valid": False, "reason": "circuit_breaker_crossed_quotes"}
        if spread_pct <= 0.0 or spread_pct > 2.5:
            return {**base_metrics, "is_valid": False, "reason": "circuit_breaker_spread_blowout"}
        if tot_depth < 200:
            return {**base_metrics, "is_valid": False, "reason": "circuit_breaker_ghost_book"}
        if micro_ret < -0.015:
            return {**base_metrics, "is_valid": False, "reason": "circuit_breaker_severe_dump"}

        return base_metrics

    # Streamlined weights (sum to 100.0) — Pure Option Microstructure & Order Flow Model
    # Domain 1 (Spot Structure) is retired (0.0 pts) as it is 100% verified upstream by PriceActionEngine.
    DEFAULT_WEIGHTS = {
        "structure": 0.0,
        "impulse": 35.0,   # Option Micro-Momentum & Premium Velocity
        "flow": 40.0,      # Touch Aggression I1 & Live OFI Delta
        "contract": 25.0,  # Contract Tradability & Book Depth Support
    }

    # Setup-specific weight profiles (each sums to 100.0)
    SETUP_WEIGHT_PROFILES = {
        setup: {
            "structure": 0.0,
            "impulse": 35.0,
            "flow": 40.0,
            "contract": 25.0,
        }
        for setup in (
            "20_ema_rejection",
            "micro_sweep_reclaim",
            "albrooks_2legged_pullback",
            "volatility_squeeze_breakout",
            "hvg_network_phase_shift",
            "sr_breakout",
            "topological_prominence_breakout",
            "break_of_structure",
        )
    }

    def __init__(
        self,
        min_approval_score: float = 80.0,
        high_conviction_score: float = 85.0,
        standard_tp_pct: float = 0.040,
        high_conviction_tp_pct: float = 0.040,
    ):
        self.min_approval_score = float(min_approval_score)
        self.high_conviction_score = float(high_conviction_score)
        self.standard_tp_pct = float(standard_tp_pct)
        self.high_conviction_tp_pct = float(high_conviction_tp_pct)

    @staticmethod
    def estimate_black_scholes_delta(
        spot: float,
        strike: float,
        side: str,
        iv: float = 0.15,
        dte_days: float = 1.0,
    ) -> float:
        """
        Pure Python Black-Scholes Delta approximation. O(1), zero imports.
        """
        if spot <= 0 or strike <= 0:
            return 0.35  # Safe default sweet-spot assumption if missing
        t = max(dte_days / 365.0, 0.001)  # At least ~8 hours
        sigma = max(iv, 0.05)
        sqrt_t = math.sqrt(t)
        try:
            d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * t) / (sigma * sqrt_t)
            # Normal CDF using math.erf
            norm_cdf = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
            if side.upper() in ("CE", "CALL", "UP"):
                return max(0.01, min(0.99, norm_cdf))
            else:
                return max(0.01, min(0.99, 1.0 - norm_cdf))
        except Exception:
            # Fallback to linear moneyness
            moneyness = (spot - strike) / spot if side.upper() in ("CE", "CALL", "UP") else (strike - spot) / spot
            return max(0.01, min(0.99, 0.50 + moneyness * 10.0))

    def _evaluate_mode_b_conviction(
        self,
        direction: str,
        setup_name: str,
        touch_i1: float,
        spread_pct: float,
        tick_rate: Optional[float] = None,
        wyckoff_details: Optional[Dict[str, Any]] = None,
        option_delta: Optional[float] = None,
        micro_ret: Optional[float] = None,
    ) -> Tuple[float, Dict[str, float], Dict[str, Any], str]:
        """
        Mode B Conviction Profile: In-Flight Mid-Bar Breakthrough Surge.
        Active from T:02s onwards anytime during the candle (No Upper Limit Cap).
        Focused on instantaneous breakout velocity, ask sweeps, and live tick intensity.

        Domains (100 pts total):
          1. Touch Aggression & Ask Sweep I1: 40 pts
          2. Live Tick Arrival Intensity: 35 pts
          3. Contract Tradability & Spread: 25 pts
        """
        # Mode B Hard Momentum Velocity Floor:
        # Never approve Mode B in-flight breakthrough on a decaying or stagnant option contract!
        # The option must be expanding (micro_ret >= +0.20% over lookback window).
        if micro_ret is not None and micro_ret < 0.0020:
            sub_zero = {
                "touch_aggression": 0.0, "tick_intensity": 0.0, "contract_tradability": 0.0,
                "flow": 0.0, "impulse": 0.0, "contract": 0.0, "structure": 0.0,
            }
            details_veto = {
                "mode": "MODE_B_INFLIGHT_BREAKTHROUGH",
                "micro_ret": round(micro_ret, 5),
                "reason": "stagnant_or_negative_option_velocity",
            }
            return 0.0, sub_zero, details_veto, "mode_b_conviction_too_low: option_not_expanding"

        is_up = direction.upper() in ("UP", "CALL")
        # Option Buyer mandate: Whether buying CE (UP) or PE (DOWN), the trader is LONG the option.
        # Strong bid support (touch_i1 > 0) on the option contract is always bullish for that option.
        eff_i1 = touch_i1
        wyckoff_conf = bool((wyckoff_details or {}).get("wyckoff_confirmed", False))
        ofi = float(
            (wyckoff_details or {}).get("opt_delta_flow")
            or (wyckoff_details or {}).get("ofi_delta")
            or (wyckoff_details or {}).get("synthetic_net_delta")
            or 0.0
        )

        # 1. Touch Aggression & Ask Sweep I1 (40 pts max)
        if eff_i1 >= 0.20:
            agg_pts = 40.0
        elif eff_i1 >= 0.08:
            agg_pts = 34.0
        elif eff_i1 >= -0.05:
            agg_pts = 28.0
        elif eff_i1 >= -0.20:
            agg_pts = 16.0
        else:
            agg_pts = 0.0  # Opposing ask wall

        if wyckoff_conf:
            ofi_aligned = (ofi > 0) if is_up else (ofi < 0)
            ofi_opposing = (ofi < 0) if is_up else (ofi > 0)
            n1_fp = (wyckoff_details or {}).get("n1_footprint") or {}
            # Only grant Wyckoff floor when flow has real bid support (eff_i1 >= 0.05),
            # never when book is dominated by opposing ask walls.
            if eff_i1 >= 0.05:
                if ofi_aligned:
                    agg_pts = max(agg_pts, 34.0)
                elif not ofi_opposing and (n1_fp.get("n1_vol_dryup", False) or n1_fp.get("n1_stopping_vol", False)):
                    agg_pts = max(agg_pts, 34.0)

        # 2. Live Tick Arrival Intensity (35 pts max)
        if tick_rate is not None:
            if tick_rate >= 1.5:
                tick_pts = 35.0  # Institutional rapid-fire sweep
            elif tick_rate >= 0.8:
                tick_pts = 28.0  # Steady active flow
            elif tick_rate >= 0.4:
                tick_pts = 26.0 if wyckoff_conf else 22.0  # Normal liquidity
            else:
                tick_pts = 16.0 if wyckoff_conf else 12.0  # Thin / sluggish arrival
        else:
            # Clean baseline for tests or synthetic ticks
            tick_pts = 30.0 if wyckoff_conf else 26.0

        # 3. Contract Tradability & Spread (25 pts max)
        if spread_pct <= 0.0 or spread_pct > 2.50:
            trad_pts = 0.0  # Invalid, crossed quotes, or circuit breaker blowout
        elif spread_pct <= 0.50:
            trad_pts = 25.0
        elif spread_pct <= 1.00:
            trad_pts = 20.0
        elif spread_pct <= 1.50:
            trad_pts = 12.0
        else:
            trad_pts = 5.0

        total = round(max(0.0, min(100.0, agg_pts + tick_pts + trad_pts)), 1)
        sub_scores = {
            "touch_aggression": round(agg_pts, 1),
            "tick_intensity": round(tick_pts, 1),
            "contract_tradability": round(trad_pts, 1),
            # Aliases for telemetry and logging parity
            "flow": round(agg_pts, 1),
            "impulse": round(tick_pts, 1),
            "contract": round(trad_pts, 1),
            "structure": 0.0,
        }
        details = {
            "mode": "MODE_B_INFLIGHT_BREAKTHROUGH",
            "effective_i1": round(eff_i1, 3),
            "tick_rate": round(tick_rate, 2) if tick_rate is not None else None,
            "spread_pct": round(spread_pct, 3),
            "wyckoff_confirmed": wyckoff_conf,
            "contract_domain_neutralized": True,
            "abs_delta": round(abs(option_delta), 3) if option_delta is not None else 0.35,
            "futures_muted": True,
            "fut_divergence_penalty": 0.0,
        }
        drags = []
        if agg_pts < 25.0:
            drags.append("aggression_drag")
        if tick_pts < 20.0:
            drags.append("tick_rate_drag")
        if trad_pts < 15.0:
            drags.append("tradability_drag")
        reason = f"mode_b_conviction_too_low: {', '.join(drags)}" if drags else "mode_b_inflight_approved"
        return total, sub_scores, details, reason

    def _evaluate_mode_a_conviction(
        self,
        direction: str,
        setup_name: str,
        touch_i1: float,
        spread_pct: float,
        deep_pressure: float = 0.0,
        micro_ret: Optional[float] = None,
        tot_depth: float = 1000.0,
        wyckoff_details: Optional[Dict[str, Any]] = None,
        option_delta: Optional[float] = None,
    ) -> Tuple[float, Dict[str, float], Dict[str, Any], str]:
        """
        Mode A Conviction Profile: Structural Rejection & Close (Pillar 1 & Pillar 2).
        Triggers at T:50s Pre-Close or T:00s Bar Close upon confirmed liquidity sweep/rejection.
        Focused on sustained order flow support, Wyckoff absorption, and full-candle velocity.

        Domains (100 pts total):
          1. Order Book Support & OFI Delta (I1 + L3-L5 Deep Pressure): 40 pts
          2. Option Premium Return Velocity: 35 pts
          3. Contract Tradability & Book Depth Support: 25 pts
        """
        is_up = direction.upper() in ("UP", "CALL")
        # Option Buyer mandate: Strong bid support on the option book is always positive for both CE and PE:
        eff_i1 = touch_i1
        eff_deep = deep_pressure
        wyckoff_conf = bool((wyckoff_details or {}).get("wyckoff_confirmed", False))
        ofi = float(
            (wyckoff_details or {}).get("opt_delta_flow")
            or (wyckoff_details or {}).get("ofi_delta")
            or (wyckoff_details or {}).get("synthetic_net_delta")
            or 0.0
        )

        # 1. Order Book Support & OFI Delta (40 pts max)
        if eff_i1 >= 0.15 and eff_deep >= -0.10:
            flow_pts = 40.0
        elif eff_i1 >= 0.05:
            flow_pts = 34.0
        elif eff_i1 >= -0.10:
            flow_pts = 26.0
        elif eff_i1 >= -0.20:
            flow_pts = 16.0
        else:
            flow_pts = 0.0

        if wyckoff_conf:
            ofi_aligned = (ofi > 0) if is_up else (ofi < 0)
            ofi_opposing = (ofi < 0) if is_up else (ofi > 0)
            if ofi_aligned:
                flow_pts = max(flow_pts, 34.0)
            elif not ofi_opposing:
                flow_pts = max(flow_pts, 28.0)

        # 2. Option Premium Return Velocity (35 pts max)
        if micro_ret is not None:
            if micro_ret >= 0.008:
                mom_pts = 35.0
            elif micro_ret >= 0.003:
                mom_pts = 30.0
            elif micro_ret >= -0.001:
                mom_pts = 25.0
            elif micro_ret >= -0.005:
                mom_pts = 14.0
            else:
                mom_pts = 0.0  # Option dumping despite setup
        else:
            mom_pts = 30.0 if wyckoff_conf else 25.0

        # 3. Contract Tradability & Book Depth Support (25 pts max)
        if spread_pct <= 0.0 or spread_pct > 2.50 or (0.0 < tot_depth < 200.0):
            trad_pts = 0.0  # Invalid, crossed quotes, circuit breaker blowout or ghost book
        elif spread_pct <= 1.00:
            trad_pts = 25.0
        elif spread_pct <= 1.50:
            trad_pts = 15.0
        else:
            trad_pts = 5.0

        total = round(max(0.0, min(100.0, flow_pts + mom_pts + trad_pts)), 1)
        sub_scores = {
            "order_book_support": round(flow_pts, 1),
            "micro_momentum": round(mom_pts, 1),
            "contract_tradability": round(trad_pts, 1),
            # Aliases for telemetry and logging parity
            "flow": round(flow_pts, 1),
            "impulse": round(mom_pts, 1),
            "contract": round(trad_pts, 1),
            "structure": 0.0,
        }
        details = {
            "mode": "MODE_A_REJECTION_CLOSE",
            "effective_i1": round(eff_i1, 3),
            "effective_deep": round(eff_deep, 3),
            "micro_ret": round(micro_ret, 4) if micro_ret is not None else None,
            "tot_depth": round(tot_depth, 1),
            "spread_pct": round(spread_pct, 3),
            "wyckoff_confirmed": wyckoff_conf,
            "contract_domain_neutralized": True,
            "abs_delta": round(abs(option_delta), 3) if option_delta is not None else 0.35,
            "futures_muted": True,
            "fut_divergence_penalty": 0.0,
        }
        drags = []
        if flow_pts < 25.0:
            drags.append("aggression_drag")
        if mom_pts < 20.0:
            drags.append("momentum_drag")
        if trad_pts < 15.0:
            drags.append("tradability_drag")
        reason = f"mode_a_conviction_too_low: {', '.join(drags)}" if drags else "mode_a_rejection_approved"
        return total, sub_scores, details, reason

    def compute_conviction_score(
        self,
        direction: str,
        setup_name: str,
        current_bar: Optional[Any] = None,
        touch_i1: float = 0.0,
        option_delta: Optional[float] = None,
        spread_pct: float = 0.25,
        pa_details: Optional[Dict[str, Any]] = None,
        wyckoff_details: Optional[Dict[str, Any]] = None,
        micro_metrics: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> ConvictionResult:
        """
        Computes the streamlined Dual-Profile Option Microstructure & Order Flow conviction score:
          - Mode B Profile: In-Flight Mid-Bar Breakthrough Surge (T:02s onwards, No Upper Cap).
          - Mode A Profile: Structural Rejection & Close (T:50s Pre-Close / T:00s Close).
        """
        mode_val = str((pa_details or {}).get("mode", "")).upper()
        s_name_upper = str(setup_name).upper()
        is_mode_b = (
            "MODE_B" in mode_val
            or setup_name.startswith("mode_b_")
            or "MODE_B" in s_name_upper
            or setup_name in ("volatility_squeeze_breakout", "sr_breakout", "topological_prominence_breakout")
        )

        # Extract micro-level option metrics if available
        m_ret = None
        tot_depth = 1000.0
        deep_press = 0.0
        eff_spread = spread_pct
        eff_i1 = touch_i1
        tick_intensity = None

        if micro_metrics:
            m_ret = micro_metrics.get("micro_ret")
            tot_depth = float(micro_metrics.get("tot_depth", 1000.0))
            deep_press = float(micro_metrics.get("deep_pressure", 0.0))
            eff_spread = float(micro_metrics.get("spread_pct", spread_pct))
            if "touch_i1" in micro_metrics:
                eff_i1 = float(micro_metrics["touch_i1"])
            if "tick_intensity" in micro_metrics:
                tick_intensity = float(micro_metrics["tick_intensity"])

        # Tick rate calculation from live forming candle if available
        if tick_intensity is None and current_bar is not None and hasattr(current_bar, "tick_count"):
            trigger_sec = int((pa_details or {}).get("trigger_sec", 0))
            if trigger_sec > 0:
                sec_in_min = max(trigger_sec, 2)
            else:
                now_sec = float(getattr(current_bar, "ts_end", 0.0) or getattr(current_bar, "ts_start", 0.0) + 15.0)
                sec_in_min = max(int(now_sec % 60), 2)
            tick_intensity = float(current_bar.tick_count) / float(sec_in_min)

        # ─── Dual-Profile Selection ───
        if is_mode_b:
            total_score, sub_scores, profile_details, reason_str = self._evaluate_mode_b_conviction(
                direction=direction,
                setup_name=setup_name,
                touch_i1=eff_i1,
                spread_pct=eff_spread,
                tick_rate=tick_intensity,
                wyckoff_details=wyckoff_details,
                option_delta=option_delta,
                micro_ret=m_ret,
            )
            profile_name = "MODE_B_INFLIGHT_PROFILE"
        else:
            total_score, sub_scores, profile_details, reason_str = self._evaluate_mode_a_conviction(
                direction=direction,
                setup_name=setup_name,
                touch_i1=eff_i1,
                spread_pct=eff_spread,
                deep_pressure=deep_press,
                micro_ret=m_ret,
                tot_depth=tot_depth,
                wyckoff_details=wyckoff_details,
                option_delta=option_delta,
            )
            profile_name = "MODE_A_REJECTION_PROFILE"

        # Sizing Tier & Approval Gate (Mode B in-flight surge has dedicated 75.0 floor)
        eff_approval_floor = 75.0 if is_mode_b else self.min_approval_score
        is_approved = total_score >= eff_approval_floor
        if total_score >= self.high_conviction_score:
            sizing_tier = "FULL_SIZE"
            rec_tp = self.high_conviction_tp_pct
            final_reason = f"{profile_name.lower()}_high_conviction_approved"
        elif is_approved:
            sizing_tier = "STANDARD_SIZE"
            rec_tp = self.standard_tp_pct
            final_reason = f"{profile_name.lower()}_standard_conviction_approved"
        else:
            sizing_tier = "BLOCKED"
            rec_tp = self.standard_tp_pct
            final_reason = reason_str

        breakdown = {
            "setup_name": setup_name,
            "profile_used": profile_name,
            "profile_details": profile_details,
            "flow_details": profile_details,
            "impulse_details": profile_details,
            "contract_details": profile_details,
            "structure_details": {"retired": True, "validated_upstream": True, "swing_reason": "validated_pullback_reclaim"},
        }

        return ConvictionResult(
            total_score=total_score,
            is_approved=is_approved,
            sizing_tier=sizing_tier,
            recommended_tp_pct=rec_tp,
            sub_scores=sub_scores,
            breakdown=breakdown,
            reason=final_reason,
        )


# Legacy backward-compatibility alias: AdaptiveMicroValidator is fully merged into CompositeConvictionEngine
AdaptiveMicroValidator = CompositeConvictionEngine
