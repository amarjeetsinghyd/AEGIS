"""Microstructure Cascade — Turnover-Driven Event Engine (Shadow Mode)"""
from __future__ import annotations
import os, json, time as pytime, math
from collections import deque
from datetime import datetime
import numpy as np
import pandas as pd
from common.config import get_data_dir
from common.logger import get_logger

logger = get_logger("cascade_engine")

class CascadeValueEngine:
    def __init__(self, token_to_symbol: dict):
        self.t2s = {str(k): str(v) for k, v in token_to_symbol.items()}
        # Target bucket: 2 Crore INR by default. Dynamic calibration later.
        self.BUCKET_VALUE = 2_00_00_000.0
        
        self.last_snap = {}
        # Accumulators for the CURRENT bucket
        self.cur_buy_val = {}
        self.cur_sell_val = {}
        self.cur_tot_val = {}
        
        # Deques to hold historical bucket metrics
        self.vpin_q = {}
        # We store completion timestamps for Fano factor calculation
        self.completion_times = {}
        
        self.LOG_FILE = os.path.join(get_data_dir(), "logs", f"shadow_cascade_{datetime.now().strftime('%Y-%m-%d')}.jsonl")
        
    def on_tick(self, msg):
        try:
            tok = str(msg.get("token", ""))
            sym = self.t2s.get(tok)
            if not sym: return
            
            lp = float(msg.get("last_traded_price") or msg.get("lp") or 0)
            if lp <= 0: return
            
            vol = float(msg.get("volume_trade_for_the_day") or msg.get("v") or 0)
            st = self.last_snap.setdefault(sym, {"vol": vol, "lp": lp})
            dVol = max(0.0, vol - st.get("vol", vol))
            st["vol"] = vol
            
            nows = pytime.time()
            if "bts" not in st: st["bts"] = nows
            
            trade_val = dVol * lp
            if trade_val > 0:
                bp1 = float(msg.get("bp1") or st.get("bp1") or 0)
                sp1 = float(msg.get("sp1") or st.get("sp1") or 0)
                
                # Assign to Buy/Sell (Aggressor)
                if bp1 > 0 and sp1 > 0:
                    if lp >= sp1: 
                        self.cur_buy_val[sym] = self.cur_buy_val.get(sym, 0.0) + trade_val
                    elif lp <= bp1:
                        self.cur_sell_val[sym] = self.cur_sell_val.get(sym, 0.0) + trade_val
                    else:
                        self.cur_buy_val[sym] = self.cur_buy_val.get(sym, 0.0) + trade_val * 0.5
                        self.cur_sell_val[sym] = self.cur_sell_val.get(sym, 0.0) + trade_val * 0.5
                else:
                    self.cur_buy_val[sym] = self.cur_buy_val.get(sym, 0.0) + trade_val * 0.5
                    self.cur_sell_val[sym] = self.cur_sell_val.get(sym, 0.0) + trade_val * 0.5
                    
                self.cur_tot_val[sym] = self.cur_tot_val.get(sym, 0.0) + trade_val
            
            # Update L2 State for MLOFI & Microprice
            for i in range(1, 6):
                st[f"bp{i}"] = float(msg.get(f"bp{i}") or st.get(f"bp{i}") or 0)
                st[f"bq{i}"] = float(msg.get(f"bq{i}") or st.get(f"bq{i}") or 0)
                st[f"sp{i}"] = float(msg.get(f"sp{i}") or st.get(f"sp{i}") or 0)
                st[f"sq{i}"] = float(msg.get(f"sq{i}") or st.get(f"sq{i}") or 0)
            
            # CHECK IF BUCKET IS FULL
            if self.cur_tot_val.get(sym, 0) >= self.BUCKET_VALUE:
                self._process_bucket(sym, lp, nows)
                
        except Exception:
            pass # Keep it fast and silent

    def _process_bucket(self, sym, lp, nows):
        st = self.last_snap.get(sym, {})
        
        # 1. TPIN
        bv = self.cur_buy_val.get(sym, 0.0)
        sv = self.cur_sell_val.get(sym, 0.0)
        tpin = abs(bv - sv) / max(self.BUCKET_VALUE, 1.0)
        self.vpin_q.setdefault(sym, deque(maxlen=20)).append(tpin)
        avg_tpin = sum(self.vpin_q[sym]) / len(self.vpin_q[sym])
        
        # 2. Hawkes Branching (Fano factor of completions in 10-sec bins over 600s)
        self.completion_times.setdefault(sym, deque(maxlen=200)).append(nows)
        times = self.completion_times[sym]
        
        # Remove timestamps older than 600 seconds
        cutoff = nows - 600
        while times and times[0] < cutoff:
            times.popleft()
            
        branching = 0.0
        if len(times) >= 10:
            # Build counts for 60 bins of 10s each
            counts = np.zeros(60)
            for t in times:
                bin_idx = int((t - cutoff) // 10)
                if 0 <= bin_idx < 60:
                    counts[bin_idx] += 1
            
            mean_c = np.mean(counts)
            var_c = np.var(counts)
            if var_c > 0 and mean_c > 0:
                branching = max(0.0, 1.0 - np.sqrt(mean_c / var_c))
        
        # 3. Microprice Value Deviation
        bp1, bq1 = st.get("bp1", 0.0), st.get("bq1", 0.0)
        sp1, sq1 = st.get("sp1", 0.0), st.get("sq1", 0.0)
        micro_dev = 0.0
        if (bq1 + sq1) > 0 and bp1 > 0 and sp1 > 0:
            b_val = bp1 * bq1
            s_val = sp1 * sq1
            imb = b_val / max(b_val + s_val, 1.0)
            microprice = ((bp1 + sp1) / 2.0) + (sp1 - bp1) * (imb - 0.5)
            micro_dev = (lp - microprice) / microprice
            
        # 4. MLOFI Value (Simplified Top 3 Levels)
        mlofi_val = 0.0
        for i in range(1, 4):
            b_val = st.get(f"bp{i}", 0) * st.get(f"bq{i}", 0)
            s_val = st.get(f"sp{i}", 0) * st.get(f"sq{i}", 0)
            weight = math.exp(-0.5 * (i - 1))
            mlofi_val += (b_val - s_val) * weight
            
        # Log to Shadow
        log_entry = {
            "ts": datetime.now().isoformat(),
            "sym": sym,
            "lp": lp,
            "bucket_val": round(self.cur_tot_val.get(sym, 0), 0),
            "tpin_raw": round(tpin, 3),
            "tpin_avg": round(avg_tpin, 3),
            "branching": round(branching, 3),
            "duration": round(duration, 1),
            "micro_dev": round(micro_dev, 6),
            "mlofi_val": round(mlofi_val, 0)
        }
        with open(self.LOG_FILE, "a") as f:
            f.write(json.dumps(log_entry) + "\n")
            
        # Reset bucket
        self.cur_buy_val[sym] = 0.0
        self.cur_sell_val[sym] = 0.0
        self.cur_tot_val[sym] = 0.0
        st["bts"] = nows


def attach_cascade_engine(broker, token_to_symbol):
    engine = CascadeValueEngine(token_to_symbol)
    target = None
    candidates = []
    for attr in ("market_data", "mdp", "market_data_provider", "_mdp", "_api", "api", "session_provider"):
        obj = getattr(broker, attr, None)
        if obj is not None:
            candidates.append(obj)
            for sub_attr in ("_api", "api", "_mdp", "mdp"):
                sub = getattr(obj, sub_attr, None)
                if sub is not None:
                    candidates.append(sub)
    candidates.append(broker)
    for c in candidates:
        if hasattr(c, "add_tick_listener"):
            target = c
            break
    if target:
        target.add_tick_listener(engine.on_tick)
        logger.info("Cascade Value Engine attached (Turnover-Driven Shadow Mode).")
    return engine
