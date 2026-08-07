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
from astra_bot.core.spot_structure import SpotStructureEngine

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
        
        # V4.3 Structure Engine (shadow mode)
        structure = SpotStructureEngine()
        from collections import deque
        from astra_bot.strategy.setup_detectors import SetupDetectors
        from astra_bot.strategy.signal_router import SignalRouter, RouterState
        from astra_bot.execution.l2_guard import depth_guard_passes
        detectors = SetupDetectors()
        router = SignalRouter()
        delta_hist = deque(maxlen=4)
        STRUCTURE_MODE = os.getenv("ASTRA_STRUCTURE_MODE", "shadow")
        compiler.set_structure_hook(lambda ts, px: structure.on_tick(ts, px))
        
        # V4.3 Synthetic Volume Bridge
        from astra_bot.core.spot_structure import SyntheticVolumeBridge
        vol_bridge = SyntheticVolumeBridge(structure)
        
        logger.info(f"[Astra] Spot Structure Engine initialized in {STRUCTURE_MODE} mode")
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
                        
                        # V4.3 Structure Engine snapshot
                        snap = structure.snapshot()
                        reg = structure.regime(compiler.current_spot)
                        if snap["last_event"] != "NONE":
                            logger.info(f"[STRUCTURE:{STRUCTURE_MODE}] {snap['last_event']} regime={reg} vwap={snap['vwap']} ema9={snap['ema9']} ema200={snap['ema200']}")
                        
                        # V4.4 Router
                        norm_delta = getattr(evaluator, "last_chain_delta", 0.0)
                        delta_hist.append(norm_delta)
                        bars3 = list(structure.candles)[-3:]
                        rstate = RouterState.IDLE
                        if bars3:
                            rstate = router.route(bars3, list(delta_hist), structure, time.time(), detectors)
                            logger.info(f"[ROUTER] ctx={structure.context_state(spot_price)} state={rstate.value} "
                                        f"veto={detectors.is_veto_active(time.time())} tel={router.get_telemetry()}")
                            if rstate == RouterState.ARM and router.armed is not None:
                                side = router.armed.side
                                otype = "CE" if side == "CALL" else "PE"
                                best_token, best_vol = None, 0.0
                                for token, data in compiled.items():
                                    if data.get("option_type") != otype: continue
                                    if consideration_strikes and data.get("strike") not in consideration_strikes: continue
                                    vol = data.get("total_buy_vol", 0) + data.get("total_sell_vol", 0)
                                    if vol > best_vol:
                                        best_vol, best_token = vol, token
                                if best_token is None:
                                    router.expire_armed(time.time())
                                else:
                                    router.armed.spot0 = spot_price
                                    router.armed.prem0 = compiler.get_last_lp(best_token) if compiler else 0.0
                                    router.armed.target_token = best_token
                                    pending_signal = {
                                        "type": side, "target_token": best_token,
                                        "spot0": spot_price, "prem0": router.armed.prem0,
                                        "arm_time": now, "setup": router.armed.setup
                                    }
                                    logger.info(f"[ROUTER] ARM {router.armed.setup}/{side} token={best_token}")
                        
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
                        state_payload.update({
                            "context_state": structure.context_state(spot_price),
                            "router_state": rstate.value,
                            "armed_setup": router.armed.setup if router.armed else None,
                            "armed_side": router.armed.side if router.armed else None,
                            "armed_strength": round(router.armed.strength, 2) if router.armed else 0.0,
                            "veto_active": bool(detectors.is_veto_active(time.time())),
                            "vwap": snap["vwap"], "band_upper": snap["upper"], "band_lower": snap["lower"],
                            "ema9": snap["ema9"], "ema200": snap["ema200"],
                            "router_telemetry": router.get_telemetry(),
                        })
                        update_astra_live_state(state_payload)
                        
                        # V4.3 Synthetic Volume Bridge poll (1Hz from shared tick file)
                        try:
                            import json
                            if os.path.exists(SHARED_TICK_FILE):
                                with open(SHARED_TICK_FILE, "r") as f:
                                    ticks = json.load(f)
                                live_accum = float(ticks.get("synthetic_volume_live_accum") or 0)
                                vol_bridge.poll(time.time(), live_accum)
                        except Exception:
                            pass
            
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
