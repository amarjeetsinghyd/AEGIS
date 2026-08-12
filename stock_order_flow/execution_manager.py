"""LIVE execution — v9.7. 5x full leverage, ATR15 TP/SL + Chandelier trail,
zone-capped take-profit (tp_dist), 5% daily loss cap on day-start snapshot,
calibrated transaction costs, robust API resolution, Noren tsym handling,
tick-aligned marketable LMT orders, TRADED status recognition, rejection logging,
pre-square-off exits (14:55), retry cap + broker position reconciliation.
v9.2: MAE/MFE attribution (Q4) + slippage/latency logging (Q5).
v9.3: Q6 consecutive-loss circuit breaker (3 full SLs -> pause day).
v9.4: MARGIN_SAFETY 0.90 on qty calc + auto-retry with 20% smaller qty on
broker 'Margin Shortfall' rejection (thin-capital fix, 2026-08-12).
v9.5: MARGIN_SAFETY reduced to 0.75 (broker margin buffer headroom).
v9.6: TICK_SIZE 0.05 -> 0.10 (universal NSE alignment; 0.10 is a multiple of
0.05 hence valid for ALL equity tick classes — fixes SBIN ORA reject loop).
v9.7: BREAKEVEN LOCK — once price runs 0.5*ATR in favor, the stop is floored
at entry; such exits book as BREAKEVEN_EXIT for attribution. Kills the
MFE->loss giveaway pattern (nightly MAE/MFE evidence 2026-08-12)."""
from __future__ import annotations

import json
import math
import os
import time as pytime
from datetime import datetime, time

from common.config import get_data_dir
from common.execution.capital_engine import CapitalEngine
from common.logger import get_logger

logger = get_logger("stock_execution")

TRADES_DIR = os.path.join(get_data_dir(), "trades")
os.makedirs(TRADES_DIR, exist_ok=True)
OPEN_PATH = os.path.join(TRADES_DIR, "open_positions.json")
HISTORY_PATH = os.path.join(TRADES_DIR, "trade_history.json")

MAX_OPEN_POSITIONS = 1
LEVERAGE = 5.0
MARGIN_SAFETY = 0.75          # v9.5
TP_ATR_MULT = 1.0
SL_ATR_MULT = 1.0
CHANDELIER_ATR_MULT = 1.0
BREAKEVEN_ATR_FRACTION = 0.5  # v9.7: profit >= 0.5*ATR -> stop floored at entry
FALLBACK_TP_PCT = 0.010
FALLBACK_SL_PCT = 0.008
DAILY_LOSS_CAP_PCT = 0.05
MAX_HOLD_MINUTES = 60
MKT_SLACK_PCT = 0.010
TICK_SIZE = 0.10              # v9.6
EOD_FLATTEN_TIME = time(14, 55)
MAX_EXIT_ATTEMPTS = 3
CONSEC_SL_LIMIT = 3           # v9.3 (Q6)


def _resolve_api(broker):
    """Find the object that can actually place orders (NorenApi or adapter)."""
    candidates = []
    sp = getattr(broker, "session_provider", None)
    for base in (broker, sp):
        if base is None:
            continue
        for attr in ("_api", "api"):
            cand = getattr(base, attr, None)
            if cand is not None:
                candidates.append(cand)
    candidates.append(broker)
    for cand in candidates:
        if hasattr(cand, "place_order") or hasattr(cand, "placeorder"):
            return cand
    return candidates[0]


class StockExecutionManager:
    def __init__(self, broker):
        self.broker = broker
        self.api = _resolve_api(broker)
        logger.info(f"[StockExecution] API resolved: {type(self.api).__name__}")
        self.open_trades = {}
        self.leverage = LEVERAGE
        self._tsym = {}
        self.capital_engine = CapitalEngine(mode="LIVE", portfolio_provider=self.broker, data_dir=get_data_dir())
        self._load_open()
        self._day_key = None
        self._day_start_margin = 0.0
        self._day_loss = 0.0
        self._consec_sl = 0        # v9.3 (Q6)
        self._day_paused = False   # v9.3 (Q6)
        self._last_rej = ""        # v9.4 (margin retry)

    def set_tsym_map(self, symbol_to_tsym: dict):
        self._tsym = {str(k): str(v) for k, v in symbol_to_tsym.items()}

    def _load_open(self):
        try:
            if os.path.exists(OPEN_PATH):
                with open(OPEN_PATH, "r") as f:
                    self.open_trades = json.load(f)
        except Exception as e:
            logger.warning(f"open_positions load failed: {e}")

    def _save_open(self):
        try:
            with open(OPEN_PATH, "w") as f:
                json.dump(self.open_trades, f, indent=2, default=str)
        except Exception as e:
            logger.warning(f"open_positions save failed: {e}")

    def _save_history(self, trade):
        try:
            history = []
            if os.path.exists(HISTORY_PATH):
                with open(HISTORY_PATH, "r") as f:
                    history = json.load(f)
            history.append(trade)
            with open(HISTORY_PATH, "w") as f:
                json.dump(history, f, indent=2, default=str)
        except Exception as e:
            logger.warning(f"trade_history save failed: {e}")

    def _ensure_day_snapshot(self):
        _today = datetime.now().date()
        if self._day_key != _today:
            self._day_key = _today
            self._day_loss = 0.0
            self._consec_sl = 0          # v9.3 (Q6)
            self._day_paused = False     # v9.3 (Q6)
            current_funds = float(self.capital_engine.get_available_funds())
            self._day_start_margin = max(current_funds, 1.0)
            logger.info(f"[StockExecution] Day snapshot: start_margin = {self._day_start_margin:.2f}")

    def _place_order(self, kwargs: dict):
        for fn_name in ("place_order", "placeorder"):
            fn = getattr(self.api, fn_name, None)
            if fn:
                try:
                    return fn(**kwargs)
                except TypeError:
                    continue
        return None

    def _cancel_order(self, ordno: str):
        for fn_name in ("cancel_order", "cancelorder"):
            fn = getattr(self.api, fn_name, None)
            if fn:
                try:
                    resp = fn(orderno=ordno)
                    logger.warning(f"[StockExecution] Cancel sent for {ordno}: {resp}")
                    return resp
                except TypeError:
                    try:
                        return fn(ordno)
                    except Exception:
                        return None
        return None

    def _order_history(self, ordno: str):
        for fn_name in ("single_order_history", "singleorderhistory"):
            fn = getattr(self.api, fn_name, None)
            if fn:
                try:
                    return fn(orderno=ordno)
                except TypeError:
                    try:
                        return fn(ordno)
                    except Exception:
                        return None
        return None

    def _position_qty(self, tsym: str):
        """Net open quantity at broker for a tsym; None if API unavailable."""
        for fn_name in ("get_positions", "positions", "getposition"):
            fn = getattr(self.api, fn_name, None)
            if not fn:
                continue
            try:
                rows = fn()
            except Exception:
                continue
            if isinstance(rows, list):
                net = 0.0
                for r in rows:
                    if str(r.get("tsym", "")) == tsym and str(r.get("exch", "")) == "NSE":
                        try:
                            net += float(r.get("netqty", 0))
                        except Exception:
                            pass
                return net
        return None

    def _confirm_fill(self, ordno: str, fallback_price: float):
        """Poll order history until TRADED/FILLED. Returns (filled, actual_fill_price).
        Priority: flprc (last fill) > avgprc (average fill) > prc (limit) > fallback."""
        self._last_rej = ""   # v9.4
        avg_prc = fallback_price
        last_row = None
        for attempt in range(6):
            pytime.sleep(1.0)
            hist = self._order_history(ordno)
            row = None
            if isinstance(hist, list) and len(hist) > 0:
                row = hist[0]
            elif isinstance(hist, dict):
                row = hist
            if row:
                last_row = row
                status = str(row.get("status", "")).upper()
                if status in {"FILLED", "COMPLETE", "COMPLETED", "TRADED"}:
                    try:
                        p = (row.get("flprc") or row.get("avgprc") or row.get("prc") or fallback_price)
                        p = float(p)
                        if p > 0:
                            logger.info(f"[StockExecution] Order {ordno} TRADED | fill_price={p:.2f} (attempt {attempt+1})")
                            return True, p
                    except Exception:
                        pass
                elif status in {"REJECTED", "CANCELED", "CANCELLED"}:
                    rej = row.get("rejreason") or row.get("exch_rej_reason") or ""
                    self._last_rej = str(rej)   # v9.4
                    logger.error(f"[StockExecution] Order {ordno} {status} | reason: {rej} | full: {row}")
                    return False, fallback_price
        if last_row:
            logger.error(f"[StockExecution] No fill confirmation for {ordno}; last: {last_row}")
        return False, avg_prc

    def _resolve_tsym(self, symbol: str) -> str:
        return self._tsym.get(symbol, f"{symbol}-EQ")

    @classmethod
    def _marketable_limit(cls, action: str, spot_price: float) -> float:
        raw = spot_price * (1.0 + MKT_SLACK_PCT) if action == "B" else spot_price * (1.0 - MKT_SLACK_PCT)
        steps = raw / TICK_SIZE
        aligned = math.ceil(steps - 1e-9) * TICK_SIZE if action == "B" else math.floor(steps + 1e-9) * TICK_SIZE
        return round(aligned, 2)

    @staticmethod
    def _calc_txn_cost(entry: float, exit_price: float, qty: int, side: str) -> float:
        """Calibrated from actual contract note (2026-08-10): 0.1096% of avg notional."""
        avg_notional = qty * ((entry + exit_price) / 2.0)
        return round(avg_notional * 0.001096, 2)

    @staticmethod
    def _slip_bps(fill: float, ref: float, side: str) -> float:
        """Positive = adverse slippage. BUY: fill above ref worse; SELL: below worse."""
        if ref <= 0:
            return 0.0
        diff = (fill - ref) / ref if side == "BUY" else (ref - fill) / ref
        return round(diff * 1e4, 2)

    def _book_close(self, trade, symbol, price, reason):
        entry = float(trade["entry_price"])
        qty = int(trade["qty"])
        side = trade["type"]
        gross_pnl = qty * (price - entry) if side == "BUY" else qty * (entry - price)
        txn_cost = self._calc_txn_cost(entry, price, qty, side)
        net_pnl = gross_pnl - txn_cost
        # v9.3 (Q6): consecutive stop-loss circuit breaker
        if reason == "STOP_LOSS" and net_pnl < 0:
            self._consec_sl += 1
            if self._consec_sl >= CONSEC_SL_LIMIT and not self._day_paused:
                self._day_paused = True
                logger.warning(f"[StockExecution] 🛑 CIRCUIT BREAKER: {self._consec_sl} consecutive SLs — entries paused for the day.")
        else:
            self._consec_sl = 0
        # v9.2 (Q4): MAE/MFE attribution from tracked extremes
        highest = float(trade.get("highest", entry))
        lowest = float(trade.get("lowest", entry))
        if side == "BUY":
            mfe = qty * (highest - entry)
            mae = qty * (lowest - entry)
        else:
            mfe = qty * (entry - lowest)
            mae = qty * (entry - highest)
        logger.info(f"[StockExecution] CLOSED: {symbol} {reason} | Gross={gross_pnl:.2f}, Cost={txn_cost:.2f}, Net={net_pnl:.2f}")
        trade.update({
            "exit_time": datetime.now().isoformat(), "exit_price": price,
            "gross_pl": round(gross_pnl, 2), "txn_cost": round(txn_cost, 2),
            "net_pl": round(net_pnl, 2), "exit_reason": reason,
            "mfe": round(mfe, 2), "mae": round(mae, 2),
            "strategy": "stock_order_flow",
        })
        self._day_loss += net_pnl
        self._save_history(trade)
        self.open_trades.pop(symbol, None)
        self._save_open()

    def execute_entry(self, signal, symbol, spot_price, reason, metrics=None):
        if len(self.open_trades) >= MAX_OPEN_POSITIONS:
            logger.info(f"Global Lock Active: Ignoring {signal} for {symbol}.")
            return
        now_time = datetime.now().time()
        if now_time < time(9, 15) or now_time >= time(14, 45):
            return
        self._ensure_day_snapshot()
        if self._day_paused:   # v9.3 (Q6)
            logger.warning("[StockExecution] Circuit breaker active — skipping entry.")
            return
        margin_budget = float(self.capital_engine.get_available_funds())
        if margin_budget < 50:
            logger.warning("Insufficient margin budget.")
            return
        loss_cap_limit = -DAILY_LOSS_CAP_PCT * self._day_start_margin
        if self._day_loss <= loss_cap_limit:
            logger.warning(f"Daily loss cap hit ({self._day_loss:.2f} <= {loss_cap_limit:.2f}) — no new entries today.")
            return
        notional_budget = margin_budget * self.leverage
        # v9.4: safety factor so broker per-symbol margin buffer doesn't reject
        qty = int((notional_budget * MARGIN_SAFETY) / max(spot_price, 0.01))
        if qty <= 0:
            return
        atr = float(metrics.get("atr", 0.0)) if metrics else 0.0
        if atr > 0:
            logger.info(f"ATR15 exits: TP={atr*TP_ATR_MULT:.2f}, InitSL={atr*SL_ATR_MULT:.2f}, Chandelier={atr*CHANDELIER_ATR_MULT:.2f}")
        else:
            logger.warning("ATR not available — using fixed % fallback exits")
        tp_dist = atr * TP_ATR_MULT if atr > 0 else 0.0
        if metrics:
            d_opp = float(metrics.get("dist_supply_above", 0.0)) if signal == "BUY" else float(metrics.get("dist_demand_below", 0.0))
            if d_opp > 0:
                tp_dist = min(tp_dist, d_opp * 0.9) if tp_dist > 0 else d_opp * 0.9
        action = "B" if signal == "BUY" else "S"
        tsym = self._resolve_tsym(symbol)
        limit_price = self._marketable_limit(action, spot_price)
        # v9.4: order loop with margin-shortfall retry (max 2 attempts)
        attempt_qty = qty
        for attempt in range(2):
            kwargs = {
                "buy_or_sell": action, "product_type": "I", "exchange": "NSE",
                "tradingsymbol": tsym, "quantity": attempt_qty, "price_type": "LMT",
                "price": limit_price, "trigger_price": None, "discloseqty": 0, "retention": "DAY",
            }
            try:
                logger.info(f"[StockExecution] Placing LIVE {action} MKT-able LMT for {attempt_qty}x {symbol} @ {limit_price:.2f} (tsym={tsym}, tp_dist={tp_dist:.2f})...")
                t0 = pytime.time()   # v9.2 (Q5)
                resp = self._place_order(kwargs)
                if resp and isinstance(resp, dict) and "Invalid Trading Symbol" in str(resp.get("emsg", "")):
                    alt = symbol if tsym != symbol else f"{symbol}-EQ"
                    logger.warning(f"[StockExecution] tsym '{tsym}' rejected — retrying with '{alt}'")
                    kwargs["tradingsymbol"] = alt
                    resp2 = self._place_order(kwargs)
                    if resp2 and isinstance(resp2, dict) and str(resp2.get("stat", "")).upper() == "OK":
                        tsym = alt
                        resp = resp2
                if resp and isinstance(resp, dict) and str(resp.get("stat", "")).upper() == "OK" and resp.get("norenordno"):
                    ordno = str(resp["norenordno"])
                    filled, avg_prc = self._confirm_fill(ordno, spot_price)
                    t1 = pytime.time()   # v9.2 (Q5)
                    if filled:
                        logger.info(f"[StockExecution] ✅ Entry FILLED @ {avg_prc:.2f}")
                        margin = (attempt_qty * avg_prc) / self.leverage
                        trade = {
                            "entry_time": datetime.now().isoformat(), "symbol": symbol, "tsym": tsym,
                            "type": signal, "entry_price": avg_prc, "qty": attempt_qty, "margin": round(margin, 2),
                            "leverage": self.leverage, "reason": reason, "broker_order_id": ordno,
                            "atr": round(atr, 4), "tp_dist": round(tp_dist, 4),
                            "highest": avg_prc, "lowest": avg_prc,
                            "entry_latency_s": round(t1 - t0, 2),
                            "entry_slip_bps": self._slip_bps(avg_prc, spot_price, signal),
                            "ml_features": metrics or {},
                        }
                        self.open_trades[symbol] = trade
                        self._save_open()
                        return
                    # entry not filled
                    self._cancel_order(ordno)
                    if "margin" in self._last_rej.lower() and attempt == 0:   # v9.4 retry
                        attempt_qty = max(1, int(attempt_qty * 0.8))
                        logger.warning(f"[StockExecution] ⚠️ Margin shortfall — retrying with qty {attempt_qty}")
                        continue
                    logger.warning("[StockExecution] ⚠️ Entry not confirmed — no position booked.")
                    return
                else:
                    logger.error(f"[StockExecution] 🚨 API REJECTION: {resp}")
                    return
            except Exception as e:
                logger.error(f"[StockExecution] Exception in execute_entry: {e}")
                return

    def manage_open_trades(self, current_prices):
        now_time = datetime.now().time()
        is_eod_flatten = now_time >= EOD_FLATTEN_TIME
        for symbol, trade in list(self.open_trades.items()):
            if symbol not in current_prices:
                continue
            current_price = float(current_prices[symbol])
            entry = float(trade["entry_price"])
            atr = float(trade.get("atr", 0.0))
            try:
                minutes_held = (datetime.now() - datetime.fromisoformat(trade["entry_time"])).total_seconds() / 60.0
            except Exception:
                minutes_held = 0.0
            tp_dist = float(trade.get("tp_dist", 0.0))
            if tp_dist <= 0:
                tp_dist = atr * TP_ATR_MULT if atr > 0 else 0.0
            exit_reason = None
            if is_eod_flatten:
                exit_reason = "EOD_FLATTEN"
            elif tp_dist > 0:
                if trade["type"] == "BUY" and current_price >= entry + tp_dist:
                    exit_reason = "TAKE_PROFIT"
                elif trade["type"] == "SELL" and current_price <= entry - tp_dist:
                    exit_reason = "TAKE_PROFIT"
            elif atr <= 0:
                pnl_pct = (current_price - entry) / entry if trade["type"] == "BUY" else (entry - current_price) / entry
                if pnl_pct >= FALLBACK_TP_PCT:
                    exit_reason = "TAKE_PROFIT"
            if exit_reason is None and atr > 0:
                if trade["type"] == "BUY":
                    trade["highest"] = max(float(trade.get("highest", entry)), current_price)
                    chan_stop = float(trade["highest"]) - atr * CHANDELIER_ATR_MULT
                    init_stop = entry - atr * SL_ATR_MULT
                    stop_price = max(init_stop, chan_stop)
                    # v9.7 BREAKEVEN LOCK: profit >= 0.5*ATR -> floor stop at entry
                    be_lock = False
                    if float(trade["highest"]) >= entry + BREAKEVEN_ATR_FRACTION * atr:
                        if entry > stop_price:
                            stop_price = entry
                            be_lock = True
                    if current_price <= stop_price:
                        if be_lock:
                            exit_reason = "BREAKEVEN_EXIT"
                        else:
                            exit_reason = "CHANDELIER_EXIT" if chan_stop > init_stop else "STOP_LOSS"
                else:
                    trade["lowest"] = min(float(trade.get("lowest", entry)), current_price)
                    chan_stop = float(trade["lowest"]) + atr * CHANDELIER_ATR_MULT
                    init_stop = entry + atr * SL_ATR_MULT
                    stop_price = min(init_stop, chan_stop)
                    # v9.7 BREAKEVEN LOCK (short side)
                    be_lock = False
                    if float(trade["lowest"]) <= entry - BREAKEVEN_ATR_FRACTION * atr:
                        if entry < stop_price:
                            stop_price = entry
                            be_lock = True
                    if current_price >= stop_price:
                        if be_lock:
                            exit_reason = "BREAKEVEN_EXIT"
                        else:
                            exit_reason = "CHANDELIER_EXIT" if chan_stop < init_stop else "STOP_LOSS"
            elif exit_reason is None and atr <= 0:
                pnl_pct = (current_price - entry) / entry if trade["type"] == "BUY" else (entry - current_price) / entry
                if pnl_pct <= -FALLBACK_SL_PCT:
                    exit_reason = "STOP_LOSS"
            if exit_reason is None and minutes_held >= MAX_HOLD_MINUTES:
                exit_reason = "TIME_STOP"
            if exit_reason:
                self._close_trade(symbol, current_price, exit_reason)

    def _close_trade(self, symbol, current_price, exit_reason):
        trade = self.open_trades.get(symbol)
        if not trade:
            return
        attempts = int(trade.get("exit_attempts", 0)) + 1
        trade["exit_attempts"] = attempts
        tsym = str(trade.get("tsym", self._resolve_tsym(symbol)))
        if attempts > MAX_EXIT_ATTEMPTS:
            net = self._position_qty(tsym)
            if net == 0.0:
                logger.warning(f"[StockExecution] Reconcile: broker flat on {symbol} — booking BROKER_SQUAREOFF.")
                self._book_close(trade, symbol, current_price, "BROKER_SQUAREOFF")
                return
        qty = int(trade["qty"])
        action = "S" if trade["type"] == "BUY" else "B"
        limit_price = self._marketable_limit(action, current_price)
        kwargs = {
            "buy_or_sell": action, "product_type": "I", "exchange": "NSE",
            "tradingsymbol": tsym, "quantity": qty, "price_type": "LMT",
            "price": limit_price, "trigger_price": None, "discloseqty": 0, "retention": "DAY",
        }
        try:
            logger.info(f"[StockExecution] Exiting {qty}x {symbol} ({exit_reason}) @ {limit_price:.2f}...")
            t0 = pytime.time()   # v9.2 (Q5)
            resp = self._place_order(kwargs)
            emsg = str(resp.get("emsg", "")) if isinstance(resp, dict) else ""
            if "square off" in emsg.lower():
                net = self._position_qty(tsym)
                if net == 0.0:
                    self._book_close(trade, symbol, current_price, "BROKER_SQUAREOFF")
                    return
            if resp and isinstance(resp, dict) and str(resp.get("stat", "")).upper() == "OK" and resp.get("norenordno"):
                ordno = str(resp["norenordno"])
                filled, avg_prc = self._confirm_fill(ordno, current_price)
                t1 = pytime.time()   # v9.2 (Q5)
                if not filled:
                    logger.warning("[StockExecution] ⚠️ Exit not confirmed — will retry next cycle.")
                    self._cancel_order(ordno)
                    return
                trade["exit_broker_order_id"] = ordno
                trade.pop("exit_attempts", None)
                trade["exit_latency_s"] = round(t1 - t0, 2)
                trade["exit_slip_bps"] = self._slip_bps(avg_prc, current_price, trade["type"])
                self._book_close(trade, symbol, avg_prc, exit_reason)
            else:
                logger.error(f"[StockExecution] 🚨 EXIT REJECTION: {resp}")
        except Exception as e:
            logger.error(f"[StockExecution] Exception in _close_trade: {e}")