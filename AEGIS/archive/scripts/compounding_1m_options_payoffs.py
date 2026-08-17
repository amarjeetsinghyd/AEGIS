import os
import sys
import re
import pandas as pd
import numpy as np
from datetime import datetime

# Ensure project root is in sys.path
BASE_DIR = r"C:\Kronos_Testing"
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from astra_bot.core.angel_one_adapter import AngelOneAdapter
from astra_bot.core.data_fetcher import DataFetcher

def run_compounding_backtest():
    log_file = r"C:\Kronos_Testing\astra_bot\logs\astra_bot.log"
    if not os.path.exists(log_file):
        print(f"Error: Log file not found at {log_file}")
        return
        
    print("Parsing astra_bot.log to find momentum signals...")
    momentum_signals = []
    
    # Regex to parse momentum log lines
    pattern = re.compile(
        r"\[(2026-07-\d{2} \d{2}:\d{2}:\d{2}\+05:30)\] Close: ([\d.]+) \| Pred Close: ([\d.]+) \(([-+\d.]+)%\) \[MOMENTUM\]"
    )
    
    with open(log_file, 'r', encoding='utf-8') as f:
        for line in f:
            match = pattern.search(line)
            if match:
                ts, close, pred, change_pct = match.groups()
                momentum_signals.append({
                    "timestamp": pd.to_datetime(ts),
                    "close": float(close),
                    "pred": float(pred),
                    "change_pct": float(change_pct) / 100.0
                })
                
    if not momentum_signals:
        print("No momentum signals parsed from logs.")
        return
        
    print(f"Parsed {len(momentum_signals)} momentum signals.")
    
    csv_path = r"C:\Kronos_Testing\astra_bot\data\scratch\nifty_1m_data.csv"
    
    if os.path.exists(csv_path):
        print(f"Loading 1-minute Nifty Spot candles from local cache: {csv_path}")
        df_1m = pd.read_csv(csv_path)
    else:
        print("\nLocal cache not found. Connecting to AngelOne to download 1-minute Nifty Spot candles...")
        broker = AngelOneAdapter()
        fetcher = DataFetcher(broker)
        token, name, exchange = fetcher.get_cash_index_token()
        df_1m = fetcher.get_historical_candles(exchange, token, "ONE_MINUTE", days_back=13)
        if df_1m.empty:
            print("Error: Could not retrieve 1-minute index candles.")
            return
        # Save to cache
        os.makedirs(os.path.dirname(csv_path), exist_ok=True)
        df_1m.to_csv(csv_path, index=False)
        print(f"Saved 1-minute candles to local cache: {csv_path}")
        
    df_1m = df_1m.sort_values('timestamp').reset_index(drop=True)
    df_1m['timestamp'] = pd.to_datetime(df_1m['timestamp'])
    
    # Map timestamps to index for fast lookup
    ts_to_idx_1m = {ts: idx for idx, ts in enumerate(df_1m['timestamp'])}
    
    # Initial Simulation Parameters
    capital = 2500.0  # Initial Paper Capital
    P0 = 35.0         # ATM Option Premium base
    delta_atm = 0.5
    gamma_atm = 0.002
    lot_size = 65
    round_trip_charge = 17.0
    
    trades = []
    
    print("Running high-fidelity compounding simulation...")
    for sig in momentum_signals:
        ts = sig["timestamp"]
        entry_index = sig["close"]
        sig_type = "CALL" if sig["change_pct"] > 0 else "PUT"
        
        # Check if we have enough capital to trade even the cheapest option
        # OTM strikes decay by 0.8 per step. Max search depth is 20.
        # Cost of option at OTM index k is P0 * (0.8^k) * lot_size
        cheapest_otm_cost = P0 * (0.8 ** 19) * lot_size # k=19
        if capital < cheapest_otm_cost:
            print(f"[{ts}] Bot is Bankrupt! Capital remaining: Rs {capital:.2f} (Cheapest option costs Rs {cheapest_otm_cost:.2f})")
            break
            
        # Step 1: Strike Selection & Lot Allocation using StrikeSelector logic
        # Cost for strike k
        strike_costs = [P0 * (0.8 ** k) * lot_size for k in range(20)]
        
        # Find first affordable OTM strike
        start_idx = -1
        for k in range(20):
            if strike_costs[k] <= capital:
                start_idx = k
                break
                
        if start_idx == -1:
            # Cannot afford any strike
            continue
            
        # Allocations using Forward Ladder & Reverse Sweep
        allocations = [0] * 20
        remaining_funds = capital
        
        # Forward Ladder
        ladder_end = min(start_idx + 5, 20)
        for i in range(start_idx, ladder_end):
            cost_per_lot = strike_costs[i]
            if remaining_funds >= cost_per_lot:
                allocations[i] += 1
                remaining_funds -= cost_per_lot
            else:
                break
                
        # Reverse Sweep (Optimized via division to support large compounded capitals)
        active_allocations = [idx for idx, qty in enumerate(allocations) if qty > 0]
        if active_allocations:
            max_idx = max(active_allocations)
            total_sweep_cost = sum(strike_costs[i] for i in range(start_idx, max_idx + 1))
            
            # Allocate maximum possible full rounds
            if total_sweep_cost > 0:
                num_rounds = int(remaining_funds // total_sweep_cost)
                if num_rounds > 0:
                    for i in range(start_idx, max_idx + 1):
                        allocations[i] += num_rounds
                    remaining_funds -= num_rounds * total_sweep_cost
            
            # Final fractional sweep pass
            for i in range(max_idx, start_idx - 1, -1):
                cost_per_lot = strike_costs[i]
                if remaining_funds >= cost_per_lot:
                    allocations[i] += 1
                    remaining_funds -= cost_per_lot

                    
        # Total entry value of the position
        pos_entry_val = sum(qty * strike_costs[idx] for idx, qty in enumerate(allocations))
        if pos_entry_val == 0:
            continue
            
        # Step 2: Simulate next 5 minutes of 1-minute candles
        if ts not in ts_to_idx_1m:
            continue
        start_idx_1m = ts_to_idx_1m[ts]
        
        candles_5m = []
        for minute in range(1, 6):
            target_idx = start_idx_1m + minute
            if target_idx < len(df_1m):
                candles_5m.append(df_1m.iloc[target_idx])
                
        if len(candles_5m) < 5:
            continue
            
        # Option price curves for each allocated strike
        # Strike delta & gamma scale down for OTM
        opt_curves = []
        for idx, qty in enumerate(allocations):
            if qty > 0:
                opt_curves.append({
                    "idx": idx,
                    "qty": qty,
                    "entry_prem": P0 * (0.8 ** idx),
                    "delta": delta_atm * (0.8 ** idx),
                    "gamma": gamma_atm * (0.8 ** idx),
                })
                
        # Simulate step-by-step
        hwm_pct = 0.0
        sl_pct = -0.20 # Initial -20% stop loss
        
        exit_price_val = None
        exit_reason = None
        
        for m_idx, m_candle in enumerate(candles_5m):
            m_high = m_candle['high']
            m_low = m_candle['low']
            m_close = m_candle['close']
            
            # Calculate position values at High, Low, and Close of this 1-minute candle
            val_high = 0.0
            val_low = 0.0
            val_close = 0.0
            
            for curve in opt_curves:
                # CALL trade
                if sig_type == "CALL":
                    # High Spot Nifty -> Option High
                    dS_high = m_high - entry_index
                    opt_high = curve["entry_prem"] + curve["delta"] * dS_high + 0.5 * curve["gamma"] * (dS_high ** 2)
                    opt_high = max(0.5, opt_high)
                    
                    # Low Spot Nifty -> Option Low
                    dS_low = m_low - entry_index
                    opt_low = curve["entry_prem"] + curve["delta"] * dS_low + 0.5 * curve["gamma"] * (dS_low ** 2)
                    opt_low = max(0.5, opt_low)
                    
                    # Close Spot Nifty -> Option Close
                    dS_close = m_close - entry_index
                    opt_close = curve["entry_prem"] + curve["delta"] * dS_close + 0.5 * curve["gamma"] * (dS_close ** 2)
                    opt_close = max(0.5, opt_close)
                # PUT trade
                else:
                    # High Spot Nifty -> Option Low (adverse)
                    dS_high = m_high - entry_index
                    opt_high = curve["entry_prem"] - curve["delta"] * dS_high + 0.5 * curve["gamma"] * (dS_high ** 2)
                    opt_high = max(0.5, opt_high)
                    
                    # Low Spot Nifty -> Option High (favorable)
                    dS_low = m_low - entry_index
                    opt_low = curve["entry_prem"] - curve["delta"] * dS_low + 0.5 * curve["gamma"] * (dS_low ** 2)
                    opt_low = max(0.5, opt_low)
                    
                    # Close Spot Nifty -> Option Close
                    dS_close = m_close - entry_index
                    opt_close = curve["entry_prem"] - curve["delta"] * dS_close + 0.5 * curve["gamma"] * (dS_close ** 2)
                    opt_close = max(0.5, opt_close)
                    
                val_high += curve["qty"] * opt_high * lot_size
                val_low += curve["qty"] * opt_low * lot_size
                val_close += curve["qty"] * opt_close * lot_size
                
            # Current PnL % at minute high/low/close
            # Highest valuation achieved is val_high for CALL and val_low for PUT
            max_val = val_high if sig_type == "CALL" else val_low
            min_val = val_low if sig_type == "CALL" else val_high
            
            pnl_high_pct = (max_val - pos_entry_val) / pos_entry_val
            pnl_low_pct = (min_val - pos_entry_val) / pos_entry_val
            pnl_close_pct = (val_close - pos_entry_val) / pos_entry_val
            
            # Update HWM
            if pnl_high_pct > hwm_pct:
                hwm_pct = pnl_high_pct
                
            # Update Trailing Stop Loss levels
            if hwm_pct >= 0.05:
                # Fast Breakeven
                sl_pct = max(sl_pct, 0.0)
            if hwm_pct >= 0.10:
                # Lock +5%
                sl_pct = max(sl_pct, 0.05)
            if hwm_pct >= 0.20:
                # Lock +12%
                sl_pct = max(sl_pct, 0.12)
            if hwm_pct >= 0.30:
                # Lock +20%
                sl_pct = max(sl_pct, 0.20)
                
            # HWM Trailing (> 15% profit)
            if hwm_pct >= 0.15:
                if hwm_pct > 0.50:
                    sl_pct = max(sl_pct, hwm_pct - 0.15) # 15% trail gap
                else:
                    sl_pct = max(sl_pct, hwm_pct - 0.10) # 10% trail gap
                    
            # Check Stop Loss Hit
            if pnl_low_pct <= sl_pct:
                exit_price_val = pos_entry_val * (1.0 + sl_pct)
                exit_reason = f"SL Hit at {sl_pct*100:+.1f}%"
                break
                
            # Theta Abort Check (at 3rd minute close)
            if m_idx == 2:
                if pnl_close_pct < 0.05:
                    exit_price_val = val_close
                    exit_reason = f"Theta Abort at 3m (PnL: {pnl_close_pct*100:.1f}%)"
                    break
                    
        # Hard Cap Close at 5th minute close
        if exit_price_val is None:
            exit_price_val = val_close
            exit_reason = "5m Hard Cap Reached"
            
        # PnL calculations
        gross_pnl_rs = exit_price_val - pos_entry_val
        net_pnl_rs = gross_pnl_rs - round_trip_charge
        
        # Compound capital!
        prev_capital = capital
        capital += net_pnl_rs
        
        trades.append({
            "timestamp": ts,
            "type": sig_type,
            "prev_capital": prev_capital,
            "new_capital": capital,
            "entry_val": pos_entry_val,
            "exit_val": exit_price_val,
            "gross_pnl": gross_pnl_rs,
            "net_pnl": net_pnl_rs,
            "reason": exit_reason,
            "hwm_pct": hwm_pct * 100,
            "lots": sum(allocations)
        })
        
    # Compile Report
    trades_df = pd.DataFrame(trades)
    total_trades = len(trades_df)
    wins = trades_df[trades_df["net_pnl"] > 0]
    losses = trades_df[trades_df["net_pnl"] <= 0]
    
    win_rate = (len(wins) / total_trades) * 100
    total_profit_rs = wins["net_pnl"].sum()
    total_loss_rs = losses["net_pnl"].sum()
    final_capital = capital
    
    # Weekly breakdown calculations
    week1_end_date = pd.Timestamp("2026-07-10 23:59:59")
    week1_trades = trades_df[trades_df["timestamp"].dt.tz_localize(None) <= week1_end_date]
    week2_trades = trades_df[trades_df["timestamp"].dt.tz_localize(None) > week1_end_date]
    
    week1_capital = week1_trades.iloc[-1]["new_capital"] if not week1_trades.empty else 2500.0
    week2_capital = week2_trades.iloc[-1]["new_capital"] if not week2_trades.empty else week1_capital
    
    print("\n" + "="*60)
    print("       COMPOUNDING PYRAMID OPTION BACKTEST REPORT       ")
    print("="*60)
    print(f"Initial Capital:         Rs 2,500.00")
    print(f"Week 1 End Capital:      Rs {week1_capital:,.2f}")
    print(f"Week 2 End Capital:      Rs {week2_capital:,.2f}")
    print(f"Net Compounded Returns:  {((final_capital - 2500) / 2500 * 100):+,.2f}%")
    print(f"Total Trades Taken:      {total_trades} (Week 1: {len(week1_trades)} | Week 2: {len(week2_trades)})")
    print(f"Win Rate:                {win_rate:.2f}% ({len(wins)} Wins / {len(losses)} Losses)")
    print(f"Total Gross Profit:      Rs {total_profit_rs:,.2f}")
    print(f"Total Gross Loss:        Rs {abs(total_loss_rs):,.2f}")
    print(f"Total Brokerage Deducted:Rs {total_trades * round_trip_charge:,.2f}")
    print("="*60)
    
    # Save markdown report
    report_content = f"""# Kronos Compounded Pyramid Option Simulation Report

This report simulates a **real-world compounding and strike scaling (pyramiding) scenario** for the **Kronos Options Bot** on the **{total_trades} momentum signals** from **July 6, 2026, to July 17, 2026**.

---

## 📊 Summary Performance Metrics

* **Initial Capital**: **Rs 2,500.00**
* **Week 1 End Capital**: **Rs {week1_capital:,.2f}**
* **Week 2 End Capital**: **Rs {week2_capital:,.2f}**
* **Net Compounded Return**: **{((final_capital - 2500) / 2500 * 100):+,.2f}%**
* **Total Round-Trip Brokerage (Rs 17/trade)**: **Rs {total_trades * round_trip_charge:,.2f}**

| Metric | Value |
| :--- | :--- |
| **Total Trades Executed** | **{total_trades}** (Week 1: {len(week1_trades)} | Week 2: {len(week2_trades)}) |
| **Win Rate** | **{win_rate:.2f}%** ({len(wins)} Wins / {len(losses)} Losses) |
| **Total Profit Generated** | **Rs {total_profit_rs:,.2f}** |
| **Total Losses Incurred** | **Rs {abs(total_loss_rs):,.2f}** |

---

## 🛡️ Exit Reasons Breakdown

| Exit Reason | Count | Percentage |
| :--- | :--- | :--- |
| **Stop Loss (SL) Hit** | {len(trades_df[trades_df["reason"].str.startswith("SL")])} | {len(trades_df[trades_df["reason"].str.startswith("SL")]) / total_trades * 100:.2f}% |
| **Theta Decay Abort (at 3m)** | {len(trades_df[trades_df["reason"].str.startswith("Theta")])} | {len(trades_df[trades_df["reason"].str.startswith("Theta")]) / total_trades * 100:.2f}% |
| **5m Hard Cap Close** | {len(trades_df[trades_df["reason"] == "5m Hard Cap Reached"])} | {len(trades_df[trades_df["reason"] == "5m Hard Cap Reached"]) / total_trades * 100:.2f}% |

---

## 📈 Compounding & Strike selector scaling logic

1. **Compounding Capital**: Profit is added to capital, and losses are deducted. 
2. **Pyramid Strike selector**: As capital grows, the selector dynamically buys more lots and scales up position size. If capital decreases, the selector shifts deeper OTM to keep trading affordable.
3. **Brokerage Deduction**: Every round trip trades is charged Rs 17.00.
"""
    
    workspace_path = r"C:\Kronos_Testing\astra_bot\data\reports\compounding_options_report.md"
    artifact_path = r"C:\Users\Amarjeet Singh\.gemini\antigravity\brain\c39de48c-b682-46a5-bbde-c9881775a017\compounding_options_report.md"
    
    os.makedirs(os.path.dirname(workspace_path), exist_ok=True)
    with open(workspace_path, 'w', encoding='utf-8') as f:
        f.write(report_content)
    with open(artifact_path, 'w', encoding='utf-8') as f:
        f.write(report_content)
        
    print(f"\nCompounding Report saved to:\n1. {workspace_path}\n2. {artifact_path}")

if __name__ == "__main__":
    run_compounding_backtest()
