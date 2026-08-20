"""
backtest_aegis_current_logic.py — Replays all 724,530 ticks of 18-Aug through the live AEGIS Gamma Engine.
"""
from __future__ import annotations
import os
import sys
import glob
import queue
from dataclasses import dataclass
import pandas as pd
import numpy as np

# Ensure project root is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from AEGIS.aegis_gamma_engine import AegisGammaEngine

@dataclass
class Tick:
    tok: str
    sym: str
    lp: float
    vol: float
    bp: list[float]
    bq: list[float]
    sp: list[float]
    sq: list[float]
    ts: float

def run_aegis_backtest_18aug():
    data_dir = os.path.join(BASE_DIR, "AEGIS", "data", "l2_raw")
    files = sorted(glob.glob(os.path.join(data_dir, "*2026-08-18*.parquet")))
    print(f"Loading {len(files)} AEGIS L2 Parquet files for 18-Aug...")
    if not files:
        print("No files found!")
        return

    dfs = [pd.read_parquet(f) for f in files]
    full_df = pd.concat(dfs, ignore_index=True).sort_values("ts").reset_index(drop=True)
    print(f"Total Ticks Loaded: {len(full_df):,}")

    token_map = {}
    token_to_symbol = {}
    tok_col = "tok" if "tok" in full_df.columns else "token"
    sym_col = "sym" if "sym" in full_df.columns else "symbol"
    
    strikes = set()
    for _, row in full_df.drop_duplicates(subset=[tok_col]).iterrows():
        tok = str(row[tok_col])
        sym = str(row[sym_col])
        strike = float(row.get("strike", 0.0))
        kind = str(row.get("kind", "CE")).upper()
        token_to_symbol[tok] = sym
        if strike > 0:
            strikes.add(strike)
            token_map[tok] = {
                "symbol": sym, "strike": strike, "side": 0 if "CE" in kind or "C" in sym else 1,
                "option_type": kind, "token": tok, "expiry": "2026-08-18"
            }

    sorted_strikes = sorted(list(strikes))
    print(f"Discovered {len(token_map)} option tokens across strikes: {sorted_strikes[:5]}...{sorted_strikes[-5:]}")

    sig_q = queue.Queue()
    engine = AegisGammaEngine(
        token_to_symbol=token_to_symbol,
        token_map=token_map,
        strike_step=50.0,
        signal_queue=sig_q
    )

    # Set underlying token and spot
    engine.underlying_tokens.add("26000")
    engine.last_spot = 24550.0
    engine.current_atm_strike = 24550.0

    signals_emitted = []

    print("\nReplaying 724,530 ticks through AEGIS engine...")
    
    # Process ticks in chronological order
    for i, row in full_df.iterrows():
        tok = str(row[tok_col])
        sym = str(row[sym_col])
        lp = float(row.get("lp", 0.0))
        vol = float(row.get("vol", 0.0))
        ts_val = float(row.get("ts", 0.0))
        if ts_val > 1e11:
            ts_val /= 1000.0

        bids = [float(row.get(f"bp{k}", 0.0)) for k in range(1, 6)]
        bids_q = [float(row.get(f"bq{k}", 0.0)) for k in range(1, 6)]
        asks = [float(row.get(f"sp{k}", 0.0)) for k in range(1, 6)]
        asks_q = [float(row.get(f"sq{k}", 0.0)) for k in range(1, 6)]

        tick_obj = Tick(
            tok=tok, sym=sym, lp=lp, vol=vol,
            bp=bids, bq=bids_q, sp=asks, sq=asks_q, ts=ts_val
        )

        engine.on_tick_parsed(tick_obj)

        while not sig_q.empty():
            signals_emitted.append(sig_q.get())

        if (i + 1) % 200000 == 0:
            print(f"  Processed {i+1:,}/{len(full_df):,} ticks | Signals: {len(signals_emitted)}")

    print("\n" + "=" * 80)
    print("🔬 AEGIS CURRENT LOGIC BACKTEST RESULTS (18-Aug-2026)")
    print("=" * 80)
    print(f"Total Ticks Processed   : {len(full_df):,}")
    print(f"Total Signals Emitted   : {len(signals_emitted)}")
    
    if signals_emitted:
        print("\n--- EMITTED SIGNALS ---")
        for s in signals_emitted:
            print(s)
    else:
        print("\n❌ 0 Signals Emitted by AEGIS Gamma Engine during the entire trading day!")
        print("\nEngine Final State Diagnostics:")
        state = engine.get_state_summary()
        print(f"Spot Price   : {state.get('spot')}")
        print(f"ATM IV       : {state.get('atm_iv')}")
        print(f"Gamma Sum    : {state.get('gamma_sum')}")
        print(f"Hawkes B     : {state.get('hawkes_B')}")
        print(f"Fills recorded: {state.get('fills_recorded')}")
    print("=" * 80)

if __name__ == "__main__":
    run_aegis_backtest_18aug()
