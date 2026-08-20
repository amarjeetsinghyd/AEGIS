"""
structure_engine.py — Market Structure and Multi-Timeframe Trend Engine.
1. Macro Trend Filter (1-Hour 20/50 EMA Ribbon Expansion).
2. Swing Structure Analyzer (2x Higher Highs / Higher Lows Detection & Breakout Registration).
"""
from __future__ import annotations
import numpy as np
from STRATOS.config import STRATOS_CONFIG

def compute_ema(prices: np.ndarray, period: int) -> np.ndarray:
    """O(N) fast Exponential Moving Average."""
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
        
        # State per symbol: structure state, swing highs, swing lows, active support line
        # States: "IDLE", "UPTREND_CONFIRMED", "BREAKOUT_ACTIVE", "AWAITING_RETEST"
        self.state = {}
        self.support_level = {}
        self.breakout_candle = {}
        self.swing_highs = {}
        self.swing_lows = {}

    def check_macro_trend_1h(self, arrays_1h: dict) -> dict:
        """
        Check 1-Hour Chart Trend Alignment:
        Bullish: Price > 20 EMA > 50 EMA and (20 EMA - 50 EMA) spread is widening.
        Bearish: Price < 20 EMA < 50 EMA and (50 EMA - 20 EMA) spread is widening.
        """
        if not arrays_1h or arrays_1h["count"] < 15:
            return {"trend": "NEUTRAL", "spread_bps": 0.0, "reason": "Insufficient 1H bars"}

        closes = arrays_1h["close"]
        ema20 = compute_ema(closes, self.cfg["macro_ema_fast"])
        ema50 = compute_ema(closes, self.cfg["macro_ema_slow"])

        if len(ema20) == 0 or len(ema50) == 0:
            return {"trend": "NEUTRAL", "spread_bps": 0.0, "reason": "EMA warming up"}

        cur_close = closes[-1]
        cur_e20, cur_e50 = ema20[-1], ema50[-1]
        prev_e20, prev_e50 = ema20[-2] if len(ema20) > 1 else cur_e20, ema50[-2] if len(ema50) > 1 else cur_e50

        spread_cur = (cur_e20 - cur_e50) / max(cur_close, 1e-6) * 10000.0
        spread_prev = (prev_e20 - prev_e50) / max(cur_close, 1e-6) * 10000.0

        if cur_close > cur_e20 > cur_e50 and spread_cur > spread_prev and spread_cur >= self.cfg["min_macro_spread_bps"]:
            return {"trend": "BULLISH", "spread_bps": spread_cur, "reason": "20/50 EMA Ribbon Expanding Up"}
        elif cur_close < cur_e20 < cur_e50 and spread_cur < spread_prev and abs(spread_cur) >= self.cfg["min_macro_spread_bps"]:
            return {"trend": "BEARISH", "spread_bps": spread_cur, "reason": "20/50 EMA Ribbon Expanding Down"}

        return {"trend": "NEUTRAL", "spread_bps": spread_cur, "reason": "Ribbon not cleanly expanding"}

    def find_pivots(self, highs: np.ndarray, lows: np.ndarray, lookback: int = 3):
        """Identify local swing highs and swing lows with lookback window."""
        n = len(highs)
        pivot_highs, pivot_lows = [], []
        for i in range(lookback, n - lookback):
            if highs[i] == max(highs[i - lookback : i + lookback + 1]):
                pivot_highs.append((i, highs[i]))
            if lows[i] == min(lows[i - lookback : i + lookback + 1]):
                pivot_lows.append((i, lows[i]))
        return pivot_highs, pivot_lows

    def update_structure(self, symbol: str, arrays_15m: dict, arrays_5m: dict) -> dict:
        """
        Evaluate 15m/5m structure for 2x Higher Highs + 2x Higher Lows and Breakout.
        """
        if not arrays_15m or arrays_15m["count"] < 10:
            return {"status": "NO_STRUCTURE", "reason": "Insufficient 15m bars"}

        highs_15 = arrays_15m["high"]
        lows_15 = arrays_15m["low"]
        closes_15 = arrays_15m["close"]

        p_highs, p_lows = self.find_pivots(highs_15, lows_15, self.cfg["swing_lookback_bars"])
        self.swing_highs[symbol] = p_highs
        self.swing_lows[symbol] = p_lows

        # ── 1. Check for 2x Higher Highs and 2x Higher Lows (Bullish Structure) ──
        if len(p_highs) >= 2 and len(p_lows) >= 2:
            hh1, hh2 = p_highs[-2][1], p_highs[-1][1]
            hl1, hl2 = p_lows[-2][1], p_lows[-1][1]

            if hh2 > hh1 and hl2 > hl1:
                cur_state = self.state.get(symbol, "IDLE")
                cur_price = closes_15[-1]

                # Check breakout above HH2
                if cur_price > hh2 * (1.0 + self.cfg["breakout_buffer_pct"]):
                    self.state[symbol] = "AWAITING_RETEST"
                    self.support_level[symbol] = hh2
                    self.breakout_candle[symbol] = {
                        "price": cur_price,
                        "volume": arrays_15m["volume"][-1],
                        "bar_idx": len(closes_15) - 1,
                        "hh2_level": hh2
                    }
                    return {
                        "status": "AWAITING_RETEST",
                        "support_level": hh2,
                        "hh1": hh1, "hh2": hh2,
                        "hl1": hl1, "hl2": hl2,
                        "reason": f"Clean Breakout above HH2 ({hh2:.2f}) -> Support Floor Registered"
                    }
                elif cur_state == "AWAITING_RETEST":
                    return {
                        "status": "AWAITING_RETEST",
                        "support_level": self.support_level[symbol],
                        "reason": f"Active Support Floor ({self.support_level[symbol]:.2f}) being tracked"
                    }

        return {"status": "NO_CLEAN_BREAKOUT", "reason": "Structure not yet in qualified 2x HH/HL breakout"}
