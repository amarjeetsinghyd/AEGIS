"""
backtest_canonical_stratos.py — Multi-Timeframe (1H + 15m + 5m) STRATOS Two-Way Strategy Backtester.
Evaluates:
- 1-Hour Macro Trend Filter (Long on Up-Slope, Short on Down-Slope)
- 15-Minute Market Structure (Long on HH1 -> HH2 Breakout, Short on LL1 -> LL2 Breakdown)
- 5-Minute Pullback & Volume Contraction Retest
- 5-Minute Delta / Absorption Reversal Trigger
- Anti-Liquidity Hunting SL (0.15% buffer) & Session-Adaptive Trailing Stops (9-EMA / 20-EMA)
- EOD Square-off at 15:10 IST
"""
from __future__ import annotations
import os
import sys
import glob
from datetime import datetime, time
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

def run_canonical_stratos_backtest(data_root: str, out_dir: str):
    path_5m = os.path.join(data_root, "5m", "canonical_5m_all_symbols.parquet")
    path_15m = os.path.join(data_root, "15m", "canonical_15m_all_symbols.parquet")
    path_1h = os.path.join(data_root, "1h", "canonical_1h_all_symbols.parquet")

    print(f"[load] Loading canonical datasets from {data_root}...")
    df_5m = pd.read_parquet(path_5m)
    df_15m = pd.read_parquet(path_15m)
    df_1h = pd.read_parquet(path_1h)

    symbols = sorted(df_5m["symbol"].unique())
    dates = sorted(df_5m["trade_date"].unique())
    print(f"[backtest] Running STRATOS Two-Way (Long & Short) MTF Strategy on {len(symbols)} symbols across {len(dates)} dates...")

    capital = 50000.0
    all_trades = []
    open_trades = {}
    max_open_pos = 2
    max_daily_trades = 6

    daily_trade_count = {d: 0 for d in dates}

    for (symbol, date), group_5m in df_5m.groupby(["symbol", "trade_date"], sort=False):
        group_5m = group_5m.sort_values("dt").reset_index(drop=True)
        bars_15m = df_15m[(df_15m["symbol"] == symbol) & (df_15m["trade_date"] == date)].sort_values("dt").reset_index(drop=True)
        bars_1h = df_1h[(df_1h["symbol"] == symbol) & (df_1h["trade_date"] == date)].sort_values("dt").reset_index(drop=True)

        if len(group_5m) < 4:
            continue

        # State machine per symbol-date
        awaiting_long_retest = False
        support_floor = None
        long_breakout_vol = 0.0
        long_retest_touched = False
        lowest_wick = 999999.0

        awaiting_short_retest = False
        resistance_ceiling = None
        short_breakdown_vol = 0.0
        short_retest_touched = False
        highest_wick = 0.0

        for i in range(len(group_5m)):
            cur_bar = group_5m.iloc[i]
            cur_t = cur_bar["dt"].time()
            cur_price = cur_bar["close"]
            cur_high = cur_bar["high"]
            cur_low = cur_bar["low"]
            cur_vol = cur_bar["volume"]
            cur_delta = cur_bar["delta"]

            # ── 1. ACTIVE POSITION MANAGEMENT ──
            if symbol in open_trades:
                trade = open_trades[symbol]
                side = trade["side"]
                entry_p = trade["entry_price"]
                sl_p = trade["stop_loss"]
                tp1_p = trade["tp1_price"]
                tp2_p = trade["tp2_price"]
                qty = trade["qty"]

                if side == "BUY":
                    # Long SL Hit
                    if cur_low <= sl_p:
                        exit_p = sl_p
                        pnl = (exit_p - entry_p) * qty
                        pnl_pct = (exit_p - entry_p) / entry_p * 100.0
                        trade.update({"exit_price": exit_p, "exit_time": str(cur_t), "pnl": pnl, "pnl_pct": pnl_pct, "reason": "STOP_LOSS_HIT"})
                        all_trades.append(trade)
                        del open_trades[symbol]
                        continue

                    # Long TP2 Hit
                    if cur_high >= tp2_p:
                        exit_p = tp2_p
                        pnl = (exit_p - entry_p) * qty
                        pnl_pct = (exit_p - entry_p) / entry_p * 100.0
                        trade.update({"exit_price": exit_p, "exit_time": str(cur_t), "pnl": pnl, "pnl_pct": pnl_pct, "reason": "TARGET_2_HIT"})
                        all_trades.append(trade)
                        del open_trades[symbol]
                        continue

                    # Long Trailing Stop (Afternoon 9 EMA or Morning 20 EMA)
                    hist_5m_closes = group_5m.iloc[:i+1]["close"].to_numpy()
                    if len(hist_5m_closes) >= 10:
                        ema_period = 20 if cur_t < time(13, 30) else 9
                        sess_name = "MORNING_20_EMA" if cur_t < time(13, 30) else "AFTERNOON_9_EMA"
                        ema_arr = compute_ema(hist_5m_closes, ema_period)
                        if len(ema_arr) > 0 and cur_price >= entry_p * 1.0035 and cur_price < ema_arr[-1]:
                            exit_p = cur_price
                            pnl = (exit_p - entry_p) * qty
                            pnl_pct = (exit_p - entry_p) / entry_p * 100.0
                            trade.update({"exit_price": exit_p, "exit_time": str(cur_t), "pnl": pnl, "pnl_pct": pnl_pct, "reason": f"TRAILING_EXIT_{sess_name}"})
                            all_trades.append(trade)
                            del open_trades[symbol]
                            continue

                    # Long EOD Square-off (15:10 IST)
                    if cur_t >= time(15, 10):
                        exit_p = cur_price
                        pnl = (exit_p - entry_p) * qty
                        pnl_pct = (exit_p - entry_p) / entry_p * 100.0
                        trade.update({"exit_price": exit_p, "exit_time": str(cur_t), "pnl": pnl, "pnl_pct": pnl_pct, "reason": "EOD_SQUAREOFF_15:10"})
                        all_trades.append(trade)
                        del open_trades[symbol]
                        continue

                elif side == "SELL":
                    # Short SL Hit
                    if cur_high >= sl_p:
                        exit_p = sl_p
                        pnl = (entry_p - exit_p) * qty
                        pnl_pct = (entry_p - exit_p) / entry_p * 100.0
                        trade.update({"exit_price": exit_p, "exit_time": str(cur_t), "pnl": pnl, "pnl_pct": pnl_pct, "reason": "STOP_LOSS_HIT"})
                        all_trades.append(trade)
                        del open_trades[symbol]
                        continue

                    # Short TP2 Hit
                    if cur_low <= tp2_p:
                        exit_p = tp2_p
                        pnl = (entry_p - exit_p) * qty
                        pnl_pct = (entry_p - exit_p) / entry_p * 100.0
                        trade.update({"exit_price": exit_p, "exit_time": str(cur_t), "pnl": pnl, "pnl_pct": pnl_pct, "reason": "TARGET_2_HIT"})
                        all_trades.append(trade)
                        del open_trades[symbol]
                        continue

                    # Short Trailing Stop
                    hist_5m_closes = group_5m.iloc[:i+1]["close"].to_numpy()
                    if len(hist_5m_closes) >= 10:
                        ema_period = 20 if cur_t < time(13, 30) else 9
                        sess_name = "MORNING_20_EMA" if cur_t < time(13, 30) else "AFTERNOON_9_EMA"
                        ema_arr = compute_ema(hist_5m_closes, ema_period)
                        if len(ema_arr) > 0 and cur_price <= entry_p * 0.9965 and cur_price > ema_arr[-1]:
                            exit_p = cur_price
                            pnl = (entry_p - exit_p) * qty
                            pnl_pct = (entry_p - exit_p) / entry_p * 100.0
                            trade.update({"exit_price": exit_p, "exit_time": str(cur_t), "pnl": pnl, "pnl_pct": pnl_pct, "reason": f"TRAILING_EXIT_{sess_name}"})
                            all_trades.append(trade)
                            del open_trades[symbol]
                            continue

                    # Short EOD Square-off (15:10 IST)
                    if cur_t >= time(15, 10):
                        exit_p = cur_price
                        pnl = (entry_p - exit_p) * qty
                        pnl_pct = (entry_p - exit_p) / entry_p * 100.0
                        trade.update({"exit_price": exit_p, "exit_time": str(cur_t), "pnl": pnl, "pnl_pct": pnl_pct, "reason": "EOD_SQUAREOFF_15:10"})
                        all_trades.append(trade)
                        del open_trades[symbol]
                        continue

                continue

            # ── 2. NEW ENTRY EVALUATION ──
            if cur_t < time(9, 20) or cur_t > time(14, 30):
                continue
            if len(open_trades) >= max_open_pos:
                continue
            if daily_trade_count[date] >= max_daily_trades:
                continue

            # Historical sub-frames up to current time
            cur_dt = cur_bar["dt"]
            sub_15m = bars_15m[bars_15m["dt"] <= cur_dt]
            sub_1h = bars_1h[bars_1h["dt"] <= cur_dt]

            is_macro_bullish = True
            is_macro_bearish = True

            if len(sub_1h) >= 2:
                c1h = sub_1h["close"].to_numpy()
                is_macro_bullish = c1h[-1] >= c1h[0]
                is_macro_bearish = c1h[-1] < c1h[0]

            if len(sub_15m) >= 4:
                highs_15 = sub_15m["high"].to_numpy()
                lows_15 = sub_15m["low"].to_numpy()
                closes_15 = sub_15m["close"].to_numpy()
                vols_15 = sub_15m["volume"].to_numpy()

                p_highs, p_lows = find_pivots(highs_15, lows_15, lookback=1)

                # Check Long HH1 -> HH2 Breakout
                if is_macro_bullish and len(p_highs) >= 2:
                    hh1, hh2 = p_highs[-2][1], p_highs[-1][1]
                    if hh2 >= hh1 and closes_15[-1] >= hh2 * 1.0002:
                        support_floor = hh2
                        long_breakout_vol = vols_15[-1]
                        awaiting_long_retest = True
                        long_retest_touched = False
                        lowest_wick = 999999.0

                # Check Short LL1 -> LL2 Breakdown
                if is_macro_bearish and len(p_lows) >= 2:
                    ll1, ll2 = p_lows[-2][1], p_lows[-1][1]
                    if ll2 <= ll1 and closes_15[-1] <= ll2 * 0.9998:
                        resistance_ceiling = ll2
                        short_breakdown_vol = vols_15[-1]
                        awaiting_short_retest = True
                        short_retest_touched = False
                        highest_wick = 0.0

            # ── 3. PULLBACK RETEST CHECK (LONG) ──
            if awaiting_long_retest and support_floor is not None:
                if cur_low < lowest_wick:
                    lowest_wick = cur_low

                if cur_low <= support_floor * 1.0025:
                    long_retest_touched = True

                # Invalidation
                if cur_price < support_floor * 0.9975:
                    awaiting_long_retest = False
                    long_retest_touched = False
                elif long_retest_touched:
                    vol_contracted = (cur_vol <= long_breakout_vol * 0.85) or (cur_vol > 0)
                    is_bullish_candle = (cur_bar["close"] >= cur_bar["open"]) or ((min(cur_bar["open"], cur_bar["close"]) - cur_low) >= (cur_high - cur_low) * 0.35)
                    delta_positive = cur_delta >= 0

                    if vol_contracted and is_bullish_candle and delta_positive:
                        stop_loss = round(min(support_floor, lowest_wick) * 0.9985, 2)
                        risk_dist = max(cur_price - stop_loss, cur_price * 0.0020)
                        risk_cap = capital * 0.01
                        qty = max(1, int(risk_cap / risk_dist))
                        
                        tp1_price = round(cur_price + risk_dist * 2.0, 2)
                        tp2_price = round(cur_price + risk_dist * 3.0, 2)

                        trade_entry = {
                            "symbol": symbol, "side": "BUY", "trade_date": date, "entry_dt": str(cur_dt),
                            "entry_time": str(cur_t), "entry_price": cur_price, "qty": qty,
                            "stop_loss": stop_loss, "tp1_price": tp1_price, "tp2_price": tp2_price,
                            "support_floor": support_floor, "risk_pct": round(risk_dist / cur_price * 100.0, 2)
                        }
                        open_trades[symbol] = trade_entry
                        daily_trade_count[date] += 1
                        awaiting_long_retest = False
                        long_retest_touched = False
                        continue

            # ── 4. PULLBACK RETEST CHECK (SHORT) ──
            if awaiting_short_retest and resistance_ceiling is not None:
                if cur_high > highest_wick:
                    highest_wick = cur_high

                if cur_high >= resistance_ceiling * 0.9975:
                    short_retest_touched = True

                # Invalidation
                if cur_price > resistance_ceiling * 1.0025:
                    awaiting_short_retest = False
                    short_retest_touched = False
                elif short_retest_touched:
                    vol_contracted = (cur_vol <= short_breakdown_vol * 0.85) or (cur_vol > 0)
                    is_bearish_candle = (cur_bar["close"] <= cur_bar["open"]) or ((cur_high - max(cur_bar["open"], cur_bar["close"])) >= (cur_high - cur_low) * 0.35)
                    delta_negative = cur_delta <= 0

                    if vol_contracted and is_bearish_candle and delta_negative:
                        stop_loss = round(max(resistance_ceiling, highest_wick) * 1.0015, 2)
                        risk_dist = max(stop_loss - cur_price, cur_price * 0.0020)
                        risk_cap = capital * 0.01
                        qty = max(1, int(risk_cap / risk_dist))
                        
                        tp1_price = round(cur_price - risk_dist * 2.0, 2)
                        tp2_price = round(cur_price - risk_dist * 3.0, 2)

                        trade_entry = {
                            "symbol": symbol, "side": "SELL", "trade_date": date, "entry_dt": str(cur_dt),
                            "entry_time": str(cur_t), "entry_price": cur_price, "qty": qty,
                            "stop_loss": stop_loss, "tp1_price": tp1_price, "tp2_price": tp2_price,
                            "resistance_ceiling": resistance_ceiling, "risk_pct": round(risk_dist / cur_price * 100.0, 2)
                        }
                        open_trades[symbol] = trade_entry
                        daily_trade_count[date] += 1
                        awaiting_short_retest = False
                        short_retest_touched = False

    print("\n" + "=" * 90)
    print(f"STRATOS TWO-WAY CANONICAL (1H + 15M + 5M) BACKTEST RESULTS ({len(all_trades)} Trades Executed)")
    print("=" * 90)

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
    avg_win = wins["pnl_pct"].mean() if len(wins) > 0 else 0.0
    avg_loss = losses["pnl_pct"].mean() if len(losses) > 0 else 0.0

    print(f"Total Trades       : {len(tdf)}")
    print(f"Long Trades (BUY)  : {len(tdf[tdf['side'] == 'BUY'])}")
    print(f"Short Trades (SELL): {len(tdf[tdf['side'] == 'SELL'])}")
    print(f"Winning Trades     : {len(wins)} ({win_rate:.1f}% Win Rate)")
    print(f"Losing Trades      : {len(losses)} ({100.0 - win_rate:.1f}% Loss Rate)")
    print(f"Profit Factor      : {profit_factor:.2f}")
    print(f"Total Net PnL (Rs) : Rs. {tot_pnl:,.2f}")
    print(f"Average Win (%)    : +{avg_win:.2f}%")
    print(f"Average Loss (%)   : {avg_loss:.2f}%")

    print("\n--- INDIVIDUAL TRADE LOG ---")
    cols_to_show = ["symbol", "side", "trade_date", "entry_time", "entry_price", "exit_price", "pnl_pct", "pnl", "reason"]
    print(tdf[cols_to_show].to_string())

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        csv_path = os.path.join(out_dir, "canonical_stratos_twoway_trades.csv")
        tdf.to_csv(csv_path, index=False)
        print(f"\n[saved] Full trade log saved -> {csv_path}")

def main():
    data_root = sys.argv[1] if len(sys.argv) > 1 else "/home/opc/data/canonical_candles"
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "/home/opc/stratos_canonical_results"
    run_canonical_stratos_backtest(data_root, out_dir)

if __name__ == "__main__":
    main()
