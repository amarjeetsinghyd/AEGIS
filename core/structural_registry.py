"""
AEGIS Structural Registry — Dynamic Support and Resistance Level Registry, Prominence and Cluster Architecture.

Maintains intraday and multi-day structural Support and Resistance levels, algorithmic prominence,
multi-touch boosts (+0.35), cluster merging, level role classification (OUTERMOST_ROOF, INTERIOR_CLUSTER, etc.),
and unbroken level carryover across market sessions.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core.price_action_engine import OHLCBar

_IST = timezone(timedelta(hours=5, minutes=30))

try:
    from logger import get_logger
    logger = get_logger("aegis_structural_registry")
except Exception:
    logger = logging.getLogger("aegis_structural_registry")

@dataclass
class StructuralLevel:
    """Represents an intraday Support or Resistance level identified by topological prominence or multi-touch."""
    level_type: str        # "SUPPORT" or "RESISTANCE"
    price: float           # Price level
    ts_created: float      # Timestamp when established
    prominence: float      # Prominence score (|depth| / ATR + volume boost)
    bar_idx: int           # Index in bars_1m where formed
    seq_idx: int = 0       # Monotonic sequential bar counter
    touch_count: int = 1   # Number of times price tested this level
    is_broken: bool = False
    ts_broken: float = 0.0
    timeframe: str = "5m"  # "DAILY", "15m", "5m", or "1m"
    is_previous_day: bool = False  # True if seeded from previous trading session
    level_role: str = "NORMAL"     # "OUTERMOST_ROOF", "OUTERMOST_FLOOR", "MAJOR_SWING", "INTERIOR_CLUSTER", "PDH", "PDL"
    is_tradeable_breakout: bool = True  # False if interior cluster or insufficient runway ahead
    breakout_tier: str = "A_PLUS"  # "A_PLUS", "A_STANDARD", "B_SWEEP_ONLY", "C_UNQUALIFIED"
    # ─── Volume & Order Flow Footprint at Anchor / Retests ───
    volume: float = 0.0            # Underlying / bar traded volume at anchor
    call_buy_vol: float = 0.0      # Aggressive Call Buying (lifted at ask)
    call_sell_vol: float = 0.0     # Call Writing / Liquidation (hit at bid)
    put_buy_vol: float = 0.0       # Aggressive Put Buying (lifted at ask)
    put_sell_vol: float = 0.0      # Put Writing / Liquidation (hit at bid)
    opt_delta_flow: float = 0.0    # Directional Flow: call_buy_vol - put_buy_vol
    opt_total_vol: float = 0.0     # Total Option volume across ATM window
    opt_pcr: float = 1.0           # Put / Call volume ratio
    vr_anchor: float = 1.0         # Volume ratio vs 60m median volume at creation
    net_delta_flow: float = 0.0    # Directional Flow alias
    pcr_at_level: float = 1.0      # PCR at level alias
    vol_ratio_at_level: float = 1.0 # VR at level alias




class StructuralRegistryMixin:
    """Mixin providing Dynamic S/R Level Registry discovery, seeding, clustering, and proximity tracking."""

    # ─── Scale-invariant proximity tolerances (basis-point normalized) ───
    # Level proximity / dedup: max(0.5 pt, ref_spot * 0.00008)  (~0.8 bps -> ~1.9 pts NIFTY 24k, ~6.4 pts SENSEX 80k)
    # Freak-bar filter:        max(2.0 pt, ref_spot * 0.0002)   (~2.0 bps -> ~4.8 pts NIFTY 24k, ~16.0 pts SENSEX 80k)

    def _ref_spot(self) -> float:
        """Spot-normalising reference: last_price > pdc > 24000.0 fallback."""
        return (
            float(self.last_price)
            if getattr(self, "last_price", 0.0) > 0
            else (float(self.pdc) if getattr(self, "pdc", 0.0) > 0 else 24000.0)
        )

    def _prox_tol(self, ref_spot: Optional[float] = None) -> float:
        """Level-proximity / dedup tolerance in points (scale-invariant bps)."""
        ref = float(ref_spot or self._ref_spot())
        return max(0.5, ref * 0.00008)

    def _freak_tol(self, ref_spot: Optional[float] = None) -> float:
        """Freak open-bar range filter in points (scale-invariant bps)."""
        ref = float(ref_spot or self._ref_spot())
        return max(2.0, ref * 0.0002)

    def _log_sr_level_birth(self, lvl: StructuralLevel, source: str) -> None:
        """
        [SR BIRTH] Permanent audit stamp for every S/R level creation / registration / restore.
        Answers 'yeh level kab aur kis din bana tha?' without log archaeology (2026-11-05 forensics mandate:
        level 78740.88 had no verifiable birth record during the 09:17:33 trade postmortem).
        """
        try:
            born_txt = "no-ts(0.0)"
            if lvl.ts_created > 0:
                born_txt = datetime.fromtimestamp(
                    lvl.ts_created, timezone(timedelta(hours=5, minutes=30))
                ).strftime("%d-%b %H:%M:%S")
            logger.info(
                f"[SR BIRTH] {source} | {lvl.level_type} @ {lvl.price:.2f} | born={born_txt} | tf={lvl.timeframe} | "
                f"prom={lvl.prominence:.2f} | seq={lvl.seq_idx} | bar={lvl.bar_idx} | touches={lvl.touch_count} | "
                f"vol={lvl.volume:.1f} | delta={lvl.opt_delta_flow:+.1f}"
            )
        except Exception:
            pass

    def _update_structural_levels(self, bar: OHLCBar, new_5m_bar: bool = False, **_kwargs: Any) -> None:
        """
        Dynamically discovers, registers, and tracks intraday Support & Resistance levels.
        Maintains an active session diary of structural peaks and troughs.
        """
        curr_day = int(bar.ts_start // 86400) if bar.ts_start > 0 else self._current_vwap_day
        if self._levels_day != curr_day:
            self.session_levels.clear()
            self._levels_day = curr_day
            # Re-seed daily macro anchors (PDH / PDL) for the new session
            if self.pdh > 0:
                _seed_pdh_lvl = StructuralLevel(
                    level_type="RESISTANCE",
                    price=round(self.pdh, 2),
                    ts_created=bar.ts_start,
                    prominence=2.5,
                    bar_idx=0,
                    seq_idx=0,
                    touch_count=2,
                    timeframe="DAILY",
                    is_previous_day=True,
                )
                self.session_levels.append(_seed_pdh_lvl)
                self._log_sr_level_birth(_seed_pdh_lvl, "SESSION_OPEN_DAILY_SEED")
            if self.pdl > 0:
                _seed_pdl_lvl = StructuralLevel(
                    level_type="SUPPORT",
                    price=round(self.pdl, 2),
                    ts_created=bar.ts_start,
                    prominence=2.5,
                    bar_idx=0,
                    seq_idx=0,
                    touch_count=2,
                    timeframe="DAILY",
                    is_previous_day=True,
                )
                self.session_levels.append(_seed_pdl_lvl)
                self._log_sr_level_birth(_seed_pdl_lvl, "SESSION_OPEN_DAILY_SEED")
            # Restore unbroken previous-day 5m/15m structural levels
            if hasattr(self, "_previous_day_levels"):
                for p_lvl in self._previous_day_levels:
                    if not p_lvl.is_broken:
                        if not any(l.level_type == p_lvl.level_type and abs(l.price - p_lvl.price) < self._prox_tol() for l in self.session_levels):
                            self.session_levels.append(p_lvl)
                            self._log_sr_level_birth(p_lvl, "SESSION_OPEN_PREV_DAY_RESTORE")

        atr = self.current_atr_1m if self.current_atr_1m > 0 else (bar.close * 0.001)

        # 1. Unconditional Break Invalidation for completed historical bars
        # Any level formed before cand_hist that cand_hist broke through is marked is_broken = True
        if len(self.bars_1m) >= 2:
            cand_hist = self.bars_1m[-2]
            broken_any = False
            for lvl in self.session_levels:
                if not lvl.is_broken and lvl.ts_created < cand_hist.ts_start:
                    if lvl.level_type == "SUPPORT" and cand_hist.close < (lvl.price - 0.20 * atr):
                        lvl.is_broken = True
                        lvl.ts_broken = cand_hist.ts_end
                        broken_any = True
                    elif lvl.level_type == "RESISTANCE" and cand_hist.close > (lvl.price + 0.20 * atr):
                        lvl.is_broken = True
                        lvl.ts_broken = cand_hist.ts_end
                        broken_any = True

        # 2. Dynamic Post-Excursion Prominence Mutation: PERMANENTLY DECOMMISSIONED (Task 4, Nov 2026).
        # Rationale: Prominence is a PURE birth/touch structural attribute. Inflating it merely because
        # spot trended away from the level made ancient levels look institutionally strong and let
        # Pillar 4 breakout candidates self-promote during strong trends. Prominence now only grows
        # through genuine multi-touch re-tests (+0.35 boost) and 5m/15m multi-timeframe confirmation.

        if len(self.bars_1m) < 3:
            return

        tol = max(0.25 * atr, bar.close * 0.0003)

        # ─── Multi-Timeframe Structural Pivot Discovery ───
        # Priority 1: 5-Minute Bars (Institutional Structural Swing Timeframe)
        # Fallback: 1-Minute Bars (prior to 3 completed 5m bars, e.g. early morning or synthetic tests)
        if len(self.bars_5m) >= 3:
            # Only discover/update 5m structural swings when a 5m bar has just closed
            if not new_5m_bar:
                # Immediate 1-minute S/R role re-classification + sweep_detector sync when a
                # level broke on this 1m bar (between 5m bar closes), so broken outermost
                # levels are retired and newly promoted outermost levels are tracked by
                # sweep_detector right away instead of waiting up to 4 minutes.
                if broken_any:
                    self._classify_level_roles()
                    if hasattr(self, "sweep_detector") and self.sweep_detector is not None:
                        pz = self.get_pain_zone_vwaps() if hasattr(self, "get_pain_zone_vwaps") else {}
                        cs_vwap = float(pz.get("vwap_call_sell", 0.0) or 0.0)
                        ps_vwap = float(pz.get("vwap_put_sell", 0.0) or 0.0)
                        self.sweep_detector.sync_levels(
                            self.session_levels,
                            pdh=getattr(self, "pdh", 0.0),
                            pdl=getattr(self, "pdl", 0.0),
                            cs_vwap=cs_vwap,
                            ps_vwap=ps_vwap,
                        )
                return
            cand = self.bars_5m[-2]
            prev = self.bars_5m[-3]
            curr = self.bars_5m[-1]
            tf_name = "5m"
            tf_prom_boost = 0.50  # 5m institutional swing boost
            window_left = list(self.bars_5m)[-7:-2] if len(self.bars_5m) >= 7 else list(self.bars_5m)[:-2]
            ref_lows = [b.low for b in window_left] + [curr.low]
            ref_highs = [b.high for b in window_left] + [curr.high]
            depth_high = cand.high - (min(ref_lows) if ref_lows else min(prev.low, curr.low))
            depth_low = (max(ref_highs) if ref_highs else max(prev.high, curr.high)) - cand.low
        else:
            cand = self.bars_1m[-2]
            prev = self.bars_1m[-3]
            curr = bar
            tf_name = "1m"
            tf_prom_boost = 0.0
            window_left = list(self.bars_1m)[-7:-2] if len(self.bars_1m) >= 7 else list(self.bars_1m)[:-2]
            ref_lows = [b.low for b in window_left] + [curr.low]
            ref_highs = [b.high for b in window_left] + [curr.high]
            depth_high = cand.high - (min(ref_lows) if ref_lows else min(prev.low, curr.low))
            depth_low = (max(ref_highs) if ref_highs else max(prev.high, curr.high)) - cand.low

        # ─── Anchor Volume & Order Flow Footprint ───
        cand_vol = float(getattr(cand, "volume", 0.0) or 0.0)
        cand_cb = float(getattr(cand, "call_buy_vol", 0.0) or 0.0)
        cand_cs = float(getattr(cand, "call_sell_vol", 0.0) or 0.0)
        cand_pb = float(getattr(cand, "put_buy_vol", 0.0) or 0.0)
        cand_ps = float(getattr(cand, "put_sell_vol", 0.0) or 0.0)
        cand_delta = float(getattr(cand, "opt_delta_flow", 0.0) or (cand_cb - cand_pb))
        cand_opt_vol = float(getattr(cand, "opt_total_vol", 0.0) or (cand_cb + cand_cs + cand_pb + cand_ps))
        total_calls = cand_cb + cand_cs
        total_puts = cand_pb + cand_ps
        cand_pcr = float(getattr(cand, "opt_pcr", 1.0) or 1.0)
        if cand_pcr <= 0 and total_calls > 0:
            cand_pcr = total_puts / total_calls

        # Pure PA + Options Flow: Normal spot volume boost purged (Zero-Entropy)
        vr = 1.0
        vol_ratio_boost = 0.0

        # Check if prev bar was a freak 09:15 opening settlement spike
        ref_atr = atr if atr > 0 else (cand.high * 0.001)
        is_prev_freak_open = (
            (prev.high - prev.low <= self._freak_tol())
            and (abs(prev.high - cand.high) > (1.5 * ref_atr) or abs(prev.low - cand.low) > (1.5 * ref_atr))
        )
        is_swing_high = (cand.high >= prev.high and cand.high > curr.high) or (is_prev_freak_open and cand.high > curr.high)
        is_swing_low = (cand.low <= prev.low and cand.low < curr.low) or (is_prev_freak_open and cand.low < curr.low)

        # 3. Swing High / Resistance Peak Detection
        if is_swing_high:
            depth = depth_high
            if depth >= 0.15 * atr:
                res_ofi_boost = 0.20 if (cand_opt_vol > 0 and (cand_cs > cand_cb or cand_delta < 0)) else 0.0
                total_prom = round((depth / max(atr, 1e-4)) + vol_ratio_boost + res_ofi_boost + tf_prom_boost, 2)
                matched = False
                for lvl in self.session_levels:
                    if lvl.level_type == "RESISTANCE" and not lvl.is_broken and abs(lvl.price - cand.high) <= tol:
                        # Only treat as a re-test touch if cand formed AFTER the anchor bar
                        if cand.ts_start > (lvl.ts_created + 60.0):
                            lvl.touch_count += 1
                            lvl.volume += cand_vol
                            lvl.call_buy_vol += cand_cb
                            lvl.call_sell_vol += cand_cs
                            lvl.put_buy_vol += cand_pb
                            lvl.put_sell_vol += cand_ps
                            lvl.opt_total_vol += cand_opt_vol
                            lvl.opt_delta_flow = round(lvl.call_buy_vol - lvl.put_buy_vol, 1)
                            lvl.net_delta_flow = lvl.opt_delta_flow
                            tot_c = lvl.call_buy_vol + lvl.call_sell_vol
                            tot_p = lvl.put_buy_vol + lvl.put_sell_vol
                            lvl.opt_pcr = round(tot_p / max(tot_c, 1.0), 2)
                            lvl.pcr_at_level = lvl.opt_pcr
                            lvl.vol_ratio_at_level = 1.0
                            retest_boost = 0.35 + (0.20 if tf_name in ("5m", "15m") else 0.0)
                            lvl.prominence = round(lvl.prominence + retest_boost, 2)
                        else:
                            if tf_name in ("5m", "15m"):
                                lvl.timeframe = tf_name
                                lvl.prominence = max(lvl.prominence, total_prom)
                        lvl.timeframe = tf_name
                        lvl.price = max(lvl.price, cand.high)
                        matched = True
                        break
                if not matched:
                    _swing_res_lvl = StructuralLevel(
                        level_type="RESISTANCE",
                        price=round(cand.high, 2),
                        ts_created=cand.ts_start,
                        prominence=total_prom,
                        bar_idx=len(self.bars_1m) - 2,
                        seq_idx=max(0, self._total_bars_seen - 2),
                        timeframe=tf_name,
                        volume=cand_vol,
                        call_buy_vol=cand_cb,
                        call_sell_vol=cand_cs,
                        put_buy_vol=cand_pb,
                        put_sell_vol=cand_ps,
                        opt_delta_flow=cand_delta,
                        opt_total_vol=cand_opt_vol,
                        opt_pcr=round(cand_pcr, 2),
                        vr_anchor=round(vr, 2),
                        net_delta_flow=cand_delta,
                        pcr_at_level=round(cand_pcr, 2),
                        vol_ratio_at_level=round(vr, 2),
                    )
                    self.session_levels.append(_swing_res_lvl)
                    self._log_sr_level_birth(_swing_res_lvl, f"{tf_name}_SWING_HIGH")

        # 4. Swing Low / Support Trough Detection
        if is_swing_low:
            depth = depth_low
            if depth >= 0.15 * atr:
                sup_ofi_boost = 0.20 if (cand_opt_vol > 0 and (cand_ps > cand_pb or cand_delta > 0)) else 0.0
                total_prom = round((depth / max(atr, 1e-4)) + vol_ratio_boost + sup_ofi_boost + tf_prom_boost, 2)
                matched = False
                for lvl in self.session_levels:
                    if lvl.level_type == "SUPPORT" and not lvl.is_broken and abs(lvl.price - cand.low) <= tol:
                        # Only treat as a re-test touch if cand formed AFTER the anchor bar
                        if cand.ts_start > (lvl.ts_created + 60.0):
                            lvl.touch_count += 1
                            lvl.volume += cand_vol
                            lvl.call_buy_vol += cand_cb
                            lvl.call_sell_vol += cand_cs
                            lvl.put_buy_vol += cand_pb
                            lvl.put_sell_vol += cand_ps
                            lvl.opt_total_vol += cand_opt_vol
                            lvl.opt_delta_flow = round(lvl.call_buy_vol - lvl.put_buy_vol, 1)
                            lvl.net_delta_flow = lvl.opt_delta_flow
                            tot_c = lvl.call_buy_vol + lvl.call_sell_vol
                            tot_p = lvl.put_buy_vol + lvl.put_sell_vol
                            lvl.opt_pcr = round(tot_p / max(tot_c, 1.0), 2)
                            lvl.pcr_at_level = lvl.opt_pcr
                            lvl.vol_ratio_at_level = 1.0
                            retest_boost = 0.35 + (0.20 if tf_name in ("5m", "15m") else 0.0)
                            lvl.prominence = round(lvl.prominence + retest_boost, 2)
                        else:
                            if tf_name in ("5m", "15m"):
                                lvl.timeframe = tf_name
                                lvl.prominence = max(lvl.prominence, total_prom)
                        lvl.timeframe = tf_name
                        lvl.price = min(lvl.price, cand.low)
                        matched = True
                        break
                if not matched:
                    _swing_sup_lvl = StructuralLevel(
                        level_type="SUPPORT",
                        price=round(cand.low, 2),
                        ts_created=cand.ts_start,
                        prominence=total_prom,
                        bar_idx=len(self.bars_1m) - 2,
                        seq_idx=max(0, self._total_bars_seen - 2),
                        timeframe=tf_name,
                        volume=cand_vol,
                        call_buy_vol=cand_cb,
                        call_sell_vol=cand_cs,
                        put_buy_vol=cand_pb,
                        put_sell_vol=cand_ps,
                        opt_delta_flow=cand_delta,
                        opt_total_vol=cand_opt_vol,
                        opt_pcr=round(cand_pcr, 2),
                        vr_anchor=round(vr, 2),
                        net_delta_flow=cand_delta,
                        pcr_at_level=round(cand_pcr, 2),
                        vol_ratio_at_level=round(vr, 2),
                    )
                    self.session_levels.append(_swing_sup_lvl)
                    self._log_sr_level_birth(_swing_sup_lvl, f"{tf_name}_SWING_LOW")

        # Re-classify level roles and breakout eligibility
        self._classify_level_roles()

    def get_active_sr_registry_snapshot(self) -> List[Dict[str, Any]]:
        """Returns active and recently broken S/R levels for telemetry and dashboard rendering."""
        snapshot = []
        for lvl in self.session_levels:
            snapshot.append({
                "type": lvl.level_type,
                "price": lvl.price,
                "prominence": lvl.prominence,
                "touch_count": lvl.touch_count,
                "timeframe": getattr(lvl, "timeframe", "5m"),
                "is_previous_day": getattr(lvl, "is_previous_day", False),
                "level_role": getattr(lvl, "level_role", "NORMAL"),
                "is_tradeable_breakout": getattr(lvl, "is_tradeable_breakout", True),
                "breakout_tier": getattr(lvl, "breakout_tier", "A_PLUS"),
                "is_broken": lvl.is_broken,
                "ts_created": lvl.ts_created,
                "ts_broken": lvl.ts_broken,
                "seq_idx": lvl.seq_idx,
                "volume": round(lvl.volume, 1),
                "vr_anchor": round(lvl.vr_anchor, 2),
                "call_buy_vol": round(lvl.call_buy_vol, 1),
                "call_sell_vol": round(lvl.call_sell_vol, 1),
                "put_buy_vol": round(lvl.put_buy_vol, 1),
                "put_sell_vol": round(lvl.put_sell_vol, 1),
                "opt_delta_flow": round(lvl.opt_delta_flow, 1),
                "opt_pcr": round(lvl.opt_pcr, 2),
                "net_delta_flow": round(lvl.net_delta_flow, 1),
                "pcr_at_level": round(lvl.pcr_at_level, 2),
                "vol_ratio_at_level": round(lvl.vol_ratio_at_level, 2),
            })
        return snapshot

    # ═══════════════════════════════════════════════════════════
    # 2. ZERO-LAG IN-FLIGHT EMA & DYNAMIC INDICATOR GETTERS
    # ═══════════════════════════════════════════════════════════


    @staticmethod
    def _parse_candle_timestamp(c_dict: Dict[str, Any]) -> float:
        """Parses candle timestamp from Shoonya broker REST dict format."""
        ssboe = c_dict.get("ssboe")
        if ssboe is not None:
            try:
                return float(ssboe)
            except (ValueError, TypeError):
                pass
        raw_ts = c_dict.get("timestamp") or c_dict.get("ts") or c_dict.get("time")
        if hasattr(raw_ts, "timestamp"):
            try:
                return float(raw_ts.timestamp())
            except Exception:
                pass
        if isinstance(raw_ts, (int, float)):
            return float(raw_ts)
        if isinstance(raw_ts, str):
            try:
                return float(raw_ts)
            except ValueError:
                for fmt in ("%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y %H:%M:%S", "%Y-%m-%d"):
                    try:
                        return datetime.strptime(raw_ts, fmt).timestamp()
                    except Exception:
                        pass
        return 0.0


    def seed_daily_levels(self, pdh: float, pdl: float, pdc: float = 0.0) -> None:
        """Seeds Previous Day High (PDH), Previous Day Low (PDL), and Previous Day Close (PDC)."""
        if pdh > 0:
            self.pdh = float(pdh)
            # Register PDH as macro RESISTANCE anchor in S/R registry
            if not any(l.level_type == "RESISTANCE" and abs(l.price - self.pdh) < self._prox_tol() for l in self.session_levels):
                _seed_res_lvl = StructuralLevel(
                    level_type="RESISTANCE",
                    price=round(self.pdh, 2),
                    ts_created=0.0,
                    prominence=2.5,
                    bar_idx=0,
                    seq_idx=0,
                    touch_count=2,
                    timeframe="DAILY",
                    is_previous_day=True,
                )
                self.session_levels.append(_seed_res_lvl)
                self._log_sr_level_birth(_seed_res_lvl, "SEED_DAILY_PDH")
        if pdl > 0:
            self.pdl = float(pdl)
            # Register PDL as macro SUPPORT anchor in S/R registry
            if not any(l.level_type == "SUPPORT" and abs(l.price - self.pdl) < self._prox_tol() for l in self.session_levels):
                _seed_sup_lvl = StructuralLevel(
                    level_type="SUPPORT",
                    price=round(self.pdl, 2),
                    ts_created=0.0,
                    prominence=2.5,
                    bar_idx=0,
                    seq_idx=0,
                    touch_count=2,
                    timeframe="DAILY",
                    is_previous_day=True,
                )
                self.session_levels.append(_seed_sup_lvl)
                self._log_sr_level_birth(_seed_sup_lvl, "SEED_DAILY_PDL")
        if pdc > 0:
            self.pdc = float(pdc)
        self._classify_level_roles()
        logger.info(
            f"[PriceActionEngine] Seeded Daily Levels: PDH={self.pdh:.2f} | PDL={self.pdl:.2f} | PDC={self.pdc:.2f} "
            f"(Proximity Threshold: {self.proximity_threshold_bps:.1f} bps)"
        )

    @staticmethod
    def _cluster_and_merge_levels(
        levels: List[StructuralLevel],
        cluster_tol: float,
    ) -> List[StructuralLevel]:
        """
        Clusters nearby S/R levels within cluster_tol and merges them based on institutional dominance:
          1. Timeframe Confluence (15m > 5m)
          2. Multiple Touches (higher touch_count)
          3. Absolute Structural Extreme:
             - RESISTANCE: Highest peak price (max) is the true structural breakout ceiling.
             - SUPPORT: Lowest trough price (min) is the true structural breakdown floor.
          4. Weaker levels in the cluster are consolidated into the dominant level:
             touches and volume aggregated, prominence boosted by +0.35.
        """
        if not levels:
            return []

        res_levels = [l for l in levels if l.level_type == "RESISTANCE"]
        sup_levels = [l for l in levels if l.level_type == "SUPPORT"]

        def _merge_group(group: List[StructuralLevel], is_resistance: bool) -> StructuralLevel:
            if len(group) == 1:
                return group[0]

            def _dom_score(lvl: StructuralLevel) -> float:
                score = lvl.prominence + (lvl.touch_count * 2.0)
                if getattr(lvl, "timeframe", "") == "15m":
                    score += 10.0
                return score

            dominant = max(group, key=_dom_score)

            if is_resistance:
                merged_price = max(l.price for l in group)
            else:
                merged_price = min(l.price for l in group)

            total_touches = sum(l.touch_count for l in group)
            total_vol = sum(l.volume for l in group)
            max_prom = max(l.prominence for l in group)
            has_15m = any(getattr(l, "timeframe", "") == "15m" for l in group)

            dominant.price = round(merged_price, 2)
            dominant.touch_count = total_touches
            dominant.volume = total_vol
            dominant.prominence = round(max_prom + 0.35, 2)
            dominant.timeframe = "15m" if has_15m else "5m"
            dominant.is_previous_day = True
            return dominant

        def _cluster_levels_by_type(lvls: List[StructuralLevel], is_resistance: bool) -> List[StructuralLevel]:
            if not lvls:
                return []
            sorted_lvls = sorted(lvls, key=lambda l: l.price)
            clusters: List[List[StructuralLevel]] = []
            curr_cluster: List[StructuralLevel] = [sorted_lvls[0]]

            for lvl in sorted_lvls[1:]:
                if abs(lvl.price - curr_cluster[-1].price) <= cluster_tol or abs(lvl.price - curr_cluster[0].price) <= cluster_tol:
                    curr_cluster.append(lvl)
                else:
                    clusters.append(curr_cluster)
                    curr_cluster = [lvl]
            if curr_cluster:
                clusters.append(curr_cluster)

            return [_merge_group(c, is_resistance) for c in clusters]

        merged_res = _cluster_levels_by_type(res_levels, is_resistance=True)
        merged_sup = _cluster_levels_by_type(sup_levels, is_resistance=False)
        return merged_res + merged_sup

    def _classify_level_roles(self) -> None:
        """
        Dynamically tags each active unbroken level with its structural role and breakout eligibility:
          - OUTERMOST_ROOF: The highest active resistance boundary (Blue Sky ahead) -> A+ Breakout.
          - OUTERMOST_FLOOR: The lowest active support boundary -> A+ Breakdown.
          - MAJOR_SWING: Has at least min_runway (1.25x ATR) of clear space before the next level -> A Standard.
          - INTERIOR_CLUSTER: Has an overhead ceiling or underlying floor within min_runway -> VETOED FOR BREAKOUT AND SWEEPS (interior chop).
          - C_UNQUALIFIED: Prominence < 1.25 -> VETOED FOR BREAKOUT.
        """
        atr = self.current_atr_1m if self.current_atr_1m > 0 else (self.pdc * 0.0008 if self.pdc > 0 else 20.0)
        ref_spot = self.last_price if self.last_price > 0 else (self.pdc if self.pdc > 0 else 24000.0)
        min_runway = max(1.25 * atr, 0.0008 * ref_spot)

        active_res = [l for l in self.session_levels if l.level_type == "RESISTANCE" and not l.is_broken]
        active_sup = [l for l in self.session_levels if l.level_type == "SUPPORT" and not l.is_broken]

        # 1. Classify Resistances
        if active_res:
            sorted_res = sorted(active_res, key=lambda l: l.price)
            highest_res = sorted_res[-1]

            for lvl in sorted_res:
                if lvl.timeframe == "DAILY" or (self.pdh > 0 and abs(lvl.price - self.pdh) < self._prox_tol()):
                    lvl.level_role = "PDH"
                    lvl.breakout_tier = "A_PLUS"
                    lvl.is_tradeable_breakout = True
                    continue

                if lvl.price == highest_res.price:
                    lvl.level_role = "OUTERMOST_ROOF"
                    lvl.breakout_tier = "A_PLUS"
                    lvl.is_tradeable_breakout = (lvl.prominence >= 1.25)
                    continue

                # Check clearance to the next higher resistance
                higher_lvls = [l for l in sorted_res if l.price > lvl.price]
                if higher_lvls:
                    dist_to_next = higher_lvls[0].price - lvl.price
                    if dist_to_next < min_runway:
                        lvl.level_role = "INTERIOR_CLUSTER"
                        lvl.is_tradeable_breakout = False
                        lvl.breakout_tier = "B_SWEEP_ONLY"
                    else:
                        lvl.level_role = "MAJOR_SWING"
                        lvl.is_tradeable_breakout = (lvl.prominence >= 1.25)
                        lvl.breakout_tier = "A_STANDARD" if lvl.prominence >= 1.25 else "C_UNQUALIFIED"

        # 2. Classify Supports
        if active_sup:
            sorted_sup = sorted(active_sup, key=lambda l: l.price, reverse=True)
            lowest_sup = sorted_sup[-1]

            for lvl in sorted_sup:
                if lvl.timeframe == "DAILY" or (self.pdl > 0 and abs(lvl.price - self.pdl) < self._prox_tol()):
                    lvl.level_role = "PDL"
                    lvl.breakout_tier = "A_PLUS"
                    lvl.is_tradeable_breakout = True
                    continue

                if lvl.price == lowest_sup.price:
                    lvl.level_role = "OUTERMOST_FLOOR"
                    lvl.breakout_tier = "A_PLUS"
                    lvl.is_tradeable_breakout = (lvl.prominence >= 1.25)
                    continue

                # Check clearance to the next lower support
                lower_lvls = [l for l in sorted_sup if l.price < lvl.price]
                if lower_lvls:
                    dist_to_next = lvl.price - lower_lvls[0].price
                    if dist_to_next < min_runway:
                        lvl.level_role = "INTERIOR_CLUSTER"
                        lvl.is_tradeable_breakout = False
                        lvl.breakout_tier = "B_SWEEP_ONLY"
                    else:
                        lvl.level_role = "MAJOR_SWING"
                        lvl.is_tradeable_breakout = (lvl.prominence >= 1.25)
                        lvl.breakout_tier = "A_STANDARD" if lvl.prominence >= 1.25 else "C_UNQUALIFIED"

        # Sync active S/R levels + Option Writer VWAPs into SweepStopRunDetector if present
        if hasattr(self, "sweep_detector") and self.sweep_detector is not None:
            pz = self.get_pain_zone_vwaps() if hasattr(self, "get_pain_zone_vwaps") else {}
            cs_vwap = float(pz.get("vwap_call_sell", 0.0) or 0.0)
            ps_vwap = float(pz.get("vwap_put_sell", 0.0) or 0.0)
            self.sweep_detector.sync_levels(
                self.session_levels,
                pdh=getattr(self, "pdh", 0.0),
                pdl=getattr(self, "pdl", 0.0),
                cs_vwap=cs_vwap,
                ps_vwap=ps_vwap,
            )

    def seed_previous_day_5m_bars(self, candles: Any) -> Dict[str, Any]:
        """
        Seeds previous trading day's 5-minute and 15-minute structural swing levels
        and macro anchors (PDH, PDL, PDC) into the Dynamic S/R Registry.
        
        Applies:
          1. 5m / 15m multi-timeframe swing pivot detection.
          2. ATR-scaled prominence calculation.
          3. Level Clustering & Merging (15m dominance, multi-touch accumulation, extreme boundaries).
          4. Registration into self.session_levels and self._previous_day_levels.
        """
        if candles is None:
            return {"levels_discovered": 0, "pdh": 0.0, "pdl": 0.0, "pdc": 0.0, "levels": []}

        if hasattr(candles, "to_dict"):
            candles = candles.to_dict(orient="records")
        if not isinstance(candles, list) or not candles:
            return {"levels_discovered": 0, "pdh": 0.0, "pdl": 0.0, "pdc": 0.0, "levels": []}

        parsed_5m = []
        for c in candles:
            try:
                op = float(c.get("open") or c.get("into") or 0.0)
                hi = float(c.get("high") or c.get("inth") or 0.0)
                lo = float(c.get("low") or c.get("intl") or 0.0)
                cl = float(c.get("close") or c.get("intc") or 0.0)
                if op <= 0 or hi <= 0 or lo <= 0 or cl <= 0:
                    continue
                vol = float(c.get("volume") or c.get("intv") or c.get("v") or 0.0)
                ts = self._parse_candle_timestamp(c)
                parsed_5m.append({
                    "ts": ts,
                    "open": op,
                    "high": hi,
                    "low": lo,
                    "close": cl,
                    "volume": vol,
                })
            except Exception:
                continue

        if len(parsed_5m) < 3:
            logger.warning(f"[PriceActionEngine] Insufficient 5m candles ({len(parsed_5m)}) to seed previous-day structural levels.")
            return {"levels_discovered": 0, "pdh": 0.0, "pdl": 0.0, "pdc": 0.0, "levels": []}

        # Chronological sort
        parsed_5m.sort(key=lambda x: x["ts"])

        # Derive PDH, PDL, PDC
        pdh = round(max(b["high"] for b in parsed_5m), 2)
        pdl = round(min(b["low"] for b in parsed_5m), 2)
        pdc = round(parsed_5m[-1]["close"], 2)

        # Seed daily macro anchors
        self.seed_daily_levels(pdh, pdl, pdc)

        # Construct 15m aggregated candles
        parsed_15m = []
        for i in range(0, len(parsed_5m), 3):
            chunk = parsed_5m[i:i + 3]
            if not chunk:
                continue
            parsed_15m.append({
                "ts": chunk[0]["ts"],
                "open": chunk[0]["open"],
                "high": max(c["high"] for c in chunk),
                "low": min(c["low"] for c in chunk),
                "close": chunk[-1]["close"],
                "volume": sum(c["volume"] for c in chunk),
            })

        # Calculate 5m ATR and scaled 1m ATR
        tr_5m = []
        for i in range(1, len(parsed_5m)):
            prev_c = parsed_5m[i - 1]["close"]
            h = parsed_5m[i]["high"]
            l = parsed_5m[i]["low"]
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
            tr_5m.append(tr)
        atr_5m = (sum(tr_5m) / len(tr_5m)) if tr_5m else (pdc * 0.001)
        ref_atr = self.current_atr_1m if self.current_atr_1m > 0 else max(atr_5m / 2.23, pdc * 0.0005)
        tol_15m = max(0.25 * ref_atr, pdc * 0.0003)

        raw_levels: List[StructuralLevel] = []
        n_bars = len(parsed_5m)
        for i in range(1, n_bars - 1):
            cand = parsed_5m[i]
            prev_b = parsed_5m[i - 1]
            next_b = parsed_5m[i + 1]

            window_left = parsed_5m[max(0, i - 5):i]
            ref_lows = [b["low"] for b in window_left] + [next_b["low"]]
            ref_highs = [b["high"] for b in window_left] + [next_b["high"]]

            depth_high = cand["high"] - min(ref_lows)
            depth_low = max(ref_highs) - cand["low"]

            is_swing_high = (cand["high"] >= prev_b["high"] and cand["high"] > next_b["high"])
            is_swing_low = (cand["low"] <= prev_b["low"] and cand["low"] < next_b["low"])

            # 1. Swing High (Resistance)
            if is_swing_high and depth_high >= (0.15 * ref_atr):
                is_15m = False
                for j in range(1, len(parsed_15m) - 1):
                    c15 = parsed_15m[j]
                    p15 = parsed_15m[j - 1]
                    n15 = parsed_15m[j + 1]
                    if c15["high"] >= p15["high"] and c15["high"] > n15["high"]:
                        if abs(cand["high"] - c15["high"]) <= tol_15m:
                            is_15m = True
                            break

                tf_name = "15m" if is_15m else "5m"
                prom_boost = 0.50 if is_15m else 0.0
                prom = round((depth_high / max(ref_atr, 1e-4)) + prom_boost, 2)
                raw_levels.append(
                    StructuralLevel(
                        level_type="RESISTANCE",
                        price=round(cand["high"], 2),
                        ts_created=cand["ts"],
                        prominence=max(1.5, prom),
                        bar_idx=0,
                        seq_idx=0,
                        touch_count=1,
                        timeframe=tf_name,
                        volume=cand["volume"],
                        is_previous_day=True,
                    )
                )

            # 2. Swing Low (Support)
            if is_swing_low and depth_low >= (0.15 * ref_atr):
                is_15m = False
                for j in range(1, len(parsed_15m) - 1):
                    c15 = parsed_15m[j]
                    p15 = parsed_15m[j - 1]
                    n15 = parsed_15m[j + 1]
                    if c15["low"] <= p15["low"] and c15["low"] < n15["low"]:
                        if abs(cand["low"] - c15["low"]) <= tol_15m:
                            is_15m = True
                            break

                tf_name = "15m" if is_15m else "5m"
                prom_boost = 0.50 if is_15m else 0.0
                prom = round((depth_low / max(ref_atr, 1e-4)) + prom_boost, 2)
                raw_levels.append(
                    StructuralLevel(
                        level_type="SUPPORT",
                        price=round(cand["low"], 2),
                        ts_created=cand["ts"],
                        prominence=max(1.5, prom),
                        bar_idx=0,
                        seq_idx=0,
                        touch_count=1,
                        timeframe=tf_name,
                        volume=cand["volume"],
                        is_previous_day=True,
                    )
                )

        # Apply clustering and merging (scale-invariant outermost structural boundary)
        cluster_tol = max(0.65 * ref_atr, pdc * 0.00065)
        merged_levels = self._cluster_and_merge_levels(raw_levels, cluster_tol)

        # Filter out levels coinciding with PDH or PDL within 2.0 pts (and enrich PDH/PDL anchors if present)
        filtered_levels: List[StructuralLevel] = []
        for lvl in merged_levels:
            if lvl.level_type == "RESISTANCE" and abs(lvl.price - pdh) < self._prox_tol(pdc):
                for sl in self.session_levels:
                    if sl.level_type == "RESISTANCE" and abs(sl.price - pdh) < self._prox_tol(pdc):
                        sl.volume = max(sl.volume, lvl.volume)
                        sl.touch_count = max(sl.touch_count, lvl.touch_count)
                        sl.prominence = max(sl.prominence, lvl.prominence)
                continue
            if lvl.level_type == "SUPPORT" and abs(lvl.price - pdl) < self._prox_tol(pdc):
                for sl in self.session_levels:
                    if sl.level_type == "SUPPORT" and abs(sl.price - pdl) < self._prox_tol(pdc):
                        sl.volume = max(sl.volume, lvl.volume)
                        sl.touch_count = max(sl.touch_count, lvl.touch_count)
                        sl.prominence = max(sl.prominence, lvl.prominence)
                continue
            filtered_levels.append(lvl)

        for lvl in filtered_levels:
            if not any(l.level_type == lvl.level_type and abs(l.price - lvl.price) < self._prox_tol(pdc) for l in self.session_levels):
                self.session_levels.append(lvl)
                self._log_sr_level_birth(lvl, "SEED_PREV_DAY_5M15M")

        # Store all previous day levels (macro PDH/PDL anchors + intermediate 5m/15m swings)
        self._previous_day_levels = [l for l in self.session_levels if getattr(l, "is_previous_day", False)]

        # Classify structural roles and breakout eligibility
        self._classify_level_roles()

        logger.info(
            f"[PriceActionEngine] Seeded Previous Day 5m/15m: {len(self._previous_day_levels)} total previous-day levels registered "
            f"({len(filtered_levels)} intermediate swings | {len([l for l in self._previous_day_levels if l.level_type == 'RESISTANCE'])} Res, "
            f"{len([l for l in self._previous_day_levels if l.level_type == 'SUPPORT'])} Sup) | "
            f"PDH={pdh:.2f} | PDL={pdl:.2f} | PDC={pdc:.2f} (Cluster Tol: {cluster_tol:.2f})"
        )

        level_dicts = [
            {
                "type": l.level_type,
                "price": l.price,
                "prominence": l.prominence,
                "touch_count": l.touch_count,
                "timeframe": l.timeframe,
                "level_role": getattr(l, "level_role", "NORMAL"),
                "is_tradeable_breakout": getattr(l, "is_tradeable_breakout", True),
                "breakout_tier": getattr(l, "breakout_tier", "A_PLUS"),
            }
            for l in self._previous_day_levels
        ]

        return {
            "levels_discovered": len(self._previous_day_levels),
            "intermediate_swings": len(filtered_levels),
            "pdh": pdh,
            "pdl": pdl,
            "pdc": pdc,
            "levels": level_dicts,
        }

    def check_key_level_proximity(self, direction: str, spot_price: float) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Key Level Proximity Veto:
        Prevents buying CALL directly into overhead PDH resistance or
        buying PUT directly into underlying PDL support.
        Threshold is scale-invariant (e.g. 8 bps: ~20 pts on NIFTY, ~60 pts on SENSEX).
        """
        if spot_price <= 0:
            return True, "spot_unavailable", {}

        threshold_pts = (self.proximity_threshold_bps / 10000.0) * spot_price

        if direction.upper() in ("UP", "CALL"):
            # Check if spot is beneath PDH resistance within proximity threshold
            if self.pdh > 0 and spot_price < self.pdh:
                dist = self.pdh - spot_price
                if dist <= threshold_pts:
                    return False, "near_overhead_pdh_resistance", {
                        "pdh": self.pdh,
                        "spot": spot_price,
                        "dist_pts": round(dist, 2),
                        "threshold_pts": round(threshold_pts, 2),
                    }

        elif direction.upper() in ("DOWN", "PUT"):
            # Check if spot is above PDL support within proximity threshold
            if self.pdl > 0 and spot_price > self.pdl:
                dist = spot_price - self.pdl
                if dist <= threshold_pts:
                    return False, "near_underlying_pdl_support", {
                        "pdl": self.pdl,
                        "spot": spot_price,
                        "dist_pts": round(dist, 2),
                        "threshold_pts": round(threshold_pts, 2),
                    }

        return True, "clear_of_key_levels", {}

    # ═══════════════════════════════════════════════════════════
    # 3. THE 3 EMPIRICALLY VERIFIED PRICE ACTION SCALPER PILLARS
    # ═══════════════════════════════════════════════════════════


    def _find_swept_sr_level(
        self,
        direction: str,
        bar: Any,
        min_sweep_depth: float,
        atr: float,
    ) -> Optional[StructuralLevel]:
        """
        Maps a candidate sweep candle to an active Structural Support (CALL) or Resistance (PUT)
        from self.session_levels or Day Extremes (PDH/PDL).
        
        Strict Institutional Qualification:
          - CALL (Spring): Must be OUTERMOST_FLOOR, PDL, or high-prominence MAJOR_SWING (>= 1.50 prom).
          - PUT (Upthrust): Must be OUTERMOST_ROOF, PDH, or high-prominence MAJOR_SWING (>= 1.50 prom).
          - INTERIOR_CLUSTER (B_SWEEP_ONLY) and C_UNQUALIFIED are STRICTLY VETOED (Task 5, Nov 2026):
            an interior cluster sits INSIDE the day range, so a "sweep" of it is ordinary chop, not a
            liquidity raid on a real structural boundary.
        """
        is_up = direction.upper() in ("UP", "CALL")
        target_type = "SUPPORT" if is_up else "RESISTANCE"

        candidates = []
        for lvl in self.session_levels:
            if lvl.level_type != target_type or lvl.is_broken:
                continue
            role = getattr(lvl, "level_role", "NORMAL")
            if role in ("C_UNQUALIFIED", "INTERIOR_CLUSTER") or getattr(lvl, "breakout_tier", "") == "C_UNQUALIFIED":
                continue
            if role == "MAJOR_SWING" and lvl.prominence < 1.50:
                continue
            if (self._total_bars_seen - lvl.seq_idx) < 2 and (getattr(bar, "ts_start", 0) - lvl.ts_created) < 120.0:
                continue
            candidates.append(lvl)

        # Include Previous Day Extremes (PDL / PDH) if not already in session_levels
        if is_up and self.pdl > 0 and not any(abs(l.price - self.pdl) < self._prox_tol() for l in candidates):
            candidates.append(
                StructuralLevel(level_type="SUPPORT", price=self.pdl, ts_created=0.0, prominence=2.0, bar_idx=0, touch_count=2, level_role="PDL")
            )
        elif (not is_up) and self.pdh > 0 and not any(abs(l.price - self.pdh) < self._prox_tol() for l in candidates):
            candidates.append(
                StructuralLevel(level_type="RESISTANCE", price=self.pdh, ts_created=0.0, prominence=2.0, bar_idx=0, touch_count=2, level_role="PDH")
            )

        # Sort by institutional strength: touch_count and prominence
        candidates.sort(key=lambda l: (l.touch_count * 0.5 + l.prominence), reverse=True)

        for lvl in candidates:
            if is_up:
                # Candle must open near/above support, plunge below by min_sweep_depth, and close back above
                if (
                    bar.open >= (lvl.price - 0.35 * atr)
                    and bar.low <= (lvl.price - min_sweep_depth)
                    and bar.close > lvl.price
                ):
                    return lvl
            else:
                # Candle must open near/below resistance, surge above by min_sweep_depth, and close back below
                if (
                    bar.open <= (lvl.price + 0.35 * atr)
                    and bar.high >= (lvl.price + min_sweep_depth)
                    and bar.close < lvl.price
                ):
                    return lvl

        return None

    def _find_inflight_swept_sr_level(
        self,
        direction: str,
        prior_bar: Any,
        curr_bar: Any,
        spot: float,
        min_sweep_depth: float,
        atr: float,
    ) -> Optional[StructuralLevel]:
        """
        Maps an in-flight forming candle to an active registered Structural Level for Mode B Reversal Breakout.
        Allows registered OUTERMOST_FLOOR, OUTERMOST_ROOF, PDL, PDH, and high-prominence MAJOR_SWING (>= 1.50).
        INTERIOR_CLUSTER (B_SWEEP_ONLY) is STRICTLY VETOED (Task 5, Nov 2026) — interior chop raids are not
        institutional liquidity sweeps, and they generated midday fake Turtle Soup entries.
        """
        is_up = direction.upper() in ("UP", "CALL")
        target_type = "SUPPORT" if is_up else "RESISTANCE"

        candidates = []
        for lvl in self.session_levels:
            if lvl.level_type != target_type or lvl.is_broken:
                continue
            role = getattr(lvl, "level_role", "NORMAL")
            if role in ("C_UNQUALIFIED", "INTERIOR_CLUSTER") or getattr(lvl, "breakout_tier", "") == "C_UNQUALIFIED":
                continue
            if role == "MAJOR_SWING" and lvl.prominence < 1.50:
                continue
            if (self._total_bars_seen - lvl.seq_idx) < 2 and (getattr(curr_bar, "ts_start", 0) - lvl.ts_created) < 120.0:
                continue
            candidates.append(lvl)

        # Include Previous Day Extremes (PDL / PDH)
        if is_up and self.pdl > 0 and not any(abs(l.price - self.pdl) < self._prox_tol() for l in candidates):
            candidates.append(
                StructuralLevel(level_type="SUPPORT", price=self.pdl, ts_created=0.0, prominence=2.0, bar_idx=0, touch_count=2, level_role="PDL")
            )
        elif (not is_up) and self.pdh > 0 and not any(abs(l.price - self.pdh) < self._prox_tol() for l in candidates):
            candidates.append(
                StructuralLevel(level_type="RESISTANCE", price=self.pdh, ts_created=0.0, prominence=2.0, bar_idx=0, touch_count=2, level_role="PDH")
            )

        candidates.sort(key=lambda l: (l.touch_count * 0.5 + l.prominence), reverse=True)

        for lvl in candidates:
            if is_up:
                # Plunge below registered support on prior bar or forming bar, live spot reclaimed above support
                plunge_ok = (prior_bar.low <= lvl.price - min_sweep_depth) or (curr_bar.low <= lvl.price - min_sweep_depth)
                reclaim_ok = (spot > lvl.price) and (spot >= prior_bar.high)
                if plunge_ok and reclaim_ok:
                    return lvl
            else:
                # Surge above registered resistance on prior bar or forming bar, live spot reclaimed below resistance
                surge_ok = (prior_bar.high >= lvl.price + min_sweep_depth) or (curr_bar.high >= lvl.price + min_sweep_depth)
                reclaim_ok = (spot < lvl.price) and (spot <= prior_bar.low)
                if surge_ok and reclaim_ok:
                    return lvl

        return None

