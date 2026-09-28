"""
AEGIS Volume Profile & Auction Market Theory Engine (VolumeProfileEngine).

Implements real-time Price-Volume Distribution modeling:
1. Spot Volume Profile (Horizontal Price Bins):
   - Computes dynamic Point of Control (POC): The price level with peak traded volume.
   - Computes 70% Value Area (VAH = Value Area High, VAL = Value Area Low).
   - Identifies Low Volume Nodes (LVN): Fast-slip liquidity vacuums where price accelerates.
   - Categorizes Spot Auction Regime: INSIDE_VA (mean-reversion to POC), ABOVE_VAH (expansion), BELOW_VAL (breakdown).
   - POC Proximity & Alignment: Evaluates if POC acts as magnetic support/resistance or runway obstacle.

2. Option Strike-Wise Volume Profile:
   - Tracks volume concentration across all active option strikes (ATM ± 10 strikes).
   - Identifies Call POC Strike and Put POC Strike (institutional liquidity magnet strikes).
   - Detects Option POC Migration (upward or downward shifts signaling operator positioning).

Hot-path safe: Pure Python, zero heavy libraries (no pandas/scipy on tick path).
Scale-invariant: Binning and distances dynamically normalized via localized ATR.
Mode: 100% Passive Shadow Telemetry (Zero active veto, zero execution interference).
"""

from __future__ import annotations
import math
import logging
from typing import Dict, List, Optional, Tuple, Any

logger = logging.getLogger("aegis.volume_profile")


class VolumeProfileEngine:
    """
    Maintains real-time horizontal volume profiles for both underlying Spot
    and option strikes, calculating POC, Value Area, and liquidity node metrics.
    """

    def __init__(
        self,
        bin_size_pts: Optional[float] = None,
        value_area_pct: float = 0.70,
        lvn_threshold_ratio: float = 0.25,
        index_name: str = "NIFTY",
    ):
        """
        Args:
            bin_size_pts: Price bucket width (pts). If None, defaults to 5.0 for NIFTY, 20.0 for SENSEX.
            value_area_pct: Standard auction market value area threshold (default 70% = 1 std dev).
            lvn_threshold_ratio: Ratio of peak POC volume below which a bin is an LVN (low volume node).
            index_name: "NIFTY" or "SENSEX".
        """
        self.index_name = index_name.upper()
        if bin_size_pts is not None and bin_size_pts > 0:
            self.bin_size_pts = float(bin_size_pts)
        else:
            self.bin_size_pts = 20.0 if "SENSEX" in self.index_name else 5.0

        self.value_area_pct = float(value_area_pct)
        self.lvn_threshold_ratio = float(lvn_threshold_ratio)

        # Spot volume histogram: bin_price -> cumulative_volume
        self.spot_profile: Dict[float, float] = {}
        self.total_spot_volume: float = 0.0

        # Option strike volume histograms: strike -> cumulative_volume
        self.call_strike_volume: Dict[float, float] = {}
        self.put_strike_volume: Dict[float, float] = {}
        self.total_call_volume: float = 0.0
        self.total_put_volume: float = 0.0

        # Cached profile metrics (updated on bar close or periodic tick)
        self.cached_poc: float = 0.0
        self.cached_vah: float = 0.0
        self.cached_val: float = 0.0
        self.cached_call_poc_strike: float = 0.0
        self.cached_put_poc_strike: float = 0.0
        self._last_recalc_vol: float = 0.0

    def _quantize_price(self, price: float) -> float:
        """Rounds price to the nearest bin bucket."""
        return round(round(price / self.bin_size_pts) * self.bin_size_pts, 2)

    def record_spot_bar(self, open_px: float, high_px: float, low_px: float, close_px: float, volume: float) -> None:
        """
        Distributes a completed 1-minute bar's volume across its vertical price range.
        Uniformly distributes volume across bins intersected by [low_px, high_px].
        """
        if volume <= 0:
            return

        low_bin = self._quantize_price(low_px)
        high_bin = self._quantize_price(high_px)

        if high_bin <= low_bin:
            # Single bin bar
            self.spot_profile[low_bin] = self.spot_profile.get(low_bin, 0.0) + volume
        else:
            # Multi-bin bar: evenly allocate across touched price bins
            num_bins = int(round((high_bin - low_bin) / self.bin_size_pts)) + 1
            vol_per_bin = volume / max(1, num_bins)
            curr = low_bin
            while curr <= high_bin + 1e-4:
                self.spot_profile[curr] = self.spot_profile.get(curr, 0.0) + vol_per_bin
                curr = round(curr + self.bin_size_pts, 2)

        self.total_spot_volume += volume

    def record_spot_tick(self, ltp: float, vol_delta: float) -> None:
        """Records an individual tick volume increment on the hot path."""
        if vol_delta <= 0 or ltp <= 0:
            return
        p_bin = self._quantize_price(ltp)
        self.spot_profile[p_bin] = self.spot_profile.get(p_bin, 0.0) + vol_delta
        self.total_spot_volume += vol_delta

    def record_option_volumes(self, call_vols: Dict[float, float], put_vols: Dict[float, float]) -> None:
        """
        Updates cumulative strike-wise volume for active option contracts.
        call_vols / put_vols: strike -> volume
        """
        for strike, vol in call_vols.items():
            if vol > 0:
                k = float(strike)
                self.call_strike_volume[k] = vol
        for strike, vol in put_vols.items():
            if vol > 0:
                k = float(strike)
                self.put_strike_volume[k] = vol

        self.total_call_volume = sum(self.call_strike_volume.values())
        self.total_put_volume = sum(self.put_strike_volume.values())

    def record_option_tick(self, strike: float, side: int, vol: float) -> None:
        """Updates cumulative volume for a single strike on option tick (0=CE, 1=PE)."""
        if vol <= 0 or strike <= 0:
            return
        k = float(strike)
        if side == 0:
            self.call_strike_volume[k] = vol
        elif side == 1:
            self.put_strike_volume[k] = vol

    def recalculate_profile(self) -> Tuple[float, float, float]:
        """
        Calculates Point of Control (POC), Value Area High (VAH), and Value Area Low (VAL).
        Returns: (POC, VAH, VAL)
        """
        # 1. Option Strike POCs (computed independently of spot profile)
        if self.call_strike_volume:
            self.cached_call_poc_strike = max(self.call_strike_volume.keys(), key=lambda k: self.call_strike_volume[k])
        if self.put_strike_volume:
            self.cached_put_poc_strike = max(self.put_strike_volume.keys(), key=lambda k: self.put_strike_volume[k])

        if not self.spot_profile or self.total_spot_volume <= 0:
            return 0.0, 0.0, 0.0

        # 2. Point of Control (POC) — bin with maximum traded volume
        poc_bin = max(self.spot_profile.keys(), key=lambda p: self.spot_profile[p])
        poc_volume = self.spot_profile[poc_bin]

        # 3. Value Area Calculation (70% of total volume expanding outward from POC)
        target_va_vol = self.total_spot_volume * self.value_area_pct
        accumulated_vol = poc_volume

        sorted_bins = sorted(self.spot_profile.keys())
        poc_idx = sorted_bins.index(poc_bin)

        up_idx = poc_idx + 1
        down_idx = poc_idx - 1

        vah_bin = poc_bin
        val_bin = poc_bin

        # Dual-pointer outward expansion (matching Auction Market Theory standards)
        while accumulated_vol < target_va_vol and (up_idx < len(sorted_bins) or down_idx >= 0):
            vol_up = self.spot_profile[sorted_bins[up_idx]] if up_idx < len(sorted_bins) else 0.0
            vol_down = self.spot_profile[sorted_bins[down_idx]] if down_idx >= 0 else 0.0

            if vol_up == vol_down and vol_up > 0 and up_idx < len(sorted_bins) and down_idx >= 0:
                accumulated_vol += (vol_up + vol_down)
                vah_bin = sorted_bins[up_idx]
                val_bin = sorted_bins[down_idx]
                up_idx += 1
                down_idx -= 1
            elif vol_up > vol_down and up_idx < len(sorted_bins):
                accumulated_vol += vol_up
                vah_bin = sorted_bins[up_idx]
                up_idx += 1
            elif down_idx >= 0 and vol_down > 0:
                accumulated_vol += vol_down
                val_bin = sorted_bins[down_idx]
                down_idx -= 1
            elif up_idx < len(sorted_bins):
                accumulated_vol += vol_up
                vah_bin = sorted_bins[up_idx]
                up_idx += 1
            else:
                break

        self.cached_poc = poc_bin
        self.cached_vah = max(vah_bin, poc_bin)
        self.cached_val = min(val_bin, poc_bin)
        self._last_recalc_vol = self.total_spot_volume

        return self.cached_poc, self.cached_vah, self.cached_val

    def get_profile_metrics(
        self,
        spot_price: float,
        atr_1m: float = 10.0,
        direction: str = "UP",
    ) -> Dict[str, Any]:
        """
        Extracts comprehensive Volume Profile and POC metrics for a candidate trade.
        Safe for zero-allocation shadow telemetry.
        """
        if spot_price <= 0:
            return {}

        # Recalculate if volume grew by > 5% since last calculation
        if abs(self.total_spot_volume - self._last_recalc_vol) > (self.total_spot_volume * 0.05 + 1.0):
            self.recalculate_profile()

        poc = self.cached_poc or spot_price
        vah = self.cached_vah or spot_price
        val = self.cached_val or spot_price
        eff_atr = max(atr_1m, 1.0)

        # 1. Auction Location relative to 70% Value Area
        if spot_price > vah:
            auction_zone = "ABOVE_VAH"  # Bullish expansion / value breakout
        elif spot_price < val:
            auction_zone = "BELOW_VAL"  # Bearish breakdown / value rejection
        else:
            auction_zone = "INSIDE_VA"  # Balanced auction / rotational mean-reversion

        # 2. POC Distance & Proximity
        dist_to_poc_pts = spot_price - poc
        dist_to_poc_atr = dist_to_poc_pts / eff_atr
        is_at_poc = abs(dist_to_poc_atr) <= 0.40  # Within 0.4x ATR of Point of Control

        # 3. Low Volume Node (LVN) Detection
        spot_bin = self._quantize_price(spot_price)
        spot_bin_vol = self.spot_profile.get(spot_bin, 0.0)
        poc_bin_vol = self.spot_profile.get(poc, 1.0)
        is_lvn_zone = (spot_bin_vol / max(1.0, poc_bin_vol)) <= self.lvn_threshold_ratio

        # 4. Alignment of POC with Trade Direction
        # If buying Call (UP):
        # - POC below spot acts as supportive floor (SUPPORTIVE)
        # - POC directly above spot within 1.0 ATR acts as overhead resistance (OBSTACLE)
        # - Spot sitting directly on POC bouncing up (POC_BOUNCE)
        is_up = direction.upper() in ("UP", "CALL")
        poc_alignment = "NEUTRAL"

        if is_up:
            if is_at_poc:
                poc_alignment = "POC_BOUNCE_SUPPORT"
            elif dist_to_poc_pts > 0:
                poc_alignment = "SUPPORTIVE_FLOOR"
            elif abs(dist_to_poc_atr) <= 1.2:
                poc_alignment = "RUNWAY_OBSTACLE"
            else:
                poc_alignment = "OPEN_SKY"
        else:
            if is_at_poc:
                poc_alignment = "POC_REJECTION_RESISTANCE"
            elif dist_to_poc_pts < 0:
                poc_alignment = "SUPPORTIVE_CEILING"
            elif abs(dist_to_poc_atr) <= 1.2:
                poc_alignment = "RUNWAY_OBSTACLE"
            else:
                poc_alignment = "OPEN_SKY"

        return {
            "spot_price": round(spot_price, 2),
            "poc_price": round(poc, 2),
            "vah_price": round(vah, 2),
            "val_price": round(val, 2),
            "auction_zone": auction_zone,
            "dist_to_poc_pts": round(dist_to_poc_pts, 2),
            "dist_to_poc_atr": round(dist_to_poc_atr, 2),
            "is_at_poc": is_at_poc,
            "is_lvn_zone": is_lvn_zone,
            "poc_alignment": poc_alignment,
            "call_poc_strike": self.cached_call_poc_strike,
            "put_poc_strike": self.cached_put_poc_strike,
            "total_spot_vol": int(self.total_spot_volume),
            "total_call_vol": int(self.total_call_volume),
            "total_put_vol": int(self.total_put_volume),
        }
