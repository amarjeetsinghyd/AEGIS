# AI AGENT INSTRUCTIONS — quant-bot repo

> **Read this before making any change in this repository.**
> Applies to every AI coder (Cursor, Antigravity, Devin, deepseek-harness, etc.) and every human contributor.

---

## 💬 USER COMMUNICATION & PRESENTATION PROTOCOL (MANDATORY FOR ALL AI AGENTS)

> **Every AI agent must strictly follow these presentation guidelines when replying to the user (Amarjeet bhai):**
> 1. **Script & Language Mandate (देवनागरी लिपि अनिवार्य)**:
>    - Jawab **hamesha Devnagari script (देवनागरी लिपि)** mein hi dena hai, Devnagari aur English quant/technical terms ko naturally mix karke (जैसे: *"अमरजीत भाई, AEGIS ने Priority Mode B Mid-Bar Breakthrough डिटेक्ट किया है..."*). Pure Roman English ya Roman English script (Hinglish) mein reply nahi dena hai.
> 2. **Readability & Ultra-Clean Presentation (पढ़ने में सबसे आसान)**:
>    - Response ko padhne mein ultra-smooth aur easy banayein: short crisp paragraphs, clear bullet points, intuitive visual badges (🟢, 🔴, ⚡, 🛡️, 🚀), clear bold highlights. Complex dense wall-of-text bilkul nahi banana hai.
> 3. **Generative UI Policy (User-Requested Only)**:
>    - Generative UI / HTML widgets proactively bina maange nahi banane hain. Inline cards ya UI artifacts tabhi banayein jab Amarjeet bhai explicitly unki demand karein.

---

## DECOMMISSIONED COMPONENT — DO NOT REVIVE

### `kronos_bot` is SHUT DOWN (decommissioned)

**Kronos is no longer an active engine in this repository.** Treat it as legacy/abandoned.

**Do NOT, under any circumstance:**
- Re-create `kronos_bot/__init__.py`, `kronos_bot/config/`, or `kronos_bot/config/settings.py`.
- Add `kronos_bot` back into `unified_dashboard` UI, brand lines, titles, or `CONSOLE_HTML`.
- Add `kRONOS`, `Kronos`, or any kronos-marketing copy to dashboards, docs, or configs.
- "Fix" failing tests by re-introducing kronos code, stubs, or packages.
- Migrate kronos logic into `AEGIS` unless explicitly told otherwise.

**Acceptable Kronos-related actions (and only these):**
- Removing kronos references from active tests, dashboards, docs, or configs.
- Deleting kronos stub packages, imports, or test functions if found.
- Leaving the historical `_archive/kronos_bot/` as read-only history — do not edit or revive it.

### Why a test may reference Kronos anyway
> **Decision (2026-08-16):** The shared-library kronos branches in `common/` are kept as
> safe fallbacks and MUST be left untouched unless the human explicitly authorizes cleaning
> them. They do not revive Kronos — they are dormant branches exercised only by one legacy
> test.

Some files still reference kronos because the shared library predates the shutdown:
- `common/runtime/paths.py` still accepts `runtime_name == "kronos"` (legacy branch, kept).
- `common/ipc/live_state.py` still defines `KRONOS_STATE_FILE`, `_DEFAULT_KRONOS`,
  `update_kronos_live_state`, `get_kronos_live_state` (legacy branch, kept).
- `tests/test_shared_runtime_paths.py` uses `get_runtime_paths("kronos")` and
  `update_kronos_live_state` — these pass because they hit the shared-library kronos
  branches, NOT the deleted `kronos_bot` package. Leave as-is.

The obsolete `test_kronos_settings_use_kronos_package_paths` test (which imported the
deleted `kronos_bot.config.settings`) has been removed from
`tests/test_bot_path_wiring.py`. Do not re-add it or any equivalent kronos-package test.

**If you are unsure whether removing a kronos reference is in scope, default to leaving
the `common/` shared branches alone and ask. Never add new kronos code.**

---

## ACTIVE ENGINES (Priority Order)

1. **`AEGIS/`** — **SOLE ACTIVE PRODUCTION ENGINE (100% Core Focus)** — **AEGIS** (**A**utonomous **E**xecution & **G**amma **I**mbalance **S**ystem) — options strategy bot (Production Version 1.0 / v1.0 Locked).
   Architecture authority: `AEGIS/AEGIS_V1.0_ARCHITECTURE.md` (Production Version 1.0 Locked).
   - **Primary Focus**: All development, telemetry, execution, and live trading center exclusively on AEGIS.
   - **Architecture & Rules**: Conform all edits to `AEGIS/AEGIS_V1.0_ARCHITECTURE.md`:
     * **4 Macro Price Action Pillars + Hybrid Intra-Bar Execution**:
       - **Priority Mode B (In-Flight Mid-Bar Breakthrough Surge from T:02s onwards / No Upper Cap)**: Evaluates live forming bars across **ALL 4 Pillars** (Pillar 3 `volatility_squeeze_breakout`, Pillar 4 `topological_prominence_breakout` + Dynamic S/R Registry, Pillar 1 `20_ema_rejection` in-flight reversal pierce, and Pillar 2 `micro_sweep_reclaim` in-flight reclaim pierce). Single-shot clean execution entering ahead of retail before candle close. Live Mode B boundaries are **Squeeze Box + S/R Registry + EMA Band / Prior Bar Extremes**; Pain Zone / 200 EMA breakthroughs are tracked **exclusively** by the `ShadowBoundaryObserver` (shadow experiment).
       - **Mode A (Rejection & Pierce / T:00s Close & T:50s Pre-Close)** for Pillar 1 (`20_ema_rejection`) & Pillar 2 (`micro_sweep_reclaim`) on finalized/pre-close candle structures.
     * **Dynamic Session S/R Level Registry (`StructuralLevel`)**: Monotonic `seq_idx` tracking, multi-touch boost (`+0.35`), dynamic post-excursion prominence, and Order Flow footprint.
     * **Scale-Invariant Day Extreme Runway Shield**: `min_runway_pts = max(1.25 * ATR_1m, 0.0008 * Spot)`.
     * **Single-Shot Clean Entry Mandate**: Atomic `0.75s` fill window, zero order chasing, immediate `_drain_signal_queue()`, powered by Rolling 5-Second Hot Strikes Pre-Staging Cache.
     * **Last-Mile In-Flight Continuation & Peak Drawdown Hygiene Gate**.
     * **Friday (4 DTE) Option Elasticity Calibration**: `+3.0%` Maker TP on Fridays (`+4.0%` Mon–Thu).
     * **Parallel Wyckoff Volume & OFI Delta Harmony Gate**.
     * **Continuous Composite Conviction Scoring ($\ge 80.0$ threshold)**.
     * **Absolute Pain Zone VWAP Band (v1.0 Production Early Veto)**.
     * **Strict 1-Lot Maker TP at +4.0% (Friday calibrated to +3.0%), Adaptive Exits, Universal Entry Candle Low/High Invalidation**.
     * **Strict 3-Trade Daily Cap (`MAX_TRADES_PER_DAY = 3`)**.
     * **Pre-Trade Abort Slot Rollback and 15:08:00 IST CAS Force Flatten**.
   - **Testing / Consistency Phase**: Validating pipeline health, signal consistency, Wyckoff delta harmony, and telemetry with current available account funds without manual top-ups.
2. **`ARGUS/`** — **DECOMMISSIONED & PURGED (Deleted October 2026)**.
   - Equities microstructure engine permanently deleted to maintain zero-entropy focus on AEGIS options trading.
3. **`unified_dashboard/`** — monitoring UI (Quant Console — AEGIS).

Shared library: `common/` (broker / execution / market / ipc / runtime / config / symbols).

---

## AEGIS CANONICAL ARCHITECTURE & PERMANENT OPERATIONAL RULES (MANDATORY IN EVERY CHAT)

> **Every AI agent starting a new conversation MUST honor these exact production rules:**

### 1. Macro Price Action 4 Pillars (Primary Triggers)
* **Pillar 1 (`20_ema_rejection`)**: Classical Dow Theory `HL2` (CALL) / `LH2` (PUT) 2nd-touch confirmation into 20 EMA High-Low Band; executes via Mode A (bar close / pre-close) and Priority Mode B (in-flight reversal pierce above/below prior bar extreme).
* **Pillar 2 (`micro_sweep_reclaim`)**: Priority 1 Dynamic S/R Registry & Day Extreme (PDH/PDL) Liquidity Sweep & Reclaim (eliminates random 5-bar noise, with 11:15–13:00 IST Lunch Chop Veto); executes via Mode A (bar close / pre-close) and Priority Mode B (in-flight reclaim pierce).
* **Pillar 3 (`volatility_squeeze_breakout`)**: 5–7 bar tight box compression (`<= 1.15x ATR`, individual candle `<= 0.85x ATR`, median touch `>= 50%`) expansion breakout + Priority Mode B In-Flight Surge (`T:02s onwards / No Upper Cap`) + Anti-Climax Cap (`<= 1.35x ATR`).
* **Pillar 4 (`topological_prominence_breakout`)**: Dynamic S/R Registry Breakout (`prominence >= 1.25`), governed by Priority Mode B In-Flight Surge (`T:02s onwards`), Day Extreme Runway Rule, and Wyckoff Stopping Volume Veto (`FastMonotonicStack`, `hvg_engine` and its 5m structural veto are permanently DECOMMISSIONED & ARCHIVED).
* **Priority Mode B (`check_mode_b_breakthrough`)**: Evaluates live forming bars mid-bar from `T:02s onwards (No Upper Cap)` across **ALL 4 Pillars** (P3 Squeeze Breakout, P4 S/R Registry Breakout, P1 20 EMA Reversal Pierce, P2 Micro-Sweep Reclaim Pierce). Executes single-shot clean entry entering ahead of retail, filling resting +4.0% Maker TP (+3.0% on Friday) before or as candle closes.

### 2. Absolute Pain Zone VWAP Band (v1.0 Production Early Veto)
* Single unified veto before pillars:
  * Spot **ABOVE** Upper Band $\implies$ **ONLY CALL** trades allowed.
  * Spot **BELOW** Lower Band $\implies$ **ONLY PUT** trades allowed.
  * Spot **INSIDE** Band $\implies$ Discarded / Neutral zone.

### 3. Parallel Wyckoff Volume & OFI Delta Gate
* Volume-to-Dispersion Ratio ($VDR \ge 1.0$) and positive order-flow intensity (OFI) delta harmony required to confirm institutional participation.

### 4. Strike Selection & Subscription Universe (25 OTM Depth)
* **Subscription Universe & Order Flow Window**: ATM $\pm 25$ strikes (51 strikes total, 102 tokens CE+PE) tracked on WebSocket ($t+d$) for low-capital strike selection, while Order Flow (OFI Delta, PCR, Pain Zone VWAPs) is strictly aggregated on **ATM $\pm 7$ strikes** to eliminate far-OTM illiquid noise.
* **Index Futures Passive Feed**: Subscribes 1 active near-month Index Future token (passive WebSocket feed, 0 polling loops, 0 extra REST calls) solely for emergency cold-boot candle seeding fallback and basis/telemetry. AEGIS does NOT trade futures; execution is strictly on options.
* **Low-Capital Testing Mode (`funds < 5000`)**: `StrikeSelector` scans all available OTM strikes (`TESTING_DEEP_DEPTH = 32`, min premium ₹4.00 charges floor). Automatically selects the closest affordable OTM strike for 1 lot (e.g. ₹15–₹28).
* **Index Option Liquidity**: Major Indian index options (NIFTY/SENSEX) have massive liquidity (millions of shares). **NEVER** apply artificial 2% spread blocks or thin-ask blocks.

### 5. Adaptive Exits & Near-Target Profit Lock (v1.0 Production Locked)
* **Hard Disaster Floor**: `-8.0%` option premium circuit breaker (calibrated to `-6.5%` on Friday 4 DTE). Post-SL 5-Minute Recovery Auditor continuously audits post-exit recovery to detect premature chokes vs justified capital saves.
* **Step 1 (Safety Net)**: Peak $\ge +3.0\% \implies$ SL compressed from -12% to `-3.0%` (wide breathing room; premature +2.0% choke permanently removed; on Friday 4 DTE compressed at $+2.0\%$).
* **Step 2 (Cost-Shield / Breakeven)**: Peak $\ge +3.5\% \implies$ SL compressed to `0.0%` (Entry Price scratch; on Friday 4 DTE compressed at $+2.2\%$).
* **Universal Spot Invalidation (All 4 Pillars & Mode B)**: CALL exits if Spot $\le \text{Entry Candle Low} - (0.10 \times \text{ATR})$; PUT exits if Spot $\ge \text{Entry Candle High} + (0.10 \times \text{ATR})$ with structural trailing (immunizes trade from option spread/wick noise; 9-EMA bar-close choke is permanently removed).
* **Mode B Bar-Close Escape Rule**: If a mid-bar Mode B breakout closes back inside the broken boundary at `T:60s`, trigger immediate scratch escape (`MODE_B_FAILED_CLOSE_ESCAPE`).
* **Near-Target Profit Lock (`NEAR_TARGET_TRIGGER_PCT = 0.035`, `NEAR_TARGET_PULLBACK_TOLERANCE = 0.008`)**:
  * Peak Bid $\ge +3.5\% \implies$ **Exits immediately on $\ge 0.8\%$ pullback from peak bid**! (On Friday 4 DTE: Trigger at $+2.5\%$, pullback tolerance $0.5\%$).
* **Strict 1-Lot Maker TP**: Resting Limit Order at **`+4.0%`** (100% quantity). On Friday 4-DTE, calibrated dynamically to **`+3.0%`** due to $5\times$ lower option elasticity ($\Omega$).
  * **Target Discipline Mandate (Never Expand to +5%–6%)**: +4.0% is the mathematically optimal safe harbor that fills passively within 10–40s across choppy regimes. Option momentum beyond +4% frequently faces sharp institutional dumps (e.g. crashing back below entry immediately after reaching +8%). Never greed for 5%–6% as holding for bigger targets destroys the 84%+ win-rate.
* **Single-Shot 0.75s Clean Entry (Zero Chasing)**: 0.75s hard timeout on entry orders; zero order chasing/modification on rejected or unfilled orders (`_drain_signal_queue` on reject).
* **3-Minute Time Harvest (DECOMMISSIONED 2026-10-09)**: Premature 180s profit choke permanently removed. Running green trades are given the full 5-minute (300s) baseline runway (`MAX_HOLD_SEC = 300s`) to reach the resting +4.0% Maker TP.
* **EOD CAS Flush**: 15:08:00 IST force flattens all open inventory.

### 6. Component Operational State (Live vs Shadow vs Decommissioned)
* **LIVE**:
  - `AegisPipelineOrchestrator` (`aegis_pipeline_orchestrator.py`): Central brain, Shoonya L2 tick router, 6-gate pipeline orchestrator.
  - `PriceActionEngine` (`price_action_engine.py`): Single source of truth for 1m/5m/15m OHLC candle construction and 4 Macro Price Action Pillars + Mode B In-Flight Surge.
  - `StructuralRegistryMixin` (`structural_registry.py`): Monotonic S/R registry, 5m candle clustering, role classification (`OUTERMOST_ROOF`, `INTERIOR_CLUSTER`).
  - `DowMacroShieldsMixin` (`dow_macro_shields.py`): Classical Dow Theory HL2/LH2 sequence, 20 EMA high/low bands, Day Extreme Runway Shield, Obstacle Wall Shield.
  - `OptionOrderFlowMixin`, `MicrostructureScorer`, `CompositeConvictionEngine` (`option_order_flow.py`): Unified tape reading (ATM $\pm 25$ flow, PCR, OFI Delta, Pain Zone VWAP), contract L1-L5 depth, continuous conviction gatekeeper ($\ge 80.0$).
  - `WyckoffVolumeEngine` (`wyckoff_volume_engine.py`): Volume dry-up (VDR), absorption pin wick, delta harmony gate.
  - `AdaptiveExits` (`aegis_execution_manager.py`): Universal Spot Invalidation, near-target profit lock (+3.5% trigger, 0.8% pullback), 1-lot resting Maker TP (+4.0% Mon–Thu, +3.0% Fri).
* **SHADOW**: Single active shadow observer: `ShadowBoundaryObserver` (Absolute Pain Zone & 200 EMA breakout analytics).
* **DECOMMISSIONED & ARCHIVED (`AEGIS/_archive/decommissioned_2026-10/`)**:
  - `aegis_gamma_engine.py`: Permanently purged; superseded by `aegis_pipeline_orchestrator.py` (`AegisPipelineOrchestrator`).
  - `composite_conviction_engine.py` & `microstructure_scorer.py`: Standalone files purged; unified into `option_order_flow.py`.
  - `FastMonotonicStack` & `legacy_bar_close_setups.py`: Decommissioned and completely deleted.
  - `adaptive_micro_validator.py`: Merged into conviction engine and archived.
  - `hvg_engine.py`: Decommissioned and archived.
  - `volume_profile_engine.py`, `shadow_quant_tracker.py`, `ml_gatekeeper.py`, `catboost_paper_trader.py`, `causal_microstructure_pipeline.py`, `time_bar_engine.py`, `signal_state_machine.py`, `regime_chop_gate.py`, `entry_quality_engine.py`, `oi_quotes_poller.py`: Decommissioned and archived.
  - Gemini 3.8 / `ai_shadow_reviewer.py`: Deleted. Kronos: Shut down.

### 7. Trading Calendar & Weekly Engine Schedule
* **Monday**: **NIFTY** (**1 DTE**) [NFO, strike step 50, lot 65]
* **Tuesday**: **NIFTY** (**0 DTE** — Expiry Day) [NFO, strike step 50, lot 65]
* **Wednesday**: **SENSEX** (**1 DTE**) [BFO, strike step 100, lot 20]
* **Thursday**: **SENSEX** (**0 DTE** — Expiry Day) [BFO, strike step 100, lot 20]
* **Friday**: **NIFTY** (**4 DTE**) [NFO, strike step 50, lot 65] — **DISABLED by default (Deep Sleep Mode)**. `is_trading_day` returns `False` on Friday unless `AEGIS_FRIDAY_TRADING_ENABLED=1`. The engine deep-sleeps from Thursday 15:17 close directly until Monday 08:55 AM IST.
* **Saturday & Sunday**: Market Closed (Shoonya WebSocket disconnect is normal, no bot trading).
* **Summary**: SENSEX trades **Wed (1 DTE) & Thu (0 DTE)**. NIFTY trades **Mon (1 DTE) & Tue (0 DTE)**. Friday is an automated sleep day. Engine dynamically determines active symbol and DTE from actual system date/time.

### 8. Testing Phase Discipline
* Currently testing consistency and pipeline stability with current available account funds without manual top-ups.
* **Do NOT top up live or paper funds.**
* **Scale-Invariance Mandatory**: Spot distances and thresholds must be normalized via Basis Points (bps) or percentage of spot. Never hardcode fixed points.

---

## 🧠 MANDATORY FIRST-PRINCIPLES QUANT REASONING PROTOCOL (FOR AI AGENTS)

Before writing or modifying ANY strategy, gatekeeper, or execution code, every AI agent MUST perform and briefly state a **5-Point Pre-Flight Quant Audit**:
1. **Unit & Lifecycle Parity (No Apples-to-Oranges)**: Verify exact units between forming bars (`_curr_1m_bar`) and closed bars (`bars_1m`). Remember: `_curr_1m_bar.volume` counts spot ticks during the minute and only becomes `opt_total_vol` at `_on_1m_bar_close`. Always use `self._opt_flow_1m` for in-flight option volume!
2. **Deque Saturation & Indexing Safety**: `self.bars_1m` has `maxlen=400` and is full at 09:14 AM after seeding. `len(self.bars_1m)` stays constant at `400`. NEVER use `len(self.bars_1m)` for bar age or time elapsed—always use monotonic `self._total_bars_seen` (`seq_idx`) or epoch timestamps (`ts_start`).
3. **End-to-End Pipeline Trace**: A trigger in `price_action_engine.py` must survive downstream gates. Always trace how a new/modified signal flows through:
   `PriceActionEngine` -> `WyckoffVolumeEngine` (fast-path needed for intra-bar?) -> `CompositeConvictionEngine` (score >= 80?) -> `AegisExecutionManager` (Last-Mile Hygiene & Slot Rollback).
4. **Scale-Invariance & Synthetic Test Compatibility**:
   - Never hardcode point values (NIFTY is 24k, SENSEX is 74k). Use `k * ATR_1m` or `bps` (`0.0008 * Spot`).
   - Unit tests often feed synthetic OHLC bars with `opt_total_vol == 0`. All option-flow/delta vetoes must gracefully bypass when `opt_total_vol <= 0`.
5. **Strict Pytest Frugality**:
   - `.md` docs, comments, log analysis -> **Run 0 tests**.
   - Single module edit -> **Run ONLY that module's targeted test file** (takes ~2s).
   - Full test suite -> **ONLY on multi-module core changes or pre-deployment**.

---

## CODING DISCIPLINE (applies to all active Python)
- **Pytest Execution Governance (Smart, Targeted, and Frugal)**:
  - **NEVER run pytest for non-engine tasks**: Do NOT run pytest when editing documentation (`.md`), writing or running analysis scripts (`scratch/`, `scripts/`), modifying data/JSON files, editing UI/HTML artifacts, or answering user questions/inspecting logs. Running tests for these wastes user time and burns tokens unnecessarily.
  - **Single-Module Edits $\implies$ Targeted Tests ONLY**: If you edit a single module, run ONLY its corresponding test file (e.g., `python -m pytest tests/test_shadow_boundary_observer.py -q`). It finishes in 2 seconds. Do NOT run the entire suite.
  - **Full Test Suite (`python -m pytest tests\ -q`) $\implies$ Core Multi-Module Changes ONLY**: Run the full suite only when refactoring shared core infrastructure (`common/execution/`, `common/market/`), modifying multi-module interfaces, or before a major production deployment.
  - **Suite Cleanliness**: The test suite must remain green when tests are appropriately run. As of 2026-08-16, Kronos is decommissioned; the obsolete `test_kronos_settings_use_kronos_package_paths` has been removed and shared-library fallback branches in `common/` are dormant. If a kronos-related test failure ever appears, remove the obsolete reference—do NOT revive Kronos code.
- Hot-path modules (`value_engine.py`, `tick_compiler.py`, `on_tick` handlers) must not
  import `pandas`/`scipy`/`sklearn`. Heavy libs are allowed only off-hot-path
  (e.g. `l2_archiver.py` 15-min flush).
- **Scale-Invariance & Normalization Mandatory**: NEVER hardcode fixed point thresholds
  (e.g. `15.0 pts`, `8.0 pts`) for index calculations. NIFTY (24k), SENSEX (80k),
  BANKNIFTY (51k), and FINNIFTY (23k) operate on radically different point scales. ALL
  spot distances, triggers, invalidations, and confirmation thresholds must be
  normalized via Basis Points (bps, where 1 bps = 0.01% of spot), percentage of spot,
  or localized volatility multiples ($k \times \text{ATR}_{1m}$).
- Single source of truth for symbols: `common/symbols.py` (`SUBSCRIPTION_UNIVERSE`,
  `NIFTY100`). Do not hardcode symbol lists elsewhere.
- Never modify Shoonya/Noren API assumptions lightly; see harness doc §1.1.1
  (weekday-only, static IP, single session, ~24h expiry). See §SHOONYA below for the
  weekend rule.

---

## ⚡ VERSION CONTROL & CHANGE TRACKING (JUJUTSU / JJ PROTOCOL MANDATORY)

> **Jujutsu (`jj`) is the STRICT and ONLY version control CLI for all AI agents and contributors in this repo.**
> Never use `git add` or `git commit`. All operations must use `jj`.

### 1. Pure Automatic Snapshots (No Manual Commits During Work)
- **Continuous Background Auto-Snapshotting**: Jujutsu automatically snapshots every single file modification directly into the current working copy (`@`).
- **Never Run Commit Commands While Working**: Do NOT run `jj commit` or any commit command after editing files. Work freely—Jujutsu captures all edits automatically without any ceremony.
- **No Staging / No Staging Area**: Concept of staging (`git add`) does not exist in Jujutsu. Your workspace is always clean and tracked.

### 2. Pushing Completed Work to GitHub Remote (`jj git push`)
- Only when a feature or task is completely finished, tested, and ready to send to GitHub remote:
  ```powershell
  jj describe -m "feat(AEGIS): <concise summary>"
  jj bookmark set main -r '@'
  jj git push
  ```

### 3. Instant Rollback & History Audit (`jj undo`)
- **Instant Rollback**: If an operation creates unintended changes or breaks a test, execute:
  ```powershell
  jj undo
  ```
- **Operation History**: Inspect operational timeline with:
  ```powershell
  jj op log
  ```

---

## SHOONYA / NORENAPI RULES (broker seam)

- **Mandatory 90-Day Password Rotation Schedule**:
  - Shoonya enforces an involuntary password reset every 90 days.
  - **Last Rotated Date**: `08 October 2026` (Active: `Freedom@2026`).
  - **Next Mandatory Rotation Deadline**: `31 December 2026`.
  - **Human Ownership**: The user maintains a personal alarm/reminder and rotates this manually via the Shoonya web portal. AI agents must NEVER attempt automated credential rotation, scraping, or brute-force logins.
  - **When user rotates password**: Update **ONLY** the single canonical root `.env` (`c:\Users\Amarjeet Singh\quant-bot\.env` locally and `/home/opc/.env` on VPS). Clear cached session files (`data/shoonya_session.json` and `tokens_cache.json`) to trigger automatic fresh OAuth login.
- **Single Root `.env` Architecture (Engine-Level `.env` Strictly Prohibited)**:
  - There is exactly **ONE** `.env` file per environment:
    - Local: `c:\Users\Amarjeet Singh\quant-bot\.env`
    - VPS: `/home/opc/.env` (and repo root `/home/opc/quant-bot/.env`)
  - **Strict Prohibition**: Engine subdirectories (such as `AEGIS/.env`, `ARGUS/.env`, `astra_bot/.env`) are **STRICTLY PROHIBITED**.
  - **Root Cause & Rationale**: On 2026-10-08, a legacy `/home/opc/AEGIS/.env` overrode the root `.env` with stale credentials, causing repeated broker auth rejections and account lockout. All engines (including `AEGIS/config/settings.py`) load solely from repo root / `/home/opc/.env`.
- **Weekend offline is normal.** The Shoonya WebSocket drops on Saturdays/Sundays and
  NSE holidays. A Sat/Sun 'no tick' or WS close is **not a bug** — do not file it,
  add alerts for it, or build weekday-specific reconnect logic for it. Reconnect /
  exponential backoff only matters on a *trading* day (see engine harness docs).
- **Static egress IP mandatory** on the VPS.
- **Single session / single credential set**; tokens expire in ~24h; refresh
  `tokens_cache.json` weekly.
- **No paper/sim mode** — noise-test off live ticks or history, not simulation.
- **History burst** ~10 req/s; websocket carries L1 + top-5-level L2 depth only.

---

## VPS & OPS ACCESS (LEAN PRODUCTION DEPLOYMENT MANDATE)

> **VPS Lean Deployment Rule**: ONLY active, production-critical runtime files are allowed on the VPS. All tests, test data, scratch experiments, historical archives, and forensics documentation MUST stay strictly on the local development machine. Never deploy or leave non-runtime files on the VPS.

- **Host:** `137.23.41.38` (Oracle Cloud, Ubuntu). **User:** `opc`. Repo on VPS: `/home/opc`.
- **Allowed on VPS (Runtime Only)**:
  - `AEGIS/` (production options engine)
  - `unified_dashboard/` (monitoring UI)
  - `common/` (broker adapters, IPC, runtime paths, symbols)
  - `ecosystem.config.js` (PM2 process manifest)
  - `.env` (root credentials only — never in subdirectories)
- **Forbidden on VPS (Local-Only)**:
  - `tests/` (unit tests run locally before deployment)
  - `scratch/` (transient workspace — must remain 100% empty on VPS)
  - `*_archive/` or any `.tar.gz` backup dumps (zero archives on VPS)
  - Heavy ML datasets, raw parquets, or forensic notebooks
- **SSH key location (local):** repo-root `Oracle SSH keys/ssh-key-2026-07-11.key`. It is the ONLY copy — never replicate it into an engine subfolder.
- **SSH command:** `ssh -i "Oracle SSH keys/ssh-key-2026-07-11.key" opc@137.23.41.38`
- **Process manager:** PM2, config `ecosystem.config.js` — active apps: `aegis-bot`, `unified-dashboard`.
- **Deploy rule:** Deploy only after 15:30 IST or before 09:15 IST.

---

## REPOSITORY LAYOUT & ZERO-ENTROPY HYGIENE ENFORCEMENT

> **Zero-Entropy Mandate**: The codebase must remain completely free of transient clutter, scratch files, one-shot diagnostics, and virtual environment leakage. Only core production code, validated unit tests, and canonical configs are committed.

Rules every AI agent and contributor must uphold:

- **Immediate Scratch & Temp Cleanup**: Any diagnostic or scratch script created in `scratch/` or `scripts/` MUST be deleted immediately after its purpose is served. Never leave temporary scripts (`check_*.py`, `patch_*.py`, `test_*.json`, `temp_*`) behind.
- **No Virtual Environment or Binary Package Tracking**: `venv/`, `.venv/`, `.pytest_cache/`, `*.pyc`, and `.coverage` are permanently ignored via `.gitignore` and must NEVER enter Jujutsu or Git tracking.
- **No Temp / Scratch Files at Repo Root**: Scratch goes in `scratch/` (which is gitignored). Never place `temp.json`, `scratch_state.json`, or ad-hoc log files at repo root.
- **No One-Shot Debug Scripts in `scripts/`**: `scripts/` is reserved ONLY for permanent operational tools (e.g. `trace_trade_pipeline.py`). Ad-hoc patch scripts must be executed in `scratch/` and promptly deleted.
- **Pristine Jujutsu Working Copy**: Before concluding a session, verify that `jj st` shows a zero-entropy state (no stray files, no untracked noise).
- **SSH key only at repo root** (`Oracle SSH keys/`), never inside an engine dir.
- **No engine-level `.env` files.** Credentials live ONLY in the root `.env` (locally) or `/home/opc/.env` (VPS). Never place `.env` inside `AEGIS/`, `ARGUS/`, or any engine subdirectory.
- **Engine files stay in their engine.** A file used by only one engine lives in
  that engine's folder; cross-engine code goes through `common/`.
- **Legacy / backup copies** go in the relevant engine's `archive/` (e.g.
  `astra_bot/archive/`, `stock_order_flow/archive/`) or root `_archive/` — never
  loose alongside live engine files.
- **Logs live in the engine's `logs/`** (e.g. `AEGIS/logs/aegis.log`),
  never loose at root or in the engine top-level.
- **Stray engine dirs from dead engines:** `kronos_bot/` was deleted on 2026-10-25
  (empty husk; full history in `_archive/kronos_bot/`). Empty `astra_bot/` +
  `kronos_bot/` shells may reappear after test runs — a mkdir side-effect of the
  protected legacy branch in `common/runtime/paths.py` (`get_runtime_paths("kronos")`).
  They are gitignored — never commit or populate them.

---

## WHEN IN DOUBT
- Trust the harness doc (v3.5) over older docs; older docs can be deleted when stale.
- Kronos is dead. If a change re-introduces Kronos, it is wrong by definition.
- Ask the human before deleting shared-library kronos branches in `common/`.
