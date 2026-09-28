# Document 1.0 - Capability Ontology

# DOC-1.1 — Capability Ontology
## Architecture Specification Document (ASD)
### Version 1.0 — FROZEN

> **STATUS: FROZEN**
> Accepted as Version 1 on 2026-06-27.
> This document must not be modified unless a Version 2 is explicitly created and approved.
> Do not expand institutional philosophy during the current engineering execution phase.

---

## 1. Purpose

This document defines the Capability Ontology for the QuantOS research and trading runtime.
It establishes the canonical vocabulary for all capabilities the system possesses, grouped
by domain. It is a governing document for architecture decisions and future institutional design.

---

## 2. Capability Domains

### 2.1 Market Data
- Real-time tick ingestion via AngelOne WebSocket
- OHLCV bar construction from raw ticks
- Market calendar awareness (NSE trading sessions)
- Instrument token resolution and caching

### 2.2 Signal Generation
- VFI (Volume Flow Indicator) computation
- 9-period EMA computation
- VWAP computation
- Signal condition evaluation (VFI cross zero + 9 EMA cross VWAP)
- Setup registration and lifecycle management

### 2.3 Execution
- Paper trading execution (PaperTrader)
- Sniper entry logic (sniper_hunt)
- Open trade management (manage_open_trade)
- 10% profit target limit order logic
- Stop-loss computation from support/resistance

### 2.4 Research Data Collection
- Continuous market tick storage
- OHLCV bar archival to Parquet
- Trade journal entry generation
- Feature vector generation per bar
- Replay capability from archived Parquet data

### 2.5 Machine Learning (Deferred)
- XGBoost signal quality scoring (lightweight — retained)
- Shadow predictor (disabled in Phase 6 — re-enable in future version)
- LSTM sequence modelling (disabled — heavyweight)
- Ensemble engine (disabled — heavyweight)
- Drift detection (disabled — heavyweight)
- Survival model (disabled — heavyweight)
- RL exit optimizer (disabled — heavyweight)

### 2.6 Observability
- Structured logging via logzero
- ZeroMQ message bus telemetry
- Flask UI node with real-time chart data
- Instrumentation hooks (profiling-disabled by default)

### 2.7 Infrastructure
- ZeroMQ multi-service message bus (FEED_PORT, EXEC_PORT)
- Async DB write queue (db_writer_queue)
- SQLite operational storage (ml_research.db, strike_research.db)
- Parquet archival for research-grade data
- PM2-managed process ecosystem (ecosystem.config.js)

---

## 3. Capability Status Matrix

| Capability | Status | Notes |
|---|---|---|
| Real-time tick ingestion | Active | feed_service.py |
| OHLCV bar construction | Active | data_fetcher.py |
| VFI / EMA / VWAP | Active | indicators.py |
| Signal generation | Active | signal_generator.py |
| Paper trading execution | Active | paper_trader.py |
| Parquet archival | Active | parquet_archiver.py |
| Research data collection | Active | research_collector.py |
| Trade journal | Active | brain_service.py |
| XGBoost scoring | Retained | Lightweight ML only |
| Shadow predictor | Disabled | shadow_service.py — preserved |
| LSTM / Ensemble | Disabled | ml_engine/ — code preserved |
| DuckDB analytics | Disabled | Dependency removed |
| Google Generative AI | Disabled | Dependency removed |

---

## 4. Frozen State Note

This Version 1 ontology reflects the production capability state as of Phase 6.2.
Future capability additions (institutional research layers, live execution, RL exit logic)
will be introduced in a Version 2 document at a later phase.
The engineering system is the current priority.