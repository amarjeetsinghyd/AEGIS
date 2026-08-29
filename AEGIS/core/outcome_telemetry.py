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
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, asdict, field


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
    ) -> None:
        """Starts forward tracking for a newly fired signal/entry."""
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

            if dt <= 300.0:
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
