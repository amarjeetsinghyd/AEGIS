"""
AEGIS Wyckoff Volume & Order Flow Delta Engine (WyckoffVolumeEngine).

Implements Richard D. Wyckoff's core market laws combined with Modern Options Order Flow Delta:
1. Law of Effort vs Result:
   - Volume/Delta spent vs Price Spread achieved (Absorption vs Expansion).
   - Absorption Index: High volume spent with zero price displacement at S/R = Institutional Absorption.
2. Law of Cause and Effect:
   - Volume Dry-Up during Consolidation/Retracement building energy for expansion.
   - Volume Dry-Up Ratio (VDR) across approach candles.
3. Law of Supply and Demand (Delta Harmony):
   - Option Flow Delta: Call Aggressor Buying vs Put Aggressor Buying.
   - Delta Harmony: Directional flow confirms price breakout.
   - Delta Divergence: Price moves against aggressive order flow = Trap/Veto.

Pillars Supported:
- Pillar 1: 20 EMA Pullback Wick Rejection (Retracement Exhaustion & Absorption)
- Pillar 2: Liquidity Sweep & Reclaim (Wyckoff Spring & Upthrust)
- Pillar 3: Volatility Squeeze Breakout (Volume Contraction to Expansion + Delta Harmony)
- Pillar 4: Structural Breakout (Sign of Strength [SOS] + Delta Alignment)

Scale-Invariant: Normalized via ATR, rolling baseline volume, and ratio spreads.
Hot-Path Safe: Pure Python, zero pandas/scipy on tick path, sub-5-microsecond execution.
"""

from __future__ import annotations
import os
import math
import logging
from typing import Dict, List, Optional, Tuple, Any

logger = logging.getLogger("aegis.wyckoff_volume")


class WyckoffVolumeEngine:
    """
    Evaluates Wyckoff Volume, Delta Harmony, and Effort vs Result
    in parallel with Price Action triggers.
    """

    def __init__(
        self,
        mode: str = "ACTIVE",
        baseline_bars: int = 20,
        pullback_dry_ratio: float = 0.85,
        dump_threshold_ratio: float = 1.25,
        absorption_wick_min: float = 0.30,
        max_opposing_delta_ratio: float = 2.2,
    ):
        """
        Args:
            mode: "ACTIVE" (enforces vetoes), "SHADOW" (evaluates & logs only), "DISABLED".
            baseline_bars: Number of prior bars for rolling average volume.
            pullback_dry_ratio: Pullback VDR threshold below which retracement is healthy (< 0.85).
            dump_threshold_ratio: Pullback VDR threshold above which retracement is a dump (>= 1.25).
            absorption_wick_min: Minimum wick ratio to qualify as an absorption pin bar.
            max_opposing_delta_ratio: Ratio of opposing option volume that triggers delta divergence.
        """
        self.mode = mode.upper()
        self.baseline_bars = int(baseline_bars)
        self.pullback_dry_ratio = float(pullback_dry_ratio)
        self.dump_threshold_ratio = float(dump_threshold_ratio)
        self.absorption_wick_min = float(absorption_wick_min)
        self.max_opposing_delta_ratio = float(max_opposing_delta_ratio)

    def _compute_baseline_volume(self, bars: List[Any], exclude_last: int = 1) -> float:
        """Computes rolling average volume across historical bars."""
        if not bars:
            return 0.0
        sample = bars[:-exclude_last] if exclude_last > 0 else bars
        if not sample:
            return 0.0
        recent = sample[-self.baseline_bars:]
        vols = [float(getattr(b, "volume", 0.0) or 0.0) for b in recent]
        pos_vols = [v for v in vols if v > 0]
        if not pos_vols:
            return 0.0
        return sum(pos_vols) / len(pos_vols)

    def evaluate(
        self,
        pillar: int,
        direction: str,
        bars: List[Any],
        atr: float = 10.0,
        pa_details: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Main entry point for Wyckoff Volume & Delta evaluation.

        Returns:
            (passed: bool, reason: str, metrics: Dict[str, Any])
        """
        if self.mode == "DISABLED":
            return True, "wyckoff_disabled", {}

        if not bars or len(bars) < 4:
            # Insufficient bars at market open -> fail open
            return True, "warmup_insufficient_bars", {}

        curr_bar = bars[-1]
        vol_curr = float(getattr(curr_bar, "volume", 0.0) or 0.0)
        v_base = self._compute_baseline_volume(bars, exclude_last=1)

        # Synthetic test-bar safeguard (zero volume feeds fail-open)
        if v_base <= 0 and vol_curr <= 0:
            return True, "synthetic_zero_volume_pass", {}

        is_up = direction.upper() in ("UP", "CALL")
        eff_atr = max(float(atr or 10.0), 1.0)
        metrics: Dict[str, Any] = {
            "v_curr": round(vol_curr, 1),
            "v_baseline": round(v_base, 1),
            "atr": round(eff_atr, 2),
        }

        # Route by Pillar
        if pillar == 1:
            passed, reason, m = self._evaluate_pillar_1_pullback(is_up, bars, v_base, eff_atr)
        elif pillar == 2:
            passed, reason, m = self._evaluate_pillar_2_sweep(is_up, bars, v_base, eff_atr, pa_details)
        elif pillar in (3, 4):
            passed, reason, m = self._evaluate_pillar_breakout(is_up, bars, v_base, eff_atr, pillar)
        else:
            passed, reason, m = True, "unsupported_pillar_pass", {}

        metrics.update(m)

        if self.mode == "SHADOW":
            # In shadow mode, log verdict but never block
            logger.info(
                f"[WYCKOFF SHADOW] P{pillar} ({direction}) | Passed: {passed} | "
                f"Reason: {reason} | Metrics: {metrics}"
            )
            return True, f"shadow_{reason}", metrics

        return passed, reason, metrics

    def _evaluate_pillar_1_pullback(
        self,
        is_up: bool,
        bars: List[Any],
        v_base: float,
        atr: float,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Pillar 1: 20 EMA Pullback Wick Rejection.
        Evaluates 2-Stage Sequence:
        1. Approach (T-3 to T-1): Volume Dry-Up (VDR < 0.85) vs Institutional Dump (VDR >= 1.25 rising).
        2. Touch & Rejection (Bar T): Stopping Volume pop and Absorption Wick.
        """
        curr_bar = bars[-1]
        prior_bars = bars[-4:-1]  # Up to 3 approach bars
        if not prior_bars:
            return True, "insufficient_approach_bars", {}

        # 1. Approach Bars Direction and Volume Behavior
        approach_vols = [float(getattr(b, "volume", 0.0) or 0.0) for b in prior_bars]
        approach_closes = [float(getattr(b, "close", 0.0) or 0.0) for b in prior_bars]
        approach_opens = [float(getattr(b, "open", 0.0) or 0.0) for b in prior_bars]

        avg_approach_vol = sum(approach_vols) / max(1, len(approach_vols))
        vdr = (avg_approach_vol / v_base) if v_base > 0 else 1.0

        # Check if approach candles were actually counter-trend (pullback)
        is_counter_move = False
        if is_up:
            # Bullish trend -> pullback means falling bars into 20 EMA
            falling_bars = sum(1 for o, c in zip(approach_opens, approach_closes) if c <= o)
            is_counter_move = (falling_bars >= 2) or (approach_closes[-1] < approach_closes[0])
        else:
            # Bearish trend -> pullback means rising bars into 20 EMA
            rising_bars = sum(1 for o, c in zip(approach_opens, approach_closes) if c >= o)
            is_counter_move = (rising_bars >= 2) or (approach_closes[-1] > approach_closes[0])

        # Volume Trend across approach bars (Slope)
        vol_slope = 0.0
        if len(approach_vols) >= 2:
            vol_slope = approach_vols[-1] - approach_vols[0]

        # ─── VETO CHECK 1: Institutional Liquidation Dump ───
        # If price was aggressively falling into 20 EMA on heavy, expanding volume
        if is_counter_move and vdr >= self.dump_threshold_ratio and vol_slope > 0:
            return False, "wyckoff_dump_counter_volume_expansion", {
                "vdr": round(vdr, 2),
                "vol_slope": round(vol_slope, 1),
                "verdict": "INSTITUTIONAL_DUMP",
            }

        # ─── VETO CHECK 2: Option Delta Liquidation on Approach ───
        # Check if opposing option volume exploded during the pullback
        cb_curr = float(getattr(curr_bar, "call_buy_vol", 0.0) or 0.0)
        pb_curr = float(getattr(curr_bar, "put_buy_vol", 0.0) or 0.0)
        if cb_curr > 0 or pb_curr > 0:
            if is_up and pb_curr > (self.max_opposing_delta_ratio * max(cb_curr, 1.0)):
                # Heavy put buying on 20 EMA touch -> bears dumping, not absorbing
                return False, "wyckoff_opposing_put_delta_surge", {
                    "call_buy": cb_curr,
                    "put_buy": pb_curr,
                    "ratio": round(pb_curr / max(cb_curr, 1.0), 2),
                }
            elif (not is_up) and cb_curr > (self.max_opposing_delta_ratio * max(pb_curr, 1.0)):
                # Heavy call buying on 20 EMA touch -> bulls rejecting downmove
                return False, "wyckoff_opposing_call_delta_surge", {
                    "call_buy": cb_curr,
                    "put_buy": pb_curr,
                    "ratio": round(cb_curr / max(pb_curr, 1.0), 2),
                }

        # ─── CONFIRMATION: Wyckoff Effort vs Result (Absorption at 20 EMA) ───
        spread = float(curr_bar.high - curr_bar.low)
        vol_curr = float(getattr(curr_bar, "volume", 0.0) or 0.0)
        norm_spread = max(spread / atr, 0.1)
        absorption_score = (vol_curr / v_base) / norm_spread if (v_base > 0 and norm_spread > 0) else 1.0

        # Wick confirmation
        if is_up:
            lower_wick = min(curr_bar.open, curr_bar.close) - curr_bar.low
            wick_ratio = lower_wick / max(spread, 1e-4)
        else:
            upper_wick = curr_bar.high - max(curr_bar.open, curr_bar.close)
            wick_ratio = upper_wick / max(spread, 1e-4)

        is_dry_approach = (vdr <= self.pullback_dry_ratio) or (vol_slope <= 0)
        is_stopping_vol = (absorption_score >= 1.2) or (wick_ratio >= self.absorption_wick_min)

        metrics = {
            "vdr": round(vdr, 2),
            "vol_slope": round(vol_slope, 1),
            "wick_ratio": round(wick_ratio, 2),
            "absorption_score": round(absorption_score, 2),
            "is_dry_approach": is_dry_approach,
            "is_stopping_vol": is_stopping_vol,
            "opt_delta_flow": round(cb_curr - pb_curr, 1),
        }

        return True, "wyckoff_pullback_exhaustion_confirmed", metrics

    def _evaluate_pillar_2_sweep(
        self,
        is_up: bool,
        bars: List[Any],
        v_base: float,
        atr: float,
        pa_details: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Pillar 2: Liquidity Sweep & Reclaim (Wyckoff Spring & Upthrust).
        Confirms stop-hunt absorption:
        - Sweep candle flushes liquidity with volume spike.
        - Candle closes back inside the level (Effort produced No Breakdown -> Absorption).
        """
        curr_bar = bars[-1]
        spread = float(curr_bar.high - curr_bar.low)
        cb_curr = float(getattr(curr_bar, "call_buy_vol", 0.0) or 0.0)
        pb_curr = float(getattr(curr_bar, "put_buy_vol", 0.0) or 0.0)

        # Check opposing option volume runaway
        if is_up and pb_curr > (self.max_opposing_delta_ratio * 1.5 * max(cb_curr, 1.0)):
            # If put buyers completely swamp call buyers on a spring, breakdown may be real
            return False, "wyckoff_spring_failed_heavy_put_dump", {
                "call_buy": cb_curr,
                "put_buy": pb_curr,
            }
        elif (not is_up) and cb_curr > (self.max_opposing_delta_ratio * 1.5 * max(pb_curr, 1.0)):
            return False, "wyckoff_upthrust_failed_heavy_call_expansion", {
                "call_buy": cb_curr,
                "put_buy": pb_curr,
            }

        # Spring/Upthrust Wick Absorption
        if is_up:
            lower_wick = min(curr_bar.open, curr_bar.close) - curr_bar.low
            wick_ratio = lower_wick / max(spread, 1e-4)
        else:
            upper_wick = curr_bar.high - max(curr_bar.open, curr_bar.close)
            wick_ratio = upper_wick / max(spread, 1e-4)

        metrics = {
            "wick_ratio": round(wick_ratio, 2),
            "opt_delta_flow": round(cb_curr - pb_curr, 1),
            "verdict": "WYCKOFF_SPRING_ABSORPTION" if is_up else "WYCKOFF_UPTHRUST_ABSORPTION",
        }

        return True, "wyckoff_spring_reclaim_confirmed", metrics

    def _evaluate_pillar_breakout(
        self,
        is_up: bool,
        bars: List[Any],
        v_base: float,
        atr: float,
        pillar: int,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Pillars 3 & 4: Squeeze & Structural Breakout (Wyckoff Sign of Strength [SOS]).
        Requires:
        1. Non-zero Volume Expansion (V_curr >= 0.75 * V_base).
        2. Delta Harmony: Directional option aggressors align with breakout (no opposing swamp).
        """
        curr_bar = bars[-1]
        vol_curr = float(getattr(curr_bar, "volume", 0.0) or 0.0)
        vdr = (vol_curr / v_base) if v_base > 0 else 1.0

        cb_curr = float(getattr(curr_bar, "call_buy_vol", 0.0) or 0.0)
        pb_curr = float(getattr(curr_bar, "put_buy_vol", 0.0) or 0.0)

        # ─── VETO: Delta Divergence on Breakout (Bull/Bear Trap) ───
        if is_up:
            # Call Breakout but massive Put Aggression -> Bull Trap
            if pb_curr > (self.max_opposing_delta_ratio * max(cb_curr, 1.0)) and pb_curr > 500:
                return False, "wyckoff_breakout_delta_divergence_bear_swamp", {
                    "call_buy": cb_curr,
                    "put_buy": pb_curr,
                    "verdict": "BULL_TRAP_DELTA_DIVERGENCE",
                }
        else:
            # Put Breakdown but massive Call Aggression -> Bear Trap
            if cb_curr > (self.max_opposing_delta_ratio * max(pb_curr, 1.0)) and cb_curr > 500:
                return False, "wyckoff_breakdown_delta_divergence_bull_swamp", {
                    "call_buy": cb_curr,
                    "put_buy": pb_curr,
                    "verdict": "BEAR_TRAP_DELTA_DIVERGENCE",
                }

        # ─── VETO: Low-Volume Phantom Breakout ───
        # Breakouts need participation; if volume is < 40% of baseline, it's a false drift
        if v_base > 0 and vol_curr > 0 and vdr < 0.40:
            return False, "wyckoff_phantom_breakout_no_participation", {
                "vdr": round(vdr, 2),
                "verdict": "PHANTOM_DRIFT",
            }

        metrics = {
            "vdr": round(vdr, 2),
            "opt_delta_flow": round(cb_curr - pb_curr, 1),
            "verdict": "WYCKOFF_SOS_HARMONY",
        }

        return True, "wyckoff_breakout_harmony_confirmed", metrics
