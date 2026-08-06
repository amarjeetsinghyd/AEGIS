# Astra V3 System Architecture

**Status:** Active source of truth for Astra V3 Options Microstructure Engine  
**Updated:** 2026-08-06 (IST — Qwen 3.8 Max Architecture Overhaul: Signed Delta, Event-Time Bucketing, OFI, ATM±3 Jump Protection, Thread Locks)  
**Scope:** Local monorepo + Oracle VPS (`opc@137.23.41.38`, user `opc`)  

This document serves as the absolute source of truth for the **Astra V3** engine. Astra is distinct from Kronos; it is a sub-second, High-Frequency Trading (HFT) options engine focused on **Microstructure Synergy (L1 Momentum + L2 Absorption)**.

---

## 1. Mission & Core Philosophy

### 1.1 The Engine's Purpose
Astra V3 is engineered to trade Nifty and Sensex weekly options by identifying institutional traps and breakouts. While traditional momentum bots fail during fakeouts (spoofing), Astra V3 waits for **T+1 Confirmation** at the exact milliseconds where momentum collides with limit-order walls.

### 1.2 Core Philosophy
* **L1 (Market Momentum & OFI):** Reveals where the market *wants* to go by tracking aggressive market orders crossing the bid-ask spread and the net addition/cancellation of top-of-book liquidity (Order Flow Imbalance).
* **L2 (Market Depth):** Reveals what is *blocking* the market by exposing limit-order walls in the options chain.

If L1 is aggressively pushing UP but an L2 wall is blocking it, the bot **waits (T+1)**. 
- If the wall breaks → **Breakout Trade**.
- If the wall holds → **Absorption (Reversal) Trade**.

---

## 2. System Topology

Astra runs as an independent PM2 process alongside the Shoonya Data Hub and Kronos. 

```text
PM2 (VPS)
├── shoonya-data-hub   → Feeds raw ticks via WebSocket
├── astra-bot          → Ingests WS ticks, compiles footprints, fires REST exits/entries
│                        Outputs: /dev/shm/kdo_live_state.json
```

---

## 3. Component 1: `FootprintCompiler` (Tick-Level Aggregation)

File: `astra_bot/core/footprint_compiler.py`

The compiler creates strict 1-minute buckets tracking footprints. It receives data from a broad subscription (ATM±10) but outputs compiled footprints specifically requested by the main loop.

### 3.1 Tick-Level Aggression & OFI Detection
On every single tick, it analyzes `LTP` against the Best Bid (`bp1`) and Best Ask (`sp1`):
* **Aggressive BUY:** `LTP >= Best Ask (sp1)`. Buyer crossed the spread.
* **Aggressive SELL:** `LTP <= Best Bid (bp1)`. Seller crossed the spread.
* **Order Flow Imbalance (OFI):** Tracks changes in `bq1` (Bid Qty) and `sq1` (Ask Qty) at the top of the book. Positive OFI indicates passive buying liquidity is being added or ask liquidity is pulled.

### 3.2 L2 Snapshotting & Quote Freshness
When an aggressive trade occurs, the compiler snapshots the full Level-2 depth (up to 5 levels).
**Quote Freshness Filter:** It records `last_depth_ts`. If the L2 update is older than 1.5 seconds, the depth is considered "stale" to prevent ghost-wall collisions.

### 3.3 Event-Time Safe 1-Minute Bucketing
The compiler strictly buckets ticks by `exchange_timestamp // 60` rather than the local machine clock. When the minute boundary crosses on the exchange feed, the `footprints` dict is copied into a `closed_footprints` snapshot. The main loop evaluates *only* this closed snapshot, ensuring zero race conditions or data corruption from late-arriving ticks.

### 3.4 L2 Depth Dynamics (Spoofing Filter)
To prevent triggering false absorption signals on flash-spoof orders, the compiler maintains a rolling `deque` of the top-of-book depth (`bq1` and `sq1`). If the current wall size drops below 20% of its recent average size, it flags the wall as a `spoof`.

---

## 4. Component 2: `ChainFootprintEvaluator` (T+1 Logic)

File: `astra_bot/strategy/evaluator.py`

### 4.1 Signed CE/PE Chain Delta & Minimum Volume Gate
The evaluator calculates a rigorously **Signed Delta**:
* `signed_flow = (ce_buy - ce_sell) + (pe_sell - pe_buy)`
This correctly treats aggressive PE buying as Bearish.
* **Volume Gate:** If the absolute total options volume (`total_chain_buy + total_chain_sell`) is < 500 lots, the minute is discarded as noise.

### 4.2 CE vs. PE Skew (Straddle Trap Filter)
* **Bullish Breakout (Market UP):** If overall `delta_pct > 10%`, the bot checks `pe_delta`. If Puts are *also* being aggressively bought (`pe_delta > +5%`), it identifies a gamma/volatility straddle spike and **aborts**.
* **Bearish Breakout (Market DOWN):** If overall `delta_pct < -10%`, checks `ce_delta > +5%` to abort.

### 4.3 Target-Strike & OFI Confirmation
Before triggering a trade based on the overall chain delta, the evaluator performs strict checks on the specific "Battleground" target strike:
1. **Target-Strike Delta:** The target CE/PE must have more buying than selling (`target_buy > target_sell`).
2. **Target-Strike OFI:** The target CE/PE must have a positive Order Flow Imbalance (`OFI > 0`).

### 4.4 T+1 Conflict Resolution
If Chain Delta is > +10% (Market UP) but the Battleground has an Ask Wall > 1.5x Bid Depth, it flags an `ABSORB_CONFLICT` (unless the quote is stale or flagged as a spoof).
In the next minute (T+1):
* **Wall Thins (<70%) + Delta Stays Positive:** Buyers chewed through the wall. → `LATE_BREAKOUT_CALL`.
* **Wall Holds (>1.5x) + Delta Stays Positive:** Buyers are trapped. Sellers absorbed the liquidity. → `ABSORPTION_PUT`.

---

## 5. Component 3: Execution & Risk (`AstraExecutionManager`)

File: `astra_bot/execution/astra_execution_manager.py`

### 5.1 Enforced ATM±3 Jump Protection (Main Loop)
The main loop dynamically calculates the ATM strike every minute. 
* **Broad Subscription:** It subscribes to `ATM±10` via WebSocket to ensure data keeps flowing even if the market spikes 100 points in seconds.
* **Narrow Evaluation:** It strictly passes only the `ATM±3` strikes to the compiler for evaluation to eliminate deep-OTM noise.

### 5.2 Thread Locks (Race Condition Prevention)
A `self.trade_lock` completely wraps `trade_context` state mutations in the Execution Manager. This prevents the WebSocket callback thread (which monitors SL/TP) from colliding with the Main Thread (which executes logic and order updates), eliminating duplicate close orders.

### 5.3 WebSocket Queue Exits (HFT Grade)
1. **Tick Callback:** `FootprintCompiler` instantly fires `tick_callback` to the EM on every WS tick.
2. **Non-Blocking Queue:** If `current_ltp >= TP` or `<= SL`, the EM puts an exit signal into `exit_signal_queue`.
3. **Main Loop Execution:** `main.py` blocks on `queue.get(timeout=0.5)`. The millisecond an exit signal arrives, the main thread wakes up and fires the REST exit order. 

### 5.4 Partial Fills Management (Anti-Naked Short)
If the resting limit TP order is only partially filled and the market reverses to hit the SL, the bot queries Shoonya's `single_order_history`, dynamically subtracts `filled_qty` from `leg.quantity`, and fires the market exit SL **only for the remaining balance**.

---

## 6. Live Paper-Trading Tuning Guidelines (Day-1 Checklist)

During live market execution, the following metrics must be monitored in the PM2 logs (`astra-bot`) to tune the V3 filters:

1. **The 500 Volume Gate:**
   - *Monitor:* The first 30 mins (09:15-09:45) and post-lunch lull (13:00-13:30).
   - *Tuning:* If great setups are blocked during low-volatility hours, make the gate time-dependent (e.g., `MIN_VOL = 200` before 10:00 AM, `500` after).
2. **The 1.5s Quote Freshness Filter:**
   - *Monitor:* Look for frequent `Stale quote - aborting trade` logs on highly liquid ATM strikes.
   - *Tuning:* Shoonya's WS feed can micro-stutter. Loosen the threshold to `2.0s` or `2.5s` if false rejections are high.
3. **OFI vs. Actual Breakouts:**
   - *Monitor:* Log `OFI` alongside `chain_delta`.
   - *Tuning:* Passive limit orders are frequently modified (flickered) in India. Adjust the `OFI > 0` threshold to filter out "fake" passive liquidity if needed.
4. **Spoofing Filter Sensitivity (`0.20` multiplier):**
   - *Monitor:* How often `ask_wall_spoof = True` triggers. 
   - *Tuning:* If it triggers > 50% of the time, decrease to `0.15`.
5. **Straddle Trap Threshold (`0.05` / 5%):**
   - *Monitor:* Ensure valid trades aren't aborted during genuine trend days.
   - *Tuning:* Raise the threshold to `0.07` or `0.08` if massive breakout days are missed.
