import os
import sys
import time
import json
from datetime import datetime, timedelta, time as dt_time
import pandas as pd
import numpy as np

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import kronos_bot.config.settings  # noqa: F401

from common.logger import get_logger
from kronos_bot.core.data_fetcher import DataFetcher
from kronos_bot.execution.kronos_execution_manager import KronosExecutionManager
from kronos_bot.strategy.signal_generator import KronosSignalGenerator
from common.ipc import update_kronos_live_state, SHARED_TICK_FILE
from common.broker import ShoonyaAdapter
from kronos_bot.config.settings import BROKER, DATA_DIR, LOGS_DIR, PREDICTION_THRESHOLD

from common.market.market_calendar import (
    is_market_open,
    is_bot_active_hours,
    is_pre_market,
    sleep_until_next_premarket,
    MARKET_OPEN_TIME,
)

logger = get_logger("kronos_bot_main")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)
DECISION_JOURNAL_DIR = os.path.join(DATA_DIR, "decisions")
DECISION_JOURNAL_PATH = os.path.join(DECISION_JOURNAL_DIR, "decision_journal.jsonl")
os.makedirs(DECISION_JOURNAL_DIR, exist_ok=True)


def _append_decision_journal(row: dict) -> None:
    """Append one 5m prediction row for EOD Supertrend / Kronos paper analysis."""
    try:
        with open(DECISION_JOURNAL_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception as exc:
        logger.warning(f"[DECISION JOURNAL] write failed: {exc}")

# Wake from pre-market deep sleep 30s before continuous open (09:14:30).
PREMARKET_WAKE_TIME = dt_time(9, 14, 30)


def _chunked_sleep_until(target: datetime, chunk_sec: float = 30.0) -> None:
    """Sleep until target in small chunks so SIGTERM/SIGINT can land cleanly."""
    while True:
        remaining = (target - datetime.now()).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(chunk_sec, remaining))


def get_shared_oi_delta_details() -> dict:
    if os.path.exists(SHARED_TICK_FILE):
        try:
            with open(SHARED_TICK_FILE, "r") as f:
                data = json.load(f)
            return {
                "net_delta_oi": float(data.get("net_delta_oi_1min", 0.0)),
                "delta_call_oi": float(data.get("delta_call_oi_1min", 0.0)),
                "delta_put_oi": float(data.get("delta_put_oi_1min", 0.0)),
                "total_call_oi": int(data.get("total_call_oi", 0)),
                "total_put_oi": int(data.get("total_put_oi", 0)),
            }
        except Exception:
            pass
    return {"net_delta_oi": 0.0, "delta_call_oi": 0.0, "delta_put_oi": 0.0, "total_call_oi": 0, "total_put_oi": 0}


def main():
    logger.info("==================================================")
    logger.info("STARTING KRONOS BOT WITH KRONOS-BASE TRANSFORMER")
    logger.info("==================================================")
    
    try:
        broker = ShoonyaAdapter()
        fetcher = DataFetcher(broker)
        trader = KronosExecutionManager(broker, fetcher)
        
        logger.info("Initializing Kronos Foundation Model Predictor...")
        signal_gen = KronosSignalGenerator(device="cpu")
    except Exception as e:
        logger.critical(f"Initialization failed: {e}")
        sys.exit(1)
        
    logger.info("Running reboot gap-fill / bootstrap service...")
    fetcher.bootstrap_candle_window()
    
    last_processed_candle_time = ''
    last_oi_log_minute = None
    eod_backfill_done = False
    opening_trade_evaluated = False
    premarket_handshake_done_for = None  # date of last 08:55 handshake

    name, exch_seg = fetcher.get_active_instrument()
    logger.info(f"PRE-MARKET HANDSHAKE: Active Index = {name} ({exch_seg}).")
    update_kronos_live_state({
        "active_index": name,
        "spot_price": fetcher.get_index_ltp(name),
        "signal": "Waiting",
        "signal_status": "Waiting",
        "reasoning": f"Handshake complete — {name} day. Sleeping until 09:14:30 IST.",
    })

    while True:
        try:
            now = datetime.now()
            # Always refresh weekday rotation (fixes overnight NIFTY→SENSEX stale label)
            name, exch_seg = fetcher.get_active_instrument()

            # 1. Check active bot runtime
            if not is_bot_active_hours(now):
                if not eod_backfill_done and now.time() >= dt_time(15, 17):
                    logger.info("==========================================================")
                    logger.info("03:17 PM IST POST-MARKET EOD BACKFILL SERVICE INITIATING")
                    logger.info("==========================================================")
                    fetcher.rest_backfill_today_candles("NIFTY")
                    fetcher.rest_backfill_today_candles("SENSEX")
                    eod_backfill_done = True
                    logger.info("Post-Market EOD backfill completed for both NIFTY and SENSEX. Entering deep sleep...")
                
                sleep_until_next_premarket()
                eod_backfill_done = False
                opening_trade_evaluated = False
                premarket_handshake_done_for = None
                name, exch_seg = fetcher.get_active_instrument()
                logger.info(f"PRE-MARKET HANDSHAKE (wake): Active Index = {name} ({exch_seg}).")
                update_kronos_live_state({
                    "active_index": name,
                    "spot_price": fetcher.get_index_ltp(name),
                    "signal": "Waiting",
                    "signal_status": "Waiting",
                    "reasoning": f"Handshake complete — {name} day. Sleeping until 09:14:30 IST.",
                })
                continue

            # 2. Pre-market: handshake once, then sleep until 09:14:30 (no 30s spam)
            if is_pre_market(now):
                if premarket_handshake_done_for != now.date():
                    spot_price = fetcher.get_index_ltp(name)
                    logger.info(
                        f"[PRE-MARKET] Active Index={name} ({exch_seg}) LTP={spot_price:.2f}. "
                        f"Sleeping until {PREMARKET_WAKE_TIME.strftime('%H:%M:%S')} IST (no polling)."
                    )
                    update_kronos_live_state({
                        "active_index": name,
                        "spot_price": spot_price,
                        "signal": "Waiting",
                        "signal_status": "Waiting",
                        "reasoning": f"{name} pre-market — wake at 09:14:30 IST.",
                    })
                    premarket_handshake_done_for = now.date()

                wake_at = datetime.combine(now.date(), PREMARKET_WAKE_TIME)
                if now < wake_at:
                    _chunked_sleep_until(wake_at)
                    continue

                # 09:14:30 → 09:15:00: short standby, then enter session loop
                open_at = datetime.combine(now.date(), MARKET_OPEN_TIME)
                if now < open_at:
                    spot_price = fetcher.get_index_ltp(name)
                    logger.info(
                        f"[PRE-MARKET WAKE] Active Index={name} LTP={spot_price:.2f}. "
                        f"Market opens in {max(0, int((open_at - now).total_seconds()))}s."
                    )
                    update_kronos_live_state({
                        "active_index": name,
                        "spot_price": spot_price,
                        "signal": "Waiting",
                        "signal_status": "Waiting",
                        "reasoning": f"{name} — final standby before 09:15 open.",
                    })
                    _chunked_sleep_until(open_at, chunk_sec=1.0)
                continue

            # 3. Transparent 1-Minute Net Delta OI Periodic Log
            if now.minute != last_oi_log_minute:
                last_oi_log_minute = now.minute
                oi_meta = get_shared_oi_delta_details()
                net_oi = oi_meta["net_delta_oi"]
                d_call = oi_meta["delta_call_oi"]
                d_put = oi_meta["delta_put_oi"]
                bias = "BULLISH SUPPORT" if net_oi > 0 else ("BEARISH RESISTANCE" if net_oi < 0 else "NEUTRAL")
                # # Orderflow OI delta log removed per user request")

            # 4. Market Open (09:15 AM): Disabled per strategy (First trade starts at 09:20 AM after full candle completes)
            opening_trade_evaluated = True

            # 5. Standard 5-Minute Candle Boundaries (09:20, 09:25, 09:30...)
            curr_minute = now.minute
            is_boundary = (curr_minute % 5 == 0) and (now.second >= 2)
            candle_time_str = now.strftime("%Y-%m-%d %H:%M:00")
            
            if is_boundary and candle_time_str != last_processed_candle_time:
                last_processed_candle_time = candle_time_str
                closed_time_short = now.strftime("%H:%M:00")
                logger.info(
                    f"========== CANDLE BOUNDARY {candle_time_str} | {name} =========="
                )

                spot_price = fetcher.get_index_ltp(name)
                if spot_price <= 0:
                    logger.warning("Could not fetch spot index price. Retrying...")
                    time.sleep(2)
                    continue

                # --- Step 1: synthetic volume (hub WS first, REST fallback) ---
                vol_source = "hub_ws"
                synth_vol = fetcher.get_hub_synthetic_volume(name)
                if synth_vol <= 0:
                    logger.warning(
                        f"[1/4 SYNTHETIC VOLUME] Hub WS reported 0 for {name} — "
                        "trying REST 5m constituent fallback"
                    )
                    try:
                        synth_vol = int(fetcher.rest_synthetic_volume_for_latest_bar(name) or 0)
                        vol_source = "rest_5m_constituents" if synth_vol > 0 else "none"
                    except Exception as e:
                        logger.warning(f"[1/4 SYNTHETIC VOLUME] REST fallback error: {e}")
                        synth_vol = 0
                        vol_source = "none"
                if synth_vol <= 0:
                    logger.warning(
                        f"[1/4 SYNTHETIC VOLUME] Still 0 for {name} — "
                        "check data-hub constituent WS subscription"
                    )
                else:
                    logger.info(
                        f"[1/4 SYNTHETIC VOLUME] Closed candle [{closed_time_short}] "
                        f"source={vol_source} | compiled_vol={synth_vol:,} shares "
                        f"(sum of active-index constituents)"
                    )

                # --- Step 2: REST 5m index OHLC ---
                target_token, _, target_exch = fetcher.get_cash_index_token(name)
                open_p = high_p = low_p = close_p = float(spot_price)
                ohlc_source = "spot_ltp_fallback"
                try:
                    hist = getattr(fetcher.broker, "historical_provider", None)
                    rest_df = None
                    if hist is not None:
                        rest_df = hist.get_historical(
                            exchange=target_exch,
                            token=str(target_token),
                            interval="FIVE_MINUTE",
                            start_time=now - timedelta(minutes=25),
                            end_time=now,
                        )
                    matched_row = None
                    if rest_df is not None and not rest_df.empty:
                        rdf = rest_df.copy()
                        rdf["timestamp"] = pd.to_datetime(rdf["timestamp"])
                        target_ts = pd.to_datetime(candle_time_str)
                        exact = rdf[rdf["timestamp"] == target_ts]
                        if not exact.empty:
                            matched_row = exact.iloc[-1]
                        else:
                            # Closed boundary bar is usually the previous completed 5m label
                            prior = rdf[rdf["timestamp"] <= target_ts]
                            matched_row = prior.iloc[-1] if not prior.empty else rdf.iloc[-1]
                        open_p = float(matched_row["open"])
                        high_p = float(matched_row["high"])
                        low_p = float(matched_row["low"])
                        close_p = float(matched_row["close"])
                        ohlc_source = "rest_5m"
                        logger.info(
                            f"[2/4 REST OHLC] {target_exch}:{target_token} 5m bar "
                            f"time={matched_row['timestamp']} | "
                            f"O={open_p:.2f} H={high_p:.2f} L={low_p:.2f} C={close_p:.2f}"
                        )
                    else:
                        logger.warning(
                            f"[2/4 REST OHLC] Empty REST series for {target_exch}:{target_token} — "
                            f"using spot LTP fallback {spot_price:.2f} for O=H=L=C"
                        )
                except Exception as e:
                    logger.warning(
                        f"[2/4 REST OHLC] Fetch failed ({e}) — "
                        f"using spot LTP fallback {spot_price:.2f} for O=H=L=C"
                    )

                if ohlc_source == "spot_ltp_fallback":
                    logger.warning(
                        f"[2/4 REST OHLC] FALLBACK flat candle O=H=L=C={spot_price:.2f} "
                        f"(this is NOT true 5m OHLC — investigate REST)"
                    )

                # --- Step 3: amount + assembled candle ---
                mean_price = (open_p + high_p + low_p + close_p) / 4.0
                amount = float(synth_vol) * float(mean_price)
                new_candle = {
                    "timestamp": pd.to_datetime(candle_time_str),
                    "open": open_p,
                    "high": high_p,
                    "low": low_p,
                    "close": close_p,
                    "volume": synth_vol,
                    "amount": amount,
                }
                logger.info(
                    f"[3/4 AMOUNT] mean_price=(O+H+L+C)/4={mean_price:.2f} | "
                    f"amount=mean×vol={mean_price:.2f}×{synth_vol:,}={amount:,.2f}"
                )
                logger.info(
                    f"[3/4 CANDLE READY] {candle_time_str} | "
                    f"O={open_p:.2f} H={high_p:.2f} L={low_p:.2f} C={close_p:.2f} | "
                    f"V={synth_vol:,} | A={amount:,.2f} | "
                    f"ohlc_src={ohlc_source} vol_src={vol_source}"
                )

                # --- Step 4: sliding window ---
                fetcher.append_live_candle(new_candle)
                window_df = fetcher.get_window_df()
                try:
                    last = window_df.iloc[-1]
                    logger.info(
                        f"[4/4 SLIDING WINDOW] size={len(window_df)}/512 | last_bar="
                        f"ts={last.get('timestamp', candle_time_str)} "
                        f"O={float(last['open']):.2f} H={float(last['high']):.2f} "
                        f"L={float(last['low']):.2f} C={float(last['close']):.2f} "
                        f"V={float(last['volume']):,.0f} A={float(last['amount']):,.2f}"
                    )
                except Exception:
                    logger.info(f"[4/4 SLIDING WINDOW] size={len(window_df) if window_df is not None else 0}/512")

                # Kronos Prediction
                pred_res = signal_gen.predict(window_df, pred_len=1, threshold=PREDICTION_THRESHOLD)
                
                ml_sig = pred_res["signal"]
                pred_close = pred_res["pred_close"]
                ret_pct = pred_res["return_pct"]
                st_bias = pred_res.get("supertrend_bias", "UNKNOWN")
                st_value = pred_res.get("supertrend")
                raw_sig = pred_res.get("raw_signal", ml_sig)
                st_str = f"{float(st_value):.2f}" if st_value is not None else "N/A"

                oi_meta = get_shared_oi_delta_details()
                net_delta_oi = oi_meta["net_delta_oi"]

                final_sig = ml_sig
                oi_alignment = "NEUTRAL"
                if ml_sig == "CALL":
                    oi_alignment = "CONFIRMING (BULLISH)" if net_delta_oi > 0 else ("CONTRADICTING (BEARISH)" if net_delta_oi < 0 else "NEUTRAL")
                elif ml_sig == "PUT":
                    oi_alignment = "CONFIRMING (BEARISH)" if net_delta_oi < 0 else ("CONTRADICTING (BULLISH)" if net_delta_oi > 0 else "NEUTRAL")

                if ml_sig in ["CALL", "PUT"]:
                    logger.info(f"🔬 [SHADOW OI RESEARCH] {ml_sig} Signal Generated! 1-Min Net Delta OI: {net_delta_oi:+,} | Orderflow Alignment: {oi_alignment}")

                logger.info(
                    f"[5m DECISION] Supertrend={st_bias} (ST={st_str}) | "
                    f"Kronos raw={raw_sig} -> final={final_sig} | "
                    f"delta={ret_pct*100:+.3f}% | {spot_price:.2f} -> {pred_close:.2f}"
                )

                try:
                    bar_close = float(window_df.iloc[-1]["close"])
                except Exception:
                    bar_close = float(spot_price)

                will_enter = final_sig in ["CALL", "PUT"] and KronosExecutionManager.can_enter(now)
                st_blocked = raw_sig in ("CALL", "PUT") and final_sig == "NEUTRAL"
                _append_decision_journal(
                    {
                        "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
                        "candle": candle_time_str,
                        "index": name,
                        "close": bar_close,
                        "spot": float(spot_price),
                        "pred_close": float(pred_close),
                        "delta_pct": float(ret_pct * 100.0),
                        "threshold_pct": float(PREDICTION_THRESHOLD * 100.0),
                        "kronos_raw": raw_sig,
                        "final_signal": final_sig,
                        "supertrend": float(st_value) if st_value is not None else None,
                        "supertrend_bias": st_bias,
                        "st_blocked": st_blocked,
                        "will_enter": will_enter,
                        "net_delta_oi": float(net_delta_oi),
                        "reason": pred_res.get("reason", ""),
                    }
                )

                update_kronos_live_state({
                    "active_index": name,
                    "spot_price": spot_price,
                    "predicted_close": pred_close,
                    "predicted_return_pct": ret_pct * 100.0,
                    "signal": final_sig,
                    "signal_status": final_sig,
                    "kronos_raw_signal": raw_sig,
                    "supertrend": st_value,
                    "supertrend_bias": st_bias,
                    "net_delta_oi": net_delta_oi,
                    "last_candle": candle_time_str
                })

                if will_enter:
                    logger.info(f"🔥 EXECUTING KRONOS ENTRY FOR {final_sig} SIGNAL 🔥")
                    trader.execute_entry(
                        final_sig,
                        spot_price,
                        decision_payload={
                            "pred_close": pred_close,
                            "return_pct": ret_pct,
                            "supertrend": st_value,
                            "supertrend_bias": st_bias,
                            "kronos_raw_signal": raw_sig,
                            "net_delta_oi": net_delta_oi,
                            "oi_alignment": oi_alignment,
                        },
                    )

                sleep_sec = 300.0 - (now.minute % 5 * 60 + now.second)
                logger.info(f"Sleeping for {max(sleep_sec, 1.0):.2f}s until next candle boundary...")

            # Always manage open risk every second (TP / Chandelier / time / force-flatten)
            window_df = None
            if trader.has_open_trade():
                try:
                    window_df = fetcher.get_window_df()
                except Exception:
                    window_df = None
            spot_now = 0.0
            try:
                spot_now = float(fetcher.get_index_ltp(name) or 0)
            except Exception:
                spot_now = 0.0
            trader.manage_open_trade(underlying_df=window_df, spot_price=spot_now)
            # Keep dashboard index label fresh between candle boundaries
            if spot_now > 0:
                update_kronos_live_state({
                    "active_index": name,
                    "spot_price": spot_now,
                })
            time.sleep(1)

        except KeyboardInterrupt:
            logger.info("Stopping Kronos Bot...")
            break
        except Exception as e:
            logger.error(f"Main loop error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()

