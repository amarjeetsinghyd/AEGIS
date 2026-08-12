"""Universe scanner v4.2b — GEMINI v3 §5 vol-predictor (talib-free).
v4.1: Noren field parsing via historical_data_manager._parse_rows.
v4.2: jump_ratio from 6-day 3m parquet, alpha penalty 0.25, volume-profile.
v4.2a: moved jump to 15m-resample (MedRV scale bug remained -> jump=1.0).
v4.2b: FIXED — jump uses SUMMED Bipower Variation (π/2)·Σ|r_i||r_{i-1}|,
same scale as RV (dimensionally correct; zero-return tolerant).
Single fast '60' call; daily aggregated in Python; progress logs;
hysteresis 12/20; TOP_N 15."""
from __future__ import annotations

import os, sys, json, math, time
from datetime import datetime, timedelta
import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from common.config import configure_runtime, get_data_dir
from common.broker import ShoonyaAdapter
from common.logger import get_logger
from stock_order_flow.historical_data_manager import fetch_and_seed_history, _parse_rows, HISTORY_DIR

logger = get_logger("universe_scanner")
configure_runtime("stock_order_flow")

RUNTIME_HOUR = 16
RUNTIME_MIN = 5
TOP_N = 15
FORCE_BAND = 12
KEEP_BAND = 20
MIN_AVG_VOL = 0         # Disabled per user request (NIFTY100 is highly liquid)
PRICE_MAX = 999999.0    # Disabled per user request
IDR_FLOOR = 0.008
VOL_FLOOR = 0.20
NIFTY_INDEX_TOKEN = "26000"

NIFTY100 = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "HINDUNILVR", "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK",
    "LT", "AXISBANK", "ASIANPAINT", "MARUTI", "TITAN", "SUNPHARMA", "ULTRACEMCO", "BAJFINANCE", "WIPRO", "HCLTECH",
    "NESTLEIND", "M&M", "ADANIENT", "ADANIPORTS", "TATAMOTORS", "POWERGRID", "NTPC", "ONGC", "JSWSTEEL", "TATASTEEL",
    "BAJAJFINSV", "TECHM", "GRASIM", "DRREDDY", "BRITANNIA", "APOLLOHOSP", "EICHERMOT", "HEROMOTOCO", "BAJAJ-AUTO", "DIVISLAB",
    "CIPLA", "BPCL", "INDUSINDBK", "UPL", "SHRIRAMFIN", "SBILIFE", "HDFCLIFE", "DABUR", "GODREJCP", "MARICO",
    "TRENT", "VBL", "PIDILITIND", "SIEMENS", "ABB", "BAJAJHLDNG", "CHOLAFIN", "TVSMOTOR", "DLF", "GODREJPROP",
    "MACROTECH", "BANKBARODA", "PNB", "CANBK", "UNIONBANK", "FEDERALBNK", "IDFCFIRSTB", "AUROPHARMA", "LUPIN", "ALKEM",
    "TORNTPHARM", "MUTHOOTFIN", "HAVELLS", "ICICIPRULI", "ICICIGI", "SBICARD", "INFOEDGE", "ZYDUSLIFE", "HAL", "BEL",
    "IRCTC", "REC", "PFC", "VOLTAS", "AMBUJACEM", "ACC", "SHREECEM", "JINDALSTEL", "COALINDIA", "HINDALCO",
    "TATAPOWER", "NMDC", "SAIL", "GAIL", "VEDL", "DMART", "BOSCHLTD", "LTIM", "PERSISTENT", "POLYCAB",
    "JIOFIN", "IRFC", "ZOMATO", "PAYTM", "NYKAA", "POLICYBZR",
]

UNIVERSE_PATH = os.path.join(get_data_dir(), "universe.json")
TOKEN_CACHE_PATH = os.path.join(get_data_dir(), "tokens_cache.json")
VOLPROFILE_PATH = os.path.join(get_data_dir(), "vol_profile.json")


def _resolve_api(broker):
    cands = []
    sp = getattr(broker, "session_provider", None)
    for base in (broker, sp):
        if base is None:
            continue
        for attr in ("_api", "api"):
            c = getattr(base, attr, None)
            if c is not None:
                cands.append(c)
    cands.append(broker)
    for c in cands:
        if hasattr(c, "searchscrip") or hasattr(c, "search_scrip"):
            return c
    return cands[0]


def load_token_cache():
    try:
        if os.path.exists(TOKEN_CACHE_PATH):
            with open(TOKEN_CACHE_PATH, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _search_values(api, query):
    for m in ("searchscrip", "search_scrip"):
        fn = getattr(api, m, None)
        if not fn:
            continue
        try:
            r = fn(exchange="NSE", searchtext=query)
        except TypeError:
            try:
                r = fn("NSE", query)
            except Exception:
                continue
        except Exception:
            continue
        if isinstance(r, dict) and r.get("values"):
            return r["values"]
        if isinstance(r, list) and r:
            return r
    return []


def resolve_tokens(api, cache):
    for sym in NIFTY100:
        if sym in cache:
            continue
        for q in (f"{sym}-EQ", sym):
            vals = _search_values(api, q)
            if vals:
                row = vals[0]
                cache[sym] = {"token": str(row.get("token")), "tsym": str(row.get("tsym", sym))}
                break
        time.sleep(0.15)
    try:
        with open(TOKEN_CACHE_PATH, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception:
        pass
    return cache


def _fetch_hourly_rows(api, token, days=45):
    try:
        end = int(datetime.now().timestamp())
        start = int((datetime.now() - timedelta(days=days)).timestamp())
        rows = api.get_time_price_series(exchange="NSE", token=token, starttime=start, endtime=end, interval="60")
        if not isinstance(rows, list) or len(rows) < 20:
            return None
        recs = _parse_rows(rows)
        return recs if len(recs) >= 20 else None
    except Exception as e:
        logger.debug(f"hourly fetch failed token={token}: {e}")
        return None


def _aggregate_daily(hourly):
    daily_map = {}
    for r in hourly:
        ts = r["timestamp"]
        day_key = ts.date() if hasattr(ts, "date") else datetime.fromtimestamp(ts).date()
        if day_key not in daily_map:
            daily_map[day_key] = {"ts": ts, "close": r["close"], "high": r["high"], "low": r["low"], "volume": r["volume"]}
        else:
            d = daily_map[day_key]
            if ts > d["ts"]:
                d["ts"] = ts
                d["close"] = r["close"]
            d["high"] = max(d["high"], r["high"])
            d["low"] = min(d["low"], r["low"])
            d["volume"] += r["volume"]
    return sorted(daily_map.values(), key=lambda x: x["ts"])


def fetch_index_daily(api, days=90):
    try:
        end = int(datetime.now().timestamp())
        start = int((datetime.now() - timedelta(days=days)).timestamp())
        rows = api.get_time_price_series(exchange="NSE", token=NIFTY_INDEX_TOKEN,
                                         starttime=start, endtime=end, interval="60")
        if not isinstance(rows, list) or len(rows) < 20:
            return None
        recs = _parse_rows(rows)
        daily_map = {}
        for r in recs:
            ts = r["timestamp"]
            day_key = ts.date() if hasattr(ts, "date") else datetime.fromtimestamp(ts).date()
            if day_key not in daily_map or ts > daily_map[day_key][0]:
                daily_map[day_key] = (ts, r["close"])
        return np.array([c for _, c in sorted(daily_map.values())], dtype=float)
    except Exception as e:
        logger.debug(f"index daily fetch failed: {e}")
        return None


def _load_parquet(symbol):
    try:
        path = os.path.join(HISTORY_DIR, f"{symbol}.parquet")
        if os.path.exists(path):
            return pd.read_parquet(path)
    except Exception:
        pass
    return None


def _jump_from_3m(df3):
    """v4.2b: Jump share = max(0, RV - BV)/RV on 15m-resampled 3m closes.
    BV = SUMMED bipower variation (π/2)·Σ|r_i||r_{i-1}| — same scale as RV
    (both are window sums ≈ n·σ²). v4.2a's MedRV omitted the n-bar summation
    -> collapsed to ~0 -> jump=1.0 artifact. Summed BV is dimensionally
    correct and zero-return tolerant."""
    try:
        if df3 is None or len(df3) < 100:
            return None
        d = df3.copy()
        d["timestamp"] = pd.to_datetime(d["timestamp"])
        d = d.sort_values("timestamp").set_index("timestamp")
        q = d["close"].resample("15min").last().dropna()
        if len(q) < 40:
            return None
        r = np.diff(np.log(q.values))
        r = r[np.isfinite(r)]
        if len(r) < 30:
            return None
        rv = float(np.sum(r ** 2))
        if rv <= 1e-12:
            return 0.0
        bv = float((math.pi / 2.0) * np.sum(np.abs(r[1:]) * np.abs(r[:-1])))
        return float(max(0.0, rv - bv) / rv)
    except Exception:
        return None


def _volume_profile(df3):
    """v4.2: median volume per 15m bucket across days (for volume-time)."""
    try:
        if df3 is None or len(df3) < 200:
            return None
        d = df3.copy()
        d["timestamp"] = pd.to_datetime(d["timestamp"])
        d["bucket"] = d["timestamp"].dt.hour * 4 + (d["timestamp"].dt.minute // 15)
        d["day"] = d["timestamp"].dt.date
        g = d.groupby(["day", "bucket"])["volume"].sum()
        prof = g.groupby(level="bucket").median()
        return {str(int(k)): float(v) for k, v in prof.items()}
    except Exception:
        return None


def compute_features(hourly, daily, idx_daily=None, jump3m=None):
    dc = np.array([r["close"] for r in daily], dtype=float)
    hc = np.array([r["close"] for r in hourly], dtype=float)
    if len(dc) < 25 or len(hc) < 40 or dc[-1] <= 0:
        return None
    dret = np.diff(np.log(dc))
    hret = np.diff(np.log(hc))
    rv1 = float(math.sqrt(252.0) * abs(dret[-1]))
    rv5 = float(math.sqrt(252.0 * np.mean(dret[-5:] ** 2)))
    rv22 = float(math.sqrt(252.0 * np.mean(dret[-22:] ** 2)))
    hr = hret[-100:]
    tot = float(np.sum(hr ** 2))
    ds_ratio = float(np.sum(hr[hr < 0] ** 2) / tot) if tot > 1e-12 else 0.5
    if jump3m is not None:
        jump_ratio = jump3m
    else:
        bv = float((math.pi / 2.0) * np.sum(np.abs(hr[1:]) * np.abs(hr[:-1])))
        jump_ratio = float(max(0.0, tot - bv) / tot) if tot > 1e-12 else 0.0
    vv20 = float(np.std(math.sqrt(252.0) * np.abs(dret[-20:])))
    corr = 0.0
    if idx_daily is not None and len(idx_daily) >= 25:
        a = dret[-25:]
        b = np.diff(np.log(idx_daily))[-25:]
        if np.std(a) > 1e-12 and np.std(b) > 1e-12:
            corr = float(np.corrcoef(a, b)[0, 1])
    dr = daily[-22:]
    idr22 = float(np.mean([(r["high"] - r["low"]) / r["close"] for r in dr if r["close"] > 0]))
    denom = float(np.sum(np.abs(dret[-22:])))
    trend_eff22 = float(abs(np.sum(dret[-22:])) / denom) if denom > 1e-12 else 0.0
    avg_vol = float(np.mean([r["volume"] for r in dr]))
    return {
        "rv1": round(rv1, 4), "rv5": round(rv5, 4), "rv22": round(rv22, 4),
        "persistence": round(rv5 / rv22, 2) if rv22 > 1e-9 else 0.0,
        "ds_ratio": round(ds_ratio, 3), "us_ratio": round(1.0 - ds_ratio, 3),
        "jump_ratio": round(jump_ratio, 3),
        "vv20": round(vv20, 4),
        "nifty_corr60": round(corr, 3),
        "idr22": round(idr22, 4),
        "trend_eff22": round(trend_eff22, 3),
        "avg_vol": int(avg_vol),
        "close": float(dc[-1]),
    }


def _score_v4(f, vv_med):
    activity = min(1.0, f["rv5"] / 0.30)
    expansion = min(1.5, max(0.5, f["rv5"] / max(f["rv22"], 1e-6))) / 1.5
    stability = min(1.0, vv_med / max(f["vv20"], 1e-6))
    jump_clean = 1.0 - min(1.0, f["jump_ratio"] / 0.30)
    alpha = 1.0 - 0.25 * max(0.0, (f["nifty_corr60"] - 0.6) / 0.4)
    return round(100.0 * (0.30 * activity + 0.25 * expansion + 0.25 * stability
                          + 0.10 * jump_clean + 0.10 * alpha), 3)


def _bias_tag(f, vv_med):
    if f["rv22"] >= VOL_FLOOR and f["us_ratio"] >= 0.55 and f["vv20"] <= vv_med:
        return "BULLISH_VOL"
    if f["ds_ratio"] >= 0.55 and f["vv20"] <= vv_med:
        return "BEARISH_VOL"
    return "TWO_WAY"


def run_scan(broker):
    api = _resolve_api(broker)
    cache = resolve_tokens(api, load_token_cache())
    idx_daily = fetch_index_daily(api)
    current = set()
    try:
        if os.path.exists(UNIVERSE_PATH):
            with open(UNIVERSE_PATH, "r") as f:
                current = {s["symbol"] for s in json.load(f).get("stocks", [])}
    except Exception:
        current = set()

    feats = {}
    profiles = {}
    funnel = {"fetch_fail": 0, "compute_fail": 0, "price_cap": 0, "vol_min": 0, "idr_min": 0, "passed": 0}
    
    for i, sym in enumerate(NIFTY100):
        if i > 0 and i % 10 == 0:
            logger.info(f"  ...scanned {i}/{len(NIFTY100)} symbols")
        try:
            meta = cache.get(sym)
            if not meta:
                continue
            hourly = _fetch_hourly_rows(api, meta["token"])
            if not hourly:
                funnel["fetch_fail"] += 1
                continue
            daily = _aggregate_daily(hourly)
            df3 = _load_parquet(sym)
            f = compute_features(hourly, daily, idx_daily, _jump_from_3m(df3))
            if not f:
                funnel["compute_fail"] += 1
                continue
            if f["close"] > PRICE_MAX:
                funnel["price_cap"] += 1
                continue
            if f["avg_vol"] < MIN_AVG_VOL:
                funnel["vol_min"] += 1
                continue
            if f["idr22"] < IDR_FLOOR:
                funnel["idr_min"] += 1
                continue
            
            funnel["passed"] += 1
            feats[sym] = f
            vp = _volume_profile(df3)
            if vp:
                profiles[sym] = vp
        finally:
            time.sleep(0.1)

    logger.info(f"Scanner Reject Funnel: {json.dumps(funnel)}")

    if not feats:
        logger.warning("No symbols passed feature gates — universe unchanged.")
        return

    vv_med = float(np.median([f["vv20"] for f in feats.values()]))
    for f in feats.values():
        f["score"] = _score_v4(f, vv_med)
        f["bias_tag"] = _bias_tag(f, vv_med)

    snap = os.path.join(get_data_dir(), "vol_features_" + datetime.now().strftime("%Y-%m-%d") + ".json")
    try:
        with open(snap, "w") as f:
            json.dump(feats, f, indent=2)
        logger.info(f"Vol-feature snapshot -> {snap} ({len(feats)} symbols)")
    except Exception as e:
        logger.warning(f"snapshot write failed: {e}")
    try:
        if profiles:
            with open(VOLPROFILE_PATH, "w") as f:
                json.dump(profiles, f)
            logger.info(f"Volume profiles -> {VOLPROFILE_PATH} ({len(profiles)} symbols)")
    except Exception as e:
        logger.warning(f"vol profile write failed: {e}")

    ranked = sorted(feats.items(), key=lambda kv: -kv[1]["score"])
    rank_of = {sym: i + 1 for i, (sym, _) in enumerate(ranked)}
    final = []
    final_syms = set()
    for sym, f in ranked:
        if len(final) >= TOP_N:
            break
        rank = rank_of[sym]
        if rank <= FORCE_BAND or (sym in current and rank <= KEEP_BAND):
            final.append((sym, f))
            final_syms.add(sym)
    for sym, f in ranked:
        if len(final) >= TOP_N:
            break
        if sym not in final_syms:
            final.append((sym, f))
            final_syms.add(sym)

    stocks = []
    for sym, f in final:
        meta = cache[sym]
        entry = {"symbol": sym, "token": meta["token"],
                 "search": [meta.get("tsym", f"{sym}-EQ"), sym], **f}
        stocks.append(entry)

    with open(UNIVERSE_PATH, "w") as f:
        json.dump({"date": str(datetime.now().date()), "stocks": stocks}, f, indent=2)
    logger.info(f"Universe scan done: {len(stocks)} stocks -> {UNIVERSE_PATH}")
    for s in stocks[:8]:
        logger.info(f"  {s['symbol']}: score={s['score']} rv5={s['rv5']} rv22={s['rv22']} "
                    f"vv20={s['vv20']} jump={s['jump_ratio']} idr22={s['idr22']} tag={s['bias_tag']}")

    token_map = {s["token"]: s["symbol"] for s in stocks}
    try:
        fetch_and_seed_history(broker, list(token_map.keys()), token_map)
        logger.info("Pre-fetch of 3m history complete.")
    except Exception as e:
        logger.warning(f"Pre-fetch failed: {e}")


def main():
    logger.info("Universe scanner started: daily 16:05 IST sleep-loop; --now for one-shot.")
    broker = ShoonyaAdapter()
    sp = getattr(broker, "session_provider", None)
    if sp is not None and hasattr(sp, "_restore_or_login"):
        sp._restore_or_login()
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
            if now.weekday() < 5 and last_run != now.date() and now.hour == RUNTIME_HOUR and now.minute == RUNTIME_MIN:
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