"""
Shoonya Data Hub — sole shared tick producer for Astra + Kronos.

Writes /dev/shm/shoonya_live_ticks.json with:
  - live spot + option quotes
  - synthetic constituent volume (WS LTQ accumulator for NIFTY50 / SENSEX30)
  - 5-minute closed synthetic volume buckets for Kronos candle assembly
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, time as dt_time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from common.config import configure_runtime
from common.logger import get_logger
from common.broker import ShoonyaAdapter
from common.ipc import SHARED_TICK_FILE
from common.ipc.live_state import _atomic_write

configure_runtime("hub")
logger = get_logger("shoonya_data_hub")

_CONSTITUENTS_PATH = os.path.join(BASE_DIR, "institutional_memory", "constituents.json")

# Continuous cash session for Shadow OI (matches Kronos 1m / CAS hygiene).
# Collect 09:15 → 15:15; last completed 1m stamp is 15:14.
_OI_SESSION_OPEN = dt_time(9, 15, 0)
_OI_SESSION_END = dt_time(15, 15, 0)


def _active_index() -> tuple[str, str]:
    day = datetime.now().strftime("%A")
    if day in ("Wednesday", "Thursday"):
        return "SENSEX", "BFO"
    return "NIFTY", "NFO"


def _cash_token(name: str) -> tuple[str, str]:
    if name == "SENSEX":
        return "1", "BSE"
    return "26000", "NSE"


def _load_constituents(name: str) -> dict:
    if not os.path.exists(_CONSTITUENTS_PATH):
        logger.warning(f"constituents.json missing at {_CONSTITUENTS_PATH}")
        return {}
    try:
        with open(_CONSTITUENTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if name == "NIFTY":
            return dict(data.get("nifty_50") or {})
        return dict(data.get("sensex_30") or {})
    except Exception as e:
        logger.error(f"Failed to load constituents: {e}")
        return {}


def _five_min_bucket(now: datetime) -> str:
    m = now.minute - (now.minute % 5)
    return now.strftime(f"%Y-%m-%d %H:{m:02d}:00")


def _in_oi_session(now: datetime = None) -> bool:
    """True during continuous session 09:15 ≤ t < 15:15 (Shadow OI collection window)."""
    now = now or datetime.now()
    t = now.time()
    return _OI_SESSION_OPEN <= t < _OI_SESSION_END


class VolumeBucket:
    """Snapshot WS synthetic volume at each completed 5-minute boundary."""

    def __init__(self):
        self.current_bucket = _five_min_bucket(datetime.now())
        self.closed_nifty = 0
        self.closed_sensex = 0
        self.last_closed_bucket = ""

    def tick(self, broker, active_name: str) -> tuple[int, int]:
        now = datetime.now()
        bucket = _five_min_bucket(now)
        # At/after boundary, close previous bucket once
        if bucket != self.current_bucket:
            closed = 0
            try:
                closed = int(broker.get_and_reset_synthetic_volume())
            except Exception:
                try:
                    closed = int(broker.market_data_provider.get_and_reset_synthetic_volume())
                except Exception:
                    closed = 0
            if active_name == "NIFTY":
                self.closed_nifty = closed
            else:
                self.closed_sensex = closed
            self.last_closed_bucket = self.current_bucket
            self.current_bucket = bucket
            logger.info(
                f"[SynthVol] Closed 5m bucket {self.last_closed_bucket} "
                f"for {active_name}: volume={closed:,}"
            )
        return self.closed_nifty, self.closed_sensex


class OiMinuteBucket:
    """Publish last completed 1-minute Net Delta OI (PEΔ − CEΔ) for Kronos shadow research.

    Collection window: 09:15 → 15:15 IST only (same continuous session as 1m bars).
    """

    def __init__(self):
        self._minute_key = None  # type: ignore
        self.net = 0.0
        self.delta_call = 0.0
        self.delta_put = 0.0
        self.total_call = 0
        self.total_put = 0
        self._rr_idx = 0
        self._session_closed = False

    def _totals(self, mdp) -> tuple:
        latest = getattr(mdp, "_option_oi_latest", {}) or {}
        ce = getattr(mdp, "_option_ce_tokens", set()) or set()
        pe = getattr(mdp, "_option_pe_tokens", set()) or set()
        total_call = sum(int(latest.get(t, 0) or 0) for t in ce)
        total_put = sum(int(latest.get(t, 0) or 0) for t in pe)
        return total_call, total_put

    def refresh_oi_round_robin(self, broker, opt_tokens, exch_seg, batch=8):
        """REST-refresh a slice of option OI so deltas work even if WS touchline omits oi."""
        if not opt_tokens or not _in_oi_session():
            return
        mdp = getattr(broker, "market_data_provider", None)
        if not mdp or not hasattr(mdp, "force_rest_quote"):
            return
        n = len(opt_tokens)
        for _ in range(min(batch, n)):
            tok = opt_tokens[self._rr_idx % n]
            self._rr_idx += 1
            try:
                mdp.force_rest_quote(exch_seg, tok)
            except Exception:
                pass

    def _close_minute(self, broker, mdp) -> None:
        try:
            net, pe_d, ce_d = broker.compile_5min_option_net_delta_oi()
        except Exception:
            try:
                net, pe_d, ce_d = mdp.get_and_reset_option_net_delta_oi()
            except Exception:
                net, pe_d, ce_d = 0.0, 0.0, 0.0
        self.net = float(net)
        self.delta_put = float(pe_d)
        self.delta_call = float(ce_d)
        if self.net != 0.0 or self.delta_call != 0.0 or self.delta_put != 0.0:
            logger.info(
                f"[ShadowOI] 1m closed net={self.net:+.0f} "
                f"(PEΔ={self.delta_put:+.0f} CEΔ={self.delta_call:+.0f}) | "
                f"totals C={self.total_call:,} P={self.total_put:,}"
            )

    def tick(self, broker, now: datetime = None):
        now = now or datetime.now()
        key = now.strftime("%Y-%m-%d %H:%M")
        mdp = getattr(broker, "market_data_provider", None)
        if mdp is not None:
            self.total_call, self.total_put = self._totals(mdp)

        # Outside 09:15–15:15: freeze last values (close final 15:14 minute once at 15:15).
        if not _in_oi_session(now):
            if (
                not self._session_closed
                and now.time() >= _OI_SESSION_END
                and self._minute_key is not None
            ):
                self._close_minute(broker, mdp)
                self._session_closed = True
                logger.info("[ShadowOI] Collection frozen at 15:15 (continuous session end)")
            return self.net, self.delta_call, self.delta_put, self.total_call, self.total_put

        # Entering / inside continuous session
        if self._session_closed:
            # New day / next session — clear minute baseline
            self._minute_key = key
            self._session_closed = False
            self.net = self.delta_call = self.delta_put = 0.0
            return self.net, self.delta_call, self.delta_put, self.total_call, self.total_put

        if self._minute_key is None:
            self._minute_key = key
            return self.net, self.delta_call, self.delta_put, self.total_call, self.total_put

        if key != self._minute_key:
            self._close_minute(broker, mdp)
            self._minute_key = key
        return self.net, self.delta_call, self.delta_put, self.total_call, self.total_put


def _build_payload(
    broker,
    name: str,
    exch_seg: str,
    opt_tokens: list[str],
    vol_bucket: VolumeBucket,
    oi_bucket: OiMinuteBucket,
    *,
    refresh_spot_rest: bool = False,
) -> dict:
    spot_token, spot_exch = _cash_token(name)
    mdp = getattr(broker, "market_data_provider", None)
    quotes: dict = {}

    def _pull(token: str, exch: str):
        if not mdp:
            return
        try:
            q = mdp.get_quote(exch, str(token)) or {}
            if isinstance(q, dict):
                quotes[str(token)] = {
                    "ltp": float(q.get("ltp") or q.get("lp") or 0.0),
                    "bid": float(q.get("bid") or q.get("bp1") or 0.0),
                    "ask": float(q.get("ask") or q.get("sp1") or 0.0),
                    "oi": float(q.get("oi") or 0.0),
                    "volume": float(q.get("volume") or q.get("v") or 0.0),
                }
        except Exception:
            pass

    # Unstick cash index LTP when WS cache is stale (REST is authoritative).
    if refresh_spot_rest and mdp and hasattr(mdp, "force_rest_quote"):
        try:
            quotes[str(spot_token)] = mdp.force_rest_quote(spot_exch, spot_token)
        except Exception:
            _pull(spot_token, spot_exch)
    else:
        _pull(spot_token, spot_exch)

    for tok in opt_tokens:
        _pull(tok, exch_seg)

    closed_nifty, closed_sensex = vol_bucket.tick(broker, name)
    try:
        live_acc = int(broker.peek_synthetic_volume())
    except Exception:
        live_acc = int(getattr(mdp, "_accumulated_synthetic_vol", 0) or 0) if mdp else 0

    net_d, d_call, d_put, total_call_oi, total_put_oi = oi_bucket.tick(broker)

    return {
        "timestamp": datetime.now().isoformat(),
        "active_index": name,
        "spot_token": spot_token,
        "spot_exch": spot_exch,
        "synthetic_nifty_volume": closed_nifty,
        "synthetic_sensex_volume": closed_sensex,
        "synthetic_constituent_volume": closed_nifty if name == "NIFTY" else closed_sensex,
        "synthetic_volume_live_accum": live_acc,
        "synthetic_volume_bucket": vol_bucket.current_bucket,
        "synthetic_volume_last_closed_bucket": vol_bucket.last_closed_bucket,
        "total_call_oi": total_call_oi,
        "total_put_oi": total_put_oi,
        "delta_call_oi_1min": d_call,
        "delta_put_oi_1min": d_put,
        "net_delta_oi_1min": net_d,
        "quotes": quotes,
    }


def _subscribe_cash_book(broker, name: str) -> list[str]:
    """Register + touchline-subscribe spot + constituents for synthetic volume."""
    mdp = broker.market_data_provider
    spot_token, spot_exch = _cash_token(name)
    mdp.subscribe([spot_token], spot_exch, feed_type="t")
    constituents = _load_constituents(name)
    const_tokens = [str(t) for t in constituents.values() if str(t).strip()]
    if const_tokens:
        cash_exch = "NSE" if name == "NIFTY" else "BSE"
        broker.register_constituent_tokens(const_tokens)
        mdp.subscribe(const_tokens, cash_exch, feed_type="t")
        logger.info(
            f"Subscribed {len(const_tokens)} {name} constituents on {cash_exch} "
            f"for synthetic volume WS feed (touchline)"
        )
    else:
        logger.warning(f"No constituents loaded for {name} — synthetic volume will stay 0")
    return const_tokens


def _subscribe_options(broker, name: str, exch_seg: str) -> list[str]:
    """Discover ATM±5 weekly CE/PE, register for OI tracking, subscribe touchline."""
    mdp = broker.market_data_provider
    spot_token, spot_exch = _cash_token(name)
    api = getattr(broker.session_provider, "_api", None)
    spot_ltp = 0.0
    try:
        q = (
            mdp.force_rest_quote(spot_exch, spot_token)
            if hasattr(mdp, "force_rest_quote")
            else (mdp.get_quote(spot_exch, spot_token) or {})
        )
        spot_ltp = float(q.get("ltp") or 0)
    except Exception:
        spot_ltp = 0.0
    if spot_ltp <= 0:
        spot_ltp = 78000.0 if name == "SENSEX" else 24000.0
    step = 100 if name == "SENSEX" else 50
    atm = round(spot_ltp / step) * step

    opt_tokens: list[str] = []
    ce_tokens: list[str] = []
    pe_tokens: list[str] = []

    if api and hasattr(api, "searchscrip"):
        # Keep option WS light — Shoonya WS has a practical subscription ceiling;
        # cash/constituents must win those slots for synthetic volume.
        for offset in range(-5, 6):
            strike = int(atm + offset * step)
            try:
                res = api.searchscrip(exchange=exch_seg, searchtext=f"{name} {strike}")
                vals = res.get("values", []) if isinstance(res, dict) else []
                for row in vals:
                    tok = str(row.get("token") or "")
                    tsym = str(row.get("tsym") or "")
                    optt = str(row.get("optt") or "").upper()
                    if not tok:
                        continue
                    is_ce = optt == "CE" or ("CE" in tsym and "PE" not in tsym)
                    is_pe = optt == "PE" or "PE" in tsym
                    if not is_ce and not is_pe:
                        continue
                    if tok not in opt_tokens:
                        opt_tokens.append(tok)
                        if is_ce:
                            ce_tokens.append(tok)
                        else:
                            pe_tokens.append(tok)
                    if len(opt_tokens) >= 80:
                        break
            except Exception:
                continue
            if len(opt_tokens) >= 80:
                break

    if opt_tokens:
        # Reset CE/PE sets for this index so rotation cannot mix NIFTY/SENSEX OI.
        mdp._option_ce_tokens = set()
        mdp._option_pe_tokens = set()
        mdp._option_oi_start = {}
        mdp._option_oi_latest = {}
        mdp.register_option_tokens(ce_tokens, pe_tokens)
        mdp.subscribe(opt_tokens, exch_seg, feed_type="t")
        logger.info(
            f"Subscribed spot + {len(opt_tokens)} option tokens for {name} "
            f"(CE={len(ce_tokens)} PE={len(pe_tokens)}, capped)"
        )
        # Bootstrap OI baselines via REST (touchline often omits oi until a trade).
        seeded = 0
        for tok in opt_tokens:
            try:
                q = mdp.force_rest_quote(exch_seg, tok)
                if float((q or {}).get("oi") or 0) > 0:
                    seeded += 1
            except Exception:
                pass
            time.sleep(0.05)
        logger.info(f"[ShadowOI] REST-seeded OI for {seeded}/{len(opt_tokens)} option tokens")
    else:
        logger.warning(f"No option tokens found for {name} — Shadow OI will stay 0")
    return opt_tokens


def main():
    logger.info("==================================================")
    logger.info("STARTING SHOONYA DATA HUB (shared tick bus)")
    logger.info("==================================================")

    try:
        broker = ShoonyaAdapter()
        if hasattr(broker, "session_provider") and hasattr(broker.session_provider, "_restore_or_login"):
            broker.session_provider._restore_or_login()
    except Exception as e:
        logger.critical(f"Hub broker init failed: {e}")
        sys.exit(1)

    name, exch_seg = _active_index()
    spot_token, spot_exch = _cash_token(name)
    opt_tokens: list[str] = []
    vol_bucket = VolumeBucket()
    oi_bucket = OiMinuteBucket()
    const_tokens: list[str] = []
    last_cash_resub = 0.0
    loop_i = 0

    try:
        mdp = broker.market_data_provider
        mdp._ensure_ws_started()
        const_tokens = _subscribe_cash_book(broker, name)
        opt_tokens = _subscribe_options(broker, name, exch_seg)
    except Exception as e:
        logger.error(f"Subscription warning: {e}")

    logger.info(f"Writing shared ticks -> {SHARED_TICK_FILE}")
    last_name = name
    while True:
        try:
            now = datetime.now()
            name, exch_seg = _active_index()
            if name != last_name:
                logger.info(f"Active index rotated {last_name} -> {name}; re-subscribing cash + options")
                const_tokens = _subscribe_cash_book(broker, name)
                opt_tokens = _subscribe_options(broker, name, exch_seg)
                last_name = name
                last_cash_resub = time.time()

            # During cash session, if WS synth accum stays dead, re-subscribe touchline cash book.
            in_cash_session = (now.hour > 9 or (now.hour == 9 and now.minute >= 15)) and (
                now.hour < 15 or (now.hour == 15 and now.minute < 30)
            )
            try:
                live_acc = int(broker.peek_synthetic_volume())
            except Exception:
                live_acc = 0
            if (
                in_cash_session
                and live_acc <= 0
                and const_tokens
                and (time.time() - last_cash_resub) >= 120
            ):
                logger.warning(
                    "[SynthVol] live_accum=0 during session — re-subscribing cash touchline feed"
                )
                const_tokens = _subscribe_cash_book(broker, name)
                last_cash_resub = time.time()

            loop_i += 1
            # Round-robin REST OI only in continuous session 09:15–15:15.
            if _in_oi_session(now) and opt_tokens:
                oi_bucket.refresh_oi_round_robin(broker, opt_tokens, exch_seg, batch=8)

            payload = _build_payload(
                broker,
                name,
                exch_seg,
                opt_tokens,
                vol_bucket,
                oi_bucket,
                refresh_spot_rest=(loop_i % 5 == 0),  # REST spot every ~5s
            )
            _atomic_write(SHARED_TICK_FILE, payload)
            time.sleep(1.0)
        except KeyboardInterrupt:
            logger.info("Data hub shutting down...")
            break
        except Exception as e:
            logger.error(f"Hub loop error: {e}")
            time.sleep(2.0)


if __name__ == "__main__":
    main()
