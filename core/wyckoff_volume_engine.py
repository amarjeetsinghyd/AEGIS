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

    def evaluate_inflight_flow(
        self,
        direction: str,
        flow_dict: Dict[str, Any],
        lot_size: float = 65.0,
        pillar: int = 1,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Single source of truth for in-flight option volume validation.
        Enforces Pillar-Aware Institutional Wyckoff Laws:
          - Pillar 1 (Pullback Reversal): N-1 Dry-Up & Stopping Wick protection + Non-swamped bounce.
          - Pillar 2 (Liquidity Sweep): Stop-run absorption confirmation (Opposing <= 2.2x).
          - Pillar 3 & 4 (Breakouts): Strict >= 3 lots volume floor + >= 1.50x directional dominance.
          - Pillar 5 (Gamma Trap): Trapped writer short-squeeze with Counter-Liquidation Shield (<= 2.5x).
        """
        is_up = direction.upper() in ("UP", "CALL")
        tot_flow = float(flow_dict.get("tot_flow", 0.0) or flow_dict.get("opt_total_vol", 0.0) or 0.0)
        delta_flow = float(flow_dict.get("opt_delta_flow", 0.0) or 0.0)

        bull_flow = float(flow_dict.get("bull_flow", 0.0) or 0.0)
        bear_flow = float(flow_dict.get("bear_flow", 0.0) or 0.0)
        if bull_flow <= 0.0 and bear_flow <= 0.0 and tot_flow > 0.0:
            bull_flow = max(0.0, (tot_flow + delta_flow) / 2.0)
            bear_flow = max(0.0, (tot_flow - delta_flow) / 2.0)
        if tot_flow <= 0.0 and (bull_flow > 0.0 or bear_flow > 0.0):
            tot_flow = bull_flow + bear_flow

        base_metrics = {
            "bull_flow": bull_flow,
            "bear_flow": bear_flow,
            "tot_flow": tot_flow,
            "opt_total_vol": tot_flow,
            "opt_delta_flow": delta_flow,
        }

        # 0. Synthetic Test / Cold Start / Zero Flow Handling
        # Live mode requires proven flow (> 0.0); synthetic tests only fail-open in non-ACTIVE mode
        if tot_flow <= 0.0:
            if self.mode == "ACTIVE":
                return False, "insufficient_intrabar_flow", {
                    **base_metrics,
                    "wyckoff_confirmed": False,
                    "flow_state": "insufficient",
                }
            return True, "wyckoff_intrabar_neutral_flow", {
                **base_metrics,
                "wyckoff_confirmed": False,
                "flow_state": "neutral",
            }

        eff_lot = float(lot_size) if lot_size and float(lot_size) > 0 else 65.0
        min_active_volume = 3.0 * eff_lot  # NIFTY ~195 Qty, SENSEX ~60 Qty

        # ══════════════════════════════════════════════════════════════════
        # PILLAR 1: 20 EMA Pullback Wick Rejection (Reversal & Exhaustion)
        # ══════════════════════════════════════════════════════════════════
        if pillar == 1:
            n1_fp = flow_dict.get("n1_footprint", {})
            n1_vol = float(n1_fp.get("n1_vol", 0.0) or 0.0)
            n1_base = float(n1_fp.get("n1_baseline_vol", 1.0) or 1.0)
            n1_stopping = bool(n1_fp.get("n1_stopping_vol", False))
            n1_dry = bool(n1_fp.get("n1_vol_dryup", False))

            # Veto 1: Unabsorbed volume dump on N-1 (spiked >= 1.25x without stopping absorption)
            if n1_base > 0 and n1_vol >= (1.25 * n1_base) and not n1_stopping:
                return False, "wyckoff_n1_unabsorbed_volume_dump", {
                    **base_metrics,
                    "n1_vol": n1_vol,
                    "n1_baseline": n1_base,
                    "verdict": "PULLBACK_DUMP_UNABSORBED",
                    "wyckoff_confirmed": False,
                }

            # Veto 2: Live Opposing Delta Swamp (bears swamp bulls on CALL by > 1.5x)
            if tot_flow >= min_active_volume:
                if is_up and bear_flow > (1.50 * max(bull_flow, 1.0)):
                    return False, "wyckoff_inflight_call_dominance_failed", {
                        **base_metrics,
                        "ratio": round(bull_flow / max(bear_flow, 1.0), 2),
                        "verdict": "VETO_WEAK_BULL_FLOW",
                        "wyckoff_confirmed": False,
                    }
                elif (not is_up) and bull_flow > (1.50 * max(bear_flow, 1.0)):
                    return False, "wyckoff_inflight_put_dominance_failed", {
                        **base_metrics,
                        "ratio": round(bear_flow / max(bull_flow, 1.0), 2),
                        "verdict": "VETO_WEAK_BEAR_FLOW",
                        "wyckoff_confirmed": False,
                    }

            # Confirmation: Confirmed if N-1 dried up, stopping vol formed, or directional delta holds
            is_confirmed = bool(n1_dry or n1_stopping or (is_up and delta_flow > 0) or ((not is_up) and delta_flow < 0))
            return True, "wyckoff_intrabar_inflight_confirmed" if is_confirmed else "wyckoff_intrabar_neutral_flow", {
                **base_metrics,
                "wyckoff_confirmed": is_confirmed,
                "flow_state": "aligned" if is_confirmed else "neutral",
            }

        # ══════════════════════════════════════════════════════════════════
        # PILLAR 2: Micro-Sweep Reclaim (Wyckoff Spring & Upthrust Absorption)
        # ══════════════════════════════════════════════════════════════════
        elif pillar == 2:
            # Veto: Severe Opposing Delta Swamp (> 2.2x opposing flow on non-trivial volume >= 8 lots)
            active_sweep_vol = bull_flow + bear_flow
            if active_sweep_vol >= 8.0 * eff_lot:
                if is_up and bear_flow > (self.max_opposing_delta_ratio * max(bull_flow, 1.0)):
                    return False, "wyckoff_p2_opposing_put_dominance", {
                        **base_metrics,
                        "ratio": round(bear_flow / max(bull_flow, 1.0), 2),
                        "verdict": "SPRING_OPPOSING_PUT_DOMINANCE",
                        "wyckoff_confirmed": False,
                    }
                elif (not is_up) and bull_flow > (self.max_opposing_delta_ratio * max(bear_flow, 1.0)):
                    return False, "wyckoff_p2_opposing_call_dominance", {
                        **base_metrics,
                        "ratio": round(bull_flow / max(bear_flow, 1.0), 2),
                        "verdict": "UPTHRUST_OPPOSING_CALL_DOMINANCE",
                        "wyckoff_confirmed": False,
                    }

            # Spring Reclaim Confirmation: absorption wick or positive reclaim flow confirms.
            # reclaim_flow > 0.0 required; falling back to directional delta gate only when reclaim is non-positive.
            sweep_shadow = flow_dict.get("sweep_shadow") or {}
            reclaim_flow = float(sweep_shadow.get("metrics", {}).get("reclaim_flow", 0.0))
            delta_aligned = (is_up and delta_flow > 0) or ((not is_up) and delta_flow < 0)
            is_confirmed = bool(reclaim_flow > 0.0 or delta_aligned)
            return True, "wyckoff_intrabar_inflight_confirmed" if is_confirmed else "wyckoff_intrabar_neutral_flow", {
                **base_metrics,
                "wyckoff_confirmed": is_confirmed,
                "flow_state": "aligned" if is_confirmed else "neutral",
            }

        # ══════════════════════════════════════════════════════════════════
        # PILLAR 5: Trapped Option Writer Short-Squeeze (Counter-Liquidation Shield)
        # ══════════════════════════════════════════════════════════════════
        elif pillar == 5:
            # Canonical Master Blueprint Rule: Opposing liquidation volume must not swamp trade direction (<= 2.5x)
            active_flow_floor = 15.0 * eff_lot
            if tot_flow > active_flow_floor:
                if is_up and bear_flow > (2.50 * max(bull_flow, 1.0)):
                    return False, "wyckoff_p5_counter_liquidation_veto", {
                        **base_metrics,
                        "ratio": round(bear_flow / max(bull_flow, 1.0), 2),
                        "verdict": "COUNTER_LIQUIDATION_SWAMP",
                        "wyckoff_confirmed": False,
                    }
                elif (not is_up) and bull_flow > (2.50 * max(bear_flow, 1.0)):
                    return False, "wyckoff_p5_counter_liquidation_veto", {
                        **base_metrics,
                        "ratio": round(bull_flow / max(bear_flow, 1.0), 2),
                        "verdict": "COUNTER_LIQUIDATION_SWAMP",
                        "wyckoff_confirmed": False,
                    }

            # Minimum active volume gate (3 lots) + 1.50x directional dominance + directional delta
            if tot_flow < min_active_volume:
                return False, "wyckoff_p5_insufficient_volume", {
                    **base_metrics,
                    "min_required": min_active_volume,
                    "verdict": "VETO_DRY_ORDER_FLOW",
                    "wyckoff_confirmed": False,
                    "flow_state": "insufficient_volume",
                }
            if is_up:
                if delta_flow <= 0.0:
                    return False, "wyckoff_p5_opposing_flow_veto", {
                        **base_metrics,
                        "verdict": "VETO_NEGATIVE_DELTA",
                        "wyckoff_confirmed": False,
                        "flow_state": "opposing",
                    }
                if bull_flow < (1.50 * max(bear_flow, 1.0)):
                    return False, "wyckoff_p5_call_dominance_failed", {
                        **base_metrics,
                        "verdict": "VETO_INSUFFICIENT_DOMINANCE",
                        "wyckoff_confirmed": False,
                        "flow_state": "opposing",
                    }
            else:
                if delta_flow >= 0.0:
                    return False, "wyckoff_p5_opposing_flow_veto", {
                        **base_metrics,
                        "verdict": "VETO_POSITIVE_DELTA",
                        "wyckoff_confirmed": False,
                        "flow_state": "opposing",
                    }
                if bear_flow < (1.50 * max(bull_flow, 1.0)):
                    return False, "wyckoff_p5_put_dominance_failed", {
                        **base_metrics,
                        "verdict": "VETO_INSUFFICIENT_DOMINANCE",
                        "wyckoff_confirmed": False,
                        "flow_state": "opposing",
                    }

            is_confirmed = (is_up and delta_flow > 0) or ((not is_up) and delta_flow < 0)
            return True, "wyckoff_intrabar_inflight_confirmed" if is_confirmed else "wyckoff_intrabar_neutral_flow", {
                **base_metrics,
                "wyckoff_confirmed": is_confirmed,
                "flow_state": "aligned" if is_confirmed else "neutral",
            }

        # ══════════════════════════════════════════════════════════════════
        # PILLARS 3 & 4: Volatility Squeeze & Structural Breakout (SOS Expansion)
        # ══════════════════════════════════════════════════════════════════
        else:
            # 1. Hard Floor: Minimum 3 lots active traded volume
            if tot_flow < min_active_volume:
                return False, "wyckoff_inflight_insufficient_volume", {
                    **base_metrics,
                    "min_required": min_active_volume,
                    "verdict": "VETO_DRY_ORDER_FLOW",
                    "wyckoff_confirmed": False,
                    "flow_state": "insufficient_volume",
                }

            # 2. Strict 1.5x Directional Dominance & Directional Delta
            if is_up:
                if delta_flow <= 0.0:
                    return False, "wyckoff_intrabar_opposing_flow_veto", {
                        **base_metrics,
                        "verdict": "VETO_NEGATIVE_DELTA",
                        "wyckoff_confirmed": False,
                        "flow_state": "opposing",
                    }
                if bull_flow < (1.50 * max(bear_flow, 1.0)):
                    return False, "wyckoff_inflight_call_dominance_failed", {
                        **base_metrics,
                        "ratio": round(bull_flow / max(bear_flow, 1.0), 2),
                        "verdict": "VETO_WEAK_BULL_FLOW",
                        "wyckoff_confirmed": False,
                        "flow_state": "weak_dominance",
                    }
            else:
                if delta_flow >= 0.0:
                    return False, "wyckoff_intrabar_opposing_flow_veto", {
                        **base_metrics,
                        "verdict": "VETO_POSITIVE_DELTA",
                        "wyckoff_confirmed": False,
                        "flow_state": "opposing",
                    }
                if bear_flow < (1.50 * max(bull_flow, 1.0)):
                    return False, "wyckoff_inflight_put_dominance_failed", {
                        **base_metrics,
                        "ratio": round(bear_flow / max(bull_flow, 1.0), 2),
                        "verdict": "VETO_WEAK_BEAR_FLOW",
                        "wyckoff_confirmed": False,
                        "flow_state": "weak_dominance",
                    }

            return True, "wyckoff_intrabar_inflight_confirmed", {
                **base_metrics,
                "wyckoff_confirmed": True,
                "flow_state": "aligned",
            }

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

        Pure Live In-Flight Mode B is the sole entry mode — `evaluate()` routes
        unconditionally to `evaluate_inflight_flow(...)` and can never return None.

        Returns:
            (passed: bool, reason: str, metrics: Dict[str, Any])
        """
        if self.mode == "DISABLED":
            return True, "wyckoff_disabled", {}

        is_up = direction.upper() in ("UP", "CALL")

        # Resolve pillar id (fall back to the `pillar` argument, then name-based inference)
        pa_name = str((pa_details or {}).get("pa_setup_name", ""))
        pillar_id = int((pa_details or {}).get("pillar", 0) or (pa_details or {}).get("pillar_id", 0) or pillar or 0)
        if pillar_id <= 0:
            if "20_ema" in pa_name:
                pillar_id = 1
            elif "sweep" in pa_name:
                pillar_id = 2
            elif "squeeze" in pa_name:
                pillar_id = 3
            elif "sr_breakout" in pa_name or "prominence" in pa_name:
                pillar_id = 4
            elif "gamma" in pa_name or "trap" in pa_name:
                pillar_id = 5
            else:
                pillar_id = 1

        is_p1 = (pillar_id == 1) or ("20_ema" in pa_name)
        lot_sz = float((pa_details or {}).get("lot_size", 0.0) or getattr(self, "lot_size", 65.0) or 65.0)

        flow_ok, flow_reason, flow_metrics = self.evaluate_inflight_flow(
            direction=direction,
            flow_dict=pa_details or {},
            lot_size=lot_sz,
            pillar=pillar_id,
        )
        if not flow_ok:
            # SHADOW mode is observability-only: log the veto but never block the entry.
            if self.mode == "SHADOW":
                flow_metrics = dict(flow_metrics or {})
                flow_metrics["shadow_veto"] = flow_reason
                flow_metrics["wyckoff_confirmed"] = False
                logger.info(
                    f"[WYCKOFF SHADOW] P{pillar_id} ({direction}) in-flight veto bypassed: {flow_reason}"
                )
                return True, f"shadow_bypass_{flow_reason}", flow_metrics
            return False, flow_reason, flow_metrics

        base_metrics = dict(flow_metrics)
        base_metrics.update({
            "in_flight_validated": True,
            "setup_name": pa_name or str((pa_details or {}).get("mode", "")),
            "atr": round(max(float(atr or 10.0), 1.0), 2),
        })
        if is_p1:
            n1_fp = (pa_details or {}).get("n1_footprint", {})
            vdr_val = 0.50 if n1_fp.get("n1_vol_dryup", False) else 1.0
            absorption = 2.0 if n1_fp.get("n1_stopping_vol", False) else 1.0
            base_metrics["vdr"] = round(vdr_val, 2)
            base_metrics["absorption_score"] = round(absorption, 1)
            base_metrics["n1_footprint"] = n1_fp

        if bars and len(bars) >= 4:
            n1_bar = bars[-1]
            vol_n1 = float(getattr(n1_bar, "volume", 0.0) or 0.0)
            v_base = self._compute_baseline_volume(bars, exclude_last=1)
            vdr_n1 = vol_n1 / v_base if v_base > 0 else 1.0
            is_n1_directional = (n1_bar.close > n1_bar.open) if is_up else (n1_bar.close < n1_bar.open)
            n1_demand_ignition = bool(vdr_n1 >= 1.10 and is_n1_directional)
            base_metrics["n1_ver"] = round(vdr_n1, 2)
            base_metrics["n1_demand_ignition"] = n1_demand_ignition
            if n1_demand_ignition:
                base_metrics["wyckoff_confirmed"] = True

        # ─── P0 FIX: In-Flight Mode B Breakthrough Terminal Verdict ───
        # The breakthrough candle is the ACTIVE FORMING candle, NOT closed bar N-1!
        # Do NOT fall through to closed-bar breakout checks where bars[-1] (bar N-1
        # inside the squeeze box) is erroneously evaluated as the breakout bar
        # (which causes false phantom drift vetoes on valid squeeze breakouts).
        verdict_reason = (
            "wyckoff_intrabar_inflight_confirmed"
            if base_metrics.get("wyckoff_confirmed")
            else "wyckoff_intrabar_neutral_flow"
        )
        return True, verdict_reason, base_metrics

