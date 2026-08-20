"""
orchestrator.py — Master Two-Way Strategy Engine for STRATOS.
Coordinates:
- Macro Trend (1H)
- 15m Market Structure (Long HH2 Breakout Support Floor vs Short LL2 Breakdown Resistance Ceiling)
- 5m Pullback Volume Contraction & Order-Flow Absorption/Rejection Triggers
- Anti-Hunt SL & Adaptive Trailing Exits
- Real-Time Live / Paper Broker Order Execution
"""
from __future__ import annotations
import time
from datetime import datetime
from STRATOS.config import STRATOS_CONFIG
from STRATOS.turnover_bucket_engine import TurnoverBucketEngine
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
        
        self.active_capital = 50000.0

    def on_tick(self, symbol: str, ts: float, price: float, cumulative_vol: float, is_buy: bool, order_flow: dict = None):
        """
        Hot-path event called on every incoming tick from WebSocket feed.
        """
        # 1. Update turnover buckets
        completed_bucket = self.bucket_engine.on_tick(symbol, ts, price, cumulative_vol, is_buy)
        
        # 2. Check active trade exits on every tick
        if symbol in self.trades.open_trades:
            trade = self.trades.open_trades[symbol]
            arrays = self.bucket_engine.get_arrays(symbol)
            exit_signal = self.trades.evaluate_exits(symbol, price, arrays)
            if exit_signal:
                exit_side = "SELL" if trade.get("side", "BUY") == "BUY" else "BUY"
                logger.info(f"[STRATOS_EXIT] 🛑 Triggered {exit_side} on {symbol} @ {price:.2f} | Reason: {exit_signal['reason']}")
                self.executor.place_order(symbol, exit_side, trade["qty"], price, exit_signal["reason"])
                self.trades.close_trade(symbol, exit_signal)
            return

        # 3. Only evaluate new entries when a bar/bucket completes
        if not completed_bucket:
            return

        cur_time = datetime.now().time()
        if cur_time < self.cfg.get("entry_start_time", time(9, 20)) or cur_time > self.cfg.get("entry_cutoff_time", time(14, 30)):
            return

        if self.trades.get_open_count() >= self.cfg["max_open_positions"]:
            return
        if self.trades.get_today_trades_count() >= self.cfg["max_trades_per_day"]:
            return

        arrays = self.bucket_engine.get_arrays(symbol)

        # ─── PHASE 1: Macro Trend Filter ───
        macro = self.structure.check_macro_trend(arrays)
        trend = macro["trend"]
        if trend not in {"BULLISH", "BEARISH"}:
            return

        # ─── PHASE 2: Market Structure Swings ───
        struct = self.structure.update_structure(symbol, arrays)
        status = struct.get("status")

        # ─── LONG SETUP (BULLISH) ───
        if trend == "BULLISH" and status == "AWAITING_LONG_RETEST":
            support_line = struct["support_level"]
            breakout_vol = self.structure.breakout_bucket.get(symbol, {}).get("volume", 1000.0)

            # Pullback Quality
            pb_res = self.pullback.analyze_pullback_quality(arrays, support_line, breakout_vol, side="BUY")
            if not pb_res["valid"]:
                return

            # Order Flow Absorption Trigger
            trig = self.trigger.evaluate_trigger(arrays, support_line, side="BUY", live_order_flow=order_flow)
            if not trig["triggered"]:
                return

            # Risk & Sizing
            levels = self.risk.calculate_trade_levels(price, support_line, pb_res["lowest_wick"], arrays, side="BUY")
            qty = self.risk.calculate_position_size(self.active_capital, price, levels["risk_dist"])
            if qty <= 0:
                return

            logger.info(
                f"[STRATOS_ENTRY] 🎯 LONG SETUP! {symbol} BUY @ {price:.2f} | "
                f"SL: {levels['stop_loss']:.2f} | TP1: {levels['tp1_price']:.2f} | "
                f"Qty: {qty} | Risk: {levels['risk_pct']:.2f}% | Reason: {trig['reason']}"
            )

            order_res = self.executor.place_order(symbol, "BUY", qty, price, trig["reason"])
            trade_record = {
                "symbol": symbol, "side": "BUY", "entry_price": price, "qty": qty,
                "stop_loss": levels["stop_loss"], "tp1_price": levels["tp1_price"],
                "tp2_price": levels["tp2_price"], "risk_dist": levels["risk_dist"],
                "support_level": support_line, "entry_time": datetime.now().isoformat(),
                "highest_price": price, "order_info": order_res
            }
            self.trades.register_trade(symbol, trade_record)

        # ─── SHORT SETUP (BEARISH) ───
        elif trend == "BEARISH" and status == "AWAITING_SHORT_RETEST":
            resistance_line = struct["resistance_level"]
            breakdown_vol = self.structure.breakdown_bucket.get(symbol, {}).get("volume", 1000.0)

            # Pullback Quality
            pb_res = self.pullback.analyze_pullback_quality(arrays, resistance_line, breakdown_vol, side="SELL")
            if not pb_res["valid"]:
                return

            # Order Flow Rejection Trigger
            trig = self.trigger.evaluate_trigger(arrays, resistance_line, side="SELL", live_order_flow=order_flow)
            if not trig["triggered"]:
                return

            # Risk & Sizing
            levels = self.risk.calculate_trade_levels(price, resistance_line, pb_res["highest_wick"], arrays, side="SELL")
            qty = self.risk.calculate_position_size(self.active_capital, price, levels["risk_dist"])
            if qty <= 0:
                return

            logger.info(
                f"[STRATOS_ENTRY] 🎯 SHORT SETUP! {symbol} SELL @ {price:.2f} | "
                f"SL: {levels['stop_loss']:.2f} | TP1: {levels['tp1_price']:.2f} | "
                f"Qty: {qty} | Risk: {levels['risk_pct']:.2f}% | Reason: {trig['reason']}"
            )

            order_res = self.executor.place_order(symbol, "SELL", qty, price, trig["reason"])
            trade_record = {
                "symbol": symbol, "side": "SELL", "entry_price": price, "qty": qty,
                "stop_loss": levels["stop_loss"], "tp1_price": levels["tp1_price"],
                "tp2_price": levels["tp2_price"], "risk_dist": levels["risk_dist"],
                "resistance_level": resistance_line, "entry_time": datetime.now().isoformat(),
                "lowest_price": price, "order_info": order_res
            }
            self.trades.register_trade(symbol, trade_record)
