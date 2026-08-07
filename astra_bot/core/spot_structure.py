"""
Astra V4.3 — Spot Structure Engine (shadow-first).
Amarjeet की मैनुअल प्लेबुक का स्टेट-मशीन रूप:
  red    = session VWAP (synthetic Nifty-50 constituent volume)
  yellow = VWAP ± band (2.5 buffer) -> trigger / rejection / SL boundary
  blue   = 9 EMA  -> trailing exit (reverse break)
  grey   = 200 EMA -> macro regime filter
Events (हर 1m candle close पर):
  BREAKDOWN / BREAKOUT / REJECT_PUT / REJECT_CALL / NONE
"""
import threading
from collections import deque
from common.logger import get_logger

logger = get_logger("spot_structure")


class SpotStructureEngine:
    def __init__(self, band_pts: float = 5.0, memory_min: float = 5.0, orb_candles: int = 15):
        self.band = band_pts
        self.memory_sec = memory_min * 60.0
        self.orb_n = orb_candles
        self.lock = threading.Lock()
        self.candles = deque(maxlen=480)
        self.cur = None
        self.vwap_num = self.vwap_den = 0.0
        self.vwap = self.upper = self.lower = 0.0
        self.ema9 = self.ema200 = 0.0
        self.orb_high = self.orb_low = 0.0
        self.candle_count = 0
        self.last_ts = 0
        self.last_above_vwap = self.last_below_vwap = 0.0
        self.put_zone_ts = self.call_zone_ts = 0.0
        self.last_event = "NONE"

    # ---------------- ingestion ----------------
    def on_tick(self, ts_epoch: float, price: float):
        if price <= 0:
            return
        minute = int(ts_epoch // 60) * 60
        with self.lock:
            self._maybe_reset(ts_epoch)
            if self.cur is None or self.cur["ts"] != minute:
                if self.cur is not None:
                    self._close_candle(self.cur)
                self.cur = {"ts": minute, "o": price, "h": price, "l": price, "c": price, "vol": 0.0}
            else:
                c = self.cur
                c["h"] = max(c["h"], price)
                c["l"] = min(c["l"], price)
                c["c"] = price

    def add_volume(self, minute_ts: int, qty: float):
        """दूसरे बॉट वाले synthetic constituent volume feed से।"""
        if qty <= 0:
            return
        with self.lock:
            if self.cur is not None and self.cur["ts"] == minute_ts:
                self.cur["vol"] += qty

    # ---------------- internals ----------------
    def _maybe_reset(self, ts):
        if self.last_ts and (ts - self.last_ts) > 20 * 3600:
            logger.info("[SpotStructure] New session — resetting VWAP/EMA/ORB")
            self.vwap_num = self.vwap_den = 0.0
            self.vwap = self.upper = self.lower = 0.0
            self.ema9 = self.ema200 = 0.0
            self.orb_high = self.orb_low = 0.0
            self.candle_count = 0
            self.cur = None
            self.last_above_vwap = self.last_below_vwap = 0.0
            self.put_zone_ts = self.call_zone_ts = 0.0
            self.last_event = "NONE"
        self.last_ts = ts

    def _close_candle(self, c):
        self.candles.append(c)
        self.candle_count += 1
        if self.candle_count <= self.orb_n:
            self.orb_high = max(self.orb_high, c["h"])
            self.orb_low = min(self.orb_low, c["l"]) if self.orb_low else c["l"]
        tp = (c["h"] + c["l"] + c["c"]) / 3.0
        vol = c["vol"] if c["vol"] > 0 else 1.0  # vol न मिले तो time-weight fallback
        self.vwap_num += tp * vol
        self.vwap_den += vol
        self.vwap = self.vwap_num / self.vwap_den
        self.upper = self.vwap + self.band
        self.lower = self.vwap - self.band
        k9, k200 = 2.0 / 10.0, 2.0 / 201.0
        self.ema9 = c["c"] if self.ema9 == 0 else c["c"] * k9 + self.ema9 * (1 - k9)
        self.ema200 = c["c"] if self.ema200 == 0 else c["c"] * k200 + self.ema200 * (1 - k200)
        self.last_event = self._evaluate(c)

    def _evaluate(self, c):
        if self.vwap <= 0 or self.candle_count < 3:
            return "NONE"
        vwap, upper, lower, now = self.vwap, self.upper, self.lower, c["ts"]
        ev = "NONE"
        # (a) single-candle fake-breakout rejection: विक VWAP पार, क्लोज़ लो-बैंड नीचे
        if c["h"] >= vwap and c["c"] < lower:
            ev = "REJECT_PUT"
        elif c["l"] <= vwap and c["c"] > upper:
            ev = "REJECT_CALL"
        # (b) breakdown/breakout: हाल में VWAP के पार था, अब दूर के बैंड के बाहर क्लोज़
        elif self.last_above_vwap and (now - self.last_above_vwap) <= self.memory_sec and c["c"] < lower:
            ev = "BREAKDOWN"
        elif self.last_below_vwap and (now - self.last_below_vwap) <= self.memory_sec and c["c"] > upper:
            ev = "BREAKOUT"
        # (c) two-step rejection: ज़ोन-टच (≤5 मिनट), रीक्लेम नहीं, अब बैंड के बाहर क्लोज़
        elif self.put_zone_ts and (now - self.put_zone_ts) <= self.memory_sec and c["c"] < lower:
            ev = "REJECT_PUT"
        elif self.call_zone_ts and (now - self.call_zone_ts) <= self.memory_sec and c["c"] > upper:
            ev = "REJECT_CALL"
        # ---- state updates ----
        if c["c"] > vwap:
            self.last_above_vwap = now
            self.put_zone_ts = 0.0  # रीक्लेम PUT-मेमोरी मारता है
        elif c["c"] < vwap:
            self.last_below_vwap = now
            self.call_zone_ts = 0.0
        if c["h"] >= lower and c["c"] < vwap:
            self.put_zone_ts = now  # "कीमत ज़ोन में पार कर चुकी है"
        if c["l"] <= upper and c["c"] > vwap:
            self.call_zone_ts = now
        return ev

    # ---------------- public reads ----------------
    def regime(self, price: float) -> str:
        if self.ema200 <= 0 or self.vwap <= 0:
            return "CHOP"
        if price > self.ema200 and price > self.vwap:
            return "BULL"
        if price < self.ema200 and price < self.vwap:
            return "BEAR"
        return "CHOP"  # दोनों के बीच = no-trade zone

    def trail_exit(self, side: str, close: float) -> bool:
        """9 EMA रिवर्स-ब्रेक (आपका मैनुअल ट्रेल)।"""
        if self.ema9 <= 0:
            return False
        return (side == "CALL" and close < self.ema9) or (side == "PUT" and close > self.ema9)

    def invalidation(self, side: str, close: float) -> bool:
        """आपका high/low VWAP SL: विपरीत बैंड क्लोज़।"""
        if self.upper <= 0:
            return False
        return (side == "CALL" and close < self.lower) or (side == "PUT" and close > self.upper)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "vwap": round(self.vwap, 2),
                "upper": round(self.upper, 2),
                "lower": round(self.lower, 2),
                "ema9": round(self.ema9, 2),
                "ema200": round(self.ema200, 2),
                "orb_high": round(self.orb_high, 2),
                "orb_low": round(self.orb_low, 2),
                "last_event": self.last_event,
            }
