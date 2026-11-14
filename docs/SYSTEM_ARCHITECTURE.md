# AEGIS — System Architecture Map

**Root architecture authority.** Maps the overall deployment, repository structure, and operational plumbing for AEGIS Production v1.0.

**Owner:** Amarjeet Singh · **Broker:** Shoonya (NorenApi), LMT only in F&O  
**Host:** Oracle Cloud VPS (Ubuntu `137.23.41.38`), 100% autonomous via PM2  
**Active Production Engine:** AEGIS (Version 1.0 Production Locked)  
**Last updated:** 14 November 2026

---

## 1. Ten-Thousand-Foot View

```
                          Oracle Cloud VPS  (opc@137.23.41.38)
                          ┌─────────────────────────────────────────────┐
    Shoonya broker ◀──WS──│   PM2 manages 2 active apps                 │
                          │   ├─ aegis-bot       (main.py)              │
                          │   └─ aegis-dashboard (dashboard/dashboard.py│
                          └─────────────────────────────────────────────┘
                                  Flattened Production Repo:
                                  core/ · execution/ · broker/
                                  market/ · dashboard/ · ipc/
```

- **Data Ingestion:** Single Shoonya WebSocket feed demuxed via `market/tick_compiler.py` (L1 + top-5 L2 depth).
- **Execution:** Zero-Regression 5-Mixin Execution Engine (`execution/execution_manager.py`).
- **Telemetry & IPC:** Shared state written via `ipc/live_state.py` to `data/ipc/aegis_live_state.json`, served by `dashboard/dashboard.py` on port 8080.
- **Decommissioned Engines:** `ARGUS` and `Kronos` are permanently decommissioned and removed. Historical artifacts reside strictly in `_archive/`.

---

## 2. Active Production Engine: AEGIS v1.0 (Production Locked)

| # | Engine | PM2 process | Entry Point | State | Harness / Authority Document |
|---|---|---|---|---|---|
| 1 | **AEGIS** (Autonomous Execution & Gamma Imbalance System) | `aegis-bot` | `main.py` | **ACTIVE PRODUCTION (100% Core Focus)** | [`docs/AEGIS_V1.0_ARCHITECTURE.md`](AEGIS_V1.0_ARCHITECTURE.md) & [`docs/5_PILLARS_MASTER_BLUEPRINT.md`](5_PILLARS_MASTER_BLUEPRINT.md) |
| 2 | **Dashboard** | `aegis-dashboard` | `dashboard/dashboard.py` | **ACTIVE UI (Port 8080)** | `dashboard/` |

---

## 3. Core Strategy & Execution Summary

- **Strategy Architecture (5 Macro Price Action Pillars + Priority Mode B Surge):**
  1. **Pillar 1 (`20_ema_rejection`)**: Classical Dow Theory 20 EMA Retracement Engine ($HH_1 \to HL_1 \to HH_2 \to HL_2$) with Rejection Approach Ladder and Candle N-1 Wyckoff Smart Gate.
  2. **Pillar 2 (`micro_sweep_reclaim`)**: Liquidity Sweep & Reclaim across Dynamic S/R Registry & Day Extremes (PDH/PDL) with `SweepStopRunDetector` ($\ge 2.2\times$ burst) and $\ge 40\%$ live wick.
  3. **Pillar 3 (`volatility_squeeze_breakout`)**: 5-7 bar box compression ($\le 1.15\times \text{ATR}_{1m}$, drift $\le 50\%$, anti-staircase/waterfall) with expansion breakout.
  4. **Pillar 4 (`sr_breakout`)**: Outermost Roof/Floor Dynamic S/R Breakout ($Prominence \ge 1.25$), 2-candle approach ladder, and Day Extreme Runway Shield ($\ge 1.25\times \text{ATR}_{1m}$).
  5. **Pillar 5 (`gamma_trap_squeeze`)**: Trapped Option Writer Short-Squeeze Engine (breaks above Call Writer VWAP or below Put Writer VWAP) with 3-bar debounce.
  - **Priority Mode B In-Flight Breakthrough**: Mid-bar entry evaluated from **$T:02s+$ onwards** without waiting for bar close.
  - **Mode A Decommissioned**: Legacy Mode A closed-bar ladders have been permanently decommissioned.

- **Execution Engine (Zero-Regression 5-Mixin Architecture):**
  - `AegisExecutionManager` in `execution/execution_manager.py` (409 LOC) composes:
    - `EntryEngineMixin` (`execution/entry_engine.py`, 959 LOC): Signal intake, strike budgeting, maker TP, and slot rollback.
    - `TradeMonitorMixin` (`execution/trade_monitor.py`, 817 LOC): Live tick monitor, noise floor, realized delta, and spot invalidation.
    - `ExitChaserMixin` (`execution/exit_chaser.py`, 923 LOC): 4-step limit exit chase, fill polling, broker net position reconciliation, and emergency flatten.
    - `TradePersistenceMixin` (`execution/trade_persistence.py`, 567 LOC): Daily state, PnL tallying, NSE charges, and context saving/loading.
    - `QualitySlotPolicy` & Risk Constants (`execution/slot_and_risk_constants.py`, 239 LOC): 3-trade daily cap, stability tracker, and scale-invariant thresholds.

- **Universal Adaptive Exit Discipline:**
  - **Strict 1-Lot Maker TP**: +4.0% Limit Maker on exchange (Monday–Thursday), +3.0% on Friday (4 DTE).
  - **Universal Spot Invalidation**: Exits evaluated against Cash Index Spot:
    $$\text{CALL SL} = \text{Entry Candle Low} - (0.10 \times \text{ATR}_{1m})$$
    $$\text{PUT SL} = \text{Entry Candle High} + (0.10 \times \text{ATR}_{1m})$$
  - **Near-Target Profit Lock**: Arms at $+3.5\%$ peak, exits on $\ge 0.8\%$ pullback.
  - **Cost-Shield Scratch**: Arms at $+3.5\%$ peak, locks SL at $0.0\%$ (Breakeven).
  - **Stepped Safety Net**: Arms at $+3.0\%$ peak, tightens SL to $-3.0\%$.
  - **Session End Flatten**: Unconditional square-off at **15:08:00 IST** (CAS force flatten).

- **Weekly Calendar & DTE Schedule:**
  - **Mon (1 DTE) & Tue (0 DTE Expiry)**: NIFTY (strike step 50, lot 65)
  - **Wed (1 DTE) & Thu (0 DTE Expiry)**: SENSEX (strike step 100, lot 20)
  - **Fri (4 DTE)**: Deep Sleep Mode by default (`is_trading_day = False`).
  - **Sat & Sun**: Market Closed (WS disconnect is normal).
