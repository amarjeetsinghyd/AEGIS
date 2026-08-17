import os
import sys
import time
import queue
from datetime import datetime, timedelta, time as dt_time
import threading

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

os.environ["BOT_RUNTIME"] = "astra"

import astra_bot.config.settings  # noqa: F401

from common.logger import get_logger
from common.broker import ShoonyaAdapter
from common.ipc import update_astra_live_state, SHARED_TICK_FILE
from astra_bot.execution.data_fetcher import DataFetcher
from astra_bot.core.footprint_compiler import FootprintCompiler
from astra_bot.strategy.evaluator import ChainFootprintEvaluator
from astra_bot.execution.astra_execution_manager import AstraExecutionManager
from astra_bot.core.gamma_burst_detector import GammaBurstDetector

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
                logger.info(f"[{name}] Subscribed to Cash Index ({spot_exch}) + {len(opt_tokens)} {exch_seg} Options")
    except Exception as e:
        logger.error(f"Pre-market WebSocket setup warning: {e}")

def build_minute_digest(now, sig, reason, compiled, evaluator, compiler, trader, pending_signal, gamma_detector=None):
    delta = getattr(evaluator, "last_chain_delta", 0.0)
    flow = sum((d.get("total_buy_vol", 0) + d.get("total_sell_vol", 0)) for d in compiled.values())
    spot = getattr(compiler, "current_spot", 0.0)
    sbc = getattr(compiler, "last_spot_bar_chg", 0.0)
    
    seg = [f"[{now.strftime('%H:%M')}] d{delta*100:+.1f}%",
           f"flow {flow:.0f}",
           f"spot {spot:.0f}({sbc:+.0f})"]
           
    if gamma_detector:
        # Get 1-minute max telemetry if available
        if hasattr(gamma_detector, 'get_and_reset_1m_telemetry'):
            t = gamma_detector.get_and_reset_1m_telemetry()
            z_live = t["max_z_live"]
            z_shad = t["max_z_shadow"]
            vpin_passes = t["vpin_passes"]
            max_flicker = t.get("max_flicker", 0)
        else:
            z_live = getattr(gamma_detector, "last_burst_z_live", 0.0)
            z_shad = getattr(gamma_detector, "last_burst_z_shadow", 0.0)
            vpin_passes = 1 if getattr(gamma_detector, "last_vpin_gate", False) else 0
            max_flicker = 0
            
        iv = getattr(gamma_detector, "ema_sigma", 0.0) * 100
        
        v_icon = "PASS" if vpin_passes > 0 else "FAIL"
        
        seg.append(f"Z_max: {z_live:.1f} (shd: {z_shad:.1f})")
        seg.append(f"VPIN: {v_icon}")
        seg.append(f"Flicker: {max_flicker}/3")
        seg.append(f"IV: {iv:.1f}%")

        if hasattr(gamma_detector, 'token_map') and spot > 0:
            try:
                step = getattr(gamma_detector, 'strike_step', 100.0)
                atm = round(spot / step) * step
                otm1_ce_stk = atm + step
                otm1_pe_stk = atm - step
                
                otm_ce_price, otm_ce_pc = 0.0, 0.0
                otm_pe_price, otm_pe_pc = 0.0, 0.0
                
                params = trader.get_instrument_params() if hasattr(trader, 'get_instrument_params') else {}
                opt_exch = "BFO" if "SENSEX" in params.get("index_name", "") else "NFO"
                mdp = getattr(getattr(trader, "broker", None), "market_data_provider", None)
                
                if mdp:
                    if not hasattr(build_minute_digest, "last_opt_ltp"):
                        build_minute_digest.last_opt_ltp = {}
                        
                    for tk, info in gamma_detector.token_map.items():
                        if info['strike'] == otm1_ce_stk and info['side'] == 0:
                            q = mdp.get_quote(opt_exch, tk)
                            if q:
                                otm_ce_price = float(q.get("ltp", 0) or 0)
                                prev_ce = build_minute_digest.last_opt_ltp.get(tk, otm_ce_price)
                                otm_ce_chg = (otm_ce_price - prev_ce) / prev_ce * 100 if prev_ce > 0 else 0.0
                                build_minute_digest.last_opt_ltp[tk] = otm_ce_price
                        elif info['strike'] == otm1_pe_stk and info['side'] == 1:
                            q = mdp.get_quote(opt_exch, tk)
                            if q:
                                otm_pe_price = float(q.get("ltp", 0) or 0)
                                prev_pe = build_minute_digest.last_opt_ltp.get(tk, otm_pe_price)
                                otm_pe_chg = (otm_pe_price - prev_pe) / prev_pe * 100 if prev_pe > 0 else 0.0
                                build_minute_digest.last_opt_ltp[tk] = otm_pe_price
                    
                    # Get Net Option OI Delta for the last minute
                    net_oi, pe_oi, ce_oi = 0.0, 0.0, 0.0
                    if hasattr(mdp, 'get_and_reset_option_net_delta_oi'):
                        try:
                            net_oi, pe_oi, ce_oi = mdp.get_and_reset_option_net_delta_oi()
                        except Exception:
                            pass
                            
                    seg.append(f"NetOI: {net_oi/1000:+.1f}k (C:{ce_oi/1000:+.1f}k P:{pe_oi/1000:+.1f}k)")
                    seg.append(f"OTM1: CE{int(otm1_ce_stk)} {otm_ce_price:.1f}({otm_ce_chg:+.1f}%) | PE{int(otm1_pe_stk)} {otm_pe_price:.1f}({otm_pe_chg:+.1f}%)")

            except Exception as e:
                logger.error(f"OTM tracker error: {e}")

    if pending_signal:
        seg.append(f"ARMED {pending_signal['type']}")
    if trader.has_open_trade():
        leg = trader.trade_context.legs[0]
        chg = ((leg.current_price - leg.entry_price) / leg.entry_price * 100) if leg.entry_price else 0.0
        seg.append(f"OPEN {leg.symbol} {chg:+.1f}%")
    return " | ".join(seg)

def depth_guard_passes(broker, token, opt_type):
    """Fallback dummy depth guard to prevent execution crashes."""
    return True

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
        
        # Gamma Burst Detector (Microstructure Engine)
        gamma_detector = GammaBurstDetector()
        
        logger.info(f"[Astra] Initialized Gamma Burst Engine")
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
                    logger.info(f"[{name}] Subscribed to Cash Index ({spot_exch}) + {len(opt_tokens)} {exch_seg} Options")
        except Exception as e:
            logger.error(f"Startup WebSocket subscription warning: {e}")
    elif is_pre_market(now_dt):
        perform_premarket_handshake(broker, fetcher)
        
    logger.info("Astra V2 Bot is ready and running!")
    
    last_minute = datetime.now().minute
    step = 100.0 if "SENSEX" in fetcher.get_active_instrument()[0] else 50.0
    
    # Attach tick listener for Gamma Burst Detector now that step is defined
    if hasattr(broker, "market_data_provider"):
        broker.market_data_provider.add_tick_listener(
            lambda msg: gamma_detector.process_raw_tick(msg, strike_step=step)
        )
        
    last_subscribed_opts = set()
    last_atm_strike = 0.0
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
                        
                        # Gamma Burst Detector - ATM Shift Handling
                        if atm_strike != last_atm_strike:
                            shift_steps = int((atm_strike - last_atm_strike) / step) if last_atm_strike > 0 else 0
                            gamma_detector.shift_atm(shift_steps, atm_strike)
                            last_atm_strike = atm_strike
                            
                        # Build Token Map for Gamma Detector
                        token_map = {}
                        for _, row in weekly_opts.iterrows():
                            tkn = str(row['token'])
                            stk = float(row.get('strike', 0))
                            sym = str(row.get('symbol', ''))
                            if stk > 0:
                                side = 0 if 'CE' in sym or ('C' in sym and sym[-1].isdigit()) else 1
                                token_map[tkn] = {'side': side, 'strike': stk}
                        gamma_detector.set_token_map(token_map)
                        logger.info(f"Gamma detector token map updated. Size: {len(token_map)}")
                        
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
                                new_tokens = all_opts_set - last_subscribed_opts if last_subscribed_opts else all_opts_set
                                broker.market_data_provider.subscribe(list(new_tokens), dynamic_exch, feed_type="d")
                                if last_subscribed_opts:
                                    logger.info(f"ATM shifted to {atm_strike}. Appended {len(new_tokens)} new strikes to live feed.")
                                else:
                                    logger.info(f"Initial setup: Subscribed to {len(all_opts_set)} options for ATM {atm_strike}.")
                                last_subscribed_opts = all_opts_set

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
                        
                        digest = build_minute_digest(now, sig, reason, compiled, evaluator, compiler, trader, pending_signal, gamma_detector)
                        logger.info(digest)
                        
                        # Removed Gamma Burst block from 1-minute boundary
                        # IPC State Update
                        state_payload = {
                            "active_index": fetcher.get_active_instrument()[0],
                            "spot_price": spot_price,
                            "chain_delta": getattr(evaluator, "last_chain_delta", 0.0),
                            "l2_wall_size": getattr(evaluator, "last_wall_size", 0.0),
                            "signal_status": sig,
                            "reasoning": digest,
                            "kinematic_ready": True,
                            "gamma_burst_z_live": gamma_detector.last_burst_z_live,
                            "gamma_burst_z_shadow": gamma_detector.last_burst_z_shadow,
                            "vpin_gate": gamma_detector.last_vpin_gate,
                            "gamma_pressure": gamma_detector.last_gamma_pressure,
                            "gamma_signal": gamma_detector.last_signal
                        }
                        update_astra_live_state(state_payload)
            
            # Fast-Path Mid-Minute Gamma Burst Execution
            if gamma_detector.last_signal == "GAMMA_BURST" and not pending_signal and not trader.has_open_trade():
                spot_price = getattr(compiler, "current_spot", 0.0) if compiler else 0.0
                if spot_price > 0:
                    side = "CALL" if gamma_detector.last_direction == "UP" else "PUT"
                    otype = "CE" if side == "CALL" else "PE"
                    
                    # Find nearest ATM token from the active token map instead of waiting for footprints
                    atm_strike = round(spot_price / step) * step
                    best_token = None
                    if hasattr(gamma_detector, 'token_map'):
                        # Look for exact ATM token for the side
                        for tk, info in gamma_detector.token_map.items():
                            if info['strike'] == atm_strike and ((side == "CALL" and info['side'] == 0) or (side == "PUT" and info['side'] == 1)):
                                best_token = tk
                                break
                    
                    if best_token:
                        pending_signal = {
                            "type": side, "target_token": best_token,
                            "spot0": spot_price, "prem0": compiler.get_last_lp(best_token) if compiler else 0.0,
                            "arm_time": now, "setup": "GAMMA_BURST",
                            "iv": getattr(gamma_detector, "ema_sigma", 0.15)
                        }
                        logger.info(f"🔥 FAST-PATH GAMMA BURST SIGNAL INJECTED: {side} ATM token={best_token}")
                        # Immediately clear the signal so we don't repeatedly inject it in the fast loop
                        gamma_detector.last_signal = None
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
                        if not depth_guard_passes(broker, token, pending_signal["type"]):
                            router.trap_guard_block()
                            logger.warning(f"TRAP_GUARD_BLOCK {token}")
                            pending_signal = None
                            continue
                        router.trigger_armed()
                        trader.execute_entry(pending_signal["type"], current_spot, target_token=token)
                        pending_signal = None

            # Manage Open Trades (Stop Loss / Take Profit tracking)
            if trader.has_open_trade():
                spot = getattr(compiler, "current_spot", 0.0)
                trader.manage_open_trade(spot_price=spot)
                
                # V4.3 Structure Engine live exit checks (LIVE mode only)
                if STRUCTURE_MODE == "live" and trader.has_open_trade():
                    side = trader.trade_context.signal_type
                    px = compiler.current_spot
                    if structure.trail_exit(side, px):
                        trader._cancel_tp()
                        trader._close_all_legs("9-EMA Trail Exit")
                    elif structure.invalidation(side, px):
                        trader._cancel_tp()
                        trader._close_all_legs("VWAP Band SL")
            
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
