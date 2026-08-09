# STOCK ORDER FLOW ENGINE 3 — DEFINITIVE ARCHITECTURE (FINAL)
Version: FINAL (V4.4 + Phase-2 + Phase-3 consolidation)
Updated: 2026-08-09 | Owner: Amarjeet Singh | Architect: Qwen
Deployment: Oracle Cloud VPS (1GB RAM) via PM2 | Broker: Shoonya (Noren API)

## 0. EXECUTIVE SUMMARY
Live institutional-grade intraday order-flow engine. Trades a DYNAMIC universe
(top-15 most volatile NIFTY-100 stocks, refreshed daily) on 3-minute candles.
Combines VWAP zones, tick-rule footprints, L2 depth guards, CVD divergence,
Volume Profile, and non-binary confidence scoring. Exits are ATR15-based with
Chandelier trailing. Full 5x MIS leverage, 5% daily loss cap on day-start
snapshot. A separate daily scanner (16:05 IST) selects the universe from
NIFTY-100 using NATR7 volatility ranking with hysteresis buffer.

## 1. CORE PHILOSOPHY
- VWAP ZONE (not line): upper=VWAP-of-Highs, lower=VWAP-of-Lows. Breakout must
  EXIT the zone; single-line crosses are noise.
- ORDER FLOW: every tick classified by initiator (lp>=sp1 BUY, lp<=bp1 SELL).
  Normalized delta = (buy-sell)/(buy+sell), range -100%..+100%.
- CONTEXT GATE: IN_ZONE (+/-0.20%), AT_EXTREME (+/-0.15% of day H/L),
  NO_MANS_LAND (engine idle).
- NON-BINARY: confidence scores 0-1 replace hard gates where possible;
  CVD divergence remains a HARD gate for Setup B.
- DYNAMIC UNIVERSE: volatility is the fuel; universe = hottest 15 stocks daily.

## 2. DATA FLOW TOPOLOGY
Shoonya WS (t+d) -> TickCompiler -> {BarBook 3m, BarBook 15m, Footprints}
-> [3m boundary] StrategyEngine.evaluate_all(footprints, book3, book15, now)
-> (symbol, signal, reason, metrics) -> ExecutionManager (5x, ATR15 exits)
UniverseScanner (separate pm2 proc, 16:05) -> REST daily bars NIFTY-100 ->
score/rank/buffer -> universe.json + 3m pre-fetch -> engine reads at boot.

## 3. FILE MAP
- main.py: boot, universe.json load (DEFAULT fallback), dual-book seeding
  (3m raw + 15m resampled), 3m boundary clock, mutex, EOD 15:17 sync.
- tick_compiler.py: alias-tolerant parsing, tick-rule, L2 sums (bv/sv fallback),
  feeds BOTH bar books, footprint reset per candle.
- bar_book.py: BarBook(minutes) thread-safe; seed/on_tick/force_close/
  completed_df; resample_bars() for 15m seeding; 2000-bar cap.
- strategy_engine.py: indicators (EMA9/200, VWAP zone, VP 24-bin POC/VAH/VAL),
  CVD tracking, setups A-D, confidence scoring, tie-break, ATR15 via
  talib.ATR(14) on 15m completed bars.
- execution_manager.py: 5x notional (no cap), 1xATR15 TP/SL + Chandelier
  (highest - 1xATR15, ratchet up only), 5% daily cap on day-start snapshot
  (reset _day_loss FIRST), txn costs ~0.105% notional, ML features logging.
- historical_data_manager.py: 6-day 3m REST gap-fill, parquet cache, volume
  fallback intv/v. (UNCHANGED)
- universe_scanner.py: as section 6.
- common/broker/shoonya_adapter.py: add_tick_listener (line ~458) shares one
  WS with engine; search_scrip for token resolution.

## 4. SETUPS (final logic)
Priority B(0) > A(1) > D(2); tie-break = confidence descending.
- SETUP B (Absorption, HARD CVD gate): IN_ZONE, |delta|>10%, delta_trend sign
  match, div_buy/div_sell TRUE (4-candle CVD vs price divergence),
  conf = 0.35*delta_score + 0.30*progress_score + 0.35*l2_score >= 0.65.
- SETUP A (Breakout): IN_ZONE, close beyond upper/lower, EMA9+EMA200 trend,
  |delta|>8%, L2 guard (<2x opposite), POC overhead guard (BUY blocked if
  close<POC), continuous trend_score = dist(EMA200)/2%, VAH/VAL confluence
  +0.15, conf >= 0.60.
- SETUP C (Veto): AT_EXTREME + |delta|>20% + progress<0.10% -> veto 300s
  (blocks A/D only).
- SETUP D (Imbalance): |delta|>40%, directional close, delta_score >= 0.60.

## 5. EXECUTION & RISK (final parameters)
- Leverage 5x full; notional = margin*5 (NO cap).
- TP = 1xATR15; Initial SL = 1xATR15; Chandelier = highest - 1xATR15
  (effective stop = max(init, chandelier) for BUY; mirror for SELL).
- Fallback (ATR missing): TP 1.0%, SL 0.8%.
- TIME_STOP 60m; EOD_FLATTEN 15:10; entry cutoff 15:00; mutex = 1 trade.
- Daily loss cap = -5% * day_start_margin (snapshot once/day, reset-first).
- Costs logged: brokerage 0.03% + STT 0.025% + slippage 0.05% (per round trip).
- trade_history.json stores ml_features snapshot for future XGBoost.

## 6. UNIVERSE SCANNER (daily 16:05 IST, pm2 sleep-loop; --now one-shot)
- Source: NIFTY-100 static list; daily bars (60d) via own REST (interval D).
- Tokens: search_scrip, cached tokens_cache.json.
- Gates: NATR7 >= 1.2% (NO upper cap), avg_vol(20d) >= 10L, close 500-5000.
- Score = NATR7 * (1 + 2*max(0, ATR7/ATR14 - 1)) + 0.5 if BB-width squeeze
  (width <= 20th percentile). NATR14 used ONLY inside expansion ratio.
- Hysteresis: newcomers need rank <= 12; incumbents survive to rank <= 20;
  fill to TOP_N = 15.
- Output: universe.json (symbol/token/tsym/search/scores); pre-fetch 3m
  parquet for warm boot. Engine reads at boot; DEFAULT_UNIVERSE fallback.

## 7. DEPLOYMENT
Paths: /home/opc/astra_bot/stock_order_flow/ (RUNNING), /home/opc/stock_order_flow/ (SCP landing).
pm2: stock-order-flow (engine), universe-scanner (scanner).
Deploy: scp 6 files -> cp to astra_bot -> pm2 restart stock-order-flow.
Scanner first run: python3 stock_order_flow/universe_scanner.py --now.
Rollback: git tag pre-stock-live (V4.4 base).

## 8. TROUBLESHOOTING
- No ticks: check add_tick_listener + WS subscribe logs.
- Setup A blocked often: POC guard working (log line present).
- Setup B silent: needs 4 CVD snapshots (~12 min) + divergence.
- universe.json missing: engine falls back to DEFAULT_UNIVERSE.
- RAM high: pgrep python3 for orphans; scanner is separate proc.

## 9. GLOSSARY
NATR7/14 = normalized ATR %; CVD = cumulative volume delta; POC/VAH/VAL =
volume profile levels; ATR15 = ATR(14) on 15m bars; Chandelier = trailing stop
highest-3x? NO: highest - 1xATR15 (final); squeeze = BB-width low percentile.

## 10. VERSION HISTORY
V4.4 base (2026-08-08) -> Phase-2 (CVD/VP/ML) -> FINAL (2026-08-09):
5x no-cap, ATR15 exits + Chandelier, 5% cap snapshot-fix, dual bar books,
dynamic universe scanner (NATR7 gate, TOP_N 15, hysteresis).
END — single source of truth.