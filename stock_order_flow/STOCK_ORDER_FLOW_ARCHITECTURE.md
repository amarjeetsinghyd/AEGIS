# STOCK ORDER FLOW — ENGINE 3: DEFINITIVE ARCHITECTURE (V4.4, AUDITED)
**Repo:** quant-bot · **Folder:** stock_order_flow/ · **Mode:** LIVE (small capital, MIS)
**Owner:** Amarjeet Singh · **Architect:** Qwen (chat) · **Implementers:** VS Code agent / Qwen Coder
**Purpose of this file:** Any AI reading ONLY this document must be able to narrate the
entire stock order-flow system — what lives where, what each piece does, and every
audited code change — like a story.

---

## 0. THE SYSTEM IN ONE PARAGRAPH
Engine 3 trades 15 liquid NSE equities intraday. WebSocket ticks flow into a
TickCompiler that builds (a) canonical 3-minute OHLCV bars and (b) a per-candle
buy/sell footprint with L2 depth averages. Every 3 minutes, a Strategy Engine
computes per-stock dynamic VWAP bands (VWAP-of-Highs / VWAP-of-Lows), EMA9/EMA200,
classifies the price context (IN_ZONE / AT_EXTREME / NO_MANS_LAND), and runs four
institutional setups (A breakout, B absorption, C exhaustion-veto, D stacked
imbalance) under a strict mutex that emits at most ONE best signal. A live
Execution Manager then places MIS market orders (5x leverage, capped at 2x funds),
manages TP/SL/time/EOD exits, and persists everything to disk. Historical 3m bars
are REST-fetched at boot and 15:17 and cached in parquet so indicators are warm at open.

## 1. DATA-FLOW TOPOLOGY
```
Shoonya WS ("t" touchline + "d" depth, 15 EQ tokens)
        │  on_message(tick dict)
        ▼
TickCompiler ──► market_depth[token] = {lp,bp1,sp1,v}          (live prices)
   │  vol_delta = cumulative-volume difference
   │  tick-rule: lp>=sp1 BUY / lp<=bp1 SELL
   ├──► footprints[token] = [{price,vol,type,l2_bid,l2_ask}]  (per 3m candle)
   └──► ThreeMinuteBarBook.on_tick → 3m OHLCV bars
REST get_time_price_series(3m) ──► parquet cache ──► bar_book.seed (boot + 15:17)
Every 3m boundary:
   bar_book.force_close_forming → get_compiled_footprints → evaluate_all
        → ONE (symbol, BUY/SELL, reason) → StockExecutionManager (MKT, MIS)
```

## 2. SEMANTIC COMPONENT MAP (file → class → function → job)
### stock_order_flow/main.py
- `EQUITY_UNIVERSE` — 15 stocks {token, tsym, search}; TATAMOTORS tracked as TMPV-EQ (rename note).
- `main()` — boot: `ShoonyaAdapter()` → session restore → `TickCompiler(broker)` →
  `StockExecutionManager(broker)` → `StockStrategyEngine()`.
- Boot sync: `fetch_and_seed_history(...)` → `compiler.bar_book.seed(token, df)` → `compiler.subscribe(tokens)`.
- Loop: EOD gap-fill at 15:17; non-trading/after-close sleep; `current_prices` from
  `compiler.market_depth[t]["lp"]`; `trader.manage_open_trades(current_prices)`;
  boundary = `minute%3==0 and second>=2` + `bucket_key` dedupe; **global mutex**
  (skip eval if any open trade); `force_close_forming` all tokens;
  `strategy.evaluate_all(sym_footprints, bar_book, now)`; entry only before
  `TRADE_ENTRY_CUTOFF_TIME`; `compiler.reset_for_new_candle()`.

### stock_order_flow/tick_compiler.py
- `TickCompiler.on_message` — alias-tolerant tick parsing (AUDIT T1–T6):
  lp from `last_traded_price|lp`; bp1 from `bp1|b|bid`; sp1 from `sp1|s|ask`;
  volume from `volume_trade_for_the_day|v`; `vol_delta` only when cumulative rises;
  L2 qty = Σ(bq1..5)/Σ(sq1..5) with `bv/sv` fallback; one-time `tick keys sample` log.
- `get_compiled_footprints()` — per token: levels, total_buy_vol, total_sell_vol,
  delta, avg_l2_bid, avg_l2_ask.
- `reset_for_new_candle()` — clears footprint matrix (bar book kept).
- `subscribe()` — `mdp.add_tick_listener(on_message)` + WS start + subscribe "t" and "d".

### stock_order_flow/bar_book.py
- `floor_3m` — 09:15 grid (09:15, 09:18, …).
- `seed(token, df)` — REST warmup into `_completed` (session-hours filter).
- `on_tick(token, ltp, ts, volume_delta)` — forming-bar OHLC + volume accumulation.
- `force_close_forming(token, now)` — closes forming bar at boundary.
- `completed_df(token, min_bars)` — DataFrame of completed bars.

### stock_order_flow/historical_data_manager.py
- `fetch_and_seed_history(broker, tokens, token_map)` — 6 calendar days, interval='3',
  fields into/inth/intl/intc + volume via `intv|v` (AUDIT H1); parquet save + 6-day prune.

### stock_order_flow/strategy_engine.py
- `StockStructure.update_from_df` — talib EMA9/EMA200; daily-anchored VWAP
  (`cum_pv/cum_vol`); `upper = cum_high_vol/cum_vol` (VWAP of Highs);
  `lower = cum_low_vol/cum_vol` (VWAP of Lows); day_high/day_low.
- `StockStructure.context_state(price)` — PROX=0.20% → IN_ZONE; EXT=0.15% of
  day_high/day_low → AT_EXTREME; else NO_MANS_LAND (zero logic).
- `StockStrategyEngine.__init__` — structures, veto_until, **prev_delta, delta_hist** (AUDIT S1).
- `evaluate_all(footprints, bar_book, now)` — AUDIT S2 version: `min_bars=30`;
  per-symbol norm_delta; Setup C veto; Setup B flip-confirmed absorption;
  Setup A 2-close-sustain breakout + L2 guard; Setup D 2-bar stacked imbalance;
  priority sort B(0) > A(1) > D(2); returns single best.

### stock_order_flow/execution_manager.py
- Constants: MAX_OPEN_POSITIONS=1; LEVERAGE=5; TP_PCT=0.010; **SL_PCT=0.008** (AUDIT E2);
  MAX_HOLD_MINUTES=60.
- `__init__` — CapitalEngine(mode="LIVE"); **_day_key/_day_loss** (AUDIT E3).
- `execute_entry` — mutex; 09:15–15:00 gate; margin≥50; **daily loss cap −2%** (AUDIT E5);
  **notional = min(margin*5, margin*2)** (AUDIT E1); MKT order product "I";
  3×1s fill confirmation via `single_order_history.avgprc`; persist open.
- `manage_open_trades` — EOD flatten 15:10; TP/SL/time-stop; `_close_trade` MKT with
  fill truth; `_save_history` accrues **daily P&L** (AUDIT E4).

### unified_dashboard/dashboard.py
- `get_runtime_paths("stock_order_flow")` → OF funds (live), open positions,
  trade history, `stock_order_flow.log` tail.

### common/ dependencies
- `ShoonyaAdapter` / `market_data_provider` — WS + REST; **must expose
  `add_tick_listener(fn)` and dispatch normalized ticks to listeners** (VERIFY item).
- `CapitalEngine(mode="LIVE")` — real broker funds. `market_calendar` — gates.

## 3. THE FOUR LINES & CONTEXT GATE (per stock)
red = daily VWAP · yellow-upper = VWAP-of-Highs · yellow-lower = VWAP-of-Lows ·
blue = EMA9 · grey = EMA200.
Gate: IN_ZONE (within ±0.20% of the band envelope) → evaluate; AT_EXTREME (within
0.15% of day H/L) → veto logic only; NO_MANS_LAND → engine does NOTHING.

## 4. SETUPS (exact rules, post-audit)
- **A Momentum:** last TWO closes > upper AND > EMA9 (mirror below) + norm_delta ≥ ±10%
  + trend alignment + L2 guard (asks < bids×2 for BUY).
- **B Absorption:** previous bar heavy |Δ|≥15% INTO zone with progress <0.15% (effort
  without result) + current bar delta FLIPS sign with |flip|≥5% + L2 ratio 1.5 → reversal.
- **C Veto Shield:** AT_EXTREME + |Δ|≥20% + progress <0.10% → veto 300s (blocks A & D).
- **D Stacked Imbalance:** 2 consecutive bars each |Δ|≥50% (≈3:1 buy:sell) with
  close>open and close>prev-close (mirror) → momentum.
Priority: B > A > D. ONE signal per boundary; ONE open position globally.

## 5. RISK PARAMETER MASTER TABLE
| Param | Value | Where |
|---|---|---|
| Universe | 15 EQ | main.EQUITY_UNIVERSE |
| Bar | 3m | bar_book |
| Context PROX/EXT | 0.20% / 0.15% | strategy.context_state |
| A delta / sustain | ≥10% / 2 closes | evaluate_all |
| B absorb/flip | ≥15% / ≥5% | evaluate_all |
| C veto | ≥20%, 300s | evaluate_all |
| D stack | ≥50% ×2 bars | evaluate_all |
| L2 guard | ask<bid×2 (BUY) | evaluate_all |
| TP / SL | 1% / 0.8% | execution constants |
| Max hold / EOD | 60m / 15:10 | execution |
| Entry cutoff | 15:00 | main + execution |
| Leverage | 5x, capped 2x funds | execution E1 |
| Daily loss cap | −2% of funds | execution E5 |
| Open positions | 1 | execution mutex |

## 6. AUDIT FIX LEDGER (what changed, where, why)
- T1–T4 tick_compiler: alias-tolerant lp/bp1/sp1/volume reads — prevents silent
  zero-data death when adapter emits short WS keys (lp/v/b/s).
- T5 tick_compiler: bv/sv fallback when 5-level bq/sq absent — keeps L2 guards alive.
- T6 tick_compiler: one-time tick-keys log — field-contract proof at boot.
- M1 main: `current_prices.get(best_sym)` guard — no KeyError crash at boundary.
- E1 execution: notional cap 2x funds — kills 100% concentration in one trade.
- E2 execution: SL 0.5%→0.8% — noise-safe for equities with MKT exits.
- E3/E4/E5 execution: daily loss accrual + −2% entry gate — streak breaker.
- H1 historical: `intv|v` volume fallback — history seeds even if field name differs.
- S1/S2 strategy: prev_delta/delta_hist; min_bars=30; B flip enforced; A 2-close
  sustain; D real 2-bar stacking with correct older-bar index — tightens all setups.

## 7. STORY OF ONE TRADE (narrative)
09:15 boot seeds 6 days of 3m bars from REST+parquet, so EMA200/VWAP are warm.
Ticks stream in; TickCompiler splits each tick into price (bar book) and aggression
(footprint). At 09:18 the boundary fires: RELIANCE is IN_ZONE; yesterday-style
absorption printed — heavy selling last bar with <0.15% fall, bids 1.8x asks — and
this bar's delta flipped +6% → Setup B BUY candidate. No other stock beats priority 0.
Global mutex free, before cutoff → MKT BUY fills in ~1s at avgprc. Management:
+1% → TAKE_PROFIT; −0.8% → STOP_LOSS; 60m → TIME_STOP; 15:10 → EOD_FLATTEN.
Close writes trade_history.json with net_pl; daily loss ledger updates; dashboard
shows funds, active trade, and logs. If instead price sat at day-high with 20% delta
and no progress, Setup C would veto momentum for 5 minutes — the engine stands aside.

## 8. DEPLOY & VCS PROTOCOL (current practice)
Local edits by VS Code agent from exact FIND/REPLACE patches → files uploaded to
Architect for line-by-line verification → THEN push to GitHub main + VPS together.
Always keep a timestamped local backup before pushing. No PRs for this engine
(user decision); Architect verification replaces PR review.

## 9. KNOWN LIMITATIONS & ROADMAP
- VERIFY: `add_tick_listener` exists in common/broker/shoonya_adapter.py and
  dispatches ticks to listeners (else no data flows).
- VERIFY: TATAMOTORS token 3456 ↔ TMPV-EQ mapping via search.
- No trailing stop / partial TP on stocks yet (fixed TP/SL only).
- L2 averages are tick-mean of 5-level sums; MKT slippage assumed small on 15 liquid names.
- Roadmap: ATR-based SL, per-setup telemetry counters, parquet backtest harness,
  9-EMA trail for stocks, paper mode toggle.