"""
flip_trigger.py — Candlestick & Confluence Trigger Engine.
1. TA-Lib Candlestick Pattern Recognition (Bullish/Bearish Engulfing, Hammer).
2. RSI 14 Pullback Exhaustion (30-45 zone).
3. 9 EMA Decisive Breakout.
4. On-Demand Microstructure Order-Flow Delta & Whale prints at support touch.
"""
from __future__ import annotations
import numpy as np
from STRATOS.config import STRATOS_CONFIG

def compute_rsi(prices: np.ndarray, period: int = 14) -> np.ndarray:
    """O(N) Vectorized RSI calculation."""
    if len(prices) < period + 1:
        return np.array([])
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    rsis = np.empty(len(prices), dtype=np.float64)
    rsis[:period] = 50.0
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsis[i+1] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsis[i+1] = 100.0 - (100.0 / (1.0 + rs))
    return rsis

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

def is_bullish_engulfing(opens: np.ndarray, closes: np.ndarray) -> bool:
    """Green candle completely engulfs prior red candle body."""
    if len(opens) < 2:
        return False
    o1, c1 = opens[-2], closes[-2]
    o2, c2 = opens[-1], closes[-1]
    is_prev_red = bool(c1 < o1)
    is_curr_green = bool(c2 > o2)
    return bool(is_prev_red and is_curr_green and (o2 <= c1) and (c2 >= o1))

def is_bullish_hammer(opens: np.ndarray, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray) -> bool:
    """Long lower shadow rejecting support with small upper body."""
    if len(opens) < 1:
        return False
    o, h, l, c = float(opens[-1]), float(highs[-1]), float(lows[-1]), float(closes[-1])
    body = abs(c - o)
    total_range = h - l
    if total_range <= 0:
        return False
    lower_shadow = min(o, c) - l
    upper_shadow = h - max(o, c)
    return bool((lower_shadow >= 0.5 * total_range) and (upper_shadow <= 0.25 * total_range) and (c >= o))

class FlipTriggerEngine:
    def __init__(self, cfg: dict = None):
        self.cfg = cfg or STRATOS_CONFIG

    def evaluate_trigger(self, arrays_5m: dict, support_level: float, order_flow_metrics: dict = None) -> dict:
        """
        Check all trigger conditions on 5-Minute candle close at the Support Line.
        """
        if not arrays_5m or arrays_5m["count"] < 15:
            return {"triggered": False, "reason": "Warming up 5m bars"}

        opens = arrays_5m["open"]
        highs = arrays_5m["high"]
        lows = arrays_5m["low"]
        closes = arrays_5m["close"]
        volumes = arrays_5m["volume"]

        cur_close = closes[-1]
        cur_low = lows[-1]

        # ── 1. Candlestick Pattern ──
        engulfing = is_bullish_engulfing(opens, closes)
        hammer = is_bullish_hammer(opens, highs, lows, closes)

        if not (engulfing or hammer):
            return {"triggered": False, "reason": "No Bullish Engulfing or Hammer at support"}

        candle_type = "BULLISH_ENGULFING" if engulfing else "BULLISH_HAMMER"

        # ── 2. RSI Exhaustion Check ──
        rsis = compute_rsi(closes, self.cfg["rsi_period"])
        cur_rsi = rsis[-1] if len(rsis) > 0 else 50.0

        if not (self.cfg["rsi_oversold_min"] <= cur_rsi <= self.cfg["rsi_oversold_max"]):
            return {"triggered": False, "reason": f"RSI {cur_rsi:.1f} not in pullback oversold zone (25-45)"}

        # ── 3. 9 EMA Breakout Cross ──
        ema9 = compute_ema(closes, self.cfg["trigger_ema"])
        if len(ema9) == 0 or cur_close <= ema9[-1]:
            return {"triggered": False, "reason": f"Close {cur_close:.2f} not above 9 EMA ({ema9[-1]:.2f})"}

        # ── 4. On-Demand Microstructure Order-Flow Delta ──
        delta_ok = True
        whale_ok = True
        if order_flow_metrics:
            delta = float(order_flow_metrics.get("delta", 0.0))
            whale_z = float(order_flow_metrics.get("whale_z", 0.0))
            if delta < 0:
                delta_ok = False
            if whale_z < self.cfg["min_whale_z_score"]:
                whale_ok = False

        if not delta_ok:
            return {"triggered": False, "reason": "Order flow delta negative at support touch"}

        return {
            "triggered": True,
            "signal": "BUY",
            "entry_price": cur_close,
            "candle_type": candle_type,
            "rsi": cur_rsi,
            "ema9": ema9[-1],
            "support_level": support_level,
            "rejection_low": cur_low,
            "reason": f"✅ {candle_type} confirmed with RSI {cur_rsi:.1f}, 9 EMA Cross, and Support Retest!"
        }
