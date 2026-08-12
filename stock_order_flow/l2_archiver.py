"""L2 Archiver v1.1 (Q14 + N-research) — passive microstructure recorder.
ZERO impact on trading logic: attaches via broker chain walk (market_data/mdp/_mdp/api),
same pattern as tick_compiler.subscribe.
Outputs:
  data/l2_archive/l2_YYYY-MM-DD.parquet  (snappy, 30d retention)
  data/logs/preopen_*.jsonl              (09:00-09:15 auction imbalance)
  data/logs/closeauction_*.jsonl         (15:30-15:40)
Per-candle research accumulators via get_candle_features() (shadow-only):
large-trade stats, asym ratio, liquidity response, adverse selection,
Pareto tail alpha, microprice deviation. Single-pass, O(1)/tick, RAM-flat."""
from __future__ import annotations
import os, math, json, time as pytime
from collections import deque
from datetime import datetime, time as dtime, timedelta
import numpy as np
import pandas as pd
from common.config import get_data_dir
from common.logger import get_logger

logger = get_logger("l2_archiver")

DATA = get_data_dir()
ARCH_DIR = os.path.join(DATA, "l2_archive")
LOGS = os.path.join(DATA, "logs")
os.makedirs(ARCH_DIR, exist_ok=True)
os.makedirs(LOGS, exist_ok=True)
TICK = 0.05
RETENTION_DAYS = 30


class L2Archiver:
    def __init__(self, token_to_symbol: dict):
        self.t2s = {str(k): str(v) for k, v in token_to_symbol.items()}
        self.prev_vol = {}
        self.last_snap = {}
        self.buffer = []
        self.auction_buf = []
        self.last_flush = pytime.time()
        self.last_aflush = pytime.time()
        self.dv = {}          # per-symbol deque of (ts, dVol, sign)
        self.logsz = {}       # per-symbol deque of log(dVol)
        self.ema_liq = {}
        self.ema_adv = {}
        self.ema_expimp = {}
        self.micro = {}
        self._prune()

    def _prune(self):
        try:
            cut_p = datetime.now() - timedelta(days=RETENTION_DAYS)
            for f in os.listdir(ARCH_DIR):
                if f.endswith(".parquet"):
                    d = f.replace("l2_", "").replace(".parquet", "")
                    if datetime.strptime(d, "%Y-%m-%d") < cut_p:
                        os.remove(os.path.join(ARCH_DIR, f))
            cut_l = datetime.now() - timedelta(days=7)
            for f in os.listdir(LOGS):
                if (f.startswith("preopen_") or f.startswith("closeauction_")) and f.endswith(".jsonl"):
                    d = f.split("_")[1].replace(".jsonl", "")
                    if datetime.strptime(d, "%Y-%m-%d") < cut_l:
                        os.remove(os.path.join(LOGS, f))
        except Exception as e:
            logger.debug(f"prune failed: {e}")

    def on_tick(self, msg):
        try:
            tok = str(msg.get("token", ""))
            sym = self.t2s.get(tok)
            if not sym:
                return
            lp = float(msg.get("last_traded_price") or msg.get("lp") or 0)
            vol = float(msg.get("volume_trade_for_the_day") or msg.get("v") or 0)
            bq1 = float(msg.get("bq1") or 0); sq1 = float(msg.get("sq1") or 0)
            bp1 = float(msg.get("bp1") or 0); sp1 = float(msg.get("sp1") or 0)
            if lp <= 0:
                return
            now = datetime.now(); ts = now.time(); nows = now.timestamp()
            # Microprice
            if bq1 + sq1 > 0 and bp1 > 0 and sp1 > 0:
                self.micro[sym] = (bp1 * sq1 + sp1 * bq1) / (bq1 + sq1)
            # Auction imbalance loggers
            if dtime(9, 0) <= ts < dtime(9, 15) or dtime(15, 30) <= ts < dtime(15, 40):
                sb = sum(float(msg.get(f"bq{i}") or 0) for i in range(1, 6))
                sa = sum(float(msg.get(f"sq{i}") or 0) for i in range(1, 6))
                st = self.last_snap.setdefault(sym, {})
                if nows - st.get("a_ts", 0) >= 2.0:
                    st["a_ts"] = nows
                    self.auction_buf.append({"ts": nows, "sym": sym, "lp": lp,
                                             "imb": (sb - sa) / max(sb + sa, 1.0)})
            # Depth snapshot (on-change + 2s heartbeat)
            st = self.last_snap.setdefault(sym, {})
            chg = (abs(lp - st.get("lp", 0)) >= 2 * TICK or
                   abs(bq1 - st.get("bq1", 0)) >= 0.2 * max(st.get("bq1", 1), 1) or
                   abs(sq1 - st.get("sq1", 0)) >= 0.2 * max(st.get("sq1", 1), 1))
            if chg or nows - st.get("s_ts", 0) >= 2.0:
                st.update({"lp": lp, "bq1": bq1, "sq1": sq1, "s_ts": nows})
                self.buffer.append({
                    "ts": int(nows * 1000), "tok": int(tok) if tok.isdigit() else 0,
                    "lp": np.float32(lp), "bp1": np.float32(bp1), "sp1": np.float32(sp1),
                    "bq1": np.int32(bq1), "sq1": np.int32(sq1),
                    "bq2": np.int32(msg.get("bq2") or 0), "sq2": np.int32(msg.get("sq2") or 0),
                    "bq3": np.int32(msg.get("bq3") or 0), "sq3": np.int32(msg.get("sq3") or 0)})
            # Research accumulators (market hours only)
            dVol = max(0.0, vol - self.prev_vol.get(sym, vol))
            self.prev_vol[sym] = vol
            if dtime(9, 15) <= ts < dtime(15, 30) and dVol > 0:
                sign = 1.0 if (lp >= sp1) else (-1.0 if lp <= bp1 else 0.0)
                self.dv.setdefault(sym, deque(maxlen=400)).append((nows, dVol, sign))
                self.logsz.setdefault(sym, deque(maxlen=200)).append(math.log(dVol))
                # Liquidity response (N3)
                pc = lp - st.get("pc", lp)
                if pc != 0 and (bq1 + sq1) > 0:
                    bdc = bq1 - st.get("pbq", bq1); adc = sq1 - st.get("psq", sq1)
                    lr = (bdc * (1 if pc > 0 else -1) + adc * (-1 if pc > 0 else 1)) / (bq1 + sq1)
                    self.ema_liq[sym] = 0.95 * self.ema_liq.get(sym, 0.0) + 0.05 * lr
                st["pc"], st["pbq"], st["psq"] = lp, bq1, sq1
                # Adverse selection (N4)
                dlp = abs(lp - st.get("plp", lp)); st["plp"] = lp
                imp = dlp / dVol
                expimp = self.ema_expimp.get(sym, imp)
                self.ema_expimp[sym] = 0.98 * expimp + 0.02 * imp
                if expimp > 1e-9:
                    self.ema_adv[sym] = 0.95 * self.ema_adv.get(sym, 1.0) + 0.05 * (imp / expimp)
            # Flushers
            if nows - self.last_flush >= 60 and self.buffer:
                self._flush_parquet(now)
            if nows - self.last_aflush >= 30 and self.auction_buf:
                self._flush_auction(now)
        except Exception as e:
            logger.debug(f"archiver tick error: {e}")

    def _flush_parquet(self, now):
        self.last_flush = pytime.time()
        try:
            df = pd.DataFrame(self.buffer); self.buffer = []
            path = os.path.join(ARCH_DIR, f"l2_{now.strftime('%Y-%m-%d')}.parquet")
            if os.path.exists(path):
                df = pd.concat([pd.read_parquet(path), df], ignore_index=True)
            df.to_parquet(path, index=False, compression="snappy")
        except Exception as e:
            logger.debug(f"parquet flush failed: {e}")

    def _flush_auction(self, now):
        self.last_aflush = pytime.time()
        try:
            kind = "preopen" if now.hour < 12 else "closeauction"
            with open(os.path.join(LOGS, f"{kind}_{now.strftime('%Y-%m-%d')}.jsonl"), "a") as f:
                for r in self.auction_buf:
                    f.write(json.dumps(r) + "\n")
            self.auction_buf = []
        except Exception as e:
            logger.debug(f"auction flush failed: {e}")

    def get_candle_features(self, symbol):
        """Research accumulators for strategy consumption (shadow-only)."""
        out = {"asym_ratio": 0.0, "large_delta": 0.0, "large_n": 0,
               "liq_resp": round(self.ema_liq.get(symbol, 0.0), 4),
               "adv_sel": round(self.ema_adv.get(symbol, 1.0), 3),
               "tail_alpha": 0.0, "micro_dev": 0.0}
        try:
            dq = self.dv.get(symbol)
            if dq and len(dq) >= 20:
                vols = np.array([v for _, v, _ in dq])
                up = sum(v for _, v, s in dq if s > 0)
                dn = sum(v for _, v, s in dq if s < 0)
                out["asym_ratio"] = round(up / max(dn, 1.0), 3)
                p95 = float(np.percentile(vols, 95))
                lb = [(v, s) for _, v, s in dq if v >= p95]
                out["large_n"] = len(lb)
                if len(lb) >= 3:
                    lbu = sum(v for v, s in lb if s > 0); lse = sum(v for v, s in lb if s < 0)
                    out["large_delta"] = round((lbu - lse) / max(lbu + lse, 1.0), 3)
            lz = self.logsz.get(symbol)
            if lz and len(lz) >= 50:
                arr = np.array(list(lz)); mn = arr.min()
                s = arr.sum() - len(arr) * mn
                if s > 1e-9:
                    out["tail_alpha"] = round(1.0 + len(arr) / s, 3)
            mp = self.micro.get(symbol, 0.0)
            st = self.last_snap.get(symbol, {})
            if mp > 0 and st.get("lp", 0) > 0:
                out["micro_dev"] = round((st["lp"] - mp) / mp, 5)
        except Exception:
            pass
        return out


def attach_archiver(broker, token_to_symbol):
    """Attach L2Archiver via broker chain walk (same pattern as tick_compiler).
    Non-fatal: if no listener found, logs warning and returns None."""
    arch = L2Archiver(token_to_symbol)
    # Chain walk to find add_tick_listener
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
    if target is None:
        logger.warning("L2Archiver: could not find add_tick_listener — archiver NOT attached (non-fatal).")
        return None
    target.add_tick_listener(arch.on_tick)
    logger.info("L2Archiver attached (passive; archive + research accumulators).")
    return arch