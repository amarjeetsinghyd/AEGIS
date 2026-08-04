# AI / Agent Change Log

Living memory for Cursor agents working on **quant-bot**.  
Canonical architecture lives in [`ARCHITECTURE.md`](ARCHITECTURE.md).  
Order/broker rules: [`shoonya_data_hub/docs/SHOONYA_ORDER_RULES.md`](shoonya_data_hub/docs/SHOONYA_ORDER_RULES.md).

**Rule for agents:** after any non-trivial change (architecture, execution, VPS ops, safety), append a dated entry here. Do not revive deleted `ai_guide.md` / old `changelog.md` / roadmap files.

---

## 2026-08-04

### Architecture doc expansion
- `ARCHITECTURE.md` rewritten as long-form AI source of truth (§0–§25): agent rules, Friday incident, Shoonya/CAS, shared kernel, Astra coexistence, Kronos full path (live WS + EOD 5m synth vol, EM, model), hub/shm schemas, dashboard, capital/strikes, VPS ops, failure modes, env catalogue, glossary, open checklist, reading order.
- Flatten time corrected in changelog Kronos notes to **15:10**. Synced to VPS `/home/opc/`.

### CAS flatten + SENSEX window readiness
- Explained force-flatten: close any open Kronos option by a hard clock so it cannot hold overnight (the Friday gap root cause).
- Moved Kronos force-flatten **15:15 → 15:10** for new Closing Auction Session (CAS) house rule.
- Fixed candle store: per-index parquet (`underlying_candles_NIFTY/SENSEX.parquet`), backfill uses correct token for `target_index`, lookback 14d toward **512** bars, `amount = mean(OHLC)×volume`.
- Supertrend(10,3): classic Wilder ATR + band logic confirmed correct; float compare hardened with `np.isclose`.

### Synthetic constituent volume (critical fix)
- Root cause: live `kronos_bot/core/data_fetcher.py` used fake `volume=10_000_000`; full engine lived unused in `execution/data_fetcher.py`.
- Live: hub WS LTQ → 5m closed bucket → Kronos at :02 REST OHLC + `amount=mean×vol` → parquet.
- EOD/backfill: index REST **5m OHLC** + constituent REST **5m volumes** summed (not 1m). Both indices at 15:31.
- Config: `PREDICTION_THRESHOLD=0.0010` (0.10%) aligned in settings + main.

### Shoonya IP whitelist documented
- `SHOONYA_ORDER_RULES.md` + `ARCHITECTURE.md` §3.0: Shoonya API is **VPS-only** (static IP `137.23.41.38` whitelisted). Never run Shoonya login/quotes/orders from local IP for testing.

### Dashboard: trade history + UI clarity
- Kronos lane moved to **left**, Astra to **right**.
- Kill switch UI/API removed from Quant Console (travel-era control).
- **Trade history** panel below telemetry with Kronos/Astra switch; reads `*_bot/data/trades/trade_history.json` on disk (not shm).
- Signal label: raw `NEUTRAL` / idle shown as **Waiting** (no CALL/PUT yet).
- Funds relabeled **Paper funds** — from each engine’s `capital_state.json` on disk; start `INITIAL_PAPER_CAPITAL=₹2500`. Kronos ₹2092.10 is remaining paper wallet after prior paper P&L, not broker cash.

### Kronos-only execution (Friday overnight gap closed)
- **Incident:** Kronos shared Astra/KDO `ExecutionManager` exits. Friday ~15:00+ PUT kept NRML overnight; DAY TP cancelled at EOD (Shoonya has **no GTC**); weekend gap-up + expiry → near-total loss.
- **Fix:** `kronos_bot/execution/kronos_execution_manager.py` — separate from KDO.
  - Entry window **09:20–14:50**
  - Force flatten **≥ 15:10** (CAS house rule)
  - Resting **+10% DAY LMT TP**
  - Exit: **5m underlying Chandelier** + **600s** time stop
  - **No** premium hard SL; **no** KDO flow/OI exits
- Wired in `kronos_bot/main.py`. Astra keeps shared KDO EM in `common/`.

### Docs consolidation
- Deleted stale Aug-2 / Jul docs: root `ai_guide.md`, `changelog.md`, `future_roadmap.md`, `HANDOVER.md`, `runtime_audit_report.md`, `project_tree.txt`, and duplicate `shoonya_data_hub/docs` architecture/changelog/roadmap/ai_guide trees.
- Single architecture source: root `ARCHITECTURE.md`.
- Shoonya rules kept under `shoonya_data_hub/docs/SHOONYA_ORDER_RULES.md`.
- Official SDK reference: `shoonya_data_hub/reference/ShoonyaApi-py`.

### Model package location
- Root `model/` (Aug 2 duplicate) removed; sole package is `kronos_bot/model/`.
- Fixed `kronos_bot/model/kronos.py` to `from .module import *` (no `sys.path` / root `model` dependency).
- Removed orphaned KDO `microstructure_engine.py` copies from `kronos_bot/` (Astra uses `astra_bot/core/microstructure_engine.py` only).

### Docs / folder reorg (local + VPS)
- Created this file (`AI_CHANGELOG.md`) as the maintained agent log; slim `README.md` points here + `ARCHITECTURE.md`.
- Deleted stale Aug-2 era: `ai_guide.md`, `changelog.md`, `future_roadmap.md`, `HANDOVER.md`, `runtime_audit_report.md`, `project_tree.txt`, `deploy_bundle.tar`, one-off test scripts.
- Cleared duplicate architecture markdown under `shoonya_data_hub/docs/` (kept `SHOONYA_ORDER_RULES.md` + short README).
- Scratch/research dumps removed (`data/scratch`, duplicate `*/data/reports`).
- Canonical `institutional_memory/constituents.json` at repo root; duplicate copies under engines removed.
- Same cleanup mirrored on VPS; `HANDOVER_AND_DEBUG_GUIDE.md` removed there.

### VPS hygiene
- Killed 4 ghost processes (`/tmp/test_tokens.py`, `/tmp/test_q.py`) running since Jul 29.
- Removed Chrome RPM, `kronos_bot.zip`, old `quant_bot/` tree, scratch/bak/legacy files, `/tmp` AI scratch + Chromium temps.
- Live stack only: `shoonya-data-hub`, `astra-bot`, `kronos-bot`, `unified-dashboard` (paper).

### Modular layout (earlier same day)
- `common/` shared broker/execution/market/ipc; thin engines; light dual-brand dashboard; hub writes `/dev/shm/shoonya_live_ticks.json`.
- Kronos model load without HuggingFace `transformers` (local `Kronos` / `KronosTokenizer`).

---

## Template for future entries

```
## YYYY-MM-DD
### Short title
- What changed and why
- Files / VPS impact
- Safety notes (paper/live, flatten rules, etc.)
```
