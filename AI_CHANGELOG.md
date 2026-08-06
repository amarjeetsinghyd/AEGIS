# AI / Agent Change Log

Living memory for Cursor agents working on **quant-bot**.  
Canonical architecture: [`ARCHITECTURE.md`](ARCHITECTURE.md).  
Code navigation: [`SEMANTIC_MAP.md`](SEMANTIC_MAP.md).  
Order/broker rules: [`shoonya_data_hub/docs/SHOONYA_ORDER_RULES.md`](shoonya_data_hub/docs/SHOONYA_ORDER_RULES.md).

**Rule for agents:** after any non-trivial change (architecture, execution, VPS ops, safety), append a dated entry here. Do not revive deleted `ai_guide.md` / old `changelog.md` / roadmap files.

---

## 2026-08-06 — Astra V2 Depth + OTM Reach & Stock Order Flow Bug Fixes

### Astra V2: OTM Options Reach & Depth Feed for Order Book
- **Problem (OTM Range):** Astra V2 was aborting trades due to "no affordable strike" because `data_fetcher.py` was hardcoded to fetch dynamic quotes only within `range(-3, 4)` of ATM. It never saw 4th/5th level OTM options, limiting its search to expensive near-ATM options.
- **Fix:** Expanded the dynamic strike list range to `range(-10, 11)` in `astra_bot/execution/data_fetcher.py`. It can now find options up to 10-levels deep.
- **Problem (Order Book Depth):** Index spot token and Option tokens were all being subscribed using `feed_type="t"` (Touchline). Touchline provides LTP/Volume but NOT the L2 Order Book (Top-5 Bid/Ask). Astra V2's `ChainFootprintEvaluator` needs Depth (`feed_type="d"`) to detect Bid/Ask Walls, but only on the actual options (spot index has no order book).
- **Fix:** Switched `astra_bot/main.py` premarket & 1m boundary to subscribe index to `"t"` and option tokens to `"d"`. Now Astra V2 can see actual Level 2 depth on options without blind spots.

### Stock Order Flow: Wrong Symbol False Preemption (Larsen & Toubro)
- **Problem:** When monitoring open positions, the `stock_order_flow` execution manager was querying `searchscrip("LT")`. Shoonya returned `LTTS-EQ` as the first result. The bot fetched the price of `LTTS` (₹3618) instead of `LT` (₹4055), assumed a massive crash, and triggered a false 'PREEMPTED' exit.
- **Fix:** Updated `_get_ltp` in `stock_order_flow/execution_manager.py` to iterate through search results and strictly match `f"{symbol}-EQ"` (e.g. `LT-EQ`).

---

## 2026-08-06 — SOF RoM clarity + Kronos entry misses
- ₹58 / ₹60 **is already leveraged P&L**: qty was sized with 5x notional (9 RELIANCE), so spot +0.5% → ~+2.5% return on margin.
- History `result` now shows `spot % | RoM % (5x) | ₹` so the MIS effect is visible; `net_pl` stays cash P&L credited to the wallet.

### Kronos CALL not taken
- **09:21 log**: was the slow **09:15** candle finishing late (~6m inference). `will_enter=false` because entry starts at **09:20** candle (by design). Now logs `[ENTRY SKIP]` with reason; refreshes wall clock after inference.
- **09:33**: real entry attempt → **no affordable strike**. Chain has cheap OTM (probe: ₹27–₹2,261/lot on ₹2,500). Selector hardened: ATM→OTM + full-chain fallback, premium band preference, clearer abort reason on dashboard.
- Dashboard CALL/PUT styling made louder (green/red badge + larger signal).

---

## 2026-08-06 — Stock Order Flow: MIS 5x realism + trade history

### Problems (from live logs / dashboard)
- Signals worked, but **Order Flow history** looked empty/wrong: rows had fractional `pnl` (0.005) and **no** `entry_price` / `exit_price` / rupee `net_pl`.
- Sizing was **all-in**: `qty = int((all_funds × 5) / price)` → first RELIANCE ate ~₹2.3k margin; INFY/LT/TATA hit **Insufficient buying power**; free cash fell to ~₹62 with 3 opens.
- Opposite signal while open was **skipped** (“trade already open”) instead of reverse/close.
- Open positions lived only in memory → PM2 restart dropped them (margin stayed deducted).

### Fixes (`stock_order_flow/execution_manager.py`)
- **MIS mimic:** free cash = margin; notional = margin × **5**; max **5** concurrent names; each new entry gets **equal share of remaining free cash** as margin budget (not 100% wallet).
- History writes dashboard fields: `entry_price`, `exit_price`, `net_pl` (₹), `qty`, `margin`, `leverage`.
- Opposite signal → close (`REVERSE_TO_*`) then allow flip.
- Persist opens to `data/trades/open_positions.json`; restore on boot.
- Dashboard empty-path text + normalizer ignore legacy fractional SOF `pnl`.
- VPS: backfilled today’s 2 closed RELIANCE rows; restored HDFCBANK / TATAMOTORS / RELIANCE opens; restarted `stock-order-flow` + `unified-dashboard` only.

---

## 2026-08-05 (full day precision updates)

### Shadow OI fix (hub was hardcoding zeros)
- Root cause: `shoonya_data_hub/main.py` published `net_delta_oi_1min=0.0` always; option tokens were subscribed but **never** `register_option_tokens(CE, PE)`, so OI trackers stayed empty. WS `std_msg` also dropped `oi`.
- Fix: register CE/PE, REST-seed + round-robin OI refresh, `OiMinuteBucket` publishes real 1m PEΔ−CEΔ into shm. Kronos reader unchanged (still shadow-only).

### Paper recharge + hard SL −15%
- Reset Kronos `capital_state.json` → **₹2,500** (starting paper wallet).
- `HARD_SL_PCT` **0.25 → 0.15** (−15% premium hard floor). TP +10% / Chandelier / 15:10 flatten unchanged.

### K-ENTRY-1500 shipped (last entry 15:00)
- Kronos `KRONOS_ENTRY_CUTOFF` **14:50 → 15:00**. Force flatten still **≥15:10**. Aligns with operator: afternoon entries allowed through 3 PM.

### K-ST-FINAL shipped (1m Supertrend gate + morning bypass)
- **Before 09:45:** `ST_BYPASS` — Kronos raw CALL/PUT is final (no Supertrend).
- **From 09:45:** `ST_1M` — gate with Supertrend(10,3) on completed 1m bars from hub LTP (`kronos_bot/core/minute_bars.py`); REST seed + `underlying_1m_*.parquet` persist.
- Kronos model / 5m parquet unchanged. Journal + live_state: `gate_mode`, `supertrend_tf`.
- Docs: IDEAS **K-ST-FINAL** → IMPLEMENTED; ARCHITECTURE §6.5 updated. Deploy Kronos only.

### Adaptive Noise Floor & Hybrid Exits (Astra Bot)
- **Problem**: The Astra bot was experiencing rapid "flickering" exits due to microscopic pullbacks treating zero-crossings of the noisy ABG-filtered signal as valid exit events. Furthermore, momentum trades were being killed due to a blind 30-second pause upon strike rollovers.
- **Solution (Noise Floor)**: Replaced static exit thresholds with a dynamic `AdaptiveNoiseFloor` using Median Absolute Deviation (MAD) of raw tick velocity over a 60-tick rolling window. Exits now require the signal to pierce a `1.0x` multiple of the live noise floor (Dynamic Hysteresis).
- **Solution (Kinematic Pause)**: Verified that the "Continuity-Preserving Micro-Bucket Footprint Compiler" (already in place in `astra_bot/core/footprint_compiler.py`) successfully rebases OI continuity offsets without flushing or pausing upon strike rollover.
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

## 2026-08-06
### SOF clock → 3m WS candles (TSF shadow on same series)
- Engine boundary **5m → 3m** (09:15 grid).
- Canonical **3m OHLCV** from WebSocket (`bar_book.py`); REST `THREE_MINUTE` warmup only.
- Shadow TSF uses WS 3m closes and **does not block** entries if series still warming.
- ABSORB_CONFLICT T+1 now resolves on the next **3m** bar (~6m worst case vs ~10m before).

### SOF ABSORB_CONFLICT → T+1 breakout/absorption resolution
- On L1/L2 conflict (strong delta vs 2× opposite wall): **NEUTRAL** + stash one-bar memory (do not blind-fade).
- Next 5m: late **BREAKOUT** if price clears ref and wall thins; **ABSORPTION** if wall holds and price fails; else clear and fall through.
- File: `stock_order_flow/evaluator.py`.

### Stock Order Flow hygiene + premarket fixes
- Root of Kronos-tab OF spam: SOF briefly logged into `kronos_bot.log` (cleaned earlier).
- SOF fixes before open:
  - 5m history lookback **3 days** (75m overnight window was empty).
  - **TATAMOTORS → TMPV-EQ (3456)** (NSE rename/split) + hardcoded token map fallback.
  - No second `start_websocket` — `add_tick_listener` + `_ensure_ws_started`; subscribe touchline+depth.
  - Session gate: idle pre-09:15 / after 15:15; entries only to 15:00; flatten ≥15:10.
  - CapitalEngine bound to `stock_order_flow/data`.

### Hygiene: Kronos dashboard log contamination from Stock Order Flow
- Symptom: Kronos telemetry tab showed `stock_main` / RELIANCE / footprint lines — SOF briefly wrote into `kronos_bot/logs/kronos_bot.log` before path fix.
- Action: rotated contaminated file → `kronos_bot.log.contam_sof_.bak`; cleaned live `kronos_bot.log`. Dashboard wiring itself was already correct (separate `of_logs` vs `kronos_logs`).
- Note: Kronos deep-sleep until ~08:55; fresh Kronos lines resume after wake.

## 2026-08-05
### Engine 3: Stock Order Flow Footprint Engine added
- Designed and implemented stock_order_flow (Engine 3) dedicated to High-Frequency Intraday Footprints on equity stocks (RELIANCE, HDFCBANK, INFY, LT, TATAMOTORS).
- Deployed on PM2 as a standalone paper engine running beside Astra and Kronos.
- Engine uses CapitalEngine for its own 2500 INR paper funds and ExecutionManager for MFE/MAE trade potential tracking in post_trade_tracking.json using a 1:1 Risk-to-Reward (0.5% SL and 0.5% TP).
- Fixed unified_dashboard to support all 3 lanes, decoupling logger paths to ensure telemetry separation.

