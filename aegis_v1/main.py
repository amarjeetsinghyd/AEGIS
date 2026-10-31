# aegis_main.py
# AEGIS V1.0 — Main WebSocket Wiring & Orchestration
# Connects Shoonya L2 feed → AEGIS Engine → Execution Manager

import os
import sys
import time
import queue
import contextlib
import logging
import threading
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
from aegis_v1.feed import DataFetcher, is_call_symbol, split_option_sides, universe_has_both_sides, TickCompiler
from aegis_v1.execution import AegisExecutionManager
from aegis_v1.pipeline import AegisPipelineOrchestrator, create_aegis_engine
from aegis_v1.infra import attach_aegis_archiver

logger = get_logger("aegis_main")


def _register_spot_reconnect_backfill(broker, price_action_engine, exchange, token):
    market_data = getattr(broker, "market_data_provider", None)
    api = getattr(broker, "_api", None)
    if (
        market_data is None
        or api is None
        or price_action_engine is None
        or not hasattr(market_data, "add_open_listener")
        or not hasattr(api, "get_time_price_series")
    ):
        return False

    backfill_lock = threading.Lock()

    def on_reconnect():
        if not backfill_lock.acquire(blocking=False):
            return

        def fetch_missing_bars():
            pending_marked = False
            try:
                from common.market.market_calendar import is_bot_active_hours, is_trading_day

                now_dt = datetime.now()
                if not is_trading_day(now_dt.date()) or not is_bot_active_hours(now_dt):
                    return

                now_ts = time.time()
                session_open = now_dt.replace(hour=9, minute=15, second=0, microsecond=0).timestamp()
                last_end = (
                    float(price_action_engine.bars_1m[-1].ts_end)
                    if price_action_engine.bars_1m
                    else 0.0
                )
                start_ts = max(last_end, session_open)
                end_ts = float(int(now_ts // 60) * 60)
                if end_ts - start_ts < 60.0:
                    return

                price_action_engine.mark_backfill_pending()
                pending_marked = True
                for attempt in range(2):
                    now_ts = time.time()
                    end_ts = float(int(now_ts // 60) * 60)
                    try:
                        candles = api.get_time_price_series(
                            exchange=exchange,
                            token=str(token),
                            starttime=int(start_ts),
                            endtime=int(end_ts),
                            interval=1,
                        )
                    except Exception as exc:
                        candles = []
                        logger.warning(f"[AegisMain] Reconnect candle backfill attempt {attempt + 1} failed: {exc}")
                    if isinstance(candles, list) and candles:
                        price_action_engine.queue_historical_backfill(candles, now_ts)
                    else:
                        price_action_engine.queue_historical_backfill([], now_ts)
                        logger.warning("[AegisMain] Reconnect candle backfill returned no 1m data")

                    if attempt == 0:
                        time.sleep(1.0)
                        if not price_action_engine.backfill_pending:
                            return
            except Exception as exc:
                if pending_marked:
                    price_action_engine.queue_historical_backfill([], time.time())
                logger.error(f"[AegisMain] Reconnect candle backfill failed: {exc}")
            finally:
                backfill_lock.release()

        threading.Thread(
            target=fetch_missing_bars,
            name="aegis-spot-candle-backfill",
            daemon=True,
        ).start()

    market_data.add_open_listener(on_reconnect)
    return True


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
            broker.market_data_provider.subscribe([token], spot_exch, feed_type="d")
            
            weekly_opts = fetcher.get_weekly_option_tokens()
            if not weekly_opts.empty:
                opt_tokens = [str(t) for t in weekly_opts['token'].tolist()]
                # FIX 2026-09-02: subscribe BOTH feeds — 'd' alone means only depth-change
                # ticks arrive (Sensex BFO went silent after WS reconnect).
                broker.market_data_provider.subscribe(opt_tokens, exch_seg, feed_type="t")
                broker.market_data_provider.subscribe(opt_tokens, exch_seg, feed_type="d")
                logger.info(f"[{name}] Subscribed to Cash Index ({spot_exch}) + {len(opt_tokens)} {exch_seg} Options (t+d)")


    except Exception as e:
        logger.error(f"Pre-market WebSocket setup warning: {e}")

# ─── Edges snapshot for dashboard (macro + micro params currently live) ───
def _build_edges_snapshot(aegis_engine, trader):
    snap = {"macro": {}, "micro": {}, "risk": {}}
    try:
        if aegis_engine is not None:
            state = aegis_engine.get_state_summary() if hasattr(aegis_engine, "get_state_summary") else {}
            tbe = getattr(aegis_engine, "time_bar_engine", None)
            pae = getattr(aegis_engine, "price_action_engine", None)
            eng = pae if pae is not None else tbe
            snap["macro"] = {
                "spot": round(float(state.get("spot", 0.0) or 0.0), 1),
                "atm": round(float(state.get("atm_strike", 0.0) or 0.0), 0),

                "ema20": round(float(getattr(eng, "ema_20_1m", 0.0) or 0.0), 1) if eng else 0.0,
                "ema50": round(float(getattr(eng, "ema_50_1m", 0.0) or getattr(eng, "ema_50_1m_live", 0.0) or 0.0), 1) if eng else 0.0,
                "atr": round(float(getattr(eng, "current_atr_1m", 0.0) or 0.0), 2) if eng else 0.0,
                "vwap": round(float(getattr(getattr(aegis_engine, "price_action_engine", None), "current_vwap", 0.0) or 0.0), 1),
            }
            snap["micro"] = {
                "p1_persist": 0.40, "p2_persist": 0.60, "p3_persist": 0.30,
                "p1_spread": 1.00, "p2_spread": 1.20, "p3_spread": 0.80,
                "p1_etf": 2.5, "p2_etf": 2.0, "p3_etf": 3.0,
                "opt_spread_max": 2.5, "opt_depth_min": 500,
                "premium_min": 5.0, "premium_max": 400.0,
            }
            # OI shield telemetry (visibility only — gates untouched).
            try:
                if hasattr(aegis_engine, "get_oi_telemetry"):
                    snap["oi"] = aegis_engine.get_oi_telemetry()
            except Exception:
                pass
        if trader is not None:
            from AEGIS.aegis_execution_manager import MAX_TRADES_PER_DAY
            snap["risk"] = {
                "max_trades": int(MAX_TRADES_PER_DAY),
                "trades_today": int(getattr(trader, "trades_today", 0)),
                "cooldown_inter": float(getattr(trader, "inter_trade_cooldown_sec", 60.0)),
                "cooldown_route": float(getattr(trader, "consecutive_route_cooldown_sec", 180.0)),
                "cooldown_loss": float(getattr(trader, "post_loss_cooloff_sec", 120.0)),
            }
    except Exception:
        pass
    return snap


def _build_oi_universe_rows(token_map, t2s):
    """Flattens token maps into OI poller rows (visibility only).

    Returns [{token, sym, strike, kind}] for option contracts only
    (kind 1=CE, 2=PE). Index/futures rows (strike<=0) are skipped.
    """
    rows = []
    try:
        for tkn, info in (token_map or {}).items():
            try:
                stk = float((info or {}).get("strike", 0.0) or 0.0)
            except Exception:
                continue
            if stk <= 0:
                continue
            sym = str((t2s or {}).get(str(tkn), "") or "")
            if not sym:
                continue
            try:
                side = int((info or {}).get("side", 0))
            except Exception:
                side = 0
            rows.append({
                "token": str(tkn),
                "sym": sym,
                "strike": stk,
                "kind": 1 if side == 0 else 2,
            })
    except Exception:
        pass
    return rows


# ─── 1-Minute Digest for IPC/Monitoring — ACCURATE SYSTEM TELEMETRY ───
def build_minute_digest(now, aegis_engine, trader, spot_price):
    seg = [f"[{now.strftime('%H:%M')}]"]
    
    if aegis_engine:
        state = aegis_engine.get_state_summary()
        eng_spot = float(state.get('spot', 0.0) or 0.0)
        disp_spot = spot_price if (spot_price and spot_price > 0) else eng_spot

        if spot_price and spot_price > 0 and eng_spot > 0:
            if abs(spot_price - eng_spot) / max(eng_spot, 1.0) > 0.002:
                seg.append(f"Spot:{disp_spot:.0f}/Eng:{eng_spot:.0f}!")
            else:
                seg.append(f"Spot:{disp_spot:.0f}")
        elif spot_price and spot_price > 0:
            seg.append(f"Spot:{spot_price:.0f}/Eng:0!")
        else:
            seg.append(f"Spot:{eng_spot:.0f}")
        seg.append(f"ATM:{state['atm_strike']:.0f}")


        # ─── Price Action Engine: 20/50 EMA, Zone Discipline, VWAP & PA Pillars ───
        pae = getattr(aegis_engine, 'price_action_engine', None) or getattr(aegis_engine, 'time_bar_engine', None)
        eff_vwap = getattr(aegis_engine, 'current_vwap', 0.0) or (getattr(pae, 'current_vwap', 0.0) if pae else 0.0)

        try:
            if pae:
                live_20, slope_20 = pae.get_live_20_ema_and_slope()
                live_50, slope_50 = pae.get_live_50_ema_and_slope()
                if live_20 > 0 and live_50 > 0:
                    seg.append(f"EMA:20@{live_20:.1f}/50@{live_50:.1f}")
                    # Strict 20 vs 50 EMA Zone Separation Status
                    bar = getattr(pae, '_curr_1m_bar', None) or (pae.bars_1m[-1] if getattr(pae, 'bars_1m', None) else None)
                    if live_20 > live_50:
                        if bar and bar.low >= live_50:
                            seg.append("Zone:BULL_CLEAR")
                        else:
                            seg.append("Zone:BULL_OVERLAP")
                    elif live_20 < live_50:
                        if bar and bar.high <= live_50:
                            seg.append("Zone:BEAR_CLEAR")
                        else:
                            seg.append("Zone:BEAR_OVERLAP")
                    else:
                        seg.append("Zone:CHOP_ENTANGLED")
                elif live_50 > 0:
                    seg.append(f"50EMA:{live_50:.1f}")

                # Institutional VWAP Alignment & Distance
                if eff_vwap > 0 and disp_spot > 0:
                    vwap_bps = (disp_spot - eff_vwap) / eff_vwap * 10000.0
                    bias = "BULL" if disp_spot >= eff_vwap else "BEAR"
                    seg.append(f"VWAP:{eff_vwap:.1f}({vwap_bps:+.1f}bps,{bias})")

                # Volatility: Current 1m ATR and Ratio
                if getattr(pae, 'current_atr_1m', 0.0) > 0:
                    seg.append(f"ATR:{pae.current_atr_1m:.1f}({pae.atr_ratio:.2f}x)")

                # Active Price Action Setup Status (passive display only — never mutate state on main loop thread)
                armed_cnt = len(getattr(pae, "_mode_a_armed_setups", {}))
                pa_status = f"ARMED({armed_cnt})" if armed_cnt > 0 else "IDLE"
                seg.append(f"PA:{pa_status}")
        except Exception:
            pass

        # Directional Persistence (60s snapshot)
        try:
            pe = getattr(aegis_engine, 'persistence_engine', None)
            if pe and getattr(pe, 'snapshots', None):
                _, score_u, _ = pe.evaluate_persistence('UP')
                _, score_d, _ = pe.evaluate_persistence('DOWN')
                seg.append(f"Persist:U{score_u:.2f}/D{score_d:.2f}")
            else:
                seg.append("Persist:1.00")
        except Exception:
            pass

        # Macro Drift 60s & 180s
        try:
            hist = getattr(aegis_engine, 'spot_history', None)
            if hist and len(hist) > 2:
                hist_now = hist[-1][0] if isinstance(hist[-1], (list, tuple)) else 0
                lp_now = hist[-1][1] if isinstance(hist[-1], (list, tuple)) else 0
                def _drift(sec):
                    cutoff = hist_now - sec
                    old = next((p for t, p in hist if t >= cutoff), hist[0][1])
                    return (lp_now - old) / old * 100 if old else 0
                seg.append(f"D60:{_drift(60):+.2f}%")
                seg.append(f"D180:{_drift(180):+.2f}%")
        except Exception:
            pass

        # Thresholds (Pillar PA EQS 80 vs Micro fallback 75)
        seg.append(f"Thresh:80(PA)/{state['high_conviction_threshold']:.0f}(Micro)")

    if trader:
        seg.append(f"Trades:{trader.trades_today}")
        seg.append(f"PnL:{trader.daily_pnl:+.0f}")
        if trader.has_open_trade() and getattr(trader, "trade_context", None) and getattr(trader.trade_context, "legs", None):
            leg = trader.trade_context.legs[0]
            chg = ((leg.current_price - leg.entry_price) / leg.entry_price * 100) if leg.entry_price else 0.0
            seg.append(f"Pos:{leg.symbol} {chg:+.1f}%")
        else:
            seg.append("Pos:FLAT")

    return " | ".join(seg)

def shutdown_background_workers(trader=None, aegis_archiver=None, compiler=None, broker=None, oi_poller=None, reason="EOD Session Close"):
    """Gracefully terminates all background threads, archivers, and broker websockets for clean off-market sleep."""
    logger.info(f"[AegisMain] Shutting down background workers ({reason})...")

    # 1. Stop Execution Manager & flatten open positions
    if trader is not None:
        try:
            trader.stop()
            if trader.has_open_trade():
                logger.warning(f"[AegisMain] Force flattening open position on {reason}")
                trader._close_all_legs(f"Shutdown: {reason}")
            logger.info("[AegisMain] Execution Manager stopped.")
        except Exception as e_em:
            logger.warning(f"[AegisMain] Execution manager stop warning: {e_em}")

    # 2. Flush and shutdown L2 Archiver
    if aegis_archiver is not None:
        try:
            logger.info("[AegisMain] Flushing AEGIS L2 Options Archiver final partition...")
            aegis_archiver.shutdown()
            logger.info("[AegisMain] AEGIS L2 archiver shutdown complete.")
        except Exception as e_arch:
            logger.warning(f"[AegisMain] Archiver shutdown warning: {e_arch}")

    # 3. Detach Tick Compiler from engine
    if compiler is not None:
        try:
            compiler.stop()
            logger.info("[AegisMain] TickCompiler stopped and detached from engine.")
        except Exception as e_comp:
            logger.warning(f"[AegisMain] TickCompiler stop warning: {e_comp}")

    # 4. Disconnect Broker WebSocket
    if broker is not None:
        try:
            if hasattr(broker, "stop_websocket"):
                broker.stop_websocket()
            elif hasattr(broker, "market_data_provider") and hasattr(broker.market_data_provider, "stop_websocket"):
                broker.market_data_provider.stop_websocket()
            logger.info("[AegisMain] Broker WebSocket cleanly closed for post-market sleep.")
        except Exception as e_ws:
            logger.warning(f"[AegisMain] WebSocket close warning: {e_ws}")

    # 5. Stop OI Poller if active
    if oi_poller is not None:
        try:
            oi_poller.stop()
            logger.info("[AegisMain] OI poller stopped.")
        except Exception:
            pass

def main():
    logger.info("==============================================")
    logger.info("STARTING AEGIS V1.0 GAMMA BURST ENGINE")
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
        # 1. Add Spot Cash Index Token
        spot_token, spot_sym, spot_exch = fetcher.get_cash_index_token()
        spot_token = str(spot_token)
        inst_name, _ = fetcher.get_active_instrument()
        strike_step = 100.0 if ("SENSEX" in inst_name.upper() or "BANKNIFTY" in inst_name.upper()) else 50.0
        
        token_map = {}
        token_to_symbol = {}
        
        if spot_token and spot_sym:
            token_to_symbol[spot_token] = spot_sym
            token_map[spot_token] = {'side': 0, 'strike': 0.0, 'is_index': True}
            logger.info(f"[AegisMain] Active instrument: {inst_name} | Spot Token: {spot_token} ({spot_sym}) | Strike Step: {strike_step}")
        


        # 2. Build token maps for AEGIS options. Retry until BOTH sides are present:
        # a single-side universe (partial searchscrip failure) blinds half the
        # option flow (CallBuy=0 / PutBuy=0) with zero log noise.
        weekly_opts = fetcher.get_weekly_option_tokens()
        for _u_attempt in range(2):
            if universe_has_both_sides(weekly_opts):
                break
            logger.warning(f"[AegisMain] option universe single-sided ({len(weekly_opts)} rows) — refetching (attempt {_u_attempt + 2}/3)")
            time.sleep(2)
            weekly_opts = fetcher.get_weekly_option_tokens()
        if not universe_has_both_sides(weekly_opts):
            logger.critical(f"[AegisMain] option universe still single-sided after retries ({len(weekly_opts)} rows) — flow will be half-blind until next rebuild")
        for _, row in weekly_opts.iterrows():
            tkn = str(row['token'])
            sym = str(row.get('symbol', ''))
            stk = float(row.get('strike', 0))
            if stk > 0 and tkn and sym:
                side = 0 if is_call_symbol(sym) else 1
                token_map[tkn] = {'side': side, 'strike': stk, 'expiry': row.get('expiry')}
                token_to_symbol[tkn] = sym
        
        # Turnover bucket mapping (₹5,00,000 per options contract) — reserved for future use,
        # engine currently scores per-tick and does not gate on buckets
        bucket_map = {sym: 500_000.0 for sym in token_to_symbol.values()}
        
        # Signal queue: AEGIS → Execution
        signal_queue = queue.Queue(maxsize=1000)
        
        aegis_engine = create_aegis_engine(
            token_to_symbol=token_to_symbol,
            token_map=token_map,
            bucket_map=bucket_map,
            strike_step=strike_step,
            signal_queue=signal_queue,
        )
        # Ensure only ACTIVE underlying token is registered (fixes Nifty/Sensex mix: 02 Sep showed Spot 24056 with ATM 76500)
        aegis_engine.underlying_tokens.clear()
        aegis_engine.underlying_tokens.add(spot_token)
        # Keep only the active index's token — do not mix Nifty+Sensex spots
        logger.info(f"[AegisMain] Underlying locked to {inst_name} token {spot_token} only")

        # ─── Pre-Warm 50 EMA with Historical 1-Minute Bars (Zero Cold-Start Lag) ───
        try:
            pa_eng = getattr(aegis_engine, "price_action_engine", None) or getattr(aegis_engine, "time_bar_engine", None)
            api = getattr(broker, "_api", None)
            if pa_eng and api and spot_token and spot_exch:
                import time as _time
                end_ts = int(_time.time())
                start_ts = end_ts - (5 * 86400)
                candles_raw = None
                # Tier 1: Try broker API with integer interval (Shoonya NorenApi requirement)
                try:
                    candles_raw = api.get_time_price_series(
                        exchange=spot_exch,
                        token=str(spot_token),
                        starttime=start_ts,
                        endtime=end_ts,
                        interval=1,
                    )
                    if not isinstance(candles_raw, list) or not candles_raw:
                        # Fallback to string interval "1"
                        candles_raw = api.get_time_price_series(
                            exchange=spot_exch,
                            token=str(spot_token),
                            starttime=start_ts,
                            endtime=end_ts,
                            interval="1",
                        )
                except Exception as e_api_call:
                    logger.debug(f"[AegisMain] Direct get_time_price_series call failed: {e_api_call}")

                if isinstance(candles_raw, list) and candles_raw:
                    seeded = pa_eng.seed_historical_bars(candles_raw)
                    live_20, live_slope_20 = getattr(pa_eng, "get_live_20_ema_and_slope", lambda: (0.0, 0.0))()
                    live_50, live_slope_50 = getattr(pa_eng, "get_live_50_ema_and_slope", lambda: (0.0, 0.0))()
                    logger.info(
                        f"[AegisMain] Seeded PriceActionEngine with {seeded} 1m bars for {inst_name} ({spot_exch}:{spot_token}) | "
                        f"20 EMA: {live_20:.2f} ({live_slope_20:+.2f}bps) | 50 EMA: {live_50:.2f} ({live_slope_50:+.2f}bps)"
                    )
                else:
                    logger.warning(f"[AegisMain] No historical 1m bars returned for {inst_name} ({spot_exch}:{spot_token})")
                    # Tier 2: Try DataFetcher 1-minute historical provider for cash index
                    seeded = 0
                    try:
                        df_1m = fetcher.get_historical_candles(spot_exch, spot_token, "ONE_MINUTE", days_back=5)
                        if df_1m is not None and not df_1m.empty:
                            candles_df = df_1m.to_dict(orient="records") if hasattr(df_1m, "to_dict") else []
                            seeded = pa_eng.seed_historical_bars(candles_df)
                            logger.info(f"[AegisMain][1M-FETCHER] Seeded {seeded} 1m bars from historical provider.")
                    except Exception as e_1m:
                        logger.debug(f"[AegisMain] 1m fetcher failed: {e_1m}")

                    # Tier 3 (REMOVED 2026-10-26): futures 1m seeding fallback eliminated.
                    # Futures is shadow telemetry-only now — seeding live 20/50 EMA bars from
                    # futures prices would corrupt the spot candle basis (premium drift).

                    # Tier 4: Fallback to 5-min cache if 1m sources were completely empty
                    if seeded <= 0:
                        try:
                            fb_df = fetcher.get_historical_candles(spot_exch, spot_token, "FIVE_MINUTE", days_back=5)
                            if fb_df is not None and not fb_df.empty:
                                fb_candles = fb_df.tail(60).to_dict(orient="records") if hasattr(fb_df, "to_dict") else []
                                norm = []
                                for r in fb_candles:
                                    try:
                                        cl = float(r.get("close") or r.get("intc") or 0.0)
                                        if cl > 0:
                                            norm.append({"open": cl, "high": cl, "low": cl, "close": cl, "time": float(r.get("timestamp") or r.get("time") or 0.0)})
                                    except Exception:
                                        continue
                                if norm:
                                    fb_seeded = pa_eng.seed_historical_bars(norm)
                                    logger.info(f"[AegisMain][COLD-BOOT FALLBACK] Seeded {fb_seeded} bars from 5-min cache.")
                                else:
                                    logger.warning("[AegisMain][COLD-BOOT] Fallback cache empty — priming purely from live ticks.")
                            else:
                                logger.warning("[AegisMain][COLD-BOOT] Fallback fetch empty — priming purely from live ticks.")
                        except Exception as e_fb:
                            logger.warning(f"[AegisMain][COLD-BOOT] Fallback failed (non-fatal): {e_fb}")
        except Exception as e_seed:
            logger.warning(f"[AegisMain] Historical 1m bar seeding warning (non-fatal): {e_seed}")

        # ─── Seed Daily Structural Levels (PDH / PDL / PDC) & Previous Day 5m/15m S/R ───
        try:
            pa_eng = getattr(aegis_engine, "price_action_engine", None) or getattr(aegis_engine, "time_bar_engine", None)
            if pa_eng and spot_token and spot_exch:
                pdh, pdl, pdc = 0.0, 0.0, 0.0
                from common.market.market_calendar import get_previous_trading_day
                prev_trading_date = get_previous_trading_day(datetime.now().date())
                logger.info(f"[AegisMain] Identified previous trading session: {prev_trading_date} for {inst_name}")

                # Priority 1: Fetch Previous Day 5m candles (~75 candles) and seed 5m/15m S/R registry + PDH/PDL/PDC
                try:
                    df_5m_prev = fetcher.get_previous_day_5m_candles(spot_exch, spot_token, prev_trading_date)
                    if df_5m_prev is not None and not df_5m_prev.empty and len(df_5m_prev) >= 3:
                        candles_5m = df_5m_prev.to_dict(orient="records") if hasattr(df_5m_prev, "to_dict") else []
                        res_prev = pa_eng.seed_previous_day_5m_bars(candles_5m)
                        pdh = float(res_prev.get("pdh", 0.0) or 0.0)
                        pdl = float(res_prev.get("pdl", 0.0) or 0.0)
                        pdc = float(res_prev.get("pdc", 0.0) or 0.0)
                        logger.info(
                            f"[AegisMain] Seeded Previous Day 5m/15m Structural Levels: "
                            f"{res_prev.get('levels_discovered', 0)} discovered | "
                            f"PDH={pdh:.2f} | PDL={pdl:.2f} | PDC={pdc:.2f}"
                        )
                except Exception as e_p5m:
                    logger.warning(f"[AegisMain] Previous day 5m seeding warning (non-fatal): {e_p5m}")

                # Priority 2: Fallback to ONE_DAY endpoint if 5m seeding did not establish PDH/PDL
                if pdh <= 0:
                    try:
                        df_day = fetcher.get_historical_candles(spot_exch, spot_token, "ONE_DAY", days_back=5)
                        if df_day is not None and not df_day.empty and len(df_day) >= 2:
                            today_str = datetime.now().strftime("%Y-%m-%d")
                            candles_day = df_day.to_dict(orient="records") if hasattr(df_day, "to_dict") else []
                            prev_days = []
                            for r in candles_day:
                                d_val = str(r.get("date", "") or r.get("time", ""))[:10]
                                if d_val and d_val < today_str:
                                    prev_days.append(r)
                            if prev_days:
                                last_day = prev_days[-1]
                                pdh = float(last_day.get("high") or last_day.get("inth") or 0.0)
                                pdl = float(last_day.get("low") or last_day.get("intl") or 0.0)
                                pdc = float(last_day.get("close") or last_day.get("intc") or 0.0)
                    except Exception as e_pd:
                        logger.debug(f"[AegisMain] Daily candle fetch failed: {e_pd}")

                # Priority 3 Fallback: derive from seeded 1m bars if available
                if pdh <= 0 and pa_eng and pa_eng.bars_1m:
                    today_d = datetime.now().date()
                    y_bars = [b for b in pa_eng.bars_1m if datetime.fromtimestamp(b.ts_start).date() < today_d]
                    if y_bars:
                        pdh = max(b.high for b in y_bars)
                        pdl = min(b.low for b in y_bars)
                        pdc = y_bars[-1].close

                if pdh > 0:
                    pa_eng.seed_daily_levels(pdh, pdl, pdc)
                    logger.info(f"[AegisMain] Seeded Daily Structural Levels: PDH={pdh:.2f} | PDL={pdl:.2f} | PDC={pdc:.2f}")
        except Exception as e_sr_seed:
            logger.warning(f"[AegisMain] Daily / 5m structural seeding warning (non-fatal): {e_sr_seed}")



        # ─── OI Quotes Poller (DISABLED per operator directive) ───
        # Operator policy: OI polling provides zero trading edge, pollutes logs,
        # and exhausts broker REST API limits. Permanently disabled.
        oi_universe = {"spot": 0.0, "strike_step": strike_step, "rows": []}
        oi_poller = None
        logger.info("[AegisMain] OI quotes poller permanently DISABLED (clean CPU & zero REST quote load)")
        
        # ─── Execution Manager ───
        trader = AegisExecutionManager(
            broker_gateway=broker,
            data_fetcher=fetcher,
            aegis_engine=aegis_engine,
            signal_queue=signal_queue,
        )
        aegis_engine.execution_manager = trader
        
        # ─── Wire Tick Feed → AEGIS ───
        # Exchange-aware: spot on BSE/NSE, futures + options on BFO/NFO.
        # Single-exchange subscribe caused BSE:1 to go as NSE:1 → Spot 0.
        compiler = TickCompiler(broker)
        if hasattr(compiler, 'reset_session'):
            compiler.reset_session()
        compiler.set_symbol_map(token_to_symbol)
        compiler.set_engine(aegis_engine)
        _, opt_exch_seg = fetcher.get_active_instrument()
        if spot_token and spot_sym:
            compiler.subscribe([spot_token], spot_exch)
        opt_only = [t for t in token_to_symbol.keys() if str(t) != str(spot_token)]
        if opt_only:
            compiler.subscribe(opt_only, opt_exch_seg)
        logger.info(f"[AegisMain] Tick feed wired: Shoonya → TickCompiler → AEGIS Engine ({len(token_to_symbol)} instruments)")

        pa_eng = getattr(aegis_engine, "price_action_engine", None) or getattr(aegis_engine, "time_bar_engine", None)
        if _register_spot_reconnect_backfill(broker, pa_eng, spot_exch, spot_token):
            logger.info("[AegisMain] Spot candle backfill armed for WebSocket reconnects")

        # ─── Attach AEGIS L2 Options Archiver (Raw Data Persistence) ───
        # Passive recorder: consumes the raw std_msg dict path (separate from the
        # engine's ParsedTick contract) so it cannot perturb the hot path.
        # Ensures 100% of AEGIS raw options ticks are archived for post-market forensics.
        aegis_archiver = attach_aegis_archiver(broker, token_to_symbol, token_map)
        if aegis_archiver:
            logger.info("[AegisMain] AEGIS L2 Options Archiver attached (raw persistence active)")
        else:
            logger.warning("[AegisMain] AEGIS archiver failed to attach — raw options data NOT persisted")
        
        logger.info("AEGIS V1.0 Core initialized successfully")
        
    except Exception as e:
        logger.critical(f"Initialization failed: {e}", exc_info=True)
        sys.exit(1)
    
    # ─── Start Execution Manager ───
    # ─── Post-Warmup Low-Latency Garbage Collection Tuning ───
    # Run full collection to clean up initialization garbage (pandas, json, seeding temp objects),
    # freeze all long-lived objects so the GC never traverses them again during live trading,
    # and adjust GC generation thresholds to prevent mid-burst latency spikes.
    import gc
    gc.collect()
    if hasattr(gc, "freeze"):
        gc.freeze()
    gc.set_threshold(50000, 20, 20)
    logger.info("[AegisMain] Post-warmup GC tuned: objects frozen, GC threshold set to (50000, 20, 20)")

    trader.start()
    
    # ─── Runtime State ───
    now_dt = datetime.now()
    if is_market_open(now_dt):
        logger.info("Market is open. Tick feed and subscriptions active via TickCompiler.")
    elif is_pre_market(now_dt):
        perform_premarket_handshake(broker, fetcher)
    
    logger.info("AEGIS V1.0 Bot is ready and running!")
    
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
                shutdown_background_workers(trader=trader, aegis_archiver=aegis_archiver, compiler=compiler, broker=broker, oi_poller=oi_poller, reason="EOD Session Close")
                aegis_archiver = None
                try:
                    update_aegis_live_state({"status": "SLEEPING", "market_state": "CLOSED", "pos": "FLAT", "trades_today": getattr(trader, "trades_today", 0), "daily_pnl": getattr(trader, "daily_pnl", 0.0)})
                except Exception:
                    pass
                logger.info("Bot active hours closed. Stopping background workers...")

                if oi_poller is not None:
                    try:
                        oi_poller.stop()
                    except Exception:
                        pass
                logger.info("Entering deep sleep until next pre-market (08:55 AM IST)...")
                sleep_until_next_premarket()
                logger.info("[AegisMain] Next pre-market reached. Exiting cleanly for fresh daily instrument boot (PM2 autorestart)...")
                sys.exit(0)
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
                if last_atm_strike <= 0:
                    last_atm_strike = atm_strike
                    logger.info(f"[AegisMain] ATM seeded: {atm_strike:.0f} from spot {spot_price:.0f}")
                elif atm_strike != last_atm_strike:
                    shift_steps = int((atm_strike - last_atm_strike) / step)
                    aegis_engine.shift_atm(shift_steps, atm_strike)
                    logger.info(f"[AegisMain] ATM shifted: {last_atm_strike} → {atm_strike} (steps: {shift_steps})")
                    last_atm_strike = atm_strike
                
                # Ensure token map is current (expiry changes, etc.)
                weekly_opts = fetcher.get_weekly_option_tokens()
                if not weekly_opts.empty:
                    # Robust side split (single-letter C/P and CE/PE formats).
                    # NEVER adopt a single-sided universe: a partial fetch would
                    # blind half the flow (CallBuy=0 / PutBuy=0). Keep last good.
                    ce_df, pe_df = split_option_sides(weekly_opts)
                    if len(ce_df) == 0 or len(pe_df) == 0:
                        logger.warning(f"[AegisMain] single-sided universe ({len(ce_df)} CE / {len(pe_df)} PE rows) — keeping last good maps")
                        all_opt_tokens = set(last_subscribed_opts)
                    else:
                        # Subscribe to all weeklies (covers ATM ±10 automatically)
                        all_opt_tokens = set(ce_df['token'].tolist() + pe_df['token'].tolist())
                        all_opt_tokens = {str(t) for t in all_opt_tokens}
                    
                    if hasattr(broker, "market_data_provider") and len(ce_df) > 0 and len(pe_df) > 0:
                        mdp = broker.market_data_provider
                        mdp._option_ce_tokens = {str(t) for t in ce_df['token'].tolist()}
                        mdp._option_pe_tokens = {str(t) for t in pe_df['token'].tolist()}

                    if all_opt_tokens != last_subscribed_opts:
                        if hasattr(broker, "market_data_provider"):
                            mdp = broker.market_data_provider
                            all_tok_list = list(all_opt_tokens)
                            mdp.subscribe(all_tok_list, exch_seg, feed_type="t")
                            mdp.subscribe(all_tok_list, exch_seg, feed_type="d")
                            # keep spot and futures tokens alive too
                            try:
                                spot_tok, _, spot_exch = fetcher.get_cash_index_token()
                                mdp.subscribe([str(spot_tok)], spot_exch, feed_type="t")
                                mdp.subscribe([str(spot_tok)], spot_exch, feed_type="d")


                            except Exception:
                                pass

                        if last_subscribed_opts:
                            new_tokens = all_opt_tokens - last_subscribed_opts
                            logger.info(f"[AegisMain] ATM {atm_strike}: {len(new_tokens)} new strikes (subscribed t+d)")
                        else:
                            logger.info(f"[AegisMain] Initial subscription: {len(all_opt_tokens)} option tokens (t+d)")
                        last_subscribed_opts = all_opt_tokens

                        # Refresh archiver token maps so newly-subscribed strikes are persisted
                        refreshed_t2s = {}
                        refreshed_tmap = {}
                        for _, row in weekly_opts.iterrows():
                            tkn = str(row['token'])
                            sym = str(row.get('symbol', ''))
                            stk = float(row.get('strike', 0))
                            if stk > 0 and tkn and sym:
                                side = 0 if is_call_symbol(sym) else 1
                                refreshed_t2s[tkn] = sym
                                refreshed_tmap[tkn] = {'side': side, 'strike': stk, 'expiry': row.get('expiry')}
                        # Include the underlying index token
                        idx_token, idx_name, _ = fetcher.get_cash_index_token()
                        refreshed_t2s[str(idx_token)] = idx_name
                        if aegis_archiver:
                            aegis_archiver.update_token_maps(refreshed_t2s, refreshed_tmap)
                        # Also refresh engine's strike map to fix P1.4 universe drift
                        if aegis_engine and hasattr(aegis_engine, 'refresh_token_maps'):
                            try:
                                aegis_engine.refresh_token_maps(refreshed_t2s, refreshed_tmap)
                            except Exception as e:
                                logger.warning(f"[AegisMain] engine refresh_token_maps failed: {e}")
                        # Critical: refresh TickCompiler pool or new strikes stay invisible
                        # (archiver grows while engine starves → Spot ok but no signals).
                        try:
                            compiler.set_symbol_map(refreshed_t2s)
                            opt_refresh = [t for t in refreshed_t2s.keys() if str(t) != str(idx_token)]
                            if opt_refresh:
                                compiler.subscribe(opt_refresh, exch_seg)
                        except Exception as e:
                            logger.warning(f"[AegisMain] compiler refresh failed: {e}")
                        # Refresh OI poller universe so ATM±5 enrichment tracks expiry rolls.
                        try:
                            oi_universe["rows"] = _build_oi_universe_rows(refreshed_tmap, refreshed_t2s)
                        except Exception:
                            pass

                # OI poller spot refresh (every minute, off hot path).
                try:
                    if spot_price and spot_price > 0:
                        oi_universe["spot"] = float(spot_price)
                except Exception:
                    pass
                
                # Log minute digest
                digest = build_minute_digest(now, aegis_engine, trader, spot_price)
                logger.info(digest)

                # 15-minute OI fortress snapshot (off hot path, at most 1 per bucket).
                try:
                    if aegis_engine is not None and hasattr(aegis_engine, "maybe_log_wall_snapshot"):
                        aegis_engine.maybe_log_wall_snapshot(spot_price, time.time())
                except Exception:
                    pass
                
                # IPC State Update
                state_payload = {
                    "active_index": name,
                    "spot_price": spot_price,
                    "atm_strike": atm_strike,
                    "atm_iv": aegis_engine.last_atm_iv if aegis_engine else 0.0,
                    "gamma_sum": 0.0,   # deprecated (empirical scorer engine); kept for dashboard schema compat
                    "hawkes_B": 0.0,    # deprecated (empirical scorer engine); kept for dashboard schema compat
                    "scorer_active": bool(aegis_engine.scorer) if aegis_engine else False,
                    "high_conviction_threshold": aegis_engine.high_conviction_threshold if aegis_engine else 0.0,
                    "trades_today": trader.trades_today,
                    "daily_pnl": trader.daily_pnl,
                    "daily_gross_pnl": round(float(getattr(trader, "daily_gross_pnl", trader.daily_pnl)), 2),
                    "daily_charges": round(float(getattr(trader, "daily_charges", 0.0)), 2),
                    "daily_net_pnl": round(float(getattr(trader, "daily_net_pnl", trader.daily_pnl)), 2),
                    "has_open_trade": trader.has_open_trade(),
                    "params_source": "calibrated" if os.path.exists(os.path.join(get_data_dir(), "params", "aegis_calibrated_params.json")) else "priors",
                    "edges": _build_edges_snapshot(aegis_engine, trader),
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
    logger.info("Shutting down AEGIS V1.0...")
    shutdown_background_workers(trader=trader, aegis_archiver=aegis_archiver, compiler=compiler, broker=broker, oi_poller=oi_poller, reason="System Shutdown")
    aegis_archiver = None

    # Stop OI quotes poller (no new REST OI polls during teardown)
    try:
        if oi_poller is not None:
            oi_poller.stop()
            logger.info("[AegisMain] OI poller stopped")
    except Exception as e_oi:
        logger.warning(f"[AegisMain] OI poller stop warning (non-fatal): {e_oi}")
    trader.stop()
    if trader.has_open_trade():
        logger.warning("Force flattening open position on shutdown")
        trader._close_all_legs("System Shutdown")
    # Flush AEGIS L2 archiver (save final raw options partition)
    if aegis_archiver:
        logger.info("Flushing AEGIS L2 Options Archiver final partition...")
        aegis_archiver.shutdown()
        logger.info("AEGIS archiver flush complete.")
    logger.info("AEGIS V1.0 stopped cleanly")

if __name__ == "__main__":
    # Import market calendar functions here to avoid circular imports
    from common.market.market_calendar import (
        is_market_open,
        is_bot_active_hours,
        is_pre_market,
        sleep_until_next_premarket
    )
    main()
