"""
Realistic Paper Broker Module for Production-Parity Backtesting & Simulation.
Provides book sweep simulation, SEBI/NSE statutory option charges, and paper execution adapter.
"""

from dataclasses import dataclass
from typing import List, Tuple, Optional
import math


@dataclass
class OptionCharges:
    brokerage: float
    stt: float
    exchange_charges: float
    stamp_duty: float
    sebi_charges: float
    gst: float
    total_charges: float


def calculate_nse_option_charges(
    buy_turnover: float,
    sell_turnover: float,
    brokerage_per_order: float = 0.0
) -> OptionCharges:
    """
    Calculate statutory and regulatory transaction charges for NSE Options trading.
    Brokerage: Flat per executed order (buy + sell = 2 orders).
    STT: 0.10% on sell turnover.
    Exchange turnover charge: 0.035% on total turnover.
    Stamp duty: 0.003% on buy turnover.
    SEBI turnover fee: 0.0001% on total turnover.
    GST: 18% on (Brokerage + Exchange charges + SEBI charges).
    """
    brokerage = round(2.0 * brokerage_per_order, 2)
    stt = round(sell_turnover * 0.0010, 2)
    exchange_charges = round((buy_turnover + sell_turnover) * 0.00035, 2)
    stamp_duty = round(buy_turnover * 0.00003, 2)
    sebi_charges = round((buy_turnover + sell_turnover) * 0.000001, 2)
    gst = round(0.18 * (brokerage + exchange_charges + sebi_charges), 2)
    total_charges = round(brokerage + stt + exchange_charges + stamp_duty + sebi_charges + gst, 2)

    return OptionCharges(
        brokerage=brokerage,
        stt=stt,
        exchange_charges=exchange_charges,
        stamp_duty=stamp_duty,
        sebi_charges=sebi_charges,
        gst=gst,
        total_charges=total_charges
    )


def simulate_book_sweep(
    side: str,
    required_qty: int,
    prices: List[float],
    quantities: List[int]
) -> Tuple[float, int, float]:
    """
    Simulate sweeping through top-N order book levels.
    side: 'BUY' (sweeping asks ascending) or 'SELL' (sweeping bids descending)
    returns (vwap_price, filled_qty, slippage_bps)
    """
    if required_qty <= 0 or not prices or not quantities:
        return 0.0, 0, 0.0

    rem_qty = required_qty
    total_cost = 0.0
    filled_qty = 0
    top_price = prices[0]

    for px, qty in zip(prices, quantities):
        if rem_qty <= 0:
            break
        alloc = min(rem_qty, qty)
        total_cost += alloc * px
        filled_qty += alloc
        rem_qty -= alloc

    if filled_qty == 0:
        return 0.0, 0, 0.0

    vwap = round(total_cost / filled_qty, 2)
    slip_bps = 0.0
    if top_price > 0:
        if side.upper() == "BUY":
            slip_bps = round(((vwap - top_price) / top_price) * 10000.0, 2)
        else:
            slip_bps = round(((top_price - vwap) / top_price) * 10000.0, 2)

    return vwap, filled_qty, slip_bps


class RealisticPaperBrokerAdapter:
    """Paper execution adapter mimicking passive and aggressive order handling."""

    def __init__(self, execution_log_path: Optional[str] = None):
        self.execution_log_path = execution_log_path

    def evaluate_resting_take_profit(
        self,
        tp_target: float,
        best_bid: float,
        best_bid_qty: int,
        required_qty: int
    ) -> Tuple[bool, float]:
        """
        Evaluate passive limit order fill condition at resting TP target.
        Fills at tp_target when best_bid >= tp_target.
        """
        if best_bid >= tp_target and best_bid_qty > 0:
            return True, float(tp_target)
        return False, 0.0