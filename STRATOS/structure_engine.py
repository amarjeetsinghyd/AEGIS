"""
structure_engine.py — Two-Way (Long & Short) High-Low Market Structure & Zone Engine for STRATOS.
Tracks:
- Bullish: 15m HH1 -> HH2 Breakout -> Generates High-Low Support Zone [HH2 * 0.9970, HH2 * 1.0025]
- Bearish: 15m LL1 -> LL2 Breakdown -> Generates High-Low Resistance Zone [LL2 * 0.9975, LL2 * 1.0030]
"""
from __future__ import annotations
import numpy as np
from STRATOS.config import STRATOS_CONFIG

def compute_ema(prices: np.ndarray, period: int) -> np.ndarray:
    n = len(prices)
    if n < period:
        return np.array([])
    alpha = 2.0 / (period + 1.0)
    ema = np.empty(n, dtype=np.float64)
    ema[0] = prices[0]
    for i in range(1, n):
        ema[i] = ema[i-1] + alpha * (prices[i] - ema[i-1])
    return ema

class MarketStructureEngine:
    def __init__(self, cfg: dict = None):
        self.cfg = cfg or STRATOS_CONFIG
        self.state: dict[str, str] = {}
        self.support_zone: dict[str, tuple[float, float]] = {}       # (zone_low, zone_high)
        self.resistance_zone: dict[str, tuple[float, float]] = {}    # (zone_low, zone_high)
        self.support_level: dict[str, float] = {}
        self.resistance_level: dict[str, float] = {}
        self.breakout_bucket: dict[str, dict] = {}
        self.breakdown_bucket: dict[str, dict] = {}
        self.swing_highs: dict[str, list] = {}
        self.swing_lows: dict[str, list] = {}

    def check_macro_trend(self, arrays: dict) -> dict:
        closes = arrays.get("close", np.empty(0))
        n = len(closes)
        if n < self.cfg.get("macro_ema_fast", 20):
            if n >= 3:
                return {"trend": "BULLISH" if closes[-1] >= closes[0] else "BEARISH", "spread_bps": 10.0}
            return {"trend": "NEUTRAL", "spread_bps": 0.0}

        ema20 = compute_ema(closes, self.cfg.get("macro_ema_fast", 20))
        if len(ema20) == 0:
            return {"trend": "NEUTRAL", "spread_bps": 0.0}

        cur_close, cur_e20 = closes[-1], ema20[-1]
        prev_e20 = ema20[-2] if len(ema20) > 1 else cur_e20

        if cur_close >= cur_e20 and cur_e20 >= prev_e20:
            return {"trend": "BULLISH", "spread_bps": (cur_close - cur_e20) / cur_close * 10000.0}
        elif cur_close <= cur_e20 and cur_e20 <= prev_e20:
            return {"trend": "BEARISH", "spread_bps": (cur_e20 - cur_close) / cur_close * 10000.0}

        return {"trend": "NEUTRAL", "spread_bps": 0.0}

    def find_pivots(self, highs: np.ndarray, lows: np.ndarray, lookback: int = 1):
        n = len(highs)
        pivot_highs, pivot_lows = [], []
        for i in range(lookback, n - lookback):
            if all(highs[i] >= highs[i - k] for k in range(1, lookback + 1)) and \
               all(highs[i] >= highs[i + k] for k in range(1, lookback + 1)):
                pivot_highs.append((i, highs[i]))
            if all(lows[i] <= lows[i - k] for k in range(1, lookback + 1)) and \
               all(lows[i] <= lows[i + k] for k in range(1, lookback + 1)):
                pivot_lows.append((i, lows[i]))
        return pivot_highs, pivot_lows

    def update_structure(self, symbol: str, arrays: dict) -> dict:
        highs = arrays.get("high", np.empty(0))
        lows = arrays.get("low", np.empty(0))
        closes = arrays.get("close", np.empty(0))
        volumes = arrays.get("volume", np.empty(0))
        deltas = arrays.get("delta", np.empty(0))
        n = len(closes)

        if n < 4:
            return {"status": "NO_STRUCTURE", "reason": "Need at least 4 bars"}

        lookback = self.cfg.get("swing_lookback_buckets", 1)
        p_highs, p_lows = self.find_pivots(highs, lows, lookback)
        self.swing_highs[symbol] = p_highs
        self.swing_lows[symbol] = p_lows

        cur_price = closes[-1]

        # ── 1. Check Bullish Breakout (HH1 -> HH2) & Build High-Low Support Zone ──
        if len(p_highs) >= 2:
            hh1, hh2 = p_highs[-2][1], p_highs[-1][1]
            if hh2 >= hh1:
                # High-Low Support Zone around HH2
                zone_low = round(hh2 * 0.9970, 2)
                zone_high = round(hh2 * 1.0025, 2)
                self.state[symbol] = "AWAITING_LONG_RETEST"
                self.support_level[symbol] = hh2
                self.support_zone[symbol] = (zone_low, zone_high)
                self.breakout_bucket[symbol] = {
                    "price": cur_price, "volume": volumes[-1] if len(volumes) > 0 else 1000.0,
                    "delta": deltas[-1] if len(deltas) > 0 else 0.0, "hh2_level": hh2
                }
                return {
                    "status": "AWAITING_LONG_RETEST", "side": "BUY",
                    "support_level": hh2, "support_zone": (zone_low, zone_high),
                    "reason": f"Breakout above HH2 ({hh2:.2f}) -> Support Zone [{zone_low:.2f} - {zone_high:.2f}]"
                }

        # ── 2. Check Bearish Breakdown (LL1 -> LL2) & Build High-Low Resistance Zone ──
        if len(p_lows) >= 2:
            ll1, ll2 = p_lows[-2][1], p_lows[-1][1]
            if ll2 <= ll1:
                # High-Low Resistance Zone around LL2
                zone_low = round(ll2 * 0.9975, 2)
                zone_high = round(ll2 * 1.0030, 2)
                self.state[symbol] = "AWAITING_SHORT_RETEST"
                self.resistance_level[symbol] = ll2
                self.resistance_zone[symbol] = (zone_low, zone_high)
                self.breakdown_bucket[symbol] = {
                    "price": cur_price, "volume": volumes[-1] if len(volumes) > 0 else 1000.0,
                    "delta": deltas[-1] if len(deltas) > 0 else 0.0, "ll2_level": ll2
                }
                return {
                    "status": "AWAITING_SHORT_RETEST", "side": "SELL",
                    "resistance_level": ll2, "resistance_zone": (zone_low, zone_high),
                    "reason": f"Breakdown below LL2 ({ll2:.2f}) -> Resistance Zone [{zone_low:.2f} - {zone_high:.2f}]"
                }

        # Return active zone if awaiting retest
        cur_state = self.state.get(symbol, "IDLE")
        if cur_state == "AWAITING_LONG_RETEST" and symbol in self.support_level:
            return {
                "status": "AWAITING_LONG_RETEST", "side": "BUY",
                "support_level": self.support_level[symbol],
                "support_zone": self.support_zone.get(symbol, (self.support_level[symbol]*0.997, self.support_level[symbol]*1.0025)),
                "reason": "Tracking Support Zone"
            }
        elif cur_state == "AWAITING_SHORT_RETEST" and symbol in self.resistance_level:
            return {
                "status": "AWAITING_SHORT_RETEST", "side": "SELL",
                "resistance_level": self.resistance_level[symbol],
                "resistance_zone": self.resistance_zone.get(symbol, (self.resistance_level[symbol]*0.9975, self.resistance_level[symbol]*1.003)),
                "reason": "Tracking Resistance Zone"
            }

        return {"status": "NO_CLEAN_BREAKOUT", "reason": "Structure awaiting clean HH2 or LL2"}
