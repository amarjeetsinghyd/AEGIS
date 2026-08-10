"""Strategy engine — final v7. VWAP zones, Volume Profile, CVD hard-gate absorption,
confidence scoring, regime-aware thresholds, shadow logging,
end-weighted + persistence-aware L2 consumption."""
from __future__ import annotations
from datetime import datetime, timedelta
import math
from collections import defaultdict
import numpy as np
import pandas as pd
import talib
from common.logger import get_logger

logger = get_logger("stock_strategy")

FLOW_EPS = 1e-9
MAX_DELTA_HIST = 10
CVD_WINDOW = 4
SHADOW_WINDOW = 5


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
        self.prev_delta = 0.0
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

    def roll_day(self, now: datetime):
        d = now.date()
        if self._day_key != d:
            self._day_key = d
            self._cvd = {}
            self._cvd_snap = {}
            self.veto_until = {}
            self._shadow_log = []
            self._last_scores = {}

    @staticmethod
    def _atr15(bar_book_15, symbol):
        df15 = bar_book_15.completed_df(symbol, min_bars=16)
        if df15 is None or len(df15) < 16:
            return 0.0
        a = talib.ATR(df15["high"].values.astype(float), df15["low"].values.astype(float), df15["close"].values.astype(float), timeperiod=14)
        return float(a[-1]) if len(a) and not math.isnan(a[-1]) else 0.0

    def _shadow(self, record: dict):
        self._shadow_log.append(record)
        if len(self._shadow_log) > 1000:
            self._shadow_log = self._shadow_log[-1000:]

    def get_shadow_log(self):
        return list(self._shadow_log)

    def _flow_quality(self, fpdata: dict) -> float:
        total = float(fpdata.get("total_buy_vol", 0.0)) + float(fpdata.get("total_sell_vol", 0.0))
        if total <= 0:
            return 0.0
        l2b = float(fpdata.get("ew_l2_bid", 0.0) or fpdata.get("avg_l2_bid", 0.0))
        l2a = float(fpdata.get("ew_l2_ask", 0.0) or fpdata.get("avg_l2_ask", 0.0))
        l2_ratio = (l2b + 1.0) / (l2a + 1.0)
        return max(0.0, min(1.0, l2_ratio / 2.0))

    def _impact_efficiency(self, close: float, prev_close: float, delta: float, atr: float) -> float:
        denom = max(abs(delta), 1.0)
        move = abs(close - prev_close)
        scale = atr if atr > 0 else max(close * 0.005, 1.0)
        return (move / denom) / max(scale, FLOW_EPS)

    def _regime_weight(self, df: pd.DataFrame, norm_delta: float, ctx: str) -> tuple:
        if df is None or len(df) < 20:
            return 0.0, 0.0
        closes = df["close"].astype(float).values
        rets = np.diff(closes) / np.maximum(closes[:-1], FLOW_EPS)
        vol = float(np.std(rets[-20:])) if len(rets) >= 5 else float(np.std(rets))
        delta_scale = max(0.05, min(0.30, vol * 20.0))
        return delta_scale, vol

    def evaluate_all(self, footprints: dict, bar_book, bar_book_15, now: datetime):
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
            ctx = struct.context_state(c)
            atr15 = self._atr15(bar_book_15, symbol)
            total_buy = float(fpdata.get("total_buy_vol", 0.0))
            total_sell = float(fpdata.get("total_sell_vol", 0.0))
            flow = total_buy + total_sell
            norm_delta = (total_buy - total_sell) / flow if flow > 0 else 0.0
            struct.prev_delta = norm_delta
            struct.delta_hist.append(norm_delta)
            if len(struct.delta_hist) > MAX_DELTA_HIST:
                struct.delta_hist.pop(0)
            delta_trend = float(np.mean(struct.delta_hist[-3:])) if struct.delta_hist else 0.0

            # v7: end-weighted L2 (fallback to legacy average) + persistence
            avg_ask = float(fpdata.get("ew_l2_ask", 0.0) or fpdata.get("avg_l2_ask", 0.0))
            avg_bid = float(fpdata.get("ew_l2_bid", 0.0) or fpdata.get("avg_l2_bid", 0.0))
            bid_persist = float(fpdata.get("bid_persistence", 0.5))
            ask_persist = float(fpdata.get("ask_persistence", 0.5))
            l2_ratio = (avg_bid + 1.0) / (avg_ask + 1.0)

            candledelta = total_buy - total_sell
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
            veto_active = now.timestamp() < self.veto_until.get(symbol, 0.0)
            if veto_active:
                self._shadow({"ts": now.isoformat(), "symbol": symbol, "status": "veto_active", "ctx": ctx, "norm_delta": norm_delta})
                continue

            metrics = {
                "ctx": ctx,
                "norm_delta": round(norm_delta, 4),
                "delta_trend": round(delta_trend, 4),
                "cvd": round(cvd, 1),
                "cvd_window_delta": round(window_delta, 1),
                "cvd_div_buy": bool(div_buy),
                "cvd_div_sell": bool(div_sell),
                "poc": round(struct.poc, 2),
                "vah": round(struct.vah, 2),
                "val": round(struct.val, 2),
                "vwap": round(struct.vwap, 2),
                "upper": round(struct.upper, 2),
                "lower": round(struct.lower, 2),
                "atr": round(atr15, 4),
                "l2_bid_ask": round(l2_ratio, 3),
                "l2_bid_persist": round(bid_persist, 3),
                "l2_ask_persist": round(ask_persist, 3),
                "impact_eff": round(self._impact_efficiency(c, prev_c, candledelta, atr15), 6),
                "time": now.strftime("%H:%M"),
            }
            delta_scale, intraday_vol = self._regime_weight(df, norm_delta, ctx)
            shadow_common = {
                "ts": now.isoformat(),
                "symbol": symbol,
                "ctx": ctx,
                "close": c,
                "open": o,
                "norm_delta": norm_delta,
                "delta_trend": delta_trend,
                "atr": atr15,
                "metrics": metrics,
            }
            if ctx == "AT_EXTREME" and abs(norm_delta) > 0.20 and progress < 0.0010:
                self.veto_until[symbol] = now.timestamp() + 300
                self._shadow({**shadow_common, "status": "setup_c_veto"})
                continue

            if ctx == "IN_ZONE" and abs(norm_delta) > max(0.08, delta_scale):
                uptrend = c > struct.ema9 and c > struct.ema200 if struct.ema200 > 0 else c > struct.ema9
                downtrend = c < struct.ema9 and c < struct.ema200 if struct.ema200 > 0 else c < struct.ema9

                # Setup B: absorption + impact efficiency + CVD hard gate + persistence
                if norm_delta < 0 and delta_trend < 0 and div_buy:
                    delta_score = min(1.0, abs(norm_delta) / max(0.20, delta_scale))
                    progress_score = max(0.0, 1.0 - (progress / 0.0015))
                    l2_score = max(0.0, min(1.0, l2_ratio / 2.0)) * (0.5 + 0.5 * bid_persist)
                    impact_score = max(0.0, min(1.0, 1.0 - min(1.0, self._impact_efficiency(c, prev_c, candledelta, atr15) * 10.0)))
                    conf = min(1.0, 0.28 * delta_score + 0.24 * progress_score + 0.26 * l2_score + 0.22 * impact_score)
                    if conf >= 0.65:
                        reason = f"Setup B Absorption+CVD-div. Delta {norm_delta*100:.1f}%, Conf {conf:.2f}"
                        candidates.append((0, symbol, "BUY", reason, metrics, conf))
                    else:
                        self._shadow({**shadow_common, "status": "setup_b_low_conf", "confidence": conf})
                elif norm_delta > 0 and delta_trend > 0 and div_sell:
                    delta_score = min(1.0, abs(norm_delta) / max(0.20, delta_scale))
                    progress_score = max(0.0, 1.0 - (progress / 0.0015))
                    l2_score = max(0.0, min(1.0, l2_ratio / 2.0)) * (0.5 + 0.5 * ask_persist)
                    impact_score = max(0.0, min(1.0, 1.0 - min(1.0, self._impact_efficiency(c, prev_c, candledelta, atr15) * 10.0)))
                    conf = min(1.0, 0.28 * delta_score + 0.24 * progress_score + 0.26 * l2_score + 0.22 * impact_score)
                    if conf >= 0.65:
                        reason = f"Setup B Absorption+CVD-div. Delta {norm_delta*100:.1f}%, Conf {conf:.2f}"
                        candidates.append((0, symbol, "SELL", reason, metrics, conf))
                    else:
                        self._shadow({**shadow_common, "status": "setup_b_low_conf", "confidence": conf})

                # Setup A: breakout with acceptance and regime-aware scoring
                if c > struct.upper and uptrend and norm_delta > 0 and delta_trend > 0:
                    if struct.poc > 0 and c < struct.poc:
                        self._shadow({**shadow_common, "status": "setup_a_blocked_poc"})
                    else:
                        delta_score = min(1.0, norm_delta / max(0.15, delta_scale))
                        trend_score = min(1.0, max(0.0, ((c - struct.ema200) / struct.ema200) / 0.02)) if struct.ema200 > 0 else 0.5
                        l2_score = max(0.0, 1.0 - (avg_ask / max(avg_bid * 2.0, 1.0)))
                        vp_bonus = 0.15 if struct.vah > 0 and c > struct.vah else 0.0
                        acceptance = max(0.0, min(1.0, 1.0 - abs(c - struct.upper) / max(atr15 if atr15 > 0 else c * 0.005, FLOW_EPS)))
                        conf = min(1.0, 0.22 * delta_score + 0.22 * trend_score + 0.24 * l2_score + 0.14 * vp_bonus + 0.18 * acceptance)
                        if conf >= 0.60:
                            tag = "VP above VAH" if vp_bonus > 0 else ""
                            reason = f"Setup A Breakout Up. Delta {norm_delta*100:.1f}%, Conf {conf:.2f} {tag}".strip()
                            candidates.append((1, symbol, "BUY", reason, metrics, conf))
                        else:
                            self._shadow({**shadow_common, "status": "setup_a_low_conf", "confidence": conf, "direction": "BUY"})
                elif c < struct.lower and downtrend and norm_delta < 0 and delta_trend < 0:
                    if struct.poc > 0 and c > struct.poc:
                        self._shadow({**shadow_common, "status": "setup_a_blocked_poc"})
                    else:
                        delta_score = min(1.0, abs(norm_delta) / max(0.15, delta_scale))
                        trend_score = min(1.0, max(0.0, ((struct.ema200 - c) / struct.ema200) / 0.02)) if struct.ema200 > 0 else 0.5
                        l2_score = max(0.0, 1.0 - (avg_bid / max(avg_ask * 2.0, 1.0)))
                        vp_bonus = 0.15 if struct.val > 0 and c < struct.val else 0.0
                        acceptance = max(0.0, min(1.0, 1.0 - abs(c - struct.lower) / max(atr15 if atr15 > 0 else c * 0.005, FLOW_EPS)))
                        conf = min(1.0, 0.22 * delta_score + 0.22 * trend_score + 0.24 * l2_score + 0.14 * vp_bonus + 0.18 * acceptance)
                        if conf >= 0.60:
                            tag = "VP below VAL" if vp_bonus > 0 else ""
                            reason = f"Setup A Breakout Down. Delta {norm_delta*100:.1f}%, Conf {conf:.2f} {tag}".strip()
                            candidates.append((1, symbol, "SELL", reason, metrics, conf))
                        else:
                            self._shadow({**shadow_common, "status": "setup_a_low_conf", "confidence": conf, "direction": "SELL"})

                # Setup D: stacked imbalance, lower-priority impulse evidence
                if abs(norm_delta) > 0.40:
                    delta_score = min(1.0, abs(norm_delta) / 0.50)
                    if delta_score >= 0.60:
                        if norm_delta > 0 and c > o:
                            candidates.append((2, symbol, "BUY", f"Setup D Imbalance. Delta {norm_delta*100:.1f}%, Conf {delta_score:.2f}", metrics, delta_score))
                        elif norm_delta < 0 and c < o:
                            candidates.append((2, symbol, "SELL", f"Setup D Imbalance. Delta {norm_delta*100:.1f}%, Conf {delta_score:.2f}", metrics, delta_score))
                        else:
                            self._shadow({**shadow_common, "status": "setup_d_shape_miss", "confidence": delta_score})
                    else:
                        self._shadow({**shadow_common, "status": "setup_d_low_conf", "confidence": delta_score})
            else:
                self._shadow({**shadow_common, "status": "context_reject"})
            self._last_scores[symbol] = metrics

        if not candidates:
            return None, None, None, {}
        candidates.sort(key=lambda x: (x[0], -x[5]))
        best = candidates[0]
        for cand in candidates[1:]:
            self._shadow({"ts": now.isoformat(), "symbol": cand[1], "status": "rejected_candidate", "signal": cand[2], "reason": cand[3], "confidence": cand[5], "metrics": cand[4]})
        self._shadow({"ts": now.isoformat(), "symbol": best[1], "status": "selected_candidate", "signal": best[2], "reason": best[3], "confidence": best[5], "metrics": best[4]})
        return best[1], best[2], best[3], best[4]
