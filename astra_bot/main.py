import os
import sys
import time
import queue
from datetime import datetime, timedelta, time as dt_time
import threading

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import astra_bot.config.settings  # noqa: F401

from common.logger import get_logger
from common.broker import ShoonyaAdapter
from common.ipc import update_astra_live_state
from astra_bot.execution.data_fetcher import DataFetcher
from astra_bot.core.footprint_compiler import FootprintCompiler
from astra_bot.strategy.evaluator import ChainFootprintEvaluator
from astra_bot.execution.astra_execution_manager import AstraExecutionManager

from common.market.market_calendar import (
    is_market_open,
    is_bot_active_hours,
    is_pre_market,
    sleep_until_next_premarket
)

logger = get_logger("astra_bot_main")

def perform_premarket_handshake(broker, fetcher):
    logger.info("08:55 AM IST PRE-MARKET WAKEUP: Initiating Handshake & Setup")
    try:
        if hasattr(broker, "session_provider"):
            broker.session_provider._restore_or_login()
    except Exception as e:
        logger.error(f"Pre-market session renewal warning: {e}")

    try:
        name, exch_seg = fetcher.get_active_instrument()
        token, _, spot_exch = fetcher.get_cash_index_token()
        
        if hasattr(broker, "market_data_provider"):
            broker.market_data_provider._ensure_ws_started()
            broker.market_data_provider.subscribe([token], spot_exch, feed_type="t")
            
            weekly_opts = fetcher.get_weekly_option_tokens()
            if not weekly_opts.empty:
                opt_tokens = [str(t) for t in weekly_opts['token'].tolist()]
                broker.market_data_provider.subscribe(opt_tokens, exch_seg, feed_type="d")
    except Exception as e:
        logger.error(f"Pre-market WebSocket setup warning: {e}")

def build_minute_digest(now, sig, reason, compiled, evaluator, compiler, trader, pending_signal):
    delta = getattr(evaluator, "last_chain_delta", 0.0)
    flow = sum((d.get("total_buy_vol", 0) + d.get("total_sell_vol", 0)) for d in compiled.values())
    spot = getattr(compiler, "current_spot", 0.0)
    sbc = getattr(compiler, "last_spot_bar_chg", 0.0)
    seg = [f"[{now.strftime('%H:%M')}] d{delta*100:+.1f}%",
           f"flow {flow:.0f}",
           f"spot {spot:.0f}({sbc:+.0f})"]
    if pending_signal:
        seg.append(f"ARMED {pending_signal['type']}")
    if trader.has_open_trade():
        leg = trader.trade_context.legs[0]
        chg = ((leg.current_price - leg.entry_price) / leg.entry_price * 100) if leg.entry_price else 0.0
        seg.append(f"OPEN {leg.symbol} {chg:+.1f}%")
    seg.append(f"=> {sig} | {reason}")
    return " | ".join(seg)

def main():
    logger.info("==============================================")
    logger.info("STARTING ASTRA V2 ORDER FLOW BOT (QUIET MODE)")
    logger.info("==============================================")
    
    import logging
    import contextlib
    logging.getLogger("footprint_compiler").setLevel(logging.WARNING)
    logging.getLogger("shoonya_adapter").setLevel(logging.WARNING)
    logging.getLogger("astra_evaluator").setLevel(logging.WARNING)
    logging.getLogger("strike_selector").setLevel(logging.WARNING)
    
    try:
        with contextlib.redirect_stdout(open(os.devnull, 'w')):
            broker = ShoonyaAdapter()
            fetcher = DataFetcher(broker)
        
        # Override broker's default ring buffer with our V2 1-min Compiler
        trader = AstraExecutionManager(broker, fetcher)
        compiler = FootprintCompiler(tick_callback=trader.process_tick_for_exit)
        if hasattr(broker, "market_data_provider"):
            broker.market_data_provider.footprint_compiler = compiler
            
        evaluator = ChainFootprintEvaluator()
    except Exception as e:
        logger.critical(f"Initialization failed: {e}")
        sys.exit(1)
        
    now_dt = datetime.now()
    if is_market_open(now_dt):
        logger.info("Market is open. Restoring broker session...")
        try:
            if hasattr(broker, "session_provider"):
                broker.session_provider._restore_or_login()
            
            name, exch_seg = fetcher.get_active_instrument()
            token, _, spot_exch = fetcher.get_cash_index_token()
            if hasattr(broker, "market_data_provider"):
                broker.market_data_provider._index_token = str(token)
                broker.market_data_provider._ensure_ws_started()
                broker.market_data_provider.subscribe([token], spot_exch, feed_type="t")
                weekly_opts = fetcher.get_weekly_option_tokens()
                if not weekly_opts.empty:
                    opt_tokens = [str(t) for t in weekly_opts['token'].tolist()]
                    broker.market_data_provider.subscribe(opt_tokens, exch_seg, feed_type="d")
        except Exception as e:
            logger.error(f"Startup WebSocket subscription warning: {e}")
    elif is_pre_market(now_dt):
        perform_premarket_handshake(broker, fetcher)
        
    logger.info("Astra V2 Bot is ready and running!")
    
    last_minute = datetime.now().minute
    step = 100.0 if "SENSEX" in fetcher.get_active_instrument()[0] else 50.0
    last_subscribed_opts = set()
    pending_signal = None

    while True:
        try:
            now = datetime.now()
            
            if not is_bot_active_hours(now):
                logger.info("Bot active runtime closed. Entering deep sleep...")
                sleep_until_next_premarket()
                perform_premarket_handshake(broker, fetcher)
                continue

            if is_pre_market(now):
                time.sleep(1)
                continue

            # Check if 1-minute boundary has crossed
            if now.minute != last_minute:
                # 1-MINUTE BOUNDARY TRIGGER
                last_minute = now.minute
                
                spot_price = getattr(compiler, "current_spot", 0.0) if compiler else 0.0
                if spot_price == 0.0:
                    spot_price = fetcher.get_index_ltp()
                    logger.debug(f"[Astra V2] Fallback spot price via REST: {spot_price}")
                    
                if spot_price > 0:
                    # Update ATM tracked strikes
                    compiler.trigger_strike_rollover(spot_price, strike_step=step)
                    
                    weekly_opts = fetcher.get_weekly_option_tokens()
                    if not weekly_opts.empty:
                        atm_strike = round(spot_price / step) * step
                        # Evaluation window is ±6 (6+6), Consideration window is ±3 (3+3)
                        evaluation_strikes = [atm_strike + (i * step) for i in range(-6, 7)]
                        consideration_strikes = [atm_strike + (i * step) for i in range(-3, 4)]
                        
                        ce_df = weekly_opts[weekly_opts['symbol'].str.contains(r'CE|C\d+$')]
                        pe_df = weekly_opts[weekly_opts['symbol'].str.contains(r'PE|P\d+$')]
                        
                        # Subscribe to a wider net (ATM±10) to handle sudden ATM jumps (Raw Data)
                        sub_ce_tokens = set(ce_df['token'].tolist())
                        sub_pe_tokens = set(pe_df['token'].tolist())
                        all_opts_set = sub_ce_tokens.union(sub_pe_tokens)
                        
                        if hasattr(broker, "market_data_provider"):
                            broker.market_data_provider._option_ce_tokens = sub_ce_tokens
                            broker.market_data_provider._option_pe_tokens = sub_pe_tokens
                            
                            if all_opts_set != last_subscribed_opts:
                                _, dynamic_exch = fetcher.get_active_instrument()
                                broker.market_data_provider.subscribe(list(all_opts_set), dynamic_exch, feed_type="d")
                                last_subscribed_opts = all_opts_set
                                logger.info(f"Dynamically subscribed to {len(all_opts_set)} options for new ATM {atm_strike}.")

                        # Pukka Calculation (Evaluate) 6+6 Strikes
                        if 'strike' in weekly_opts.columns:
                            ce_df_eval = ce_df[ce_df['strike'].isin(evaluation_strikes)]
                            pe_df_eval = pe_df[pe_df['strike'].isin(evaluation_strikes)]
                            
                        ce_tokens_eval = set(ce_df_eval['token'].tolist())
                        pe_tokens_eval = set(pe_df_eval['token'].tolist())
                        
                        # Compile footprint for the past minute on 6+6 strikes
                        compiled = compiler.get_compiled_footprints(ce_tokens_eval, pe_tokens_eval)
                        
                        # Evaluate Strategy with 3+3 consideration
                        spot_bar_chg = getattr(compiler, "last_spot_bar_chg", 0.0)
                        sig, reason, target_token = evaluator.evaluate_1m_boundary(
                            compiled, spot_price, now, spot_bar_chg=spot_bar_chg, consideration_strikes=consideration_strikes
                        )
                        
                        digest = build_minute_digest(now, sig, reason, compiled, evaluator, compiler, trader, pending_signal)
                        logger.info(digest)
                        
                        # IPC State Update
                        state_payload = {
                            "active_index": fetcher.get_active_instrument()[0],
                            "spot_price": spot_price,
                            "chain_delta": getattr(evaluator, "last_chain_delta", 0.0),
                            "l2_wall_size": getattr(evaluator, "last_wall_size", 0.0),
                            "signal_status": sig,
                            "reasoning": digest,
                            "kinematic_ready": True
                        }
                        update_astra_live_state(state_payload)
                        
                        # Arm Pending Signal
                        if sig in ["CALL", "PUT"] and not trader.has_open_trade():
                            logger.info(f"Signal ARMED: {sig}. Waiting for confirmation block to clear...")
                            pending_signal = {
                                "type": sig,
                                "target_token": target_token,
                                "spot0": spot_price,
                                "prem0": compiler.get_last_lp(target_token) if compiler else 0.0,
                                "arm_time": now
                            }
            
            # Pending Entry Window Evaluation (~0.5s polling)
            if pending_signal and not trader.has_open_trade():
                age_sec = (now - pending_signal["arm_time"]).total_seconds()
                if age_sec > 120:
                    logger.warning(f"Pending {pending_signal['type']} signal EXPIRED (>{age_sec:.0f}s). No confirmation.")
                    pending_signal = None
                else:
                    token = pending_signal["target_token"]
                    # ✅ FIX: Read live state directly. DO NOT call get_compiled_footprints!
                    current_prem = compiler.get_last_lp(token) if compiler else 0.0
                    current_spot = compiler.current_spot if compiler else 0.0
                    
                    d_spot = current_spot - pending_signal["spot0"]
                    d_prem = (current_prem - pending_signal["prem0"]) if (current_prem > 0 and pending_signal["prem0"] > 0) else 0.0
                    dirn = 1 if pending_signal["type"] == "CALL" else -1

                    # 1. Invalidation (Price runs against us)
                    if dirn * d_spot <= -8.0 or d_prem <= -0.25:
                        logger.info(f"SIGNAL INVALIDATED: spot {d_spot:+.1f} / prem {d_prem:+.2f}")
                        pending_signal = None
                        continue
                        
                    # 2. Trigger (Price confirms effect)
                    if dirn * d_spot >= 3.0 or d_prem >= 0.10:
                        logger.info(f"🚀 CONFIRMATION TRIGGER: spot {d_spot:+.1f} / prem {d_prem:+.2f}")
                        trader.execute_entry(pending_signal["type"], current_spot, target_token=token)
                        pending_signal = None

            # Manage Open Trades (Stop Loss / Take Profit tracking)
            if trader.has_open_trade():
                spot = getattr(compiler, "current_spot", 0.0)
                trader.manage_open_trade(spot_price=spot)
                
            # Instant WS Exit Check or 500ms heartbeat
            try:
                reason = trader.exit_signal_queue.get(timeout=0.5)
                if trader.has_open_trade():
                    logger.warning(f"[AstraEM] Executing WS Exit Signal: {reason}")
                    trader._cancel_tp()
                    trader._close_all_legs(reason)
            except queue.Empty:
                pass

        except Exception as e:
            logger.error(f"Main loop exception: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
