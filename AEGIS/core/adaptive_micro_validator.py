"""AEGIS Adaptive Microstructure Energy Validator (OFEE)

Pure Order Flow Energy & Momentum Secondary Validator for AEGIS V3.
Replaces legacy 14-feature burst predictor and disjointed multi-gates
with a lean, compensatory 3-Pillar Energy model:
  1. Price Momentum (30s Micro-Price Return)      [0 - 40 pts]
  2. Buyer Aggression (L1 Touch Imbalance I1)     [0 - 35 pts]
  3. Participation Velocity & Depth Support       [0 - 25 pts]

Pass condition: Total Energy Score >= 60.0 / 100.
Hot-Path Discipline: Pure Python, zero allocation, NO pandas/scipy/sklearn.
"""

from __future__ import annotations
from typing import Dict, List, Tuple, Optional, Any, Sequence


class AdaptiveMicroValidator:
    """Evaluates real-time option contract energy and order-flow momentum
    at the close of a 1-minute price action rejection candle (T-30s to T).
    """

    def __init__(self, min_pass_score: float = 60.0):
        self.min_pass_score = min_pass_score
        # BOS-strict pass mark: breakout entries need real momentum, not chop.
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
    ) -> Tuple[bool, float, Dict[str, Any]]:
        """Evaluates 30-second order flow energy bound to candle close anchor T.

        Args:
            target_sym: Target option symbol (e.g. NIFTY15SEP26C23650)
            tick_buffer: Iterable of tick tuples:
                         (ts, lp, bp1, sp1, b1, a1, b2, a2, b3, a3, b4, a4, b5, a5, ...)
            anchor_T: Timestamp of closed 1m candle close (event-bound).
            direction: "CALL" / "PUT" (or "UP" / "DOWN")
            pillar: Setup pillar (1: 20-EMA, 2: 50-EMA, 3: Squeeze Breakout)
            min_pass_score: Threshold override (default 60.0, 70.0 when strict_bos)
            strict_bos: BOS-breakout momentum gate — direction-aligned touch,
                        non-negative 10s acceleration and no deep-book wall.
                        Weak break = discard. Default False (legacy behaviour).

        Returns:
            (is_passed, total_score, metrics_dict)
        """
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

        # ─── 1. Slice Strictly to the Final 30s Window [T-30, T] ───
        if anchor_T is not None and anchor_T > 0:
            w_start = anchor_T - 30.0
            sl = [r for r in tick_buffer if w_start <= r[0] <= anchor_T]
        else:
            # Fallback for unanchored/unit test callers
            last_ts = tick_buffer[-1][0]
            w_start = last_ts - 30.0
            sl = [r for r in tick_buffer if r[0] >= w_start]

        if len(sl) < 2:
            return False, 0.0, {
                "reason": "insufficient_anchored_ticks",
                "anchor_T": float(anchor_T or 0.0),
                "n_ticks": len(sl),
            }

        last_r = sl[-1]
        first_r = sl[0]

        # Extract prices
        p0 = float(first_r[1])
        p1 = float(last_r[1])

        # Find latest tick in slice that contains non-zero book depth/quotes
        # (Shoonya NorenAPI trade ticks sometimes omit depth arrays)
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

        # Depth extraction (levels 1 to 5)
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

        # Mid-price and spread
        mid_px = (bp1 + sp1) / 2.0 if (bp1 > 0 and sp1 > 0) else p1
        spread_pct = ((sp1 - bp1) / mid_px * 100.0) if (mid_px > 0 and bp1 > 0 and sp1 > 0) else 0.10

        # First tick mid-price
        bp0 = float(first_r[2]) if len(first_r) > 2 else 0.0
        sp0 = float(first_r[3]) if len(first_r) > 3 else 0.0
        mid_0 = (bp0 + sp0) / 2.0 if (bp0 > 0 and sp0 > 0) else p0

        # Micro-price return across 30s (Mid-price based to eliminate bid-ask bounce)
        micro_ret = (mid_px - mid_0) / mid_0 if mid_0 > 0 else ((p1 - p0) / p0 if p0 > 0 else 0.0)

        # Touch imbalance I1: (Bid1 - Ask1) / (Bid1 + Ask1)
        touch_i1 = (b1 - a1) / (b1 + a1 + 1e-4)

        # Deep-book pressure L3-L5: negative = sell wall stacked above.
        i3 = (b3 - a3) / (b3 + a3 + 1e-4)
        i4 = (b4 - a4) / (b4 + a4 + 1e-4)
        i5 = (b5 - a5) / (b5 + a5 + 1e-4)
        deep_pressure = (i3 + i4 + i5) / 3.0

        n_ticks = len(sl)

        # Kyle's Lambda price impact (index 16 carries traded cumulative volume; index 14 is d_ofi)
        delta_px = abs(mid_px - mid_0)
        vol_0 = float(first_r[16]) if len(first_r) > 16 and float(first_r[16]) > 0 else 0.0
        vol_1 = float(last_r[16]) if len(last_r) > 16 and float(last_r[16]) > 0 else 0.0
        delta_vol = vol_1 - vol_0
        eff_vol = delta_vol if delta_vol > 0 else float(n_ticks)
        kyle_lambda_30s = float(delta_px / max(eff_vol, 1.0))

        # Momentum Acceleration (10s return vs 30s return)
        t_10_start = (anchor_T - 10.0) if anchor_T else (last_r[0] - 10.0)
        mid_10 = mid_0
        for r in sl:
            if r[0] >= t_10_start:
                r_bp = float(r[2]) if len(r) > 2 else 0.0
                r_sp = float(r[3]) if len(r) > 3 else 0.0
                mid_10 = (r_bp + r_sp) / 2.0 if (r_bp > 0 and r_sp > 0) else float(r[1])
                break
        ret_10 = (mid_px - mid_10) / mid_10 if mid_10 > 0 else 0.0
        momentum_accel_10s = float(ret_10 - (micro_ret / 3.0))

        base_metrics = {
            "anchor_T": float(anchor_T or 0.0),
            "target_sym": target_sym,
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

        # ─── 2. Circuit Breakers (Deal-Breakers Only) ───
        # Circuit Breaker 1: Spread blowout (extreme illiquidity)
        if spread_pct > 2.5:
            return False, 0.0, {
                **base_metrics,
                "reason": "circuit_breaker_spread_blowout",
                "energy_score": 0.0,
            }

        # Circuit Breaker 2: Ghost book (deserted liquidity)
        if tot_depth < 200:
            return False, 0.0, {
                **base_metrics,
                "reason": "circuit_breaker_ghost_book",
                "energy_score": 0.0,
            }

        # Circuit Breaker 3: Severe flush / dump into candle close (counter-trend collapse)
        if micro_ret < -0.015:
            return False, 0.0, {
                **base_metrics,
                "reason": "circuit_breaker_severe_dump",
                "energy_score": 0.0,
            }

        # ─── 3. Pillar 1: Price Momentum / Rejection Acceleration (0-40 pts) ───
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

        # ─── 4. Pillar 2: Buyer Aggression / Touch Imbalance I1 (0-35 pts) ───
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

        # ─── 5. Pillar 3: Participation Velocity & Depth Support (0-25 pts) ───
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

        # ─── BOS-strict momentum gate: weak break = discard ───
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
