"""
AEGIS V4.0 — High-Fidelity Historical Parquet Backtester
Evaluates production AEGIS V4.0 algorithmic stack against recorded Level-2 parquet data.

Core Pipeline:
1. Macro Priority Engine:
   - Pillar 1: 20 EMA Wick Rejection
   - Pillar 2: Liquidity Sweep & Reclaim
   - Pillar 3: Volatility Squeeze Breakout
   - Pillar 4: Dual-Envelope HVG Network Phase Shift Breakout (Monotonic Stack)
2. Decoupled Confirm15 State Machine on Continuous Futures:
   - Scale-invariant drift z_15 >= 0.75
   - Path efficiency ER_15 >= 0.55
   - Elastic candle body ratio phi
   - Option premium concordance r_opt >= +1.0%
   - Adverse excursion invalidation: 0.45 * ATR_1m
   - Stabilization window: 8.0s to 45.0s
3. Top-5 Depth Microstructure Guard:
   - Micro-price skew intercept
   - Spread expansion filter
4. Single-Target 100% Quantity Execution Engine:
   - 100% quantity exit at fixed +4.0% TP (or +5.0% if conviction >= 85)
   - Hard risk floor at -3.5% SL
   - Time ceiling: 300s (5 min) max hold
   - Single lot (65 shares), no fractional lot splits, no runner state churn.
"""

from __future__ import annotations
import os
import sys
import glob
import time
import math
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
import polars as pl
import numpy as np

# Ensure repo root is on sys.path
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from AEGIS.core.aegis_v4_core import (
    Direction,
    SignalSource,
    FastTickRingBuffer,
    FastDualEnvelopeHVG,
    Top5DepthGuard,
    CandleBar,
    MacroPriorityEngine,
    WatchSlotState,
    Confirm15StateMachine,
    PositionState,
    ActivePosition,
    SingleTargetExecutionEngine,
)


@dataclass
class BacktestTradeRecord:
    trade_id: int
    symbol: str
    direction: str
    source_name: str
    entry_ts: float
    entry_time_str: str
    entry_price: float
    qty: int
    trade_val: float
    exit_ts: float = 0.0
    exit_time_str: str = ""
    exit_price: float = 0.0
    exit_pct: float = 0.0
    exit_reason: str = ""
    hold_sec: float = 0.0
    gross_pnl_rs: float = 0.0
    charges_rs: float = 16.50
    net_pnl_rs: float = 0.0
    capital_after: float = 0.0


class AegisV4ParquetBacktester:
    def __init__(
        self,
        data_dir: str = "/home/opc/AEGIS/data/l2_raw/Nifty",
        date_str: str = "2026-09-15",
        start_capital: float = 2000.0,
        lot_size: int = 65,
        target_tp_pct: float = 4.0,
        stop_loss_pct: float = 3.5,
        max_hold_sec: float = 300.0,
        charges_per_trade: float = 16.50,
        cooldown_sec: float = 60.0,
    ) -> None:
        self.data_dir = data_dir
        self.date_str = date_str
        self.start_capital = start_capital
        self.current_capital = start_capital
        self.peak_capital = start_capital
        self.lot_size = lot_size
        self.target_tp_pct = target_tp_pct
        self.stop_loss_pct = stop_loss_pct
        self.max_hold_sec = max_hold_sec
        self.charges_per_trade = charges_per_trade
        self.cooldown_sec = cooldown_sec

        # V4 Subsystems
        self.macro_engine = MacroPriorityEngine()
        self.state_machine = Confirm15StateMachine()
        self.depth_guard = Top5DepthGuard(history_len=30)
        self.exec_engine = SingleTargetExecutionEngine()
        self.futures_buffer = FastTickRingBuffer(size=256)

        # Depth matrices
        self.bids_matrix = np.zeros((5, 2), dtype=np.float64)
        self.asks_matrix = np.zeros((5, 2), dtype=np.float64)

        # Candle aggregate state
        self.spot_1m_bars: List[CandleBar] = []
        self.spot_5m_bars: List[CandleBar] = []
        self.hist_vol_60m = np.zeros(60, dtype=np.float64)
        self.vol_idx = 0
        self.current_spot_atr = 14.0
        self.current_ema20 = 0.0

        # Futures in-progress candle
        self.fut_candle_open = 0.0
        self.fut_candle_high = 0.0
        self.fut_candle_low = 0.0
        self.fut_candle_last = 0.0
        self.fut_candle_open_ts = 0.0

        # Armed setup state
        self.armed_source = SignalSource.NONE
        self.armed_direction = Direction.FLAT
        self.armed_target_sym = ""
        self.armed_target_tok = ""
        self.armed_opt_ltp = 0.0

        # Active trade tracking
        self.active_trade_record: Optional[BacktestTradeRecord] = None
        self.completed_trades: List[BacktestTradeRecord] = []
        self.last_exit_ts = -1.0e9
        self.trade_counter = 0

        # Universe maps
        self.token_to_sym: Dict[str, str] = {}
        self.sym_to_token: Dict[str, str] = {}
        self.opt_meta: Dict[str, Dict[str, Any]] = {}  # tok -> {side, strike, sym}

    def _discover_files(self) -> List[str]:
        pattern = os.path.join(self.data_dir, f"aegis_l2_raw_{self.date_str}_part_*.parquet")
        files = sorted(glob.glob(pattern))
        valid = [f for f in files if os.path.getsize(f) >= 50000]
        return valid

    def _build_universe(self, files: List[str]) -> None:
        for f in files:
            try:
                meta = pl.read_parquet(f, columns=["tok", "sym", "kind", "strike"]).unique()
                for row in meta.iter_rows(named=True):
                    tok = str(row["tok"])
                    sym = str(row["sym"])
                    kind = int(row["kind"])
                    strike = float(row.get("strike", 0.0) or 0.0)

                    self.token_to_sym[tok] = sym
                    self.sym_to_token[sym] = tok
                    if kind in (1, 2):  # 1=CE, 2=PE
                        side = "CE" if kind == 1 else "PE"
                        self.opt_meta[tok] = {
                            "side": side,
                            "strike": strike,
                            "sym": sym,
                            "tok": tok,
                        }
            except Exception:
                pass

    def _select_option_token(self, direction: Direction, ref_spot: float) -> Optional[Dict[str, Any]]:
        target_side = "CE" if direction == Direction.BULLISH else "PE"
        candidates = [m for m in self.opt_meta.values() if m["side"] == target_side and m["strike"] > 0]
        if not candidates:
            return None
        # Select strike nearest to spot
        best = min(candidates, key=lambda x: abs(x["strike"] - ref_spot))
        return best

    def _update_indicators(self) -> None:
        if len(self.spot_1m_bars) < 15:
            return
        tr_sum = 0.0
        n = len(self.spot_1m_bars)
        for i in range(n - 14, n):
            b = self.spot_1m_bars[i]
            prev_c = self.spot_1m_bars[i - 1].close
            tr = max(b.high - b.low, abs(b.high - prev_c), abs(b.low - prev_c))
            tr_sum += tr
        self.current_spot_atr = tr_sum / 14.0

        if len(self.spot_1m_bars) >= 20:
            k = 2.0 / (20.0 + 1.0)
            if self.current_ema20 <= 0.0:
                self.current_ema20 = float(np.mean([b.close for b in self.spot_1m_bars[-20:]]))
            else:
                self.current_ema20 = (self.spot_1m_bars[-1].close * k) + (self.current_ema20 * (1.0 - k))

    def run(self) -> Dict[str, Any]:
        files = self._discover_files()
        if not files:
            raise FileNotFoundError(f"No parquet files found for {self.date_str} in {self.data_dir}")

        self._build_universe(files)

        total_ticks = 0
        t0 = time.perf_counter()

        # In-progress candle state
        cur_min_bucket = -1
        cur_5m_bucket = -1
        s1m_o, s1m_h, s1m_l, s1m_c, s1m_v = 0.0, 0.0, 0.0, 0.0, 0.0
        s5m_o, s5m_h, s5m_l, s5m_c, s5m_v = 0.0, 0.0, 0.0, 0.0, 0.0

        last_known_opt_prices: Dict[str, float] = {}
        last_known_opt_bids: Dict[str, float] = {}
        last_known_opt_asks: Dict[str, float] = {}
        last_spot_price = 0.0

        signals_emitted = []

        for fpath in files:
            df = pl.read_parquet(fpath)
            total_ticks += len(df)

            tok_list = [str(x) for x in df["tok"].to_list()]
            lp_list = [float(x) for x in df["lp"].to_list()]
            ts_list = [int(x) for x in df["ts"].to_list()]
            vol_list = [float(x or 0.0) for x in df["vol"].to_list()] if "vol" in df.columns else [0.0] * len(df)
            bp1_list = [float(x or 0.0) for x in df["bp1"].to_list()] if "bp1" in df.columns else [0.0] * len(df)
            sp1_list = [float(x or 0.0) for x in df["sp1"].to_list()] if "sp1" in df.columns else [0.0] * len(df)
            bq1_list = [float(x or 0.0) for x in df["bq1"].to_list()] if "bq1" in df.columns else [0.0] * len(df)
            sq1_list = [float(x or 0.0) for x in df["sq1"].to_list()] if "sq1" in df.columns else [0.0] * len(df)

            for i in range(len(df)):
                tok = tok_list[i]
                lp = lp_list[i]
                ts = ts_list[i]
                ts_sec = ts / 1000.0 if ts > 1e11 else float(ts)
                vol = vol_list[i]
                bp1 = bp1_list[i]
                sp1 = sp1_list[i]
                bq1 = bq1_list[i]
                sq1 = sq1_list[i]

                # Update live cache
                if tok in self.opt_meta and lp > 0:
                    last_known_opt_prices[tok] = lp
                    if bp1 > 0: last_known_opt_bids[tok] = bp1
                    if sp1 > 0: last_known_opt_asks[tok] = sp1

                # ------------------------------------------------------------
                # ROUTE 1: SPOT INDEX (Token 26000) -> 1m / 5m Candle Aggregator
                # ------------------------------------------------------------
                if tok == "26000" and lp > 0:
                    last_spot_price = lp
                    bucket_1m = int(ts_sec // 60)
                    bucket_5m = int(ts_sec // 300)

                    # 1m Candle Rollover
                    if cur_min_bucket == -1:
                        cur_min_bucket = bucket_1m
                        s1m_o, s1m_h, s1m_l, s1m_c, s1m_v = lp, lp, lp, lp, vol
                    elif bucket_1m != cur_min_bucket:
                        # Close prior 1m bar
                        bar_1m = CandleBar(
                            timestamp=cur_min_bucket * 60.0,
                            open=s1m_o,
                            high=s1m_h,
                            low=s1m_l,
                            close=s1m_c,
                            volume=s1m_v,
                        )
                        self.spot_1m_bars.append(bar_1m)
                        if len(self.spot_1m_bars) > 120:
                            self.spot_1m_bars.pop(0)

                        self.hist_vol_60m[self.vol_idx] = s1m_v
                        self.vol_idx = (self.vol_idx + 1) % 60
                        self._update_indicators()

                        # 5m Candle Rollover check
                        is_5m_close = False
                        bar_5m = None
                        if cur_5m_bucket == -1:
                            cur_5m_bucket = bucket_5m
                            s5m_o, s5m_h, s5m_l, s5m_c, s5m_v = lp, lp, lp, lp, vol
                        elif bucket_5m != cur_5m_bucket:
                            bar_5m = CandleBar(
                                timestamp=cur_5m_bucket * 300.0,
                                open=s5m_o,
                                high=s5m_h,
                                low=s5m_l,
                                close=s5m_c,
                                volume=s5m_v,
                            )
                            self.spot_5m_bars.append(bar_5m)
                            if len(self.spot_5m_bars) > 60:
                                self.spot_5m_bars.pop(0)
                            cur_5m_bucket = bucket_5m
                            s5m_o, s5m_h, s5m_l, s5m_c, s5m_v = lp, lp, lp, lp, vol
                            is_5m_close = True

                        # Evaluate Macro Priority Engine at 1m close
                        # Mutex: only if no active trade and outside cooldown
                        if (
                            self.exec_engine.active_pos is None
                            and (ts_sec - self.last_exit_ts) >= self.cooldown_sec
                        ):
                            # Ensure we have at least 1 5m bar
                            five_m_feed = self.spot_5m_bars if self.spot_5m_bars else [bar_1m]
                            src, d, hub_p = self.macro_engine.evaluate(
                                self.spot_1m_bars,
                                five_m_feed,
                                self.current_spot_atr,
                                self.current_ema20,
                                self.hist_vol_60m,
                            )

                            if src != SignalSource.NONE and d != Direction.FLAT:
                                # Pick option
                                opt_info = self._select_option_token(d, s1m_c)
                                if opt_info:
                                    opt_tok = opt_info["tok"]
                                    opt_p = last_known_opt_prices.get(opt_tok, 0.0)
                                    if opt_p >= 5.0 and opt_p <= 400.0:
                                        self.armed_source = src
                                        self.armed_direction = d
                                        self.armed_target_tok = opt_tok
                                        self.armed_target_sym = opt_info["sym"]
                                        self.armed_opt_ltp = opt_p

                                        self.state_machine.arm(
                                            source=src,
                                            direction=d,
                                            hub_price=hub_p,
                                            opt_ltp=opt_p,
                                            atr_1m=self.current_spot_atr,
                                            ts=ts_sec,
                                            candle_open=self.fut_candle_open,
                                            fut_price=self.fut_candle_last,
                                        )
                                        signals_emitted.append({
                                            "time": time.strftime("%H:%M:%S", time.localtime(ts_sec)),
                                            "source": src.name,
                                            "direction": "CALL" if d == Direction.BULLISH else "PUT",
                                            "spot": s1m_c,
                                            "symbol": opt_info["sym"],
                                            "opt_ltp": opt_p,
                                        })

                        # Reset 1m accumulator
                        cur_min_bucket = bucket_1m
                        s1m_o, s1m_h, s1m_l, s1m_c, s1m_v = lp, lp, lp, lp, vol
                    else:
                        s1m_h = max(s1m_h, lp)
                        s1m_l = min(s1m_l, lp)
                        s1m_c = lp
                        s1m_v += vol

                    if cur_5m_bucket != -1:
                        s5m_h = max(s5m_h, lp)
                        s5m_l = min(s5m_l, lp)
                        s5m_c = lp
                        s5m_v += vol

                # ------------------------------------------------------------
                # ROUTE 2: CONTINUOUS FUTURES (Token 68407) -> Confirm15 Loop
                # ------------------------------------------------------------
                elif tok == "68407" and lp > 0:
                    bp = bp1 if bp1 > 0 else lp
                    sp = sp1 if sp1 > 0 else lp
                    self.futures_buffer.push(ts_sec, lp, vol, bp, sp)

                    # Update in-progress futures candle
                    if ts_sec - self.fut_candle_open_ts >= 60.0:
                        self.fut_candle_open_ts = ts_sec
                        self.fut_candle_open = lp
                        self.fut_candle_high = lp
                        self.fut_candle_low = lp
                    else:
                        if lp > self.fut_candle_high: self.fut_candle_high = lp
                        if lp < self.fut_candle_low: self.fut_candle_low = lp
                    self.fut_candle_last = lp

                    # Check Confirm15 if armed
                    if self.state_machine.state == WatchSlotState.ARMED:
                        opt_tok = self.armed_target_tok
                        curr_opt_p = last_known_opt_prices.get(opt_tok, self.armed_opt_ltp)

                        status = self.state_machine.evaluate_tick(
                            futures_buffer=self.futures_buffer,
                            opt_ltp=curr_opt_p,
                            current_fut_price=lp,
                            current_fut_high=self.fut_candle_high,
                            current_fut_low=self.fut_candle_low,
                            current_fut_open=self.fut_candle_open,
                            ts=ts_sec,
                        )

                        if status == WatchSlotState.TRIGGERED:
                            # Validate microstructure Top-5 Depth Guard
                            if bp1 > 0: self.bids_matrix[0, 0] = bp1
                            if bq1 > 0: self.bids_matrix[0, 1] = bq1
                            if sp1 > 0: self.asks_matrix[0, 0] = sp1
                            if sq1 > 0: self.asks_matrix[0, 1] = sq1

                            passed, guard_reason = self.depth_guard.validate_execution(
                                direction=self.state_machine.direction,
                                bids=self.bids_matrix,
                                asks=self.asks_matrix,
                            )

                            if passed and self.exec_engine.active_pos is None:
                                # EXECUTE TRADE ENTRY
                                # Price: Best Ask + 2 ticks buffer (scale invariant)
                                ask_fill = last_known_opt_asks.get(opt_tok, curr_opt_p)
                                fill_price = round(ask_fill + 0.10, 2)
                                trade_val = fill_price * self.lot_size

                                self.trade_counter += 1
                                self.exec_engine.open_position(
                                    direction=self.state_machine.direction,
                                    fill_price=fill_price,
                                    qty=self.lot_size,
                                    ts=ts_sec,
                                    conviction=75.0,
                                )

                                self.active_trade_record = BacktestTradeRecord(
                                    trade_id=self.trade_counter,
                                    symbol=self.armed_target_sym,
                                    direction="CALL" if self.armed_direction == Direction.BULLISH else "PUT",
                                    source_name=self.armed_source.name,
                                    entry_ts=ts_sec,
                                    entry_time_str=time.strftime("%H:%M:%S", time.localtime(ts_sec)),
                                    entry_price=fill_price,
                                    qty=self.lot_size,
                                    trade_val=trade_val,
                                )
                                self.state_machine.reset()
                            else:
                                self.state_machine.reset()

                        elif status in (WatchSlotState.INVALIDATED, WatchSlotState.TIMEOUT):
                            self.state_machine.reset()

                # ------------------------------------------------------------
                # ROUTE 3: ACTIVE OPTION TICKS -> SingleTargetExecutionEngine
                # ------------------------------------------------------------
                if (
                    self.exec_engine.active_pos is not None
                    and self.exec_engine.active_pos.state == PositionState.OPEN
                    and self.active_trade_record is not None
                ):
                    pos_sym = self.active_trade_record.symbol
                    pos_tok = self.sym_to_token.get(pos_sym, "")

                    if tok == pos_tok and lp > 0:
                        exit_price = bp1 if bp1 > 0 else lp
                        pivot_ref = self.spot_1m_bars[-1].low if self.spot_1m_bars else 0.0

                        should_exit, qty_close, reason = self.exec_engine.evaluate_exit(
                            current_opt_ltp=exit_price,
                            ts=ts_sec,
                            spot_1m_pivot=pivot_ref,
                        )

                        if should_exit:
                            rec = self.active_trade_record
                            rec.exit_ts = ts_sec
                            rec.exit_time_str = time.strftime("%H:%M:%S", time.localtime(ts_sec))
                            rec.exit_price = exit_price
                            rec.exit_pct = ((exit_price - rec.entry_price) / rec.entry_price) * 100.0
                            rec.exit_reason = reason
                            rec.hold_sec = ts_sec - rec.entry_ts
                            rec.gross_pnl_rs = (exit_price - rec.entry_price) * rec.qty
                            rec.charges_rs = self.charges_per_trade
                            rec.net_pnl_rs = rec.gross_pnl_rs - rec.charges_rs
                            self.current_capital += rec.net_pnl_rs
                            self.peak_capital = max(self.peak_capital, self.current_capital)
                            rec.capital_after = self.current_capital

                            self.completed_trades.append(rec)
                            self.active_trade_record = None
                            self.last_exit_ts = ts_sec

        elapsed = time.perf_counter() - t0
        throughput = total_ticks / max(0.001, elapsed)

        return {
            "date": self.date_str,
            "total_ticks": total_ticks,
            "files_count": len(files),
            "signals_emitted": signals_emitted,
            "signal_count": len(signals_emitted),
            "executed_trades": self.completed_trades,
            "trade_count": len(self.completed_trades),
            "elapsed_sec": elapsed,
            "throughput_tps": throughput,
            "start_capital": self.start_capital,
            "final_capital": self.current_capital,
            "peak_capital": self.peak_capital,
            "last_spot": last_spot_price,
        }

    def print_summary(self, res: Dict[str, Any]) -> None:
        trades = res["executed_trades"]
        start_cap = res["start_capital"]
        final_cap = res["final_capital"]
        net_pnl = final_cap - start_cap
        roi_pct = (net_pnl / start_cap) * 100.0 if start_cap > 0 else 0.0

        wins = sum(1 for t in trades if t.net_pnl_rs > 0)
        losses = sum(1 for t in trades if t.net_pnl_rs <= 0)
        win_rate = (wins / len(trades) * 100.0) if trades else 0.0
        total_gross = sum(t.gross_pnl_rs for t in trades)
        total_charges = sum(t.charges_rs for t in trades)

        print("\n" + "=" * 95)
        print(f"       AEGIS V4.0 INSTITUTIONAL BACKTEST REPORT — NIFTY ({res['date']})       ")
        print("=" * 95)
        print(f"Parquet Files Evaluated:     {res['files_count']} files")
        print(f"Total Ticks Streamed:        {res['total_ticks']:,}")
        print(f"Replay Execution Time:       {res['elapsed_sec']:.2f} seconds ({res['throughput_tps']:,.0f} ticks/s)")
        print(f"Underlying Spot Closes At:   Rs {res['last_spot']:.2f}")
        print(f"Signals Armed (Macro PA):    {res['signal_count']}")
        print(f"Executed Trades Confirmed:   {len(trades)}")
        print("=" * 95)

        print(f"\n--- PERFORMANCE LEDGER (START CAPITAL: Rs {start_cap:,.2f}) ---")
        print(f"Winning Trades:              {wins} ({win_rate:.1f}%)")
        print(f"Losing Trades:               {losses} ({100.0 - win_rate:.1f}%)")
        print(f"Gross P&L:                   Rs {total_gross:+,.2f}")
        print(f"Total Brokerage Charges:     Rs {total_charges:,.2f} ({len(trades)} trades x Rs {self.charges_per_trade:.2f})")
        print(f"NET P&L (After Charges):     Rs {net_pnl:+,.2f} ({roi_pct:+.2f}% ROI)")
        print(f"Final Capital:               Rs {final_cap:,.2f}")
        print(f"Peak Capital:                Rs {res['peak_capital']:,.2f}")
        print("=" * 95)

        if trades:
            print("\n--- CHRONOLOGICAL EXECUTED TRADES LEDGER ---")
            print(f"{'#':<3} | {'Entry':<8} | {'Exit':<8} | {'Dir':<4} | {'Setup Source':<20} | {'Symbol':<19} | {'Entry':<7} | {'Exit':<7} | {'Gain%':<6} | {'Reason':<25} | {'Net PnL':<8}")
            print("-" * 135)
            for t in trades:
                print(
                    f"{t.trade_id:02d}  | {t.entry_time_str:<8} | {t.exit_time_str:<8} | {t.direction:<4} | {t.source_name:<20} | {t.symbol:<19} | {t.entry_price:<7.2f} | {t.exit_price:<7.2f} | {t.exit_pct:+5.1f}% | {t.exit_reason:<25} | Rs{t.net_pnl_rs:+7.2f}"
                )
            print("-" * 135)
        else:
            print("\nZero trades executed (no setups passed 4-pillar filters and Confirm15).")
        print("=" * 95 + "\n")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="AEGIS V4.0 Backtester")
    parser.add_argument("--date", type=str, default="2026-09-15", help="Target date YYYY-MM-DD")
    parser.add_argument("--data-dir", type=str, default="/home/opc/AEGIS/data/l2_raw/Nifty", help="L2 data dir")
    parser.add_argument("--capital", type=float, default=2000.0, help="Initial capital")
    args = parser.parse_args()

    bt = AegisV4ParquetBacktester(
        data_dir=args.data_dir,
        date_str=args.date,
        start_capital=args.capital,
    )
    res = bt.run()
    bt.print_summary(res)


if __name__ == "__main__":
    main()
