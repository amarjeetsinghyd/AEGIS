"""Stock Order Flow — Engine 3 main loop (paper), 3-minute WS clock."""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, time as dt_time

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from common.config import configure_runtime

configure_runtime("stock_order_flow")

from common.broker import ShoonyaAdapter
from common.logger import get_logger
from common.market.market_calendar import (
    is_trading_day,
    is_market_open,
    MARKET_OPEN_TIME,
    MARKET_CLOSE_TIME,
    TRADE_ENTRY_CUTOFF_TIME,
)
from stock_order_flow.tick_compiler import TickCompiler
from stock_order_flow.evaluator import FootprintEvaluator
from stock_order_flow.execution_manager import StockExecutionManager

logger = get_logger("stock_main")

BAR_MINUTES = 3

# Canonical NSE equity tokens (searchscrip is flaky / renamed symbols).
EQUITY_UNIVERSE = {
    "RELIANCE": {"token": "2885", "tsym": "RELIANCE-EQ", "search": ["RELIANCE-EQ", "RELIANCE"]},
    "HDFCBANK": {"token": "1333", "tsym": "HDFCBANK-EQ", "search": ["HDFCBANK-EQ", "HDFCBANK"]},
    "INFY": {"token": "1594", "tsym": "INFY-EQ", "search": ["INFY-EQ", "INFY"]},
    "LT": {"token": "11483", "tsym": "LT-EQ", "search": ["LT-EQ", "LT"]},
    "TATAMOTORS": {
        "token": "3456",
        "tsym": "TMPV-EQ",
        "search": ["Tata Motors", "TMPV-EQ", "TATAMOTORS-EQ"],
        "note": "NSE renamed/split; tracking TMPV-EQ (ex-TATAMOTORS passenger)",
    },
}

STOCKS = list(EQUITY_UNIVERSE.keys())


def _resolve_equity(broker, symbol: str) -> tuple[str, str]:
    meta = EQUITY_UNIVERSE[symbol]
    api = getattr(getattr(broker, "session_provider", None), "_api", None)
    queries = list(meta.get("search") or [])
    preferred_tsym = meta["tsym"]

    if api is not None:
        for q in queries:
            try:
                res = api.searchscrip(exchange="NSE", searchtext=q)
            except Exception:
                continue
            vals = res.get("values", []) if isinstance(res, dict) else []
            for row in vals:
                tok = str(row.get("token") or "")
                tsym = str(row.get("tsym") or "")
                if not tok:
                    continue
                if tsym == preferred_tsym or (tsym.endswith("-EQ") and symbol in tsym):
                    return tok, tsym
            for row in vals:
                tok = str(row.get("token") or "")
                tsym = str(row.get("tsym") or "")
                if tok and "-EQ" in tsym:
                    return tok, tsym

    note = meta.get("note")
    if note:
        logger.warning(f"{symbol}: using hardcoded token map — {note}")
    return str(meta["token"]), str(meta["tsym"])


def _seed_3m_from_rest(broker, token: str, now: datetime):
    """Warmup only — live TSF continues on WS-built 3m bars."""
    try:
        hist = broker.historical_provider.get_historical(
            exchange="NSE",
            token=str(token),
            interval="THREE_MINUTE",
            start_time=now - timedelta(days=3),
            end_time=now,
        )
    except Exception as exc:
        logger.warning(f"REST 3m seed failed for {token}: {exc}")
        return None
    if hist is None or getattr(hist, "empty", True):
        return None
    try:
        ts = hist["timestamp"]
        if hasattr(ts.dt, "time"):
            hist = hist[ts.dt.time <= dt_time(15, 15, 0)].copy()
    except Exception:
        pass
    return hist


def _sleep_until(target: datetime, chunk: float = 30.0) -> None:
    while True:
        remaining = (target - datetime.now()).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(chunk, remaining))


def main():
    logger.info(
        "Starting Stock Order Flow Engine (Engine 3) — PAPER | "
        f"clock={BAR_MINUTES}m WS candles | TSF=shadow on WS 3m"
    )
    try:
        broker = ShoonyaAdapter()
        if hasattr(broker, "session_provider") and hasattr(broker.session_provider, "_restore_or_login"):
            broker.session_provider._restore_or_login()
        compiler = TickCompiler(broker)
        evaluator = FootprintEvaluator()
        trader = StockExecutionManager(broker)
    except Exception as e:
        logger.error(f"Initialization failed: {e}")
        sys.exit(1)

    tokens: list[str] = []
    token_to_symbol: dict[str, str] = {}
    for sym in STOCKS:
        try:
            tok, tsym = _resolve_equity(broker, sym)
            tokens.append(tok)
            token_to_symbol[tok] = sym
            logger.info(f"Resolved {sym} -> {tok} ({tsym})")
        except Exception as e:
            logger.error(f"Could not resolve {sym}: {e}")

    if not tokens:
        logger.error("No valid tokens found. Exiting.")
        sys.exit(1)

    # REST warmup so shadow TSF has ≥14 bars near open; then WS owns the series
    now0 = datetime.now()
    for tok in tokens:
        seed_df = _seed_3m_from_rest(broker, tok, now0)
        if seed_df is not None:
            compiler.bar_book.seed(tok, seed_df)

    compiler.subscribe(tokens)

    last_processed_bucket = None
    last_day = None

    while True:
        try:
            now = datetime.now()

            if not is_trading_day(now.date()):
                logger.info("Non-trading day — sleeping 15 minutes")
                time.sleep(900)
                continue

            if now.time() < MARKET_OPEN_TIME:
                wake = datetime.combine(now.date(), MARKET_OPEN_TIME)
                logger.info(f"Premarket — sleeping until {wake.strftime('%H:%M:%S')}")
                _sleep_until(wake)
                last_processed_bucket = None
                continue

            if now.time() > MARKET_CLOSE_TIME:
                if last_day != now.date():
                    logger.info("Session ended (≥15:15) — idle until next trading day")
                    last_day = now.date()
                time.sleep(60)
                continue

            current_prices = {}
            for t in tokens:
                sym = token_to_symbol[t]
                if t in compiler.market_depth and compiler.market_depth[t]["lp"] > 0:
                    current_prices[sym] = compiler.market_depth[t]["lp"]
                elif now.second % 5 == 0:
                    try:
                        q = broker.market_data_provider.get_quote("NSE", t) or {}
                        ltp = float(q.get("ltp") or 0)
                        if ltp > 0:
                            current_prices[sym] = ltp
                            compiler.market_depth.setdefault(
                                t, {"lp": 0.0, "v": 0, "bp1": 0.0, "sp1": 0.0}
                            )
                            compiler.market_depth[t]["lp"] = ltp
                            compiler.bar_book.on_tick(t, ltp, ts=now, volume_delta=0.0)
                    except Exception:
                        pass

            trader.manage_open_trades(current_prices)

            # 3-minute boundary on 09:15 grid (minute % 3 == 0)
            bucket_key = (now.date(), now.hour, now.minute - (now.minute % BAR_MINUTES))
            is_boundary = (now.minute % BAR_MINUTES == 0) and (now.second >= 2)
            if (
                is_boundary
                and bucket_key != last_processed_bucket
                and is_market_open(now)
            ):
                last_processed_bucket = bucket_key
                logger.info(
                    f"========== {BAR_MINUTES}m BOUNDARY {now.strftime('%H:%M:00')} =========="
                )

                footprints = compiler.get_compiled_footprints()

                for token, sym in token_to_symbol.items():
                    try:
                        compiler.bar_book.force_close_forming(token, now)
                        hist = compiler.bar_book.completed_df(token, min_bars=14)
                        n_bars = compiler.bar_book.completed_count(token)
                        if hist is None:
                            logger.info(
                                f"{sym}: WS 3m bars={n_bars}/14 (TSF shadow warming) — "
                                f"footprint still evaluated"
                            )

                        fp_data = footprints.get(token, {})
                        if sym in current_prices:
                            current_price = current_prices[sym]
                        elif hist is not None:
                            current_price = float(hist.iloc[-1]["close"])
                        else:
                            logger.warning(f"{sym}: no LTP yet — skip")
                            continue

                        signal, reason = evaluator.evaluate_boundary(
                            symbol=sym,
                            current_price=current_price,
                            historical_df=hist,
                            footprint_data=fp_data,
                            now=now,
                        )

                        allow_entry = now.time() < TRADE_ENTRY_CUTOFF_TIME
                        if signal in ("BUY", "SELL") and allow_entry:
                            logger.info(f"🔥 {sym} SIGNAL: {signal} - {reason}")
                            trader.execute_entry(signal, sym, current_price, reason)
                        elif signal in ("BUY", "SELL") and not allow_entry:
                            logger.info(f"{sym}: {signal} blocked after entry cutoff — {reason}")
                        else:
                            logger.info(f"{sym}: {reason}")
                    except Exception as e:
                        logger.error(f"Error evaluating {sym}: {e}")

                compiler.reset_for_new_candle()

            time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Stopping Engine 3...")
            break
        except Exception as e:
            logger.error(f"Loop error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
