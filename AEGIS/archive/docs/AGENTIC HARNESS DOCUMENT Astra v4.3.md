# ASTRA V4.3 SYSTEM ARCHITECTURE
### Order-Flow + Spot-Structure Hybrid Trading System — Nifty/BankNifty Weeklies
**Owner:** Amarjeet Singh · **Master Architect:** Qwen (chat) · **Implementer:** Qwen Coder · **Deployer:** Antigravity
**Last updated:** 07-Aug-2026 · **Broker:** Shoonya (NorenApi) · **Orders:** LMT only (NSE bans MKT in F&O)

---

## 0. Purpose & Operating Philosophy
Astra is a **retail-scale** intraday options bot (~₹2,000 capital, 1 lot × 65, deep-OTM premiums ≤ ₹30).
Core beliefs baked into the code:
1. **Flow leads price by 1–2 min** → enter only on price *confirmation*, never on flow alone.
2. **Fewer, better trades** → charges (~₹16/round-trip) make trade-count the #1 economic lever.
3. **Capital protection first** → small fast losses, hard daily bounds, no revenge trading.
4. **Price-action + order-flow fusion (V4.3)** → the user's proven manual VWAP/EMA playbook is encoded as a state machine; flow is confirmer/veto, not trigger.

---

## 1. Process Topology (PM2)
| Process | Role | Restart order |
|---|---|---|
| `shoonya-data-hub` | Standalone daemon: broker WS → `/dev/shm/shoonya_live_ticks.json` @1Hz | **1st** |
| `astra-bot` | Trading engine (this repo) | 2nd |
| `unified-dashboard` | Web UI (reads /dev/shm + IPC) | – |
| `stock-order-flow` | Order-flow visualizer | – |

**Rule:** restart hub *before* astra-bot. Deploys **only after 15:30 IST** (or before 09:15).

---

## 2. Data Plane — who writes/reads what
### 2.1 `shoonya_data_hub/main.py` (daemon)
- Subscribes: index spot, weekly options (touchline `feed_type="t"`), and **50 Nifty constituents** (`institutional_memory/constituents.json`).
- `VolumeBucket` class: 5-min constituent volume snapshots (legacy Kronos).
- **KRONOS SECTION (~line 286, "Priority 3"):** re-activated in V4.3 so constituents stream again.
- Writes `/dev/shm/shoonya_live_ticks.json` @1Hz with keys:
  - `synthetic_constituent_volume` (closed 5m buckets)
  - `synthetic_volume_live_accum` (live intra-bucket accumulation)

### 2.2 `common/broker/shoonya_adapter.py`
- `ShoonyaMarketDataProvider.on_message`: WS tick parsing.
- Constituent accumulation: `if token in self._constituent_tokens and ltq>0: self._accumulated_synthetic_vol += ltq`.
- API: `register_constituent_tokens()`, `get_and_reset_synthetic_volume()`, `peek_synthetic_volume()`.
- Also routes option/index ticks → `FootprintCompiler.process_tick`.

### 2.3 `astra_bot/main.py`
- `LiveStateReader`: polls `/dev/shm/shoonya_live_ticks.json` @1Hz (Astra never opens its own constituent WS).
- `SyntheticVolumeBridge.poll()` (V4.3) consumes `synthetic_volume_live_accum` → `SpotStructureEngine.add_volume()`.

---

## 3. Footprint Compiler — `astra_bot/core/footprint_compiler.py`
Class `FootprintCompiler`. Per-token state in `market_depth[token]`: `{b,s,bv,sv,v,lp,oi}`.
- `process_tick(token, ...)`: updates depth; computes per-1m-bar metrics:
  - `bar_open_lp`, `premium_chg_pct` (battleground premium move), `last_spot_bar_chg` (spot bar delta).
  - **OFI** (order-flow imbalance) from bv/sv changes.
- **Volume-injection kill (V4.2):** first trade of an illiquid token previously injected *cumulative* daily volume as one aggressive trade (the +44%/+76% spikes). Fix: `old_vol==0 → vol_delta = ltq or 0`; single-tick cap **1000 lots** → warn + zero (`Suspicious single-tick vol_delta … zeroed`).
- Deduplicator: ignores repeated trade ticks.
- `get_last_lp(token)`: last traded price (used by pending window).
- `trigger_strike_rollover`: ATM rollover with **30-pt hysteresis** (kills per-minute 24600↔24550 flicker).
- **`structure_hook` (V4.3):** set by main; on every spot tick calls `SpotStructureEngine.on_tick(ts, price)`.

---

## 4. Spot Structure Engine (V4.3) — `astra_bot/core/spot_structure.py`
Encodes the user's manual playbook: **red=session VWAP** (synthetic volume), **yellow=dynamic High/Low bands** (VWAP-of-Highs & VWAP-of-Lows), **blue=9 EMA trail**, **grey=200 EMA regime**.
Class `SpotStructureEngine(memory_min=5.0, orb_candles=15)` — `band_pts` deprecated:
- `on_tick(ts, price)`: builds 1m candles (thread-safe).
- `add_volume(minute_ts, qty)`: synthetic volume into current 1m candle (VWAP becomes volume-weighted; falls back to time-weighted if vol=0).
- `_close_candle`: updates session **VWAP** (typical-price weighted), **upper** (VWAP-of-Highs = `high_num/vwap_den`), **lower** (VWAP-of-Lows = `low_num/vwap_den`), **EMA9**, **EMA200**, **ORB15** (first 15 candles).
- `_evaluate(candle)` → event per 1m close:
  - Guard: `if upper - lower < 1.0: return "NONE"` (degenerate early-session bands).
  - `REJECT_PUT`: wick ≥ VWAP & close < lower (single-candle fake-breakout), OR zone-touch ≤5min ago + no reclaim + close < lower (two-step rejection).
  - `REJECT_CALL`: mirror.
  - `BREAKDOWN`: recently above VWAP, close < lower. `BREAKOUT`: mirror.
  - `NONE`.
- `regime(price)`: `BULL` (price>200EMA & >VWAP) / `BEAR` (<both) / `CHOP` (between = **no-trade zone**).
- `trail_exit(side, close)`: **9-EMA reverse break** (CALL exits on close<EMA9; PUT on close>EMA9) — the user's manual trailing SL.
- `invalidation(side, close)`: opposite-band close = hard invalidation (user's high/low-VWAP SL).
- `snapshot()`: dict for digest/IPC.
Class `SyntheticVolumeBridge`: `poll(ts, live_accum)` → delta (handles 5m hub reset) → `add_volume`.

---

## 5. Evaluator — `astra_bot/strategy/evaluator.py`
`evaluate_1m_boundary(compiled, index_spot, spot_bar_chg, now)`:
- **Signed delta:** `(ce_buy−ce_sell)+(pe_sell−pe_buy)` ÷ flow (PE buying = bearish).
- `_MIN_FLOW_VOL = 500` (noise gate). `_CHAIN_DELTA_THR = 0.10`.
- `_confirmation_block(...)`: returns reason if blocked —
  battleground delta>0 · premium impact ≥0.4% · OFI>0 · stale-depth >1.5s · spot veto ±2pts.
- `ICEBERG_SHADOW`: strong flow + zero price effect → log-only (future absorption short).

---

## 6. Main Loop — `astra_bot/main.py`
- 1-min boundary → compile → evaluate → digest.
- `build_minute_digest(...)`: ONE INFO line/min, e.g. `[12:07] d-1.6% | flow 54275 | spot 24553(+3) => NEUTRAL | …`. Quiet-mode: module loggers at WARNING; SDK stdout suppressed.
- **Pending Entry Window:** dict `pending_signal{arm_time, spot0, prem0, target_token, type}`; max **120s**;
  trigger: spot +3pts OR premium +₹0.10; invalidate: spot −8pts OR premium −₹0.25; else EXPIRED.
- **Differential subscribe:** ATM±10 net, ±3 evaluate (cuts WS load).
- **V4.3 wiring:** `ASTRA_STRUCTURE_MODE` env (`shadow` default). At boundary log `[STRUCTURE:mode] event regime vwap ema9 ema200`. In `live`, structure events arm the pending window (REJECT_PUT/BREAKDOWN+BEAR→PUT; REJECT_CALL/BREAKOUT+BULL→CALL); per-candle `trail_exit`/`invalidation` close open trades.

---

## 7. Execution Manager — `astra_bot/execution/astra_execution_manager.py`
- `execute_entry`: price anchor = **live `sp1` (ask)** not stale LTP; buffer `max(2.0, 8%)`; **sq1<3 → skip** (liquidity-vacuum guard); capital deducted at `leg.entry_price`; ENTERED log prints truth.
- `manage_open_trade`: **No-Effect stop** (300s, exit if chg<+1%); **Profit-Lock** via `_effective_sl_price` (floor +1–2% once half-TP reached).
- `_chase_exit_thread`: 3-step exit chase; **fill truth** from `single_order_history` `avgprc`/`fillshares` (dashboard P&L now real).
- `trade_lock` + `EXIT_PENDING` (no double-exit).
- `trades_today = 5`, **persisted with date** (survives restarts). TP fixed **10%**. Hard SL **10%(Fri)/15%**. Force-flatten **15:10**, entry cutoff **15:00**, 1 open trade.

---

## 8. Order Lifecycle — `common/execution/order_lifecycle.py`
`execute_entry(leg, live_ask)` / `execute_exit(leg, live_bid)`; buffer `max(2.0, 8%)`; 3-step chase (−1%/−2%/−3%); returns fill qty.

## 9. Strike Selector — `astra_bot/strategy/strike_selector.py`
Affordability (premium ≤₹30) → OTM bias; **3-min cooldown** per strike; **2% spread gate**.

---

## 10. Risk Parameter Master Table
| Param | Default | Where |
|---|---|---|
| Chain Δ threshold | 0.10 | evaluator |
| Min flow | 500 | evaluator |
| Premium impact | ≥0.4% | evaluator |
| Spot veto | ±2pts | evaluator |
| Stale depth | >1.5s | evaluator |
| Pending window | 120s | main |
| Trigger / Invalidate | +3/+0.10 · −8/−0.25 | main |
| Entry buffer | max(2.0, 8%) | lifecycle |
| No-Effect | 300s, <+1% | exec mgr |
| Profit-Lock floor | +1–2% | exec mgr |
| Hard SL | 10%/15% | exec mgr |
| TP | 10% | exec mgr |
| Trades/day | 5 (persisted) | exec mgr |
| VWAP bands | dynamic (High/Low VWAP) | spot_structure |
| Band spread guard | ≥1.0 pts | spot_structure._evaluate |
| Zone memory | 5min | spot_structure |
| Hysteresis | 30pts | compiler |

## 11. Order Sequence (happy path)
signal → ARMED → (flow+structure confirm) → TRIGGER → entry chase → TP rest → monitor (No-Effect / Profit-Lock / 9-EMA trail / band invalidation) → exit chase → fill truth → history + daily-count persist.

## 12. State Machines
- **Pending window:** ARMED → {TRIGGER→entry} | {INVALIDATED} | {EXPIRED}.
- **Structure events:** NONE→{BREAKOUT/BREAKDOWN/REJECT_CALL/REJECT_PUT} (regime-gated).
- **Regime:** BULL (longs only) / BEAR (shorts only) / CHOP (stand aside).

## 13. IPC / Dashboard
Digest string → `update_astra_live_state(reasoning)`; header shows daily P&L (from history, date-reset).

## 14. Deploy & VCS Protocol
GitHub = **source of truth**. Qwen Coder = PR-only (no direct push, no deploy). User = merge. Antigravity = deploy after 15:30. Tags: `v4.2-live`, `v4.3-shadow`. Rollback via tag or SCP timestamped backup.

## 15. Known Limitations & Roadmap
- V4.3 currently **shadow** → flip to live after 1 clean session.
- Deep-OTM delta (~0.1–0.15) caps payoff; real fix = capital → ATM (₹15–20k).
- Calibration plan: count ARMED/TRIGGER/INVALIDATED/EXPIRED over 2–3 sessions before tuning.