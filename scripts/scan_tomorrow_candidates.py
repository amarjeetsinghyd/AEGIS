"""
scan_tomorrow_candidates.py — Scans all 110 universe symbols at today's EOD close (20-Aug)
to identify high-probability STRATOS setups primed for tomorrow morning (21-Aug):
1. Bullish: 1H Up + 15m HH2 Breakout established + Retracing towards Support Zone.
2. Bearish: 1H Down + 15m LL2 Breakdown established + Retracing towards Resistance Zone.
"""
from __future__ import annotations
import os
import sys
import numpy as np
import pandas as pd

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

def find_pivots(highs: np.ndarray, lows: np.ndarray, lookback: int = 1) -> tuple[list, list]:
    pivot_highs, pivot_lows = [], []
    n = len(highs)
    for i in range(lookback, n - lookback):
        if all(highs[i] >= highs[i - k] for k in range(1, lookback + 1)) and \
           all(highs[i] >= highs[i + k] for k in range(1, lookback + 1)):
            pivot_highs.append((i, highs[i]))
        if all(lows[i] <= lows[i - k] for k in range(1, lookback + 1)) and \
           all(lows[i] <= lows[i + k] for k in range(1, lookback + 1)):
            pivot_lows.append((i, lows[i]))
    return pivot_highs, pivot_lows

def scan_candidates(data_root: str):
    p_15m = os.path.join(data_root, "15m", "canonical_15m_all_symbols.parquet")
    p_1h = os.path.join(data_root, "1h", "canonical_1h_all_symbols.parquet")

    df_15m = pd.read_parquet(p_15m)
    df_1h = pd.read_parquet(p_1h)

    symbols = sorted(df_15m["symbol"].unique())
    
    long_candidates = []
    short_candidates = []

    for sym in symbols:
        sub_15 = df_15m[df_15m["symbol"] == sym].sort_values("dt").reset_index(drop=True)
        sub_1h = df_1h[df_1h["symbol"] == sym].sort_values("dt").reset_index(drop=True)

        if len(sub_15) < 6 or len(sub_1h) < 3:
            continue

        c1h = sub_1h["close"].to_numpy()
        is_bullish_1h = c1h[-1] >= c1h[0]
        is_bearish_1h = c1h[-1] < c1h[0]

        highs_15 = sub_15["high"].to_numpy()
        lows_15 = sub_15["low"].to_numpy()
        closes_15 = sub_15["close"].to_numpy()
        cur_price = closes_15[-1]

        p_highs, p_lows = find_pivots(highs_15, lows_15, lookback=1)

        # ── 1. Check Bullish Breakout ($HH_1 \to HH_2$) ──
        if is_bullish_1h and len(p_highs) >= 2:
            hh1, hh2 = p_highs[-2][1], p_highs[-1][1]
            if hh2 > hh1 and closes_15[-1] >= hh2 * 0.995:
                zone_low = round(hh2 * 0.9970, 2)
                zone_high = round(hh2 * 1.0025, 2)
                dist_pct = ((cur_price - hh2) / hh2) * 100.0
                
                # Close enough (within 1.5% of the zone)
                if -0.8 <= dist_pct <= 2.5:
                    status = "IN_ZONE" if (cur_price >= zone_low and cur_price <= zone_high) else ("APPROACHING_FLOOR" if cur_price > zone_high else "NEAR_FLOOR")
                    long_candidates.append({
                        "symbol": sym, "side": "BUY (Long)", "cur_price": cur_price,
                        "hh1": round(hh1, 2), "hh2": round(hh2, 2),
                        "zone": f"₹{zone_low:.2f} - ₹{zone_high:.2f}",
                        "dist_pct": round(dist_pct, 2), "status": status
                    })

        # ── 2. Check Bearish Breakdown ($LL_1 \to LL_2$) ──
        if is_bearish_1h and len(p_lows) >= 2:
            ll1, ll2 = p_lows[-2][1], p_lows[-1][1]
            if ll2 < ll1 and closes_15[-1] <= ll2 * 1.005:
                zone_low = round(ll2 * 0.9975, 2)
                zone_high = round(ll2 * 1.0030, 2)
                dist_pct = ((cur_price - ll2) / ll2) * 100.0

                if -2.5 <= dist_pct <= 0.8:
                    status = "IN_ZONE" if (cur_price >= zone_low and cur_price <= zone_high) else ("APPROACHING_CEILING" if cur_price < zone_low else "NEAR_CEILING")
                    short_candidates.append({
                        "symbol": sym, "side": "SELL (Short)", "cur_price": cur_price,
                        "ll1": round(ll1, 2), "ll2": round(ll2, 2),
                        "zone": f"₹{zone_low:.2f} - ₹{zone_high:.2f}",
                        "dist_pct": round(dist_pct, 2), "status": status
                    })

    print("=" * 95)
    print("🌅 TOMORROW'S PRIME WATCHLIST (STRATOS 15M HIGH-LOW ZONE SETUPS FOR 21-AUG)")
    print("=" * 95)

    print("\n🟢 1. TOP BULLISH BREAKOUT SETUPS (1H Up + 15m HH2 Breakout -> Awaiting Support Retest):")
    if long_candidates:
        ldf = pd.DataFrame(long_candidates).sort_values("dist_pct")
        print(ldf.to_string(index=False))
    else:
        print("No immediate bullish candidates within threshold.")

    print("\n🔴 2. TOP BEARISH BREAKDOWN SETUPS (1H Down + 15m LL2 Breakdown -> Awaiting Resistance Retest):")
    if short_candidates:
        sdf = pd.DataFrame(short_candidates).sort_values("dist_pct", ascending=False)
        print(sdf.to_string(index=False))
    else:
        print("No immediate bearish candidates within threshold.")
    print("=" * 95)

def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "/home/opc/data/canonical_candles"
    scan_candidates(root)

if __name__ == "__main__":
    main()
