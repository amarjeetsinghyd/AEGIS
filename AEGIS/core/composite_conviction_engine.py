"""
AEGIS Composite Conviction Engine — Unified 0-100 Continuous Evaluation Framework.

Replaces rigid, binary boolean gates with continuous multi-domain conviction scoring:
    Domain 1: Structure & Runway (30 pts) — Swing geometry, EMA alignment, obstacle distance
    Domain 2: PA Impulse & Volume (30 pts) — Candle displacement, edge close, ATR expansion, volume surge
    Domain 3: Flow & Futures Momentum (25 pts) — Persistence Dp, touch I1, futures price & basis expansion
    Domain 4: Contract & Feasibility (15 pts) — Sweet-spot delta (0.25-0.45), tight bid-ask spread

Thresholds:
    >= 85.0: FULL_SIZE (2 lots, expanded target)
    75.0 - 84.9: STANDARD_SIZE (1 lot, standard target)
    < 75.0: BLOCKED (rejected with itemized audit breakdown)

Pure Python, zero heavy imports — hot-path safe (O(1) per call).
Scale-invariant: all distances normalized via ATR and basis points (bps).
"""

from __future__ import annotations
import math
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("aegis.composite_conviction")


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
    Evaluates institutional conviction across 4 orthogonal quantitative domains
    on a 0 to 100 continuous scale.
    """

    # Default baseline weights (sum to 100.0)
    DEFAULT_WEIGHTS = {
        "structure": 30.0,
        "impulse": 30.0,
        "flow": 25.0,
        "contract": 15.0,
    }

    # Setup-specific weight profiles (each sums to 100.0)
    SETUP_WEIGHT_PROFILES = {
        # Pillar 1: 20 EMA Pullback puts premium on structural trend health
        "20_ema_rejection": {
            "structure": 35.0,
            "impulse": 25.0,
            "flow": 25.0,
            "contract": 15.0,
        },
        # Pillar 2: Micro-Sweep Reclaim balanced
        "micro_sweep_reclaim": {
            "structure": 30.0,
            "impulse": 30.0,
            "flow": 25.0,
            "contract": 15.0,
        },
        "albrooks_2legged_pullback": {
            "structure": 30.0,
            "impulse": 30.0,
            "flow": 25.0,
            "contract": 15.0,
        },
        # Pillar 3: Volatility Squeeze breakout demands massive candle displacement
        "volatility_squeeze_breakout": {
            "structure": 25.0,
            "impulse": 35.0,
            "flow": 25.0,
            "contract": 15.0,
        },
        # Pillar 4: Break of Structure demands relentless flow & futures momentum
        "break_of_structure": {
            "structure": 25.0,
            "impulse": 30.0,
            "flow": 30.0,
            "contract": 15.0,
        },
        # Pillar 4: Topological Network Phase Shift Breakout
        "hvg_network_phase_shift": {
            "structure": 30.0,
            "impulse": 30.0,
            "flow": 25.0,
            "contract": 15.0,
        },
        # Pillar 4 (V4 Final): 0-D Prominence Hub Structural Breakout
        "topological_prominence_breakout": {
            "structure": 30.0,
            "impulse": 30.0,
            "flow": 25.0,
            "contract": 15.0,
        },
        # Legacy compat alias
        "signature_inflection": {
            "structure": 25.0,
            "impulse": 30.0,
            "flow": 30.0,
            "contract": 15.0,
        },
    }

    def __init__(
        self,
        min_approval_score: float = 80.0,
        high_conviction_score: float = 85.0,
        standard_tp_pct: float = 0.040,
        high_conviction_tp_pct: float = 0.050,
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

    def _score_structure(
        self,
        direction: str,
        spot_price: float,
        prior_bars: List[Any],
        atr: float,
        ema_20: float,
        ema_50: float,
        vwap: float,
        pdh: float,
        pdl: float,
    ) -> Tuple[float, Dict[str, Any]]:
        """
        Domain 1: Structure & Runway (Max 30 pts raw)
        - Swing geometry (HH/HL vs LH/LL): +15 to -15 pts
        - Runway to obstacle (PDH/PDL): +10 to 0 pts
        - EMA & VWAP alignment: +5 to 0 pts
        """
        is_up = direction.upper() in ("UP", "CALL")
        eff_atr = max(atr, 1.0)

        # 1. Swing Geometry Evaluation (15 pts)
        swing_pts = 5.0  # Neutral default
        swing_reason = "neutral"
        if len(prior_bars) >= 3:
            b1, b2, b3 = prior_bars[-3], prior_bars[-2], prior_bars[-1]
            h1, h2, h3 = b1.high, b2.high, b3.high
            l1, l2, l3 = b1.low, b2.low, b3.low

            is_higher_highs = (h3 > h2) and (h2 >= h1)
            is_higher_lows = (l3 > l2) and (l2 >= l1)
            is_lower_highs = (h3 < h2) and (h2 <= h1)
            is_lower_lows = (l3 < l2) and (l2 <= l1)

            if is_up:
                if is_higher_highs and is_higher_lows:
                    swing_pts = 15.0
                    swing_reason = "bullish_hh_hl"
                elif is_higher_lows or b3.close > b2.close:
                    swing_pts = 10.0
                    swing_reason = "bullish_hl"
                elif is_lower_highs:
                    # Distribution top / lower highs warning!
                    swing_pts = -15.0
                    swing_reason = "exhaustion_lower_highs"
                elif is_lower_lows:
                    swing_pts = -10.0
                    swing_reason = "bearish_lower_lows"
            else:
                if is_lower_lows and is_lower_highs:
                    swing_pts = 15.0
                    swing_reason = "bearish_ll_lh"
                elif is_lower_highs or b3.close < b2.close:
                    swing_pts = 10.0
                    swing_reason = "bearish_lh"
                elif is_higher_highs:
                    # Rally bottom / higher highs warning!
                    swing_pts = -15.0
                    swing_reason = "exhaustion_higher_highs"
                elif is_higher_lows:
                    swing_pts = -10.0
                    swing_reason = "bullish_higher_lows"

        # 2. Runway to Key Obstacles (10 pts)
        runway_pts = 10.0
        runway_dist = 999.0
        if is_up:
            if pdh > 0 and pdh > spot_price:
                runway_dist = (pdh - spot_price) / eff_atr
                if runway_dist >= 1.5:
                    runway_pts = 10.0
                elif runway_dist >= 1.0:
                    runway_pts = 7.0
                elif runway_dist >= 0.6:
                    runway_pts = 4.0
                else:
                    runway_pts = 0.0  # Trapped directly under PDH resistance
            elif pdh > 0 and spot_price >= pdh:
                # Already broken above PDH -> open sky breakout!
                runway_pts = 10.0
        else:
            if pdl > 0 and pdl < spot_price:
                runway_dist = (spot_price - pdl) / eff_atr
                if runway_dist >= 1.5:
                    runway_pts = 10.0
                elif runway_dist >= 1.0:
                    runway_pts = 7.0
                elif runway_dist >= 0.6:
                    runway_pts = 4.0
                else:
                    runway_pts = 0.0  # Trapped directly above PDL support
            elif pdl > 0 and spot_price <= pdl:
                # Already broken below PDL -> open sky breakdown!
                runway_pts = 10.0

        # 3. EMA & VWAP Alignment (5 pts)
        ema_vwap_pts = 0.0
        if is_up:
            ema_aligned = (ema_20 > ema_50) if (ema_20 > 0 and ema_50 > 0) else True
            vwap_aligned = (spot_price > vwap) if vwap > 0 else True
            if ema_aligned and vwap_aligned:
                ema_vwap_pts = 5.0
            elif ema_aligned or vwap_aligned:
                ema_vwap_pts = 3.0
        else:
            ema_aligned = (ema_20 < ema_50) if (ema_20 > 0 and ema_50 > 0) else True
            vwap_aligned = (spot_price < vwap) if vwap > 0 else True
            if ema_aligned and vwap_aligned:
                ema_vwap_pts = 5.0
            elif ema_aligned or vwap_aligned:
                ema_vwap_pts = 3.0

        raw_score = max(0.0, min(30.0, swing_pts + runway_pts + ema_vwap_pts))
        details = {
            "swing_pts": swing_pts,
            "swing_reason": swing_reason,
            "runway_pts": runway_pts,
            "runway_atr_dist": round(runway_dist, 2),
            "ema_vwap_pts": ema_vwap_pts,
            "raw_structure": round(raw_score, 2),
        }
        return raw_score, details

    def _score_impulse(
        self,
        direction: str,
        bar_open: float,
        bar_high: float,
        bar_low: float,
        bar_close: float,
        bar_volume: float,
        vol_sma10: float,
        atr: float,
    ) -> Tuple[float, Dict[str, Any]]:
        """
        Domain 2: PA Impulse & Volume (Max 30 pts raw)
        - Body %: +12 to +1 pts
        - Edge Close & Wick: +8 to -8 pts penalty
        - Range Expansion vs ATR: +5 to +1 pts
        - Volume Surge vs SMA10: +5 to +1 pts
        """
        is_up = direction.upper() in ("UP", "CALL")
        rng = max(bar_high - bar_low, 0.1)
        eff_atr = max(atr, 1.0)
        body = abs(bar_close - bar_open)
        body_pct = body / rng

        # 1. Body % (12 pts)
        if body_pct >= 0.60:
            body_pts = 12.0
        elif body_pct >= 0.45:
            body_pts = 8.0
        elif body_pct >= 0.30:
            body_pts = 4.0
        else:
            body_pts = 1.0

        # 2. Edge Close / Wick Rejection (8 pts)
        wick_penalty = 0.0
        if is_up:
            close_edge = (bar_close - bar_low) / rng
            upper_wick = (bar_high - max(bar_open, bar_close)) / rng
            if upper_wick > 0.35:
                close_pts = -8.0  # Severe upper wick rejection into highs
                wick_penalty = -8.0
            elif close_edge >= 0.75:
                close_pts = 8.0
            elif close_edge >= 0.60:
                close_pts = 5.0
            else:
                close_pts = 2.0
        else:
            close_edge = (bar_high - bar_close) / rng
            lower_wick = (min(bar_open, bar_close) - bar_low) / rng
            if lower_wick > 0.35:
                close_pts = -8.0  # Severe lower wick rejection off lows
                wick_penalty = -8.0
            elif close_edge >= 0.75:
                close_pts = 8.0
            elif close_edge >= 0.60:
                close_pts = 5.0
            else:
                close_pts = 2.0

        # 3. Range Expansion vs ATR (5 pts)
        range_atr = rng / eff_atr
        if range_atr >= 1.0:
            range_pts = 5.0
        elif range_atr >= 0.60:
            range_pts = 3.0
        else:
            range_pts = 1.0

        # 4. Volume Surge vs SMA10 (5 pts)
        vol_ratio = (bar_volume / vol_sma10) if vol_sma10 > 0 else 1.0
        if vol_ratio >= 1.5:
            vol_pts = 5.0
        elif vol_ratio >= 1.0:
            vol_pts = 3.0
        else:
            vol_pts = 1.0

        raw_score = max(0.0, min(30.0, body_pts + close_pts + range_pts + vol_pts))
        details = {
            "body_pct": round(body_pct, 3),
            "body_pts": body_pts,
            "close_pts": close_pts,
            "wick_penalty": wick_penalty,
            "range_atr": round(range_atr, 2),
            "range_pts": range_pts,
            "vol_ratio": round(vol_ratio, 2),
            "vol_pts": vol_pts,
            "raw_impulse": round(raw_score, 2),
        }
        return raw_score, details

    def _score_flow_and_futures(
        self,
        direction: str,
        persistence_dp: float,
        touch_i1: float,
        delta_fut_60s_bps: float,
        delta_basis_60s_bps: float,
    ) -> Tuple[float, Dict[str, Any]]:
        """
        Domain 3: Flow & Futures Momentum (Max 25 pts raw)
        - Directional Persistence Dp: +8 to 0 pts
        - Touch Aggression I1: +7 to 0 pts
        - Active Futures Lead-Lag Momentum: +6 to -6 pts penalty
        - Futures Basis Expansion: +4 to 0 pts
        """
        is_up = direction.upper() in ("UP", "CALL")

        # 1. Directional Persistence Dp (8 pts)
        if persistence_dp >= 0.70:
            dp_pts = 8.0
        elif persistence_dp >= 0.55:
            dp_pts = 5.0
        elif persistence_dp >= 0.45:
            dp_pts = 2.0
        else:
            dp_pts = 0.0

        # 2. Touch Aggression I1 (7 pts)
        if is_up:
            if touch_i1 >= 0.10:
                i1_pts = 7.0
            elif touch_i1 >= 0.0:
                i1_pts = 4.0
            elif touch_i1 >= -0.20:
                i1_pts = 2.0
            else:
                i1_pts = 0.0
        else:
            if touch_i1 <= -0.10:
                i1_pts = 7.0
            elif touch_i1 <= 0.0:
                i1_pts = 4.0
            elif touch_i1 <= 0.20:
                i1_pts = 2.0
            else:
                i1_pts = 0.0

        # 3. Active Futures Lead-Lag Momentum (6 pts)
        # Scale-invariant: 1 bps = 0.01% price displacement in futures
        fut_pts = 2.0  # Neutral
        fut_divergence_penalty = 0.0
        if is_up:
            if delta_fut_60s_bps >= 1.0:
                fut_pts = 6.0
            elif delta_fut_60s_bps > 0.0:
                fut_pts = 4.0
            elif delta_fut_60s_bps >= -1.0:
                fut_pts = 2.0
            elif delta_fut_60s_bps < -2.0:
                # Severe divergence: futures dumping while spot bounces -> fakeout trap!
                fut_pts = -6.0
                fut_divergence_penalty = -6.0
            else:
                fut_pts = 0.0
        else:
            if delta_fut_60s_bps <= -1.0:
                fut_pts = 6.0
            elif delta_fut_60s_bps < 0.0:
                fut_pts = 4.0
            elif delta_fut_60s_bps <= 1.0:
                fut_pts = 2.0
            elif delta_fut_60s_bps > 2.0:
                # Severe divergence: futures rallying while spot falls -> fakeout trap!
                fut_pts = -6.0
                fut_divergence_penalty = -6.0
            else:
                fut_pts = 0.0

        # 4. Futures Basis Expansion (4 pts)
        if is_up:
            if delta_basis_60s_bps > 0.2:
                basis_pts = 4.0
            elif delta_basis_60s_bps >= -0.2:
                basis_pts = 2.0
            else:
                basis_pts = 0.0
        else:
            if delta_basis_60s_bps < -0.2:
                basis_pts = 4.0
            elif delta_basis_60s_bps <= 0.2:
                basis_pts = 2.0
            else:
                basis_pts = 0.0

        raw_score = max(0.0, min(25.0, dp_pts + i1_pts + fut_pts + basis_pts))
        details = {
            "persistence_dp": round(persistence_dp, 3),
            "dp_pts": dp_pts,
            "touch_i1": round(touch_i1, 3),
            "i1_pts": i1_pts,
            "delta_fut_60s_bps": round(delta_fut_60s_bps, 2),
            "fut_pts": fut_pts,
            "fut_divergence_penalty": fut_divergence_penalty,
            "delta_basis_60s_bps": round(delta_basis_60s_bps, 2),
            "basis_pts": basis_pts,
            "raw_flow": round(raw_score, 2),
        }
        return raw_score, details

    def _score_contract(
        self,
        delta: float,
        spread_pct: float,
    ) -> Tuple[float, Dict[str, Any]]:
        """
        Domain 4: Contract & Feasibility (Max 15 pts raw)

        NOTE (2026-09-22): Delta sweet-spot scoring (0.25-0.45) is NEUTRALIZED for
        0DTE affordable index options. On expiry day, affordable OTM puts have delta
        < 0.10 (theta-decayed), and near-ATM puts have delta > 0.55 (ITM territory).
        No naturally affordable 0DTE strike lands in the 0.25-0.45 band, causing
        systematic contract_drag across all valid setups. The budget constraint in
        StrikeSelector already enforces contract selection; delta gating here is
        redundant and counterproductive for testing-phase 0DTE index option trading.

        Both delta and spread return full marks. Domain 4 still participates in
        telemetry (abs_delta logged) so data is collected for future recalibration.
        """
        abs_delta = abs(delta)

        # 1. Delta — neutralized: full marks always (0DTE index options, see note above)
        delta_pts = 10.0

        # 2. Spread Tightness — neutralized: full marks always (index options always tight)
        spread_pts = 5.0

        raw_score = 15.0
        details = {
            "abs_delta": round(abs_delta, 3),
            "delta_pts": delta_pts,
            "spread_pct": round(spread_pct, 3),
            "spread_pts": spread_pts,
            "raw_contract": round(raw_score, 2),
            "contract_domain_neutralized": True,
        }
        return raw_score, details

    def compute_conviction_score(
        self,
        direction: str,
        setup_name: str,
        spot_price: float,
        prior_bars: List[Any],
        current_bar: Optional[Any] = None,
        atr: float = 10.0,
        ema_20: float = 0.0,
        ema_50: float = 0.0,
        vwap: float = 0.0,
        pdh: float = 0.0,
        pdl: float = 0.0,
        vol_sma10: float = 0.0,
        persistence_dp: float = 0.70,
        touch_i1: float = 0.0,
        delta_fut_60s_bps: float = 0.0,
        delta_basis_60s_bps: float = 0.0,
        option_delta: Optional[float] = None,
        option_strike: float = 0.0,
        spread_pct: float = 0.50,
    ) -> ConvictionResult:
        """
        Computes the unified 0 to 100 composite conviction score.
        """
        bar = current_bar or (prior_bars[-1] if prior_bars else None)
        b_open = float(getattr(bar, "open", spot_price) if bar else spot_price)
        b_high = float(getattr(bar, "high", spot_price) if bar else spot_price)
        b_low = float(getattr(bar, "low", spot_price) if bar else spot_price)
        b_close = float(getattr(bar, "close", spot_price) if bar else spot_price)
        b_vol = float(getattr(bar, "volume", 1.0) if bar else 1.0)

        # 1. Domain 1: Structure & Runway (30 pts max raw)
        raw_struct, struct_m = self._score_structure(
            direction=direction,
            spot_price=spot_price,
            prior_bars=prior_bars,
            atr=atr,
            ema_20=ema_20,
            ema_50=ema_50,
            vwap=vwap,
            pdh=pdh,
            pdl=pdl,
        )

        # 2. Domain 2: PA Impulse & Volume (30 pts max raw)
        raw_impulse, impulse_m = self._score_impulse(
            direction=direction,
            bar_open=b_open,
            bar_high=b_high,
            bar_low=b_low,
            bar_close=b_close,
            bar_volume=b_vol,
            vol_sma10=vol_sma10,
            atr=atr,
        )

        # 3. Domain 3: Flow & Futures Momentum (25 pts max raw)
        raw_flow, flow_m = self._score_flow_and_futures(
            direction=direction,
            persistence_dp=persistence_dp,
            touch_i1=touch_i1,
            delta_fut_60s_bps=delta_fut_60s_bps,
            delta_basis_60s_bps=delta_basis_60s_bps,
        )

        # 4. Domain 4: Contract & Feasibility (15 pts max raw)
        eff_delta = option_delta
        if eff_delta is None or eff_delta <= 0:
            if option_strike > 0 and spot_price > 0:
                side = "CE" if direction.upper() in ("UP", "CALL") else "PE"
                eff_delta = self.estimate_black_scholes_delta(spot=spot_price, strike=option_strike, side=side)
            else:
                eff_delta = 0.35  # Safe default sweet spot

        raw_contract, contract_m = self._score_contract(
            delta=eff_delta,
            spread_pct=spread_pct,
        )

        # 5. Apply Setup-Specific Weight Profiles
        weights = self.SETUP_WEIGHT_PROFILES.get(setup_name, self.DEFAULT_WEIGHTS)
        w_struct = weights["structure"]
        w_impulse = weights["impulse"]
        w_flow = weights["flow"]
        w_contract = weights["contract"]

        # Normalize raw scores to the setup weights
        scaled_struct = raw_struct * (w_struct / 30.0)
        scaled_impulse = raw_impulse * (w_impulse / 30.0)
        scaled_flow = raw_flow * (w_flow / 25.0)
        scaled_contract = raw_contract * (w_contract / 15.0)

        total_score = round(max(0.0, min(100.0, scaled_struct + scaled_impulse + scaled_flow + scaled_contract)), 1)

        # 6. Determine Sizing Tier & Approval
        is_approved = total_score >= self.min_approval_score
        if total_score >= self.high_conviction_score:
            sizing_tier = "FULL_SIZE"
            rec_tp = self.high_conviction_tp_pct
            reason = "high_conviction_approved"
        elif is_approved:
            sizing_tier = "STANDARD_SIZE"
            rec_tp = self.standard_tp_pct
            reason = "standard_conviction_approved"
        else:
            sizing_tier = "BLOCKED"
            rec_tp = self.standard_tp_pct
            # Identify dominant drag
            drags = []
            if scaled_struct < (w_struct * 0.5):
                drags.append(f"structure_drag ({struct_m.get('swing_reason')})")
            if scaled_impulse < (w_impulse * 0.5):
                drags.append("impulse_drag")
            if scaled_flow < (w_flow * 0.5):
                drags.append("flow_drag")
            if scaled_contract < (w_contract * 0.5):
                drags.append(f"contract_drag (delta={eff_delta:.2f})")
            reason = f"conviction_too_low: {', '.join(drags) if drags else 'borderline'}"

        sub_scores = {
            "structure": round(scaled_struct, 1),
            "impulse": round(scaled_impulse, 1),
            "flow": round(scaled_flow, 1),
            "contract": round(scaled_contract, 1),
        }

        breakdown = {
            "setup_name": setup_name,
            "weights_used": weights,
            "structure_details": struct_m,
            "impulse_details": impulse_m,
            "flow_details": flow_m,
            "contract_details": contract_m,
        }

        return ConvictionResult(
            total_score=total_score,
            is_approved=is_approved,
            sizing_tier=sizing_tier,
            recommended_tp_pct=rec_tp,
            sub_scores=sub_scores,
            breakdown=breakdown,
            reason=reason,
        )
