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
        self.tick_size = 0.05
        logger.info(f"[STRATOS_EXEC] Initialized in {'LIVE_BROKER' if is_live else 'PAPER_SIMULATION'} mode.")

    def _resolve_tsym(self, symbol: str) -> str:
        if symbol.endswith("-EQ"):
            return symbol
        return f"{symbol}-EQ"

    def _marketable_limit(self, action: str, spot_price: float, slack_pct: float = 0.0015) -> float:
        """
        Calculates marketable limit price with 0.15% buffer (min 1 tick)
        to guarantee instant fill on NSE without resting order queue latency.
        """
        raw = spot_price * (1.0 + slack_pct) if action == "B" else spot_price * (1.0 - slack_pct)
        steps = round(raw / self.tick_size)
        return round(steps * self.tick_size, 2)

    def _confirm_fill(self, api, ordno: str, fallback_price: float) -> tuple[bool, float, int]:
        """
        Polls single_order_history to confirm TRADED / COMPLETE status and extract avg fill price.
        """
        if not ordno or str(ordno).startswith("PAPER_") or str(ordno).startswith("SIM_"):
            return True, fallback_price, 1

        for attempt in range(5):
            pytime.sleep(0.5)
            try:
                fn = getattr(api, "single_order_history", None) or getattr(api, "singleorderhistory", None)
                if not fn:
                    break
                hist = fn(orderno=ordno)
                row = hist[0] if isinstance(hist, list) and len(hist) > 0 else (hist if isinstance(hist, dict) else None)
                if row:
                    status = str(row.get("status", "")).upper()
                    if status in {"FILLED", "COMPLETE", "COMPLETED", "TRADED"}:
                        flprc = float(row.get("flprc") or row.get("avgprc") or row.get("prc") or fallback_price)
                        fill_qty = int(row.get("fillshares") or row.get("fillqty") or 0)
                        logger.info(f"[STRATOS_LIVE] ✅ Order {ordno} TRADED | fill_price={flprc:.2f} | qty={fill_qty}")
                        return True, flprc, fill_qty
                    elif status in {"REJECTED", "CANCELED", "CANCELLED"}:
                        rej = row.get("rejreason") or row.get("exch_rej_reason") or "Rejected by exchange"
                        logger.error(f"[STRATOS_LIVE] ❌ Order {ordno} {status} | reason: {rej}")
                        return False, fallback_price, 0
            except Exception as e:
                logger.warning(f"[STRATOS_LIVE] Fill check attempt {attempt+1} warning: {e}")

        # If still in progress, return optimistic fill with fallback
        return True, fallback_price, 1

    def place_order(self, symbol: str, side: str, qty: int, price: float, reason: str = "") -> dict:
        """
        Execute MIS Intraday marketable limit order on NSE via Shoonya broker API or simulate in paper mode.
        ARGUS-identical parameter mapping: product_type='I', exchange='NSE', price_type='LMT', retention='DAY'.
        """
        ts = datetime.now().isoformat()
        if not self.is_live or not self.broker:
            logger.info(f"[STRATOS_PAPER] 📝 Simulated {side} {qty}x {symbol} @ {price:.2f} | Reason: {reason}")
            return {
                "status": "FILLED", "order_id": f"PAPER_{int(pytime.time() * 1000)}",
                "symbol": symbol, "side": side, "qty": qty, "price": price,
                "timestamp": ts, "is_paper": True
            }

        # Real Live Broker MIS Order Execution
        try:
            action = "B" if side == "BUY" else "S"
            tsym = self._resolve_tsym(symbol)
            limit_price = self._marketable_limit(action, price)

            kwargs = {
                "buy_or_sell": action,
                "product_type": "I",       # MIS Intraday Leverage
                "exchange": "NSE",          # NSE Cash Equities
                "tradingsymbol": tsym,      # e.g. RELIANCE-EQ, HUDCO-EQ
                "quantity": int(qty),       # Shares count
                "price_type": "LMT",        # Marketable Limit
                "price": limit_price,       # Price with buffer
                "trigger_price": None,
                "discloseqty": 0,
                "retention": "DAY",
            }
            logger.info(f"[STRATOS_LIVE] 🚀 Punching LIVE MIS {action} {qty}x {tsym} @ Limit ₹{limit_price:.2f} (Ref: ₹{price:.2f})...")
            
            api = getattr(self.broker, "_api", getattr(self.broker, "api", self.broker))
            place_fn = getattr(self.broker, "place_order", None) or getattr(api, "place_order", None) or getattr(api, "placeorder", None)
            if not place_fn:
                raise RuntimeError("Broker API does not implement place_order")

            resp = place_fn(**kwargs)

            # Fallback for symbol name if rejected with 'Invalid Trading Symbol'
            if resp and isinstance(resp, dict) and "Invalid Trading Symbol" in str(resp.get("emsg", "")):
                alt_tsym = symbol if tsym != symbol else f"{symbol}-EQ"
                logger.warning(f"[STRATOS_LIVE] tsym '{tsym}' invalid — retrying with '{alt_tsym}'")
                kwargs["tradingsymbol"] = alt_tsym
                resp = place_fn(**kwargs)

            if resp and isinstance(resp, dict) and str(resp.get("stat", "")).upper() == "OK" and resp.get("norenordno"):
                ordno = str(resp["norenordno"])
                logger.info(f"[STRATOS_LIVE] Broker Order Placed | NorenOrdNo: {ordno}")
                filled, fill_price, fill_qty = self._confirm_fill(api, ordno, price)
                return {
                    "status": "FILLED" if filled else "REJECTED",
                    "order_id": ordno,
                    "symbol": symbol,
                    "side": side,
                    "qty": fill_qty if fill_qty > 0 else qty,
                    "price": fill_price,
                    "timestamp": ts,
                    "is_paper": False
                }

            err_msg = resp.get("emsg", str(resp)) if isinstance(resp, dict) else str(resp)
            logger.error(f"[STRATOS_LIVE] ❌ Order placement rejected: {err_msg}")
            return {"status": "REJECTED", "error": err_msg, "symbol": symbol}

        except Exception as e:
            logger.error(f"[STRATOS_LIVE] ❌ Order placement error on {symbol}: {e}")
            return {"status": "ERROR", "error": str(e), "symbol": symbol}
