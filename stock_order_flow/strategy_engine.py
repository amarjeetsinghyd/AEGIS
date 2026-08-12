"""Strategy engine — v11.5 PURE-QUANT + REGIME + UNIFIED SELECTION + D-TREND v2.
v11.1: StructureMap removed; pure-quant middle layer (M1 VWAP z, M7 jump-share,
M3 CVD term, M5 Kyle's Lambda) + scanner bias gate. Setups A/B/D.
v11.2 (2026-08-12 live-calibrated, Blueprint v2.0 §3 Phase 0):
  §3.1 VWAP Regime Bias Gate (Setup B side control), §3.2 Delta Extremity Guard,
  §3.3 Realized Volatility Gate (dead-regime conf lift), §3.4 Anti-chase
  dislocation (0.50%), + regime attribution logging.
v11.3: HARD PRIORITY REMOVED. Unified selection = confidence + data-calibrated
  setup prior (setup_priors.json, nightly). Strongest signal wins.
v11.4: Setup D trend alignment replaced with EMA9-premium expansion.
v11.5 (2026-08-12, Calibrated): Flow based on Turnover Value. Adaptive ATR-based
  regime filter. Adaptive volatility-based D-Trend lookback."""
from __future__ import annotations

from datetime import datetime, time as dtime
from collections import deque
import json
import math
import os

import numpy as np
import pandas as pd
import talib

from common.config import get_data_dir
from common.logger import get_logger
from stock_order_flow.historical_data_manager import HISTORY_DIR

logger = get_logger("stock_strategy")

FLOW_EPS = 1e-9
MAX_DELTA_HIST = 10
CVD_WINDOW = 4
JUMP_EVENT_TH = 0.25
KYLE_WINDOW = 30
KYLE_Z_TH = 1.0
SOFT_CAP = 0.15

# ---- v11.2 REGIME LAYER ----
REGIME_VOL_BARS = 20
REGIME_MIN_RANGE_PCT = 0.20
REGIME_DEAD_LIFT = 0.10
REGIME_MAX_DISLOC_PCT = 0.50
REGIME_DELTA_EXTREME = 0.90

# ---- v11.3 UNIFIED SELECTION ----
SETUP_PRIOR_DEFAULT = {"B": 0.05, "A": 0.0, "D": 0.0}

# ---- v11.4 SETUP D TREND (EMA9 premium expansion) ----
D_TREND_LOOKBACK = 3   # sweet-spot on 3m bars; tune 3-5

SHADOW_DIR = os.path.join(get_data_dir(), "logs")
os.makedirs(SHADOW_DIR, exist_ok=True)


class StockStructure:
    def __init__(self):
        self.vwap = 0.0
        self.upper = 0.0
        self.lower = 0.0
        self.ema9 = 0.0
        self.ema200 = 0.0
        self.day_high = 0.0
        self.day_low = 0.0
        self.poc = 0.0
        self.vah = 0.0
        self.val = 0.0
        self.delta_hist = []

    def update_from_df(self, df: pd.DataFrame):
        if df is None or df.empty:
            return
        d = df.copy()
        if "timestamp" not in d.columns:
            return
        d["timestamp"] = pd.to_datetime(d["timestamp"])
        d = d.sort_values("timestamp")
        if len(d) < 2:
            return
        d["date"] = d["timestamp"].dt.date
        tp = (d["high"] + d["low"] + d["close"]) / 3.0
        d["cumvol"] = d.groupby("date")["volume"].cumsum()
        d["cumpv"] = (tp * d["volume"]).groupby(d["date"]).cumsum()
        d["cumhighvol"] = (d["high"] * d["volume"]).groupby(d["date"]).cumsum()
        d["cumlowvol"] = (d["low"] * d["volume"]).groupby(d["date"]).cumsum()
        d["vwap"] = d["cumpv"] / d["cumvol"].replace(0, np.nan)
        d["upper"] = d["cumhighvol"] / d["cumvol"].replace(0, np.nan)
        d["lower"] = d["cumlowvol"] / d["cumvol"].replace(0, np.nan)
        d["day_high"] = d.groupby("date")["high"].cummax()
        d["day_low"] = d.groupby("date")["low"].cummin()
        d["ema9"] = talib.EMA(d["close"].astype(float).values, timeperiod=9)
        d["ema200"] = talib.EMA(d["close"].astype(float).values, timeperiod=200)
        last = d.iloc[-1]
        self.vwap = float(last["vwap"]) if pd.notna(last["vwap"]) else 0.0
        self.upper = float(last["upper"]) if pd.notna(last["upper"]) else 0.0
        self.lower = float(last["lower"]) if pd.notna(last["lower"]) else 0.0
        self.ema9 = float(last["ema9"]) if pd.notna(last["ema9"]) else 0.0
        self.ema200 = float(last["ema200"]) if pd.notna(last["ema200"]) else 0.0
        self.day_high = float(last["day_high"]) if pd.notna(last["day_high"]) else 0.0
        self.day_low = float(last["day_low"]) if pd.notna(last["day_low"]) else 0.0
        self._update_volume_profile(d, last["date"])

    def _update_volume_profile(self, df: pd.DataFrame, today):
        try:
            tdf = df[df["date"] == today]
            if len(tdf) < 5:
                return
            day_high = float(tdf["high"].max())
            day_low = float(tdf["low"].min())
            rng = day_high - day_low
            if rng <= 0:
                return
            nbins = 24
            step = rng / nbins
            hist = np.zeros(nbins, dtype=float)
            for r in tdf.itertuples(index=False):
                v = float(getattr(r, "volume", 0.0))
                if v <= 0:
                    continue
                lo = float(getattr(r, "low"))
                hi = float(getattr(r, "high"))
                if hi <= lo:
                    idx = min(nbins - 1, max(0, int((float(getattr(r, "close")) - day_low) / step)))
                    hist[idx] += v
                    continue
                b0 = max(0, int((lo - day_low) / step))
                b1 = min(nbins - 1, int((hi - day_low) / step))
                per = v / (b1 - b0 + 1)
                for b in range(b0, b1 + 1):
                    hist[b] += per
            total = float(hist.sum())
            if total <= 0:
                return
            pocidx = int(np.argmax(hist))
            self.poc = day_low + (pocidx + 0.5) * step
            target = 0.70 * total
            acc = float(hist[pocidx])
            up = pocidx
            dn = pocidx
            while acc < target and (up < nbins - 1 or dn > 0):
                upv = hist[up + 1] if up < nbins - 1 else -1.0
                dnv = hist[dn - 1] if dn > 0 else -1.0
                if upv >= dnv:
                    up += 1
                    acc += hist[up]
                else:
                    dn -= 1
                    acc += hist[dn]
            self.vah = min(day_high, day_low + (up + 1) * step)
            self.val = max(day_low, day_low + dn * step)
        except Exception as e:
            logger.debug(f"Volume profile compute failed: {e}")

    def context_state(self, price: float) -> str:
        prox = max(0.0020 * price, 0.01)
        extrem = max(0.0015 * price, 0.01)
        if self.upper <= 0 or pd.isna(self.upper):
            return "NO_MANS_LAND"
        if self.lower - prox <= price <= self.upper + prox:
            return "IN_ZONE"
        if abs(price - self.day_high) <= extrem or abs(price - self.day_low) <= extrem:
            return "AT_EXTREME"
        return "NO_MANS_LAND"


class StockStrategyEngine:
    def __init__(self):
        self.structures = {}
        self.veto_until = {}
        self._day_key = None
        self._cvd = {}
        self._cvd_snap = {}
        self._shadow_log = []
        self._last_scores = {}
        self._hist15_cache = {}
        self._bias_map = {}
        self._setup_prior = dict(SETUP_PRIOR_DEFAULT)
        self._svol = {}
        self._lam_hist = {}

    def roll_day(self, now: datetime):
        d = now.date()
        if self._day_key != d:
            self._day_key = d
            self._cvd = {}
            self._cvd_snap = {}
            self.veto_until = {}
            self._shadow_log = []
            self._last_scores = {}
            self._hist15_cache = {}
            self._bias_map = self._load_bias_map()
            self._setup_prior = self._load_priors()
            self._svol = {}
            self._lam_hist = {}

    def _load_bias_map(self):
        m = {}
        try:
            p = os.path.join(get_data_dir(), "universe.json")
            if os.path.exists(p):
                with open(p, "r") as f:
                    u = json.load(f)
                for s in u.get("stocks", []):
                    m[s.get("symbol")] = s.get("bias_tag", "TWO_WAY")
        except Exception as e:
            logger.debug(f"bias map load failed: {e}")
        return m

    def _bias_adjust(self, symbol, signal):
        tag = self._bias_map.get(symbol, "TWO_WAY")
        if tag == "TWO_WAY":
            return 0.0, tag
        pro = (tag == "BULLISH_VOL" and signal == "BUY") or (tag == "BEARISH_VOL" and signal == "SELL")
        return (0.05 if pro else -0.10), tag

    def _load_priors(self):
        p = os.path.join(get_data_dir(), "config", "setup_priors.json")
        try:
            if os.path.exists(p):
                with open(p, "r") as f:
                    m = json.load(f)
                return {k: float(v) for k, v in m.items()}
        except Exception as e:
            logger.debug(f"setup priors load failed: {e}")
        return dict(SETUP_PRIOR_DEFAULT)

    def _hist15_df(self, symbol):
        if symbol in self._hist15_cache:
            return self._hist15_cache[symbol]
        df15 = None
        try:
            path = os.path.join(HISTORY_DIR, f"{symbol}.parquet")
            if os.path.exists(path):
                d = pd.read_parquet(path)
                d["timestamp"] = pd.to_datetime(d["timestamp"])
                d = d.sort_values("timestamp")
                d = d[d["timestamp"] < pd.Timestamp(datetime.now().date())]
                if len(d) >= 20:
                    r = (d.set_index("timestamp")[["open", "high", "low", "close", "volume"]]
                         .resample("15min", closed="right", label="right")
                         .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
                         .dropna())
                    if len(r) >= 16:
                        df15 = r.reset_index()
        except Exception as e:
            logger.debug(f"hist15 load failed {symbol}: {e}")
        self._hist15_cache[symbol] = df15
        return df15

    def _atr15(self, bar_book_15, symbol, price: float = 0.0):
        df15 = bar_book_15.completed_df(symbol, min_bars=16)
        if df15 is not None and len(df15) >= 16:
            a = talib.ATR(df15["high"].values.astype(float), df15["low"].values.astype(float), df15["close"].values.astype(float), timeperiod=14)
            v = float(a[-1]) if len(a) and not math.isnan(a[-1]) else 0.0
            if v > 0:
                return v
        h = self._hist15_df(symbol)
        if h is not None and len(h) >= 16:
            a = talib.ATR(h["high"].values.astype(float), h["low"].values.astype(float), h["close"].values.astype(float), timeperiod=14)
            v = float(a[-1]) if len(a) and not math.isnan(a[-1]) else 0.0
            if v > 0:
                return v
        if h is not None and len(h) and price > 0:
            try:
                hd = pd.to_datetime(h["timestamp"]).dt.date
                tdf = h[hd == hd.max()]
                if len(tdf):
                    pd_range = float(tdf["high"].max() - tdf["low"].min())
                    proxy = max(0.5 * pd_range / 14.0, price * 0.005)
                    if pd_range < 0.015 * price:
                        proxy *= 0.80
                    elif pd_range > 0.035 * price:
                        proxy *= 1.20
                    return proxy
            except Exception:
                pass
        return 0.0

    def _vwap_zscore(self, price, vwap, closes):
        if vwap <= 0 or len(closes) < 20:
            return None
        rets = np.diff(np.log(np.maximum(closes[-31:], 1e-9)))
        sigma = float(np.std(rets))
        if sigma <= 1e-9:
            return None
        return ((price - vwap) / vwap) / sigma

    def _jump_share(self, closes, window=20):
        if len(closes) < window + 1:
            return 0.0
        rets = np.diff(np.log(np.maximum(closes[-(window + 1):], 1e-9)))
        rv = float(np.sum(rets ** 2))
        if rv <= 1e-12:
            return 0.0
        bv = float((math.pi / 2.0) * np.sum(np.abs(rets[1:]) * np.abs(rets[:-1])))
        return float(max(0.0, rv - bv) / rv)

    def _cvd_term(self, snaps):
        if len(snaps) < 12:
            return "NEUTRAL"
        cvds = np.array([s[0] for s in snaps], dtype=float)

        def _slope(a):
            n = len(a)
            if n < 2:
                return 0.0
            x = np.arange(n, dtype=float)
            xm, ym = x.mean(), a.mean()
            denom = float(np.sum((x - xm) ** 2))
            return float(np.sum((x - xm) * (a - ym)) / denom) if denom > 1e-9 else 0.0

        s_short = _slope(cvds[-4:])
        s_long = _slope(cvds[-12:])
        if s_short > 0 and s_long > 0:
            return "BULL_ALIGNED"
        if s_short < 0 and s_long < 0:
            return "BEAR_ALIGNED"
        if s_short * s_long < 0:
            return "DIVERGENT"
        return "MIXED"

    def _kyle_lambda_z(self, symbol, closes, window=KYLE_WINDOW):
        svol = self._svol.get(symbol)
        if svol is None or len(svol) < window or len(closes) < window + 1:
            return None
        rets = np.diff(np.log(np.maximum(closes[-(window + 1):], 1e-9)))
        x = np.array(list(svol)[-window:], dtype=float)
        y = rets[-window:]
        dx = x - x.mean()
        denom = float(np.sum(dx * dx))
        if denom <= 1e-18:
            return None
        lam = float(np.sum(dx * (y - y.mean())) / denom)
        hist = self._lam_hist.setdefault(symbol, deque(maxlen=60))
        z = None
        if len(hist) >= 10:
            arr = np.array(hist, dtype=float)
            mu, sd = float(arr.mean()), float(arr.std())
            if sd > 1e-18:
                z = (lam - mu) / sd
        hist.append(lam)
        return z

    def _m_adjust(self, signal, setup_kind, z, jump_share, term_state, kyle_z):
        adj = 0.0
        if z is not None:
            if signal == "BUY" and z <= -2.0:
                adj += 0.05
            elif signal == "SELL" and z >= 2.0:
                adj += 0.05
            elif signal == "BUY" and z >= 2.0:
                adj -= 0.05
            elif signal == "SELL" and z <= -2.0:
                adj -= 0.05
        if jump_share > JUMP_EVENT_TH:
            if setup_kind in ("A", "D"):
                adj += 0.06
            elif setup_kind == "B":
                adj -= 0.05
        if term_state == "BULL_ALIGNED" and signal == "BUY":
            adj += 0.08
        elif term_state == "BEAR_ALIGNED" and signal == "SELL":
            adj += 0.08
        elif term_state == "DIVERGENT" and setup_kind == "B":
            adj += 0.05
        if kyle_z is not None:
            if kyle_z > KYLE_Z_TH:
                if setup_kind in ("A", "D"):
                    adj += 0.06
                elif setup_kind == "B":
                    adj -= 0.04
            elif kyle_z < -KYLE_Z_TH:
                if setup_kind == "B":
                    adj += 0.06
                elif setup_kind in ("A", "D"):
                    adj -= 0.04
        return adj

    def _regime_gate(self, signal, kind, c, struct, norm_delta, disloc_pct, dead):
        lift = REGIME_DEAD_LIFT if dead else 0.0
        if kind == "B":
            base = 0.65 + lift
            if struct.vwap > 0:
                if signal == "BUY" and not (c > struct.vwap):
                    return False, base, "VWAP_BIAS"
                if signal == "SELL" and not (c < struct.vwap):
                    return False, base, "VWAP_BIAS"
                if disloc_pct > REGIME_MAX_DISLOC_PCT:
                    return False, base, "OVEREXTENDED"
            if abs(norm_delta) > REGIME_DELTA_EXTREME:
                return False, base, "DELTA_EXTREME"
            return True, base, "OK"
        if kind == "A":
            return True, 0.60 + lift, "OK"
        if kind == "D":
            return True, 0.60 + lift, "OK"
        return True, 0.65 + lift, "OK"

    def _shadow(self, record: dict):
        self._shadow_log.append(record)
        if len(self._shadow_log) > 1000:
            self._shadow_log = self._shadow_log[-1000:]
        try:
            day = datetime.now().strftime("%Y-%m-%d")
            with open(os.path.join(SHADOW_DIR, f"shadow_log_{day}.jsonl"), "a") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception as e:
            logger.debug(f"shadow persist failed: {e}")

    def get_shadow_log(self):
        return list(self._shadow_log)

    def _impact_efficiency(self, close, prev_close, delta, atr):
        denom = max(abs(delta), 1.0)
        move = abs(close - prev_close)
        scale = atr if atr > 0 else max(close * 0.005, 1.0)
        return (move / denom) / max(scale, FLOW_EPS)

    def _regime_weight(self, df, norm_delta, ctx):
        if df is None or len(df) < 20:
            return 0.0, 0.0
        closes = df["close"].astype(float).values
        rets = np.diff(closes) / np.maximum(closes[:-1], FLOW_EPS)
        vol = float(np.std(rets[-20:])) if len(rets) >= 5 else float(np.std(rets))
        delta_scale = max(0.05, min(0.30, vol * 20.0))
        return delta_scale, vol

    def evaluate_all(self, footprints: dict, bar_book, bar_book_15, now: datetime, smap=None):
        self.roll_day(now)
        candidates = []
        for symbol, fpdata in footprints.items():
            df = bar_book.completed_df(symbol, min_bars=2)
            if df is None or len(df) < 2:
                continue
            if symbol not in self.structures:
                self.structures[symbol] = StockStructure()
            struct = self.structures[symbol]
            struct.update_from_df(df)
            c = float(df.iloc[-1]["close"])
            o = float(df.iloc[-1]["open"])
            prev_c = float(df.iloc[-2]["close"])
            closes = df["close"].astype(float).values
            ctx = struct.context_state(c)
            atr15 = self._atr15(bar_book_15, symbol, c)
            total_buy = float(fpdata.get("total_buy_vol", 0.0))
            total_sell = float(fpdata.get("total_sell_vol", 0.0))
            flow = total_buy + total_sell
            norm_delta = (total_buy - total_sell) / flow if flow > 0 else 0.0
            struct.delta_hist.append(norm_delta)
            if len(struct.delta_hist) > MAX_DELTA_HIST:
                struct.delta_hist.pop(0)
            delta_trend = float(np.mean(struct.delta_hist[-3:])) if struct.delta_hist else 0.0
            avg_ask = float(fpdata.get("ew_l2_ask", 0.0) or fpdata.get("avg_l2_ask", 0.0))
            avg_bid = float(fpdata.get("ew_l2_bid", 0.0) or fpdata.get("avg_l2_bid", 0.0))
            bid_persist = float(fpdata.get("bid_persistence", 0.5))
            ask_persist = float(fpdata.get("ask_persistence", 0.5))
            l2_ratio = (avg_bid + 1.0) / (avg_ask + 1.0)
            candledelta = total_buy - total_sell
            self._svol.setdefault(symbol, deque(maxlen=64)).append(candledelta)
            cvd = self._cvd.get(symbol, 0.0) + candledelta
            self._cvd[symbol] = cvd
            snaps = self._cvd_snap.setdefault(symbol, [])
            snaps.append((cvd, c, norm_delta))
            if len(snaps) > 30:
                snaps.pop(0)
            window_delta = 0.0
            div_buy = False
            div_sell = False
            if len(snaps) >= CVD_WINDOW:
                cvd_prev, close_prev, _ = snaps[-CVD_WINDOW]
                window_delta = cvd - cvd_prev
                div_buy = window_delta < 0 and c >= close_prev * 0.999
                div_sell = window_delta > 0 and c <= close_prev * 1.001
            progress = abs(c - o) / max(c, FLOW_EPS)
            avg_vol_3m = float(df["volume"].tail(20).mean()) if len(df) >= 5 else 0.0
            min_flow_val = max(25_00_000.0, 0.25 * avg_vol_3m * c)
            thin_flow = (flow * c) < min_flow_val
            min_flow = min_flow_val / c if c > 0 else 500.0 # Just for logging compatibility

            # v11.5: Adaptive Setup D Lookback based on volatility
            volatility_pct = atr15 / c if c > 0 else 0
            d_lookback = 2 if volatility_pct > 0.0035 else (4 if volatility_pct < 0.0015 else D_TREND_LOOKBACK)

            ema9_arr = talib.EMA(closes, timeperiod=9)
            gap_now = c - struct.ema9
            if len(ema9_arr) > d_lookback and not math.isnan(ema9_arr[-1 - d_lookback]):
                gap_prev = float(closes[-1 - d_lookback]) - float(ema9_arr[-1 - d_lookback])
            else:
                gap_prev = 0.0
            d_trend_up = (gap_now > 0) and (gap_now > gap_prev)   # above EMA9 + gap expanding
            d_trend_dn = (gap_now < 0) and (gap_now < gap_prev)   # below EMA9 + gap expanding

            # v11.5 regime state (ATR based)
            r_highs = df["high"].astype(float).tail(REGIME_VOL_BARS).values
            r_lows = df["low"].astype(float).tail(REGIME_VOL_BARS).values
            range_1h = float(np.max(r_highs) - np.min(r_lows)) if len(r_highs) else 0.0
            range_pct = range_1h / c * 100.0 if c > 0 else 0.0
            dead = range_1h < (1.5 * atr15) if atr15 > 0 else (range_pct < REGIME_MIN_RANGE_PCT)
            disloc_pct = abs(c - struct.vwap) / c * 100.0 if struct.vwap > 0 else 0.0

            z = self._vwap_zscore(c, struct.vwap, closes)
            jump_share = self._jump_share(closes)
            term_state = self._cvd_term(snaps)
            kyle_z = self._kyle_lambda_z(symbol, closes)
            bias_tag = self._bias_map.get(symbol, "TWO_WAY")
            metrics = {
                "ctx": ctx, "norm_delta": round(norm_delta, 4), "delta_trend": round(delta_trend, 4),
                "cvd": round(cvd, 1), "cvd_window_delta": round(window_delta, 1),
                "cvd_div_buy": bool(div_buy), "cvd_div_sell": bool(div_sell),
                "poc": round(struct.poc, 2), "vah": round(struct.vah, 2), "val": round(struct.val, 2),
                "vwap": round(struct.vwap, 2), "upper": round(struct.upper, 2), "lower": round(struct.lower, 2),
                "atr": round(atr15, 4), "l2_bid_ask": round(l2_ratio, 3),
                "l2_bid_persist": round(bid_persist, 3), "l2_ask_persist": round(ask_persist, 3),
                "impact_eff": round(self._impact_efficiency(c, prev_c, candledelta, atr15), 6),
                "flow": round(flow, 1), "thin_flow": bool(thin_flow),
                "z": round(z, 3) if z is not None else None,
                "jump_share": round(jump_share, 3), "term_state": term_state,
                "kyle_z": round(kyle_z, 3) if kyle_z is not None else None,
                "gap_now": round(gap_now, 4), "gap_prev": round(gap_prev, 4),
                "bias_tag": bias_tag, "time": now.strftime("%H:%M"),
                "range_1hr_pct": round(range_pct, 3), "dead_regime": bool(dead),
                "disloc_pct": round(disloc_pct, 3),
            }
            delta_scale, intraday_vol = self._regime_weight(df, norm_delta, ctx)
            shadow_common = {
                "ts": now.isoformat(), "symbol": symbol, "ctx": ctx, "close": c, "open": o,
                "norm_delta": norm_delta, "delta_trend": delta_trend, "atr": atr15, "metrics": metrics,
            }
            if thin_flow:
                self._shadow({**shadow_common, "status": "thin_flow_skip", "flow": round(flow, 1), "min_flow": round(min_flow, 1)})
            if ctx == "AT_EXTREME" and abs(norm_delta) > 0.20 and progress < 0.0010:
                self.veto_until[symbol] = now.timestamp() + 300
                self._shadow({**shadow_common, "status": "setup_c_veto"})
                continue
            if now.timestamp() < self.veto_until.get(symbol, 0.0):
                continue
            if ctx == "IN_ZONE" and abs(norm_delta) > max(0.08, delta_scale) and not thin_flow:
                uptrend = c > struct.ema9 and c > struct.ema200 if struct.ema200 > 0 else c > struct.ema9
                downtrend = c < struct.ema9 and c < struct.ema200 if struct.ema200 > 0 else c < struct.ema9
                # Setup B — Absorption (regime-gated)
                if norm_delta < 0 and delta_trend < 0 and div_buy:
                    allowed, min_conf, rreason = self._regime_gate("BUY", "B", c, struct, norm_delta, disloc_pct, dead)
                    if not allowed:
                        self._shadow({**shadow_common, "status": "regime_reject", "setup": "B",
                                      "signal": "BUY", "reason": rreason,
                                      "range_pct": round(range_pct, 3), "disloc_pct": round(disloc_pct, 3)})
                    else:
                        delta_score = min(1.0, abs(norm_delta) / max(0.20, delta_scale))
                        progress_score = max(0.0, 1.0 - (progress / 0.0015))
                        l2_score = max(0.0, min(1.0, l2_ratio / 2.0)) * (0.5 + 0.5 * bid_persist)
                        impact_score = max(0.0, min(1.0, 1.0 - min(1.0, self._impact_efficiency(c, prev_c, candledelta, atr15) * 10.0)))
                        conf = min(1.0, 0.30 * delta_score + 0.26 * progress_score + 0.26 * l2_score + 0.18 * impact_score)
                        if conf >= min_conf:
                            candidates.append((0, symbol, "BUY", "B", f"Setup B Absorption+CVD-div. Delta {norm_delta*100:.1f}%, Conf {conf:.2f}", metrics, conf))
                        else:
                            self._shadow({**shadow_common, "status": "setup_b_low_conf", "confidence": conf})
                elif norm_delta > 0 and delta_trend > 0 and div_sell:
                    allowed, min_conf, rreason = self._regime_gate("SELL", "B", c, struct, norm_delta, disloc_pct, dead)
                    if not allowed:
                        self._shadow({**shadow_common, "status": "regime_reject", "setup": "B",
                                      "signal": "SELL", "reason": rreason,
                                      "range_pct": round(range_pct, 3), "disloc_pct": round(disloc_pct, 3)})
                    else:
                        delta_score = min(1.0, abs(norm_delta) / max(0.20, delta_scale))
                        progress_score = max(0.0, 1.0 - (progress / 0.0015))
                        l2_score = max(0.0, min(1.0, l2_ratio / 2.0)) * (0.5 + 0.5 * ask_persist)
                        impact_score = max(0.0, min(1.0, 1.0 - min(1.0, self._impact_efficiency(c, prev_c, candledelta, atr15) * 10.0)))
                        conf = min(1.0, 0.30 * delta_score + 0.26 * progress_score + 0.26 * l2_score + 0.18 * impact_score)
                        if conf >= min_conf:
                            candidates.append((0, symbol, "SELL", "B", f"Setup B Absorption+CVD-div. Delta {norm_delta*100:.1f}%, Conf {conf:.2f}", metrics, conf))
                        else:
                            self._shadow({**shadow_common, "status": "setup_b_low_conf", "confidence": conf})
                # Setup A — Breakout (POC guard; dead-regime floor)
                if c > struct.upper and uptrend and norm_delta > 0 and delta_trend > 0:
                    allowed, min_conf, rreason = self._regime_gate("BUY", "A", c, struct, norm_delta, disloc_pct, dead)
                    if not allowed:
                        self._shadow({**shadow_common, "status": "regime_reject", "setup": "A",
                                      "signal": "BUY", "reason": rreason, "range_pct": round(range_pct, 3)})
                    elif struct.poc > 0 and c < struct.poc:
                        self._shadow({**shadow_common, "status": "setup_a_blocked_poc"})
                    else:
                        delta_score = min(1.0, norm_delta / max(0.15, delta_scale))
                        trend_score = min(1.0, max(0.0, ((c - struct.ema200) / struct.ema200) / 0.02)) if struct.ema200 > 0 else 0.5
                        l2_score = max(0.0, 1.0 - (avg_ask / max(avg_bid * 2.0, 1.0)))
                        vp_bonus = 0.15 if struct.vah > 0 and c > struct.vah else 0.0
                        acceptance = max(0.0, min(1.0, 1.0 - abs(c - struct.upper) / max(atr15 if atr15 > 0 else c * 0.005, FLOW_EPS)))
                        conf = min(1.0, 0.26 * delta_score + 0.24 * trend_score + 0.24 * l2_score + 0.12 * vp_bonus + 0.14 * acceptance)
                        if conf >= min_conf:
                            candidates.append((1, symbol, "BUY", "A", f"Setup A Breakout Up. Delta {norm_delta*100:.1f}%, Conf {conf:.2f}", metrics, conf))
                        else:
                            self._shadow({**shadow_common, "status": "setup_a_low_conf", "confidence": conf, "direction": "BUY"})
                elif c < struct.lower and downtrend and norm_delta < 0 and delta_trend < 0:
                    allowed, min_conf, rreason = self._regime_gate("SELL", "A", c, struct, norm_delta, disloc_pct, dead)
                    if not allowed:
                        self._shadow({**shadow_common, "status": "regime_reject", "setup": "A",
                                      "signal": "SELL", "reason": rreason, "range_pct": round(range_pct, 3)})
                    elif struct.poc > 0 and c > struct.poc:
                        self._shadow({**shadow_common, "status": "setup_a_blocked_poc"})
                    else:
                        delta_score = min(1.0, abs(norm_delta) / max(0.15, delta_scale))
                        trend_score = min(1.0, max(0.0, ((struct.ema200 - c) / struct.ema200) / 0.02)) if struct.ema200 > 0 else 0.5
                        l2_score = max(0.0, 1.0 - (avg_bid / max(avg_ask * 2.0, 1.0)))
                        vp_bonus = 0.15 if struct.val > 0 and c < struct.val else 0.0
                        acceptance = max(0.0, min(1.0, 1.0 - abs(c - struct.lower) / max(atr15 if atr15 > 0 else c * 0.005, FLOW_EPS)))
                        conf = min(1.0, 0.26 * delta_score + 0.24 * trend_score + 0.24 * l2_score + 0.12 * vp_bonus + 0.14 * acceptance)
                        if conf >= min_conf:
                            candidates.append((1, symbol, "SELL", "A", f"Setup A Breakout Down. Delta {norm_delta*100:.1f}%, Conf {conf:.2f}", metrics, conf))
                        else:
                            self._shadow({**shadow_common, "status": "setup_a_low_conf", "confidence": conf, "direction": "SELL"})
                # Setup D — Imbalance v2 (EMA9 premium expansion trend; v11.4)
                if abs(norm_delta) > 0.35:
                    delta_score = min(1.0, abs(norm_delta) / 0.50)
                    persist_ok = (norm_delta > 0 and delta_trend > 0) or (norm_delta < 0 and delta_trend < 0)
                    if persist_ok and delta_score >= 0.60:
                        d_conf = min(1.0, 0.60 * delta_score + 0.40 * min(1.0, abs(norm_delta) / 0.60))
                        if norm_delta > 0 and c > o and d_trend_up:
                            allowed, min_conf, rreason = self._regime_gate("BUY", "D", c, struct, norm_delta, disloc_pct, dead)
                            if not allowed:
                                self._shadow({**shadow_common, "status": "regime_reject", "setup": "D",
                                              "signal": "BUY", "reason": rreason, "range_pct": round(range_pct, 3)})
                            elif d_conf >= min_conf:
                                candidates.append((2, symbol, "BUY", "D", f"Setup D Imbalance+EMA9-exp. Delta {norm_delta*100:.1f}%, Conf {d_conf:.2f}", metrics, d_conf))
                            else:
                                self._shadow({**shadow_common, "status": "setup_d_low_conf", "confidence": d_conf})
                        elif norm_delta < 0 and c < o and d_trend_dn:
                            allowed, min_conf, rreason = self._regime_gate("SELL", "D", c, struct, norm_delta, disloc_pct, dead)
                            if not allowed:
                                self._shadow({**shadow_common, "status": "regime_reject", "setup": "D",
                                              "signal": "SELL", "reason": rreason, "range_pct": round(range_pct, 3)})
                            elif d_conf >= min_conf:
                                candidates.append((2, symbol, "SELL", "D", f"Setup D Imbalance+EMA9-exp. Delta {norm_delta*100:.1f}%, Conf {d_conf:.2f}", metrics, d_conf))
                            else:
                                self._shadow({**shadow_common, "status": "setup_d_low_conf", "confidence": d_conf})
                        else:
                            self._shadow({**shadow_common, "status": "setup_d_trend_miss", "confidence": delta_score})
                    else:
                        self._shadow({**shadow_common, "status": "setup_d_low_conf", "confidence": delta_score})
            elif not thin_flow:
                self._shadow({**shadow_common, "status": "context_reject"})
            self._last_scores[symbol] = metrics
        # ---- middle-layer + bias adjustments ----
        adjusted = []
        for (prio, sym, sig, kind, reason, met, conf) in candidates:
            m_adj = self._m_adjust(sig, kind, met.get("z"), met.get("jump_share", 0.0),
                                   met.get("term_state", "NEUTRAL"), met.get("kyle_z"))
            b_adj, tag = self._bias_adjust(sym, sig)
            total_adj = max(-SOFT_CAP, min(SOFT_CAP, m_adj + b_adj))
            new_conf = max(0.0, min(1.0, conf + total_adj))
            met["m_adj"] = round(m_adj, 3)
            met["bias_adj"] = round(b_adj, 3)
            met["total_adj"] = round(total_adj, 3)
            self._shadow({"ts": now.isoformat(), "symbol": sym, "status": "adjusted_candidate",
                          "signal": sig, "setup": kind, "base_conf": round(conf, 3),
                          "m_adj": round(m_adj, 3), "bias_adj": round(b_adj, 3),
                          "total_adj": round(total_adj, 3), "final_conf": round(new_conf, 3),
                          "z": met.get("z"), "jump_share": met.get("jump_share"),
                          "term_state": met.get("term_state"), "kyle_z": met.get("kyle_z"),
                          "bias_tag": tag})
            adjusted.append((prio, sym, sig, kind, reason, met, new_conf))
        candidates = adjusted
        # ---- heartbeat / UNIFIED selection (v11.3) ----
        if not candidates:
            hot_sym, hot_m = None, None
            for sym, m in self._last_scores.items():
                if hot_m is None or abs(float(m.get("norm_delta", 0))) > abs(float(hot_m.get("norm_delta", 0))):
                    hot_sym, hot_m = sym, m
            if hot_sym:
                logger.info(
                    f"[heartbeat] {now.strftime('%H:%M')} quiet | hottest {hot_sym}: "
                    f"ctx={hot_m.get('ctx')} d={float(hot_m.get('norm_delta', 0))*100:.1f}% "
                    f"atr={hot_m.get('atr')} z={hot_m.get('z')} jump={hot_m.get('jump_share')} "
                    f"term={hot_m.get('term_state')} kyle={hot_m.get('kyle_z')} "
                    f"thin={hot_m.get('thin_flow')} bias={hot_m.get('bias_tag')} "
                    f"range1h={hot_m.get('range_1hr_pct')} dead={hot_m.get('dead_regime')}"
                )
            return None, None, None, {}
        candidates.sort(key=lambda x: -(x[6] + self._setup_prior.get(x[3], 0.0)))
        best = candidates[0]
        for cand in candidates[1:]:
            self._shadow({"ts": now.isoformat(), "symbol": cand[1], "status": "rejected_candidate",
                          "signal": cand[2], "reason": cand[4], "confidence": cand[6],
                          "prior": self._setup_prior.get(cand[3], 0.0), "metrics": cand[5]})
        self._shadow({"ts": now.isoformat(), "symbol": best[1], "status": "selected_candidate",
                      "signal": best[2], "reason": best[4], "confidence": best[6],
                      "prior": self._setup_prior.get(best[3], 0.0), "metrics": best[5]})
        return best[1], best[2], best[4], best[5]