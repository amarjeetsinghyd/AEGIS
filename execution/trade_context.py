from typing import List, Dict, Any
from datetime import datetime
import uuid


def resolve_leg_net_qty(leg_or_qty, lot_size: int) -> int:
    """👑 Single source of truth for the open-position share-units (net_qty).

    ABSOLUTE UNIT INVARIANT (Exit-Quantity Double-Multiplication Shield):
    leg.quantity is canonically stored in LOTS (fixed 1-lot discipline => value 1).
    Every order-egress point (entry, exit, TP, chase, PnL) MUST resolve exact
    share-units through this helper. Never multiply leg.quantity by lot_size twice!

    Defensive path: legacy persisted trade contexts (pre-invariant snapshots) may
    hold share-valued quantity (e.g. 20 for a 1-lot SENSEX after a fillshares
    overwrite). If the value already encodes an exact whole-lot share count
    (>= lot_size and cleanly divisible), it is treated as shares — never
    re-multiplied. This keeps NFO (Nifty=65) and BFO (Sensex=20) contract
    specifications scale-safe alike.
    """
    raw = getattr(leg_or_qty, "quantity", leg_or_qty)
    try:
        raw = int(raw)
    except (TypeError, ValueError):
        raw = 0
    lot = max(int(lot_size or 0), 1)
    if raw <= 0:
        # Degenerate state (None/0): assume the canonical default = 1 lot.
        return lot
    if raw >= lot and raw % lot == 0:
        # Already share-denominated (e.g. broker fillshares reconciliation).
        return raw
    # Canonical LOTS -> exact share-units.
    return raw * lot


class TradeLeg:
    def __init__(self, token: str, symbol: str, strike: float, option_type: str, quantity: int, entry_price: float, broker_order_id: str = None, paper_order_id: str = None):
        self.leg_id = str(uuid.uuid4())
        self.token = token
        self.symbol = symbol
        self.strike = strike
        self.option_type = option_type
        self.quantity = quantity          # Canonical: LOTS (fixed 1-lot discipline)
        self.filled_units = 0             # Broker-verified fillshares (units), re-confirmed at fill time
        self.entry_price = entry_price
        self.current_price = entry_price
        self.broker_order_id = broker_order_id
        self.paper_order_id = paper_order_id
        self.status = "OPEN" # OPEN, CLOSED, REJECTED, PENDING_EXIT
        self.entry_time = datetime.now()
        self.exit_time = None
        self.exit_price = None
        self.exit_reason = None
        self.pnl = 0.0
        
        # SL State Tracking
        self.sl_orderno = None
        self.sl_trigger_price = 0.0
        self.sl_limit_price = 0.0
        self.last_sl_modify_time = 0.0
        self.highest_price = entry_price
        self.lowest_price = entry_price
        self.time_to_highest_sec = 0.0
        self.time_to_lowest_sec = 0.0

    @property
    def realized_pnl(self):
        return self.pnl

    def close_leg(self, exit_price: float, reason: str, lot_size: int = 1):
        self.status = "CLOSED"
        self.exit_time = datetime.now()
        self.exit_price = exit_price
        self.exit_reason = reason
        net_qty = resolve_leg_net_qty(self, lot_size)
        self.pnl = (self.exit_price - self.entry_price) * net_qty

    def to_dict(self) -> Dict[str, Any]:
        return {
            "leg_id": self.leg_id,
            "token": self.token,
            "symbol": self.symbol,
            "strike": self.strike,
            "option_type": self.option_type,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "current_price": self.current_price,
            "broker_order_id": self.broker_order_id,
            "paper_order_id": self.paper_order_id,
            "status": self.status,
            "entry_time": self.entry_time.isoformat() if self.entry_time else None,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
            "pnl": self.pnl,
            "sl_orderno": self.sl_orderno,
            "sl_trigger_price": self.sl_trigger_price,
            "sl_limit_price": self.sl_limit_price,
            "highest_price": self.highest_price,
            "lowest_price": getattr(self, "lowest_price", self.entry_price),
            "time_to_highest_sec": getattr(self, "time_to_highest_sec", 0.0),
            "time_to_lowest_sec": getattr(self, "time_to_lowest_sec", 0.0),
        }


class TradeContext:
    def __init__(self, signal_type: str, strategy: str, index_entry: float, decision_payload: Dict[str, Any] = None):
        self.context_id = str(uuid.uuid4())
        self.signal_type = signal_type
        self.strategy = strategy
        self.index_entry = index_entry
        self.decision_payload = decision_payload
        self.legs: List[TradeLeg] = []
        self.status = "OPEN" # OPEN, CLOSED
        self.entry_time = datetime.now()
        self.exit_time = None
        self.total_pnl = 0.0
        
        # Context-wide tracking
        self.hwm_profit_pct = 0.0
        self.active_phase = "PHASE_1_CONSERVATIVE"

        # Spot Excursion Tracking (Spot MFE / MAE in points and timestamps)
        self.spot_peak_mfe_pts: float = 0.0
        self.spot_peak_mae_pts: float = 0.0
        self.spot_time_to_mfe_sec: float = 0.0
        self.spot_time_to_mae_sec: float = 0.0
        self.spot_at_mfe: float = float(index_entry) if index_entry else 0.0
        self.spot_at_mae: float = float(index_entry) if index_entry else 0.0

    def add_leg(self, leg: TradeLeg):
        self.legs.append(leg)

    def close_context(self):
        self.status = "CLOSED"
        self.exit_time = datetime.now()
        self.total_pnl = sum(leg.pnl for leg in self.legs if leg.status == "CLOSED")

    def all_legs_closed(self) -> bool:
        return all(leg.status in ["CLOSED", "REJECTED"] for leg in self.legs)

    def has_open_trade(self) -> bool:
        return self.status == "OPEN" and any(leg.status == "OPEN" for leg in self.legs)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "context_id": self.context_id,
            "signal_type": self.signal_type,
            "strategy": self.strategy,
            "index_entry": self.index_entry,
            "spot_peak_mfe_pts": self.spot_peak_mfe_pts,
            "spot_peak_mae_pts": self.spot_peak_mae_pts,
            "spot_time_to_mfe_sec": self.spot_time_to_mfe_sec,
            "spot_time_to_mae_sec": self.spot_time_to_mae_sec,
            "spot_at_mfe": self.spot_at_mfe,
            "spot_at_mae": self.spot_at_mae,
            "status": self.status,
            "entry_time": self.entry_time.isoformat() if self.entry_time else None,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "total_pnl": self.total_pnl,
            "hwm_profit_pct": self.hwm_profit_pct,
            "active_phase": self.active_phase,
            "decision_payload": self.decision_payload,
            "legs": [leg.to_dict() for leg in self.legs]
        }
