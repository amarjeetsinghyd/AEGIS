# Astra V2 Implementation Plan (Long-Term Enhancements)

This document tracks the advanced "Part 2" enhancements identified by the GLM 5.2 / Perplexity AI review. These are high-level, sophisticated HFT concepts intended to be implemented in a future iteration (Astra V2.1 or V3) to drastically reduce false-positive signals in complex market regimes.

*Note: The critical Part 1 bugs (Data Loss, L2 Cache, CE/PE Separation, and Rollover False Breakouts) have already been fixed and deployed to the VPS.*

---

## 1. Divergence-Based Confirmation

### 1.1 L1 Chain Delta vs. Underlying Price Delta Divergence
*   **Concept:** Compare the options chain delta (L1 signal) with the actual spot price movement in the same 1-minute window.
*   **Logic:** If chain delta is +15% but the underlying index moved only +0.02%, the aggressive option flow is being absorbed by the index itself (higher-order absorption).
*   **Action:** Downgrade signal confidence or block the trade.

### 1.2 Call Delta vs. Put Delta Divergence (Skew Shift)
*   **Concept:** Instead of collapsing the entire chain into one net delta, track CE delta and PE delta separately.
*   **Logic:** In a genuine breakout, CE buyers are aggressive AND PE sellers are passive (or vice versa). If both CE and PE are being aggressively bought simultaneously, it indicates gamma-driven hedging or speculative volatility buying, not directional conviction.
*   **Action:** Only fire signals when CE and PE deltas are diverging (one positive, one negative).

## 2. Multi-Index Cross-Confirmation

### 2.1 Inter-Index Microstructure Confluence
*   **Concept:** When trading Nifty, subscribe to BankNifty and Sensex spot/options feeds at reduced granularity.
*   **Logic:** A true market momentum breakout will reflect across multiple indices. If Nifty calls are heavily bought but BankNifty puts are being bought, it's a divergence warning.
*   **Action:** Require at least 2 out of 3 indices to show aligned chain delta direction before firing a trade.

## 3. L2 Depth Dynamics (Beyond Static Walls)

### 3.1 Wall Velocity & Persistence Scoring
*   **Concept:** Instead of a binary "wall exists" check, compute a persistence score.
*   **Logic:** Track how long the wall has been resting without cancellation. A wall that persists for 30+ seconds with <5% size fluctuation is likely a genuine institutional order. A wall that appeared 2 seconds ago and rapidly fluctuates is likely spoofing.
*   **Action:** Weight the `ABSORB_CONFLICT` generation based on the wall's persistence score.

### 3.2 Wall Refresh-Cancel Detection (Spoofing Filter)
*   **Concept:** Track the cancel-replace rate on wall orders.
*   **Logic:** If a wall's size oscillates by >20% between consecutive depth updates, it is unstable (likely a spoofer).
*   **Action:** Ignore unstable walls; only stable walls should trigger `ABSORB_CONFLICT`.

### 3.3 Multi-Level Book Pressure Profile
*   **Concept:** Compute a weighted depth profile instead of a simple sum of `bq1..bq5`.
*   **Logic:** Levels closer to the spread have higher execution probability.
*   **Action:** Use a formula like `weighted_bid = bq1*1.0 + bq2*0.7 + bq3*0.5 + bq4*0.3 + bq5*0.2`.

## 4. Microprice & Order Flow Imbalance (OFI)

### 4.1 Microprice Divergence
*   **Concept:** Compute the microprice: `(bp1 * sq1 + sp1 * bq1) / (bq1 + sq1)`.
*   **Logic:** In a genuine breakout, the microprice (fair value weighted by book pressure) leads the mid-price. If microprice diverges from the L1 aggression direction, the book is disagreeing with the flow.
*   **Action:** Block entry if microprice trend contradicts the L1 Chain Delta.

### 4.2 Order Flow Imbalance (OFI) Velocity
*   **Concept:** Track the rate of change of (bid depth added − ask depth added).
*   **Logic:** A positive chain delta with negative OFI velocity means the book is thinning on the bid side while aggression is buying. This often precedes sudden reversals (exhaustion).

## 5. Volatility-Context Adaptation

### 5.1 IV-Rank-Adjusted Thresholds
*   **Concept:** Dynamically adjust the `_CHAIN_DELTA_THR` (currently 10%) based on Implied Volatility (IV).
*   **Logic:** In high-IV regimes, 10% delta is just noise. In low-IV regimes, 10% is massive.
*   **Action:** Scale the threshold: High IV -> 15%, Low IV -> 7%.

### 5.2 Theta-Aware Exit Enhancement
*   **Concept:** Adjust the 10-minute timeout based on Theta decay.
*   **Logic:** On expiry day, Theta decay is extreme, so positions shouldn't be held sideways for 10 minutes. On Monday, 10 minutes is fine.
*   **Action:** Shorten the timeout dynamically as expiry approaches.
