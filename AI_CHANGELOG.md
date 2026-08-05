# AI / Agent Change Log

Living memory for Cursor agents working on **quant-bot**.  
Canonical architecture: [`ARCHITECTURE.md`](ARCHITECTURE.md).  
Code navigation: [`SEMANTIC_MAP.md`](SEMANTIC_MAP.md).  
Order/broker rules: [`shoonya_data_hub/docs/SHOONYA_ORDER_RULES.md`](shoonya_data_hub/docs/SHOONYA_ORDER_RULES.md).

**Rule for agents:** after any non-trivial change (architecture, execution, VPS ops, safety), append a dated entry here. Do not revive deleted `ai_guide.md` / old `changelog.md` / roadmap files.

---

## 2026-08-05 (full day precision updates)

### Idea parked: Kronos Supertrend on 1m (EOD only)
- Operator compared TV 1m vs 5m ST(10,3): 1m flips earlier / less lag on turns. Proposal: keep Kronos **5m** prediction; gate with **1m** ST from index WS LTP → 1m OHLC (hub/Astra tick path already exists).
- **Do not implement mid-session.** Queued in [`IDEAS.md`](IDEAS.md) **K-ST-1m** + ARCHITECTURE §24. Revisit after 16:00 / next premarket; validate with `decision_journal.jsonl` before live money.

### Adaptive Noise Floor & Hybrid Exits (Astra Bot)
- **Problem**: The Astra bot was experiencing rapid "flickering" exits due to microscopic pullbacks treating zero-crossings of the noisy ABG-filtered signal as valid exit events. Furthermore, momentum trades were being killed due to a blind 30-second pause upon strike rollovers.
- **Solution (Noise Floor)**: Replaced static exit thresholds with a dynamic `AdaptiveNoiseFloor` using Median Absolute Deviation (MAD) of raw tick velocity over a 60-tick rolling window. Exits now require the signal to pierce a `1.0x` multiple of the live noise floor (Dynamic Hysteresis).
- **Solution (Kinematic Pause)**: Verified that the "Continuity-Preserving Micro-Bucket Ring Buffer" (already in place in `astra_bot/core/ring_buffer.py`) successfully rebases OI continuity offsets without flushing or pausing upon strike rollover.
- **Solution (Hard Stop)**: Replaced the 10-minute hard cap with a symmetrical 120-second (2-minute) hard cap time limit for scalping.
- **Files**: `astra_bot/core/microstructure_engine.py`, `common/execution/execution_manager.py`.
- **VPS Impact**: Safely pushed updated code via `scp` and restarted ONLY `astra-bot` via PM2, successfully preserving the live `kronos-bot` execution completely untouched.

---

## 2026-08-05 (full day rollup)

Session focus: Wednesday = **SENSEX**, paper mode. Docs + live path hardened so a token-exhausted / new AI can continue from files alone.

### Decision journal for paper ST analysis
- Every 5m prediction now appends to `kronos_bot/data/decisions/decision_journal.jsonl` (`kronos_raw`, `final_signal`, `st_blocked`, ST bias, delta, will_enter). Survives PM2 log rotation for multi-day paper review before live capital.
- File: `kronos_bot/main.py`. Restart Kronos only.



### Premarket standby + Wed index label
- Kronos logged **NIFTY** on Wednesday because `name` was cached once at process start (overnight from Tuesday). Hub already on **SENSEX**; LTP ~78428 was correct Sensex cash.
- Fix: refresh `get_active_instrument()` every loop + after deep-sleep wake; push `active_index` to live state.
- Premarket: after 08:55 handshake, sleep until **09:14:30** (no 30s “is market open?” spam).
- Astra: refresh index/step/weekly opts after overnight wake handshake.
- Files: `kronos_bot/main.py`, `astra_bot/main.py`.

### Synth vol / stuck spot (WS cash path)
- Not an API outage. REST LTP/vol fine. Hub used depth feed `d` + broken `_on_ws_tick` (`lp` vs `last_traded_price`) + ~300 option WS subs crowding cash.
- Fix: touchline `feed_type=t`, fix tick field map, REST spot refresh ~5s, re-sub cash if `live_accum=0`, cap options at 80, Kronos REST 5m synth-vol fallback when hub=0.
- Verified: spot moving, `live_accum` climbing; later bars e.g. 09:40 vol ~112k, 09:50 ~161k.
- Files: `common/broker/shoonya_adapter.py`, `shoonya_data_hub/main.py`, `kronos_bot/core/data_fetcher.py`.

### Inference `generator didn't stop after throw()`
- Real error: `calc_time_stamps` used `DatetimeIndex.dt` → `AttributeError`.
- Masked by `_no_grad_ctx` `except AttributeError` (meant only for missing `inference_mode`).
- Fix: DatetimeIndex-safe `calc_time_stamps`; safer `_no_grad_ctx`.
- Verified: e.g. `NEUTRAL (−0.082%, 78807 → 78742)`.
- Files: `kronos_bot/model/kronos.py`, `kronos_bot/strategy/signal_generator.py`.

### Corrupt morning candles + REST repair
- Live path: REST fail → flat `O=H=L=C=spot_ltp`; also called non-existent `get_historical_candles` (silent).
- 09:15–09:35 stuck ~78428 / vol 0; later bars had vol but flat OHLC until fixed.
- Disk repair: `rest_backfill_today_candles("SENSEX")` — **no restart required** for data-only.
- Code: use `historical_provider.get_historical(...)`; 4-step boundary logs (WS vol → REST OHLC → amount → window).
- Files: `kronos_bot/main.py`, `kronos_bot/core/data_fetcher.py`.

### −25% premium hard SL (Chandelier fallback)
- Chandelier alone can be messy (both lines above price). Added **−25% premium hard SL** ahead of Chandelier; still no KDO flow/OI exits.
- Exit order: **15:10 flatten → +10% TP → −25% SL → Chandelier → 600s**.
- File: `kronos_bot/execution/kronos_execution_manager.py` (`HARD_SL_PCT = 0.25`).
- ARCHITECTURE exit table + constants updated.

### Dashboard
- Kronos brand steel blue (off red); CALL whole tile green / PUT red.
- File: `unified_dashboard/dashboard.py`.

### Docs for AI handoff
- Created [`SEMANTIC_MAP.md`](SEMANTIC_MAP.md): process → folder → file → function → symptom triage.
- Refreshed [`ARCHITECTURE.md`](ARCHITECTURE.md) header/§0/§15/§24/§25 to link the map and lock −25% SL.
- This full-day rollup so chat history is not required.

### Amount / Kronos units (clarified, no code change)
- `amount = mean(OHLC) × volume` (index points × constituent shares). Model z-scores per window — consistency matters more than absolute INR vs CNY scale.

### VPS impact (session)
- Selective restarts: hub + Kronos (and dashboard when UI changed). Astra only when its code changed.
- Paper default unchanged.

---

## 2026-08-04

### Long-form architecture handoff
- `ARCHITECTURE.md` rewritten as AI source of truth (§0–§25): agent rules, Friday incident, Shoonya/CAS, shared kernel, Astra coexistence, Kronos full path, hub/shm, dashboard, capital/strikes, VPS ops, failure modes, env catalogue, glossary, open checklist, reading order.
- Flatten time in notes corrected to **15:10**. Synced to VPS `/home/opc/`.

### CAS flatten + SENSEX window readiness
- Force-flatten moved **15:15 → 15:10** for CAS house rule.
- Per-index parquet (`underlying_candles_NIFTY/SENSEX.parquet`), lookback toward **512** bars, `amount = mean(OHLC)×volume`.
- Supertrend(10,3): Wilder ATR confirmed; float compare hardened with `np.isclose`.

### Synthetic constituent volume (critical fix)
- Live path had used fake `volume=10_000_000`; engine lived unused in `execution/data_fetcher.py`.
- Live: hub WS LTQ → 5m closed bucket → Kronos at :02 REST OHLC + `amount=mean×vol` → parquet.
- EOD/backfill: index REST **5m OHLC** + constituent REST **5m volumes** summed. Both indices at 15:31.
- `PREDICTION_THRESHOLD=0.0010` (0.10%) aligned.

### Shoonya IP whitelist documented
- `SHOONYA_ORDER_RULES.md` + `ARCHITECTURE.md` §3.0: Shoonya API **VPS-only** (static IP `137.23.41.38`). Never run Shoonya from local IP.

### Dashboard: trade history + UI clarity
- Kronos lane **left**, Astra **right**. Kill switch removed from Quant Console.
- Trade history panel reads `*_bot/data/trades/trade_history.json`.
- Signal idle shown as **Waiting**. Paper funds from `capital_state.json` (`INITIAL_PAPER_CAPITAL=₹2500`).

### Kronos-only execution (Friday overnight gap closed)
- **Incident:** Kronos shared KDO EM; Friday late PUT → NRML overnight; DAY TP cancelled; weekend gap → near-total loss.
- **Fix:** `kronos_execution_manager.py` — entry **09:20–14:50**, flatten **≥15:10**, +10% DAY TP, Chandelier + 600s, **no KDO exits**. (Hard −25% SL added next day — see 2026-08-05.)

### Docs consolidation / VPS hygiene / modular layout
- Deleted stale Aug-2 / Jul docs; single `ARCHITECTURE.md` + this changelog + `SHOONYA_ORDER_RULES.md`.
- Model only under `kronos_bot/model/`. Canonical `institutional_memory/constituents.json` at root.
- Ghost processes killed; live PM2: hub + astra + kronos + dashboard (paper).
- `common/` shared kernel; Kronos model load without HuggingFace `transformers`.

---

## Template for future entries

```
## YYYY-MM-DD
### Short title
- What changed and why
- Files / VPS impact
- Safety notes (paper/live, flatten rules, etc.)
```
