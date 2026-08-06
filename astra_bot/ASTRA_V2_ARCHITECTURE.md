# Astra V2 Architecture Deep Dive (L1 + L2 Synergy)

Astra V2 is a high-frequency options trading bot engineered to trade BankNifty, Nifty, and Sensex. Its defining feature is a tick-level order flow aggregation engine (L1) coupled with real-time Level-2 market depth analysis (L2). This dual-layer approach filters out institutional spoofing and orderbook traps that traditional momentum bots fall prey to.

Unlike Astra V1 and Kronos which relied purely on broad price action or volume, Astra V2 implements a true **Microstructure Synergy (L1 + L2)** with a **T+1 Confirmation Logic** to definitively detect absorption and breakouts.

---

## 1. Core Philosophy

The core philosophy of Astra V2 is straightforward:
* **L1 (Market Momentum):** Reveals where the market *wants* to go by tracking aggressive market orders. (Direction)
* **L2 (Market Depth):** Reveals what is *blocking* the market from getting there by exposing limit-order walls. (Resistance/Traps)

If L1 momentum is aggressively pushing up but an L2 wall is blocking it, the bot **waits**. If the wall breaks, it trades the breakout. If the wall holds, it trades the reversal (absorption).

---

## 2. Component 1: `FootprintCompiler` (Tick-by-Tick Aggregation)

The `FootprintCompiler` (formerly `ring_buffer.py`) is a strict 1-minute bucket. It simultaneously tracks 14 strikes (ATM ± 3 Calls and Puts). It does not use a rolling 60-second window; it strictly flushes on the exact clock minute (e.g., 10:05:59 -> 10:06:00).

### 2.1. Tick-Level Aggression Detection
On every single tick (at the millisecond level), the compiler analyzes the Last Traded Price (LTP) against the Best Bid (bp1) and Best Ask (sp1) to determine the aggressor:
* **Aggressive BUY:** If `LTP >= Best Ask (sp1)`. This means a buyer crossed the spread to take liquidity.
* **Aggressive SELL:** If `LTP <= Best Bid (bp1)`. This means a seller crossed the spread to dump liquidity.
* **Passive (NEUTRAL):** Trades that execute between the spread are ignored and entirely discarded from the delta calculation.

### 2.2. Tick-Level L2 Snapshotting
At the exact millisecond an aggressive trade is detected, the compiler captures a snapshot of the entire Level-2 order book depth:
* `L2 Bid Depth = sum(bq1 to bq5)`
* `L2 Ask Depth = sum(sq1 to sq5)`
This L2 snapshot is attached directly to that specific aggressive tick's footprint. 

### 2.3. The 1-Minute Flush
At the exact turn of the minute, the compiler loops through all recorded aggressive ticks for that minute. It calculates:
* `Total Aggressive Buy Volume`
* `Total Aggressive Sell Volume`
* `Average L2 Bid Depth` (average across all aggressive tick snapshots)
* `Average L2 Ask Depth` (average across all aggressive tick snapshots)

Once these values are passed to the Evaluator, the `FootprintCompiler` bucket is immediately flushed empty to begin the next minute.

---

## 3. Component 2: `ChainFootprintEvaluator` (T+1 Logic)

This is the "Brain" of Astra V2. It evaluates the 1-minute compiled footprint data and makes trading decisions using the **T+1 Conflict Resolution** model.

### 3.1. Calculate Chain Delta
The bot calculates the net aggression across the entire 14-strike options chain:
* `Chain Delta = (Total Aggressive Buy Vol - Total Aggressive Sell Vol) / Total Vol`
* If `Delta > +10%`, the broad market (L1) direction is UP (CALL).
* If `Delta < -10%`, the broad market (L1) direction is DOWN (PUT).

### 3.2. Check L2 Walls (Trap Detection)
Before firing a trade in the direction of the Delta, it checks the Average L2 snapshots for the battleground strike.
* **Ask Wall:** Is `Avg L2 Ask > Avg L2 Bid * 1.5`?
* If an Ask Wall exists while Delta is positive, it signifies heavy aggressive buying is occurring, but massive passive limit-sellers (Operators) are absorbing it.

### 3.3. T+1 Conflict Resolution
If there is a conflict (L1 = UP, L2 = BLOCKED), Astra V2 outputs an `ABSORB_CONFLICT` signal and waits 1 minute (T+1).
* **Breakout (Wall Breaks):** In the next minute (T+1), if the Ask Wall thins out (< 70% of its original size) and Delta remains positive, it confirms the buyers won. It fires a `LATE BREAKOUT CALL`.
* **Absorption (Wall Holds):** In the next minute (T+1), if the Ask Wall remains strong (still > 1.5x Bids), it confirms the buyers are trapped. It fires an `ABSORPTION PUT` (a reversal trade betting against the trapped buyers).

---

## 4. Component 3: Execution & Trade Lifecycle (`AstraExecutionManager`)

When a valid signal is generated, the `AstraExecutionManager` handles the exact mechanics of entry and exit. It enforces a strict "One Open Trade at a Time" rule.

### 4.1. Entry Mechanics (Marketable Limit Orders)
Due to extreme illiquidity in far OTM strikes or sudden gamma spikes, standard Market Orders are dangerous and lead to severe slippage. Astra V2 enters using a `Marketable Limit Order`:
* **Order Price = `LTP + 2.5%`**
* This ensures the order executes instantly against the L2 Ask, but acts as a hard ceiling. It strictly caps slippage at a maximum of 2.5%. If the Ask suddenly vanishes, the order rests at the limit price instead of buying at exorbitant prices.

### 4.2. Target and Stop Loss Calculation
Upon successful execution, the bot records the `Average Execution Price`. 
It immediately calculates the hard boundaries for the trade:
* **Take Profit (TP):** `Execution Price * 1.10` (10% Gain)
* **Stop Loss (SL):** `Execution Price * 0.85` (-15% Loss)

### 4.3. Exit Mechanics (Tick-by-Tick Monitoring & Timeout)
Once a trade is open, Astra V2 monitors the WebSocket LTP on a **tick-by-tick basis**.
* **Instant TP/SL:** Every incoming tick is compared against the calculated TP and SL boundaries. If the `LTP >= TP` or `LTP <= SL`, a market exit order is fired within milliseconds.
* **10-Minute Timeout:** Options suffer from Theta decay. If the market goes sideways and neither TP nor SL is hit within 600 seconds (10 minutes) of entry, the bot aggressively exits the position at the current market price (`LTP`). This prevents capital from being tied up in stagnant, decaying strikes.
