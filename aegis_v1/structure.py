"""AEGIS structure — Dynamic S/R registry (levels, clustering, sweeps, key-level proximity) and Classical Dow
macro shields (HL2/LH2 validator, day-extreme runway, structural obstacle).

Consolidated from structural_registry.py + dow_macro_shields.py. The former mixins are plain module-level
functions whose first parameter `eng` is the explicit engine-state argument (no inheritance, no MRO).
Function bodies are byte-identical to the originals except `self` -> `eng` and mixin-internal calls
self.f(x) -> f(eng, x).
"""

from __future__ import annotations
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any


_IST = timezone(timedelta(hours=5, minutes=30))

try:
    from common.logger import get_logger
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

# ─── Structural registry functions (formerly StructuralRegistryMixin) ───


def _update_structural_levels(eng, bar: OHLCBar, new_5m_bar: bool = False, new_15m_bar: bool = False) -> None:
    """
        Dynamically discovers, registers, and tracks intraday Support & Resistance levels.
        Maintains an active session diary of structural peaks and troughs.
        """
    curr_day = int(bar.ts_start // 86400) if bar.ts_start > 0 else eng._current_vwap_day
    if eng._levels_day != curr_day:
        eng.session_levels.clear()
        eng._levels_day = curr_day
        # Re-seed daily macro anchors (PDH / PDL) for the new session
        if eng.pdh > 0:
            eng.session_levels.append(
                StructuralLevel(
                    level_type="RESISTANCE",
                    price=round(eng.pdh, 2),
                    ts_created=bar.ts_start,
                    prominence=2.5,
                    bar_idx=0,
                    seq_idx=0,
                    touch_count=2,
                    timeframe="DAILY",
                    is_previous_day=True,
                )
            )
        if eng.pdl > 0:
            eng.session_levels.append(
                StructuralLevel(
                    level_type="SUPPORT",
                    price=round(eng.pdl, 2),
                    ts_created=bar.ts_start,
                    prominence=2.5,
                    bar_idx=0,
                    seq_idx=0,
                    touch_count=2,
                    timeframe="DAILY",
                    is_previous_day=True,
                )
            )
        # Restore unbroken previous-day 5m/15m structural levels
        if hasattr(eng, "_previous_day_levels"):
            for p_lvl in eng._previous_day_levels:
                if not p_lvl.is_broken:
                    if not any(l.level_type == p_lvl.level_type and abs(l.price - p_lvl.price) < 2.0 for l in eng.session_levels):
                        eng.session_levels.append(p_lvl)

    atr = eng.current_atr_1m if eng.current_atr_1m > 0 else (bar.close * 0.001)

    # 1. Unconditional Break Invalidation for completed historical bars
    # Any level formed before cand_hist that cand_hist broke through is marked is_broken = True
    if len(eng.bars_1m) >= 2:
        cand_hist = eng.bars_1m[-2]
        for lvl in eng.session_levels:
            if not lvl.is_broken and lvl.ts_created < cand_hist.ts_start:
                if lvl.level_type == "SUPPORT" and cand_hist.close < (lvl.price - 0.20 * atr):
                    lvl.is_broken = True
                    lvl.ts_broken = cand_hist.ts_end
                elif lvl.level_type == "RESISTANCE" and cand_hist.close > (lvl.price + 0.20 * atr):
                    lvl.is_broken = True
                    lvl.ts_broken = cand_hist.ts_end

    # 2. Dynamic Post-Excursion Prominence Update for active unbroken levels
    for lvl in eng.session_levels:
        if not lvl.is_broken:
            if lvl.level_type == "SUPPORT":
                excursion = (bar.high - lvl.price) / max(atr, 1e-4)
                if excursion > lvl.prominence:
                    lvl.prominence = round(excursion, 2)
            elif lvl.level_type == "RESISTANCE":
                excursion = (lvl.price - bar.low) / max(atr, 1e-4)
                if excursion > lvl.prominence:
                    lvl.prominence = round(excursion, 2)

    if len(eng.bars_1m) < 3:
        return

    tol = max(0.25 * atr, bar.close * 0.0003)

    # ─── Multi-Timeframe Structural Pivot Discovery ───
    # Priority 1: 5-Minute Bars (Institutional Structural Swing Timeframe)
    # Fallback: 1-Minute Bars (prior to 3 completed 5m bars, e.g. early morning or synthetic tests)
    if len(eng.bars_5m) >= 3:
        # Only discover/update 5m structural swings when a 5m bar has just closed
        if not new_5m_bar:
            return
        cand = eng.bars_5m[-2]
        prev = eng.bars_5m[-3]
        curr = eng.bars_5m[-1]
        tf_name = "5m"
        tf_prom_boost = 0.50  # 5m institutional swing boost
        window_left = list(eng.bars_5m)[-7:-2] if len(eng.bars_5m) >= 7 else list(eng.bars_5m)[:-2]
        ref_lows = [b.low for b in window_left] + [curr.low]
        ref_highs = [b.high for b in window_left] + [curr.high]
        depth_high = cand.high - (min(ref_lows) if ref_lows else min(prev.low, curr.low))
        depth_low = (max(ref_highs) if ref_highs else max(prev.high, curr.high)) - cand.low
        # Check 15m confluence
        if len(eng.bars_15m) >= 3:
            cand_15 = eng.bars_15m[-2]
            prev_15 = eng.bars_15m[-3]
            curr_15 = eng.bars_15m[-1]
            if abs(cand.high - cand_15.high) <= tol and (cand_15.high >= prev_15.high and cand_15.high > curr_15.high):
                tf_name = "15m"
                tf_prom_boost += 0.50
            elif abs(cand.low - cand_15.low) <= tol and (cand_15.low <= prev_15.low and cand_15.low < curr_15.low):
                tf_name = "15m"
                tf_prom_boost += 0.50
    else:
        cand = eng.bars_1m[-2]
        prev = eng.bars_1m[-3]
        curr = bar
        tf_name = "1m"
        tf_prom_boost = 0.0
        window_left = list(eng.bars_1m)[-7:-2] if len(eng.bars_1m) >= 7 else list(eng.bars_1m)[:-2]
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

    # 60m Median Volume for Scale-Invariant Volume Ratio (VR)
    vols_60 = [float(getattr(b, "volume", 0.0) or 0.0) for b in list(eng.bars_1m)[-60:]]
    if vols_60:
        s_v = sorted(vols_60)
        med_vol = float(s_v[len(s_v)//2])
    else:
        med_vol = 1.0
    vr = cand_vol / max(med_vol, 1.0) if med_vol > 0 else 1.0
    vol_ratio_boost = 0.30 * min(2.5, max(0.0, vr - 1.0))

    # Check if prev bar was a freak 09:15 opening settlement spike
    ref_atr = atr if atr > 0 else (cand.high * 0.001)
    is_prev_freak_open = (
        (prev.high - prev.low <= 5.0)
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
            for lvl in eng.session_levels:
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
                        lvl.vol_ratio_at_level = round(lvl.volume / max(med_vol, 1.0), 2)
                        retest_boost = 0.35 + (0.15 if vr >= 1.20 else 0.0) + (0.20 if tf_name in ("5m", "15m") else 0.0)
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
                eng.session_levels.append(
                    StructuralLevel(
                        level_type="RESISTANCE",
                        price=round(cand.high, 2),
                        ts_created=cand.ts_start,
                        prominence=total_prom,
                        bar_idx=len(eng.bars_1m) - 2,
                        seq_idx=max(0, eng._total_bars_seen - 2),
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
                )

    # 4. Swing Low / Support Trough Detection
    if is_swing_low:
        depth = depth_low
        if depth >= 0.15 * atr:
            sup_ofi_boost = 0.20 if (cand_opt_vol > 0 and (cand_ps > cand_pb or cand_delta > 0)) else 0.0
            total_prom = round((depth / max(atr, 1e-4)) + vol_ratio_boost + sup_ofi_boost + tf_prom_boost, 2)
            matched = False
            for lvl in eng.session_levels:
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
                        lvl.vol_ratio_at_level = round(lvl.volume / max(med_vol, 1.0), 2)
                        retest_boost = 0.35 + (0.15 if vr >= 1.20 else 0.0) + (0.20 if tf_name in ("5m", "15m") else 0.0)
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
                eng.session_levels.append(
                    StructuralLevel(
                        level_type="SUPPORT",
                        price=round(cand.low, 2),
                        ts_created=cand.ts_start,
                        prominence=total_prom,
                        bar_idx=len(eng.bars_1m) - 2,
                        seq_idx=max(0, eng._total_bars_seen - 2),
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
                )

    # Re-classify level roles and breakout eligibility
    _classify_level_roles(eng)

def get_active_sr_registry_snapshot(eng) -> List[Dict[str, Any]]:
    """Returns active and recently broken S/R levels for telemetry and dashboard rendering."""
    snapshot = []
    for lvl in eng.session_levels:
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

def seed_daily_levels(eng, pdh: float, pdl: float, pdc: float = 0.0) -> None:
    """Seeds Previous Day High (PDH), Previous Day Low (PDL), and Previous Day Close (PDC)."""
    if pdh > 0:
        eng.pdh = float(pdh)
        # Register PDH as macro RESISTANCE anchor in S/R registry
        if not any(l.level_type == "RESISTANCE" and abs(l.price - eng.pdh) < 2.0 for l in eng.session_levels):
            eng.session_levels.append(
                StructuralLevel(
                    level_type="RESISTANCE",
                    price=round(eng.pdh, 2),
                    ts_created=0.0,
                    prominence=2.5,
                    bar_idx=0,
                    seq_idx=0,
                    touch_count=2,
                    timeframe="DAILY",
                    is_previous_day=True,
                )
            )
    if pdl > 0:
        eng.pdl = float(pdl)
        # Register PDL as macro SUPPORT anchor in S/R registry
        if not any(l.level_type == "SUPPORT" and abs(l.price - eng.pdl) < 2.0 for l in eng.session_levels):
            eng.session_levels.append(
                StructuralLevel(
                    level_type="SUPPORT",
                    price=round(eng.pdl, 2),
                    ts_created=0.0,
                    prominence=2.5,
                    bar_idx=0,
                    seq_idx=0,
                    touch_count=2,
                    timeframe="DAILY",
                    is_previous_day=True,
                )
            )
    if pdc > 0:
        eng.pdc = float(pdc)
    _classify_level_roles(eng)
    logger.info(
        f"[PriceActionEngine] Seeded Daily Levels: PDH={eng.pdh:.2f} | PDL={eng.pdl:.2f} | PDC={eng.pdc:.2f} "
        f"(Proximity Threshold: {eng.proximity_threshold_bps:.1f} bps)"
    )

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

def _classify_level_roles(eng) -> None:
    """
        Dynamically tags each active unbroken level with its structural role and breakout eligibility:
          - OUTERMOST_ROOF: The highest active resistance boundary (Blue Sky ahead) -> A+ Breakout.
          - OUTERMOST_FLOOR: The lowest active support boundary -> A+ Breakdown.
          - MAJOR_SWING: Has at least min_runway (1.25x ATR) of clear space before the next level -> A Standard.
          - INTERIOR_CLUSTER: Has an overhead ceiling or underlying floor within min_runway -> VETOED FOR BREAKOUT (B_SWEEP_ONLY).
          - C_UNQUALIFIED: Prominence < 1.25 -> VETOED FOR BREAKOUT.
        """
    atr = eng.current_atr_1m if eng.current_atr_1m > 0 else (eng.pdc * 0.0008 if eng.pdc > 0 else 20.0)
    ref_spot = eng.last_price if eng.last_price > 0 else (eng.pdc if eng.pdc > 0 else 24000.0)
    min_runway = max(1.25 * atr, 0.0008 * ref_spot)

    active_res = [l for l in eng.session_levels if l.level_type == "RESISTANCE" and not l.is_broken]
    active_sup = [l for l in eng.session_levels if l.level_type == "SUPPORT" and not l.is_broken]

    # 1. Classify Resistances
    if active_res:
        sorted_res = sorted(active_res, key=lambda l: l.price)
        highest_res = sorted_res[-1]

        for lvl in sorted_res:
            if lvl.timeframe == "DAILY" or (eng.pdh > 0 and abs(lvl.price - eng.pdh) < 2.0):
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
            if lvl.timeframe == "DAILY" or (eng.pdl > 0 and abs(lvl.price - eng.pdl) < 2.0):
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

def seed_previous_day_5m_bars(eng, candles: Any) -> Dict[str, Any]:
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
            ts = _parse_candle_timestamp(c)
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
    seed_daily_levels(eng, pdh, pdl, pdc)

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
    ref_atr = eng.current_atr_1m if eng.current_atr_1m > 0 else max(atr_5m / 2.23, pdc * 0.0005)
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
    merged_levels = _cluster_and_merge_levels(raw_levels, cluster_tol)

    # Filter out levels coinciding with PDH or PDL within 2.0 pts (and enrich PDH/PDL anchors if present)
    filtered_levels: List[StructuralLevel] = []
    for lvl in merged_levels:
        if lvl.level_type == "RESISTANCE" and abs(lvl.price - pdh) < 2.0:
            for sl in eng.session_levels:
                if sl.level_type == "RESISTANCE" and abs(sl.price - pdh) < 2.0:
                    sl.volume = max(sl.volume, lvl.volume)
                    sl.touch_count = max(sl.touch_count, lvl.touch_count)
                    sl.prominence = max(sl.prominence, lvl.prominence)
            continue
        if lvl.level_type == "SUPPORT" and abs(lvl.price - pdl) < 2.0:
            for sl in eng.session_levels:
                if sl.level_type == "SUPPORT" and abs(sl.price - pdl) < 2.0:
                    sl.volume = max(sl.volume, lvl.volume)
                    sl.touch_count = max(sl.touch_count, lvl.touch_count)
                    sl.prominence = max(sl.prominence, lvl.prominence)
            continue
        filtered_levels.append(lvl)

    for lvl in filtered_levels:
        if not any(l.level_type == lvl.level_type and abs(l.price - lvl.price) < 2.0 for l in eng.session_levels):
            eng.session_levels.append(lvl)

    # Store all previous day levels (macro PDH/PDL anchors + intermediate 5m/15m swings)
    eng._previous_day_levels = [l for l in eng.session_levels if getattr(l, "is_previous_day", False)]

    # Classify structural roles and breakout eligibility
    _classify_level_roles(eng)

    logger.info(
        f"[PriceActionEngine] Seeded Previous Day 5m/15m: {len(eng._previous_day_levels)} total previous-day levels registered "
        f"({len(filtered_levels)} intermediate swings | {len([l for l in eng._previous_day_levels if l.level_type == 'RESISTANCE'])} Res, "
        f"{len([l for l in eng._previous_day_levels if l.level_type == 'SUPPORT'])} Sup) | "
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
        for l in eng._previous_day_levels
    ]

    return {
        "levels_discovered": len(eng._previous_day_levels),
        "intermediate_swings": len(filtered_levels),
        "pdh": pdh,
        "pdl": pdl,
        "pdc": pdc,
        "levels": level_dicts,
    }

def check_key_level_proximity(eng, direction: str, spot_price: float) -> Tuple[bool, str, Dict[str, Any]]:
    """
        Key Level Proximity Veto:
        Prevents buying CALL directly into overhead PDH resistance or
        buying PUT directly into underlying PDL support.
        Threshold is scale-invariant (e.g. 8 bps: ~20 pts on NIFTY, ~60 pts on SENSEX).
        """
    if spot_price <= 0:
        return True, "spot_unavailable", {}

    threshold_pts = (eng.proximity_threshold_bps / 10000.0) * spot_price

    if direction.upper() in ("UP", "CALL"):
        # Check if spot is beneath PDH resistance within proximity threshold
        if eng.pdh > 0 and spot_price < eng.pdh:
            dist = eng.pdh - spot_price
            if dist <= threshold_pts:
                return False, "near_overhead_pdh_resistance", {
                    "pdh": eng.pdh,
                    "spot": spot_price,
                    "dist_pts": round(dist, 2),
                    "threshold_pts": round(threshold_pts, 2),
                }

    elif direction.upper() in ("DOWN", "PUT"):
        # Check if spot is above PDL support within proximity threshold
        if eng.pdl > 0 and spot_price > eng.pdl:
            dist = spot_price - eng.pdl
            if dist <= threshold_pts:
                return False, "near_underlying_pdl_support", {
                    "pdl": eng.pdl,
                    "spot": spot_price,
                    "dist_pts": round(dist, 2),
                    "threshold_pts": round(threshold_pts, 2),
                }

    return True, "clear_of_key_levels", {}

# ═══════════════════════════════════════════════════════════
# 3. THE 3 EMPIRICALLY VERIFIED PRICE ACTION SCALPER PILLARS
# ═══════════════════════════════════════════════════════════


def _find_swept_sr_level(
    eng,
    direction: str,
    bar: Any,
    min_sweep_depth: float,
    atr: float,
) -> Optional[StructuralLevel]:
    """
        Maps a candidate sweep candle to an active Structural Support (CALL) or Resistance (PUT)
        from self.session_levels or Day Extremes (PDH/PDL).
        
        Strict Institutional Qualification:
          - CALL (Spring): Must be OUTERMOST_FLOOR, PDL, or high-prominence MAJOR_SWING (>= 1.25 prom).
          - PUT (Upthrust): Must be OUTERMOST_ROOF, PDH, or high-prominence MAJOR_SWING (>= 1.25 prom).
          - INTERIOR_CLUSTER and C_UNQUALIFIED are STRICTLY VETOED.
        """
    is_up = direction.upper() in ("UP", "CALL")
    target_type = "SUPPORT" if is_up else "RESISTANCE"

    candidates = []
    for lvl in eng.session_levels:
        if lvl.level_type != target_type or lvl.is_broken:
            continue
        role = getattr(lvl, "level_role", "NORMAL")
        if role in ("INTERIOR_CLUSTER", "C_UNQUALIFIED"):
            continue
        if role == "MAJOR_SWING" and (lvl.prominence < 1.25 or getattr(lvl, "breakout_tier", "") == "C_UNQUALIFIED"):
            continue
        if (eng._total_bars_seen - lvl.seq_idx) < 2 and (getattr(bar, "ts_start", 0) - lvl.ts_created) < 120.0:
            continue
        candidates.append(lvl)

    # Include Previous Day Extremes (PDL / PDH) if not already in session_levels
    if is_up and eng.pdl > 0 and not any(abs(l.price - eng.pdl) < 2.0 for l in candidates):
        candidates.append(
            StructuralLevel(level_type="SUPPORT", price=eng.pdl, ts_created=0.0, prominence=2.0, bar_idx=0, touch_count=2, level_role="PDL")
        )
    elif (not is_up) and eng.pdh > 0 and not any(abs(l.price - eng.pdh) < 2.0 for l in candidates):
        candidates.append(
            StructuralLevel(level_type="RESISTANCE", price=eng.pdh, ts_created=0.0, prominence=2.0, bar_idx=0, touch_count=2, level_role="PDH")
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
    eng,
    direction: str,
    prior_bar: Any,
    curr_bar: Any,
    spot: float,
    min_sweep_depth: float,
    atr: float,
) -> Optional[StructuralLevel]:
    """
        Maps an in-flight forming candle to an active registered Structural Level for Mode B Reversal Breakout.
        Strictly requires registered OUTERMOST_FLOOR, OUTERMOST_ROOF, PDL, PDH, or high-prominence MAJOR_SWING.
        Zero tolerance for arbitrary 5-bar local swings.
        """
    is_up = direction.upper() in ("UP", "CALL")
    target_type = "SUPPORT" if is_up else "RESISTANCE"

    candidates = []
    for lvl in eng.session_levels:
        if lvl.level_type != target_type or lvl.is_broken:
            continue
        role = getattr(lvl, "level_role", "NORMAL")
        if role in ("INTERIOR_CLUSTER", "C_UNQUALIFIED"):
            continue
        if role == "MAJOR_SWING" and (lvl.prominence < 1.25 or getattr(lvl, "breakout_tier", "") == "C_UNQUALIFIED"):
            continue
        if (eng._total_bars_seen - lvl.seq_idx) < 2 and (getattr(curr_bar, "ts_start", 0) - lvl.ts_created) < 120.0:
            continue
        candidates.append(lvl)

    # Include Previous Day Extremes (PDL / PDH)
    if is_up and eng.pdl > 0 and not any(abs(l.price - eng.pdl) < 2.0 for l in candidates):
        candidates.append(
            StructuralLevel(level_type="SUPPORT", price=eng.pdl, ts_created=0.0, prominence=2.0, bar_idx=0, touch_count=2, level_role="PDL")
        )
    elif (not is_up) and eng.pdh > 0 and not any(abs(l.price - eng.pdh) < 2.0 for l in candidates):
        candidates.append(
            StructuralLevel(level_type="RESISTANCE", price=eng.pdh, ts_created=0.0, prominence=2.0, bar_idx=0, touch_count=2, level_role="PDH")
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

@dataclass
class DowStructureChain:
    """Tracks the 4-point Classical Dow Theory sequence for Pillar 1."""
    is_valid: bool = False
    veto_reason: str = "uninitialized"
    # Indices in bars slice
    idx_1: int = -1       # HH1 (for CALL) or LL1 (for PUT)
    idx_touch1: int = -1  # HL1 (for CALL) or LH1 (for PUT) - 1st Touch (No Entry)
    idx_2: int = -1       # HH2 (for CALL) or LL2 (for PUT)
    idx_touch2: int = -1  # HL2 (for CALL) or LH2 (for PUT) - 2nd Touch (Entry)
    # Price levels
    impulse_1: float = 0.0   # HH1 or LL1 price
    touch_1: float = 0.0     # HL1 or LH1 price
    impulse_2: float = 0.0   # HH2 or LL2 price
    touch_2: float = 0.0     # HL2 or LH2 price
    pullback_bars: int = 0   # Exact candle count from HH2->HL2 or LL2->LH2
    orderly_ratio: float = 0.0
    imp_avg_vol: float = 0.0
    pb_avg_vol: float = 0.0
    entry_vol: float = 0.0
    swing_vdr: float = 1.0
    imp_directional_flow: float = 0.0
    pb_counter_flow: float = 0.0

# ─── Dow macro shield functions (formerly DowMacroShieldsMixin) ───


def _tail_list(dq: Any, n: int) -> List[Any]:
    """O(n) fast tail slice directly from deque without full list conversion."""
    L = len(dq) if dq is not None else 0
    if L == 0:
        return []
    if n >= L:
        return list(dq)
    return [dq[i] for i in range(-n, 0)]

def get_20_ema_band_arrays(eng, n: int) -> Tuple[List[float], List[float], List[float]]:
    """
        Returns aligned lists of (ema_close, ema_high, ema_low) for the last n closed bars.
        Falls back to derived band from ATR if high/low history is warming up.
        """
    bars = _tail_list(eng.bars_1m, n)
    e_close = _tail_list(eng.ema_20_history, n)
    if len(e_close) < len(bars):
        pad_len = len(bars) - len(e_close)
        e_close = [e_close[0] if e_close else eng.ema_20_1m] * pad_len + e_close

    has_hl_band = (
        hasattr(eng, "ema_20_high_history")
        and len(eng.ema_20_high_history) >= len(bars)
        and len(eng.ema_20_low_history) >= len(bars)
    )
    if has_hl_band:
        e_high = _tail_list(eng.ema_20_high_history, n)
        e_low = _tail_list(eng.ema_20_low_history, n)
    else:
        half_band = max(2.5, 0.35 * (eng.current_atr_1m if eng.current_atr_1m > 0 else 10.0))
        e_high = [ec + half_band for ec in e_close]
        e_low = [ec - half_band for ec in e_close]

    return e_close, e_high, e_low

def _validate_dow_hl2_lh2_structure(
    eng,
    direction: str,
    touch2_offset: int = 0,
    tolerance_bps: float = 1.5,
    lookback: int = 45,
    virtual_bar: Optional[OHLCBar] = None,
) -> DowStructureChain:
    """
        Strict Classical Dow Theory Validator for Pillar 1:
          - CALL requires: HH1 -> HL1 (1st touch, skipped) -> HH2 (HH2 > HH1) -> 4..12 bar orderly pullback -> HL2 (HL2 > HL1).
          - PUT requires:  LL1 -> LH1 (1st touch, skipped) -> LL2 (LL2 < LL1) -> 4..12 bar orderly pullback -> LH2 (LH2 < LH1).
          - Zero-Tolerance Anti-Breakout Veto: Price must never close OR open across
            the opposite side of the 20 EMA Band (a candle piercing from below/above
            is a breakout, never a pullback rejection).
        Args:
            virtual_bar: Optional in-flight forming bar (Mode A Pre-Close). When provided,
            it is appended as the LAST bar (the touch2 candidate) so the Dow HL2/LH2
            chain is judged on the live candle instead of the previous closed bar.
        """
    chain = DowStructureChain()
    n = min(lookback, len(eng.bars_1m))
    if n < 12:
        chain.veto_reason = "insufficient_bars_for_dow_structure"
        return chain

    bars = list(eng.bars_1m)[-n:]
    e_close, e_high, e_low = get_20_ema_band_arrays(eng, n)
    if virtual_bar is not None:
        bars.append(virtual_bar)
        e_close.append(eng.ema_20_1m if getattr(eng, "ema_20_1m", 0.0) > 0 else (e_close[-1] if e_close else 0.0))
        e_high.append(eng.ema_20_high_1m if getattr(eng, "ema_20_high_1m", 0.0) > 0 else (e_high[-1] if e_high else 0.0))
        e_low.append(eng.ema_20_low_1m if getattr(eng, "ema_20_low_1m", 0.0) > 0 else (e_low[-1] if e_low else 0.0))
    atr = eng.current_atr_1m if eng.current_atr_1m > 0 else (bars[-1].close * 0.001)
    tol_pts = max(1.5, bars[-1].close * (tolerance_bps / 10000.0), 0.12 * atr)
    min_bos_pts = max(1.5, 0.15 * atr)
    min_hl_diff_pts = max(1.0, 0.10 * atr)

    idx_t2 = len(bars) - 1 - touch2_offset
    if idx_t2 < 10:
        chain.veto_reason = "insufficient_history_before_touch2"
        return chain

    is_call = direction.upper() in ("UP", "CALL")

    # ═══════════════════════════════════════════════════════════
    # ═══════════════════════════════════════════════════════════
    # CASE 1: CALL — UPTREND (HH1 -> HL1 -> HH2 -> HL2)
    # ═══════════════════════════════════════════════════════════
    if is_call:
        strict_floor_tol = 0.25 * tol_pts
        # 1. Zero-Tolerance Anti-Breakout Veto (last 10 bars):
        #    A candle that OPENS below the band and closes back above is a
        #    breakout-from-below pierce, not an orderly above-band pullback.
        recent_start = max(0, idx_t2 - 10)
        for j in range(recent_start, idx_t2 + 1):
            if bars[j].close < (e_low[j] - strict_floor_tol) or bars[j].open < (e_low[j] - strict_floor_tol):
                chain.veto_reason = "breakout_from_below_veto"
                return chain

        hl2_price = min(bars[idx_t2].low, bars[-1].low)

        # Continuous uptrend start from idx_t2
        trend_start = idx_t2
        for j in range(idx_t2, -1, -1):
            if bars[j].close < (e_low[j] - strict_floor_tol) or bars[j].open < (e_low[j] - strict_floor_tol):
                break
            trend_start = j

        trend_age = idx_t2 - trend_start
        if trend_age < 10:
            chain.veto_reason = "noise_veto_pullback_too_short"
            return chain

        # 2. Find Global HH2 within 4 to 12 bars prior to idx_t2
        # 2. Find Global HH2 within 5 to 10 bars prior to idx_t2
        search_hh2_start = max(trend_start + 3, idx_t2 - 12)
        if idx_t2 - 5 < search_hh2_start:
            chain.veto_reason = "noise_veto_pullback_too_short"
            return chain

        idx_hh2 = max(range(search_hh2_start, idx_t2), key=lambda j: bars[j].high)
        hh2_price = bars[idx_hh2].high

        if hh2_price <= (e_high[idx_hh2] + 0.20 * atr) or max(bars[idx_hh2].open, bars[idx_hh2].close) < e_high[idx_hh2]:
            chain.veto_reason = "no_established_hh2_above_band"
            return chain

        pullback_bars = idx_t2 - idx_hh2
        if not (5 <= pullback_bars <= 10):
            chain.veto_reason = f"noise_veto_pullback_out_of_bounds ({pullback_bars})"
            return chain

        # 3. Orderly Descending Retracement Check (>= 60%)
        descent_steps = 0
        for j in range(idx_hh2 + 1, idx_t2 + 1):
            if bars[j].close < (e_low[j] - strict_floor_tol) or bars[j].open < (e_low[j] - strict_floor_tol):
                chain.veto_reason = "breakout_from_below_veto"
                return chain
            if (bars[j].close < bars[j].open) or (bars[j].high <= bars[j - 1].high) or (bars[j].close <= bars[j - 1].close):
                descent_steps += 1

        orderly_ratio = descent_steps / float(pullback_bars) if pullback_bars > 0 else 0.0
        if orderly_ratio < 0.60:
            chain.veto_reason = f"erratic_pullback_not_orderly ({orderly_ratio:.2f})"
            return chain

        if (hh2_price - hl2_price) < (0.35 * atr):
            chain.veto_reason = "shallow_pullback_insufficient_retracement"
            return chain

        # 4. Find Continuous Uptrend Pair (HH1, HL1) Prior to HH2
        found_pair = False
        idx_hh1, hh1_price, idx_hl1, hl1_price = -1, -1.0, -1, 1e18

        for m in range(idx_hh2 - 4, trend_start, -1):
            if m <= 0 or m + 1 >= idx_hh2:
                continue
            if bars[m].high <= e_high[m]:
                continue
            cand_hh1 = bars[m].high

            trough_idx = min(range(m + 1, idx_hh2), key=lambda x: bars[x].low)
            cand_hl1 = bars[trough_idx].low
            if trough_idx <= m or trough_idx >= idx_hh2:
                continue
            trough_tested = cand_hl1 <= (e_high[trough_idx] + tol_pts)

            if (
                trough_tested
                and (cand_hh1 - cand_hl1) >= (0.25 * atr)
                and (hh2_price >= cand_hh1 + min_bos_pts)
                and (hl2_price >= cand_hl1 + min_hl_diff_pts)
            ):
                idx_hh1, hh1_price = m, cand_hh1
                idx_hl1, hl1_price = trough_idx, cand_hl1
                found_pair = True
                break

        if not found_pair:
            chain.veto_reason = "first_touch_hl1_or_hh1_structure_not_met"
            return chain

        # 5. Swing vs Retracement Volume & Flow Validation
        imp_vol_slice = [getattr(b, "volume", 0.0) or 0.0 for b in bars[idx_hl1 + 1 : idx_hh2 + 1]]
        imp_avg_vol = (sum(imp_vol_slice) / len(imp_vol_slice)) if imp_vol_slice else 0.0
        pb_vol_slice = [getattr(b, "volume", 0.0) or 0.0 for b in bars[idx_hh2 + 1 : idx_t2 + 1]]
        pb_avg_vol = (sum(pb_vol_slice) / len(pb_vol_slice)) if pb_vol_slice else 0.0
        entry_vol = getattr(bars[-1], "volume", 0.0) or 0.0

        imp_bull_slice = [
            float(getattr(b, "call_buy_vol", 0.0) or 0.0) + float(getattr(b, "put_sell_vol", 0.0) or 0.0)
            for b in bars[idx_hl1 + 1 : idx_hh2 + 1]
        ]
        imp_bull_flow = (sum(imp_bull_slice) / len(imp_bull_slice)) if imp_bull_slice else 0.0

        pb_bear_slice = [
            float(getattr(b, "put_buy_vol", 0.0) or 0.0) + float(getattr(b, "call_sell_vol", 0.0) or 0.0)
            for b in bars[idx_hh2 + 1 : idx_t2 + 1]
        ]
        pb_bear_flow = (sum(pb_bear_slice) / len(pb_bear_slice)) if pb_bear_slice else 0.0

        imp_directional_flow = imp_bull_flow
        pb_counter_flow = pb_bear_flow
        swing_vdr = round(pb_avg_vol / imp_avg_vol, 2) if imp_avg_vol > 0 else 1.0

        if imp_avg_vol > 10.0:
            if pb_avg_vol > 1.15 * imp_avg_vol:
                chain.veto_reason = f"volume_dump_pullback_heavy (pb={pb_avg_vol:.0f} > 1.15*imp={imp_avg_vol:.0f})"
                return chain
            if entry_vol < 0.70 * pb_avg_vol:
                chain.veto_reason = f"entry_volume_too_thin (entry={entry_vol:.0f} < 0.70*pb={pb_avg_vol:.0f})"
                return chain

        if imp_directional_flow > 0 and pb_counter_flow > 1.15 * imp_directional_flow:
            chain.veto_reason = "wyckoff_pullback_counter_flow_too_heavy"
            return chain

        chain.is_valid = True
        chain.veto_reason = "dow_hl2_confirmed"
        chain.idx_1, chain.idx_touch1, chain.idx_2, chain.idx_touch2 = idx_hh1, idx_hl1, idx_hh2, idx_t2
        chain.impulse_1, chain.touch_1, chain.impulse_2, chain.touch_2 = hh1_price, hl1_price, hh2_price, hl2_price
        chain.pullback_bars = pullback_bars
        chain.orderly_ratio = round(orderly_ratio, 2)
        chain.imp_avg_vol = round(imp_avg_vol, 1)
        chain.pb_avg_vol = round(pb_avg_vol, 1)
        chain.entry_vol = round(entry_vol, 1)
        chain.swing_vdr = swing_vdr
        chain.imp_directional_flow = round(imp_directional_flow, 1)
        chain.pb_counter_flow = round(pb_counter_flow, 1)
        return chain

    # ═══════════════════════════════════════════════════════════
    # CASE 2: PUT — DOWNTREND (LL1 -> LH1 -> LL2 -> LH2)
    # ═══════════════════════════════════════════════════════════
    else:
        strict_roof_tol = 0.25 * tol_pts
        # 1. Zero-Tolerance Anti-Breakdown Veto (last 10 bars):
        #    A candle that OPENS above the band and closes back below is a
        #    breakdown-from-above pierce, not an orderly below-band pullback.
        recent_start = max(0, idx_t2 - 10)
        for j in range(recent_start, idx_t2 + 1):
            if bars[j].close > (e_high[j] + strict_roof_tol) or bars[j].open > (e_high[j] + strict_roof_tol):
                chain.veto_reason = "breakdown_from_above_veto"
                return chain

        lh2_price = max(bars[idx_t2].high, bars[-1].high)

        # Continuous downtrend start from idx_t2
        trend_start = idx_t2
        for j in range(idx_t2, -1, -1):
            if bars[j].close > (e_high[j] + strict_roof_tol) or bars[j].open > (e_high[j] + strict_roof_tol):
                break
            trend_start = j

        trend_age = idx_t2 - trend_start
        if trend_age < 10:
            chain.veto_reason = "noise_veto_pullback_too_short"
            return chain

        # 2. Find Global LL2 within 5 to 10 bars prior to idx_t2
        search_ll2_start = max(trend_start + 3, idx_t2 - 12)
        if idx_t2 - 5 < search_ll2_start:
            chain.veto_reason = "noise_veto_pullback_too_short"
            return chain

        idx_ll2 = min(range(search_ll2_start, idx_t2), key=lambda j: bars[j].low)
        ll2_price = bars[idx_ll2].low

        if ll2_price >= (e_low[idx_ll2] - 0.20 * atr) or min(bars[idx_ll2].open, bars[idx_ll2].close) > e_low[idx_ll2]:
            chain.veto_reason = "no_established_ll2_below_band"
            return chain

        pullback_bars = idx_t2 - idx_ll2
        if not (5 <= pullback_bars <= 10):
            chain.veto_reason = f"noise_veto_pullback_out_of_bounds ({pullback_bars})"
            return chain

        # 3. Orderly Ascending Retracement Check (>= 60%)
        ascent_steps = 0
        for j in range(idx_ll2 + 1, idx_t2 + 1):
            if bars[j].close > (e_high[j] + strict_roof_tol) or bars[j].open > (e_high[j] + strict_roof_tol):
                chain.veto_reason = "breakdown_from_above_veto"
                return chain
            if (bars[j].close > bars[j].open) or (bars[j].low >= bars[j - 1].low) or (bars[j].close >= bars[j - 1].close):
                ascent_steps += 1

        orderly_ratio = ascent_steps / float(pullback_bars) if pullback_bars > 0 else 0.0
        if orderly_ratio < 0.60:
            chain.veto_reason = f"erratic_pullback_not_orderly ({orderly_ratio:.2f})"
            return chain

        if (lh2_price - ll2_price) < (0.35 * atr):
            chain.veto_reason = "shallow_pullback_insufficient_retracement"
            return chain

        # 4. Find Continuous Downtrend Pair (LL1, LH1) Prior to LL2
        found_pair = False
        idx_ll1, ll1_price, idx_lh1, lh1_price = -1, 1e18, -1, -1.0

        for m in range(idx_ll2 - 4, trend_start, -1):
            if m <= 0 or m + 1 >= idx_ll2:
                continue
            if bars[m].low >= e_low[m]:
                continue
            cand_ll1 = bars[m].low

            peak_idx = max(range(m + 1, idx_ll2), key=lambda x: bars[x].high)
            cand_lh1 = bars[peak_idx].high
            if peak_idx <= m or peak_idx >= idx_ll2:
                continue
            peak_tested = cand_lh1 >= (e_low[peak_idx] - tol_pts)

            if (
                peak_tested
                and (cand_lh1 - cand_ll1) >= (0.25 * atr)
                and (ll2_price <= cand_ll1 - min_bos_pts)
                and (lh2_price <= cand_lh1 - min_hl_diff_pts)
            ):
                idx_ll1, ll1_price = m, cand_ll1
                idx_lh1, lh1_price = peak_idx, cand_lh1
                found_pair = True
                break

        if not found_pair:
            chain.veto_reason = "first_touch_lh1_or_ll1_structure_not_met"
            return chain

        # 5. Swing vs Retracement Volume & Flow Validation
        imp_vol_slice = [getattr(b, "volume", 0.0) or 0.0 for b in bars[idx_lh1 + 1 : idx_ll2 + 1]]
        imp_avg_vol = (sum(imp_vol_slice) / len(imp_vol_slice)) if imp_vol_slice else 0.0
        pb_vol_slice = [getattr(b, "volume", 0.0) or 0.0 for b in bars[idx_ll2 + 1 : idx_t2 + 1]]
        pb_avg_vol = (sum(pb_vol_slice) / len(pb_vol_slice)) if pb_vol_slice else 0.0
        entry_vol = getattr(bars[-1], "volume", 0.0) or 0.0

        imp_bear_slice = [
            float(getattr(b, "put_buy_vol", 0.0) or 0.0) + float(getattr(b, "call_sell_vol", 0.0) or 0.0)
            for b in bars[idx_lh1 + 1 : idx_ll2 + 1]
        ]
        imp_bear_flow = (sum(imp_bear_slice) / len(imp_bear_slice)) if imp_bear_slice else 0.0

        pb_bull_slice = [
            float(getattr(b, "call_buy_vol", 0.0) or 0.0) + float(getattr(b, "put_sell_vol", 0.0) or 0.0)
            for b in bars[idx_ll2 + 1 : idx_t2 + 1]
        ]
        pb_bull_flow = (sum(pb_bull_slice) / len(pb_bull_slice)) if pb_bull_slice else 0.0

        imp_directional_flow = imp_bear_flow
        pb_counter_flow = pb_bull_flow
        swing_vdr = round(pb_avg_vol / imp_avg_vol, 2) if imp_avg_vol > 0 else 1.0

        if imp_avg_vol > 10.0:
            if pb_avg_vol > 1.15 * imp_avg_vol:
                chain.veto_reason = f"volume_dump_pullback_heavy (pb={pb_avg_vol:.0f} > 1.15*imp={imp_avg_vol:.0f})"
                return chain
            if entry_vol < 0.70 * pb_avg_vol:
                chain.veto_reason = f"entry_volume_too_thin (entry={entry_vol:.0f} < 0.70*pb={pb_avg_vol:.0f})"
                return chain

        if imp_directional_flow > 0 and pb_counter_flow > 1.15 * imp_directional_flow:
            chain.veto_reason = "wyckoff_pullback_counter_flow_too_heavy"
            return chain

        chain.is_valid = True
        chain.veto_reason = "dow_lh2_confirmed"
        chain.idx_1, chain.idx_touch1, chain.idx_2, chain.idx_touch2 = idx_ll1, idx_lh1, idx_ll2, idx_t2
        chain.impulse_1, chain.touch_1, chain.impulse_2, chain.touch_2 = ll1_price, lh1_price, ll2_price, lh2_price
        chain.pullback_bars = pullback_bars
        chain.orderly_ratio = round(orderly_ratio, 2)
        chain.imp_avg_vol = round(imp_avg_vol, 1)
        chain.pb_avg_vol = round(pb_avg_vol, 1)
        chain.entry_vol = round(entry_vol, 1)
        chain.swing_vdr = swing_vdr
        chain.imp_directional_flow = round(imp_directional_flow, 1)
        chain.pb_counter_flow = round(pb_counter_flow, 1)
        return chain

def _check_day_extreme_runway(
    eng,
    direction: str,
    current_price: float,
) -> Tuple[bool, str, Dict[str, Any]]:
    """
        Scale-Invariant Macro Runway & Day Extreme Clearance Rule for Breakout Pillars (P3 & P4).
        Enforces:
        1. True Structural Expansion: If at or breaking Day Extreme, must decisively clear it:
           - PUT: Close <= Day_Low - min_clearance
           - CALL: Close >= Day_High + min_clearance
        2. Internal Breakout Runway: If breaking an internal box/hub before Day Extreme, must have open runway:
           - min_runway_pts = max(1.25 * ATR_{1m}, 0.0008 * Spot)
           - PUT: Close - Day_Low >= min_runway_pts
           - CALL: Day_High - Close >= min_runway_pts
        """
    if eng.current_atr_1m <= 0 or current_price <= 0:
        return True, "clear", {}

    if len(eng.bars_1m) < 4:
        return True, "clear_insufficient_bars", {}

    curr_bar = eng.bars_1m[-1]
    curr_day = int(curr_bar.ts_start // 86400) if curr_bar else eng._current_vwap_day
    is_inflight = (eng._curr_1m_bar is not None and getattr(eng._curr_1m_bar, "tick_count", 0) > 0)
    source_bars = list(eng.bars_1m) if is_inflight else list(eng.bars_1m)[:-1]
    intraday_prior_bars = [
        b for b in source_bars
        if (curr_day <= 0 or int(b.ts_start // 86400) == curr_day)
    ]
    if not intraday_prior_bars and len(eng.bars_1m) >= 2:
        intraday_prior_bars = source_bars

    if not intraday_prior_bars or len(intraday_prior_bars) < 3:
        return True, "clear_insufficient_prior_bars", {}

    # Filter out 09:15 freak pre-open settlement tick when calculating day extremes
    ref_atr = eng.current_atr_1m if eng.current_atr_1m > 0 else (current_price * 0.001)
    valid_prior_bars = []
    for idx, b in enumerate(intraday_prior_bars):
        if idx == 0 and len(intraday_prior_bars) > 1:
            next_b = intraday_prior_bars[1]
            is_freak = (
                (b.high - b.low <= 5.0)
                and (abs(b.high - next_b.high) > (1.5 * ref_atr) or abs(b.low - next_b.low) > (1.5 * ref_atr))
            )
            if is_freak:
                continue
        valid_prior_bars.append(b)

    bars_for_extremes = valid_prior_bars if valid_prior_bars else intraday_prior_bars
    day_high = max(b.high for b in bars_for_extremes)
    day_low = min(b.low for b in bars_for_extremes)
    min_runway_pts = max(1.25 * eng.current_atr_1m, 0.0008 * current_price)
    min_clearance = max(2.0, (current_price / 24000.0) * 2.0, 0.08 * eng.current_atr_1m)

    if direction.upper() in ("DOWN", "PUT"):
        if current_price > day_low:
            dist_to_day_low = current_price - day_low
            if dist_to_day_low < min_runway_pts:
                return False, "insufficient_runway_to_day_low", {
                    "day_low": round(day_low, 2),
                    "room_pts": round(dist_to_day_low, 2),
                    "threshold_pts": round(min_runway_pts, 2),
                    "atr_1m": round(eng.current_atr_1m, 2),
                }
        elif current_price > (day_low - min_clearance):
            return False, "insufficient_day_low_clearance", {
                "day_low": round(day_low, 2),
                "clearance_pts": round(day_low - current_price, 2),
                "req_clearance_pts": round(min_clearance, 2),
                "atr_1m": round(eng.current_atr_1m, 2),
            }

    elif direction.upper() in ("UP", "CALL"):
        if current_price < day_high:
            dist_to_day_high = day_high - current_price
            if dist_to_day_high < min_runway_pts:
                return False, "insufficient_runway_to_day_high", {
                    "day_high": round(day_high, 2),
                    "room_pts": round(dist_to_day_high, 2),
                    "threshold_pts": round(min_runway_pts, 2),
                    "atr_1m": round(eng.current_atr_1m, 2),
                }
        elif current_price < (day_high + min_clearance):
            return False, "insufficient_day_high_clearance", {
                "day_high": round(day_high, 2),
                "clearance_pts": round(current_price - day_high, 2),
                "req_clearance_pts": round(min_clearance, 2),
                "atr_1m": round(eng.current_atr_1m, 2),
            }

    return True, "clear_day_extreme_runway", {}

def _check_structural_obstacle(
    eng,
    direction: str,
    current_price: float,
    lookback: int = 120,
    ignore_pivots_below: Optional[float] = None,
    ignore_pivots_above: Optional[float] = None,
    check_day_extremes: bool = False,
) -> Tuple[bool, str, Dict[str, Any]]:
    """
        Structural Obstacle Shield (Dynamic S/R Registry Anchors + Multi-Touch Horizontal Clusters).
        Prevents buying PUTs directly into an underlying support wall (e.g. 11:33 & 11:39 double-bottom)
        or buying CALLs directly into an overhead resistance wall.
        Threshold is normalized via localized volatility (0.85x ATR).
        """
    if eng.current_atr_1m <= 0 or current_price <= 0:
        return True, "clear", {}

    hurdle_pts = 0.85 * eng.current_atr_1m

    # 1m Structural Horizontal Pivots / Multi-Touch Clusters
    if len(eng.bars_1m) >= 4:
        is_inflight_obs = (eng._curr_1m_bar is not None and getattr(eng._curr_1m_bar, "tick_count", 0) > 0)
        bars_src = list(eng.bars_1m) if is_inflight_obs else list(eng.bars_1m)[:-1]
        bars = bars_src[-lookback:]
        pivot_lows = []
        pivot_highs = []
        prom = 0.10 * eng.current_atr_1m
        for i in range(1, len(bars) - 1):
            p_b, c_b, n_b = bars[i - 1], bars[i], bars[i + 1]
            if c_b.low <= (p_b.low - prom) and c_b.low < (n_b.low - prom):
                pivot_lows.append(c_b.low)
            if c_b.high >= (p_b.high + prom) and c_b.high > (n_b.high + prom):
                pivot_highs.append(c_b.high)

        if direction.upper() in ("DOWN", "PUT"):
            # Find any established support level below current price or at current price
            # If ignore_pivots_above is specified (e.g. LL1), broken prior lows >= that level are ignored
            supports_below = [
                pl for pl in pivot_lows
                if pl <= current_price and (ignore_pivots_above is None or pl < ignore_pivots_above)
            ]
            if supports_below:
                nearest_support = max(supports_below)
                dist_to_support = current_price - nearest_support
                if dist_to_support < hurdle_pts:
                    return False, "trapped_near_support", {
                        "support_level": round(nearest_support, 2),
                        "room_pts": round(dist_to_support, 2),
                        "threshold_pts": round(hurdle_pts, 2),
                        "atr_1m": round(eng.current_atr_1m, 2),
                    }

        elif direction.upper() in ("UP", "CALL"):
            # If ignore_pivots_below is specified (e.g. HH1), broken prior highs <= that level are ignored
            resistances_above = [
                ph for ph in pivot_highs
                if ph >= current_price and (ignore_pivots_below is None or ph > ignore_pivots_below)
            ]
            if resistances_above:
                nearest_resistance = min(resistances_above)
                dist_to_res = nearest_resistance - current_price
                if dist_to_res < hurdle_pts:
                    return False, "trapped_near_resistance", {
                        "resistance_level": round(nearest_resistance, 2),
                        "room_pts": round(dist_to_res, 2),
                        "threshold_pts": round(hurdle_pts, 2),
                        "atr_1m": round(eng.current_atr_1m, 2),
                    }

    # 3. Macro Runway & Day Extreme Clearance Rule (Breakout Setups)
    if check_day_extremes:
        ok_de, r_de, det_de = _check_day_extreme_runway(eng, direction, current_price)
        if not ok_de:
            return False, r_de, det_de

    return True, "clear_of_structural_obstacles", {}

# DEPRECATED import-compat shell: NOT a base class of anything in this package. Legacy `from ... import StructuralRegistryMixin` and
# test doubles that subclass it keep working; the logic lives in the module-level functions above (explicit `eng` arg).
class StructuralRegistryMixin:
    """Deprecated compat shell; see module functions."""


for _f in (_update_structural_levels, get_active_sr_registry_snapshot, seed_daily_levels, _classify_level_roles, seed_previous_day_5m_bars, check_key_level_proximity, _find_swept_sr_level, _find_inflight_swept_sr_level):
    setattr(StructuralRegistryMixin, _f.__name__, _f)
for _f in (_parse_candle_timestamp, _cluster_and_merge_levels):
    setattr(StructuralRegistryMixin, _f.__name__, staticmethod(_f))
del _f

# DEPRECATED import-compat shell: NOT a base class of anything in this package. Legacy `from ... import DowMacroShieldsMixin` and
# test doubles that subclass it keep working; the logic lives in the module-level functions above (explicit `eng` arg).
class DowMacroShieldsMixin:
    """Deprecated compat shell; see module functions."""


for _f in (get_20_ema_band_arrays, _validate_dow_hl2_lh2_structure, _check_day_extreme_runway, _check_structural_obstacle):
    setattr(DowMacroShieldsMixin, _f.__name__, _f)
for _f in (_tail_list,):
    setattr(DowMacroShieldsMixin, _f.__name__, staticmethod(_f))
del _f
