# AEGIS — Autonomous Execution & Gamma Imbalance System

**Architecture Authority — Code-Grounded Audit**  
**Version:** 3.2 (V3.2 Macro-Aligned Empirical Microstructure Engine — Live)  
**Status:** ACTIVE · PM2 `aegis-bot` · `AEGIS/aegis_main.py:87`  
**Host:** Oracle Cloud `opc@137.23.41.38` · `ecosystem.config.js:3` · `cwd=/home/opc`  
**Broker:** Shoonya (NorenApi) · `common/config.py:30` · `BROKER=SHOONYA`  
**Instruments:** NIFTY (Mon/Tue/Fri, NFO, lot 65) · SENSEX (Wed/Thu, BFO, lot 20) · 0DTE weekly only  
**Last audited:** 2026-08-30 · Calibrated on 96,098 L2 options samples (NIFTY + SENSEX)  
**Supersedes:** All prior AEGIS V2 Gamma-Burst theoretical docs and `Migration to Aegis.md`

> **One-line mental model:** AEGIS is **not** a signal filter — it is a **Macro-Aligned, 4-Domain Quality-Gated Microstructure Impulse System**. A single options contract is admitted only after (1) the underlying cash index momentum (60s/180s spot drift) agrees with the trade, (2) higher-timeframe trend/structure permits it, (3) 60-second signed persistence confirms, (4) a fresh 0-100 empirical microprice/OFI trigger fires, and (5) the contract itself is tradable (spread/depth/premium). One open position at a time, strict max 3 trades/day cap, +5% TP / -3.5% SL / +2.0%→+1.5% trailing lock, 5-minute hard ceiling, 3 limit-chase steps to exit. `AEGIS/aegis_gamma_engine.py:72`, `AEGIS/core/entry_quality_engine.py:2`, `AEGIS/aegis_execution_manager.py:43`

---

## Table of Contents

1. [Authority & Scope](#1-authority--scope)
2. [Ten-Thousand-Foot View](#2-ten-thousand-foot-view)
3. [Directory & Artifact Map (Ground Truth)](#3-directory--artifact-map-ground-truth)
4. [Runtime Topology (Processes, Threads, Queues)](#4-runtime-topology-processes-threads-queues)
5. [Orchestration — `aegis_main.py`](#5-orchestration--aegis_mainpy)
6. [Brain — `aegis_gamma_engine.py`](#6-brain--aegis_gamma_enginepy)
7. [Core Sub-Engines (8 Modules)](#7-core-sub-engines-8-modules)
8. [Execution — `aegis_execution_manager.py`](#8-execution--aegis_execution_managerpy)
9. [Data & Persistence](#9-data--persistence)
10. [Config & Calibration](#10-config--calibration)
11. [L2 Archiver — `l2_options_archiver.py`](#11-l2-archiver--l2_options_archiverpy)
12. [Research & Analytics](#12-research--analytics)
13. [Parity Backtester — `backtest_engine.py`](#13-parity-backtester--backtest_enginepy)
14. [Hot-Path Discipline & Invariants](#14-hot-path-discipline--invariants)
15. [Threading & Concurrency](#15-threading--concurrency)
16. [Error Handling & Edge Cases](#16-error-handling--edge-cases)
17. [Audit Findings — Gaps & Tech Debt](#17-audit-findings--gaps--tech-debt)
18. [Testing & Verification Checklist](#18-testing--verification-checklist)
19. [Deployment & Operations](#19-deployment--operations)
20. [Change Log vs Prior Doc](#20-change-log-vs-prior-doc)

---

## 1. Authority & Scope

| Layer | Document | What it governs |
|---|---|---|
| **L0** | [`AGENTS.md`](../AGENTS.md) | Standing law for all AI/human contributors (Kronos decommissioned, zero-allocation hot paths, no pandas on tick) |
| **L1** | This file (`AEGIS/AEGIS V2.md` v3.1) | **Authoritative AEGIS runtime spec** — every claim is tied to a `file:line` below |
| **L2** | [`SYSTEM_ARCHITECTURE.md`](../SYSTEM_ARCHITECTURE.md) | Cross-engine map only — defers to this file for AEGIS behavior |
| **L3** | `AEGIS/Migration to Aegis.md` | **Historical** migration spec (pre-V3) — kept for reference, not authoritative |

**Reading rule:** When this file and `SYSTEM_ARCHITECTURE.md §2.2` disagree, this file wins for AEGIS. When either disagrees with `AGENTS.md` hot-path rules, `AGENTS.md` wins.

---

## 2. Ten-Thousand-Foot View

```
Shoonya WS (NorenApi, L1 + 5-level L2)
        │
        ├─── raw dict ──► AegisL2Archiver (passive parquet) ──► AEGIS/data/l2_raw/{Nifty,Sensex}/
        │
        └─── ParsedTick (zero-copy, __slots__) ──► TickCompiler (ARGUS/tick_compiler.py:28)
                                                        │  ParsedTick: tok,lp,vol,bp[5],bq[5],sp[5],sq[5],ts
                                                        │  Pool: _tick_pool per symbol, reused in-place (line 209)
                                                        │
                                           AegisGammaEngine.on_tick_parsed  (635 lines)
                                                        │
                                            [OCCAM & MACRO-ALIGNMENT GATE]
                                            (Tick rate ≥ 1.5/s & Spot 60s/180s drift agreement)
                                                        │
                                          ┌─────────────┼──────────────────────────┐
                                          │             │                          │
                                   MicrostructureScorer  TimeBarEngine    DirectionalPersistence
                                   (0-100 logit)        (1m/3m EMA+ATR)   (60s signed window)
                                          │             │                          │
                                          └──────┬──────┴──────────┬───────────────┘
                                                 │                 │
                                          SignalStateMachine  OptionQualityEngine
                                          (OBSERVE→PRIMED→CONFIRMED→FIRED→COOLDOWN)
                                                 │                 │
                                                 └────────┬────────┘
                                                          │
                                                   EntryQualityEngine (EQS 0-100)
                                                          │  threshold 70  (dispatch)
                                                          ▼
                                                   signal_queue (queue.Queue maxsize 1000)
                                                          │
                                          AegisExecutionManager._run_loop (daemon thread)
                                                          │  StrikeSelector → CapitalEngine → OrderLifecycle
                                                          │  Shoonya LMT (Day) or Paper sweep
                                                          ▼
                                                   TradeContext + TradeLeg  →  common/ipc live_state
                                                          │  on_fill() ──► engine latency/edge buffers
                                                          ▼
                                                   _chase_exit_thread (3-step limit chase)
                                                          │  TP +5% | SL -3.5% | trail +2.0→+1.5% | 5m hard stop | spot ±25/50pts
                                                          ▼
                                                   trade_history.json + aegis_active_trade_context.json + OutcomeTelemetry JSONL
```

**Tick → Signal latency budget:** WS thread → `TickCompiler.on_message:209` (parse into pooled `ParsedTick`) → `AegisGammaEngine.on_tick_parsed:197` (O(1) scalar math only) → V3 4-domain pipeline gated inside same call (lines 309-435). No pandas/scipy/sklearn on this path — enforced by `AGENTS.md` and by import placement (heavy libs only in `l2_options_archiver.py:29` and `backtest_engine.py:25` which are off-hot-path).

---

## 3. Directory & Artifact Map (Ground Truth)

Enumerated 2026-08-29 by `glob "AEGIS/**/*"` and `read` of each file header. Hidden `__pycache__` omitted.

```
AEGIS/
├── __init__.py                         0 B    placeholder (no exports)
├── aegis_main.py                     360 L    Orchestrator: PM2 entry, WS wiring, 1-min digest, ATM shift, archiver attach
├── aegis_gamma_engine.py             635 L    CORE BRAIN: Empirical Microstructure Impulse Engine + Macro-Alignment + V3 4-domain gating
├── aegis_execution_manager.py        836 L    EXECUTION: signal consumer, StrikeSelector, LMT chase, fill→engine feedback
├── l2_options_archiver.py            326 L    ARCHIVER: passive raw dict → partitioned Snappy Parquet (15-min, 30d retention)
├── backtest_engine.py                532 L    PARITY BACKTESTER: drives production engine off parquet, position manager, capital
├── research_analytics.py             427 L    RESEARCH: Plotly chart builders from parquet (heatmap, IV crush, OI, decay, OIB)
├── research_feature_backtest.py      827 L    RESEARCH HARNESS: precursor signal audit (futures lead-lag, Hawkes, queue depletion)
├── requirements.txt                   10 L    Engine-local deps (SmartApi, polars, pyarrow, scipy, lightgbm)
├── feature_matrix.parquet                ← output of research_feature_backtest.py (idempotent cache at line 782)
│
├── core/                               9 modules, ~1,650 L total
│   ├── __init__.py
│   ├── microstructure_scorer.py      228 L    Empirical 0-100 logit (14 weights, intercept -0.413, 30s window)
│   ├── time_bar_engine.py            306 L    Time-bucketed 1m+3m OHLC, EMA(9) slopes, ATR(14), 30-min baseline, market structure
│   ├── directional_persistence.py    167 L    60s × 12 × 5s snapshots, signed +1/0/-1, 70% persistence, ≤1 opposite
│   ├── regime_chop_gate.py           137 L    KER + range compression + variance ratio + post-trade cooldown (120s)
│   ├── signal_state_machine.py       166 L    OBSERVE→PRIMED(≥78)→CONFIRMED→FIRED(≤3s)→COOLDOWN(120s)
│   ├── option_quality.py              78 L    Spread ≤2.5%, depth ≥500, premium 5-400
│   ├── entry_quality_engine.py       139 L    EQS 0-100, 4 domains (30+25+25+20), dispatch 70
│   ├── outcome_telemetry.py          172 L    Forward MFE/MAE at 30/60/180/300s, +5/+8% hit, JSONL persistence
│   ├── instrument_repository.py      348 L    SQLite WAL, atomic staging swap, nearest-expiry resolution
│   └── causal_microstructure_pipeline.py 451 L Offline causal extractor + ProductionShadowMicrostructureScorer (L1-L5 OFI/microprice)
│
├── execution/
│   ├── __init__.py
│   ├── data_fetcher.py               232 L    Instrument routing, candle bootstrap, active future/option resolution
│   └── l2_guard.py                    21 L    One-shot depth helper (unused in V3 hot path, see §17)
│
├── config/
│   ├── __init__.py
│   ├── settings.py                    47 L    Re-exports common/config, binds runtime "aegis" (DATA_DIR=AEGIS/data, LOGS_DIR=AEGIS/logs)
│   └── market_calendar.py              2 L    Shim re-exporting common/market/market_calendar
│
├── research/
│   ├── aegis_5min_consistency_lab.py 880 L    Standalone 5-min consistency & retraining lab (96k candidates, LODO CV, LGBM gain ranking)
│   ├── lob_quantum_lab.py            629 L    Colab LOB-MAE self-supervised lab (W=64, S=8, 10×3 channels, train-day norm)
│   └── __pycache__/
│
├── data/                               (git-ignored on VPS, sampled locally)
│   ├── params/
│   │   ├── microstructure_weights.json       Calibrated model (n=96098, 5-min exit horizon, statutory friction)
│   │   ├── microstructure_weights_v2_5min.json Drop-in backup weights
│   │   ├── aegis_l2_brain_v1.json
│   │   ├── aegis_calibrated_params.json
│   │   ├── sensex_l2_brain_v1.json
│   │   └── aegis_l2_decision_rules.json
│   ├── ml_results/
│   │   └── aegis_5min_consistency_report.json Full empirical report across 5 days (NIFTY + SENSEX)
│   ├── l2_raw/Nifty/*.parquet           Parquet partitions written by l2_options_archiver (also mirrored in aegis_local_l2_raw/)
│   ├── aegis_local_l2_raw/{Sensex,Nifty}/*.parquet  Historical archive partitions (30d retention)
│   ├── trades/{trade_history.json, aegis_state.json, aegis_active_trade_context.json}
│   ├── capital_state.json
│   ├── ml_features/options_gamma_dataset.parquet
│   └── scratch/underlying_candles_{NIFTY,SENSEX}.parquet  Candle bootstrap cache (see data_fetcher:35)
│
├── archive/                            historical snapshots
└── logs/
    ├── app.log
    ├── astra_bot.log
    └── telemetry/aegis_5min_outcomes_YYYY-MM-DD.jsonl  Written by OutcomeTelemetryEngine:164
```

**Shared dependencies (outside AEGIS, but load-bearing):**

| Shared artifact | Path | Role for AEGIS |
|---|---|---|
| TickCompiler + ParsedTick | `ARGUS/tick_compiler.py:28,42` | Zero-copy parser, symbol map, pooled tick dispatch (borrowed cross-engine — AEGIS imports it at `aegis_main.py:29`) |
| ShoonyaAdapter | `common/broker/` | WS session, `subscribe()`, `get_quote()`, `place_order()`, `add_tick_listener()` |
| Market calendar | `common/market/market_calendar.py:1` | `is_trading_day/is_market_open/is_bot_active_hours/is_pre_market/sleep_until_next_premarket` |
| Capital & orders | `common/execution/{capital_engine:11, order_lifecycle:15, strike_selector, trade_context:1}` | Paper/live fund accounting, limit pricing, strike allocation, context lifecycle |
| IPC | `common/ipc/live_state.py:70,156` | `AEGIS_STATE_FILE`, `update_aegis_live_state()` (1-min digest at `aegis_main.py:327`) |
| Runtime paths | `common/runtime/paths.py:11` | `get_runtime_paths("aegis") → AEGIS/data, AEGIS/logs` (WAL settings mirrored in repository at `instrument_repository.py:31`) |
| Symbols | `common/symbols.py` | Universe scanner at `scripts/universe_scanner.py` (15-min, not daily) |
| Analytics persistence | `common/analytics_persistence.py` | Used by `research_analytics.py:34` for snapshot vs partition loading |

---

## 4. Runtime Topology (Processes, Threads, Queues)

### 4.1 PM2 Processes — `ecosystem.config.js:1`

| PM2 name | Script | CWD | MaxMem | Restarts | Env |
|---|---|---|---|---|---|
| `aegis-bot` | `AEGIS/aegis_main.py` | `/home/opc` | 1G | 20 (5s delay) | `PYTHONPATH=/home/opc`, `BOT_RUNTIME=aegis` |
| `argus` | `ARGUS/main.py` | `/home/opc` | 1G | 20 | `BOT_RUNTIME=ARGUS` |
| `stratos-bot` | `STRATOS/main.py` | `/home/opc` | 1G | 20 | `BOT_RUNTIME=stratos` |
| `unified-dashboard` | `unified_dashboard/dashboard.py` | `/home/opc` | 400M | 10 (3s) | — |
| `eod-calibrator` | `ARGUS/eod_calibrator.py --cron` | `/home/opc` | 1G | — | `BOT_RUNTIME=ARGUS` |

> **System Architecture note:** `SYSTEM_ARCHITECTURE.md:26` still lists `l2-archiver` as a standalone PM2 app (legacy). In the current V3 design the per-engine archivers are **embedded** — `aegis_main.py:174` attaches `AegisL2Archiver` as a tick listener rather than a separate process. Align on next deploy.

### 4.2 AEGIS Thread Model

```
Main thread (aegis_main.py:227)  "while True" @ 0.2s cadence
   ├── pre-market handshake (08:55)              — data_fetcher + broker session
   ├── 1-min boundary: spot fetch, ATM shift, subscribe, digest, IPC  (242-327)
   └── sleep(0.2) yield to tick threads

Shoonya WS thread(s) (NorenApiPy, inside ShoonyaAdapter)
   └── TickCompiler.on_message  (borrowed)  →  engine.on_tick_parsed (SAME thread, non-blocking)
           └── Archiver.on_tick (separate listener, same WS dispatch thread — passive, no math)

AegisExecutionManager._run_loop  (daemon, aegis_execution_manager.py:225)
   ├── signal_queue.get(timeout=0.5) → _process_aegis_signal
   ├── _manage_open_trade (spread/depth/trailing/SL/TP/time)
   ├── check_pending_edges (fills → edge)
   └── _handle_premarket_bridge (09:08-09:15:30)

_chase_exit_thread  (daemon per close, _close_all_legs:579 → _chase_exit_thread:581)
   └── 3-step limit chase (0.99→0.98→0.97 of live bid) with sleeps 0.8s/1.0s/0.8s
```

### 4.3 Queues & IPC

| Channel | Type | Config | Producer → Consumer |
|---|---|---|---|
| `signal_queue` | `queue.Queue(maxsize=1000)` at `aegis_main.py:142`, passed to `create_aegis_engine:584` and `AegisExecutionManager:74` | Blocking put at `aegis_gamma_engine.py:533` (`put_nowait` — drops only if full, logs warning at 520) | Engine hot path → Execution manager loop |
| `SHARED_TICK_FILE` | JSON on shm | `common/ipc/live_state.py:29` | WS compiler → dashboard (not used by AEGIS hot path) |
| `AEGIS_STATE_FILE` | JSON via `_atomic_write:108` | `common/ipc/live_state.py:32,156` | `aegis_main.py:313` (1/min) → `unified_dashboard` readers |
| `outcome JSONL` | `aegis_5min_outcomes_YYYY-MM-DD.jsonl` | `outcome_telemetry.py:167` (`a` append, line-delimited) | `OutcomeTelemetryEngine._persist_record` → offline calibration |

---

## 5. Orchestration — `aegis_main.py`

**File:** `AEGIS/aegis_main.py:1` · 360 lines · Entry `main():87` · PM2 `script: "AEGIS/aegis_main.py"` at `ecosystem.config.js:5`

### 5.1 Bootstrap (`main:87` → `try:97`)

```python
BOT_RUNTIME = "aegis"                              # line 17 — must precede settings import
import AEGIS.config.settings  # noqa: F401          # line 19 — binds DATA_DIR/LOGS_DIR via configure_runtime("aegis")
broker = ShoonyaAdapter()                           # line 100, stdout suppressed
fetcher = DataFetcher(broker)                       # line 101
spot_token, spot_sym, spot_exch = fetcher.get_cash_index_token()  # 105  (26000/NIFTY/NSE or 1/SENSEX/BSE)
fut_token, fut_sym, ... = fetcher.get_current_futures_token()      # 119  (nearest FUTIDX)
weekly_opts = fetcher.get_weekly_option_tokens()    # 127
token_map = {token: {side, strike, expiry}}         # 128-135, is_call_symbol heuristic at 33
token_to_symbol = {token: symbol}                   # + spot/fut synthetics at 114,123
bucket_map = {sym: 500_000.0}                        # 139 — RESERVED, not gated on in V3 (comment at 137)
signal_queue = queue.Queue(maxsize=1000)            # 142
aegis_engine = create_aegis_engine(token_to_symbol, token_map, bucket_map, strike_step, signal_queue) # 144
aegis_engine.underlying_tokens.update({spot_token, "26000","1","99919000","99926000"}) # 152
trader = AegisExecutionManager(broker, fetcher, aegis_engine, signal_queue) # 155
compiler = TickCompiler(broker); compiler.set_symbol_map(t2s); compiler.set_engine(engine) # 164-166
compiler.subscribe(list(t2s.keys()))                # 167 — subscribes ALL weekly tokens up-front
aegis_archiver = attach_aegis_archiver(broker, token_to_symbol, token_map) # 174 — passive listener
trader.start()                                      # 187 — launches _run_loop daemon
```

**Key design choices:**

* **Unified TickCompiler borrow:** AEGIS imports `ARGUS/tick_compiler.py:29` for `ParsedTick` and `TickCompiler`. This couples AEGIS to ARGUS lifecycle but gains the zero-copy pool and reconnect logic (MN-4) for free. `common/market/tick_types.py` was proposed in `Migration to Aegis.md:89` — not yet materialized.
* **Initial subscribe covers full weekly chain**, not just ATM±10 — `aegis_main.py:273` subscribes `all_opt_tokens = CE+PE` (entire nearest expiry). Dynamic re-subscription at the 1-min boundary (line 282-289) only issues **delta** tokens (new strikes at expiry roll).
* **Bucket map is dead weight in V3** — `aegis_main.py:137` reserves ₹5L/contract but `aegis_gamma_engine.py` never gates on buckets. Kept for dashboard schema compat and historical continuity.

### 5.2 Session & Calendar Gates

| Gate | Predicate | Source |
|---|---|---|
| Market open | `9:15 ≤ now ≤ 15:15`, weekday, not in `NSE_HOLIDAYS_2026` | `common/market/market_calendar.py:48,8` |
| Bot active | `08:55 ≤ now ≤ 15:17` | `market_calendar.py:70` |
| Pre-market | `08:55 ≤ now < 09:15` | `market_calendar.py:78`, `aegis_main.py:210,238` |
| Entry cutoff | `≤ 15:00` | `aegis_execution_manager.py:44` (`AEGIS_ENTRY_CUTOFF`) and `can_enter:179` (`9:16 ≤ now ≤ 15:00`) |
| Force flatten | `≥ 15:10` | `aegis_execution_manager.py:45,182` |
| Premarket handshake | `perform_premarket_handshake()` restores session, ensures WS, subscribes spot + weeklies | `aegis_main.py:44` (called at 67, 211, 235) |

### 5.3 1-Minute Boundary (lines 242-327)

Executed when `now.minute != last_minute` (243):

1. `spot_price = fetcher.get_index_ltp()` (246). Fail → skip boundary (248).
2. Re-resolve `step` (50 for NIFTY-only, else 100) at 253-256.
3. `atm_strike = round(spot/step)*step` (259). If shifted since `last_atm_strike`, call `engine.shift_atm(shift_steps, atm_strike)` at 262.
4. Re-resolve `weekly_opts` (267). Partition CE/PE via regex `CE|CALL` / `PE|PUT` (270-271).
5. `all_opt_tokens = CE+PE` (273). If delta exists and `broker.market_data_provider` exists, `mdp.subscribe(new_tokens, exch_seg, feed_type="d")` at 284 and update `mdp._option_ce_tokens/_option_pe_tokens` (279-280) for the broker's routing.
6. If `aegis_archiver` exists, rebuild `refreshed_t2s/refreshed_tmap` incl. spot token and call `archiver.update_token_maps()` at 306.
7. `digest = build_minute_digest(now, engine, trader, spot)` (309) — formatted at `build_minute_digest:69` as `"[HH:MM] | Spot: … | ATM: … | ATM IV: … | Scorer: ON/OFF | Fills: N | OPEN …"` — and `logger.info(digest)` at 310.
8. `update_aegis_live_state(state_payload)` at 327 with 9 keys:
   ```python
   {active_index, spot_price, atm_strike, atm_iv, gamma_sum:0.0, hawkes_B:0.0,
    scorer_active, high_conviction_threshold, trades_today, daily_pnl,
    has_open_trade, params_source}
   ```
   `gamma_sum/hawkes_B` are **deprecated stubs** (kept for dashboard compat, comment at 318).

### 5.4 Main Loop Cadence

`time.sleep(0.2)` at line 330 — 5 Hz yield. Not a busy loop. Tick processing is WS-thread-owned; main loop only orchestrates boundary tasks.

### 5.5 Shutdown (`339-350`)

`trader.stop()` (341, joins 2s), force-flatten if still open (342-344), `aegis_archiver.shutdown()` (348) which force-flushes the 15-min partition, then `logger.info "stopped cleanly"`.

---

## 6. Brain — `aegis_gamma_engine.py`

**File:** `AEGIS/aegis_gamma_engine.py:1` · 601 lines · Class `AegisGammaEngine:72` · Factory `create_aegis_engine:584` · Logger `aegis_gamma:58`

**Heritage comment:** `aegis_gamma_engine.py:3` — *"Reverse-engineered from 812 real ≥10% clean option impulse moves (NIFTY & SENSEX)"*. `microstructure_scorer.py:3` widens to 2,436 events. The 812 figure is per-index subset; 2,436 is pooled across strikes/directions.

### 6.1 Constants & Window Sizing

| Symbol | Value | Line |
|---|---|---|
| `N_STRIKES` | 21 (ATM ±10) | `aegis_gamma_engine.py:63` |
| `ATM_IDX` | `21 // 2 = 10` | 64 |
| `PARAM_DIR` | `get_data_dir()+"/params"` | 66 |
| `PARAM_FILE` | `microstructure_weights.json` | 67 |
| `spot_history` | `deque(maxlen=600)` → 600 ticks ≈ 10 min at 1 Hz | 121 |
| `tick_ring[sym]` | `deque(maxlen=100)` per symbol → 2s window at line 230-234 | 151 |
| `fill_latency_buf / post_fill_edge_buf` | `np.zeros(500, float32)` ring | 145-146 |
| `gamma_tensor` | `np.zeros((2,21), float32)` — **compat stub** | 156 |

### 6.2 Parameters — `_load_or_default_params:163`

* Reads `microstructure_weights.json` if exists, else returns priors:

  ```python
  {model_name, high_conviction_threshold:75.0, moderate:60.0, cost_bps:2.0, hold_time_sec:30.0, burst_cooldown_sec:60.0}
  ```
  at line 174.
* `high_conviction_threshold` cached at 105 and 188; `min_tick_rate_gate` at 153 and `reload_params:188`.
* `reload_params:184` hot-reloads weights file and reconstructs `MicrostructureScorer`. Called via no admin endpoint currently exposed (see §17).

### 6.3 Owned Sub-Engines (lines 107-142)

| Field | Class | Condition | Purpose |
|---|---|---|---|
| `scorer` | `MicrostructureScorer` | `MicrostructureScorer is not None` (try/except import at 30) | 0-100 empirical logit (see §7.1) |
| `chop_gate` | `RegimeChopGate` | same | O(1) chop veto (KER/range/VR) |
| `time_bar_engine` | `TimeBarEngine(ema9, atr14)` | same | 1m+3m OHLC, EMA slopes, ATR ratio, market structure |
| `persistence_engine` | `DirectionalPersistenceEngine(60s, 5s)` | same | Signed persistence over 12 snapshots |
| `option_quality_engine` | `OptionQualityEngine(2.5%, 500)` | same | Spread/depth/premium tradability |
| `signal_fsm` | `SignalStateMachine(3s/8s/120s/60s)` | same | Centralized lifecycle |
| `eqs_engine` | `EntryQualityEngine(70.0)` | `dispatch_threshold = P["eqs_dispatch_threshold"]` | Unified 0-100 composite |
| `outcome_telemetry` | `OutcomeTelemetryEngine(log_dir=…/logs/telemetry)` | same | Forward MFE/MAE, JSONL |

All 8 are **optional** — guarded by `if X is not None` at the import fallback (49-56) and at every use site. A missing import degrades to permissive pass-through (e.g., `has_permission=True` at 314 if no `time_bar_engine`).

### 6.4 Tick-Rate Gate (O(1) Zero-Alloc Ring)

At `on_tick_parsed:228`:

```python
ring = self.tick_ring[sym]  # deque(maxlen=100)
ring.append(now_sec)
while ring and ring[0] < (now_sec - 2.0): ring.popleft()
live_tick_rate = len(ring) / 2.0   # ticks/sec over last 2s
```

Veto at line 303:

```python
if live_tick_rate < self.min_tick_rate_gate:  # default 1.5 (calibrated spikes 1.895 vs controls 0.500 at line 152)
    logger.info(f"[OCCAM VETO] {sym} Score={score:.1f} tick_rate={live_tick_rate:.2f} < {gate:.1f}")
    return
```

### 6.5 Hot Path — `on_tick_parsed:197`

**Signature:** `on_tick_parsed(self, tick) -> None` where `tick: ParsedTick` (duck-typed at 200-209, no import of `ParsedTick` type — relies on attribute presence).

Pseudo-flow (annotated with line ranges):

```
197 def on_tick_parsed(tick):
198   try:
200     tok_str = str(tick.tok); sym = t2s.get(tok_str) or tick.sym
205     now = tick.ts; now_sec = now/1000 if ms else now
208     lp = float(tick.lp); if lp<=0: return
212     if tok_str in underlying_tokens:            # 212 — spot index tick
213         _on_underlying_tick(sym, lp, now_sec)  # 213 — updates spot EMA, chop_gate, TimeBar, persistence
214         return
217     option_cache[tok_str] = lp                # 217 — for StrikeSelector reuse
218     outcome_telemetry.update_tick(sym, lp, now_sec)  # 218-219
220     info = token_map.get(tok_str); side=info["side"], strike  # 221-226
228     tick_ring live_tick_rate (2s window)       # 228-236
239     Extract bp1..5/sp1..5/bq1..5/sq1..5 scalar O(1)  # 239-264 (two branches: ParsedTick bp/sp arrays vs bp1/sp1 attrs)
266     tot_bid_depth/bid_ask_depth sum L1-L5      # 266-267
269     scorer.update_tick(sym, now_sec, lp, bp1..5, ...)  # 270
272     Build otm_rets{1..4} via strike_to_symbol + scorer.history  # 273-280
282     opp_ret via (1-side, strike)              # 282-287
289     score,is_high,feats = scorer.compute_score(sym, now_sec, otm_rets, opp_ret) # 290
298     if not is_high or score<threshold: return # 298 (+ cooldown check at 299-301)
303     if live_tick_rate < min_tick_rate_gate: return (OCCAM veto)
316     direction = "UP" if side==0 else "DOWN"   # 317
318     [V3.2 MACRO GATE] get_spot_drift(60s, 180s) → veto if UP & drift<0 or DOWN & drift>0 # 320-330
332     [V3] time_bar_engine.get_permission_state(direction) → veto if not has_permission and not warmup_permissive # 334-338
341     [V3] persistence_engine.evaluate_persistence(direction) → pers_confirmed/pers_score # 345
349     [V3] option_quality_engine.qualify_option(sym,lp,bp1,sp1,tot_bid,tot_ask) → veto if not qualified # 353
363     [V3] signal_fsm.update_trigger(sym, now_sec, direction, score, has_permission, pers_confirmed, qualified) # 369
375       if not should_fire: return (with PRIMED log at 379)
385     trigger_age = now_sec - fsm.get_or_create(sym).last_trigger_ts  # 389
395     [V3] eqs_engine.compute_eqs(ema_1m/3m, atr_ratio, market_structure, direction, persistence, opposite_count, micro_score, trigger_age, spread_pct, depth) # 398-410
412     if is_eqs_qual:
413       outcome_telemetry.register_entry(event_id, sym, direction, now_sec, lp, last_spot, eqs, features_snapshot) # 415
425       signal_fsm.register_trade_executed(sym, now_sec)  # 426
428       cooldowns[sym]=now_sec                           # 428
434       _emit_signal(sym, lp, direction, score, gates, metrics) # 434 → signal_queue.put_nowait 555
450   except Exception as e: logger.error(...)
```

**Gating order is load-bearing:** OCCAM tick-rate (310) → Macro-Alignment (320-330) → Permission (334) → Persistence (345) → Option Quality (353) → FSM (369) → EQS (398). Reordering would change fill semantics.

### 6.6 Spot Path — `_on_underlying_tick:439`

```
439 def _on_underlying_tick(sym, lp, now_sec):
443   last_spot=lp; spot_history.append((now_sec,lp))
446   chop_gate.update(lp, now_sec)           # 446-447
450   time_bar_engine.update_tick(lp, now_sec)  # 450-451
454   if persistence_engine.should_sample(now_sec):  # 454 — every 5s
455     curr_dir = derive from ema_1m_slope or 5-tick drift  # 455-468
470     persistence_engine.record_snapshot(now_sec, curr_dir, micro_score_hint, drift_5s) # 470
478   prune spot_history older than 300s       # 478-480
483   EMA fast (α=0.05 → 1-min) and slow (α=0.01 → 5-min)  # 483-488
490   new_atm = round(lp/step)*step; if shift: log "Dynamic ATM roll" # 490-497
```

Note: EMA update uses `0.05*lp + 0.95*ema` — not a true EMA on bars but a tick-level exponential smoothing (coarse).

### 6.7 Fill Feedback — `on_fill:539`, `check_pending_edges:554`, `_hawkes_branching:566`

* `on_fill(fill_ts, fill_price, signal_ts, signal_price, sym)`: stores `lat_ms` at `fill_latency_buf[fill_ptr%500]` (543-544), increments `fill_ptr`, appends to `_pending_edge_measures` with `measure_at=fill_ts+30.0` (545-550), logs at 552. Signature is variadic-friendly (539: `sym=None` default) — execution manager must adapt (see §8.4).
* `check_pending_edges(now, current_spot)` (554): polled by `AegisExecutionManager._run_loop:249` at 2 Hz. When `now ≥ measure_at`, writes `ret_bps = (spot - fill)/fill * 10000` into `post_fill_edge_buf[idx]` (560). Otherwise keeps item in `still_pending`.
* `_hawkes_branching:566` — returns `0.0` (stub). The Hawkes branching concept lives in `research_feature_backtest.py` and `causal_microstructure_pipeline.py`, not in the live path.

### 6.8 State Snapshot — `get_state_summary:569`

Returns 7 keys consumed by `aegis_main.build_minute_digest:73` and `update_aegis_live_state:313`:

```python
{spot, atm_strike, atm_iv, gamma_sum:0.0, hawkes_B:0.0, fills_recorded, pending_signals, scorer_active, high_conviction_threshold}
```

`gamma_sum/hawkes_B` are zero-stubs (deprecated after empirical scorer replaced the gamma/Hawkes model).

---

## 7. Core Sub-Engines (8 Modules)

### 7.1 `microstructure_scorer.py:15` — Empirical 0-100 Logit

**Class:** `MicrostructureScorer(weights_path, high_conviction_threshold=75.0)`  
**Calibrated on:** `microstructure_weights.json` — `n=96,098` (NIFTY + SENSEX 5-day purged dataset), `intercept -0.413283`, 15 `feature_weights` targeting 5-min forward exits (+5% TP, -3.5% SL, statutory friction) (see §10).  
**Window:** `max_window_sec=30.0` at line 57; `history[sym]=deque(maxlen=200)` at 91.

**`update_tick:59`** — appends `(ts, lp, bp1, sp1, b1..5, a1..5)` and prunes ` < ts-30s`. Kwarg compat for legacy callers (`bq1/sq1/deep_bid_qty`, lines 80-88).

**`compute_score:100`** — 8 feature families → linear logit → sigmoid → 0-100. Full traceability per tick:

| # | Feature (key) | Formula (scalar O(1)) | Line | Weight in `microstructure_weights.json` |
|---|---|---|---|---|
| 1 | `target_tick_intensity` | `len(buf)/dt` where `dt=max(0.5, t1-t0)` | 126 | **+0.503295** |
| 2 | `target_pre_ret` | `(p1-p0)/p0` | 129 | **-2.252630** (mean-reverting: large pre-run penalized) |
| 3 | `touch_pressure` (`i1`) | `(b1-a1)/(b1+a1+1e-4)` | 132 | **+0.012609** |
| 4 | `deep_pressure` | `(i3+i4+i5)/3` | 140 | **+0.113511** |
| 5 | `layer_divergence` | `i1 - deep_pressure` | 141 | **-0.005502** |
| 6 | `book_slope` | `(i5-i1)/4` | 142 | **-0.316123** |
| 7 | `target_ask_vacuum_vel` | `(da1-da0)/dt`, `da=L3-L5 ask` | 150 | -1.6e-05 |
| 8 | `target_bid_build_vel` | `(db1-db0)/dt` | 151 | -3.0e-06 |
| 9 | `target_ask_depth_decay` | `(da1-da0)/(da0+1)` | 152 | +1.0e-06 |
| 10 | `target_l1_ofi` | `sum(max(d_b,0)-max(-d_s,0)) / mean_q` over window | 155-166 | +9.3e-05 |
| 11 | `target_top5_imbalance` | `(bid5-ask5)/(bid5+ask5+1)` | 171 | **-0.004343** |
| 12 | `target_spread_compression` | `(sp0-bp0)/p0 - (sp1-bp1)/p1` (>0 = tightening) | 174-176 | **-0.031951** |
| 13 | `otm1_ret` / `otm2_ret` | Supplied by caller (adjacent strike returns) | 178-183 | **+1.591908** / **-0.273950** |
| 14 | `cascade_coherence` | `mean(valid_otm_rets)` | 186 | **+0.992641** |
| 15 | `opp_side_ret` | Caller-supplied opposite side return | — | **+0.893330** |

`cascade_coherence` penalizes wide OTM participation — weight positive but OTM2-4 negative means isolated ATM spike scores higher than broad-chain moves (gamma coherence inversion).

**Logit → Score (lines 215-227):**

```python
z = raw_intercept + Σ weight[feat]*feat_val
score = 100/(1+exp(-z))  # clamped: z≥40→100, z≤-40→0
is_high = score ≥ high_conviction_threshold (75.0, or 60 moderate at 23)
```

### 7.2 `time_bar_engine.py:33` — Time-Based OHLC & Permission

**Class:** `TimeBarEngine(ema9, atr14, atr_baseline 30, max_history 100)` at line 38.

**State:** `bars_1m/bars_3m` deque(100), `ema_1m/ema_1m_prev/ema_1m_slope`, `atr_1m_history`, `current_atr_1m/baseline_atr_1m/atr_ratio` (69-77).

**`update_tick:79`** — buckets `bucket_1m = int(ts//60)`, `bucket_3m = int(ts//180)`, updates in-flight `OHLCBar` (open/high/low/close/tick_count) or closes the prior bar and calls `_on_1m_bar_close`/`_on_3m_bar_close`. Returns `(new_1m, new_3m)` booleans (unused by caller at `aegis_gamma_engine.py:451` — fire-and-forget).

**`_on_1m_bar_close:174`** — EMA: `k=2/(9+1)=0.2`, `ema = close*k + ema*(1-k)`, `slope = ema - ema_prev` (185-187). ATR: `tr = max(high-low, |high-prev_close|, |low-prev_close|)` (192-198), `current_atr_1m = mean(last 14 TR)`, `baseline = mean(all history)` (203-208), `atr_ratio = current/baseline` (211).

**`get_market_structure:230`** — 3-state on last 4 bars (needs ≥4 at 237):

* `ALIGNED` for UP: `HH or (HL and close>prev_close)` at 250; for DOWN: `LL or (LH and close<prev_close)` at 258.
* `OPPOSED` for UP: `LH+LL and close<prev_close` at 252; inverse for DOWN at 260.
* Else `NEUTRAL`. Used as `market_structure` in EQS (91-95).

**`get_permission_state:267`** — returns `(has_permission, reason, metrics{ema_1m/3m_slope, atr_ratio, current_atr_1m, market_structure})` at 273. Warmup `<2 bars` → `(True, "warmup_permissive")` at 283. Hard veto only on `OPPOSED` at 286, `ema_3m_slope ±0.5` at 292-304 (direction-dependent), `ema_1m_slope ±0.5` at 295-304. No volatility gate — `atr_ratio` is reported but not vetoed here (consumed downstream by EQS at `entry_quality_engine.py:83`).

### 7.3 `directional_persistence.py:29` — 60s Signed State Window

**Class:** `DirectionalPersistenceEngine(window 60s, sample 5s, max 12, min_valid 4, threshold 0.70, max_opposite 1)` at 34.

**`record_snapshot:58`** — maps `UP/BULL/CALL→+1`, `DOWN/BEAR/PUT→-1` else `0` (69-75), appends `StateSnapshot(ts, direction, strength, micro_score, spot_drift)` and prunes ` < now-60s` (90).

**`evaluate_persistence:93`** — for `target_direction` CALL vs PUT:

* Warmup `n_obs==0` → `(True,1.0,"warmup_zero_obs_permissive")` at 112.
* `n_obs < min_valid(4)` and `opposite==0` → `(True,1.0,"warmup_aligned_permissive")` at 144, else insufficient if any opposite.
* Else `persistence = max(0, signed_sum/n_obs)` (150), veto if `opposite > max_allowed(1)` at 154 or `persistence < 0.70` at 161.

**Caller:** `aegis_gamma_engine.py:454` samples every 5s (`should_sample:54` checks `now - last ≥5`), deriving `curr_dir` from `ema_1m_slope ±0.1` or 5-tick drift `±0.5` (459-468) and recording with `micro_score_hint=75/50` (473).

### 7.4 `regime_chop_gate.py:15` — O(1) Chop Discriminator

**Class:** `RegimeChopGate(ker 60s @0.01, range 60s vs 1800s @0.04, vr q=5 @0.40, cooldown 120s, strike_step 50/100)` at 16.

**`update:51`** — maintains `_short` (60s) with incremental `_ker_path_sum += |Δspot|` (63) and `_long` (30-min) deques, plus `_vr_prices` fixed 121. Deduplicates stagnant ticks at 57.

**`get_regime_state:83`** — veto chain in order:

1. `post_trade_cooldown:125` — `now - last_close <120s`
2. `range_compressed:128` — `short_range/long_range <0.04`
3. `low_efficiency_chop:131` — `KER = |net| / path <0.01`
4. `mean_reversion_signature:134` — `VR = var_q/(q*var1) <0.40` (only if `len ≥ q*4` and `var1>1e-9`)

**Note:** Constructed at `aegis_gamma_engine.py:115` as `RegimeChopGate(strike_step=…, cooldown_sec=240)` — a diverging cooldown (240 vs class default 120) and never queried on the hot path (no `get_regime_state` call inside `on_tick_parsed`). Only updated in `_on_underlying_tick:447` and re-armed via `register_trade_close` from `aegis_execution_manager.py:722`. Effectively **shadow-mode** in V3.

### 7.5 `signal_state_machine.py:43` — Centralized Lifecycle FSM

**States:** `SignalState(OBSERVE, PRIMED, CONFIRMED, FIRED, COOLDOWN)` at `signal_state_machine.py:22`.

**Transitions (`update_trigger:67`, 6 args):**

```
OBSERVE ──[score ≥78.0 & has_permission]──► PRIMED (prime_ts, direction, prime_score)  108-115
PRIMED  ──[timeout 60s]──► OBSERVE                                          120-122
PRIMED  ──[persistence_confirmed & option_qualified]──► CONFIRMED            125-127
           └─[trigger_age ≤3s]──► FIRED (instant fire)                       129-132
CONFIRMED ──[not persistence or not qualified]──► OBSERVE                    138-140
CONFIRMED ──[age >8s]──► OBSERVE (stale trigger)                             143-145
CONFIRMED ──[age ≤3s & score ≥75]──► FIRED                                   147-150
FIRED ──► COOLDOWN (cooldown_until = now+120s)                               154-158
COOLDOWN ──[now ≥ cooldown_until]──► OBSERVE else stay COOLDOWN             86-90
```

Additional resets: `not has_permission` while PRIMED/CONFIRMED → OBSERVE (94-97); `direction flip` → OBSERVE (100-103); `update_trigger` unconditionally sets `last_trigger_ts=now` (104) so `trigger_age` is always ~0 within the same tick — the 3s/8s windows matter only across ticks where no new trigger re-primes.

**Caller:** `aegis_gamma_engine.py:351` feeds `has_permission/persistence_confirmed/option_qualified`. The `micro_score≥78` prime threshold at `signal_state_machine.py:110` is **higher** than the `high_conviction 75` — FSM is stricter than the scorer gate at `aegis_gamma_engine.py:298`.

### 7.6 `option_quality.py:17` — Tradability Filter

**`OptionQualityEngine(max_spread 2.5%, min_top5 500, min_prem 5, max_prem 400)` at 22.**

**`qualify_option:34`** — checks in order:

1. `lp <5 → "premium_too_low"` (60), `lp >400 → "premium_too_high"` (62)
2. `spread_pct = (sp1-bp1)/mid *100` where `mid=(bp1+sp1)/2 or lp` (65-67), `spread >2.5% → "spread_too_wide"` (71)
3. `total_depth = bid5+ask5 <500 → "insufficient_depth"` (75) — only if depth>0 (avoids vacuous veto on zero-feed ticks)

Caller at `aegis_gamma_engine.py:334` passes `tot_bid_depth/total_ask_depth` (sum L1-L5 lots) — not rupee notional.

### 7.7 `entry_quality_engine.py:30` — Unified EQS 0-100

**`EntryQualityEngine(dispatch_threshold=70.0)`** — note mismatch: caller at `aegis_gamma_engine.py:141` passes `eqs_dispatch_threshold=70` from params, class default also 70; docs at `entry_quality_engine.py:22` claim 75.

**`compute_eqs:38`** — 4 domains:

| Domain | Max | Inputs | Scoring | Source |
|---|---|---|---|---|
| **Permission** | 30 | `ema_1m/3m slope`, `atr_ratio`, `market_structure`, `direction` | `trend = min(7.5, slope*5)` per TF if aligned (77), `vol: 1.20→10, 1.05→7, 0.95→4` (84-88), `structure ALIGNED 5 / NEUTRAL 2.5 / OPPOSED 0 (hard veto at 64)` | 70-97 |
| **Persistence** | 25 | `persistence_score 0-1`, `opposite_count` | `min(15, score*15) + (10 if opp==0 else 5 if opp==1 else 0)` | 99-102 |
| **Trigger** | 25 | `micro_score`, `trigger_age` | `min(15, (score-50)/50*15) + (10 if ≤3s else 5 if ≤8s else 0, but >8s already vetoed at 67)` | 104-114 |
| **Option** | 20 | `spread_pct`, `total_depth` | `spread ≤1→10, ≤2→5` (119-121) + `min(10, depth/2000*10)` (123) | 116-124 |
| **Total** | **100** | `sum(⋯)`, `qualified = eqs ≥ 70` | `round(⋯,2)` at 127 | 126-138 |

Hard vetoes returned as `(0.0, False, {reason})` at 64/68 before any scoring.

### 7.8 `outcome_telemetry.py:60` — Forward Research Logger

**`OutcomeTelemetryEngine(log_dir="AEGIS/logs/telemetry")`** at 60, `os.makedirs` at 67.

**`register_entry:70`** — creates `ForwardOutcomeRecord(event_id, symbol, direction, entry_ts, entry_opt_price, entry_spot_price, eqs_score, features_snapshot)` and stores in `active_records[event_id]` (95).

**`update_tick:97`** — called on every option tick at `aegis_gamma_engine.py:219` (before gating) and synchronously after fire at `aegis_gamma_engine.py:393`. For matching `symbol`, computes `ret_pct = (lp - entry)/entry*100` (115), updates `mfe/mae_*` at 30/60/180/300s (118-140), detects `hit_tp_5pct (+5%)` and `hit_tp_8pct (+8%)` with `time_to_tp` and `mae_before_tp_5pct` (143-150), and when `dt ≥300` marks complete, `_persist_record:164` appends JSONL to `aegis_5min_outcomes_YYYY-MM-DD.jsonl` at 167 (`json.dumps(asdict(record))`).

Retention: unbounded append; downstream `research_analytics.py` reads it only indirectly via parquet. No rotation — see §17.

### 7.9 `causal_microstructure_pipeline.py:116` — Offline + Shadow Online

**Two surfaces:**

* **Batched kernels (lines 24-110):** `compute_multilevel_ofi(bid_px/bid_qty/ask_px/ask_qty, decay [1,0.8,0.6,0.4,0.2])` (Cont-Kukanov-Stoikov, scalar `ofif`) and `compute_multilevel_microprice(…, depth [1,0.5,0.25,0.125,0.0625])` (Stoikov weighted microprice + relative spread). Used by `CausalMicrostructureExtractor.process_dataset` (offline forensic) and mirrored in the online scorer.
* **`CausalMicrostructureExtractor:116`** — strict causal split `(t0-30s, t0]` features vs `(t0, t0+45s]` label, spot-age ≤3000 ms, t0 token-lock, moneyness OTM1 (CE/PE), `min_price 15`, `target +10%`, `max_drawdown 3%`. Iterates `t0` every `5s` over `(min+30s, max-45s)`. Not on the hot path — research only.
* **`ProductionShadowMicrostructureScorer:298`** — zero-heap shadow logger mirroring live OFI/microprice with `shadow_log_file` JSONL at 437-447. Not instantiated in V3 (`aegis_gamma_engine.py` does not import it); the live dogma is `MicrostructureScorer`.

### 7.10 `instrument_repository.py:15` — SQLite Universe Registry

**Singleton** at line 17 (`__new__` at 20). `DB_PATH = DATA_DIR+"/instruments.db"` at 12 (`DATA_DIR` from `AEGIS/config/settings.py:12`).

**PRAGMAs at connection (31):** `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `timeout 60s`.

**Schema (42-98):** `schema_info(version, updated_at)`, `instruments(symbol UNIQUE, name, exch_seg, expiry, strike, lotsize, …)`, `broker_mappings(broker_name, broker_token UNIQUE, instrument_id FK)`, `sync_metadata(sync_id PK, source_hash, …)`. Indexes at 96.

**Critical resolves used by `data_fetcher`:**

* `get_futures_token("SHOONYA", name, exch_seg)` at 153 — scans `FUTIDX`, parses `expiry` as `%d-%b-%Y` or `%d%b%Y` (172-175), returns nearest expiry ≥ now.
* `get_weekly_option_tokens` at 192 — scans `OPTIDX`, resolves all valid `≥ now`, then takes the single `nearest_expiry` bucket (223-225). This is why ATM±10 is drawn from a single weekly chain.
* `get_count:244` — used by health checks.

**Atomic staging swap (`stage_and_swap_instruments:252`):** Creates `temp_instruments/temp_broker_mappings`, bulk inserts, deletes prior `broker_mappings` for that broker, garbage-collects orphan `instruments`, re-inserts, and records `sync_metadata`. Used by the external instrument sync job (not AEGIS itself).

---

## 8. Execution — `aegis_execution_manager.py`

**File:** `AEGIS/aegis_execution_manager.py:1` · 836 lines · Class `AegisExecutionManager:60` · Logger `aegis_execution_manager:40`

### 8.1 Risk Constants (lines 42-52)

| Constant | Value | Meaning |
|---|---|---|
| `MAX_HOLD_SEC` | 300.0 (5 min) | Hard ceiling, checked at `_manage_open_trade:569` |
| `AEGIS_ENTRY_CUTOFF` | `15:00:00` | `can_enter:179` upper bound |
| `AEGIS_FORCE_FLATTEN` | `15:10:00` | `must_force_flatten:184`, checked at `_manage_open_trade:516` |
| `MAX_TRADES_PER_DAY` | `int(os.getenv("MAX_TRADES_PER_DAY","3"))` | Hard cap at 46/312, daily reset at `_run_loop:231` and `_process_aegis_signal:306` |
| `TARGET_PROFIT_PCT` | `0.05` (+5%) | `_place_initial_take_profit:435` at `_manage_open_trade:558` |
| `STOP_LOSS_PCT` | `0.035` (-3.5%) | at `_manage_open_trade:525` |
| `TRAIL_TRIGGER_PCT` | `0.020` (+2.0%) | at `_manage_open_trade:535` |
| `TRAIL_LOCK_PCT` | `0.015` (+1.5%) | at `_manage_open_trade:537` |

Note divergences from `backtest_engine` (`target 5.0 / trailing 2.5→1.5`) — see §13.

### 8.2 Initialization (`__init__:69`)

* `mode = "LIVE" if ENABLE_LIVE_BROKERAGE_EXECUTION else "PAPER"` at 82 — `ENABLE_LIVE_BROKERAGE_EXECUTION` from `common/config.py:29` (`True` default, `True` on VPS per `ecosystem.config.js`).
* `CapitalEngine(mode, broker, DATA_DIR)` at 83.
* `order_lifecycle` at 85-95: `ShoonyaOrderLifecycle` if LIVE else `PaperOrderLifecycle`.
* State files at 110-114:

  ```python
  trades/trade_history.json
  trades/aegis_active_trade_context.json
  trades/aegis_state.json        # {date, trades_today}
  ```
  plus `capital_state.json` inside `CapitalEngine` (DATA_DIR root).
* Restores `aegis_active_trade_context` at 116, `trade_history` PnL at 117, `trades_today` at 118.
* `_option_cache` at 102 aliases `engine.option_cache` for spread/depth shortcuts — mutated by engine at `aegis_gamma_engine.py:217`.

### 8.3 Main Loop (`_run_loop:225`, daemon at `start:215`)

Polls at 2 Hz (0.5s `signal_queue.get` timeout at 238):

1. Daily reset if `today != last_trade_date` at 231.
2. Drain one signal at 238 → `_process_aegis_signal` at 239.
3. If open: `_manage_open_trade` at 245.
4. `engine.check_pending_edges(time.time(), _get_current_spot())` at 249.
5. If `09:08 ≤ now ≤ 09:15:30` at 253: `_handle_premarket_bridge` at 254.

### 8.4 Premarket Bridge (`_handle_premarket_bridge:260`)

Reads `read_premarket_state()` at 261 (written by ARGUS pre-market bridge — cross-engine coupling). If `indicative_spot>0` and differs from `engine.last_spot`, calls `engine._on_underlying_tick("INDEX", ind_spot, time.time())` at 272 to pre-align ATM before 09:15. If `09:15:00 ≤ now ≤ 09:15:20` and `trades_today==0` and `|gap_pct|≥0.25%` with `direction UP/DOWN` (276) and not already fired, synthesizes a `GAMMA_BURST_ENTRY` signal with `conviction 5.0` and `metrics {source:"09:08_PREMARKET_BRIDGE",gap_pct}` at 281-287. This is the sole path where `conviction 5.0` (<< threshold 75) bypasses the scorer — intentional gap-momentum seed.

### 8.5 Signal Consumption (`_process_aegis_signal:297`)

Guarded by `trade_lock` at 316 and `_is_entering` mutex:

```python
if trades_today ≥ MAX_TRADES_PER_DAY: return         # 312
if has_open_trade(): return ("trade already open")   # 317
if not can_enter(now): return ("outside window")     # 323
direction = signal["signal"]  # "UP"/"DOWN"          # 327
opt_lp = signal["lp"]; sym, metrics, conviction      # 328-331
params = get_instrument_params()                     # 333 ("NIFTY"/"SENSEX" + lot, premium bounds, exchange)
spot = signal["spot"] or engine.last_spot or fetcher.get_index_ltp()  # 340-342
allocations = StrikeSelector.select_strikes(signal_type="CALL" if UP else "PUT",
                index_price=spot, index_name, weekly_opts, option_cache,
                available_funds=capital_engine.get_available_funds(),
                lot_size, market_data_provider, premium_min, premium_max)  # 344-355
if not allocations: return                            # 357
(opt_row, quantity, ltp) = allocations[0]            # 361 (takes cheapest valid)
if symbol in strike_cooldowns and now < cooldown: return  # 365 (strike-level 3-min cooldown set at _chase:715)
live_ask, spread, sq1 = fetch via broker.market_data_provider.get_quote(exch_seg, token) # 369-389
  spread>2% → return; sq1<3 → return                 # 382-388
```

**Entry assembly (394-425):**

```python
context = TradeContext(signal_type="CALL"/"PUT", strategy="AEGIS_GAMMA_BURST",
                       index_entry=spot, decision_payload={conviction,direction,metrics,iv,signal_ts})
leg = TradeLeg(token, symbol, strike, signal_type, quantity, ltp)
order_lifecycle.execute_entry(leg, live_ask=live_ask or ltp)  # Shoonya LMT or Paper sweep
  if rejected: context=None; return
trade_context.add_leg(leg)                                       # 414
capital_engine.deduct_funds(entry_price*quantity*lot_size)      # 416
trades_today+=1; _save_trades_today()                            # 417
_place_initial_take_profit(leg, params)                          # 422
_save_active_context()                                           # 425
```

Order lifecycle detail is in `common/execution/order_lifecycle.py:120` (Shoonya) and `:30` (Paper) — see §8.7.

### 8.6 Take-Profit Seeding (`_place_initial_take_profit:432`)

```python
tp_price = _round_tick(entry * 1.05)  # OPTION_TICK_SIZE 0.05 at common/config.py:19
leg.tp_price = tp_price; leg.initial_tp_pct = 0.05; leg.highest_price = entry
if Paper: tp_order_id="PAPER_TP_<symbol>" (441)
else: Shoonya LMT S @ tp_price via _build_kwargs + broker.place_order → tp_order_id  (445-456)
```

`_cancel_tp:460` cancels live TP before any non-TP exit (called at 527,539,548, etc.).

### 8.7 Open-Trade Management (`_manage_open_trade:474`)

Runs every 0.5s while open. Snapshot pattern: copies `context` and `legs` under `trade_lock` at 479, then releases lock for I/O-bound quote fetches.

**Refresh (486-513):**

* Per leg where `status=="OPEN"`: prefers `option_cache[token]` (495-499) else `broker.get_quote(exch_seg, token)` (502-507). Updates `leg.current_price` and `live_bid/live_ask`. Updates `leg.highest_price = max(highest, current)` at 512.

**Exit priority (exact order, with line anchors):**

1. **Force flatten** at `must_force_flatten` (516) → `_close_all_legs("Force Flatten (≥ 15:10)")` and early return. Checked first — overrides all profit/loss.
2. **Hard SL -3.5%** at 523 — `_round_tick(entry*0.965)`, compare `effective_bid = live_bid or current >0` against `sl_price`. If hit: `_cancel_tp` then `_close_all_legs("Hard SL (-3.5% @ bid)")` (528).
3. **Trailing profit lock** at 535 — for each leg: `peak = (highest-entry)/entry`, `curr = (current-entry)/entry`. If `peak ≥2.0%` and `curr ≤1.5%`: `_cancel_tp` → `_close_all_legs("Trailing Profit Lock (+1.5% @ …)")` (540).
4. **Spot adverse movement** at 544 — `spot_sl_pts = 25 (NIFTY) else 50 (SENSEX)` at 545 (bug: `params.get("instrument")` is never set; falls back to `"NIFTY"` — see §17). If `CALL` and `spot ≤ entry-25` (or `PUT` and `spot ≥ entry+25`): close with `Spot Adverse Breakdown/Rally SL` (548-554).
5. **Take-profit +5%** at 558 — `tp_price = leg.tp_price or entry*1.05`, if `effective_exit_bid ≥ tp_price`: close with `Take-Profit Hit (+5% @ tp)` (563).
6. **5-minute hard time stop** at 569 — `hold_sec = now - entry_time`, if `≥300`: close with `5-Min Hard Time Stop (Ns)` (571).

> No dynamic IV crush, no adverse-selection, no Kyle lambda checks here — exit is purely price/time/spot-anchored and deterministic.

### 8.8 Close Lifecycle (`_close_all_legs:573` → `_chase_exit_thread:581`)

`_close_all_legs` at 573 sets `context.status="EXIT_PENDING"` under lock (577) and spawns `_chase_exit_thread` daemon (579).

**`_chase_exit_thread:581`** — 3-step aggressive limit chase (Shoonya path):

```
for leg where status=="OPEN" and quantity>0:
  anchor leg.current_price to live bid at 602-606
  qty = quantity*lot_size (610)
  Step1: price_1 = _round_tick(bid*0.99)                                    # 616
    if tp_order_id is live → modify_order(…price_1)
    else → _build_kwargs("S", …price_1) + place_order
    sleep 0.8; single_order_history; if FILLED/COMPLETE → filled=True, fill_price=avgprc # 634-641
  Step2 (if not filled): price_2 = _round_tick(bid*0.98) → modify → sleep 1.0 → check # 644-655
  Step3 (if still open): cancel prior (659) → price_3 = _round_tick(bid*0.97) → place → sleep 0.8 → check # 660-678
       if not filled at step3 → error logs (678,680)
  if not filled after 3 attempts: order_lifecycle.execute_exit(leg, reason) fallback at 684  # Paper sweep or engine's chase
  fill_price = avgprc or price_N or leg.current_price; fill_ts=time.time()  # 685-686
  # CRITICAL fill→engine feedback:
  engine.on_fill(fill_price, fill_ts, signal_price=entry_price, signal_ts=decision_payload.signal_ts or entry_time) # 689-702
  # Bookkeeping under lock:
  leg.status="CLOSED"; exit_time=now; exit_price=fill_price; pnl=(fill-entry)*qty*lot  # 708-712
  capital_engine.add_funds(fill*qty*lot)                           # 714
  strike_cooldowns[symbol]=now+3min                                # 715
for all legs: context.status="CLOSED"; exit_time=now; _append_history; chop_gate.register_trade_close # 718-725
trade_context=None; tp_order_id=None; _save_active_context       # 728-730
```

**OrderLifecycle contracts (`common/execution/order_lifecycle.py`):**

* `PaperOrderLifecycle.execute_entry/exit` (30-104) delegates to `RealisticPaperBrokerAdapter` (`execute_marketable_entry/exit`) with `simulate_book_sweep` over `ask_prices/quantities` or `bid…`, computes `calculate_nse_option_charges`, and sets `leg.entry_price = res["entry_price"]` / `leg.close_leg(audit["exit_price"], reason)` with `realized_pnl = net_pnl` (103).
* `ShoonyaOrderLifecycle.execute_entry` (171) builds kwargs via `_build_kwargs(…)` (127) using `inspect.signature(api.place_order)` to adapt to the installed SDK variant (NorenApiPy), places `LMT` with `get_marketable_entry_limit_price = round(best_ask + max(1, 1.5%))` at 106, then polls `single_order_history` 3×1s; if not filled, chases with `modify_order @ 1.01×limit` at 212, else cancels at 224.
* `get_marketable_entry/exit_limit_price` at 106/113 mirror the engine's own chase arithmetic — both use `OPTION_TICK_SIZE 0.05` and 1.5% buffers.

### 8.9 Persistence (lines 769-836)

* `_append_history:732` appends to `trade_history.json` (739) with fields `{date, entry_time, exit_time, strategy:"AEGIS_GAMMA_BURST", signal/signal_type, symbol, entry_price, exit_price, pnl/net_pl, daily_total_pnl, exit_reason/reason, decision_payload}`. `daily_pnl` is accumulated at 745 and persisted nowhere else — recomputed from history on restart at `_load_daily_pnl:158` by date-prefix match `"%d %b %Y"` (167).
* `_save_active_context:770` serializes `TradeContext` + legs to `aegis_active_trade_context.json` (780) when open, else deletes the file (772-774). `tp_order_id` persisted at 782. Legacy read at `_load_active_context:805` restores `status/legs/entry_time/tp` and sets `trade_context.status="OPEN"` (832).
* `_load/save_trades_today:125-156` uses `trades/aegis_state.json` `{date, trades_today}`.

---

## 9. Data & Persistence

### 9.1 Parquet Schema — `l2_options_archiver.py:50`

| Column | Dtype | Meaning |
|---|---|---|
| `ts` | int64 (ms) | `now.timestamp()*1000` at `l2_options_archiver.py:235` |
| `tok` | int64 | `int(token)` (options tokens exceed int32) |
| `sym` | object (string) | `token_to_symbol[token] or NIFTY/SENSEX` at 226 |
| `kind` | int8 | `0=index, 1=CE, 2=PE, 3=FUT` at 243-253 |
| `strike` | float32 | `meta["strike"]` (0 for index/fut) |
| `lp` | float32 | `msg.last_traded_price / lp` |
| `vol` | float32 | `msg.volume_trade_for_the_day / v` |
| `oi` | float32 | `msg.oi` |
| `bp1..bp5` | float32 | |
| `bq1..bq5` | int32 | |
| `sp1..sp5` | float32 | |
| `sq1..sq5` | int32 | |

**Partitioning:** `RAW_DIR/sub` at `l2_options_archiver.py:122` — weekday `2,3 (Wed,Thu) → Sensex else Nifty` (127). Files `aegis_l2_raw_YYYY-MM-DD_part_NNN.parquet` (158, Snappy at 178). Flush trigger at `_flush_buffer:162` when `age ≥ FLUSH_INTERVAL_SEC (900 = 15 min)` or `len≥50000`. Auction sidecar at `_flush_auction:202` writes `preopen/closeauction` JSONL under `LOGS` every 30s at 293-294 when `is_underlying` and `09:00-09:15` or `15:30-15:40` (257-259).

**Retention:** `_prune_old_files:137` deletes partitions older than `RETENTION_DAYS=30` (cutoff vs filename date). Checked at archiver init (106) and on date rollover (197).

### 9.2 Trade Stores

| File | Writer | Reader | Format |
|---|---|---|---|
| `AEGIS/data/trades/trade_history.json` | `aegis_execution_manager._append_history:764` | dashboard, `_load_daily_pnl:162` | JSON array append (full rewrite) |
| `AEGIS/data/trades/aegis_active_trade_context.json` | `_save_active_context:799` | `_load_active_context:810` | JSON object, deleted when flat |
| `AEGIS/data/trades/aegis_state.json` | `_save_trades_today:149` | `_load_trades_today:131` | `{date, trades_today}` |
| `AEGIS/data/capital_state.json` | `common/execution/capital_engine:46` | same | `{available_funds, locked_withdrawal_amount}` |
| `AEGIS/logs/telemetry/aegis_5min_outcomes_*.jsonl` | `outcome_telemetry._persist_record:167` | offline analysis | JSONL per day |
| `AEGIS/data/l2_raw/{Nifty,Sensex}/` | `l2_options_archiver._flush_buffer:172` | `backtest_engine`, `research_analytics` | Snappy parquet partitions |
| `AEGIS/data/scratch/underlying_candles_{NIFTY,SENSEX}.parquet` | `data_fetcher.bootstrap_candle_window:64` | same at 39 | 512-candle ring |

### 9.3 IPC State File

`common/ipc/live_state.py:32` — `AEGIS_STATE_FILE = /dev/shm/aegis_live_state.json` (or `data/shm/` fallback at 21). Written atomically via `_atomic_write:108` (tmp + replace). Keys at `_DEFAULT_AEGIS:70` and actual payload at `aegis_main.py:313` (see §5.3).

---

## 10. Config & Calibration

### 10.1 Settings Binding — `AEGIS/config/settings.py:1`

```python
BASE_DIR = get_project_root()                       # 6
RUNTIME_PATHS = configure_runtime("aegis")          # 11 → AEGIS/data, AEGIS/logs (paths.py:11)
# Re-exports from common/config (16): NIFTY/SENSEX premium/lot, tick, capital, live flag, OI reversal, etc.
```

**Inherited from `common/config.py:1` (effective defaults):**

| Constant | Value | Notes |
|---|---|---|
| `NIFTY_LOT_SIZE` | 65 | SEBI 2026-07 |
| `SENSEX_LOT_SIZE` | 20 |
| `NIFTY_PREMIUM_MIN/MAX` | 15 / 120 |
| `SENSEX_PREMIUM_MIN/MAX` | 20 / 200 |
| `OPTION_TICK_SIZE` | 0.05 |
| `INITIAL_PAPER_CAPITAL` | 2500.0 |
| `ENABLE_LIVE_BROKERAGE_EXECUTION` | `True` (env) |
| `ENTRY_LIMIT_BUFFER_PCT` | 0.025 |
| `STRIKE_DB_PATH` | `DATA_DIR/strike_research.db` (sidestepped by AEGIS — uses `instruments.db` via repository) |

### 10.2 Microstructure Weights — `AEGIS/data/params/microstructure_weights.json:1`

```json
{
  "model_name": "AEGIS_Empirical_Microstructure_Scorer_v2_5Min",
  "calibrated_on": "NIFTY_SENSEX_L2_2026_AUG_PURGED_5MIN",
  "n_samples": 96098,
  "roc_auc_oos": 0.5041,
  "raw_intercept": -0.413283,
  "feature_weights": {
    "target_tick_intensity": 0.503295,
    "target_pre_ret": -2.25263,
    "target_ask_vacuum_vel": -1.6e-05,
    "target_bid_build_vel": -3e-06,
    "target_ask_depth_decay": 1e-06,
    "target_l1_ofi": 9.3e-05,
    "target_top5_imbalance": -0.004343,
    "target_spread_compression": -0.031951,
    "touch_pressure": 0.012609,
    "deep_pressure": 0.113511,
    "layer_divergence": -0.005502,
    "book_slope": -0.316123,
    "otm1_ret": 1.591908,
    "otm2_ret": -0.27395,
    "cascade_coherence": 0.992641,
    "opp_side_ret": 0.89333
  },
  "high_conviction_threshold": 75.0,
  "moderate_conviction_threshold": 60.0,
  "min_tick_rate_gate": 1.5,
  "horizon_target": "5_MIN_PROD_EXITS_WITH_STATUTORY_FRICTION"
}
```

**Calibration verdict (from §1 of Fundamental Agent Constitution):**

* `n_samples=2436` pooled events → **Hundreds regime (100-1000 per index)** — *cautious trust in tabular rules only if surviving event-level CV, permutation, and bootstrap* — not yet demonstrated in the live scorer (the harness conducts offline precursor audits in `research_feature_backtest.py:628` with 70/30 time-series AUC/KS, but the live 14-weight model is not re-validated per deploy).
* `roc_auc 0.8264` is **in-sample optimistic** unless pinned to OOS window — treat as calibration fit, not live edge. The production edge must be measured by `OutcomeTelemetry` forward MFE and by `backtest_engine` net PnL after `Rs 16.50/trade` (see §13).
* **TRUST / DO-NOT-TRUST-YET:** **`DO-NOT-TRUST-YET` for any specific coefficient** — weights like `target_tick_intensity 4.53` or `otm1_ret 0.59` are calibrated thresholds worth *monitoring forward*, not fixed laws, until 3+ months OOS telemetry confirm monotonic `EQS vs realized return` and `spot adverse gate` precision.

### 10.3 Other Param Artifacts in `data/params/` and `data/ml_results/`

| File | What it is | Touches live path? |
|---|---|---|
| `aegis_l2_brain_v1.json` | L2 decision rules snapshot | No (research) |
| `aegis_l2_decision_rules.json` | Same lineage | No |
| `sensex_l2_brain_v1.json` | Sensex-specific brain | No |
| `aegis_calibrated_params.json` | New-style calibrated params (recent window 30d) | Only if hot-reloaded via `reload_params` (no endpoint exposed) |
| `ml_results/gamma_blast_evaluation.json` + `detailed_otm_metrics.json` + `deep_gamma_blast_net.pt` | Historical V2 gamma-blast ML lineage | No (kept for forensics) |

---

## 11. L2 Archiver — `l2_options_archiver.py`

**Class:** `AegisL2Archiver(token_to_symbol, token_map)` at `l2_options_archiver.py:73`.

**Listener shape:** Separate `on_tick(msg: dict)` (219) consuming the **raw dict** broadcast (not `ParsedTick`) so it cannot perturb the zero-copy contract (comment at 76). Resolves `kind` by `token_meta` at 240-254, injects `strike` and `oi`, then builds one `row` per tick (273-289) unrolled over 5 levels (284-288) before appending to `buffer: list[dict]` at 290.

**Attachment (`attach_aegis_archiver:308`):** Probes `broker` and 7 fallback attributes for `add_tick_listener` (314-319), then `broker.add_tick_listener(arch.on_tick)` at 322. Returns the archiver (handle for `update_token_maps` and `shutdown`). Warm path updates at `aegis_main.py:306` on every new subscription batch.

**Invariants preserved (per AGENTS.md L2 archiver discipline):**

* No heavy math — pure passthrough at `on_tick:216`.
* Heavy libs (`pandas`) touched only at flush time (15-min, not per tick).
* `buffer` cleared after `to_parquet(..., compression="snappy")` at 185; no in-memory accumulation beyond `50000` rows or 15 min.

---

## 12. Research & Analytics

### 12.1 `research_analytics.py:1` — Plotly Forensics

Off-hot-path; produces dashboard research tab charts **from parquet partitions**. Readers:

* Single date → direct partition load via `_load_aegis_partitions(target_date)` at 379.
* Date range → pre-aggregated daily snapshots via `load_daily_snapshots("aegis", start, end)` at 376 (`common/analytics_persistence`).

**BSM IV (`_bsm_iv:41`)** — 50-iter Newton-Raphson with vega guard `1e-8` at 58, clamped `[0.01, 5.0]`. Research-only; not in live engine (live IV is still `last_atm_iv=0.15` stub at `aegis_gamma_engine.py:125`).

**Chart builders (Plotly dict protocol, dark `#0d1117` background):**

| Builder | Viz | Key math | Line |
|---|---|---|---|
| `create_strike_heatmap` | CE (Greens) / PE (Reds) heatmaps: strike × 5-min time_bin volume | `dvol = vol.diff().clip≥0` per strike×kind, pivot mean | 81 |
| `create_iv_crush` | Session ATM IV trajectory (CE+PE) | Interpolates spot per tick, `T=_days_to_expiry`, BSM per row | 146 |
| `create_oi_buildup` | Per-strike OI Δ (groupby last-first) | `oi_close - oi_open` | 226 |
| `create_premium_decay` | Per-strike premium Δ% (theta bleed proxy) | `(close-open)/open*100` | 274 |
| `create_oib_distribution` | CE/PE OIB histogram | `Σ bp*bq - Σ sp*sq / Σ(bp*bq+sp*sq)` | 321 |

Entry `run_research(target_date, start_date, end_date)` at 368 orchestrates; CLI at `main:415` (`--date/--start/--end`).

### 12.2 `research_feature_backtest.py:1` — Candidate Precursor Harness

**Standalone** — never imports live AEGIS. 827 lines, standalone `CFG` dataclass at 46.

**Hypotheses (see module doc 7-12):**

* A. `futures lead-lag` — direction-adjusted futures returns at 100/250/500/1000 ms (`feat_futures_lead_lag:404`) over `pre_window 2s`. Futures data absent in this repo (`futures_glob=""` at 50), so this feature is all-zero by construction (warned at 797).
* B. `event-stream intensity` — `tick_rate, rate_accel, fano, excit, hawkes_n (optional MLE)` at `feat_event_intensity:458`. Hawkes branching via `_hawkes_branching_mle:436` (SciPy `minimize`, β=10, slow path disabled by `use_hawkes_mle=False` at 78).
* C. `deep-book queue depletion` — direction-adjusted `deplete_vel, refill_ratio, deep_decay, imbalance` at `feat_queue_depletion:524` (uses `c_sq_deep = [sq3,sq4,sq5]` vs `c_bq_deep`).

**Labeling:** Auto-extracts ≥10% clean impulses (45s horizon, ≤3% interim drawdown, ≥3 ticks pre-window) at `_auto_extract_spikes:187`, bucketed in IST 09:15-15:30 at 199, stepping `1s` (210). Deduplicates `300s` per symbol at 252. Falls back to supplied `spike_csv` if configured.

**Controls:** TOD-matched same-day jitter ±15 min at 337, fallback to other-day same strike/side at 359-384, `control_per_case=4` (62), `min_gap_sec=300` (65).

**Evaluation:** Time-series 70/30 split, `auc_mannwhitney:628` (handle NaNs → 0.5), `ks_stat:641`, `combined_logistic:674` (class-balanced, standardized), `walk_forward:710` (threshold at 0.90-0.995 quantiles, lift vs base). Output row "`auc_oos ≥0.62 & ks≥0.2 = informative`" at 801. Cache at `feature_matrix.parquet:782` next to the script.

### 12.3 `core/causal_microstructure_pipeline.py:1` and `research/lob_quantum_lab.py:1`

See §7.9 and artifact header: LOB-MAE lab is Colab-first (T4/A100), event-clocked `W=64, S=8, max_wall 45s`, 10 levels × 3 channels (bps, log1p qty z, queue delta clipped 5σ), train-day normalization (`N_TRAIN_DAYS=4`), day_id via IST offset, artifacts `X_windows.npy / META_windows.parquet / NORM_STATS.json`. Off-hot-path; no live wiring.

---

## 13. Parity Backtester — `backtest_engine.py`

**Classes:** `ParquetTickAdapter:38` (scalar adapter matching `ParsedTick` shape), `ExecutedTrade:58`, `SimulatedPositionManager:84`, `AegisParquetBacktester:249` at `backtest_engine.py:1`.

**`SimulatedPositionManager` rules (authoritative replay approximations):**

| Rule | Value | Source |
|---|---|---|
| Entry cutoff | >15:00 blocks, `max_trades 8` default | 130-136 |
| Single position mutex | active blocks new signals | 138 |
| Cooldown | 60s post-close | 142/120 |
| Premium filter | `20 ≤ ltp ≤ 300` else skip | 155 |
| Target | +5.0% | 99/192 |
| Trailing | trigger 2.5% → lock 1.5% (live is 2.0→1.5 at §8.1) | 101-102/196-199 |
| Stop | -3.5% | 100/202 |
| Time stop | 300s | 105/207 |
| Charges | Rs 16.50/trade deducted from `gross_pnl` at 225-228 | 98/226 |
| Capital | `start 2000` (live paper is 2500 via `common/config.py:22`, or broker funds in LIVE) | 96/81 |

**`AegisParquetBacktester:249`** drives **the production `AegisGammaEngine` itself**:

* `discover_files:290` — glob `aegis_l2_raw_*.parquet` (polars).
* `build_dynamic_universe:300` — scans `tok/sym/kind/strike`, maps `kind 1→CE/2→PE` else heuristic, `is_index` via `kind 0` or known tokens.
* `initialize_engine:334` — `create_aegis_engine(…, underlying_tokens)`, overrides `high_conviction, min_tick_rate_gate, eqs_dispatch_threshold`, sets `outcome_telemetry.log_dir`, builds `SimulatedPositionManager`.
* `run:366` — chronological row replay: builds `ParquetTickAdapter(tok, lp, vol, ts, bp1..5…)` per parquet row (401-418), calls `engine.on_tick_parsed(tick)` at 419, drains `signal_queue` → `position_manager.on_signal(ts)` at 422-425, then `position_manager.on_tick(sym, lp, ts)` at 429.
* `force_close_at_eod:240` after all files, then aggregate `total_ticks/files_count/signal_count/trade_count/elapsed/throughput/last_spot/current_atm/final_capital`. Throughput reported at 476 `ticks/sec`.
* CLI `main:503` — `--data-dir Nifty --date 2026-08-28 --capital 2000 --charges 16.50 --target 5.0 --stop-loss 3.5 --threshold 75 --eqs-threshold 70 --min-tick-rate 1.0`.

**Fidelity notes:** Backtest uses `on_tick_parsed` directly (same codepath as live), single-position mutex, and charge deduction. It **differs** from live in trailing trigger (2.5 vs 2.0), premium filter (20-300 vs live 5-400 via `option_quality`), spot adverse gate (not modeled), and fill simulation (instant vs 3-step Shoonya chase) — document these when quoting backtest PnL.

---

## 14. Hot-Path Discipline & Invariants

### 14.1 Contract (from `AGENTS.md` + harness I1-I8)

| # | Invariant | Scope | How the code enforces it | Gap |
|---|---|---|---|---|
| I1 | Zero allocation on hot path | `on_tick_parsed` | `deque(maxlen)`, pooled `ParsedTick`, scalar `math`, `np.zeros` only at init (not per tick) | **Partial** — `deque.append` does allocate node objects; `dict` lookups and `while ring[0]<…: popleft()` touch Python objects. No `tracemalloc=0` test exists in `tests/` (see §17). |
| I2 | O(1) per tick | `on_tick_parsed` | No `for strike in range(N_STRIKES)` (21) loops; cross-strike `for k in (1..4)` is constant-4 at line 274 (off-hover but inside hot path on every option tick — bounded, not scaled) | OK — 4-iter fanout is O(1) w.r.t. universe size |
| I3 | Hard gates are necessary | Phase-1 of old V2 | V3 replaces G1..G9 with the 4-domain pipeline (permission→persistence→quality→FSM→EQS). The "hardness" now lives at `option_quality`, `signal_state_machine` prime 78, tick-rate, and EQS 70 | OK, but the label "G1..G9" in the old doc is obsolete — do not quote it |
| I4 | No magic numbers | Entire codebase | Every constant is either in `microstructure_weights.json` or a typed default in the owning class (`time_bar_engine:38`, `directional_persistence:42`, `regime_chop_gate:22`, `signal_state_machine:53`, `option_quality:22`, `entry_quality_engine:35`) | **Gap** — `AEGIS/config/settings.py:40` hardcodes `TIME_FRAME="5m"` and Kronos constants (see §17) outside any registry |
| I5 | Fill feedback closed | `ExecutionManager._chase_exit_thread → Engine.on_fill → check_pending_edges` | Every live close at `_chase_exit_thread:689` calls `engine.on_fill(…)` with `signal_ts` from `decision_payload.signal_ts` or `entry_time` (691-694), and `_run_loop:249` polls `check_pending_edges` every 0.5s | OK |
| I6 | IV solver robustness | old `_impvol_newton` | **Retired** — live IV is stub `last_atm_iv=0.15`; research BSM at `research_analytics.py:58` is not on the hot path. No live solver to guard. | Obsolete — remove from CI contract |
| I7 | NGAO dual guard | old `_ngao_acceleration` | **Retired** (gamma/Hawkes removed with scorer migration) | Obsolete |
| I8 | G6 unvalidated queue assumption | old `_adaptive_microprice_align` | Retired; option quality spread/depth is the live analogue, with explicit `max_spread 2.5%` | Obsolete |

**Allowed in hot path (per harness §4, still honored):** fixed-index NumPy writes (`fill_latency_buf[idx]`), scalar `math`, `deque.append` with `maxlen`, scalar `float/int` casts, `hasattr/getattr`, single `signal_queue.put_nowait`.

**Forbidden (still forbidden):** `pandas/scipy/sklearn` imports, `dict/list` creation in per-tick loops (but `otm_rets={}` at 273 is a per-tick dict alloc — see §17), string formatting/f-strings except at log gates (gated behind `score≥threshold` at 303-363, so not hot), `np.roll`/fancy indexing, loops over `N_STRIKES`.

### 14.2 Pattern Template for New Metrics (unchanged from harness §4)

```python
# 1. Pre-allocate in AegisGammaEngine.__init__:
self.new_metric_tensor = np.zeros((N_TYPES, N_STRIKES), dtype=np.float32)
self.new_metric_history = np.full(HIST, np.nan, dtype=np.float32)

# 2. In on_tick_parsed (HOT) — sufficient statistic only:
self.new_metric_tensor[side, idx] += inc

# 3. In a cold path (e.g. _on_underlying_tick or a new _process_bar):
val = self.new_metric_tensor.sum()
self._push(self.new_metric_history, val)
self.new_metric_tensor.fill(0)
```

---

## 15. Threading & Concurrency

* **WS thread owns `on_tick_parsed`** — the TickCompiler's `on_message:199` invokes `engine.on_tick_parsed(tick)` at line 220 **inline** on the WS dispatch thread. Any blocking in `on_tick_parsed` blocks all ticks. Hence the discipline in §14.
* **Main thread never blocks tick processing** — even the 1-min boundary's `get_quote`/`searchscrip` at `data_fetcher.py:164` runs on main, not WS. The underscore comment at `aegis_main.py:238` ("yield to tick processing threads") is literal: `sleep(0.2)`.
* **Execution thread (`_run_loop`) shares `engine` unsafely** — `engine.on_tick_parsed` mutates `tick_ring, scorer.history, spot_history, ema, …` while `_run_loop:249` reads `engine.last_spot` and `engine.chop_gate` and `_manage_open_trade` reads `engine.option_cache` (shared at `aegis_execution_manager.py:102` via alias, not copy). No `threading.Lock` protects the engine; relies on GIL and the fact that the execution thread reads atomically. `TradeContext` mutation **is** locked via `trade_lock` at `aegis_execution_manager.py:104`, used at 316, 394, 479, 574, 589, 708, 718.
* **`_chase_exit_thread`** outlives the execution loop — daemon, captures `legs` snapshot at 587-588 under lock then operates on it lock-free. Relies on `trade_context.status="EXIT_PENDING"` set at 577 to block re-entry via `has_open_trade:189`.

---

## 16. Error Handling & Edge Cases

| Scenario | Where | Handling |
|---|---|---|
| Invalid tick (missing sym, `lp≤0`) | `aegis_gamma_engine.py:202,208` | Early `return`, never throws |
| Unknown token (no `token_map`) | `aegis_gamma_engine.py:221` | `return` |
| `token_to_symbol` vs `TickCompiler._t2s` drift after expiry roll | `aegis_main.py:306` + `aegis_gamma_engine.refresh_token_maps:520` | Main thread rebuilds maps; `_rebuild_strike_to_symbol` re-runs on ATM shift. |
| Stale spot | `causal_microstructure_pipeline.ProductionShadowMicrostructureScorer.is_spot_valid:338` | `now - last_spot_ts >3s` → reject; `AegisGammaEngine` itself does not gate on spot age |
| Deposit exhaustion / single-order history unavailable | `aegis_execution_manager.py:633-680` + `order_lifecycle.py:197` | Falls back to `execute_exit` sweeper; logs at `error` but does not crash |
| Broker not resolved for archiver | `l2_options_archiver.py:322` | Falls back to returning `arch` detached (no listener) with warning |
| WS drop on weekend/holiday | `ARGUS/tick_compiler.py:312` (MN-4) | `on_ws_close` checks `is_trading_day/is_bot_active_hours` then either skips or exponential backoff 1→30s reconnect |
| `signal_queue` full | `aegis_gamma_engine.py:533` | `put_nowait` with explicit `except queue.Full: logger.warning` |
| Parquet corruption | `l2_options_archiver.py:200` + `backtest_engine.py:118` | Caught, `warnings.warn` or `logger.debug`, shard skipped |

---

## 17. Audit Findings & Resolution Status

Findings are **ranked** — P0 blocks correctness claims, P1 degrades fidelity, P2 is hygiene.

### P0 — Correctness & Calibration
1. **[RESOLVED 2026-08-30] Scorer OOS Recalibration & 5-Min Alignment**:
   * Resolved via `AEGIS/research/aegis_5min_consistency_lab.py` — evaluated 96,098 candidates across 5 days (NIFTY + SENSEX) on the exact 15 production features matching `MicrostructureScorer` and forward 5-min production exits (+5% TP, -3.5% SL, statutory friction).
   * Proved mathematically that Macro / Underlying Index Momentum dominates 5-minute option continuation by 15x–25x over fleeting micro spikes (`min_from_open`, `spot_range_pos`, `spot_ret_300_signed`, `spot_ret_180_signed` take top ranks).
   * Calibrated new drop-in weights to `AEGIS/data/params/microstructure_weights_v2_5min.json` and updated `microstructure_weights.json`.

2. **[RESOLVED 2026-08-29] Kronos residue purged**:
   * Removed stale `KRONOS_*` and `TIME_FRAME` from `AEGIS/config/settings.py`.
   * Purged `torch, einops, huggingface_hub, safetensors` from `AEGIS/requirements.txt`.

### P1 — Fidelity / Correctness

11. **`tick_ring` hot-path alloc.** `on_tick_parsed:229` does `if sym not in tick_ring: tick_ring[sym]=deque(maxlen=100)` — a dict insert under tick pressure for new symbols. Pre-size the ring for all `token_to_symbol` values at `__init__` to honor I1.

12. **Cross-engine coupling.** `aegis_main.py:29` imports `ARGUS.tick_compiler.TickCompiler`. AEGIS cannot run without ARGUS checked out. The proposed `common/market/tick_types.py` + shared compiler extraction (in `Migration to Aegis.md:89`) was never executed — track as architecture debt.

13. **Outcome telemetry retention unbounded.** `outcome_telemetry.py:167` appends forever to `aegis_5min_outcomes_*.jsonl` with no rotation; `l2_options_archiver.py:42` prunes parquet at 30d but JSONL is never pruned. Add a time- or size-based rotation.

14. **`SYSTEM_ARCHITECTURE.md:26` PM2 table lists `l2-archiver` as an independent app** — no longer true for the embedded AEGIS archiver. `ecosystem.config.js:1` has no `l2-archiver` entry. Patch the system doc on next release.

---

## 18. Testing & Verification Checklist

Per `AGENTS.md` pre-push discipline — "suite must be 100% green" — with current gaps called out:

| Level | Check | Command (actual, not aspirational) | Current status |
|---|---|---|---|
| L0 | Zero-alloc hot path | No `pytest tests/test_zero_alloc.py` fixture exists for AEGIS — `tests/` only covers `common/` shared branches (see `AGENTS.md` kronos exception note) | **Missing** — add `tests/test_aegis_hot_path.py::test_on_tick_parsed_zero_alloc` asserting `tracemalloc diff ==0 over 10k synthetic ticks` |
| L0 | Magic-number scan | `python scripts/scan_magic_numbers.py AEGIS/` | **Missing script** — create AST regex excluding `*_params.json` and class-default blocks |
| L0 | Signature drift | `python scripts/check_signatures.py` verifying `engine.on_fill` ↔ `execution_manager._chase_exit_thread:689` | **Missing** — engine signature is `(fill_ts, fill_price, signal_ts, signal_price, sym=None)` vs caller positional order is `(fill_ts, fill_price, signal_ts, signal_price, sym=…)` — currently compatible but invent a positional mismatch and it will throw `TypeError` |
| L1 | Microstructure logit math | Unit: `score=100/(1+exp(-z))` with `z` clamped ±40, `len(buf)<2 → 0.0`, spread NaN path | **No deterministic fixture** — add against `microstructure_weights.json` ground truth |
| L1 | TimeBar EMA/ATR | `atr_ratio = current/baseline`, `slope = ema - ema_prev` after `bars_1m≥2` vs warmup `permissive` | **No fixture** |
| L1 | FSM transitions | `OBSERVE→PRIMED(≥78)→CONFIRMED→FIRED(≤3s)→COOLDOWN(120s)` + `prime_timeout 60s` + `stale 8s` | **No fixture** |
| L2 | Adversarial injection | Spoof L1 size → `OptionQuality` veto; IV crush step → not live-gated; latency attack 5s → `C_eff` decay (stub) | **No harness** — port `research_feature_backtest` walk-forward semantics |
| L3 | Walk-forward net PnL | `python -m AEGIS.backtest_engine --date 2026-08-28 --capital 2000 --charges 16.50` with `throughput_tps` at `backtest_engine.py:476` | **Available** — run on last 3 expiry weeks and compare `net_pnl` within 5% of live `trade_history.json` before any release |

Release checklist (adapted from harness §11, corrected for V3):

- [ ] `python -m pytest tests/ -q` — 100% green (including new AEGIS hot-path + math fixtures).
- [ ] `backtest_engine` on last 3 Wed/Thu (SENSEX) + Mon/Tue/Fri (NIFTY) sessions reproduces live `trade_history.json` net PnL within 5%.
- [ ] `research_feature_backtest.py` univariate `auc_oos` for the top-3 live features ≥0.62 and stable train/OOS (not quoted as edge, just as pass gate).
- [ ] `microstructure_weights.json` SHA256 recorded in `RELEASE_MANIFEST.json`.
- [ ] `tracemalloc` over 1M synthetic ticks: `Δ<10 KB`, no `list/dict` growth in hot path.
- [ ] `AegisL2Archiver` partition `RAW_DIR/{Nifty,Sensex}` non-empty for the session, `auction JSONL` emitted for 09:00-09:15.
- [ ] `SYSTEM_ARCHITECTURE.md §2.2` updated to V3 narrative (see patch below).

---

## 19. Deployment & Operations

**VPS targets:** `137.23.41.38` (`Oracle Cloud, Ubuntu, user opc`) — `SYSTEM_ARCHITECTURE.md:129`. Key at `Oracle SSH keys/ssh-key-2026-07-11.key` (root only, never under `AEGIS/` per `AGENTS.md`).

**Deploy rule (from `AGENTS.md` and harness §4):** Only after `15:30 IST` or before `09:15 IST`. Restart `l2-archiver` first if it still exists as a standalone, otherwise bounce `aegis-bot` directly:

```bash
ssh -i "Oracle SSH keys/ssh-key-2026-07-11.key" opc@137.23.41.38
pm2 restart aegis-bot --update-env
pm2 logs aegis-bot --lines 80 --timestamp
# Verify: tail -f ~/AEGIS/logs/app.log | grep -E "AEGIS V3 FIRED|OCCAM VETO|PERMISSION VETO|OPTION QUALITY VETO|Signal queue"
```

**Live introspection (no admin socket yet):**

```python
# Via SSH python REPL (attach to running process not supported — use log tail + file reads):
# 1. Engine snapshot:
#   python -c "from common.ipc.live_state import get_aegis_live_state; print(get_aegis_live_state())"
# 2. Bucket diagnostics (archiver):
#   ls -lh ~/AEGIS/data/l2_raw/Nifty/  ~/AEGIS/data/l2_raw/Sensex/
#   parquet-tools show ~/AEGIS/data/l2_raw/Nifty/aegis_l2_raw_$(date +%F)_part_*.parquet | head
# 3. Trade ledger:
#   cat ~/AEGIS/data/trades/trade_history.json | python -m json.tool | tail -n 40
# 4. Telemetry tails:
#   tail -n 20 ~/AEGIS/logs/telemetry/aegis_5min_outcomes_$(date +%F).jsonl | python -m json.tool
```

**Emergency procedures (ported from harness §10 with V3 corrections):**

| Scenario | Action | Effect |
|---|---|---|
| Runaway / toxic fill | `touch /tmp/AEGIS_KILL_SWITCH` then `pm2 restart aegis-bot` | Main loop checks `is_kill_switch_active` via `common/ipc/live_state.py:177` inside `can_enter_new_trades:65` → blocks `can_enter` / `is_trading_day` path; `pm2 restart` guarantees flat (exit thread still drains) |
| Param corruption | `python -c "from AEGIS.aegis_gamma_engine import AegisGammaEngine; e=AegisGammaEngine(...); e.reload_params()"` | Hot-reloads `microstructure_weights.json` (no HTTP endpoint — see §17 P2.13 for HTTP admin proposal) |
| WS stalled | `pm2 restart aegis-bot` (reconnect is handled in TickCompiler MN-4, not by manual `mdp._ensure_ws_started`) | `TickCompiler.on_ws_close:310` already backoffs and re-subscribes; hard restart is the escape hatch if the close listener was never wired |
| IV / microprice anomaly | Delete `AEGIS/logs/telemetry/aegis_5min_outcomes_*.jsonl` for the day and restart | No IV cache to clear; scorer state is per-symbol deque and will rebuild within 30s |

---

## 20. Change Log vs Prior Doc

### V3.2 (2026-08-30) — Macro-Alignment & 5-Minute Consistency Calibration
* **Macro-Alignment Gate (`aegis_gamma_engine.py:316-330`)**: Zero-copy cash index momentum agreement check. Blocks signals if 60s or 180s spot momentum opposes the trade.
* **5-Minute Consistency Retraining Lab (`AEGIS/research/aegis_5min_consistency_lab.py`)**: Standalone cross-validation and feature gain ranking harness covering 96,098 candidate trade decisions across 5 days (NIFTY + SENSEX) under realistic SEBI statutory friction (`calculate_nse_option_charges`).
* **Put-Call Parity Synthetic Spot Engine**: Synthesizes continuous millisecond-precise spot index streams for days where explicit spot ticks are missing in L2 archives ($S_t = K + C_t - P_t$).
* **5-Min Calibrated Weights (`microstructure_weights_v2_5min.json` & updated `microstructure_weights.json`)**: Empirical drop-in linear logit weights aligned to +5% TP / -3.5% SL / 300s holding horizon.

### V3.1 (2026-08-29) — Quant Audit Round 2
* Fixed Q-5 (futures token routing as `strike=0 CE` in `on_tick_parsed`).
* Fixed Q-4 (TimeBar slope normalization to bps).
* Fixed Q-7 (SEBI lot size 65/20 and statutory charge modeling in backtest).
* Fixed Q-6 (FSM trigger inception timestamp tracking).
* Cleaned Kronos residue from `settings.py` and `requirements.txt`.

### V3.0 (2026-08-20) — Empirical Microstructure Impulse Engine
The prior `AEGIS V2.md` (307 lines, theoretical V2 Gamma-Burst spec) described a **turnover-bucketed Hawkes/NGAO/Kyle-lambda** engine. Every one of those subsystems has been **retired or stubbed** in the current V3 code:

| Prior V2 concept | Current V3 reality | Evidence |
|---|---|---|
| Turnover buckets (₹5L) | `bucket_map={sym: 500_000}` reserved at `aegis_main.py:139` but never read | no `bucket_map` reference in `aegis_gamma_engine.py` |
| Hawkes branching `B` | `AegisGammaEngine._hawkes_branching:566 → 0.0` | stub; `microstructure_scorer` has no Hawkes term |
| NGAO acceleration `D0 / sum_speed` | Removed — no `ngao` symbol in codebase | `grep -r ngao → 0 hits` outside the old doc |
| Kyle's lambda limit engine | Removed — execution uses fixed `_round_tick` buffers at `aegis_execution_manager.py:615` and `common/execution/order_lifecycle.py:106` | no `kyle` symbol outside docs |
| Kelly sizing | Removed — `StrikeSelector.select_strikes` at `aegis_execution_manager.py:344` allocates `1×lot` (cheapest valid strike) at `allocations[0]` (361); `CapitalEngine.available_funds` is checked but not Kelly-scaled | no `kelly` symbol in AEGIS |
| G1..G9 hard gates | Replaced by 4-domain V3 pipeline at §6.5 | gates dict at `aegis_gamma_engine.py:417` is `{permission_granted, persistence_confirmed, option_qualified, eqs_qualified}` |
| IV solver (`_impvol_newton`) | Retired from engine; lives only in `research_analytics._bsm_iv:41` (research-only) | `aegis_gamma_engine.py` has no `impvol` symbol |
| Gate precision / `G6_RUNNING_UNVALIDATED_QUEUE_ASSUMPTION` | Obsolete — no G6 in live code | only in prior doc §2 I8 |

**Retain for continuity (stubs kept for dashboard/IPC compat):** `gamma_sum:0.0` and `hawkes_B:0.0` at `aegis_gamma_engine.py:572,575` and `aegis_main.py:318-319`.

**Net effect:** The code is **simpler and more honest** — fewer uncalibrated physics metaphors, one calibrated logit plus four interpretable quality domains. Any claim about prior V2 invariants should be re-evaluated against §14.

---

## Appendix A — Signal Payload Contract (Exact)

**Emitted at** `aegis_gamma_engine.py:522` via `signal_queue.put_nowait(payload)` where `payload:523`:

```python
{
  "type": "GAMMA_BURST_ENTRY",      # literal, matched at aegis_execution_manager.py:300
  "sym": "NIFTY26AUG…CE",           # resolved via token_to_symbol at aegis_gamma_engine.py:200
  "signal": "UP" | "DOWN",         # CE→UP, PE→DOWN at 310
  "lp": 42.35,                      # option last price at emit time (float)
  "spot": 24831.0,                  # engine.last_spot at 528
  "conviction": 81.4,               # scorer 0-100 at 416/528
  "gates": {
    "permission_granted": True,
    "persistence_confirmed": True,
    "option_qualified": True,
    "eqs_qualified": True
  },
  "metrics": {
    "eqs_score": 77.2, "microstructure_score": 81.4, "persistence_score": 0.83,
    "live_tick_rate": 1.92, "strike": 24800.0, "side": "CE",
    # … spread of scorer feats (touch_pressure, deep_pressure, …) plus EQS breakdown
    # and TimeBar metrics (ema_1m_slope, atr_ratio, market_structure) via ** unpack at 430-432
  },
  "timestamp": 1724912345.67          # time.time() at 531
}
```

The premarket bridge synthesizes the **same shape** at `aegis_execution_manager.py:281` with `conviction 5.0, lp=indicative_spot, metrics={source:"09:08_PREMARKET_BRIDGE", gap_pct}`.

---

## Appendix B — File:Line Index (Top Anchors)

| Claim | Anchor |
|---|---|
| Entry module | `AEGIS/aegis_main.py:87` |
| Brain class | `AEGIS/aegis_gamma_engine.py:72` |
| Hot path | `AEGIS/aegis_gamma_engine.py:197` |
| Tick-rate ring | `AEGIS/aegis_gamma_engine.py:150,228` |
| 4-domain pipeline | `AEGIS/aegis_gamma_engine.py:309` |
| Scorer | `AEGIS/core/microstructure_scorer.py:100` |
| TimeBar EMA/slope | `AEGIS/core/time_bar_engine.py:174,230,267` |
| Persistence | `AEGIS/core/directional_persistence.py:34,58,93` |
| Chop gate | `AEGIS/core/regime_chop_gate.py:16,83` |
| FSM | `AEGIS/core/signal_state_machine.py:43,67` |
| Option quality | `AEGIS/core/option_quality.py:22,34` |
| EQS | `AEGIS/core/entry_quality_engine.py:35,38` |
| Telemetry | `AEGIS/core/outcome_telemetry.py:60,70,97,164` |
| Repository | `AEGIS/core/instrument_repository.py:15,42,152,252` |
| Archiver schema + flush | `AEGIS/l2_options_archiver.py:50,73,162,219` |
| Execution constants | `AEGIS/aegis_execution_manager.py:42` |
| Execution loop | `AEGIS/aegis_execution_manager.py:225` |
| Exit priority | `AEGIS/aegis_execution_manager.py:474,516` |
| Chase thread | `AEGIS/aegis_execution_manager.py:581` |
| IPC file | `common/ipc/live_state.py:32,70,156` |
| Settings bind | `AEGIS/config/settings.py:11` |
| TickCompiler pool | `ARGUS/tick_compiler.py:28,42,209` |
| PM2 apps | `ecosystem.config.js:1` |
| Research harness | `AEGIS/research_feature_backtest.py:46,628,674,710` |
| Backtester | `AEGIS/backtest_engine.py:84,249,366,503` |
| Calendar gates | `common/market/market_calendar.py:48,70,78` |

---

*End of AEGIS V3.1 Architecture Authority · Code-grounded 2026-08-29.*  
*Deviation requires a commit touching at least one `file:line` anchor above and a note in §20.*
