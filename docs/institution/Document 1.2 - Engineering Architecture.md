# Document 1.2 - Engineering Architecture

# DOC-1.2 — Engineering Optimization Roadmap
## Architecture Specification Document (ASD)
### Version 1.0 — FROZEN

> **STATUS: FROZEN**
> Accepted as Version 1 on 2026-06-27.
> This document must not be modified unless a Version 2 is explicitly created and approved.
> Institution building is deferred. Engineering execution is the current priority.

---

## 1. Purpose

This document defines the engineering optimization roadmap for the QuantOS runtime.
It governs the sequence and scope of implementation work from Phase 6.2 onward
until the system is production-ready for continuous research-grade operation.

---

## 2. Governing Constraints

- The existing trading strategy (VFI + 9 EMA + VWAP) must be preserved exactly as-is.
- The trading bot must remain stable throughout all optimization work.
- No institutional redesign during this phase.
- No new heavyweight ML infrastructure.
- Only XGBoost is retained as lightweight, currently-justified ML.
- All code for disabled components must be preserved (not deleted).
- The architecture must remain extensible for future institutional capabilities.

---

## 3. Phase Roadmap

### Phase 6.3 — Dependency & Runtime Optimization
**Objective:** Reduce pip install time, RAM footprint, and import cost.

| Task | Action | Target |
|---|---|---|
| Remove scikit-learn | Uninstall, remove from requirements.txt | -50MB RAM |
| Remove lightgbm | Uninstall, remove from requirements.txt | -80MB RAM |
| Remove catboost | Uninstall, remove from requirements.txt | -200MB RAM |
| Remove shap | Uninstall, remove from requirements.txt | -30MB RAM |
| Remove duckdb | Uninstall, remove from requirements.txt | -40MB RAM |
| Remove google-generativeai | Uninstall, remove from requirements.txt | -120MB install |
| Retain xgboost | Keep in requirements.txt | Lightweight ML |
| Retain pyzmq | Keep — message bus critical | Required |

**Retained dependencies:** SmartApi-Python, pyotp, logzero, websocket-client,
pandas, python-dotenv, flask, xgboost, pyzmq, pyarrow

---

### Phase 6.4 — Shadow Service Isolation
**Objective:** Disable heavyweight ML service without removing code.

| Task | Action |
|---|---|
| Disable shadow_service.py in start_all.py | Comment out p_shadow, add SHADOW_ENABLED flag |
| Add engineering_config.SHADOW_ENABLED = False | Config-driven disable |
| Preserve all shadow_service.py code intact | Do not delete — re-enable in future |
| Preserve ml_engine/ directory entirely | All ML code preserved, just not loaded |

---

### Phase 6.5 — Research Data Collection Service
**Objective:** Continuous research-grade data collection to Parquet.

| Task | Action |
|---|---|
| Create src/services/research_collector.py | New lightweight service |
| Subscribe to FEED_PORT ZMQ ticks | Independent from brain_service |
| Buffer ticks in memory (deque, max 10k) | Low RAM circular buffer |
| Flush to Parquet every N bars or on schedule | pyarrow, partitioned by date |
| Store: timestamp, symbol, ohlcv, vfi, ema, vwap, volume | Full feature row |
| Write to DATA_DIR/research/YYYY-MM-DD.parquet | Partitioned by date |
| Add to start_all.py as p_research | Independent process |
| Add RESEARCH_ENABLED flag to engineering_config | Config-driven |

---

### Phase 6.6 — main.py Orchestration Reduction
**Objective:** main.py must only be the Flask UI node — no business logic.

| Task | Current State | Target State |
|---|---|---|
| ZMQ listener thread | In main.py | Keep — UI needs telemetry |
| History state management | In main.py | Keep — UI state |
| Flask routes | In main.py | Keep — UI endpoints |
| Any indicator computation | Move out if present | To brain_service |
| Any trade logic | Move out if present | To brain_service |

*main.py is already close to orchestration-only. Verify no business logic present.*

---

### Phase 6.7 — Replay Capability Verification
**Objective:** Confirm market_replay.py works against Parquet archives.

| Task | Action |
|---|---|
| Verify src/ml_engine/market_replay.py loads Parquet | Test against real archive |
| Verify replay publishes to message bus correctly | ZMQ replay feed |
| Document replay invocation in README | Usage instructions |

---

### Phase 6.8 — Journaling & Feature Generation Audit
**Objective:** Confirm journal and features are being written on every trade.

| Task | Action |
|---|---|
| Verify trade journal entries written to SQLite | brain_service.py SIGNAL_RESOLVED |
| Verify feature rows written per bar | db_writer_queue.py |
| Confirm Parquet archival runs nightly | parquet_archiver.py |
| Add health-check log line per flush | Observability |

---

### Phase 6.9 — CPU / Memory / Disk Optimization
**Objective:** System runs continuously on a personal PC without degradation.

| Task | Action |
|---|---|
| Remove profiling overhead | PROFILING_ENABLED = False (already default) |
| Limit log rotation | logs/ directory — max 7 days retention |
| Limit SQLite WAL growth | db_writer_queue flush interval tuning |
| Parquet compression | Use snappy compression in pyarrow writes |
| DataFrame memory | Use float32 where float64 not needed |
| ZMQ socket cleanup | Confirm all sockets closed on KeyboardInterrupt |

---

## 4. Success Criteria

The engineering optimization phase is complete when:

1. `pip install -r requirements.txt` completes in under 60 seconds
2. All 4 active services start cleanly from `start_all.py`
3. Trading bot executes at least one full session without crash
4. Research Parquet files are written every trading session
5. Replay from Parquet produces the same indicators as live
6. Total RAM footprint of all services under 400MB idle
7. No log files older than 7 days accumulate on disk

---

## 5. Future Integration Points (Not This Phase)

The following are reserved for a future version and must not be implemented now:

- Live brokerage execution (AngelOne live orders)
- Institutional research report generation
- Shadow predictor re-enablement
- RL exit optimizer
- LSTM sequence modelling
- Ensemble engine
- Multi-instrument trading
- Automated strategy discovery