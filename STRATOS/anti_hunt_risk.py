"""
anti_hunt_risk.py — Two-Way Anti-Liquidity Hunting Risk Engine & Position Sizing.
1. Long: Stop Loss below Support / Rejection Low - 0.15% buffer. Targets above entry.
2. Short: Stop Loss above Resistance / Rejection High + 0.15% buffer. Targets below entry.
3. 1% Risk Sizing with 5x Intraday MIS Leverage.
"""
from __future__ import annotations
import numpy as np
from STRATOS.config import STRATOS_CONFIG

def compute_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < 2:
        return float(closes[-1] * 0.005) if len(closes) > 0 else 1.0
    effective_period = min(period, len(closes) - 1)
    tr = np.maximum(highs[1:] - lows[1:], np.maximum(abs(highs[1:] - closes[:-1]), abs(lows[1:] - closes[:-1])))
    return float(np.mean(tr[-effective_period:]))

class AntiHuntRiskEngine:
    def __init__(self, cfg: dict = None):
        self.cfg = cfg or STRATOS_CONFIG

    def calculate_trade_levels(
        self,
        entry_price: float,
        level: float = None,
        rejection_extreme: float = None,
        arrays: dict = None,
        side: str = "BUY",
        support_level: float = None,
        rejection_low: float = None
    ) -> dict:
        if level is None:
            level = support_level if support_level is not None else entry_price * 0.995
        if rejection_extreme is None:
            rejection_extreme = rejection_low if rejection_low is not None else level

        highs = arrays.get("high", np.empty(0)) if arrays else np.empty(0)
        lows = arrays.get("low", np.empty(0)) if arrays else np.empty(0)
        closes = arrays.get("close", np.empty(0)) if arrays else np.empty(0)

        atr = compute_atr(highs, lows, closes, 14)
        buffer = max(entry_price * self.cfg.get("anti_hunt_buffer_pct", 0.0015), atr * self.cfg.get("anti_hunt_atr_fraction", 0.25))

        if side == "BUY":
            structural_floor = min(level, rejection_extreme)
            stop_loss = structural_floor - buffer
            risk_dist = entry_price - stop_loss
            if risk_dist <= 0.05:
                risk_dist = entry_price * 0.0025
                stop_loss = entry_price - risk_dist

            tp1_dist = risk_dist * self.cfg.get("min_reward_risk_ratio", 2.0)
            tp2_dist = risk_dist * self.cfg.get("tp2_target_rr", 3.0)

            tp1_price = entry_price + tp1_dist
            tp2_price = entry_price + tp2_dist
        else: # SELL (Short)
            structural_ceiling = max(level, rejection_extreme)
            stop_loss = structural_ceiling + buffer
            risk_dist = stop_loss - entry_price
            if risk_dist <= 0.05:
                risk_dist = entry_price * 0.0025
                stop_loss = entry_price + risk_dist

            tp1_dist = risk_dist * self.cfg.get("min_reward_risk_ratio", 2.0)
            tp2_dist = risk_dist * self.cfg.get("tp2_target_rr", 3.0)

            tp1_price = entry_price - tp1_dist
            tp2_price = entry_price - tp2_dist

        return {
            "entry_price": round(entry_price, 2),
            "side": side,
            "stop_loss": round(stop_loss, 2),
            "risk_dist": round(risk_dist, 2),
            "risk_pct": round((risk_dist / entry_price) * 100.0, 3),
            "tp1_price": round(tp1_price, 2),
            "tp2_price": round(tp2_price, 2),
            "reward_risk": self.cfg.get("min_reward_risk_ratio", 2.0),
            "atr": round(atr, 2),
            "buffer_applied": round(buffer, 2)
        }

    def calculate_position_size(self, capital: float, entry_price: float, risk_dist: float, max_risk_per_trade_pct: float = 0.01) -> int:
        if entry_price <= 0 or risk_dist <= 0:
            return 0
        risk_capital = capital * max_risk_per_trade_pct
        qty_risk = int(risk_capital / max(risk_dist, 0.05))
        
        margin_budget = capital * self.cfg.get("leverage", 5.0) * self.cfg.get("margin_safety", 1.0)
        qty_margin = int(margin_budget / entry_price)
        
        qty = max(1, min(qty_risk, qty_margin))
        return qty
