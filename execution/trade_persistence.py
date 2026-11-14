"""
AEGIS V1.0 — Trade Persistence Mixin.
Handles daily state persistence, trade history recording, PnL tallying,
NSE option charge calculation, and crash-recovery active context saving/loading.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Optional, Dict, Any, List

from logger import get_logger
from execution.slot_and_risk_constants import (
    MAX_HOLD_EXTENDED_SEC,
    _DateTimeProxy,
    datetime,
)
from execution.trade_context import TradeContext, TradeLeg, resolve_leg_net_qty
from execution.realistic_paper_broker import calculate_nse_option_charges

logger = get_logger("aegis_execution_manager")


class TradePersistenceMixin:
    """Mixin for daily state, PnL tracking, trade history, and crash-recovery context persistence."""

    def _load_trades_today(self):
        today = datetime.now().date()
        if not os.path.exists(self.state_file):
            self.trades_today = 0
            self.last_trade_date = today
            return
        try:
            with open(self.state_file, "r") as f:
                state = json.load(f)
            state_date = datetime.fromisoformat(state.get("date", "1970-01-01")).date()
            if state_date == today:
                self.trades_today = int(state.get("trades_today", 0))
                self._consec_losses = int(state.get("consec_losses", 0))
                self.last_trade_loss = bool(state.get("last_trade_loss", False))
                self.last_trade_exit_time = float(state.get("last_trade_exit_time", 0.0))
                self.last_exited_route = str(state.get("last_exited_route", "") or "")
                self.last_trade_date = today
                logger.info(f"[AegisEM] Restored trades_today: {self.trades_today} | Consec Losses: {self._consec_losses}")
            else:
                self.trades_today = 0
                self.last_trade_date = today
        except Exception as e:
            logger.error(f"[AegisEM] Load trades_today failed: {e}")
            self.trades_today = 0
            self.last_trade_date = today


    def _save_trades_today(self):
        try:
            state = {
                "date": (self.last_trade_date or datetime.now().date()).isoformat(),
                "trades_today": self.trades_today,
                "consec_losses": getattr(self, "_consec_losses", 0),
                "last_trade_loss": getattr(self, "last_trade_loss", False),
                "last_trade_exit_time": getattr(self, "last_trade_exit_time", 0.0),
                "last_exited_route": getattr(self, "last_exited_route", ""),
            }
            tmp_file = f"{self.state_file}.tmp"
            with open(tmp_file, "w") as f:
                json.dump(state, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_file, self.state_file)
        except Exception as e:
            logger.error(f"[AegisEM] Save trades_today failed: {e}")


    def _load_daily_pnl(self):
        self.daily_pnl = 0.0
        self.daily_gross_pnl = 0.0
        self.daily_charges = 0.0
        self.daily_net_pnl = 0.0
        if not os.path.exists(self.history_file):
            return
        try:
            with open(self.history_file, "r") as f:
                hist = json.load(f)
            today_str = datetime.now().strftime("%d %b %Y")
            for trade in hist:
                if trade.get("entry_time", "").startswith(today_str):
                    gross = float(trade.get("gross_pnl", trade.get("pnl", 0.0)))
                    net = float(trade.get("net_pnl", trade.get("net_pl", trade.get("pnl", 0.0))))
                    chgs = float(trade.get("total_charges", trade.get("charges", 0.0)))
                    self.daily_gross_pnl += gross
                    self.daily_charges += chgs
                    self.daily_net_pnl += net
            self.daily_pnl = round(self.daily_net_pnl, 2)
            logger.info(
                f"[AegisEM] Restored daily PnL: Gross ₹{self.daily_gross_pnl:.2f} | "
                f"Charges ₹{self.daily_charges:.2f} | Net ₹{self.daily_net_pnl:.2f}"
            )
        except Exception as e:
            logger.error(f"[AegisEM] Load daily PnL failed: {e}")


    def _append_history(self, reason: str, total_pnl: float):
        hist = []
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, "r") as f:
                    hist = json.load(f)
            except Exception:
                hist = []
        
        context = self.trade_context
        leg0 = context.legs[0] if context and context.legs else None
        now = datetime.now()
        entry_dt_str = context.entry_time.strftime("%d %b %Y %H:%M:%S") if context and context.entry_time else now.strftime("%d %b %Y %H:%M:%S")
        exit_dt_str = now.strftime("%d %b %Y %H:%M:%S")

        # Ghost-trade dedup guard: same (entry_time, symbol) already recorded -> skip BEFORE
        # any state mutation (daily_pnl / cooldown / history / telemetry) to avoid double count.
        dup_sym = leg0.symbol if leg0 else ""
        for existing in hist:
            if existing.get("entry_time") == entry_dt_str and existing.get("symbol") == dup_sym:
                logger.warning(
                    f"[AegisEM] Duplicate close detected ({entry_dt_str} {dup_sym}) — "
                    f"history append skipped (ghost-trade guard)"
                )
                return

        entry_px = float(leg0.entry_price) if leg0 else 0.0
        exit_px = float(leg0.exit_price) if (leg0 and leg0.exit_price is not None) else 0.0
        pnl_pct = round(((exit_px - entry_px) / entry_px * 100.0), 2) if entry_px > 0 else 0.0

        sym_str = leg0.symbol if leg0 else ""
        try:
            params = self.get_instrument_params() if hasattr(self, "get_instrument_params") else {}
        except Exception:
            params = {}
        lot_sz = int(params.get("lot_size", 20 if "SENSEX" in sym_str else 65))
        # 👑 UNIT INVARIANT: exact open share-units, single-sourced via resolve_leg_net_qty()
        tot_units = resolve_leg_net_qty(leg0, lot_sz)
        buy_turnover = (entry_px * tot_units) if entry_px > 0 else 0.0
        sell_turnover = (exit_px * tot_units) if exit_px > 0 else 0.0
        charges_obj = calculate_nse_option_charges(buy_turnover, sell_turnover, brokerage_per_order=5.0)

        brokerage = round(charges_obj.brokerage, 2)
        total_charges = round(charges_obj.total_charges, 2)
        taxes = round(total_charges - brokerage, 2)
        gross_pnl = round(total_pnl, 2)
        net_pnl = round(gross_pnl - total_charges, 2)

        self.daily_gross_pnl = round(getattr(self, "daily_gross_pnl", 0.0) + gross_pnl, 2)
        self.daily_charges = round(getattr(self, "daily_charges", 0.0) + total_charges, 2)
        self.daily_net_pnl = round(getattr(self, "daily_net_pnl", 0.0) + net_pnl, 2)
        self.daily_pnl = self.daily_net_pnl

        now_epoch = time.time()
        cooldown_interval = int(now_epoch - self.last_trade_exit_time) if self.last_trade_exit_time > 0 else 0
        self.last_trade_exit_time = now_epoch
        self.last_trade_loss = (net_pnl < 0)

        # Notify 60s post-trade milestone tracker of broker exit
        if self.aegis_engine and getattr(self.aegis_engine, "post_trade_tracker", None) and sym_str:
            try:
                self.aegis_engine.post_trade_tracker.notify_trade_exit(
                    symbol=sym_str,
                    exit_price=exit_px,
                    exit_ts=now_epoch,
                    exit_reason=reason,
                )
            except Exception as _e_nex:
                logger.debug(f"[AegisEM] Post-trade exit notification error: {_e_nex}")

        # Quality Stack: consecutive-loss tracking + engine cross-wire
        if net_pnl < 0:
            self._consec_losses = int(getattr(self, "_consec_losses", 0)) + 1
        else:
            self._consec_losses = 0
        if self.aegis_engine is not None:
            try:
                self.aegis_engine._em_consec_losses = self._consec_losses
                self.aegis_engine._em_daily_pnl = self.daily_pnl
            except Exception:
                pass
        dp = dict(context.decision_payload) if context and context.decision_payload else {}
        self.last_exited_route = str(getattr(context, "pa_setup_name", "") or dp.get("pa_setup_name", "") or "")
        if self.last_exited_route:
            logger.info(
                f"[AegisEM] Exited route '{self.last_exited_route}' -> Strict non-consecutive lockout armed (setup blocked until alternate route triggers)"
            )
        post_loss_sec = getattr(self, "post_loss_cooloff_sec", 120.0)
        inter_trade_sec = getattr(self, "inter_trade_cooldown_sec", 60.0)
        if self.last_trade_loss:
            logger.info(f"[AegisEM] Trade closed in loss (Gross ₹{gross_pnl:+.2f} | Net ₹{net_pnl:+.2f}) -> Post-loss cooloff active for {int(post_loss_sec)}s")
        else:
            logger.info(f"[AegisEM] Trade closed in profit (Gross ₹{gross_pnl:+.2f} | Net ₹{net_pnl:+.2f}) -> Inter-trade cooldown active for {int(inter_trade_sec)}s")
        
        # ─── Post-Trade Quantitative Feature Attribution ───
        duration_sec = int((context.exit_time - context.entry_time).total_seconds()) if (context and context.entry_time and context.exit_time) else int((now - context.entry_time).total_seconds()) if (context and context.entry_time) else 0
        entry_px = float(leg0.entry_price) if leg0 else 0.0
        exit_px = float(leg0.exit_price) if (leg0 and leg0.exit_price is not None) else 0.0
        pnl_pct = round(((exit_px - entry_px) / entry_px * 100.0), 2) if entry_px > 0 else 0.0

        highest_px = float(getattr(leg0, "highest_price", entry_px)) if leg0 else entry_px
        lowest_px = float(getattr(leg0, "lowest_price", entry_px)) if leg0 else entry_px
        if exit_px > 0:
            highest_px = max(highest_px, exit_px)
            lowest_px = min(lowest_px, exit_px)
        peak_mfe_pct = round(max(0.0, ((highest_px - entry_px) / entry_px * 100.0)), 2) if entry_px > 0 else 0.0
        if pnl_pct > 0:
            peak_mfe_pct = max(peak_mfe_pct, pnl_pct)
        peak_mfe_rs = round(max(0.0, highest_px - entry_px), 2)
        time_to_peak_mfe_sec = round(float(getattr(leg0, "time_to_highest_sec", 0.0)), 1) if leg0 else 0.0
        peak_mae_pct = round(min(0.0, ((lowest_px - entry_px) / entry_px * 100.0)), 2) if entry_px > 0 else 0.0
        if pnl_pct < 0:
            peak_mae_pct = min(peak_mae_pct, pnl_pct)
        peak_mae_rs = round(min(0.0, lowest_px - entry_px), 2)

        entry_spot = float(context.index_entry) if context else 0.0
        exit_spot = float(self._get_current_spot())
        spot_delta_pts = round(exit_spot - entry_spot, 2) if (entry_spot > 0 and exit_spot > 0) else 0.0
        spot_delta_bps = round((spot_delta_pts / entry_spot) * 10000.0, 2) if entry_spot > 0 else 0.0

        # Spot MFE / MAE metrics
        spot_peak_mfe_pts = float(getattr(context, "spot_peak_mfe_pts", 0.0) or 0.0)
        spot_peak_mae_pts = float(getattr(context, "spot_peak_mae_pts", 0.0) or 0.0)
        spot_time_to_mfe_sec = round(float(getattr(context, "spot_time_to_mfe_sec", 0.0) or 0.0), 3)
        spot_time_to_mfe_ms = int(spot_time_to_mfe_sec * 1000)
        spot_time_to_mae_sec = round(float(getattr(context, "spot_time_to_mae_sec", 0.0) or 0.0), 3)
        spot_time_to_mae_ms = int(spot_time_to_mae_sec * 1000)
        spot_peak_mfe_bps = round((spot_peak_mfe_pts / entry_spot) * 10000.0, 2) if entry_spot > 0 else 0.0
        spot_peak_mae_bps = round((spot_peak_mae_pts / entry_spot) * 10000.0, 2) if entry_spot > 0 else 0.0
        spot_at_mfe = float(getattr(context, "spot_at_mfe", 0.0) or entry_spot)
        spot_at_mae = float(getattr(context, "spot_at_mae", 0.0) or entry_spot)

        entry_fut_px = float(dp.get("futures_price", 0.0) or 0.0)
        exit_fut_px = float(getattr(self.aegis_engine, "last_futures_price", 0.0) if self.aegis_engine else 0.0)
        fut_delta_pts = round(exit_fut_px - entry_fut_px, 2) if (entry_fut_px > 0 and exit_fut_px > 0) else 0.0
        basis_pts_entry = float(dp.get("basis_pts", 0.0) or 0.0)

        entry_latency_ms = float(dp.get("entry_latency_ms", 0.0) or 0.0)
        vol_burst_age_sec = float(dp.get("volatility_burst_age_sec", 0.0) or 0.0)
        trigger_age_sec = float(dp.get("trigger_age_sec", 0.0) or 0.0)
        eqs_score = float(dp.get("eqs_score", 0.0) or 0.0)
        micro_score = float(dp.get("microstructure_score", 0.0) or dp.get("conviction", 0.0) or 0.0)
        persistence_score = float(dp.get("persistence_score", 0.0) or 0.0)

        is_win = net_pnl > 0
        is_scratch = net_pnl == 0
        outcome = "WINNER" if is_win else ("SCRATCH" if is_scratch else "LOSER")

        # Determine classification tag
        if "Take-Profit" in reason or "TP" in reason:
            classification = "TAKE_PROFIT_LIMIT"
        elif "Extended Time" in reason:
            classification = "EXTENDED_6MIN_TIME_EXIT"
        elif "Time Stop" in reason:
            classification = "MOMENTUM_STALL_TIME_EXIT" if is_win else "TIME_STOP_LOSS"
        elif "Trend Reversal" in reason:
            classification = "TREND_REVERSAL_EXIT"
        elif "Breakeven" in reason:
            classification = "BREAKEVEN_GUARD"
        elif "SL" in reason:
            classification = "STOP_LOSS_HIT"
        elif "Force Flatten" in reason:
            classification = "FORCE_FLATTEN"
        else:
            classification = "OTHER"

        # Futures correlation assessment
        fut_corr = "NEUTRAL"
        if entry_fut_px > 0 and exit_fut_px > 0:
            signal_dir = context.signal_type if context else "CALL"
            favored_fut = (fut_delta_pts > 0 and signal_dir == "CALL") or (fut_delta_pts < 0 and signal_dir == "PUT")
            favored_spot = (spot_delta_pts > 0 and signal_dir == "CALL") or (spot_delta_pts < 0 and signal_dir == "PUT")
            if favored_fut and favored_spot:
                fut_corr = "CONFIRMED_DIRECTION"
            elif favored_fut and spot_delta_pts == 0:
                fut_corr = "CONFIRMED_DIRECTION"
            elif favored_fut and not favored_spot:
                fut_corr = "FUTURES_LEAD_SPOT_LAG"
            elif not favored_fut and favored_spot:
                fut_corr = "SPOT_DRIVEN_FUTURES_DIVERGENCE"
            elif not favored_fut and not favored_spot and (fut_delta_pts != 0 or spot_delta_pts != 0):
                fut_corr = "OPPOSED"

        sig_type = context.signal_type if context else "TRADE"
        sym_str = leg0.symbol if leg0 else "OPTION"
        diag_summary = (
            f"{outcome} {sig_type} on {sym_str} ({duration_sec}s) | "
            f"Gross: ₹{gross_pnl:+.2f} ({pnl_pct:+.1f}%) | Brokerage: ₹{brokerage:.2f} | Taxes: ₹{taxes:.2f} | Total Charges: ₹{total_charges:.2f} | Net: ₹{net_pnl:+.2f} | "
            f"Exit: {reason} | "
            f"Opt MFE: {peak_mfe_pct:+.1f}% (+₹{peak_mfe_rs:.2f} @ {time_to_peak_mfe_sec}s), Opt MAE: {peak_mae_pct:.1f}% | "
            f"Spot MFE: {spot_peak_mfe_pts:+.1f}pts ({spot_peak_mfe_bps:+.1f}bps @ {spot_time_to_mfe_sec}s), Spot MAE: {spot_peak_mae_pts:+.1f}pts ({spot_peak_mae_bps:+.1f}bps) | "
            f"Spot Δ: {spot_delta_pts:+.1f}pts ({spot_delta_bps:+.1f}bps), Fut Δ: {fut_delta_pts:+.1f}pts (Basis: {basis_pts_entry:+.1f}pts | {fut_corr}) | "
            f"Lat: {entry_latency_ms:.0f}ms, VolBurstAge: {vol_burst_age_sec}s, EQS: {eqs_score:.1f}"
        )

        post_trade_attribution = {
            "outcome": outcome,
            "classification": classification,
            "gross_pnl_rs": gross_pnl,
            "brokerage_rs": brokerage,
            "taxes_rs": taxes,
            "charges_rs": total_charges,
            "net_pnl_rs": net_pnl,
            "pnl_rs": gross_pnl,
            "pnl_pct": pnl_pct,
            "trade_duration_sec": duration_sec,
            "exit_reason": reason,
            "peak_mfe_pct": peak_mfe_pct,
            "peak_mfe_rs": peak_mfe_rs,
            "time_to_peak_mfe_sec": time_to_peak_mfe_sec,
            "peak_mae_pct": peak_mae_pct,
            "peak_mae_rs": peak_mae_rs,
            "spot_peak_mfe_pts": spot_peak_mfe_pts,
            "spot_peak_mfe_bps": spot_peak_mfe_bps,
            "spot_time_to_mfe_sec": spot_time_to_mfe_sec,
            "spot_time_to_mfe_ms": spot_time_to_mfe_ms,
            "spot_peak_mae_pts": spot_peak_mae_pts,
            "spot_peak_mae_bps": spot_peak_mae_bps,
            "spot_time_to_mae_sec": spot_time_to_mae_sec,
            "spot_time_to_mae_ms": spot_time_to_mae_ms,
            "spot_at_mfe": spot_at_mfe,
            "spot_at_mae": spot_at_mae,
            "spot_move_pts": spot_delta_pts,
            "spot_move_bps": spot_delta_bps,
            "futures_move_pts": fut_delta_pts,
            "futures_basis_pts": basis_pts_entry,
            "futures_correlation": fut_corr,
            "entry_latency_ms": entry_latency_ms,
            "volatility_burst_age_sec": vol_burst_age_sec,
            "trigger_age_sec": trigger_age_sec,
            "time_since_last_trade_sec": cooldown_interval,
            "eqs_score": eqs_score,
            "microstructure_score": micro_score,
            "persistence_score": persistence_score,
            "diagnostic_summary": diag_summary,
        }

        trade_telemetry = {
            "impulse_time": dp.get("impulse_time_iso", ""),
            "fill_time": dp.get("fill_time_iso", ""),
            "entry_latency_ms": entry_latency_ms,
            "trade_duration_sec": duration_sec,
            "entry_spot": entry_spot,
            "exit_spot": exit_spot,
            "spot_move_pts": spot_delta_pts,
            "spot_peak_mfe_pts": spot_peak_mfe_pts,
            "spot_peak_mfe_bps": spot_peak_mfe_bps,
            "spot_time_to_mfe_sec": spot_time_to_mfe_sec,
            "spot_time_to_mfe_ms": spot_time_to_mfe_ms,
            "spot_peak_mae_pts": spot_peak_mae_pts,
            "spot_peak_mae_bps": spot_peak_mae_bps,
            "spot_time_to_mae_sec": spot_time_to_mae_sec,
            "spot_time_to_mae_ms": spot_time_to_mae_ms,
            "spot_at_mfe": spot_at_mfe,
            "spot_at_mae": spot_at_mae,
            "entry_futures": entry_fut_px,
            "exit_futures": exit_fut_px,
            "futures_move_pts": fut_delta_pts,
            "basis_pts_entry": basis_pts_entry,
            "peak_mfe_pct": peak_mfe_pct,
            "peak_mfe_rs": peak_mfe_rs,
            "time_to_peak_mfe_sec": time_to_peak_mfe_sec,
            "peak_mae_pct": peak_mae_pct,
            "peak_mae_rs": peak_mae_rs,
        }

        trade_record = {
            "date": entry_dt_str,
            "entry_time": entry_dt_str,
            "exit_time": exit_dt_str,
            "strategy": "AEGIS_V1",
            "signal": context.signal_type if context else "",
            "signal_type": context.signal_type if context else "",
            "symbol": leg0.symbol if leg0 else "",
            "quantity": leg0.quantity if leg0 else 1,
            "lot_size": lot_sz,
            "entry_price": entry_px,
            "exit_price": exit_px,
            "pnl": gross_pnl,
            "gross_pnl": gross_pnl,
            "brokerage": brokerage,
            "taxes": taxes,
            "charges": total_charges,
            "total_charges": total_charges,
            "net_pl": net_pnl,
            "net_pnl": net_pnl,
            "spot_peak_mfe_pts": spot_peak_mfe_pts,
            "spot_peak_mae_pts": spot_peak_mae_pts,
            "spot_time_to_mfe_sec": spot_time_to_mfe_sec,
            "spot_time_to_mfe_ms": spot_time_to_mfe_ms,
            "daily_total_pnl": round(self.daily_pnl, 2),
            "daily_gross_pnl": round(self.daily_gross_pnl, 2),
            "daily_charges": round(self.daily_charges, 2),
            "daily_net_pnl": round(self.daily_net_pnl, 2),
            "exit_reason": reason,
            "reason": reason,
            "telemetry": trade_telemetry,
            "post_trade_attribution": post_trade_attribution,
            "decision_payload": dp,
        }
        hist.append(trade_record)

        try:
            with open(self.history_file, "w") as f:
                json.dump(hist, f, indent=2)
        except Exception as e:
            logger.error(f"[AegisEM] History write failed: {e}")

        # Append to daily research telemetry JSONL (production only, skip in unit test runs)
        try:
            import sys
            if "pytest" not in sys.modules and not os.environ.get("PYTEST_CURRENT_TEST"):
                telem_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "telemetry")
                os.makedirs(telem_dir, exist_ok=True)
                telem_file = os.path.join(telem_dir, f"aegis_trade_telemetry_{now.strftime('%Y-%m-%d')}.jsonl")
                with open(telem_file, "a", encoding="utf-8") as tf:
                    tf.write(json.dumps(trade_record, default=str) + "\n")

                # Automatic production trade journal update & Excel sync
                from dashboard.telemetry_loader import reconcile_and_auto_journal
                reconcile_and_auto_journal()
        except Exception as e_telem:
            logger.warning(f"[AegisEM] Telemetry write warning: {e_telem}")

        logger.info(f"📊 [AegisEM Post-Trade] {diag_summary}")


    def _save_active_context(self):
        try:
            if not self.trade_context or str(getattr(self.trade_context, "status", "")).upper() not in ("OPEN", "PENDING", "SUBMITTED", "EXIT_PENDING"):
                if os.path.exists(self.active_context_file):
                    try:
                        os.remove(self.active_context_file)
                    except Exception:
                        pass
                return
            payload = {
                "status": "OPEN",
                "signal_type": self.trade_context.signal_type,
                "strategy": "AEGIS_V1",
                "entry_time": self.trade_context.entry_time.isoformat() if self.trade_context.entry_time else None,
                "index_entry": self.trade_context.index_entry,
                "spot_peak_mfe_pts": getattr(self.trade_context, "spot_peak_mfe_pts", 0.0),
                "spot_peak_mae_pts": getattr(self.trade_context, "spot_peak_mae_pts", 0.0),
                "spot_time_to_mfe_sec": getattr(self.trade_context, "spot_time_to_mfe_sec", 0.0),
                "spot_time_to_mae_sec": getattr(self.trade_context, "spot_time_to_mae_sec", 0.0),
                "spot_at_mfe": getattr(self.trade_context, "spot_at_mfe", self.trade_context.index_entry),
                "spot_at_mae": getattr(self.trade_context, "spot_at_mae", self.trade_context.index_entry),
                "trailing_spot_sl": getattr(self.trade_context, "trailing_spot_sl", 0.0),
                "candle_low": getattr(self.trade_context, "candle_low", 0.0),
                "candle_high": getattr(self.trade_context, "candle_high", 0.0),
                "pillar": getattr(self.trade_context, "pillar", 0),
                "pa_setup_name": getattr(self.trade_context, "pa_setup_name", ""),
                "execution_mode": getattr(self.trade_context, "execution_mode", ""),
                "boundary_level": getattr(self.trade_context, "boundary_level", 0.0),
                "entry_bar_bucket": getattr(self.trade_context, "entry_bar_bucket", None),
                "spot_sl_price": getattr(self.trade_context, "spot_sl_price", 0.0),
                "decision_payload": getattr(self.trade_context, "decision_payload", {}),
                "tp_order_id": self.tp_order_id,
                "last_tp_order_id": self.last_tp_order_id,
                "legs": [
                    {
                        "token": leg.token,
                        "symbol": leg.symbol,
                        "strike": leg.strike,
                        "option_type": getattr(leg, "option_type", None) or self.trade_context.signal_type,
                        "quantity": leg.quantity,
                        "entry_price": leg.entry_price,
                        "current_price": leg.current_price,
                        "status": leg.status,
                        "initial_tp_pct": getattr(leg, "initial_tp_pct", 0.10),
                        "tp_pct": getattr(leg, "tp_pct", 0.10),
                        "tp_price": getattr(leg, "tp_price", None),
                        "highest_price": getattr(leg, "highest_price", leg.entry_price),
                        "highest_bid": getattr(leg, "highest_bid", leg.entry_price),
                        "breakeven_locked": getattr(leg, "breakeven_locked", False),
                        "step1_sl_armed": getattr(leg, "step1_sl_armed", False),
                    }
                    for leg in self.trade_context.legs
                ],
            }
            with open(self.active_context_file, "w") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            logger.error(f"[AegisEM] Save context failed: {e}")


    def _load_active_context(self):
        if not os.path.exists(self.active_context_file):
            return
        try:
            with open(self.active_context_file, "r") as f:
                data = json.load(f)
            if data.get("status") != "OPEN":
                return
            self.trade_context = TradeContext(
                signal_type=data.get("signal_type", "CALL"),
                strategy="AEGIS_V1",
                index_entry=float(data.get("index_entry") or 0),
            )
            self.trade_context.spot_peak_mfe_pts = float(data.get("spot_peak_mfe_pts", 0.0) or 0.0)
            self.trade_context.spot_peak_mae_pts = float(data.get("spot_peak_mae_pts", 0.0) or 0.0)
            self.trade_context.spot_time_to_mfe_sec = float(data.get("spot_time_to_mfe_sec", 0.0) or 0.0)
            self.trade_context.spot_time_to_mae_sec = float(data.get("spot_time_to_mae_sec", 0.0) or 0.0)
            self.trade_context.spot_at_mfe = float(data.get("spot_at_mfe", 0.0) or self.trade_context.index_entry)
            self.trade_context.spot_at_mae = float(data.get("spot_at_mae", 0.0) or self.trade_context.index_entry)
            self.trade_context.trailing_spot_sl = float(data.get("trailing_spot_sl", 0.0) or 0.0)
            self.trade_context.candle_low = float(data.get("candle_low", 0.0) or 0.0)
            self.trade_context.candle_high = float(data.get("candle_high", 0.0) or 0.0)
            self.trade_context.pillar = int(data.get("pillar", 0) or 0)
            self.trade_context.pa_setup_name = str(data.get("pa_setup_name", "") or "")
            self.trade_context.execution_mode = str(data.get("execution_mode", "") or "")
            self.trade_context.boundary_level = float(data.get("boundary_level", 0.0) or 0.0)
            self.trade_context.decision_payload = data.get("decision_payload", {})
            self.trade_context.entry_bar_bucket = data.get("entry_bar_bucket")
            self.trade_context.spot_sl_price = float(data.get("spot_sl_price", 0.0) or 0.0)
            if data.get("entry_time"):
                entry_dt = datetime.fromisoformat(data["entry_time"])
                age_sec = (datetime.now() - entry_dt).total_seconds()
                if age_sec > MAX_HOLD_EXTENDED_SEC:
                    logger.warning(
                        f"[AegisEM] Stale active context found ({age_sec:.0f}s old > "
                        f"{MAX_HOLD_EXTENDED_SEC:.0f}s). Discarding ghost trade."
                    )
                    if os.path.exists(self.active_context_file):
                        try:
                            os.remove(self.active_context_file)
                        except Exception:
                            pass
                    self.trade_context = None
                    return
                self.trade_context.entry_time = entry_dt
            self.tp_order_id = data.get("tp_order_id")
            self.last_tp_order_id = data.get("last_tp_order_id")
            for ld in data.get("legs") or []:
                opt_type = ld.get("option_type") or ld.get("signal_type") or data.get("signal_type") or "CALL"
                leg = TradeLeg(ld["token"], ld["symbol"], float(ld["strike"]), opt_type,
                               int(ld.get("quantity") or 1), float(ld.get("entry_price") or 0))
                leg.current_price = float(ld.get("current_price") or leg.entry_price)
                leg.status = ld.get("status") or "OPEN"
                leg.initial_tp_pct = float(ld.get("initial_tp_pct") or 0.10)
                leg.tp_pct = float(ld.get("tp_pct") or leg.initial_tp_pct)
                if ld.get("tp_price"):
                    leg.tp_price = float(ld["tp_price"])
                leg.highest_price = float(ld.get("highest_price") or leg.entry_price)
                leg.highest_bid = float(ld.get("highest_bid", leg.entry_price) or leg.entry_price)
                leg.breakeven_locked = bool(ld.get("breakeven_locked", False))
                leg.step1_sl_armed = bool(ld.get("step1_sl_armed", False))
                self.trade_context.add_leg(leg)
            self.trade_context.status = "OPEN"
            logger.warning("[AegisEM] Restored OPEN trade from disk")
        except Exception as e:
            logger.error(f"[AegisEM] Load context failed: {e}")
            self.trade_context = None
