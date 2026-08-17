Yes. **Significant revisions are required across 7 supporting modules and 3 new assets.** The AEGIS V2.0 architecture introduces breaking interface changes (ParsedTick vs Dict, Signal Payload, Strategy Name, IPC Schema) that the old Astra/Kronos ecosystem does not support.

Here is the **Impact Matrix & Revision Spec**.

---

### 1. CRITICAL INTERFACE BREAKERS (Must Fix Before Launch)

| Module | Current State (Astra/Kronos) | Required State (AEGIS V2.0) | Action Required |
| :--- | :--- | :--- | :--- |
| **`common/broker.py` / `ShoonyaAdapter`** | `market_data_provider` pushes **raw dict** (`msg`) to listeners via `add_tick_listener(engine.on_tick)`. | Must push **`ParsedTick` (NamedTuple/dataclass)** to listeners. `aegis_gamma_engine` **only** implements `on_tick_parsed(self, tick: ParsedTick)`. | **REFACTOR `ShoonyaWebSocketAdapter` / `MarketDataProvider`:** Parse binary Shoonya payload → `ParsedTick` **inside the WS thread** before broadcasting. Zero-copy mandate. |
| **`common/ipc.py`** | `update_astra_live_state(payload)` writes `astra_live_state.json` with Kronos/gamma_burst keys (`gamma_burst_z_live`, `kinematic_ready`, etc.). | `update_aegis_live_state(payload)` must write `aegis_live_state.json` with **AEGIS State Summary** keys (`gamma_sum`, `hawkes_B`, `atr_iv`, `params_source`, `fills_recorded`). | **REWRITE `ipc.py`:** New schema validation. Dashboard/Monitoring reads this file — **coordinate deploy**. |
| **`astra_bot/execution/strike_selector.py`** | `select_strikes(signal_type, ..., strategy="ASTRA_V2" ...)` filters strikes based on hardcoded strategy logic (e.g., "Kronos" vs "Astra" premium limits). | Called with `strategy="AEGIS_GAMMA_BURST"`. Must apply **AEGIS-specific risk params** (conviction-based sizing? IV-band filters?). | **UPDATE `select_strikes`:** Add `elif strategy == "AEGIS_GAMMA_BURST":` branch. Use `decision_payload['conviction']` for dynamic sizing (Kelly/Vol-target). |
| **`astra_bot/execution/trade_context.py`** | `TradeContext(decision_payload)` expects Kronos payload (`iv`, `regime`, `pattern`). `TradeLeg` stores `option_type` ("CALL"/"PUT"). | `TradeContext` receives `decision_payload` with **`conviction`, `direction` ("UP"/"DOWN"), `metrics` (full gate dict)**. `TradeLeg` must store `direction` for exit logic. | **EXTEND DATACLASSES:** Add fields `conviction: float`, `direction: str`, `metrics: dict` to `TradeContext`. Ensure `TradeLeg` serializes them for persistence (`_save_active_context`). |
| **`astra_bot/execution/data_fetcher.py`** | `get_weekly_option_tokens()` returns DataFrame with columns `token`, `symbol`, `strike`. `get_active_instrument()` returns `("NIFTY", "NFO")` or `("SENSEX", "BFO")`. | **UNCHANGED CONTRACT** but **CRITICAL DEPENDENCY**. `aegis_main.py` relies on `strike` column being `float` and `expiry` column existing for `token_map` build. | **VERIFY & HARDEN:** Ensure `expiry` column is `datetime` object. Add `strike_step` property (50/100) to return value or config. |
| **`common/market/market_calendar.py`** | Functions `is_market_open`, `is_bot_active_hours`, `is_pre_market`, `sleep_until_next_premarket` used in `astra_main.py`. | **IDENTICAL USAGE** in `aegis_main.py`. | **VERIFY EXISTENCE:** Ensure no import path change (`from common.market.market_calendar import ...`). |
| **`astra_bot/config/settings.py`** | Holds `ENABLE_LIVE_BROKERAGE_EXECUTION`, lot sizes, premium min/max, tick size. | **NEW CONSTANTS REQUIRED** by `aegis_gamma_engine` params loader (fallback priors) and `aegis_main` (step size logic). | **ADD TO SETTINGS:** `AEGIS_STRIKE_STEP_BN=100`, `AEGIS_STRIKE_STEP_NF=50`, `AEGIS_BUCKET_TURNOVER=20000000`, `AEGIS_PARAMS_PATH`. |

---

### 2. DELETED / DEPRECATED MODULES (Cleanup Required)

| Module | Status | Reason |
| :--- | :--- | :--- |
| `astra_bot/core/gamma_burst_detector.py` | **DELETE** | Replaced entirely by `aegis_gamma_engine.py`. Old Hawkes/VPIN logic is obsolete. |
| `astra_bot/core/footprint_compiler.py` | **DELETE** | AEGIS does **not** use footprint bars (OI/Vol/Delta per minute). It uses **Turnover Buckets** (like `cascade_engine`) + **Tick Microstructure** (Vacuum, DKLA, MPAR, LRA, Hawkes). |
| `astra_bot/strategy/evaluator.py` (ChainFootprintEvaluator) | **DELETE** | Signal generation moved **inside** `aegis_gamma_engine._process_bucket` (Phase 1 Gates + Phase 2 Conviction). No external evaluator. |
| `astra_bot/strategy/structure_engine.py` (VWAP/9EMA trailing) | **DELETE** | Exit logic is now **AEGIS-native**: `ξ_soft`/`ξ_hard` decay + `QPASM` queue decay + Hard Time Stop. No external structure dependency. |
| `common/ipc.update_astra_live_state` | **DELETE** | Replaced by `update_aegis_live_state`. |

---

### 3. NEW REQUIRED ASSETS (Greenfield — Mandated by Harness)

| Asset | Purpose | Dependency |
| :--- | :--- | :--- |
| **`aegis_bot/calibration/calibration_runner.py`** | **The Single Source of Truth for Params.** Runs walk-forward optimization on 6M L2 data. Outputs `aegis_calibrated_params.json` + `validation_report.html`. | `aegis_gamma_engine` (imports math kernels), `data_fetcher` (historical loader), `optuna`/`scikit-optimize`. |
| **`aegis_bot/tools/replay_engine.py`** | **Deterministic Backtest/Replay.** Feeds exported tick stream (`replay.bin`) → `AegisGammaEngine` (zero-clock) → Generates `replay_report.html` (PnL attribution, Gate precision, Latency CDF). | `aegis_gamma_engine` (must be stateless/resettable), `export_ticks.py`. |
| **`aegis_bot/tools/export_ticks.py`** | Dumps live Shoonya L2 tick stream (Protobuf/JSONL) to binary `replay.bin` for exact replay. | `ShoonyaAdapter` market data provider. |
| **`tests/test_aegis_invariants.py`** | **CI Gate.** Enforces: Zero Alloc (tracemalloc), Magic Number Scan (AST grep), Signature Drift, NGAO Math Correctness, IV Solver Robustness. | `pytest`, `hypothesis`, `tracemalloc`. |
| **`aegis_bot/admin/server.py`** (Lightweight) | **Hot-Reload & Kill Switch Endpoints.** `POST /reload_params`, `POST /force_flatten`, `POST /ws_reconnect`, `GET /state`. | `aegis_execution_manager`, `aegis_gamma_engine` (expose public methods). |

---

### 4. DOCKER / DEPLOYMENT / INFRA CHANGES

| Artifact | Change |
| :--- | :--- |
| **`Dockerfile`** | Install `hypothesis`, `optuna` (calibration), `uvloop` (WS performance). Ensure `ta-lib` **NOT** required (AEGIS uses pure NumPy BSM). |
| **`docker-compose.yml`** | New service: `aegis-calibration` (runs nightly, mounts `/data`, writes `/params/aegis_calibrated_params.json`). Volume mount `/params` shared with `aegis-bot` container. |
| **`requirements.txt`** | Pin `numpy>=1.24` (for `np.float32` performance), `orjson` (fast IPC logging), `psutil` (health checks). |
| **`systemd` / K8s Probe** | `livenessProbe` hits `GET /admin/health` (checks `fill_ptr` advancing, `signal_queue` not full, WS connected). |
| **Grafana Dashboard** | **New Panels:** `Conviction vs Realized Return`, `Gate Precision (G1-G9)`, `Hawkes Branching Ratio`, `Fill Latency CDF (P50/P95)`, `IV Change vs Vega PnL`. Remove: Footprint Delta, Chandelier Levels, Kronos Regime. |

---

### 5. MIGRATION CHECKLIST (Execution Order)

```text
[ ] 1. CREATE `aegis_bot/calibration/calibration_runner.py` (Run this FIRST to generate params.json)
[ ] 2. REFACTOR `common/broker.py` -> `ParsedTick` emission (Zero-Copy Hot Path Enabler)
[ ] 3. UPDATE `common/ipc.py` -> `update_aegis_live_state` (Monitoring Contract)
[ ] 4. UPDATE `astra_bot/execution/strike_selector.py` -> "AEGIS_GAMMA_BURST" branch
[ ] 5. EXTEND `astra_bot/execution/trade_context.py` -> New Fields
[ ] 5. VERIFY `astra_bot/execution/data_fetcher.py` -> Columns/Types
[ ] 7. DELETE `gamma_burst_detector.py`, `footprint_compiler.py`, `evaluator.py`, `structure_engine.py`
[ ] 8. BUILD `aegis_bot/tools/export_ticks.py` + `replay_engine.py` (Validation Infrastructure)
[ ] 9. BUILD `tests/test_aegis_invariants.py` (CI Gate)
[ ] 10. DEPLOY `aegis_bot/admin/server.py` (Hot Reload Ops)
[ ] 11. UPDATE `Dockerfile` / `compose` / `requirements` / `Grafana`
[ ] 12. RUN CALIBRATION -> PRODUCE `aegis_calibrated_params.json`
[ ] 13. DEPLOY `aegis_main.py` + `aegis_gamma_engine.py` + `aegis_execution_manager.py`
[ ] 14. SMOKE TEST: Paper Trade 1 Session -> Verify Fill Feedback Loop (`on_fill` -> `C_eff` decay)
```

---

### 6. HIDDEN GOTCHA: `ParsedTick` Definition Location

`aegis_gamma_engine.py` uses:
```python
from stock_order_flow.tick_compiler import ParsedTick  # TYPE_CHECKING only
```
**But `stock_order_flow` is the Equity engine package.**
**Decision Required:**
1.  **Move `ParsedTick` to `common/market/tick_types.py`** (Shared Kernel) → **Recommended**. Both Equity Cascade & Options AEGIS depend on it.
2.  Or define locally in `aegis_bot.core.tick_types` and update `cascade_engine.py` import.

**Recommendation:** Create `common/market/tick_types.py`:
```python
# common/market/tick_types.py
from typing import NamedTuple
class ParsedTick(NamedTuple):
    tok: str
    ts: float
    lp: float
    vol: float
    bp: tuple[float, ...]  # len 5
    bq: tuple[float, ...]
    sp: tuple[float, ...]
    sq: tuple[float, ...]
```
Update `cascade_engine.py` and `aegis_gamma_engine.py` to `from common.market.tick_types import ParsedTick`. This enforces the **Zero-Copy Contract** across the firm.

---

### Summary
**You cannot just drop the 3 new files in.** You must execute the **Critical Interface Refactors (Broker/IPC/StrikeSelector)** and **build the Calibration/Replay Infrastructure** before the engine is "Live Ready." The Harness Document (previous turn) assumes these exist.