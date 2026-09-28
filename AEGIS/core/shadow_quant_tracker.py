"""
AEGIS ShadowQuantTracker — Passive High-Frequency Intraday Options Quant Telemetry.

Passively tracks 5 novel academic and quantitative institutional anomalies in SHADOW MODE
(Zero vetoes, zero trade blocking — 100% empirical telemetry observation for Monday live audit):

1. ATM Straddle Volatility Expansion (P_CE + P_PE compression vs >=1.0% expansion burst).
2. Call vs Put Skew Dislocation (Relative IV/price velocity divergence before breakouts).
3. Option-to-Spot Velocity Lead-Lag (Aggressor option velocity leading spot by >=3x).
4. 0-DTE Vanna and Charm Window (Post-13:30 IST institutional dealer forced rebalancing flow).
5. Intraday Seasonality Windows (Morning Momentum vs Lunch Chop Trap vs Afternoon Gamma).
"""

from __future__ import annotations
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Tuple, Any

IST = timezone(timedelta(hours=5, minutes=30))


class ShadowQuantTracker:
    """
    Lightweight, zero-copy, O(1) shadow telemetry tracker.
    Maintains rolling price histories for ATM Straddle, Call/Put Skew, and Option-Spot Velocity.
    """

    def __init__(self, history_len: int = 120):
        self.history_len = history_len

        # Rolling (ts, straddle_price) deque
        self._straddle_history: deque[Tuple[float, float]] = deque(maxlen=history_len)

        # Rolling option prices: sym -> deque of (ts, price)
        self._option_history: Dict[str, deque[Tuple[float, float]]] = {}

        # Rolling spot prices: deque of (ts, spot_price)
        self._spot_history: deque[Tuple[float, float]] = deque(maxlen=history_len)

    def update_spot(self, spot_price: float, ts_sec: float) -> None:
        """Updates rolling spot price history."""
        if spot_price > 0 and ts_sec > 0:
            self._spot_history.append((ts_sec, spot_price))

    def update_option(self, symbol: str, price: float, ts_sec: float) -> None:
        """Updates rolling option price history for a symbol."""
        if price <= 0 or ts_sec <= 0:
            return
        if symbol not in self._option_history:
            self._option_history[symbol] = deque(maxlen=self.history_len)
        self._option_history[symbol].append((ts_sec, price))

    def update_straddle(self, ce_price: float, pe_price: float, ts_sec: float) -> Optional[float]:
        """Updates ATM Straddle combined price."""
        if ce_price > 0 and pe_price > 0 and ts_sec > 0:
            straddle_px = round(ce_price + pe_price, 2)
            self._straddle_history.append((ts_sec, straddle_px))
            return straddle_px
        return None

    def get_shadow_metrics(
        self,
        symbol: str,
        direction: str,
        current_opt_price: float,
        current_spot_price: float,
        ce_price: float = 0.0,
        pe_price: float = 0.0,
        now_sec: float = 0.0,
    ) -> Dict[str, Any]:
        """
        Computes all 5 shadow quant metrics at the exact instant of trade trigger.
        Returns a rich telemetry dictionary to be recorded in trade logs.
        """
        if now_sec <= 0:
            now_sec = time.time()

        # Update latest prices
        self.update_spot(current_spot_price, now_sec)
        if symbol and current_opt_price > 0:
            self.update_option(symbol, current_opt_price, now_sec)
        if ce_price > 0 and pe_price > 0:
            self.update_straddle(ce_price, pe_price, now_sec)

        # ─── 1. ATM STRADDLE VOLATILITY EXPANSION ───
        straddle_px = (ce_price + pe_price) if (ce_price > 0 and pe_price > 0) else 0.0
        straddle_ret_60s = 0.0
        straddle_expanding = False
        if len(self._straddle_history) >= 2:
            target_ts = now_sec - 60.0
            base_px = self._straddle_history[0][1]
            for ts, px in self._straddle_history:
                if ts >= target_ts:
                    base_px = px
                    break
            if base_px > 0 and straddle_px > 0:
                straddle_ret_60s = (straddle_px - base_px) / base_px
                straddle_expanding = straddle_ret_60s >= 0.010

        # ─── 2. CALL VS PUT SKEW DISLOCATION ───
        skew_ratio_60s = 0.0
        skew_bias = "NEUTRAL"
        skew_aligned = False
        if ce_price > 0 and pe_price > 0:
            skew_ratio_60s = round(ce_price / pe_price, 3) if pe_price > 0 else 1.0
            if skew_ratio_60s >= 1.15:
                skew_bias = "CALL_SKEW"
            elif skew_ratio_60s <= 0.85:
                skew_bias = "PUT_SKEW"
            else:
                skew_bias = "BALANCED"

            skew_aligned = (direction == "UP" and skew_bias == "CALL_SKEW") or \
                           (direction == "DOWN" and skew_bias == "PUT_SKEW")

        # ─── 3. OPTION-TO-SPOT VELOCITY LEAD-LAG ───
        opt_velocity_bps = 0.0
        spot_velocity_bps = 0.0
        velocity_ratio = 1.0
        option_leads_spot = False

        if symbol in self._option_history and len(self._option_history[symbol]) >= 2:
            opt_hist = self._option_history[symbol]
            target_ts = now_sec - 30.0
            base_opt = opt_hist[0][1]
            for ts, px in opt_hist:
                if ts >= target_ts:
                    base_opt = px
                    break
            if base_opt > 0 and current_opt_price > 0:
                opt_velocity_bps = ((current_opt_price - base_opt) / base_opt) * 10000.0

        if len(self._spot_history) >= 2:
            target_ts = now_sec - 30.0
            base_spot = self._spot_history[0][1]
            for ts, px in self._spot_history:
                if ts >= target_ts:
                    base_spot = px
                    break
            if base_spot > 0 and current_spot_price > 0:
                spot_velocity_bps = ((current_spot_price - base_spot) / base_spot) * 10000.0

        dir_mult = 1.0 if direction == "UP" else -1.0
        eff_opt_vel = opt_velocity_bps
        eff_spot_vel = spot_velocity_bps * dir_mult

        if abs(eff_spot_vel) > 0.5:
            velocity_ratio = eff_opt_vel / abs(eff_spot_vel)
        else:
            velocity_ratio = 1.0 if eff_opt_vel <= 0 else 3.5

        option_leads_spot = (eff_opt_vel >= 15.0) and (velocity_ratio >= 2.5)

        # ─── 4. 0-DTE VANNA & CHARM WINDOW ───
        dt_ist = datetime.fromtimestamp(now_sec, IST)
        hour = dt_ist.hour
        minute = dt_ist.minute
        time_mins = hour * 60 + minute

        # 13:30 to 15:15 IST = (13*60 + 30 = 810) to (15*60 + 15 = 915)
        is_afternoon_charm_window = (810 <= time_mins <= 915)
        charm_flow_bias = "NEUTRAL"
        if is_afternoon_charm_window:
            charm_flow_bias = "BULLISH_CHARM" if spot_velocity_bps > 0 else "BEARISH_CHARM"

        # ─── 5. INTRADAY SEASONALITY ZONE ───
        if time_mins < 585:
            seasonality_zone = "OPENING_UNWIND"
        elif 585 <= time_mins < 675:
            seasonality_zone = "MORNING_MOMENTUM"
        elif 675 <= time_mins < 795:
            seasonality_zone = "LUNCH_CHOP_TRAP"
        elif 795 <= time_mins <= 915:
            seasonality_zone = "AFTERNOON_GAMMA"
        else:
            seasonality_zone = "INTRADAY_CLOSE"

        return {
            "shadow_straddle_px": round(straddle_px, 2),
            "shadow_straddle_ret_60s_pct": round(straddle_ret_60s * 100.0, 3),
            "shadow_straddle_expanding": bool(straddle_expanding),
            "shadow_skew_ratio_60s": round(skew_ratio_60s, 3),
            "shadow_skew_bias": skew_bias,
            "shadow_skew_aligned": bool(skew_aligned),
            "shadow_opt_velocity_30s_bps": round(opt_velocity_bps, 1),
            "shadow_spot_velocity_30s_bps": round(spot_velocity_bps, 1),
            "shadow_velocity_ratio": round(velocity_ratio, 2),
            "shadow_option_leads_spot": bool(option_leads_spot),
            "shadow_is_charm_window": bool(is_afternoon_charm_window),
            "shadow_charm_flow_bias": charm_flow_bias,
            "shadow_seasonality_zone": seasonality_zone,
            "shadow_time_ist": dt_ist.strftime("%H:%M:%S"),
        }
