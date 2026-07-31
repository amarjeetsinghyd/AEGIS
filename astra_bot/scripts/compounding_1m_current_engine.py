import os
import sys
import pandas as pd
import numpy as np
from datetime import datetime, time, timedelta

# Ensure project root is in sys.path
BASE_DIR = r"C:\Kronos_Testing"
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from astra_bot.strategy.indicators import append_all_indicators
from astra_bot.strategy.signal_generator import SignalGenerator

def run_compounding_backtest():
    csv_path = r"C:\Kronos_Testing\astra_bot\data\scratch\nifty_1m_data.csv"
    
    if not os.path.exists(csv_path):
        print(f"Error: Cache file not found at {csv_path}. Please run download_synthetic_data.py first.")
        return
        
    print(f"Loading Nifty 1-minute Spot candles from {csv_path}...")
    df_1m = pd.read_csv(csv_path)
    df_1m['timestamp'] = pd.to_datetime(df_1m['timestamp']).dt.tz_localize(None)
    df_1m = df_1m.sort_values('timestamp').reset_index(drop=True)
    
    print("Calculating technical indicators (VWAP, EMA_9, VFI, VFI_EMA) via Polars...")
    df_1m = append_all_indicators(df_1m)
    
    # Initialize the production SignalGenerator
    signal_gen = SignalGenerator()
    
    # Target backtest date range
    target_start_date = pd.Timestamp("2026-07-06").date()
    target_end_date = pd.Timestamp("2026-07-17").date()
    
    print(f"Scanning candles from {target_start_date} to {target_end_date} for strategy signals...")
    
    # Simulation Parameters
    capital = 2500.0
    P0 = 35.0
    delta_atm = 0.5
    gamma_atm = 0.002
    lot_size = 65
    round_trip_charge = 17.0
    
    trades = []
    active_trade_exit_idx = -1
    
    # Loop over candles
    for i in range(131, len(df_1m)):
        # Skip if we are inside a currently open trade window (to avoid multiple entries)
        if i <= active_trade_exit_idx:
            continue
            
        # Get historical window up to current index
        df_slice = df_1m.iloc[:i+1]
        
        latest = df_slice.iloc[-1]
        candle_ts = latest['timestamp']
        candle_date = candle_ts.date()
        
        # Check date range
        if not (target_start_date <= candle_date <= target_end_date):
            continue
            
        # Check active trading hours (09:15 to 15:30)
        c_time = candle_ts.time()
        if not (time(9, 15) <= c_time <= time(15, 30)):
            continue
            
        # Evaluate strategies in production priority order
        s1_sig, _ = signal_gen.check_signal(df_slice)
        s2_sig, _ = signal_gen.check_rejection_signal(df_slice)
        s3_sig, _ = signal_gen.check_vwap_band_breakout_signal(df_slice)
        
        sig_type = None
        strategy_name = ""
        
        if s1_sig:
            sig_type = s1_sig["type"]
            strategy_name = "S1_WINDOW_ALIGN"
        elif s2_sig:
            sig_type = s2_sig["type"]
            strategy_name = "S2_REJECTION"
        elif s3_sig:
            sig_type = s3_sig["type"]
            strategy_name = "S3_VWAP_BREAKOUT"
            
        if not sig_type:
            continue
            
        # Execute Trade
        entry_price = latest['close']
        
        cheapest_otm_cost = P0 * (0.8 ** 19) * lot_size
        if capital < cheapest_otm_cost:
            print(f"[{candle_ts}] Bot is Bankrupt! Capital: Rs {capital:.2f}")
            break
            
        # Strike Selection & Lot Allocation
        strike_costs = [P0 * (0.8 ** k) * lot_size for k in range(20)]
        start_idx = -1
        for k in range(20):
            if strike_costs[k] <= capital:
                start_idx = k
                break
        if start_idx == -1:
            continue
            
        allocations = [0] * 20
        remaining_funds = capital
        
        # Forward Ladder
        ladder_end = min(start_idx + 5, 20)
        for idx in range(start_idx, ladder_end):
            cost_per_lot = strike_costs[idx]
            if remaining_funds >= cost_per_lot:
                allocations[idx] += 1
                remaining_funds -= cost_per_lot
            else:
                break
                
        # Reverse Sweep
        active_allocations = [idx for idx, qty in enumerate(allocations) if qty > 0]
        if active_allocations:
            max_idx = max(active_allocations)
            total_sweep_cost = sum(strike_costs[idx] for idx in range(start_idx, max_idx + 1))
            if total_sweep_cost > 0:
                num_rounds = int(remaining_funds // total_sweep_cost)
                if num_rounds > 0:
                    for idx in range(start_idx, max_idx + 1):
                        allocations[idx] += num_rounds
                    remaining_funds -= num_rounds * total_sweep_cost
            
            for idx in range(max_idx, start_idx - 1, -1):
                cost_per_lot = strike_costs[idx]
                if remaining_funds >= cost_per_lot:
                    allocations[idx] += 1
                    remaining_funds -= cost_per_lot
                    
        pos_entry_val = sum(qty * strike_costs[idx] for idx, qty in enumerate(allocations))
        if pos_entry_val == 0:
            continue
            
        # Simulate next 5 minutes
        candles_5m = []
        for minute in range(1, 6):
            target_idx = i + minute
            if target_idx < len(df_1m):
                candles_5m.append(df_1m.iloc[target_idx])
                
        if len(candles_5m) < 5:
            continue
            
        # Set active trade exit index to lock out scanning during the 5 minutes
        active_trade_exit_idx = i + 5
        
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
                
        hwm_pct = 0.0
        sl_pct = -0.20
        exit_price_val = None
        exit_reason = None
        
        for m_idx, m_candle in enumerate(candles_5m):
            m_high = m_candle['high']
            m_low = m_candle['low']
            m_close = m_candle['close']
            
            val_high = 0.0
            val_low = 0.0
            val_close = 0.0
            
            for curve in opt_curves:
                if sig_type == "CALL":
                    dS_high = m_high - entry_price
                    opt_high = curve["entry_prem"] + curve["delta"] * dS_high + 0.5 * curve["gamma"] * (dS_high ** 2)
                    opt_high = max(0.5, opt_high)
                    
                    dS_low = m_low - entry_price
                    opt_low = curve["entry_prem"] + curve["delta"] * dS_low + 0.5 * curve["gamma"] * (dS_low ** 2)
                    opt_low = max(0.5, opt_low)
                    
                    dS_close = m_close - entry_price
                    opt_close = curve["entry_prem"] + curve["delta"] * dS_close + 0.5 * curve["gamma"] * (dS_close ** 2)
                    opt_close = max(0.5, opt_close)
                else:
                    dS_high = m_high - entry_price
                    opt_high = curve["entry_prem"] - curve["delta"] * dS_high + 0.5 * curve["gamma"] * (dS_high ** 2)
                    opt_high = max(0.5, opt_high)
                    
                    dS_low = m_low - entry_price
                    opt_low = curve["entry_prem"] - curve["delta"] * dS_low + 0.5 * curve["gamma"] * (dS_low ** 2)
                    opt_low = max(0.5, opt_low)
                    
                    dS_close = m_close - entry_price
                    opt_close = curve["entry_prem"] - curve["delta"] * dS_close + 0.5 * curve["gamma"] * (dS_close ** 2)
                    opt_close = max(0.5, opt_close)
                    
                val_high += curve["qty"] * opt_high * lot_size
                val_low += curve["qty"] * opt_low * lot_size
                val_close += curve["qty"] * opt_close * lot_size
                
            max_val = val_high if sig_type == "CALL" else val_low
            min_val = val_low if sig_type == "CALL" else val_high
            
            pnl_high_pct = (max_val - pos_entry_val) / pos_entry_val
            pnl_low_pct = (min_val - pos_entry_val) / pos_entry_val
            pnl_close_pct = (val_close - pos_entry_val) / pos_entry_val
            
            if pnl_high_pct > hwm_pct:
                hwm_pct = pnl_high_pct
                
            if hwm_pct >= 0.05:
                sl_pct = max(sl_pct, 0.0)
            if hwm_pct >= 0.10:
                sl_pct = max(sl_pct, 0.05)
            if hwm_pct >= 0.20:
                sl_pct = max(sl_pct, 0.12)
            if hwm_pct >= 0.30:
                sl_pct = max(sl_pct, 0.20)
                
            if hwm_pct >= 0.15:
                if hwm_pct > 0.50:
                    sl_pct = max(sl_pct, hwm_pct - 0.15)
                else:
                    sl_pct = max(sl_pct, hwm_pct - 0.10)
                    
            if pnl_low_pct <= sl_pct:
                exit_price_val = pos_entry_val * (1.0 + sl_pct)
                exit_reason = f"SL Hit at {sl_pct*100:+.1f}%"
                break
                
            if m_idx == 2:
                if pnl_close_pct < 0.05:
                    exit_price_val = val_close
                    exit_reason = f"Theta Abort at 3m (PnL: {pnl_close_pct*100:.1f}%)"
                    break
                    
        if exit_price_val is None:
            exit_price_val = val_close
            exit_reason = "5m Hard Cap Reached"
            
        gross_pnl_rs = exit_price_val - pos_entry_val
        net_pnl_rs = gross_pnl_rs - round_trip_charge
        
        prev_capital = capital
        capital += net_pnl_rs
        
        trades.append({
            "timestamp": candle_ts,
            "type": sig_type,
            "strategy": strategy_name,
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
        
    if not trades:
        print("No strategy trades were triggered during the backtest period.")
        return
        
    trades_df = pd.DataFrame(trades)
    total_trades = len(trades_df)
    wins = trades_df[trades_df["net_pnl"] > 0]
    losses = trades_df[trades_df["net_pnl"] <= 0]
    
    win_rate = (len(wins) / total_trades) * 100
    total_profit_rs = wins["net_pnl"].sum()
    total_loss_rs = losses["net_pnl"].sum()
    final_capital = capital
    
    week1_end_date = pd.Timestamp("2026-07-10 23:59:59")
    week1_trades = trades_df[trades_df["timestamp"] <= week1_end_date]
    week2_trades = trades_df[trades_df["timestamp"] > week1_end_date]
    
    week1_capital = week1_trades.iloc[-1]["new_capital"] if not week1_trades.empty else 2500.0
    week2_capital = week2_trades.iloc[-1]["new_capital"] if not week2_trades.empty else week1_capital
    
    print("\n" + "="*60)
    print("      CURRENT ENGINE COMPOUNDING BACKTEST REPORT       ")
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
    report_content = f"""# Current Engine Compounded Option Simulation Report

This report simulates a **real-world compounding and strike scaling (pyramiding) scenario** for the **Current Engine's 3 Strategies** (Window Alignment, Rejection Window, VWAP Band Breakout) from **July 6, 2026, to July 17, 2026**.

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
    
    workspace_path = r"C:\Kronos_Testing\astra_bot\data\reports\compounding_options_report_current_engine.md"
    artifact_path = r"C:\Users\Amarjeet Singh\.gemini\antigravity\brain\c39de48c-b682-46a5-bbde-c9881775a017\compounding_options_report_current_engine.md"
    
    os.makedirs(os.path.dirname(workspace_path), exist_ok=True)
    with open(workspace_path, 'w', encoding='utf-8') as f:
        f.write(report_content)
    with open(artifact_path, 'w', encoding='utf-8') as f:
        f.write(report_content)
        
    print(f"\nReport saved to:\n1. {workspace_path}\n2. {artifact_path}")

if __name__ == "__main__":
    run_compounding_backtest()
