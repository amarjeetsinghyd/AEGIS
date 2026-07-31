# Kronos Options Trading System - Future Roadmap & Shadow Calibration Plan

This document outlines the strategic roadmap, empirical shadow observation plans, and proposed technical enhancements for the **Kronos Options Trading System**.

---

## 1. Active Shadow Calibration Modes (Observation Phase)

### 1.1 Shadow Mode 1: OI Reversal Exit Threshold Calibration
- **Current State**: Code active in shadow mode (`OI_REVERSAL_EXIT_ENABLED = False`).
- **Objective**: Observe logged `[SHADOW MODE]` OI reversal triggers across live trades over 1–2 trading days.
- **Parameters Being Evaluated**:
  - `OI_REVERSAL_MIN_HOLD_SEC`: 60.0s (1-minute hold floor).
  - `OI_REVERSAL_EXIT_THRESHOLD`: 50,000 contracts (Option B: Traded-Strike Pair Net Delta OI $\Delta\text{OI}_{\text{PE}} - \Delta\text{OI}_{\text{CE}}$).
- **Target**: Empirically calibrate `OI_REVERSAL_EXIT_THRESHOLD` specifically for SENSEX (BSE) and NIFTY (NSE) contracts before toggling `OI_REVERSAL_EXIT_ENABLED = True`.

### 1.2 Shadow Mode 2: Post-Trade Trend Continuation Observation Engine
- **Current State**: Active logging starting 24 July 2026 onwards (`post_trade_shadow_ledger.ndjson`).
- **Objective**: Whenever a trade context exits, monitor the closed option contract for 5 minutes post-exit at 1-minute intervals (+60s, +120s, +180s, +240s, +300s).
- **Metrics Collected**:
  - Post-exit Option LTP and percentage return (`price_delta_pct`).
  - Post-exit live Net Option Delta OI (`net_option_delta_oi`).
  - Alignment flag (`aligned_in_trade_direction`): True if Net Delta OI continues to support the trade direction.
  - Hypothesized additional PnL (`hypo_pnl`).
- **Target**: Validate the hypothesis of extending position hold duration beyond 5 minutes (hard cap) when Net Option Delta OI continues strongly in our favor, saving cooldown delays and capturing multi-candle trends.

---

## 2. Proposed Future System Enhancements

### 2.1 Multi-Strike / Multi-Leg Option Pyramiding
- **Concept**: Allocate available tradable margin dynamically across multiple affordable option strikes (e.g. Leg 1 ATM + Leg 2 next affordable strike).
- **Execution**:
  - Select highest affordable ATM strike for Leg 1.
  - If remaining capital permits, select next affordable strike for Leg 2.
- **Exit Strategy**: Managed via **Option B (Independent Per-Leg Traded-Strike Pair Net Delta OI Tracking)**, allowing weak/capped strikes to be trimmed independently while letting strong strikes ride the trend.

### 2.2 09:15:01 AM REST API Opening Tick Pre-Fetch
- **Concept**: Eliminate initial WebSocket tick streaming delay during the market opening seconds (09:15:00 to 09:15:05 AM IST).
- **Implementation**: Issue a targeted `get_quotes()` REST call right at 09:15:01 AM IST for ATM option quotes to seed initial cache prices instantaneously, guaranteeing 0ms latency for Trade #1.

### 2.3 Automated Google Drive Data Backup Pipeline
- **Concept**: Automated weekly or monthly backup of persistent historical candle data and trade logs.
- **Components to Backup**:
  - Underlying index candles: `data/scratch/underlying_candles_SENSEX.parquet` & `NIFTY.parquet`.
  - Post-trade shadow ledger: `data/trades/post_trade_shadow_ledger.ndjson`.
  - Trade execution history: `data/trades/trade_history.json`.
- **Execution**: Background cron script running post-market on weekends (`rclone` or Google Drive API).

### 2.4 SQLite Runtime Store Migration
- **Concept**: Migrate JSON-based state files (`capital_state.json`, `active_trade_context.json`) to an embedded SQLite database (`kronos_runtime.db`).
- **Benefit**: Provides ACID transaction safety, eliminates file write locks, and improves multi-process concurrency between `astra-bot` and `astra-dashboard`.

---

## 3. Summary Schedule

| Milestone / Task | Status | Target Timeline |
| :--- | :--- | :--- |
| **Real-Money Live Trading Switch** | 🟢 Live | Active (`FN215498`) |
| **OI Reversal Shadow Calibration** | 🟡 Active Shadow | 24 - 25 July 2026 (1–2 Days) |
| **Post-Trade Continuation Logging** | 🟡 Active Shadow | 24 - 31 July 2026 |
| **09:15 AM Opening Pre-Fetch** | ⚪ Proposed | Next Feature Sprint |
| **Multi-Strike Option Pyramiding** | ⚪ Proposed | Future Enhancements Phase |
| **Google Drive Backup Script** | ⚪ Proposed | Weekend Utility Task |
| **SQLite Store Migration** | ⚪ Proposed | Future Architecture Refactoring |
