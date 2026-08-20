"""
orchestrator.py — Master Strategy Engine for STRATOS.
Coordinates Pure Information-Time Turnover Buckets, Macro Trend, Structure Swings,
Pullback Volume Contraction, Order-Flow Absorption Trigger, and Live Execution.
"""
from __future__ import annotations
import time
from datetime import datetime
from STRATOS.config import STRATOS_CONFIG
from STRATOS.turnover_bucket_engine import TurnoverBucketEngine, TurnoverBucket
from STRATOS.structure_engine import MarketStructureEngine
from STRATOS.pullback_analyzer import PullbackAnalyzer
from STRATOS.flip_trigger import FlipTriggerEngine
from STRATOS.anti_hunt_risk import AntiHuntRiskEngine
from STRATOS.trade_manager import ActiveTradeManager
from STRATOS.execution_manager import StratosExecutionManager
from common.logger import get_logger

logger = get_logger("stratos_orchestrator")

class StratosOrchestrator:
    def __init__(self, symbols: list[str], broker=None, is_live: bool = True):
        self.symbols = symbols
        self.cfg = STRATOS_CONFIG
        
        # Subsystem Initializations
        self.bucket_engine = TurnoverBucketEngine(
            symbols=symbols,
            default_turnover_inr=self.cfg.get("turnover_bucket_inr", 20_000_000.0),
            max_history=self.cfg.get("max_history_buckets", 200)
        )
        self.structure = MarketStructureEngine(self.cfg)
        self.pullback = PullbackAnalyzer(self.cfg)
        self.trigger = FlipTriggerEngine(self.cfg)
        self.risk = AntiHuntRiskEngine(self.cfg)
        self.trades = ActiveTradeManager(self.cfg)
        self.executor = StratosExecutionManager(broker, is_live)
        
        self.active_capital = 50000.0  # Default margin capital base

    def on_tick(self, symbol: str, ts: float, price: float, cumulative_vol: float, is_buy: bool, order_flow: dict = None):
        """
        Hot-path event called on every incoming tick from WebSocket feed.
        """
        # 1. Feed tick to Information-Time Turnover Bucket Engine
        completed_bucket = self.bucket_engine.on_tick(symbol, ts, price, cumulative_vol, is_buy)
        
        # 2. Check active trade exits on every tick
        if symbol in self.trades.open_trades:
            arrays = self.bucket_engine.get_arrays(symbol)
            exit_signal = self.trades.evaluate_exits(symbol, price, arrays)
            if exit_signal:
                logger.info(f"[STRATOS_EXIT] 🛑 Triggered exit on {symbol} @ {price:.2f} | Reason: {exit_signal['reason']}")
                self.executor.place_order(symbol, "SELL", self.trades.open_trades[symbol]["qty"], price, exit_signal["reason"])
                self.trades.close_trade(symbol, exit_signal)
            return

        # 3. Only evaluate new structural entries when a Turnover Bucket completes
        if not completed_bucket:
            return

        cur_time = datetime.now().time()
        if cur_time < self.cfg.get("entry_start_time", time(9, 20)) or cur_time > self.cfg.get("entry_cutoff_time", time(14, 30)):
            return

        # Gate: Max open positions & Daily trade limits
        if self.trades.get_open_count() >= self.cfg["max_open_positions"]:
            return
        if self.trades.get_today_trades_count() >= self.cfg["max_trades_per_day"]:
            return

        arrays = self.bucket_engine.get_arrays(symbol)

        # ─── PHASE 1: Macro Trend Filter across Turnover Buckets ───
        macro = self.structure.check_macro_trend(arrays)
        if macro["trend"] != "BULLISH":
            return

        # ─── PHASE 2: Market Structure Swings (Turnover Waves) ───
        struct = self.structure.update_structure(symbol, arrays)
        if struct["status"] != "AWAITING_RETEST":
            return

        support_line = struct["support_level"]
        breakout_vol = self.structure.breakout_bucket.get(symbol, {}).get("volume", 1000.0)

        # ─── PHASE 3: Pullback & Volume Contraction Analysis ───
        pb_res = self.pullback.analyze_pullback_quality(arrays, support_line, breakout_vol)
        if not pb_res["valid"]:
            return

        # ─── PHASE 4: Order-Flow Absorption & Delta Trigger ───
        trig = self.trigger.evaluate_trigger(arrays, support_line, order_flow)
        if not trig["triggered"]:
            return

        # ─── PHASE 5: Anti-Hunt Risk Levels & Execution ───
        levels = self.risk.calculate_trade_levels(price, support_line, pb_res["lowest_wick"], arrays)
        qty = self.risk.calculate_position_size(self.active_capital, price, levels["risk_dist"])
        if qty <= 0:
            return

        logger.info(
            f"[STRATOS_ENTRY] 🎯 INSTITUTIONAL SETUP! {symbol} BUY @ {price:.2f} | "
            f"SL: {levels['stop_loss']:.2f} | TP1: {levels['tp1_price']:.2f} | "
            f"Qty: {qty} | Risk: {levels['risk_pct']:.2f}% | Reason: {trig['reason']}"
        )

        order_res = self.executor.place_order(symbol, "BUY", qty, price, trig["reason"])
        trade_record = {
            "symbol": symbol, "entry_price": price, "qty": qty,
            "stop_loss": levels["stop_loss"], "tp1_price": levels["tp1_price"],
            "tp2_price": levels["tp2_price"], "risk_dist": levels["risk_dist"],
            "support_level": support_line, "entry_time": datetime.now().isoformat(),
            "highest_price": price, "order_info": order_res
        }
        self.trades.register_trade(symbol, trade_record)
