"""
AEGIS Quality Stack — 3-Slot Daily Policy + Post-Trigger 10s Stability Telemetry.

SHADOW-first safety gear built on the DeepSeek-V4-Pro audit recommendations:
  1. QualitySlotPolicy: max 3 trades/day with afternoon slot reservation,
     dynamic score bar, consecutive-loss stop and daily loss cap.
  2. PostTriggerStabilityTracker: arms every fired setup at trigger time T and
     resolves the hold/fade verdict at T + window_sec using option mid-quotes.

Pure Python, zero heavy imports — hot-path safe (O(1) per call).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Tuple


class QualitySlotPolicy:
    """Deterministic 3-quality-slots-per-day admission policy."""

    def __init__(
        self,
        max_trades_per_day: int = 3,
        reserve_release_min: int = 885,  # 14:45 IST — release reserved 3rd slot
        dynamic_bar: bool = True,
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
        is_afternoon = tmin >= self.reserve_release_min
        if not is_afternoon and self.used >= (self.max_trades_per_day - 1):
            return False, "slot_reserved_afternoon"
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
