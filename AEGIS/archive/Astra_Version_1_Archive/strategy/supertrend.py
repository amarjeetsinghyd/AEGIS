"""Lightweight 5-minute Supertrend helpers for entry filtering."""
from __future__ import annotations

import numpy as np
import pandas as pd


def latest_supertrend(candles: pd.DataFrame, period: int = 10, multiplier: float = 3.0):
    """Return the latest Supertrend value from completed OHLC candles."""
    required = {"high", "low", "close"}
    if candles is None or not required.issubset(candles.columns):
        return None

    frame = candles.loc[:, ["high", "low", "close"]].copy()
    frame = frame.apply(pd.to_numeric, errors="coerce").dropna()
    if len(frame) < period + 1:
        return None

    high, low, close = (frame[col].to_numpy(dtype=float) for col in ("high", "low", "close"))
    count = len(frame)
    previous_close = np.empty(count, dtype=float)
    previous_close[0], previous_close[1:] = close[0], close[:-1]
    true_range = np.maximum.reduce((high - low, np.abs(high - previous_close), np.abs(low - previous_close)))

    atr = np.full(count, np.nan, dtype=float)
    start = period - 1
    atr[start] = np.mean(true_range[:period])
    for index in range(start + 1, count):
        atr[index] = ((atr[index - 1] * (period - 1)) + true_range[index]) / period

    midpoint = (high + low) / 2.0
    basic_upper, basic_lower = midpoint + multiplier * atr, midpoint - multiplier * atr
    final_upper, final_lower, supertrend = (np.full(count, np.nan, dtype=float) for _ in range(3))
    final_upper[start], final_lower[start] = basic_upper[start], basic_lower[start]
    supertrend[start] = final_lower[start] if close[start] > final_upper[start] else final_upper[start]

    for index in range(start + 1, count):
        final_upper[index] = basic_upper[index] if basic_upper[index] < final_upper[index - 1] or close[index - 1] > final_upper[index - 1] else final_upper[index - 1]
        final_lower[index] = basic_lower[index] if basic_lower[index] > final_lower[index - 1] or close[index - 1] < final_lower[index - 1] else final_lower[index - 1]
        was_upper = supertrend[index - 1] == final_upper[index - 1]
        supertrend[index] = final_upper[index] if was_upper and close[index] <= final_upper[index] else final_lower[index] if was_upper or close[index] >= final_lower[index] else final_upper[index]

    value = float(supertrend[-1])
    return value if np.isfinite(value) else None


def filter_entry_signal(signal: str, current_price: float, supertrend_value: float):
    """Block counter-trend option entry signals while preserving other signals."""
    signal_text = str(signal).upper().replace("_", " ").strip()
    signal_text = signal_text.replace("BUY", "").replace("SELL", "").strip()

    if signal_text == "PUT" and current_price > supertrend_value:
        return "NEUTRAL", "PUT blocked: price above 5-minute Supertrend"
    if signal_text == "CALL" and current_price < supertrend_value:
        return "NEUTRAL", "CALL blocked: price below 5-minute Supertrend"
    if signal_text in {"CALL", "PUT"}:
        return signal_text, "Supertrend aligned"
    return signal_text or str(signal).upper(), "Supertrend aligned"
