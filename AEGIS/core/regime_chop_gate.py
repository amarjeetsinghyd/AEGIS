"""
RegimeChopGate — O(1) per-tick chop-vs-breakout regime discriminator for AEGIS.

Computes:
1. Kaufman Efficiency Ratio (KER): net displacement / total path length
2. Range Compression (Donchian-style): short range vs reference window range
3. Lo-MacKinlay Variance Ratio: random walk vs mean-reverting chop vs trending
4. Post-Trade Cooldown: prevents immediate re-entry into the same chop zone

Status 2026-08-29 audit P1.6: SHADOW MODE in V3.1 — constructed at aegis_gamma_engine.py:115,
updated at _on_underlying_tick:447 and register_trade_close via execution_manager, but
get_regime_state() is NOT yet wired into the hot-path permission chain. Either promote
to a hard veto in V3.2 or delete to avoid false confidence. Currently logged but not enforced.
"""

from collections import deque
from typing import Dict, Optional, Tuple


class RegimeChopGate:
    def __init__(
        self,
        ker_window_sec: float = 60.0,
        ker_veto_threshold: float = 0.01,       # veto only on virtually zero net displacement
        range_window_sec: float = 60.0,
        range_ref_window_sec: float = 1800.0,   # 30-min reference window
        range_compression_veto: float = 0.04,   # veto only on ultra-compressed dead range (< 4%)
        vr_window_ticks: int = 120,
        vr_q: int = 5,
        vr_veto_threshold: float = 0.40,        # veto if severe mean-reversion (VR < 0.40)
        cooldown_sec: float = 120.0,            # 2-minute post trade cooldown
        strike_step: float = 50.0,              # 50 for NIFTY, 100 for SENSEX
    ):
        self.ker_window_sec = ker_window_sec
        self.ker_veto_threshold = ker_veto_threshold
        self.range_window_sec = range_window_sec
        self.range_ref_window_sec = range_ref_window_sec
        self.range_compression_veto = range_compression_veto
        self.vr_window_ticks = vr_window_ticks
        self.vr_q = vr_q
        self.vr_veto_threshold = vr_veto_threshold
        self.cooldown_sec = cooldown_sec
        self.strike_step = strike_step

        self._short: deque = deque()             # (ts, price), time-windowed
        self._long: deque = deque()               # (ts, price), time-windowed
        self._vr_prices: deque = deque(maxlen=vr_window_ticks + 1)  # fixed-count

        self._ker_path_sum: float = 0.0
        self._last_trade_close_ts: float = -1.0e18

    def register_trade_close(self, ts: float) -> None:
        """Call when a trade closes (SL, TP, or time-stop) to start the cooldown."""
        self._last_trade_close_ts = ts

    def update(self, spot: float, ts: float) -> Tuple[bool, str, Dict[str, Optional[float]]]:
        """Call on every new underlying spot tick."""
        if spot <= 0:
            return False, "invalid_spot", {}

        # Avoid polluting incremental sum with identical stagnant spot ticks
        if self._short and spot == self._short[-1][1] and (ts - self._short[-1][0]) < 0.2:
            return self.get_regime_state(ts)

        # Maintain short window & incremental path-length sum
        if self._short:
            _, last_price = self._short[-1]
            self._ker_path_sum += abs(spot - last_price)
        self._short.append((ts, spot))
        while self._short and (ts - self._short[0][0]) > self.ker_window_sec:
            old_ts, old_price = self._short.popleft()
            if self._short:
                _, nxt_price = self._short[0]
                self._ker_path_sum -= abs(nxt_price - old_price)
                if self._ker_path_sum < 0.0:
                    self._ker_path_sum = 0.0  # guard against float drift

        # Maintain long (reference range) window
        self._long.append((ts, spot))
        while self._long and (ts - self._long[0][0]) > self.range_ref_window_sec:
            self._long.popleft()

        # Maintain fixed-count window for variance ratio
        self._vr_prices.append(spot)

        return self.get_regime_state(ts)

    def get_regime_state(self, ts: float) -> Tuple[bool, str, Dict[str, Optional[float]]]:
        """Read-only evaluation of the current regime state."""
        metrics: Dict[str, Optional[float]] = {}

        if len(self._short) < 5 or len(self._long) < 5:
            return False, "warmup", metrics

        # 1. Kaufman Efficiency Ratio: net displacement / total path length walked
        net_move = abs(self._short[-1][1] - self._short[0][1])
        ker = (net_move / self._ker_path_sum) if self._ker_path_sum > 1e-9 else 0.0
        metrics["ker"] = ker

        # 2. Range compression: current short-window range vs long reference range
        short_high = max(p for _, p in self._short)
        short_low = min(p for _, p in self._short)
        long_high = max(p for _, p in self._long)
        long_low = min(p for _, p in self._long)
        short_range = short_high - short_low
        long_range = long_high - long_low
        range_ratio = (short_range / long_range) if long_range > 1e-9 else 0.0
        metrics["range_ratio"] = range_ratio
        metrics["short_range_pts"] = short_range
        metrics["short_range_strikes"] = short_range / self.strike_step

        # 3. Lo-MacKinlay-style variance ratio: Var(q-tick return) / (q * Var(1-tick return))
        vr = None
        if len(self._vr_prices) >= self.vr_q * 4:
            prices = list(self._vr_prices)
            rets_1 = [prices[i + 1] - prices[i] for i in range(len(prices) - 1)]
            n1 = len(rets_1)
            mean1 = sum(rets_1) / n1
            var1 = sum((r - mean1) ** 2 for r in rets_1) / max(n1 - 1, 1)
            q = self.vr_q
            rets_q = [prices[i + q] - prices[i] for i in range(0, len(prices) - q, q)]
            if rets_q and var1 > 1e-9:
                nq = len(rets_q)
                meanq = sum(rets_q) / nq
                varq = sum((r - meanq) ** 2 for r in rets_q) / max(nq - 1, 1)
                vr = varq / (q * var1)
        metrics["variance_ratio"] = vr

        # ---- veto chain ----
        if (ts - self._last_trade_close_ts) < self.cooldown_sec:
            return False, "post_trade_cooldown", metrics

        if range_ratio < self.range_compression_veto:
            return False, "range_compressed", metrics

        if ker < self.ker_veto_threshold:
            return False, "low_efficiency_chop", metrics

        if vr is not None and vr < self.vr_veto_threshold:
            return False, "mean_reversion_signature", metrics

        return True, "regime_ok", metrics
