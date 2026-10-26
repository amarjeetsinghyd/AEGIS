"""
AEGIS Composite Conviction Engine — Unified 0-100 Continuous Evaluation Framework.
Zero-Entropy De-bloated Architecture (25-25-25-25 Model):
    Domain 1: Structure & Spot Trend Persistence (25 pts) — Dp persistence, swing geometry, runway
    Domain 2: PA Impulse & Velocity (25 pts) — Candle displacement, edge close, tick intensity, volume surge
    Domain 3: Flow & Order Flow Momentum (25 pts) — Touch Aggression I1 (13 pts), Live OFI Delta & Dp (12 pts) [Futures Muted]
    Domain 4: Contract & Liquidity Feasibility (25 pts) — Delta Sweet-Spot (15 pts), Spread & Depth (10 pts)

Thresholds:
    >= 85.0: FULL_SIZE (2 lots, expanded target)
    80.0 - 84.9: STANDARD_SIZE (1 lot, standard target)
    < 80.0: BLOCKED (rejected with itemized audit breakdown)

Pure Python, zero heavy imports — hot-path safe (O(1) per call).
Scale-invariant: all distances normalized via ATR and basis points (bps).
"""

from __future__ import annotations
import math
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Sequence

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


class AdaptiveMicroValidator:
    """
    AEGIS Adaptive Microstructure Energy Validator (OFEE) — Consolidated Engine.
    Merged into composite_conviction_engine.py for Zero-Entropy Monorepo Architecture.
    Evaluates real-time option contract energy and order-flow momentum (T-30s to T).
    """

    def __init__(self, min_pass_score: float = 60.0):
        self.min_pass_score = min_pass_score
        self.bos_min_pass_score = 70.0

    def evaluate(
        self,
        target_sym: str,
        tick_buffer: Sequence[Any],
        anchor_T: Optional[float] = None,
        direction: str = "CALL",
        pillar: int = 1,
        min_pass_score: Optional[float] = None,
        strict_bos: bool = False,
        lookback_sec: float = 30.0,
    ) -> Tuple[bool, float, Dict[str, Any]]:
        if min_pass_score is None:
            threshold = self.bos_min_pass_score if strict_bos else self.min_pass_score
        else:
            threshold = min_pass_score

        if not tick_buffer:
            return False, 0.0, {
                "reason": "insufficient_buffer",
                "anchor_T": float(anchor_T or 0.0),
                "n_ticks": 0,
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
            return False, 0.0, {
                "reason": "insufficient_anchored_ticks",
                "anchor_T": float(anchor_T or 0.0),
                "n_ticks": len(sl),
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

        bp1 = float(depth_r[2]) if len(depth_r) > 2 else p1 - 0.05
        sp1 = float(depth_r[3]) if len(depth_r) > 3 else p1 + 0.05

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

        mid_px = (bp1 + sp1) / 2.0 if (bp1 > 0 and sp1 > 0) else p1
        spread_pct = ((sp1 - bp1) / mid_px * 100.0) if (mid_px > 0 and bp1 > 0 and sp1 > 0) else 0.10

        bp0 = float(first_r[2]) if len(first_r) > 2 else 0.0
        sp0 = float(first_r[3]) if len(first_r) > 3 else 0.0
        mid_0 = (bp0 + sp0) / 2.0 if (bp0 > 0 and sp0 > 0) else p0

        micro_ret = (mid_px - mid_0) / mid_0 if mid_0 > 0 else ((p1 - p0) / p0 if p0 > 0 else 0.0)
        touch_i1 = (b1 - a1) / (b1 + a1 + 1e-4)

        i3 = (b3 - a3) / (b3 + a3 + 1e-4)
        i4 = (b4 - a4) / (b4 + a4 + 1e-4)
        i5 = (b5 - a5) / (b5 + a5 + 1e-4)
        deep_pressure = (i3 + i4 + i5) / 3.0

        n_ticks = len(sl)
        delta_px = abs(mid_px - mid_0)
        vol_0 = float(first_r[16]) if len(first_r) > 16 and float(first_r[16]) > 0 else 0.0
        vol_1 = float(last_r[16]) if len(last_r) > 16 and float(last_r[16]) > 0 else 0.0
        delta_vol = vol_1 - vol_0
        eff_vol = delta_vol if delta_vol > 0 else float(n_ticks)
        kyle_lambda_30s = float(delta_px / max(eff_vol, 1.0))

        t_10_start = (anchor_T - 10.0) if anchor_T else (last_r[0] - 10.0)
        mid_10 = mid_0
        for r in sl:
            if r[0] >= t_10_start:
                r_bp = float(r[2]) if len(r) > 2 else 0.0
                r_sp = float(r[3]) if len(r) > 3 else 0.0
                mid_10 = (r_bp + r_sp) / 2.0 if (r_bp > 0 and r_sp > 0) else float(r[1])
                break
        ret_10 = (mid_px - mid_10) / mid_10 if mid_10 > 0 else 0.0
        lookback_ratio = max(lookback / 10.0, 1.0)
        momentum_accel_10s = float(ret_10 - (micro_ret / lookback_ratio))

        base_metrics = {
            "anchor_T": float(anchor_T or 0.0),
            "target_sym": target_sym,
            "lookback_sec": float(lookback),
            "micro_ret": float(micro_ret),
            "micro_ret_30s": float(micro_ret),
            "option_ret_30s": float(micro_ret),
            "kyle_lambda_30s": float(kyle_lambda_30s),
            "momentum_accel_10s": float(momentum_accel_10s),
            "touch_i1": float(touch_i1),
            "deep_pressure": float(deep_pressure),
            "tot_depth": float(tot_depth),
            "tot_bid_depth": float(tot_bid_depth),
            "tot_ask_depth": float(tot_ask_depth),
            "spread_pct": float(spread_pct),
            "n_ticks": int(n_ticks),
            "p0": float(p0),
            "p1": float(p1),
            "bp1": float(bp1),
            "sp1": float(sp1),
        }

        # Circuit breakers
        if spread_pct > 2.5:
            return False, 0.0, {**base_metrics, "reason": "circuit_breaker_spread_blowout", "energy_score": 0.0}
        if tot_depth < 200:
            return False, 0.0, {**base_metrics, "reason": "circuit_breaker_ghost_book", "energy_score": 0.0}
        if micro_ret < -0.015:
            return False, 0.0, {**base_metrics, "reason": "circuit_breaker_severe_dump", "energy_score": 0.0}

        # Pillar 1: Price Momentum (0-40 pts)
        if micro_ret >= 0.010:
            score_momentum = 40.0
        elif micro_ret >= 0.003:
            score_momentum = 32.0
        elif micro_ret >= -0.001:
            score_momentum = 24.0
        elif micro_ret >= -0.004:
            score_momentum = 12.0
        else:
            score_momentum = 0.0

        # Pillar 2: Buyer Aggression / Touch Imbalance I1 (0-35 pts)
        if touch_i1 >= 0.35:
            score_aggression = 35.0
        elif touch_i1 >= 0.10:
            score_aggression = 28.0
        elif touch_i1 >= -0.15:
            score_aggression = 20.0
        elif touch_i1 >= -0.35:
            score_aggression = 10.0
        else:
            score_aggression = 0.0

        # Pillar 3: Participation Velocity & Depth Support (0-25 pts)
        if n_ticks >= 30 and tot_depth >= 1000:
            score_velocity = 25.0
        elif n_ticks >= 15 and tot_depth >= 500:
            score_velocity = 20.0
        elif n_ticks >= 5 and tot_depth >= 350:
            score_velocity = 12.0
        elif n_ticks >= 5:
            score_velocity = 8.0
        else:
            score_velocity = 0.0

        total_score = score_momentum + score_aggression + score_velocity
        is_passed = total_score >= threshold

        result_metrics = {
            **base_metrics,
            "score_momentum": float(score_momentum),
            "score_aggression": float(score_aggression),
            "score_velocity": float(score_velocity),
            "energy_score": float(total_score),
            "pass_threshold": float(threshold),
            "strict_bos": bool(strict_bos),
            "reason": "energy_confirmed" if is_passed else "energy_insufficient",
        }

        # BOS-strict momentum gate
        if strict_bos and is_passed:
            is_up = direction.upper() in ("UP", "CALL", "BULL")
            veto_reason = None
            if momentum_accel_10s < 0.0:
                veto_reason = "bos_momentum_stall"
            elif is_up and touch_i1 < 0.10:
                veto_reason = "bos_weak_aggression"
            elif not is_up and touch_i1 > -0.10:
                veto_reason = "bos_weak_aggression"
            elif is_up and deep_pressure < -0.60:
                veto_reason = "bos_deep_opposition"
            elif not is_up and deep_pressure > 0.60:
                veto_reason = "bos_deep_opposition"
            if veto_reason is not None:
                return False, float(total_score), {**result_metrics, "reason": veto_reason}

        return is_passed, float(total_score), result_metrics


class CompositeConvictionEngine:
    """
    Evaluates institutional conviction across orthogonal quantitative domains
    on a 0 to 100 continuous scale.
    """

    @staticmethod
    def extract_micro_metrics(
        tick_buffer: Sequence[Any],
        anchor_T: Optional[float] = None,
        lookback_sec: float = 30.0,
    ) -> Dict[str, Any]:
        """
        Extracts micro-price return, touch I1, tick arrival count, depth, and spread
        directly from the raw option tick buffer (consolidated from AdaptiveMicroValidator).
        """
        validator = AdaptiveMicroValidator()
        passed, score, metrics = validator.evaluate(
            target_sym="GENERIC",
            tick_buffer=tick_buffer,
            anchor_T=anchor_T,
            lookback_sec=lookback_sec,
        )
        return metrics

    # Default baseline weights (sum to 100.0) — Zero-Entropy 25-25-25-25 Model
    DEFAULT_WEIGHTS = {
        "structure": 25.0,
        "impulse": 25.0,
        "flow": 25.0,
        "contract": 25.0,
    }

    # Setup-specific weight profiles (each sums to 100.0)
    SETUP_WEIGHT_PROFILES = {
        "20_ema_rejection": {
            "structure": 25.0,
            "impulse": 25.0,
            "flow": 25.0,
            "contract": 25.0,
        },
        "micro_sweep_reclaim": {
            "structure": 25.0,
            "impulse": 25.0,
            "flow": 25.0,
            "contract": 25.0,
        },
        "albrooks_2legged_pullback": {
            "structure": 25.0,
            "impulse": 25.0,
            "flow": 25.0,
            "contract": 25.0,
        },
        "volatility_squeeze_breakout": {
            "structure": 25.0,
            "impulse": 25.0,
            "flow": 25.0,
            "contract": 25.0,
        },
        "hvg_network_phase_shift": {
            "structure": 25.0,
            "impulse": 25.0,
            "flow": 25.0,
            "contract": 25.0,
        },
        "topological_prominence_breakout": {
            "structure": 25.0,
            "impulse": 25.0,
            "flow": 25.0,
            "contract": 25.0,
        },
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
        setup_name: str = "",
        pa_details: Optional[Dict[str, Any]] = None,
    ) -> Tuple[float, Dict[str, Any]]:
        """
        Domain 1: Structure & Runway (Max 30 pts raw)
        - Swing geometry (HH/HL vs LH/LL): +15 to -15 pts (Pillar-aware)
        - Runway to obstacle (PDH/PDL): +10 to 0 pts
        - EMA & VWAP alignment: +5 to 0 pts
        """
        is_up = direction.upper() in ("UP", "CALL")
        eff_atr = max(atr, 1.0)
        s_name_lower = setup_name.lower()
        is_sweep = "sweep" in s_name_lower or "reclaim" in s_name_lower
        is_rejection = "rejection" in s_name_lower or "20_ema" in s_name_lower or "pullback" in s_name_lower
        is_sweep_or_rejection = is_sweep or is_rejection or setup_name in ("micro_sweep_reclaim", "20_ema_rejection", "albrooks_2legged_pullback")

        # 1. Swing Geometry Evaluation (12 pts max)
        swing_pts = 4.0  # Neutral default
        swing_reason = "neutral"

        if is_sweep_or_rejection:
            # Case A — Pullback & Sweep Setups: Do NOT penalize lower highs / lower lows.
            # A dip/pullback into the key level is mandatory for these setups.
            # Since PriceActionEngine already validated the sweep reclaim or EMA rejection, award full points.
            swing_pts = 12.0
            swing_reason = "validated_pullback_reclaim"
        elif len(prior_bars) >= 3:
            b1, b2, b3 = prior_bars[-3], prior_bars[-2], prior_bars[-1]
            h1, h2, h3 = b1.high, b2.high, b3.high
            l1, l2, l3 = b1.low, b2.low, b3.low

            is_higher_highs = (h3 > h2) and (h2 >= h1)
            is_higher_lows = (l3 > l2) and (l2 >= l1)
            is_lower_highs = (h3 < h2) and (h2 <= h1)
            is_lower_lows = (l3 < l2) and (l2 <= l1)

            # Case B — Breakout Setups
            if (
                setup_name in ("volatility_squeeze_breakout", "topological_prominence_breakout")
                or "squeeze" in s_name_lower
                or "prominence" in s_name_lower
                or "breakout" in s_name_lower
            ):
                ref_high = max(b1.high, b2.high)
                ref_low = min(b1.low, b2.low)
                check_price = max(spot_price, b3.close) if is_up else min(spot_price, b3.close)
                if is_up and check_price > ref_high:
                    swing_pts = 12.0
                    swing_reason = "breakout_structure_expansion"
                elif (not is_up) and check_price < ref_low:
                    swing_pts = 12.0
                    swing_reason = "breakout_structure_expansion"
                elif is_up:
                    if is_higher_highs and is_higher_lows:
                        swing_pts = 12.0
                        swing_reason = "bullish_hh_hl"
                    elif is_higher_lows or b3.close > b2.close:
                        swing_pts = 8.0
                        swing_reason = "bullish_hl"
                    else:
                        swing_pts = 0.0
                        swing_reason = "breakout_compression_neutral"
                else:
                    if is_lower_lows and is_lower_highs:
                        swing_pts = 12.0
                        swing_reason = "bearish_ll_lh"
                    elif is_lower_highs or b3.close < b2.close:
                        swing_pts = 8.0
                        swing_reason = "bearish_lh"
                    else:
                        swing_pts = 0.0
                        swing_reason = "breakout_compression_neutral"
            else:
                # Case C — Fallback / General
                if is_up:
                    if is_higher_highs and is_higher_lows:
                        swing_pts = 12.0
                        swing_reason = "bullish_hh_hl"
                    elif is_higher_lows or b3.close > b2.close:
                        swing_pts = 8.0
                        swing_reason = "bullish_hl"
                    elif is_lower_highs:
                        # Distribution top / lower highs warning!
                        swing_pts = -12.0
                        swing_reason = "exhaustion_lower_highs"
                    elif is_lower_lows:
                        swing_pts = -8.0
                        swing_reason = "bearish_lower_lows"
                else:
                    if is_lower_lows and is_lower_highs:
                        swing_pts = 12.0
                        swing_reason = "bearish_ll_lh"
                    elif is_lower_highs or b3.close < b2.close:
                        swing_pts = 8.0
                        swing_reason = "bearish_lh"
                    elif is_higher_highs:
                        # Rally bottom / higher highs warning!
                        swing_pts = -12.0
                        swing_reason = "exhaustion_higher_highs"
                    elif is_higher_lows:
                        swing_pts = -8.0
                        swing_reason = "bullish_higher_lows"

        # 2. Runway to Key Obstacles (8 pts max)
        runway_pts = 8.0
        runway_dist = 999.0
        if is_up:
            if pdh > 0 and pdh > spot_price:
                runway_dist = (pdh - spot_price) / eff_atr
                if runway_dist >= 1.5:
                    runway_pts = 8.0
                elif runway_dist >= 1.0:
                    runway_pts = 5.0
                elif runway_dist >= 0.6:
                    runway_pts = 3.0
                else:
                    runway_pts = 0.0  # Trapped directly under PDH resistance
            elif pdh > 0 and spot_price >= pdh:
                # Already broken above PDH -> open sky breakout!
                runway_pts = 8.0
        else:
            if pdl > 0 and pdl < spot_price:
                runway_dist = (spot_price - pdl) / eff_atr
                if runway_dist >= 1.5:
                    runway_pts = 8.0
                elif runway_dist >= 1.0:
                    runway_pts = 5.0
                elif runway_dist >= 0.6:
                    runway_pts = 3.0
                else:
                    runway_pts = 0.0  # Trapped directly above PDL support
            elif pdl > 0 and spot_price <= pdl:
                # Already broken below PDL -> open sky breakdown!
                runway_pts = 8.0

        # 3. EMA & VWAP Alignment (5 pts max)
        ema_vwap_pts = 0.0
        is_p1_rejection = (is_rejection and not is_sweep) or setup_name == "20_ema_rejection"
        if is_up:
            ema_aligned = (ema_20 > ema_50) if (ema_20 > 0 and ema_50 > 0) else True
            vwap_aligned = (spot_price > vwap) if vwap > 0 else True
            if is_p1_rejection:
                if ema_aligned and vwap_aligned:
                    ema_vwap_pts = 5.0
                elif ema_aligned or vwap_aligned:
                    ema_vwap_pts = 3.0
                else:
                    ema_vwap_pts = 1.0
            else:
                # S/R Reactions & Breakouts (Pillar 2 Sweep, Pillar 3 Squeeze, Pillar 4 S/R Breakout):
                # Never penalize for lagging 20/50 EMA cross; VWAP alignment provides full directional compliance.
                if vwap_aligned:
                    ema_vwap_pts = 5.0
                elif ema_aligned:
                    ema_vwap_pts = 4.0
                else:
                    ema_vwap_pts = 2.0
        else:
            ema_aligned = (ema_20 < ema_50) if (ema_20 > 0 and ema_50 > 0) else True
            vwap_aligned = (spot_price < vwap) if vwap > 0 else True
            if is_p1_rejection:
                if ema_aligned and vwap_aligned:
                    ema_vwap_pts = 5.0
                elif ema_aligned or vwap_aligned:
                    ema_vwap_pts = 3.0
                else:
                    ema_vwap_pts = 1.0
            else:
                # S/R Reactions & Breakouts (Pillar 2 Sweep, Pillar 3 Squeeze, Pillar 4 S/R Breakout):
                # Never penalize for lagging 20/50 EMA cross; VWAP alignment provides full directional compliance.
                if vwap_aligned:
                    ema_vwap_pts = 5.0
                elif ema_aligned:
                    ema_vwap_pts = 4.0
                else:
                    ema_vwap_pts = 2.0

        raw_score = max(0.0, min(25.0, swing_pts + runway_pts + ema_vwap_pts))
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
        setup_name: str = "",
        pa_details: Optional[Dict[str, Any]] = None,
        wyckoff_details: Optional[Dict[str, Any]] = None,
    ) -> Tuple[float, Dict[str, Any]]:
        """
        Domain 2: PA Impulse & Volume (Max 30 pts raw) — Pillar-Aware
        - Candle Anatomy (Body vs Wick Rejection): +12 to +2 pts
        - Edge Close & Wick: +8 to -4 pts penalty
        - Range Expansion vs ATR: +5 to +2 pts
        - Volume / Institutional Participation: +5 to +2 pts
        """
        is_up = direction.upper() in ("UP", "CALL")
        rng = max(bar_high - bar_low, 0.1)
        eff_atr = max(atr, 1.0)
        body = abs(bar_close - bar_open)
        body_pct = body / rng

        # Directional wick and edge close
        if is_up:
            close_edge = (bar_close - bar_low) / rng
            favorable_wick = (min(bar_open, bar_close) - bar_low) / rng
            adverse_wick = (bar_high - max(bar_open, bar_close)) / rng
        else:
            close_edge = (bar_high - bar_close) / rng
            favorable_wick = (bar_high - max(bar_open, bar_close)) / rng
            adverse_wick = (min(bar_open, bar_close) - bar_low) / rng

        pa_wick_ratio = float((pa_details or {}).get("wick_ratio", 0.0))
        eff_wick = max(favorable_wick, pa_wick_ratio)
        s_name_lower = setup_name.lower()
        is_sweep = "sweep" in s_name_lower or "reclaim" in s_name_lower
        is_rejection = "rejection" in s_name_lower or "20_ema" in s_name_lower or "pullback" in s_name_lower
        is_sweep_or_rejection = is_sweep or is_rejection or setup_name in ("micro_sweep_reclaim", "20_ema_rejection", "albrooks_2legged_pullback")

        # 1. Candle Anatomy (10 pts max)
        if is_sweep_or_rejection:
            # Reward EITHER strong rejection wick OR strong expansion body
            # (Liquidity sweep & EMA rejection candles inherently have long tails/wicks that absorbed retail stops)
            if eff_wick >= 0.33 or body_pct >= 0.50:
                body_pts = 10.0
            elif eff_wick >= 0.25 or body_pct >= 0.35:
                body_pts = 7.5
            else:
                body_pts = 4.0
        else:
            # Breakout Setups (P3, P4, BOS)
            if body_pct >= 0.55:
                body_pts = 10.0
            elif body_pct >= 0.40:
                body_pts = 7.5
            elif body_pct >= 0.25:
                body_pts = 5.0
            else:
                body_pts = 2.0

        # 2. Edge Close / Wick Rejection (7 pts max)
        wick_penalty = 0.0
        if adverse_wick > 0.40:
            close_pts = -4.0
            wick_penalty = -4.0
        elif close_edge >= 0.70 or (is_sweep_or_rejection and eff_wick >= 0.35 and close_edge >= 0.55):
            close_pts = 7.0
        elif close_edge >= 0.55:
            close_pts = 5.0
        else:
            close_pts = 2.5

        # 3. Range Expansion vs ATR (4 pts max)
        range_atr = rng / eff_atr
        if range_atr >= 0.90:
            range_pts = 4.0
        elif range_atr >= 0.55:
            range_pts = 3.0
        else:
            range_pts = 1.5

        # 4. Volume / Institutional Participation (4 pts max)
        vol_ratio = (bar_volume / vol_sma10) if vol_sma10 > 0 else 1.0
        pa_vr = float((pa_details or {}).get("vr_t", 0.0))
        wyckoff_vdr = float((wyckoff_details or {}).get("vdr", 0.0))
        wyckoff_abs = float((wyckoff_details or {}).get("absorption_score") or (wyckoff_details or {}).get("absorption_ratio") or 0.0)
        eff_vol_ratio = max(vol_ratio, pa_vr, wyckoff_vdr, wyckoff_abs)
        wyckoff_conf = bool((wyckoff_details or {}).get("wyckoff_confirmed", False))

        if eff_vol_ratio >= 1.2 or wyckoff_conf:
            vol_pts = 4.0
        elif eff_vol_ratio >= 0.9:
            vol_pts = 3.0
        else:
            vol_pts = 1.5

        raw_score = max(0.0, min(25.0, body_pts + close_pts + range_pts + vol_pts))
        details = {
            "body_pct": round(body_pct, 3),
            "body_pts": body_pts,
            "close_pts": close_pts,
            "wick_penalty": wick_penalty,
            "eff_wick": round(eff_wick, 3),
            "adverse_wick": round(adverse_wick, 3),
            "range_atr": round(range_atr, 2),
            "range_pts": range_pts,
            "vol_ratio": round(vol_ratio, 2),
            "eff_vol_ratio": round(eff_vol_ratio, 2),
            "vol_pts": vol_pts,
            "raw_impulse": round(raw_score, 2),
        }
        return raw_score, details

    def _score_flow_and_futures(
        self,
        direction: str,
        persistence_dp: float,
        touch_i1: float,
        delta_fut_60s_bps: float = 0.0,
        delta_basis_60s_bps: float = 0.0,
        setup_name: str = "",
        wyckoff_details: Optional[Dict[str, Any]] = None,
    ) -> Tuple[float, Dict[str, Any]]:
        """
        Domain 3: Flow & Real-Time Order Flow Momentum (Max 25 pts raw) — Wyckoff & OFI Wired
        - Directional Persistence Dp: +12 to 0 pts (Pure Spot Price Stability)
        - Touch Aggression I1 & Live OFI Delta: +13 to 0 pts (Option Order Flow Dominance)
        - Futures Metrics: MUTED per operator directive (0 pts, zero reliance on external futures feeds)
        """
        is_up = direction.upper() in ("UP", "CALL")

        wyckoff_confirmed = bool((wyckoff_details or {}).get("wyckoff_confirmed", False))
        ofi = float(
            (wyckoff_details or {}).get("opt_delta_flow")
            or (wyckoff_details or {}).get("ofi_delta")
            or (wyckoff_details or {}).get("synthetic_net_delta")
            or 0.0
        )

        # 1. Directional Persistence / Spot Trend Stability (12 pts max)
        is_sweep_rejection = any(k in setup_name.lower() for k in ("sweep", "reclaim", "rejection", "20_ema"))
        if persistence_dp >= 0.65:
            dp_pts = 12.0
        elif wyckoff_confirmed and (setup_name in ("micro_sweep_reclaim", "20_ema_rejection") or is_sweep_rejection):
            dp_pts = 10.5
        elif persistence_dp >= 0.50 or wyckoff_confirmed:
            dp_pts = 9.0
        elif persistence_dp >= 0.40:
            dp_pts = 5.0
        else:
            dp_pts = 0.0

        # 2. Touch Aggression / Option Flow Imbalance & Live OFI (13 pts max)
        if is_up:
            if touch_i1 >= 0.10:
                i1_pts = 13.0
            elif touch_i1 >= 0.0:
                i1_pts = 9.0
            elif touch_i1 >= -0.20:
                i1_pts = 5.0
            else:
                i1_pts = 0.0
        else:
            if touch_i1 <= -0.10:
                i1_pts = 13.0
            elif touch_i1 <= 0.0:
                i1_pts = 9.0
            elif touch_i1 <= 0.20:
                i1_pts = 5.0
            else:
                i1_pts = 0.0

        if wyckoff_confirmed:
            ofi_aligned = (ofi > 0) if is_up else (ofi < 0)
            if ofi_aligned or ofi != 0:
                i1_pts = max(i1_pts, 11.0)

        # 3. Futures Metrics: MUTED permanently per operator directive (zero REST/WS load)
        fut_pts = 0.0
        basis_pts = 0.0
        fut_divergence_penalty = 0.0

        raw_score = max(0.0, min(25.0, dp_pts + i1_pts))
        details = {
            "persistence_dp": round(persistence_dp, 3),
            "dp_pts": dp_pts,
            "touch_i1": round(touch_i1, 3),
            "i1_pts": i1_pts,
            "delta_fut_60s_bps": 0.0,
            "fut_pts": 0.0,
            "fut_divergence_penalty": 0.0,
            "delta_basis_60s_bps": 0.0,
            "basis_pts": 0.0,
            "raw_flow": round(raw_score, 2),
            "futures_muted": True,
        }
        return raw_score, details

    def _score_contract(
        self,
        delta: float,
        spread_pct: float,
    ) -> Tuple[float, Dict[str, Any]]:
        """
        Domain 4: Contract Quality & Liquidity (Max 25 pts raw) — Zero-Entropy Scaled
        - Delta: 15.0 pts (sweet-spot 0.25-0.45 or neutralized 0DTE floor)
        - Spread Tightness: 10.0 pts (liquid index options)
        """
        abs_delta = abs(delta)

        # 1. Delta — 15 pts max (neutralized floor for affordable 0DTE index options)
        delta_pts = 15.0

        # 2. Spread Tightness — 10 pts max (index options always tight)
        spread_pts = 10.0

        raw_score = 25.0
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
        pa_details: Optional[Dict[str, Any]] = None,
        wyckoff_details: Optional[Dict[str, Any]] = None,
    ) -> ConvictionResult:
        """
        Computes the unified 0 to 100 composite conviction score.
        """
        # ─── Intra-Bar Mode A / Mode B Priority Fast-Path ───
        mode_val = str((pa_details or {}).get("mode", "")).upper()
        s_name_upper = str(setup_name).upper()
        if (
            setup_name.startswith(("mode_b_", "mode_a_"))
            or mode_val.startswith(("MODE_A", "MODE_B"))
            or "MODE_B" in s_name_upper
            or "MODE_A" in s_name_upper
        ):
            return ConvictionResult(
                total_score=85.0,
                is_approved=True,
                sizing_tier="STANDARD_SIZE",
                recommended_tp_pct=self.standard_tp_pct,
                sub_scores={"intrabar_core": 85.0},
                breakdown={"intrabar_inflight_momentum": 85.0, "setup_name": setup_name, "mode": mode_val},
                reason="intrabar_inflight_momentum_approved",
            )

        # Bug #1 Fix — The Newborn Bar Evaluation Guard
        # If evaluating an intra-bar breakout or live forming candle, judge the real-time forming bar
        # Never replace with last_closed if it's an intra-bar trigger or has developed expansion!
        is_intrabar = (
            setup_name.startswith(("mode_b_", "mode_a_"))
            or mode_val.startswith(("MODE_A", "MODE_B"))
            or "MODE_B" in s_name_upper
            or "MODE_A" in s_name_upper
        )
        bar = current_bar or (prior_bars[-1] if prior_bars else None)
        if prior_bars and not is_intrabar:
            last_closed = prior_bars[-1]
            if current_bar is not None and current_bar is not last_closed:
                c_rng = max(getattr(current_bar, "high", spot_price) - getattr(current_bar, "low", spot_price), 0.0)
                p_rng = max(getattr(last_closed, "high", spot_price) - getattr(last_closed, "low", spot_price), 0.0)
                eff_atr = max(atr, 1.0)
                if setup_name in (
                    "micro_sweep_reclaim",
                    "20_ema_rejection",
                    "topological_prominence_breakout",
                    "volatility_squeeze_breakout",
                    "albrooks_2legged_pullback",
                ):
                    if c_rng < (0.5 * eff_atr) or c_rng < p_rng:
                        bar = last_closed

        b_open = float(getattr(bar, "open", spot_price) if bar else spot_price)
        b_high = float(getattr(bar, "high", spot_price) if bar else spot_price)
        b_low = float(getattr(bar, "low", spot_price) if bar else spot_price)
        # Real-time forming candle anatomy: close is the current live spot price!
        if bar is current_bar and spot_price > 0:
            b_close = float(spot_price)
            b_high = max(b_high, spot_price)
            b_low = min(b_low, spot_price)
        else:
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
            setup_name=setup_name,
            pa_details=pa_details,
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
            setup_name=setup_name,
            pa_details=pa_details,
            wyckoff_details=wyckoff_details,
        )

        # 3. Domain 3: Flow & Futures Momentum (25 pts max raw)
        raw_flow, flow_m = self._score_flow_and_futures(
            direction=direction,
            persistence_dp=persistence_dp,
            touch_i1=touch_i1,
            delta_fut_60s_bps=delta_fut_60s_bps,
            delta_basis_60s_bps=delta_basis_60s_bps,
            setup_name=setup_name,
            wyckoff_details=wyckoff_details,
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
        profile_key = setup_name
        if profile_key not in self.SETUP_WEIGHT_PROFILES:
            s_low = setup_name.lower()
            if "sweep" in s_low or "reclaim" in s_low:
                profile_key = "micro_sweep_reclaim"
            elif "20_ema" in s_low or "rejection" in s_low:
                profile_key = "20_ema_rejection"
            elif "squeeze" in s_low:
                profile_key = "volatility_squeeze_breakout"
            elif "prominence" in s_low or "topological" in s_low:
                profile_key = "topological_prominence_breakout"

        weights = self.SETUP_WEIGHT_PROFILES.get(profile_key, self.DEFAULT_WEIGHTS)
        w_struct = weights["structure"]
        w_impulse = weights["impulse"]
        w_flow = weights["flow"]
        w_contract = weights["contract"]

        # Normalize raw scores to the setup weights (each 25.0 max)
        scaled_struct = raw_struct * (w_struct / 25.0)
        scaled_impulse = raw_impulse * (w_impulse / 25.0)
        scaled_flow = raw_flow * (w_flow / 25.0)
        scaled_contract = raw_contract * (w_contract / 25.0)

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
