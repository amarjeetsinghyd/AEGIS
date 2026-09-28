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
| 2 | **AEGIS** — **A**utonomous **E**xecution & **G**amma **I**mbalance **S**ystem (Nifty/Sensex 0DTE Options) | `aegis-bot` | `AEGIS/aegis_main.py` | active | [`AEGIS/AEGIS_V3_ARCHITECTURE.md`](AEGIS/AEGIS_V3_ARCHITECTURE.md) |
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

### 2.2 Engine 2 — AEGIS (Autonomous Execution & Gamma Imbalance System) — V3.9 LIVE
**Current Production Configuration (2026-09-20 Calibrated, Audited & Verified):**
- **Macro Price Action Primary Trigger (`PriceActionEngine`):** Pure price action is the primary trigger across 4 scalper pillars:
  1. **Pillar 1 (20 EMA Wick Rejection):** Dual-path (Single-Bar vs Two-Bar retest expansion), 40-bar lookback, minimum 5 pause bars, strict same-side close discipline, 20 EMA leading 50 EMA, and session VWAP alignment.
  2. **Pillar 2 (Dual-Mode Liquidity Sweep & Reclaim):** Live 5-bar turtle soup reclaim + parallel shadow topological structural sweep (`TOPO_P2` on $T-1$ frozen causal graph, local extremum constraints, $T-1$ seeded unbroken anchor tracking, signed $D_{VWAP}$).
  3. **Pillar 3 (Volatility Squeeze Breakout):** 4-bar box compression ($\le 1.25 \times \text{ATR}$) followed by full-bar breakout close; metric scale preservation.
  4. **Pillar 4 (Dual-Timeframe Topological Phase Shift Breakout):** Dual-timeframe 1m+5m strict HVG monotonic stack $O(N)$, prominent anchor visibility ($\ge 1.75$), 5m macro structural alignment & proximity shield, and continuous 400ms Anti-Spoofing L2 Wall Shield.
- **Composite Conviction Continuous Scoring (`CompositeConvictionEngine`):** 0–100 multi-domain scoring (Structure, Impulse, Flow, Feasibility). Sizing tiers: $\ge 85.0$ $\to$ FULL_SIZE (2 lots, +5.0% TP), $75.0 - 84.9$ $\to$ STANDARD_SIZE (1 lot, +4.0% TP), $< 75.0$ $\to$ BLOCKED.
- **LightGBM ML Gatekeeper (`MLGatekeeper`):** Sub-millisecond C-API inference on 4 microstructure alpha features (`kyle_lambda_30s`, `momentum_accel_10s`, `option_ret_30s`, `spread_pct`).
- **Institutional Quality Stack (`QualitySlotPolicy`):** Max 3 trades/day, 3rd slot reserved for afternoon ($\ge 14:45\text{ IST}$), dynamic score hurdle (beat prior best), consecutive loss stop (2 losses), daily loss cap (₹2,000).
- **Secondary Microstructure Shield (Validator):** Validates liquidity and orderbook safety:
  - 60s Directional Persistence ($D_p \ge 0.70$, opposite $\le 2$).
  - Adaptive Order Flow Energy (OFEE $\ge 60.0$, strict BOS $\ge 70.0$).
  - Real-Time OI Built-Up & Wall Gate (`OIBuiltupGate`) and Index VWAP Extension Gate.
- **Execution & Adaptive Quantitative Exits (`AegisExecutionManager`):**
  - Single open position, `MAX_TRADES_PER_DAY = 3`.
  - **Strict Non-Consecutive Setup Lockout:** Same route blocked for 180s until an alternate route fires.
  - **Resting Maker Limit TP:** Exchange limit order at **`+4.0%`** / **`+5.0%`** (zero slippage maker fill).
  - **Claude's 4-Pillar Adaptive Quantitative Exits:** 30s Squeeze Momentum Timeout ($< +1.5\%$ hurdle), Two-Key Breakeven Guard ($\ge 2.5\times$ noise floor + 60s/3.5 bps confirmation), Dynamic Runner Trailing ($\ge 3.0\times$ floor / $+15\%$, 20% trail), Spot Invalidation SL (6.5 bps + 1.0x ATR, realized delta derived, 35% emergency cap), Candle Wick Invalidation SL ($\pm 5$ pts buffer), Trend Reversal Invalidation, Adaptive 5m $\to$ 6m Time Ceiling, and 3-step limit chase thread.
- **Zero Cold-Start Warmup:** 09:14 AM historical 1-minute bar pre-seeding via Shoonya REST API (rolls through 5m clock aggregator).
- Full authority → [`AEGIS/AEGIS_V3_ARCHITECTURE.md`](AEGIS/AEGIS_V3_ARCHITECTURE.md).

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
