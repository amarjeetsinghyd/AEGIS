"""dump_detector.py - O(1) noise-pullback vs institutional-dump detector.

One instance per open leg. The feed thread calls on_tick() for every message
of the HELD token only. No allocation of containers per call: all state is
floats in __slots__, the 5-level book arrays are read in place.

Decision rule (expected-value, not a fixed threshold):
    hold EV = (1-p)*TP + p*SL          exit EV = P - slip
    => exit when  p  >  (TP - P + slip) / (TP - SL)  =: theta(P)
P = current exit price as a fraction of entry, p = P(dump | evidence).
Close to TP, theta is small but floored (theta_min) so the maker TP is
left alone; near breakeven theta rises, because a false exit costs more.
theta_min=0.50 is a deliberately conservative start: p is an UNCALIBRATED
score, not yet a probability, and a false exit costs real win-rate.
Lower it only as replay evidence earns it.

ALL COEFFICIENTS BELOW ARE UNCALIBRATED PRIORS. Fit them on replayed L2 data
(see calibrate notes in the reply) before trusting any threshold.
"""
from __future__ import annotations

from math import exp

# Depth weights L1..L5 (front of book matters most)
_W0, _W1, _W2, _W3, _W4 = 1.0, 0.6, 0.4, 0.25, 0.15

# Logit coefficients: intercept, sell-aggression, bid-side imbalance,
# bid-depth collapse, ask stacking, bid retreat
_B0, _B1, _B2, _B3, _B4, _B5 = -3.0, 2.5, 1.5, 2.0, 1.0, 1.0


class DumpDetector:
    __slots__ = (
        # config
        "tick", "v0", "tp", "sl", "slip", "arm_pct", "pull_ticks", "persist_s",
        "p_flash", "theta_min", "inv_tf", "inv_tb", "inv_ts",
        # state
        "armed", "zone", "entry", "peak_bid", "last_ts", "last_vol", "last_lp",
        "last_sign", "pb1", "pa1", "sv", "bv", "di", "bw", "aw", "bw_base",
        "aw_base", "t_fire", "n_fire",
        # last evaluation (read these for logging; written every evaluated tick)
        "p", "theta", "pnl", "x1", "x2", "x3", "x4", "x5",
    )

    def __init__(self, tick: float = 0.05, lot: int = 65, tp_pct: float = 0.04,
                 sl_pct: float = -0.08, slip_ticks: float = 2.0,
                 arm_pct: float = 0.02, pull_ticks: float = 2.0,
                 persist_s: float = 0.30, p_flash: float = 0.90,
                 theta_min: float = 0.50, tau_flow: float = 2.0,
                 tau_book: float = 1.0, tau_base: float = 20.0):
        self.tick = tick
        self.v0 = 2.0 * lot            # shrinkage volume for the aggression ratio
        self.tp, self.sl = tp_pct, sl_pct
        self.slip = slip_ticks * tick  # absolute price; converted per entry in arm()
        self.arm_pct, self.pull_ticks = arm_pct, pull_ticks
        self.persist_s, self.p_flash, self.theta_min = persist_s, p_flash, theta_min
        self.inv_tf, self.inv_tb, self.inv_ts = 1.0 / tau_flow, 1.0 / tau_book, 1.0 / tau_base
        self.armed = self.zone = False
        self.entry = self.peak_bid = 0.0
        self.last_ts = self.last_vol = self.last_lp = 0.0
        self.last_sign = 1
        self.pb1 = self.pa1 = 0.0
        self.sv = self.bv = self.di = self.bw = self.aw = self.bw_base = self.aw_base = 0.0
        self.t_fire = 0.0
        self.n_fire = 0
        self.p = self.theta = self.pnl = 0.0
        self.x1 = self.x2 = self.x3 = self.x4 = self.x5 = 0.0

    def arm(self, entry_price: float, tp_pct: float = 0.04, sl_pct: float = -0.08,
            lot: int = 0) -> None:
        """Call at fill time.

        ``lot`` re-derives the shrinkage volume ``v0 = 2 * lot`` for the ACTIVE
        instrument (NIFTY 65 / SENSEX 20). Without it a SENSEX leg inherits the
        NIFTY v0=130 and the aggression ratio becomes 3x too insensitive.

        Cross-trade reset: every tape/book accumulator is zeroed because a new
        strike never inherits the previous trade's cumulative-volume baseline.
        Without this, trade 2's cumulative volume can stay < trade 1's
        ``last_vol`` and ``dv`` freezes at 0.0 for the whole trade.
        """
        if lot > 0:
            self.v0 = 2.0 * float(lot)
        self.entry, self.peak_bid = entry_price, 0.0
        self.tp, self.sl = tp_pct, sl_pct
        self.zone = False
        self.n_fire = 0
        self.t_fire = 0.0
        # Reset cross-trade tape & book state (no inheritance across strikes)
        self.last_ts = self.last_vol = self.last_lp = 0.0
        self.last_sign = 1
        self.pb1 = self.pa1 = 0.0
        self.sv = self.bv = self.di = self.bw = self.aw = 0.0
        self.bw_base = self.aw_base = 0.0
        self.p = self.theta = self.pnl = 0.0
        self.x1 = self.x2 = self.x3 = self.x4 = self.x5 = 0.0
        self.armed = True

    def disarm(self) -> None:
        self.armed = False

    def on_tick(self, ts: float, lp: float, vol: float, bp, bq, sp, sq) -> int:
        """Returns 1 exactly once when an emergency exit should be sent, else 0."""
        b0 = bp[0]
        a0 = sp[0]
        if b0 <= 0.0 or a0 <= 0.0 or b0 >= a0:   # empty / crossed snapshot: skip
            return 0

        dt = ts - self.last_ts if self.last_ts > 0.0 else 0.0
        if dt < 0.0:
            dt = 0.0
        a_f = exp(-dt * self.inv_tf)
        k_b = 1.0 - exp(-dt * self.inv_tb)
        k_s = 1.0 - exp(-dt * self.inv_ts)

        # ---- tape: classify the volume delta against the PRE-update quote ----
        lv = self.last_vol
        dv = vol - lv if (lv > 0.0 and vol > lv) else 0.0
        if vol > lv:
            self.last_vol = vol
        sell = buy = 0.0
        if dv > 0.0:
            pb1 = self.pb1
            if pb1 > 0.0:
                pa1 = self.pa1
                if lp >= pa1:
                    sign = 1
                elif lp <= pb1:
                    sign = -1
                else:
                    mid = 0.5 * (pb1 + pa1)          # inside spread: quote rule,
                    if lp > mid:                     # then tick rule
                        sign = 1
                    elif lp < mid:
                        sign = -1
                    elif lp > self.last_lp:
                        sign = 1
                    elif lp < self.last_lp:
                        sign = -1
                    else:
                        sign = self.last_sign
            else:
                sign = self.last_sign
            self.last_sign = sign
            if sign > 0:
                buy = dv
            else:
                sell = dv
        self.sv = self.sv * a_f + sell
        self.bv = self.bv * a_f + buy

        # ---- book: weighted L1-L5 depth, fast vs slow EWMA ----
        wb = _W0 * bq[0] + _W1 * bq[1] + _W2 * bq[2] + _W3 * bq[3] + _W4 * bq[4]
        wa = _W0 * sq[0] + _W1 * sq[1] + _W2 * sq[2] + _W3 * sq[3] + _W4 * sq[4]
        tot = wb + wa
        imb = (wb - wa) / tot if tot > 0.0 else 0.0
        if self.bw_base <= 0.0:                      # first valid snapshot
            self.di, self.bw, self.aw = imb, wb, wa
            self.bw_base, self.aw_base = wb, wa
        else:
            self.di += k_b * (imb - self.di)
            self.bw += k_b * (wb - self.bw)
            self.aw += k_b * (wa - self.aw)
            self.bw_base += k_s * (wb - self.bw_base)
            self.aw_base += k_s * (wa - self.aw_base)

        self.pb1, self.pa1, self.last_lp, self.last_ts = b0, a0, lp, ts

        if not self.armed:
            return 0

        # ---- gate: only judge while in the profit zone AND pulling back ----
        if b0 > self.peak_bid:
            self.peak_bid = b0
        entry = self.entry
        if not self.zone:
            if self.peak_bid >= entry * (1.0 + self.arm_pct):
                self.zone = True
            else:
                return 0
        pull = (self.peak_bid - b0) / self.tick
        if pull < self.pull_ticks:
            self.n_fire = 0
            return 0

        # ---- features, each in [0, 1] ----
        sar = (self.sv - self.bv) / (self.sv + self.bv + self.v0)
        x1 = sar if sar > 0.0 else 0.0
        x2 = -self.di if self.di < 0.0 else 0.0
        x3 = 1.0 - self.bw / self.bw_base if self.bw_base > 0.0 else 0.0
        x3 = 0.0 if x3 < 0.0 else (1.0 if x3 > 1.0 else x3)
        x4 = self.aw / self.aw_base - 1.0 if self.aw_base > 0.0 else 0.0
        x4 = 0.0 if x4 < 0.0 else (1.0 if x4 > 1.0 else x4)
        x5 = pull / 6.0
        x5 = 1.0 if x5 > 1.0 else x5
        z = _B0 + _B1 * x1 + _B2 * x2 + _B3 * x3 + _B4 * x4 + _B5 * x5
        z = 30.0 if z > 30.0 else (-30.0 if z < -30.0 else z)
        p = 1.0 / (1.0 + exp(-z))

        pnl = (b0 - entry) / entry
        th = (self.tp - pnl + self.slip / entry) / (self.tp - self.sl)
        th = self.theta_min if th < self.theta_min else (0.95 if th > 0.95 else th)

        self.p, self.theta, self.pnl = p, th, pnl
        self.x1, self.x2, self.x3, self.x4, self.x5 = x1, x2, x3, x4, x5

        # ---- Schmitt trigger + persistence in event time ----
        if p >= th:
            if self.n_fire == 0:
                self.t_fire = ts
            self.n_fire += 1
            if self.n_fire >= 2 and (p >= self.p_flash or ts - self.t_fire >= self.persist_s):
                self.armed = False                   # single shot
                return 1
        elif p < 0.8 * th:
            self.n_fire = 0
        return 0

    def reason(self) -> str:
        """Build the log string only when firing (allocates; off the hot path)."""
        return (f"DUMP p={self.p:.2f}>=theta={self.theta:.2f} pnl={self.pnl*100:+.1f}% "
                f"sar={self.x1:.2f} imb={self.x2:.2f} bidcollapse={self.x3:.2f} "
                f"askstack={self.x4:.2f} retreat={self.x5:.2f}")
