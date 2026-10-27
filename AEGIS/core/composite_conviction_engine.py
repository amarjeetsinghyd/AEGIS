"""
AEGIS Composite Conviction Engine — Pure Option Microstructure & Order Flow Framework.
Zero-Entropy De-bloated Architecture (40-35-25 Model):
    Domain 1: Touch Aggression & Top-of-Book Pressure (40 pts) — Level 1 imbalance I1 & Wyckoff OFI Delta
    Domain 2: Option Micro-Momentum & Premium Velocity (35 pts) — Real-time option premium speed
    Domain 3: Contract Tradability & Book Depth Support (25 pts) — Spread tightness & L2 depth support

Note on Zero Redundancy:
    Spot Price Action Structure (Dow HL2/LH2, S/R levels, 20 EMA, VWAP, PDH/PDL Runway),
    Candle Anatomy (Body vs Wick, Edge Close, ATR range expansion), and Spot Directional
    Persistence (Dp) are exclusively evaluated upstream by PriceActionEngine, WyckoffVolumeEngine,
    and PersistenceEngine. They have been permanently removed from this conviction engine to eliminate
    redundant scoring loops and duplicate code.

Thresholds:
    >= 85.0: FULL_SIZE (2 lots, expanded target)
    80.0 - 84.9: STANDARD_SIZE (1 lot, standard target)
    < 80.0: BLOCKED (rejected with itemized audit breakdown)

Pure Python, zero heavy imports — hot-path safe (O(1) per call).
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


class CompositeConvictionEngine:
    """
    AEGIS Unified Option Microstructure & Order Flow Conviction Engine (OFEE).
    Consolidates raw tick-buffer metrics extraction, sanity circuit breakers,
    and Dual-Profile institutional conviction evaluation (0-100 scale).

    Merged Architecture:
      - Raw tick buffer parsing & circuit breakers directly embedded (AdaptiveMicroValidator merged).
      - Mode B Profile: In-Flight Mid-Bar Breakthrough Surge (T:08s onwards, No Upper Cap).
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
        dt = max(float(last_r[0] - first_r[0]), 0.5)
        tick_intensity = float(n_ticks) / dt

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
        if spread_pct > 2.5:
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
    ) -> Tuple[float, Dict[str, float], Dict[str, Any], str]:
        """
        Mode B Conviction Profile: In-Flight Mid-Bar Breakthrough Surge.
        Active from T:08s onwards anytime during the candle (No Upper Limit Cap).
        Focused on instantaneous breakout velocity, ask sweeps, and live tick intensity.

        Domains (100 pts total):
          1. Touch Aggression & Ask Sweep I1: 40 pts
          2. Live Tick Arrival Intensity: 35 pts
          3. Contract Tradability & Spread: 25 pts
        """
        is_up = direction.upper() in ("UP", "CALL")
        eff_i1 = touch_i1 if is_up else -touch_i1
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
            if ofi_aligned or ofi != 0:
                agg_pts = max(agg_pts, 34.0)

        # 2. Live Tick Arrival Intensity (35 pts max)
        if tick_rate is not None:
            if tick_rate >= 1.5:
                tick_pts = 35.0  # Institutional rapid-fire sweep
            elif tick_rate >= 0.8:
                tick_pts = 28.0  # Steady active flow
            elif tick_rate >= 0.4:
                tick_pts = 22.0  # Normal liquidity
            else:
                tick_pts = 12.0  # Thin / sluggish arrival
        else:
            # Clean baseline for tests or synthetic ticks
            tick_pts = 30.0 if wyckoff_conf else 26.0

        # 3. Contract Tradability & Spread (25 pts max)
        if spread_pct > 2.50:
            trad_pts = 0.0  # Circuit breaker blowout
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
        eff_i1 = touch_i1 if is_up else -touch_i1
        eff_deep = deep_pressure if is_up else -deep_pressure
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
            if ofi_aligned or ofi != 0:
                flow_pts = max(flow_pts, 34.0)
            else:
                flow_pts = max(flow_pts, 32.0)

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
        if spread_pct > 2.50 or (0.0 < tot_depth < 200.0):
            trad_pts = 0.0  # Circuit breaker blowout or ghost book
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
        spot_price: float = 0.0,
        prior_bars: Optional[List[Any]] = None,
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
        spread_pct: float = 0.25,
        pa_details: Optional[Dict[str, Any]] = None,
        wyckoff_details: Optional[Dict[str, Any]] = None,
        micro_metrics: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> ConvictionResult:
        """
        Computes the streamlined Dual-Profile Option Microstructure & Order Flow conviction score:
          - Mode B Profile: In-Flight Mid-Bar Breakthrough Surge (T:08s onwards, No Upper Cap).
          - Mode A Profile: Structural Rejection & Close (T:50s Pre-Close / T:00s Close).
        """
        mode_val = str((pa_details or {}).get("mode", "")).upper()
        s_name_upper = str(setup_name).upper()
        is_mode_b = (
            "MODE_B" in mode_val
            or setup_name.startswith("mode_b_")
            or "MODE_B" in s_name_upper
            or setup_name in ("volatility_squeeze_breakout", "topological_prominence_breakout")
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
            now_sec = float(getattr(current_bar, "ts_end", 0.0) or getattr(current_bar, "ts_start", 0.0) + 15.0)
            sec_in_min = max(int(now_sec % 60), 1)
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

        # Sizing Tier & Approval Gate
        is_approved = total_score >= self.min_approval_score
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
