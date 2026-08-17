# Astra V3: Advanced Microstructure Enhancement Blueprint

This document serves as the comprehensive implementation guide for upgrading Astra V2 to V3. These enhancements transform the system from a reactive L1/L2 synergy model into a predictive, context-aware microstructure engine. 

---

## Enhancement 1: Divergence-Based Confirmation

### 1.1 Chain Delta vs. Spot Delta Divergence
**Concept:** Aggressive option buying (L1) should translate to underlying spot movement. If options flow is heavily bullish but the underlying spot price is stagnant or dropping, the flow is likely speculative hedging or being absorbed at the index level.

**Mathematical Formula:**
*   `Spot Momentum (P_mom) = (Spot_Close_1m - Spot_Open_1m) / Spot_Open_1m`
*   `Scaled Flow (F) = Sign(Chain_Delta) * |Chain_Delta|`
*   `Scaled Spot (S) = Sign(P_mom) * |P_mom| * K` *(where K is a scaling factor, e.g., 100, to normalize index % change to option delta %)*
*   `Flow-Price Divergence (FPD) = F - S`
*   *If `FPD > Threshold` (e.g., 0.15):* Bullish absorption at the index level. Downgrade CALL signal.

**Architectural Placement:** `FootprintCompiler` (Spot Tracking) -> `ChainFootprintEvaluator` (Calculation)

**Implementation Logic:**
In `FootprintCompiler`, add a `spot_1m_open` and `spot_1m_close` to the compiled output.
```python
# In FootprintCompiler.process_tick (Spot Tick branch):
if token == index_token and price > 0:
    if self.spot_1m_open == 0.0:
        self.spot_1m_open = price
    self.spot_1m_current = price

# In get_compiled_footprints:
compiled["spot_open"] = self.spot_1m_open
compiled["spot_close"] = self.spot_1m_current
self.spot_1m_open = 0.0 # Reset for next minute
```

```python
# In ChainFootprintEvaluator.evaluate_1m_boundary
spot_open = compiled_footprints.get("spot_open", 0.0)
spot_close = compiled_footprints.get("spot_close", 0.0)
if spot_open > 0:
    p_mom = (spot_close - spot_open) / spot_open
    scaled_spot = np.sign(p_mom) * abs(p_mom) * 100  # K=100
    scaled_flow = np.sign(delta_pct) * abs(delta_pct)
    fpd = scaled_flow - scaled_spot
    
    # If flow is heavily positive but spot is flat/negative:
    if delta_pct > _CHAIN_DELTA_THR and fpd > 0.15:
        return "NEUTRAL", f"Index Absorption: Chain Δ +{delta_pct*100:.1f}% but Spot Δ {p_mom*100:.2f}%", None
```

### 1.2 CE vs. PE Skew Divergence
**Concept:** Genuine directional momentum shows aggression on the side being bought *and* passivity on the opposite side. If both CEs and PEs are being aggressively bought, it's a gamma-driven straddle play, not directional conviction.

**Mathematical Formula:**
*   `CE_Delta = (CE_Buy_Vol - CE_Sell_Vol) / Total_CE_Vol`
*   `PE_Delta = (PE_Buy_Vol - PE_Sell_Vol) / Total_PE_Vol`
*   `Skew Divergence = CE_Delta - PE_Delta` *(Note: If market is going UP, CE_Delta is positive, PE_Delta should be negative. A healthy bullish breakout has a large positive Skew Divergence).*
*   *If `Chain_Delta > 10%` but `Skew_Divergence < 0`:* False signal (both sides being bought).

**Architectural Placement:** `ChainFootprintEvaluator`

**Implementation Logic:**
```python
# In ChainFootprintEvaluator
ce_buy, ce_sell, pe_buy, pe_sell = 0, 0, 0, 0
for token, data in compiled_footprints.items():
    if data["option_type"] == "CE":
        ce_buy += data["total_buy_vol"]
        ce_sell += data["total_sell_vol"]
    else:
        pe_buy += data["total_buy_vol"]
        pe_sell += data["total_sell_vol"]

ce_delta = (ce_buy - ce_sell) / max(ce_buy + ce_sell, 1)
pe_delta = (pe_buy - pe_sell) / max(pe_buy + pe_sell, 1)
skew_divergence = ce_delta - pe_delta

# Filter: If buying calls, but puts are also being bought (pe_delta > 0)
if delta_pct > _CHAIN_DELTA_THR and pe_delta > 0.05:
    return "NEUTRAL", "Straddle Flow Detected: Both CE and PE being accumulated", None
```

---

## Enhancement 2: L2 Depth Dynamics (Spoofing Filter)

**Concept:** A static L2 snapshot cannot distinguish between a massive institutional limit order and a flash-spoof order pulled milliseconds later. We must track the *persistence* and *velocity* of the wall.

**Mathematical Formula:**
*   `Wall Persistence Score (WPS) = Sum(1 for t in T if wall_size_t > 0.8 * wall_size_t-1) / len(T)`
*   `Pull Event = wall_size drops by > 80% without corresponding aggressive trade volume`
*   *If `WPS < 0.50` OR `Pull_Event == True`:* Flag wall as Spoof. Ignore for T+1 conflict logic.

**Architectural Placement:** `FootprintCompiler` (Tracking) -> `ChainFootprintEvaluator` (Filtering)

**Implementation Logic:**
In `FootprintCompiler`, maintain a rolling history of L2 states per token.
```python
# In FootprintCompiler.__init__:
self.l2_history = defaultdict(lambda: collections.deque(maxlen=10)) # 10 L2 updates

# In FootprintCompiler.process_tick (inside depth update block):
# Track history only for top-of-book (bq1/sq1) for spoofing
if "bq1" in message or "sq1" in message:
    self.l2_history[token].append({
        "ts": time.time(),
        "bq1": depth.get("bq1", 0),
        "sq1": depth.get("sq1", 0)
    })

# In get_compiled_footprints:
# Calculate persistence for the battleground ask wall
history = list(self.l2_history[token])
spoof_flag = False
if len(history) > 3:
    # Check if the wall fluctuates wildly
    sizes = [h["sq1"] for h in history if h["sq1"] > 0]
    if sizes:
        avg_size = sum(sizes) / len(sizes)
        # If current size is < 20% of average, it was likely pulled
        if depth.get("sq1", 0) < avg_size * 0.2:
            spoof_flag = True

compiled[token]["ask_wall_spoof"] = spoof_flag
```

```python
# In ChainFootprintEvaluator T=0 evaluation:
asks = data.get("last_l2_ask", 0)
bids = data.get("last_l2_bid", 0)
is_spoof = data.get("ask_wall_spoof", False)

if asks > bids * _WALL_MULT and asks > 0 and not is_spoof:
    # Trigger ABSORB_CONFLICT
```

---

## Enhancement 3: Microprice & Order Flow Imbalance (OFI)

**Concept:** Microprice calculates the "true" fair value weighted by book size. OFI tracks the net liquidity being added or pulled. A genuine breakout requires the book to *support* the aggressive flow.

**Mathematical Formula:**
*   `Microprice (M_p) = (bp1 * sq1 + sp1 * bq1) / (bq1 + sq1)`
*   `Midprice (M) = (bp1 + sp1) / 2`
*   `Microprice Divergence = (M_p - M) / M`
*   `OFI (1m) = Sum(bq1_t - bq1_t-1) - Sum(sq1_t - sq1_t-1)`

**Architectural Placement:** `FootprintCompiler` (Continuous Calculation)

**Implementation Logic:**
```python
# In FootprintCompiler.process_tick (inside depth update block):
bp1 = depth.get("bp1", 0)
sp1 = depth.get("sp1", 0)
bq1 = depth.get("bq1", 0)
sq1 = depth.get("sq1", 0)

# Calculate Microprice
if bp1 > 0 and sp1 > 0 and (bq1 + sq1) > 0:
    depth["microprice"] = (bp1 * sq1 + sp1 * bq1) / (bq1 + sq1)
    depth["midprice"] = (bp1 + sp1) / 2

# Calculate OFI Delta (Liquidity added vs removed)
# Needs previous bq1/sq1 stored in depth dict
ofi_delta = 0
if "prev_bq1" in depth:
    ofi_delta += (bq1 - depth["prev_bq1"])
    ofi_delta -= (sq1 - depth["prev_sq1"])
depth["prev_bq1"] = bq1
depth["prev_sq1"] = sq1

# Accumulate OFI for the minute
if "ofi_1m" not in depth: depth["ofi_1m"] = 0
depth["ofi_1m"] += ofi_delta
```

```python
# In get_compiled_footprints:
compiled[token]["ofi_1m"] = depth.get("ofi_1m", 0)
compiled[token]["microprice"] = depth.get("microprice", 0)
compiled[token]["midprice"] = depth.get("midprice", 0)

# In Evaluator:
# If late breakout call, but OFI is negative (liquidity being pulled)
if sig == "CALL" and battle_data.get("ofi_1m", 0) < 0:
    return "NEUTRAL", "Breakout invalidated: Liquidity pulling (OFI < 0)", None
```

---

## Enhancement 4: Volatility-Context Adaptation

**Concept:** Static thresholds (10% Delta, 1.5x Wall, 10-min timeout) fail across varying market regimes. High IV requires wider thresholds; expiry day requires aggressive theta-based time stops.

**Mathematical Formula:**
*   `IV Rank (IVR) = (Current_IV - Low_52w) / (High_52w - Low_52w)`
*   `Dynamic Delta Thr = 0.10 * (1 + (IVR - 0.5) * 0.5)` *(Range: 7.5% to 12.5%)*
*   `Dynamic Wall Mult = 1.5 + (IVR * 0.5)` *(Range: 1.5x to 2.0x)*
*   `Theta Velocity = 1 / Days_To_Expiry`
*   `Dynamic Time Stop (sec) = 600 * (1 - Theta_Velocity * 0.5)` *(If DTE=1, Time Stop = 300s)*

**Architectural Placement:** `DataFetcher` (Fetch IV) -> `ChainFootprintEvaluator` (Adjust Thr) -> `AstraExecutionManager` (Adjust Time Stop)

**Implementation Logic:**
```python
# In DataFetcher (Fetch once at pre-market):
def fetch_iv_rank(self):
    # Call Shoonya/External API for India VIX or ATM IV
    # Return IVR between 0.0 and 1.0
    return 0.6 # Example

# In ChainFootprintEvaluator.__init__:
self.ivr = 0.5 # Default

# In ChainFootprintEvaluator.evaluate_1m_boundary:
dynamic_delta_thr = _CHAIN_DELTA_THR * (1 + (self.ivr - 0.5) * 0.5)
dynamic_wall_mult = _WALL_MULT + (self.ivr * 0.5)

if delta_pct > dynamic_delta_thr:
    # ... use dynamic_wall_mult for wall checks
```

```python
# In AstraExecutionManager.execute_entry:
dte = self._get_dte(opt_row) # Calculate days to expiry
theta_velocity = 1 / max(dte, 1)
dynamic_max_hold = MAX_HOLD_SEC * (1 - theta_velocity * 0.5)

# Pass dynamic_max_hold to manage_open_trade or store in TradeContext
self.trade_context.max_hold_sec = dynamic_max_hold

# In manage_open_trade:
if hold_sec >= self.trade_context.max_hold_sec:
    # Trigger time stop
```
