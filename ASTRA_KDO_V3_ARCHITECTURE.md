# ASTRA KDO v3.0
## Continuity-Preserving Gamma-Regime Orderflow Engine

---

## 1. System Identity

**ASTRA KDO v3.0** is the upgraded microstructure engine that now governs the active KDO runtime inside the Kronos bot deployment. It replaces the older flush-and-pause orderflow architecture with a continuity-preserving basket model, low-lag kinematic state estimation, and distribution-free anomaly scoring designed for a constrained Oracle Cloud VPS.

This document is the authoritative architecture specification for the post-upgrade live runtime. It supersedes the older KDO v2 behavioral description wherever that older document still refers to full ring-buffer flushes, enforced rollover blindness, deep EMA lag pipelines, or hard-coded acceleration thresholding.

---

## 2. Mission

The mission of ASTRA KDO v3.0 is to detect sharp, explosive options momentum only when the market is in a negative gamma exposure regime and only when the observed orderflow is statistically unusual relative to its own recent empirical behavior.

The engine is specifically optimized for four realities:

1. **Live Shoonya WebSocket ingestion** rather than slow polling.
2. **Strict memory limits** on a 1 GB Oracle VPS.
3. **Strike-basket drift** during strong directional markets.
4. **Need for low-lag inference** without the false comfort of heavy smoothing.

---

## 3. What Changed From KDO v2

ASTRA KDO v3.0 replaces the old design assumptions below:

- **Old model:** ring buffer flush on strike rollover.
- **New model:** preserve the ring buffer and rebase OI continuity offsets across the new basket.

- **Old model:** enforce a hard 30-second kinematic pause after ATM drift.
- **New model:** keep the engine live, mark rollover as a transition state, and degrade confidence only when coverage is incomplete.

- **Old model:** dual-stage EMA smoothing plus a fixed institutional acceleration threshold and Z-score gate.
- **New model:** causal low-lag state estimation plus empirical tail scoring with no hard absolute anomaly threshold.

- **Old model:** thresholds tied to static magnitude floors.
- **New model:** anomaly detection tied to the recent empirical distribution of acceleration, velocity, residual stress, breadth, and flow duration.

---

## 4. Active Runtime Topology

The live runtime remains a two-process PM2 deployment:

- `astra-bot` -> `astra_bot/main.py`
- `astra-dashboard` -> `astra_bot/dashboard.py`

These are the only runtime entrypoints that matter for production behavior.

The bot and dashboard communicate through Linux shared memory:

- Shared state path: `/dev/shm/live_kdo_state.json`

The dashboard is therefore not the source of truth. The bot process is the source of truth, and the dashboard is only a read-side projection of the latest bot state.

---

## 5. Files That Define ASTRA KDO v3.0

The live architecture is concretely expressed through the following runtime files:

- `astra_bot/core/ring_buffer.py`
- `astra_bot/core/microstructure_engine.py`
- `astra_bot/main.py`

These files were upgraded in-place in the active runtime tree rather than by creating parallel production entrypoints.

---

## 6. Core Design Principles

### 6.1 Continuity over reset
The engine must never intentionally blind itself during the very market moves it is supposed to capture.

### 6.2 Causality over lag
All signal estimation must be causal and live-safe. The engine cannot rely on future bars or symmetric smoothers.

### 6.3 Relative surprise over fixed magnitude
An anomaly is not defined by a hard number like 10,000 or 1,000,000. It is defined by how unusual the current state is versus the recent empirical behavior of the same system.

### 6.4 Regime gating before direction
Directional signals are subordinate to regime. If gamma regime is not negative, momentum entries are blocked.

### 6.5 Confidence degrades gracefully
When the basket changes or coverage is incomplete, the engine should not collapse into blindness. It should reduce confidence, surface transition state, and continue collecting continuity-preserving state.

### 6.6 Memory discipline
The engine must remain lightweight enough for a 1 GB RAM environment and avoid unnecessary history expansion, dataframe overhead, or heavyweight inference stacks in the live microstructure path.

---

## 7. Dataflow Overview

### 7.1 Ingestion
Shoonya WebSocket ticks feed live spot, option prices, and option OI into the market-data layer.

### 7.2 Deduplication
Incoming ticks are deduplicated in O(1) fashion so that repeated broker broadcasts do not distort the orderflow series.

### 7.3 Micro-bucketing
Ticks are aggregated into 5-second micro-buckets.

### 7.4 Continuity-preserving basket state
The active CE/PE token sets are tracked continuously. When the strike basket changes, the engine rebases its level offsets instead of clearing historical state.

### 7.5 Kinematic inference
The net delta OI series is passed through a causal state-estimation layer to infer level, velocity, acceleration, and residual stress.

### 7.6 Distribution-free anomaly scoring
The engine compares current kinematic stress to its empirical recent tail behavior rather than to absolute hand-tuned floors.

### 7.7 Gamma regime gate
Only negative gamma regimes are allowed to emit actionable directional entries.

### 7.8 Dashboard projection
The bot writes operator-readable live state to `/dev/shm/live_kdo_state.json`, and the dashboard displays the latest snapshot.

---

## 8. Continuous Basket Architecture

### 8.1 Problem
In a trending market, the relevant ATM-centered option basket shifts as spot moves. If the engine replaces the old basket with the new basket naively, the aggregate OI level jumps for structural reasons rather than informational reasons.

### 8.2 Old failure mode
The older engine handled this with a destructive reset: clear the 60-second ring buffer and freeze trading while new samples accumulate.

### 8.3 ASTRA solution
ASTRA KDO v3.0 treats basket rollover as a **continuity transformation** rather than a reset event.

When the CE/PE token set changes:

1. The previous continuous call and put OI levels are remembered.
2. Raw OI for the new token set is measured immediately.
3. Offsets are computed so that the new basket begins on the same continuous level as the previous basket.
4. Future buckets evolve from this rebased state naturally.

This preserves the interpretability of first and second differences without forcing the engine to go blind.

### 8.4 Transition semantics
Rollover is still recorded as a transition state, but it is not treated as a mandatory pause. Confidence is reduced only when coverage is too thin during the rollover grace interval.

---

## 9. Ring Buffer Model

The engine keeps a compact 60-second state window using 12 snapshots of 5 seconds each.

Each snapshot carries the minimum live information needed for microstructure inference:

- timestamp
- spot price
- continuous calls OI
- continuous puts OI
- net delta OI
- raw calls OI
- raw puts OI
- raw net delta OI
- basket coverage
- number of active tokens
- option prices
- option OI map
- ATM strike
- rollover state flags

The ring buffer is therefore a stateful inference structure, not a passive history dump.

---

## 10. Kinematic State Estimation

### 10.1 Objective
The goal is to estimate orderflow level, velocity, and acceleration with minimal lag and without unstable raw differencing.

### 10.2 Model family
ASTRA uses a causal alpha-beta-gamma style state estimator over the net delta OI trajectory.

State variables:

- level \(x_t\)
- velocity \(v_t\)
- acceleration \(a_t\)

Prediction step:

\[
\hat{x}_{t|t-1} = x_{t-1} + v_{t-1}\Delta t + \frac{1}{2}a_{t-1}\Delta t^2
\]

\[
\hat{v}_{t|t-1} = v_{t-1} + a_{t-1}\Delta t
\]

\[
\hat{a}_{t|t-1} = a_{t-1}
\]

Innovation:

\[
r_t = y_t - \hat{x}_{t|t-1}
\]

Update step:

\[
x_t = \hat{x}_{t|t-1} + \alpha r_t
\]

\[
v_t = \hat{v}_{t|t-1} + \beta \frac{r_t}{\Delta t}
\]

\[
a_t = \hat{a}_{t|t-1} + \gamma \frac{2r_t}{\Delta t^2}
\]

This design is causal, lightweight, and far lower-lag than deep EMA smoothing for breakout capture.

### 10.3 Residual stress
The estimator also retains residuals \(r_t\). Residual magnitude relative to robust residual scale is used as an additional stress signal because breakouts are often characterized not only by directional acceleration but by model surprise.

---

## 11. Robust Scale Estimation

Instead of using plain standard deviation with hand-inserted safety floors, ASTRA uses a robust scale estimate derived from median absolute deviation.

For residual sample set \(R\):

\[
\text{MAD}(R) = \text{median}(|R_i - \text{median}(R)|)
\]

Robust scale:

\[
\sigma_{\text{robust}} \approx 1.4826 \cdot \text{MAD}(R)
\]

This makes the engine less fragile under fat tails, outliers, and sudden microstructure jumps.

---

## 12. Distribution-Free Anomaly Scoring

### 12.1 Motivation
A fixed acceleration threshold is structurally wrong because the same absolute value can be trivial in one regime and extreme in another.

### 12.2 Empirical tail model
ASTRA maintains rolling empirical tails for the following quantities:

- absolute acceleration
- absolute velocity
- residual stress
- participation breadth
- flow duration

For each feature value \(z\), the engine computes an empirical upper-tail probability:

\[
p(z) = \frac{1 + \#\{u \in H : u \ge z\}}{|H| + 1}
\]

where \(H\) is the recent historical sample buffer for that feature.

This produces a conformal-style distribution-free rarity score without assuming Gaussianity.

### 12.3 Combined anomaly score
The engine combines the individual tail probabilities geometrically:

\[
p_{\text{combined}} = \max\left((p_{acc} p_{vel} p_{resid} p_{breadth} p_{flow})^{1/5}, 10^{-12}\right)
\]

Surprisal score:

\[
S = -\log(p_{\text{combined}})
\]

Actionability is based on whether the combined empirical p-value is sufficiently small, not on whether acceleration exceeds an arbitrary absolute floor.

---

## 13. Participation Breadth

A move is stronger when it is supported by a wider fraction of the active basket rather than by one or two noisy contracts.

ASTRA therefore computes directional participation breadth across the tracked CE/PE tokens by comparing option-level OI movement over the current 60-second window.

Broad participation increases confidence. Weak or contradictory participation suppresses entries.

---

## 14. Trade-Signing Layer

The engine retains a practical signer interpretation over the rolling window using aggregate changes in call-side and put-side OI.

Representative states include:

- `BULLISH_ACCUMULATION`
- `BEARISH_ACCUMULATION`
- `PUT_BUILDUP`
- `CALL_BUILDUP`
- `MIXED_BULLISH`
- `MIXED_BEARISH`
- `UNWINDING`
- `NEUTRAL`

This signer layer is not used as a standalone trigger. It is used as a directional coherence gate against the kinematic estimate.

---

## 15. Gamma Engine

ASTRA preserves the vectorized pure-NumPy BSM-based gamma engine concept from the prior architecture, because the negative-versus-positive gamma regime remains structurally central to trade permissioning.

For each strike, implied volatility is solved iteratively, gamma is estimated, and strike-level gamma exposure is aggregated into net gamma exposure.

Conceptually:

\[
\Gamma_k = \frac{N'(d_1)}{S \sigma \sqrt{T}}
\]

\[
GEX_k = OI_k \cdot \Gamma_k \cdot S
\]

\[
GEX_{net} = \sum_{puts} GEX_k - \sum_{calls} GEX_k
\]

Regime rule:

- If \(GEX_{net} < 0\): negative gamma regime, directional entries allowed.
- If \(GEX_{net} \ge 0\): positive gamma regime, directional entries blocked.

---

## 16. Signal Decision Logic

The engine evaluates every incoming tick against a layered gate stack:

1. **Sufficient window?**
   If the 60-second window is not built yet, emit warmup state.

2. **Negative gamma?**
   If net gamma is non-negative, block directional entries.

3. **Directional coherence?**
   Velocity and acceleration must agree in sign for a directional state.

4. **Persistence?**
   Same-sign velocity must persist for enough consecutive buckets.

5. **Coverage adequacy?**
   Basket coverage must be high enough to trust the inference.

6. **Rollover transition safety?**
   During active rollover transition, stricter coverage is required.

7. **Breadth coherence?**
   Participation breadth direction must not contradict kinematics.

8. **Signer coherence?**
   Trade signer direction must not materially oppose kinematics.

9. **Empirical rarity?**
   Combined empirical p-value must be small enough to qualify as a true anomaly.

If all gates pass, the engine emits `CALL` or `PUT`.

---

## 17. Operator States

The upgraded runtime is designed to project intelligible operator states through the dashboard.

Important states include:

- `WAITING FOR LIVE MARKET TICK FEED`
- `KINEMATIC WARMUP (BUILDING 60s WINDOW)`
- `STRIKE ROLLOVER (CONTINUITY PRESERVED)`
- `NEUTRAL (BLOCKED BY POSITIVE GAMMA)`
- `NEUTRAL (CONFLICT DETECTED)`
- `STRONG BULLISH (CALL READY)`
- `STRONG BEARISH (PUT READY)`
- `EMERGENCY KILL SWITCH ACTIVATED`

These states are not cosmetic. They are the operator-facing shadow of the internal gate stack.

---

## 18. Warmup and Edge Initialization

Warmup is no longer a blind architectural defect. It is an explicit and visible initialization phase while the 12-bucket window is being built.

The edge-initialization problem at strike rollover is also redefined. Instead of destroying the buffer and rebuilding from zero, ASTRA preserves state continuity and marks the period as a transition regime.

This distinction is critical:

- **Warmup** means the engine lacks sufficient temporal depth.
- **Transition** means the engine has depth but is evaluating continuity under a changing basket.

---

## 19. Kill Switch Semantics

The engine supports an app-level emergency kill switch using marker files.

If either of the following files exists, trading is blocked:

- `/dev/shm/KILL_SWITCH_ACTIVE`
- `astra_bot/data/KILL_SWITCH_ACTIVE`

This is independent of broker-side segment controls. The app-level kill switch only affects the bot’s own trading loop.

---

## 20. Deployment Model

### 20.1 Live repo
Production runtime path:

- `/home/opc/astra_bot`

### 20.2 Merge workspace
A timestamped merge workspace may be created as a staging copy before overwriting runtime files. This workspace is not a second production repo.

### 20.3 Promotion model
Runtime replacements should be written into the merge copy, syntax-checked, and then copied into the live repo path.

This keeps unrelated branches and non-runtime code intact while safely replacing the active engine.

---

## 21. Runtime Constraints

ASTRA KDO v3.0 is explicitly designed for a constrained Oracle VPS environment.

The live microstructure path should remain lightweight in both memory and compute. The design intentionally avoids heavyweight online inference dependencies in the orderflow engine itself and keeps the core state bounded by compact deques and dictionaries.

---

## 22. Verification Checklist

### 22.1 Process health
```bash
pm2 status
```
Expected: `astra-bot` and `astra-dashboard` both online.

### 22.2 Live state API
```bash
python3 -c "import requests, json; print(json.dumps(requests.get('http://127.0.0.1:5000/api/live_state').json(), indent=2)[:2500])"
```
Expected during market close: idle state, no active kill switch, waiting for live ticks.

Expected during market hours: live spot, option chain, gamma regime, and kinematic state updating.

### 22.3 Deployed code fingerprints
```bash
grep -n "EmpiricalTail\|Preserving ring buffer\|KINEMATIC WARMUP\|CONTINUITY PRESERVED" \
/home/opc/astra_bot/astra_bot/core/microstructure_engine.py \
/home/opc/astra_bot/astra_bot/core/ring_buffer.py \
/home/opc/astra_bot/astra_bot/main.py
```
Expected: matches present in active runtime files.

### 22.4 Kill-switch reset
```bash
rm -f /dev/shm/KILL_SWITCH_ACTIVE
rm -f /home/opc/astra_bot/astra_bot/data/KILL_SWITCH_ACTIVE
rm -f /dev/shm/live_kdo_state.json
pm2 restart astra-bot astra-dashboard
```
Expected: `kill_switch_active` becomes false after restart and state refresh.

---

## 23. Known Gaps

The upgraded architecture is materially stronger than KDO v2, but a few areas remain open:

1. Dashboard recent-log panels can still display historical pre-upgrade rollover lines until new session logs dominate.
2. Dashboard chain BSM presentation may still contain legacy assumptions around DTE handling outside the microstructure core.
3. Broker-side paper/live mode remains distinct from the app-level kill switch and must be handled at configuration level.
4. The live runtime still depends on the broader Kronos bot shell, so the microstructure core is upgraded inside an inherited system boundary rather than in a greenfield standalone service.

---

## 24. Canonical Interpretation

ASTRA KDO v3.0 should be understood as a **continuity-preserving, gamma-gated, low-lag microstructure engine** for options momentum breakout detection.

Its edge does not come from retail indicators, symmetric smoothing, or fixed thresholds. Its edge comes from preserving state through structural basket drift, estimating kinematics causally, and measuring surprise relative to the engine’s own evolving empirical distribution.

---

## 25. Version Record

- **Name:** ASTRA KDO v3.0
- **Expansion:** Continuity-Preserving Gamma-Regime Orderflow Engine
- **Runtime family:** Kronos bot live KDO runtime
- **Primary active files:** `astra_bot/main.py`, `astra_bot/core/ring_buffer.py`, `astra_bot/core/microstructure_engine.py`
- **Dashboard IPC:** `/dev/shm/live_kdo_state.json`
- **Process manager:** PM2
- **Status:** Deployed in active runtime path
