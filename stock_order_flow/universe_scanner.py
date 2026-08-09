"""Daily universe scanner — standalone pm2 sleep-loop process.
Runs at 16:05 IST on trading days (or one-shot with --now):
ranks NIFTY-100 by recent volatility (NATR7), applies hysteresis buffer,
writes universe.json, pre-fetches 3m history for warm boot.
All data from our own Shoonya REST (daily bars) — no external source.
Gates: NATR7 >= 1.2% (no upper cap), avg_vol >= 10L, close <= Rs 2000 (no lower cap)."""
from __future__ import annotations
import os, sys, json, math, time
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from common.config import configure_runtime, get_data_dir
configure_runtime("stock_order_flow")

from common.broker import ShoonyaAdapter
from common.logger import get_logger
from stock_order_flow.historical_data_manager import fetch_and_seed_history

logger = get_logger("universe_scanner")
import numpy as np
import talib

RUN_TIME = (16, 5)          # daily scan time (IST, post close-auction)
TOP_N = 15                  # final universe size
FORCE_BAND = 12             # newcomers must rank <= 12
KEEP_BAND = 20              # existing members survive if rank <= 20
NATR7_FLOOR = 1.2           # volatility floor (%), NO upper cap
MIN_AVG_VOL = 1_000_000     # 20-day avg volume floor
PRICE_MAX = 2000.0          # upper price cap (NO lower cap)

NIFTY100 = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "HINDUNILVR", "ITC", "SBIN",
    "BHARTIARTL", "KOTAKBANK", "LT", "AXISBANK", "ASIANPAINT", "MARUTI", "TITAN",
    "SUNPHARMA", "ULTRACEMCO", "BAJFINANCE", "WIPRO", "HCLTECH", "NESTLEIND", "M&M",
    "ADANIENT", "ADANIPORTS", "TATAMOTORS", "POWERGRID", "NTPC", "ONGC", "JSWSTEEL",
    "TATASTEEL", "BAJAJFINSV", "TECHM", "GRASIM", "DRREDDY", "BRITANNIA", "APOLLOHOSP",
    "EICHERMOT", "HEROMOTOCO", "BAJAJ-AUTO", "DIVISLAB", "CIPLA", "BPCL", "INDUSINDBK",
    "UPL", "SHRIRAMFIN", "SBILIFE", "HDFCLIFE", "DABUR", "GODREJCP", "MARICO",
    "TRENT", "VBL", "PIDILITIND", "SIEMENS", "ABB", "BAJAJHLDNG", "CHOLAFIN",
    "TVSMOTOR", "DLF", "GODREJPROP", "MACROTECH", "BANKBARODA", "PNB", "CANBK",
    "UNIONBANK", "FEDERALBNK", "IDFCFIRSTB", "AUROPHARMA", "LUPIN", "ALKEM",
    "TORNTPHARM", "MUTHOOTFIN", "HAVELLS", "ICICIPRULI", "ICICIGI", "SBICARD",
    "INFOEDGE", "ZYDUSLIFE", "HAL", "BEL", "IRCTC", "REC", "PFC", "VOLTAS",
    "AMBUJACEM", "ACC", "SHREECEM", "JINDALSTEL", "COALINDIA", "HINDALCO",
    "TATAPOWER", "NMDC", "SAIL", "GAIL", "VEDL", "DMART", "BOSCHLTD", "LTIM",
    "PERSISTENT", "POLYCAB", "JIOFIN", "IRFC", "ZOMATO", "PAYTM", "NYKAA", "POLICYBZR",
]

UNIVERSE_PATH = os.path.join(get_data_dir(), "universe.json")
TOKEN_CACHE_PATH = os.path.join(get_data_dir(), "tokens_cache.json")


def _nan(x):
    return x is None or (isinstance(x, float) and math.isnan(x))


def load_token_cache():
    try:
        if os.path.exists(TOKEN_CACHE_PATH):
            with open(TOKEN_CACHE_PATH, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def resolve_tokens(api, cache):
    """Resolve NIFTY-100 tokens via search_scrip (cached to disk)."""
    for sym in NIFTY100:
        if sym in cache:
            continue
        try:
            r = api.search_scrip("NSE", sym + "-EQ")
            if isinstance(r, list) and r:
                cache[sym] = {"token": str(r[0]["token"]), "tsym": str(r[0]["tsym"])}
            else:
                r2 = api.search_scrip("NSE", sym)
                if isinstance(r2, list) and r2:
                    cache[sym] = {"token": str(r2[0]["token"]), "tsym": str(r2[0]["tsym"])}
        except Exception as e:
            logger.debug(f"search failed for {sym}: {e}")
        time.sleep(0.2)
    try:
        with open(TOKEN_CACHE_PATH, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception:
        pass
    return cache


def fetch_daily(api, token, days=60):
    """Daily bars from our own adapter REST, sorted old -> new."""
    try:
        end = int(datetime.now().timestamp())
        start = int((datetime.now() - timedelta(days=days)).timestamp())
        rows = api.get_time_price_series(exchange="NSE", token=token,
                                         starttime=start, endtime=end, interval="D")
        if not isinstance(rows, list) or len(rows) < 20:
            return None
        recs = []
        for r in rows:
            recs.append({
                "ts": int(r.get("time", 0)),
                "close": float(r.get("intc", 0)),
                "high": float(r.get("inth", 0)),
                "low": float(r.get("intl", 0)),
                "volume": float(r.get("intv") or r.get("v") or 0),
            })
        recs.sort(key=lambda x: x["ts"])
        return recs
    except Exception as e:
        logger.debug(f"daily fetch failed token={token}: {e}")
        return None


def score_symbol(recs):
    """Volatility metrics + hard gates + score (highest recent ATR on top)."""
    close = np.array([r["close"] for r in recs], dtype=float)
    high = np.array([r["high"] for r in recs], dtype=float)
    low = np.array([r["low"] for r in recs], dtype=float)
    vol = np.array([r["volume"] for r in recs], dtype=float)
    if len(close) < 25 or close[-1] <= 0:
        return None
    natr7 = talib.NATR(high, low, close, timeperiod=7)[-1]
    natr14 = talib.NATR(high, low, close, timeperiod=14)[-1]
    atr7 = talib.ATR(high, low, close, timeperiod=7)[-1]
    atr14 = talib.ATR(high, low, close, timeperiod=14)[-1]
    up, mid, lo = talib.BBANDS(close, timeperiod=14)
    if _nan(natr7) or _nan(natr14) or _nan(atr14) or float(atr14) <= 0:
        return None
    natr7 = float(natr7)
    natr14 = float(natr14)
    ratio = float(atr7) / float(atr14)          # volatility expansion (7 vs 14)
    width = (up - lo) / mid
    w = width[~np.isnan(width)]
    squeeze = bool(len(w) > 10 and not math.isnan(float(width[-1]))
                   and float(width[-1]) <= np.percentile(w, 20))
    adx = talib.ADX(high, low, close, timeperiod=14)[-1]
    avg_vol = float(vol[-20:].mean()) if len(vol) >= 20 else float(vol.mean())
    c = float(close[-1])

    # ---- Hard gates: volatility floor + liquidity + upper price cap only ----
    if natr7 < NATR7_FLOOR:                     # recent-heat floor, NO upper cap
        return None
    if avg_vol < MIN_AVG_VOL:                   # liquidity floor
        return None
    if c > PRICE_MAX:                           # Rs 2000 upper cap, NO lower cap
        return None

    # ---- Score: NATR7 base + expansion bonus + squeeze bonus ----
    score = natr7 * (1.0 + max(0.0, ratio - 1.0) * 2.0) + (0.5 if squeeze else 0.0)
    return {
        "score": round(score, 3), "natr7": round(natr7, 2), "natr14": round(natr14, 2),
        "ratio": round(ratio, 2), "squeeze": squeeze,
        "adx": round(float(adx), 1) if not _nan(adx) else 0.0,
        "close": c, "avg_vol": int(avg_vol),
    }


def run_scan(broker):
    api = broker._api if hasattr(broker, "_api") else broker
    cache = resolve_tokens(api, load_token_cache())

    current = []
    try:
        if os.path.exists(UNIVERSE_PATH):
            with open(UNIVERSE_PATH, "r") as f:
                current = [s["symbol"] for s in json.load(f).get("stocks", [])]
    except Exception:
        current = []

    ranked = []
    for sym in NIFTY100:
        meta = cache.get(sym)
        if not meta:
            continue
        recs = fetch_daily(api, meta["token"])
        if recs:
            m = score_symbol(recs)
            if m:
                ranked.append((sym, m))
        time.sleep(0.1)

    ranked.sort(key=lambda x: -x[1]["score"])
    rank_of = {sym: i + 1 for i, (sym, _) in enumerate(ranked)}

    # ---- Hysteresis buffer: stable yet fresh ----
    final = []
    for sym, m in ranked:
        if len(final) >= TOP_N:
            break
        if rank_of[sym] <= FORCE_BAND or (sym in current and rank_of[sym] <= KEEP_BAND):
            final.append((sym, m))
    final_syms = {f[0] for f in final}
    for sym, m in ranked:
        if len(final) >= TOP_N:
            break
        if sym not in final_syms:
            final.append((sym, m))
            final_syms.add(sym)

    stocks = []
    for sym, m in final:
        meta = cache[sym]
        stocks.append({"symbol": sym, "token": meta["token"], "tsym": meta.get("tsym", ""),
                       "search": [meta.get("tsym", sym + "-EQ"), sym], **m})

    with open(UNIVERSE_PATH, "w") as f:
        json.dump({"date": str(datetime.now().date()), "stocks": stocks}, f, indent=2)
    logger.info(f"Universe scan done: {len(stocks)} stocks -> {UNIVERSE_PATH}")
    for s in stocks[:8]:
        logger.info(f"  {s['symbol']}: score={s['score']} natr7={s['natr7']}% ratio={s['ratio']} squeeze={s['squeeze']}")

    # Pre-fetch 3m history for tomorrow (warm boot)
    token_map = {s["token"]: s["symbol"] for s in stocks}
    try:
        fetch_and_seed_history(broker, list(token_map.keys()), token_map)
        logger.info("Pre-fetch of 3m history complete.")
    except Exception as e:
        logger.warning(f"Pre-fetch failed: {e}")


def main():
    logger.info("Universe scanner started (daily 16:05 IST sleep-loop; --now for one-shot)")
    broker = ShoonyaAdapter()
    if hasattr(broker, "session_provider"):
        broker.session_provider._restore_or_login()

    if "--now" in sys.argv:
        try:
            run_scan(broker)
        except Exception as e:
            logger.error(f"Scan failed: {e}")
        logger.info("--now scan complete.")
        return

    last_run = None
    while True:
        try:
            now = datetime.now()
            if (now.weekday() < 5 and last_run != now.date()
                    and (now.hour, now.minute) >= RUN_TIME):
                try:
                    run_scan(broker)
                except Exception as e:
                    logger.error(f"Scan failed: {e}")
                last_run = now.date()
        except Exception as e:
            logger.error(f"Scanner loop error: {e}")
        time.sleep(30)


if __name__ == "__main__":
    main()