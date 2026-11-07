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
        supply_dry_test_ratio: float = 0.60,
        dump_threshold_ratio: float = 1.25,
        absorption_wick_min: float = 0.30,
        max_opposing_delta_ratio: float = 2.2,
        max_liquidation_ratio: float = 1.5,
        min_liquidation_volume: float = 500.0,
        breakout_imbalance_ratio: float = 2.0,
    ):
        """
        Args:
            mode: "ACTIVE" (enforces vetoes), "SHADOW" (evaluates & logs only), "DISABLED".
            baseline_bars: Number of prior bars for rolling average volume.
            pullback_dry_ratio: Pullback VDR threshold below which retracement is healthy (< 0.85).
            supply_dry_test_ratio: Deep volume dry-up ratio proving supply exhaustion (< 0.60).
            dump_threshold_ratio: Pullback VDR threshold above which retracement is a dump (>= 1.25).
            absorption_wick_min: Minimum wick ratio to qualify as an absorption pin bar.
            max_opposing_delta_ratio: Ratio of opposing option volume that triggers delta divergence.
            max_liquidation_ratio: Ratio of sell volume vs buy volume that triggers liquidation dump veto.
            min_liquidation_volume: Minimum volume threshold to trigger liquidation dump or imbalance checks.
            breakout_imbalance_ratio: Minimum aggressor volume multiple (strictly 2.0x) on breakout bars.
        """
        self.mode = mode.upper()
        self.baseline_bars = int(baseline_bars)
        self.pullback_dry_ratio = float(pullback_dry_ratio)
        self.supply_dry_test_ratio = float(supply_dry_test_ratio)
        self.dump_threshold_ratio = float(dump_threshold_ratio)
        self.absorption_wick_min = float(absorption_wick_min)
        self.max_opposing_delta_ratio = float(max_opposing_delta_ratio)
        self.max_liquidation_ratio = float(max_liquidation_ratio)
        self.min_liquidation_volume = float(min_liquidation_volume)
        self.breakout_imbalance_ratio = float(breakout_imbalance_ratio)

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

        base_metrics: Dict[str, Any] = {}
        pa_name = str((pa_details or {}).get("pa_setup_name", ""))
        mode_val = str((pa_details or {}).get("mode", "")).upper()
        is_inflight = pa_name.startswith(("mode_b_", "mode_a_")) or mode_val.startswith(("MODE_A", "MODE_B"))
        is_up = direction.upper() in ("UP", "CALL")

        if is_inflight:
            pillar_id = (pa_details or {}).get("pillar") or (pa_details or {}).get("pillar_id")
            is_p1 = (pillar_id == 1) or ("20_ema" in pa_name)
            live_delta = float((pa_details or {}).get("opt_delta_flow", 0.0))
            live_vol = float((pa_details or {}).get("opt_total_vol", 0.0))
            n1_fp = (pa_details or {}).get("n1_footprint", {}) if is_p1 else {}
            vdr_val = 0.50 if n1_fp.get("n1_vol_dryup", False) else 1.0
            absorption = 2.0 if n1_fp.get("n1_stopping_vol", False) else 1.0

            # Explicit 3-State In-Flight Flow Contract:
            # 1. Aligned Flow: Delta matches trade direction -> Approved & wyckoff_confirmed=True
            # 2. Opposing Flow: Delta swamps against trade direction -> Hard Veto & wyckoff_confirmed=False
            # 3. Neutral / Zero Volume: Fails-open for synthetic/cold start -> Passed, but wyckoff_confirmed=False
            is_opposing = (live_delta < 0 and live_vol > 0) if is_up else (live_delta > 0 and live_vol > 0)
            is_aligned = (live_delta > 0 and live_vol > 0) if is_up else (live_delta < 0 and live_vol > 0)

            base_metrics = {
                "in_flight_validated": True,
                "setup_name": pa_name or mode_val,
                "atr": round(max(float(atr or 10.0), 1.0), 2),
                "opt_delta_flow": round(live_delta, 1),
                "opt_total_vol": round(live_vol, 1),
                "ofi_delta": round(live_delta, 1),
                "flow_state": "opposing" if is_opposing else ("aligned" if is_aligned else "neutral"),
                "wyckoff_confirmed": bool(is_aligned),
            }
            if is_p1:
                base_metrics["vdr"] = round(vdr_val, 2)
                base_metrics["absorption_score"] = round(absorption, 1)
                base_metrics["n1_footprint"] = n1_fp

            if is_opposing:
                return False, "wyckoff_intrabar_opposing_flow_veto", {
                    **base_metrics,
                    "wyckoff_confirmed": False,
                    "flow_state": "opposing",
                }

        if not bars or len(bars) < 4:
            # Insufficient bars at market open -> fail open
            if is_inflight and base_metrics:
                return True, "wyckoff_intrabar_inflight_confirmed" if base_metrics.get("wyckoff_confirmed") else "wyckoff_intrabar_neutral_flow", base_metrics
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
            passed, reason, m = self._evaluate_pillar_1_pullback(is_up, bars, v_base, eff_atr, pa_details=pa_details)
        elif pillar == 2:
            passed, reason, m = self._evaluate_pillar_2_sweep(is_up, bars, v_base, eff_atr, pa_details)
        elif pillar in (3, 4, 5):
            passed, reason, m = self._evaluate_pillar_breakout(is_up, bars, v_base, eff_atr, pillar)
        else:
            passed, reason, m = True, "unsupported_pillar_pass", {}

        metrics.update(m)
        if base_metrics:
            live_delta_val = base_metrics.get("opt_delta_flow", 0.0)
            metrics["live_opt_delta"] = live_delta_val
            metrics["opt_delta_flow"] = live_delta_val
            metrics.update({k: v for k, v in base_metrics.items() if k not in metrics or k in ("opt_total_vol", "ofi_delta")})
            if base_metrics.get("wyckoff_confirmed"):
                metrics["wyckoff_confirmed"] = True

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
        pa_details: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Pillar 1: 20 EMA Pullback Wick Rejection.
        Evaluates 2-Stage Sequence:
        1. Approach (T-3 to T-1): Volume Dry-Up (VDR < 0.85) vs Institutional Dump (VDR >= 1.25 rising).
        2. Touch & Rejection (Bar T): Stopping Volume pop and Absorption Wick.
        """
        curr_bar = bars[-1]
        vol_curr = float(getattr(curr_bar, "volume", 0.0) or 0.0)

        # ─── PATH A: Classical Dow Theory Swing vs Retracement Volume & Flow ───
        has_dow_swings = False
        imp_avg_vol = 0.0
        pb_avg_vol = 0.0
        entry_vol = 0.0
        swing_vdr = 1.0
        imp_directional_flow = 0.0
        pb_counter_flow = 0.0

        if pa_details:
            imp_avg_vol = float(pa_details.get("imp_avg_vol", 0.0) or 0.0)
            pb_avg_vol = float(pa_details.get("pb_avg_vol", 0.0) or 0.0)
            entry_vol = float(pa_details.get("entry_vol", 0.0) or 0.0)
            swing_vdr = float(pa_details.get("swing_vdr", 0.0) or 0.0)
            imp_directional_flow = float(pa_details.get("imp_directional_flow", 0.0) or 0.0)
            pb_counter_flow = float(pa_details.get("pb_counter_flow", 0.0) or 0.0)
            if imp_avg_vol > 0 and pb_avg_vol > 0:
                has_dow_swings = True

        if has_dow_swings:
            # True Swing VDR: Retracement Average Volume / Impulse Upswing Average Volume
            vdr = swing_vdr if swing_vdr > 0 else (pb_avg_vol / imp_avg_vol)

            # ─── VETO CHECK 1: Pullback Volume Dump ───
            if vdr >= self.dump_threshold_ratio:
                return False, "wyckoff_dump_pullback_exceeds_upswing", {
                    "vdr": round(vdr, 2),
                    "imp_avg_vol": round(imp_avg_vol, 1),
                    "pb_avg_vol": round(pb_avg_vol, 1),
                    "verdict": "INSTITUTIONAL_DUMP",
                }

            # ─── VETO CHECK 2: Option Flow Divergence Across Swing ───
            if imp_directional_flow > 0 and pb_counter_flow > 1.25 * imp_directional_flow:
                return False, "wyckoff_pullback_counter_flow_dominance", {
                    "vdr": round(vdr, 2),
                    "imp_flow": round(imp_directional_flow, 1),
                    "pb_counter_flow": round(pb_counter_flow, 1),
                    "ratio": round(pb_counter_flow / imp_directional_flow, 2),
                    "verdict": "COUNTER_FLOW_DOMINANCE",
                }

            # ─── VETO CHECK 2b: Opposing Delta Surge on Entry Bar ───
            cb_curr = float(getattr(curr_bar, "call_buy_vol", 0.0) or 0.0)
            pb_curr = float(getattr(curr_bar, "put_buy_vol", 0.0) or 0.0)
            cs_curr = float(getattr(curr_bar, "call_sell_vol", 0.0) or 0.0)
            ps_curr = float(getattr(curr_bar, "put_sell_vol", 0.0) or 0.0)
            if cb_curr > 0 or pb_curr > 0:
                if is_up and pb_curr > (self.max_opposing_delta_ratio * max(cb_curr, 1.0)):
                    return False, "wyckoff_opposing_put_delta_surge", {
                        "call_buy": cb_curr,
                        "put_buy": pb_curr,
                        "ratio": round(pb_curr / max(cb_curr, 1.0), 2),
                    }
                elif (not is_up) and cb_curr > (self.max_opposing_delta_ratio * max(pb_curr, 1.0)):
                    return False, "wyckoff_opposing_call_delta_surge", {
                        "call_buy": cb_curr,
                        "put_buy": pb_curr,
                        "ratio": round(cb_curr / max(pb_curr, 1.0), 2),
                    }

            # ─── VETO CHECK 2c: Liquidation Unwinding Dump ───
            if is_up:
                if cs_curr > (self.max_liquidation_ratio * max(cb_curr, 1.0)) and cs_curr >= self.min_liquidation_volume:
                    return False, "wyckoff_call_liquidation_dump", {
                        "call_buy": cb_curr,
                        "call_sell": cs_curr,
                        "verdict": "CALL_LIQUIDATION_DUMP",
                    }
            else:
                if ps_curr > (self.max_liquidation_ratio * max(pb_curr, 1.0)) and ps_curr >= self.min_liquidation_volume:
                    return False, "wyckoff_put_liquidation_dump", {
                        "put_buy": pb_curr,
                        "put_sell": ps_curr,
                        "verdict": "PUT_LIQUIDATION_DUMP",
                    }

            # ─── CONFIRMATION: Dow Swing Volume Retracement & Entry Volume Check ───
            synth_delta = (cb_curr + ps_curr) - (pb_curr + cs_curr)
            prev_bar = bars[-2] if len(bars) >= 2 else None
            delta_n1 = 0.0
            delta_curr = cb_curr - pb_curr
            delta_flip_confirmed = False
            if prev_bar:
                pb_cb = float(getattr(prev_bar, "call_buy_vol", 0.0) or 0.0)
                pb_pb = float(getattr(prev_bar, "put_buy_vol", 0.0) or 0.0)
                delta_n1 = pb_cb - pb_pb
                prev_has_opt = (pb_cb > 0 or pb_pb > 0)
                curr_has_opt = (cb_curr > 0 or pb_curr > 0)
                if prev_has_opt and curr_has_opt:
                    if is_up:
                        delta_flip_confirmed = bool(delta_n1 <= 0.0 and delta_curr >= 0.0)
                    else:
                        delta_flip_confirmed = bool(delta_n1 >= 0.0 and delta_curr <= 0.0)
            if vdr <= 1.05 and (vol_curr >= 0.70 * pb_avg_vol or vol_curr == 0.0):
                verdict = "SUPPLY_DRYUP_TEST" if vdr <= self.supply_dry_test_ratio else "DOW_SWING_VOLUME_CONFIRMED"
                metrics = {
                    "vdr": round(vdr, 2),
                    "imp_avg_vol": round(imp_avg_vol, 1),
                    "pb_avg_vol": round(pb_avg_vol, 1),
                    "entry_vol": round(vol_curr, 1),
                    "absorption_score": 1.0,
                    "opt_delta_flow": round(cb_curr - pb_curr, 1),
                    "live_opt_delta": round(cb_curr - pb_curr, 1),
                    "n1_opt_delta": round(delta_n1, 1),
                    "delta_n1": round(delta_n1, 1),
                    "delta_curr": round(delta_curr, 1),
                    "delta_flip_confirmed": delta_flip_confirmed,
                    "wyckoff_confirmed": bool(delta_flip_confirmed or (vdr <= 0.85)),
                    "synthetic_net_delta": round(synth_delta, 1),
                    "call_buy": cb_curr,
                    "call_sell": cs_curr,
                    "put_buy": pb_curr,
                    "put_sell": ps_curr,
                    "verdict": verdict,
                }
                return True, "wyckoff_dow_swing_volume_confirmed", metrics

            spread = float(curr_bar.high - curr_bar.low)
            body = abs(curr_bar.close - curr_bar.open)
            body_ratio = body / max(spread, 1e-4)
            is_directional_bar = (curr_bar.close > curr_bar.open) if is_up else (curr_bar.close < curr_bar.open)

            if is_directional_bar and body_ratio >= 0.40 and vol_curr >= 0.85 * pb_avg_vol:
                metrics = {
                    "vdr": round(vdr, 2),
                    "imp_avg_vol": round(imp_avg_vol, 1),
                    "pb_avg_vol": round(pb_avg_vol, 1),
                    "entry_vol": round(vol_curr, 1),
                    "absorption_score": round((vol_curr / pb_avg_vol), 2) if pb_avg_vol > 0 else 1.0,
                    "opt_delta_flow": round(cb_curr - pb_curr, 1),
                    "synthetic_net_delta": round(synth_delta, 1),
                    "call_buy": cb_curr,
                    "call_sell": cs_curr,
                    "put_buy": pb_curr,
                    "put_sell": ps_curr,
                    "verdict": "EXPANSION_RETEST_CONFIRMATION",
                }
                return True, "wyckoff_expansion_retest_confirmed", metrics

            return False, "wyckoff_pullback_no_absorption_nor_dryup", {
                "vdr": round(vdr, 2),
                "imp_avg_vol": round(imp_avg_vol, 1),
                "pb_avg_vol": round(pb_avg_vol, 1),
                "entry_vol": round(vol_curr, 1),
                "verdict": "PULLBACK_LACKS_DRYUP_AND_ABSORPTION",
            }

        # ─── PATH B: Legacy Rolling Baseline Fallback ───
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
        cs_curr = float(getattr(curr_bar, "call_sell_vol", 0.0) or 0.0)
        ps_curr = float(getattr(curr_bar, "put_sell_vol", 0.0) or 0.0)
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

        # ─── VETO CHECK 2b: Liquidation Unwinding Dump (CallSell / PutSell Trap) ───
        if is_up:
            if cs_curr > (self.max_liquidation_ratio * max(cb_curr, 1.0)) and cs_curr >= self.min_liquidation_volume:
                return False, "wyckoff_call_liquidation_dump", {
                    "call_buy": cb_curr,
                    "call_sell": cs_curr,
                    "verdict": "CALL_LIQUIDATION_DUMP",
                }
        else:
            if ps_curr > (self.max_liquidation_ratio * max(pb_curr, 1.0)) and ps_curr >= self.min_liquidation_volume:
                return False, "wyckoff_put_liquidation_dump", {
                    "put_buy": pb_curr,
                    "put_sell": ps_curr,
                    "verdict": "PUT_LIQUIDATION_DUMP",
                }

        # ─── CONFIRMATION: Wyckoff Effort vs Result (Absorption at 20 EMA) ───
        spread = float(curr_bar.high - curr_bar.low)
        vol_curr = float(getattr(curr_bar, "volume", 0.0) or 0.0)
        norm_spread = max(spread / atr, 0.1)
        absorption_score = (vol_curr / v_base) / norm_spread if (v_base > 0 and norm_spread > 0) else 1.0

        body = abs(curr_bar.close - curr_bar.open)
        body_ratio = body / max(spread, 1e-4)

        is_directional_bar = (curr_bar.close > curr_bar.open) if is_up else (curr_bar.close < curr_bar.open)

        # Prior bar wick calculation for two-bar retest support (valid when current bar confirms bounce in direction)
        prev_bar = bars[-2] if len(bars) >= 2 else None
        prev_wick = 0.0
        if prev_bar and is_directional_bar:
            prev_spread = max(float(prev_bar.high - prev_bar.low), 1e-4)
            if is_up:
                prev_wick = (min(prev_bar.open, prev_bar.close) - prev_bar.low) / prev_spread
            else:
                prev_wick = (prev_bar.high - max(prev_bar.open, prev_bar.close)) / prev_spread

        # Wick and Asymmetric Effort calculation
        if is_up:
            lower_wick = min(curr_bar.open, curr_bar.close) - curr_bar.low
            wick_ratio = lower_wick / max(spread, 1e-4)
            opposing_effort = pb_curr + cs_curr
            supporting_effort = cb_curr + ps_curr
        else:
            upper_wick = curr_bar.high - max(curr_bar.open, curr_bar.close)
            wick_ratio = upper_wick / max(spread, 1e-4)
            opposing_effort = cb_curr + ps_curr
            supporting_effort = pb_curr + cs_curr

        eff_wick_ratio = max(wick_ratio, prev_wick)
        norm_opposing = (opposing_effort / max(v_base, 1.0)) if v_base > 0 else 0.0
        asym_absorption_score = norm_opposing / norm_spread if norm_spread > 0 else 0.0

        # Supply Dry-Up Test (Law of Cause and Effect)
        is_deep_dry_test = (vdr <= self.supply_dry_test_ratio)
        is_moderate_dry = (vdr <= self.pullback_dry_ratio) and (vol_slope <= 0)
        is_dry_approach = is_deep_dry_test or is_moderate_dry

        # Stopping Volume / Asymmetric Absorption at 20 EMA (Supports Two-Bar Expansion/Retest)
        is_asym_absorption = (eff_wick_ratio >= self.absorption_wick_min) and (
            (asym_absorption_score >= 1.0) or (absorption_score >= 1.20) or (vol_curr >= v_base * 0.9)
        )
        is_expansion_retest = is_directional_bar and (body_ratio >= 0.40) and (vol_curr >= v_base * 0.85) and (supporting_effort >= opposing_effort)
        is_stopping_vol = is_asym_absorption or (absorption_score >= 1.2 and eff_wick_ratio >= self.absorption_wick_min) or is_expansion_retest

        # ─── VETO CHECK 3: Supply Dry-Up or Stopping Absorption Requirement ───
        # In Wyckoff theory, if volume didn't dry up (supply still heavy), and there was no absorption
        # (no stopping volume with prominent rejection wick), then the pullback is NOT ready for continuation!
        if (v_base > 0 and vol_curr > 0) and (not is_dry_approach) and (not is_stopping_vol):
            return False, "wyckoff_pullback_no_absorption_nor_dryup", {
                "vdr": round(vdr, 2),
                "vol_slope": round(vol_slope, 1),
                "wick_ratio": round(eff_wick_ratio, 2),
                "absorption_score": round(absorption_score, 2),
                "asym_absorption_score": round(asym_absorption_score, 2),
                "verdict": "PULLBACK_LACKS_DRYUP_AND_ABSORPTION",
            }

        synth_delta = (cb_curr + ps_curr) - (pb_curr + cs_curr)
        prev_bar = bars[-2] if len(bars) >= 2 else None
        delta_n1 = 0.0
        delta_curr = cb_curr - pb_curr
        delta_flip_confirmed = False
        if prev_bar:
            pb_cb = float(getattr(prev_bar, "call_buy_vol", 0.0) or 0.0)
            pb_pb = float(getattr(prev_bar, "put_buy_vol", 0.0) or 0.0)
            delta_n1 = pb_cb - pb_pb
            prev_has_opt = (pb_cb > 0 or pb_pb > 0)
            curr_has_opt = (cb_curr > 0 or pb_curr > 0)
            if prev_has_opt and curr_has_opt:
                if is_up:
                    delta_flip_confirmed = bool(delta_n1 <= 0.0 and delta_curr >= 0.0)
                else:
                    delta_flip_confirmed = bool(delta_n1 >= 0.0 and delta_curr <= 0.0)

        verdict = "WYCKOFF_DELTA_FLIP_REJECTION" if delta_flip_confirmed else (
            "ASYMMETRIC_STOPPING_ABSORPTION" if is_asym_absorption else (
                "EXPANSION_RETEST_CONFIRMATION" if is_expansion_retest else (
                    "SUPPLY_DRYUP_TEST" if is_deep_dry_test else "WYCKOFF_PULLBACK_EXHAUSTION"
                )
            )
        )

        metrics = {
            "vdr": round(vdr, 2),
            "vol_slope": round(vol_slope, 1),
            "wick_ratio": round(eff_wick_ratio, 2),
            "body_ratio": round(body_ratio, 2),
            "absorption_score": round(absorption_score, 2),
            "asym_absorption_score": round(asym_absorption_score, 2),
            "is_dry_approach": is_dry_approach,
            "is_deep_dry_test": is_deep_dry_test,
            "is_stopping_vol": is_stopping_vol,
            "is_expansion_retest": is_expansion_retest,
            "opt_delta_flow": round(cb_curr - pb_curr, 1),
            "live_opt_delta": round(cb_curr - pb_curr, 1),
            "n1_opt_delta": round(delta_n1, 1),
            "delta_n1": round(delta_n1, 1),
            "delta_curr": round(delta_curr, 1),
            "delta_flip_confirmed": delta_flip_confirmed,
            "wyckoff_confirmed": bool(delta_flip_confirmed or is_dry_approach or is_stopping_vol),
            "synthetic_net_delta": round(synth_delta, 1),
            "call_buy": cb_curr,
            "call_sell": cs_curr,
            "put_buy": pb_curr,
            "put_sell": ps_curr,
            "verdict": verdict,
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
        cs_curr = float(getattr(curr_bar, "call_sell_vol", 0.0) or 0.0)
        ps_curr = float(getattr(curr_bar, "put_sell_vol", 0.0) or 0.0)

        # Check opposing option volume runaway
        if (cb_curr + pb_curr) >= self.min_liquidation_volume:
            if is_up and pb_curr > (1.5 * max(cb_curr, 1.0)):
                return False, "wyckoff_p2_opposing_put_dominance", {
                    "call_buy": cb_curr,
                    "put_buy": pb_curr,
                    "verdict": "SPRING_OPPOSING_PUT_DOMINANCE",
                }
            elif (not is_up) and cb_curr > (1.5 * max(pb_curr, 1.0)):
                return False, "wyckoff_p2_opposing_call_dominance", {
                    "call_buy": cb_curr,
                    "put_buy": pb_curr,
                    "verdict": "UPTHRUST_OPPOSING_CALL_DOMINANCE",
                }
        else:
            if is_up and pb_curr > (self.max_opposing_delta_ratio * max(cb_curr, 1.0)):
                return False, "wyckoff_spring_failed_heavy_put_dump", {
                    "call_buy": cb_curr,
                    "put_buy": pb_curr,
                }
            elif (not is_up) and cb_curr > (self.max_opposing_delta_ratio * max(pb_curr, 1.0)):
                return False, "wyckoff_upthrust_failed_heavy_call_expansion", {
                    "call_buy": cb_curr,
                    "put_buy": pb_curr,
                }

        # Check liquidation dump on spring/upthrust (CallSell / PutSell Trap)
        if is_up and cs_curr > (self.max_liquidation_ratio * max(cb_curr, 1.0)) and cs_curr >= self.min_liquidation_volume:
            return False, "wyckoff_spring_failed_call_liquidation", {
                "call_buy": cb_curr,
                "call_sell": cs_curr,
                "verdict": "SPRING_CALL_LIQUIDATION",
            }
        elif (not is_up) and ps_curr > (self.max_liquidation_ratio * max(pb_curr, 1.0)) and ps_curr >= self.min_liquidation_volume:
            return False, "wyckoff_upthrust_failed_put_liquidation", {
                "put_buy": pb_curr,
                "put_sell": ps_curr,
                "verdict": "UPTHRUST_PUT_LIQUIDATION",
            }

        # ─── VETO 3: Spring/Upthrust Wick Absorption Requirement ───
        # A true Wyckoff spring MUST show rejection of lower prices (prominent lower wick).
        # An upthrust MUST show rejection of higher prices (prominent upper wick).
        if is_up:
            lower_wick = min(curr_bar.open, curr_bar.close) - curr_bar.low
            wick_ratio = lower_wick / max(spread, 1e-4)
            if spread > 0 and wick_ratio < self.absorption_wick_min:
                return False, "wyckoff_spring_failed_insufficient_wick_absorption", {
                    "wick_ratio": round(wick_ratio, 2),
                    "min_required": self.absorption_wick_min,
                    "verdict": "SPRING_INSUFFICIENT_ABSORPTION_WICK",
                }
        else:
            upper_wick = curr_bar.high - max(curr_bar.open, curr_bar.close)
            wick_ratio = upper_wick / max(spread, 1e-4)
            if spread > 0 and wick_ratio < self.absorption_wick_min:
                return False, "wyckoff_upthrust_failed_insufficient_wick_absorption", {
                    "wick_ratio": round(wick_ratio, 2),
                    "min_required": self.absorption_wick_min,
                    "verdict": "UPTHRUST_INSUFFICIENT_ABSORPTION_WICK",
                }

        # ─── VETO 4 & CONFIRMATION: Delta Flip Velocity (Wyckoff Spring & Upthrust) ───
        # In a true Wyckoff Spring (CALL):
        # Bar N-1 (the sweep bar) flushes liquidity on panic selling -> Delta N-1 <= 0.
        # Bar N (reclaim bar) sees aggressive institutional buying -> Delta N >= 0.
        # In a true Wyckoff Upthrust (PUT):
        # Bar N-1 flushes buyers on FOMO greed -> Delta N-1 >= 0.
        # Bar N sees aggressive institutional selling -> Delta N <= 0.
        prev_bar = bars[-2] if len(bars) >= 2 else None
        delta_n1 = 0.0
        delta_curr = cb_curr - pb_curr
        delta_flip_confirmed = False
        if prev_bar:
            pb_cb = float(getattr(prev_bar, "call_buy_vol", 0.0) or 0.0)
            pb_pb = float(getattr(prev_bar, "put_buy_vol", 0.0) or 0.0)
            delta_n1 = pb_cb - pb_pb
            prev_has_opt = (pb_cb > 0 or pb_pb > 0)
            curr_has_opt = (cb_curr > 0 or pb_curr > 0)
            if prev_has_opt and curr_has_opt:
                if is_up:
                    delta_flip_confirmed = bool(delta_n1 <= 0.0 and delta_curr >= 0.0)
                else:
                    delta_flip_confirmed = bool(delta_n1 >= 0.0 and delta_curr <= 0.0)
            else:
                delta_flip_confirmed = False
        else:
            delta_flip_confirmed = False

        vol_curr = float(getattr(curr_bar, "volume", 0.0) or 0.0)
        vdr = (vol_curr / v_base) if v_base > 0 else 1.0
        norm_spread = max(spread / atr, 0.1)
        absorption_score = (vol_curr / v_base) / norm_spread if (v_base > 0 and norm_spread > 0) else 1.0
        synth_delta = (cb_curr + ps_curr) - (pb_curr + cs_curr)

        metrics = {
            "vdr": round(vdr, 2),
            "wick_ratio": round(wick_ratio, 2),
            "absorption_score": round(absorption_score, 2),
            "opt_delta_flow": round(delta_curr, 1),
            "live_opt_delta": round(delta_curr, 1),
            "n1_opt_delta": round(delta_n1, 1),
            "delta_n1": round(delta_n1, 1),
            "delta_curr": round(delta_curr, 1),
            "delta_flip_confirmed": delta_flip_confirmed,
            "wyckoff_confirmed": bool(delta_flip_confirmed or (vdr >= 1.0)),
            "synthetic_net_delta": round(synth_delta, 1),
            "call_buy": cb_curr,
            "call_sell": cs_curr,
            "put_buy": pb_curr,
            "put_sell": ps_curr,
            "verdict": ("WYCKOFF_DELTA_FLIP_SPRING" if delta_flip_confirmed else "WYCKOFF_SPRING_ABSORPTION") if is_up else (
                "WYCKOFF_DELTA_FLIP_UPTHRUST" if delta_flip_confirmed else "WYCKOFF_UPTHRUST_ABSORPTION"
            ),
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
        1. Non-zero Volume Expansion (V_curr >= 0.40 * V_base).
        2. Delta Harmony: Directional option aggressors align with breakout (no opposing swamp).
        3. Liquidation Protection: Opposing liquidation dump does not exceed thresholds.
        4. Delta Imbalance: Directional volume achieves at least 2.0x of opposing side on active bars.
        5. Synthetic Net Delta Confirmation: (CallBuy + PutSell) > (PutBuy + CallSell) for Call, etc.
        """
        curr_bar = bars[-1]
        vol_curr = float(getattr(curr_bar, "volume", 0.0) or 0.0)
        vdr = (vol_curr / v_base) if v_base > 0 else 1.0

        cb_curr = float(getattr(curr_bar, "call_buy_vol", 0.0) or 0.0)
        pb_curr = float(getattr(curr_bar, "put_buy_vol", 0.0) or 0.0)
        cs_curr = float(getattr(curr_bar, "call_sell_vol", 0.0) or 0.0)
        ps_curr = float(getattr(curr_bar, "put_sell_vol", 0.0) or 0.0)

        bullish_vol = cb_curr + ps_curr
        bearish_vol = pb_curr + cs_curr
        synth_delta = bullish_vol - bearish_vol

        # ─── VETO 1: Delta Divergence on Breakout (Bull/Bear Trap) ───
        if is_up:
            # Call Breakout but massive Put Aggression -> Bull Trap
            if pb_curr > (self.max_opposing_delta_ratio * max(cb_curr, 1.0)) and pb_curr > 500:
                return False, "wyckoff_breakout_delta_divergence_bear_swamp", {
                    "call_buy": cb_curr,
                    "put_buy": pb_curr,
                    "verdict": "BULL_TRAP_DELTA_DIVERGENCE",
                }
            # Call Breakout but massive Call Liquidation Dumping -> Bull Trap
            if cs_curr > (self.max_liquidation_ratio * max(cb_curr, 1.0)) and cs_curr >= self.min_liquidation_volume:
                return False, "wyckoff_breakout_call_liquidation_dump", {
                    "call_buy": cb_curr,
                    "call_sell": cs_curr,
                    "verdict": "BULL_TRAP_CALL_LIQUIDATION",
                }
            # Positive Delta Imbalance Check (Strictly >= 2.0x) on non-trivial active volume
            if (cb_curr + pb_curr) >= self.min_liquidation_volume and cb_curr < (self.breakout_imbalance_ratio * max(pb_curr, 1.0)):
                return False, "wyckoff_breakout_insufficient_delta_imbalance", {
                    "call_buy": cb_curr,
                    "put_buy": pb_curr,
                    "ratio": round(cb_curr / max(pb_curr, 1.0), 2),
                    "min_required": self.breakout_imbalance_ratio,
                    "verdict": "INSUFFICIENT_DELTA_IMBALANCE",
                }
            # Synthetic Net Delta Negative Trap Veto
            if (bullish_vol + bearish_vol) >= self.min_liquidation_volume and synth_delta < 0:
                return False, "wyckoff_breakout_synthetic_delta_negative", {
                    "bullish_vol": bullish_vol,
                    "bearish_vol": bearish_vol,
                    "synthetic_net_delta": synth_delta,
                    "verdict": "BULL_TRAP_SYNTHETIC_DELTA_NEGATIVE",
                }
        else:
            # Put Breakdown but massive Call Aggression -> Bear Trap
            if cb_curr > (self.max_opposing_delta_ratio * max(pb_curr, 1.0)) and cb_curr > 500:
                return False, "wyckoff_breakdown_delta_divergence_bull_swamp", {
                    "call_buy": cb_curr,
                    "put_buy": pb_curr,
                    "verdict": "BEAR_TRAP_DELTA_DIVERGENCE",
                }
            # Put Breakdown but massive Put Liquidation Dumping -> Bear Trap
            if ps_curr > (self.max_liquidation_ratio * max(pb_curr, 1.0)) and ps_curr >= self.min_liquidation_volume:
                return False, "wyckoff_breakdown_put_liquidation_dump", {
                    "put_buy": pb_curr,
                    "put_sell": ps_curr,
                    "verdict": "BEAR_TRAP_PUT_LIQUIDATION",
                }
            # Positive Delta Imbalance Check (Strictly >= 2.0x) on non-trivial active volume
            if (cb_curr + pb_curr) >= self.min_liquidation_volume and pb_curr < (self.breakout_imbalance_ratio * max(cb_curr, 1.0)):
                return False, "wyckoff_breakdown_insufficient_delta_imbalance", {
                    "call_buy": cb_curr,
                    "put_buy": pb_curr,
                    "ratio": round(pb_curr / max(cb_curr, 1.0), 2),
                    "min_required": self.breakout_imbalance_ratio,
                    "verdict": "INSUFFICIENT_DELTA_IMBALANCE",
                }
            # Synthetic Net Delta Positive Trap Veto
            if (bullish_vol + bearish_vol) >= self.min_liquidation_volume and synth_delta > 0:
                return False, "wyckoff_breakdown_synthetic_delta_positive", {
                    "bullish_vol": bullish_vol,
                    "bearish_vol": bearish_vol,
                    "synthetic_net_delta": synth_delta,
                    "verdict": "BEAR_TRAP_SYNTHETIC_DELTA_POSITIVE",
                }

        # ─── VETO 2: Low-Volume Phantom Breakout ───
        # Breakouts need participation; if volume is < 40% of baseline, it's a false drift
        if v_base > 0 and vol_curr > 0 and vdr < 0.40:
            return False, "wyckoff_phantom_breakout_no_participation", {
                "vdr": round(vdr, 2),
                "verdict": "PHANTOM_DRIFT",
            }

        # ─── N-1 Demand Ignition / Volume Expansion Ratio (VER) ───
        is_n1_directional = (curr_bar.close > curr_bar.open) if is_up else (curr_bar.close < curr_bar.open)
        n1_demand_ignition = bool(vdr >= 1.10 and is_n1_directional)

        # ─── Squeeze Box Volume Expansion (Pillar 3) ───
        box_expansion_ratio = 1.0
        squeeze_expansion_confirmed = False
        if pillar == 3 and len(bars) >= 6:
            box_bars = bars[-6:-1]
            box_vols = [float(getattr(b, "volume", 0.0) or 0.0) for b in box_bars]
            box_avg = sum(box_vols) / len(box_vols) if box_vols else 0.0
            if box_avg > 0:
                box_expansion_ratio = vol_curr / box_avg
                squeeze_expansion_confirmed = box_expansion_ratio >= 1.50

        n1_delta = round(cb_curr - pb_curr, 1)
        metrics = {
            "vdr": round(vdr, 2),
            "n1_opt_delta": n1_delta,
            "opt_delta_flow": n1_delta,
            "synthetic_net_delta": round(synth_delta, 1),
            "call_buy": cb_curr,
            "call_sell": cs_curr,
            "put_buy": pb_curr,
            "put_sell": ps_curr,
            "n1_ver": round(vdr, 2),
            "n1_demand_ignition": n1_demand_ignition,
            "wyckoff_confirmed": bool(n1_demand_ignition or (vdr >= 1.0)),
            "verdict": "WYCKOFF_DEMAND_IGNITION_CONFIRMED" if n1_demand_ignition else "WYCKOFF_SOS_HARMONY",
        }
        if pillar == 3:
            metrics["box_expansion_ratio"] = round(box_expansion_ratio, 2)
            metrics["squeeze_expansion_confirmed"] = squeeze_expansion_confirmed

        return True, "wyckoff_breakout_harmony_confirmed", metrics
