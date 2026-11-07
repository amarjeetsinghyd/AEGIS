"""AEGIS Synthetic Iceberg & Passive Absorption Shadow Detector — Zero-Latency O(1) Engine.

Microstructure Principles:
1. Zero-Allocation NumPy Arrays: Uses pre-allocated C-contiguous float64, int32,
   and int8 buffers for 51 strikes. Zero heap churn, zero dict/object instantiation
   on tick ingestion.
2. Synthetic MBO Emulation via L1 MBP: Captures hidden institutional iceberg orders
   where aggressive market volume absorbs resting visible touch quantity without moving
   price, and visible quantity is continuously refilled.
3. State Transitions:
   - STATE_NONE (0): Normal market flow
   - STATE_BID_ICEBERG_HOLD (+1): Invisible buyer wall absorbing flow at Best Bid
   - STATE_ASK_ICEBERG_HOLD (-1): Invisible seller wall absorbing flow at Best Ask
   - STATE_BID_ICEBERG_BROKEN (-2): Support wall exhausted -> price ticks down
   - STATE_ASK_ICEBERG_BROKEN (+2): Resistance wall exhausted -> price ticks up (fast upside)
4. STRICT SHADOW INVARIANT: Never vetoes or delays live execution. Computes in background
   and enriches Decision Lifecycle signal telemetry.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Sequence, Tuple

import numpy as np

try:
    from logger import get_logger
    logger = get_logger("aegis_iceberg_detector")
except Exception:
    logger = logging.getLogger("aegis_iceberg_detector")

# State Constants
STATE_NONE: int = 0
STATE_BID_ICEBERG_HOLD: int = 1      # Invisible support wall active
STATE_ASK_ICEBERG_HOLD: int = -1     # Invisible resistance wall active
STATE_BID_ICEBERG_BROKEN: int = -2   # Support iceberg exhausted -> Fast downside
STATE_ASK_ICEBERG_BROKEN: int = 2    # Resistance iceberg exhausted -> Fast upside

# Shadow Verdict Tags
VERDICT_VETO_ASK_ICEBERG: str = "SHADOW_VETO_ASK_ICEBERG"
VERDICT_SUPPORT_BID_ICEBERG: str = "SHADOW_SUPPORT_BID_ICEBERG"
VERDICT_AMPLIFY_ICEBERG_BREAK: str = "SHADOW_AMPLIFY_ICEBERG_BREAK"
VERDICT_NEUTRAL_PASS: str = "NEUTRAL_PASS"


class IcebergShadowDetector:
    """Zero-allocation O(1) Synthetic Iceberg & Passive Absorption Shadow Detector.

    Maintains pre-allocated C-contiguous NumPy arrays indexed by strike slot (default 51 strikes).
    """

    __slots__ = (
        "num_strikes",
        "strike_step",
        "last_bid_px",
        "last_ask_px",
        "last_bid_qty",
        "last_ask_qty",
        "last_cum_vol",
        "bid_absorbed_vol",
        "ask_absorbed_vol",
        "bid_refill_cnt",
        "ask_refill_cnt",
        "bid_visible_anchor",
        "ask_visible_anchor",
        "iceberg_state",
        "iceberg_ratio",
        "absorbed_vol",
        "broken_ts",
        "strikes",
        "strike_to_idx",
        "_strikes_tuple",
    )

    def __init__(self, num_strikes: int = 51, strike_step: float = 50.0) -> None:
        self.num_strikes = int(num_strikes)
        self.strike_step = float(strike_step)

        # Pre-allocated contiguous arrays for zero heap churn
        self.last_bid_px = np.zeros(self.num_strikes, dtype=np.float64)
        self.last_ask_px = np.zeros(self.num_strikes, dtype=np.float64)
        self.last_bid_qty = np.zeros(self.num_strikes, dtype=np.float64)
        self.last_ask_qty = np.zeros(self.num_strikes, dtype=np.float64)
        self.last_cum_vol = np.zeros(self.num_strikes, dtype=np.float64)

        # Absorption & Refill accumulators at stationary touch price
        self.bid_absorbed_vol = np.zeros(self.num_strikes, dtype=np.float64)
        self.ask_absorbed_vol = np.zeros(self.num_strikes, dtype=np.float64)
        self.bid_refill_cnt = np.zeros(self.num_strikes, dtype=np.int32)
        self.ask_refill_cnt = np.zeros(self.num_strikes, dtype=np.int32)
        self.bid_visible_anchor = np.ones(self.num_strikes, dtype=np.float64)
        self.ask_visible_anchor = np.ones(self.num_strikes, dtype=np.float64)

        # Output state array read in O(1) by Decision Lifecycle
        self.iceberg_state = np.zeros(self.num_strikes, dtype=np.int8)
        self.iceberg_ratio = np.zeros(self.num_strikes, dtype=np.float64)

        # Telemetry & 3-Second Decay buffers
        self.absorbed_vol = np.zeros(self.num_strikes, dtype=np.float64)
        self.broken_ts = np.zeros(self.num_strikes, dtype=np.float64)

        # Strike mapping
        self.strikes = np.zeros(self.num_strikes, dtype=np.float64)
        self.strike_to_idx: Dict[float, int] = {}
        self._strikes_tuple: Tuple[float, ...] = ()

    def on_tick_o1(
        self,
        idx: int,
        ltp: float,
        cum_vol: float,
        bid_px: float,
        bid_qty: float,
        ask_px: float,
        ask_qty: float,
        min_refills: int = 3,
        ratio_threshold: float = 5.0,
        now_sec: float = 0.0,
    ) -> None:
        """Single-index O(1) update for incoming tick on strike index idx.

        Zero heap churn: no dicts, no objects, no dynamic allocations.
        """
        if idx < 0 or idx >= self.num_strikes:
            return

        # ── 3-Second Decay Window Check for BROKEN state ──
        if now_sec > 0.0 and self.broken_ts[idx] > 0.0:
            if (now_sec - self.broken_ts[idx]) > 3.0:
                if self.iceberg_state[idx] in (STATE_BID_ICEBERG_BROKEN, STATE_ASK_ICEBERG_BROKEN):
                    self.iceberg_state[idx] = STATE_NONE
                    self.iceberg_ratio[idx] = 0.0
                    self.broken_ts[idx] = 0.0

        dv = cum_vol - self.last_cum_vol[idx]
        if dv <= 0.0:
            # Check price-jump breakout even on quote-only depth messages
            prev_ask = self.last_ask_px[idx]
            prev_bid = self.last_bid_px[idx]
            if self.iceberg_state[idx] == STATE_ASK_ICEBERG_HOLD and (ask_px > prev_ask or prev_ask == 0.0):
                self.iceberg_state[idx] = STATE_ASK_ICEBERG_BROKEN
                self.broken_ts[idx] = now_sec
                self.absorbed_vol[idx] = max(self.ask_absorbed_vol[idx], self.absorbed_vol[idx])
                self.ask_absorbed_vol[idx] = 0.0
                self.ask_refill_cnt[idx] = 0
                self.ask_visible_anchor[idx] = max(ask_qty, 1.0)
            elif self.iceberg_state[idx] == STATE_BID_ICEBERG_HOLD and (bid_px < prev_bid or prev_bid == 0.0):
                self.iceberg_state[idx] = STATE_BID_ICEBERG_BROKEN
                self.broken_ts[idx] = now_sec
                self.absorbed_vol[idx] = max(self.bid_absorbed_vol[idx], self.absorbed_vol[idx])
                self.bid_absorbed_vol[idx] = 0.0
                self.bid_refill_cnt[idx] = 0
                self.bid_visible_anchor[idx] = max(bid_qty, 1.0)

            self.last_bid_px[idx] = bid_px
            self.last_ask_px[idx] = ask_px
            self.last_bid_qty[idx] = bid_qty
            self.last_ask_qty[idx] = ask_qty
            if self.last_cum_vol[idx] == 0.0:
                self.last_cum_vol[idx] = cum_vol
            if self.ask_absorbed_vol[idx] == 0.0 and ask_qty > 0.0:
                self.ask_visible_anchor[idx] = ask_qty
            if self.bid_absorbed_vol[idx] == 0.0 and bid_qty > 0.0:
                self.bid_visible_anchor[idx] = bid_qty
            return

        prev_ask_px = self.last_ask_px[idx]
        prev_bid_px = self.last_bid_px[idx]

        # --- 1. ASK ICEBERG TRACKING ---
        if ask_px == prev_ask_px and ltp >= prev_ask_px and prev_ask_px > 0.0:
            if self.ask_absorbed_vol[idx] == 0.0:
                self.ask_visible_anchor[idx] = max(self.last_ask_qty[idx], ask_qty, 1.0)
            self.ask_absorbed_vol[idx] += dv
            expected_rem_ask = max(0.0, self.last_ask_qty[idx] - dv)
            # Refill detected: visible size did not drop as much as traded volume
            if ask_qty > expected_rem_ask:
                self.ask_refill_cnt[idx] += 1

            ratio = self.ask_absorbed_vol[idx] / self.ask_visible_anchor[idx]
            if self.ask_refill_cnt[idx] >= min_refills and ratio >= ratio_threshold:
                self.iceberg_state[idx] = STATE_ASK_ICEBERG_HOLD
                self.iceberg_ratio[idx] = ratio
                self.absorbed_vol[idx] = self.ask_absorbed_vol[idx]

        elif (ask_px > prev_ask_px and prev_ask_px > 0.0) or (self.iceberg_state[idx] == STATE_ASK_ICEBERG_HOLD and (ask_px > prev_ask_px or prev_ask_px == 0.0)):
            # Price ticked UP after an active Ask Iceberg -> Wall Broken!
            if self.iceberg_state[idx] == STATE_ASK_ICEBERG_HOLD:
                self.iceberg_state[idx] = STATE_ASK_ICEBERG_BROKEN
                self.broken_ts[idx] = now_sec
                self.absorbed_vol[idx] = max(self.ask_absorbed_vol[idx], self.absorbed_vol[idx])
            elif self.iceberg_state[idx] != STATE_ASK_ICEBERG_BROKEN:
                if self.iceberg_state[idx] in (STATE_ASK_ICEBERG_HOLD,):
                    self.iceberg_state[idx] = STATE_NONE
                    self.broken_ts[idx] = 0.0
            self.ask_absorbed_vol[idx] = 0.0
            self.ask_refill_cnt[idx] = 0
            self.ask_visible_anchor[idx] = max(ask_qty, 1.0)

        elif ask_px < prev_ask_px:
            # Ask stepped down, reset ask accumulator and only ask-related states
            if self.iceberg_state[idx] in (STATE_ASK_ICEBERG_HOLD, STATE_ASK_ICEBERG_BROKEN):
                self.iceberg_state[idx] = STATE_NONE
                self.broken_ts[idx] = 0.0
            self.ask_absorbed_vol[idx] = 0.0
            self.ask_refill_cnt[idx] = 0
            self.ask_visible_anchor[idx] = max(ask_qty, 1.0)

        # --- 2. BID ICEBERG TRACKING ---
        if bid_px == prev_bid_px and ltp <= prev_bid_px and prev_bid_px > 0.0:
            if self.bid_absorbed_vol[idx] == 0.0:
                self.bid_visible_anchor[idx] = max(self.last_bid_qty[idx], bid_qty, 1.0)
            self.bid_absorbed_vol[idx] += dv
            expected_rem_bid = max(0.0, self.last_bid_qty[idx] - dv)
            if bid_qty > expected_rem_bid:
                self.bid_refill_cnt[idx] += 1

            ratio = self.bid_absorbed_vol[idx] / self.bid_visible_anchor[idx]
            if self.bid_refill_cnt[idx] >= min_refills and ratio >= ratio_threshold:
                self.iceberg_state[idx] = STATE_BID_ICEBERG_HOLD
                self.iceberg_ratio[idx] = ratio
                self.absorbed_vol[idx] = self.bid_absorbed_vol[idx]

        elif (bid_px < prev_bid_px and prev_bid_px > 0.0) or (self.iceberg_state[idx] == STATE_BID_ICEBERG_HOLD and (bid_px < prev_bid_px or prev_bid_px == 0.0)):
            # Price ticked DOWN after an active Bid Iceberg -> Support Wall Broken!
            if self.iceberg_state[idx] == STATE_BID_ICEBERG_HOLD:
                self.iceberg_state[idx] = STATE_BID_ICEBERG_BROKEN
                self.broken_ts[idx] = now_sec
                self.absorbed_vol[idx] = max(self.bid_absorbed_vol[idx], self.absorbed_vol[idx])
            elif self.iceberg_state[idx] != STATE_BID_ICEBERG_BROKEN:
                if self.iceberg_state[idx] in (STATE_BID_ICEBERG_HOLD,):
                    self.iceberg_state[idx] = STATE_NONE
                    self.broken_ts[idx] = 0.0
            self.bid_absorbed_vol[idx] = 0.0
            self.bid_refill_cnt[idx] = 0
            self.bid_visible_anchor[idx] = max(bid_qty, 1.0)

        elif bid_px > prev_bid_px:
            # Bid stepped up, reset bid accumulator and only bid-related states
            if self.iceberg_state[idx] in (STATE_BID_ICEBERG_HOLD, STATE_BID_ICEBERG_BROKEN):
                self.iceberg_state[idx] = STATE_NONE
                self.broken_ts[idx] = 0.0
            self.bid_absorbed_vol[idx] = 0.0
            self.bid_refill_cnt[idx] = 0
            self.bid_visible_anchor[idx] = max(bid_qty, 1.0)


        # Update previous state scalars
        self.last_cum_vol[idx] = cum_vol
        self.last_bid_px[idx] = bid_px
        self.last_ask_px[idx] = ask_px
        self.last_bid_qty[idx] = bid_qty
        self.last_ask_qty[idx] = ask_qty

    def evaluate_shadow_verdict(
        self,
        strike_idx: int,
        now_sec: float = 0.0,
    ) -> Dict[str, Any]:
        """Evaluates shadow iceberg verdict for candidate signal in O(1) time.

        STRICT SHADOW INVARIANT: Never vetoes or delays live execution.
        """
        if strike_idx < 0 or strike_idx >= self.num_strikes:
            return {
                "shadow_verdict": VERDICT_NEUTRAL_PASS,
                "iceberg_state": STATE_NONE,
                "iceberg_ratio": 0.0,
                "absorbed_vol": 0.0,
                "refill_cnt": 0,
            }

        # Check decay for broken state
        st = int(self.iceberg_state[strike_idx])
        if st in (STATE_BID_ICEBERG_BROKEN, STATE_ASK_ICEBERG_BROKEN):
            if now_sec > 0.0 and self.broken_ts[strike_idx] > 0.0:
                if (now_sec - self.broken_ts[strike_idx]) > 3.0:
                    st = STATE_NONE
                    self.iceberg_state[strike_idx] = STATE_NONE
                    self.iceberg_ratio[strike_idx] = 0.0
                    self.broken_ts[strike_idx] = 0.0

        ratio = float(self.iceberg_ratio[strike_idx])
        abs_vol = float(self.absorbed_vol[strike_idx])

        # Verdict classification for option buyer:
        if st == STATE_ASK_ICEBERG_HOLD:
            verdict = VERDICT_VETO_ASK_ICEBERG
            refills = int(self.ask_refill_cnt[strike_idx])
        elif st == STATE_BID_ICEBERG_HOLD:
            verdict = VERDICT_SUPPORT_BID_ICEBERG
            refills = int(self.bid_refill_cnt[strike_idx])
        elif st == STATE_ASK_ICEBERG_BROKEN:
            verdict = VERDICT_AMPLIFY_ICEBERG_BREAK
            refills = int(self.ask_refill_cnt[strike_idx])
        else:
            verdict = VERDICT_NEUTRAL_PASS
            refills = 0

        return {
            "shadow_verdict": verdict,
            "iceberg_state": st,
            "iceberg_ratio": round(ratio, 2),
            "absorbed_vol": round(abs_vol, 1),
            "refill_cnt": refills,
        }

    def align_strikes(self, atm_strike: float) -> None:
        """Aligns the 51-strike grid symmetrically around ATM."""
        if atm_strike <= 0.0:
            return
        half = self.num_strikes // 2
        stks = [float(atm_strike + (i - half) * self.strike_step) for i in range(self.num_strikes)]
        self.set_strikes(stks)

    def set_strikes(self, strike_list: Sequence[float]) -> None:
        """Sets strike array and remaps existing buffers where strikes overlap."""
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

        # Remap existing state buffers where strikes overlap
        new_last_bid_px = np.zeros(self.num_strikes, dtype=np.float64)
        new_last_ask_px = np.zeros(self.num_strikes, dtype=np.float64)
        new_last_bid_qty = np.zeros(self.num_strikes, dtype=np.float64)
        new_last_ask_qty = np.zeros(self.num_strikes, dtype=np.float64)
        new_last_cum_vol = np.zeros(self.num_strikes, dtype=np.float64)
        new_bid_abs = np.zeros(self.num_strikes, dtype=np.float64)
        new_ask_abs = np.zeros(self.num_strikes, dtype=np.float64)
        new_bid_refill = np.zeros(self.num_strikes, dtype=np.int32)
        new_ask_refill = np.zeros(self.num_strikes, dtype=np.int32)
        new_bid_anchor = np.ones(self.num_strikes, dtype=np.float64)
        new_ask_anchor = np.ones(self.num_strikes, dtype=np.float64)
        new_state = np.zeros(self.num_strikes, dtype=np.int8)
        new_ratio = np.zeros(self.num_strikes, dtype=np.float64)
        new_abs_vol = np.zeros(self.num_strikes, dtype=np.float64)
        new_broken_ts = np.zeros(self.num_strikes, dtype=np.float64)

        if self.strike_to_idx:
            for stk, new_idx in new_strike_to_idx.items():
                old_idx = self.strike_to_idx.get(stk)
                if old_idx is not None:
                    new_last_bid_px[new_idx] = self.last_bid_px[old_idx]
                    new_last_ask_px[new_idx] = self.last_ask_px[old_idx]
                    new_last_bid_qty[new_idx] = self.last_bid_qty[old_idx]
                    new_last_ask_qty[new_idx] = self.last_ask_qty[old_idx]
                    new_last_cum_vol[new_idx] = self.last_cum_vol[old_idx]
                    new_bid_abs[new_idx] = self.bid_absorbed_vol[old_idx]
                    new_ask_abs[new_idx] = self.ask_absorbed_vol[old_idx]
                    new_bid_refill[new_idx] = self.bid_refill_cnt[old_idx]
                    new_ask_refill[new_idx] = self.ask_refill_cnt[old_idx]
                    new_bid_anchor[new_idx] = self.bid_visible_anchor[old_idx]
                    new_ask_anchor[new_idx] = self.ask_visible_anchor[old_idx]
                    new_state[new_idx] = self.iceberg_state[old_idx]
                    new_ratio[new_idx] = self.iceberg_ratio[old_idx]
                    new_abs_vol[new_idx] = self.absorbed_vol[old_idx]
                    new_broken_ts[new_idx] = self.broken_ts[old_idx]

        for i, s in enumerate(full_stks):
            self.strikes[i] = s
        self.last_bid_px[:] = new_last_bid_px
        self.last_ask_px[:] = new_last_ask_px
        self.last_bid_qty[:] = new_last_bid_qty
        self.last_ask_qty[:] = new_last_ask_qty
        self.last_cum_vol[:] = new_last_cum_vol
        self.bid_absorbed_vol[:] = new_bid_abs
        self.ask_absorbed_vol[:] = new_ask_abs
        self.bid_refill_cnt[:] = new_bid_refill
        self.ask_refill_cnt[:] = new_ask_refill
        self.bid_visible_anchor[:] = new_bid_anchor
        self.ask_visible_anchor[:] = new_ask_anchor
        self.iceberg_state[:] = new_state
        self.iceberg_ratio[:] = new_ratio
        self.absorbed_vol[:] = new_abs_vol
        self.broken_ts[:] = new_broken_ts

        self.strike_to_idx = new_strike_to_idx
        self._strikes_tuple = tuple(full_stks)

    def reset_session(self) -> None:
        """Resets session accumulators for clean day rollover."""
        self.last_bid_px.fill(0.0)
        self.last_ask_px.fill(0.0)
        self.last_bid_qty.fill(0.0)
        self.last_ask_qty.fill(0.0)
        self.last_cum_vol.fill(0.0)
        self.bid_absorbed_vol.fill(0.0)
        self.ask_absorbed_vol.fill(0.0)
        self.bid_refill_cnt.fill(0)
        self.ask_refill_cnt.fill(0)
        self.bid_visible_anchor.fill(1.0)
        self.ask_visible_anchor.fill(1.0)
        self.iceberg_state.fill(0)
        self.iceberg_ratio.fill(0.0)
        self.absorbed_vol.fill(0.0)
        self.broken_ts.fill(0.0)

    def reset_strike(self, idx: int) -> None:
        """Resets accumulators for a single strike slot."""
        if 0 <= idx < self.num_strikes:
            self.last_bid_px[idx] = 0.0
            self.last_ask_px[idx] = 0.0
            self.last_bid_qty[idx] = 0.0
            self.last_ask_qty[idx] = 0.0
            self.last_cum_vol[idx] = 0.0
            self.bid_absorbed_vol[idx] = 0.0
            self.ask_absorbed_vol[idx] = 0.0
            self.bid_refill_cnt[idx] = 0
            self.ask_refill_cnt[idx] = 0
            self.bid_visible_anchor[idx] = 1.0
            self.ask_visible_anchor[idx] = 1.0
            self.iceberg_state[idx] = 0
            self.iceberg_ratio[idx] = 0.0
            self.absorbed_vol[idx] = 0.0
            self.broken_ts[idx] = 0.0
