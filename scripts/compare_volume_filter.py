"""
compare_volume_filter.py — Compare STRATOS Performance With vs Without Volume Filter.
"""
from __future__ import annotations
import os
import sys
import pandas as pd
import numpy as np
from datetime import time

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

def simulate_strategy(data_root: str, use_volume_filter: bool, max_open_pos: int = 1):
    path_5m = os.path.join(data_root, "5m", "canonical_5m_all_symbols.parquet")
    path_15m = os.path.join(data_root, "15m", "canonical_15m_all_symbols.parquet")
    path_1h = os.path.join(data_root, "1h", "canonical_1h_all_symbols.parquet")

    df_5m = pd.read_parquet(path_5m)
    df_15m = pd.read_parquet(path_15m)
    df_1h = pd.read_parquet(path_1h)

    symbols = sorted(df_5m["symbol"].unique())
    dates = sorted(df_5m["trade_date"].unique())

    capital = 1000.0
    all_trades = []
    active_trades = {}  # {symbol: trade_dict}

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
        closed_syms = []
        for sym, active_trade in list(active_trades.items()):
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

                # Long exits
                if side == "BUY":
                    if cur_low <= sl_p:
                        exit_p = sl_p
                        pnl = buying_power * ((exit_p - entry_p) / entry_p)
                        capital += pnl
                        active_trade.update({"exit_dt": str(cur_dt), "exit_price": exit_p, "pnl": pnl, "pnl_pct": (exit_p - entry_p)/entry_p*100, "reason": "STOP_LOSS_HIT"})
                        all_trades.append(active_trade)
                        closed_syms.append(sym)
                    elif cur_high >= tp2_p:
                        exit_p = tp2_p
                        pnl = buying_power * ((exit_p - entry_p) / entry_p)
                        capital += pnl
                        active_trade.update({"exit_dt": str(cur_dt), "exit_price": exit_p, "pnl": pnl, "pnl_pct": (exit_p - entry_p)/entry_p*100, "reason": "TARGET_2_HIT"})
                        all_trades.append(active_trade)
                        closed_syms.append(sym)
                    else:
                        hist_closes = sym_5m[sym_5m["dt"] <= cur_dt]["close"].to_numpy()
                        if len(hist_closes) >= 10:
                            ema_period = 20 if cur_t < time(13, 30) else 9
                            ema_arr = compute_ema(hist_closes, ema_period)
                            if len(ema_arr) > 0 and cur_price >= entry_p * 1.0035 and cur_price < ema_arr[-1]:
                                exit_p = cur_price
                                pnl = buying_power * ((exit_p - entry_p) / entry_p)
                                capital += pnl
                                active_trade.update({"exit_dt": str(cur_dt), "exit_price": exit_p, "pnl": pnl, "pnl_pct": (exit_p - entry_p)/entry_p*100, "reason": "TRAILING_EMA_EXIT"})
                                all_trades.append(active_trade)
                                closed_syms.append(sym)
                        if sym not in closed_syms and cur_t >= time(15, 10):
                            exit_p = cur_price
                            pnl = buying_power * ((exit_p - entry_p) / entry_p)
                            capital += pnl
                            active_trade.update({"exit_dt": str(cur_dt), "exit_price": exit_p, "pnl": pnl, "pnl_pct": (exit_p - entry_p)/entry_p*100, "reason": "EOD_SQUAREOFF"})
                            all_trades.append(active_trade)
                            closed_syms.append(sym)

                # Short exits
                elif side == "SELL":
                    if cur_high >= sl_p:
                        exit_p = sl_p
                        pnl = buying_power * ((entry_p - exit_p) / entry_p)
                        capital += pnl
                        active_trade.update({"exit_dt": str(cur_dt), "exit_price": exit_p, "pnl": pnl, "pnl_pct": (entry_p - exit_p)/entry_p*100, "reason": "STOP_LOSS_HIT"})
                        all_trades.append(active_trade)
                        closed_syms.append(sym)
                    elif cur_low <= tp2_p:
                        exit_p = tp2_p
                        pnl = buying_power * ((entry_p - exit_p) / entry_p)
                        capital += pnl
                        active_trade.update({"exit_dt": str(cur_dt), "exit_price": exit_p, "pnl": pnl, "pnl_pct": (entry_p - exit_p)/entry_p*100, "reason": "TARGET_2_HIT"})
                        all_trades.append(active_trade)
                        closed_syms.append(sym)
                    else:
                        hist_closes = sym_5m[sym_5m["dt"] <= cur_dt]["close"].to_numpy()
                        if len(hist_closes) >= 10:
                            ema_period = 20 if cur_t < time(13, 30) else 9
                            ema_arr = compute_ema(hist_closes, ema_period)
                            if len(ema_arr) > 0 and cur_price <= entry_p * 0.9965 and cur_price > ema_arr[-1]:
                                exit_p = cur_price
                                pnl = buying_power * ((entry_p - exit_p) / entry_p)
                                capital += pnl
                                active_trade.update({"exit_dt": str(cur_dt), "exit_price": exit_p, "pnl": pnl, "pnl_pct": (entry_p - exit_p)/entry_p*100, "reason": "TRAILING_EMA_EXIT"})
                                all_trades.append(active_trade)
                                closed_syms.append(sym)
                        if sym not in closed_syms and cur_t >= time(15, 10):
                            exit_p = cur_price
                            pnl = buying_power * ((entry_p - exit_p) / entry_p)
                            capital += pnl
                            active_trade.update({"exit_dt": str(cur_dt), "exit_price": exit_p, "pnl": pnl, "pnl_pct": (entry_p - exit_p)/entry_p*100, "reason": "EOD_SQUAREOFF"})
                            all_trades.append(active_trade)
                            closed_syms.append(sym)

        for s in closed_syms:
            active_trades.pop(s, None)

        # ── 2. NEW ENTRY EVALUATION ──
        if len(active_trades) >= max_open_pos:
            continue
        if cur_t < time(9, 20) or cur_t > time(14, 30):
            continue

        slice_5m = df_5m[df_5m["dt"] == cur_dt]
        for _, cur_bar in slice_5m.iterrows():
            if len(active_trades) >= max_open_pos:
                break
            sym = cur_bar["symbol"]
            if sym in active_trades:
                continue
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

            # Long
            if is_bullish_1h and len(p_highs) >= 2:
                hh1, hh2 = p_highs[-2][1], p_highs[-1][1]
                level_key = f"BUY_{round(hh2, 1)}"
                if level_key not in used_levels[cur_date][sym]:
                    if hh2 > hh1 and closes_15[-1] >= hh2 * 1.0002:
                        support_floor = hh2
                        if cur_low <= support_floor * 1.0025 and cur_price >= support_floor * 0.9975:
                            is_vol_valid = True
                            if use_volume_filter:
                                vols_15m = sub_15m["volume"].to_numpy()
                                vols_5m = sub_5m["volume"].to_numpy()
                                breakout_vol = vols_15m[-1] if len(vols_15m) > 0 else 1000.0
                                pb_vols = vols_5m[-4:] if len(vols_5m) >= 4 else vols_5m
                                max_pb_vol = float(np.max(pb_vols)) if len(pb_vols) > 0 else 0.0
                                avg_pb_vol = float(np.mean(pb_vols)) if len(pb_vols) > 0 else 0.0
                                if breakout_vol > 0 and len(pb_vols) >= 2:
                                    if max_pb_vol > breakout_vol * 1.25 or (avg_pb_vol > breakout_vol * 0.90 and len(pb_vols) >= 4):
                                        is_vol_valid = False

                            if is_vol_valid:
                                is_bullish_candle = (cur_bar["close"] >= cur_bar["open"]) or ((min(cur_bar["open"], cur_bar["close"]) - cur_low) >= (cur_high - cur_low) * 0.35)
                                if is_bullish_candle and cur_delta >= 0:
                                    stop_loss = round(support_floor * 0.9985, 2)
                                    risk_dist = max(cur_price - stop_loss, cur_price * 0.0020)
                                    tp2_price = round(cur_price + risk_dist * 3.0, 2)
                                    used_levels[cur_date][sym].add(level_key)
                                    symbol_last_entry[sym] = cur_dt
                                    symbol_daily_trade_count[cur_date][sym] += 1
                                    buying_power = (capital / max_open_pos) * 5.0
                                    active_trades[sym] = {
                                        "symbol": sym, "side": "BUY", "entry_dt": str(cur_dt),
                                        "entry_price": cur_price, "stop_loss": stop_loss, "tp2_price": tp2_price,
                                        "buying_power": buying_power, "start_capital": capital
                                    }

            # Short
            if is_bearish_1h and len(p_lows) >= 2 and len(active_trades) < max_open_pos and sym not in active_trades:
                ll1, ll2 = p_lows[-2][1], p_lows[-1][1]
                level_key = f"SELL_{round(ll2, 1)}"
                if level_key not in used_levels[cur_date][sym]:
                    if ll2 < ll1 and closes_15[-1] <= ll2 * 0.9998:
                        resistance_ceiling = ll2
                        if cur_high >= resistance_ceiling * 0.9975 and cur_price <= resistance_ceiling * 1.0025:
                            is_vol_valid = True
                            if use_volume_filter:
                                vols_15m = sub_15m["volume"].to_numpy()
                                vols_5m = sub_5m["volume"].to_numpy()
                                breakdown_vol = vols_15m[-1] if len(vols_15m) > 0 else 1000.0
                                pb_vols = vols_5m[-4:] if len(vols_5m) >= 4 else vols_5m
                                max_pb_vol = float(np.max(pb_vols)) if len(pb_vols) > 0 else 0.0
                                avg_pb_vol = float(np.mean(pb_vols)) if len(pb_vols) > 0 else 0.0
                                if breakdown_vol > 0 and len(pb_vols) >= 2:
                                    if max_pb_vol > breakdown_vol * 1.25 or (avg_pb_vol > breakdown_vol * 0.90 and len(pb_vols) >= 4):
                                        is_vol_valid = False

                            if is_vol_valid:
                                is_bearish_candle = (cur_bar["close"] <= cur_bar["open"]) or ((cur_high - max(cur_bar["open"], cur_bar["close"])) >= (cur_high - cur_low) * 0.35)
                                if is_bearish_candle and cur_delta <= 0:
                                    stop_loss = round(resistance_ceiling * 1.0015, 2)
                                    risk_dist = max(stop_loss - cur_price, cur_price * 0.0020)
                                    tp2_price = round(cur_price - risk_dist * 3.0, 2)
                                    used_levels[cur_date][sym].add(level_key)
                                    symbol_last_entry[sym] = cur_dt
                                    symbol_daily_trade_count[cur_date][sym] += 1
                                    buying_power = (capital / max_open_pos) * 5.0
                                    active_trades[sym] = {
                                        "symbol": sym, "side": "SELL", "entry_dt": str(cur_dt),
                                        "entry_price": cur_price, "stop_loss": stop_loss, "tp2_price": tp2_price,
                                        "buying_power": buying_power, "start_capital": capital
                                    }

    tdf = pd.DataFrame(all_trades)
    if tdf.empty:
        return {"trades": 0, "win_rate": 0, "final_capital": capital, "pnl": 0, "pf": 0}
    wins = tdf[tdf["pnl"] > 0]
    losses = tdf[tdf["pnl"] <= 0]
    win_rate = (len(wins) / len(tdf)) * 100.0
    tot_gain = wins["pnl"].sum()
    tot_loss = abs(losses["pnl"].sum())
    pf = (tot_gain / tot_loss) if tot_loss > 0 else 999.0
    return {
        "trades": len(tdf), "win_rate": round(win_rate, 1),
        "final_capital": round(capital, 2), "pnl": round(tdf["pnl"].sum(), 2),
        "roi_pct": round(((capital - 1000.0)/1000.0)*100, 2), "pf": round(pf, 2),
        "df": tdf
    }

def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "/home/opc/data/canonical_candles"
    print("=" * 80)
    print("🔬 RUNNING COMPARATIVE SCIENTIFIC STUDY: WITH VS WITHOUT VOLUME FILTER")
    print("=" * 80)

    # 1. Pure Price Action (NO Volume Filter) - Global Mutex (Max 1 Pos)
    r1 = simulate_strategy(root, use_volume_filter=False, max_open_pos=1)
    print(f"\n1. Pure Price Action (NO Volume Filter, Mutex=1) : {r1['trades']} Trades | WinRate: {r1['win_rate']}% | PF: {r1['pf']} | Final: Rs. {r1['final_capital']} (ROI: +{r1['roi_pct']}%)")

    # 2. With Volume Filter - Global Mutex (Max 1 Pos)
    r2 = simulate_strategy(root, use_volume_filter=True, max_open_pos=1)
    print(f"2. With Volume Filter (Mutex=1)                    : {r2['trades']} Trades | WinRate: {r2['win_rate']}% | PF: {r2['pf']} | Final: Rs. {r2['final_capital']} (ROI: +{r2['roi_pct']}%)")

    # 3. Pure Price Action (NO Volume Filter) - Dual Mutex (Max 2 Pos)
    r3 = simulate_strategy(root, use_volume_filter=False, max_open_pos=2)
    print(f"\n3. Pure Price Action (NO Volume Filter, MaxPos=2): {r3['trades']} Trades | WinRate: {r3['win_rate']}% | PF: {r3['pf']} | Final: Rs. {r3['final_capital']} (ROI: +{r3['roi_pct']}%)")

    # 4. Pure Price Action (NO Volume Filter) - Triple Mutex (Max 3 Pos)
    r4 = simulate_strategy(root, use_volume_filter=False, max_open_pos=3)
    print(f"4. Pure Price Action (NO Volume Filter, MaxPos=3): {r4['trades']} Trades | WinRate: {r4['win_rate']}% | PF: {r4['pf']} | Final: Rs. {r4['final_capital']} (ROI: +{r4['roi_pct']}%)")
    print("=" * 80)

if __name__ == "__main__":
    main()
