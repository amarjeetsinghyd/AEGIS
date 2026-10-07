# AI AGENT INSTRUCTIONS — quant-bot repo

> **Read this before making any change in this repository.**
> Applies to every AI coder (Cursor, Antigravity, Devin, deepseek-harness, etc.) and every human contributor.

---

## DECOMMISSIONED COMPONENT — DO NOT REVIVE

### `kronos_bot` is SHUT DOWN (decommissioned)

**Kronos is no longer an active engine in this repository.** Treat it as legacy/abandoned.

**Do NOT, under any circumstance:**
- Re-create `kronos_bot/__init__.py`, `kronos_bot/config/`, or `kronos_bot/config/settings.py`.
- Add `kronos_bot` back into `unified_dashboard` UI, brand lines, titles, or `CONSOLE_HTML`.
- Add `kRONOS`, `Kronos`, or any kronos-marketing copy to dashboards, docs, or configs.
- "Fix" failing tests by re-introducing kronos code, stubs, or packages.
- Migrate kronos logic into `astra_bot` or `stock_order_flow` unless explicitly told otherwise.

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

1. **`AEGIS/`** (and `astra_bot/`) — **PRIMARY ACTIVE ENGINE (Core Focus)** — **AEGIS** (**A**utonomous **E**xecution & **G**amma **I**mbalance **S**ystem) — options strategy bot (Production Version 1.0 / v1.0 Locked).
   Architecture authority: `AEGIS/AEGIS_V3_ARCHITECTURE.md` (Production Version 1.0 Locked).
   - **Primary Focus**: All current development, telemetry review, and trading logic verification center on AEGIS.
   - **Architecture & Rules**: Conform all edits to `AEGIS/AEGIS_V3_ARCHITECTURE.md` (Macro Price Action Primary Trigger across 4 Pillars [P1 20 EMA Pullback, P2 Liquidity Sweep & Reclaim, P3 Volatility Squeeze Breakout, P4 0-D Prominence Hub Breakout], Parallel Wyckoff Volume & Order Flow Delta Gate, Continuous Composite Conviction Scoring with >=80.0 threshold, Strict 1-Lot Maker TP at +4.0%, 100% Shadow ML & Standalone CatBoost Paper Trader, Volume Profile & Auction Market Theory Engine, Pain Zone Band (v1.0 Production), Pre-Trade Abort Slot Rollback, and 15:08:00 IST CAS Force Flatten).
   - **Testing / Consistency Phase**: Currently in consistency testing / validation mode. We are validating pipeline health, signal consistency, Wyckoff delta harmony, and telemetry without topping up paper/live funds.
2. **`ARGUS/`** — **ARGUS** (**A**lgorithmic **R**eal-time **G**ranular **U**ltra-microstructure **S**calper) — HFT microstructure equities engine.
   - **Status**: **SECONDARY / DEPRIORITIZED (Ignore for now)**. We are NOT focusing on ARGUS at this stage. Treat as secondary/dormant unless explicitly directed.
3. **`unified_dashboard/`** — monitoring UI, dual-brands: Quant Console — AEGIS × ARGUS.

Shared library: `common/` (broker / execution / market / ipc / runtime / config / symbols).

---

## AEGIS CANONICAL ARCHITECTURE & PERMANENT OPERATIONAL RULES (MANDATORY IN EVERY CHAT)

> **Every AI agent starting a new conversation MUST honor these exact production rules:**

### 1. Macro Price Action 4 Pillars (Primary Triggers)
* **Pillar 1 (`20_ema_rejection`)**: Trend continuation bounce off the 20 EMA with open runway.
* **Pillar 2 (`micro_sweep_reclaim`)**: Liquidity sweep and spring reclaim beyond key levels.
* **Pillar 3 (`volatility_squeeze_breakout`)**: Bollinger / Keltner volatility compression expansion box breakout.
* **Pillar 4 (`hvg_network_phase_shift`)**: Topological Prominence Hub / High Volatility Gap network phase shift.

### 2. Absolute Pain Zone VWAP Band (v1.0 Production Early Veto)
* Single unified veto before pillars:
  * Spot **ABOVE** Upper Band $\implies$ **ONLY CALL** trades allowed.
  * Spot **BELOW** Lower Band $\implies$ **ONLY PUT** trades allowed.
  * Spot **INSIDE** Band $\implies$ Discarded / Neutral zone.

### 3. Parallel Wyckoff Volume & OFI Delta Gate
* Volume-to-Dispersion Ratio ($VDR \ge 1.0$) and positive order-flow intensity (OFI) delta harmony required to confirm institutional participation.

### 4. Strike Selection & Subscription Universe (15 OTM Depth)
* **Subscription Universe**: ATM $\pm 15$ strikes (31 strikes total, 62 tokens CE+PE) tracked on WebSocket ($t+d$).
* **Low-Capital Testing Mode (`funds < 5000`)**: `StrikeSelector` scans all 15 OTM strikes (`TESTING_DEEP_DEPTH = 16`). Automatically selects the closest affordable OTM strike for 1 lot (e.g. ₹15–₹28).
* **Index Option Liquidity**: Major Indian index options (NIFTY/SENSEX) have massive liquidity (millions of shares). **NEVER** apply artificial 2% spread blocks or thin-ask blocks.

### 5. Adaptive Exits & Near-Target Profit Lock (v1.0 Production Locked)
* **Hard Disaster Floor**: `-12.0%` option premium circuit breaker.
* **Step 1 (Safety Net)**: Peak $\ge +3.0\% \implies$ SL compressed from -12% to `-3.0%` (wide breathing room; premature +2.0% choke permanently removed).
* **Step 2 (Cost-Shield / Breakeven)**: Peak $\ge +3.5\% \implies$ SL compressed to `0.0%` (Entry Price scratch).
* **Pillar 4 Spot Invalidation**: CALL exits if Spot $\le$ Entry Candle Low; PUT exits if Spot $\ge$ Entry Candle High (immunizes trade from option spread/wick noise).
* **Near-Target Profit Lock (`NEAR_TARGET_TRIGGER_PCT = 0.035`, `NEAR_TARGET_PULLBACK_TOLERANCE = 0.008`)**:
  * Peak Bid $\ge +3.5\% \implies$ **Exits immediately on $\ge 0.8\%$ pullback from peak bid**!
* **Strict 1-Lot Maker TP**: Resting Limit Order at **`+4.0%`** (100% quantity).
  * **Target Discipline Mandate (Never Expand to +5%–6%)**: +4.0% is the mathematically optimal safe harbor that fills passively within 10–40s across choppy regimes. Option momentum beyond +4% frequently faces sharp institutional dumps (e.g. crashing back below entry immediately after reaching +8%). Never greed for 5%–6% as holding for bigger targets destroys the 84%+ win-rate.
  * **Exchange Tick-Size Rounding**: Option target prices are rounded to the nearest ₹0.05 tick (e.g. ₹8.10 entry rounds 8.424 to ₹8.40 = +3.70%), ensuring instant passive fill on the opening crest.
* **3-Minute Time Harvest**: Stagnation $> 180$s with $+1.5\%$ to $+2.5\%$ locks profit.
* **EOD CAS Flush**: 15:08:00 IST force flattens all open inventory.

### 6. Component Operational State (Live vs Shadow)
* **LIVE**: 4 Price Action Pillars, Absolute Pain Zone Band, Wyckoff Volume & OFI Gate, Composite Conviction Engine ($\ge 80$), Adaptive Exits, StrikeSelector.
* **SHADOW**: Volume Profile Engine (VAH/VAL/POC), CatBoost ML Gatekeeper, Standalone CatBoost Paper Trader, ShadowQuantTracker.
* **DECOMMISSIONED**: Gemini 3.8 / `ai_shadow_reviewer.py` is DELETED. Kronos is SHUT DOWN.

### 7. Trading Calendar & Weekly Engine Schedule
* **Monday**: **NIFTY** (**1 DTE**) [NFO, strike step 50, lot 65]
* **Tuesday**: **NIFTY** (**0 DTE** — Expiry Day) [NFO, strike step 50, lot 65]
* **Wednesday**: **SENSEX** (**1 DTE**) [BFO, strike step 100, lot 20]
* **Thursday**: **SENSEX** (**0 DTE** — Expiry Day) [BFO, strike step 100, lot 20]
* **Friday**: **NIFTY** (**4 DTE**) [NFO, strike step 50, lot 65]
* **Saturday & Sunday**: Market Closed (Shoonya WebSocket disconnect is normal, no bot trading).
* **Summary**: SENSEX trades **Wed (1 DTE) & Thu (0 DTE)**. NIFTY trades **Mon (1 DTE), Tue (0 DTE), & Fri (4 DTE)**. Today is Wednesday $\implies$ **1 DTE for SENSEX**.

### 8. Testing Phase Discipline
* Currently testing consistency and pipeline stability with current available funds (~₹588.36).
* **Do NOT top up live or paper funds.**
* **Scale-Invariance Mandatory**: Spot distances and thresholds must be normalized via Basis Points (bps) or percentage of spot. Never hardcode fixed points.

---

## CODING DISCIPLINE (applies to all active Python)
- Run `python -m pytest tests\ -q` before declaring work done. **The suite must be
  100% green.** As of 2026-08-16 the suite is fully green with Kronos decommissioned:
  the obsolete ``test_kronos_settings_use_kronos_package_paths`` test has been removed and the
  shared-library kronos branches in `common/` are kept as dormant fallbacks. If you ever
  see a kronos-related failure reappear, delete or trim the offending kronos reference —
  do NOT re-add kronos code to make it pass.
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

## SHOONYA / NORENAPI RULES (broker seam)

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

## VPS & OPS ACCESS

- **Host:** `137.23.41.38` (Oracle Cloud, Ubuntu). **User:** `opc`. Repo on VPS: `/home/opc`.
- **SSH key location (local):** repo-root `Oracle SSH keys/ssh-key-2026-07-11.key`.
  It is the ONLY copy — never replicate it into an engine subfolder (e.g. do **not**
  copy it into `ARGUS/`).
- **SSH command:** `ssh -i "Oracle SSH keys/ssh-key-2026-07-11.key" opc@137.23.41.38`
- **Process manager:** PM2, config `ecosystem.config.js` — active apps: `aegis-bot`,
  `argus`, `unified-dashboard`, `l2-archiver`, `eod-calibrator`.
- **Deploy rule:** restart `shoonya-data-hub` *before* `astra-bot`; deploy only after
  15:30 IST or before 09:15 IST.

---

## REPOSITORY LAYOUT ENFORCEMENT

Canonical layout is documented in [`SYSTEM_ARCHITECTURE.md`](SYSTEM_ARCHITECTURE.md) §8.
Rules every change must uphold:

- **No temp / scratch files at repo root.** Scratch goes in `scratch/`. No
  `temp.json`, `scratch_state.json`, `scratch_state_pretty.json`, or log files
  at the repo root.
- **No one-shot debug scripts at repo root.** Diagnostic/ops scripts go in
  `scripts/`. Scripts that patch a specific trade, a specific installed
  package on the VPS, or output into a tool's private brain are deprecated
  one-shots — delete them, do not commit new ones.
- **SSH key only at repo root** (`Oracle SSH keys/`), never inside an engine dir.
- **Engine files stay in their engine.** A file used by only one engine lives in
  that engine's folder; cross-engine code goes through `common/`.
- **Legacy / backup copies** go in the relevant engine's `archive/` (e.g.
  `astra_bot/archive/`, `stock_order_flow/archive/`) or root `_archive/` — never
  loose alongside live engine files.
- **Logs live in the engine's `logs/`** (e.g. `astra_bot/logs/astra_bot.log`),
  never loose at root or in the engine top-level.
- **Stray engine dirs from dead engines:** the live `kronos_bot/` directory (only
  empty `data/` + `logs/`) is left to rot and not committed as a package —
  see §DECOMMISSIONED above.

---

## WHEN IN DOUBT
- Trust the harness doc (v3.5) over older docs; older docs can be deleted when stale.
- Kronos is dead. If a change re-introduces Kronos, it is wrong by definition.
- Ask the human before deleting shared-library kronos branches in `common/`.
