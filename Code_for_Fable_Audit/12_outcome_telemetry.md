# Source: AEGIS/core/outcome_telemetry.py

`python
"""
AEGIS OutcomeTelemetryEngine — Ground-Truth 5-Minute Outcome & MFE/MAE Research Telemetry.

Architecture:
1. Tracks exact Option Price forward outcomes at 30s, 60s, 180s, and 300s (5-minute horizon).
2. Calculates:
   - Maximum Favorable Excursion (MFE) %
   - Maximum Adverse Excursion (MAE) %
   - Target +5% Hit & Time-to-Target (seconds)
   - Target +8% Hit & Time-to-Target (seconds)
   - MAE before Target hit
3. Persists completed research records into structured JSONL files for offline calibration.
"""

from __future__ import annotations
import json
import os
import time
import logging
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, asdict, field

logger = logging.getLogger("aegis_outcome_telemetry")


@dataclass
class ForwardOutcomeRecord:
    event_id: str
    symbol: str
    direction: str
    entry_ts: float
    entry_opt_price: float
    entry_spot_price: float
    eqs_score: float
    features_snapshot: Dict[str, Any] = field(default_factory=dict)

    # 30s Horizon
    mfe_30s_pct: float = 0.0
    mae_30s_pct: float = 0.0

    # 60s Horizon
    mfe_60s_pct: float = 0.0
    mae_60s_pct: float = 0.0

    # 180s (3-min) Horizon
    mfe_180s_pct: float = 0.0
    mae_180s_pct: float = 0.0

    # 300s (5-min) Horizon
    mfe_300s_pct: float = 0.0
    mae_300s_pct: float = 0.0

    # Target Hits
    hit_tp_5pct: bool = False
    time_to_tp_5pct_sec: float = 0.0
    hit_tp_8pct: bool = False
    time_to_tp_8pct_sec: float = 0.0
    mae_before_tp_5pct: float = 0.0

    trade_status: str = "FIRED"  # "FIRED", "DROPPED_BY_ML", "SHADOW_VETO"
    is_complete: bool = False


class OutcomeTelemetryEngine:
    """
    Online forward-outcome evaluation engine tracking the exact 5-minute performance of every signal.
    """

    def __init__(self, log_dir: str = "AEGIS/logs/telemetry"):
        self.log_dir = log_dir
        os.makedirs(self.log_dir, exist_ok=True)
        self.active_records: Dict[str, ForwardOutcomeRecord] = {}

    def register_entry(
        self,
        event_id: str,
        symbol: str,
        direction: str,
        entry_ts: float,
        entry_opt_price: float,
        entry_spot_price: float,
        eqs_score: float,
        features_snapshot: Optional[Dict[str, Any]] = None,
        trade_status: str = "FIRED",
    ) -> None:
        """Starts forward tracking for a newly fired signal/entry or dropped trade."""
        if entry_opt_price <= 0:
            return

        record = ForwardOutcomeRecord(
            event_id=event_id,
            symbol=symbol,
            direction=direction,
            entry_ts=entry_ts,
            entry_opt_price=entry_opt_price,
            entry_spot_price=entry_spot_price,
            eqs_score=eqs_score,
            features_snapshot=features_snapshot or {},
            trade_status=trade_status,
        )
        self.active_records[event_id] = record

    def update_tick(self, symbol: str, current_opt_price: float, now_sec: float) -> List[ForwardOutcomeRecord]:
        """
        Updates in-flight outcome records on every new option tick.
        Returns any records that completed their 300s (5-min) evaluation window.
        """
        completed: List[ForwardOutcomeRecord] = []
        if current_opt_price <= 0:
            return completed

        to_remove = []
        for event_id, rec in self.active_records.items():
            if rec.symbol != symbol:
                continue

            dt = now_sec - rec.entry_ts
            if dt < 0:
                continue

            ret_pct = ((current_opt_price - rec.entry_opt_price) / rec.entry_opt_price) * 100.0

            # Update MFE / MAE
            if dt <= 30.0:
                if ret_pct > rec.mfe_30s_pct:
                    rec.mfe_30s_pct = ret_pct
                if ret_pct < rec.mae_30s_pct:
                    rec.mae_30s_pct = ret_pct

            if dt <= 60.0:
                if ret_pct > rec.mfe_60s_pct:
                    rec.mfe_60s_pct = ret_pct
                if ret_pct < rec.mae_60s_pct:
                    rec.mae_60s_pct = ret_pct

            if dt <= 180.0:
                if ret_pct > rec.mfe_180s_pct:
                    rec.mfe_180s_pct = ret_pct
                if ret_pct < rec.mae_180s_pct:
                    rec.mae_180s_pct = ret_pct

            if dt <= 300.0 or not rec.is_complete:
                if ret_pct > rec.mfe_300s_pct:
                    rec.mfe_300s_pct = ret_pct
                if ret_pct < rec.mae_300s_pct:
                    rec.mae_300s_pct = ret_pct

                # Target Hit Detection (+5% and +8%)
                if ret_pct >= 5.0 and not rec.hit_tp_5pct:
                    rec.hit_tp_5pct = True
                    rec.time_to_tp_5pct_sec = dt
                    rec.mae_before_tp_5pct = rec.mae_300s_pct

                if ret_pct >= 8.0 and not rec.hit_tp_8pct:
                    rec.hit_tp_8pct = True
                    rec.time_to_tp_8pct_sec = dt

            # If 300s window has finished, mark complete and persist
            if dt >= 300.0:
                rec.is_complete = True
                completed.append(rec)
                to_remove.append(event_id)
                self._persist_record(rec)

        for eid in to_remove:
            del self.active_records[eid]

        return completed

    def _persist_record(self, record: ForwardOutcomeRecord) -> None:
        """Appends completed outcome record to daily JSONL file."""
        date_str = time.strftime("%Y-%m-%d", time.localtime(record.entry_ts))
        filepath = os.path.join(self.log_dir, f"aegis_5min_outcomes_{date_str}.jsonl")
        try:
            with open(filepath, "a") as f:
                f.write(json.dumps(asdict(record)) + "\n")
        except Exception:
            pass

        if record.trade_status in ("DROPPED_BY_ML", "SHADOW_VETO"):
            verdict = "MISSED_WINNER" if record.hit_tp_5pct else "TRAP_AVOIDED"
            logger.info(
                f"[DROPPED TRADE 5M AUDIT] {record.symbol} ({record.direction}) | "
                f"MFE_5m: +{record.mfe_300s_pct:.1f}% | MAE_5m: {record.mae_300s_pct:.1f}% | "
                f"Verdict: {verdict} ({record.trade_status})"
            )


# ═══════════════════════════════════════════════════════════════════════════
# AEGIS PostTradeMilestoneTracker — 60-Second Extended MFE & Monotonicity
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class PostTradeMilestoneRecord:
    trade_id: str
    symbol: str
    direction: str
    entry_price: float
    entry_ts: float
    entry_time_str: str = ""
    setup_name: str = ""
    pillar: int = 0
    mode: str = ""

    # Live broker execution state
    exit_price: float = 0.0
    exit_ts: float = 0.0
    exit_reason: str = "RUNNING"
    is_closed_in_broker: bool = False

    # 60s Milestone Hit Flags & Timings (Seconds from Entry Fill)
    hit_4pct: bool = False
    time_to_4pct_sec: float = 0.0

    hit_5pct: bool = False
    time_to_5pct_sec: float = 0.0
    sec_4_to_5: float = 0.0          # Time taken from 4% to 5%

    hit_6pct: bool = False
    time_to_6pct_sec: float = 0.0
    sec_5_to_6: float = 0.0          # Time taken from 5% to 6%

    hit_7pct: bool = False
    time_to_7pct_sec: float = 0.0
    sec_6_to_7: float = 0.0          # Time taken from 6% to 7%

    hit_8pct: bool = False
    time_to_8pct_sec: float = 0.0

    hit_10pct: bool = False
    time_to_10pct_sec: float = 0.0

    # 60-Second Scalp Horizon Overall Excursions
    peak_mfe_60s_pct: float = 0.0
    time_to_peak_mfe_sec: float = 0.0
    peak_mae_60s_pct: float = 0.0
    time_to_peak_mae_sec: float = 0.0

    # Flow & Monotonicity Dynamics (क्या एक ही फ्लो में गया या ऊपर-नीचे हुआ)
    max_pullback_4_to_5_pct: float = 0.0
    max_pullback_5_to_6_pct: float = 0.0
    max_pullback_6_to_7_pct: float = 0.0

    # Post-4% Structural Health
    max_pullback_post_4pct: float = 0.0         # Peak drawdown experienced after passing 4%
    retrace_below_4pct_count: int = 0           # Times price dipped back below +4.0%
    dropped_below_entry_post_4pct: bool = False # Did price crash back below 0% after 4%?
    min_pct_post_4pct: float = 0.0              # Deepest dip after passing 4%

    # Qualitative Flow Character Classification
    flow_character: str = "PENDING"
    # "CLEAN_MONOTONIC_FLOW"  : Hit 7% within 60s, Pullback <= 0.35%, direct institutional surge
    # "STAIR_STEP_CONTINUATION": Hit 7% within 60s, Pullback 0.35% - 1.0%
    # "CHOPPY_EXPANSION"      : Hit 7% within 60s, Pullback > 1.0% or multiple 4% retests
    # "MOMENTUM_STALL_60S"    : Hit 4% or 5%, stalled and failed to reach 7% within 60s
    # "PUMP_AND_DUMP_TRAP"    : Hit 4%, then dumped back below entry within 60s
    # "NEVER_REACHED_4PCT"    : Failed to even reach 4% in 60s

    # Tick-by-Tick Path: [(relative_sec, ltp, return_pct)]
    tick_trajectory: List[Tuple[float, float, float]] = field(default_factory=list)

    # 5-Minute Horizon Checkpoint Returns (% from Entry)
    ret_30s_pct: Optional[float] = None
    ret_60s_pct: Optional[float] = None
    ret_120s_pct: Optional[float] = None
    ret_180s_pct: Optional[float] = None
    ret_300s_pct: Optional[float] = None

    # Post-Exit / Post-SL Recovery Telemetry (5-Minute Horizon)
    exit_pnl_pct: float = 0.0                   # Actual PnL % at exit
    post_exit_lowest_pct: float = 0.0           # Deepest dip after exit
    post_exit_highest_pct: float = 0.0          # Highest bounce after exit
    sl_recovered_to_be: bool = False            # Did price bounce back to Entry Price (>= 0.0%)?
    time_to_be_sec: float = 0.0                 # Seconds from entry when BE reached
    sl_recovered_to_tp: bool = False            # Did price bounce back to +4.0% TP?
    time_to_tp_sec: float = 0.0                 # Seconds from entry when TP reached
    post_sl_verdict: str = "N/A"
    # "JUSTIFIED_DISASTER_SAVE": Price crashed deeper post-exit (SL saved capital)
    # "DEAD_TRADE_STAGNATION"  : Price hovered negative, never recovered
    # "BOUNCED_TO_BREAKEVEN"   : Price bounced back to BE, but didn't reach TP
    # "PREMATURE_CHOKE_RECOVERED_TO_TP": Price bounced back and reached TP (+4%)
    # "WINNER_MAKER_TP_TAKEN"  : Normal winning trade exited on TP

    is_complete: bool = False
    tracking_window_sec: float = 300.0

    # Internal tracking helpers (not persisted in summary)
    _highest_after_4pct: float = 0.0
    _peak_4_to_5: float = 0.0
    _peak_5_to_6: float = 0.0
    _peak_6_to_7: float = 0.0
    _currently_below_4pct: bool = False

    def to_dict(self, include_trajectory: bool = True) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("_highest_after_4pct", None)
        d.pop("_peak_4_to_5", None)
        d.pop("_peak_5_to_6", None)
        d.pop("_peak_6_to_7", None)
        d.pop("_currently_below_4pct", None)
        if not include_trajectory:
            d.pop("tick_trajectory", None)
        return d


class PostTradeMilestoneTracker:
    """
    Dedicated 60-Second Post-Trade Milestone and Flow Monotonicity Tracker.
    Monitors all executed trades across the scalp window regardless of broker exit time.
    """

    def __init__(
        self,
        log_dir: str = "AEGIS/logs/telemetry",
        history_file: str = "data/aegis_trade_history.json",
        tracking_window_sec: Optional[float] = None,
    ):
        self.log_dir = log_dir
        self.history_file = history_file
        os.makedirs(self.log_dir, exist_ok=True)
        self.tracking_window_sec = (
            float(tracking_window_sec)
            if tracking_window_sec is not None
            else float(os.getenv("AEGIS_POST_TRADE_TRACK_SEC", "300.0"))
        )
        self.active_records: Dict[str, PostTradeMilestoneRecord] = {}

    def register_executed_trade(
        self,
        trade_id: str,
        symbol: str,
        direction: str,
        entry_price: float,
        entry_ts: float,
        setup_name: str = "",
        pillar: int = 0,
        mode: str = "",
    ) -> Optional[PostTradeMilestoneRecord]:
        """Registers an executed trade for 5-minute (300s) milestone & post-SL recovery tracking."""
        if entry_price <= 0:
            return None

        entry_time_str = time.strftime("%d %b %Y %H:%M:%S", time.localtime(entry_ts))
        record = PostTradeMilestoneRecord(
            trade_id=trade_id,
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            entry_ts=entry_ts,
            entry_time_str=entry_time_str,
            setup_name=setup_name,
            pillar=pillar,
            mode=mode,
            tracking_window_sec=self.tracking_window_sec,
        )
        self.active_records[trade_id] = record
        logger.info(
            f"[PostTradeTracker] Registered executed trade {trade_id} ({symbol} {direction} @ ₹{entry_price:.2f}) "
            f"for {self.tracking_window_sec:.0f}s milestone & recovery tracking"
        )
        return record

    def notify_trade_exit(
        self,
        symbol: str,
        exit_price: float,
        exit_ts: float,
        exit_reason: str,
    ) -> None:
        """Notifies the tracker of a broker-side trade exit without interrupting the 5-minute observation window."""
        for rec in self.active_records.values():
            if rec.symbol == symbol and not rec.is_closed_in_broker:
                rec.exit_price = exit_price
                rec.exit_ts = exit_ts
                rec.exit_reason = exit_reason
                rec.is_closed_in_broker = True
                if rec.entry_price > 0:
                    rec.exit_pnl_pct = round(((exit_price - rec.entry_price) / rec.entry_price) * 100.0, 2)
                    rec.post_exit_lowest_pct = rec.exit_pnl_pct
                    rec.post_exit_highest_pct = rec.exit_pnl_pct
                logger.info(
                    f"[PostTradeTracker] Trade {rec.trade_id} closed at broker ({exit_reason} @ ₹{exit_price:.2f}, "
                    f"PnL: {rec.exit_pnl_pct:+.1f}%) — {self.tracking_window_sec:.0f}s post-exit recovery audit running"
                )

    def on_option_tick(
        self,
        symbol: str,
        current_opt_price: float,
        now_sec: float,
    ) -> List[PostTradeMilestoneRecord]:
        """Updates active milestone records on every option tick. Returns records completed on this tick."""
        completed: List[PostTradeMilestoneRecord] = []
        if current_opt_price <= 0 or not self.active_records:
            return completed

        to_remove = []
        for trade_id, rec in self.active_records.items():
            if rec.symbol != symbol or rec.is_complete:
                continue

            dt = now_sec - rec.entry_ts
            if dt < 0:
                continue

            ret_pct = ((current_opt_price - rec.entry_price) / rec.entry_price) * 100.0

            # 1. Update overall 60s MFE / MAE
            if ret_pct > rec.peak_mfe_60s_pct:
                rec.peak_mfe_60s_pct = round(ret_pct, 2)
                rec.time_to_peak_mfe_sec = round(dt, 2)
            if ret_pct < rec.peak_mae_60s_pct:
                rec.peak_mae_60s_pct = round(ret_pct, 2)
                rec.time_to_peak_mae_sec = round(dt, 2)

            # 2. Check milestone hits (+4%, +5%, +6%, +7%, +8%, +10%)
            if ret_pct >= 4.0 and not rec.hit_4pct:
                rec.hit_4pct = True
                rec.time_to_4pct_sec = round(dt, 2)
                rec._highest_after_4pct = ret_pct
                rec._peak_4_to_5 = ret_pct

            if ret_pct >= 5.0 and not rec.hit_5pct:
                rec.hit_5pct = True
                rec.time_to_5pct_sec = round(dt, 2)
                rec.sec_4_to_5 = round(rec.time_to_5pct_sec - rec.time_to_4pct_sec, 2) if rec.hit_4pct else 0.0
                rec._peak_5_to_6 = ret_pct

            if ret_pct >= 6.0 and not rec.hit_6pct:
                rec.hit_6pct = True
                rec.time_to_6pct_sec = round(dt, 2)
                rec.sec_5_to_6 = round(rec.time_to_6pct_sec - rec.time_to_5pct_sec, 2) if rec.hit_5pct else 0.0
                rec._peak_6_to_7 = ret_pct

            if ret_pct >= 7.0 and not rec.hit_7pct:
                rec.hit_7pct = True
                rec.time_to_7pct_sec = round(dt, 2)
                rec.sec_6_to_7 = round(rec.time_to_7pct_sec - rec.time_to_6pct_sec, 2) if rec.hit_6pct else 0.0

            if ret_pct >= 8.0 and not rec.hit_8pct:
                rec.hit_8pct = True
                rec.time_to_8pct_sec = round(dt, 2)

            if ret_pct >= 10.0 and not rec.hit_10pct:
                rec.hit_10pct = True
                rec.time_to_10pct_sec = round(dt, 2)

            # 3. Flow Monotonicity & Pullback Measurements
            if rec.hit_4pct:
                if ret_pct > rec._highest_after_4pct:
                    rec._highest_after_4pct = ret_pct
                pb_now = rec._highest_after_4pct - ret_pct
                if pb_now > rec.max_pullback_post_4pct:
                    rec.max_pullback_post_4pct = round(pb_now, 2)

                if ret_pct < rec.min_pct_post_4pct:
                    rec.min_pct_post_4pct = round(ret_pct, 2)

                if ret_pct < 0.0:
                    rec.dropped_below_entry_post_4pct = True

                # Retrace below 4% count
                if ret_pct < 4.0 and not rec._currently_below_4pct:
                    rec.retrace_below_4pct_count += 1
                    rec._currently_below_4pct = True
                elif ret_pct >= 4.0:
                    rec._currently_below_4pct = False

            # Segment pullbacks
            if rec.hit_4pct and not rec.hit_5pct:
                if ret_pct > rec._peak_4_to_5:
                    rec._peak_4_to_5 = ret_pct
                pb_4_5 = rec._peak_4_to_5 - ret_pct
                if pb_4_5 > rec.max_pullback_4_to_5_pct:
                    rec.max_pullback_4_to_5_pct = round(pb_4_5, 2)

            if rec.hit_5pct and not rec.hit_6pct:
                if ret_pct > rec._peak_5_to_6:
                    rec._peak_5_to_6 = ret_pct
                pb_5_6 = rec._peak_5_to_6 - ret_pct
                if pb_5_6 > rec.max_pullback_5_to_6_pct:
                    rec.max_pullback_5_to_6_pct = round(pb_5_6, 2)

            if rec.hit_6pct and not rec.hit_7pct:
                if ret_pct > rec._peak_6_to_7:
                    rec._peak_6_to_7 = ret_pct
                pb_6_7 = rec._peak_6_to_7 - ret_pct
                if pb_6_7 > rec.max_pullback_6_to_7_pct:
                    rec.max_pullback_6_to_7_pct = round(pb_6_7, 2)

            # 4. Checkpoint Horizon Returns (% from Entry)
            if dt >= 30.0 and rec.ret_30s_pct is None:
                rec.ret_30s_pct = round(ret_pct, 2)
            if dt >= 60.0 and rec.ret_60s_pct is None:
                rec.ret_60s_pct = round(ret_pct, 2)
            if dt >= 120.0 and rec.ret_120s_pct is None:
                rec.ret_120s_pct = round(ret_pct, 2)
            if dt >= 180.0 and rec.ret_180s_pct is None:
                rec.ret_180s_pct = round(ret_pct, 2)
            if dt >= 300.0 and rec.ret_300s_pct is None:
                rec.ret_300s_pct = round(ret_pct, 2)

            # 5. Post-Exit / Post-SL Recovery Telemetry Tracking
            if rec.is_closed_in_broker:
                if ret_pct < rec.post_exit_lowest_pct:
                    rec.post_exit_lowest_pct = round(ret_pct, 2)
                if ret_pct > rec.post_exit_highest_pct:
                    rec.post_exit_highest_pct = round(ret_pct, 2)

                # Check if a stopped-out trade recovered during remaining horizon
                if rec.exit_pnl_pct < 0:
                    if ret_pct >= 0.0 and not rec.sl_recovered_to_be:
                        rec.sl_recovered_to_be = True
                        rec.time_to_be_sec = round(dt, 2)
                    if ret_pct >= 4.0 and not rec.sl_recovered_to_tp:
                        rec.sl_recovered_to_tp = True
                        rec.time_to_tp_sec = round(dt, 2)

            # 6. Tick Path Trajectory Recording (Capped at 1500 ticks)
            if len(rec.tick_trajectory) < 1500:
                rec.tick_trajectory.append((round(dt, 2), round(current_opt_price, 2), round(ret_pct, 2)))

            # 7. Window Expiry (dt >= tracking_window_sec)
            if dt >= rec.tracking_window_sec:
                self._finalize_record(rec)
                completed.append(rec)
                to_remove.append(trade_id)

        for tid in to_remove:
            del self.active_records[tid]

        return completed

    def _finalize_record(self, record: PostTradeMilestoneRecord) -> None:
        """Classifies flow character, writes to daily JSONL, and enriches trade history."""
        record.is_complete = True

        # Flow character attribution
        if record.hit_7pct:
            if record.max_pullback_post_4pct <= 0.35:
                record.flow_character = "CLEAN_MONOTONIC_FLOW"
            elif record.max_pullback_post_4pct <= 1.0 and record.retrace_below_4pct_count == 0:
                record.flow_character = "STAIR_STEP_CONTINUATION"
            else:
                record.flow_character = "CHOPPY_EXPANSION"
        elif record.hit_4pct:
            if record.dropped_below_entry_post_4pct:
                record.flow_character = "PUMP_AND_DUMP_TRAP"
            else:
                record.flow_character = "MOMENTUM_STALL_60S"
        else:
            record.flow_character = "NEVER_REACHED_4PCT"

        # Post-Exit / Post-SL Recovery Verdict Attribution
        if not record.is_closed_in_broker or record.exit_pnl_pct >= 0:
            record.post_sl_verdict = "WINNER_MAKER_TP_TAKEN" if record.hit_4pct else "NORMAL_PROFIT_EXIT"
        else:
            # Trade was stopped out at a loss
            if record.sl_recovered_to_tp:
                record.post_sl_verdict = "PREMATURE_CHOKE_RECOVERED_TO_TP"
            elif record.sl_recovered_to_be:
                record.post_sl_verdict = "BOUNCED_TO_BREAKEVEN"
            elif record.post_exit_lowest_pct <= (record.exit_pnl_pct - 2.5):
                record.post_sl_verdict = "JUSTIFIED_DISASTER_SAVE"
            else:
                record.post_sl_verdict = "DEAD_TRADE_STAGNATION"

        # 1. Append to daily research JSONL
        date_str = time.strftime("%Y-%m-%d", time.localtime(record.entry_ts))
        filepath = os.path.join(self.log_dir, f"aegis_post_trade_milestones_{date_str}.jsonl")
        try:
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(record.to_dict(include_trajectory=True)) + "\n")
        except Exception as e_jsonl:
            logger.warning(f"[PostTradeTracker] JSONL write failed: {e_jsonl}")

        # 2. Enrich closed trade in data/aegis_trade_history.json
        self._enrich_trade_history(record)

        # 3. High-visibility summary log
        hit_4_str = f"YES ({record.time_to_4pct_sec:.1f}s)" if record.hit_4pct else "NO"
        hit_5_str = f"YES (+{record.sec_4_to_5:.1f}s)" if record.hit_5pct else "NO"
        hit_6_str = f"YES (+{record.sec_5_to_6:.1f}s)" if record.hit_6pct else "NO"
        hit_7_str = f"YES (+{record.sec_6_to_7:.1f}s)" if record.hit_7pct else "NO"

        logger.info(
            f"🎯 [AEGIS POST-TRADE MILESTONE] {record.symbol} ({record.direction}) | Entry: ₹{record.entry_price:.2f} | "
            f"4%: {hit_4_str} | 5%: {hit_5_str} | 6%: {hit_6_str} | 7%: {hit_7_str} | "
            f"Peak: +{record.peak_mfe_60s_pct:.1f}% | Flow: {record.flow_character}"
        )

        # 4. Dedicated Post-SL 5-Minute Recovery Audit Log
        if record.exit_pnl_pct < 0:
            if record.post_sl_verdict == "JUSTIFIED_DISASTER_SAVE":
                logger.info(
                    f"🛡️ [POST-SL 5M AUDIT] {record.symbol} | Exited @ {record.exit_pnl_pct:+.1f}% | "
                    f"Plunged deeper to {record.post_exit_lowest_pct:+.1f}% (Post-exit bounce max: {record.post_exit_highest_pct:+.1f}%) | "
                    f"VERDICT: JUSTIFIED_DISASTER_SAVE (Capital Protected!)"
                )
            elif record.post_sl_verdict == "PREMATURE_CHOKE_RECOVERED_TO_TP":
                logger.warning(
                    f"⚠️ [POST-SL 5M AUDIT] {record.symbol} | Exited @ {record.exit_pnl_pct:+.1f}% | "
                    f"Recovered to BE @ {record.time_to_be_sec:.1f}s | Hit +4% TP @ {record.time_to_tp_sec:.1f}s | "
                    f"VERDICT: PREMATURE_CHOKE_RECOVERED_TO_TP"
                )
            elif record.post_sl_verdict == "BOUNCED_TO_BREAKEVEN":
                logger.info(
                    f"🔄 [POST-SL 5M AUDIT] {record.symbol} | Exited @ {record.exit_pnl_pct:+.1f}% | "
                    f"Bounced to BE ({record.post_exit_highest_pct:+.1f}%) but stalled below TP | "
                    f"VERDICT: BOUNCED_TO_BREAKEVEN"
                )
            else:
                logger.info(
                    f"💀 [POST-SL 5M AUDIT] {record.symbol} | Exited @ {record.exit_pnl_pct:+.1f}% | "
                    f"Stagnated in loss (Lowest: {record.post_exit_lowest_pct:+.1f}%, Highest: {record.post_exit_highest_pct:+.1f}%) | "
                    f"VERDICT: DEAD_TRADE_STAGNATION"
                )

    def _enrich_trade_history(self, record: PostTradeMilestoneRecord) -> None:
        """Enriches the matching trade record in trade_history.json with milestone analytics."""
        target_file = None
        for cand in [self.history_file, "AEGIS/data/trades/trade_history.json", "data/aegis_trade_history.json"]:
            if cand and os.path.exists(cand):
                target_file = cand
                break
        if not target_file:
            return

        try:
            with open(target_file, "r", encoding="utf-8") as f:
                history = json.load(f)

            if not isinstance(history, list) or not history:
                return

            updated = False
            summary_dict = record.to_dict(include_trajectory=False)
            audit_5m = {
                "exit_pnl_pct": record.exit_pnl_pct,
                "post_exit_lowest_pct": record.post_exit_lowest_pct,
                "post_exit_highest_pct": record.post_exit_highest_pct,
                "sl_recovered_to_be": record.sl_recovered_to_be,
                "time_to_be_sec": record.time_to_be_sec,
                "sl_recovered_to_tp": record.sl_recovered_to_tp,
                "time_to_tp_sec": record.time_to_tp_sec,
                "post_sl_verdict": record.post_sl_verdict,
                "ret_30s_pct": record.ret_30s_pct,
                "ret_60s_pct": record.ret_60s_pct,
                "ret_120s_pct": record.ret_120s_pct,
                "ret_180s_pct": record.ret_180s_pct,
                "ret_300s_pct": record.ret_300s_pct,
            }

            # Match by symbol and entry_time / date
            for entry in reversed(history):
                sym = entry.get("symbol", "")
                etime = entry.get("entry_time", "") or entry.get("date", "")
                if sym == record.symbol and (record.entry_time_str in etime or not etime):
                    entry["extended_60s_milestones"] = summary_dict
                    entry["post_exit_5m_audit"] = audit_5m
                    updated = True
                    break

            # Fallback: if not matched by entry_time_str, update the latest trade with matching symbol
            if not updated:
                for entry in reversed(history):
                    if entry.get("symbol") == record.symbol:
                        entry["extended_60s_milestones"] = summary_dict
                        entry["post_exit_5m_audit"] = audit_5m
                        updated = True
                        break

            if updated:
                with open(target_file, "w", encoding="utf-8") as f:
                    json.dump(history, f, indent=2)
                logger.debug(f"[PostTradeTracker] Enriched {target_file} with milestones for {record.symbol}")
        except Exception as e_hist:
            logger.warning(f"[PostTradeTracker] Trade history enrichment failed: {e_hist}")


`
