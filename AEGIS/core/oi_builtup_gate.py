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

    def evaluate_gate(
        self,
        sym: str,
        spot_price: float,
        direction: str,  # "UP" / "CE" or "DOWN" / "PE"
        now_sec: float = 0.0,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Evaluates OI Gate permission for a candidate trade.

        Returns:
            (allowed: bool, reason: str, metrics: dict)
        """
        if not self.enabled:
            return True, "gate_disabled", {}

        is_pe = (direction.upper() in ["DOWN", "PE", "PUT"]) or ("PE" in sym.upper())
        is_ce = not is_pe

        metrics = {
            "sym": sym,
            "is_pe": is_pe,
            "spot": spot_price,
            "max_ce_strike": self._max_ce_strike,
            "max_ce_oi": self._max_ce_oi,
            "max_ce_oi_chg": self._max_ce_oi_chg,
            "max_pe_strike": self._max_pe_strike,
            "max_pe_oi": self._max_pe_oi,
            "max_pe_oi_chg": self._max_pe_oi_chg,
            "builtup": "UNKNOWN",
            "is_squeeze": False,
        }

        # 1. Check Contract Built-up Confirmation
        builtup, dp, doi = self.get_contract_builtup(sym)
        metrics["builtup"] = builtup
        metrics["dp"] = dp
        metrics["doi"] = doi

        if self.require_builtup_confirmation and builtup != "NEUTRAL":
            # If buying PE, option MUST NOT be in Long Liquidation (buyers dumping) or Short Buildup (heavy selling)
            if is_pe and builtup in ["LONG_LIQUIDATION", "SHORT_BUILDUP"]:
                return False, f"veto_pe_{builtup.lower()}", metrics
            # If buying CE, option MUST NOT be in Long Liquidation or Short Buildup
            if is_ce and builtup in ["LONG_LIQUIDATION", "SHORT_BUILDUP"]:
                return False, f"veto_ce_{builtup.lower()}", metrics

        # 2. Check ATM / Immediate Strike Heavy Writing Gate (+50% to +200%+ OI surge)
        if spot_price > 0 and self.strike_step > 0:
            atm_strike = round(spot_price / self.strike_step) * self.strike_step
            min_writing_oi = self.min_oi_for_wall * 0.5

            if is_pe:
                # Strikes right at or below spot defending immediate downside
                check_strikes = {atm_strike - self.strike_step, atm_strike}
                for sym_k, d in self._strikes.items():
                    if d["kind"] == 2 and d["strike"] in check_strikes and d["strike"] <= (spot_price + 0.25 * self.strike_step):
                        cur_oi = d["latest_oi"]
                        open_oi = d["open_oi"]
                        if cur_oi >= min_writing_oi:
                            doi_pct = ((cur_oi - open_oi) / open_oi * 100.0) if open_oi > 0 else (100.0 if cur_oi > 0 else 0.0)
                            if doi_pct >= 50.0:
                                _, speed_3m = self.get_unwind_metrics(sym_k, ts_sec=now_sec)
                                if speed_3m > self.zone2_unwind_speed_pct:  # Writers defending, not panicking
                                    metrics["heavy_writing_strike"] = d["strike"]
                                    metrics["heavy_writing_doi_pct"] = doi_pct
                                    return False, f"veto_heavy_put_writing_{int(d['strike'])}_doi_{int(doi_pct)}pct", metrics

            elif is_ce:
                # Strikes right at or above spot defending immediate upside
                check_strikes = {atm_strike, atm_strike + self.strike_step}
                for sym_k, d in self._strikes.items():
                    if d["kind"] == 1 and d["strike"] in check_strikes and d["strike"] >= (spot_price - 0.25 * self.strike_step):
                        cur_oi = d["latest_oi"]
                        open_oi = d["open_oi"]
                        if cur_oi >= min_writing_oi:
                            doi_pct = ((cur_oi - open_oi) / open_oi * 100.0) if open_oi > 0 else (100.0 if cur_oi > 0 else 0.0)
                            if doi_pct >= 50.0:
                                _, speed_3m = self.get_unwind_metrics(sym_k, ts_sec=now_sec)
                                if speed_3m > self.zone2_unwind_speed_pct:  # Writers defending, not panicking
                                    metrics["heavy_writing_strike"] = d["strike"]
                                    metrics["heavy_writing_doi_pct"] = doi_pct
                                    return False, f"veto_heavy_call_writing_{int(d['strike'])}_doi_{int(doi_pct)}pct", metrics

        # 3. Check Directional Major OI Wall Proximity (Ceiling & Floor Defense)
        if spot_price > 0:
            if is_pe:
                # Immediate Floor Candidates: PE strikes at or below spot
                floor_candidates = [
                    (d["strike"], sym_k, d["latest_oi"], d["latest_oi"] - d["open_oi"])
                    for sym_k, d in self._strikes.items()
                    if d["kind"] == 2
                    and d["latest_oi"] >= self.min_oi_for_wall
                    and d["strike"] <= (spot_price + 0.20 * self.strike_step)
                ]

                floor_strike = 0.0
                floor_sym = ""
                floor_oi = 0.0
                floor_oi_chg = 0.0

                if floor_candidates:
                    floor_strike, floor_sym, floor_oi, floor_oi_chg = max(floor_candidates, key=lambda x: x[2])
                elif self._max_pe_strike > 0 and self._max_pe_oi >= self.min_oi_for_wall:
                    floor_strike = self._max_pe_strike
                    floor_sym = self._max_pe_sym
                    floor_oi = self._max_pe_oi
                    floor_oi_chg = self._max_pe_oi_chg

                if floor_strike > 0 and floor_oi >= self.min_oi_for_wall:
                    dist_to_pe_wall = spot_price - floor_strike
                    dist_strikes = dist_to_pe_wall / self.strike_step if self.strike_step > 0 else 999.0
                    metrics["dist_to_wall"] = dist_to_pe_wall
                    metrics["dist_to_wall_strikes"] = dist_strikes

                    # 3-Minute Rolling Unwinding Speed on the Put Wall Strike
                    doi_3m, speed_3m = self.get_unwind_metrics(floor_sym, ts_sec=now_sec)
                    metrics["wall_doi_3m"] = doi_3m
                    metrics["wall_speed_pct_3m"] = speed_3m

                    # Zone-Adaptive Wall Defense (Normalized by Strike Step):
                    if dist_strikes > self.zone2_strike_dist:
                        # Zone 3: Clear Runway (> 1.0 strike away) -> No wall restriction!
                        pass
                    elif dist_strikes > self.zone1_strike_dist:
                        # Zone 2: Buffer Zone (0.40 to 1.00 strikes away)
                        # Requires moderate panic unwinding (speed <= zone2_unwind_speed_pct, e.g. <= -0.5% in 3m)
                        if speed_3m <= self.zone2_unwind_speed_pct:
                            metrics["is_squeeze"] = True
                        elif floor_oi_chg >= 0 or speed_3m > self.zone2_unwind_speed_pct:
                            return False, f"veto_firm_pe_wall_zone2_{int(floor_strike)}", metrics
                    elif dist_strikes >= 0:
                        # Zone 1: Danger Zone (0.0 to 0.40 strikes away)
                        # Requires heavy panic unwinding (speed <= zone1_unwind_speed_pct, e.g. <= -1.0% in 3m)
                        if speed_3m <= self.zone1_unwind_speed_pct:
                            metrics["is_squeeze"] = True
                        else:
                            return False, f"veto_firm_pe_wall_zone1_{int(floor_strike)}", metrics
                    else:
                        # Wall Penetration (dist < 0, spot has penetrated past the floor)
                        if floor_oi_chg < 0 or speed_3m < 0:
                            metrics["is_squeeze"] = True

            elif is_ce:
                # Immediate Ceiling Candidates: CE strikes at or above spot
                ceil_candidates = [
                    (d["strike"], sym_k, d["latest_oi"], d["latest_oi"] - d["open_oi"])
                    for sym_k, d in self._strikes.items()
                    if d["kind"] == 1
                    and d["latest_oi"] >= self.min_oi_for_wall
                    and d["strike"] >= (spot_price - 0.20 * self.strike_step)
                ]

                ceil_strike = 0.0
                ceil_sym = ""
                ceil_oi = 0.0
                ceil_oi_chg = 0.0

                if ceil_candidates:
                    ceil_strike, ceil_sym, ceil_oi, ceil_oi_chg = max(ceil_candidates, key=lambda x: x[2])
                elif self._max_ce_strike > 0 and self._max_ce_oi >= self.min_oi_for_wall:
                    ceil_strike = self._max_ce_strike
                    ceil_sym = self._max_ce_sym
                    ceil_oi = self._max_ce_oi
                    ceil_oi_chg = self._max_ce_oi_chg

                if ceil_strike > 0 and ceil_oi >= self.min_oi_for_wall:
                    dist_to_ce_wall = ceil_strike - spot_price
                    dist_strikes = dist_to_ce_wall / self.strike_step if self.strike_step > 0 else 999.0
                    metrics["dist_to_wall"] = dist_to_ce_wall
                    metrics["dist_to_wall_strikes"] = dist_strikes

                    # 3-Minute Rolling Unwinding Speed on the Call Wall Strike
                    doi_3m, speed_3m = self.get_unwind_metrics(ceil_sym, ts_sec=now_sec)
                    metrics["wall_doi_3m"] = doi_3m
                    metrics["wall_speed_pct_3m"] = speed_3m

                    # Zone-Adaptive Wall Defense (Normalized by Strike Step):
                    if dist_strikes > self.zone2_strike_dist:
                        # Zone 3: Clear Runway (> 1.0 strike away) -> No wall restriction!
                        pass
                    elif dist_strikes > self.zone1_strike_dist:
                        # Zone 2: Buffer Zone (0.40 to 1.00 strikes away)
                        # Requires moderate panic unwinding (speed <= zone2_unwind_speed_pct, e.g. <= -0.5% in 3m)
                        if speed_3m <= self.zone2_unwind_speed_pct:
                            metrics["is_squeeze"] = True
                        elif ceil_oi_chg >= 0 or speed_3m > self.zone2_unwind_speed_pct:
                            return False, f"veto_firm_ce_wall_zone2_{int(ceil_strike)}", metrics
                    elif dist_strikes >= 0:
                        # Zone 1: Danger Zone (0.0 to 0.40 strikes away)
                        # Requires heavy panic unwinding (speed <= zone1_unwind_speed_pct, e.g. <= -1.0% in 3m)
                        if speed_3m <= self.zone1_unwind_speed_pct:
                            metrics["is_squeeze"] = True
                        else:
                            return False, f"veto_firm_ce_wall_zone1_{int(ceil_strike)}", metrics
                    else:
                        # Wall Penetration (dist < 0, spot has penetrated past the ceiling)
                        if ceil_oi_chg < 0 or speed_3m < 0:
                            metrics["is_squeeze"] = True

        return True, "permitted", metrics
