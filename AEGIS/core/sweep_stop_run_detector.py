"""
AEGIS Sweep Stop-Run & Liquidity Sweep Order-Flow Validator (SweepStopRunDetector)
==================================================================================
High-Frequency, Zero-Allocation O(1) Microstructure Stop-Cascade & Absorption Observer.

Microstructure Foundation:
  - Stop-Loss orders are conditional orders that DO NOT sit as passive limit liquidity in the order book.
  - When Spot breaches a registered S/R level or Day Extreme (PDH/PDL), conditional stops trigger simultaneously
    and enter the market as explosive bursts of Aggressive Market Orders:
      * Downside Support Breach: Long Call SLs dump into Bid (Aggressive Call Sell) + Short Put SLs panic buy Ask (Aggressive Put Buy).
        PanicFlow = Aggressive Call Sell Vol + Aggressive Put Buy Vol
      * Upside Resistance Breach: Short Call SLs panic buy Ask (Aggressive Call Buy) + Long Put SLs dump into Bid (Aggressive Put Sell).
        PanicFlow = Aggressive Put Sell Vol + Aggressive Call Buy Vol
  - Dry Sweep Fakeout: If Spot breaches a level without an aggressive order flow burst (StopBurstRatio < 1.2),
    no institutional/retail stop cascade occurred.
  - Confirmed Stop Hunt: StopBurstRatio >= 2.2 during breach + directional OFI flip on reclaim.

Strict Invariants:
  - Zero Heap Churn: Pre-allocated NumPy arrays (float64, int8) mapped to S/R Registry level slots.
  - Zero Live Blocking Initially: Runs 100% in Shadow Mode by default (ENFORCE_LIVE_SWEEP_GATE = False).
  - Synthetic Test Compatibility: Gracefully bypasses when option flow is zero/uninitialized.
"""

import os
import logging
from typing import Dict, Any, List, Tuple, Optional
import numpy as np

logger = logging.getLogger("aegis.sweep_stop_run")

# Sweep Verdict Constants
SWEEP_NONE: int = 0
SWEEP_CONFIRMED_STOP_HUNT: int = 1     # High aggressive stop burst + reclaim absorption flip
SWEEP_VETO_DRY_NO_STOPS: int = -1     # Level breached on low aggressive flow (no stops hit)
SWEEP_VETO_UNABSORBED_PANIC: int = -2 # Stop cascade still accelerating against trade / no absorption

# Empirical Microstructure Thresholds
BURST_RATIO_CONFIRMED: float = 2.2
BURST_RATIO_DRY: float = 1.2
MAX_LEVELS_CAPACITY: int = 32
RECLAIM_FRESHNESS_SEC: float = 60.0
MIN_BREACH_WINDOW_SEC: float = 0.25
ATM_WINDOW_STEPS: float = 2.5  # ATM ± 2 strikes


class SweepStopRunDetector:
    """
    O(1) Zero-Allocation Stop-Run & Liquidity Sweep Order-Flow Validator.
    Tracks breach excursions outside registered S/R levels and computes
    stop cascade burst ratios and reclaim absorption flips in real-time.
    """

    def __init__(self, num_levels: int = MAX_LEVELS_CAPACITY):
        self.capacity: int = num_levels

        # Pre-allocated NumPy state arrays mapped to registry slots
        self.sr_levels = np.zeros(num_levels, dtype=np.float64)
        self.level_types = np.zeros(num_levels, dtype=np.int8)  # +1 = SUPPORT, -1 = RESISTANCE
        self.in_breach = np.zeros(num_levels, dtype=np.int8)
        self.breach_start_ts = np.zeros(num_levels, dtype=np.float64)
        self.breach_panic_vol = np.zeros(num_levels, dtype=np.float64)
        self.breach_peak_rate = np.zeros(num_levels, dtype=np.float64)
        self.breach_extreme_spot = np.zeros(num_levels, dtype=np.float64)

        # Historical / Reclaim snapshot states
        self.last_verdict = np.zeros(num_levels, dtype=np.int8)
        self.last_burst_ratio = np.zeros(num_levels, dtype=np.float64)
        self.last_reclaim_ts = np.zeros(num_levels, dtype=np.float64)
        self.last_reclaim_flow = np.zeros(num_levels, dtype=np.float64)
        self.last_peak_rate = np.zeros(num_levels, dtype=np.float64)
        self.last_baseline_rate = np.zeros(num_levels, dtype=np.float64)
        self.last_panic_vol = np.zeros(num_levels, dtype=np.float64)

        # Fast lookup mapping: rounded level price -> slot index
        self._price_to_slot: Dict[float, int] = {}
        self._active_slot_count: int = 0

        # Rolling 60s baseline aggressive volume accumulator (60 1-second circular buckets)
        self._sec_buckets = np.zeros(60, dtype=np.float64)
        self._sec_timestamps = np.zeros(60, dtype=np.int64)

        # Rolling 5s directional reclaim flow accumulator (5 1-second circular buckets)
        self._dir_ce_delta_5s = np.zeros(5, dtype=np.float64)  # ce_buy - ce_sell
        self._dir_pe_delta_5s = np.zeros(5, dtype=np.float64)  # pe_buy - pe_sell
        self._dir_timestamps_5s = np.zeros(5, dtype=np.int64)

        self._last_spot: float = 0.0
        self._last_spot_ts: float = 0.0
        self._total_option_ticks_seen: int = 0
        self._total_spot_ticks_seen: int = 0

        # Default fallback baseline rate (vol/s) before warm-up
        self.default_baseline_rate: float = float(os.getenv("AEGIS_SWEEP_DEFAULT_BASELINE_RATE", "100.0"))

    def sync_levels(
        self,
        session_levels: List[Any],
        pdh: float = 0.0,
        pdl: float = 0.0,
    ) -> None:
        """
        Syncs active S/R levels into pre-allocated NumPy array slots.
        Preserves existing in-flight breach states for unbroken levels.
        """
        current_active: List[Tuple[float, int]] = []

        # 1. Daily macro extremes
        if pdl > 0.0:
            current_active.append((round(pdl, 2), 1))   # Support
        if pdh > 0.0:
            current_active.append((round(pdh, 2), -1))  # Resistance

        # 2. Registered session levels
        for lvl in session_levels:
            if getattr(lvl, "is_broken", False):
                continue
            lvl_price = round(float(getattr(lvl, "price", 0.0)), 2)
            if lvl_price <= 0.0:
                continue
            ltype_str = str(getattr(lvl, "level_type", "")).upper()
            ltype = 1 if ltype_str in ("SUPPORT", "FLOOR") else -1
            if not any(abs(p - lvl_price) < 0.25 and t == ltype for p, t in current_active):
                current_active.append((lvl_price, ltype))

        # Re-map into slots
        new_price_to_slot: Dict[float, int] = {}
        slots_to_keep = set()

        for lvl_price, ltype in current_active[:self.capacity]:
            existing_slot = self._price_to_slot.get(lvl_price)
            if existing_slot is not None and existing_slot < self.capacity:
                new_price_to_slot[lvl_price] = existing_slot
                self.sr_levels[existing_slot] = lvl_price
                self.level_types[existing_slot] = ltype
                slots_to_keep.add(existing_slot)
            else:
                # Find first available slot
                available_slot = -1
                for s in range(self.capacity):
                    if s not in slots_to_keep and s not in new_price_to_slot.values():
                        available_slot = s
                        break
                if available_slot >= 0:
                    new_price_to_slot[lvl_price] = available_slot
                    self.sr_levels[available_slot] = lvl_price
                    self.level_types[available_slot] = ltype
                    self.in_breach[available_slot] = 0
                    self.breach_start_ts[available_slot] = 0.0
                    self.breach_panic_vol[available_slot] = 0.0
                    self.breach_peak_rate[available_slot] = 0.0
                    self.breach_extreme_spot[available_slot] = lvl_price
                    self.last_verdict[available_slot] = SWEEP_NONE
                    self.last_burst_ratio[available_slot] = 0.0
                    slots_to_keep.add(available_slot)

        # Clear retired slots
        for s in range(self.capacity):
            if s not in slots_to_keep:
                self.sr_levels[s] = 0.0
                self.level_types[s] = 0
                self.in_breach[s] = 0
                self.breach_start_ts[s] = 0.0
                self.breach_panic_vol[s] = 0.0
                self.breach_peak_rate[s] = 0.0
                self.breach_extreme_spot[s] = 0.0

        self._price_to_slot = new_price_to_slot
        self._active_slot_count = len(new_price_to_slot)

    def on_spot_tick(self, spot: float, ts: float) -> None:
        """
        O(1) Underlier Spot Tick Evaluation.
        Detects breach transitions (outside S/R) and reclaim transitions (inside S/R).
        """
        if spot <= 0.0 or ts <= 0.0:
            return

        self._last_spot = spot
        self._last_spot_ts = ts
        self._total_spot_ticks_seen += 1

        if self._active_slot_count == 0:
            return

        baseline_rate = self.get_rolling_60s_baseline_rate(ts)

        for lvl_price, slot in self._price_to_slot.items():
            lvl = self.sr_levels[slot]
            if lvl <= 0.0:
                continue

            ltype = self.level_types[slot]
            # Support breach: spot < lvl | Resistance breach: spot > lvl
            is_breached = (spot < lvl) if ltype == 1 else (spot > lvl)

            if is_breached:
                if self.in_breach[slot] == 0:
                    # Fresh breach excursion initiation
                    self.in_breach[slot] = 1
                    self.breach_start_ts[slot] = ts
                    self.breach_panic_vol[slot] = 0.0
                    self.breach_peak_rate[slot] = 0.0
                    self.breach_extreme_spot[slot] = spot
                else:
                    # Continuing excursion
                    if ltype == 1:
                        if spot < self.breach_extreme_spot[slot]:
                            self.breach_extreme_spot[slot] = spot
                    else:
                        if spot > self.breach_extreme_spot[slot]:
                            self.breach_extreme_spot[slot] = spot

                    elapsed = max(ts - self.breach_start_ts[slot], MIN_BREACH_WINDOW_SEC)
                    curr_rate = self.breach_panic_vol[slot] / elapsed
                    if curr_rate > self.breach_peak_rate[slot]:
                        self.breach_peak_rate[slot] = curr_rate

            else:
                # Spot is inside/on the level
                if self.in_breach[slot] == 1:
                    # ─── RECLAIM MOMENT (Turtle Soup / Mode B Trigger Moment) ───
                    elapsed = max(ts - self.breach_start_ts[slot], MIN_BREACH_WINDOW_SEC)
                    final_panic_vol = self.breach_panic_vol[slot]
                    peak_rate = max(self.breach_peak_rate[slot], final_panic_vol / elapsed)

                    effective_baseline = max(baseline_rate, 1.0)
                    burst_ratio = peak_rate / effective_baseline

                    # Directional Reclaim Flow from recent 5s window
                    reclaim_dir_flow = self.get_recent_reclaim_flow(ltype, ts)

                    # Compute Verdict
                    if self._total_option_ticks_seen < 5 or baseline_rate <= 0.0:
                        # Synthetic test environment or cold start: neutral verdict
                        verdict = SWEEP_NONE
                    elif burst_ratio < BURST_RATIO_DRY:
                        # Level crossed on dry flow; no stops were hit
                        verdict = SWEEP_VETO_DRY_NO_STOPS
                    elif burst_ratio >= BURST_RATIO_CONFIRMED and reclaim_dir_flow >= 0.0:
                        # Genuine stop hunt followed by institutional absorption & reclaim flip
                        verdict = SWEEP_CONFIRMED_STOP_HUNT
                    else:
                        # Stop cascade still accelerating against trade or failed absorption
                        verdict = SWEEP_VETO_UNABSORBED_PANIC

                    # Snapshot state
                    self.last_verdict[slot] = verdict
                    self.last_burst_ratio[slot] = burst_ratio
                    self.last_reclaim_ts[slot] = ts
                    self.last_reclaim_flow[slot] = reclaim_dir_flow
                    self.last_peak_rate[slot] = peak_rate
                    self.last_baseline_rate[slot] = effective_baseline
                    self.last_panic_vol[slot] = final_panic_vol

                    # Reset breach flag
                    self.in_breach[slot] = 0

    def on_option_tick(
        self,
        sym: str,
        strike: float,
        side: int,             # 0 = CE, 1 = PE
        buy_vol: float,        # Aggressive buy volume (lifted ask)
        sell_vol: float,       # Aggressive sell volume (hit bid)
        ts: float,
        atm_strike: float,
        strike_step: float = 50.0,
    ) -> None:
        """
        O(1) Option Trade Flow Accumulator.
        Routes classified aggressive volume into rolling 60s baseline and active breach panic counters.
        """
        if (buy_vol + sell_vol) <= 0.0 or ts <= 0.0:
            return

        self._total_option_ticks_seen += 1
        tot_vol = buy_vol + sell_vol

        # 1. Update rolling 60s aggressive baseline (Near-ATM window: ATM ± 2.5 strikes)
        ref_atm = atm_strike if atm_strike > 0.0 else self._last_spot
        is_near_atm = (ref_atm <= 0.0) or (abs(strike - ref_atm) <= (ATM_WINDOW_STEPS * strike_step))

        if is_near_atm:
            sec_idx = int(ts) % 60
            if self._sec_timestamps[sec_idx] != int(ts):
                self._sec_timestamps[sec_idx] = int(ts)
                self._sec_buckets[sec_idx] = 0.0
            self._sec_buckets[sec_idx] += tot_vol

            # Update recent 5s directional reclaim flow
            sec_idx_5s = int(ts) % 5
            if self._dir_timestamps_5s[sec_idx_5s] != int(ts):
                self._dir_timestamps_5s[sec_idx_5s] = int(ts)
                self._dir_ce_delta_5s[sec_idx_5s] = 0.0
                self._dir_pe_delta_5s[sec_idx_5s] = 0.0

            if side == 0:  # CE
                self._dir_ce_delta_5s[sec_idx_5s] += (buy_vol - sell_vol)
            elif side == 1:  # PE
                self._dir_pe_delta_5s[sec_idx_5s] += (buy_vol - sell_vol)

        # 2. Accumulate Panic Flow for any S/R levels currently in breach
        if self._active_slot_count > 0:
            for lvl_price, slot in self._price_to_slot.items():
                if self.in_breach[slot] == 1 and is_near_atm:
                    ltype = self.level_types[slot]
                    # Support breach panic: Call Sell (long calls stopping) + Put Buy (short puts stopping / breakout puts)
                    # Resistance breach panic: Put Sell (long puts stopping) + Call Buy (short calls stopping / breakout calls)
                    if ltype == 1:  # SUPPORT
                        panic_dv = (sell_vol if side == 0 else 0.0) + (buy_vol if side == 1 else 0.0)
                    else:           # RESISTANCE
                        panic_dv = (sell_vol if side == 1 else 0.0) + (buy_vol if side == 0 else 0.0)

                    self.breach_panic_vol[slot] += panic_dv
                    elapsed = max(ts - self.breach_start_ts[slot], MIN_BREACH_WINDOW_SEC)
                    curr_rate = self.breach_panic_vol[slot] / elapsed
                    if curr_rate > self.breach_peak_rate[slot]:
                        self.breach_peak_rate[slot] = curr_rate

    def get_rolling_60s_baseline_rate(self, current_ts: float) -> float:
        """Computes rolling 60s aggressive volume rate per second (O(60) = O(1))."""
        curr_int_sec = int(current_ts)
        active_sum = 0.0
        active_seconds = 0

        for i in range(60):
            bucket_ts = self._sec_timestamps[i]
            if (curr_int_sec - bucket_ts) < 60:
                active_sum += self._sec_buckets[i]
                active_seconds += 1

        if active_seconds >= 5 and active_sum > 0.0:
            return active_sum / max(float(active_seconds), 1.0)
        return self.default_baseline_rate

    def get_recent_reclaim_flow(self, level_type: int, current_ts: float) -> float:
        """
        Computes immediate directional option flow at reclaim moment.
        Examines the reclaim second (or latest active second) to verify
        that aggressive flow flipped in trade direction:
          - Support Reclaim (CALL trade / ltype=1): net Call delta (ce_buy - ce_sell >= 0)
          - Resistance Reclaim (PUT trade / ltype=-1): net Put delta (pe_buy - pe_sell >= 0)
        """
        curr_int_sec = int(current_ts)

        # Check current second first
        for i in range(5):
            if self._dir_timestamps_5s[i] == curr_int_sec:
                if level_type == 1:
                    return float(self._dir_ce_delta_5s[i])
                else:
                    return float(self._dir_pe_delta_5s[i])

        # Fallback to the latest active bucket within last 2 seconds
        latest_ts = -1
        latest_idx = -1
        for i in range(5):
            b_ts = self._dir_timestamps_5s[i]
            if b_ts > latest_ts and (curr_int_sec - b_ts) <= 2:
                latest_ts = b_ts
                latest_idx = i

        if latest_idx >= 0:
            if level_type == 1:
                return float(self._dir_ce_delta_5s[latest_idx])
            else:
                return float(self._dir_pe_delta_5s[latest_idx])

        return 0.0

    def evaluate_sweep(
        self,
        swept_level: float,
        direction: str,
        current_time: float,
    ) -> Tuple[int, str, Dict[str, Any]]:
        """
        Evaluates the swept S/R level for Stop-Loss Hunt vs Dry Sweep.

        Returns:
            verdict: SWEEP_CONFIRMED_STOP_HUNT (+1), SWEEP_VETO_DRY_NO_STOPS (-1),
                     SWEEP_VETO_UNABSORBED_PANIC (-2), or SWEEP_NONE (0).
            reason: Human-readable verdict string.
            metrics: Diagnostic metrics dictionary.
        """
        now_sec = current_time if current_time > 0.0 else self._last_spot_ts
        swept_price = round(float(swept_level), 2)

        # Find closest registered slot within 1.0 point tolerance
        target_slot = -1
        min_dist = 999.0
        for price_val, slot in self._price_to_slot.items():
            dist = abs(price_val - swept_price)
            if dist < min_dist and dist <= 1.0:
                min_dist = dist
                target_slot = slot

        if target_slot < 0 or target_slot >= self.capacity:
            return SWEEP_NONE, "level_not_tracked_in_sweep_detector", {
                "swept_level": swept_price,
                "tracked_levels": list(self._price_to_slot.keys()),
            }

        # 1. Check if level is currently STILL in breach (reclaim not yet achieved on spot ticks)
        if self.in_breach[target_slot] == 1:
            # Mode B may trigger on spot crossing back inside before next discrete tick
            elapsed = max(now_sec - self.breach_start_ts[target_slot], MIN_BREACH_WINDOW_SEC)
            peak_rate = self.breach_peak_rate[target_slot]
            baseline = max(self.get_rolling_60s_baseline_rate(now_sec), 1.0)
            ratio = peak_rate / baseline
            ltype = self.level_types[target_slot]
            reclaim_flow = self.get_recent_reclaim_flow(ltype, now_sec)

            if ratio >= BURST_RATIO_CONFIRMED and reclaim_flow >= 0.0:
                verdict = SWEEP_CONFIRMED_STOP_HUNT
                reason = "in_flight_confirmed_stop_hunt"
            elif ratio < BURST_RATIO_DRY:
                verdict = SWEEP_VETO_DRY_NO_STOPS
                reason = "in_flight_dry_sweep_no_stops"
            else:
                verdict = SWEEP_VETO_UNABSORBED_PANIC
                reason = "in_flight_unabsorbed_panic_cascade"

            metrics = {
                "swept_level": swept_price,
                "burst_ratio": round(ratio, 2),
                "peak_rate": round(peak_rate, 1),
                "baseline_rate": round(baseline, 1),
                "reclaim_flow": round(reclaim_flow, 1),
                "panic_vol": round(self.breach_panic_vol[target_slot], 1),
                "extreme_spot": round(self.breach_extreme_spot[target_slot], 2),
                "status": "IN_FLIGHT_BREACH",
            }
            return verdict, reason, metrics

        # 2. Check last recorded reclaim verdict
        last_rec_ts = self.last_reclaim_ts[target_slot]
        if last_rec_ts <= 0.0:
            return SWEEP_NONE, "no_recent_breach_recorded", {
                "swept_level": swept_price,
                "status": "NO_PRIOR_BREACH",
            }

        rec_age = now_sec - last_rec_ts
        if rec_age > RECLAIM_FRESHNESS_SEC:
            return SWEEP_NONE, "breach_reclaim_too_stale", {
                "swept_level": swept_price,
                "reclaim_age_sec": round(rec_age, 1),
                "max_allowed_sec": RECLAIM_FRESHNESS_SEC,
            }

        verdict = int(self.last_verdict[target_slot])
        burst_ratio = float(self.last_burst_ratio[target_slot])
        peak_rate = float(self.last_peak_rate[target_slot])
        baseline_rate = float(self.last_baseline_rate[target_slot])
        reclaim_flow = float(self.last_reclaim_flow[target_slot])
        panic_vol = float(self.last_panic_vol[target_slot])

        reason_map = {
            SWEEP_CONFIRMED_STOP_HUNT: "confirmed_stop_hunt",
            SWEEP_VETO_DRY_NO_STOPS: "dry_sweep_no_stops",
            SWEEP_VETO_UNABSORBED_PANIC: "unabsorbed_panic_cascade",
            SWEEP_NONE: "sweep_unclassified",
        }
        reason = reason_map.get(verdict, "sweep_unknown")

        metrics = {
            "swept_level": swept_price,
            "burst_ratio": round(burst_ratio, 2),
            "peak_rate": round(peak_rate, 1),
            "baseline_rate": round(baseline_rate, 1),
            "reclaim_flow": round(reclaim_flow, 1),
            "panic_vol": round(panic_vol, 1),
            "reclaim_age_sec": round(rec_age, 1),
            "status": "RECLAIMED",
        }
        return verdict, reason, metrics
