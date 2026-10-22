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
        price_action_engine: Any,
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
        
        # References populated by AegisGammaEngine
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
        """Fires Mode A In-Flight Entry when live tick pierces anchor price."""
        if not self.armed_setups:
            return
            
        for b_name in list(self.armed_setups.keys()):
            armed = self.armed_setups[b_name]
            direction = armed["direction"]
            anchor_h = armed["anchor_high"]
            anchor_l = armed["anchor_low"]
            
            triggered = False
            if direction == "UP" and spot > anchor_h:
                triggered = True
            elif direction == "DOWN" and spot < anchor_l:
                triggered = True
                
            if triggered:
                # Fire Shadow Trade!
                del self.armed_setups[b_name]
                self._open_shadow_trade(b_name, direction, spot, anchor_h, anchor_l, now_sec)

    def _check_inflight_breakthrough(self, spot: float, now_sec: float) -> None:
        """
        Mode B: Live In-Flight Intra-Bar Breakthrough Surge.
        Triggers when an active forming 1-minute candle breaks through the Pain Zone or 200 EMA
        with strong directional expansion mid-bar.
        """
        if self.pa_engine is None:
            return
        curr_bar = getattr(self.pa_engine, "_curr_1m_bar", None)
        if curr_bar is None or curr_bar.tick_count < 5:
            return

        sec_in_min = int(now_sec % 60)
        # Active intra-bar expansion window (between 8s and 52s)
        if not (8 <= sec_in_min <= 52):
            return

        atr = getattr(self.pa_engine, "current_atr_1m", 10.0) or 10.0
        if atr <= 0:
            atr = 10.0

        bar_idx = len(self.pa_engine.bars_1m)

        # 1. Pain Zone Breakthrough
        pz_top, pz_bot = self._get_pain_zone_levels(atr)
        if pz_top > 0 and pz_bot > 0:
            last_trig = self.last_trigger_bar_idx.get("PAIN_ZONE_BREAKTHROUGH", -100)
            if (bar_idx - last_trig) >= 3:
                # Bullish Breakthrough: Open was <= pz_top, live spot surges above pz_top + 0.15 * atr
                if curr_bar.open <= pz_top and spot >= (pz_top + 0.15 * atr):
                    if self._check_wyckoff_harmony("UP", curr_bar):
                        self.last_trigger_bar_idx["PAIN_ZONE_BREAKTHROUGH"] = bar_idx
                        logger.info(f"[SHADOW_INTRABAR_SURGE] PAIN_ZONE_BREAKTHROUGH CALL | Spot: {spot:.2f} > Top: {pz_top:.2f} at T+{sec_in_min}s")
                        self._open_shadow_trade("PAIN_ZONE_BREAKTHROUGH", "UP", spot, curr_bar.high, curr_bar.low, now_sec)

                # Bearish Breakthrough: Open was >= pz_bot, live spot drops below pz_bot - 0.15 * atr
                elif curr_bar.open >= pz_bot and spot <= (pz_bot - 0.15 * atr):
                    if self._check_wyckoff_harmony("DOWN", curr_bar):
                        self.last_trigger_bar_idx["PAIN_ZONE_BREAKTHROUGH"] = bar_idx
                        logger.info(f"[SHADOW_INTRABAR_SURGE] PAIN_ZONE_BREAKTHROUGH PUT | Spot: {spot:.2f} < Bot: {pz_bot:.2f} at T+{sec_in_min}s")
                        self._open_shadow_trade("PAIN_ZONE_BREAKTHROUGH", "DOWN", spot, curr_bar.high, curr_bar.low, now_sec)

        # 2. 200 EMA Breakthrough
        ema200 = float(getattr(self.pa_engine, "ema_200_1m", 0.0) or 0.0)
        if ema200 > 0:
            last_trig_ema = self.last_trigger_bar_idx.get("EMA_200_BREAKTHROUGH", -100)
            if (bar_idx - last_trig_ema) >= 3:
                # Bullish cross above 200 EMA
                if curr_bar.open <= ema200 and spot >= (ema200 + 0.15 * atr):
                    if self._check_wyckoff_harmony("UP", curr_bar):
                        self.last_trigger_bar_idx["EMA_200_BREAKTHROUGH"] = bar_idx
                        logger.info(f"[SHADOW_INTRABAR_SURGE] EMA_200_BREAKTHROUGH CALL | Spot: {spot:.2f} > EMA200: {ema200:.2f} at T+{sec_in_min}s")
                        self._open_shadow_trade("EMA_200_BREAKTHROUGH", "UP", spot, curr_bar.high, curr_bar.low, now_sec)

                # Bearish cross below 200 EMA
                elif curr_bar.open >= ema200 and spot <= (ema200 - 0.15 * atr):
                    if self._check_wyckoff_harmony("DOWN", curr_bar):
                        self.last_trigger_bar_idx["EMA_200_BREAKTHROUGH"] = bar_idx
                        logger.info(f"[SHADOW_INTRABAR_SURGE] EMA_200_BREAKTHROUGH PUT | Spot: {spot:.2f} < EMA200: {ema200:.2f} at T+{sec_in_min}s")
                        self._open_shadow_trade("EMA_200_BREAKTHROUGH", "DOWN", spot, curr_bar.high, curr_bar.low, now_sec)

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

    def _check_wyckoff_harmony(self, direction: str, bar: Any) -> bool:
        """Verifies institutional order flow delta harmony (no opposing liquidation dump)."""
        cb = float(getattr(bar, "call_buy_vol", 0.0) or 0.0)
        pb = float(getattr(bar, "put_buy_vol", 0.0) or 0.0)
        
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
        now_sec: float
    ) -> None:
        """Creates a new OTM 5 paper trade with sub-minute telemetry."""
        opt_sym, opt_strike, opt_side = self.select_otm5_symbol(direction, spot)
        opt_lp = self.option_cache.get(opt_sym, 0.0)
        
        time_str = time.strftime("%H:%M:%S", time.localtime(now_sec))
        trade_id = f"SHADOW_{boundary}_{direction}_{int(now_sec)}"
        
        spot_sl = (anchor_low - 2.0) if direction == "UP" else (anchor_high + 2.0)
        tp_price = opt_lp * 1.04 if opt_lp > 0 else 0.0
        sl_price = opt_lp * 0.88 if opt_lp > 0 else 0.0
        
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
        )
        
        self.active_trades.append(trade)
        logger.info(
            f"⚡ [SHADOW_ENTRY] {boundary} {direction} Triggered @ {time_str} | "
            f"Spot: {spot:.2f} (SL: {spot_sl:.2f}) | "
            f"OTM5: {opt_sym} @ ₹{opt_lp:.2f} (TP +4%: ₹{tp_price:.2f}, SL -12%: ₹{sl_price:.2f})"
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
            f"60s Move: Opt {trade.opt_mfe_60s_pct:+.1f}%, Spot {trade.spot_mfe_60s:+.1f}pts"
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
