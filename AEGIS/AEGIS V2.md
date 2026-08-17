# AEGIS V2.0 — AGENTIC MAINTENANCE HARNESS
**Classification:** Principal Engineering Directive — Autonomous Agent Operations Manual  
**Version:** 2.0 (Aligned with AEGIS V2.0 Whitepaper & Implementation)  
**Target Audience:** Agentic Coding AI (Cursor, Devin, Custom SWE-Agent)  
**Prime Directive:** *Preserve the First-Principles Invariants. Zero Allocation is Law. Calibration is Truth.*

---

## 1. ARCHITECTURAL MENTAL MODEL (The "Why")

Do not treat this as a typical signal-processing pipeline. **AEGIS is a structural arbitrage engine** modeling an adversarial game.

| Component | Role in Game | Theoretical Basis |
| :--- | :--- | :--- |
| **Market Makers (MM)** | Short Gamma, Delta Hedge, Manage Spread/Toxicity | Inventory Risk + Adverse Selection (Glosten-Milgrom) |
| **Aggressive Whale (AW)** | Initiates Cascade via Multi-Strike Flow | Strategic Order Splitting + Gamma Forcing |
| **AEGIS Engine** | Detects *Inception* (T=0 of Cascade) | Hawkes Criticality + Non-Linear Gamma Feedback (NGAO) |
| **Execution Manager** | Captures Edge before Decay | Latency-Aware Conviction Decay (Queue Position Risk) |

**Core Loop:**  
`Tick (O(1) Stats) → Bucket Boundary (Heavy Math) → Phase 1 Gates (Structural Invariants) → Phase 2 Conviction (Risk-Adjusted Score) → Signal → Execution → Fill Feedback → Latency Decay Update`

---

## 2. INVIOLABLE INVARIANTS (The "Contracts")

**Any PR violating these is automatically rejected.**

| # | Invariant | Scope | Verification Method |
| :--- | :--- | :--- | :--- |
| **I1** | **Zero Allocation on Hot Path** | `AegisGammaEngine.on_tick_parsed` | `tracemalloc` snapshot diff = 0 bytes / 10k ticks |
| **I2** | **O(1) Per-Tick Complexity** | `on_tick_parsed` | No loops over `N_STRIKES` (21), `N_LEVELS` (5), or History. Only fixed-index array writes & scalar math. |
| **I3** | **Hard Gates are Necessary Conditions** | `recompute_aegis` Phase 1 | `G1..G9` derived from Whitepaper Sec 1-3. None removable without re-derivation proof. |
| **I4** | **No Magic Numbers** | Entire Codebase | Every scalar constant **MUST** exist in `CalibrationRegistry` (`_load_or_default_params`) with source/validation metadata. |
| **I5** | **Fill Feedback Loop Closed** | `ExecutionManager._chase_exit_thread` → `AegisEngine.on_fill` | Every fill *must* call `aegis_engine.on_fill(fill_ts, fill_price, signal_ts, signal_price)`. |
| **I6** | **IV Solver Robustness** | `_impvol_newton` | Must return `np.nan` (never crash) for Deep ITM/OTM, T→0, Arb-violating prices. Bisection fallback mandatory. |
| **I7** | **NGAO Dual Guard** | `_ngao_acceleration` | Must handle `D0 <= 1e-8` **AND** `sum_speed ≈ 0` (symmetric chain cancellation). |
| **I8** | **G6 Unvalidated Warning** | `_adaptive_microprice_align` | Logs `G6_RUNNING_UNVALIDATED_QUEUE_ASSUMPTION` every bucket until `_g6_validated=True`. |

---

## 3. MODULE MAP & RESPONSIBILITY BOUNDARIES

```
aegis_bot/
├── core/
│   ├── aegis_gamma_engine.py      # CORE BRAIN: State tensors, Math Kernels, Bucket Recompute, Signal Gen
│   └── __init__.py
├── execution/
│   ├── aegis_execution_manager.py # EXECUTION: Signal Consumption, Order Lifecycle, Fill Feedback, Risk
│   ├── capital_engine.py          # Capital Allocation (Shared)
│   ├── order_lifecycle.py         # Shoonya/Paper REST Abstraction (Shared)
│   ├── strike_selector.py         # Strike Selection Logic (Shared)
│   └── trade_context.py           # Trade State Persistence (Shared)
├── main.py                        # ORCHESTRATOR: WS Wiring, Session Mgmt, Dynamic Subscriptions
└── config/
    └── settings.py                # Env Constants (Lot sizes, Tick sizes, Paths)
```

### **Ownership Rules for Agent**
| File | Agent Permission Level | Notes |
| :--- | :--- | :--- |
| `aegis_gamma_engine.py` | **RESTRICTED** | Math kernels, tensor layouts, gate logic. **Requires Whitepaper re-derivation for changes.** |
| `aegis_execution_manager.py` | **MODERATE** | Order logic, risk params, fill handling. **Must preserve `on_fill` callback signature.** |
| `main.py` | **FLEXIBLE** | Wiring, subscriptions, session mgmt. **Must not block tick listener thread.** |
| `calibration_runner.py` (External) | **PRIMARY TOOL** | Only way to update `aegis_calibrated_params.json`. Agent triggers this, never edits JSON manually. |

---

## 4. HOT PATH DISCIPLINE: `on_tick_parsed` (The "Clean Room")

**This function is the zero-allocation critical section.**

### **Allowed Operations (O(1), Zero Alloc):**
- Fixed-index NumPy array writes: `self.bucket_ofi[side, idx] += dvol`
- Scalar math: `max`, `min`, `abs`, `+`, `-`, `*`, `/`
- `deque.append` (pre-allocated `maxlen`)
- `getattr`/`setattr` for per-symbol/per-strike scalar state (volume tracking)
- `math.exp`, `math.log` (scalar)

### **FORBIDDEN Operations (Allocator/Complexity Traps):**
- ❌ **Any `np.` function call** (creates views/arrays). Use scalar `math.` or pre-computed tensors.
- ❌ **Any `dict`/`list` creation** ( `{}` , `[]` , `dict()` , `list()` ).
- ❌ **Loops over `N_STRIKES` (21) or `N_LEVELS` (5)**. Unroll manually if needed (see `cascade_engine.py` `_walk_book_impact_fast`).
- ❌ **String formatting / f-strings** (allocates string objects). Log only at bucket boundary.
- ❌ **Function calls with complex signatures** (keep call stack shallow).
- ❌ **Accessing `self.hawkes_bins` 2D array via fancy indexing**. Use `self._hawkes_bin_ptr` scalar index.

### **Pattern Template for New Per-Tick Metrics:**
```python
# 1. Pre-allocate in __init__:
self.new_metric_tensor = np.zeros((N_TYPES, N_STRIKES), dtype=np.float32)
self.new_metric_history = np.full(HIST, np.nan, dtype=np.float32) # For z-scoring

# 2. In on_tick_parsed (HOT):
#    ONLY update sufficient statistic (scalar/array element)
self.new_metric_tensor[side, idx] += incremental_value

# 3. In _process_bucket (COLD):
#    Compute heavy math, push to history, reset tensor
val = self.new_metric_tensor.sum()
self._push(self.new_metric_history, val)
self.new_metric_tensor.fill(0)
```

---

## 5. CALIBRATION & PARAMETER MANAGEMENT PROTOCOL

**The Agent NEVER edits `aegis_calibrated_params.json` directly.**

### **The Registry Contract (`_load_or_default_params`)**
Every parameter row **MUST** have:
```python
"param_name": {
    "value": 1.23,
    "source": "walkforward_2024_Q3 | prior_median | theoretical_bound",
    "validation": "Sharpe > 1.5 OOS | precision@recall > 0.85 | D0 > 1e-8",
    "bounds": [min, max],
    "links": ["Eq 2.3", "Sec 4.2", "Registry Row 14"]
}
```

### **Agent Workflow for Parameter Changes:**
1.  **Detect Drift:** Monitoring alerts (Sharpe decay, Gate precision drop, Fill latency shift).
2.  **Propose Hypothesis:** "Gamma decay length `ℓ` shifted due to new MM liquidity provisioning."
3.  **Modify `calibration_runner.py` Config:** Update search space/priors for target param.
4.  **Trigger Calibration Run:** `python -m aegis_bot.calibration_runner --symbol BANKNIFTY --window 6M`.
5.  **Validate Output:** Runner emits `aegis_calibrated_params_new.json` + Validation Report (HTML/JSON).
5.  **Atomic Swap:** If report passes **ALL** validation guards → `mv new.json aegis_calibrated_params.json`.
6.  **Hot Reload:** Call `aegis_engine.reload_params()` via admin endpoint (no restart).

### **Protected Parameters (Require Whitepaper Amendment):**
- `liquidity_decay_len` (ℓ) — Changes `R_ij` topology.
- `hawkes_base_excitation` (α₀) — Changes criticality boundary.
- `conviction_threshold_frac` — Changes Phase 2 decision boundary.
- `exit_w`, `xi_soft`, `xi_hard` — Changes exit dynamics fundamentally.

---

## 6. TESTING & VALIDATION STRATEGY (The "CI/CD" for Agents)

### **Level 0: Static Invariants (Pre-Commit Hooks)**
```bash
# 1. Zero Alloc Check (pytest + tracemalloc)
pytest tests/test_zero_alloc.py::test_on_tick_parsed_zero_alloc

# 2. Magic Number Scan (regex)
# Fails if float/int literal found outside _load_or_default_params or constants block
python scripts/scan_magic_numbers.py aegis_bot/core/aegis_gamma_engine.py

# 3. Signature Drift Check
# Ensures on_fill(callback) signature matches ExecutionManager call site
python scripts/check_signatures.py
```

### **Level 1: Unit Math Correctness (Deterministic)**
| Test | Input | Expected | Tolerance |
| :--- | :--- | :--- | :--- |
| `test_ngao_quadratic_root` | `V_oe=1e7, ΣΓ=0.5, ΣSpeed=1e-4, η=1.5` | Matches Whitepaper Eq 2✓ (manual calc) | `1e-9` rel |
| `test_ngao_speed_zero_guard` | `sum_speed=0.0` | Returns `1/D0` (Linear limit) | Exact |
| `test_ngao_criticality_guard` | `D0=1e-9` | Returns `1e6` (Explosive) | Exact |
| `test_impvol_newton_atm` | `S=50000, K=50000, T=3/365, iv=0.15` | `0.15` | `1e-6` abs |
| `test_impvol_newton_deep_itm` | `S=50000, K=45000, T=1/365, iv=0.3` | Valid IV / NaN | No Crash |
| `test_csldt_routing_matrix` | Synthetic Γ, `ℓ=2.0` | Rows sum to 1.0, Symmetric decay | `1e-9` |
| `test_hawkes_branching_bounds` | Random `z` | `0.0 <= B <= 10.0` | Bounds |

### **Level 2: Adversarial Simulation (Property-Based)**
*Run via `hypothesis` or custom simulator.*
- **Spoof Injection:** Inject fake L1 size (bid/ask) for 1-2 ticks → Verify `G6` rejects (or logs warning).
- **IV Crush Injection:** Step `IV` -200bps in 1 bucket → Verify `G7` Hard Block triggers.
- **Symmetric Chain:** `Speed` sum ≈ 0 → Verify NGAO doesn't DivZero.
- **Latency Attack:** Simulate 5s fill delay → Verify `C_effective` decays per `S_hat`.

### **Level 3: Walk-Forward Integration (The "Truth")**
- **Data:** 6 Months NSE L2 (Options + Future) + Tick Trades.
- **Metric:** **Net PnL after Costs** (Slippage + Fees + Latency Decay).
- **Constraint:** Must beat **Buy & Hold Straddle** and **Random Entry** baselines.
- **Gate Precision:** `G1..G9` Precision@Recall > 85% on OOS month.
- **Latency CDF:** `S_hat` (Post-fill edge survival) > 60% at `T_hold=30s`.

---

## 7. COMMON FAILURE MODES & CANONICAL FIXES

| Symptom | Root Cause (First Principles) | Canonical Fix |
| :--- | :--- | :--- |
| **"NameError: is_call"** | Missing param in function sig (IV Solver) | Add `is_call=True` to `_impvol_newton` signature. **Run `test_impvol_signature`**. |
| **`ZeroDivisionError` in NGAO** | `sum_speed ≈ 0` (Symmetric Chain) | Dual Guard: `if abs(sum_speed) < 1e-10: return 1/D0`. **Run `test_ngao_speed_zero_guard`**. |
| **Conviction `NaN`** | `sigma_x = 0` in `tanh(x/sigma)` | Ensure all `sigma_*` params > 0 in Registry. Add `max(sigma, 1e-6)` in `_compute_conviction`. |
| **G6 Passes on Spoof** | Back-of-queue assumption invalid / `w_q` too high | 1. Log `G6_RUNNING_UNVALIDATED...` 2. Source Order-ID Data 3. Calibrate `τ_short/med/long`. |
| **Latency Decay `C_eff` > `C0`** | `S_hat` logic inverted (Fill Latency vs Hold Time) | Fix `_effective_conviction`: `S_hat = P(Edge_PostFill > Cost)`, NOT `P(L_fill > T_hold)`. |
| **Memory Leak (Slow)** | `deque` without `maxlen` OR growing `list` in hot path | Audit `self.__init__`: ALL deques `maxlen=X`. No `list.append` in `on_tick_parsed`. |
| **ATM Shift Corrupts Tensors** | `np.roll` on 3D arrays shifts wrong axis | Verify `shift_atm`: `axis=1` for `(Type, Strike, Level)`, `axis=1` for `(Type, Strike)`. |
| **TP Order Stuck "OPEN"** | Chaser logic modifies wrong `ordno` / Exchange Segment | Log `ordno`, `exch_seg`, `symbol` at every chase step. Verify `modify_order` vs `cancel+place`. |

---

## 8. AGENT WORKFLOWS (Standard Operating Procedures)

### **Workflow A: "I need to add a new microstructure metric (e.g., VPIN variant)"**
1.  **Define Math:** Write Whitepaper Section (First Principles Derivation). **No code yet.**
2.  **Update Registry:** Add rows for all new constants (weights, scales, thresholds).
3.  **Pre-allocate Tensors:** Add `self.new_metric_tensor`, `self.new_metric_hist` in `__init__`.
4.  **Hot Path:** Add **single line** scalar update in `on_tick_parsed` (sufficient stat only).
5.  **Cold Path:** Add computation in `_process_bucket` (vectorized NumPy).
6.  **Gate/Conviction:** Add `G_new` (if structural invariant) or `C_new` (if probabilistic).
7.  **Tests:** Add Level 0 (Alloc), Level 1 (Math), Level 2 (Adversarial).
8.  **Calibration:** Add to `calibration_runner.py` search space.
9.  **PR:** Include Whitepaper diff + Registry diff + Test results.

### **Workflow B: "Execution fill price looks wrong / Slippage high"**
1.  **Check `_chase_exit_thread`:** Verify `price_1/2/3` calc uses `leg.current_price` (Live Bid), not `ltp`.
2.  **Check `on_fill` Callback:** Ensure `fill_ts` is `time.time()` **immediately after** REST `place_order` returns success, NOT after history poll.
3.  **Check AEGIS Latency Buffer:** Verify `post_fill_edge_buf` is populated at `fill_ts + T_hold` (async check in main loop).
4.  **Verify `cost_bps`:** In Registry, matches actual Brokerage + STT + Exchange Fees + Slippage Buffer.
5.  **Run Latency Simulation:** `python -m aegis_bot.sim.latency_attack --delay 2.0 --duration 100`.

### **Workflow C: "Daily PnL Negative / Sharpe Decay"**
1.  **Pull Validation Report:** `latest_calibration_report.html`.
2.  **Check Gate Precision:** `G1..G9` confusion matrices on recent OOS window.
3.  **Check Conviction Calibration:** Plot `Conviction` vs `Realized_Return` (Should be monotonic).
4.  **Check Latency CDF:** `S_hat` dropping? → Broker API slower? → Increase `lambda_decay` or `hold_time_sec`.
4.  **Check IV Model:** `bucket_iv_change` vs Realized IV Move (Is Vega Veto `G7` firing correctly?).
5.  **Trigger Re-calibration:** `calibration_runner.py --recent-window 30D --full-grid`.

---

## 9. DEBUGGING TOOLKIT (Agent Accessible)

### **Live Introspection (via Admin Socket / Logs)**
```python
# 1. Engine State Snapshot (callable via RPC)
aegis_engine.get_state_summary() 
# Returns: spot, atm_iv, gamma_sum, speed_sum, hawkes_B, fills_recorded, params_source

# 2. Bucket Diagnostics (Logged every bucket in JSONL)
# Tail -f logs/aegis_live_YYYY-MM-DD.jsonl | jq '.conviction_eff, .gates'

# 3. Signal Queue Monitor
# watch -n 1 'python -c "import queue; q=...; print(q.qsize())"'

# 4. Fill Latency CDF (Runtime)
import numpy as np
n = min(aegis_engine.fill_ptr, 500)
latencies = aegis_engine.fill_latency_buf[:n]
edges = aegis_engine.post_fill_edge_buf[:n]
print(f"Median Latency: {np.median(latencies):.2f}s | Edge Survival @2bps: {(edges>2).mean():.2%}")
```

### **Post-Mortem Replay (Deterministic)**
```bash
# 1. Extract Tick Stream (Protobuf/JSONL) for target date
python -m aegis_bot.tools.export_ticks --date 2024-10-15 --symbol BANKNIFTY --out /data/replay.bin

# 2. Run Replay Engine (Zero-Clock, Deterministic)
python -m aegis_bot.tools.replay_engine --ticks /data/replay.bin --params aegis_calibrated_params.json --output /data/replay_report.html

# 3. Report Contains:
#    - Tick-by-tick Conviction / Gates / Signal
#    - Fill Simulation (Latency Model + Chase Logic)
#    - PnL Attribution (Gamma / Vega / Theta / Slippage)
#    - Gate Precision/Recall vs Labels
```

---

## 10. EMERGENCY PROCEDURES

| Scenario | Command | Effect |
| :--- | :--- | :--- |
| **Runaway Position** | `curl -X POST localhost:8080/admin/force_flatten` | Sets `EXIT_PENDING`, cancels TP, chases exit immediately. |
| **Param Corruption** | `curl -X POST localhost:8080/admin/reload_params` | Hot-reloads `aegis_calibrated_params.json` (validates schema first). |
| **WS Feed Stalled** | `curl -X POST localhost:8080/admin/ws_reconnect` | Triggers `mdp._ensure_ws_started()` + resubscribe tokens. |
| **IV Solver Stuck** | `curl -X POST localhost:8080/admin/clear_iv_cache` | Resets `iv_tensor` to NaN, forces re-solve next bucket. |
| **Kill Switch** | `touch /tmp/AEGIS_KILL_SWITCH` | Main loop checks file -> `trader.stop()` -> `sys.exit(0)`. |

---

## 11. VERSIONING & RELEASE CHECKLIST

**No Release Without:**
- [ ] `pytest tests/ -xvs` **PASSES** (Level 0, 1, 2).
- [ ] `calibration_runner.py --validate-latest` **PASSES** (Sharpe > 1.5, Gates > 85% Prec).
- [ ] `replay_engine` on **Last 3 Expiry Weeks** matches Live PnL within **5%**.
- [ ] `scan_magic_numbers.py` **CLEAN**.
- [ ] `tracemalloc` on 1M synthetic ticks: **Delta < 10 KB**.
- [ ] `aegis_calibrated_params.json` **SHA256** recorded in `RELEASE_MANIFEST.json`.
- [ ] Whitepaper **PDF** updated with any derivation changes (Git LFS).

---

**END OF HARNESS DOCUMENT**  
*This document is the contract between the Principal Engineer and the Agent. Deviation requires written (commented) justification rooted in the Whitepaper.*