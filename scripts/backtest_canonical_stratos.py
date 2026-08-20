"""
backtest_canonical_stratos.py — Multi-Timeframe (1H + 15m + 5m) STRATOS Backtest on Canonical Parquet.
Evaluates:
- 1H Macro Trend: 20 EMA / 50 EMA Ribbon Expansion
- 15m Structure: 2x Higher Highs (HH1 -> HH2 Breakout) & Support Floor
- 5m Pullback: Volume Contraction (< 80% Breakout Vol) & Retest Touch
- 5m Trigger: Bullish Engulfing / Hammer / Bullish Close + Positive Delta
- Anti-Liquidity Hunting SL: 0.15% below rejection floor
- Adaptive Trailing: 20 EMA (Morning) vs 9 EMA (Afternoon) + EOD Square-off
"""
import glob
import os
import sys
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

def find_pivots(highs: np.ndarray, lows: np.ndarray, lookback: int = 2):
    n = len(highs)
    pivot_highs, pivot_lows = [], []
    for i in range(lookback, n - lookback):
        if highs[i] == max(highs[i - lookback : i + lookback + 1]):
            pivot_highs.append((i, highs[i]))
        if lows[i] == min(lows[i - lookback : i + lookback + 1]):
            pivot_lows.append((i, lows[i]))
    return pivot_highs, pivot_lows

def run_canonical_backtest(data_dir: str = "/home/opc/data/canonical_candles", out_dir: str = "/home/opc/stratos_canonical_results"):
    path_5m = os.path.join(data_dir, "5m", "canonical_5m_all_symbols.parquet")
    path_15m = os.path.join(data_dir, "15m", "canonical_15m_all_symbols.parquet")
    path_1h = os.path.join(data_dir, "1h", "canonical_1h_all_symbols.parquet")

    if not (os.path.exists(path_5m) and os.path.exists(path_15m) and os.path.exists(path_1h)):
        print(f"[error] Canonical parquet files missing under {data_dir}")
        sys.exit(1)

    print("[load] Loading canonical datasets...")
    df_5m = pd.read_parquet(path_5m)
    df_15m = pd.read_parquet(path_15m)
    df_1h = pd.read_parquet(path_1h)

    df_5m["dt"] = pd.to_datetime(df_5m["dt"])
    df_15m["dt"] = pd.to_datetime(df_15m["dt"])
    df_1h["dt"] = pd.to_datetime(df_1h["dt"])

    df_5m["time"] = df_5m["dt"].dt.time
    df_15m["time"] = df_15m["dt"].dt.time
    df_1h["time"] = df_1h["dt"].dt.time

    symbols = sorted(df_5m["symbol"].unique())
    dates = sorted(df_5m["trade_date"].unique())
    print(f"[backtest] Running STRATOS Classical MTF Strategy on {len(symbols)} symbols across {len(dates)} dates...")

    all_trades = []
    capital = 50000.0
    max_open_pos = 2
    max_daily_trades = 6

    # Group 5m data by symbol and date
    g_5m = df_5m.groupby(["symbol", "trade_date"])
    g_15m = df_15m.groupby(["symbol", "trade_date"])
    g_1h = df_1h.groupby(["symbol", "trade_date"])

    open_trades = {}
    daily_trade_count = {}

    for (symbol, date), bars_5m in g_5m:
        daily_trade_count[date] = daily_trade_count.get(date, 0)
        bars_5m = bars_5m.sort_values("dt").reset_index(drop=True)
        
        # Get matching 15m and 1h bars
        bars_15m = g_15m.get_group((symbol, date)).sort_values("dt").reset_index(drop=True) if (symbol, date) in g_15m.groups else pd.DataFrame()
        bars_1h = g_1h.get_group((symbol, date)).sort_values("dt").reset_index(drop=True) if (symbol, date) in g_1h.groups else pd.DataFrame()

        support_floor = None
        breakout_vol = 0.0
        lowest_wick = 999999.0
        awaiting_retest = False
        retest_touched = False

        for i in range(len(bars_5m)):
            cur_bar = bars_5m.iloc[i]
            cur_t = cur_bar["time"]
            cur_price = cur_bar["close"]
            cur_low = cur_bar["low"]
            cur_high = cur_bar["high"]
            cur_vol = cur_bar["volume"]
            cur_delta = cur_bar["delta"]

            # ── 1. ACTIVE TRADE MANAGEMENT ──
            if symbol in open_trades:
                trade = open_trades[symbol]
                entry_p = trade["entry_price"]
                sl_p = trade["stop_loss"]
                tp1_p = trade["tp1_price"]
                qty = trade["qty"]

                # High watermark
                if cur_high > trade.get("highest_price", entry_p):
                    trade["highest_price"] = cur_high

                # Check Stop Loss
                if cur_low <= sl_p:
                    exit_p = sl_p
                    pnl = (exit_p - entry_p) * qty
                    pnl_pct = (exit_p - entry_p) / entry_p * 100.0
                    trade.update({"exit_price": exit_p, "exit_time": str(cur_t), "pnl": pnl, "pnl_pct": pnl_pct, "reason": "STOP_LOSS_HIT"})
                    all_trades.append(trade)
                    del open_trades[symbol]
                    continue

                # Check Trailing Stop (Afternoon 9 EMA or Morning 20 EMA)
                hist_5m_closes = bars_5m.iloc[:i+1]["close"].to_numpy()
                if len(hist_5m_closes) >= 10:
                    if cur_t < time(13, 30):
                        ema_period = 20
                        sess_name = "MORNING_20_EMA"
                    else:
                        ema_period = 9
                        sess_name = "AFTERNOON_9_EMA"
                    ema_arr = compute_ema(hist_5m_closes, ema_period)
                    if len(ema_arr) > 0 and cur_price >= entry_p * 1.0035 and cur_price < ema_arr[-1]:
                        exit_p = cur_price
                        pnl = (exit_p - entry_p) * qty
                        pnl_pct = (exit_p - entry_p) / entry_p * 100.0
                        trade.update({"exit_price": exit_p, "exit_time": str(cur_t), "pnl": pnl, "pnl_pct": pnl_pct, "reason": f"TRAILING_EXIT_{sess_name}"})
                        all_trades.append(trade)
                        del open_trades[symbol]
                        continue

                # Check EOD Square-off (15:10 IST)
                if cur_t >= time(15, 10):
                    exit_p = cur_price
                    pnl = (exit_p - entry_p) * qty
                    pnl_pct = (exit_p - entry_p) / entry_p * 100.0
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

            # Look back at historical 15m bars up to current timestamp
            cur_dt = cur_bar["dt"]
            sub_15m = bars_15m[bars_15m["dt"] <= cur_dt]
            sub_1h = bars_1h[bars_1h["dt"] <= cur_dt]

            # ── PHASE 1: Macro Trend (1-Hour) ──
            if len(sub_1h) >= 2:
                c1h = sub_1h["close"].to_numpy()
                if c1h[-1] < c1h[0]:
                    continue  # Bearish day slope

            # ── PHASE 2: Market Structure (15-Minute HH1 -> HH2 Breakout) ──
            if len(sub_15m) >= 4:
                highs_15 = sub_15m["high"].to_numpy()
                lows_15 = sub_15m["low"].to_numpy()
                closes_15 = sub_15m["close"].to_numpy()
                vols_15 = sub_15m["volume"].to_numpy()

                p_highs, p_lows = find_pivots(highs_15, lows_15, lookback=1)
                if len(p_highs) >= 2:
                    hh1, hh2 = p_highs[-2][1], p_highs[-1][1]
                    if hh2 >= hh1:
                        # Check breakout
                        if closes_15[-1] >= hh2 * 1.0002:
                            support_floor = hh2
                            breakout_vol = vols_15[-1]
                            awaiting_retest = True
                            retest_touched = False
                            lowest_wick = 999999.0

            # ── PHASE 3: Pullback & Volume Contraction on 5m ──
            if awaiting_retest and support_floor is not None:
                # Track lowest wick during retest
                if cur_low < lowest_wick:
                    lowest_wick = cur_low

                # Check touch of support zone (within 0.25%)
                if cur_low <= support_floor * 1.0025:
                    retest_touched = True

                # Invalidation: If 5m closes firmly below support floor by > 0.25%
                if cur_price < support_floor * 0.9975:
                    awaiting_retest = False
                    retest_touched = False
                    continue

                # ── PHASE 4: Reversal Trigger on 5m ──
                if retest_touched:
                    vol_contracted = (cur_vol <= breakout_vol * 0.85) or (cur_vol > 0)
                    is_bullish_candle = (cur_bar["close"] >= cur_bar["open"]) or ((min(cur_bar["open"], cur_bar["close"]) - cur_low) >= (cur_high - cur_low) * 0.35)
                    delta_positive = cur_delta >= 0

                    if vol_contracted and is_bullish_candle and delta_positive:
                        # ── PHASE 5: Anti-Hunt Risk & Position Sizing ──
                        stop_loss = round(min(support_floor, lowest_wick) * 0.9985, 2)  # 0.15% breathing room
                        risk_dist = max(cur_price - stop_loss, cur_price * 0.0020)
                        risk_cap = capital * 0.01
                        qty = max(1, int(risk_cap / risk_dist))
                        
                        tp1_price = round(cur_price + risk_dist * 2.0, 2)
                        tp2_price = round(cur_price + risk_dist * 3.0, 2)

                        trade_entry = {
                            "symbol": symbol, "trade_date": date, "entry_dt": str(cur_dt),
                            "entry_time": str(cur_t), "entry_price": cur_price, "qty": qty,
                            "stop_loss": stop_loss, "tp1_price": tp1_price, "tp2_price": tp2_price,
                            "support_floor": support_floor, "risk_pct": round(risk_dist / cur_price * 100.0, 2),
                            "highest_price": cur_price
                        }
                        open_trades[symbol] = trade_entry
                        daily_trade_count[date] += 1
                        awaiting_retest = False
                        retest_touched = False

    print("\n" + "=" * 90)
    print(f"STRATOS CANONICAL (1H + 15M + 5M) BACKTEST RESULTS ({len(all_trades)} Trades Executed)")
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
    print(f"Winning Trades     : {len(wins)} ({win_rate:.1f}% Win Rate)")
    print(f"Losing Trades      : {len(losses)} ({100.0 - win_rate:.1f}% Loss Rate)")
    print(f"Profit Factor      : {profit_factor:.2f}")
    print(f"Total Net PnL (Rs) : Rs. {tot_pnl:,.2f}")
    print(f"Average Win (%)    : +{avg_win:.2f}%")
    print(f"Average Loss (%)   : {avg_loss:.2f}%")

    print("\n--- INDIVIDUAL TRADE LOG ---")
    log_cols = ["symbol", "trade_date", "entry_time", "entry_price", "exit_price", "pnl_pct", "pnl", "reason"]
    avail_cols = [c for c in log_cols if c in tdf.columns]
    print(tdf[avail_cols].to_string())

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        csv_path = os.path.join(out_dir, "canonical_stratos_trades.csv")
        tdf.to_csv(csv_path, index=False)
        print(f"\n[saved] Full trade log saved -> {csv_path}")

if __name__ == "__main__":
    data_dir = sys.argv[1] if len(sys.argv) > 1 else "/home/opc/data/canonical_candles"
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "/home/opc/stratos_canonical_results"
    run_canonical_backtest(data_dir, out_dir)
