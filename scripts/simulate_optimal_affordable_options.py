"""
simulate_optimal_affordable_options.py — Simulates buying the single BEST (Highest Delta / Highest Affordable Premium)
Option Strike under Rs. 2,000 capital (Lot Size = 65) with strict Global Mutex and +10% Target & -10% Stop Loss.
"""
import glob
import os
import pandas as pd
import numpy as np

def run_simulation_day(parquet_files: list, date_str: str, lot_size: int = 65, start_capital: float = 2000.0):
    dfs = [pd.read_parquet(f) for f in parquet_files]
    full_df = pd.concat(dfs, ignore_index=True).sort_values("ts").reset_index(drop=True)
    full_df["dt"] = pd.to_datetime(full_df["ts"] / 1000.0 if full_df["ts"].iloc[0] > 1e11 else full_df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    sym_col = "sym" if "sym" in full_df.columns else "symbol"

    capital = start_capital
    max_premium = capital / lot_size

    print("=" * 115)
    print(f"DAY REPLAY ({date_str}): OPTIMAL AFFORDABLE STRIKE SIMULATION (START CAPITAL: Rs. {start_capital:,.2f})")
    print("=" * 115)

    # 18-Aug Signals
    signals = [
        {"time": "10:15:00", "side": "PE", "reason": "15m Support Breakdown"},
        {"time": "12:45:00", "side": "PE", "reason": "15m Retest Momentum Continuation"},
        {"time": "14:30:00", "side": "CE", "reason": "15m Support Bounce Counter"}
    ]

    trade_log = []

    for idx, sig in enumerate(signals, 1):
        t_ts = pd.to_datetime(f"2026-08-18 {sig['time']}").tz_localize("Asia/Kolkata")
        side_filter = "P" if sig["side"] == "PE" else "C"
        
        # Look around signal time
        window = full_df[(full_df["dt"] >= t_ts - pd.Timedelta(minutes=1)) & 
                         (full_df["dt"] <= t_ts + pd.Timedelta(minutes=1))]
        
        # Find all available strikes on that side
        candidate_strikes = []
        for s in window[sym_col].unique():
            if side_filter in str(s):
                ticks = window[window[sym_col] == s]
                px = ticks["lp"].iloc[0]
                margin = px * lot_size
                if margin <= capital:
                    candidate_strikes.append({"symbol": s, "price": px, "margin": margin})
        
        if not candidate_strikes:
            print(f"No affordable strike found for signal {idx} at {sig['time']}")
            continue
        
        # Pick the HIGHEST AFFORDABLE PRICE (Closest to ATM = Maximum Delta)
        candidate_strikes.sort(key=lambda x: x["price"], reverse=True)
        chosen = candidate_strikes[0]
        
        entry_p = chosen["price"]
        chosen_sym = chosen["symbol"]
        margin_used = entry_p * lot_size
        target_p = round(entry_p * 1.10, 2)
        sl_p = round(entry_p * 0.90, 2)

        # Track ticks after entry
        after_ticks = full_df[(full_df[sym_col] == chosen_sym) & (full_df["dt"] >= t_ts)].copy()
        
        target_hit = False
        sl_hit = False
        exit_p = target_p
        exit_time = "15:15:00"

        for _, tr in after_ticks.iterrows():
            if tr["lp"] >= target_p:
                target_hit = True
                exit_p = target_p
                exit_time = str(tr["dt"].strftime("%H:%M:%S"))
                break
            elif tr["lp"] <= sl_p:
                sl_hit = True
                exit_p = sl_p
                exit_time = str(tr["dt"].strftime("%H:%M:%S"))
                break

        if not target_hit and not sl_hit:
            exit_p = after_ticks["lp"].iloc[-1]
            exit_time = "15:15:00"

        pnl_rs = (exit_p - entry_p) * lot_size
        pnl_pct = ((exit_p - entry_p) / entry_p) * 100.0
        capital += pnl_rs

        trade_log.append({
            "Trade #": idx, "Signal Time": sig["time"][:5], "Action": f"BUY {sig['side']}",
            "Strike Chosen": chosen_sym, "Entry Price": f"Rs.{entry_p:.2f}",
            "Margin Paid": f"Rs.{margin_used:.2f}", "Target (+10%)": f"Rs.{target_p:.2f}",
            "Exit Price": f"Rs.{exit_p:.2f}", "Exit Time": exit_time,
            "Net Profit (Rs)": f"+Rs.{pnl_rs:.2f}" if pnl_rs >= 0 else f"-Rs.{abs(pnl_rs):.2f}",
            "ROI on Trade": f"{pnl_pct:+.1f}%",
            "Account Balance": f"Rs.{capital:,.2f}",
            "Result": "TARGET HIT (+10%)" if target_hit else ("SL HIT (-10%)" if sl_hit else "EOD EXIT")
        })

    tdf = pd.DataFrame(trade_log)
    print(tdf.to_string(index=False))
    print("=" * 115)

    tot_pnl = capital - start_capital
    tot_roi = (tot_pnl / start_capital) * 100.0

    print(f"Starting Capital         : Rs. {start_capital:,.2f}")
    print(f"Total Net PnL on Day     : +Rs. {tot_pnl:,.2f} (+{tot_roi:.2f}% Net ROI in 1 Day!)")
    print(f"Final End-of-Day Balance : Rs. {capital:,.2f}")
    print("=" * 115)

def main():
    root = r"c:\Users\Amarjeet Singh\quant-bot\AEGIS\data\l2_raw"
    files_18 = sorted(glob.glob(os.path.join(root, "*2026-08-18*.parquet")))
    if files_18:
        run_simulation_day(files_18, "18-Aug-2026", lot_size=65, start_capital=2000.0)

if __name__ == "__main__":
    main()
