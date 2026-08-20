"""
orchestrator.py — Master Strategy Engine for STRATOS.
Coordinates Multi-Timeframe Resampling, 1H Macro Trend, 15m Structure Swings,
5m Pullback Volume Contraction, TA-Lib Reversal Candlesticks, and Live Execution.
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
        
        # Subsystem Initializations
        self.resampler = MultiTimeframeResampler(symbols)
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
        # 1. Update multi-timeframe candles
        events = self.resampler.on_tick(symbol, ts, price, cumulative_vol, is_buy)
        
        # 2. Check active trade exits on tick
        if symbol in self.trades.open_trades:
            arrays_5m = self.resampler.get_arrays(symbol, "5m")
            exit_signal = self.trades.evaluate_exits(symbol, price, arrays_5m)
            if exit_signal:
                logger.info(f"[STRATOS_EXIT] 🛑 Triggered exit on {symbol} @ {price:.2f} | Reason: {exit_signal['reason']}")
                self.executor.place_order(symbol, "SELL", self.trades.open_trades[symbol]["qty"], price, exit_signal["reason"])
                self.trades.close_trade(symbol, exit_signal)
            return

        # 3. Only evaluate new entries on 5-minute candle close
        if not events["closed_5m"]:
            return

        # Gate: Max open positions & Daily trade limits
        if self.trades.get_open_count() >= self.cfg["max_open_positions"]:
            return
        if self.trades.get_today_trades_count() >= self.cfg["max_trades_per_day"]:
            return

        # ─── PHASE 1: Macro Trend Filter (1-Hour) ───
        arrays_1h = self.resampler.get_arrays(symbol, "1h")
        macro = self.structure.check_macro_trend_1h(arrays_1h)
        if macro["trend"] != "BULLISH":
            return

        # ─── PHASE 2: Market Structure Swings (15-Minute) ───
        arrays_15m = self.resampler.get_arrays(symbol, "15m")
        arrays_5m = self.resampler.get_arrays(symbol, "5m")
        struct = self.structure.update_structure(symbol, arrays_15m, arrays_5m)
        if struct["status"] != "AWAITING_RETEST":
            return

        support_line = struct["support_level"]
        breakout_vol = self.structure.breakout_candle.get(symbol, {}).get("volume", 1000.0)

        # ─── PHASE 3: Pullback & Volume Contraction Analysis ───
        pb_res = self.pullback.analyze_pullback_quality(arrays_5m, support_line, breakout_vol)
        if not pb_res["valid"]:
            return

        # ─── PHASE 4: Candlestick, 9-EMA & Order-Flow Trigger ───
        trig = self.trigger.evaluate_trigger(arrays_5m, support_line, order_flow)
        if not trig["triggered"]:
            return

        # ─── PHASE 5: Anti-Hunt Risk Levels & Execution ───
        levels = self.risk.calculate_trade_levels(price, support_line, pb_res["lowest_wick"], arrays_5m)
        qty = self.risk.calculate_position_size(self.active_capital, price, levels["risk_dist"])
        if qty <= 0:
            return

        logger.info(
            f"[STRATOS_ENTRY] 🎯 HIGH-CONVICTION SETUP! {symbol} BUY @ {price:.2f} | "
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
