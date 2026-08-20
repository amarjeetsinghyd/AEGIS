"""
structure_engine.py — Two-Way (Long & Short) Market Structure Engine for STRATOS.
Tracks:
- Bullish: Higher Highs (HH1 -> HH2 Breakout) -> Support Floor
- Bearish: Lower Lows (LL1 -> LL2 Breakdown) -> Resistance Ceiling
"""
from __future__ import annotations
import numpy as np
from STRATOS.config import STRATOS_CONFIG

def compute_ema(prices: np.ndarray, period: int) -> np.ndarray:
    """Fast Exponential Moving Average."""
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
        self.support_level: dict[str, float] = {}
        self.resistance_level: dict[str, float] = {}
        self.breakout_bucket: dict[str, dict] = {}
        self.breakdown_bucket: dict[str, dict] = {}
        self.swing_highs: dict[str, list] = {}
        self.swing_lows: dict[str, list] = {}

    def check_macro_trend(self, arrays: dict) -> dict:
        """
        Check Trend Alignment across Candles / Buckets:
        Bullish: Price > 20 EMA and 20 EMA is sloping upward.
        Bearish: Price < 20 EMA and 20 EMA is sloping downward.
        """
        closes = arrays.get("close", np.empty(0))
        n = len(closes)
        if n < self.cfg.get("macro_ema_fast", 20):
            if n >= 3:
                if closes[-1] >= closes[0]:
                    return {"trend": "BULLISH", "spread_bps": 10.0, "reason": "Early bullish momentum"}
                else:
                    return {"trend": "BEARISH", "spread_bps": 10.0, "reason": "Early bearish momentum"}
            return {"trend": "NEUTRAL", "spread_bps": 0.0, "reason": "Warming up"}

        ema20 = compute_ema(closes, self.cfg.get("macro_ema_fast", 20))
        if len(ema20) == 0:
            return {"trend": "NEUTRAL", "spread_bps": 0.0, "reason": "EMA warming up"}

        cur_close = closes[-1]
        cur_e20 = ema20[-1]
        prev_e20 = ema20[-2] if len(ema20) > 1 else cur_e20

        if cur_close >= cur_e20 and cur_e20 >= prev_e20:
            spread_bps = (cur_close - cur_e20) / max(cur_close, 1e-6) * 10000.0
            return {"trend": "BULLISH", "spread_bps": spread_bps, "reason": "Price > 20 EMA & Rising"}
        elif cur_close <= cur_e20 and cur_e20 <= prev_e20:
            spread_bps = (cur_e20 - cur_close) / max(cur_close, 1e-6) * 10000.0
            return {"trend": "BEARISH", "spread_bps": spread_bps, "reason": "Price < 20 EMA & Falling"}

        return {"trend": "NEUTRAL", "spread_bps": 0.0, "reason": "Chop/Flat EMA"}

    def find_pivots(self, highs: np.ndarray, lows: np.ndarray, lookback: int = 1):
        """Identify local swing highs and swing lows."""
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
        """
        Evaluate market structure for Two-Way setups:
        1. Bullish HH1 -> HH2 Breakout
        2. Bearish LL1 -> LL2 Breakdown
        """
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

        # ── 1. Check Bullish Breakout (HH1 -> HH2) ──
        if len(p_highs) >= 2:
            hh1, hh2 = p_highs[-2][1], p_highs[-1][1]
            if hh2 >= hh1 and cur_price >= hh2 * 1.0002:
                self.state[symbol] = "AWAITING_LONG_RETEST"
                self.support_level[symbol] = hh2
                self.breakout_bucket[symbol] = {
                    "price": cur_price, "volume": volumes[-1] if len(volumes) > 0 else 1000.0,
                    "delta": deltas[-1] if len(deltas) > 0 else 0.0, "hh2_level": hh2
                }
                return {
                    "status": "AWAITING_LONG_RETEST", "side": "BUY",
                    "support_level": hh2, "hh1": hh1, "hh2": hh2,
                    "reason": f"Breakout above HH2 ({hh2:.2f}) -> Support Floor Set"
                }

        # ── 2. Check Bearish Breakdown (LL1 -> LL2) ──
        if len(p_lows) >= 2:
            ll1, ll2 = p_lows[-2][1], p_lows[-1][1]
            if ll2 <= ll1 and cur_price <= ll2 * 0.9998:
                self.state[symbol] = "AWAITING_SHORT_RETEST"
                self.resistance_level[symbol] = ll2
                self.breakdown_bucket[symbol] = {
                    "price": cur_price, "volume": volumes[-1] if len(volumes) > 0 else 1000.0,
                    "delta": deltas[-1] if len(deltas) > 0 else 0.0, "ll2_level": ll2
                }
                return {
                    "status": "AWAITING_SHORT_RETEST", "side": "SELL",
                    "resistance_level": ll2, "ll1": ll1, "ll2": ll2,
                    "reason": f"Breakdown below LL2 ({ll2:.2f}) -> Resistance Ceiling Set"
                }

        # Return active state if awaiting retest
        cur_state = self.state.get(symbol, "IDLE")
        if cur_state == "AWAITING_LONG_RETEST" and symbol in self.support_level:
            return {"status": "AWAITING_LONG_RETEST", "side": "BUY", "support_level": self.support_level[symbol], "reason": "Tracking Support Floor"}
        elif cur_state == "AWAITING_SHORT_RETEST" and symbol in self.resistance_level:
            return {"status": "AWAITING_SHORT_RETEST", "side": "SELL", "resistance_level": self.resistance_level[symbol], "reason": "Tracking Resistance Ceiling"}

        return {"status": "NO_CLEAN_BREAKOUT", "reason": "Structure awaiting clean HH2 or LL2"}
