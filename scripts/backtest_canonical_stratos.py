"""
backtest_canonical_stratos.py — 15m High-Low Zone Market Structure Two-Way Backtester with Global Mutex.
Rules:
1. 1H Macro Trend Alignment.
2. 15m Structure Breakout ($HH_1 \to HH_2$ for Long, $LL_1 \to LL_2$ for Short).
3. 15m High-Low Support Zone $[HH_2 \times 0.9970, HH_2 \times 1.0025]$ for Long.
   15m High-Low Resistance Zone $[LL_2 \times 0.9975, LL_2 \times 1.0030]$ for Short.
4. Clean touch/reversal anywhere inside the High-Low Zone (Rejection Wick >= 35% or Close + Delta).
5. Single Trade Per Breakout Zone (Level consumed upon entry).
6. Symbol Cooldown (Min 45m cooldown, max 2 trades/day/symbol).
7. Global Mutex (Max 1 active position at a time across the entire portfolio).
8. Anti-Liquidity Hunting SL (0.15% beyond zone wick) & 1:3 RR / 9-20 EMA Trailing Exits.
"""
from __future__ import annotations
import os
import sys
import glob
from datetime import datetime, time, timedelta
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
    pivot_highs = []
    pivot_lows = []
    n = len(highs)
    for i in range(lookback, n - lookback):
        if all(highs[i] >= highs[i - k] for k in range(1, lookback + 1)) and \
           all(highs[i] >= highs[i + k] for k in range(1, lookback + 1)):
            pivot_highs.append((i, highs[i]))
        if all(lows[i] <= lows[i - k] for k in range(1, lookback + 1)) and \
           all(lows[i] <= lows[i + k] for k in range(1, lookback + 1)):
            pivot_lows.append((i, lows[i]))
    return pivot_highs, pivot_lows

def run_zone_mutex_backtest(data_root: str, out_dir: str):
    path_5m = os.path.join(data_root, "5m", "canonical_5m_all_symbols.parquet")
    path_15m = os.path.join(data_root, "15m", "canonical_15m_all_symbols.parquet")
    path_1h = os.path.join(data_root, "1h", "canonical_1h_all_symbols.parquet")

    print(f"[load] Loading canonical datasets from {data_root}...")
    df_5m = pd.read_parquet(path_5m)
    df_15m = pd.read_parquet(path_15m)
    df_1h = pd.read_parquet(path_1h)

    symbols = sorted(df_5m["symbol"].unique())
    dates = sorted(df_5m["trade_date"].unique())
    print(f"[backtest] Running STRATOS 15m High-Low Zone Setup with Global Mutex on {len(symbols)} symbols across {len(dates)} dates ({dates})...")

    capital = 1000.0  # Base capital for compounding
    all_trades = []
    active_trade = None  # Global Mutex (Max 1 Position)

    used_levels = {d: {s: set() for s in symbols} for d in dates}
    symbol_last_entry = {s: None for s in symbols}
    symbol_daily_trade_count = {d: {s: 0 for s in symbols} for d in dates}

    group_5m_dict = {sym: df_5m[df_5m["symbol"] == sym].sort_values("dt").reset_index(drop=True) for sym in symbols}
    group_15m_dict = {sym: df_15m[df_15m["symbol"] == sym].sort_values("dt").reset_index(drop=True) for sym in symbols}
    group_1h_dict = {sym: df_1h[df_1h["symbol"] == sym].sort_values("dt").reset_index(drop=True) for sym in symbols}

    unique_timestamps = sorted(df_5m["dt"].unique())

    for cur_dt in unique_timestamps:
        cur_t = cur_dt.time()
        cur_date = str(cur_dt.date())

        # ── 1. ACTIVE POSITION MANAGEMENT ──
        if active_trade is not None:
            sym = active_trade["symbol"]
            sym_5m = group_5m_dict[sym]
            sub_bar = sym_5m[sym_5m["dt"] == cur_dt]
            
            if not sub_bar.empty:
                bar = sub_bar.iloc[0]
                cur_price = bar["close"]
                cur_high = bar["high"]
                cur_low = bar["low"]
                
                side = active_trade["side"]
                entry_p = active_trade["entry_price"]
                sl_p = active_trade["stop_loss"]
                tp2_p = active_trade["tp2_price"]
                buying_power = active_trade["buying_power"]
                qty = active_trade["qty"]

                # ── Long Exits ──
                if side == "BUY":
                    if cur_low <= sl_p:
                        exit_p = sl_p
                        pnl_pct = (exit_p - entry_p) / entry_p * 100.0
                        pnl = buying_power * (pnl_pct / 100.0)
                        capital += pnl
                        active_trade.update({"exit_dt": str(cur_dt), "exit_time": str(cur_t), "exit_price": exit_p, "pnl_pct": round(pnl_pct, 2), "pnl": round(pnl, 2), "new_capital": round(capital, 2), "reason": "STOP_LOSS_HIT"})
                        all_trades.append(active_trade)
                        active_trade = None

                    elif cur_high >= tp2_p:
                        exit_p = tp2_p
                        pnl_pct = (exit_p - entry_p) / entry_p * 100.0
                        pnl = buying_power * (pnl_pct / 100.0)
                        capital += pnl
                        active_trade.update({"exit_dt": str(cur_dt), "exit_time": str(cur_t), "exit_price": exit_p, "pnl_pct": round(pnl_pct, 2), "pnl": round(pnl, 2), "new_capital": round(capital, 2), "reason": "TARGET_2_HIT (1:3 RR)"})
                        all_trades.append(active_trade)
                        active_trade = None

                    else:
                        hist_closes = sym_5m[sym_5m["dt"] <= cur_dt]["close"].to_numpy()
                        if len(hist_closes) >= 10:
                            ema_period = 20 if cur_t < time(13, 30) else 9
                            sess_name = "MORNING_20_EMA" if cur_t < time(13, 30) else "AFTERNOON_9_EMA"
                            ema_arr = compute_ema(hist_closes, ema_period)
                            if len(ema_arr) > 0 and cur_price >= entry_p * 1.0035 and cur_price < ema_arr[-1]:
                                exit_p = cur_price
                                pnl_pct = (exit_p - entry_p) / entry_p * 100.0
                                pnl = buying_power * (pnl_pct / 100.0)
                                capital += pnl
                                active_trade.update({"exit_dt": str(cur_dt), "exit_time": str(cur_t), "exit_price": exit_p, "pnl_pct": round(pnl_pct, 2), "pnl": round(pnl, 2), "new_capital": round(capital, 2), "reason": f"TRAILING_EXIT_{sess_name}"})
                                all_trades.append(active_trade)
                                active_trade = None

                        if active_trade is not None and cur_t >= time(15, 10):
                            exit_p = cur_price
                            pnl_pct = (exit_p - entry_p) / entry_p * 100.0
                            pnl = buying_power * (pnl_pct / 100.0)
                            capital += pnl
                            active_trade.update({"exit_dt": str(cur_dt), "exit_time": str(cur_t), "exit_price": exit_p, "pnl_pct": round(pnl_pct, 2), "pnl": round(pnl, 2), "new_capital": round(capital, 2), "reason": "EOD_SQUAREOFF_15:10"})
                            all_trades.append(active_trade)
                            active_trade = None

                # ── Short Exits ──
                elif side == "SELL":
                    if cur_high >= sl_p:
                        exit_p = sl_p
                        pnl_pct = (entry_p - exit_p) / entry_p * 100.0
                        pnl = buying_power * (pnl_pct / 100.0)
                        capital += pnl
                        active_trade.update({"exit_dt": str(cur_dt), "exit_time": str(cur_t), "exit_price": exit_p, "pnl_pct": round(pnl_pct, 2), "pnl": round(pnl, 2), "new_capital": round(capital, 2), "reason": "STOP_LOSS_HIT"})
                        all_trades.append(active_trade)
                        active_trade = None

                    elif cur_low <= tp2_p:
                        exit_p = tp2_p
                        pnl_pct = (entry_p - exit_p) / entry_p * 100.0
                        pnl = buying_power * (pnl_pct / 100.0)
                        capital += pnl
                        active_trade.update({"exit_dt": str(cur_dt), "exit_time": str(cur_t), "exit_price": exit_p, "pnl_pct": round(pnl_pct, 2), "pnl": round(pnl, 2), "new_capital": round(capital, 2), "reason": "TARGET_2_HIT (1:3 RR)"})
                        all_trades.append(active_trade)
                        active_trade = None

                    else:
                        hist_closes = sym_5m[sym_5m["dt"] <= cur_dt]["close"].to_numpy()
                        if len(hist_closes) >= 10:
                            ema_period = 20 if cur_t < time(13, 30) else 9
                            sess_name = "MORNING_20_EMA" if cur_t < time(13, 30) else "AFTERNOON_9_EMA"
                            ema_arr = compute_ema(hist_closes, ema_period)
                            if len(ema_arr) > 0 and cur_price <= entry_p * 0.9965 and cur_price > ema_arr[-1]:
                                exit_p = cur_price
                                pnl_pct = (entry_p - exit_p) / entry_p * 100.0
                                pnl = buying_power * (pnl_pct / 100.0)
                                capital += pnl
                                active_trade.update({"exit_dt": str(cur_dt), "exit_time": str(cur_t), "exit_price": exit_p, "pnl_pct": round(pnl_pct, 2), "pnl": round(pnl, 2), "new_capital": round(capital, 2), "reason": f"TRAILING_EXIT_{sess_name}"})
                                all_trades.append(active_trade)
                                active_trade = None

                        if active_trade is not None and cur_t >= time(15, 10):
                            exit_p = cur_price
                            pnl_pct = (entry_p - exit_p) / entry_p * 100.0
                            pnl = buying_power * (pnl_pct / 100.0)
                            capital += pnl
                            active_trade.update({"exit_dt": str(cur_dt), "exit_time": str(cur_t), "exit_price": exit_p, "pnl_pct": round(pnl_pct, 2), "pnl": round(pnl, 2), "new_capital": round(capital, 2), "reason": "EOD_SQUAREOFF_15:10"})
                            all_trades.append(active_trade)
                            active_trade = None

            if active_trade is not None:
                continue

        # ── 2. NEW ENTRY EVALUATION (Global Mutex Free) ──
        if cur_t < time(9, 20) or cur_t > time(14, 30):
            continue

        candidate_entries = []
        slice_5m = df_5m[df_5m["dt"] == cur_dt]

        for _, cur_bar in slice_5m.iterrows():
            sym = cur_bar["symbol"]
            
            if symbol_daily_trade_count[cur_date][sym] >= 2:
                continue
            last_entry = symbol_last_entry.get(sym)
            if last_entry and (cur_dt - last_entry).total_seconds() < 2700:
                continue

            sym_5m = group_5m_dict[sym]
            sym_15m = group_15m_dict[sym]
            sym_1h = group_1h_dict[sym]

            sub_5m = sym_5m[sym_5m["dt"] <= cur_dt]
            sub_15m = sym_15m[sym_15m["dt"] <= cur_dt]
            sub_1h = sym_1h[sym_1h["dt"] <= cur_dt]

            if len(sub_5m) < 4 or len(sub_15m) < 4:
                continue

            cur_price = cur_bar["close"]
            cur_high = cur_bar["high"]
            cur_low = cur_bar["low"]
            cur_delta = cur_bar["delta"]

            is_bullish_1h = True
            is_bearish_1h = True
            if len(sub_1h) >= 2:
                c1h = sub_1h["close"].to_numpy()
                is_bullish_1h = c1h[-1] >= c1h[0]
                is_bearish_1h = c1h[-1] < c1h[0]

            highs_15 = sub_15m["high"].to_numpy()
            lows_15 = sub_15m["low"].to_numpy()
            closes_15 = sub_15m["close"].to_numpy()

            p_highs, p_lows = find_pivots(highs_15, lows_15, lookback=1)

            # ── Check Bullish Retest into 15m Support Zone [HH2 * 0.9970, HH2 * 1.0025] ──
            if is_bullish_1h and len(p_highs) >= 2:
                hh1, hh2 = p_highs[-2][1], p_highs[-1][1]
                level_key = f"BUY_{round(hh2, 1)}"
                
                if level_key not in used_levels[cur_date][sym]:
                    if hh2 > hh1 and closes_15[-1] >= hh2 * 1.0002:
                        zone_low = round(hh2 * 0.9970, 2)
                        zone_high = round(hh2 * 1.0025, 2)

                        # Inside / Touching High-Low Support Zone
                        if cur_low <= zone_high and cur_price >= zone_low * 0.9980:
                            is_bullish_candle = (cur_bar["close"] >= cur_bar["open"]) or ((min(cur_bar["open"], cur_bar["close"]) - cur_low) >= (cur_high - cur_low) * 0.35)
                            if is_bullish_candle and cur_delta >= 0:
                                stop_loss = round(min(zone_low, cur_low) * 0.9985, 2)
                                risk_dist = max(cur_price - stop_loss, cur_price * 0.0020)
                                tp2_price = round(cur_price + risk_dist * 3.0, 2)
                                candidate_entries.append({
                                    "symbol": sym, "side": "BUY", "level_key": level_key,
                                    "entry_dt": str(cur_dt), "entry_time": str(cur_t),
                                    "entry_price": cur_price, "stop_loss": stop_loss, "tp2_price": tp2_price,
                                    "risk_pct": round(risk_dist / cur_price * 100.0, 2),
                                    "zone": f"[{zone_low}-{zone_high}]"
                                })

            # ── Check Bearish Retest into 15m Resistance Zone [LL2 * 0.9975, LL2 * 1.0030] ──
            if is_bearish_1h and len(p_lows) >= 2:
                ll1, ll2 = p_lows[-2][1], p_lows[-1][1]
                level_key = f"SELL_{round(ll2, 1)}"

                if level_key not in used_levels[cur_date][sym]:
                    if ll2 < ll1 and closes_15[-1] <= ll2 * 0.9998:
                        zone_low = round(ll2 * 0.9975, 2)
                        zone_high = round(ll2 * 1.0030, 2)

                        # Inside / Touching High-Low Resistance Zone
                        if cur_high >= zone_low and cur_price <= zone_high * 1.0020:
                            is_bearish_candle = (cur_bar["close"] <= cur_bar["open"]) or ((cur_high - max(cur_bar["open"], cur_bar["close"])) >= (cur_high - cur_low) * 0.35)
                            if is_bearish_candle and cur_delta <= 0:
                                stop_loss = round(max(zone_high, cur_high) * 1.0015, 2)
                                risk_dist = max(stop_loss - cur_price, cur_price * 0.0020)
                                tp2_price = round(cur_price - risk_dist * 3.0, 2)
                                candidate_entries.append({
                                    "symbol": sym, "side": "SELL", "level_key": level_key,
                                    "entry_dt": str(cur_dt), "entry_time": str(cur_t),
                                    "entry_price": cur_price, "stop_loss": stop_loss, "tp2_price": tp2_price,
                                    "risk_pct": round(risk_dist / cur_price * 100.0, 2),
                                    "zone": f"[{zone_low}-{zone_high}]"
                                })

        if candidate_entries and active_trade is None:
            chosen = candidate_entries[0]
            sym = chosen["symbol"]
            used_levels[cur_date][sym].add(chosen["level_key"])
            symbol_last_entry[sym] = cur_dt
            symbol_daily_trade_count[cur_date][sym] += 1

            buying_power = capital * 5.0
            qty = max(1, int(buying_power / chosen["entry_price"]))
            chosen["start_capital"] = round(capital, 2)
            chosen["buying_power"] = round(buying_power, 2)
            chosen["qty"] = qty
            active_trade = chosen

    print("\n" + "=" * 95)
    print(f"STRATOS 15M HIGH-LOW ZONE + GLOBAL MUTEX RESULTS ({len(all_trades)} Sequential Trades)")
    print("=" * 95)

    if not all_trades:
        print("[warn] Zero trades executed.")
        return

    tdf = pd.DataFrame(all_trades)
    wins = tdf[tdf["pnl"] > 0]
    losses = tdf[tdf["pnl"] <= 0]

    win_rate = (len(wins) / len(tdf)) * 100.0
    tot_pnl = tdf["pnl"].sum()
    tot_gain = wins["pnl"].sum()
    tot_loss = abs(losses["pnl"].sum())
    profit_factor = (tot_gain / tot_loss) if tot_loss > 0 else 999.0
    roi_pct = ((capital - 1000.0) / 1000.0) * 100.0

    print(f"Starting Capital   : Rs. 1,000.00")
    print(f"Final Capital      : Rs. {capital:,.2f} (ROI: +{roi_pct:.2f}%)")
    print(f"Total Trades       : {len(tdf)}")
    print(f"Winning Trades     : {len(wins)} ({win_rate:.1f}% Win Rate)")
    print(f"Losing Trades      : {len(losses)} ({100.0 - win_rate:.1f}% Loss Rate)")
    print(f"Profit Factor      : {profit_factor:.2f}")
    print(f"Total Net PnL (Rs) : Rs. {tot_pnl:,.2f}")

    print("\n--- UNIQUE SEQUENTIAL TRADE LOG ---")
    cols = ["symbol", "side", "entry_dt", "exit_dt", "entry_price", "exit_price", "pnl_pct", "pnl", "new_capital", "reason"]
    print(tdf[cols].to_string(index=False))

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        csv_path = os.path.join(out_dir, "canonical_stratos_zone_mutex_trades.csv")
        tdf.to_csv(csv_path, index=False)
        print(f"\n[saved] Sequential trade log saved -> {csv_path}")

def main():
    data_root = sys.argv[1] if len(sys.argv) > 1 else "/home/opc/data/canonical_candles"
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "/home/opc/stratos_canonical_results"
    run_zone_mutex_backtest(data_root, out_dir)

if __name__ == "__main__":
    main()
