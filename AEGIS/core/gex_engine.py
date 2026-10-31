"""AEGIS GEX (Gamma Exposure) & Net Gamma Engine — Zero-Latency Shadow Mode.

Vectorized Black-Scholes Gamma, Static vs Flow-Adjusted Dealer GEX, and O(1)
Shadow Observability for Indian Index Options (NIFTY & SENSEX).

Key Microstructure Principles:
1. Pure Vectorized NumPy Black-Scholes Gamma: Pre-allocated contiguous float64
   buffers, zero heap churn in runtime, zero scipy/pandas imports on hot path.
2. Dual GEX Profiles:
   - Static GEX: Standard dealer assumption (CE_OI - PE_OI) * Gamma * Scale.
   - Flow-Signed GEX: Adjusts dealer sign (-1.0 short gamma vs +1.0 long gamma)
     based on Lee-Ready & touch-first classified order flow per strike.
3. Extracted O(1) Scalars:
   - net_gex: Net session dealer gamma exposure across the 51-strike window.
   - gex_regime: POSITIVE_MEAN_REVERT (net > 0) vs NEGATIVE_MOMENTUM (net < 0).
   - pos_gex_wall: Strike with maximum positive GEX (mean-reversion / pin wall).
   - neg_gex_wall: Strike with maximum negative GEX (momentum acceleration zone).
   - zero_gamma_flip: Linearly interpolated spot level where cumulative GEX flips sign.
4. STRICT SHADOW INVARIANT: Computes in background and attaches telemetry tags.
   NEVER blocks, delays, or alters live order execution in Decision Lifecycle.
"""

from __future__ import annotations

import math
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

try:
    from logger import get_logger
    logger = get_logger("aegis_gex_engine")
except Exception:
    logger = logging.getLogger("aegis_gex_engine")

INV_SQRT_2PI: float = 0.3989422804014327  # 1.0 / math.sqrt(2.0 * math.pi)


class GexShadowEngine:
    """Zero-allocation Vectorized GEX Shadow Engine for AEGIS.
    
    Tracks 51 strikes (ATM +- 25) using pre-allocated C-contiguous float64 arrays.
    """

    __slots__ = (
        "num_strikes",
        "strike_step",
        "lot_size",
        "strikes",
        "ce_oi",
        "pe_oi",
        "iv",
        "mm_ce_sign",
        "mm_pe_sign",
        "ce_buy_vol",
        "ce_sell_vol",
        "pe_buy_vol",
        "pe_sell_vol",
        "_vol_t",
        "_d1",
        "_gamma",
        "_scratch",
        "_oi_diff",
        "_signed_ce",
        "_signed_pe",
        "_signed_oi",
        "static_gex_profile",
        "flow_gex_profile",
        "_cum_gex",
        "_signbits",
        "strike_to_idx",
        "_strikes_tuple",
        "net_gex",
        "gex_regime",
        "pos_wall_strike",
        "neg_wall_strike",
        "zero_gamma_level",
        "static_net_gex",
        "static_pos_wall",
        "static_neg_wall",
        "flow_pos_wall",
        "flow_neg_wall",
        "call_pos_wall",
        "put_pos_wall",
        "last_spot",
        "last_calc_ts",
        "_oi_dirty",
    )

    def __init__(
        self,
        num_strikes: int = 51,
        strike_step: float = 50.0,
        lot_size: float = 65.0,
        atm_strike: float = 0.0,
    ) -> None:
        self.num_strikes = int(num_strikes)
        self.strike_step = float(strike_step)
        self.lot_size = float(lot_size)

        # ── Pre-allocated contiguous C-buffers for zero heap churn ──
        self.strikes = np.zeros(self.num_strikes, dtype=np.float64)
        self.ce_oi = np.zeros(self.num_strikes, dtype=np.float64)
        self.pe_oi = np.zeros(self.num_strikes, dtype=np.float64)
        self.iv = np.full(self.num_strikes, 0.15, dtype=np.float64)

        # Dealer positioning sign: +1.0 if MM is Long Gamma, -1.0 if MM is Short Gamma
        # Default naive assumption: MMs long calls (+1.0) and short puts (-1.0)
        self.mm_ce_sign = np.ones(self.num_strikes, dtype=np.float64)
        self.mm_pe_sign = np.full(self.num_strikes, -1.0, dtype=np.float64)

        # Per-strike cumulative aggressive order flow tracking
        self.ce_buy_vol = np.zeros(self.num_strikes, dtype=np.float64)
        self.ce_sell_vol = np.zeros(self.num_strikes, dtype=np.float64)
        self.pe_buy_vol = np.zeros(self.num_strikes, dtype=np.float64)
        self.pe_sell_vol = np.zeros(self.num_strikes, dtype=np.float64)

        # Pre-allocated scratchpad buffers for in-place math (zero runtime allocations)
        self._vol_t = np.zeros(self.num_strikes, dtype=np.float64)
        self._d1 = np.zeros(self.num_strikes, dtype=np.float64)
        self._gamma = np.zeros(self.num_strikes, dtype=np.float64)
        self._scratch = np.zeros(self.num_strikes, dtype=np.float64)
        self._oi_diff = np.zeros(self.num_strikes, dtype=np.float64)
        self._signed_ce = np.zeros(self.num_strikes, dtype=np.float64)
        self._signed_pe = np.zeros(self.num_strikes, dtype=np.float64)
        self._signed_oi = np.zeros(self.num_strikes, dtype=np.float64)
        self.static_gex_profile = np.zeros(self.num_strikes, dtype=np.float64)
        self.flow_gex_profile = np.zeros(self.num_strikes, dtype=np.float64)
        self._cum_gex = np.zeros(self.num_strikes, dtype=np.float64)
        self._signbits = np.zeros(self.num_strikes, dtype=bool)

        # Fast O(1) strike-to-index lookup
        self.strike_to_idx: Dict[float, int] = {}
        self._strikes_tuple: Tuple[float, ...] = tuple(float(s) for s in self.strikes)

        # O(1) Scalar outputs for Decision Lifecycle read
        self.net_gex = 0.0
        self.gex_regime = "POSITIVE_MEAN_REVERT"
        self.pos_wall_strike = 0.0
        self.neg_wall_strike = 0.0
        self.zero_gamma_level = 0.0
        self.static_net_gex = 0.0
        self.static_pos_wall = 0.0
        self.static_neg_wall = 0.0
        self.flow_pos_wall = 0.0
        self.flow_neg_wall = 0.0
        self.call_pos_wall = 0.0
        self.put_pos_wall = 0.0
        self.last_spot = 0.0
        self.last_calc_ts = 0.0
        self._oi_dirty = False

        if atm_strike > 0.0:
            self.align_strikes(atm_strike)

    def align_strikes(self, atm_strike: float) -> None:
        """Aligns 51 strikes centered at ATM +- 25 strikes."""
        if atm_strike <= 0.0:
            return
        half = self.num_strikes // 2
        new_strikes = [float(atm_strike + (i - half) * self.strike_step) for i in range(self.num_strikes)]
        
        # Build new map
        new_strike_to_idx = {stk: i for i, stk in enumerate(new_strikes)}

        # Remap existing OI and flow buffers where strikes overlap
        new_ce_oi = np.zeros(self.num_strikes, dtype=np.float64)
        new_pe_oi = np.zeros(self.num_strikes, dtype=np.float64)
        new_mm_ce_sign = np.ones(self.num_strikes, dtype=np.float64)
        new_mm_pe_sign = np.full(self.num_strikes, -1.0, dtype=np.float64)
        new_ce_buy = np.zeros(self.num_strikes, dtype=np.float64)
        new_ce_sell = np.zeros(self.num_strikes, dtype=np.float64)
        new_pe_buy = np.zeros(self.num_strikes, dtype=np.float64)
        new_pe_sell = np.zeros(self.num_strikes, dtype=np.float64)

        if self.strike_to_idx:
            for stk, new_idx in new_strike_to_idx.items():
                old_idx = self.strike_to_idx.get(stk)
                if old_idx is not None:
                    new_ce_oi[new_idx] = self.ce_oi[old_idx]
                    new_pe_oi[new_idx] = self.pe_oi[old_idx]
                    new_mm_ce_sign[new_idx] = self.mm_ce_sign[old_idx]
                    new_mm_pe_sign[new_idx] = self.mm_pe_sign[old_idx]
                    new_ce_buy[new_idx] = self.ce_buy_vol[old_idx]
                    new_ce_sell[new_idx] = self.ce_sell_vol[old_idx]
                    new_pe_buy[new_idx] = self.pe_buy_vol[old_idx]
                    new_pe_sell[new_idx] = self.pe_sell_vol[old_idx]

        for i, s in enumerate(new_strikes):
            self.strikes[i] = s
        self.ce_oi[:] = new_ce_oi
        self.pe_oi[:] = new_pe_oi
        self.mm_ce_sign[:] = new_mm_ce_sign
        self.mm_pe_sign[:] = new_mm_pe_sign
        self.ce_buy_vol[:] = new_ce_buy
        self.ce_sell_vol[:] = new_ce_sell
        self.pe_buy_vol[:] = new_pe_buy
        self.pe_sell_vol[:] = new_pe_sell
        self.strike_to_idx = new_strike_to_idx
        self._strikes_tuple = tuple(new_strikes)
        self._oi_dirty = True

    def set_strikes(self, strike_list: Sequence[float]) -> None:
        """Sets custom strikes from universe (pads/extrapolates to num_strikes, sorted).
        
        Guarantees that all num_strikes array elements are strictly positive, valid,
        sorted strikes. Completely eliminates zero-division in vectorized BS-gamma math.
        """
        sorted_stks = sorted(list({float(s) for s in strike_list if float(s) > 0.0}))
        n = len(sorted_stks)
        if n == 0:
            return

        step = self.strike_step
        if n >= 2:
            diffs = [sorted_stks[i + 1] - sorted_stks[i] for i in range(min(5, n - 1))]
            pos_diffs = [d for d in diffs if d > 0]
            if pos_diffs:
                step = float(min(pos_diffs))

        if n < self.num_strikes:
            needed = self.num_strikes - n
            needed_before = needed // 2
            needed_after = needed - needed_before
            before = [float(sorted_stks[0] - (needed_before - i) * step) for i in range(needed_before)]
            after = [float(sorted_stks[-1] + (i + 1) * step) for i in range(needed_after)]
            full_stks = before + sorted_stks + after
        elif n > self.num_strikes:
            start = (n - self.num_strikes) // 2
            full_stks = sorted_stks[start : start + self.num_strikes]
        else:
            full_stks = sorted_stks

        new_strike_to_idx = {stk: i for i, stk in enumerate(full_stks)}

        # Remap existing OI and flow buffers where strikes overlap
        new_ce_oi = np.zeros(self.num_strikes, dtype=np.float64)
        new_pe_oi = np.zeros(self.num_strikes, dtype=np.float64)
        new_mm_ce_sign = np.ones(self.num_strikes, dtype=np.float64)
        new_mm_pe_sign = np.full(self.num_strikes, -1.0, dtype=np.float64)
        new_ce_buy = np.zeros(self.num_strikes, dtype=np.float64)
        new_ce_sell = np.zeros(self.num_strikes, dtype=np.float64)
        new_pe_buy = np.zeros(self.num_strikes, dtype=np.float64)
        new_pe_sell = np.zeros(self.num_strikes, dtype=np.float64)

        if self.strike_to_idx:
            for stk, new_idx in new_strike_to_idx.items():
                old_idx = self.strike_to_idx.get(stk)
                if old_idx is not None:
                    new_ce_oi[new_idx] = self.ce_oi[old_idx]
                    new_pe_oi[new_idx] = self.pe_oi[old_idx]
                    new_mm_ce_sign[new_idx] = self.mm_ce_sign[old_idx]
                    new_mm_pe_sign[new_idx] = self.mm_pe_sign[old_idx]
                    new_ce_buy[new_idx] = self.ce_buy_vol[old_idx]
                    new_ce_sell[new_idx] = self.ce_sell_vol[old_idx]
                    new_pe_buy[new_idx] = self.pe_buy_vol[old_idx]
                    new_pe_sell[new_idx] = self.pe_sell_vol[old_idx]

        for i, s in enumerate(full_stks):
            self.strikes[i] = s
        self.ce_oi[:] = new_ce_oi
        self.pe_oi[:] = new_pe_oi
        self.mm_ce_sign[:] = new_mm_ce_sign
        self.mm_pe_sign[:] = new_mm_pe_sign
        self.ce_buy_vol[:] = new_ce_buy
        self.ce_sell_vol[:] = new_ce_sell
        self.pe_buy_vol[:] = new_pe_buy
        self.pe_sell_vol[:] = new_pe_sell
        self.strike_to_idx = new_strike_to_idx
        self._strikes_tuple = tuple(full_stks)
        self._oi_dirty = True

    def set_iv(self, iv_val: float) -> None:
        """Sets constant IV across all strikes in O(1) time."""
        if iv_val > 0.0:
            self.iv.fill(float(iv_val))
            self._oi_dirty = True

    def update_iv(self, strike: float, iv_val: float) -> None:
        """Updates IV for an individual strike in O(1) time."""
        if iv_val <= 0.0 or strike <= 0.0:
            return
        idx = self.strike_to_idx.get(strike)
        if idx is not None and 0 <= idx < self.num_strikes:
            self.iv[idx] = float(iv_val)
            self._oi_dirty = True

    def update_oi(self, strike: float, side: int, oi: float) -> None:
        """Updates open interest for a strike in O(1) time.
        
        Args:
            strike: Strike price
            side: 0 for Call (CE), 1 for Put (PE)
            oi: Open interest volume
        """
        if oi <= 0.0 or strike <= 0.0:
            return
        idx = self.strike_to_idx.get(strike)
        if idx is None:
            if self.strikes[0] > 0.0 and self.strike_step > 0.0:
                calc_idx = int(round((strike - self.strikes[0]) / self.strike_step))
                if 0 <= calc_idx < self.num_strikes and abs(self.strikes[calc_idx] - strike) < 1e-4:
                    idx = calc_idx
        if idx is not None and 0 <= idx < self.num_strikes:
            if side == 0:
                self.ce_oi[idx] = float(oi)
            elif side == 1:
                self.pe_oi[idx] = float(oi)
            self._oi_dirty = True

    def update_order_flow(self, strike: float, side: int, buy_vol: float, sell_vol: float) -> None:
        """Integrates classified order flow per strike to adjust dealer sign.
        
        When aggressive retail buying dominates (buy_vol > sell_vol):
        - Call buying forces MMs to sell calls -> MM is Short Gamma (-1.0)
        - Put buying forces MMs to sell puts -> MM is Short Gamma (-1.0)
        When aggressive retail selling dominates (sell_vol > buy_vol):
        - Call selling forces MMs to buy calls -> MM is Long Gamma (+1.0)
        - Put selling forces MMs to buy puts -> MM is Long Gamma (+1.0)
        """
        if buy_vol <= 0.0 and sell_vol <= 0.0:
            return
        idx = self.strike_to_idx.get(strike)
        if idx is None:
            if self.strikes[0] > 0.0 and self.strike_step > 0.0:
                calc_idx = int(round((strike - self.strikes[0]) / self.strike_step))
                if 0 <= calc_idx < self.num_strikes and abs(self.strikes[calc_idx] - strike) < 1e-4:
                    idx = calc_idx
        if idx is None or not (0 <= idx < self.num_strikes):
            return

        if side == 0:  # CALL
            self.ce_buy_vol[idx] += buy_vol
            self.ce_sell_vol[idx] += sell_vol
            net_call_flow = self.ce_buy_vol[idx] - self.ce_sell_vol[idx]
            # MMs take counter-party position:
            if net_call_flow > 0.0:
                self.mm_ce_sign[idx] = -1.0  # MMs are Short Call -> Negative Gamma
            elif net_call_flow < 0.0:
                self.mm_ce_sign[idx] = 1.0   # MMs are Long Call -> Positive Gamma
            else:
                self.mm_ce_sign[idx] = 1.0   # Baseline naive assumption
        elif side == 1:  # PUT
            self.pe_buy_vol[idx] += buy_vol
            self.pe_sell_vol[idx] += sell_vol
            net_put_flow = self.pe_buy_vol[idx] - self.pe_sell_vol[idx]
            if net_put_flow > 0.0:
                self.mm_pe_sign[idx] = -1.0  # MMs are Short Put -> Negative Gamma
            elif net_put_flow < 0.0:
                self.mm_pe_sign[idx] = 1.0   # MMs are Long Put -> Positive Gamma
            else:
                self.mm_pe_sign[idx] = -1.0  # Baseline naive assumption
        self._oi_dirty = True

    def update_gex_snapshot(self, spot: float, time_to_expiry_yrs: float) -> None:
        """Pure Vectorized Black-Scholes Gamma & GEX computation.
        
        Zero heap allocations in steady state. Uses pre-allocated contiguous
        arrays and ndarray C methods.
        """
        if spot <= 0.0 or self.strikes[0] <= 0.0:
            return

        t_sqrt = math.sqrt(max(time_to_expiry_yrs, 1e-6))
        # _vol_t[:] = iv * t_sqrt
        np.multiply(self.iv, t_sqrt, out=self._vol_t)
        np.maximum(self._vol_t, 1e-6, out=self._vol_t)

        # In-place d1 calculation: d1 = [ln(spot/K) + 0.5 * iv^2 * T] / (iv * sqrt(T))
        np.divide(spot, self.strikes, out=self._d1)
        np.log(self._d1, out=self._d1)
        np.multiply(self.iv, self.iv, out=self._gamma)  # scratchpad
        np.multiply(self._gamma, 0.5 * time_to_expiry_yrs, out=self._gamma)
        np.add(self._d1, self._gamma, out=self._d1)
        np.divide(self._d1, self._vol_t, out=self._d1)

        # In-place Gamma calculation: Gamma = [INV_SQRT_2PI * exp(-0.5 * d1^2)] / (spot * vol_t)
        np.multiply(self._d1, self._d1, out=self._gamma)
        np.multiply(self._gamma, -0.5, out=self._gamma)
        np.exp(self._gamma, out=self._gamma)
        np.multiply(self._gamma, INV_SQRT_2PI, out=self._gamma)
        np.multiply(self._vol_t, spot, out=self._scratch)
        np.maximum(self._scratch, 1e-12, out=self._scratch)
        np.divide(self._gamma, self._scratch, out=self._gamma)

        # Scale factor: LotSize * Spot^2 * 0.01 (represents rupee exposure per 1% move)
        scale = self.lot_size * (spot * spot) * 0.01

        # 1. Static GEX: (CE_OI - PE_OI) * Gamma * Scale
        np.subtract(self.ce_oi, self.pe_oi, out=self._oi_diff)
        np.multiply(self._oi_diff, self._gamma, out=self.static_gex_profile)
        np.multiply(self.static_gex_profile, scale, out=self.static_gex_profile)

        # 2. Flow-Signed GEX: (DealerSign_CE * CE_OI + DealerSign_PE * PE_OI) * Gamma * Scale
        np.multiply(self.mm_ce_sign, self.ce_oi, out=self._signed_ce)
        np.multiply(self.mm_pe_sign, self.pe_oi, out=self._signed_pe)
        np.add(self._signed_ce, self._signed_pe, out=self._signed_oi)
        np.multiply(self._signed_oi, self._gamma, out=self.flow_gex_profile)
        np.multiply(self.flow_gex_profile, scale, out=self.flow_gex_profile)

        # Signed CE and PE for Call/Put wall decomposition
        np.multiply(self._signed_ce, self._gamma, out=self._signed_ce)
        np.multiply(self._signed_ce, scale, out=self._signed_ce)
        np.multiply(self._signed_pe, self._gamma, out=self._signed_pe)
        np.multiply(self._signed_pe, scale, out=self._signed_pe)

        # Single-pass scalar reduction and cumulative sum (zero allocations)
        c = 0.0
        static_sum = 0.0
        flow_max_val = -1e30
        flow_min_val = 1e30
        flow_max_idx = -1
        flow_min_idx = -1
        static_max_val = -1e30
        static_min_val = 1e30
        static_max_idx = -1
        static_min_idx = -1
        call_max_val = -1e30
        call_max_idx = -1
        put_max_val = -1e30
        put_max_idx = -1

        for i in range(self.num_strikes):
            fv = float(self.flow_gex_profile[i])
            sv = float(self.static_gex_profile[i])
            cv = float(self._signed_ce[i])
            pv = float(self._signed_pe[i])

            c += fv
            static_sum += sv
            self._cum_gex[i] = c

            if fv > flow_max_val:
                flow_max_val = fv
                flow_max_idx = i
            if fv < flow_min_val:
                flow_min_val = fv
                flow_min_idx = i
            if sv > static_max_val:
                static_max_val = sv
                static_max_idx = i
            if sv < static_min_val:
                static_min_val = sv
                static_min_idx = i
            if cv > call_max_val:
                call_max_val = cv
                call_max_idx = i
            if pv > put_max_val:
                put_max_val = pv
                put_max_idx = i

        self.net_gex = c
        self.static_net_gex = static_sum
        self.gex_regime = "POSITIVE_MEAN_REVERT" if self.net_gex >= 0.0 else "NEGATIVE_MOMENTUM"

        # Valid walls exist ONLY if strictly positive or strictly negative
        self.pos_wall_strike = self._strikes_tuple[flow_max_idx] if (flow_max_idx >= 0 and flow_max_val > 1e-4) else 0.0
        self.neg_wall_strike = self._strikes_tuple[flow_min_idx] if (flow_min_idx >= 0 and flow_min_val < -1e-4) else 0.0
        self.static_pos_wall = self._strikes_tuple[static_max_idx] if (static_max_idx >= 0 and static_max_val > 1e-4) else 0.0
        self.static_neg_wall = self._strikes_tuple[static_min_idx] if (static_min_idx >= 0 and static_min_val < -1e-4) else 0.0
        self.flow_pos_wall = self.pos_wall_strike
        self.flow_neg_wall = self.neg_wall_strike
        self.call_pos_wall = self._strikes_tuple[call_max_idx] if (call_max_idx >= 0 and call_max_val > 1e-4) else 0.0
        self.put_pos_wall = self._strikes_tuple[put_max_idx] if (put_max_idx >= 0 and put_max_val > 1e-4) else 0.0
        self.last_spot = spot

        # Zero Gamma Flip Level (strict linear interpolation where cumulative GEX flips sign)
        zero_flip = spot
        for i in range(self.num_strikes - 1):
            g1 = float(self._cum_gex[i])
            g2 = float(self._cum_gex[i + 1])
            # Strict sign change: one strictly positive, one strictly negative
            if (g1 > 1e-6 and g2 < -1e-6) or (g1 < -1e-6 and g2 > 1e-6):
                k1 = self._strikes_tuple[i]
                k2 = self._strikes_tuple[i + 1]
                denom = g2 - g1
                if abs(denom) > 1e-12:
                    zero_flip = k1 - g1 * (k2 - k1) / denom
                else:
                    zero_flip = (k1 + k2) * 0.5
                break
        self.zero_gamma_level = zero_flip

    def on_tick_throttled(
        self,
        spot: float,
        time_to_expiry_yrs: float,
        now_sec: float,
        min_interval_sec: float = 1.0,
    ) -> bool:
        """Throttled trigger for GEX snapshot update (1-to-3s cadence or on fresh OI).
        
        Zero Hot-Path Overhead: never blocks every tick.
        """
        if spot <= 0.0:
            return False
        if (now_sec - self.last_calc_ts) >= min_interval_sec or self._oi_dirty:
            self.update_gex_snapshot(spot, time_to_expiry_yrs)
            self.last_calc_ts = now_sec
            self._oi_dirty = False
            return True
        return False

    def evaluate_shadow_signal(
        self,
        spot: float,
        direction: str,
        pillar: int = 1,
        pa_details: Optional[Dict[str, Any]] = None,
        wall_proximity_pts: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Evaluates shadow rules for a candidate signal in O(1) time.
        
        STRICT SHADOW INVARIANT: Never vetoes or delays live execution.
        Returns a telemetry dictionary to enrich signal/trade payload.
        """
        spot_eff = spot if spot > 0.0 else self.last_spot
        side = "CALL" if str(direction).upper() in ("UP", "CALL", "CE") else "PUT"

        # Scale-invariant proximity threshold: 0.5 * strike_step (~25 pts NIFTY, ~50 pts SENSEX) or 10 bps
        prox_threshold = (
            float(wall_proximity_pts)
            if wall_proximity_pts is not None and wall_proximity_pts > 0.0
            else max(0.5 * self.strike_step, 0.0010 * spot_eff)
        )

        pos_wall = self.pos_wall_strike
        neg_wall = self.neg_wall_strike
        call_wall = self.call_pos_wall
        put_wall = self.put_pos_wall

        # Distance calculation
        dist_to_pos_wall = 999999.0
        if side == "CALL":
            # Resistance wall: prefer call_wall if present, else fallback to pos_wall if above spot and no distinct walls
            eff_pos = call_wall if call_wall > 0.0 else (pos_wall if (pos_wall > 0.0 and pos_wall >= spot_eff and put_wall <= 0.0) else 0.0)
            if eff_pos > 0.0:
                dist_to_pos_wall = eff_pos - spot_eff  # approaching resistance from below
        else:
            # Support wall: prefer put_wall if present, else fallback to pos_wall if below spot and no distinct walls
            eff_pos = put_wall if put_wall > 0.0 else (pos_wall if (pos_wall > 0.0 and pos_wall <= spot_eff and call_wall <= 0.0) else 0.0)
            if eff_pos > 0.0:
                dist_to_pos_wall = spot_eff - eff_pos  # approaching support from above

        dist_to_neg_wall = abs(spot_eff - neg_wall) if neg_wall > 0.0 else 999999.0

        # ── Shadow Rule Evaluation ──
        # 1. Pos Wall Pinning / Resistance: MMs hedge against move (selling futures into rally / buying at support)
        # 2. Neg Wall Breakout / Acceleration: MMs hedge with move (buying futures into rally / selling into dump)
        shadow_verdict = "NEUTRAL_PASS"
        if dist_to_pos_wall < 999900.0 and 0.0 <= dist_to_pos_wall <= prox_threshold:
            shadow_verdict = "SHADOW_VETO_POS_WALL"
        elif dist_to_neg_wall < 999900.0 and dist_to_neg_wall <= prox_threshold:
            shadow_verdict = "SHADOW_AMPLIFY_NEG_ZONE"

        # 3. Regime Alignment Check:
        # Pullback/Reversal (P1 20 EMA, P2 Sweep Reclaim) align with POSITIVE_MEAN_REVERT
        # Breakout/Momentum (P3 Squeeze Breakout, P4 Prominence Breakout) align with NEGATIVE_MOMENTUM
        is_breakout_pillar = pillar in (3, 4)
        is_reversal_pillar = pillar in (1, 2)
        if self.gex_regime == "POSITIVE_MEAN_REVERT":
            regime_alignment = "ALIGNED_RANGE" if is_reversal_pillar else "MISMATCH_RANGE_BREAKOUT"
        else:
            regime_alignment = "ALIGNED_TREND" if is_breakout_pillar else "MISMATCH_TREND_FADE"

        if "POS" in shadow_verdict and dist_to_pos_wall < 999900.0:
            dist_eff = dist_to_pos_wall
        elif "NEG" in shadow_verdict and dist_to_neg_wall < 999900.0:
            dist_eff = dist_to_neg_wall
        else:
            d_min = min(dist_to_pos_wall, dist_to_neg_wall)
            dist_eff = d_min if d_min < 999900.0 else 0.0

        return {
            "shadow_verdict": shadow_verdict,
            "net_gex": self.net_gex,
            "gex_regime": self.gex_regime,
            "pos_gex_wall": self.pos_wall_strike,
            "neg_gex_wall": self.neg_wall_strike,
            "static_pos_wall": self.static_pos_wall,
            "static_neg_wall": self.static_neg_wall,
            "flow_pos_wall": self.flow_pos_wall,
            "flow_neg_wall": self.flow_neg_wall,
            "call_pos_wall": self.call_pos_wall,
            "put_pos_wall": self.put_pos_wall,
            "zero_gamma_flip": self.zero_gamma_level,
            "dist_to_pos_wall": round(dist_to_pos_wall, 2) if dist_to_pos_wall < 999900.0 else 0.0,
            "dist_to_neg_wall": round(dist_to_neg_wall, 2) if dist_to_neg_wall < 999900.0 else 0.0,
            "dist_to_wall": round(dist_eff, 2),
            "regime_alignment": regime_alignment,
            "wall_proximity_pts": round(prox_threshold, 2),
        }
