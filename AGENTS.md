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

## ACTIVE ENGINES (the only ones you should build on)

1. **`stock_order_flow/`** — **ARGUS** (**A**lgorithmic **R**eal-time **G**ranular **U**ltra-microstructure **S**calper) — HFT microstructure equities engine (primary active system).
   Architecture authority: `stock_order_flow/AGENTIC HARNESS DOCUMENT v3.5.md` (v3.6).
   Conform all Python edits to that document (init order, conviction trigger, adverse selection shield,
   Kyle's lambda limit engine, Kelly sizing, 4-pillar exits, zero-copy tick path, no heavy imports in tick handlers).
2. **`AEGIS/`** (and `astra_bot/`) — **AEGIS** (**A**utonomous **E**xecution & **G**amma **I**mbalance **S**ystem) — options strategy bot (active).
3. **`unified_dashboard/`** — monitoring UI, dual-brands: Quant Console — ARGUS (Algorithmic Real-Time Granular Ultra-Microstructure Scalper) × AEGIS.

Shared library: `common/` (broker / execution / market / ipc / runtime / config / symbols).

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
  copy it into `stock_order_flow/`).
- **SSH command:** `ssh -i "Oracle SSH keys/ssh-key-2026-07-11.key" opc@137.23.41.38`
- **Process manager:** PM2, config `ecosystem.config.js` — 3 apps: `astra-bot`,
  `stock-order-flow`, `unified-dashboard`.
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
