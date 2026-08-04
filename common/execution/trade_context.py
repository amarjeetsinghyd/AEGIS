from typing import List, Dict, Any
from datetime import datetime
import uuid

class TradeLeg:
    def __init__(self, token: str, symbol: str, strike: float, option_type: str, quantity: int, entry_price: float, broker_order_id: str = None, paper_order_id: str = None):
        self.leg_id = str(uuid.uuid4())
        self.token = token
        self.symbol = symbol
        self.strike = strike
        self.option_type = option_type
        self.quantity = quantity
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

    def close_leg(self, exit_price: float, reason: str):
        self.status = "CLOSED"
        self.exit_time = datetime.now()
        self.exit_price = exit_price
        self.exit_reason = reason
        self.pnl = (self.exit_price - self.entry_price) * self.quantity

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
            "highest_price": self.highest_price
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

        # Entry Microstructure State for Flow-Exit Engine
        d_payload = decision_payload or {}
        self.entry_velocity = float(d_payload.get("velocity", 0.0))
        self.entry_acceleration = float(d_payload.get("acceleration", 0.0))
        self.entry_flowdurationsec = int(d_payload.get("flowdurationsec", d_payload.get("flow_duration_sec", 0)))
        self.entry_tradesigner = str(d_payload.get("tradesigner", d_payload.get("trade_signer", "NEUTRAL")))
        self.entry_zscore = float(d_payload.get("zscore", d_payload.get("z_score", 0.0)))
        self.adverse_flow_checks = 0
        self.recovery_checks = 0
        self.flow_exit_armed = False

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
            "status": self.status,
            "entry_time": self.entry_time.isoformat() if self.entry_time else None,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "total_pnl": self.total_pnl,
            "hwm_profit_pct": self.hwm_profit_pct,
            "active_phase": self.active_phase,
            "decision_payload": self.decision_payload,
            "legs": [leg.to_dict() for leg in self.legs]
        }
