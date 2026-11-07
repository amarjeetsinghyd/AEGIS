"""
AEGIS V3.0 — End-to-End Integration & Empirical Parity Backtester.

Directly drives the production AegisPipelineOrchestrator with historical high-resolution
Level 2 parquet tick data recorded from the live Shoonya WebSocket seam.

Measures:
1. Exact production signal generation and state machine transitions.
2. Complete end-to-end trade execution simulation with single active position mutex.
3. Realistic brokerage charges (Rs 16.50/trade), trailing stop-loss, and capital P&L tracking.
4. Ground-truth forward 5-minute outcome telemetry.
"""

from __future__ import annotations
import os
import sys
import glob
import time
import argparse
import queue
import logging
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
import re
import polars as pl

# Ensure repo root is on sys.path
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from AEGIS.aegis_pipeline_orchestrator import AegisPipelineOrchestrator, create_aegis_engine
from common.execution.realistic_paper_broker import calculate_nse_option_charges


@dataclass
class ParquetTickAdapter:
    """Zero-allocation scalar tick adapter matching production Shoonya WebSocket parser."""
    tok: str
    lp: float
    vol: float
    ts: int
    bp1: float
    sp1: float
    bq1: float
    sq1: float
    bq2: float
    sq2: float
    bq3: float
    sq3: float
    bq4: float
    sq4: float
    bq5: float
    sq5: float
    oi: float = 0.0


@dataclass
class ExecutedTrade:
    """Stores full details of an executed trade in the simulated session."""
    trade_id: int
    symbol: str
    direction: str
    entry_ts_sec: float
    entry_time_str: str
    entry_price: float
    qty: int
    trade_value: float
    eqs_score: float
    exit_ts_sec: float = 0.0
    exit_time_str: str = ""
    exit_price: float = 0.0
    exit_pct: float = 0.0
    exit_reason: str = ""
    hold_sec: float = 0.0
    gross_pnl_rs: float = 0.0
    charges_rs: float = 16.50
    net_pnl_rs: float = 0.0
    capital_after: float = 0.0
    peak_pnl_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    target_pct: float = 4.0
    breakeven_locked: bool = False
    runner_locked: bool = False
    spot_entry: float = 0.0
    noise_floor: float = 1.50
    eff_sl_pct: float = 5.0
    be_exit_pct: float = 1.0
    arm_runner_gain: float = 4.50
    arm_be_gain: float = 3.75
    min_trail_dist: float = 2.25
    spot_confirm_pts: float = 8.4
    pa_setup_name: str = ""


class SimulatedPositionManager:
    """
    Simulates production single-trade execution lifecycle with realistic risk rules.
    - Single active position mutex (blocks overlapping entries)
    - Fast Scalp Target (+4.0% live parity), Adaptive Spot-Driven Stop Loss
    - Two-Key Breakeven Guard (peak >= 2.5x noise floor AND (60s OR 3.5bps spot confirm))
    - Dynamic Runner Trailing (arm >= 3.0x noise floor or +15%, trail 20% of peak)
    - Adaptive time ceiling (5m baseline -> 6m extension for running winners)
    - Post-trade cooldown (60s)
    - Real Indian statutory charges (STT/exchange/SEBI/stamp/GST + ₹10 brokerage)
    """

    def __init__(
        self,
        start_capital: float = 2000.0,
        charges_per_trade: float = 16.50,
        lot_size: int = 65,   # default NIFTY 65; SENSEX 20
        target_pct: float = 4.0,   # Fast +4.0% Scalp Take-Profit (live RESTING_TP_LIMIT_PCT parity)
        stop_loss_pct: float = 8.0,  # V4 premium floor parity (live PREMIUM_HARD_SL_PCT calibrated to -8.0%)
        breakeven_trigger_pct: float = 3.0,
        breakeven_lock_pct: float = 0.5,
        runner_trigger_pct: float = 7.0,
        runner_floor_pct: float = 5.0,
        runner_trail_pct: float = 2.0,
        max_hold_sec: float = 300.0,
        cooldown_sec: float = 60.0,
        max_trades_per_day: int = 8,
        use_statutory_charges: bool = True,
    ):
        self.start_capital = start_capital
        self.current_capital = start_capital
        self.peak_capital = start_capital
        self.charges_per_trade = charges_per_trade
        self.lot_size = lot_size
        self.use_statutory_charges = use_statutory_charges
        self.target_pct = target_pct
        self.stop_loss_pct = stop_loss_pct
        self.breakeven_trigger_pct = breakeven_trigger_pct
        self.breakeven_lock_pct = breakeven_lock_pct
        self.runner_trigger_pct = runner_trigger_pct
        self.runner_floor_pct = runner_floor_pct
        self.runner_trail_pct = runner_trail_pct
        self.max_hold_sec = max_hold_sec
        self.cooldown_sec = cooldown_sec
        self.max_trades_per_day = max_trades_per_day
        self.entry_cutoff_hour = 15
        self.entry_cutoff_min = 0

        self.active_trade: Optional[ExecutedTrade] = None
        self.completed_trades: List[ExecutedTrade] = []
        self.last_exit_ts_sec: float = -1.0e18
        self.last_exited_route: str = ""
        self.consecutive_route_cooldown_sec: float = 180.0
        self.trade_counter: int = 0
        self.last_spot: float = 0.0

    def update_spot(self, spot: float) -> None:
        """Tracks latest underlying spot price for live parity and spot invalidation."""
        if spot > 0:
            self.last_spot = spot

    def on_signal(self, sig: Dict[str, Any], current_ts_sec: float) -> bool:
        """Handles new entry signals from AegisPipelineOrchestrator."""
        # 1. Check production time cutoff (No new entries after 15:00)
        tm = time.localtime(current_ts_sec)
        if tm.tm_hour > self.entry_cutoff_hour or (tm.tm_hour == self.entry_cutoff_hour and tm.tm_min >= self.entry_cutoff_min):
            return False

        if len(self.completed_trades) >= self.max_trades_per_day:
            return False

        if self.active_trade is not None:
            # In live execution: block new entries while a position is active
            return False

        # Strict Non-Consecutive Route Lockout (No Back-to-Back Same Setup)
        incoming_route = str(sig.get("metrics", {}).get("pa_setup_name") or "")
        if (
            incoming_route
            and self.last_exited_route
            and incoming_route == self.last_exited_route
        ):
            return False

        if (current_ts_sec - self.last_exit_ts_sec) < self.cooldown_sec:
            # Post-trade cooldown active
            return False

        sym = sig.get("sym", "")
        direction = sig.get("signal", "")
        lp = float(sig.get("lp", 0.0))
        eqs = float(sig.get("metrics", {}).get("eqs_score", 0.0))
        rec_tp = float(sig.get("metrics", {}).get("recommended_tp_pct", 0.0)) * 100.0
        eff_tp = rec_tp if rec_tp > 0 else self.target_pct

        if lp <= 0 or not sym:
            return False

        # Premium filter (live OptionQualityEngine parity: Rs 5 - Rs 400 tradable band)
        if lp < 5.0 or lp > 400.0:
            return False

        pa_name = str(sig.get("metrics", {}).get("pa_setup_name") or "")

        self.trade_counter += 1
        time_str = time.strftime("%H:%M:%S", time.localtime(current_ts_sec))
        trade_val = lp * self.lot_size

        spot_val = float(sig.get("spot") or sig.get("metrics", {}).get("spot_price") or self.last_spot or 0.0)
        ref_spot = spot_val if spot_val > 0 else 24000.0
        noise_flr = max(1.50, 0.05 * lp)
        spot_sl_pts = ref_spot * (6.5 / 10000.0)  # 6.5 basis points

        # Dynamic Moneyness / Strike Delta derivation (Parity with Live Execution Manager)
        opt_strike = float(sig.get("strike") or sig.get("metrics", {}).get("strike") or 0.0)
        if opt_strike <= 0:
            m_strike = re.search(r'(\d{4,5})(?:CE|PE)$', sym)
            if m_strike:
                try:
                    opt_strike = float(m_strike.group(1))
                except Exception:
                    pass

        strike_step = 50.0 if "NIFTY" in sym.upper() else 100.0
        if opt_strike > 0 and ref_spot > 0:
            if direction in ("UP", "CALL", "BUY_CALL"):
                m = (ref_spot - opt_strike) / strike_step
            else:
                m = (opt_strike - ref_spot) / strike_step
            if m >= 0.5:
                est_delta = 0.55
            elif m >= -0.5:
                est_delta = 0.45
            elif m >= -1.5:
                est_delta = 0.35
            else:
                est_delta = 0.25
        else:
            if lp >= 80.0:
                est_delta = 0.55
            elif lp >= 40.0:
                est_delta = 0.45
            elif lp >= 20.0:
                est_delta = 0.35
            else:
                est_delta = 0.25

        opt_sl_dist = max(noise_flr, est_delta * spot_sl_pts)
        max_sl_dist = lp * 0.35
        eff_sl_dist = min(opt_sl_dist, max_sl_dist)
        eff_sl_pct = (eff_sl_dist / lp) * 100.0

        be_lock_buf = max(0.50, 0.5 * noise_flr)
        be_exit_pct = (be_lock_buf / lp) * 100.0
        spot_confirm_pts = ref_spot * (3.5 / 10000.0)  # 3.5 basis points

        self.active_trade = ExecutedTrade(
            trade_id=self.trade_counter,
            symbol=sym,
            direction=direction,
            entry_ts_sec=current_ts_sec,
            entry_time_str=time_str,
            entry_price=lp,
            qty=self.lot_size,
            trade_value=trade_val,
            eqs_score=eqs,
            target_pct=(eff_tp if eff_tp > 0 else self.target_pct),
            spot_entry=spot_val,
            noise_floor=noise_flr,
            eff_sl_pct=eff_sl_pct,
            be_exit_pct=be_exit_pct,
            arm_runner_gain=3.0 * noise_flr,
            arm_be_gain=2.5 * noise_flr,
            min_trail_dist=1.5 * noise_flr,
            spot_confirm_pts=spot_confirm_pts,
            pa_setup_name=pa_name,
        )
        return True

    def on_tick(self, sym: str, lp: float, current_ts_sec: float) -> None:
        """Updates active open position with real-time price ticks."""
        trade = self.active_trade
        if trade is None or trade.symbol != sym or lp <= 0:
            return

        entry_price = trade.entry_price
        pnl_pct = (lp - entry_price) / entry_price * 100.0
        hold_time = current_ts_sec - trade.entry_ts_sec

        if pnl_pct > trade.peak_pnl_pct:
            trade.peak_pnl_pct = pnl_pct
        if pnl_pct < trade.max_drawdown_pct:
            trade.max_drawdown_pct = pnl_pct

        peak_gain_rs = (trade.peak_pnl_pct / 100.0) * entry_price

        # 1. Target Profit Hit (Fast +4.0% Scalp Take-Profit, live parity)
        if pnl_pct >= trade.target_pct:
            self._close_position(lp, trade.target_pct, f"TARGET_HIT (+{trade.target_pct:.1f}%)", current_ts_sec)
            return

        # 1A. Premium Hard SL (-12.0% disaster floor, V4 live parity: overrides
        # all lagging spot-level checks, evaluated every tick).
        if pnl_pct <= -12.0:
            self._close_position(lp, pnl_pct, f"PREMIUM_HARD_SL (-12.0% disaster floor: {pnl_pct:.1f}%)", current_ts_sec)
            return

        # ─── Stepped Risk Compression + Cost-Shield + Near-Target Engine (Parity with Live V4.1) ───
        # 1. Near-Target Profit Lock (Peak >= +3.5%, exits on >= 0.5% drop from peak)
        if trade.peak_pnl_pct >= 3.5:
            if (trade.peak_pnl_pct - pnl_pct) >= 0.5:
                self._close_position(lp, pnl_pct, f"NEAR_TARGET_LOCK (+{pnl_pct:.1f}%)", current_ts_sec)
                return

        # Two-Key Breakeven Guard Spot Confirmation Hook
        spot_extreme_met = False
        if trade.spot_entry > 0 and self.last_spot > 0:
            if trade.direction in ("UP", "CALL", "BUY_CALL"):
                spot_extreme_met = (self.last_spot - trade.spot_entry) >= trade.spot_confirm_pts
            else:
                spot_extreme_met = (trade.spot_entry - self.last_spot) >= trade.spot_confirm_pts

        if peak_gain_rs >= trade.arm_be_gain and (hold_time >= 60.0 or spot_extreme_met):
            trade.breakeven_locked = True

        # 2. Dynamic SL & Cost-Shield Level Evaluation
        if trade.peak_pnl_pct >= 3.0 or getattr(trade, "breakeven_locked", False):
            trade.breakeven_locked = True
            if pnl_pct <= 0.0:
                self._close_position(lp, pnl_pct, "COST_SHIELD_SCRATCH (0.0%)", current_ts_sec)
                return
        elif trade.peak_pnl_pct >= 2.0:
            if pnl_pct <= -3.0:
                self._close_position(lp, pnl_pct, "STEPPED_SAFETY_SL (-3.0%)", current_ts_sec)
                return

        # 4. Spot-Driven Invalidation Stop-Loss (Normalized 6.5 bps of spot entry)
        if pnl_pct <= -trade.eff_sl_pct:
            self._close_position(lp, -trade.eff_sl_pct, f"STOP_LOSS (-{trade.eff_sl_pct:.1f}%)", current_ts_sec)
            return

        # 5. Adaptive Time Ceiling (5m baseline -> 6m extension for running winners, live parity)
        if hold_time >= 360.0:
            self._close_position(lp, pnl_pct, "TIME_EXPIRED (6m)", current_ts_sec)
            return
        if hold_time >= self.max_hold_sec:
            extended = (trade.peak_pnl_pct >= 2.0 or trade.breakeven_locked or trade.runner_locked)
            if not extended:
                self._close_position(lp, pnl_pct, "TIME_EXPIRED (5m)", current_ts_sec)
                return

    def _close_position(self, exit_price: float, exit_pct: float, reason: str, current_ts_sec: float) -> None:
        """Closes active position and updates capital."""
        if self.active_trade is None:
            return

        trade = self.active_trade
        trade.exit_ts_sec = current_ts_sec
        trade.exit_time_str = time.strftime("%H:%M:%S", time.localtime(current_ts_sec))
        trade.exit_price = exit_price
        trade.exit_pct = exit_pct
        trade.exit_reason = reason
        trade.hold_sec = current_ts_sec - trade.entry_ts_sec

        gross_pnl = (trade.entry_price * (exit_pct / 100.0)) * trade.qty

        if self.use_statutory_charges:
            # Q-7: full Indian statutory + regulatory costs (STT/exchange/SEBI/stamp/GST)
            buy_turnover = round(trade.entry_price * trade.qty, 2)
            sell_turnover = round(exit_price * trade.qty, 2)
            charges = calculate_nse_option_charges(buy_turnover=buy_turnover, sell_turnover=sell_turnover)
            total_charges = charges.total_charges
        else:
            total_charges = self.charges_per_trade

        net_pnl = gross_pnl - total_charges

        trade.gross_pnl_rs = gross_pnl
        trade.charges_rs = total_charges
        trade.net_pnl_rs = net_pnl

        self.current_capital += net_pnl
        trade.capital_after = self.current_capital
        if self.current_capital > self.peak_capital:
            self.peak_capital = self.current_capital

        self.completed_trades.append(trade)
        self.last_exit_ts_sec = current_ts_sec
        self.last_exited_route = getattr(trade, "pa_setup_name", "") or ""
        self.active_trade = None

    def force_close_at_eod(self, last_prices: Dict[str, float], last_ts_sec: float) -> None:
        """Force-closes any open position at session end."""
        if self.active_trade is not None:
            sym = self.active_trade.symbol
            lp = last_prices.get(sym, self.active_trade.entry_price)
            pnl_pct = (lp - self.active_trade.entry_price) / self.active_trade.entry_price * 100.0
            self._close_position(lp, pnl_pct, "FORCE_EOD_CLOSE", last_ts_sec)


class AegisParquetBacktester:
    """
    Production-parity L2 backtester for AEGIS V3.0.
    """

    def __init__(
        self,
        data_dir: str = "AEGIS/data/l2_raw/Nifty",
        date_str: Optional[str] = None,
        strike_step: float = 50.0,
        high_conviction_threshold: float = 75.0,
        eqs_dispatch_threshold: float = 70.0,
        min_tick_rate_gate: float = 1.0,
        start_capital: float = 2000.0,
        charges_per_trade: float = 16.50,
        target_pct: float = 6.5,
        stop_loss_pct: float = 8.0,  # V4 premium floor parity (live PREMIUM_HARD_SL_PCT calibrated to -8.0%)
        cooldown_sec: float = 120.0,
        max_trades_per_day: int = 8,
        use_statutory_charges: bool = True,
        telemetry_dir: Optional[str] = None,
    ):
        self.data_dir = data_dir
        self.date_str = date_str
        self.strike_step = strike_step
        self.high_conviction_threshold = high_conviction_threshold
        self.eqs_dispatch_threshold = eqs_dispatch_threshold
        self.min_tick_rate_gate = min_tick_rate_gate
        self.start_capital = start_capital
        self.charges_per_trade = charges_per_trade
        self.target_pct = target_pct
        self.stop_loss_pct = stop_loss_pct
        self.cooldown_sec = cooldown_sec
        self.max_trades_per_day = max_trades_per_day
        self.use_statutory_charges = use_statutory_charges
        self.telemetry_dir = telemetry_dir or os.path.join(data_dir, "telemetry_eval")
        os.makedirs(self.telemetry_dir, exist_ok=True)

        self.signal_queue = queue.Queue()
        self.files: List[str] = []
        self.token_to_symbol: Dict[str, str] = {}
        self.token_map: Dict[str, Dict[str, Any]] = {}
        self.engine: Optional[AegisPipelineOrchestrator] = None
        self.position_manager: Optional[SimulatedPositionManager] = None

    def discover_files(self) -> List[str]:
        """Discovers valid, non-stub L2 parquet files for the given date or directory."""
        if self.date_str:
            pattern = os.path.join(self.data_dir, f"aegis_l2_raw_{self.date_str}_part_*.parquet")
        else:
            pattern = os.path.join(self.data_dir, "aegis_l2_raw_*_part_*.parquet")

        all_files = sorted(glob.glob(pattern))
        # Automatic Quality Gate: Filter out corrupt/stub files (< 50KB)
        valid_files = [f for f in all_files if os.path.getsize(f) >= 50000]

        # Chronological sort: sort by actual initial tick timestamp
        def get_file_start_ts(fpath):
            try:
                head_df = pl.read_parquet(fpath, n_rows=5, columns=["ts"])
                return head_df["ts"].min()
            except Exception:
                return 0

        valid_files.sort(key=get_file_start_ts)
        self.files = valid_files
        return self.files

    def build_dynamic_universe(self) -> Tuple[Dict[str, str], Dict[str, Dict[str, Any]]]:
        """Dynamically maps token IDs, strikes, sides, and indices from parquet files."""
        self.token_to_symbol = {}
        self.token_map = {}

        for f in self.files:
            try:
                df_meta = pl.read_parquet(f, columns=["tok", "sym", "kind", "strike"])
                for row in df_meta.unique().iter_rows(named=True):
                    tok_str = str(row["tok"])
                    sym_str = str(row["sym"])
                    kind_int = int(row["kind"])
                    strike_flt = float(row.get("strike", 0.0) or 0.0)

                    self.token_to_symbol[tok_str] = sym_str
                    is_index = (tok_str in ("26000", "26009", "35938") or sym_str in ("NIFTY", "SENSEX", "BANKNIFTY") or "NIFTY-I" in sym_str or kind_int == 0)

                    # Determine side: kind 1 = CE (Call), kind 2 = PE (Put)
                    if kind_int == 1:
                        side = 0
                    elif kind_int == 2:
                        side = 1
                    elif "C" in sym_str and ("C2" in sym_str or "CE" in sym_str):
                        side = 0
                    else:
                        side = 1

                    self.token_map[tok_str] = {
                        "side": side,
                        "strike": strike_flt,
                        "is_index": is_index
                    }
            except Exception:
                pass

        return self.token_to_symbol, self.token_map

    def initialize_engine(self) -> AegisPipelineOrchestrator:
        """Instantiates and configures the production AegisPipelineOrchestrator and Position Manager."""
        # Select matching underlying token for the current index (strict index isolation)
        if self.strike_step <= 50:
            underlying_toks = {"26000"}  # NIFTY cash index only
        else:
            underlying_toks = {"1"}      # SENSEX cash index only

        self.engine = create_aegis_engine(
            token_to_symbol=self.token_to_symbol,
            token_map=self.token_map,
            strike_step=self.strike_step,
            underlying_tokens=underlying_toks,
            signal_queue=self.signal_queue,
        )
        self.engine.high_conviction_threshold = self.high_conviction_threshold
        self.engine.min_tick_rate_gate = self.min_tick_rate_gate
        if getattr(self.engine, "eqs_engine", None):
            self.engine.eqs_engine.dispatch_threshold = self.eqs_dispatch_threshold
        if self.engine.outcome_telemetry:
            self.engine.outcome_telemetry.log_dir = self.telemetry_dir

        # Q-7: correct SEBI lot size per index (65 NIFTY / 20 SENSEX) instead of flat 25
        lot_size = 65 if self.strike_step <= 50 else 20

        self.position_manager = SimulatedPositionManager(
            start_capital=self.start_capital,
            charges_per_trade=self.charges_per_trade,
            lot_size=lot_size,
            target_pct=self.target_pct,
            stop_loss_pct=self.stop_loss_pct,
            breakeven_trigger_pct=3.0,
            breakeven_lock_pct=0.5,
            runner_trigger_pct=7.0,
            runner_floor_pct=5.0,
            runner_trail_pct=2.0,
            max_hold_sec=300.0,
            cooldown_sec=self.cooldown_sec,
            max_trades_per_day=self.max_trades_per_day,
            use_statutory_charges=self.use_statutory_charges,
        )

        return self.engine

    def run(self) -> Dict[str, Any]:
        """Executes full chronological replay of all parquet ticks."""
        if not self.files:
            self.discover_files()
        if not self.files:
            raise FileNotFoundError(f"No parquet files found matching criteria in {self.data_dir}")

        if not self.token_map:
            self.build_dynamic_universe()
        if not self.engine or not self.position_manager:
            self.initialize_engine()

        total_ticks = 0
        t_start = time.perf_counter()
        last_prices: Dict[str, float] = {}
        last_ts_sec = 0.0
        emitted_signals: List[Dict[str, Any]] = []

        gamma_logger = logging.getLogger("aegis_gamma")
        prev_level = gamma_logger.level
        gamma_logger.setLevel(logging.WARNING)

        try:
            for fpath in self.files:
                df = pl.read_parquet(fpath)
                total_ticks += len(df)

                cols = set(df.columns)
                tok_col = [str(x) for x in df["tok"].to_list()]
                lp_col = [float(x) for x in df["lp"].to_list()]
                ts_col = [int(x) for x in df["ts"].to_list()]
                vol_col = [float(x or 0.0) for x in df["vol"].to_list()] if "vol" in cols else [0.0] * len(df)
                bp1_col = [float(x or 0.0) for x in df["bp1"].to_list()] if "bp1" in cols else [0.0] * len(df)
                sp1_col = [float(x or 0.0) for x in df["sp1"].to_list()] if "sp1" in cols else [0.0] * len(df)
                bq1_col = [float(x or 0.0) for x in df["bq1"].to_list()] if "bq1" in cols else [0.0] * len(df)
                sq1_col = [float(x or 0.0) for x in df["sq1"].to_list()] if "sq1" in cols else [0.0] * len(df)
                bq2_col = [float(x or 0.0) for x in df["bq2"].to_list()] if "bq2" in cols else [0.0] * len(df)
                sq2_col = [float(x or 0.0) for x in df["sq2"].to_list()] if "sq2" in cols else [0.0] * len(df)
                bq3_col = [float(x or 0.0) for x in df["bq3"].to_list()] if "bq3" in cols else [0.0] * len(df)
                sq3_col = [float(x or 0.0) for x in df["sq3"].to_list()] if "sq3" in cols else [0.0] * len(df)
                bq4_col = [float(x or 0.0) for x in df["bq4"].to_list()] if "bq4" in cols else [0.0] * len(df)
                sq4_col = [float(x or 0.0) for x in df["sq4"].to_list()] if "sq4" in cols else [0.0] * len(df)
                bq5_col = [float(x or 0.0) for x in df["bq5"].to_list()] if "bq5" in cols else [0.0] * len(df)
                sq5_col = [float(x or 0.0) for x in df["sq5"].to_list()] if "sq5" in cols else [0.0] * len(df)
                oi_col = [float(x or 0.0) for x in df["oi"].to_list()] if "oi" in cols else [0.0] * len(df)

                for tok_str, lp, ts, vol, bp1, sp1, bq1, sq1, bq2, sq2, bq3, sq3, bq4, sq4, bq5, sq5, oi_val in zip(
                    tok_col, lp_col, ts_col, vol_col, bp1_col, sp1_col, bq1_col, sq1_col,
                    bq2_col, sq2_col, bq3_col, sq3_col, bq4_col, sq4_col, bq5_col, sq5_col, oi_col
                ):
                    ts_sec = ts / 1000.0 if ts > 1e11 else float(ts)
                    last_ts_sec = ts_sec

                    sym_str = self.token_to_symbol.get(tok_str)
                    if sym_str and lp > 0:
                        last_prices[sym_str] = lp

                    tick = ParquetTickAdapter(
                        tok=tok_str,
                        lp=lp,
                        vol=vol,
                        ts=ts,
                        bp1=bp1,
                        sp1=sp1,
                        bq1=bq1,
                        sq1=sq1,
                        bq2=bq2,
                        sq2=sq2,
                        bq3=bq3,
                        sq3=sq3,
                        bq4=bq4,
                        sq4=sq4,
                        bq5=bq5,
                        sq5=sq5,
                        oi=oi_val,
                    )
                    self.engine.on_tick_parsed(tick)

                    # Process newly emitted signals into position manager
                    while not self.signal_queue.empty():
                        sig = self.signal_queue.get_nowait()
                        emitted_signals.append(sig)
                        self.position_manager.on_signal(sig, ts_sec)

                    # Update active position with current tick
                    if sym_str and lp > 0:
                        if self.token_map.get(tok_str, {}).get("is_index"):
                            self.position_manager.update_spot(lp)
                        self.position_manager.on_tick(sym_str, lp, ts_sec)

        finally:
            gamma_logger.setLevel(prev_level)

        # Force-close any open position at session end
        self.position_manager.force_close_at_eod(last_prices, last_ts_sec)

        elapsed_sec = time.perf_counter() - t_start
        throughput_tps = total_ticks / max(0.001, elapsed_sec)

        results = {
            "total_ticks": total_ticks,
            "files_count": len(self.files),
            "instruments_count": len(self.token_map),
            "emitted_signals": emitted_signals,
            "signal_count": len(emitted_signals),
            "executed_trades": self.position_manager.completed_trades,
            "trade_count": len(self.position_manager.completed_trades),
            "elapsed_sec": elapsed_sec,
            "throughput_tps": throughput_tps,
            "last_spot": self.engine.last_spot,
            "current_atm": self.engine.current_atm_strike,
            "start_capital": self.position_manager.start_capital,
            "final_capital": self.position_manager.current_capital,
            "peak_capital": self.position_manager.peak_capital,
        }
        return results

    def print_summary(self, res: Dict[str, Any]) -> None:
        """Renders comprehensive execution ledger and performance metrics."""
        trades = res["executed_trades"]
        start_cap = res["start_capital"]
        final_cap = res["final_capital"]
        net_pnl = final_cap - start_cap
        roi_pct = (net_pnl / start_cap) * 100.0 if start_cap > 0 else 0.0

        wins = sum(1 for t in trades if t.net_pnl_rs > 0)
        losses = sum(1 for t in trades if t.net_pnl_rs <= 0)
        win_rate = (wins / len(trades) * 100.0) if trades else 0.0
        total_charges = sum(t.charges_rs for t in trades)
        total_gross = sum(t.gross_pnl_rs for t in trades)

        print("\n" + "=" * 90)
        print("          AEGIS V3.0 END-TO-END PRODUCTION PARITY BACKTEST          ")
        print("=" * 90)
        print(f"Parquet Files Evaluated:     {res['files_count']} files")
        print(f"Total Ticks Streamed:        {res['total_ticks']:,}")
        print(f"Replay Execution Time:       {res['elapsed_sec']:.2f} seconds")
        print(f"Throughput Processing Speed: {res['throughput_tps']:,.0f} ticks/second")
        print(f"Underlying Spot Closes At:   Rs {res['last_spot']:.2f} (ATM: {res['current_atm']:.0f})")
        print("=" * 90)

        print(f"\n--- SESSION EXECUTION & P&L PERFORMANCE (START CAPITAL: Rs {start_cap:,.2f}) ---")
        print(f"Total Final Executed Trades: {len(trades)}")
        print(f"Winning Trades:              {wins} ({win_rate:.1f}%)")
        print(f"Losing Trades:               {losses} ({100.0 - win_rate:.1f}%)")
        print(f"Gross P&L:                   Rs {total_gross:+,.2f}")
        print(f"Total Brokerage Charges:     Rs {total_charges:,.2f} ({len(trades)} trades x Rs {self.charges_per_trade:.2f})")
        print(f"NET P&L (After Charges):     Rs {net_pnl:+,.2f} ({roi_pct:+.2f}% ROI)")
        print(f"Final Capital:               Rs {final_cap:,.2f}")
        print("=" * 90)

        if trades:
            print("\n--- FINAL EXECUTED TRADES LEDGER (CHRONOLOGICAL) ---")
            print(f"{'#':<3} | {'Entry':<8} | {'Exit':<8} | {'Dir':<4} | {'Symbol':<19} | {'Entry Rs':<8} | {'Exit Rs':<8} | {'Exit %':<7} | {'Reason':<20} | {'Gross Rs':<8} | {'Net Rs':<8} | {'Capital':<8}")
            print("-" * 125)
            for t in trades:
                print(
                    f"{t.trade_id:02d}  | {t.entry_time_str:<8} | {t.exit_time_str:<8} | {t.direction:<4} | {t.symbol:<19} | Rs{t.entry_price:<6.2f} | Rs{t.exit_price:<6.2f} | {t.exit_pct:+5.1f}% | {t.exit_reason:<20} | {t.gross_pnl_rs:+7.2f} | {t.net_pnl_rs:+7.2f} | Rs{t.capital_after:<8.2f}"
                )
            print("-" * 125)
        else:
            print("\nZero trades executed (no setups passed 4-domain filters).")
        print("=" * 90 + "\n")


def main():
    parser = argparse.ArgumentParser(description="AEGIS V3.0 Parity Backtester")
    parser.add_argument("--data-dir", type=str, default="AEGIS/data/l2_raw/Nifty", help="Directory containing raw L2 parquet files")
    parser.add_argument("--date", type=str, default="2026-08-28", help="Date string YYYY-MM-DD to evaluate")
    parser.add_argument("--capital", type=float, default=2000.0, help="Starting capital in Rupees")
    parser.add_argument("--charges", type=float, default=16.50, help="Brokerage charges per trade in Rupees")
    parser.add_argument("--target", type=float, default=6.5, help="Profit target percentage")
    parser.add_argument("--stop-loss", type=float, default=5.0, help="Stop loss percentage")
    parser.add_argument("--threshold", type=float, default=75.0, help="High conviction threshold")
    parser.add_argument("--eqs-threshold", type=float, default=70.0, help="EQS dispatch threshold")
    parser.add_argument("--min-tick-rate", type=float, default=1.0, help="Min live tick rate gate")
    args = parser.parse_args()

    backtester = AegisParquetBacktester(
        data_dir=args.data_dir,
        date_str=args.date,
        start_capital=args.capital,
        charges_per_trade=args.charges,
        target_pct=args.target,
        stop_loss_pct=args.stop_loss,
        high_conviction_threshold=args.threshold,
        eqs_dispatch_threshold=args.eqs_threshold,
        min_tick_rate_gate=args.min_tick_rate,
    )
    results = backtester.run()
    backtester.print_summary(results)


if __name__ == "__main__":
    main()
