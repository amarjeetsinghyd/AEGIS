"""
AEGIS V1.0 — Trade Monitor Mixin.
Handles active trade lifecycle monitoring, tick-level option feeds, ATM rolls,
dynamic noise-floor calculation, realized delta derivation, and exit triggers.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from collections import deque
from datetime import timedelta, time as dt_time
from typing import Optional, Dict, Any, List, Tuple

import numpy as np

from logger import get_logger
from execution.slot_and_risk_constants import (
    AEGIS_FORCE_FLATTEN,
    MAX_HOLD_SEC,
    MAX_HOLD_EXTENDED_SEC,
    HOLD_EXTEND_MIN_PROFIT_PCT,
    DEFAULT_MIN_NOISE_FLOOR_RS,
    NOISE_FLOOR_SPREAD_MULT,
    NOISE_FLOOR_SIGMA_MULT,
    NOISE_FLOOR_MIN_PCT,
    RESTING_TP_LIMIT_PCT,
    ENABLE_PREMIUM_DISASTER_FLOOR,
    PREMIUM_HARD_SL_PCT,
    FRIDAY_HARD_SL_PCT,
    DEFAULT_SPOT_SL_BPS,
    SPOT_SL_ATR_MULT,
    DELTA_MIN_SPOT_BPS,
    MIN_STRUCTURAL_BAR_RANGE_BPS,
    MIN_STRUCTURAL_BAR_RANGE_ATR_MULT,
    STRUCTURAL_SL_BUFFER_BPS,
    STRUCTURAL_SL_BUFFER_ATR_MULT,
    EMERGENCY_MAX_SL_PCT,
    STEP1_TRIGGER_PCT,
    STEP1_SL_PCT,
    STEP2_TRIGGER_PCT,
    STEP2_SL_PCT,
    NEAR_TARGET_TRIGGER_PCT,
    NEAR_TARGET_PULLBACK_TOLERANCE,
    OPTION_TICK_SIZE,
    _round_tick,
    _DateTimeProxy,
    datetime,
)
from execution.trade_context import TradeContext, TradeLeg

logger = get_logger("aegis_execution_manager")


class TradeMonitorMixin:
    """Mixin for active-trade monitoring, tick-level dump/invalidation, and SL/TP evaluation."""

    @staticmethod
    def must_force_flatten(now: datetime = None) -> bool:
        now = now or datetime.now()
        return now.time() >= AEGIS_FORCE_FLATTEN


    def has_open_trade(self) -> bool:
        if getattr(self, "_is_entering", False):
            return True
        if getattr(self, "_is_exiting", False):
            return True
        return bool(self.trade_context and str(self.trade_context.status).upper() in ("OPEN", "PENDING", "SUBMITTED", "EXIT_PENDING"))


    def on_option_tick(self, tick: Any) -> None:
        """
        Sub-millisecond direct tick feed for open option leg.
        Evaluates O(1) Institutional Dump Detector on incoming Level 1-5 book depth.
        """
        if not self.has_open_trade() or getattr(self, "_is_exiting", False):
            return
        context = self.trade_context
        if not context or not context.legs:
            return
        leg = context.legs[0]
        tok_str = str(getattr(tick, "tok", ""))
        sym = str(getattr(tick, "sym", "") or "")
        if tok_str != str(leg.token) and sym != leg.symbol:
            return

        det = getattr(self, "dump_detector", None)
        if det is None or not det.armed:
            return

        bp = getattr(tick, "bp", None)
        sp = getattr(tick, "sp", None)
        bq = getattr(tick, "bq", None)
        sq = getattr(tick, "sq", None)
        if bp is None or sp is None or bq is None or sq is None or len(bp) < 5 or len(sp) < 5:
            return

        try:
            raw_ts = float(tick.ts) if hasattr(tick, "ts") else time.time()
            ts = raw_ts if raw_ts > 1e9 else raw_ts * 1000.0
            ts_sec = ts / 1000.0 if ts > 1e11 else ts
            lp = float(getattr(tick, "lp", 0.0) or 0.0)
            vol = float(getattr(tick, "vol", 0.0) or 0.0)
            if lp <= 0.0:
                return

            if det.on_tick(ts_sec, lp, vol, bp, bq, sp, sq) == 1:
                reason = det.reason()
                logger.warning(f"[AegisEM] 🚨 Sub-millisecond Institutional Dump Detected at +2% zone: {reason}")
                self._close_all_legs(f"INSTITUTIONAL_DUMP_EXIT ({reason})")
        except Exception as e_dump:
            logger.debug(f"[AegisEM] Dump detector tick evaluation exception: {e_dump}")


    def on_atm_roll(self, _snap=None) -> None:
        """ATM-bus listener: re-stage budget anchors on the next loop pass instead of waiting
        out the 10 s throttle (float write; safe across threads)."""
        self._last_budget_target_refresh = 0.0


    def _compute_noise_floor(self, leg: TradeLeg, live_bid: float, live_ask: float) -> float:
        """
        Calculates dynamic noise floor (in Rupees) based on bid-ask spread and realized micro-volatility:
        noise_floor_rs = max(k1 * spread, k2 * stdev_30s, min_floor)
        where min_floor scales proportionally with option entry premium (min 5% or ₹1.50).
        """
        now_ts = time.time()
        spread = max(live_ask - live_bid, 0.15) if (live_ask > 0 and live_bid > 0 and live_ask >= live_bid) else 0.15
        
        # Realized micro-volatility over last 30-45s
        recent_prices = [p for (ts, _, p, _, _) in self._trade_quote_history if (now_ts - ts) <= 45.0 and p > 0]
        if len(recent_prices) >= 4:
            stdev_prem = float(np.std(recent_prices))
        else:
            stdev_prem = spread * 0.5
            
        proportional_floor = leg.entry_price * NOISE_FLOOR_MIN_PCT if leg.entry_price > 0 else DEFAULT_MIN_NOISE_FLOOR_RS
        effective_min_floor = max(DEFAULT_MIN_NOISE_FLOOR_RS, proportional_floor)

        noise_floor = max(
            NOISE_FLOOR_SPREAD_MULT * spread,
            NOISE_FLOOR_SIGMA_MULT * stdev_prem,
            effective_min_floor,
        )
        return round(noise_floor, 2)


    def _compute_realized_delta(self, context: TradeContext, leg: TradeLeg, curr_spot: float) -> float:
        """
        Empirical / Realized Local Delta (Δpremium / Δspot) without heavy Black-Scholes.
        Uses live observed relationship or fast moneyness approximation.
        Normalizes minimum spot move in basis points (DELTA_MIN_SPOT_BPS).
        """
        spot_entry = context.index_entry
        prem_entry = leg.entry_price
        delta_spot = abs(curr_spot - spot_entry)
        delta_prem = abs(leg.current_price - prem_entry)

        # Normalized minimum spot move hurdle (in bps: 1.25 bps ~ 3 pts NIFTY, ~10 pts SENSEX)
        ref_spot = spot_entry if spot_entry > 0 else (curr_spot if curr_spot > 0 else 24000.0)
        min_spot_move = max(1.0, ref_spot * (DELTA_MIN_SPOT_BPS / 10000.0))

        if delta_spot >= min_spot_move:
            emp_delta = delta_prem / delta_spot
            return max(0.25, min(0.70, emp_delta))

        # Fast theoretical fallback based on Moneyness (Normalized by strike step)
        params = self.get_instrument_params()
        strike_step = 50.0 if "NIFTY" in params.get("index_name", "NIFTY").upper() else 100.0
        sig_type = str(context.signal_type).upper()
        if sig_type in ("CALL", "BUY_CALL"):
            m = (spot_entry - leg.strike) / strike_step
        else:
            m = (leg.strike - spot_entry) / strike_step

        if m >= 0.5:
            return 0.55  # ITM
        elif m >= -0.5:
            return 0.45  # ATM
        elif m >= -1.5:
            return 0.35  # 1-strike OTM
        else:
            return 0.25  # 2-strike OTM


    def _is_active_instrument_expiry(self, now: Optional[datetime] = None, leg: Optional[TradeLeg] = None) -> bool:
        """
        Determines if today is Expiry Day for the active instrument or contract.
        Matches symbol date (e.g. 01OCT26), or standard weekday schedule:
        - SENSEX: Thursday (weekday 3) -> 0 DTE Expiry Day (Wednesday is 1 DTE)
        - NIFTY: Tuesday (weekday 1) -> 0 DTE Expiry Day (Monday is 1 DTE, Friday is 4 DTE)
        """
        if now is None:
            now = datetime.now()
        
        # Method 1: Check contract symbol string for today's date stamp (e.g. '16SEP')
        if leg and leg.symbol:
            day_str = now.strftime("%d%b").upper()
            day_str_alt = now.strftime("%d%b%y").upper()
            sym_upper = leg.symbol.upper()
            if day_str in sym_upper or day_str_alt in sym_upper:
                return True

        # Method 2: Check active instrument standard weekday
        # Calendar Schedule:
        # Mon: NIFTY (1 DTE), Tue: NIFTY (0 DTE Expiry)
        # Wed: SENSEX (1 DTE), Thu: SENSEX (0 DTE Expiry)
        # Fri: NIFTY (4 DTE)
        params = self.get_instrument_params()
        idx_name = str(params.get("index_name", "NIFTY")).upper()
        weekday = now.weekday()
        if "SENSEX" in idx_name and weekday == 3:  # Thursday (0 DTE Expiry)
            return True
        elif "NIFTY" in idx_name and weekday == 1:  # Tuesday (0 DTE Expiry)
            return True

        return False

    # ─── Open Trade Management ───

    def _manage_open_trade(self, now: Optional[datetime] = None):
        """Check exit conditions: AEGIS signals, force flatten, TP/SL, Trailing SL."""
        if now is None:
            now = datetime.now()
        now_ts = now.timestamp()
        
        with self.trade_lock:
            if not self.has_open_trade() or getattr(self, "_is_exiting", False):
                return
            if not self.trade_context or str(self.trade_context.status).upper() != "OPEN":
                return
            context = self.trade_context
            legs = list(context.legs)

        curr_spot = self._get_current_spot(local_only=True)
        
        # Refresh marks and update highest price
        live_bid = 0.0
        live_ask = 0.0
        for leg in legs:
            if leg.status != "OPEN":
                continue
            # L1 Book Snapshot: Query true top-of-book bid & ask from live tick stream
            _, b_lp, b_bid, b_ask = self._latest_option_book(leg.symbol)
            opt_cache = getattr(self, "_option_cache", None)
            if b_bid > 0 and b_ask > 0:
                leg.current_price = b_lp if b_lp > 0 else b_bid
                live_bid = b_bid
                live_ask = b_ask
            elif opt_cache and leg.token in opt_cache and opt_cache[leg.token] > 0:
                c_lp = opt_cache[leg.token]
                leg.current_price = c_lp
                live_bid = c_lp
                live_ask = c_lp

            # Update excursion metrics (MFE & MAE tracking)
            hold_sec_now = (now - context.entry_time).total_seconds() if context.entry_time else 0.0
            if leg.current_price > getattr(leg, "highest_price", leg.entry_price):
                leg.highest_price = leg.current_price
                leg.time_to_highest_sec = hold_sec_now
            if not hasattr(leg, "lowest_price") or leg.current_price < leg.lowest_price:
                leg.lowest_price = leg.current_price
                leg.time_to_lowest_sec = hold_sec_now
            eff_bid_now = live_bid if live_bid > 0 else leg.current_price
            if eff_bid_now > getattr(leg, "highest_bid", 0.0):
                leg.highest_bid = eff_bid_now

            # Record in rolling trade history (0.75s-gated so 60 samples span ~45s,
            # not ~2ms — keeps _compute_noise_floor() stdev meaningful)
            if (now_ts - self._last_quote_hist_ts) >= 0.75:
                self._trade_quote_history.append((now_ts, curr_spot, leg.current_price, live_bid, live_ask))
                self._last_quote_hist_ts = now_ts

        # Update spot excursion metrics (Spot MFE & Spot MAE tracking)
        entry_spot = float(getattr(context, "index_entry", 0.0) or 0.0)
        curr_spot_val = float(curr_spot)
        hold_sec_now = (now - context.entry_time).total_seconds() if context.entry_time else 0.0
        if entry_spot > 0 and curr_spot_val > 0:
            sig_type = getattr(context, "signal_type", "CALL")
            # Signed excursion in spot points relative to trade direction:
            # Positive = Favorable (Index rose for CALL, or Index fell for PUT)
            # Negative = Adverse (Index fell for CALL, or Index rose for PUT)
            spot_excursion = (curr_spot_val - entry_spot) if sig_type == "CALL" else (entry_spot - curr_spot_val)

            if spot_excursion > getattr(context, "spot_peak_mfe_pts", 0.0):
                context.spot_peak_mfe_pts = round(spot_excursion, 2)
                context.spot_time_to_mfe_sec = round(hold_sec_now, 3)
                context.spot_at_mfe = round(curr_spot_val, 2)

            if spot_excursion < getattr(context, "spot_peak_mae_pts", 0.0):
                context.spot_peak_mae_pts = round(spot_excursion, 2)
                context.spot_time_to_mae_sec = round(hold_sec_now, 3)
                context.spot_at_mae = round(curr_spot_val, 2)

        # Order updates are read from local memory before any local exit rules.
        if self.tp_order_id and not str(self.tp_order_id).startswith("PAPER_"):
            filled, fill_px = self._order_fill(self.tp_order_id)
            if filled:
                actual_px = fill_px if fill_px > 0 else float(getattr(legs[0], "tp_price", 0.0))
                logger.info(f"[AegisEM] Resting TP order {self.tp_order_id} FILLED via order update @ {actual_px:.2f}")
                self.last_tp_order_id = self.tp_order_id
                self.tp_order_id = None
                self._close_all_legs(
                    f"Take-Profit Hit (+{(actual_px - legs[0].entry_price)/legs[0].entry_price*100:.1f}% @ {actual_px:.2f})",
                    is_tp_maker=True,
                    limit_fill_price=actual_px,
                )
                return

        # 0B. Local Take-Profit Hit Check (Paper + Live backstop — never depends on
        # broker resting-order fill polling alone). Fires the instant bid touch/crosses
        # leg.tp_price, so a +4% day-move can no longer ride to the time stop.
        for leg in legs:
            if leg.status != "OPEN":
                continue
            tp_price = float(getattr(leg, "tp_price", 0.0) or 0.0)
            if tp_price > 0 and leg.entry_price > 0:
                eff_bid = live_bid if live_bid > 0 else leg.current_price
                if eff_bid > 0 and eff_bid >= tp_price:
                    gain_pct = ((tp_price - leg.entry_price) / leg.entry_price) * 100.0
                    logger.info(
                        f"[AegisEM] Local TP hit: {leg.symbol} bid {eff_bid:.2f} >= tp {tp_price:.2f} (+{gain_pct:.1f}%)"
                    )
                    actual_fill_px = tp_price
                    if self.tp_order_id and not str(self.tp_order_id).startswith("PAPER_"):
                        filled, fill_px = self._order_fill(self.tp_order_id)
                        if filled and fill_px > 0:
                            actual_fill_px = fill_px
                            logger.info(f"[AegisEM] Resting TP {self.tp_order_id} verified FILLED via order update @ {actual_fill_px:.2f}")
                    self.last_tp_order_id = self.tp_order_id
                    self.tp_order_id = None
                    self._close_all_legs(
                        f"Take-Profit Hit (+{gain_pct:.1f}% @ {actual_fill_px:.2f})",
                        is_tp_maker=True,
                        limit_fill_price=actual_fill_px,
                    )
                    return

        # 0C. Option Premium Hard SL: DECOMMISSIONED by Amarjeet bhai's mandate (Nov 12, 2026).
        # Reason: Option bid wiggles/spreads on 0-DTE cause premature fake shakeouts while Spot is 100% safe.
        # AEGIS relies strictly on Universal Spot Invalidation (Entry Candle Low/High + Cushion Buffer) below.
        _enable_disaster = getattr(
            sys.modules.get("execution.execution_manager"),
            "ENABLE_PREMIUM_DISASTER_FLOOR",
            ENABLE_PREMIUM_DISASTER_FLOOR,
        )
        if _enable_disaster:
            for leg in legs:
                if leg.status != "OPEN" or leg.entry_price <= 0:
                    continue
                eff_px = live_bid if live_bid > 0 else leg.current_price
                if eff_px > 0:
                    loss_frac = (eff_px - leg.entry_price) / leg.entry_price
                    init_tp = float(getattr(leg, "initial_tp_pct", RESTING_TP_LIMIT_PCT) or RESTING_TP_LIMIT_PCT)
                    is_reduced_tp = init_tp <= 0.032
                    eff_hard_sl = FRIDAY_HARD_SL_PCT if is_reduced_tp else PREMIUM_HARD_SL_PCT
                    if loss_frac <= -eff_hard_sl:
                        self._close_all_legs(
                            f"Premium Hard SL (-{eff_hard_sl*100:.1f}% disaster floor: {loss_frac*100:.1f}% @ {eff_px:.2f})"
                        )
                        return

        # 1. Force flatten at session end
        if self.must_force_flatten(now):
            self._close_all_legs(f"Force Flatten (>= {AEGIS_FORCE_FLATTEN.strftime('%H:%M')})")
            return

        # 2. Dynamic Time Stop (Point 2: 0DTE Dynamic Time-Decay Shield)
        if context.entry_time:
            hold_sec = (now - context.entry_time).total_seconds()
            leg0 = legs[0] if legs else None
            eff_bid = live_bid if live_bid > 0 else (leg0.current_price if leg0 else 0.0)
            cur_gain_pct = ((eff_bid - leg0.entry_price) / leg0.entry_price) if (leg0 and leg0.entry_price > 0) else 0.0

            # Dynamic Max Hold Time Configuration:
            # - Non-expiry: 300s baseline (extendable to 360s for running winners)
            # - Expiry Days (Before 12:30 PM): 240s (4 min strict)
            # - Expiry Days (At or After 12:30 PM): 180s (3 min strict theta compression)
            is_expiry = self._is_active_instrument_expiry(now, leg0)
            is_post_1230 = now.time() >= dt_time(12, 30)
            if is_expiry:
                dynamic_max_hold = 180.0 if is_post_1230 else 240.0
            else:
                dynamic_max_hold = MAX_HOLD_SEC  # 300.0s

            # Point 2: Conditional Stagnation Trap Check at 90s (Expiry session after 12:30 PM)
            if is_expiry and is_post_1230 and hold_sec >= 90.0:
                if cur_gain_pct < 0.005:  # Gain < +0.5%
                    pa_engine = self._pa_engine
                    market_struct = pa_engine.get_market_structure(context.signal_type) if (pa_engine and hasattr(pa_engine, "get_market_structure")) else "NEUTRAL"
                    # Live 20-EMA band alignment
                    live_ema, _ = getattr(pa_engine, "get_live_20_ema_and_slope", lambda: (0.0, 0.0))() if pa_engine else (0.0, 0.0)
                    is_opposed = (market_struct == "OPPOSED")
                    if live_ema > 0 and curr_spot > 0:
                        if context.signal_type == "CALL" and curr_spot < live_ema:
                            is_opposed = True
                        elif context.signal_type == "PUT" and curr_spot > live_ema:
                            is_opposed = True
                    if is_opposed:
                        self._close_all_legs(
                            f"0DTE_STAGNATION_TRAP_EXIT ({int(hold_sec)}s, gain {cur_gain_pct*100:+.1f}%, EMA opposed)"
                        )
                        return

            # Check eligibility for 6-minute extension (Only non-expiry days or strong running momentum):
            pa_engine = self._pa_engine
            market_struct = pa_engine.get_market_structure(context.signal_type) if (pa_engine and hasattr(pa_engine, "get_market_structure")) else "NEUTRAL"

            is_profit_running = False
            if leg0 and leg0.entry_price > 0:
                is_profit_running = (
                    cur_gain_pct >= HOLD_EXTEND_MIN_PROFIT_PCT
                    or getattr(leg0, "breakeven_locked", False)
                )

            can_extend_to_6min = is_profit_running and (market_struct != "OPPOSED") and not (is_expiry and is_post_1230)

            if hold_sec >= MAX_HOLD_EXTENDED_SEC:
                self._close_all_legs(f"6-Min Extended Time Stop ({int(hold_sec)}s)")
                return
            elif hold_sec >= dynamic_max_hold:
                if can_extend_to_6min and hold_sec < MAX_HOLD_EXTENDED_SEC:
                    if not getattr(context, "_extended_to_6m_logged", False):
                        gain_pct_disp = cur_gain_pct * 100.0
                        logger.info(
                            f"[AegisEM] Trade running in profit (+{gain_pct_disp:.1f}%) with favorable structure "
                            f"at {int(dynamic_max_hold)}s -> Extending time ceiling to 360s"
                        )
                        context._extended_to_6m_logged = True
                else:
                    if is_expiry:
                        expiry_tag = f"0DTE {'Afternoon' if is_post_1230 else 'Morning'} "
                        self._close_all_legs(f"{expiry_tag}Time Stop ({int(hold_sec)}s, ceiling {int(dynamic_max_hold)}s)")
                    else:
                        self._close_all_legs(f"5-Min Hard Time Stop ({int(hold_sec)}s)")
                    return

        # ─── Mode B Bar-Close Escape Check (Scratch exit if candle closes back inside boundary) ───
        exec_mode = str(getattr(context, "execution_mode", "") or "")
        pa_name_lower = str(getattr(context, "pa_setup_name", "") or "").lower()
        is_mode_b = (
            exec_mode.startswith("MODE_B")
            or "mode_b" in pa_name_lower
            or "breakthrough" in pa_name_lower
            or getattr(context, "pillar", 0) in (3, 4, 5)
        )
        if is_mode_b:
            b_level = float(getattr(context, "boundary_level", 0.0) or 0.0)
            # ─── In-flight boundary re-break exit (0.02% safety buffer) ───
            # Exit immediately when spot retraces back across the boundary by
            # more than 2 bps, without waiting for the 1-minute bar to close.
            if b_level > 0 and curr_spot > 0:
                buffer_pts = b_level * 0.0002
                if context.signal_type == "CALL" and curr_spot <= (b_level - buffer_pts):
                    logger.info(
                        f"[AegisEM] MODE_B_INFLIGHT_BOUNDARY_REBREAK: Spot {curr_spot:.1f} <= {b_level:.1f} - {buffer_pts:.1f}pts -> Immediate Exit"
                    )
                    self._close_all_legs(f"MODE_B_INFLIGHT_BOUNDARY_REBREAK (Spot {curr_spot:.1f} <= {b_level:.1f} - {buffer_pts:.1f}pts)")
                    return
                elif context.signal_type == "PUT" and curr_spot >= (b_level + buffer_pts):
                    logger.info(
                        f"[AegisEM] MODE_B_INFLIGHT_BOUNDARY_REBREAK: Spot {curr_spot:.1f} >= {b_level:.1f} + {buffer_pts:.1f}pts -> Immediate Exit"
                    )
                    self._close_all_legs(f"MODE_B_INFLIGHT_BOUNDARY_REBREAK (Spot {curr_spot:.1f} >= {b_level:.1f} + {buffer_pts:.1f}pts)")
                    return

            entry_bucket = getattr(context, "entry_bar_bucket", None)
            pa_eng = self._pa_engine
            if entry_bucket is not None and b_level > 0 and pa_eng and getattr(pa_eng, "bars_1m", None):
                curr_bucket = int(now_ts // 60)
                if curr_bucket > entry_bucket:
                    # Judge the EXACT entry candle (bucket-matched), never the previous
                    # closed bar. Before the first tick of the new minute closes the
                    # entry candle, bars_1m[-1] is still the PRE-entry box bar (which
                    # closed inside the boundary by definition) — judging it would
                    # scratch a healthy breakout prematurely.
                    entry_bar = None
                    try:
                        closed_bars = pa_eng.bars_1m.copy()
                        for _b in reversed(closed_bars):
                            if int(getattr(_b, "ts_start", 0.0) // 60) == int(entry_bucket):
                                entry_bar = _b
                                break
                    except Exception:
                        entry_bar = None
                    if entry_bar is not None:
                        if context.signal_type == "CALL" and entry_bar.close <= b_level:
                            logger.info(
                                f"[AegisEM] Mode B Failed Breakthrough Bar-Close Escape: Entry candle (bucket {entry_bucket}) "
                                f"closed at {entry_bar.close:.2f} <= boundary {b_level:.2f} -> Scratch Exit"
                            )
                            self._close_all_legs("MODE_B_FAILED_CLOSE_ESCAPE")
                            return
                        elif context.signal_type == "PUT" and entry_bar.close >= b_level:
                            logger.info(
                                f"[AegisEM] Mode B Failed Breakthrough Bar-Close Escape: Entry candle (bucket {entry_bucket}) "
                                f"closed at {entry_bar.close:.2f} >= boundary {b_level:.2f} -> Scratch Exit"
                            )
                            self._close_all_legs("MODE_B_FAILED_CLOSE_ESCAPE")
                            return

        # 3. Spot Adverse Movement SL (Normalized Basis Points + 1m ATR regime expansion)
        pa_engine = self._pa_engine
        atr_1m = getattr(pa_engine, "current_atr_1m", 0.0) if pa_engine else 0.0
        ref_spot = context.index_entry if context.index_entry > 0 else (curr_spot if curr_spot > 0 else 24000.0)
        base_spot_sl_pts = ref_spot * (DEFAULT_SPOT_SL_BPS / 10000.0)
        spot_sl_pts = max(base_spot_sl_pts, SPOT_SL_ATR_MULT * atr_1m) if atr_1m > 0 else base_spot_sl_pts

        # 3A. Universal Price Action Entry Candle Invalidation Stop-Loss (All 4 Pillars: P1, P2, P3, P4)
        c_low = float(getattr(context, "candle_low", 0.0) or 0.0)
        c_high = float(getattr(context, "candle_high", 0.0) or 0.0)
        pa_name = getattr(context, "pa_setup_name", "") or ""
        pillar_num = int(getattr(context, "pillar", 0) or 0)
        is_pa = bool(getattr(context, "is_price_action_setup", False)) or bool(getattr(context, "is_50ema_pullback", False))

        # Detect pillar identity across P1, P2, P3, P4, P5
        is_p1 = (pillar_num == 1 or "20_ema" in pa_name.lower() or "pullback" in pa_name.lower() or "rejection" in pa_name.lower())
        is_p2 = (pillar_num == 2 or "sweep" in pa_name.lower())
        is_p3 = (pillar_num == 3 or "squeeze" in pa_name.lower())
        is_p4 = (pillar_num == 4 or "prominence" in pa_name.lower() or "phase_shift" in pa_name.lower() or "hvg" in pa_name.lower())
        is_p5 = (pillar_num == 5 or "gamma_trap" in pa_name.lower() or "writer_trap" in pa_name.lower())

        is_pillar_trade = is_p1 or is_p2 or is_p3 or is_p4 or is_p5 or is_pa or bool(pa_name) or (pillar_num > 0)

        if is_p1:
            pillar_label = "Pillar 1"
        elif is_p2:
            pillar_label = "Pillar 2"
        elif is_p3:
            pillar_label = "Pillar 3"
        elif is_p4:
            pillar_label = "Pillar 4"
        elif is_p5:
            pillar_label = "Pillar 5"
        elif pa_name:
            pillar_label = pa_name
        else:
            pillar_label = "Price Action"

        # Resilient fallback: populate candle low/high from bars_1m if not set on context
        if is_pillar_trade and (c_low <= 0 or c_high <= 0) and pa_engine and getattr(pa_engine, "bars_1m", None):
            last_bar = pa_engine.bars_1m[-1]
            if c_low <= 0:
                c_low = float(last_bar.low)
                context.candle_low = c_low
            if c_high <= 0:
                c_high = float(last_bar.high)
                context.candle_high = c_high

        # 3A.1 Universal Normalized Structural Invalidation Metrics (Basis Points & ATR Multiples)
        # Scale-Invariant Hurdle against thin candles/dojis & sub-tick wick breathing room
        base_range_hurdle = ref_spot * (MIN_STRUCTURAL_BAR_RANGE_BPS / 10000.0)
        min_bar_range_pts = max(base_range_hurdle, MIN_STRUCTURAL_BAR_RANGE_ATR_MULT * atr_1m) if atr_1m > 0 else base_range_hurdle

        base_wick_cushion = ref_spot * (STRUCTURAL_SL_BUFFER_BPS / 10000.0)
        wick_cushion_pts = max(base_wick_cushion, STRUCTURAL_SL_BUFFER_ATR_MULT * atr_1m) if atr_1m > 0 else base_wick_cushion

        # Apply Normalized Cushion to First Candle (Signal Candle Low/High)
        if context.signal_type == "CALL":
            initial_sl = (c_low - wick_cushion_pts) if c_low > 0 else 0.0
            effective_sl = initial_sl
        else:
            initial_sl = (c_high + wick_cushion_pts) if c_high > 0 else 0.0
            effective_sl = initial_sl

        trailing_sl = float(getattr(context, "trailing_spot_sl", 0.0) or 0.0)
        if trailing_sl > 0:
            if context.signal_type == "CALL":
                effective_sl = max(effective_sl, trailing_sl)
            elif context.signal_type == "PUT":
                effective_sl = min(effective_sl, trailing_sl) if effective_sl > 0 else trailing_sl

        entry_time = getattr(context, "entry_time", None)
        entry_ts = float(
            (getattr(context, "decision_payload", {}) or {}).get("fill_timestamp", 0.0)
            or (entry_time.timestamp() if entry_time else 0.0)
        )

        # ─── P3 OPTIMIZATION: ZERO-COPY REVERSE SCAN FOR CLOSED BARS POST-ENTRY ───
        if is_pillar_trade and pa_engine and getattr(pa_engine, "bars_1m", None) and entry_ts > 0:
            # 400 बार्स की कॉपी (copy()) हटाकर केवल एंट्री के बाद क्लोज हुए 1-2 बार्स को O(1) में स्कैन करें
            recent_bars = []
            for b in reversed(pa_engine.bars_1m):
                b_end = getattr(b, "ts_end", getattr(b, "timestamp", 0.0))
                if b_end <= entry_ts:
                    break
                recent_bars.append(b)

            if recent_bars:
                recent_bars.reverse()  # क्रोनोलॉजिकल ऑर्डर में प्रोसेस करें
                for b in recent_bars:
                    b_end = getattr(b, "ts_end", getattr(b, "timestamp", 0.0))
                    b_start = getattr(b, "ts_start", b_end - 60.0)

                    # 1. Option A: Check Minimum Structural Range Hurdle (Thin Candle / Doji filter)
                    bar_range = float(b.high - b.low)
                    if bar_range < min_bar_range_pts:
                        logger.debug(
                            f"[AegisEM] Skipping thin candle for structural trail: "
                            f"range {bar_range:.1f}pts < min {min_bar_range_pts:.1f}pts ({MIN_STRUCTURAL_BAR_RANGE_BPS}bps)"
                        )
                        continue

                    # 2. Option C: Bar passed structural hurdle -> trail with normalized wick cushion
                    is_entry_bar = (b_start <= entry_ts <= b_end)
                    if context.signal_type == "CALL":
                        cand_sl = float(b.low - wick_cushion_pts)
                        favorable_progress = (b.close >= b.open or b.close > context.index_entry or (c_high > 0 and b.high > c_high))
                        if is_entry_bar and b.low > 0:
                            if favorable_progress and cand_sl > effective_sl:
                                effective_sl = cand_sl
                                context.trailing_spot_sl = effective_sl
                                context.entry_candle_low = b.low
                                if not getattr(context, "_entry_candle_low_trailed", False):
                                    logger.info(
                                        f"[AegisEM] Entry candle closed (Range {bar_range:.1f}pts >= {min_bar_range_pts:.1f}pts): "
                                        f"Trailed CALL structural SL {initial_sl:.1f} -> {effective_sl:.1f} "
                                        f"(Low {b.low:.1f} - cushion {wick_cushion_pts:.1f}pts, +{effective_sl - initial_sl:.1f} pts)"
                                    )
                                    context._entry_candle_low_trailed = True
                        elif (not is_entry_bar) and b.low > 0:
                            if favorable_progress and cand_sl > effective_sl:
                                old_sl = effective_sl
                                effective_sl = cand_sl
                                context.trailing_spot_sl = effective_sl
                                logger.info(
                                    f"[AegisEM] Subsequent bar closed (Range {bar_range:.1f}pts): Trailed CALL structural SL "
                                    f"{old_sl:.1f} -> {effective_sl:.1f} (+{effective_sl - old_sl:.1f} pts)"
                                )
                    elif context.signal_type == "PUT":
                        cand_sl = float(b.high + wick_cushion_pts)
                        favorable_progress = (b.close <= b.open or b.close < context.index_entry or (c_low > 0 and b.low < c_low))
                        if is_entry_bar and b.high > 0:
                            if favorable_progress and (effective_sl <= 0 or cand_sl < effective_sl):
                                effective_sl = cand_sl
                                context.trailing_spot_sl = effective_sl
                                context.entry_candle_high = b.high
                                if not getattr(context, "_entry_candle_high_trailed", False):
                                    logger.info(
                                        f"[AegisEM] Entry candle closed (Range {bar_range:.1f}pts >= {min_bar_range_pts:.1f}pts): "
                                        f"Trailed PUT structural SL {initial_sl:.1f} -> {effective_sl:.1f} "
                                        f"(High {b.high:.1f} + cushion {wick_cushion_pts:.1f}pts, {effective_sl - initial_sl:.1f} pts)"
                                    )
                                    context._entry_candle_high_trailed = True
                        elif (not is_entry_bar) and b.high > 0:
                            if favorable_progress and (effective_sl <= 0 or cand_sl < effective_sl):
                                old_sl = effective_sl
                                effective_sl = cand_sl
                                context.trailing_spot_sl = effective_sl
                                logger.info(
                                    f"[AegisEM] Subsequent bar closed (Range {bar_range:.1f}pts): Trailed PUT structural SL "
                                    f"{old_sl:.1f} -> {effective_sl:.1f} ({effective_sl - old_sl:.1f} pts)"
                                )

        # Universal Rule: ALL 4 Pillars exit on Entry Candle / Structural Trailing Low/High Invalidation
        if is_pillar_trade and curr_spot > 0:
            if context.signal_type == "CALL" and effective_sl > 0 and curr_spot <= effective_sl:
                if c_low > 0 and effective_sl > initial_sl:
                    sl_desc = f"Candle Low {effective_sl:.1f} [Trailed with cushion {wick_cushion_pts:.1f}pts from {c_low:.1f}]"
                else:
                    sl_desc = f"Candle Low {effective_sl:.1f} [Initial {c_low:.1f} - cushion {wick_cushion_pts:.1f}pts]"
                self._close_all_legs(
                    f"{pillar_label} Entry Candle Low Broken SL (Spot {curr_spot:.1f} <= {sl_desc})"
                )
                return
            elif context.signal_type == "PUT" and effective_sl > 0 and curr_spot >= effective_sl:
                if c_high > 0 and effective_sl < initial_sl:
                    sl_desc = f"Candle High {effective_sl:.1f} [Trailed with cushion {wick_cushion_pts:.1f}pts from {c_high:.1f}]"
                else:
                    sl_desc = f"Candle High {effective_sl:.1f} [Initial {c_high:.1f} + cushion {wick_cushion_pts:.1f}pts]"
                self._close_all_legs(
                    f"{pillar_label} Entry Candle High Broken SL (Spot {curr_spot:.1f} >= {sl_desc})"
                )
                return

        # 3B. Spot Adverse Movement SL (Normalized Basis Points + 1m ATR regime expansion)
        if curr_spot > 0 and context.index_entry > 0:
            if context.signal_type == "CALL" and curr_spot <= (context.index_entry - spot_sl_pts):
                self._close_all_legs(f"Spot Adverse Breakdown SL ({curr_spot:.1f} vs entry {context.index_entry:.1f}, -{spot_sl_pts:.1f}pts [{(spot_sl_pts/ref_spot)*10000:.1f}bps])")
                return
            elif context.signal_type == "PUT" and curr_spot >= (context.index_entry + spot_sl_pts):
                self._close_all_legs(f"Spot Adverse Rally SL ({curr_spot:.1f} vs entry {context.index_entry:.1f}, +{spot_sl_pts:.1f}pts [{(spot_sl_pts/ref_spot)*10000:.1f}bps])")
                return

        # ─── Adaptive Quantitative Microstructure Exits ───
        for leg in legs:
            if leg.status != "OPEN" or leg.entry_price <= 0:
                continue

            highest = getattr(leg, "highest_price", leg.entry_price)
            peak_gain_rs = highest - leg.entry_price
            effective_bid = live_bid if live_bid > 0 else leg.current_price
            cur_pnl_rs = effective_bid - leg.entry_price
            cur_pnl_pct = (cur_pnl_rs / leg.entry_price) * 100.0
            peak_pnl_pct = (peak_gain_rs / leg.entry_price) * 100.0
            noise_floor = self._compute_noise_floor(leg, live_bid, live_ask)
            hold_sec = (now - context.entry_time).total_seconds() if context.entry_time else 0.0


            # ─── Point 4: Stepped Risk Compression + Cost-Shield + Near-Target Engine (V4.1) ───
            # Scale-Invariant Normalized Percentage Model (strictly % normalized, no fixed paise!)
            init_tp = float(getattr(leg, "initial_tp_pct", RESTING_TP_LIMIT_PCT) or RESTING_TP_LIMIT_PCT)
            is_reduced_tp = init_tp <= 0.032  # Friday 4 DTE (3.0% TP) or lowered target mode

            # Stepped Protection Smoothing:
            step1_trigger = 0.018 if is_reduced_tp else float(os.environ.get("AEGIS_STEP1_TRIGGER_PCT", "0.030"))   # Armed at +3.0% gain
            step1_sl_pct  = -0.015 if is_reduced_tp else float(os.environ.get("AEGIS_STEP1_SL_PCT", "-0.030"))      # Wide breathing room: -3.0%
            step2_trigger = 0.022 if is_reduced_tp else float(os.environ.get("AEGIS_STEP2_TRIGGER_PCT", "0.035"))   # Armed at +3.5% gain -> Lock 0.0% Breakeven
            near_target_trigger = 0.025 if is_reduced_tp else float(os.environ.get("AEGIS_NEAR_TARGET_TRIGGER_PCT", "0.035"))
            near_target_tolerance = 0.005 if is_reduced_tp else float(os.environ.get("AEGIS_NEAR_TARGET_TOLERANCE", "0.006"))

            peak_bid = getattr(leg, "highest_bid", effective_bid)
            peak_bid_gain = max(peak_gain_rs, peak_bid - leg.entry_price)
            peak_pnl_frac = peak_bid_gain / leg.entry_price if leg.entry_price > 0 else 0.0
            cur_pnl_frac = cur_pnl_rs / leg.entry_price if leg.entry_price > 0 else 0.0
            peak_bid_frac = (peak_bid - leg.entry_price) / leg.entry_price if leg.entry_price > 0 else 0.0

            # 1. Near-Target Profit Lock (Peak Bid >= near_target_trigger, exits on pullback from peak bid)
            if peak_bid_frac >= near_target_trigger:
                pullback_frac = peak_bid_frac - cur_pnl_frac
                if pullback_frac >= near_target_tolerance:
                    self._close_all_legs(
                        f"Near-Target Profit Lock (+{cur_pnl_pct:.1f}% @ {effective_bid:.2f} | Peak was +{peak_pnl_pct:.1f}%)"
                    )
                    return

            # 2. Dynamic SL & Cost-Shield Level Evaluation
            dynamic_sl_price = None
            dynamic_sl_reason = ""

            if peak_pnl_frac >= step2_trigger or getattr(leg, "breakeven_locked", False):
                # Step 2: Peak reached >= step2_trigger (+3.5% normal, +2.5% Friday) -> Cost-Shield Scratch Active (SL = Entry Price / 0.0%)
                if not getattr(leg, "breakeven_locked", False):
                    leg.breakeven_locked = True
                    self._save_active_context()
                    logger.info(
                        f"🛡️ [AegisEM] Cost-Shield Scratch Armed: {leg.symbol} peak +{peak_pnl_pct:.1f}% (₹{peak_gain_rs:.2f}) -> "
                        f"SL locked at Entry Price ₹{leg.entry_price:.2f} (0.0% Scratch / Loss Prevented)"
                    )
                dynamic_sl_price = _round_tick(leg.entry_price * (1.0 + STEP2_SL_PCT))  # 0.0%
                dynamic_sl_reason = (
                    f"Cost-Shield Scratch Exit (Breakeven Guard 0.0% @ {effective_bid:.2f} | "
                    f"Peak was +{peak_pnl_pct:.1f}% | Loss Prevented!)"
                )
            elif peak_pnl_frac >= step1_trigger or getattr(leg, "step1_sl_armed", False):
                # Step 1: Peak reached >= step1_trigger (+2.5% normal, +1.8% Friday) -> Stepped Safety Net Active (SL tightened to -2.0% normal, -1.5% Friday)
                if not getattr(leg, "step1_sl_armed", False):
                    leg.step1_sl_armed = True
                    self._save_active_context()
                    logger.info(
                        f"🛡️ [AegisEM] Stepped Safety Net Armed: {leg.symbol} peak +{peak_pnl_pct:.1f}% -> "
                        f"SL tightened to {step1_sl_pct*100:.1f}% (₹{_round_tick(leg.entry_price * (1.0 + step1_sl_pct)):.2f}, wide breathing room)"
                    )
                dynamic_sl_price = _round_tick(leg.entry_price * (1.0 + step1_sl_pct))
                dynamic_sl_reason = (
                    f"Stepped Safety Net SL ({cur_pnl_pct:+.1f}% @ {effective_bid:.2f} | "
                    f"Peak was +{peak_pnl_pct:.1f}%)"
                )

            # 3. Dynamic SL Trigger Check
            if dynamic_sl_price is not None and effective_bid > 0 and effective_bid <= dynamic_sl_price:
                self._close_all_legs(dynamic_sl_reason)
                return

            # 6. Spot-Driven Invalidation Stop-Loss (Derived from Spot SL pts & Realized Delta)
            realized_delta = self._compute_realized_delta(context, leg, curr_spot)
            opt_sl_dist = max(noise_floor, realized_delta * spot_sl_pts)
            # Emergency max SL cap (35%)
            max_sl_dist = leg.entry_price * EMERGENCY_MAX_SL_PCT
            effective_sl_dist = min(opt_sl_dist, max_sl_dist)
            effective_sl_price = _round_tick(leg.entry_price - effective_sl_dist)

            if effective_bid > 0 and effective_bid <= effective_sl_price:
                loss_rs = leg.entry_price - effective_bid
                loss_pct = (loss_rs / leg.entry_price) * 100.0
                self._close_all_legs(
                    f"Adaptive Spot Invalidation SL (-{loss_pct:.1f}% @ {effective_bid:.2f}, -₹{loss_rs:.2f} | "
                    f"Δ={realized_delta:.2f}, Floor=₹{noise_floor:.2f})"
                )
                return

        # Periodic context re-save (every 10s while trade is active)
        last_ctx_save = getattr(context, "_last_context_save_ts", 0.0)
        if (now_ts - last_ctx_save) >= 10.0:
            context._last_context_save_ts = now_ts
            self._save_active_context()

        if self.tp_order_id and not str(self.tp_order_id).startswith("PAPER_"):
            filled, fill_px = self._order_fill(self.tp_order_id, allow_rest=True)
            if filled:
                actual_px = fill_px if fill_px > 0 else float(getattr(legs[0], "tp_price", 0.0))
                logger.info(f"[AegisEM] Resting TP order {self.tp_order_id} confirmed by order update or safety poll @ {actual_px:.2f}")
                self.last_tp_order_id = self.tp_order_id
                self.tp_order_id = None
                self._close_all_legs(
                    f"Take-Profit Hit (+{(actual_px - legs[0].entry_price)/legs[0].entry_price*100:.1f}% @ {actual_px:.2f})",
                    is_tp_maker=True,
                    limit_fill_price=actual_px,
                )


