"""
structure_engine.py — Turnover Bucket Market Structure Engine for STRATOS.
Tracks Higher Highs (HH), Higher Lows (HL), and Breakout Support Floors directly across
Information-Time Turnover Buckets (no time slicing).
"""
from __future__ import annotations
import numpy as np
from STRATOS.config import STRATOS_CONFIG

def compute_ema(prices: np.ndarray, period: int) -> np.ndarray:
    """Fast Exponential Moving Average across Turnover Buckets."""
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
        self.breakout_bucket: dict[str, dict] = {}
        self.swing_highs: dict[str, list] = {}
        self.swing_lows: dict[str, list] = {}

    def check_macro_trend(self, arrays: dict) -> dict:
        """
        Check Trend Alignment across Turnover Buckets:
        Bullish: Price > 20 EMA and 20 EMA is sloping upward.
        """
        closes = arrays.get("close", np.empty(0))
        n = len(closes)
        if n < self.cfg.get("macro_ema_fast", 20):
            # Not enough buckets yet for 20 EMA, check basic price progression
            if n >= 3 and closes[-1] >= closes[0]:
                return {"trend": "BULLISH", "spread_bps": 10.0, "reason": "Early bucket bullish momentum"}
            return {"trend": "NEUTRAL", "spread_bps": 0.0, "reason": "Warming up turnover buckets"}

        ema20 = compute_ema(closes, self.cfg.get("macro_ema_fast", 20))
        if len(ema20) == 0:
            return {"trend": "NEUTRAL", "spread_bps": 0.0, "reason": "EMA warming up"}

        cur_close = closes[-1]
        cur_e20 = ema20[-1]
        prev_e20 = ema20[-2] if len(ema20) > 1 else cur_e20

        if cur_close >= cur_e20 and cur_e20 >= prev_e20:
            spread_bps = (cur_close - cur_e20) / max(cur_close, 1e-6) * 10000.0
            return {"trend": "BULLISH", "spread_bps": spread_bps, "reason": "Price > 20-Bucket EMA & Rising"}
        elif cur_close < cur_e20:
            spread_bps = (cur_e20 - cur_close) / max(cur_close, 1e-6) * 10000.0
            return {"trend": "BEARISH", "spread_bps": spread_bps, "reason": "Price below 20-Bucket EMA"}

        return {"trend": "NEUTRAL", "spread_bps": 0.0, "reason": "Chop/Flat EMA"}

    def find_pivots(self, highs: np.ndarray, lows: np.ndarray, lookback: int = 2):
        """Identify local swing highs and swing lows across turnover buckets."""
        n = len(highs)
        pivot_highs, pivot_lows = [], []
        for i in range(lookback, n - lookback):
            if highs[i] == max(highs[i - lookback : i + lookback + 1]):
                pivot_highs.append((i, highs[i]))
            if lows[i] == min(lows[i - lookback : i + lookback + 1]):
                pivot_lows.append((i, lows[i]))
        return pivot_highs, pivot_lows

    def update_structure(self, symbol: str, arrays: dict) -> dict:
        """
        Evaluate turnover-bucket structure for 2x Higher Highs + Breakout.
        """
        highs = arrays.get("high", np.empty(0))
        lows = arrays.get("low", np.empty(0))
        closes = arrays.get("close", np.empty(0))
        volumes = arrays.get("volume", np.empty(0))
        deltas = arrays.get("delta", np.empty(0))
        n = len(closes)

        if n < 5:
            return {"status": "NO_STRUCTURE", "reason": "Need at least 5 turnover buckets"}

        lookback = self.cfg.get("swing_lookback_buckets", 2)
        p_highs, p_lows = self.find_pivots(highs, lows, lookback)
        self.swing_highs[symbol] = p_highs
        self.swing_lows[symbol] = p_lows

        # If we already have an active support level awaiting retest, verify it
        cur_state = self.state.get(symbol, "IDLE")
        cur_price = closes[-1]

        if len(p_highs) >= 2:
            hh1, hh2 = p_highs[-2][1], p_highs[-1][1]
            # Verify bullish swing progression: HH2 > HH1
            if hh2 > hh1:
                # Check for breakout close above HH2
                if cur_price > hh2 * (1.0 + self.cfg.get("breakout_buffer_pct", 0.0005)):
                    self.state[symbol] = "AWAITING_RETEST"
                    self.support_level[symbol] = hh2
                    self.breakout_bucket[symbol] = {
                        "price": cur_price,
                        "volume": volumes[-1] if len(volumes) > 0 else 1000.0,
                        "delta": deltas[-1] if len(deltas) > 0 else 0.0,
                        "bucket_idx": n - 1,
                        "hh2_level": hh2
                    }
                    return {
                        "status": "AWAITING_RETEST",
                        "support_level": hh2,
                        "hh1": hh1, "hh2": hh2,
                        "reason": f"Turnover Breakout above HH2 ({hh2:.2f}) -> Support Floor Set"
                    }

        if cur_state == "AWAITING_RETEST" and symbol in self.support_level:
            return {
                "status": "AWAITING_RETEST",
                "support_level": self.support_level[symbol],
                "reason": f"Active Support Floor ({self.support_level[symbol]:.2f}) being tracked"
            }

        return {"status": "NO_CLEAN_BREAKOUT", "reason": "Structure not yet in qualified HH2 breakout"}
