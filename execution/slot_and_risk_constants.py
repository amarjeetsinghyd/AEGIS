"""
AEGIS V1.0 — Execution Slot Policy, Stability Telemetry & Quantitative Risk Constants.
Deterministic quality admission, post-trigger stability tracking, and scale-invariant risk parameters.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime as _real_datetime, time as dt_time
from typing import Optional, Dict, Any, List, Tuple

from config.settings import OPTION_TICK_SIZE


class _DateTimeProxy:
    """Dynamic datetime proxy ensuring test monkeypatches on execution.execution_manager.datetime work."""
    def __getattr__(self, name: str) -> Any:
        em = sys.modules.get("execution.execution_manager")
        target = getattr(em, "datetime", _real_datetime) if em else _real_datetime
        return getattr(target, name)

    def __call__(self, *args, **kwargs) -> Any:
        em = sys.modules.get("execution.execution_manager")
        target = getattr(em, "datetime", _real_datetime) if em else _real_datetime
        return target(*args, **kwargs)


datetime = _DateTimeProxy()

class QualitySlotPolicy:
    """Deterministic 3-quality-slots-per-day admission policy."""

    def __init__(
        self,
        max_trades_per_day: int = 3,
        reserve_release_min: int = 0,  # 0 = disabled (no afternoon lockout; all slots free all day)
        dynamic_bar: bool = False,     # False = disabled (any score >= 80 admitted; no artificial escalation)
        consec_loss_stop: int = 2,
        daily_loss_cap_rs: float = 2000.0,
    ):
        self.max_trades_per_day = int(max_trades_per_day)
        self.reserve_release_min = int(reserve_release_min)
        self.dynamic_bar = bool(dynamic_bar)
        self.consec_loss_stop = int(consec_loss_stop)
        self.daily_loss_cap_rs = float(daily_loss_cap_rs)
        self.used = 0
        self._date = ""
        self.day_best_score = -1.0
        self.afternoon_best_score = -1.0

    def roll_date(self, now_sec: float) -> None:
        d = datetime.fromtimestamp(now_sec).strftime("%Y-%m-%d")
        if d != self._date:
            self._date = d
            self.used = 0
            self.day_best_score = -1.0
            self.afternoon_best_score = -1.0

    def admit(
        self,
        score: float,
        now_sec: float,
        consec_losses: int = 0,
        daily_pnl: float = 0.0,
    ) -> Tuple[bool, str]:
        """Decides whether a candidate may consume one of today's slots."""
        self.roll_date(now_sec)
        self._last_admit_ts = float(now_sec)
        if self.used >= self.max_trades_per_day:
            return False, "slot_limit"
        if self.consec_loss_stop > 0 and int(consec_losses) >= self.consec_loss_stop:
            return False, "consec_loss_stop"
        if self.daily_loss_cap_rs > 0 and float(daily_pnl) <= -self.daily_loss_cap_rs:
            return False, "daily_loss_cap"
        dt = datetime.fromtimestamp(now_sec)
        tmin = dt.hour * 60 + dt.minute
        if self.reserve_release_min > 0:
            is_afternoon = tmin >= self.reserve_release_min
            if not is_afternoon and self.used >= (self.max_trades_per_day - 1):
                return False, "slot_reserved_afternoon"
        else:
            is_afternoon = True
        if self.used >= 1 and self.dynamic_bar:
            if not is_afternoon and float(score) <= self.day_best_score:
                return False, "slot_dynamic_bar"
            elif is_afternoon and self.afternoon_best_score > 0 and float(score) <= self.afternoon_best_score:
                return False, "slot_dynamic_bar"
        return True, "slot_admit"

    def mark_admitted(self, score: float, now_sec: float = 0.0) -> None:
        self.used += 1
        s = float(score)
        if s > self.day_best_score:
            self.day_best_score = s
        eff_ts = now_sec if now_sec > 0 else getattr(self, "_last_admit_ts", 0.0)
        if eff_ts > 0:
            dt = datetime.fromtimestamp(eff_ts)
            tmin = dt.hour * 60 + dt.minute
            if tmin >= self.reserve_release_min and s > self.afternoon_best_score:
                self.afternoon_best_score = s

    def rollback_admission(self) -> None:
        """Rolls back the last admitted slot if downstream execution manager rejects the signal."""
        if self.used > 0:
            self.used -= 1

    def state(self) -> Dict[str, Any]:
        return {
            "used": self.used,
            "max_trades_per_day": self.max_trades_per_day,
            "day_best_score": round(self.day_best_score, 4),
            "afternoon_best_score": round(self.afternoon_best_score, 4),
            "date": self._date,
        }


class PostTriggerStabilityTracker:
    """Telemetry-only post-trigger stability verifier (T + window_sec verdict)."""

    def __init__(self, window_sec: float = 10.0, fade_frac: float = 0.25):
        self.window_sec = float(window_sec)
        self.fade_frac = float(fade_frac)
        self._pending: List[Dict[str, Any]] = []

    def arm(
        self,
        sym: str,
        direction: str,
        t_sec: float,
        mid_T: float,
        meta: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        item = {
            "sym": sym,
            "direction": str(direction).upper(),
            "t": float(t_sec),
            "due": float(t_sec) + self.window_sec,
            "mid_T": float(mid_T),
            "meta": dict(meta or {}),
        }
        self._pending.append(item)
        self._pending.sort(key=lambda x: x["due"])
        return item

    def due(self, now_sec: float) -> List[Dict[str, Any]]:
        return [p for p in self._pending if now_sec >= p["due"]]

    def pending_count(self) -> int:
        return len(self._pending)

    def resolve(self, item: Dict[str, Any], mid_now: float) -> Dict[str, Any]:
        self._pending = [p for p in self._pending if p is not item]
        d = item["direction"]
        mid_T = float(item["mid_T"])
        ret_10s = 0.0
        if mid_T > 0 and mid_now > 0:
            ret_10s = (mid_now - mid_T) / mid_T
        trig_ret = abs(float(item["meta"].get("opt_ret_30s", 0.0)))
        fade_thresh = self.fade_frac * max(trig_ret, 1e-4)
        fade_frac_real = 0.0
        if ret_10s < 0 and fade_thresh > 0:
            fade_frac_real = min(1.0, abs(ret_10s) / fade_thresh)
        if mid_now <= 0:
            hold = False
            reason = "no_quote"
        elif ret_10s < -fade_thresh:
            hold = False
            reason = "fade"
        else:
            hold = True
            reason = "hold"
        return {
            "sym": item["sym"],
            "direction": d,
            "t": item["t"],
            "due": item["due"],
            "mid_T": round(mid_T, 4),
            "mid_now": round(float(mid_now), 4),
            "ret_10s": round(ret_10s, 6),
            "fade_frac": round(fade_frac_real, 4),
            "hold": hold,
            "reason": reason,
            "meta": item["meta"],
        }


# ─── AEGIS Risk Constants ───
MAX_HOLD_SEC = 300.0                    # 5-minute baseline hard ceiling
MAX_HOLD_EXTENDED_SEC = 360.0           # 6-minute conditional extension for running winners
HOLD_EXTEND_MIN_PROFIT_PCT = 0.020      # +2.0% minimum gain required to qualify for 6-min hold
def _parse_time_env(env_name: str, default_h: int, default_m: int, default_s: int = 0) -> dt_time:
    raw = os.getenv(env_name, "").strip()
    if not raw:
        return dt_time(default_h, default_m, default_s)
    try:
        parts = [int(p) for p in raw.split(":")]
        return dt_time(*parts)
    except Exception:
        return dt_time(default_h, default_m, default_s)

AEGIS_ENTRY_CUTOFF = _parse_time_env("AEGIS_ENTRY_CUTOFF", 15, 0, 0)   # No new entries after 15:00 (>=8 min before 15:08 flatten)
AEGIS_FORCE_FLATTEN = _parse_time_env("AEGIS_FORCE_FLATTEN", 15, 8, 0) # Force flatten at 15:08 (CAS / broker square-off shield)
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "3")) # Hard safety cap: max 3 trades per day

# ─── Adaptive Quantitative Exit Constants (Claude's 4-Pillar Framework) ───
DEFAULT_MIN_NOISE_FLOOR_RS = 1.50       # Minimum rupee noise floor (₹1.50 = 30 ticks of 5p)
NOISE_FLOOR_SPREAD_MULT = 2.5           # k1: 2.5x bid-ask spread
NOISE_FLOOR_SIGMA_MULT = 2.0            # k2: 2.0x rolling 30-45s premium stdev
NOISE_FLOOR_MIN_PCT = 0.05              # 5% of option entry price as proportional noise hurdle
RESTING_TP_LIMIT_PCT = 0.040            # Fast 4.0% Scalp Take-Profit (Maker Limit order for Mon-Thu 0DTE/1DTE)
FRIDAY_TP_LIMIT_PCT = float(os.environ.get("AEGIS_FRIDAY_TP_PCT", "0.030"))  # Friday 4 DTE adaptive 3.0% TP (1% discount for missing gamma)
FRIDAY_MIN_CAPITAL_RS = float(os.getenv("AEGIS_FRIDAY_MIN_CAPITAL_RS", "3000.0"))  # Friday 4-DTE low-capital live entry shield threshold


def get_adaptive_target_profit_pct(now: Optional[datetime] = None) -> float:
    """Return adaptive TP limit %: 3.0% on Friday (4 DTE, missing gamma), 4.0% on Mon-Thu (0 DTE / 1 DTE)."""
    dt = now or datetime.now()
    if dt.weekday() == 4:
        return FRIDAY_TP_LIMIT_PCT
    return float(os.environ.get("AEGIS_RESTING_TP_PCT", str(RESTING_TP_LIMIT_PCT)))


# ─── Scale-Invariant Normalized Metrics (Basis Points & ATR Multiples) ───
DEFAULT_SPOT_SL_BPS = 6.5               # 6.5 bps of spot (0.065% ~15.5 pts NIFTY, ~52 pts SENSEX)
SPOT_SL_ATR_MULT = 1.0                  # 1.0x 1-minute ATR (dynamic regime expansion)
DELTA_MIN_SPOT_BPS = 1.25               # 1.25 bps minimum spot move for empirical delta (~3 pts NIFTY, ~10 pts SENSEX)

# ─── Universal Price Action Structural Invalidation Constants (Scale-Invariant) ───
MIN_STRUCTURAL_BAR_RANGE_BPS = float(os.environ.get("AEGIS_MIN_STRUCTURAL_RANGE_BPS", "2.0"))   # 2.0 bps (0.02% ~4.6 pts NIFTY, ~14.7 pts SENSEX) hurdle against thin candles/dojis
MIN_STRUCTURAL_BAR_RANGE_ATR_MULT = 0.30                                                       # 0.30x 1m ATR regime floor
STRUCTURAL_SL_BUFFER_BPS = float(os.environ.get("AEGIS_STRUCTURAL_SL_BUFFER_BPS", "0.5"))     # 0.5 bps (~1.15 pts NIFTY, ~3.9 pts SENSEX) sub-tick breathing cushion
STRUCTURAL_SL_BUFFER_ATR_MULT = float(os.environ.get("AEGIS_STRUCTURAL_SL_BUFFER_ATR_MULT", "0.10"))                                                           # 0.10x 1m ATR regime cushion

# Legacy fallbacks maintained for external callers/tests
EMERGENCY_MAX_SL_PCT = 0.35

# ─── AEGIS V4.1 Stepped Risk Compression + Cost-Shield + Near-Target Engine ───
# Scale-Invariant Normalized Percentage Model (strictly %, no fixed paise)
ENABLE_PREMIUM_DISASTER_FLOOR = os.environ.get("AEGIS_ENABLE_PREMIUM_DISASTER_FLOOR", "0").lower() in ("1", "true")
DEFAULT_PREMIUM_HARD_SL_PCT = 0.080  # Retained for optional env flag override
PREMIUM_HARD_SL_PCT = float(os.environ.get("AEGIS_PREMIUM_HARD_SL_PCT", str(DEFAULT_PREMIUM_HARD_SL_PCT)))
FRIDAY_HARD_SL_PCT = float(os.environ.get("AEGIS_FRIDAY_HARD_SL_PCT", "0.065"))     # Friday 4-DTE calibrated disaster floor (-6.5%)
STEP1_TRIGGER_PCT = float(os.environ.get("AEGIS_STEP1_TRIGGER_PCT", "0.030"))         # +3.0% peak gain arms stepped safety net (wide breathing room; +2% choke removed)
STEP1_SL_PCT = float(os.environ.get("AEGIS_STEP1_SL_PCT", "-0.030"))                  # Tightens disaster floor from -12% to -3.0%
STEP2_TRIGGER_PCT = float(os.environ.get("AEGIS_STEP2_TRIGGER_PCT", "0.035"))         # +3.5% peak gain arms cost-shield scratch
STEP2_SL_PCT = float(os.environ.get("AEGIS_STEP2_SL_PCT", "0.000"))                   # Tightens SL to 0.0% (Entry Price / Scratch)
NEAR_TARGET_TRIGGER_PCT = float(os.environ.get("AEGIS_NEAR_TARGET_TRIGGER_PCT", "0.035")) # +3.5% peak gain arms near-target lock
NEAR_TARGET_PULLBACK_TOLERANCE = float(os.environ.get("AEGIS_NEAR_TARGET_TOLERANCE", "0.008")) # 0.8% pullback from peak triggers market exit
def _round_tick(price: float, tick: float = OPTION_TICK_SIZE) -> float:
    if price <= 0:
        return 0.0
    return round(round(price / tick) * tick, 2)


