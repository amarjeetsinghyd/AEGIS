"""
orchestrator.py — Master Strategy Engine for STRATOS (Pure 15-Minute Architecture).
Coordinates:
- Multi-Timeframe Resampling (1H Macro, 15m Canonical Structure, 5m Trigger)
- 15m Market Structure (Long HH2 Breakout Support Zone vs Short LL2 Breakdown Resistance Zone)
- Order-Flow Absorption / Rejection Zone Triggers
- Anti-Liquidity Hunting SL & 1:4 RR Runner / 15m Trailing Exits
- Real-Time Live / Paper Broker Order Execution
"""
from __future__ import annotations
import time
from datetime import datetime
from STRATOS.config import STRATOS_CONFIG
from STRATOS.candle_resampler import MultiTimeframeResampler
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
        
        # Pure 15m / 5m / 1H Multi-Timeframe Resampler
        self.resampler = MultiTimeframeResampler(symbols)
        self.structure = MarketStructureEngine(self.cfg)
        self.pullback = PullbackAnalyzer(self.cfg)
        self.trigger = FlipTriggerEngine(self.cfg)
        self.risk = AntiHuntRiskEngine(self.cfg)
        self.trades = ActiveTradeManager(self.cfg)
        self.executor = StratosExecutionManager(broker, is_live)
        
        self.active_capital = 50000.0

    def on_tick(self, symbol: str, ts: float, price: float, cumulative_vol: float, is_buy: bool, order_flow: dict = None):
        """
        Hot-path event called on every incoming tick from WebSocket/REST API feed.
        """
        # 1. Update Multi-Timeframe Candles (1H, 15m, 5m)
        events = self.resampler.on_tick(symbol, ts, price, cumulative_vol, is_buy)
        
        # 2. Check active trade exits on every tick
        if symbol in self.trades.open_trades:
            trade = self.trades.open_trades[symbol]
            arrays_15m = self.resampler.get_arrays(symbol, "15m")
            exit_signal = self.trades.evaluate_exits(symbol, price, arrays_15m)
            if exit_signal:
                exit_side = "SELL" if trade.get("side", "BUY") == "BUY" else "BUY"
                logger.info(f"[STRATOS_EXIT] 🛑 Triggered {exit_side} on {symbol} @ {price:.2f} | Reason: {exit_signal['reason']}")
                self.executor.place_order(symbol, exit_side, trade["qty"], price, exit_signal["reason"])
                self.trades.close_trade(symbol, exit_signal)
                self.structure.state[symbol] = "IDLE"
            return

        # 3. Time Window Guard
        cur_time = datetime.now().time()
        if cur_time < self.cfg.get("entry_start_time", time(9, 20)) or cur_time > self.cfg.get("entry_cutoff_time", time(15, 0)):
            return

        # 4. Global Mutex & Max Daily Trades
        if self.trades.get_open_count() >= self.cfg["max_open_positions"]:
            return
        if self.trades.get_today_trades_count() >= self.cfg["max_trades_per_day"]:
            return

        arrays_15m = self.resampler.get_arrays(symbol, "15m")
        if not arrays_15m or arrays_15m["count"] < 4:
            return

        arrays_1h = self.resampler.get_arrays(symbol, "1h") or arrays_15m

        # ─── PHASE 1: Macro Trend Filter (1H) ───
        macro = self.structure.check_macro_trend(arrays_1h)
        trend = macro["trend"]
        if trend not in {"BULLISH", "BEARISH"}:
            return

        # ─── PHASE 2: 15m Market Structure Swings (HH1->HH2 / LL1->LL2) ───
        struct = self.structure.update_structure(symbol, arrays_15m)
        status = struct.get("status")

        # ─── LONG SETUP (BULLISH) ───
        if trend == "BULLISH" and status == "AWAITING_LONG_RETEST":
            support_line = struct["support_level"]
            breakout_vol = self.structure.breakout_bucket.get(symbol, {}).get("volume", 1000.0)

            # Pullback Quality on 15m
            pb_res = self.pullback.analyze_pullback_quality(arrays_15m, support_line, breakout_vol, side="BUY")
            if not pb_res["valid"]:
                return

            # Order Flow Absorption Reversal Trigger
            trig = self.trigger.evaluate_trigger(arrays_15m, support_line, side="BUY", live_order_flow=order_flow)
            if not trig["triggered"]:
                return

            # Risk & Sizing (1:4 RR Runner)
            levels = self.risk.calculate_trade_levels(price, support_line, pb_res["lowest_wick"], arrays_15m, side="BUY")
            qty = self.risk.calculate_position_size(self.active_capital, price, levels["risk_dist"])
            if qty <= 0:
                return

            logger.info(
                f"[STRATOS_ENTRY] 🎯 15m LONG SETUP! {symbol} BUY @ {price:.2f} | "
                f"SL: {levels['stop_loss']:.2f} | TP2 (1:4 RR): {levels['tp2_price']:.2f} | "
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
            self.structure.state[symbol] = "IN_TRADE"
            self.trades.register_trade(symbol, trade_record)

        # ─── SHORT SETUP (BEARISH) ───
        elif trend == "BEARISH" and status == "AWAITING_SHORT_RETEST":
            resistance_line = struct["resistance_level"]
            breakdown_vol = self.structure.breakdown_bucket.get(symbol, {}).get("volume", 1000.0)

            # Pullback Quality on 15m
            pb_res = self.pullback.analyze_pullback_quality(arrays_15m, resistance_line, breakdown_vol, side="SELL")
            if not pb_res["valid"]:
                return

            # Order Flow Rejection Trigger
            trig = self.trigger.evaluate_trigger(arrays_15m, resistance_line, side="SELL", live_order_flow=order_flow)
            if not trig["triggered"]:
                return

            # Risk & Sizing (1:4 RR Runner)
            levels = self.risk.calculate_trade_levels(price, resistance_line, pb_res["highest_wick"], arrays_15m, side="SELL")
            qty = self.risk.calculate_position_size(self.active_capital, price, levels["risk_dist"])
            if qty <= 0:
                return

            logger.info(
                f"[STRATOS_ENTRY] 🎯 15m SHORT SETUP! {symbol} SELL @ {price:.2f} | "
                f"SL: {levels['stop_loss']:.2f} | TP2 (1:4 RR): {levels['tp2_price']:.2f} | "
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
            self.structure.state[symbol] = "IN_TRADE"
            self.trades.register_trade(symbol, trade_record)
