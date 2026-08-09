"""Strategy engine — final. VWAP zones, Volume Profile, CVD hard-gate absorption,
confidence scoring, continuous trend score, tie-breaking, ATR15 from 15m book."""
from datetime import datetime
import math
from common.logger import get_logger

logger = get_logger("stock_strategy")
import pandas as pd
import talib


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
        df['ema9'] = talib.EMA(df['close'], timeperiod=9)
        df['ema200'] = talib.EMA(df['close'], timeperiod=200)
        df['tp'] = (df['high'] + df['low'] + df['close']) / 3.0
        df['date'] = df['timestamp'].dt.date
        df['cum_vol'] = df.groupby('date')['volume'].cumsum()
        df['cum_pv'] = (df['tp'] * df['volume']).groupby(df['date']).cumsum()
        df['vwap'] = df['cum_pv'] / df['cum_vol']
        df['cum_high_vol'] = (df['high'] * df['volume']).groupby(df['date']).cumsum()
        df['cum_low_vol'] = (df['low'] * df['volume']).groupby(df['date']).cumsum()
        df['upper'] = df['cum_high_vol'] / df['cum_vol']
        df['lower'] = df['cum_low_vol'] / df['cum_vol']
        df['day_high'] = df.groupby('date')['high'].cummax()
        df['day_low'] = df.groupby('date')['low'].cummin()
        last_row = df.iloc[-1]
        self.vwap = float(last_row['vwap'])
        self.upper = float(last_row['upper'])
        self.lower = float(last_row['lower'])
        self.ema9 = float(last_row['ema9'])
        self.ema200 = float(last_row['ema200']) if not pd.isna(last_row['ema200']) else 0.0
        self.day_high = float(last_row['day_high'])
        self.day_low = float(last_row['day_low'])
        self._update_volume_profile(df, last_row['date'])

    def _update_volume_profile(self, df, today):
        try:
            tdf = df[df['date'] == today]
            if len(tdf) < 5:
                return
            day_high = float(tdf['high'].max())
            day_low = float(tdf['low'].min())
            rng = day_high - day_low
            if rng <= 0:
                return
            n_bins = 24
            step = rng / n_bins
            hist = [0.0] * n_bins
            for r in tdf.itertuples(index=False):
                v = float(r.volume)
                if v <= 0:
                    continue
                lo = float(r.low)
                hi = float(r.high)
                if hi <= lo:
                    b = min(n_bins - 1, max(0, int((float(r.close) - day_low) / step)))
                    hist[b] += v
                    continue
                b0 = max(0, int((lo - day_low) / step))
                b1 = min(n_bins - 1, int((hi - day_low) / step))
                per = v / (b1 - b0 + 1)
                for b in range(b0, b1 + 1):
                    hist[b] += per
            total = sum(hist)
            if total <= 0:
                return
            poc_idx = max(range(n_bins), key=lambda i: hist[i])
            self.poc = day_low + (poc_idx + 0.5) * step
            target = 0.70 * total
            acc = hist[poc_idx]
            up = poc_idx
            dn = poc_idx
            while acc < target and (up < n_bins - 1 or dn > 0):
                up_v = hist[up + 1] if up < n_bins - 1 else -1.0
                dn_v = hist[dn - 1] if dn > 0 else -1.0
                if up_v >= dn_v:
                    up += 1
                    acc += hist[up]
                else:
                    dn -= 1
                    acc += hist[dn]
            self.vah = min(day_high, day_low + (up + 1) * step)
            self.val = max(day_low, day_low + dn * step)
        except Exception as e:
            logger.debug(f"Volume profile compute failed: {e}")

    def context_state(self, price):
        PROX = price * 0.0020
        EXT = price * 0.0015
        if self.upper <= 0 or pd.isna(self.upper):
            return 'NO_MANS_LAND'
        if (self.lower - PROX) <= price <= (self.upper + PROX):
            return 'IN_ZONE'
        if abs(price - self.day_high) <= EXT or abs(price - self.day_low) <= EXT:
            return 'AT_EXTREME'
        return 'NO_MANS_LAND'


class StockStrategyEngine:
    def __init__(self):
        self.structures = {}
        self.veto_until = {}
        self._day_key = None
        self._cvd = {}
        self._cvd_snap = {}

    def _roll_day(self, now):
        d = now.date()
        if self._day_key != d:
            self._day_key = d
            self._cvd = {}
            self._cvd_snap = {}

    @staticmethod
    def _atr15(bar_book_15, symbol):
        df15 = bar_book_15.completed_df(symbol, min_bars=16)
        if df15 is None or len(df15) < 16:
            return 0.0
        a = talib.ATR(df15['high'].values, df15['low'].values, df15['close'].values, timeperiod=14)
        if len(a) == 0 or math.isnan(a[-1]):
            return 0.0
        return float(a[-1])

    def evaluate_all(self, footprints, bar_book, bar_book_15, now):
        """Returns (best_symbol, signal, reason, metrics) or (None, None, None, {})."""
        self._roll_day(now)
        candidates = []
        for symbol, fp_data in footprints.items():
            df = bar_book.completed_df(symbol, min_bars=2)
            if df is None or len(df) < 2:
                continue
            if symbol not in self.structures:
                self.structures[symbol] = StockStructure()
            struct = self.structures[symbol]
            struct.update_from_df(df)
            c = float(df.iloc[-1]['close'])
            ctx = struct.context_state(c)
            atr15 = self._atr15(bar_book_15, symbol)

            total_buy = fp_data.get("total_buy_vol", 0)
            total_sell = fp_data.get("total_sell_vol", 0)
            flow = total_buy + total_sell
            norm_delta = (total_buy - total_sell) / flow if flow > 0 else 0.0

            struct.prev_delta = norm_delta
            struct.delta_hist.append(norm_delta)
            if len(struct.delta_hist) > 10:
                struct.delta_hist.pop(0)
            delta_trend = sum(struct.delta_hist[-3:]) / min(3, len(struct.delta_hist)) if struct.delta_hist else 0.0

            avg_ask = fp_data.get("avg_l2_ask", 0)
            avg_bid = fp_data.get("avg_l2_bid", 0)

            # CVD accumulation + divergence
            candle_delta = total_buy - total_sell
            cvd = self._cvd.get(symbol, 0.0) + candle_delta
            self._cvd[symbol] = cvd
            snaps = self._cvd_snap.setdefault(symbol, [])
            snaps.append((cvd, c))
            if len(snaps) > 40:
                snaps.pop(0)
            window_delta = 0.0
            div_buy = False
            div_sell = False
            if len(snaps) >= 4:
                cvd_prev, close_prev = snaps[-4]
                window_delta = cvd - cvd_prev
                div_buy = (window_delta < 0) and (c >= close_prev * 0.999)
                div_sell = (window_delta > 0) and (c <= close_prev * 1.001)

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
                "l2_bid_ask": round(avg_bid / avg_ask, 3) if avg_ask > 0 else 0.0,
                "time": now.strftime("%H:%M"),
            }

            # Setup C: Veto Shield
            if ctx == 'AT_EXTREME' and abs(norm_delta) > 0.20:
                prog = abs(c - float(df.iloc[-1]['open'])) / c
                if prog < 0.0010:
                    self.veto_until[symbol] = now.timestamp() + 300
                    logger.info(f"{symbol}: Setup C VETO ACTIVE (Exhaustion)")
                    continue
            veto_active = self.veto_until.get(symbol, 0) > now.timestamp()

            # Setup B: Absorption (CVD divergence = HARD GATE) + confidence
            if ctx == 'IN_ZONE' and abs(norm_delta) > 0.10:
                prog = abs(c - float(df.iloc[-1]['open'])) / c
                if norm_delta < 0 and delta_trend < 0 and div_buy:
                    delta_score = min(1.0, abs(norm_delta) / 0.20)
                    progress_score = max(0.0, 1.0 - (prog / 0.0015))
                    l2_score = min(1.0, (avg_bid / avg_ask) / 2.0) if avg_ask > 0 else 0.0
                    conf = min(1.0, delta_score * 0.35 + progress_score * 0.30 + l2_score * 0.35)
                    if conf >= 0.65:
                        candidates.append((0, symbol, "BUY",
                                           f"Setup B Absorption+CVD-div: Delta {norm_delta*100:.1f}%, Conf {conf:.2f}",
                                           metrics, conf))
                if norm_delta > 0 and delta_trend > 0 and div_sell:
                    delta_score = min(1.0, abs(norm_delta) / 0.20)
                    progress_score = max(0.0, 1.0 - (prog / 0.0015))
                    l2_score = min(1.0, (avg_ask / avg_bid) / 2.0) if avg_bid > 0 else 0.0
                    conf = min(1.0, delta_score * 0.35 + progress_score * 0.30 + l2_score * 0.35)
                    if conf >= 0.65:
                        candidates.append((0, symbol, "SELL",
                                           f"Setup B Absorption+CVD-div: Delta {norm_delta*100:.1f}%, Conf {conf:.2f}",
                                           metrics, conf))

            if veto_active:
                continue

            # Setup A: Breakout + POC guard + VAH/VAL confluence + continuous trend
            if ctx == 'IN_ZONE' and abs(norm_delta) > 0.08:
                up_trend = c > struct.ema9 and c > struct.ema200
                down_trend = c < struct.ema9 and c < struct.ema200

                if c > struct.upper and up_trend and norm_delta > 0 and delta_trend > 0:
                    if avg_ask < avg_bid * 2:
                        if struct.poc > 0 and c < struct.poc:
                            logger.info(f"{symbol}: Setup A BUY blocked — close below POC (overhead supply).")
                        else:
                            delta_score = min(1.0, norm_delta / 0.15)
                            trend_score = min(1.0, max(0.0, ((c - struct.ema200) / struct.ema200) / 0.02)) if struct.ema200 > 0 else 0.0
                            l2_score = max(0.0, 1.0 - (avg_ask / (avg_bid * 2))) if avg_bid > 0 else 0.0
                            vp_bonus = 0.0
                            vp_tag = ""
                            if struct.vah > 0 and c > struct.vah:
                                vp_bonus = 0.15
                                vp_tag = " [VP: above VAH]"
                            conf = min(1.0, delta_score * 0.30 + trend_score * 0.25 + l2_score * 0.25 + vp_bonus)
                            if conf >= 0.60:
                                candidates.append((1, symbol, "BUY",
                                                   f"Setup A Breakout (Up). Delta {norm_delta*100:.1f}%, Conf {conf:.2f}{vp_tag}",
                                                   metrics, conf))
                elif c < struct.lower and down_trend and norm_delta < 0 and delta_trend < 0:
                    if avg_bid < avg_ask * 2:
                        if struct.poc > 0 and c > struct.poc:
                            logger.info(f"{symbol}: Setup A SELL blocked — close above POC (underhead demand).")
                        else:
                            delta_score = min(1.0, abs(norm_delta) / 0.15)
                            trend_score = min(1.0, max(0.0, ((struct.ema200 - c) / struct.ema200) / 0.02)) if struct.ema200 > 0 else 0.0
                            l2_score = max(0.0, 1.0 - (avg_bid / (avg_ask * 2))) if avg_ask > 0 else 0.0
                            vp_bonus = 0.0
                            vp_tag = ""
                            if struct.val > 0 and c < struct.val:
                                vp_bonus = 0.15
                                vp_tag = " [VP: below VAL]"
                            conf = min(1.0, delta_score * 0.30 + trend_score * 0.25 + l2_score * 0.25 + vp_bonus)
                            if conf >= 0.60:
                                candidates.append((1, symbol, "SELL",
                                                   f"Setup A Breakout (Down). Delta {norm_delta*100:.1f}%, Conf {conf:.2f}{vp_tag}",
                                                   metrics, conf))

            # Setup D: Stacked Imbalance (explicit threshold)
            if abs(norm_delta) > 0.40:
                delta_score = min(1.0, abs(norm_delta) / 0.50)
                if delta_score >= 0.60:
                    if norm_delta > 0 and c > float(df.iloc[-1]['open']):
                        candidates.append((2, symbol, "BUY",
                                           f"Setup D Imbalance. Delta {norm_delta*100:.1f}%, Conf {delta_score:.2f}",
                                           metrics, delta_score))
                    elif norm_delta < 0 and c < float(df.iloc[-1]['open']):
                        candidates.append((2, symbol, "SELL",
                                           f"Setup D Imbalance. Delta {norm_delta*100:.1f}%, Conf {delta_score:.2f}",
                                           metrics, delta_score))

        if not candidates:
            return None, None, None, {}
        candidates.sort(key=lambda x: (x[0], -x[5]))
        best = candidates[0]
        return best[1], best[2], best[3], best[4]