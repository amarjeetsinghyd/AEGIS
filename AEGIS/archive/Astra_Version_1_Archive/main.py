import os
import sys
import time
from datetime import datetime, timedelta, time as dt_time
import pandas as pd
import numpy as np

# Ensure project root is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Bind astra runtime paths before other common imports
import astra_bot.config.settings  # noqa: F401

from common.logger import get_logger
from astra_bot.core.data_fetcher import DataFetcher
from common.execution import ExecutionManager
from astra_bot.core.microstructure_engine import KineticMicrostructureEngine, bsm_vectorized_gex
from common.ipc import update_kdo_live_state, is_kill_switch_active
from common.broker import ShoonyaAdapter
from astra_bot.config.settings import BROKER, COOLDOWN_MINUTES, DATA_DIR

from common.market.market_calendar import (
    is_market_open,
    is_bot_active_hours,
    can_enter_new_trades,
    is_pre_market,
    is_trading_day,
    sleep_until_next_premarket
)

logger = get_logger("astra_bot_main")


def perform_premarket_handshake(broker, fetcher):
    """
    Executes the 08:55 AM pre-market setup:
    1. Re-authenticates broker OAuth session tokens FIRST.
    2. Subscribes WebSocket feed to Index Spot and dynamic Option Chain tokens.
    """
    logger.info("==========================================================")
    logger.info("08:55 AM IST PRE-MARKET WAKEUP: Initiating Handshake & Setup")
    logger.info("==========================================================")
    
    # 1. Session renewal FIRST
    try:
        if hasattr(broker, "session_provider") and hasattr(broker.session_provider, "_restore_or_login"):
            logger.info("Refreshing broker authentication session tokens...")
            broker.session_provider._restore_or_login()
    except Exception as e:
        logger.error(f"Pre-market session renewal warning: {e}")

    # 2. Subscribe WebSocket stream for Spot & Option Chain
    try:
        name, exch_seg = fetcher.get_active_instrument()
        token, _, spot_exch = fetcher.get_cash_index_token()
        
        if hasattr(broker, "market_data_provider"):
            broker.market_data_provider._ensure_ws_started()
            broker.market_data_provider.subscribe([token], spot_exch)
            logger.info(f"Subscribed {name} Index Spot token {token} on {spot_exch}.")
            
            weekly_opts = fetcher.get_weekly_option_tokens(target_index=name)
            if not weekly_opts.empty:
                opt_tokens = [str(t) for t in weekly_opts['token'].tolist()]
                broker.market_data_provider.subscribe(opt_tokens, exch_seg)
                if hasattr(broker.market_data_provider, "register_option_tokens"):
                    ce_tokens = [str(t) for t in weekly_opts[weekly_opts['symbol'].str.contains(r'CE|C\d+$', regex=True)]['token'].tolist()]
                    pe_tokens = [str(t) for t in weekly_opts[weekly_opts['symbol'].str.contains(r'PE|P\d+$', regex=True)]['token'].tolist()]
                    broker.market_data_provider.register_option_tokens(ce_tokens, pe_tokens)
                logger.info(f"Subscribed {len(opt_tokens)} option tokens for {name} on {exch_seg}.")
    except Exception as e:
        logger.error(f"Pre-market WebSocket setup warning: {e}")
        
    logger.info("Pre-market setup complete! Standing by until market opens at 09:15:00 AM IST...")


def main():
    logger.info("==============================================")
    logger.info("STARTING KINETIC DELTA ORDERFLOW (KDO) BOT")
    logger.info("==============================================")
    
    # 1. Initialize broker, execution manager & microstructure engine
    try:
        if BROKER.upper() == "SHOONYA":
            broker = ShoonyaAdapter()
        else:
            broker = ShoonyaAdapter()
        fetcher = DataFetcher(broker)
        trader = ExecutionManager(broker, fetcher)
        micro_engine = KineticMicrostructureEngine()
    except Exception as e:
        logger.critical(f"Initialization failed: {e}")
        sys.exit(1)
        
    # 2. Handle startup timing state
    now_dt = datetime.now()
    weekly_opts = pd.DataFrame()
    if is_market_open(now_dt):
        logger.info("Market is open. Restoring broker session and subscribing WebSocket feeds...")
        try:
            if hasattr(broker, "session_provider") and hasattr(broker.session_provider, "_restore_or_login"):
                broker.session_provider._restore_or_login()
        except Exception as e:
            logger.error(f"Market open session renewal warning: {e}")

        try:
            name, exch_seg = fetcher.get_active_instrument()
            token, _, spot_exch = fetcher.get_cash_index_token()
            if hasattr(broker, "market_data_provider"):
                broker.market_data_provider._ensure_ws_started()
                broker.market_data_provider.subscribe([token], spot_exch)
                
                weekly_opts = fetcher.get_weekly_option_tokens(target_index=name)
                if not weekly_opts.empty:
                    opt_tokens = [str(t) for t in weekly_opts['token'].tolist()]
                    broker.market_data_provider.subscribe(opt_tokens, exch_seg)
                    if hasattr(broker.market_data_provider, "register_option_tokens"):
                        ce_tokens = [str(t) for t in weekly_opts[weekly_opts['symbol'].str.contains(r'CE|C\d+$', regex=True)]['token'].tolist()]
                        pe_tokens = [str(t) for t in weekly_opts[weekly_opts['symbol'].str.contains(r'PE|P\d+$', regex=True)]['token'].tolist()]
                        broker.market_data_provider.register_option_tokens(ce_tokens, pe_tokens)
                    logger.info(f"Subscribed {len(opt_tokens)} option tokens for {name} on {exch_seg}.")
        except Exception as e:
            logger.error(f"Startup WebSocket subscription warning: {e}")
    elif is_pre_market(now_dt):
        perform_premarket_handshake(broker, fetcher)
    else:
        logger.info("Market is currently closed. Will enter deep sleep until pre-market.")
        
    logger.info("KDO Bot is ready and running!")
    
    # --- Event-Driven Main Loop ---
    ring_buf = getattr(broker.market_data_provider, "ring_buffer_manager", None)
    name, exch_seg = fetcher.get_active_instrument()
    step = 100.0 if "SENSEX" in name else 50.0

    while True:
        try:
            now = datetime.now()
            
            # Check active bot runtime (08:55 AM to 15:31 PM IST)
            if not is_bot_active_hours(now):
                logger.info(f"Bot active runtime closed at {now.strftime('%H:%M:%S')}. Entering deep sleep...")
                sleep_until_next_premarket()
                perform_premarket_handshake(broker, fetcher)
                name, exch_seg = fetcher.get_active_instrument()
                step = 100.0 if "SENSEX" in name else 50.0
                try:
                    weekly_opts = fetcher.get_weekly_option_tokens(target_index=name)
                except Exception as e:
                    logger.error(f"Post-wake weekly options refresh failed: {e}")
                    weekly_opts = pd.DataFrame()
                continue

            # If pre-market (08:55 AM to 09:15 AM IST), wait for market open (09:15:02 AM IST)
            if is_pre_market(now):
                market_open_dt = datetime.combine(now.date(), dt_time(9, 15, 2))
                sleep_to_open = (market_open_dt - now).total_seconds()
                if sleep_to_open > 0:
                    time.sleep(min(sleep_to_open, 5))
                    continue

            # Spot price query
            spot_price = getattr(ring_buf, "current_spot", 0.0) if ring_buf else fetcher.get_index_ltp()
            if spot_price > 0 and ring_buf:
                # 1. Monitor Strike Rollover Kinematic Pause
                ring_buf.trigger_strike_rollover_pause(spot_price, strike_step=step)
                
                # 2. Evaluate Microstructure Signals
                snapshots = ring_buf.get_latest_snapshots()
                opt_meta = weekly_opts.to_dict("records") if not weekly_opts.empty else []
                
                kdo_result = micro_engine.evaluate_signals(snapshots, opt_meta, spotprice=spot_price)
                
                sig = kdo_result["signal"]
                gex_net = kdo_result["gex_net"]
                v_t = kdo_result["velocity"]
                a_t = kdo_result["acceleration"]

                buffer_ready = ring_buf.has_full_window() if hasattr(ring_buf, "has_full_window") else ring_buf.is_kinematic_ready()
                rollover_transition = ring_buf.is_rollover_transition_active() if hasattr(ring_buf, "is_rollover_transition_active") else False
                rollover_pause = rollover_transition or (not buffer_ready)

                # 3. Translate signal into human readable text for Bloomberg UI
                kill_active = is_kill_switch_active(DATA_DIR)
                if kill_active:
                    status_str = "🛑 EMERGENCY KILL SWITCH ACTIVATED"
                    reason_str = "Emergency Mobile Kill Switch triggered via Web Dashboard. Trading operations halted."
                elif not buffer_ready:
                    status_str = "⏳ KINEMATIC WARMUP (BUILDING 60s WINDOW)"
                    reason_str = f"Collecting continuous 5-second orderflow buckets ({len(snapshots)}/{getattr(ring_buf.buffer, 'maxlen', 12)}) before enabling full kinematic evaluation."
                elif rollover_transition:
                    from_atm = getattr(ring_buf, "last_rollover_from", None)
                    to_atm = getattr(ring_buf, "last_rollover_to", None)
                    status_str = "🔄 STRIKE ROLLOVER (CONTINUITY PRESERVED)"
                    if from_atm is not None and to_atm is not None:
                        reason_str = f"ATM shifted from {from_atm:,.0f} to {to_atm:,.0f}. Ring buffer retained and OI series rebased without blind pause."
                    else:
                        reason_str = "Strike basket transitioned with ring-buffer continuity preserved. Kinematic state remains live."
                elif sig == "CALL":
                    status_str = "🟢 STRONG BULLISH (CALL READY)"
                    reason_str = kdo_result.get("reason", f"Put writing surge detected (A_t = +{a_t:,.0f}) in Negative Gamma Regime.")
                elif sig == "PUT":
                    status_str = "🔴 STRONG BEARISH (PUT READY)"
                    reason_str = kdo_result.get("reason", f"Call writing surge detected (A_t = {a_t:,.0f}) in Negative Gamma Regime.")
                elif "CONFLICT" in kdo_result.get("reason", ""):
                    status_str = "🟠 NEUTRAL (CONFLICT DETECTED)"
                    reason_str = kdo_result.get("reason", "")
                elif gex_net >= 0:
                    status_str = "🟡 NEUTRAL (BLOCKED BY POSITIVE GAMMA)"
                    reason_str = f"Market is in Positive Gamma Regime (GEX = Rs {gex_net:,.0f} >= 0). Volatility is suppressed."
                else:
                    status_str = "🟡 NEUTRAL (CHOPPY / WAIT)"
                    reason_str = kdo_result.get("reason", "No statistically unusual orderflow acceleration detected in the current negative-gamma regime.")

                # 4. Build Sensibull Verification Table (ATM +- 5 strikes)
                atm_strike = round(spot_price / step) * step
                strikes_range = [atm_strike + i * step for i in range(-5, 6)]
                chain_rows = []

                # Compute actual DTE from the nearest weekly expiry (replaces hardcoded 4.0)
                dte_days = 4.0  # safe fallback
                if not weekly_opts.empty and 'expiry_dt' in weekly_opts.columns:
                    try:
                        nearest_expiry = weekly_opts['expiry_dt'].min()
                        if hasattr(nearest_expiry, 'date'):
                            delta = (nearest_expiry.date() - datetime.now().date()).days
                        else:
                            from datetime import date as _date
                            delta = (nearest_expiry - _date.today()).days
                        dte_days = max(float(delta), 0.25)  # floor at 0.25 (expiry day intraday)
                    except Exception:
                        dte_days = 4.0  # fallback on parse error
                T_bsm = dte_days / 365.0

                latest_snap = snapshots[-1] if snapshots else {}
                prices = latest_snap.get("option_prices", {})

                for k in strikes_range:
                    ce_meta = next((m for m in opt_meta if float(m.get("strike", 0)) == k and ("CE" in str(m.get("symbol")) or "C" in str(m.get("symbol")))), {})
                    pe_meta = next((m for m in opt_meta if float(m.get("strike", 0)) == k and ("PE" in str(m.get("symbol")) or "P" in str(m.get("symbol")))), {})
                    
                    ce_tok = str(ce_meta.get("token", ""))
                    pe_tok = str(pe_meta.get("token", ""))

                    ce_ltp = prices.get(ce_tok, ce_meta.get("ltp", 0.0))
                    pe_ltp = prices.get(pe_tok, pe_meta.get("ltp", 0.0))

                    # Query live WebSocket quote cache for live Open Interest (OI)
                    ce_quote = broker.market_data_provider.get_quote(exch_seg, ce_tok) if hasattr(broker, "market_data_provider") else {}
                    pe_quote = broker.market_data_provider.get_quote(exch_seg, pe_tok) if hasattr(broker, "market_data_provider") else {}

                    ce_oi = float(ce_quote.get("oi", ce_quote.get("v", ce_meta.get("oi", 0))) or 0)
                    pe_oi = float(pe_quote.get("oi", pe_quote.get("v", pe_meta.get("oi", 0))) or 0)

                    # If WebSocket hasn't broadcast OI tick yet, fetch live REST quote from Shoonya API
                    if ce_oi == 0 and hasattr(broker, "_api") and ce_tok:
                        try:
                            q = broker._api.get_quotes(exchange=exch_seg, token=ce_tok)
                            if isinstance(q, dict) and q.get("stat") == "Ok" and "oi" in q:
                                ce_oi = float(q["oi"])
                                if ring_buf:
                                    ring_buf.latest_oi[ce_tok] = ce_oi
                        except Exception:
                            pass

                    if pe_oi == 0 and hasattr(broker, "_api") and pe_tok:
                        try:
                            q = broker._api.get_quotes(exchange=exch_seg, token=pe_tok)
                            if isinstance(q, dict) and q.get("stat") == "Ok" and "oi" in q:
                                pe_oi = float(q["oi"])
                                if ring_buf:
                                    ring_buf.latest_oi[pe_tok] = pe_oi
                        except Exception:
                            pass

                    # Quick BSM calculation for display — uses dynamic T_bsm (actual DTE / 365)
                    ce_iv, ce_gamma, _ = bsm_vectorized_gex(spot_price, np.array([k]), np.array([T_bsm]), np.array([max(ce_ltp, 1.0)]), np.array([True]), np.array([max(ce_oi, 10000.0)])) if ce_ltp > 0 else (np.array([0.0]), np.array([0.0]), 0)
                    pe_iv, pe_gamma, _ = bsm_vectorized_gex(spot_price, np.array([k]), np.array([T_bsm]), np.array([max(pe_ltp, 1.0)]), np.array([False]), np.array([max(pe_oi, 10000.0)])) if pe_ltp > 0 else (np.array([0.0]), np.array([0.0]), 0)

                    chain_rows.append({
                        "strike": int(k),
                        "ce_ltp": float(ce_ltp),
                        "ce_oi": float(ce_oi),
                        "ce_iv": float(ce_iv[0]) if len(ce_iv) > 0 else 0.0,
                        "ce_gamma": float(ce_gamma[0]) if len(ce_gamma) > 0 else 0.0,
                        "pe_ltp": float(pe_ltp),
                        "pe_oi": float(pe_oi),
                        "pe_iv": float(pe_iv[0]) if len(pe_iv) > 0 else 0.0,
                        "pe_gamma": float(pe_gamma[0]) if len(pe_gamma) > 0 else 0.0,
                    })

                # 5. Push live state into in-memory dashboard cache (0ms overhead)
                update_kdo_live_state({
                    "active_index": name,
                    "spot_price": spot_price,
                    "gex_net": gex_net,
                    "gex_regime": kdo_result.get("gex_regime", "NEUTRAL_GAMMA"),
                    "velocity": v_t,
                    "acceleration": a_t,
                    "signal_status": status_str,
                    "reasoning": reason_str,
                    "kinematic_ready": ring_buf.is_kinematic_ready(),
                    "rollover_pause": rollover_pause,
                    "option_chain": chain_rows
                })

                # 6. Entry Evaluation Gate
                if ring_buf.is_kinematic_ready() and sig in ["CALL", "PUT"]:
                    if can_enter_new_trades(now) and not trader.has_open_trade():
                        logger.info(f"🚀 KDO SIGNAL TRIGGERED: {sig} | Reason: {kdo_result['reason']}")
                        trader.execute_kdo_entry(sig, spot_price, v_t, decision_payload=kdo_result)

                # 7. Active Position Zero-Order Exit Observer
                if trader.has_open_trade():
                    trader.manage_open_trade(net_delta_velocity=v_t, kdo_state=kdo_result)

            time.sleep(0.1)

        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received. Shutting down gracefully...")
            break
        except Exception as e:
            logger.error(f"Main loop error: {e}")
            time.sleep(1)


if __name__ == "__main__":
    main()
