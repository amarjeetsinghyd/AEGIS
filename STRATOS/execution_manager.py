"""
execution_manager.py — Dedicated Shoonya LIVE Execution Manager for STRATOS.
Punches real limit/marketable orders, manages fills, brackets SL/TP, and reconciles positions.
"""
from __future__ import annotations
import os
import sys
import json
import time as pytime
import logging
from datetime import datetime
from STRATOS.config import STRATOS_CONFIG
from common.logger import get_logger

logger = get_logger("stratos_execution")

class StratosExecutionManager:
    def __init__(self, broker=None, is_live: bool = True):
        self.broker = broker
        self.is_live = is_live
        self.cfg = STRATOS_CONFIG
        logger.info(f"[STRATOS_EXEC] Initialized in {'LIVE_BROKER' if is_live else 'PAPER_SIMULATION'} mode.")

    def place_order(self, symbol: str, side: str, qty: int, price: float, reason: str = "") -> dict:
        """
        Execute market/limit order to Shoonya broker API or simulate in paper mode.
        """
        ts = datetime.now().isoformat()
        if not self.is_live or not self.broker:
            logger.info(f"[STRATOS_PAPER] 📝 Simulated {side} {qty}x {symbol} @ {price:.2f} | Reason: {reason}")
            return {
                "status": "FILLED", "order_id": f"PAPER_{int(pytime.time())}",
                "symbol": symbol, "side": side, "qty": qty, "price": price,
                "timestamp": ts, "is_paper": True
            }

        # Real Live Broker Order Execution
        try:
            action = "B" if side == "BUY" else "S"
            tsym = f"{symbol}-EQ"
            kwargs = {
                "buy_or_sell": action, "product_type": "I", "exchange": "NSE",
                "tradingsymbol": tsym, "quantity": qty, "price_type": "LMT",
                "price": round(price, 2), "trigger_price": None, "discloseqty": 0, "retention": "DAY",
            }
            logger.info(f"[STRATOS_LIVE] 🚀 Punching LIVE {action} {qty}x {tsym} @ {price:.2f}...")
            api = getattr(self.broker, "api", self.broker)
            resp = api.place_order(**kwargs)
            logger.info(f"[STRATOS_LIVE] Broker response: {resp}")
            return {
                "status": "SENT", "order_id": str(resp.get("norenordno", "")),
                "symbol": symbol, "side": side, "qty": qty, "price": price,
                "timestamp": ts, "is_paper": False
            }
        except Exception as e:
            logger.error(f"[STRATOS_LIVE] ❌ Order placement error on {symbol}: {e}")
            return {"status": "ERROR", "error": str(e), "symbol": symbol}
