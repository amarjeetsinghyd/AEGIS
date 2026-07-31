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

from kronos_bot.core.logger import get_logger
from kronos_bot.core.data_fetcher import DataFetcher
from kronos_bot.execution.execution_manager import ExecutionManager
from kronos_bot.strategy.signal_generator import KronosSignalGenerator
from kronos_bot.dashboard import update_kdo_live_state
from kronos_bot.config.settings import BROKER

from kronos_bot.config.market_calendar import (
    is_market_open,
    is_bot_active_hours,
    can_enter_new_trades,
    is_pre_market,
    sleep_until_next_premarket
)

logger = get_logger("kronos_bot_main")
SHARED_TICK_FILE = "/dev/shm/shoonya_live_ticks.json"


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
        from kronos_bot.core.shoonya_adapter import ShoonyaAdapter
        broker = ShoonyaAdapter()
        fetcher = DataFetcher(broker)
        trader = ExecutionManager(broker, fetcher)
        
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
    last_premarket_log_time = 0
    
    name, exch_seg = fetcher.get_active_instrument()
    logger.info(f"08:55 AM PRE-MARKET HANDSHAKE: Active Index = {name} ({exch_seg}). Pre-market setup complete.")

    while True:
        try:
            now = datetime.now()
            
            # 1. Check active bot runtime
            if not is_bot_active_hours(now):
                if not eod_backfill_done and now.time() >= dt_time(15, 31):
                    logger.info("==========================================================")
                    logger.info("03:31 PM IST POST-MARKET EOD BACKFILL SERVICE INITIATING")
                    logger.info("==========================================================")
                    fetcher.rest_backfill_today_candles("NIFTY")
                    fetcher.rest_backfill_today_candles("SENSEX")
                    eod_backfill_done = True
                    logger.info("Post-Market EOD backfill completed for both NIFTY and SENSEX. Entering deep sleep...")
                
                sleep_until_next_premarket()
                eod_backfill_done = False
                opening_trade_evaluated = False
                continue

            # 2. Pre-market logging heartbeat (08:55 AM to 09:15 AM IST)
            if is_pre_market(now):
                if time.time() - last_premarket_log_time >= 30:
                    last_premarket_log_time = time.time()
                    spot_price = fetcher.get_index_ltp()
                    target_time = datetime.combine(now.date(), dt_time(9, 15, 0))
                    secs_to_open = (target_time - now).total_seconds()
                    logger.info(f"[PRE-MARKET STANDBY] Active Index: {name} (LTP: {spot_price:.2f}). Standing by until 09:15:00 AM IST market open ({int(secs_to_open)}s remaining)...")
                time.sleep(2)
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
                
                spot_price = fetcher.get_index_ltp()
                if spot_price <= 0:
                    logger.warning("Could not fetch spot index price. Retrying...")
                    time.sleep(2)
                    continue

                synth_vol = 0
                if os.path.exists(SHARED_TICK_FILE):
                    try:
                        with open(SHARED_TICK_FILE, "r") as f:
                            d_hub = json.load(f)
                        key = "synthetic_nifty_volume" if name == "NIFTY" else "synthetic_sensex_volume"
                        synth_vol = int(d_hub.get(key, 0))
                    except Exception:
                        pass

                closed_time_short = now.strftime("%H:%M:00")
                logger.info(f"[SYNTHETIC VOLUME] Compiled live 5-minute volume for closed candle [{closed_time_short}]: {synth_vol:,} shares.")
                logger.info(f"Combining Price & Volume into sliding window for candle [{candle_time_str}] (Close: {spot_price:.2f} | Vol: {synth_vol:,})...")

                # Fetch true completed 5-min REST candle to replace provisional opening candle
                target_token, _, target_exch = fetcher.get_cash_index_token()
                open_p, high_p, low_p, close_p = spot_price, spot_price, spot_price, spot_price
                try:
                    rest_series = fetcher.broker.historical_provider.get_historical_candles(
                        exchange=target_exch, token=target_token, timeframe="5", start_time=now - timedelta(minutes=15), end_time=now
                    )
                    if isinstance(rest_series, list) and len(rest_series) > 0:
                        latest_c = rest_series[0]
                        open_p = float(latest_c.get('into', spot_price))
                        high_p = float(latest_c.get('inth', spot_price))
                        low_p = float(latest_c.get('intl', spot_price))
                        close_p = float(latest_c.get('intc', spot_price))
                except Exception:
                    pass

                mean_price = (open_p + high_p + low_p + close_p) / 4.0
                amount = synth_vol * mean_price
                new_candle = {
                    "timestamp": pd.to_datetime(candle_time_str),
                    "open": open_p,
                    "high": high_p,
                    "low": low_p,
                    "close": close_p,
                    "volume": synth_vol,
                    "amount": amount
                }
                fetcher.append_live_candle(new_candle)
                
                # Kronos Prediction
                window_df = fetcher.get_window_df()
                pred_res = signal_gen.predict(window_df, pred_len=1, threshold=0.0010)
                
                ml_sig = pred_res["signal"]
                pred_close = pred_res["pred_close"]
                ret_pct = pred_res["return_pct"]
                
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

                logger.info(f"Signal is {final_sig} ({ret_pct*100:+.3f}% | Current: {spot_price:.2f} -> Pred: {pred_close:.2f}). Standing by.")
                
                update_kdo_live_state({
                    "active_index": name,
                    "spot_price": spot_price,
                    "predicted_close": pred_close,
                    "predicted_return_pct": ret_pct * 100.0,
                    "signal": final_sig,
                    "net_delta_oi": net_delta_oi,
                    "last_candle": candle_time_str
                })

                if final_sig in ["CALL", "PUT"] and can_enter_new_trades(now):
                    logger.info(f"🔥 EXECUTING LIVE BROKERAGE ENTRY FOR {final_sig} SIGNAL 🔥")
                    trader.execute_kdo_entry(final_sig, spot_price, net_delta_oi)

                sleep_sec = 300.0 - (now.minute % 5 * 60 + now.second)
                logger.info(f"Sleeping for {max(sleep_sec, 1.0):.2f}s until next candle boundary...")

            oi_meta = get_shared_oi_delta_details()
            trader.manage_open_trade(net_delta_velocity=oi_meta["net_delta_oi"])
            time.sleep(1)

        except KeyboardInterrupt:
            logger.info("Stopping Kronos Bot...")
            break
        except Exception as e:
            logger.error(f"Main loop error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()

