"""Microstructure Cascade — Unified Live Execution Engine (v3.0)
Integrates: Fixed Microstructure Core + MPAR + LRA + DKLA + Unified Conviction Trigger
Optimized for Indian Equities/Options HFT — Zero-Allocation Hot Path
"""
from __future__ import annotations
import os
import sys

# ─── BOOTSTRAP ───
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import json, time as pytime, math
from collections import deque
from datetime import datetime
import queue
import numpy as np
from typing import Dict, Deque, List, Tuple, Any, TYPE_CHECKING
from common.config import get_data_dir
from common.logger import get_logger

if TYPE_CHECKING:
    from stock_order_flow.tick_compiler import ParsedTick

logger = get_logger("cascade_engine")

# ──────────────────────────────────────────────────────────────
# ZERO-ALLOCATION HOT PATH HELPERS
# ──────────────────────────────────────────────────────────────

def _walk_book_impact_fast(prices: List[float], qtys: List[float], target_notional: float, mid: float) -> float:
    """O(1) Manual Unrolled Book Walk for 5 Levels. Returns impact in bps.
    Accumulates SHARES filled (not notional*price), then avg_price = target_notional/shares.
    (Old version accumulated cost = notional*price, which is a notional-weighted price, not a
    share-weighted execution price -- close for tight books, systematically biased for wide ones.)"""
    rem = target_notional
    shares = 0.0
    # L1
    lv = prices[0] * qtys[0]
    if lv >= rem: shares += rem / max(prices[0], 1e-9); rem = 0.0
    else: shares += qtys[0]; rem -= lv
    # L2
    lv = prices[1] * qtys[1]
    if lv >= rem: shares += rem / max(prices[1], 1e-9); rem = 0.0
    else: shares += qtys[1]; rem -= lv
    # L3
    lv = prices[2] * qtys[2]
    if lv >= rem: shares += rem / max(prices[2], 1e-9); rem = 0.0
    else: shares += qtys[2]; rem -= lv
    # L4
    lv = prices[3] * qtys[3]
    if lv >= rem: shares += rem / max(prices[3], 1e-9); rem = 0.0
    else: shares += qtys[3]; rem -= lv
    # L5
    lv = prices[4] * qtys[4]
    if lv >= rem: shares += rem / max(prices[4], 1e-9); rem = 0.0
    else: return 9999.0
    if rem > 1e-6 or shares <= 0: return 9999.0
    return ((target_notional / shares) - mid) / mid * 10_000.0

def _ema_update(ema: float, val: float, alpha: float) -> float:
    return ema + alpha * (val - ema)

# ──────────────────────────────────────────────────────────────
# MAIN ENGINE CLASS
# ──────────────────────────────────────────────────────────────

class CascadeValueEngine:
    def __init__(
        self, 
        token_to_symbol: dict, 
        bucket_map: dict = None, 
        vacuum_map: dict = None,
        log_path: str = None,
        dkla_notional: float = 1_00_00_000.0,
        # Trigger Tuning Parameters (Exposed for Config)
        trigger_config: dict = None
    ):
        self.t2s = {str(k): str(v) for k, v in token_to_symbol.items()}
        self.bucket_map = bucket_map or {}
        self.vacuum_map = vacuum_map or {}
        self.dkla_notional = dkla_notional
        
        self.signal_queue = queue.Queue(maxsize=1000)
        
        # ─── Bucket Accumulators ───
        self.cur_buy_val: Dict[str, float] = {}
        self.cur_sell_val: Dict[str, float] = {}
        self.cur_tot_val: Dict[str, float] = {}
        self.cur_tick_count: Dict[str, int] = {}
        self.cur_max_tick_val: Dict[str, float] = {}
        self.cur_net_vacuum: Dict[str, float] = {}
        
        # ─── State ───
        self.last_snap: Dict[str, Dict] = {}
        self.last_depth_state: Dict[str, Dict] = {}
        self.trade_times: Dict[str, Deque[float]] = {}
        self.completion_times: Dict[str, Deque[float]] = {}
        
        # ─── Rolling Windows ───
        self.vpin_q: Dict[str, Deque[float]] = {}
        self.whale_history: Dict[str, Deque[float]] = {}
        self.vacuum_history: Dict[str, Deque[float]] = {}
        self.mlofi_history: Dict[str, Deque[float]] = {}
        self.toxicity_history: Dict[str, Deque[float]] = {}
        
        # ─── MPAR State ───
        self.mp_prev: Dict[str, float] = {}
        self.mp_vel: Dict[str, float] = {}
        self.mp_accel: Dict[str, float] = {}
        
        # ─── LRA State ───
        self.lra_ema: Dict[str, Dict[str, List[float]]] = {}
        self.lra_asym_prev: Dict[str, float] = {}
        
        # ─── DKLA State ───
        self.dkla_asym_prev: Dict[str, float] = {}
        
        # ─── Config ───
        self.CFG = {
            "bucket_turnover": 2_00_00_000.0,
            "vacuum_threshold": 2_500_000.0,
            "glitch_guard_cr": 50.0,
            "min_ticks_per_bucket": 3,
            "whale_window": 50,
            "vacuum_window": 100,
            "mlofi_window": 100,
            "toxicity_window": 200,
            "hawkes_lookback_sec": 600,
            "hawkes_bin_sec": 10,
            "mp_alpha": 0.3,
            "mp_accel_alpha": 0.2,
            "lra_alpha": 0.05,
            "dkla_notional": dkla_notional,
        }
        self._hawkes_bins = int(self.CFG["hawkes_lookback_sec"] / self.CFG["hawkes_bin_sec"])
        
        # ─── TRIGGER CONFIG (The "Holy Grail" Thresholds) ───
        default_trigger = {
            # Core Gates (Must Pass All)
            "min_ticks": 3,
            "min_branching": 0.35,      # Self-exciting flow
            "min_whale_z": 1.2,         # Statistical whale presence
            "min_tox_pct": 75,          # Toxicity percentile
            
            # Long Conviction Weights (Sum > THRESHOLD triggers)
            "w_mlofi": 1.5,             # Book Pressure vs Flow
            "w_mpar": 2.0,              # Microprice Acceleration (Highest Alpha)
            "w_vac": 1.5,               # Liquidity Vacuum
            "w_lra": 1.5,               # MM Retreat
            "w_dkla": 1.5,              # Impact Asymmetry
            "w_micro": 0.5,             # Microprice Stretch
            "conviction_threshold": 7.5, # Out of ~10.5 max
            
            # Dynamic Normalization Clamps
            "mlofi_cap": 3.0,
            "mpar_cap": 3.0,
            "vac_cap": 100.0,           # bps
            "lra_cap": 1.0,
            "dkla_cap": 1.0,
        }
        self.CFG.update(default_trigger)   # apply defaults FIRST
        if trigger_config: self.CFG.update(trigger_config)  # then apply any explicit overrides
        self.TRIG = {k: v for k, v in self.CFG.items() if k in default_trigger}
        
        # Logging
        self.LOG_FILE = log_path or os.path.join(
            get_data_dir(), "logs", f"cascade_live_{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        )
        os.makedirs(os.path.dirname(self.LOG_FILE), exist_ok=True)

    # ═══════════════════════════════════════════════════════════
    # CORE MICROSTRUCTURE CALCULATIONS (FIXED & OPTIMIZED)
    # ═══════════════════════════════════════════════════════════

    def _calculate_vacuum_score(self, sym: str, curr_bids: Dict, curr_asks: Dict, 
                                trade_qty: float, trade_price: float, 
                                touch_bid: float, touch_ask: float) -> float:
        vacuum_raw = 0.0
        last_state = self.last_depth_state.get(sym, {'bids': {}, 'asks': {}})
        for price, old_qty in last_state['bids'].items():
            if price in curr_bids:
                dropped = old_qty - curr_bids[price]
                if dropped > 0:
                    if abs(price - trade_price) < 1e-6: dropped = max(0, dropped - trade_qty)
                    if dropped > 0: vacuum_raw -= dropped * price
        for price, old_qty in last_state['asks'].items():
            if price in curr_asks:
                dropped = old_qty - curr_asks[price]
                if dropped > 0:
                    if abs(price - trade_price) < 1e-6: dropped = max(0, dropped - trade_qty)
                    if dropped > 0: vacuum_raw += dropped * price
        self.last_depth_state[sym] = {'bids': curr_bids, 'asks': curr_asks}
        total_depth = sum(p*q for p,q in curr_bids.items()) + sum(p*q for p,q in curr_asks.items())
        hist = self.vacuum_history.setdefault(sym, deque(maxlen=self.CFG["vacuum_window"]))
        hist.append(total_depth)
        return (vacuum_raw / max(np.mean(hist) if hist else 1.0, 1.0)) * 10_000.0

    def _calculate_mlofi(self, sym: str, snap: Dict, flow_imb: float) -> float:
        b_val = snap.get("bp1",0)*snap.get("bq1",0); s_val = snap.get("sp1",0)*snap.get("sq1",0)
        book_num = (b_val - s_val) * 1.0; book_den = (b_val + s_val) * 1.0
        b_val = snap.get("bp2",0)*snap.get("bq2",0); s_val = snap.get("sp2",0)*snap.get("sq2",0)
        book_num += (b_val - s_val) * 0.7408; book_den += (b_val + s_val) * 0.7408
        b_val = snap.get("bp3",0)*snap.get("bq3",0); s_val = snap.get("sp3",0)*snap.get("sq3",0)
        book_num += (b_val - s_val) * 0.5488; book_den += (b_val + s_val) * 0.5488
        b_val = snap.get("bp4",0)*snap.get("bq4",0); s_val = snap.get("sp4",0)*snap.get("sq4",0)
        book_num += (b_val - s_val) * 0.4066; book_den += (b_val + s_val) * 0.4066
        b_val = snap.get("bp5",0)*snap.get("bq5",0); s_val = snap.get("sp5",0)*snap.get("sq5",0)
        book_num += (b_val - s_val) * 0.3012; book_den += (b_val + s_val) * 0.3012
        book_imb = book_num / max(book_den, 1.0)
        mlofi_raw = book_imb - flow_imb
        hist = self.mlofi_history.setdefault(sym, deque(maxlen=self.CFG["mlofi_window"]))
        hist.append(mlofi_raw)
        if len(hist) > 10: return (mlofi_raw - np.mean(hist)) / max(np.std(hist), 1e-6)
        return mlofi_raw

    def _calculate_whale_z(self, sym: str, conc: float) -> float:
        hist = self.whale_history.setdefault(sym, deque(maxlen=self.CFG["whale_window"]))
        hist.append(conc)
        if len(hist) < 10: return 0.0
        return (conc - np.mean(hist)) / max(np.std(hist), 1e-6)

    def _calculate_dkla(self, snap: Dict, mid: float) -> Tuple[float, float, float, float]:
        notional = self.CFG["dkla_notional"]
        ap = [snap.get(f"sp{i}",0.0) for i in range(1,6)]; aq = [snap.get(f"sq{i}",0.0) for i in range(1,6)]
        bp = [snap.get(f"bp{i}",0.0) for i in range(1,6)]; bq = [snap.get(f"bq{i}",0.0) for i in range(1,6)]
        lam_buy = _walk_book_impact_fast(ap, aq, notional, mid)
        lam_sell = _walk_book_impact_fast(bp, bq, notional, mid)
        denom = lam_buy + lam_sell
        asym = (lam_buy - lam_sell) / max(denom, 1e-6) if denom > 0 else 0.0
        eff_lam = max(lam_buy, lam_sell)
        return lam_buy, lam_sell, asym, eff_lam

    def _update_lra(self, sym: str, snap: Dict, curr_bids: Dict, curr_asks: Dict, 
                    trade_price: float, trade_qty: float) -> Tuple[float, float]:
        alpha = self.CFG["lra_alpha"]
        state = self.lra_ema.setdefault(sym, {'bid': [0.0]*5, 'ask': [0.0]*5})
        bid_emas, ask_emas = state['bid'], state['ask']
        last = self.last_depth_state.get(sym, {'bids': {}, 'asks': {}})
        
        for i in range(1, 6):
            p = snap.get(f"bp{i}", 0.0)
            if p > 0:
                oq = last['bids'].get(p, 0); nq = curr_bids.get(p, 0)
                if nq > oq and not (abs(p - trade_price) < 1e-6 and trade_qty > 0):
                    bid_emas[i-1] = _ema_update(bid_emas[i-1], float(nq - oq), alpha)
        for i in range(1, 6):
            p = snap.get(f"sp{i}", 0.0)
            if p > 0:
                oq = last['asks'].get(p, 0); nq = curr_asks.get(p, 0)
                if nq > oq and not (abs(p - trade_price) < 1e-6 and trade_qty > 0):
                    ask_emas[i-1] = _ema_update(ask_emas[i-1], float(nq - oq), alpha)
        
        bid_res = bid_emas[0]*0.7 + bid_emas[1]*0.3
        ask_res = ask_emas[0]*0.7 + ask_emas[1]*0.3
        denom = bid_res + ask_res
        asym = (bid_res - ask_res) / max(denom, 1e-6) if denom > 0 else 0.0
        vel = asym - self.lra_asym_prev.get(sym, 0.0)
        self.lra_asym_prev[sym] = asym
        return asym, vel

    def _calculate_queue_decay(self, sym: str, snap: Dict, curr_bids: Dict, curr_asks: Dict) -> Tuple[float, float]:
        """
        Returns (bid_queue_decay_bps, ask_queue_decay_bps)
        Measures how fast the queue ahead of a hypothetical new limit order is being consumed.
        High decay = liquidity vanishing = adverse selection risk.
        """
        bp1, bq1 = snap.get("bp1", 0.0), snap.get("bq1", 0)
        sp1, sq1 = snap.get("sp1", 0.0), snap.get("sq1", 0)
        
        if bq1 <= 0 or bp1 <= 0 or sp1 <= 0 or sq1 <= 0:
            return 0.0, 0.0
        
        last = self.last_depth_state.get(sym, {'bids': {}, 'asks': {}})
        
        # Bid queue consumption = cancellations at bid + trades hitting bid (aggressive sells)
        bid_cancelled = max(0, last['bids'].get(bp1, 0) - curr_bids.get(bp1, 0))
        # Trade at bid proxy: sell volume when lp <= bp1
        trade_at_bid = max(0, -self.cur_sell_val.get(sym, 0)) if self.cur_sell_val.get(sym, 0) < 0 else 0
        
        # Ask queue consumption = cancellations at ask + trades hitting ask (aggressive buys)
        ask_cancelled = max(0, last['asks'].get(sp1, 0) - curr_asks.get(sp1, 0))
        trade_at_ask = max(0, self.cur_buy_val.get(sym, 0)) if self.cur_buy_val.get(sym, 0) > 0 else 0
        
        bid_consumption = bid_cancelled + trade_at_bid
        ask_consumption = ask_cancelled + trade_at_ask
        
        bid_decay_bps = (bid_consumption / max(bq1, 1)) * 10000
        ask_decay_bps = (ask_consumption / max(sq1, 1)) * 10000
        
        return bid_decay_bps, ask_decay_bps

    def _update_mpar(self, sym: str, snap: Dict, dt: float) -> Tuple[float, float, float]:
        bp1, bq1 = snap.get("bp1",0.0), snap.get("bq1",0.0)
        ap1, aq1 = snap.get("sp1",0.0), snap.get("sq1",0.0)
        if not (bp1>0 and ap1>0 and bq1>0 and aq1>0): return 0.0, 0.0, 0.0
        mp = (bp1*aq1 + ap1*bq1) / (bq1 + aq1)
        prev_mp = self.mp_prev.get(sym, mp)
        prev_vel = self.mp_vel.get(sym, 0.0)
        vel = _ema_update(prev_vel, (mp - prev_mp)/max(dt,1e-6), self.CFG["mp_alpha"])
        accel = _ema_update(self.mp_accel.get(sym,0.0), (vel - prev_vel)/max(dt,1e-6), self.CFG["mp_accel_alpha"])
        self.mp_prev[sym], self.mp_vel[sym], self.mp_accel[sym] = mp, vel, accel
        # Instant Book Pressure (OIB)
        book_p = (bp1*bq1 - ap1*aq1) / max(bp1*bq1 + ap1*aq1, 1.0)
        align = 1.0 if (accel * book_p) > 0 else -1.0
        return accel, align, mp

    # ═══════════════════════════════════════════════════════════
    # MAIN TICK LOOP
    # ═══════════════════════════════════════════════════════════

    def on_tick(self, msg: Dict) -> None:
        try:
            tok = str(msg.get("token", ""))
            sym = self.t2s.get(tok)
            if not sym: return
            st = self.last_snap.setdefault(sym, {})
            lp = float(msg.get("last_traded_price") or msg.get("lp") or st.get("lp") or 0)
            if lp <= 0: return
            
            nows = pytime.time()
            dt = nows - st.get("last_ts", nows)
            st["last_ts"] = nows
            
            # Volume
            vol = float(msg.get("volume_trade_for_the_day") or msg.get("v") or st.get("vol", 0))
            dVol = max(0.0, vol - st.get("vol", vol))
            st["vol"] = vol; st["lp"] = lp
            if "bts" not in st: st["bts"] = nows
            exch_ts = float(msg.get("timestamp") or msg.get("exch_ts") or msg.get("tt") or nows)
            st["tick_latency"] = max(0.0, nows - exch_ts) if exch_ts > 1e9 else 0.0

            # Parse Depth (Unrolled)
            curr_bids, curr_asks = {}, {}
            touch_bid = touch_ask = 0.0
            for i in range(1, 6):
                b_p = float(msg.get(f"bp{i}") or st.get(f"bp{i}") or 0)
                b_q = float(msg.get(f"bq{i}") or st.get(f"bq{i}") or 0)
                a_p = float(msg.get(f"sp{i}") or st.get(f"sp{i}") or 0)
                a_q = float(msg.get(f"sq{i}") or st.get(f"sq{i}") or 0)
                st[f"bp{i}"]=b_p; st[f"bq{i}"]=b_q; st[f"sp{i}"]=a_p; st[f"sq{i}"]=a_q
                if i==1: touch_bid, touch_ask = b_p, a_p
                if b_p>0: curr_bids[b_p]=int(b_q)
                if a_p>0: curr_asks[a_p]=int(a_q)

            trade_val = dVol * lp
            if trade_val > self.CFG["glitch_guard_cr"]*1_00_00_000.0: trade_val = self.CFG["glitch_guard_cr"]*1_00_00_000.0
            
            if trade_val > 0:
                bp1, sp1 = st.get("bp1",0.0), st.get("sp1",0.0)
                if bp1>0 and sp1>0:
                    if lp >= sp1: self.cur_buy_val[sym] = self.cur_buy_val.get(sym,0.0) + trade_val
                    elif lp <= bp1: self.cur_sell_val[sym] = self.cur_sell_val.get(sym,0.0) + trade_val
                    else:
                        self.cur_buy_val[sym] = self.cur_buy_val.get(sym,0.0) + trade_val*0.5
                        self.cur_sell_val[sym] = self.cur_sell_val.get(sym,0.0) + trade_val*0.5
                else:
                    self.cur_buy_val[sym] = self.cur_buy_val.get(sym,0.0) + trade_val*0.5
                    self.cur_sell_val[sym] = self.cur_sell_val.get(sym,0.0) + trade_val*0.5
                self.cur_tot_val[sym] = self.cur_tot_val.get(sym,0.0) + trade_val
                self.cur_tick_count[sym] = self.cur_tick_count.get(sym,0) + 1
                self.cur_max_tick_val[sym] = max(self.cur_max_tick_val.get(sym,0.0), trade_val)
                self.trade_times.setdefault(sym, deque(maxlen=50000)).append(nows)

            # ── PER-TICK ADVANCED METRICS ──
            # NOTE: LRA must run BEFORE vacuum, since vacuum overwrites self.last_depth_state[sym]
            # with the CURRENT snapshot, and LRA needs to compare against the PREVIOUS snapshot to
            # detect size adds. Reversing this order silently zeros LRA forever (oq==nq always).
            lra_asym, lra_vel = self._update_lra(sym, st, curr_bids, curr_asks, lp, dVol)
            vac_bps = self._calculate_vacuum_score(sym, curr_bids, curr_asks, dVol, lp, touch_bid, touch_ask) if dVol >= 0 else 0.0
            self.cur_net_vacuum[sym] = self.cur_net_vacuum.get(sym, 0.0) + vac_bps
            
            # Queue Position Decay (Adverse Selection Signal)
            bid_queue_decay, ask_queue_decay = self._calculate_queue_decay(sym, st, curr_bids, curr_asks)
            
            mid = (touch_bid + touch_ask)*0.5 if touch_bid>0 and touch_ask>0 else lp
            lam_buy, lam_sell, dkla_asym, eff_lam = self._calculate_dkla(st, mid)
            mp_accel, mp_align, mp_val = self._update_mpar(sym, st, dt)
            
            st["_adv"] = {"vac_bps_tick": vac_bps, "lam_buy": lam_buy, "lam_sell": lam_sell, 
                          "dkla_asym": dkla_asym, "eff_lam": eff_lam,
                          "lra_asym": lra_asym, "lra_vel": lra_vel,
                          "mp_accel": mp_accel, "mp_align": mp_align, "mp_val": mp_val,
                          "bid_queue_decay": bid_queue_decay, "ask_queue_decay": ask_queue_decay}

            # Bucket Check
            if self.cur_tot_val.get(sym, 0) >= self.bucket_map.get(sym, self.CFG["bucket_turnover"]):
                self._process_bucket(sym, lp, nows, dt)
        except Exception as e:
            logger.error(f"on_tick {sym}: {e}", exc_info=True)

    def on_tick_parsed(self, tick: "ParsedTick") -> None:
        """
        Zero-allocation hot path entry point.
        Accepts pre-parsed ParsedTick from TickCompiler, bypassing dict creation.
        """
        try:
            sym = self.t2s.get(tick.tok)
            if not sym: return
            st = self.last_snap.setdefault(sym, {})
            lp = tick.lp
            if lp <= 0: return
            
            nows = tick.ts
            dt = nows - st.get("last_ts", nows)
            st["last_ts"] = nows
            
            # Volume
            vol = tick.vol
            dVol = max(0.0, vol - st.get("vol", vol))
            st["vol"] = vol; st["lp"] = lp
            if "bts" not in st: st["bts"] = nows
            # Note: tick_latency not available in zero-copy path (no exch_ts)
            
            # Parse Depth from pre-filled arrays (Unrolled)
            curr_bids, curr_asks = {}, {}
            touch_bid = touch_ask = 0.0
            for i in range(5):
                b_p = tick.bp[i]
                b_q = tick.bq[i]
                a_p = tick.sp[i]
                a_q = tick.sq[i]
                st[f"bp{i+1}"]=b_p; st[f"bq{i+1}"]=b_q; st[f"sp{i+1}"]=a_p; st[f"sq{i+1}"]=a_q
                if i==0: touch_bid, touch_ask = b_p, a_p
                if b_p>0: curr_bids[b_p]=int(b_q)
                if a_p>0: curr_asks[a_p]=int(a_q)

            trade_val = dVol * lp
            if trade_val > self.CFG["glitch_guard_cr"]*1_00_00_000.0: trade_val = self.CFG["glitch_guard_cr"]*1_00_00_000.0
            
            if trade_val > 0:
                bp1, sp1 = st.get("bp1",0.0), st.get("sp1",0.0)
                if bp1>0 and sp1>0:
                    if lp >= sp1: self.cur_buy_val[sym] = self.cur_buy_val.get(sym,0.0) + trade_val
                    elif lp <= bp1: self.cur_sell_val[sym] = self.cur_sell_val.get(sym,0.0) + trade_val
                    else:
                        self.cur_buy_val[sym] = self.cur_buy_val.get(sym,0.0) + trade_val*0.5
                        self.cur_sell_val[sym] = self.cur_sell_val.get(sym,0.0) + trade_val*0.5
                else:
                    self.cur_buy_val[sym] = self.cur_buy_val.get(sym,0.0) + trade_val*0.5
                    self.cur_sell_val[sym] = self.cur_sell_val.get(sym,0.0) + trade_val*0.5
                self.cur_tot_val[sym] = self.cur_tot_val.get(sym,0.0) + trade_val
                self.cur_tick_count[sym] = self.cur_tick_count.get(sym,0) + 1
                self.cur_max_tick_val[sym] = max(self.cur_max_tick_val.get(sym,0.0), trade_val)
                self.trade_times.setdefault(sym, deque(maxlen=50000)).append(nows)

            # ── PER-TICK ADVANCED METRICS ──
            # NOTE: LRA must run BEFORE vacuum, since vacuum overwrites self.last_depth_state[sym]
            # with the CURRENT snapshot, and LRA needs to compare against the PREVIOUS snapshot to
            # detect size adds. Reversing this order silently zeros LRA forever (oq==nq always).
            lra_asym, lra_vel = self._update_lra(sym, st, curr_bids, curr_asks, lp, dVol)
            vac_bps = self._calculate_vacuum_score(sym, curr_bids, curr_asks, dVol, lp, touch_bid, touch_ask) if dVol >= 0 else 0.0
            self.cur_net_vacuum[sym] = self.cur_net_vacuum.get(sym, 0.0) + vac_bps
            
            # Queue Position Decay (Adverse Selection Signal)
            bid_queue_decay, ask_queue_decay = self._calculate_queue_decay(sym, st, curr_bids, curr_asks)
            
            mid = (touch_bid + touch_ask)*0.5 if touch_bid>0 and touch_ask>0 else lp
            lam_buy, lam_sell, dkla_asym, eff_lam = self._calculate_dkla(st, mid)
            mp_accel, mp_align, mp_val = self._update_mpar(sym, st, dt)
            
            st["_adv"] = {"vac_bps_tick": vac_bps, "lam_buy": lam_buy, "lam_sell": lam_sell, 
                          "dkla_asym": dkla_asym, "eff_lam": eff_lam,
                          "lra_asym": lra_asym, "lra_vel": lra_vel,
                          "mp_accel": mp_accel, "mp_align": mp_align, "mp_val": mp_val,
                          "bid_queue_decay": bid_queue_decay, "ask_queue_decay": ask_queue_decay}

            # Bucket Check
            if self.cur_tot_val.get(sym, 0) >= self.bucket_map.get(sym, self.CFG["bucket_turnover"]):
                self._process_bucket(sym, lp, nows, dt)
        except Exception as e:
            logger.error(f"on_tick_parsed {sym}: {e}", exc_info=True)

    # ═══════════════════════════════════════════════════════════
    # BUCKET PROCESSING & UNIFIED CONVICTION TRIGGER
    # ═══════════════════════════════════════════════════════════

    def _process_bucket(self, sym: str, lp: float, nows: float, dt: float) -> None:
        st = self.last_snap.get(sym, {})
        adv = st.get("_adv", {})
        target_bucket = self.bucket_map.get(sym, self.CFG["bucket_turnover"])
        
        # ── Core Calculations ──
        bv = self.cur_buy_val.get(sym, 0.0); sv = self.cur_sell_val.get(sym, 0.0); tv = self.cur_tot_val.get(sym, 0.0)
        tpin = (bv - sv) / max(target_bucket, 1.0)
        self.vpin_q.setdefault(sym, deque(maxlen=20)).append(abs(tpin))
        avg_tpin = np.mean(self.vpin_q[sym]) if self.vpin_q[sym] else 0.0
        
        # Hawkes
        branching = 0.0
        if sym in self.trade_times:
            times = self.trade_times[sym]; cutoff = nows - self.CFG["hawkes_lookback_sec"]
            while times and times[0] < cutoff: times.popleft()
            if len(times) >= 10:
                counts = np.zeros(self._hawkes_bins)
                for t in times:
                    bi = int((t - cutoff) // self.CFG["hawkes_bin_sec"])
                    if 0 <= bi < self._hawkes_bins: counts[bi] += 1
                mc, vc = np.mean(counts), np.var(counts)
                if vc > 0 and mc > 0: branching = max(0.0, 1.0 - np.sqrt(mc / vc))
        
        # Microprice Dev
        bp1, bq1, sp1, sq1 = st.get("bp1",0), st.get("bq1",0), st.get("sp1",0), st.get("sq1",0)
        micro_dev = 0.0
        if (bq1+sq1)>0 and bp1>0 and sp1>0:
            b_val, s_val = bp1*bq1, sp1*sq1
            imb = b_val / max(b_val+s_val,1.0)
            mp = (bp1+sp1)*0.5 + (sp1-bp1)*(imb-0.5)
            micro_dev = (lp - mp) / mp
        
        # MLOFI (Fixed)
        mlofi = self._calculate_mlofi(sym, st, tpin)
        
        # Spread/OIMB
        spread = max(0.0, sp1-bp1) if sp1>0 and bp1>0 else 0.0
        b_tot = sum(st.get(f"bq{i}",0) for i in range(1,6))
        s_tot = sum(st.get(f"sq{i}",0) for i in range(1,6))
        oimb = float(b_tot/s_tot) if s_tot>0 else (999.0 if b_tot>0 else 1.0)
        
        # Whale
        tc = max(1, self.cur_tick_count.get(sym,1))
        whale_conc = self.cur_max_tick_val.get(sym,0.0)/max(tv,1.0)
        whale_z = self._calculate_whale_z(sym, whale_conc)
        net_vac = self.cur_net_vacuum.get(sym, 0.0)
        
        # ── ADVANCED AGGREGATES ──
        spread_norm = spread / max(lp, 1e-6)
        mp_accel_norm = adv.get("mp_accel",0.0) / max(spread_norm * math.sqrt(max(dt,1e-6)), 1e-6)
        divergence = mp_accel_norm - (tpin * spread_norm * 100)
        mpar_score = adv.get("mp_align",0.0) * divergence * abs(mlofi)
        
        toxicity = adv.get("eff_lam",0.0) * abs(tpin)
        tox_hist = self.toxicity_history.setdefault(sym, deque(maxlen=self.CFG["toxicity_window"]))
        tox_hist.append(toxicity)
        tox_p90 = np.percentile(tox_hist, 90) if len(tox_hist) > 20 else toxicity
        
        lra_asym = adv.get("lra_asym", 0.0)
        lra_vel = adv.get("lra_vel", 0.0)
        lra_signal = lra_asym if tpin > 0.1 else (-lra_asym if tpin < -0.1 else 0.0)
        dkla_asym = adv.get("dkla_asym", 0.0)
        
        # ═══════════════════════════════════════════════
        # UNIFIED CONVICTION SCORING (THE TRIGGER LOGIC)
        # ═══════════════════════════════════════════════
        
        # --- HARD GATES (Must Pass) ---
        gate_ticks = tc >= self.TRIG["min_ticks"]
        gate_branching = branching >= self.TRIG["min_branching"]
        gate_whale = whale_z >= self.TRIG["min_whale_z"]
        gate_tox = (len(tox_hist) < 20 or toxicity >= np.percentile(tox_hist, self.TRIG["min_tox_pct"]))
        
        hard_gate_pass = bool(gate_ticks and gate_branching and gate_whale and gate_tox)
        
        # ULTRA-DETAILED TELEMETRY: Log all Phase 1 Hard Gates with pass/fail
        logger.info(
            f"[GATE] {sym} | Ticks: {tc}/{self.TRIG['min_ticks']} {'✅' if gate_ticks else '❌'} | "
            f"Branching: {branching:.3f}/{self.TRIG['min_branching']} {'✅' if gate_branching else '❌'} | "
            f"WhaleZ: {whale_z:.2f}/{self.TRIG['min_whale_z']} {'✅' if gate_whale else '❌'} | "
            f"Toxicity: {toxicity:.1f}/{tox_p90:.1f}(P90) {'✅' if gate_tox else '❌'} | "
            f"HARD_GATE: {'PASS' if hard_gate_pass else 'FAIL'}"
        )
        
        signal_side = None
        conviction = 0.0
        long_score = 0.0
        short_score = 0.0
        
        if hard_gate_pass:
            # --- LONG CONVICTION ---
            # Components normalized to ~ [0, 1] range via tanh/clamp
            c_mlofi = max(0.0, min(self.TRIG["mlofi_cap"], mlofi)) / self.TRIG["mlofi_cap"]      # Book supports bid
            c_mpar  = max(0.0, min(self.TRIG["mpar_cap"], mpar_score)) / self.TRIG["mpar_cap"]     # Accel up
            c_vac   = max(0.0, min(self.TRIG["vac_cap"], net_vac)) / self.TRIG["vac_cap"]          # Ask vacuum
            c_lra   = max(0.0, min(self.TRIG["lra_cap"], lra_signal)) / self.TRIG["lra_cap"]       # MM flee ask
            c_dkla  = max(0.0, min(self.TRIG["dkla_cap"], dkla_asym)) / self.TRIG["dkla_cap"]      # Lambda Buy > Sell
            c_micro = 1.0 if micro_dev > 0.0002 else 0.0                                           # Price > Fair Value
            
            long_score = (
                c_mlofi * self.TRIG["w_mlofi"] +
                c_mpar  * self.TRIG["w_mpar"]  +
                c_vac   * self.TRIG["w_vac"]   +
                c_lra   * self.TRIG["w_lra"]   +
                c_dkla  * self.TRIG["w_dkla"]  +
                c_micro * self.TRIG["w_micro"]
            )
            
            # --- SHORT CONVICTION ---
            c_mlofi_s = max(0.0, min(self.TRIG["mlofi_cap"], -mlofi)) / self.TRIG["mlofi_cap"]
            c_mpar_s  = max(0.0, min(self.TRIG["mpar_cap"], -mpar_score)) / self.TRIG["mpar_cap"]
            c_vac_s   = max(0.0, min(self.TRIG["vac_cap"], -net_vac)) / self.TRIG["vac_cap"]
            c_lra_s   = max(0.0, min(self.TRIG["lra_cap"], -lra_signal)) / self.TRIG["lra_cap"]
            c_dkla_s  = max(0.0, min(self.TRIG["dkla_cap"], -dkla_asym)) / self.TRIG["dkla_cap"]
            c_micro_s = 1.0 if micro_dev < -0.0002 else 0.0
            
            short_score = (
                c_mlofi_s * self.TRIG["w_mlofi"] +
                c_mpar_s  * self.TRIG["w_mpar"]  +
                c_vac_s   * self.TRIG["w_vac"]   +
                c_lra_s   * self.TRIG["w_lra"]   +
                c_dkla_s  * self.TRIG["w_dkla"]  +
                c_micro_s * self.TRIG["w_micro"]
            )
            
            thresh = self.TRIG["conviction_threshold"]
            if long_score >= thresh and long_score > short_score:
                signal_side = "BUY"
                conviction = long_score
            elif short_score >= thresh and short_score > long_score:
                signal_side = "SELL"
                conviction = short_score
        
        # ULTRA-DETAILED TELEMETRY: Log Phase 2 Conviction Scores with component breakdown
        if hard_gate_pass:
            logger.info(
                f"[CONVICTION] {sym} | LONG: {long_score:.2f}/{thresh} {'✅' if long_score >= thresh else '❌'} | "
                f"SHORT: {short_score:.2f}/{thresh} {'✅' if short_score >= thresh else '❌'} | "
                f"Signal: {signal_side or 'NONE'} | Conviction: {conviction:.2f}"
            )
            logger.info(
                f"[CONVICTION_DETAIL] {sym} | "
                f"MLOFI: {c_mlofi:.3f}*{self.TRIG['w_mlofi']}={c_mlofi*self.TRIG['w_mlofi']:.2f} | "
                f"MPAR: {c_mpar:.3f}*{self.TRIG['w_mpar']}={c_mpar*self.TRIG['w_mpar']:.2f} | "
                f"VAC: {c_vac:.3f}*{self.TRIG['w_vac']}={c_vac*self.TRIG['w_vac']:.2f} | "
                f"LRA: {c_lra:.3f}*{self.TRIG['w_lra']}={c_lra*self.TRIG['w_lra']:.2f} | "
                f"DKLA: {c_dkla:.3f}*{self.TRIG['w_dkla']}={c_dkla*self.TRIG['w_dkla']:.2f} | "
                f"MICRO: {c_micro:.3f}*{self.TRIG['w_micro']}={c_micro*self.TRIG['w_micro']:.2f}"
            )
            logger.info(
                f"[CONVICTION_DETAIL] {sym} | "
                f"MLOFI_s: {c_mlofi_s:.3f}*{self.TRIG['w_mlofi']}={c_mlofi_s*self.TRIG['w_mlofi']:.2f} | "
                f"MPAR_s: {c_mpar_s:.3f}*{self.TRIG['w_mpar']}={c_mpar_s*self.TRIG['w_mpar']:.2f} | "
                f"VAC_s: {c_vac_s:.3f}*{self.TRIG['w_vac']}={c_vac_s*self.TRIG['w_vac']:.2f} | "
                f"LRA_s: {c_lra_s:.3f}*{self.TRIG['w_lra']}={c_lra_s*self.TRIG['w_lra']:.2f} | "
                f"DKLA_s: {c_dkla_s:.3f}*{self.TRIG['w_dkla']}={c_dkla_s*self.TRIG['w_dkla']:.2f} | "
                f"MICRO_s: {c_micro_s:.3f}*{self.TRIG['w_micro']}={c_micro_s*self.TRIG['w_micro']:.2f}"
            )
        
        # ── COMPREHENSIVE LOG ENTRY ──
        log_entry = {
            "ts": datetime.now().isoformat(), "sym": sym, "lp": round(lp,2), "bucket_val": round(tv,0),
            "duration_sec": round(nows - st.get("bts", nows), 2), "tick_count": tc,
            # Core
            "tpin": round(tpin,5), "tpin_avg": round(avg_tpin,5), "branching": round(branching,5),
            "mlofi_z": round(mlofi,5), "spread": round(spread,2), "oimb": round(oimb,3),
            "micro_dev": round(micro_dev,6), "whale_conc": round(whale_conc,5), "whale_z": round(whale_z,5),
            "net_vacuum_bps": round(net_vac,2), "tick_latency": round(st.get("tick_latency",0),5),
            # Advanced Raw
            "mpar_score": round(mpar_score,5), "mpar_alignment": round(adv.get("mp_align",0),3),
            "lra_asymmetry": round(lra_asym,5), "lra_velocity": round(lra_vel,5), "lra_signal": round(lra_signal,5),
            "dkla_lambda_buy_bps": round(adv.get("lam_buy",0),2), "dkla_lambda_sell_bps": round(adv.get("lam_sell",0),2),
            "dkla_asymmetry": round(dkla_asym,5), "dkla_toxicity": round(toxicity,2), "dkla_tox_p90": round(tox_p90,2),
            # Trigger Diagnostics
            "hard_gate_pass": hard_gate_pass, "conviction_long": round(long_score, 2),
            "conviction_short": round(short_score, 2),
            "signal_generated": signal_side, "conviction_score": round(conviction, 2)
        }
        
        # Async Log
        try:
            with open(self.LOG_FILE, "a") as f: f.write(json.dumps(log_entry, default=lambda o: bool(o) if isinstance(o, np.bool_) else float(o) if isinstance(o, np.floating) else int(o) if isinstance(o, np.integer) else str(o)) + "\n")
        except IOError: pass
        
        # ── PUSH ACTIONABLE SIGNAL ──
        if signal_side:
            sig_payload = {
                "type": "ENTRY_SIGNAL",
                "sym": sym, "signal": signal_side, "lp": lp,
                "conviction": round(conviction, 2),
                "reason": f"Unified_Conviction_v3",
                "metrics": log_entry
            }
            self.signal_queue.put(sig_payload)
            logger.info(f"🚀 SIGNAL {signal_side} {sym} @ {lp:.2f} | Conv: {conviction:.2f} | Branching: {branching:.2f} WhaleZ: {whale_z:.2f}")
            
            # ULTRA-DETAILED TELEMETRY: Log entry decision with full conviction metrics
            logger.info(
                f"[ENTRY_DECISION] {sym} | Side: {signal_side} | LP: {lp:.2f} | Conviction: {conviction:.2f}/{thresh} | "
                f"TPIN: {tpin:.5f} | Branching: {branching:.3f} | WhaleZ: {whale_z:.2f} | "
                f"Toxicity: {toxicity:.1f}(P90:{tox_p90:.1f}) | NetVac: {net_vac:.1f}bps | "
                f"MPAR: {mpar_score:.3f}(align:{adv.get('mp_align',0):.1f}) | LRA: {lra_signal:.3f} | DKLA: {dkla_asym:.3f} | "
                f"MicroDev: {micro_dev:.6f} | QueueDecay: Bid={adv.get('bid_queue_decay',0):.1f} Ask={adv.get('ask_queue_decay',0):.1f}bps"
            )
        
        # ── RESET BUCKET ──
        # Preserve absolute overflow amount, not ratio
        overflow_val = max(0.0, tv - target_bucket)
        if overflow_val > 0:
            # Distribute overflow proportionally to buy/sell
            buy_ratio = bv / max(tv, 1.0)
            sell_ratio = sv / max(tv, 1.0)
            self.cur_buy_val[sym] = overflow_val * buy_ratio
            self.cur_sell_val[sym] = overflow_val * sell_ratio
            self.cur_tot_val[sym] = overflow_val
        else:
            self.cur_buy_val[sym] = 0.0
            self.cur_sell_val[sym] = 0.0
            self.cur_tot_val[sym] = 0.0
        self.cur_tick_count[sym] = 0
        self.cur_max_tick_val[sym] = 0.0
        self.cur_net_vacuum[sym] = 0.0
        st["bts"] = nows
        self.completion_times.setdefault(sym, deque(maxlen=1000)).append(nows)

    # ═══════════════════════════════════════════════════════════
    # MONITORING
    # ═══════════════════════════════════════════════════════════
    def get_market_state_summary(self) -> str:
        if not self.cur_net_vacuum: return "scanning..."
        try:
            hottest = max(self.cur_net_vacuum.keys(), key=lambda s: abs(self.cur_net_vacuum.get(s,0)))
            st = self.last_snap.get(hottest, {}); adv = st.get("_adv", {})
            return (f"{hottest} Bkt:{self.cur_tot_val.get(hottest,0)/self.bucket_map.get(hottest,1)*100:.0f}% "
                    f"Vac:{self.cur_net_vacuum.get(hottest,0):+.0f}bps | "
                    f"MPAR:{adv.get('mp_align',0):+.1f}*{adv.get('mp_accel',0):+.2f} "
                    f"LRA:{adv.get('lra_signal',0):+.2f} "
                    f"DKLA:{adv.get('dkla_asym',0):+.2f} "
                    f"WhaleZ:{self._calculate_whale_z(hottest, self.cur_max_tick_val.get(hottest,0)/max(self.cur_tot_val.get(hottest,1),1)):.1f}")
        except Exception: return "error"

def attach_cascade_engine(broker, token_to_symbol, bucket_map=None, vacuum_map=None, 
                          dkla_notional=1_00_00_000.0, trigger_config=None):
    engine = CascadeValueEngine(token_to_symbol, bucket_map, vacuum_map, 
                                dkla_notional=dkla_notional, trigger_config=trigger_config)
    candidates = [broker]
    for attr in ("market_data","mdp","market_data_provider","_mdp","_api","api","session_provider"):
        obj = getattr(broker, attr, None)
        if obj: candidates.append(obj)
    for c in candidates:
        if hasattr(c, "add_tick_listener"):
            c.add_tick_listener(engine.on_tick)
            logger.info("Cascade Engine LIVE Attached (Unified Conviction Trigger Active)")
            return engine
    logger.error("No add_tick_listener found"); return engine