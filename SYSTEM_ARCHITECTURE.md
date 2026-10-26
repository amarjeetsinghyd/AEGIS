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
| 1 | **AEGIS** — **A**utonomous **E**xecution & **G**amma **I**mbalance **S**ystem (Nifty/Sensex 0DTE Options) | `aegis-bot` | `AEGIS/aegis_main.py` | **PRIMARY ACTIVE** (Core Focus) | [`AEGIS/AEGIS_V1.0_ARCHITECTURE.md`](AEGIS/AEGIS_V1.0_ARCHITECTURE.md) |
| 2 | **ARGUS** — **A**lgorithmic **R**eal-time **G**ranular **U**ltra-microstructure **S**calper (Nifty 100 cash) | `argus` | `ARGUS/main.py` | Secondary / Deprioritized | [`ARGUS/AGENTIC HARNESS DOCUMENT v3.5.md`](ARGUS/AGENTIC%20HARNESS%20DOCUMENT%20v3.5.md) |
| ~~3~~ | ~~Kronos Bot~~ | — | — | **DECOMMISSIONED** — see §5 | (historical only — do not revive) |

Each engine owns its detailed spec; this root file only maps them and the
shared plumbing. **When an engine's behaviour is in question, read that
engine's harness document — it overrides everything here for that engine.**

### 2.1 Engine 1 (Primary) — AEGIS (Autonomous Execution & Gamma Imbalance System) — Production Version 1.0 (v1.0 Locked) LIVE
**Current Production Configuration (Calibrated, Audited & Verified):**
- **Macro Price Action Primary Trigger & Priority Mode B (`PriceActionEngine`):** Pure price action primary trigger across 4 scalper pillars (P1 20 EMA Pullback with Wyckoff Candle $N-1$ Smart Gate, P2 Dynamic S/R Registry & Day Extreme Sweep & Reclaim, P3 Volatility Squeeze Breakout, P4 Dynamic S/R & Prominence Hub Breakout) + **Priority Mode B In-Flight Mid-Bar Breakthrough Surge (`T:08s onwards / No Upper Cap`)**.
- **Absolute Pain Zone VWAP Band (v1.0 Production Early Veto):** Spot above Upper Band $\implies$ CALL only; Spot below Lower Band $\implies$ PUT only; inside Band $\implies$ Discarded.
- **Wyckoff Volume & Order Flow Delta Engine (`wyckoff_volume_engine.py`):** Effort vs Result, Volume Dry-Up Ratio ($VDR \ge 1.0$), Absorption pin wick, and Delta Harmony (Call Buy vs Put Buy with $\ge 1.30\times$ ratio).
- **Weekly Trading Calendar & DTE Schedule:**
  - **Monday**: NIFTY (**1 DTE**) [NFO, step 50, lot 65]
  - **Tuesday**: NIFTY (**0 DTE** Expiry Day) [NFO, step 50, lot 65]
  - **Wednesday**: SENSEX (**1 DTE**) [BFO, step 100, lot 20]
  - **Thursday**: SENSEX (**0 DTE** Expiry Day) [BFO, step 100, lot 20]
  - **Friday**: NIFTY (**4 DTE**) [NFO, step 50, lot 65] — *Disabled by default (Deep Sleep Mode)*
  - *(Saturday / Sunday: Market Closed, no bot trading)*
- **Single-Shot Clean Entry (Zero Chasing Mandate):** 0.75s atomic fill window, instant signal queue draining, zero order chasing/modification.
- **Deterministic Edge (ML Decommissioned):** CatBoost ML, Volume Profile, and HVG engines permanently decommissioned and archived.
- **Adaptive Stepped Exits & Permanent Target Discipline:** -12% disaster floor $\to$ Step 1 (-3% SL @ +3.0% peak) $\to$ Step 2 (0.0% cost-shield @ +3.5% peak) $\to$ Step 3 (0.8% near-target lock) $\to$ Strict +4.0% Maker TP (Friday calibrated to +3.0%; never greed 5%–6%) $\to$ 15:08:00 CAS force-flatten.
- **Full Architecture Authority:** [`AEGIS/AEGIS_V1.0_ARCHITECTURE.md`](AEGIS/AEGIS_V1.0_ARCHITECTURE.md) (Production Version 1.0 Locked).

### 2.2 Engine 2 (Secondary / Deprioritized) — ARGUS (Algorithmic Real-Time Granular Ultra-Microstructure Scalper v4.0)
Zero-copy `ParsedTick` (`__slots__`), per-symbol object pool, unified
composite impulse engine ($S_{\text{flow}}$) with **continuous multi-horizon momentum alignment ($M_{\text{align}}$ across 10s, 1m, 5m, 15m)**,
**dynamic regime & TOD threshold calibration ($\theta_{\text{dynamic}} \in [0.35, 0.60]$)**,
**Enhanced Directional Adverse Selection Shield with L2 Queue Depletion & Spoofing Detection ($P_{\text{adv}}$)**,
**Kyle's Lambda Limit Price Engine**, **Kelly Criterion Dynamic Sizing**, **pre-market alpha bridge (09:08)**,
and **4-pillar microstructure-decay exits with Adaptive SVI & Peak-Lock**
(`MPAR_DECEL`/`DKLA_FLIP`/`VAC_REVERSAL`/`LRA_VEL_FLIP` + confirmation
`TOX_SPIKE`/`WHALE_EXIT`/`BRANCHING_COLLAPSE`, plus `TIME_STOP`/
`EOD_FLATTEN`/`BREAKEVEN_LOCK`/`CHANDELIER_ATR`). Full spec → [`ARGUS/ARGUS_SYSTEM_ARCHITECTURE.md`](ARGUS/ARGUS_SYSTEM_ARCHITECTURE.md).

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
├── SYSTEM_CONTEXT.md, SYSTEM_STATE.md, SYSTEM_GENESIS_AND_EVOLUTION.md, VPS_OPERATIONS.md
├── CHANGELOG.md              # ordered engineering changelog
├── README.md
├── ecosystem.config.js       # PM2 app definitions
├── pytest.ini                # pytest config (slow marker; testpaths=tests)
├── Oracle SSH keys/          # SSH key lives ONLY here (root)
├── *_symbols.txt             # broker symbol master files (runtime, gitignored)
├── common/                   # shared library (§3)
├── AEGIS/                    # Engine 1 (PRIMARY, LIVE) — options engine (v1.0 Locked)
│   └── _archive/             # AEGIS evolution history + decommissioned_2026-10/ (11 dead modules)
├── ARGUS/                    # Engine 2 (Secondary / Dormant) — HFT equities scalper
├── unified_dashboard/        # Flask UI (Quant Console)
├── institutional_memory/     # constituents.json etc.
├── scripts/                  # curated operational + diagnostic scripts ONLY (16 files)
├── scratch/                  # throwaway workspace — gitignored, never committed
├── tests/                    # pytest suite (216 tests, 100% green; _archive/ holds retired tests)
└── _archive/                 # read-only history — see ARCHIVE_MANIFEST.md (kronos_bot/, astra_bot_2026-10/, STRATOS/, 2026-10_scratch_research/, stale docs)
```
- Engine-permanent files stay in that engine's folder; cross-engine only via `common/`.
- Temp/scratch/scratch_state*.json never at repo root — keep scratch in `scratch/` (gitignored).
- Legacy/backup copies go into the relevant engine's `archive/` or root `_archive/`, never loose in the engine dir.
- `kronos_bot/` was deleted 2026-10-25 (history in `_archive/kronos_bot/`); `astra_bot/` was archived to `_archive/astra_bot_2026-10/` (legacy runtime data). Empty shells of both may reappear via the protected `get_runtime_paths("kronos")` test branch — gitignored, harmless.
- Archived entries can be permanently deleted after ~2-3 months per `_archive/ARCHIVE_MANIFEST.md`.

---

## 9. Where authority lives (priority order)

1. [`AGENTS.md`](AGENTS.md) — standing instructions for any AI/human (this file is law).
2. Each engine's harness document — overrides everything **for that engine**.
3. This root `SYSTEM_ARCHITECTURE.md` — the cross-engine map only.
4. `AI_CHANGELOG.md` — narrative change history (append-only, not a spec).

Older architecture/review docs inside `_archive/` and inside each engine's
`archive/` are **historical only**; trust the live harness docs above them.
