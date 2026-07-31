# 🗺️ ASTRA KDO v3.0 Master Architecture & Semantic System Map

**System Identity**: ASTRA Kinetic Delta Orderflow (KDO v3.0) Engine  
**Deployment Profile**: Low-Footprint PM2 Dual-Process Deployment (1 GB Oracle Cloud VPS / Linux RAM Disk)  
**Authoritative Reference**: 2026-07-29  

---

## 1. System Topology & Dual-Process PM2 Model

The **ASTRA KDO v3.0** trading engine runs as a lightweight, two-process PM2 deployment synced via Linux shared memory (`/dev/shm`):

```
                        ┌─────────────────────────────────────────┐
                        │      Oracle Cloud VPS (1 GB RAM)        │
                        └────────────────────┬────────────────────┘
                                             │
                       ┌─────────────────────┴─────────────────────┐
                       │           PM2 Process Manager            │
                       └──────────────┬──────────────┬─────────────┘
                                      │              │
                                      ▼              ▼
                    ┌───────────────────┐          ┌──────────────────────┐
                    │     astra-bot     │          │   astra-dashboard    │
                    │   (main.py PID)   │          │  (dashboard.py PID)  │
                    └─────────┬─────────┘          └──────────▲───────────┘
                              │                               │
                              │ Write State payload           │ Read JSON
                              ▼                               │ (0ms Latency)
                   ┌──────────────────────────────────────────┴───────────┐
                   │ Linux Shared Memory RAM Disk (/dev/shm)              │
                   │ Path: /dev/shm/kdo_live_state.json (Atomic os.replace)│
                   └──────────────────────────────────────────────────────┘
```

* **Process 1 (`astra-bot`)**: Execution & Signal Core (`astra_bot/main.py`). Runs the WebSocket streaming feed, BSM Gamma calculation, Kinematic State Estimator, Order Lifecycle, and Risk Engine.
* **Process 2 (`astra-dashboard`)**: Read-Only UI Projection (`astra_bot/dashboard.py`). Runs a Flask HTTP server on port 5000, reading `/dev/shm/kdo_live_state.json` every 2 seconds to render the Bloomberg Terminal web UI.

---

## 2. Semantic Dependency & Step-by-Step Data Flow Map

```
                  ┌────────────────────────────────────────────────────────┐
                  │                 Shoonya Broker API / WS                │
                  └──────────────┬──────────────────────────▲──────────────┘
                                 │ Live WebSocket Ticks     │ REST Orders / Session
                                 ▼                          │
                  ┌─────────────────────────────────────────┴──────────────┐
                  │ ShoonyaAdapter / ShoonyaMarketDataProvider             │
                  └──────────────┬─────────────────────────────────────────┘
                                 │ Ticks (LTP, OI, Volume)
                                 ▼
                  ┌────────────────────────────────────────────────────────┐
                  │ TickDeduplicator & RingBuffer (ring_buffer.py)          │
                  │  - 5-second micro-bucket aggregation                   │
                  │  - 60-second sliding window deque (maxlen=12)          │
                  │  - Strike Rollover Re-basing (ATM drift continuity)    │
                  └──────────────┬─────────────────────────────────────────┘
                                 │ Snapshots Array & Option Metadata
                                 ▼
                  ┌────────────────────────────────────────────────────────┐
                  │ KineticMicrostructureEngine (microstructure_engine.py) │
                  │  - Vectorized BSM IV & Net Gamma (GEX_net)             │
                  │  - Low-Lag Alpha-Beta-Gamma Filter (Velocity Vt, Acc At)│
                  │  - Conformal Empirical Tail Scorer (p-val & Surprisal) │
                  │  - Trade Signer Categorization (PUTBUILDUP, etc.)      │
                  └──────────────┬─────────────────────────────────────────┘
                                 │ Signal Payload & Microstructure Feature Vector
                                 ▼
                  ┌────────────────────────────────────────────────────────┐
                  │ ExecutionManager & Risk Engine (execution_manager.py)  │
                  │  - CapitalEngine (Affordability & Funds Allocation)    │
                  │  - StrikeSelector (ATM ± 5 Option Strike Resolution)   │
                  │  - Order Lifecycle (PaperOrderLifecycle / Shoonya)     │
                  │  - Stateful Flow-Decay Exit Engine (Schmitt Trigger)   │
                  └──────────────┬─────────────────────────────────────────┘
                                 │ Live State Payload
                                 ▼
                  ┌────────────────────────────────────────────────────────┐
                  │ update_kdo_live_state() -> /dev/shm/kdo_live_state.json│
                  └────────────────────────────────────────────────────────┘
```

---

## 3. Module-by-Module Component Breakdown

### **1. Core System Entrypoint (`astra_bot/main.py`)**
* **Role**: Central orchestrator loop running at 10 Hz (`time.sleep(0.1)`).
* **Pre-Market Handshake (`08:55:00` AM IST)**: Re-authenticates Shoonya OAuth sessions, subscribes Index Spot token (`NSE|26000` NIFTY or `BSE|1` SENSEX) and 11 weekly option strikes ($\text{ATM} \pm 5$).
* **Trading Loop (`09:15:02` to `15:31:00` IST)**: Queries Spot LTP, feeds snapshots to `KineticMicrostructureEngine`, triggers `ExecutionManager.execute_kdo_entry()`, evaluates open positions with `ExecutionManager.manage_open_trade(net_delta_velocity=v_t, kdo_state=kdo_result)`, and dumps live state to `/dev/shm/kdo_live_state.json`.

### **2. Data Ingestion & Ring Buffer (`astra_bot/core/ring_buffer.py`)**
* **Role**: Zero-allocation tick deduplication and time-series aggregation.
* **`TickDeduplicator`**: $O(1)$ hash set deduplication preventing duplicate WebSocket tick frames.
* **`KineticRingBuffer`**: Holds a 60-second sliding window (`maxlen=12` buckets of 5 seconds each).
* **Continuity-Preserving Rollover**: When Spot drifts to a new ATM strike, the ring buffer re-bases Open Interest offsets without clearing the 60s memory window, preventing artificial kinematic spikes.

### **3. Microstructure & Mathematical Engine (`astra_bot/core/microstructure_engine.py`)**
* **Role**: Pure NumPy vectorized BSM IV/Gamma calculator and statistical anomaly engine.
* **Vectorized BSM Engine**: Solves Black-Scholes IV via Newton-Raphson and computes Net Gamma Exposure ($\text{GEX}_{\text{net}}$):

  $$\text{GEX}_{\text{net}} = \sum_{i} \text{OI}_i \times \Gamma_i \times S^2 \times 0.01$$

* **Causal Alpha-Beta-Gamma Kinematic Filter**: Estimates low-lag velocity ($V_t$) and acceleration ($A_t$) on Net Option Delta OI without heavy EMA lag.
* **Conformal Empirical Tail Scorer**: Computes empirical tail p-values across 5 dimensions (acceleration, velocity, residual scale, breadth, flow duration) to derive Surprisal ($S_t = -\ln(\text{combined\_p})$).
* **Trade Signer Classification**: Normalizes and classifies incoming orderflow into canonical tokens: `PUTBUILDUP`, `CALLBUILDUP`, `BULLISHACCUMULATION`, `BEARISHACCUMULATION`, `UNWINDING`.
* **Explicit Payload Emission**: Emits `"persistent": persistent` explicitly alongside velocity, acceleration, zscore, and tradesigner.

### **4. Execution Manager & Risk Engine (`astra_bot/execution/execution_manager.py`)**
* **Role**: Trade execution, capital allocation, and 5-branch risk observer.
* **Capital & Strike Resolution**: `CapitalEngine` checks tradable cash, and `StrikeSelector` picks optimal affordable strikes in the $\text{ATM} \pm 5$ window.
* **Order Lifecycle**: Delegates to `PaperOrderLifecycle` (paper mode) or `ShoonyaOrderLifecycle` (live mode).
* **Risk Engine Exit Cascade (5 Branches)**:
  1. **Hard Stop Loss (-10%)**: Triggers if option premium drops $\le -10\%$.
  2. **Trailing Stop Loss**: Triggers if profit retraces $>50\%$ after achieving $\ge +15\%$ gain.
  3. **300-Second Hard Cap**: Exits trade after 300 seconds maximum hold time.
  4. **Delta OI Reversal**: Triggers if Net Delta OI reverses beyond threshold after 60s minimum hold.
  5. **Stateful Flow-Decay Observer**: Evaluated via `_evaluate_flow_decay()`.

---

## 4. Stateful Flow-Decay Exit Engine Specification

```
                  ┌────────────────────────────────────────────────────────┐
                  │        Open Trade Monitoring Cycle (~1 Hz)             │
                  └──────────────────────────┬─────────────────────────────┘
                                             │
                                             ▼
                             Is hold_sec >= 15.0s (Grace Window)?
                                    │                     │
                             NO     │                     │ YES
                                    ▼                     ▼
                        [Keep Trade Open]       Compute Hysteresis Threshold:
                        (Hard SL still active)  V_thresh = -max(2000, 0.25*|V_entry|)
                                                          │
                                                          ▼
                                                Deterioration Score (0-4):
                                                1. Vt <= V_thresh
                                                2. At < 0 (Adverse Acc)
                                                3. persistent == False
                                                4. Conflicting Signer
                                                          │
                                                          ▼
                                              Schmitt-Trigger Counter:
                                              Score >= 2 -> adverse_checks += 1
                                              Score < 2  -> adverse_checks -= 1
                                                          │
                                                          ▼
                                              Exit Trigger Conditions:
                                              - Cond A: Score >= 2 for 2 checks
                                              - Cond B: Score >= 3 for 1 check
```

### **Flow Decay Configuration Constants**
```python
FLOW_EXIT_MIN_HOLD_SEC = 15.0               # 15-second initial grace window
FLOW_EXIT_CONFIRM_CHECKS = 2                # 2 consecutive checks for moderate decay (Score >= 2)
FLOW_EXIT_STRONG_CONFIRM_CHECKS = 1         # 1 check for severe decay (Score >= 3)
FLOW_EXIT_MIN_ABS_VELOCITY = 2000.0         # Minimum noise floor magnitude (delta/s)
FLOW_EXIT_ENTRY_VELOCITY_FRACTION = 0.25    # 25% of entry velocity hysteresis floor
FLOW_EXIT_RECOVERY_RESET_CHECKS = 1         # Hysteresis recovery decay step
```

### **Signer Normalization & Canonical Matching Protocol**
To eliminate string format mismatches between live engine emissions (e.g. `PUTBUILDUP` vs `PUT_BUILDUP`), the string is stripped of spaces and underscores:
```python
def _normalize_signer(signer_str: str) -> str:
    if not signer_str: return "NEUTRAL"
    return str(signer_str).upper().replace("_", "").replace(" ", "").strip()

BEARISH_SIGNERS_NORMALIZED = {"BEARISHACCUMULATION", "CALLBUILDUP", "MIXEDBEARISH", "UNWINDING"}
BULLISH_SIGNERS_NORMALIZED = {"BULLISHACCUMULATION", "PUTBUILDUP", "MIXEDBULLISH", "UNWINDING"}
```

### **Neutral Missing Field Protocol**
Any missing or `None` field in `kdo_state` evaluates to neutral ($0$ score points added). Unknown microstructure telemetry never penalizes a trade or triggers an exit by default.

---

## 5. File System & Data Directory Structure

```
c:\Users\Amarjeet Singh\Astra\astra_bot\
├── .env                                 # Environment credentials (SHOONYA, ENABLE_LIVE_BROKERAGE_EXECUTION)
├── CHANGELOG.md                         # Authoritative chronological modification record
├── ASTRA_KDO_V3_ARCHITECTURE_SPECIFICATION.md # Master Architecture Document
├── ecosystem.config.js                  # PM2 process configuration
├── requirements.txt                     # Lightweight dependencies (flask, pandas, numpy, requests, python-dotenv)
└── astra_bot/
    ├── __init__.py
    ├── main.py                          # PM2 Entrypoint Process 1 (astra-bot)
    ├── dashboard.py                     # PM2 Entrypoint Process 2 (astra-dashboard)
    ├── config/
    │   ├── settings.py                  # Single source of truth for lot sizes, capital, and defaults
    │   └── market_calendar.py           # Pre-market (08:55 AM) & market hours logic
    ├── core/
    │   ├── logger.py                    # Console & file logging formatter
    │   ├── data_fetcher.py              # Shoonya Index Spot & Option Chain resolver
    │   ├── ring_buffer.py               # 60s RingBuffer & Strike Rollover Re-basing
    │   ├── microstructure_engine.py     # BSM Gamma Engine, Alpha-Beta-Gamma Filter, Tail Scorer
    │   └── shoonya_adapter.py           # Shoonya REST & WebSocket API gateway
    └── execution/
        ├── capital_engine.py            # Paper / Live tradable margin allocator
        ├── strike_selector.py           # OTM Option strike selection (ATM ± 5 window)
        ├── order_lifecycle.py           # PaperOrderLifecycle / ShoonyaOrderLifecycle
        ├── trade_context.py             # TradeContext & TradeLeg state containers
        └── execution_manager.py         # Execution Manager & 5-Branch Risk Observer
```

---

## 6. Shared Memory JSON Protocol (`/dev/shm/kdo_live_state.json`)

The live state is serialized via atomic `os.replace` to `/dev/shm/kdo_live_state.json` with the following schema:

```json
{
  "timestamp": "2026-07-29 09:51:17",
  "active_index": "SENSEX",
  "spot_price": 77541.10,
  "gex_net": -209877280.09,
  "gex_regime": "NEGATIVE_GAMMA",
  "velocity": 15911.28,
  "acceleration": 1130.98,
  "signal_status": "🟢 STRONG BULLISH (CALL READY)",
  "reasoning": "CALL Entry Triggered | GEX=-209,877,280, V=15,911/s, A=1,131/s²...",
  "kinematic_ready": true,
  "rollover_pause": false,
  "available_funds": 2500.00,
  "active_trade": null,
  "option_chain": [ ... 11 strikes (ATM ± 5) ... ],
  "history": [ ... trade records ... ],
  "recent_logs": [ ... log lines ... ]
}
```
