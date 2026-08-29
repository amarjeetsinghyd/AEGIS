# Quant Bot — System Architecture Map

**Root architecture authority.** Maps the whole deployment and links into each
engine's own authoritative harness document.

**Owner:** Amarjeet Singh · **Broker:** Shoonya (NorenApi), LMT only in F&O
**Host:** Oracle Cloud VPS (Ubuntu), 100% autonomous via PM2
**Last updated:** 2026-08-19

---

## 1. Ten-thousand-foot view

`                          Oracle Cloud VPS  (opc@137.23.41.38)
                          ┌─────────────────────────────────────────────┐
    Shoonya broker ◀──WS──│   PM2 manages 5 apps                         │
                          │   ├─ l2-archiver        (Raw Logger, 1st)     │
                          │   ├─ aegis-bot          (Options Engine, 2nd) │
                          │   ├─ argus              (Equities Engine, 3rd)│
                          │   ├─ unified-dashboard  (Flask UI reader)    │
                          │   └─ eod-calibrator     (Daily EOD ML Cron)   │
                          └─────────────────────────────────────────────┘
                                        common/  (shared library)
                                        /dev/shm/*.json  (IPC)
```
`l2-archiver` is the standalone raw L2 logger (starts at 08:55 IST for auction capture); engines open their own WS via TickCompiler. All PM2 apps share the root `common/` library and the unified dashboard reads state JSON off disk.

---

## 2. Engines

| # | Engine | PM2 process | Entry | State | Harness document (authoritative) |
|---|---|---|---|---|---|
| 1 | **ARGUS** — **A**lgorithmic **R**eal-time **G**ranular **U**ltra-microstructure **S**calper (Nifty 100 cash) | `argus` | `ARGUS/main.py` | active | [`ARGUS/AGENTIC HARNESS DOCUMENT v3.5.md`](ARGUS/AGENTIC%20HARNESS%20DOCUMENT%20v3.5.md) |
| 2 | **AEGIS** — **A**utonomous **E**xecution & **G**amma **I**mbalance **S**ystem (Nifty/Sensex 0DTE Options) | `aegis-bot` | `AEGIS/aegis_main.py` | active | [`astra_bot/AGENTIC HARNESS DOCUMENT Astra v4.3.md`](astra_bot/AGENTIC%20HARNESS%20DOCUMENT%20Astra%20v4.3.md) |
| ~~3~~ | ~~Kronos Bot~~ | — | — | **DECOMMISSIONED** — see §5 | (historical only — do not revive) |

Each engine owns its detailed spec; this root file only maps them and the
shared plumbing. **When an engine's behaviour is in question, read that
engine's harness document — it overrides everything here for that engine.**

### 2.1 Engine 1 — ARGUS (Algorithmic Real-Time Granular Ultra-Microstructure Scalper v4.0)
Zero-copy `ParsedTick` (`__slots__`), per-symbol object pool, unified
composite impulse engine ($S_{\text{flow}}$) with **continuous multi-horizon momentum alignment ($M_{\text{align}}$ across 10s, 1m, 5m, 15m)**,
**dynamic regime & TOD threshold calibration ($\theta_{\text{dynamic}} \in [0.35, 0.60]$)**,
**Enhanced Directional Adverse Selection Shield with L2 Queue Depletion & Spoofing Detection ($P_{\text{adv}}$)**,
**Kyle's Lambda Limit Price Engine**, **Kelly Criterion Dynamic Sizing**, **pre-market alpha bridge (09:08)**,
and **4-pillar microstructure-decay exits with Adaptive SVI & Peak-Lock**
(`MPAR_DECEL`/`DKLA_FLIP`/`VAC_REVERSAL`/`LRA_VEL_FLIP` + confirmation
`TOX_SPIKE`/`WHALE_EXIT`/`BRANCHING_COLLAPSE`, plus `TIME_STOP`/
`EOD_FLATTEN`/`BREAKEVEN_LOCK`/`CHANDELIER_ATR`). Full spec → [`ARGUS/ARGUS_SYSTEM_ARCHITECTURE.md`](ARGUS/ARGUS_SYSTEM_ARCHITECTURE.md).

### 2.2 Engine 2 — AEGIS (Autonomous Execution & Gamma Imbalance System) — V3.1 LIVE

**What it actually is (code-grounded 2026-08-29):** Empirical 0-100 microstructure impulse detector
(`AEGIS/aegis_gamma_engine.py:72` → `core/microstructure_scorer.py:15`, calibrated `n=2436, ROC AUC 0.8264`,
intercept `-6.04`, 14 weights at `data/params/microstructure_weights.json:7`) fused through a
4-domain quality gate — (1) Permission: `core/time_bar_engine.py:33` (1m/3m EMA9 slopes, ATR14, 30-min baseline,
market structure ALIGNED/NEUTRAL/OPPOSED), (2) Confirmation: `core/directional_persistence.py:29` (60s × 12 × 5s
signed window, 70% persistence, ≤1 opposite), (3) Tradability: `core/option_quality.py:17` (spread ≤2.5%,
depth ≥500, premium 5-400), (4) Centralized FSM: `core/signal_state_machine.py:43`
`OBSERVE→PRIMED(≥78)→CONFIRMED→FIRED(≤3s)→COOLDOWN(120s)` → unified `EQS 0-100` at
`core/entry_quality_engine.py:30` (30+25+25+20, dispatch 70). Hot path is scalar O(1) at
`aegis_gamma_engine.py:197` via pooled `ParsedTick` (`ARGUS/tick_compiler.py:28`) with a 2s tick-rate ring
veto (`min_tick_rate_gate 1.5`) at `aegis_gamma_engine.py:303`. Single open position, `MAX_TRADES 3`,
`+5% TP / -3.5% SL / +2.0→+1.5% trail / ±25/50pts spot / 300s hard` in
`aegis_execution_manager.py:42` with 3-step limit chase (`0.99→0.98→0.97×bid`), fill→edge feedback
(`on_fill:539` → `check_pending_edges:554`), and forward telemetry `MFE/MAE +5/+8%` JSONL at
`core/outcome_telemetry.py:60`. Passive parquet archiver (`l2_options_archiver.py:73`, 15-min Snappy, 30d)
is embedded as a tick listener (`aegis_main.py:174`), not a standalone `l2-archiver` PM2 app.
0DTE only, Nifty Mon/Tue/Fri + Sensex Wed/Thu, entry `9:16-15:00`, flatten `≥15:10`, LMT only.
Full authority → [`AEGIS/AEGIS V2.md v3.1`](AEGIS/AEGIS%20V2.md) (code-grounded, supersedes all prior V2 theory
including Hawkes/NGAO/Kyle/Kelly/G1..G9 which are retired/stubbed — see §20 there).

### 2.3 L2 Archivers — Raw Data Loggers (Passive)

* **ARGUS equities:** `ARGUS/l2_archiver.py` — standalone `l2-archiver` PM2 (legacy standalone; 08:55 start, 110 symbols from `common/symbols.py`, 15-min Snappy under `ARGUS/data/l2_raw/`, auction 09:00-09:15/15:30-15:40).
* **AEGIS options:** `AEGIS/l2_options_archiver.py:73` — **embedded** passive listener (attached at `AEGIS/aegis_main.py:174` via `broker.add_tick_listener`), not a standalone PM2 app. Schema adds `kind(0=index,1=CE,2=PE,3=FUT)` + `strike` + `oi` (`l2_options_archiver.py:50`), writes to `AEGIS/data/l2_raw/{Nifty,Sensex}/aegis_l2_raw_YYYY-MM-DD_part_NNN.parquet` every 15 min or 50k rows (900s flush at line 162, `compression="snappy"` at 178, 30d prune at 137), auction JSONL to `data/logs/`. Must be attached by 08:55 to capture 09:00-09:08.
* **System view:** `SYSTEM_ARCHITECTURE.md:13` diagram's `l2-archiver` box is historical — on VPS the live wiring is per-engine embedded archivers; align `ecosystem.config.js` (no `l2-archiver` entry) and this section on next deploy.

---

## 3. Shared library — `common/`

Single import surface for both engines + dashboard.

| Subpath | Responsibility |
|---|---|
| `common/config.py` | `configure_runtime(name)`/`get_data_dir()` — runtime selection |
| `common/symbols.py` | **Single source of truth** for `SUBSCRIPTION_UNIVERSE` (110), `NIFTY100` |
| `common/logger.py` | structured logger factory |
| `common/broker/` | Shoonya + (legacy Angel) adapters, `session_manager`, `rate_limiter`, `manifest`, `interfaces` |
| `common/execution/` | `capital_engine`, `order_lifecycle`, `execution_manager`, `strike_selector`, `decision_lifecycle`, `trade_context` |
| `common/market/` | `market_calendar`, `instrument_registry`, `instrument_repository` |
| `common/ipc/` | `live_state.py` — shared tick file + per-engine state JSON writers |
| `common/runtime/` | `paths.py` — `get_runtime_paths(name)` → engine data/log roots |

> **Dormant kronos branches** (`paths.py` `runtime_name=="kronos"` and
> `ipc/live_state.py` `KRONOS_*`) are intentionally KEPT as safe fallbacks.
> They do not revive Kronos. Do not delete without the human's OK — see
> [`AGENTS.md`](AGENTS.md).

---

## 4. Shoonya (NorenApi) constraints — the broker seam

Codified from harness doc §1.1.1; do not assume otherwise without verifying:
- **Weekday-only.** The Shoonya WS connection goes dark on Saturdays/Sundays
  (and NSE holidays). A Sat/Sun WS drop or "no tick" is **not a bug** — do not
  file it or build reconnect logic around it. Reconnect/backoff only matters
  on a *trading* day.
- **Static IP mandatory** on the VPS egress.
- **Single session / single credential** set; tokens expire in ~24h.
- `tokens_cache.json` weekly refresh window.
- **No paper/sim mode** — backtest must be done off live ticks.
- **History burst** ~10 req/s; L1 + top-5-level L2 depth only.

---

## 5. Kronos — decommissioned

`kronos_bot` is shut down. Code lives only in `_archive/kronos_bot/`
(read-only history). The live `kronos_bot/` directory at repo root contains
only auto-created empty `data/` and `logs/` and is left to rot — do **not**
revive it, re-add its package, add it to the dashboard, write tests against it,
or migrate its logic into the active engines. Full policy → [`AGENTS.md`](AGENTS.md)
(§"DECOMMISSIONED COMPONENT").

---

## 6. Unified Dashboard — `unified_dashboard/dashboard.py`

Lightweight Flask server + flat HTML/JS/CSS. **Decoupled** from the engines:
reads `capital_state.json` / `trade_history.json` off disk. Two-column grid
œ **Quant Console × ORDER FLOW × ASTRA** — **no Kronos** brand, tab, or state
panel. PM2 app `unified-dashboard`.

---

## 7. VPS & ops access

| Item | Value |
|---|---|
| Host | `137.23.41.38` (Oracle Cloud, Ubuntu) |
| User | `opc` |
| Repo path on VPS | `/home/opc` |
| SSH key (local) | `Oracle SSH keys/ssh-key-2026-07-11.key` at **project repo root** — never buried inside an engine subfolder |
| SSH command | `ssh -i "Oracle SSH keys/ssh-key-2026-07-11.key" opc@137.23.41.38` |
| Process manager | PM2, config `ecosystem.config.js` (4 apps: `l2-archiver`, `astra-bot`, `stock-order-flow`, `unified-dashboard`) |
| Deploy rule | PM2 restart `l2-archiver` **first** at 08:55 IST, then `astra-bot`, then `stock-order-flow`; deploy only after 15:30 IST or before 09:15 IST |

Canonical layout + repo-hygiene rules enforced in [`AGENTS.md`](AGENTS.md).

---

## 8. Repository layout (canonical)

```
quant-bot/
├── AGENTS.md                 # standing AI instructions (authoritative)
├── SYSTEM_ARCHITECTURE.md    # THIS file — the map
├── README.md
├── AI_CHANGELOG.md
├── ecosystem.config.js       # PM2 app definitions
├── Oracle SSH keys/          # SSH key lives ONLY here (root)
├── common/                   # shared library (§3)
├── stock_order_flow/         # Engine 1 + its harness doc (+ docs/, archive/)
│   └── stock_calibration.json  # Extracted calibration params (regime weights, thresholds, priors)
├── astra_bot/                # Engine 2 + its harness doc (+ core/, execution/, archive/)
├── unified_dashboard/        # Flask UI
├── institutional_memory/     # constituents.json etc.
├── scripts/                  # ad-hoc diagnostic / ops scripts
├── tests/                    # pytest suite (must stay 100% green)
├── _archive/                 # read-only history (kronos_bot/, shoonya_data_hub/, stale docs)
└── venv/ (local), node_modules/ (local)        # not committed
```
- Engine-permanent files stay in that engine's folder; cross-engine only via `common/`.
- Temp/scratch/scratch_state*.json never at repo root — keep scratch in `scratch/`.
- Legacy/backup copies go into the relevant engine's `archive/` or root `_archive/`, never loose in the engine dir.
- **Universe Scanner** runs at 15-min intervals during market hours (not 16:05 only) — see `scripts/universe_scanner.py`.
- **L2 Archiver** must start by 08:55 IST to capture 09:00-09:08 auction window.

---

## 9. Where authority lives (priority order)

1. [`AGENTS.md`](AGENTS.md) — standing instructions for any AI/human (this file is law).
2. Each engine's harness document — overrides everything **for that engine**.
3. This root `SYSTEM_ARCHITECTURE.md` — the cross-engine map only.
4. `AI_CHANGELOG.md` — narrative change history (append-only, not a spec).

Older architecture/review docs inside `_archive/` and inside each engine's
`archive/` are **historical only**; trust the live harness docs above them.
