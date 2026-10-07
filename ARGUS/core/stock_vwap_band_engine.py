"""
ARGUS StockVwapBandEngine — Intraday Pain Zone VWAP Band & Early Structural Veto (v5.0).
Directly ported from AEGIS v1.0 Production Architecture with stock-specific scale-invariance.

Early Directional Filter Mandate:
  - Spot ABOVE Upper Band  => ONLY BUY (Long) trades permitted.
  - Spot BELOW Lower Band  => ONLY SELL (Short) trades permitted.
  - Spot INSIDE Band       => DISCARD / VETO (Chop Zone). Eliminates >60% false intraday churn.
"""

from __future__ import annotations

import logging
from typing import Tuple, Dict, Any, Optional

try:
    from common.logger import get_logger
    logger = get_logger("argus_vwap_band")
except Exception:
    logger = logging.getLogger("argus_vwap_band")


class StockVwapBandEngine:
    """
    Computes intraday volume-weighted average price (VWAP) and maintains
    a dynamic ATR-scaled Pain Zone Band (+/- 0.80x ATR).
    """

    def __init__(self, symbol: str = "EQUITY", band_atr_mult: float = 0.80):
        self.symbol = symbol
        self.band_atr_mult = band_atr_mult

        # VWAP Accumulators
        self.cum_vol_price: float = 0.0
        self.cum_vol: float = 0.0
        self.vwap: float = 0.0

        # Band metrics
        self.upper_band: float = 0.0
        self.lower_band: float = 0.0
        self.current_atr: float = 0.0
        self.last_spot: float = 0.0

    def reset(self) -> None:
        """Reset state at intraday market open (09:15 IST)."""
        self.cum_vol_price = 0.0
        self.cum_vol = 0.0
        self.vwap = 0.0
        self.upper_band = 0.0
        self.lower_band = 0.0
        self.current_atr = 0.0
        self.last_spot = 0.0

    def update_tick(self, price: float, volume_delta: float, atr: float = 0.0) -> float:
        """
        Updates VWAP accumulators with incremental volume.
        Returns the updated VWAP.
        """
        if price <= 0:
            return self.vwap

        self.last_spot = price
        if atr > 0:
            self.current_atr = atr

        if volume_delta > 0:
            self.cum_vol_price += price * volume_delta
            self.cum_vol += volume_delta
            if self.cum_vol > 0:
                self.vwap = self.cum_vol_price / self.cum_vol

        # Update bands if ATR available
        if self.vwap > 0 and self.current_atr > 0:
            band_half = self.current_atr * self.band_atr_mult
            self.upper_band = self.vwap + band_half
            self.lower_band = self.vwap - band_half

        return self.vwap

    def check_direction_veto(self, direction: str, current_price: Optional[float] = None) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Validates trade direction against the Pain Zone VWAP Band.
        Returns: (passed, reason, metrics_dict)
        """
        price = current_price or self.last_spot
        if price <= 0 or self.vwap <= 0 or self.upper_band <= 0 or self.lower_band <= 0:
            # Permissive if VWAP not yet initialized (e.g. first 2 minutes)
            return True, "vwap_initializing", {
                "spot": price,
                "vwap": self.vwap,
                "upper_band": self.upper_band,
                "lower_band": self.lower_band,
            }

        metrics = {
            "spot": round(price, 2),
            "vwap": round(self.vwap, 2),
            "upper_band": round(self.upper_band, 2),
            "lower_band": round(self.lower_band, 2),
            "dist_vwap_bps": round((price - self.vwap) / self.vwap * 10000, 1),
        }

        # Inside Pain Zone Band => VETO (Chop trap)
        if self.lower_band <= price <= self.upper_band:
            return False, "inside_pain_zone_band", metrics

        # Above Upper Band => ONLY BUY permitted
        if price > self.upper_band:
            if direction == "BUY":
                return True, "above_upper_band", metrics
            else:
                return False, "short_vetoed_above_upper_band", metrics

        # Below Lower Band => ONLY SELL permitted
        if price < self.lower_band:
            if direction == "SELL":
                return True, "below_lower_band", metrics
            else:
                return False, "buy_vetoed_below_lower_band", metrics

        return False, "unknown_band_state", metrics
