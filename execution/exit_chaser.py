"""
AEGIS V1.0 — Exit Chaser & Reconciliation Mixin.
Handles 4-step limit exit chasing, order updates, fill polling, resting TP cancellation,
and broker position reconciliation / emergency flattening.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple

from logger import get_logger
from config.settings import (
    OPTION_TICK_SIZE,
    ENABLE_LIVE_BROKERAGE_EXECUTION,
)
from execution.slot_and_risk_constants import (
    _round_tick,
)
from execution.trade_context import TradeContext, TradeLeg, resolve_leg_net_qty
from execution.order_lifecycle import get_marketable_exit_limit_price
from execution.realistic_paper_broker import calculate_nse_option_charges

logger = get_logger("aegis_execution_manager")


class ExitChaserMixin:
    """Mixin for exit-chasing, order-update/fill polling, TP cancellation, and broker reconciliation."""

    def on_order_update(self, message: Any) -> None:
        """Cache broker order events so exit management can inspect fills without REST."""
        if not isinstance(message, dict):
            return
        order_id = str(
            message.get("norenordno")
            or message.get("orderno")
            or message.get("order_id")
            or ""
        )
        if not order_id:
            return
        with self._order_update_lock:
            self._order_updates[order_id] = dict(message)
            while len(self._order_updates) > 256:
                self._order_updates.pop(next(iter(self._order_updates)))


    def _order_fill(self, order_id: str, allow_rest: bool = False, force_rest: bool = False) -> Tuple[bool, float]:
        """Resolve an order fill from WebSocket state first, REST only when throttled."""
        order_id = str(order_id or "")
        if not order_id or order_id.startswith("PAPER_"):
            return False, 0.0

        now_mono = time.monotonic()
        with self._order_update_lock:
            update = self._order_updates.get(order_id)
            filled, fill_price = self._check_hist_filled([update] if update else [])
            if filled or not allow_rest:
                return filled, fill_price

            poll_times = getattr(self, "_order_history_poll_ts", None)
            if poll_times is None:
                poll_times = {}
                self._order_history_poll_ts = poll_times
            last_poll = poll_times.get(order_id)
            if (
                not force_rest
                and last_poll is not None
                and now_mono - last_poll < getattr(self, "_order_history_poll_interval_sec", 2.5)
            ):
                return False, 0.0
            poll_times[order_id] = now_mono
            # Bounded cache hygiene: maintain max 256 keys to prevent multi-day memory leak
            while len(poll_times) > 256:
                poll_times.pop(next(iter(poll_times)))

        api = getattr(self.broker, "_api", None)
        history = getattr(api, "single_order_history", None)
        if not callable(history):
            return False, 0.0
        try:
            filled, fill_price = self._check_hist_filled(history(orderno=order_id))
        except Exception as exc:
            logger.warning(f"[AegisEM] Order history safety poll failed for {order_id}: {exc}")
            return False, 0.0
        if filled:
            with self._order_update_lock:
                self._order_updates[order_id] = {
                    "norenordno": order_id,
                    "status": "FILLED",
                    "avgprc": fill_price,
                }
        return filled, fill_price

    # ─── Persistence ───

    @staticmethod
    def _check_hist_filled(hist: Any) -> Tuple[bool, float]:
        """
        Safely inspects Shoonya single_order_history list (returned newest-first).
        Checks all events in history: if any is FILLED/COMPLETE or rpt == 'Fill',
        returns (True, fill_price).
        """
        if not isinstance(hist, list) or not hist:
            return False, 0.0
        for h in hist:
            if isinstance(h, dict):
                status = str(h.get("status", "")).upper()
                rpt = str(h.get("rpt", "")).upper()
                if status in ("FILLED", "COMPLETE") or rpt == "FILL":
                    px = float(h.get("flprc") or h.get("avgprc") or 0.0)
                    return True, px
        return False, 0.0


    def _cancel_tp(self):
        if not self.tp_order_id or str(self.tp_order_id).startswith("PAPER_"):
            self.tp_order_id = None
            return
        ordno = self.tp_order_id
        self.last_tp_order_id = ordno
        try:
            api = getattr(self.broker, "_api", None)
            if api and hasattr(api, "single_order_history"):
                filled, fill_px = self._order_fill(ordno, allow_rest=True)
                if filled:
                    logger.info(f"[AegisEM] Resting TP order {ordno} already FILLED at broker @ {fill_px:.2f}! Skipping cancel.")
                    self.tp_order_id = None
                    return
            # Cancel through the rate-limited execution provider when available.
            cancel_api = self._order_api() or api
            if cancel_api and hasattr(cancel_api, "cancel_order"):
                resp = cancel_api.cancel_order(orderno=ordno)
                logger.info(f"[AegisEM] Cancelled TP order {ordno} (resp: {resp})")
        except Exception as e:
            logger.warning(f"[AegisEM] TP cancel warning: {e}")
        self.tp_order_id = None

    # ─── Adaptive Quantitative Microstructure Helpers ───

    def _close_all_legs(self, reason: str, is_tp_maker: bool = False, limit_fill_price: float = 0.0):
        with self.trade_lock:
            if not self.has_open_trade() or getattr(self, "_is_exiting", False):
                return
            self._is_exiting = True
            if self.trade_context:
                self.trade_context.status = "EXIT_PENDING"
        
        if getattr(self, "dump_detector", None) is not None:
            self.dump_detector.disarm()
        
        threading.Thread(
            target=self._chase_exit_thread,
            args=(reason, is_tp_maker, limit_fill_price),
            daemon=True,
        ).start()


    def _chase_exit_thread(self, reason: str, is_tp_maker: bool = False, limit_fill_price: float = 0.0):
        """Limit exit execution — preserves TP maker fills and chases emergency exits."""
        try:
            self._do_chase_exit(reason, is_tp_maker, limit_fill_price)
        except Exception as e_chase:
            logger.critical(f"[AegisEM] Exit chaser thread crashed: {e_chase}", exc_info=True)
            with self.trade_lock:
                if self.trade_context and self.has_open_trade():
                    # Attempt emergency fallback: verify live net position at broker and
                    # dispatch exact-qty emergency flatten so the position is never orphaned.
                    logger.warning("[AegisEM] Triggering emergency reconcile-flatten fallback for unclosed trade...")
                    try:
                        flat_all = self._emergency_reconcile_flatten(reason=f"chase_crash_fallback_{reason}")
                    except Exception as e_em:
                        flat_all = False
                        logger.critical(f"[AegisEM] Emergency reconcile-flatten fallback also failed: {e_em}", exc_info=True)
                    if not flat_all:
                        # Revert status to OPEN so subsequent ticks and 15:08 CAS flatten can retry!
                        self.trade_context.status = "OPEN"
        finally:
            with self.trade_lock:
                self._is_exiting = False


    def _do_chase_exit(self, reason: str, is_tp_maker: bool = False, limit_fill_price: float = 0.0):
        params = self.get_instrument_params()
        exch_seg = params["exchange"]
        
        with self.trade_lock:
            if not self.trade_context:
                return
            legs = list(self.trade_context.legs)
            context = self.trade_context
        
        # Rate-limited execution seam (ShoonyaExecutionProvider in production,
        # raw `_api` only for mock/unit-test gateways without a provider).
        api = self._order_api()
        total_pnl = 0.0

        for leg in legs:
            if leg.status != "OPEN":
                continue
            
            if leg.quantity <= 0:
                continue

            # Anchor to live bid
            try:
                q = self.broker.market_data_provider.get_quote(exch_seg, leg.token)
                if q and float(q.get("bid") or q.get("bp1") or 0) > 0:
                    leg.current_price = float(q.get("bid") or q.get("bp1") or 0)
            except Exception:
                pass

            # 👑 UNIT INVARIANT: exit order quantity = actual open share-units (net_qty), single-sourced
            # (SENSEX 1 lot => 20, NIFTY 1 lot => 65).
            qty = str(resolve_leg_net_qty(leg, int(params["lot_size"])))
            fill_price = 0.0
            fill_ts = time.time()
            filled = False

            # 🛡️ Broker-verified-flat shortcut: previous reconciliation proved this
            # position is already closed at the broker — bookkeeping-only close,
            # NEVER dispatch another SELL (prevents naked-short double exit).
            if getattr(leg, "broker_verified_flat", False):
                filled = True
                fill_price = float(getattr(leg, "exit_price", 0.0) or leg.current_price or leg.entry_price)
                logger.info(
                    f"[AegisEM] {leg.symbol}: broker net position verified FLAT earlier — "
                    f"bookkeeping-only close @ {fill_price:.2f} (exit order suppressed)"
                )

            # Case A: Passive Take-Profit Maker Fill (Preserve 100% of profit without discount!)
            # NOTE: tp_order_id may already be None here (cleared in Section 0B / _cancel_tp)
            # but last_tp_order_id holds the actual broker orderno we must verify against.
            if is_tp_maker and limit_fill_price > 0:
                if str(getattr(self, "tp_order_id", "")).startswith("PAPER_") or \
                   str(getattr(self, "last_tp_order_id", "")).startswith("PAPER_"):
                    filled = True
                    fill_price = limit_fill_price
                    logger.info(f"[AegisEM] Paper TP fill accepted @ {fill_price:.2f}")
                else:
                    # Check live order — prefer current tp_order_id, fall back to last_tp_order_id
                    check_ordno = self.tp_order_id or self.last_tp_order_id
                    if check_ordno and api:
                        try:
                            hist_filled, hist_fill_px = self._order_fill(check_ordno, allow_rest=True)
                            if hist_filled:
                                filled = True
                                fill_price = hist_fill_px if hist_fill_px > 0 else limit_fill_price
                                logger.info(
                                    f"[AegisEM] Case A: TP order {check_ordno} confirmed FILLED @ "
                                    f"{fill_price:.2f}"
                                )
                            else:
                                logger.warning(
                                    f"[AegisEM] Case A: TP order {check_ordno} not confirmed; "
                                    "falling through to active chase exit"
                                )
                                filled = False
                        except Exception as e_tp:
                            logger.warning(f"[AegisEM] Maker TP status check warning: {e_tp} — falling through to active chase exit")
                            filled = False
                    else:
                        logger.warning("[AegisEM] Case A: No check_ordno or api available — falling through to active chase exit")
                        filled = False

            # Case B: Emergency or Trail Exit Chase (Anchored to live bid: Bid-0% -> Bid-1% -> Bid-2%)
            # Parse urgency tier from close reason
            norm_reason = "EMERGENCY"
            r_upper = str(reason).upper()
            if any(k in r_upper for k in ("NEAR-TARGET", "NEAR_TARGET", "PROFIT LOCK", "PROFIT_LOCK")):
                norm_reason = "NEAR_TARGET_LOCK"
            elif any(k in r_upper for k in ("COST-SHIELD", "BREAKEVEN")):
                norm_reason = "BREAKEVEN_GUARD"
            elif any(k in r_upper for k in ("PILLAR 4", "SPOT INVALIDATION", "STRUCTURAL")):
                norm_reason = "STRUCTURAL_SL"
            elif any(k in r_upper for k in ("STEPPED SL", "STEPPED_SL")):
                norm_reason = "STEPPED_SL"
            elif any(k in r_upper for k in ("TIMEOUT", "TIME STOP", "MAX HOLD")):
                norm_reason = "TIME_STOP"

            is_catastrophic = any(
                k in r_upper
                for k in (
                    "PREMIUM HARD SL",
                    "DISASTER",
                    "FORCE FLATTEN",
                    "EMERGENCY",
                    "BROKEN SL",
                    "ENTRY CANDLE",
                    "SPOT ADVERSE",
                    "STRUCTURAL",
                )
            )

            def _get_live_quote(ref_px: float) -> tuple:
                try:
                    if hasattr(self.broker, "market_data_provider"):
                        q = self.broker.market_data_provider.get_quote(exch_seg, leg.token)
                        if q:
                            bp1 = float(q.get("bid") or q.get("bp1") or 0)
                            sp1 = float(q.get("ask") or q.get("sp1") or 0)
                            return (bp1 if bp1 > 0 else ref_px, sp1 if sp1 > 0 else (ref_px + OPTION_TICK_SIZE))
                except Exception:
                    pass
                return (ref_px, ref_px + OPTION_TICK_SIZE)

            def _get_live_bid(ref_px: float) -> float:
                return _get_live_quote(ref_px)[0]

            if not filled:
                # Pre-chase safety check: Did the resting TP order already fill at broker?
                # Order history lives on the RAW NorenApi (`_api`), not on the
                # rate-limited provider — check history capability separately.
                check_ordno = self.tp_order_id or self.last_tp_order_id
                _hist_api = getattr(self.broker, "_api", None)
                if check_ordno and not str(check_ordno).startswith("PAPER_") and _hist_api and hasattr(_hist_api, "single_order_history"):
                    try:
                        hist_filled, hist_fill_px = self._order_fill(check_ordno, allow_rest=True)
                        if hist_filled:
                            filled = True
                            fill_price = hist_fill_px if hist_fill_px > 0 else (getattr(leg, "tp_price", 0.0) or leg.current_price)
                            logger.info(
                                f"🎉 [AegisEM] Resting TP order {check_ordno} was already FILLED at broker @ {fill_price:.2f}! "
                                f"Skipping chase exit (position already flat)."
                            )
                    except Exception as e_hist:
                        logger.warning(f"[AegisEM] Pre-chase TP check warning: {e_hist}")

            if not filled:
                # Step 1: Anchor to fresh live bid (marketable fill)
                live_bid_1, live_ask_1 = _get_live_quote(leg.current_price)
                price_1 = _round_tick(get_marketable_exit_limit_price(
                    live_bid_1,
                    exit_reason=norm_reason,
                    entry_price=leg.entry_price,
                    best_ask=live_ask_1
                )) if live_bid_1 > 0 else _round_tick(leg.current_price * 0.99)
                ordno = None

                if is_catastrophic:
                    logger.warning(
                        f"🚨 [AegisEM FastExit] Catastrophic exit triggered ({reason})! "
                        f"Bypassing 2.6s chase sleep -> executing ZERO-LAG Marketable Sweep"
                    )
                    # Step A: Atomic Modify resting TP order in-place if exists
                    if self.tp_order_id and not str(self.tp_order_id).startswith("PAPER_"):
                        ordno = self.tp_order_id
                        if api and hasattr(api, "modify_order"):
                            try:
                                logger.info(
                                    f"[AegisEM FastExit] ⚡ ATOMIC IN-PLACE EXIT: Modifying resting TP order {ordno} -> "
                                    f"LMT @ {price_1:.2f} (live_bid={live_bid_1:.2f}) for instant fill"
                                )
                                resp = api.modify_order(
                                    exchange=exch_seg,
                                    tradingsymbol=leg.symbol,
                                    orderno=ordno,
                                    newquantity=qty,
                                    newprice_type="LMT",
                                    newprice=f"{price_1:.2f}",
                                )
                                logger.info(f"[AegisEM FastExit] modify_order response: {resp}")
                                if isinstance(resp, dict) and resp.get("stat") != "Ok":
                                    emsg = str(resp.get("emsg", "")).lower()
                                    if "complete" in emsg or "filled" in emsg:
                                        logger.info(f"[AegisEM FastExit] Resting TP order {ordno} already filled at broker: {resp}")
                                        filled = True
                                        fill_price = getattr(leg, "tp_price", price_1)
                                    else:
                                        logger.warning(f"[AegisEM FastExit] modify_order failed: {resp}. Cancelling before fallback place_order.")
                                        self._cancel_tp()
                                        ordno = None
                            except Exception as e_mod:
                                logger.error(f"[AegisEM FastExit] Exception in modify_order: {e_mod}. Cancelling before fallback place_order.")
                                self._cancel_tp()
                                ordno = None

                    # Step B: If no resting TP or modify failed, cancel TP and place fresh aggressive order
                    if not ordno and not filled:
                        self._cancel_tp()
                        if api:
                            if hasattr(self.order_lifecycle, "_build_kwargs"):
                                kwargs = self.order_lifecycle._build_kwargs("S", exch_seg, leg.symbol, qty, f"{price_1:.2f}")
                            else:
                                kwargs = {
                                    "buy_or_sell": "S",
                                    "product_type": "M",
                                    "exchange": exch_seg,
                                    "tradingsymbol": leg.symbol,
                                    "quantity": qty,
                                    "price_type": "LMT",
                                    "price": f"{price_1:.2f}",
                                }
                            place = getattr(api, "place_order", None)
                            if place:
                                try:
                                    resp = place(**kwargs)
                                    if isinstance(resp, dict) and resp.get("stat") == "Ok":
                                        ordno = resp.get("norenordno")
                                except Exception as e_p1:
                                    logger.error(f"[AegisEM FastExit] place_order step 1 exception: {e_p1}")

                    # Step C: Micro-poll with sub-50ms checks (Total max 150ms instead of 2600ms!)
                    if ordno and not filled:
                        for d_sec in (0.03, 0.05, 0.07):
                            time.sleep(d_sec)
                            h_filled, h_px = self._order_fill(ordno, allow_rest=True, force_rest=True)
                            if h_filled:
                                filled = True
                                fill_price = h_px if h_px > 0 else price_1
                                fill_ts = time.time()
                                break

                        if not filled:
                            # Step D: Immediate sweep if still unfilled after 150ms (zero lazy sleep)
                            live_bid_fast, live_ask_fast = _get_live_quote(price_1)
                            price_fast = _round_tick(get_marketable_exit_limit_price(
                                live_bid_fast * 0.98,
                                exit_reason=norm_reason,
                                entry_price=leg.entry_price,
                                best_ask=live_ask_fast,
                            )) if live_bid_fast > 0 else _round_tick(leg.current_price * 0.97)
                            if api and hasattr(api, "modify_order"):
                                try:
                                    logger.warning(f"[AegisEM FastExit] Immediate sweep modify @ {price_fast:.2f} (live_bid={live_bid_fast:.2f})")
                                    api.modify_order(
                                        exchange=exch_seg,
                                        tradingsymbol=leg.symbol,
                                        orderno=ordno,
                                        newquantity=qty,
                                        newprice_type="LMT",
                                        newprice=f"{price_fast:.2f}",
                                    )
                                except Exception as e_mod_fast:
                                    logger.error(f"[AegisEM FastExit] Sweep modify exception: {e_mod_fast}")
                            for d_sec in (0.03, 0.05):
                                time.sleep(d_sec)
                                h_filled, h_px = self._order_fill(ordno, allow_rest=True, force_rest=True)
                                if h_filled:
                                    filled = True
                                    fill_price = h_px if h_px > 0 else price_fast
                                    fill_ts = time.time()
                                    break

                else:
                    # Case B2: Normal chase path (non-catastrophic: NEAR_TARGET_LOCK, BREAKEVEN_GUARD, STEPPED_SL, TIME_STOP)
                    if self.tp_order_id and not str(self.tp_order_id).startswith("PAPER_"):
                        ordno = self.tp_order_id
                        if api and hasattr(api, "modify_order"):
                            try:
                                logger.info(
                                    f"[AegisEM] ⚡ ATOMIC IN-PLACE EXIT: Modifying resting TP order {ordno} -> "
                                    f"LMT @ {price_1:.2f} (live_bid={live_bid_1:.2f}) for instant fill"
                                )
                                resp = api.modify_order(
                                    exchange=exch_seg,
                                    tradingsymbol=leg.symbol,
                                    orderno=ordno,
                                    newquantity=qty,
                                    newprice_type="LMT",
                                    newprice=f"{price_1:.2f}",
                                )
                                logger.info(f"[AegisEM] modify_order response: {resp}")
                                if isinstance(resp, dict) and resp.get("stat") != "Ok":
                                    emsg = str(resp.get("emsg", "")).lower()
                                    if "complete" in emsg or "filled" in emsg:
                                        logger.info(f"[AegisEM] Resting TP order {ordno} already filled at broker: {resp}")
                                        filled = True
                                        fill_price = getattr(leg, "tp_price", price_1)
                                    else:
                                        logger.warning(f"[AegisEM] modify_order failed: {resp}. Cancelling before fallback place_order.")
                                        self._cancel_tp()
                                        ordno = None
                            except Exception as e_mod:
                                logger.error(f"[AegisEM] Exception in modify_order: {e_mod}. Cancelling before fallback place_order.")
                                self._cancel_tp()
                                ordno = None

                    if not ordno and not filled:
                        if api:
                            if hasattr(self.order_lifecycle, "_build_kwargs"):
                                kwargs = self.order_lifecycle._build_kwargs("S", exch_seg, leg.symbol, qty, f"{price_1:.2f}")
                            else:
                                kwargs = {
                                    "buy_or_sell": "S",
                                    "product_type": "M",
                                    "exchange": exch_seg,
                                    "tradingsymbol": leg.symbol,
                                    "quantity": qty,
                                    "price_type": "LMT",
                                    "price": f"{price_1:.2f}",
                                }
                            place = getattr(api, "place_order", None)
                            if place:
                                try:
                                    resp = place(**kwargs)
                                    if isinstance(resp, dict) and resp.get("stat") == "Ok":
                                        ordno = resp.get("norenordno")
                                except Exception as e_p1:
                                    logger.error(f"[AegisEM] place_order step 1 exception: {e_p1}")

                    if ordno:
                        time.sleep(0.8)
                        h_filled, h_px = self._order_fill(ordno, allow_rest=True)
                        if h_filled:
                            filled = True
                            fill_price = h_px if h_px > 0 else price_1
                        else:
                            # Step 2: Refresh live bid and chase 1% below current bid
                            live_bid_2, live_ask_2 = _get_live_quote(price_1)
                            price_2 = _round_tick(get_marketable_exit_limit_price(
                                live_bid_2 * 0.99,
                                exit_reason=norm_reason,
                                entry_price=leg.entry_price,
                                best_ask=live_ask_2
                            )) if live_bid_2 > 0 else _round_tick(leg.current_price * 0.98)
                            if api and hasattr(api, "modify_order"):
                                try:
                                    api.modify_order(exchange=exch_seg, tradingsymbol=leg.symbol, orderno=ordno,
                                                     newquantity=qty, newprice_type="LMT", newprice=f"{price_2:.2f}")
                                except Exception as e_mod2:
                                    logger.error(f"[AegisEM] modify_order step 2 exception: {e_mod2}")
                            time.sleep(1.0)
                            h_filled, h_px = self._order_fill(ordno, allow_rest=True)
                            if h_filled:
                                filled = True
                                fill_price = h_px if h_px > 0 else price_2
                            else:
                                verified_net = self._get_verified_broker_net_units(leg)
                                if verified_net == 0:
                                    filled = True
                                    fill_price = float(getattr(leg, "exit_price", 0.0) or leg.current_price or leg.entry_price)
                                    fill_ts = time.time()
                                    logger.info(f"[AegisEM] {leg.symbol}: broker net position already 0 — suppressing Step-3 duplicate sell order")
                                else:
                                    # Step 3: Cancel & Replace at aggressive exit 2% below fresh live bid
                                    if api and hasattr(api, "cancel_order"):
                                        try:
                                            api.cancel_order(orderno=ordno)
                                        except Exception:
                                            pass
                                    live_bid_3 = _get_live_bid(price_2)
                                    price_3 = _round_tick(min(live_bid_3 * 0.98, leg.current_price * 0.97))
                                    if api and hasattr(self.order_lifecycle, "_build_kwargs"):
                                        kwargs = self.order_lifecycle._build_kwargs("S", exch_seg, leg.symbol, qty, f"{price_3:.2f}")
                                        place = getattr(api, "place_order", None)
                                        if place:
                                            logger.warning(f"[AegisEM Chaser] Final aggressive exit @ {price_3:.2f} (live_bid={live_bid_3:.2f})")
                                            try:
                                                resp = place(**kwargs)
                                                ordno_3 = resp.get("norenordno") if isinstance(resp, dict) and resp.get("stat") == "Ok" else None
                                            except Exception as e_p3:
                                                logger.error(f"[AegisEM Chaser] Step-3 aggressive exit place exception: {e_p3}")
                                                ordno_3 = None
                                            if ordno_3:
                                                time.sleep(0.8)
                                                h_filled, h_px = self._order_fill(ordno_3, allow_rest=True)
                                                if h_filled:
                                                    fill_price = h_px if h_px > 0 else price_3
                                                    filled = True
                                                    fill_ts = time.time()
                                                else:
                                                    with self._order_update_lock:
                                                        latest = self._order_updates.get(ordno_3, {})
                                                    status = latest.get("status", "OPEN")
                                                    logger.error(f"[AegisEM Chaser] Step-3 exit order {ordno_3} NOT filled (status={status}) — position may still be open at broker")
                                            else:
                                                logger.error(f"[AegisEM Chaser] Step-3 aggressive exit order placement failed: {resp}")
            
            if not filled:
                verified_net = self._get_verified_broker_net_units(leg)
                if verified_net == 0:
                    filled = True
                    fill_price = float(getattr(leg, "exit_price", 0.0) or leg.current_price or leg.entry_price)
                    fill_ts = time.time()
                    logger.info(f"[AegisEM] {leg.symbol}: broker net position already 0 — suppressing Fallback duplicate sell order")
                else:
                    # Fallback: marketable exit order
                    exit_ok = self.order_lifecycle.execute_exit(leg, reason)
                    if exit_ok:
                        filled = True
                        # 🛡️ REAL-FILL RESOLUTION: a placed LMT is NOT a filled exit.
                        # Resolve the actual broker fill price before PnL bookkeeping —
                        # booking px=0.00 records a fake -100% loss and corrupts capital
                        # (incident 2026-11-05: SENSEX 58000CE booked -₹572 vs real -₹77,
                        # freezing internal funds ~₹170 vs broker ₹647 and making the
                        # next trade select a 10x cheaper ₹6.75 strike).
                        _exit_ordno = getattr(self.order_lifecycle, "_last_exit_order_id", None)
                        if _exit_ordno:
                            try:
                                _rf_ok, _rf_px = self._order_fill(_exit_ordno, allow_rest=True)
                                if _rf_ok and _rf_px > 0:
                                    fill_price = _rf_px
                                    fill_ts = time.time()
                                    logger.info(
                                        f"[AegisEM] Fallback exit order {_exit_ordno} confirmed FILLED @ ₹{fill_price:.2f}"
                                    )
                                elif leg.exit_price and leg.exit_price > 0:
                                    fill_price = float(leg.exit_price)
                                    fill_ts = time.time()
                                    logger.warning(
                                        f"[AegisEM] Fallback exit order {_exit_ordno} fill unconfirmed at broker; "
                                        f"provisional price ₹{fill_price:.2f} used — deferred capital re-sync will reconcile."
                                    )
                            except Exception as e_rf:
                                logger.error(f"[AegisEM] Fallback fill resolution failed (non-fatal): {e_rf}")
                        # Never trust the PnL below if no positive price exists anywhere:
                        # the deferred broker re-sync is mandatory to reconcile real funds.
                        if hasattr(self, "capital_engine") and self.capital_engine and fill_price <= 0:
                            self.capital_engine._needs_broker_sync = True
                    else:
                        logger.critical(
                            f"[AegisEM] 🚨 CRITICAL: Exit order unconfirmed/rejected by broker for {leg.symbol}! "
                            f"Position may remain open at broker. Flagging urgent broker sync!"
                        )
                        if hasattr(self, "capital_engine") and self.capital_engine:
                            self.capital_engine._needs_broker_sync = True
                        # 🛡️ ORDER-REJECT FAIL-SAFE: verify the LIVE net position at the broker
                        # (never trust local state after a rejection) and dispatch an
                        # EXACT-verified-net-qty emergency flatten. A REJECTED exit must never
                        # orphan an unhedged position (incident 2026-11: SENSEX 578800CE).
                        recon_flat, recon_px = self._reconcile_leg_position(leg, reason=reason)
                        if recon_flat:
                            filled = True
                            fill_price = recon_px if recon_px > 0 else leg.current_price
                            fill_ts = time.time()
                if not filled:
                    fill_price = leg.exit_price if leg.exit_price and leg.exit_price > 0 else leg.current_price
                    fill_ts = time.time()
            
            # ─── CRITICAL: Feed fill back to AEGIS for latency decay (real fills only) ───
            if self.aegis_engine and filled:
                signal_price = leg.entry_price
                dp = getattr(context, "decision_payload", None) or {}
                signal_ts = float(dp.get("signal_ts") or 0.0)
                if signal_ts <= 0:
                    signal_ts = context.entry_time.timestamp() if context.entry_time else fill_ts
                try:
                    self.aegis_engine.on_fill(
                        sym=context.legs[0].symbol if context.legs else leg.symbol,
                        token=leg.token,
                        fill_price=fill_price,
                        fill_ts=fill_ts,
                        signal_price=signal_price,
                        signal_ts=signal_ts
                    )
                except Exception as e:
                    # Feedback must never break trade bookkeeping below
                    logger.error(f"[AegisEM] on_fill feedback failed (non-fatal): {e}")
            
            if not filled:
                # 🛡️ ORDER-REJECT FAIL-SAFE: position NOT confirmed closed — DO NOT mark
                # flat, DO NOT record fake PnL, DO NOT stop tracking. Keep leg OPEN so
                # per-tick exit triggers and the 15:08 CAS flatten keep retrying.
                with self.trade_lock:
                    logger.critical(
                        f"[AegisEM] 🚨 UNRECONCILED POSITION {leg.symbol}: broker still holds it — "
                        f"leg kept OPEN for continuous retry (no flat/PnL mark)."
                    )
                    context.status = "OPEN"
                    self._has_unreconciled_position = True
                    self._save_active_context()
                continue
            
            # Record PnL
            with self.trade_lock:
                leg.status = "CLOSED"
                leg.exit_time = datetime.now()
                leg.exit_price = fill_price
                lot_sz = int(params.get("lot_size", 20 if "SENSEX" in leg.symbol else 65))
                # 👑 UNIT INVARIANT: exact open share-units, single-sourced via resolve_leg_net_qty()
                leg_units = resolve_leg_net_qty(leg, lot_sz)
                leg.pnl = (fill_price - leg.entry_price) * leg_units
                total_pnl += leg.pnl
                # Deferred broker sync: the exit SELL is not settled at the broker
                # yet, so book proceeds locally and let _schedule_post_close_capital_resync
                # (t+30s / t+90s) reconcile real funds. Mock capital engines in tests
                # only accept the single positional arg — fall back for them.
                try:
                    self.capital_engine.add_funds(fill_price * leg_units, defer_broker_sync=True)
                except TypeError:
                    self.capital_engine.add_funds(fill_price * leg_units)
                self.strike_cooldowns[leg.symbol] = datetime.now() + timedelta(minutes=3)

                # Compute SEBI & brokerage charges
                buy_turnover = leg.entry_price * leg_units
                sell_turnover = fill_price * leg_units
                leg_charges = calculate_nse_option_charges(buy_turnover, sell_turnover, brokerage_per_order=5.0)
                leg_brokerage = round(leg_charges.brokerage, 2)
                leg_total_charges = round(leg_charges.total_charges, 2)
                leg_taxes = round(leg_total_charges - leg_brokerage, 2)
                leg_gross_pnl = round(leg.pnl, 2)
                leg_net_pnl = round(leg_gross_pnl - leg_total_charges, 2)
                leg_pnl_pct = round(((fill_price - leg.entry_price) / leg.entry_price * 100.0), 2) if leg.entry_price > 0 else 0.0

                logger.info(
                    f"[TRADE CLOSED] {leg.symbol}: {leg.entry_price:.2f} → {fill_price:.2f} | "
                    f"Gross PnL: ₹{leg_gross_pnl:+.2f} ({leg_pnl_pct:+.2f}%) | "
                    f"Brokerage: ₹{leg_brokerage:.2f} | Taxes: ₹{leg_taxes:.2f} | "
                    f"Total Charges: ₹{leg_total_charges:.2f} | Net PnL: ₹{leg_net_pnl:+.2f} | {reason}"
                )

        any_open_left = any(getattr(_l, "status", "") == "OPEN" for _l in legs)
        with self.trade_lock:
            if any_open_left:
                # 🛡️ ORDER-REJECT FAIL-SAFE: tracking must NEVER stop while the broker
                # still holds units. Keep context OPEN (triggers + CAS flatten stay armed),
                # record no PnL/history, keep trade_context alive.
                context.status = "OPEN"
                self._has_unreconciled_position = True
                logger.critical(
                    f"[AegisEM] 🚨 OPEN POSITION PERSISTS after exit attempt (reason: {reason}) — "
                    f"tracking preserved; emergency reconcile + 15:08 CAS flatten remain armed."
                )
                self._sound_critical_alarm()
                self._save_active_context()
                return
            context.status = "CLOSED"
            self._has_unreconciled_position = False
            self._emerg_recon_state = {}
            context.exit_time = datetime.now()
            self._append_history(reason, total_pnl)
            # 🛡️ CAPITAL SELF-HEAL: reconcile funds with the broker AFTER the exit
            # SELL actually settles there (see _schedule_post_close_capital_resync).
            self._schedule_post_close_capital_resync()
            if self.trade_context == context:
                self.trade_context = None
            self.tp_order_id = None
            self.last_tp_order_id = None
            self._save_active_context()

    # ─── 🛡️ Capital Self-Heal: Deferred Post-Close Broker Re-Sync ───

    def _schedule_post_close_capital_resync(self, delay_sec: float = 30.0, verify_sec: float = 90.0):
        """Reconcile internal funds with the broker AFTER the exit SELL settles there.

        Incident 2026-11-05: the synchronous post-exit sync ran while the marketable
        LMT exit was still resting at the broker (actual fill came +25s later), so the
        engine froze margin-blocked stale funds (~₹170 internal vs ₹647 broker), the
        budget-cluster ceiling collapsed from ₹29 to ₹7 per unit, and the next entry
        picked a 10x cheaper ₹6.75 strike. This deferred pass runs twice (t+30s fill
        margin-release, t+90s daily-limits settle) so the next budget selection always
        sees the broker's real freed cash."""
        def _resync_pass(tag: str):
            try:
                ce = self.capital_engine
                if not ce:
                    return
                pre = float(ce.get_available_funds())
                live = ce.sync_funds_from_broker()
                if live != pre:
                    logger.warning(
                        f"[AegisEM] Post-close capital re-sync ({tag}): internal ₹{pre:.2f} -> broker ₹{live:.2f} "
                        f"(Δ ₹{live - pre:+.2f}) — stale internal funds corrected"
                    )
                else:
                    logger.info(f"[AegisEM] Post-close capital re-sync ({tag}): broker agrees ₹{live:.2f}")
            except Exception as e_cap:
                logger.error(f"[AegisEM] Post-close capital re-sync ({tag}) failed (non-fatal): {e_cap}")

        def _worker():
            time.sleep(delay_sec)
            _resync_pass("t+30s")
            time.sleep(max(0.0, verify_sec - delay_sec))
            _resync_pass("t+90s")

        try:
            threading.Thread(target=_worker, daemon=True, name="aegis-em-capital-resync").start()
        except Exception as e_thr:
            logger.error(f"[AegisEM] Deferred capital re-sync spawn failed (non-fatal): {e_thr}")

    # ─── 🛡️ Order-Reject Fail-Safe: Verified Net-Position Reconciliation ───

    def _get_verified_broker_net_units(self, leg: TradeLeg) -> Optional[int]:
        """Exact live net position (share units) for this contract straight from the broker.
        Returns None when the broker positions feed is unavailable (cannot verify)."""
        try:
            get_pos = getattr(self.broker, "get_positions", None)
            if not callable(get_pos):
                return None
            positions = get_pos() or []
        except Exception as exc:
            logger.warning(f"[AegisEM] Reconcile get_positions query failed: {exc}")
            return None
        for pos in positions:
            try:
                if not isinstance(pos, dict):
                    continue
                tsym = str(pos.get("tsym") or pos.get("tradingsymbol") or "")
                tok = str(pos.get("tok") or pos.get("token") or "")
                if tsym != leg.symbol and tok != str(leg.token):
                    continue
                return abs(int(round(float(pos.get("netqty") or 0))))
            except Exception:
                continue
        return None


    def _sound_critical_alarm(self) -> None:
        """Emergency audible/terminal notification. Best-effort, never fatal."""
        if os.environ.get("AEGIS_SILENCE_ALARM", "").strip().lower() in ("1", "true", "yes"):
            return
        try:
            sys.stdout.write("\a")  # terminal bell
            sys.stdout.flush()
        except Exception:
            pass
        try:
            import winsound  # local terminal beep (Windows)
            winsound.Beep(880, 350)
            winsound.Beep(660, 450)
        except Exception:
            pass


    def _reconcile_leg_position(self, leg: TradeLeg, reason: str = "REORDER") -> Tuple[bool, float]:
        """🛡️ Fail-safe: verify the LIVE net position at the broker and dispatch an
        EXACT-verified-net-qty emergency flatten.
        Returns (flat: bool, verified_fill_price: float)."""
        now_mono = time.monotonic()
        state = getattr(self, "_emerg_recon_state", None)
        if not isinstance(state, dict):
            state = {}
            self._emerg_recon_state = state
        leg_state = state.setdefault(leg.leg_id, {"last_attempt_ts": 0.0, "attempts": 0})
        if now_mono - leg_state["last_attempt_ts"] < 10.0:
            remaining = 10.0 - (now_mono - leg_state["last_attempt_ts"])
            logger.warning(
                f"[AegisEM] 🛡️ Reconcile anti-spam guard active for {leg.symbol} "
                f"({remaining:.1f}s cooldown) — duplicate emergency dispatch suppressed."
            )
            return False, 0.0
        leg_state["last_attempt_ts"] = now_mono
        leg_state["attempts"] = int(leg_state.get("attempts", 0)) + 1

        # (a) CRITICAL alert with the broker's exact rejection reason.
        self._log_last_rejection_reason(leg)
        verified = self._get_verified_broker_net_units(leg)
        if verified == 0:
            logger.info(
                f"[AegisEM] Reconcile: {leg.symbol} verified FLAT at broker (netqty=0) — "
                f"position already closed."
            )
            leg.broker_verified_flat = True
            return True, float(getattr(leg, "exit_price", 0.0) or leg.current_price or leg.entry_price)

        lot_size = int(self.get_instrument_params()["lot_size"])
        # (c) NEVER trust the ambiguous local qty — use the broker-verified net position;
        # fall back to the unit-invariant resolver only when the broker feed is unavailable.
        net_units = verified if verified is not None else resolve_leg_net_qty(leg, lot_size)
        if net_units <= 0:
            logger.info(f"[AegisEM] Reconcile: {leg.symbol} has no open units to flatten.")
            return True, float(getattr(leg, "exit_price", 0.0) or leg.current_price or leg.entry_price)
        logger.critical(
            f"[AegisEM] 🚨🚨 EMERGENCY RECONCILE #{leg_state['attempts']} (audio/terminal alert fired): "
            f"{leg.symbol} still OPEN with {net_units} share-units (broker-verified: {verified is not None}) "
            f"— dispatching exact-qty emergency flatten (reason: {reason})."
        )
        self._sound_critical_alarm()

        api = self._order_api()
        if not api or not hasattr(api, "place_order"):
            logger.critical("[AegisEM] 🚨 EMERGENCY flatten abort: broker place_order unavailable!")
            return False, 0.0
        exch = self.get_instrument_params()["exchange"]
        try:
            qrow = self.broker.market_data_provider.get_quote(exch, leg.token)
            bid = float(qrow.get("bid") or qrow.get("bp1") or 0) if isinstance(qrow, dict) else 0.0
        except Exception:
            bid = 0.0
        ref = bid if bid > 0 else (leg.current_price or leg.entry_price)
        limit_price = get_marketable_exit_limit_price(ref * 0.98, exit_reason="EMERGENCY", entry_price=leg.entry_price)
        if limit_price <= 0:
            limit_price = max(OPTION_TICK_SIZE, ref * 0.90)
        if hasattr(self.order_lifecycle, "_build_kwargs"):
            kwargs = self.order_lifecycle._build_kwargs("S", exch, leg.symbol, str(int(net_units)), f"{limit_price:.2f}")
        else:
            kwargs = {
                "buy_or_sell": "S", "product_type": "M", "exchange": exch,
                "tradingsymbol": leg.symbol, "quantity": str(int(net_units)),
                "price_type": "LMT", "price": f"{limit_price:.2f}",
            }
        ordno = None
        try:
            resp = api.place_order(**kwargs)
            if isinstance(resp, dict) and resp.get("stat") == "Ok":
                ordno = resp.get("norenordno")
            else:
                emsg = resp.get("emsg", str(resp)) if isinstance(resp, dict) else str(resp)
                logger.critical(f"[AegisEM] 🚨 EMERGENCY flatten order REJECTED for {leg.symbol}: {emsg}")
        except Exception as exc:
            logger.critical(f"[AegisEM] 🚨 EMERGENCY flatten dispatch failed for {leg.symbol}: {exc}")
        if ordno:
            time.sleep(0.8)
            h_filled, h_px = self._order_fill(str(ordno), allow_rest=True)
            if h_filled:
                leg.broker_verified_flat = True
                leg.exit_price = h_px if h_px and h_px > 0 else limit_price
                logger.critical(
                    f"[AegisEM] ✅ EMERGENCY flatten FILLED for {leg.symbol}: {net_units} units "
                    f"@ {leg.exit_price:.2f} — position secured."
                )
                return True, float(leg.exit_price)
        return False, 0.0


    def _emergency_reconcile_flatten(self, reason: str = "RECONCILE") -> bool:
        """Full-context emergency reconcile — True only when every open leg is verified flat."""
        with self.trade_lock:
            open_legs = [leg_item for leg_item in (self.trade_context.legs if self.trade_context else []) if leg_item.status == "OPEN"]
        if not open_legs:
            return True
        flat_all = True
        for leg in open_legs:
            flat, _px = self._reconcile_leg_position(leg, reason=reason)
            flat_all = flat_all and flat
        return flat_all

