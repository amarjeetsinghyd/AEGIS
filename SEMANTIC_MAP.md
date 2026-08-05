# Quant-Bot Semantic Code Map

**Purpose:** Tell any AI/engineer *which folder owns what*, *which file to open for a symptom*, and *which function does the work*.  
**Canonical design:** [`ARCHITECTURE.md`](ARCHITECTURE.md)  
**Living history:** [`AI_CHANGELOG.md`](AI_CHANGELOG.md)  
**Updated:** 2026-08-05 (session: WS synth vol, inference fix, −25% SL, candle logs)

---

## 1. Process → entrypoint → owns

| PM2 name | Entrypoint | Owns |
|---|---|---|
| `shoonya-data-hub` | `shoonya_data_hub/main.py` | Shoonya session, WS quotes, constituent LTQ → synth vol buckets, `/dev/shm/shoonya_live_ticks.json` |
| `kronos-bot` | `kronos_bot/main.py` | 5m Kronos loop, signals, Kronos EM, parquet windows |
| `astra-bot` | `astra_bot/main.py` | KDO microstructure loop + shared KDO EM |
| `unified-dashboard` | `unified_dashboard/dashboard.py` | HTTP :5000 Quant Console |

`ecosystem.config.js` — PM2 definitions (`cwd=/home/opc`, `PYTHONPATH=/home/opc`).

---

## 2. Folder meaning (one sentence each)

| Folder | Meaning |
|---|---|
| `common/` | **Shared kernel only** — broker, capital/strikes/orders, calendar, IPC, config. Prefer editing here for cross-engine behavior. |
| `shoonya_data_hub/` | **Sole live tick + synth-vol producer**. Do not duplicate WS login in bots for market bus. |
| `kronos_bot/` | **ML engine** — model, Supertrend, Kronos EM, 5m candles. Priority until paper-stable. |
| `astra_bot/` | **KDO engine** — microstructure; uses `common.execution.ExecutionManager`. Deferred deep work. |
| `unified_dashboard/` | Read-only console over shm + disk trade/capital files. |
| `institutional_memory/` | `constituents.json` (NIFTY50 + SENSEX30 Shoonya tokens). |
| `shared_runtime/` | Thin re-export of `common.runtime.paths` for tests. |
| `tests/` | Path/dashboard smoke tests (local; no Shoonya). |
| `scripts/` | VPS helper shell scripts (audit/deploy/restart). |

---

## 3. “I need to change X → open Y”

| Intent | Primary file(s) | Key symbols |
|---|---|---|
| Kronos main loop / premarket sleep / candle logs | `kronos_bot/main.py` | `main`, `_chunked_sleep_until`, `get_shared_oi_delta_details` |
| Active index Mon–Fri rotation | `kronos_bot/core/data_fetcher.py` + hub `_active_index` | `DataFetcher.get_active_instrument` |
| Live / EOD candles + synth vol join | `kronos_bot/core/data_fetcher.py` | `get_hub_synthetic_volume`, `rest_synthetic_volume_for_latest_bar`, `rest_backfill_today_candles`, `append_live_candle`, `get_window_df` |
| Kronos CALL/PUT signal | `kronos_bot/strategy/signal_generator.py` | `KronosSignalGenerator.predict`, `_load_predictor`, `_no_grad_ctx` |
| 1m OHLC for ST gate | `kronos_bot/core/minute_bars.py` | `MinuteBarAggregator`, `seed_minute_bars_from_rest` |
| Supertrend gate (1m / bypass) | `kronos_bot/strategy/supertrend.py` | `latest_supertrend`, `supertrend_bias`, `filter_entry_signal` |
| Kronos entries/exits (TP/SL/Chandelier/flatten) | `kronos_bot/execution/kronos_execution_manager.py` | `KronosExecutionManager`, `manage_open_trade`, `execute_entry`, `compute_chandelier_stops` |
| Model math / timestamps | `kronos_bot/model/kronos.py` | `KronosPredictor.predict`, `calc_time_stamps`, `auto_regressive_inference` |
| Model modules | `kronos_bot/model/module.py` | tokenizer / transformer blocks |
| Hub tick bus + synth buckets | `shoonya_data_hub/main.py` | `main`, `VolumeBucket`, `_build_payload`, `_subscribe_cash_book` |
| WS LTQ accumulator / quote cache | `common/broker/shoonya_adapter.py` | `ShoonyaMarketDataProvider` (`subscribe`, `_on_ws_tick`, `register_constituent_tokens`, `get_and_reset_synthetic_volume`, `force_rest_quote`) |
| Broker login | `common/broker/shoonya_auth.py` + session in adapter | `ShoonyaSessionProvider._restore_or_login` |
| Historical REST OHLC/vol | `common/broker/shoonya_adapter.py` | `ShoonyaHistoricalProvider.get_historical` |
| Shared KDO exits (**Astra only**) | `common/execution/execution_manager.py` | `ExecutionManager` |
| Paper/live orders | `common/execution/order_lifecycle.py` | `PaperOrderLifecycle`, `ShoonyaOrderLifecycle` |
| Strike pick | `common/execution/strike_selector.py` | `StrikeSelector.select_strikes` |
| Paper wallet | `common/execution/capital_engine.py` | `CapitalEngine` |
| Trade state objects | `common/execution/trade_context.py` | `TradeContext`, `TradeLeg` |
| Calendar / CAS times | `common/market/market_calendar.py` | `is_pre_market`, `KRONOS_FORCE_FLATTEN_TIME`, `sleep_until_next_premarket` |
| Lot / premium / live flag | `common/config.py` | `configure_runtime`, `INITIAL_PAPER_CAPITAL`, `ENABLE_LIVE_BROKERAGE_EXECUTION` |
| Shm read/write | `common/ipc/live_state.py` | `update_kronos_live_state`, `SHARED_TICK_FILE`, `_atomic_write` |
| Paths | `common/runtime/paths.py` | `get_project_root`, `get_runtime_paths` |
| Dashboard UI/API | `unified_dashboard/dashboard.py` | `live_state`, `index`, signal CSS CALL/PUT |
| Constituent tokens | `institutional_memory/constituents.json` | `nifty_50`, `sensex_30` |

---

## 4. Runtime data flow (who calls whom)

```text
Shoonya WS/REST
    │
    ▼
common/broker/shoonya_adapter.py
    │  LTQ → _accumulated_synthetic_vol
    │  quotes → _live_quotes
    ▼
shoonya_data_hub/main.py
    │  VolumeBucket.close → synthetic_*_volume
    │  _atomic_write
    ▼
/dev/shm/shoonya_live_ticks.json
    │
    ├──────────────────┬────────────────────┐
    ▼                  ▼                    ▼
kronos_bot/main.py   astra_bot/main.py   unified_dashboard/
    │                  │
    ├ data_fetcher     ├ microstructure
    │  (parquet)       │
    ├ signal_generator ├ common.execution.ExecutionManager
    │                  │
    └ KronosExecutionManager
         │
         ├ CapitalEngine / StrikeSelector / OrderLifecycle (common)
         └ disk: kronos_bot/data/trades/*, capital_state.json
```

**Kronos 5m boundary sequence (logged as 1/4…4/4):**
1. Hub WS synth vol (`get_hub_synthetic_volume`) → else REST constituents  
2. REST index 5m OHLC (`historical_provider.get_historical`)  
3. `amount = mean(OHLC) × volume`  
4. `append_live_candle` → `get_window_df` → `signal_generator.predict` → maybe `execute_entry`

---

## 5. File-level semantic detail

### 5.1 `common/broker/shoonya_adapter.py`
| Class / fn | Role |
|---|---|
| `ShoonyaAdapter` | Facade: session + market + hist + exec + portfolio |
| `ShoonyaMarketDataProvider.subscribe` | WS touchline (`feed_type=t`) |
| `register_constituent_tokens` | Replace set + reset accumulator (NIFTY↔SENSEX) |
| `event_handler_quote_update` (inside `start_live_feed`) | Raw LTQ → synth accum |
| `_on_ws_tick` | Normalized/raw fields → `_live_quotes` cache |
| `force_rest_quote` | Unstick spot LTP via REST |
| `ShoonyaHistoricalProvider.get_historical` | REST OHLCV DataFrame (`FIVE_MINUTE`, etc.) |

### 5.2 `shoonya_data_hub/main.py`
| Symbol | Role |
|---|---|
| `_active_index` | Wed/Thu SENSEX else NIFTY |
| `_subscribe_cash_book` | Spot + constituents touchline sub |
| `VolumeBucket.tick` | Close 5m bucket → `get_and_reset_synthetic_volume` |
| `_build_payload` | shm JSON (quotes + synth fields); REST spot every ~5s |
| `main` loop | Rotate index; re-sub if `live_accum=0` mid-session |

### 5.3 `kronos_bot/core/data_fetcher.py` (`DataFetcher`)
| Method | Role |
|---|---|
| `get_active_instrument` | Weekday → NIFTY/SENSEX |
| `get_hub_synthetic_volume` | Read closed bucket from shm |
| `rest_synthetic_volume_for_latest_bar` | Fallback sum of REST 5m constituent vols |
| `rest_backfill_today_candles` | Full window rebuild OHLC+synth (repair corrupt bars) |
| `append_live_candle` / `get_window_df` | Parquet sliding window (512) |

### 5.4 `kronos_bot/execution/kronos_execution_manager.py`
| Method | Role |
|---|---|
| `can_enter` / `must_force_flatten` | 09:20–15:00 / ≥15:10 |
| `execute_entry` | 1-lot CE/PE + resting +10% DAY TP |
| `manage_open_trade` | TP → flatten → **−25% hard SL** → Chandelier → 600s |
| `compute_chandelier_stops` | 5m underlying period 10 mult 2.0 |

### 5.5 `kronos_bot/strategy/signal_generator.py`
| Method | Role |
|---|---|
| `_load_predictor` | `Kronos` + `KronosTokenizer` from HF (no `transformers`) |
| `predict` | Δ% gate 0.10% + Supertrend(10,3) → CALL/PUT/NEUTRAL; logs `[SUPERTREND] UPTREND|DOWNTREND` then `[SIGNAL]` |
| `_no_grad_ctx` | Must **not** swallow body `AttributeError` |

### 5.6 `kronos_bot/model/kronos.py`
| Symbol | Role |
|---|---|
| `calc_time_stamps` | **DatetimeIndex**-safe (no `.dt` on index) |
| `KronosPredictor.predict` | z-score window → autoregressive forecast |

### 5.7 Astra (deferred)
| File | Role |
|---|---|
| `astra_bot/main.py` | Tick loop + handshake |
| `astra_bot/core/microstructure_engine.py` | KDO signals |
| `common/execution/execution_manager.py` | KDO exits only |

### 5.8 Dashboard
| File | Role |
|---|---|
| `unified_dashboard/dashboard.py` | Kronos left / Astra right; CALL green / PUT red; steel-blue Kronos brand |

---

## 6. Disk / shm artifacts (by owner)

| Path | Writer | Consumer |
|---|---|---|
| `/dev/shm/shoonya_live_ticks.json` | hub | Kronos, Astra, dashboard |
| `/dev/shm/kronos_live_state.json` | Kronos | dashboard |
| `/dev/shm/kdo_live_state.json` | Astra | dashboard |
| `kronos_bot/data/underlying_candles_{NIFTY,SENSEX}.parquet` | Kronos fetcher | Kronos model window |
| `kronos_bot/data/capital_state.json` | Kronos CapitalEngine | dashboard |
| `kronos_bot/data/trades/trade_history.json` | Kronos EM | dashboard |
| `kronos_bot/data/trades/active_trade_context.json` | Kronos EM | restart restore |
| `astra_bot/data/...` | Astra | dashboard |
| `institutional_memory/constituents.json` | ops/human | hub + Kronos REST synth |

---

## 7. Duplicates / traps (do not “clean up” blindly)

| Trap | Truth |
|---|---|
| `kronos_bot/execution/data_fetcher.py` | Legacy; **live** path is `kronos_bot/core/data_fetcher.py` |
| `astra_bot/core/shoonya_adapter.py` etc. | Historical copies; prefer `common/broker` |
| `get_historical_candles` on hub broker | **Does not exist** on `ShoonyaHistoricalProvider` — use `get_historical` |
| Root `architecture.md` vs `ARCHITECTURE.md` | Same content on Windows; prefer `ARCHITECTURE.md` |
| Nested `shoonya_data_hub/reference/ShoonyaApi-py` | Ignored vendor clone; use upstream GitHub |

---

## 8. Docs map

| Doc | Use when |
|---|---|
| `ARCHITECTURE.md` | Design rules, calendars, exits, never-again |
| `SEMANTIC_MAP.md` (this file) | “Where is the code?” |
| `AI_CHANGELOG.md` | What changed recently / why |
| `shoonya_data_hub/docs/SHOONYA_ORDER_RULES.md` | DAY/NRML/IP whitelist |
| `README.md` | Short pointer |

---

## 9. 2026-08-05 session symptoms → files (quick triage)

| Symptom | Likely file |
|---|---|
| Active index wrong overnight | `kronos_bot/main.py` (must refresh `get_active_instrument` every loop) |
| Synth vol always 0 / stuck spot | `common/broker/shoonya_adapter.py` + `shoonya_data_hub/main.py` |
| Flat O=H=L=C on live candles | `kronos_bot/main.py` REST OHLC step (must call `get_historical`) |
| `generator didn't stop after throw()` | `kronos_bot/model/kronos.py` `calc_time_stamps` + `signal_generator._no_grad_ctx` |
| Corrupt morning bars in window | `DataFetcher.rest_backfill_today_candles` (disk repair; no restart required) |
| Want exit on −25% premium | `kronos_execution_manager.manage_open_trade` |

---

When docs conflict, **ARCHITECTURE.md wins** for policy; this map wins for *navigation*.
