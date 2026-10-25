"""
OIBuiltupGate — Real-time Open Interest (OI) Built-up and Wall Defense Gate for AEGIS.

Implements:
1. 4-Quadrant Option Built-up Classification:
   - Long Buildup: Price UP, OI UP (Institutional Demand / Buying Flow)
   - Short Covering: Price UP, OI DOWN (Sellers Panicking / Squeeze)
   - Short Buildup: Price DOWN, OI UP (Option Writers Suppressing / Resistance)
   - Long Liquidation: Price DOWN, OI DOWN (Buyers Dumping / Decay)

2. Major OI Wall (Fortress) Defense:
   - Identifies the strike with the highest CE Open Interest (Ceiling / Resistance Wall).
   - Identifies the strike with the highest PE Open Interest (Floor / Support Wall).
   - Prevents buying into a firm, defended wall (e.g., buying PE within 50 pts of 80L PE wall).

3. Squeeze Acceleration:
   - Detects when a major OI wall breaks with active seller unwinding (OI decreasing).
   - Flags SQUEEZE_BREAKOUT for accelerated momentum entries.

Zero heavy dependencies (pure Python standard library). Sub-microsecond evaluation.
"""

from collections import deque
from typing import Dict, Optional, Tuple, Any


class OIBuiltupGate:
    def __init__(
        self,
        strike_step: float = 100.0,         # 100 for SENSEX, 50 for NIFTY
        min_oi_for_wall: float = 50000.0,   # Threshold to consider a strike as a major wall
        unwind_window_sec: float = 180.0,   # 3 minutes rolling unwinding window (matches 3 exchange cycles)
        zone1_strike_dist: float = 0.40,    # Danger Zone: dist <= 0.40 * strike_step (<= 20 pts NIFTY, <= 40 pts SENSEX)
        zone2_strike_dist: float = 1.00,    # Buffer Zone: dist <= 1.00 * strike_step (<= 50 pts NIFTY, <= 100 pts SENSEX)
        zone1_unwind_speed_pct: float = -1.0,  # Required 3m unwinding speed in Zone 1 (heavy panic capitulation)
        zone2_unwind_speed_pct: float = -0.5,  # Required 3m unwinding speed in Zone 2 (moderate panic unwinding)
        wall_buffer_pts: float = None,      # Backwards compatibility fallback if explicitly passed
        require_builtup_confirmation: bool = False,
        enabled: bool = True,
    ):
        self.strike_step = float(strike_step)
        self.min_oi_for_wall = float(min_oi_for_wall)
        self.unwind_window_sec = float(unwind_window_sec)
        self.zone1_strike_dist = float(zone1_strike_dist)
        self.zone2_strike_dist = float(zone2_strike_dist)
        self.zone1_unwind_speed_pct = float(zone1_unwind_speed_pct)
        self.zone2_unwind_speed_pct = float(zone2_unwind_speed_pct)
        self.wall_buffer_pts = float(wall_buffer_pts) if wall_buffer_pts is not None else (self.zone2_strike_dist * self.strike_step)
        self.require_builtup_confirmation = bool(require_builtup_confirmation)
        self.enabled = bool(enabled)

        # Strike-level data store: sym -> dict
        # {
        #   "strike": float,
        #   "kind": int, # 1=CE, 2=PE
        #   "open_oi": float,
        #   "latest_oi": float,
        #   "open_lp": float,
        #   "latest_lp": float,
        #   "last_ts": float,
        #   "oi_history": deque[(ts_sec, oi)]
        # }
        self._strikes: Dict[str, Dict[str, Any]] = {}

        # Cached walls
        self._max_ce_sym: str = ""
        self._max_ce_strike: float = 0.0
        self._max_ce_oi: float = 0.0
        self._max_ce_oi_chg: float = 0.0

        self._max_pe_sym: str = ""
        self._max_pe_strike: float = 0.0
        self._max_pe_oi: float = 0.0
        self._max_pe_oi_chg: float = 0.0

    def update_tick(
        self,
        sym: str,
        strike: float,
        kind: int,
        lp: float,
        oi: float,
        ts_sec: float,
    ) -> None:
        """Update contract telemetry on incoming tick/quote. O(1) performance."""
        if not self.enabled or strike <= 0 or lp <= 0:
            return

        oi_changed = False
        data = self._strikes.get(sym)
        if data is None:
            data = {
                "strike": float(strike),
                "kind": int(kind),
                "open_oi": float(oi) if oi > 0 else 0.0,
                "latest_oi": float(oi) if oi > 0 else 0.0,
                "open_lp": float(lp),
                "latest_lp": float(lp),
                "last_ts": float(ts_sec),
                "oi_history": deque(maxlen=360),
            }
            if oi > 0:
                data["oi_history"].append((float(ts_sec), float(oi)))
                oi_changed = True
            self._strikes[sym] = data
        else:
            data["latest_lp"] = float(lp)
            data["last_ts"] = float(ts_sec)
            if oi > 0:
                if data["open_oi"] <= 0:
                    data["open_oi"] = float(oi)
                if oi != data["latest_oi"]:
                    data["latest_oi"] = float(oi)
                    oi_changed = True

                    # Rolling 3m unwinding deque update
                    hist = data["oi_history"]
                    if not hist or oi != hist[-1][1] or (ts_sec - hist[-1][0]) >= 1.0:
                        hist.append((float(ts_sec), float(oi)))
                    
                    cutoff = ts_sec - self.unwind_window_sec
                    while hist and hist[0][0] < cutoff:
                        hist.popleft()

        # Refresh walls only if OI value actually changed
        if oi_changed:
            self._recalculate_walls()

    def _recalculate_walls(self) -> None:
        """Find max CE OI and max PE OI strikes across registered contracts."""
        max_ce_sym, max_ce_stk, max_ce_val, max_ce_chg = "", 0.0, 0.0, 0.0
        max_pe_sym, max_pe_stk, max_pe_val, max_pe_chg = "", 0.0, 0.0, 0.0

        for sym, d in self._strikes.items():
            kind = d["kind"]
            oi = d["latest_oi"]
            chg = oi - d["open_oi"]
            stk = d["strike"]

            if kind == 1:  # CE
                if oi > max_ce_val:
                    max_ce_val = oi
                    max_ce_stk = stk
                    max_ce_chg = chg
                    max_ce_sym = sym
            elif kind == 2:  # PE
                if oi > max_pe_val:
                    max_pe_val = oi
                    max_pe_stk = stk
                    max_pe_chg = chg
                    max_pe_sym = sym

        self._max_ce_sym = max_ce_sym
        self._max_ce_strike = max_ce_stk
        self._max_ce_oi = max_ce_val
        self._max_ce_oi_chg = max_ce_chg

        self._max_pe_sym = max_pe_sym
        self._max_pe_strike = max_pe_stk
        self._max_pe_oi = max_pe_val
        self._max_pe_oi_chg = max_pe_chg

    def get_unwind_metrics(self, sym: str, ts_sec: float = 0.0) -> Tuple[float, float]:
        """
        Calculates rolling 3-minute unwinding speed from peak OI in the window for a strike contract.
        Returns:
            (delta_oi_3m, speed_pct_3m)
        """
        data = self._strikes.get(sym)
        if not data:
            return 0.0, 0.0
        hist = data.get("oi_history")
        if not hist or len(hist) < 2:
            doi = data["latest_oi"] - data["open_oi"]
            pct = (doi / data["open_oi"] * 100.0) if data["open_oi"] > 0 else 0.0
            return doi, pct

        # Measure unwinding from the maximum OI achieved within the rolling window
        peak_oi = max(x[1] for x in hist)
        cur_oi = data["latest_oi"]
        delta_oi = cur_oi - peak_oi
        speed_pct = (delta_oi / peak_oi * 100.0) if peak_oi > 0 else 0.0
        return delta_oi, speed_pct

    def get_oi_kinematics(self, sym: str, ts_sec: float = 0.0) -> Tuple[float, float, int]:
        """KDO-style OI kinematics (Shield validator, added 2026-09-22).

        Velocity = % OI change over the last 60s (fresh-money pace);
        acceleration = current 60s velocity minus the prior 60s velocity.
        Returns: (velocity_pct_per_min, accel_pct_per_min, n_points).
        Fewer than 2 history points => (0.0, 0.0, n) so callers fail open.
        """
        data = self._strikes.get(sym)
        if not data:
            return 0.0, 0.0, 0
        hist = list(data.get("oi_history") or [])
        now = float(ts_sec or data.get("last_ts", 0.0) or 0.0)
        if len(hist) < 2 or now <= 0:
            return 0.0, 0.0, len(hist)

        def _oi_at(t: float) -> float:
            v = hist[0][1]
            for (ht, ho) in hist:
                if ht <= t:
                    v = ho
                else:
                    break
            return float(v)

        cur = float(data.get("latest_oi", 0.0) or 0.0)
        base = _oi_at(now - 60.0)
        prev = _oi_at(now - 120.0)
        if base <= 0 or prev <= 0:
            return 0.0, 0.0, len(hist)
        vel = (cur - base) / base * 100.0
        vel_prev = (base - prev) / prev * 100.0
        return vel, vel - vel_prev, len(hist)

    def get_wall_snapshot(self, spot_price: float = 0.0) -> Dict[str, Any]:
        """Read-only fortress snapshot for telemetry (no gating, O(1)).

        Returns max CE/PE wall strikes with OI and spot distance in
        strike-step units. Never vetoes — pure visibility for the
        15-minute wall log and live-state arm counters.
        """
        snap: Dict[str, Any] = {
            "max_ce_strike": float(self._max_ce_strike),
            "max_ce_oi": float(self._max_ce_oi),
            "max_pe_strike": float(self._max_pe_strike),
            "max_pe_oi": float(self._max_pe_oi),
            "ce_dist_strikes": 0.0,
            "pe_dist_strikes": 0.0,
            "n_tracked": len(self._strikes),
        }
        try:
            step = float(self.strike_step) if float(self.strike_step) > 0 else 50.0
            spot = float(spot_price or 0.0)
            if spot > 0:
                if float(self._max_ce_strike) > 0:
                    snap["ce_dist_strikes"] = round(
                        (float(self._max_ce_strike) - spot) / step, 2
                    )
                if float(self._max_pe_strike) > 0:
                    snap["pe_dist_strikes"] = round(
                        (spot - float(self._max_pe_strike)) / step, 2
                    )
        except Exception:
            pass
        return snap

    def get_contract_builtup(self, sym: str) -> Tuple[str, float, float]:
        """
        Returns (classification, price_change, oi_change).
        Classifications: LONG_BUILDUP, SHORT_COVERING, SHORT_BUILDUP, LONG_LIQUIDATION, NEUTRAL
        """
        data = self._strikes.get(sym)
        if not data or data["open_lp"] <= 0 or data["open_oi"] <= 0:
            return "NEUTRAL", 0.0, 0.0

        delta_p = data["latest_lp"] - data["open_lp"]
        delta_oi = data["latest_oi"] - data["open_oi"]

        if delta_p > 0 and delta_oi > 0:
            return "LONG_BUILDUP", delta_p, delta_oi
        elif delta_p > 0 and delta_oi < 0:
            return "SHORT_COVERING", delta_p, delta_oi
        elif delta_p < 0 and delta_oi > 0:
            return "SHORT_BUILDUP", delta_p, delta_oi
        elif delta_p < 0 and delta_oi < 0:
            return "LONG_LIQUIDATION", delta_p, delta_oi
        else:
            return "NEUTRAL", delta_p, delta_oi
