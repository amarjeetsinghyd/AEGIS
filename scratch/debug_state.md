# Debugging State — quant-bot repo
_Last updated: 2026-08-16 (post-cleanup, suite fully green)_

## AUTHORITATIVE POLICY
- See `AGENTS.md` at repo root. It governs all AI/human contributors.
- **kronos_bot is decommissioned.** Never re-add it, stub it, or list it in dashboards/docs.
- pytest suite is **100% green** as of 2026-08-16; the obsolete kronos-package test has been
  removed (not skipped). No "expected failure" remains.

## Cleanup performed this session (consistent with AGENTS.md)
- DELETED kronos stub packages (`kronos_bot/__init__.py`, `config/__init__.py`,
  `config/settings.py` + pycache). Only auto-created `kronos_bot/data` and `kronos_bot/logs` remain.
- REVERTED `unified_dashboard/dashboard.py` brand line (removed `x KRONOS` I wrongly added).
- TRIMMED `tests/test_combined_dashboard.py` (removed `assert "KRONOS" in CONSOLE_HTML`).
- REMOVED `test_kronos_settings_use_kronos_package_paths` from `tests/test_bot_path_wiring.py`.
- "Wrote root policy `AGENTS.md`; updated it to record the fully-green-suite state.

## Kept per human decision (2026-08-16)
- Legacy kronos branches in `common/runtime/paths.py` and `common/ipc/live_state.py` are
  LEFT UNTOUCHED as safe fallbacks. `tests/test_shared_runtime_paths.py` hits those branches
  and passes without the deleted `kronos_bot` package. Do not clean unless the human says so.

## Confirmed healthy (no action needed)
- stock_order_flow/ core modules py_compile clean; architecture matches harness doc v3.5.
- value_engine.py / tick_compiler.py: no heavy hot-path imports (§1.2.3 compliant).
- l2_archiver.py pandas: only in 15-min flush, not tick handler — compliant.

## Remaining real work (NOT kronos, NOT test failures)
- ✅ MN-4 — TickCompiler WS reconnect/backoff **FIXED 2026-08-16**: `on_ws_close` + `_reconnect_loop` (exp backoff 1→30s) re-issues `subscribe`; `ShoonyaMarketDataProvider.add_close_listener` wired to Noren `socket_close_callback`; trading-day-gated (`is_trading_day` skips weekend/holiday drops).
- ✅ MN-6 — execution_manager.py breakeven gap detection **FIXED 2026-08-16**: `GAP_ATR_MULT` (1.5×ATR) guard in `manage_open_trades` skips the breakeven floor lock on single-tick gaps past the prior extreme, treating the gap as an exit signal not a floor.
- Bug audit now 23/23 fixed, 0 remaining (harness doc §13). pytest 4 passed green. Deploy blockers cleared.

---

## Reorg pass (2026-08-16, continued) — architecture map + cleanup

### Docs authored / refreshed
- REWROTE root `SYSTEM_ARCHITECTURE.md` (115 -> ~180 lines): cross-engine map
  (Oracle VPS, PM2, unified Shoonya WS adapter, `common/` table, unified
  dashboard), 3-engine table with Engine 1 = stock_order_flow, Engine 2 =
  astra_bot, Engine 3 (Kronos) = DECOMMISSIONED w/ link to AGENTS.md. Links
  into each engine's local harness doc (See Engine docs table). Shoonya §4
  weekend rule + §7 VPS creds/SSH key at repo root + §8 canonical layout.
- RENAMED `astra_bot/ASTRA_V2_ARCHITECTURE.md` -> `astra_bot/AGENTIC HARNESS
  DOCUMENT Astra v4.3.md` (filename now matches its V4.3 title; no refs to old
  name existed in .md/.py/.js). This is the **authoritative Engine 2 harness doc**.
- UPDATED `stock_order_flow/AGENTIC HARNESS DOCUMENT v3.5.md` §14: resolved
  `<VPS_IP>` placeholder to `137.23.41.38` (L664, L667) and clarified L651
  that the SSH key lives at the project repo root.
- REWROTE root `README.md` (it referenced dead `ARCHITECTURE.md` /
  `SEMANTIC_MAP.md` / `shoonya_data_hub/` / active Kronos). New version
  points to AGENTS.md + SYSTEM_ARCHITECTURE.md + both engine harness docs.

### AGENTS.md sections added
- "SHOONYA / NORENAPI RULES" — weekend-offline-is-normal rule (Sat/Sun WS drop
  != bug; reconnect only matters on a trading day).
- "VPS & OPS ACCESS" — host/user/repo, SSH key at repo root, exact ssh cmd,
  PM2 config + deploy-order rule.
- "REPOSITORY LAYOUT ENFORCEMENT" — no temp/scratch at root, no one-shot debug
  scripts at root, SSH key only at root, engine files stay in engine, legacy/
  backup -> engine `archive/` or root `_archive/`, logs in engine `logs/`.

### Loose-file cleanup at repo root (aggressive, user-approved 2026-08-16)
- DELETED root garbage: `temp.json`, `scratch_state.json`,
  `scratch_state_pretty.json`, `astra_bot.log`, and one-shot VPS debug
  scripts `analyze_astra.py` (wrote to .gemini brain), `fix_trades.py`
  (single-trade patcher), `patch_noren.py` (patches installed VPS package);
  also `stock_order_flow/desktop.ini` Windows junk.
- MOVED to `scripts/`: `check_otm_prices.py`, `dep_check.py`, `scan_debug.py`
  (diagnostic, kept for reuse).
- MOVED to `_archive/`: `deployment_audit_report.md` (situational audit).

### Environment facts confirmed
- SSH key at repo root: `Oracle SSH keys/ssh-key-2026-07-11.key` (+ `.pub`).
  It was already at root (summary's "buried in stock_order_flow/" was stale);
  only doc references needed resolving.
- live `kronos_bot/` (root) holds only empty `data/` + `logs/` — left to rot,
  never re-created as a package.

### Test gate
- `python -m pytest tests\ -q` => **4 passed in 0.65s (exit 0)** after all
  reorg/cleanup. Suite stays 100% green.
