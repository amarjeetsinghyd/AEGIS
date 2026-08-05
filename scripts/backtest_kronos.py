import os
import sys
import pandas as pd
import numpy as np
from datetime import datetime, time as dt_time

# Ensure root is in sys.path
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from kronos_bot.strategy.signal_generator import KronosSignalGenerator
from kronos_bot.execution.kronos_execution_manager import compute_chandelier_stops

def run_backtest():
    print("Loading Data...")
    parquet_path = os.path.join(BASE, "data", "underlying_candles_NIFTY.parquet")
    df = pd.read_parquet(parquet_path)
    
    # Initialize the signal generator
    generator = KronosSignalGenerator(device='cpu')
    
    # Backtest parameters
    INITIAL_CAPITAL = 25000.0
    LOT_SIZE = 65
    BASE_PREMIUM = 30.76  # roughly 2000 INR per lot
    DELTA = 0.5
    
    capital = INITIAL_CAPITAL
    open_trade = None
    trades = []
    
    min_bars = 50
    total_bars = len(df)
    
    print(f"Starting Simulation on {total_bars - min_bars} bars...")
    
    for i in range(min_bars, total_bars):
        window_df = df.iloc[:i+1]
        current_bar = window_df.iloc[-1]
        now = current_bar['timestamp']
        current_close = float(current_bar['close'])
        
        # If trade is OPEN, check for EXITS
        if open_trade is not None:
            # Update Option Premium Proxy
            if open_trade['direction'] == 'CALL':
                current_premium = open_trade['entry_premium'] + (current_close - open_trade['entry_index']) * DELTA
            else:
                current_premium = open_trade['entry_premium'] + (open_trade['entry_index'] - current_close) * DELTA
                
            current_time = now.time()
            elapsed_sec = (now - open_trade['entry_time']).total_seconds()
            
            exit_reason = None
            
            # 1. Force Flatten
            if current_time >= dt_time(15, 10, 0):
                exit_reason = "15:10 Force Flatten"
            
            # 2. Hard SL (-25%)
            elif current_premium <= open_trade['sl_price']:
                exit_reason = "-25% Hard SL"
                
            # 3. Take Profit (+10%)
            elif current_premium >= open_trade['tp_price']:
                exit_reason = "+10% Take Profit"
                
            # 4. Time Stop (600s)
            elif elapsed_sec >= 600:
                exit_reason = "600s Time Stop"
                
            # 5. Chandelier Stop
            else:
                long_stop, short_stop = compute_chandelier_stops(window_df)
                if long_stop is not None and short_stop is not None:
                    if open_trade['direction'] == 'CALL' and current_close < long_stop:
                        exit_reason = "Chandelier Exit (Long)"
                    elif open_trade['direction'] == 'PUT' and current_close > short_stop:
                        exit_reason = "Chandelier Exit (Short)"
                        
            if exit_reason:
                pnl = (current_premium - open_trade['entry_premium']) * LOT_SIZE
                capital += pnl
                trades.append({
                    'entry_time': open_trade['entry_time'],
                    'exit_time': now,
                    'direction': open_trade['direction'],
                    'entry_index': open_trade['entry_index'],
                    'exit_index': current_close,
                    'entry_premium': open_trade['entry_premium'],
                    'exit_premium': current_premium,
                    'pnl': pnl,
                    'reason': exit_reason,
                    'elapsed_min': elapsed_sec / 60
                })
                print(f"[{now}] EXIT {open_trade['direction']} | {exit_reason} | PnL: Rs.{pnl:.2f} | Premium: Rs.{current_premium:.2f} | Cap: Rs.{capital:.2f}")
                open_trade = None
                continue  # Skip entry logic on the bar we exit
        
        # Check for ENTRIES
        current_time = now.time()
        if dt_time(9, 20, 0) <= current_time <= dt_time(14, 50, 0) and open_trade is None:
            signal = generator.predict(window_df, threshold=0.001)
            sig = signal.get("signal", "NEUTRAL")
            
            if sig in ["CALL", "PUT"]:
                open_trade = {
                    'direction': sig,
                    'entry_time': now,
                    'entry_index': current_close,
                    'entry_premium': BASE_PREMIUM,
                    'tp_price': BASE_PREMIUM * 1.10,
                    'sl_price': BASE_PREMIUM * 0.75
                }
                print(f"[{now}] ENTER {sig} | Index: {current_close:.2f} | Premium: Rs.{BASE_PREMIUM:.2f} | Reason: {signal.get('reason','')}")

    # EOD cleanup
    if open_trade is not None:
        print("Closing open trade at end of simulation data...")
        if open_trade['direction'] == 'CALL':
            current_premium = open_trade['entry_premium'] + (current_close - open_trade['entry_index']) * DELTA
        else:
            current_premium = open_trade['entry_premium'] + (open_trade['entry_index'] - current_close) * DELTA
            
        pnl = (current_premium - open_trade['entry_premium']) * LOT_SIZE
        capital += pnl
        trades.append({
            'entry_time': open_trade['entry_time'],
            'exit_time': now,
            'direction': open_trade['direction'],
            'entry_index': open_trade['entry_index'],
            'exit_index': current_close,
            'entry_premium': open_trade['entry_premium'],
            'exit_premium': current_premium,
            'pnl': pnl,
            'reason': "End of Data Flatten",
            'elapsed_min': (now - open_trade['entry_time']).total_seconds() / 60
        })

    # Summary
    print("\n" + "="*50)
    print("BACKTEST SIMULATION SUMMARY")
    print("="*50)
    print(f"Initial Capital: Rs.{INITIAL_CAPITAL:.2f}")
    print(f"Final Capital:   Rs.{capital:.2f}")
    print(f"Total Return:    Rs.{capital - INITIAL_CAPITAL:.2f} ({((capital/INITIAL_CAPITAL)-1)*100:.2f}%)")
    print(f"Total Trades:    {len(trades)}")
    
    if trades:
        wins = [t for t in trades if t['pnl'] > 0]
        losses = [t for t in trades if t['pnl'] <= 0]
        print(f"Win Rate:        {len(wins)/len(trades)*100:.2f}% ({len(wins)}W / {len(losses)}L)")
        if wins:
            print(f"Avg Win:         Rs.{np.mean([t['pnl'] for t in wins]):.2f}")
        if losses:
            print(f"Avg Loss:        Rs.{np.mean([t['pnl'] for t in losses]):.2f}")
        
        print("\nTRADE LOG:")
        df_trades = pd.DataFrame(trades)
        print(df_trades[['entry_time', 'direction', 'reason', 'pnl', 'exit_time', 'elapsed_min']].to_string())
    
if __name__ == "__main__":
    run_backtest()
