# Master AI Assistant & Developer Guide (`ai_guide.md`)
## Kronos Options Trading System - System Architecture & Maintenance Protocols

Welcome! If you are an AI assistant or human engineer working on this codebase, **read this document first**. It contains the architectural sitemap, design principles, execution safety protocols, and operational workflows for maintaining the **Kronos Options Trading System**.

---

## 1. System Vision & Overview

Kronos is an autonomous, high-frequency 5-minute index options trading bot operating on BSE (SENSEX) and NSE (NIFTY) weekly options via the Shoonya (Finvasia) REST & WebSocket API.

- **Primary Predictive Brain**: A 6-channel Transformer Model (`NeoQuasar/Kronos-base`) processing 512-candle sliding windows ($512 \times 5\text{-minute}$ context length).
- **Core Signal Thresholds**: Dynamic expected price change thresholding ($\pm 0.20\%$ on 0DTE Expiry Days vs $\pm 0.25\%$ on 1DTE & normal days).
- **Institutional Guard Filter**: False Breakout Filter comparing Transformer predictions against 5-minute `Net Option Delta OI` ($\Delta\text{OI}_{\text{Puts}} - \Delta\text{OI}_{\text{Calls}}$).
- **Trade Execution Engine**: Guaranteed fast `MARKET` entry, 2-phase dynamic trailing risk engine, 60s minimum hold floor, 5-minute (300s) hard cap, and 2-stage `Limit-then-Market` fallback exits.

---

## 2. Directory Structure & Key Module Sitemap

```
C:\Kronos_Testing\
├── astra_bot/
│   ├── main.py                     # Main bot orchestrator loop & EOD backfill scheduler
│   ├── dashboard.py                # Flask Web Control Dashboard (Port 5000)
│   ├── config/
│   │   ├── settings.py             # Global constants, thresholds, parameters
│   │   └── market_calendar.py      # Trading hours (09:15-15:45 IST) & 03:20 PM cutoff
│   ├── core/
│   │   ├── shoonya_adapter.py      # Shoonya REST/WS gateway, Volume & Net Delta OI accumulators
│   │   ├── data_fetcher.py         # Historical Parquet caching & candle sliding window manager
│   │   └── logger.py               # Dual-handler color console & clean file logger
│   ├── strategy/
│   │   ├── signal_generator.py     # Kronos Transformer predictor & sliding window formatting
│   │   └── signals.py              # Expiry thresholding & False Breakout Filter
│   ├── execution/
│   │   ├── execution_manager.py    # 2-phase trailing risk engine & per-leg OI exit trigger
│   │   ├── order_lifecycle.py      # Shoonya real-broker order placement & double-sell guard
│   │   └── capital_engine.py       # Live Shoonya cash balance sync & Dashboard capital lock
│   ├── data/
│   │   ├── scratch/                # Underlying candles (.parquet) & quote caches (.json)
│   │   └── trades/                 # Trade history, active context, post-trade shadow ledger
│   └── scratch/                    # Regression unit test suites & debug scripts
├── architecture.md                 # Complete system design & flowcharts
├── changelog.md                    # Detailed release notes with technical rationale (WHY)
├── future_roadmap.md               # Strategic roadmap & shadow calibration plans
└── ai_guide.md                     # THIS FILE: Master AI Developer Guide
```

---

## 3. Core Subsystem Responsibilities

### 3.1 Main Orchestration (`main.py`)
- Synchronizes execution to 5-minute candle boundaries (`wait_until_next_candle(interval_minutes=5)`).
- Enforces active bot hours from **09:15 AM to 15:45 PM IST** (`is_bot_active_hours`).
- Enforces trade entry cutoff at **03:20 PM IST** (`can_enter_new_trades`).
- Executes post-market EOD REST candle backfill for active & inactive indices at **03:35 PM IST**, saving full 15:25 candles to disk before sleeping at 03:45 PM.

### 3.2 Strategy & Signal Generation (`signals.py`)
- Evaluates Kronos Transformer expected price change percentage against dynamic thresholds.
- **False Breakout Filter**: Cross-examines `CALL` signals against Net Delta OI (blocks `CALL` if Net Delta OI is negative) and `PUT` signals against Net Delta OI (blocks `PUT` if Net Delta OI is positive).
- **Market Opening Neutral Gate**: Passes signals when Net Delta OI == 0 during opening minutes (09:15-09:20 AM), capturing explosive opening momentum bursts.

### 3.3 Broker Adapter & OI Accumulators (`shoonya_adapter.py`)
- Maintains Shoonya REST session and WebSocket stream.
- **Synthetic Volume Accumulator**: Accumulates tick volume across constituent stocks (30 BSE / 50 NSE) to synthesize live 5-minute volume.
- **Net Option Delta OI Accumulator**: Subscribes 11 CE and 11 PE option tokens.
- **Option B Strike Mapping**: Maps option tokens to strike prices (`register_option_token_metadata`) and provides `get_strike_net_delta_oi(strike_price)` for single traded-strike Net Delta OI tracking.

### 3.4 Execution Manager & Risk Engine (`execution_manager.py`)
- **2-Phase Trailing Risk Engine**:
  - Phase 1 (0% to +15% profit): Initial -20% SL, breakeven at +7%, +5% lock at +10%, 180s Theta Decay Abort (<5% profit).
  - Phase 2 (> +15% profit): Uncapped High-Water-Mark (HWM) trailing (10% gap at >15%, 15% gap at >50%, 20% gap at >150%).
- **5-Minute Hard Cap**: Mandatory exit after 300s.
- **OI Reversal Exit (Option B)**: Evaluates live strike pair Net Delta OI for each open leg after 60s hold floor (`OI_REVERSAL_MIN_HOLD_SEC = 60.0`). Code currently runs in Shadow Calibration Mode (`OI_REVERSAL_EXIT_ENABLED = False`).
- **Post-Trade Shadow Observer**: Records post-exit option LTP, Net Delta OI, and trade-direction alignment for 5 minutes post-exit (+60s to +300s) in `post_trade_shadow_ledger.ndjson`.

### 3.5 Real-Broker Order Lifecycle (`order_lifecycle.py`)
- **Entry Orders**: Executed strictly as `MARKET` orders (`price_type="MKT"`).
- **Explicit Product Type**: Hardcoded to `product_type="N"` (`NRML` / Regular). `MIS` (`"M"`) is strictly disabled to comply with Shoonya BSE index option regulations.
- **2-Stage Exit Fallback**: Submits `LIMIT` order at best bid `bp1`, waits 5s (`EXIT_LIMIT_GRACE_PERIOD_SEC`), cancels, verifies order status post-cancel to catch race condition fills, and submits `MARKET` sell fallback.

---

## 4. Mandatory Guidelines for AI Assistants

When modifying this codebase, you **MUST ALWAYS** obey the following directives:

1. **Do NOT Disturb Live VPS During Market Hours**:
   - Never run modifying SSH commands, git pull, or PM2 restarts on the live VPS during active market hours (09:15 AM - 03:30 PM IST) unless explicitly directed by the user.

2. **Always Run Unit Tests Locally Before Pushing**:
   - Before committing code, run the unit test suite:
     ```bash
     python astra_bot/scratch/test_oi_reversal_and_order_mechanics.py
     ```
   - All tests must output `ALL UNIT & REGRESSION TESTS PASSED SUCCESSFULLY!`.

3. **Obey Explicit Product Type Constraints**:
   - All real Shoonya orders MUST specify `product_type="N"` (`NRML`). Never change product type to `"M"` (`MIS`).

4. **Preserve Documentation Integrity & Rationale (WHY)**:
   - When documenting changes in `changelog.md`, always explain **WHY** the change was made (the strategic or technical rationale), not just what code lines were altered.
   - Update `architecture.md` whenever core execution flows or data structures change.

5. **Local State Sync & Live Capital Safety**:
   - In `LIVE` mode (`ENABLE_LIVE_BROKERAGE_EXECUTION = True`), the Capital Engine must auto-sync live Shoonya cash balance (`get_limits()`) to `capital_state.json` so the Web Dashboard subtracts locked capital dynamically from real tradable margin.
