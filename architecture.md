# Quant-Bot System Architecture (Canonical)

**Status:** Active source of truth — read this before changing code  
**Updated:** 2026-08-05 (IST — WS synth vol, inference, −25% SL, semantic map)  
**Scope:** Local monorepo + Oracle VPS (`opc@137.23.41.38`, user `opc`)  
**Companion living log:** [`AI_CHANGELOG.md`](AI_CHANGELOG.md) (append after non-trivial work)  
**Code navigation map:** [`SEMANTIC_MAP.md`](SEMANTIC_MAP.md) (folder → file → function)  
**Broker quick rules:** [`shoonya_data_hub/docs/SHOONYA_ORDER_RULES.md`](shoonya_data_hub/docs/SHOONYA_ORDER_RULES.md)

All previous architecture / KDO-v2 / ASTRA-v3 / `ai_guide` / `future_roadmap` / old `changelog` markdown files were deleted. **This file wins** over chat memory when they conflict. For “where is the code?”, use [`SEMANTIC_MAP.md`](SEMANTIC_MAP.md).

---

## 0. Instructions for any AI / engineer opening this repo

1. Read **§1 (mission + Friday incident)** and **§12 (never again)** first.
2. For file/folder/function lookup, open [`SEMANTIC_MAP.md`](SEMANTIC_MAP.md) (symptom → file triage at bottom).
3. Kronos is the **priority engine** until paper-stable. Do **not** refactor Astra exits while Kronos gaps remain.
4. Shoonya API is **VPS-only** (IP whitelist). Never run broker login/WS/orders from a local laptop.
5. After any non-trivial change: update **this file**, append [`AI_CHANGELOG.md`](AI_CHANGELOG.md), and refresh [`SEMANTIC_MAP.md`](SEMANTIC_MAP.md) if ownership/paths moved.
6. Restart PM2 **only for processes whose code changed**, preferably **once at end of a batch** — not after every micro-edit. Do not needlessly restart Astra when only Kronos/hub changed.
7. Default capital mode is **paper**: `ENABLE_LIVE_BROKERAGE_EXECUTION=False`.
8. Do not invent GTC orders. Do not put Kronos on KDO `ExecutionManager`. Do not fake volume as `10_000_000`.

---

## 1. Mission and hard lessons

### 1.1 Mission

Run two independent Indian **index options** engines on one VPS:

| Engine | Role | Signal TF | Execution module |
|---|---|---|---|
| **Astra** | Kinetic Delta Orderflow (KDO) microstructure | tick / sub-second | `common/execution/execution_manager.py` (KDO exits) |
| **Kronos** | Kronos foundation-model directional + Supertrend | **5-minute** | `kronos_bot/execution/kronos_execution_manager.py` **only** |

Shared infrastructure:

- Shoonya broker stack under `common/broker/`
- Shoonya Data Hub → `/dev/shm/shoonya_live_ticks.json`
- Unified light Quant Console (`unified_dashboard/`)
- Market calendar under `common/market/`

**They must not share exit logic.** Shared OK: capital, strikes, trade context, order lifecycle I/O, calendar helpers.

### 1.2 Incident that forced this rewrite (Friday overnight PUT)

A Kronos PUT was entered **late session** (~after 15:00). Operator expected a **+10% premium target** to exit. Exit did not fire. **NRML** position carried over the weekend. Market **gap-up** crushed the PUT. By next session / expiry premium → ~zero (total loss).

**Root causes:**

1. Kronos was wired to the **shared KDO `ExecutionManager`** (flow-decay / premium trailing / OI gates) — wrong exit model for Kronos.
2. Shoonya resting orders use **`retention=DAY`**, **not GTC**. EOD cancels resting TP. **NRML positions can still carry overnight** → naked option with no working exit.
3. Entry cutoff too late (`15:20`); a 10-minute hold collides with close / weekend / weekly expiry.
4. Synthetic volume path was broken / fake (`10_000_000` filler), so ML features were garbage when volume mattered.

**Non-negotiable fixes (locked):**

- Dedicated **KronosExecutionManager**
- Resting **+10% DAY LMT TP** on entry
- Exit: **5m underlying Chandelier** + **−25% premium hard SL (fallback)** + **600s time stop** + **force flatten ≥ 15:10**
- Premium hard SL is **only** the −25% fallback (not KDO trailing / flow exits)
- **No KDO flow / OI exits** on Kronos
- Entry window **09:20–14:50**
- Real **synthetic constituent volume** (never fake 10M)
- Never overnight weekly options in this strategy

---

## 2. Repository layout (modular)

```text
quant-bot/
  ARCHITECTURE.md                 # THIS FILE (canonical policy/design)
  SEMANTIC_MAP.md                 # Folder → file → function navigation
  AI_CHANGELOG.md                 # Agent living memory (append-only dated notes)
  README.md                       # Short pointer to the docs above
  ecosystem.config.js             # PM2: hub + astra + kronos + dashboard
  requirements.txt
  institutional_memory/
    constituents.json             # nifty_50 (50) + sensex_30 (30) Shoonya tokens

  common/                         # Shared kernel ONLY
    broker/                       # Shoonya/Angel adapters, auth, interfaces, ring buffer
                                  # WS LTQ → synthetic vol accumulator (constituent tokens)
    execution/                    # CapitalEngine, StrikeSelector, TradeContext,
                                  # Paper/Shoonya OrderLifecycle, Astra/KDO ExecutionManager
    market/                       # calendar, instruments
    ipc/                          # /dev/shm helpers (no Flask)
    runtime/paths.py              # get_project_root, get_runtime_paths
    config.py                     # shared constants + configure_runtime()
    logger.py

  shoonya_data_hub/               # Sole tick + synth-vol producer (PM2: shoonya-data-hub)
    main.py                       # WS spot + options + constituents; 5m vol buckets
    docs/
      SHOONYA_ORDER_RULES.md
      README.md
    reference/ShoonyaApi-py/      # Official Finvasia/Shoonya SDK clone

  astra_bot/                      # KDO engine (do not “simplify” onto Kronos exits)
    main.py
    config/settings.py            # configure_runtime("astra")
    core/microstructure_engine.py
    data/, logs/

  kronos_bot/                     # ML engine (priority until paper-stable)
    main.py                       # 5m loop, signal, Kronos EM, EOD backfill both indices
    config/settings.py            # configure_runtime("kronos"); PREDICTION_THRESHOLD=0.0010
    strategy/
      signal_generator.py         # Kronos.from_pretrained + Supertrend (NO transformers)
      supertrend.py               # Wilder ATR Supertrend(10, 3.0)
    model/                        # Sole model package (kronos.py, module.py)
    execution/
      kronos_execution_manager.py # Kronos-only entry/exit
      strike_selector.py          # may shim/shared
    core/
      data_fetcher.py             # LIVE path: parquet windows + REST OHLC + synth vol join
    data/
      underlying_candles_NIFTY.parquet
      underlying_candles_SENSEX.parquet
      underlying_candles.parquet  # legacy mirror of *active* index
      capital_state.json          # paper wallet
      trades/trade_history.json   # closed trades (disk)
      trades/active_trade_context.json
    logs/

  unified_dashboard/
    dashboard.py                  # Light Quant Console; Kronos LEFT; trade history disk

  shared_runtime/                 # Thin re-export shim → common.runtime.paths (tests)
  tests/
```

### 2.1 Process topology (VPS)

```text
PM2  (cwd=/home/opc, PYTHONPATH=/home/opc)
├── shoonya-data-hub   → /dev/shm/shoonya_live_ticks.json
│                        (quotes + closed 5m synthetic constituent volume)
├── astra-bot          → /dev/shm/kdo_live_state.json
├── kronos-bot         → /dev/shm/kronos_live_state.json
│                        + kronos_bot/data/underlying_candles_*.parquet
 └── unified-dashboard  → HTTP :5000  (reads shm + per-bot data/ on disk)
```

Paper/live gate: `ENABLE_LIVE_BROKERAGE_EXECUTION` (default **False**).

SSH: key under local `Oracle SSH keys/ssh-key-2026-07-11.key` → `opc@137.23.41.38`.

### 2.2 PM2 restart policy (for agents)

| Situation | Action |
|---|---|
| Edit only docs | No restart |
| Edit `kronos_bot/**` runtime code | `pm2 restart kronos-bot` (end of batch) |
| Edit `shoonya_data_hub/main.py` or broker WS synth | `pm2 restart shoonya-data-hub` (+ kronos if it must re-read) |
| Edit dashboard | `pm2 restart unified-dashboard` |
| Edit shared `common/` used by all | Restart affected apps once at end |
| Do **not** | Restart entire stack after every keystroke |

---

## 3. Shoonya order rules (authoritative)

SDK clone: `shoonya_data_hub/reference/ShoonyaApi-py`  
Upstream: https://github.com/Shoonya-Dev/ShoonyaApi-py  
Ops summary: `shoonya_data_hub/docs/SHOONYA_ORDER_RULES.md`

### 3.0 IP whitelist — VPS only (critical)

| Environment | Allowed? |
|---|---|
| Oracle VPS `137.23.41.38` (whitelisted static IP) | **Yes — only place API/WS/orders run** |
| Local laptop / home / office / dynamic IP | **Never** |

Paper mode on VPS still uses Shoonya session for **market data**; whitelist still applies.  
If VPS public IP changes → re-whitelist with Finvasia/Shoonya before any API use.

### 3.1 Retention — there is no GTC

| `ret` | Meaning | Supported? |
|---|---|---|
| **DAY** | Valid that session; cancelled at EOD | Yes (default) |
| **IOC** | Immediate or cancel | Yes |
| **EOS** | End of session | Yes |
| **GTC** | Good till cancelled across days | **No** |

Implication: resting +10% TP is DAY. If unfilled at close, **order vanishes**; **NRML position may remain**. Strategy must **force-flatten** open Kronos options before close.

### 3.2 Product types (`prd`)

| Code | Product |
|---|---|
| `C` | CNC |
| `M` | **NRML** (official) |
| `I` | MIS |
| `B` | Bracket |
| `H` | Cover |

House rule: index options = **NRML**, never MIS.

> Runtime historically used `DEFAULT_PRODUCT_TYPE = "N"`. Official docs map NRML → **`M`**. Validate vs account `prarr` before live capital; align `common/config.py` + order lifecycle. Do not guess in production.

### 3.3 Price types

Prefer **`LMT` only** (marketable limits: limit slightly worse than touch).  
Do not depend on naked `MKT` for the core path.

### 3.4 Quantity

Qty is **shares**, not lots: `lots × lot_size`  
Lot sizes (SEBI 2026-07): **NIFTY 65**, **SENSEX 20**.

### 3.5 place_order (SDK signature)

```text
place_order(buy_or_sell, product_type, exchange, tradingsymbol, quantity,
            discloseqty, price_type, price=0.0, trigger_price=None,
            retention='DAY', amo='NO', remarks=None)
```

### 3.6 Closing Auction Session (CAS) — market timing context (from Aug 2026)

**House continuous session for Kronos/data:** **09:15 → 15:15 IST**. After 15:15, cash enters auction (CAS); we do **not** treat auction prints as normal 5m ML bars.

| Time (IST) | What happens |
|---|---|
| 09:15–15:15 | Continuous cash (CAS-eligible underlyings) |
| **15:10–15:15** | Last Kronos 5m bar (**timestamp label 15:10**, closes at **15:15**). Hub WS synth vol still accrues for this bar. |
| **≥15:15 / 15:17** | Continuous done → EOD REST backfill for NIFTY+SENSEX (buffer before auction spike) |
| ~15:20–15:35 | Closing auction phases (cash) — flat/zero-vol then spike on charts |
| **15:40** | Index / stock **F&O** may still trade — **not** a Kronos hold license |

**Candle naming (important):** a bar stamped `15:10:00` is the interval **[15:10, 15:15)**. Keeping “last bar = 15:10” in parquet **includes** continuous trading through **15:15**. Rejecting stamps `> 15:10` drops CAS buckets (15:15 / 15:20 / 15:25), not the last continuous interval.

**House rule for Kronos options:** force-flatten **≥ 15:10** (start of that last bar / before late chaos). Next morning **09:15–09:20** first candle → natural gap vs prior **15:10-labeled** close for Kronos.

---

## 4. Shared kernel (`common/`)

### 4.1 Runtime binding

Each process calls `configure_runtime("astra"|"kronos"|"hub")` which binds:

- `BOT_DATA_DIR` → `<engine>/data`
- `BOT_LOGS_DIR` → `<engine>/logs`

Capital JSON and trade history **must** stay under that engine’s `data/`.

### 4.2 IPC files (`/dev/shm` on Linux; local fallback `data/shm/` if no shm)

| Path | Writer | Readers | Notes |
|---|---|---|---|
| `shoonya_live_ticks.json` | data hub | Kronos, Astra, dashboard | quotes + synth vol buckets |
| `kdo_live_state.json` | Astra | dashboard | signal metrics |
| `kronos_live_state.json` | Kronos | dashboard | signal metrics |
| `KILL_SWITCH_ACTIVE` | ops (legacy) | calendar helpers | Dashboard kill UI **removed**; file may still exist |

Atomic writes: write `*.tmp` then `os.replace`.

### 4.3 Shared execution primitives (OK to share)

- `CapitalEngine` — paper wallet on disk (`capital_state.json`); start `INITIAL_PAPER_CAPITAL = 2500`
- `StrikeSelector` — ATM→OTM affordable 1-lot
- `TradeContext` / `TradeLeg`
- `PaperOrderLifecycle` / `ShoonyaOrderLifecycle` (broker I/O only)

### 4.4 Shared KDO `ExecutionManager` — **Astra only**

File: `common/execution/execution_manager.py`

KDO exits (do not copy onto Kronos):

1. Premium hard SL (legacy KDO path; e.g. −25% / −10% variants historically)
2. Premium trailing
3. Time cap
4. Optional OI reversal
5. Flow-decay hysteresis

**Kronos must never instantiate this class for live risk.**

### 4.5 Synthetic volume accumulator (broker WS)

In `common/broker/shoonya_adapter.py` (`ShoonyaMarketDataProvider`):

- `register_constituent_tokens(tokens)` — **replaces** the set (does not merge) and **resets** accumulator (critical on NIFTY↔SENSEX rotation)
- On each WS tick: if token ∈ constituents and `ltq` > 0 → add to `_accumulated_synthetic_vol`
- `get_and_reset_synthetic_volume()` / `peek_synthetic_volume()` exposed on `ShoonyaAdapter`

---

## 5. Astra engine (deferred priority — enough for safe coexistence)

**Do not deep-refactor Astra until Kronos is paper-stable.** This section exists so agents do not accidentally merge the two exit models.

### 5.1 Role

Astra is the **Kinetic Delta Orderflow (KDO)** engine: tick/sub-second microstructure over the shared Shoonya quote bus, trading the same daily index rotation (NIFTY Mon/Tue/Fri, SENSEX Wed/Thu).

### 5.2 Key files

| Piece | Path |
|---|---|
| Loop | `astra_bot/main.py` |
| Config / runtime | `astra_bot/config/settings.py` → `configure_runtime("astra")` |
| Signal engine | `astra_bot/core/microstructure_engine.py` (`KineticMicrostructureEngine`) |
| Ring buffer | `astra_bot/core/ring_buffer.py` (or shared broker ring) |
| Exits / entries | **`common/execution/execution_manager.py`** (KDO EM — Astra only) |
| Live state | `/dev/shm/kdo_live_state.json` via `common.ipc.update_astra_live_state` |
| Paper capital | `astra_bot/data/capital_state.json` |
| Trade history | `astra_bot/data/trades/trade_history.json` |

### 5.3 KDO exit stack (Astra only — never copy to Kronos)

From `common/execution/execution_manager.py` (order of evaluation):

1. **Premium hard SL** — e.g. −25% on option premium (KDO path).
2. **Premium trailing** / related premium risk.
3. **Time cap** (120-second / 2-minute hard scalp cap).
4. **OI reversal** (optional; gated by `OI_REVERSAL_EXIT_ENABLED`, threshold 50k, min hold 60s).
5. **Dynamic Flow-decay hysteresis** (MAD-based Adaptive Noise Floor): Exits trigger only if the velocity drops a 1.0x multiple below the live background noise floor measured continuously by `KineticMicrostructureEngine`. Removes microscopic flickering exits.

Strategy tag on trades: `KINETIC_DELTA_ORDERFLOW`.

### 5.4 Coexistence rules

- Same hub ticks, same calendar helpers, same strike/capital primitives — **OK**.
- Same `ExecutionManager` class for Kronos — **FORBIDDEN**.
- Restarting Astra is not required when only Kronos/hub/docs change.
- Dashboard shows Astra on the **right** lane; do not remove Astra UI while “focusing on Kronos.”

### 5.5 When Astra work is allowed again

Only after Kronos paper day proves: entry → DAY TP → Chandelier/time/**15:10 flatten**, with real synth volume and no overnight options.

---

## 6. Kronos engine (priority — full detail)

### 6.1 Daily index rotation

| Weekday | Active index | Options exchange | Cash token |
|---|---|---|---|
| Mon, Tue, Fri | **NIFTY** | NFO | NSE `26000` |
| Wed, Thu | **SENSEX** | BFO | BSE `1` |

Constituents file: `institutional_memory/constituents.json`

- `nifty_50` → 50 NSE equity tokens  
- `sensex_30` → 30 BSE equity tokens  

### 6.2 Six-channel candle schema (parquet)

Files:

- `kronos_bot/data/underlying_candles_NIFTY.parquet`
- `kronos_bot/data/underlying_candles_SENSEX.parquet`
- `kronos_bot/data/underlying_candles.parquet` — legacy mirror of **active** index only

Columns (required):

| Column | Source |
|---|---|
| `timestamp` | 5m bar label |
| `open, high, low, close` | Index **cash** REST OHLC |
| `volume` | **Synthetic constituent volume** (sum of 50/30) — **never** fake `10000000` |
| `amount` | `mean(open,high,low,close) × volume` |

Sliding window target: **512** bars (= Kronos `max_context`).

### 6.3 Synthetic volume — two paths (locked)

#### A) Live path (active index only, during market hours)

Owner: **shoonya-data-hub** + **kronos_bot/main.py**

1. Hub loads constituents for active index, `register_constituent_tokens`, WS-subscribe cash equities.
2. During each open 5m bucket, WS LTQ accumulates on the hub’s broker adapter.
3. At 5m boundary, hub **closes bucket** via `get_and_reset_synthetic_volume()` and publishes into shm:
   - `synthetic_nifty_volume` / `synthetic_sensex_volume`
   - `synthetic_constituent_volume`
   - bucket metadata fields
4. Kronos wakes at boundary **second ≥ 2**:
   - Read closed synth volume from hub (`get_hub_synthetic_volume`)
   - REST-pull completed index 5m OHLC
   - `amount = mean(OHLC) × synth_vol`
   - `append_live_candle` → per-index parquet (trim to 512)
5. Then run signal → maybe enter → always `manage_open_trade` every second.

**Why WS for live volume:** pulling 30–50 names via REST every 5 minutes is too slow / rate-limited. WS LTQ compile is the live design.

**Rotation:** when weekday flips NIFTY↔SENSEX, hub re-registers constituents (**replace set + reset accumulator**) so volumes never mix.

#### B) EOD / historical backfill path

Owner: `kronos_bot/core/data_fetcher.py` → `rest_backfill_today_candles(index)`

1. REST index **5m** OHLC (~14 calendar days lookback, trim to 512).
2. REST each constituent **5m** volume series (chunked ~7 days; clip vol ≥ 0).
3. Sum constituents → synthetic volume series; join on timestamp.
4. `amount = mean(OHLC) × volume`.
5. Write per-index parquet.

**Do not use 1-minute constituent pulls for EOD** — Kronos is a 5m engine; 5m REST is correct and much faster.

Triggered:

- On bootstrap if window `< 512` or fake-10M detected
- **EOD ~15:31** in `kronos_bot/main.py` for **both** NIFTY and SENSEX (so tomorrow’s inactive→active index is warm)

### 6.4 Intraday main loop timeline

| Time | Action |
|---|---|
| 08:55 | Pre-market standby (handshake, LTP heartbeat) |
| 09:15 | Market open; **no** first-trade until 09:20 full candle |
| 09:20, 09:25, … | At `:02` of each 5m boundary: synth vol + REST OHLC + append + predict |
| 09:20–14:50 | New entries allowed if CALL/PUT and no open trade |
| After 14:50 | No new entries; manage open risk only |
| ≥ 15:10 | **Force flatten** any open Kronos option |
| ~15:31 | EOD REST backfill **both** indices (OHLC + 5m synth vol) |
| Else | Deep sleep until next 08:55 |

### 6.5 Signal stack (entry conditions)

File: `kronos_bot/strategy/signal_generator.py`  
Threshold: `PREDICTION_THRESHOLD = 0.0010` (**0.10%**) in `kronos_bot/config/settings.py` (main must use this constant).

Pipeline:

1. Validate window (≥ Supertrend warm-up bars; ideally 512).
2. Load model once: `KronosTokenizer.from_pretrained` + `Kronos.from_pretrained` (**no `transformers`**).
3. Predict next 5m close → `return_pct = (pred_close − close) / close`.
4. Magnitude gate:
   - `return_pct ≥ +0.10%` → raw **CALL**
   - `return_pct ≤ −0.10%` → raw **PUT**
   - else **NEUTRAL**
5. Supertrend gate on **same 5m** OHLC — period **10**, multiplier **3.0**, Wilder ATR (`kronos_bot/strategy/supertrend.py`):
   - **UPTREND** = close > Supertrend (TradingView green line below price) → CALL allowed
   - **DOWNTREND** = close < Supertrend (TradingView red line above price) → PUT allowed
   - Counter-trend raw Kronos signal → force **NEUTRAL**
   - Every 5m logs: `[SUPERTREND] UPTREND|DOWNTREND`, then `[SIGNAL] final=… | kronos_raw=…`, then `[5m DECISION] …`
6. **1-minute Net Delta OI is SHADOW ONLY** — logged / saved in decision payload; **never blocks entry**.

Entry proceeds only if final signal ∈ {CALL, PUT} **and** `KronosExecutionManager.can_enter(now)`.

### 6.6 Execution manager (Kronos-only)

File: `kronos_bot/execution/kronos_execution_manager.py`

#### Entry gates

| Gate | Rule |
|---|---|
| Calendar | Trading day |
| Clock | **09:20 ≤ t ≤ 14:50** |
| Position | No OPEN trade already |
| Strike | Weekly CE/PE, ATM→OTM, **1 lot**, cost ≤ paper funds |
| Cooldown | Symbol not in strike cooldown |

#### Exit priority (locked)

| Priority | Rule | Detail |
|---|---|---|
| 0 | Force flatten | `now.time() ≥ 15:10` |
| 1 | +10% TP | Resting DAY LMT sell at `round_tick(entry × 1.10)` |
| 2 | **−25% premium hard SL** | Fallback when Chandelier is unreliable (both lines above price, etc.) |
| 3 | Chandelier | 5m underlying, period 10, mult **2.0**; ratchet favorable only |
| 4 | Time stop | **600 seconds** from entry |
| — | No KDO flow/OI exits | Explicit |

Chandelier formulas:

- CALL: `stop = highest_high(10) − 2 × ATR(10)`
- PUT: `stop = lowest_low(10) + 2 × ATR(10)`

On non-TP exits: **cancel TP order id**, then marketable LMT sell.

Persistence:

- Active: `data/trades/active_trade_context.json`
- Closed: append `data/trades/trade_history.json` (disk — dashboard reads this)

### 6.7 Model loading

Correct:

```python
tokenizer = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
model = Kronos.from_pretrained("NeoQuasar/Kronos-base")
predictor = KronosPredictor(model, tokenizer, device="cpu", max_context=512)
```

Incorrect: `transformers.AutoModelForCausalLM`.

Package location: **`kronos_bot/model/` only** (relative import `from .module import *`).  
Deps: `torch`, `huggingface_hub`. Weights via HuggingFace hub cache on VPS under `~/.cache/huggingface/`.

### 6.8 Key Kronos config constants

| Constant | Value | Where |
|---|---|---|
| `PREDICTION_THRESHOLD` | `0.0010` (0.10%) | `kronos_bot/config/settings.py` |
| Supertrend | period 10, mult 3.0 | `signal_generator.py` / `supertrend.py` |
| Chandelier | period 10, mult 2.0 | `kronos_execution_manager.py` |
| TP | +10% | `kronos_execution_manager.py` |
| Premium hard SL | −25% (fallback) | `kronos_execution_manager.py` |
| Max hold | 600s | `kronos_execution_manager.py` |
| Entry cutoff | 14:50 | `KRONOS_ENTRY_CUTOFF` |
| Force flatten | 15:10 | `KRONOS_FORCE_FLATTEN` |
| Context | 512 bars | model + parquet trim |
| Paper start | ₹2500 | `common.config.INITIAL_PAPER_CAPITAL` |
| Lot sizes | NIFTY 65 / SENSEX 20 | `common.config` |
| Model / tokenizer | `NeoQuasar/Kronos-base` / `Kronos-Tokenizer-base` | `kronos_bot/config/settings.py` |
| Cooldown | 15 minutes (strike) | `COOLDOWN_MINUTES` |
| Timeframe | `5m` only | `TIME_FRAME` |
| Daily trade cap | unlimited (`UNLIMITED_DAILY_TRADES=True`) | settings |

### 6.9 Persistence & restart resilience

| Artifact | Path | Purpose |
|---|---|---|
| Active trade | `kronos_bot/data/trades/active_trade_context.json` | Survive PM2 restart mid-trade |
| History | `kronos_bot/data/trades/trade_history.json` | Dashboard + audit |
| Capital | `kronos_bot/data/capital_state.json` | Paper wallet |
| Candles | `underlying_candles_{NIFTY,SENSEX}.parquet` | ML window |
| Live metrics | `/dev/shm/kronos_live_state.json` | Dashboard only (ephemeral) |

On EM init, if active context is OPEN, restore and continue enforcing TP / −25% SL / Chandelier / 15:10 / 600s — do not orphan NRML risk after a restart.

---

## 7. Data hub (deep)

File: `shoonya_data_hub/main.py`  
PM2 name: `shoonya-data-hub`  
Docs: `shoonya_data_hub/docs/SHOONYA_ORDER_RULES.md`, `shoonya_data_hub/docs/README.md`  
SDK clone: `shoonya_data_hub/reference/ShoonyaApi-py/`

### 7.1 Responsibilities

1. Shoonya login/restore session (VPS IP only).
2. Subscribe **spot** cash index token for active day (`NSE:26000` NIFTY / `BSE:1` SENSEX).
3. Subscribe **constituents** (50 or 30) for synthetic volume WS LTQ.
4. Subscribe weekly **option** tokens around ATM (quote bus / OI consumers).
5. Every ~1s atomically rewrite `/dev/shm/shoonya_live_ticks.json` (`_atomic_write` → `*.tmp` + `os.replace`).
6. Maintain **5m volume bucket**: on bucket change, `get_and_reset_synthetic_volume()` and publish closed values for Kronos.
7. On weekday index rotation: **replace** constituent token set + **reset** accumulator (never merge NIFTY into SENSEX).

If hub is down / ticks stale: dashboard shows tick age; engines must not pretend quotes are live.

### 7.2 `shoonya_live_ticks.json` schema (hub → consumers)

| Field | Meaning |
|---|---|
| `timestamp` | ISO write time |
| `active_index` | `NIFTY` or `SENSEX` |
| `spot_token` / `spot_exch` | Cash index token + exchange |
| `quotes` | `{token: {ltp, bid, ask, oi, volume}}` for spot + subscribed options |
| `synthetic_nifty_volume` | Last **closed** 5m NIFTY constituent sum (0 if not that bucket) |
| `synthetic_sensex_volume` | Last **closed** 5m SENSEX constituent sum |
| `synthetic_constituent_volume` | Closed vol for **active** index (convenience alias) |
| `synthetic_volume_live_accum` | Open-bucket running LTQ sum (peek; not yet closed) |
| `synthetic_volume_bucket` | Current open 5m bucket id |
| `synthetic_volume_last_closed_bucket` | Previous closed bucket id |
| `total_call_oi` / `total_put_oi` | Aggregates from option quotes (hub-side) |
| `delta_*_oi_1min` / `net_delta_oi_1min` | May be stubbed/zero on hub; Kronos treats Net Delta OI as **shadow only** |

Kronos reads closed synth vol via `get_hub_synthetic_volume` in `kronos_bot/core/data_fetcher.py` — prefer **closed** bucket fields, not live accum, when assembling a completed 5m candle.

### 7.3 Engine live-state schemas (shm)

**Astra** → `kdo_live_state.json` defaults (`common/ipc/live_state.py`):  
`spot_price`, `gex_net`, `gex_regime`, `velocity`, `acceleration`, `signal_status`, `reasoning`, `kinematic_ready`, `rollover_pause`, `option_chain`, `active_trade`, `available_funds`, `engine=astra`.

**Kronos** → `kronos_live_state.json` defaults:  
`spot_price`, `predicted_close`, `predicted_return_pct`, `signal` / `signal_status`, `net_delta_oi`, `last_candle`, `active_trade`, `available_funds`, `reasoning`, `engine=kronos`.

Local Windows without `/dev/shm`: files land under `<project>/data/shm/`.

---

## 8. Dashboard (Quant Console)

File: `unified_dashboard/dashboard.py`  
Port **5000**, light dual-brand UI.  
PM2: `unified-dashboard`.

Layout:

- **Kronos lane LEFT**, Astra RIGHT
- Shared market bus strip (tick age; paper wallet explanation)
- Telemetry logs with Kronos/Astra switch (**Kronos default**)
- **Trade history** below logs with Kronos/Astra switch — reads **disk** `*/data/trades/trade_history.json` (not shm)
- Signal labels: raw `NEUTRAL` / `BOOT` / idle → UI **Waiting**
- Funds labeled **Paper funds** from each engine’s `capital_state.json`
- **Kill switch UI removed** (travel-era control); `KILL_SWITCH_ACTIVE` file may still exist in shm — do not rebuild kill UI unless operator asks

APIs: `/`, `/api/health`, `/api/live_state`.

Shadow analytics notes (optional/legacy): `unified_dashboard/shadow_mode/` — not required for core paper path.

---

## 9. Calendar

File: `common/market/market_calendar.py`

| Constant | Time IST | Notes |
|---|---|---|
| `PRE_MARKET_TIME` | 08:55 | Bot wakeup / handshake |
| `MARKET_OPEN_TIME` | 09:15 | Continuous open |
| `KRONOS_ENTRY_CUTOFF_TIME` | 14:50 | Kronos last new entry (see also IDEAS K-ENTRY-1500) |
| `KRONOS_FORCE_FLATTEN_TIME` | 15:10 | Flatten at start of last continuous 5m bar |
| `TRADE_ENTRY_CUTOFF_TIME` | 15:00 | Shared helper (Astra/CAS); Kronos uses EM gates |
| `MARKET_CLOSE_TIME` | **15:15** | Continuous cash close (CAS-era house standard) |
| `POST_MARKET_TIME` | **15:17** | EOD REST after last bar **closes** at 15:15 (label 15:10) |
| F&O session (exchange) | → 15:40 | Do not use as Kronos hold license |

Also: NSE/BSE holiday set 2026; weekend skip; deep sleep until next pre-market.

---

## 10. Capital / paper trading / strikes

### 10.1 Paper wallet

- Each engine owns `data/capital_state.json` via `CapitalEngine` (`common/execution/capital_engine.py`).
- Start: `INITIAL_PAPER_CAPITAL = 2500.0`.
- Deduct on entry (premium × qty), credit on exit (paper). Qty = **1 lot × lot_size** (shares).
- Dashboard “Paper funds” = this file — **not** broker cash.
- Live brokerage only when `ENABLE_LIVE_BROKERAGE_EXECUTION=True` **and** operator explicitly enables after paper proof.

### 10.2 Strike selection (shared)

`common/execution/strike_selector.py`:

1. Compute ATM from spot (index-specific strike step).
2. Walk ATM → progressively OTM (depth up to ~30).
3. Filter by premium band (`NIFTY_PREMIUM_MIN/MAX`, `SENSEX_PREMIUM_MIN/MAX`).
4. Pick first affordable **1-lot** cost ≤ available paper funds.

Premium bands (`common/config.py`):

| Index | Min ₹ | Max ₹ | Lot |
|---|---|---|---|
| NIFTY | 15 | 120 | 65 |
| SENSEX | 20 | 200 | 20 |

Entry limit buffer (marketable LMT): `ENTRY_LIMIT_BUFFER_PCT=0.025`, floor `ENTRY_LIMIT_MIN_BUFFER_PTS=3.0`, tick `OPTION_TICK_SIZE=0.05`.

### 10.3 Product / live flag

| Constant | Default | Note |
|---|---|---|
| `DEFAULT_PRODUCT_TYPE` | `"N"` | Official NRML is often `"M"` — **validate vs `prarr` before live** |
| `ENABLE_LIVE_BROKERAGE_EXECUTION` | `False` | Env override |
| `BROKER` | `SHOONYA` | Env override |
| `OI_REVERSAL_EXIT_*` | off / 50k / 60s | **Astra KDO only** |

---

## 11. Safety checklist before enabling live capital

1. Paper proves: entry → DAY TP place → Chandelier or time or **15:10 flatten** path.
2. Confirm product code `M` vs `N` vs account `prarr`.
3. Confirm DAY TP visible / cancels correctly on flatten.
4. No open Kronos options after 15:10 IST (especially Friday / expiry week).
5. Hub shows non-zero synth vol during live session for active index.
6. Parquet volumes are **not** flat `10000000`.
7. Shoonya still only from VPS IP.
8. `AI_CHANGELOG.md` updated for the change that flips live flag.

---

## 12. What must never happen again

- Kronos using Astra/KDO exit rules
- Assuming resting TP survives overnight (**GTC fantasy**)
- Entries after ~15:00 on a 10-minute strategy
- Weekly option left open into close / weekend / expiry
- Silent “NEUTRAL forever” because `transformers` import failed while PM2 stays “online”
- Shoonya API from non-whitelisted local IP
- Holding Kronos past **15:10**
- Fake `volume=10_000_000` in the ML window
- Mixing NIFTY and SENSEX constituent accumulators across day rotation
- EOD synthetic volume via unnecessary **1m** constituent pulls (use **5m**)
- Deleting/rewriting this architecture without migrating facts into the new version

---

## 13. Agent operating rules

1. Prefer editing Kronos path until paper-stable; leave Astra alone unless asked.
2. Update `ARCHITECTURE.md` + append `AI_CHANGELOG.md` after non-trivial work; refresh `SEMANTIC_MAP.md` if ownership/paths moved.
3. Restart PM2 selectively at **end of batch**.
4. Never commit secrets (`.env`, session JSON, SSH private keys).
5. Never invent broker features Shoonya does not support (GTC, etc.).
6. When unsure about order product codes — verify against live `prarr`, don’t guess.

---

## 14. Implementation status snapshot (2026-08-05)

| Item | Status |
|---|---|
| Modular `common/` + hub + dual engines + light dashboard | Done |
| Kronos local model load (no `transformers`) | Done |
| Separate Kronos EM (TP + **−25% hard SL** + Chandelier + 600s + **15:10** flatten) | Done (paper) |
| Per-index 512-bar windows + REST candle repair | Done |
| Live WS synthetic constituent volume (hub touchline + tick map) | Done (verified climbing Aug 5) |
| Kronos REST synth-vol fallback when hub=0 | Done |
| Inference timestamp / `_no_grad_ctx` mask fix | Done (verified NEUTRAL) |
| Premarket sleep to 09:14:30 + active-index refresh | Done |
| EOD/backfill REST **5m** constituent volumes | Done |
| Dashboard Kronos-left + CALL/PUT colors + trade history | Done |
| `SEMANTIC_MAP.md` code navigation | Done |
| Shoonya IP whitelist documented | Done |
| Product code `M` vs `N` confirmation | **Open** — before live capital |
| Full paper entry → TP/SL/Chandelier/15:10 on clean SENSEX day | **Pending** observe |
| Astra deep work | **Deferred** until Kronos paper-stable |

---

## 15. Quick file map (where to edit what)

**Full semantic map (folder → class → function → symptom triage):** [`SEMANTIC_MAP.md`](SEMANTIC_MAP.md)

| Concern | Primary file(s) |
|---|---|
| Kronos loop / EOD / candle logs | `kronos_bot/main.py` |
| Candle OHLC+vol + REST repair | `kronos_bot/core/data_fetcher.py` |
| Signal + Supertrend | `kronos_bot/strategy/signal_generator.py`, `supertrend.py` |
| Entries/exits (TP / −25% SL / Chandelier) | `kronos_bot/execution/kronos_execution_manager.py` |
| Model / timestamps | `kronos_bot/model/kronos.py` |
| Hub ticks + WS synth vol | `shoonya_data_hub/main.py` |
| WS accumulator / tick map | `common/broker/shoonya_adapter.py` |
| Constituents | `institutional_memory/constituents.json` |
| Calendar / CAS times | `common/market/market_calendar.py` |
| Dashboard | `unified_dashboard/dashboard.py` |
| PM2 | `ecosystem.config.js` |
| Paper capital default | `common/config.py` |

---

When this file conflicts with older chat notes, deleted markdown, or stale comments in code, **this file wins** — then fix the code/comments to match.

---

## 16. End-to-end dataflow (Kronos candle → trade)

```text
Constituent equities (50/30)
        │ WS LTQ
        ▼
Shoonya adapter accumulator (_accumulated_synthetic_vol)
        │ 5m bucket close → get_and_reset
        ▼
/dev/shm/shoonya_live_ticks.json  (closed synth vol + quotes)
        │
        ├──────────────────────────────┐
        ▼                              ▼
Kronos @ :02 boundary            Astra microstructure loop
  REST index 5m OHLC
  join synth vol → amount
  append parquet (512)
  Kronos predict + Supertrend
  (Net ΔOI shadow only)
        │
        ▼
KronosExecutionManager
  entry 09:20–14:50 → 1 lot CE/PE
  resting DAY LMT TP +10%
  manage_open_trade every ~1s:
    TP fill | ≥15:10 flatten | Chandelier | 600s
        │
        ▼
disk: active_trade_context.json / trade_history.json / capital_state.json
        │
        ▼
Dashboard :5000 (shm metrics + disk history)
```

Inactive index parquet is **not** updated live; it is warmed by **EOD ~15:31 REST 5m** backfill for both indices.

---

## 17. Auth, session, and broker adapters

### 17.1 Where credentials live

- Root `.env` and/or `kronos_bot/.env` / engine `.env` (never commit; never paste into docs).
- Shoonya session restore typically via adapter `session_provider._restore_or_login()` (hub starts this first).
- HuggingFace model weights: VPS `~/.cache/huggingface/` after first download (`NeoQuasar/Kronos-base`, `NeoQuasar/Kronos-Tokenizer-base`).

### 17.2 Adapter layout (intentional duplication / shims)

Historical copies exist under `astra_bot/core/*`, `kronos_bot/core/*`, `shoonya_data_hub/core/*`.  
**Canonical shared broker code** is under `common/broker/` (`shoonya_adapter.py`, `shoonya_auth.py`, `session_manager.py`, `interfaces.py`, `rate_limiter.py`, `ring_buffer.py`).

Agents should:

- Prefer `common/broker` + `common/execution` for new shared behavior.
- Not “clean up” duplicate adapters mid-session unless asked — risk of breaking imports on VPS.
- `shared_runtime/paths.py` is a thin re-export for tests → `common.runtime.paths`.

### 17.3 Rate limiting

`common/broker/rate_limiter.py` guards REST bursts (important for EOD constituent volume pulls — chunked ~7-day 5m windows). Do not hammer full chain every second.

---

## 18. VPS operations (agents)

| Item | Value |
|---|---|
| Host | `opc@137.23.41.38` |
| Local SSH key | `Oracle SSH keys/ssh-key-2026-07-11.key` |
| Deploy cwd | `/home/opc` (= monorepo root on VPS) |
| PYTHONPATH | `/home/opc` |
| PM2 config | `/home/opc/ecosystem.config.js` |
| Shm | `/dev/shm/*.json` |
| Kronos data | `/home/opc/kronos_bot/data/` |
| Logs | PM2 + per-bot `*/logs/` |

### 18.1 Common commands

```bash
# status
pm2 status
pm2 logs kronos-bot --lines 100
pm2 logs shoonya-data-hub --lines 80

# selective restart (end of batch)
pm2 restart kronos-bot
pm2 restart shoonya-data-hub
pm2 restart unified-dashboard
# avoid: pm2 restart all  (unless intentional full recycle)

# quick shm peek
python3 -c "import json;print(json.load(open('/dev/shm/shoonya_live_ticks.json')).get('synthetic_constituent_volume'))"
```

### 18.2 Deploy hygiene

- Sync changed files with `scp`/`rsync` from laptop → `/home/opc/...`.
- Restart **only** processes that load the changed code.
- Do not leave orphan long-running `/tmp/test_*.py` processes (past incident: Jul-29 ghosts).
- Do not recreate deleted junk trees (`quant_bot/`, old archives) on VPS.

### 18.3 What local laptops may do

- Edit code/docs, run unit tests that do **not** call Shoonya.
- **Must not:** Shoonya login, WS, place/cancel orders, or pretend paper fills need local broker.

---

## 19. Failure modes and how to diagnose

| Symptom | Likely cause | Where to look |
|---|---|---|
| Kronos “online” but forever Waiting / NEUTRAL | Model load failed (`transformers` mistake) or window &lt; warm-up | `kronos-bot` logs; confirm `kronos_bot/model` import path |
| Predictions weird / flat volume | Fake `10_000_000` or hub synth=0 | parquet `volume` column; hub `synthetic_*` fields |
| No synth vol at open | Hub not subscribed / wrong constituents / rotation not reset | hub logs `[SynthVol]`; `constituents.json` |
| TP vanished overnight / weekend loss | DAY retention + no flatten | Never again — force flatten ≥15:10; no Friday late entries |
| Entry after 14:50 | Wrong EM / old cutoff | `KronosExecutionManager.can_enter` |
| Still in trade after 15:10 | Flatten not wired / EM not Kronos | Confirm `kronos_execution_manager.py` in `main.py` |
| Dashboard funds ≠ expectation | Reading wrong `capital_state.json` | Per-engine path under `astra_bot/data` vs `kronos_bot/data` |
| Local API errors | IP not whitelisted | Stop; use VPS only |
| SENSEX day empty parquet | Missed prior EOD backfill | Run/wait 15:31 both-index REST rebuild |
| PM2 flap / OOM | Model + torch memory | `max_memory_restart` in ecosystem; check RSS |

---

## 20. Environment variables (non-secret catalogue)

| Variable | Typical | Effect |
|---|---|---|
| `BOT_RUNTIME` | `hub` / `astra` / `kronos` | Set by PM2 `env` + `configure_runtime` |
| `BOT_DATA_DIR` / `BOT_LOGS_DIR` | set by configure_runtime | Per-process data/logs |
| `PYTHONPATH` | `/home/opc` | Monorepo imports |
| `ENABLE_LIVE_BROKERAGE_EXECUTION` | `False` | Paper vs live orders |
| `BROKER` | `SHOONYA` | Active broker stack |
| `OI_REVERSAL_EXIT_ENABLED` | `False` | Astra KDO optional exit |
| HuggingFace / Shoonya secrets | in `.env` | **Never commit; never document values** |

---

## 21. Kronos intrabar management detail

Between 5m prediction boundaries, `kronos_bot/main.py` still calls `trader.manage_open_trade(...)` on a **~1 second** cadence while market session is active. That loop:

1. Refreshes option mark / LTP.
2. Detects TP fill (broker fill **or** paper synthetic premium ≥ +10%).
3. If `now ≥ 15:10` → force flatten (cancel TP, marketable LMT sell).
4. Else evaluate Chandelier on latest 5m underlying window (ratchet favorable only).
5. Else if hold ≥ 600s → time stop.
6. Persist active context to disk so a mid-session PM2 restart can restore OPEN risk and keep enforcing exits.

On non-TP exits: **cancel TP order id first**, then exit sell. Strategy tag: `KRONOS_AI`.

Paper mode may arm `PAPER_TP_*` synthetic ids when live resting place is skipped — still must flatten by 15:10.

---

## 22. Tests, scripts, and non-runtime trees

| Area | Notes |
|---|---|
| `tests/` | Path wiring / dashboard smoke (`test_shared_runtime_paths.py`, `test_combined_dashboard.py`, …) |
| `kronos_bot/tests/` | e.g. Supertrend filter |
| `kronos_bot/scripts/` | Backtests / compounding / simulation — **not** PM2 live path |
| `kronos_bot/upgrade_templates/` | Reference snippets (Chandelier / Supertrend) — do not import as live modules blindly |
| Duplicate `execution/data_fetcher.py` under kronos | Historical; **live** candle path is `kronos_bot/core/data_fetcher.py` |
| Duplicate root `architecture.md` | Same canonical content as `ARCHITECTURE.md` on case-insensitive FS — prefer **`ARCHITECTURE.md`** name in links |

---

## 23. Glossary (so agents share vocabulary)

| Term | Meaning here |
|---|---|
| **CAS** | Closing Auction Session (cash); house flatten 15:10 for Kronos options |
| **DAY retention** | Resting order dies at EOD — not GTC |
| **NRML** | Carry-capable product; options here must still be flattened same day by strategy |
| **Synthetic volume** | Sum of constituent equity volumes (WS LTQ live / REST 5m EOD), not index cash volume |
| **Fake 10M** | Old broken filler `volume=10_000_000` — banned |
| **KDO EM** | Astra `common/execution/execution_manager.py` |
| **Kronos EM** | `kronos_bot/execution/kronos_execution_manager.py` |
| **Shadow OI** | Net Delta OI logged but never blocks Kronos entry |
| **Paper funds** | Disk wallet, not broker ledger |
| **Active index** | Weekday-rotated NIFTY or SENSEX |
| **512 window** | Kronos `max_context` bars of 5m OHLCVA |

---

## 24. Open items / next session checklist

1. **Paper-test** remaining session + next 1–2 days: clean candles → entry → +10% TP **or** −25% hard SL **or** Chandelier **or** 15:10 flatten. Review `decision_journal.jsonl` before live capital.
2. Confirm product code **`M` vs `N`** vs account `prarr` before any live capital.
3. Watch late-session volume quirks (CAS flat bars / occasional spikes) — log, don’t silently fake.
4. Keep Astra deferred until Kronos paper-stable.
5. **EOD / peer-review ideas:** [`IDEAS.md`](IDEAS.md) **K-ST-1m**; **K-ST-GAP** (open/gap ST challenge — problem only until operator submits a design). Do not change mid-session.
6. After each non-trivial change: update **this file** + append **`AI_CHANGELOG.md`** + refresh **`SEMANTIC_MAP.md`** if paths moved; sync docs to VPS `/home/opc/`.

---

## 25. Reading order for a brand-new AI agent

1. §0 agent instructions  
2. §1 mission + Friday incident  
3. §12 never-again list  
4. [`SEMANTIC_MAP.md`](SEMANTIC_MAP.md) — where code lives / symptom → file  
5. §6 Kronos full detail + §16 dataflow  
6. §3 Shoonya rules + §18 VPS ops  
7. §7 hub schemas + §19 failure modes  
8. §14 status snapshot + §24 open checklist  
9. Skim §5 Astra only enough to avoid contaminating Kronos  
10. Append outcomes to `AI_CHANGELOG.md`

**End of canonical architecture.** Navigation → [`SEMANTIC_MAP.md`](SEMANTIC_MAP.md); living history → [`AI_CHANGELOG.md`](AI_CHANGELOG.md).
