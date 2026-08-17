# aegis_main.py
# AEGIS V2.0 — Main WebSocket Wiring & Orchestration
# Connects Shoonya L2 feed → AEGIS Engine → Execution Manager

import os
import sys
import time
import queue
import contextlib
import logging
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

os.environ["BOT_RUNTIME"] = "aegis"

import AEGIS.config.settings  # noqa: F401

from common.logger import get_logger
from common.broker import ShoonyaAdapter
from common.config import get_data_dir
from common.ipc import update_aegis_live_state, SHARED_TICK_FILE
from AEGIS.execution.data_fetcher import DataFetcher
from AEGIS.execution.aegis_execution_manager import AegisExecutionManager
from AEGIS.core.aegis_gamma_engine import AegisGammaEngine, create_aegis_engine
from AEGIS.l2_options_archiver import attach_aegis_archiver

logger = get_logger("aegis_main")

# ─── Pre-market Setup ───
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

# ─── 1-Minute Digest for IPC/Monitoring ───
def build_minute_digest(now, aegis_engine, trader, spot_price):
    seg = [f"[{now.strftime('%H:%M')}]"]
    
    if aegis_engine:
        state = aegis_engine.get_state_summary()
        seg.append(f"Spot: {state['spot']:.0f}")
        seg.append(f"ATM IV: {state['atm_iv']*100:.1f}%")
        seg.append(f"Γ Sum: {state['gamma_sum']:.4f}")
        seg.append(f"Hawkes B: {state['hawkes_B']:.3f}")
        seg.append(f"Fills: {state['fills_recorded']}")
    
    if trader and trader.has_open_trade():
        leg = trader.trade_context.legs[0]
        chg = ((leg.current_price - leg.entry_price) / leg.entry_price * 100) if leg.entry_price else 0.0
        seg.append(f"OPEN {leg.symbol} {chg:+.1f}%")
    
    return " | ".join(seg)

def main():
    logger.info("==============================================")
    logger.info("STARTING AEGIS V2.0 GAMMA BURST ENGINE")
    logger.info("==============================================")
    
    # Silence verbose submodules
    logging.getLogger("aegis_gamma").setLevel(logging.INFO)
    logging.getLogger("aegis_execution_manager").setLevel(logging.INFO)
    logging.getLogger("shoonya_adapter").setLevel(logging.WARNING)
    
    try:
        # ─── Core Components ───
        with contextlib.redirect_stdout(open(os.devnull, 'w')):
            broker = ShoonyaAdapter()
            fetcher = DataFetcher(broker)
        
        # ─── AEGIS Engine ───
        # Build token maps for AEGIS
        weekly_opts = fetcher.get_weekly_option_tokens()
        token_map = {}
        token_to_symbol = {}
        for _, row in weekly_opts.iterrows():
            tkn = str(row['token'])
            sym = str(row.get('symbol', ''))
            stk = float(row.get('strike', 0))
            if stk > 0 and tkn and sym:
                side = 0 if ('CE' in sym or 'CALL' in sym.upper()) else 1
                token_map[tkn] = {'side': side, 'strike': stk, 'expiry': row.get('expiry')}
                token_to_symbol[tkn] = sym
        
        # Bucket mapping (₹2Cr default)
        bucket_map = {sym: 2_00_00_000.0 for sym in token_to_symbol.values()}
        
        # Signal queue: AEGIS → Execution
        signal_queue = queue.Queue(maxsize=1000)
        
        aegis_engine = create_aegis_engine(
            token_to_symbol=token_to_symbol,
            token_map=token_map,
            bucket_map=bucket_map,
            strike_step=100.0,  # BankNifty; will be overridden per instrument
            signal_queue=signal_queue,
        )
        
        # ─── Execution Manager ───
        trader = AegisExecutionManager(
            broker_gateway=broker,
            data_fetcher=fetcher,
            aegis_engine=aegis_engine,
            signal_queue=signal_queue,
        )
        
        # ─── Wire Tick Feed → AEGIS ───
        # ShoonyaAdapter pushes ticks to registered listeners
        if hasattr(broker, "market_data_provider"):
            mdp = broker.market_data_provider
            # Replace any existing footprint compiler with direct AEGIS feed
            mdp.add_tick_listener(aegis_engine.on_tick_parsed)
            logger.info("[AegisMain] Tick feed wired: Shoonya → AEGIS Engine (zero-copy)")
        
        # ─── Attach AEGIS L2 Options Archiver (Raw Data Persistence) ───
        # Passive recorder: consumes the raw std_msg dict path (separate from the
        # engine's ParsedTick contract) so it cannot perturb the hot path.
        # Ensures 100% of AEGIS raw options ticks are archived for post-market forensics.
        aegis_archiver = attach_aegis_archiver(broker, token_to_symbol, token_map)
        if aegis_archiver:
            logger.info("[AegisMain] AEGIS L2 Options Archiver attached (raw persistence active)")
        else:
            logger.warning("[AegisMain] AEGIS archiver failed to attach — raw options data NOT persisted")
        
        logger.info("AEGIS V2.0 Core initialized successfully")
        
    except Exception as e:
        logger.critical(f"Initialization failed: {e}", exc_info=True)
        sys.exit(1)
    
    # ─── Start Execution Manager ───
    trader.start()
    
    # ─── Runtime State ───
    now_dt = datetime.now()
    if is_market_open(now_dt):
        logger.info("Market is open. Restoring session & subscriptions...")
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
                    logger.info(f"[{name}] Subscribed to Cash Index + {len(opt_tokens)} Options")
        except Exception as e:
            logger.error(f"Startup subscription warning: {e}")
    elif is_pre_market(now_dt):
        perform_premarket_handshake(broker, fetcher)
    
    logger.info("AEGIS V2.0 Bot is ready and running!")
    
    # ─── Dynamic Subscription Management ───
    last_minute = datetime.now().minute
    last_subscribed_opts = set()
    last_atm_strike = 0.0
    step = 100.0  # Default BankNifty; updated per instrument
    
    # Determine initial step
    name, _ = fetcher.get_active_instrument()
    if "NIFTY" in name and "SENSEX" not in name:
        step = 50.0
    
    # Main Loop
    while True:
        try:
            now = datetime.now()
            
            # ─── Session Management ───
            if not is_bot_active_hours(now):
                logger.info("Bot active hours closed. Entering deep sleep...")
                sleep_until_next_premarket()
                perform_premarket_handshake(broker, fetcher)
                continue
            
            if is_pre_market(now):
                time.sleep(1)
                continue
            
            # ─── 1-Minute Boundary Processing ───
            if now.minute != last_minute:
                last_minute = now.minute
                
                spot_price = fetcher.get_index_ltp()
                if spot_price <= 0:
                    logger.warning("[AegisMain] No spot price, skipping boundary")
                    continue
                
                # Update instrument step if changed
                name, exch_seg = fetcher.get_active_instrument()
                new_step = 50.0 if ("NIFTY" in name and "SENSEX" not in name) else 100.0
                if new_step != step:
                    step = new_step
                    logger.info(f"[AegisMain] Strike step updated to {step}")
                
                # Update ATM strike in AEGIS
                atm_strike = round(spot_price / step) * step
                if atm_strike != last_atm_strike and last_atm_strike > 0:
                    shift_steps = int((atm_strike - last_atm_strike) / step)
                    aegis_engine.shift_atm(shift_steps, atm_strike)
                    logger.info(f"[AegisMain] ATM shifted: {last_atm_strike} → {atm_strike} (steps: {shift_steps})")
                last_atm_strike = atm_strike
                
                # Ensure token map is current (expiry changes, etc.)
                weekly_opts = fetcher.get_weekly_option_tokens()
                if not weekly_opts.empty:
                    # Dynamic subscription for ATM ±10 (21 strikes × 2 types = 42 tokens)
                    ce_df = weekly_opts[weekly_opts['symbol'].str.contains(r'CE|CALL')]
                    pe_df = weekly_opts[weekly_opts['symbol'].str.contains(r'PE|PUT')]
                    
                    # Subscribe to all weeklies (covers ATM ±10 automatically)
                    all_opt_tokens = set(ce_df['token'].tolist() + pe_df['token'].tolist())
                    all_opt_tokens = {str(t) for t in all_opt_tokens}
                    
                    if hasattr(broker, "market_data_provider"):
                        mdp = broker.market_data_provider
                        mdp._option_ce_tokens = {str(t) for t in ce_df['token'].tolist()}
                        mdp._option_pe_tokens = {str(t) for t in pe_df['token'].tolist()}
                        
                        if all_opt_tokens != last_subscribed_opts:
                            new_tokens = all_opt_tokens - last_subscribed_opts if last_subscribed_opts else all_opt_tokens
                            mdp.subscribe(list(new_tokens), exch_seg, feed_type="d")
                            if last_subscribed_opts:
                                logger.info(f"[AegisMain] ATM {atm_strike}: Subscribed {len(new_tokens)} new strikes")
                            else:
                                logger.info(f"[AegisMain] Initial subscription: {len(all_opt_tokens)} option tokens")
                            last_subscribed_opts = all_opt_tokens
                            
                            # Refresh archiver token maps so newly-subscribed strikes are persisted
                            if aegis_archiver:
                                refreshed_t2s = {}
                                refreshed_tmap = {}
                                for _, row in weekly_opts.iterrows():
                                    tkn = str(row['token'])
                                    sym = str(row.get('symbol', ''))
                                    stk = float(row.get('strike', 0))
                                    if stk > 0 and tkn and sym:
                                        side = 0 if ('CE' in sym or 'CALL' in sym.upper()) else 1
                                        refreshed_t2s[tkn] = sym
                                        refreshed_tmap[tkn] = {'side': side, 'strike': stk, 'expiry': row.get('expiry')}
                                # Include the underlying index token
                                idx_token, idx_name, _ = fetcher.get_cash_index_token()
                                refreshed_t2s[str(idx_token)] = idx_name
                                aegis_archiver.update_token_maps(refreshed_t2s, refreshed_tmap)
                
                # Log minute digest
                digest = build_minute_digest(now, aegis_engine, trader, spot_price)
                logger.info(digest)
                
                # IPC State Update
                state_payload = {
                    "active_index": name,
                    "spot_price": spot_price,
                    "atm_strike": atm_strike,
                    "atm_iv": aegis_engine.last_atm_iv if aegis_engine else 0.0,
                    "gamma_sum": aegis_engine.gamma_tensor.sum() if aegis_engine else 0.0,
                    "hawkes_B": aegis_engine._hawkes_branching() if aegis_engine else 0.0,
                    "trades_today": trader.trades_today,
                    "daily_pnl": trader.daily_pnl,
                    "has_open_trade": trader.has_open_trade(),
                    "params_source": "calibrated" if os.path.exists(os.path.join(get_data_dir(), "params", "aegis_calibrated_params.json")) else "priors",
                }
                update_aegis_live_state(state_payload)
            
            # ─── Short Sleep (yield to tick processing threads) ───
            time.sleep(0.2)
            
        except KeyboardInterrupt:
            logger.info("Shutdown signal received")
            break
        except Exception as e:
            logger.error(f"Main loop exception: {e}", exc_info=True)
            time.sleep(5)
    
    # ─── Graceful Shutdown ───
    logger.info("Shutting down AEGIS V2.0...")
    trader.stop()
    if trader.has_open_trade():
        logger.warning("Force flattening open position on shutdown")
        trader._close_all_legs("System Shutdown")
    # Flush AEGIS L2 archiver (save final raw options partition)
    if aegis_archiver:
        logger.info("Flushing AEGIS L2 Options Archiver final partition...")
        aegis_archiver.shutdown()
        logger.info("AEGIS archiver flush complete.")
    logger.info("AEGIS V2.0 stopped cleanly")

if __name__ == "__main__":
    # Import market calendar functions here to avoid circular imports
    from common.market.market_calendar import (
        is_market_open,
        is_bot_active_hours,
        is_pre_market,
        sleep_until_next_premarket
    )
    main()
