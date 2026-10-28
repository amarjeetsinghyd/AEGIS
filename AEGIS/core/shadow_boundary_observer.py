"""
AEGIS Shadow Boundary Observer (ShadowBoundaryObserver)
======================================================
Passive Microstructure Paper-Trader & Boundary Rejection Observer.

Monitors Two Macro Structural Boundaries:
  1. PAIN_ZONE_BAND: Live 4-Line Trapped Buyer VWAP Band (zone_top / zone_bottom)
  2. EMA_200_BAND: 200 EMA 1-Minute Ribbon (ema_200 ± 0.25 ATR)

Rules:
  - First-Touch / Any-Touch Boundary Rejection: Macro regime barriers reject on first contact.
  - Symmetrical Mirror: CALL (support bounce up) and PUT (resistance rejection down).
  - Mode A (In-Flight Intrabar Piercing): Fires immediately when forming tick pierces anchor high/low.
  - OTM 5 Paper Trading: Selects OTM 5 strike (ATM ± 5 strikes) and tracks simulated fills.
  - Microsecond Telemetry:
      * Next 30 seconds excursion (Spot & Option MFE/MAE)
      * Next 60 seconds excursion (Spot & Option MFE/MAE)
      * Peak 300 seconds runway (+4.0% Maker TP vs -12.0% Disaster SL)
  - 100% Shadow / Zero Real Orders: Passive telemetry logging to JSONL.
"""

import os
import json
import time
import logging
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any, List, Tuple

logger = logging.getLogger("aegis.shadow_boundary")


@dataclass
class ShadowTrade:
    trade_id: str
    boundary: str            # 'PAIN_ZONE_BAND' or 'EMA_200_BAND'
    direction: str           # 'UP' (CALL) or 'DOWN' (PUT)
    entry_ts: float          # Epoch timestamp in seconds
    entry_time_str: str      # HH:MM:SS
    spot_entry: float
    spot_anchor_high: float
    spot_anchor_low: float
    spot_sl_price: float
    
    # OTM 5 Option Details
    opt_symbol: str
    opt_strike: float
    opt_side: str            # 'CE' or 'PE'
    opt_entry: float
    opt_tp_price: float      # Entry + 4.0%
    opt_sl_price: float      # Entry - 12.0%
    
    # Telemetry Tracking
    spot_mfe_30s: float = 0.0
    spot_mae_30s: float = 0.0
    spot_mfe_60s: float = 0.0
    spot_mae_60s: float = 0.0
    spot_peak_mfe: float = 0.0
    spot_peak_mae: float = 0.0
    
    opt_mfe_30s_pct: float = 0.0
    opt_mae_30s_pct: float = 0.0
    opt_mfe_60s_pct: float = 0.0
    opt_mae_60s_pct: float = 0.0
    opt_peak_mfe_pct: float = 0.0
    opt_peak_mae_pct: float = 0.0
    
    # N-1 Testing Candle Wyckoff & Order Flow Footprint (Empirical Shadow Tracking)
    n1_vol: float = 0.0
    n1_baseline_vol: float = 0.0
    n1_vol_dryup: bool = False
    n1_net_delta: float = 0.0
    n1_stopping_vol: bool = False

    # Pain Zone 4-Line Snapshot & Band Spread (Empirical Tracking)
    pz_top: float = 0.0
    pz_bottom: float = 0.0
    pz_spread_pts: float = 0.0
    pz_call_buy: float = 0.0
    pz_call_sell: float = 0.0
    pz_put_buy: float = 0.0
    pz_put_sell: float = 0.0
    
    # State & Outcome
    captured_30s: bool = False
    captured_60s: bool = False
    is_active: bool = True
    exit_ts: float = 0.0
    exit_reason: str = "RUNNING"
    outcome: str = "PENDING"  # 'WINNER' (+4% TP), 'LOSER' (SL), 'EXPIRED'
    duration_sec: float = 0.0


class ShadowBoundaryObserver:
    """
    Evaluates real-time First-Touch Boundary Rejections on Pain Zone and 200 EMA,
    paper trades OTM 5 options, and tracks sub-minute (30s/60s/300s) forward telemetry.
    """

    def __init__(
        self,
        price_action_engine: Any = None,
        wyckoff_engine: Any = None,
        strike_step: float = 50.0,
        log_dir: Optional[str] = None,
    ):
        self.pa_engine = price_action_engine
        self.wyckoff_engine = wyckoff_engine
        self.strike_step = float(strike_step)
        
        # Paths
        default_dir = os.path.join(os.path.dirname(__file__), "..", "data", "logs", "telemetry")
        self.log_dir = log_dir or os.getenv("AEGIS_TELEMETRY_DIR", default_dir)
        try:
            os.makedirs(self.log_dir, exist_ok=True)
        except Exception:
            pass
            
        self.active_trades: List[ShadowTrade] = []
        
        # Armed Boundary Setup State
        # boundary_name -> {'direction': 'UP'/'DOWN', 'anchor_high': float, 'anchor_low': float, 'armed_bar_idx': int, 'ts': float}
        self.armed_setups: Dict[str, Dict[str, Any]] = {}
        self.last_trigger_bar_idx: Dict[str, int] = {}
        self._mode_b_up_ticks: int = 0
        self._mode_b_down_ticks: int = 0
        
        # References populated by AegisPipelineOrchestrator
        self.option_cache: Dict[str, float] = {}
        self.strike_to_symbol: Dict[Tuple[int, float], str] = {}
        self.current_atm_strike: float = 0.0
        
        logger.info(f"[ShadowBoundaryObserver] Initialized (StrikeStep={self.strike_step}, LogDir={self.log_dir})")

    def update_atm(self, atm: float) -> None:
        self.current_atm_strike = float(atm)

    def select_otm5_symbol(self, direction: str, spot: float) -> Tuple[str, float, str]:
        """
        Selects OTM 5 contract based on current ATM and direction.
        Returns (symbol, strike, side_str).
        """
        atm = self.current_atm_strike or (round(spot / self.strike_step) * self.strike_step)
        if direction.upper() in ("UP", "CALL", "CE"):
            side_int = 0
            side_str = "CE"
            target_strike = atm + 5.0 * self.strike_step
        else:
            side_int = 1
            side_str = "PE"
            target_strike = atm - 5.0 * self.strike_step
            
        sym = self.strike_to_symbol.get((side_int, target_strike), "")
        return sym, target_strike, side_str

    def on_option_tick(self, sym: str, lp: float, now_sec: float) -> None:
        """Updates active shadow trades with option tick prices."""
        if lp <= 0 or not self.active_trades:
            return
            
        for trade in self.active_trades:
            if not trade.is_active or trade.opt_symbol != sym:
                continue
                
            entry_opt = trade.opt_entry
            if entry_opt <= 0:
                continue
                
            pnl_pct = ((lp - entry_opt) / entry_opt) * 100.0
            
            # Update peak MFE / MAE
            if pnl_pct > trade.opt_peak_mfe_pct:
                trade.opt_peak_mfe_pct = pnl_pct
            if pnl_pct < trade.opt_peak_mae_pct:
                trade.opt_peak_mae_pct = pnl_pct
                
            elapsed = now_sec - trade.entry_ts
            
            # 30s Snapshot
            if elapsed >= 30.0 and not trade.captured_30s:
                trade.opt_mfe_30s_pct = trade.opt_peak_mfe_pct
                trade.opt_mae_30s_pct = trade.opt_peak_mae_pct
                trade.captured_30s = True
                
            # 60s Snapshot
            if elapsed >= 60.0 and not trade.captured_60s:
                trade.opt_mfe_60s_pct = trade.opt_peak_mfe_pct
                trade.opt_mae_60s_pct = trade.opt_peak_mae_pct
                trade.captured_60s = True
                
            # Exit Conditions
            # 1. Target Hit (+4.0% Maker TP)
            if lp >= trade.opt_tp_price:
                self._close_trade(trade, now_sec, "TAKE_PROFIT_HIT", "WINNER")
            # 2. Disaster SL Hit (-12.0%)
            elif lp <= trade.opt_sl_price:
                self._close_trade(trade, now_sec, "DISASTER_SL_HIT", "LOSER")
            # 3. Max hold time expired (300s / 5 mins)
            elif elapsed >= 300.0:
                outcome = "WINNER" if pnl_pct > 0 else "LOSER"
                self._close_trade(trade, now_sec, "TIME_EXPIRED_300S", outcome)

    def on_underlying_tick(self, spot: float, now_sec: float) -> None:
        """
        Processes underlying spot index tick:
          1. Updates active shadow trades with spot MFE/MAE.
          2. Checks In-Flight Intrabar piercing of armed boundary setups.
          3. Evaluates boundary touches at bar close.
        """
        if spot <= 0:
            return
            
        # 1. Update Active Trades Spot Excursion
        for trade in list(self.active_trades):
            if not trade.is_active:
                continue
                
            elapsed = now_sec - trade.entry_ts
            if trade.direction == "UP":
                mfe_pts = spot - trade.spot_entry
                mae_pts = trade.spot_entry - spot
            else:
                mfe_pts = trade.spot_entry - spot
                mae_pts = spot - trade.spot_entry
                
            if mfe_pts > trade.spot_peak_mfe:
                trade.spot_peak_mfe = mfe_pts
            if mae_pts > trade.spot_peak_mae:
                trade.spot_peak_mae = mae_pts
                
            if elapsed >= 30.0 and not trade.captured_30s:
                trade.spot_mfe_30s = trade.spot_peak_mfe
                trade.spot_mae_30s = trade.spot_peak_mae
                
            if elapsed >= 60.0 and not trade.captured_60s:
                trade.spot_mfe_60s = trade.spot_peak_mfe
                trade.spot_mae_60s = trade.spot_peak_mae
                
            # Spot Invalidation Check
            if trade.direction == "UP" and spot <= trade.spot_sl_price:
                self._close_trade(trade, now_sec, "SPOT_INVALIDATION_SL", "LOSER")
            elif trade.direction == "DOWN" and spot >= trade.spot_sl_price:
                self._close_trade(trade, now_sec, "SPOT_INVALIDATION_SL", "LOSER")
            elif elapsed >= 300.0:
                self._close_trade(trade, now_sec, "TIME_EXPIRED_300S", "EXPIRED")

        # 2. Check In-Flight Intrabar Trigger on Armed Boundaries (Mode A: Rejection Bounce & Pierce)
        self._check_inflight_piercing(spot, now_sec)

        # 3. Check In-Flight Breakthrough Surge on Active Forming Candle (Mode B: Breakout through Pain Zone / 200 EMA)
        self._check_inflight_breakthrough(spot, now_sec)

        # 4. Check for New Armed Setups at Bar Transitions
        self._evaluate_boundary_arming(spot, now_sec)

    def _check_inflight_piercing(self, spot: float, now_sec: float) -> None:
        """
        Fires Mode A In-Flight Entry when live tick pierces anchor price.
        Active during T:00s - T:20s of Bar N+1.
        """
        if not self.armed_setups:
            return

        sec_in_min = int(now_sec % 60)
        atr = getattr(self.pa_engine, "current_atr_1m", 10.0) or 10.0
        pz_top, pz_bot = self._get_pain_zone_levels(atr)

        for b_name in list(self.armed_setups.keys()):
            armed = self.armed_setups[b_name]
            direction = armed["direction"]
            anchor_h = armed["anchor_high"]
            anchor_l = armed["anchor_low"]

            # 1. Priority Window: Disarm after 20s if not pierced
            if sec_in_min > 20:
                del self.armed_setups[b_name]
                continue

            # 2. Auto-Flip Disarm Rule: If new candle reverses across opposite boundary by 0.15*ATR
            if direction == "UP" and pz_bot > 0 and spot <= (pz_bot - 0.15 * atr):
                del self.armed_setups[b_name]
                continue
            elif direction == "DOWN" and pz_top > 0 and spot >= (pz_top + 0.15 * atr):
                del self.armed_setups[b_name]
                continue

            triggered = False
            if direction == "UP" and spot >= anchor_h:
                triggered = True
            elif direction == "DOWN" and spot <= anchor_l:
                triggered = True

            if triggered:
                # Fire Shadow Trade!
                del self.armed_setups[b_name]
                self._open_shadow_trade(b_name, direction, spot, anchor_h, anchor_l, now_sec)

    def _check_inflight_breakthrough(self, spot: float, now_sec: float) -> None:
        """
        Mode B: Live In-Flight Intra-Bar Breakthrough Surge with 4-Point Trap Filter.
        Triggers when an active forming 1-minute candle breaks through the Pain Zone, 200 EMA,
        or 20 EMA with strong directional expansion from T:08s onwards (upper cap removed per user mandate).
        """
        if self.pa_engine is None:
            return
        curr_bar = getattr(self.pa_engine, "_curr_1m_bar", None)
        if curr_bar is None or curr_bar.tick_count < 2:
            return

        sec_in_min = int(now_sec % 60)
        # Active intra-bar expansion window from T:02s onwards (rollover guard T:00s-T:01s)
        if sec_in_min < 2:
            return

        atr = getattr(self.pa_engine, "current_atr_1m", 10.0) or 10.0
        if atr <= 0:
            atr = 10.0

        curr_rng = curr_bar.high - curr_bar.low
        # Zero-latency: activity floor and min_clearance removed per Mode B parity

        # 2. In-Flight Option Flow & Spot Tick Arrival Rate
        tick_rate = curr_bar.tick_count / max(float(sec_in_min), 1.0)
        opt_flow = getattr(self.pa_engine, "_opt_flow_1m", None)
        if not isinstance(opt_flow, dict):
            opt_flow = {}
        cb = float(opt_flow.get("call_buy_vol", 0.0) or getattr(curr_bar, "call_buy_vol", 0.0) or 0.0)
        cs = float(opt_flow.get("call_sell_vol", 0.0) or getattr(curr_bar, "call_sell_vol", 0.0) or 0.0)
        pb = float(opt_flow.get("put_buy_vol", 0.0) or getattr(curr_bar, "put_buy_vol", 0.0) or 0.0)
        ps = float(opt_flow.get("put_sell_vol", 0.0) or getattr(curr_bar, "put_sell_vol", 0.0) or 0.0)
        inflight_opt_vol = cb + cs + pb + ps

        # Cache 60m median volume at 1m bar transitions to eliminate per-tick list allocation & GC pressure
        total_bars = getattr(self.pa_engine, "_total_bars_seen", None)
        bar_count = int(total_bars) if isinstance(total_bars, (int, float)) else len(getattr(self.pa_engine, "bars_1m", []))
        if bar_count != getattr(self, "_cached_med_vol_bar_count", -1):
            bars = self.pa_engine.bars_1m
            n_bars = len(bars)
            start_idx = max(0, n_bars - 60)
            vols = [float(getattr(bars[i], "volume", 0.0) or 0.0) for i in range(start_idx, n_bars)]
            if vols:
                vols.sort()
                self._cached_med_vol_60 = float(vols[len(vols) // 2])
            else:
                self._cached_med_vol_60 = 1.0
            self._cached_med_vol_bar_count = bar_count

        med_vol = getattr(self, "_cached_med_vol_60", 1.0)
        if med_vol > 0 and inflight_opt_vol > 0:
            expected_opt_vol = max(1.0, med_vol * (float(sec_in_min) / 60.0))
            opt_pace_ok = (inflight_opt_vol / expected_opt_vol) >= 1.15
        else:
            opt_pace_ok = True

        if not (opt_pace_ok or tick_rate >= 1.5):
            return

        bar_idx = bar_count
        bars = getattr(self.pa_engine, "bars_1m", [])
        if not bars:
            return
        prior_bar = bars[-1]
        n1_fp = self._compute_n1_footprint(prior_bar, atr)

        # 1. Pain Zone Rejection-Breakout (LRRB)
        # Prior bar tested Pain Zone level, live candle breaks prior bar extreme (Strict 1-Bar Expiry)
        pz_top, pz_bot = self._get_pain_zone_levels(atr)
        if pz_top > 0 and pz_bot > 0:
            last_trig = max(self.last_trigger_bar_idx.get("PAIN_ZONE_REJECTION_BREAKOUT", -100), self.last_trigger_bar_idx.get("PAIN_ZONE_BREAKOUT", -100))
            if (bar_idx - last_trig) >= 3:
                # Bullish Rejection Breakout: Prior (or current) bar tested Pain Zone Top, live spot breaks prior bar High
                pz_up_tested = (prior_bar.low <= pz_top + 0.25 * atr and prior_bar.high >= pz_top and prior_bar.close >= pz_bot - 0.25 * atr)
                pz_up_action = (prior_bar.close <= prior_bar.open) or ((prior_bar.close - prior_bar.low) >= 0.15 * max(prior_bar.high - prior_bar.low, 0.1))
                if pz_up_tested and pz_up_action and spot >= prior_bar.high:
                    if self._check_wyckoff_harmony("UP", curr_bar):
                        self.last_trigger_bar_idx["PAIN_ZONE_REJECTION_BREAKOUT"] = bar_idx
                        lowest_low = min(prior_bar.low, curr_bar.low)
                        logger.info(f"[SHADOW_INTRABAR_SURGE] PAIN_ZONE_REJECTION_BREAKOUT CALL | Spot: {spot:.2f} >= AnchorHigh: {prior_bar.high:.2f} at T+{sec_in_min}s")
                        self._open_shadow_trade("PAIN_ZONE_REJECTION_BREAKOUT", "UP", spot, prior_bar.high, lowest_low, now_sec, n1_fp)
                        return

                # Bearish Rejection Breakdown: Prior (or current) bar tested Pain Zone Bottom, live spot breaks prior bar Low
                pz_dn_tested = (prior_bar.high >= pz_bot - 0.25 * atr and prior_bar.low <= pz_bot and prior_bar.close <= pz_top + 0.25 * atr)
                pz_dn_action = (prior_bar.close >= prior_bar.open) or ((prior_bar.high - prior_bar.close) >= 0.15 * max(prior_bar.high - prior_bar.low, 0.1))
                if pz_dn_tested and pz_dn_action and spot <= prior_bar.low:
                    if self._check_wyckoff_harmony("DOWN", curr_bar):
                        self.last_trigger_bar_idx["PAIN_ZONE_REJECTION_BREAKOUT"] = bar_idx
                        highest_high = max(prior_bar.high, curr_bar.high)
                        logger.info(f"[SHADOW_INTRABAR_SURGE] PAIN_ZONE_REJECTION_BREAKOUT PUT | Spot: {spot:.2f} <= AnchorLow: {prior_bar.low:.2f} at T+{sec_in_min}s")
                        self._open_shadow_trade("PAIN_ZONE_REJECTION_BREAKOUT", "DOWN", spot, highest_high, prior_bar.low, now_sec, n1_fp)
                        return

        # 2. 200 EMA Rejection-Breakout (LRRB)
        # Prior bar tested 200 EMA level, live candle breaks prior bar extreme (Strict 1-Bar Expiry)
        ema200 = float(getattr(self.pa_engine, "ema_200_1m", 0.0) or 0.0)
        if ema200 > 0:
            last_trig_ema = max(self.last_trigger_bar_idx.get("EMA_200_REJECTION_BREAKOUT", -100), self.last_trigger_bar_idx.get("EMA_200_BREAKOUT", -100))
            if (bar_idx - last_trig_ema) >= 3:
                # Bullish Rejection Breakout: Prior (or current) bar tested 200 EMA, live spot breaks prior bar High
                ema_up_tested = (prior_bar.low <= ema200 + 0.25 * atr and prior_bar.high >= ema200)
                ema_up_action = (prior_bar.close <= prior_bar.open) or ((prior_bar.close - prior_bar.low) >= 0.15 * max(prior_bar.high - prior_bar.low, 0.1))
                if ema_up_tested and ema_up_action and spot >= prior_bar.high:
                    if self._check_wyckoff_harmony("UP", curr_bar):
                        self.last_trigger_bar_idx["EMA_200_REJECTION_BREAKOUT"] = bar_idx
                        lowest_low = min(prior_bar.low, curr_bar.low)
                        logger.info(f"[SHADOW_INTRABAR_SURGE] EMA_200_REJECTION_BREAKOUT CALL | Spot: {spot:.2f} >= AnchorHigh: {prior_bar.high:.2f} at T+{sec_in_min}s")
                        self._open_shadow_trade("EMA_200_REJECTION_BREAKOUT", "UP", spot, prior_bar.high, lowest_low, now_sec, n1_fp)
                        return

                # Bearish Rejection Breakdown: Prior (or current) bar tested 200 EMA from below, live spot breaks prior bar Low
                ema_dn_tested = (prior_bar.high >= ema200 - 0.25 * atr and prior_bar.low <= ema200)
                ema_dn_action = (prior_bar.close >= prior_bar.open) or ((prior_bar.high - prior_bar.close) >= 0.15 * max(prior_bar.high - prior_bar.low, 0.1))
                if ema_dn_tested and ema_dn_action and spot <= prior_bar.low:
                    if self._check_wyckoff_harmony("DOWN", curr_bar):
                        self.last_trigger_bar_idx["EMA_200_REJECTION_BREAKOUT"] = bar_idx
                        highest_high = max(prior_bar.high, curr_bar.high)
                        logger.info(f"[SHADOW_INTRABAR_SURGE] EMA_200_REJECTION_BREAKOUT PUT | Spot: {spot:.2f} <= AnchorLow: {prior_bar.low:.2f} at T+{sec_in_min}s")
                        self._open_shadow_trade("EMA_200_REJECTION_BREAKOUT", "DOWN", spot, highest_high, prior_bar.low, now_sec, n1_fp)
                        return

        # 3. 20 EMA Rejection-Breakout (LRRB Shadow Observer)
        # Prior bar tested 20 EMA level, live candle breaks prior bar extreme (Strict 1-Bar Expiry)
        ema20 = float(getattr(self.pa_engine, "ema_20_1m", 0.0) or 0.0)
        if ema20 > 0:
            last_trig_ema20 = self.last_trigger_bar_idx.get("EMA_20_REJECTION_BREAKOUT", -100)
            if (bar_idx - last_trig_ema20) >= 3:
                # Bullish Rejection Breakout: Prior (or current) bar tested 20 EMA from above, live spot breaks prior bar High
                ema20_up_tested = (prior_bar.low <= ema20 + 0.25 * atr and prior_bar.high >= ema20)
                ema20_up_action = (prior_bar.close <= prior_bar.open) or ((prior_bar.close - prior_bar.low) >= 0.15 * max(prior_bar.high - prior_bar.low, 0.1))
                if ema20_up_tested and ema20_up_action and spot >= prior_bar.high:
                    if self._check_wyckoff_harmony("UP", curr_bar):
                        self.last_trigger_bar_idx["EMA_20_REJECTION_BREAKOUT"] = bar_idx
                        lowest_low = min(prior_bar.low, curr_bar.low)
                        logger.info(f"[SHADOW_INTRABAR_SURGE] EMA_20_REJECTION_BREAKOUT CALL | Spot: {spot:.2f} >= AnchorHigh: {prior_bar.high:.2f} at T+{sec_in_min}s")
                        self._open_shadow_trade("EMA_20_REJECTION_BREAKOUT", "UP", spot, prior_bar.high, lowest_low, now_sec, n1_fp)
                        return

                # Bearish Rejection Breakdown: Prior (or current) bar tested 20 EMA from below, live spot breaks prior bar Low
                ema20_dn_tested = (prior_bar.high >= ema20 - 0.25 * atr and prior_bar.low <= ema20)
                ema20_dn_action = (prior_bar.close >= prior_bar.open) or ((prior_bar.high - prior_bar.close) >= 0.15 * max(prior_bar.high - prior_bar.low, 0.1))
                if ema20_dn_tested and ema20_dn_action and spot <= prior_bar.low:
                    if self._check_wyckoff_harmony("DOWN", curr_bar):
                        self.last_trigger_bar_idx["EMA_20_REJECTION_BREAKOUT"] = bar_idx
                        highest_high = max(prior_bar.high, curr_bar.high)
                        logger.info(f"[SHADOW_INTRABAR_SURGE] EMA_20_REJECTION_BREAKOUT PUT | Spot: {spot:.2f} <= AnchorLow: {prior_bar.low:.2f} at T+{sec_in_min}s")
                        self._open_shadow_trade("EMA_20_REJECTION_BREAKOUT", "DOWN", spot, highest_high, prior_bar.low, now_sec, n1_fp)
                        return

        # 4. Pain Zone Direct Breakout / Breakdown (Mode B In-Flight Surge)
        # S/R Breakout parity: Triggers instantly when spot breaks highest line (zone_top) for CALL
        # or lowest line (zone_bottom) for PUT from T:02s onwards with zero debounce
        if pz_top > 0 and pz_bot > 0:
            last_trig_pz_bo = max(self.last_trigger_bar_idx.get("PAIN_ZONE_BREAKOUT", -100), self.last_trigger_bar_idx.get("PAIN_ZONE_REJECTION_BREAKOUT", -100))
            if (bar_idx - last_trig_pz_bo) >= 3:
                # Bullish Breakout: Candle opened inside/below Pain Zone, spot breaks above highest line (zone_top)
                if (curr_bar.open <= pz_top or prior_bar.close <= pz_top) and spot > pz_top:
                    if self._check_dow_thrust_progression("UP", curr_bar, spot, atr):
                        if self._check_wyckoff_harmony("UP", curr_bar):
                            self.last_trigger_bar_idx["PAIN_ZONE_BREAKOUT"] = bar_idx
                            lowest_low = min(prior_bar.low, curr_bar.low)
                            highest_high = max(spot, curr_bar.high)
                            logger.info(f"[SHADOW_INTRABAR_SURGE] PAIN_ZONE_BREAKOUT CALL | Spot: {spot:.2f} > ZoneTop: {pz_top:.2f} at T+{sec_in_min}s | Dow Thrust OK")
                            self._open_shadow_trade("PAIN_ZONE_BREAKOUT", "UP", spot, highest_high, lowest_low, now_sec, n1_fp)
                            return

                # Bearish Breakdown: Candle opened inside/above Pain Zone, spot breaks below lowest line (zone_bottom)
                if (curr_bar.open >= pz_bot or prior_bar.close >= pz_bot) and spot < pz_bot:
                    if self._check_dow_thrust_progression("DOWN", curr_bar, spot, atr):
                        if self._check_wyckoff_harmony("DOWN", curr_bar):
                            self.last_trigger_bar_idx["PAIN_ZONE_BREAKOUT"] = bar_idx
                            highest_high = max(prior_bar.high, curr_bar.high)
                            lowest_low = min(spot, curr_bar.low)
                            logger.info(f"[SHADOW_INTRABAR_SURGE] PAIN_ZONE_BREAKOUT PUT | Spot: {spot:.2f} < ZoneBottom: {pz_bot:.2f} at T+{sec_in_min}s | Dow Thrust OK")
                            self._open_shadow_trade("PAIN_ZONE_BREAKOUT", "DOWN", spot, highest_high, lowest_low, now_sec, n1_fp)
                            return

        # 5. 200 EMA Direct Breakout / Breakdown (Mode B In-Flight Surge)
        # S/R Breakout parity: Triggers instantly when spot crosses 200 EMA to the upside (CALL)
        # or breaks below 200 EMA low / ribbon lower boundary (PUT)
        if ema200 > 0:
            last_trig_ema_bo = max(self.last_trigger_bar_idx.get("EMA_200_BREAKOUT", -100), self.last_trigger_bar_idx.get("EMA_200_REJECTION_BREAKOUT", -100))
            if (bar_idx - last_trig_ema_bo) >= 3:
                _, ema_bot = self._get_ema200_levels(atr)
                ema_breakdown_level = ema_bot if ema_bot > 0 else ema200

                # Bullish Breakout: Candle opened at/below 200 EMA, spot crosses above 200 EMA
                if (curr_bar.open <= ema200 or prior_bar.close <= ema200) and spot > ema200:
                    if self._check_dow_thrust_progression("UP", curr_bar, spot, atr):
                        if self._check_wyckoff_harmony("UP", curr_bar):
                            self.last_trigger_bar_idx["EMA_200_BREAKOUT"] = bar_idx
                            lowest_low = min(prior_bar.low, curr_bar.low)
                            highest_high = max(spot, curr_bar.high)
                            logger.info(f"[SHADOW_INTRABAR_SURGE] EMA_200_BREAKOUT CALL | Spot: {spot:.2f} > EMA200: {ema200:.2f} at T+{sec_in_min}s | Dow Thrust OK")
                            self._open_shadow_trade("EMA_200_BREAKOUT", "UP", spot, highest_high, lowest_low, now_sec, n1_fp)
                            return

                # Bearish Breakdown: Candle opened at/above 200 EMA low, spot breaks below 200 EMA low
                if (curr_bar.open >= ema_breakdown_level or prior_bar.close >= ema_breakdown_level) and spot < ema_breakdown_level:
                    if self._check_dow_thrust_progression("DOWN", curr_bar, spot, atr):
                        if self._check_wyckoff_harmony("DOWN", curr_bar):
                            self.last_trigger_bar_idx["EMA_200_BREAKOUT"] = bar_idx
                            highest_high = max(prior_bar.high, curr_bar.high)
                            lowest_low = min(spot, curr_bar.low)
                            logger.info(f"[SHADOW_INTRABAR_SURGE] EMA_200_BREAKOUT PUT | Spot: {spot:.2f} < EMA200Low: {ema_breakdown_level:.2f} at T+{sec_in_min}s | Dow Thrust OK")
                            self._open_shadow_trade("EMA_200_BREAKOUT", "DOWN", spot, highest_high, lowest_low, now_sec, n1_fp)
                            return

    def _evaluate_boundary_arming(self, spot: float, now_sec: float) -> None:
        """Evaluates boundary rejection on latest closed 1m candle and arms Mode A."""
        if self.pa_engine is None or len(self.pa_engine.bars_1m) < 20:
            return
            
        bars = self.pa_engine.bars_1m
        bar_idx = len(bars) - 1
        bar = bars[-1]
        
        atr = getattr(self.pa_engine, "current_atr_1m", 10.0) or 10.0
        if atr <= 0:
            atr = 10.0
            
        # Candle Geometry
        c_range = bar.high - bar.low
        if c_range <= 0 or (c_range / atr) > 1.25:  # Filter out exhaustion bars > 1.25 ATR
            return
            
        lower_wick = (min(bar.open, bar.close) - bar.low)
        upper_wick = (bar.high - max(bar.open, bar.close))
        lower_wick_ratio = lower_wick / c_range
        upper_wick_ratio = upper_wick / c_range
        
        boundaries_to_check = [
            ("PAIN_ZONE_BAND", self._get_pain_zone_levels(atr)),
            ("EMA_200_BAND", self._get_ema200_levels(atr)),
        ]
        
        for b_name, (b_top, b_bottom) in boundaries_to_check:
            if b_top <= 0 or b_bottom <= 0:
                continue
                
            # Prevent multi-triggering within 3 bars
            last_idx = self.last_trigger_bar_idx.get(b_name, -100)
            if (bar_idx - last_idx) < 3:
                continue
                
            # CALL Bounce Check (Support Rejection)
            call_touch = (bar.low <= b_top + 0.35 * atr) and (bar.close >= b_bottom - 0.25 * atr)
            call_rej = (lower_wick_ratio >= 0.20) or (bar.close > bar.open)
            
            if call_touch and call_rej:
                # Wyckoff Delta Harmony
                if self._check_wyckoff_harmony("UP", bar):
                    self.armed_setups[b_name] = {
                        "direction": "UP",
                        "anchor_high": bar.high,
                        "anchor_low": bar.low,
                        "armed_bar_idx": bar_idx,
                        "ts": now_sec,
                    }
                    self.last_trigger_bar_idx[b_name] = bar_idx
                    logger.info(f"[SHADOW_ARMED] {b_name} CALL Rejection Armed | AnchorHigh: {bar.high:.2f} | Await in-flight pierce")
                    continue
                    
            # PUT Rejection Check (Resistance Rejection)
            put_touch = (bar.high >= b_bottom - 0.35 * atr) and (bar.close <= b_top + 0.25 * atr)
            put_rej = (upper_wick_ratio >= 0.20) or (bar.close < bar.open)
            
            if put_touch and put_rej:
                if self._check_wyckoff_harmony("DOWN", bar):
                    self.armed_setups[b_name] = {
                        "direction": "DOWN",
                        "anchor_high": bar.high,
                        "anchor_low": bar.low,
                        "armed_bar_idx": bar_idx,
                        "ts": now_sec,
                    }
                    self.last_trigger_bar_idx[b_name] = bar_idx
                    logger.info(f"[SHADOW_ARMED] {b_name} PUT Rejection Armed | AnchorLow: {bar.low:.2f} | Await in-flight pierce")

    def _compute_n1_footprint(self, prior_bar: Any, atr: float) -> Dict[str, Any]:
        """
        Computes Wyckoff Volume & Order Flow Footprint on testing candle N-1:
          1. Volume Dry-Up (Supply/Demand exhaustion)
          2. Synthetic Net Delta: (Call_Buy + Put_Sell) - (Put_Buy + Call_Sell)
          3. Stopping Volume (Effort vs Result: high volume into support with small range)
        """
        bars = getattr(self.pa_engine, "bars_1m", [])
        n_bars = len(bars)
        start_idx = max(0, n_bars - 21)
        # Exclude prior_bar (which is bars[-1]) from baseline
        sample_vols = [float(getattr(bars[i], "volume", 0.0) or 0.0) for i in range(start_idx, max(0, n_bars - 1))]
        pos_vols = [v for v in sample_vols if v > 0]
        base_vol = (sum(pos_vols) / len(pos_vols)) if pos_vols else 1.0

        n1_vol = float(getattr(prior_bar, "volume", 0.0) or 0.0)
        cb = float(getattr(prior_bar, "call_buy_vol", 0.0) or 0.0)
        cs = float(getattr(prior_bar, "call_sell_vol", 0.0) or 0.0)
        pb = float(getattr(prior_bar, "put_buy_vol", 0.0) or 0.0)
        ps = float(getattr(prior_bar, "put_sell_vol", 0.0) or 0.0)

        # 1. Volume Dry-Up: volume is lower than 80% of baseline (proves sellers/buyers exhausted)
        vol_dryup = (n1_vol < 0.80 * base_vol) if (base_vol > 0 and n1_vol > 0) else False

        # 2. Synthetic Net Delta: Bulls (CB + PS) vs Bears (PB + CS)
        bulls = cb + ps
        bears = pb + cs
        net_delta = bulls - bears

        # 3. Stopping Volume: Volume expanded >= 1.25x baseline, but candle range stayed <= 0.80x ATR
        c_range = float(getattr(prior_bar, "high", 0.0) - getattr(prior_bar, "low", 0.0))
        stopping_vol = (n1_vol >= 1.25 * base_vol and c_range <= 0.80 * atr) if base_vol > 0 else False

        return {
            "n1_vol": round(n1_vol, 1),
            "n1_baseline_vol": round(base_vol, 1),
            "n1_vol_dryup": vol_dryup,
            "n1_net_delta": round(net_delta, 1),
            "n1_bulls": round(bulls, 1),
            "n1_bears": round(bears, 1),
            "n1_stopping_vol": stopping_vol,
        }

    def _get_pain_zone_levels(self, atr: float) -> Tuple[float, float]:
        """Fetches live Pain Zone levels directly from PriceActionEngine single source."""
        try:
            pz = self.pa_engine.get_pain_zone_vwaps()
            return float(pz.get("zone_top", 0.0) or 0.0), float(pz.get("zone_bottom", 0.0) or 0.0)
        except Exception:
            return 0.0, 0.0

    def _get_ema200_levels(self, atr: float) -> Tuple[float, float]:
        """Fetches live 200 EMA ribbon levels from PriceActionEngine."""
        try:
            ema200 = float(getattr(self.pa_engine, "ema_200_1m", 0.0) or 0.0)
            if ema200 > 0:
                return (ema200 + 0.25 * atr), (ema200 - 0.25 * atr)
        except Exception:
            pass
        return 0.0, 0.0

    def _check_dow_thrust_progression(self, direction: str, curr_bar: Any, spot: float, atr: float) -> bool:
        """
        Dow Theory 3-Candle Directional Thrust Validator for Boundary Breakouts:
        Filters out false/whipsaw breakouts when price coils / ranges around VWAP (Pain Zone) or 200 EMA.

        Requires a clean 3-candle ascending (CALL) or descending (PUT) ladder:
          - CALL (HH/HL Thrust from below):
              1. Bar N-1 High > Bar N-2 High
              2. Bar N-1 Low > Bar N-2 Low
              3. Forming Bar N Low >= (Bar N-1 Low - 0.05 * atr)
              4. Live Spot > Bar N-1 High (or Forming Bar N High > Bar N-1 High)
          - PUT (LH/LL Thrust from above):
              1. Bar N-1 Low < Bar N-2 Low
              2. Bar N-1 High < Bar N-2 High
              3. Forming Bar N High <= (Bar N-1 High + 0.05 * atr)
              4. Live Spot < Bar N-1 Low (or Forming Bar N Low < Bar N-1 Low)
        """
        bars = getattr(self.pa_engine, "bars_1m", [])
        if len(bars) < 2:
            return False

        bar_n1 = bars[-1]
        bar_n2 = bars[-2]

        h_n1, l_n1 = float(getattr(bar_n1, "high", 0.0)), float(getattr(bar_n1, "low", 0.0))
        h_n2, l_n2 = float(getattr(bar_n2, "high", 0.0)), float(getattr(bar_n2, "low", 0.0))
        curr_h = float(getattr(curr_bar, "high", 0.0))
        curr_l = float(getattr(curr_bar, "low", 0.0))

        tol = 0.05 * max(atr, 1.0)

        if direction.upper() in ("UP", "CALL"):
            # 1. Bar N-1 made Higher High & Higher Low relative to Bar N-2
            n1_ascending = (h_n1 > h_n2) and (l_n1 > l_n2)
            # 2. Forming Bar N Low holds above Bar N-1 Low (within micro-wick tolerance)
            curr_hl = curr_l >= (l_n1 - tol)
            # 3. Forming Bar / Live Spot breaks above Bar N-1 High
            curr_hh = (spot > h_n1) or (curr_h > h_n1)
            return bool(n1_ascending and curr_hl and curr_hh)
        else:
            # 1. Bar N-1 made Lower Low & Lower High relative to Bar N-2
            n1_descending = (l_n1 < l_n2) and (h_n1 < h_n2)
            # 2. Forming Bar N High holds below Bar N-1 High (within micro-wick tolerance)
            curr_lh = curr_h <= (h_n1 + tol)
            # 3. Forming Bar / Live Spot breaks below Bar N-1 Low
            curr_ll = (spot < l_n1) or (curr_l < l_n1)
            return bool(n1_descending and curr_lh and curr_ll)

    def _check_wyckoff_harmony(self, direction: str, bar: Any) -> bool:
        """Verifies institutional order flow delta harmony (no opposing liquidation dump)."""
        opt_flow = getattr(self.pa_engine, "_opt_flow_1m", {}) if self.pa_engine else {}
        if not isinstance(opt_flow, dict):
            opt_flow = {}
        cb = float(opt_flow.get("call_buy_vol", 0.0) or getattr(bar, "call_buy_vol", 0.0) or 0.0)
        pb = float(opt_flow.get("put_buy_vol", 0.0) or getattr(bar, "put_buy_vol", 0.0) or 0.0)
        
        # If option flow volume is active
        if cb > 1000.0 or pb > 1000.0:
            if direction == "UP" and pb > 2.5 * max(cb, 1.0):
                return False  # Heavy put buying into support (dumping)
            elif direction == "DOWN" and cb > 2.5 * max(pb, 1.0):
                return False  # Heavy call buying into resistance (short squeeze)
        return True

    def _open_shadow_trade(
        self,
        boundary: str,
        direction: str,
        spot: float,
        anchor_high: float,
        anchor_low: float,
        now_sec: float,
        n1_fp: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Creates a new OTM 5 paper trade with sub-minute telemetry and N-1 Wyckoff footprint."""
        opt_sym, opt_strike, opt_side = self.select_otm5_symbol(direction, spot)
        opt_lp = self.option_cache.get(opt_sym, 0.0)
        if opt_lp <= 0.0:
            return
        
        time_str = time.strftime("%H:%M:%S", time.localtime(now_sec))
        trade_id = f"SHADOW_{boundary}_{direction}_{int(now_sec)}"
        
        atr = getattr(self.pa_engine, "current_atr_1m", 10.0) or 10.0
        invalidation_cushion = max(2.0, (spot / 24000.0) * 2.0, 0.10 * atr)
        spot_sl = (anchor_low - invalidation_cushion) if direction == "UP" else (anchor_high + invalidation_cushion)
        tp_price = opt_lp * 1.04 if opt_lp > 0 else 0.0
        sl_price = opt_lp * 0.88 if opt_lp > 0 else 0.0

        pz_dict = self.pa_engine.get_pain_zone_vwaps() if hasattr(self.pa_engine, "get_pain_zone_vwaps") else {}
        pz_top_val = float(pz_dict.get("zone_top", 0.0) or 0.0)
        pz_bot_val = float(pz_dict.get("zone_bottom", 0.0) or 0.0)
        pz_spread = round(pz_top_val - pz_bot_val, 2) if (pz_top_val > 0 and pz_bot_val > 0) else 0.0

        n1 = n1_fp or {}
        trade = ShadowTrade(
            trade_id=trade_id,
            boundary=boundary,
            direction=direction,
            entry_ts=now_sec,
            entry_time_str=time_str,
            spot_entry=spot,
            spot_anchor_high=anchor_high,
            spot_anchor_low=anchor_low,
            spot_sl_price=spot_sl,
            opt_symbol=opt_sym,
            opt_strike=opt_strike,
            opt_side=opt_side,
            opt_entry=opt_lp,
            opt_tp_price=tp_price,
            opt_sl_price=sl_price,
            n1_vol=float(n1.get("n1_vol", 0.0)),
            n1_baseline_vol=float(n1.get("n1_baseline_vol", 0.0)),
            n1_vol_dryup=bool(n1.get("n1_vol_dryup", False)),
            n1_net_delta=float(n1.get("n1_net_delta", 0.0)),
            n1_stopping_vol=bool(n1.get("n1_stopping_vol", False)),
            pz_top=pz_top_val,
            pz_bottom=pz_bot_val,
            pz_spread_pts=pz_spread,
            pz_call_buy=float(pz_dict.get("vwap_call_buy", 0.0) or 0.0),
            pz_call_sell=float(pz_dict.get("vwap_call_sell", 0.0) or 0.0),
            pz_put_buy=float(pz_dict.get("vwap_put_buy", 0.0) or 0.0),
            pz_put_sell=float(pz_dict.get("vwap_put_sell", 0.0) or 0.0),
        )
        
        self.active_trades.append(trade)
        logger.info(
            f"⚡ [SHADOW_ENTRY] {boundary} {direction} Triggered @ {time_str} | "
            f"Spot: {spot:.2f} (SL: {spot_sl:.2f}) | "
            f"OTM5: {opt_sym} @ ₹{opt_lp:.2f} (TP +4%: ₹{tp_price:.2f}, SL -12%: ₹{sl_price:.2f}) | "
            f"N-1 Footprint: Vol={trade.n1_vol:.0f} (Base={trade.n1_baseline_vol:.0f}, Dry={trade.n1_vol_dryup}) "
            f"NetDelta={trade.n1_net_delta:+.0f} StoppingVol={trade.n1_stopping_vol}"
        )

    def _close_trade(self, trade: ShadowTrade, now_sec: float, exit_reason: str, outcome: str) -> None:
        """Finalizes shadow trade and appends telemetry record to JSONL."""
        trade.is_active = False
        trade.exit_ts = now_sec
        trade.exit_reason = exit_reason
        trade.outcome = outcome
        trade.duration_sec = round(now_sec - trade.entry_ts, 1)
        
        # Log outcome
        log_badge = "🟢 WINNER" if outcome == "WINNER" else ("🔴 LOSER" if outcome == "LOSER" else "⚪ EXPIRED")
        logger.info(
            f"{log_badge} [SHADOW_OUTCOME] {trade.boundary} {trade.direction} | "
            f"Exit: {exit_reason} in {trade.duration_sec}s | "
            f"Option: Peak MFE +{trade.opt_peak_mfe_pct:.1f}%, MAE {trade.opt_peak_mae_pct:.1f}% | "
            f"Spot: Peak MFE +{trade.spot_peak_mfe:.1f}pts, MAE {trade.spot_peak_mae:.1f}pts | "
            f"30s Move: Opt {trade.opt_mfe_30s_pct:+.1f}%, Spot {trade.spot_mfe_30s:+.1f}pts | "
            f"60s Move: Opt {trade.opt_mfe_60s_pct:+.1f}%, Spot {trade.spot_mfe_60s:+.1f}pts | "
            f"N-1 Footprint: NetDelta={trade.n1_net_delta:+.0f}, Dry={trade.n1_vol_dryup}, StopVol={trade.n1_stopping_vol}"
        )
        
        # Save to disk
        self._persist_trade(trade)
        if trade in self.active_trades:
            self.active_trades.remove(trade)

    def _persist_trade(self, trade: ShadowTrade) -> None:
        """Appends trade telemetry to daily JSONL file."""
        try:
            date_str = time.strftime("%Y-%m-%d", time.localtime(trade.entry_ts))
            file_path = os.path.join(self.log_dir, f"aegis_boundary_shadow_trades_{date_str}.jsonl")
            with open(file_path, "a") as f:
                f.write(json.dumps(asdict(trade)) + "\n")
        except Exception as e:
            logger.debug(f"[ShadowBoundaryObserver] Error persisting trade: {e}")
