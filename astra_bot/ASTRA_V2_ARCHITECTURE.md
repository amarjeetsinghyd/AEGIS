# Astra V4.2 Architecture Deep Dive (L1 + L2 Synergy)

Astra V4.2 is a high-frequency options trading bot engineered to trade BankNifty, Nifty, and Sensex. Its defining feature is a tick-level order flow aggregation engine (L1) coupled with real-time Level-2 market depth analysis (L2) and a novel Pending Entry Confirmation Window. This multi-layer approach filters out institutional spoofing, orderbook traps, and lag effects that traditional momentum bots fall prey to.

Unlike Astra V1 and Kronos which relied purely on broad price action or volume, Astra V4.2 implements a true **Microstructure Synergy (L1 + L2)** with a **Pending Entry Window** to definitively detect absorption and breakouts.

---

## 1. Core Philosophy

The core philosophy of Astra V4.2 is straightforward:
* **L1 (Market Momentum):** Reveals where the market *wants* to go by tracking aggressive market orders. (Direction)
* **L2 (Market Depth):** Reveals what is *blocking* the market from getting there by exposing limit-order walls. (Resistance/Traps)
* **Price Effect (Confirmation):** Order flow often leads price by seconds or minutes. We never enter immediately on flow; we arm a signal and wait for the underlying price to physically move in our direction before committing capital.

---

## 2. Component 1: `FootprintCompiler` (Tick-by-Tick Aggregation)

The `FootprintCompiler` is a strict 1-minute bucket. It simultaneously tracks 14 strikes (ATM ± 3 Calls and Puts). It does not use a rolling 60-second window; it strictly flushes on the exact clock minute.

### 2.1. Tick-Level Aggression Detection
On every single tick (at the millisecond level), the compiler analyzes the Last Traded Price (LTP) against the Best Bid (bp1) and Best Ask (sp1) to determine the aggressor:
* **Aggressive BUY:** If `LTP >= Best Ask (sp1)`.
* **Aggressive SELL:** If `LTP <= Best Bid (bp1)`.
* **Passive (NEUTRAL):** Trades that execute between the spread are ignored.

### 2.2. Noise Filtering (Cumulative Volume Trap)
Astra uses `vol_delta` instead of absolute volume to track footprint sizes. Single-tick volumes exceeding 1,000 lots are flagged as exchange baseline snapshots and explicitly zeroed out to prevent them from corrupting the real-time flow delta.

### 2.3. Tick-Level L2 Snapshotting
At the exact millisecond an aggressive trade is detected, the compiler captures a snapshot of the Level-2 order book depth (bp1-5, sp1-5) and tracks Order Flow Imbalance (OFI).

---

## 3. Component 2: `ChainFootprintEvaluator` (Signal Generation)

This is the "Brain" of Astra V4.2. It evaluates the 1-minute compiled footprint data and makes trading decisions using Signed Flow Delta and Iceberg detection.

### 3.1. Calculate Chain Delta
The bot calculates the net aggression across the entire 14-strike options chain:
* `Chain Delta = (Total Aggressive Buy Vol - Total Aggressive Sell Vol) / Total Vol`
* Signed flow is used to isolate direction (e.g., PE selling is bullish).

### 3.2. Iceberg Shadow Detection (`_confirmation_block`)
Before firing a trade in the direction of the Delta, it checks the L2 snapshots. If an Ask Wall exists while Delta is positive, it signifies heavy aggressive buying is occurring, but massive passive limit-sellers (Operators) are absorbing it via Icebergs. The system generates a "confirmation block" and refuses to trade until the iceberg melts or the price breaks through.

---

## 4. Component 3: Execution & Trade Lifecycle (`AstraExecutionManager`)

When a valid signal is generated, it is passed to the Execution Manager. It enforces strict risk management, slippage limits, and rapid exit logic.

### 4.1. The Pending Entry Window (0.5s Polling)
Astra V4.2 **does not execute instantly**. When a signal is triggered, it is "ARMED" and placed in a Pending Entry Window. The bot polls the market every 0.5s:
* **Trigger:** If the underlying moves +3.0 pts in our favor (or premium +0.10), the flow effect is confirmed, and the trade is executed.
* **Invalidation:** If the underlying moves -8.0 pts against us (or premium -0.25), the flow was fake/absorbed, and the signal is killed.
* **Expiry:** Signals expire if unconfirmed after 120 seconds.

### 4.2. Entry Mechanics (Marketable Limit Orders)
Due to extreme illiquidity in far OTM strikes or sudden gamma spikes, standard Market Orders are dangerous. Astra V4.2 enters using a `Marketable Limit Order` with a buffer:
* **Order Price = `LTP + 8%` (minimum buffer of 2.0 pts)**
* This ensures instant execution against the L2 Ask but acts as a hard ceiling, protecting the bot from catastrophic slippage on thin strikes.

### 4.3. Dynamic Exits (Trailing SL & No-Effect Stops)
Once open, Astra V4.2 monitors the position tick-by-tick without blocking the main loop:
* **Take Profit (TP):** Resting limit order at +10% premium.
* **Profit-Lock SL:** If the premium reaches halfway to TP (+5%), the hard SL is moved up to +1% to guarantee a break-even trade.
* **Hard SL:** -15% premium stop loss (marketable limit).
* **No-Effect Early Stop (5 Minutes):** Options suffer from Theta decay. If the trade is open for 300 seconds and the premium hasn't moved > 1%, the bot aggressively exits to prevent bleeding capital. 
* **Hard Time Stop (10 Minutes):** Absolute max hold time before auto-flattening.
