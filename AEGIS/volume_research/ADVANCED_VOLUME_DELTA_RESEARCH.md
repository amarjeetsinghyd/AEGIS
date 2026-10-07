# AEGIS — Advanced Volume vs Price & Order Flow Delta Research

> **Repository Location:** `AEGIS/volume_research/ADVANCED_VOLUME_DELTA_RESEARCH.md`  
> **Status:** ACTIVE RESEARCH & HYPOTHESIS SPECIFICATION  
> **Target Engine:** AEGIS (Autonomous Execution & Gamma Imbalance System v4.1)  
> **Influences & References:** TradingView Up/Down Volume & Volume Delta (2021–2023), Marketcalls (Rajendran R), Trading Chanakya, Theta Gainers, Karthik Marar (VSA), Equity2Commodity, Richard D. Wyckoff, Anna Coulling (Volume Price Analysis).

---

## 1. Executive Summary & Problem Statement

Currently in AEGIS:
- Tick volume is decomposed via **Hybrid Touch-First + Lee-Ready Tick Rule** on WebSocket options ticks into 4 primitives:
  $$\Delta V \longrightarrow \{\text{CallBuy}, \text{CallSell}, \text{PutBuy}, \text{PutSell}\}$$
- The active Order Flow Intensity (OFI) is currently defined as:
  $$\text{OFI}_{\text{Simple}} = \text{CallBuy} - \text{PutBuy}$$
- While effective at capturing direct market-order buyers, this creates a **structural blind spot**:
  - If heavy **Call Selling / Liquidation** occurs during a rally, $\text{OFI}_{\text{Simple}}$ fails to register the dump.
  - If institutions are aggressively **Selling Puts** (bullish floor support), $\text{OFI}_{\text{Simple}}$ ignores the bullish put-writing support.

This research formulates an **Advanced Asymmetric Directional Delta Model** that unifies all four components into true composite Bullish and Bearish pressure, enabling high-precision **Delta Divergence, Delta Imbalance Ratios, and Stopping Volume Absorption**.

---

## 2. Core Mathematical Framework

### 2.1 The Synthetic Net Directional Delta
In institutional options microstructure, true directional momentum is synthetic across both calls and puts:

$$\mathbf{Bullish\ Volume\ (Aggressive\ Long\ Pressure)} = \text{CallBuy} + \text{PutSell}$$
$$\mathbf{Bearish\ Volume\ (Aggressive\ Short\ Pressure)} = \text{PutBuy} + \text{CallSell}$$

Where:
- $\text{CallBuy}$: Aggressive buyer lifting the Call ask (Direct Long).
- $\text{PutSell}$: Aggressive seller hitting the Put bid (Direct Short Put / Put Unwinding).
- $\text{PutBuy}$: Aggressive buyer lifting the Put ask (Direct Long Put / Hedging).
- $\text{CallSell}$: Aggressive seller hitting the Call bid (Direct Short Call / Call Dumping).

The **Synthetic Net Delta ($\Delta_{\text{synth}}$)** is defined as:
$$\Delta_{\text{synth}} = (\text{CallBuy} + \text{PutSell}) - (\text{PutBuy} + \text{CallSell})$$

Notice algebraic equivalence:
$$\Delta_{\text{synth}} = (\text{CallBuy} - \text{PutBuy}) + (\text{PutSell} - \text{CallSell})$$
$$\Delta_{\text{synth}} = \text{Buyer Aggression Delta} + \text{Seller Skew Delta}$$

---

### 2.2 Advanced Analysis Rules (From TradingView & Indian Quant Community)

#### Rule 1: True Delta Imbalance Ratio ($\ge 2.5:1$)
Instead of simple volume multiples, institutional footprints are confirmed when directional buying swamps opposing aggression by at least 2.5x:
$$\text{Bullish Imbalance Ratio} = \frac{\text{CallBuy} + \text{PutSell}}{\max(\text{PutBuy} + \text{CallSell}, 1.0)} \ge 2.50$$
$$\text{Bearish Imbalance Ratio} = \frac{\text{PutBuy} + \text{CallSell}}{\max(\text{CallBuy} + \text{PutSell}, 1.0)} \ge 2.50$$
*Significance:* Filters out low-conviction churn ($1:1$ chop) and admits only one-sided institutional flows.

#### Rule 2: Delta Divergence (Trap & Exhaustion Veto)
Inspired by Cumulative Volume Delta (CVD) Divergence:
- **Bull Trap Divergence**: Spot makes a Higher High ($\text{Spot}_t > \text{Spot}_{t-1}$), but Synthetic Net Delta makes a lower peak ($\Delta_{\text{synth}, t} < \Delta_{\text{synth}, t-1}$ or turns negative).
  $\implies$ **Immediate VETO on Call Breakouts (Pillars 3 & 4)**.
- **Bear Trap Divergence**: Spot makes a Lower Low ($\text{Spot}_t < \text{Spot}_{t-1}$), but Bearish Volume collapses or turns positive.
  $\implies$ **Immediate VETO on Put Breakdowns**.

#### Rule 3: Asymmetric Stopping Volume & Absorption (Wyckoff Pillars 1 & 2)
- **At 20 EMA (Pillar 1) or Key Support (Pillar 2)**:
  - When price pulls back to 20 EMA, if Bearish Volume $(\text{PutBuy} + \text{CallSell})$ surges (Heavy Effort by Bears), BUT the candle produces **No Breakdown** (Result = Zero) and leaves a **Lower Wick $\ge 35\%$**:
    $$\text{Absorption Index} = \frac{(\text{PutBuy} + \text{CallSell}) / \text{BaselineVolume}}{\text{Spread} / \text{ATR}} \ge 1.50$$
  - $\implies$ **Grade-A Institutional Spring / Stopping Volume Confirmation!**
  - All bearish market orders were absorbed by passive institutional limit buy orders.

#### Rule 4: Volume Climax + Dry-Up Test ($VDR < 0.60$)
- A high-effort absorption candle must be followed by a **Dry-Up Test candle** where volume drops below $60\%$ of 20-bar baseline ($VDR < 0.60$).
- Proves floating supply/sellers are exhausted, leaving an open runway for trend continuation.

---

## 3. Empirical Test Plan & Hypotheses

| # | Hypothesis | Verification Method |
|---|---|---|
| **H1** | $\text{CallBuy}$ and $\text{PutSell}$ exhibit positive correlation during index expansion. | Pearson & Spearman correlation on 1-min aggregated candles. |
| **H2** | $\text{PutBuy}$ and $\text{CallSell}$ exhibit strong positive correlation during index sell-offs. | Pearson & Spearman correlation on 1-min candles during bearish moves. |
| **H3** | Synthetic Net Delta $[(\text{CallBuy} + \text{PutSell}) - (\text{PutBuy} + \text{CallSell})]$ has higher predictive power ($R^2$ / correlation) with forward 1m & 3m Spot Return ($\Delta \text{Spot}$) than raw OFI $(\text{CallBuy} - \text{PutBuy})$. | Linear regression & information coefficient (IC) comparison against $\Delta \text{Spot}_{t+1}$. |

---

## 4. Integration Roadmap for AEGIS

1. **Phase 1 (Audit & Empirical Proof)**:
   - Run correlation script on yesterday's (29-Sep-2026, NIFTY) and today's (30-Sep-2026, SENSEX) logs.
   - Quantify $R^2$ and correlation improvement of Synthetic Delta vs Simple OFI.
2. **Phase 2 (Engine Enhancement in `PriceActionEngine`)**:
   - Compute `bar.synthetic_buy_vol = cb + ps` and `bar.synthetic_sell_vol = pb + cs`.
   - Compute `bar.synthetic_delta = (cb + ps) - (pb + cs)`.
3. **Phase 3 (Wyckoff Delta Harmony Upgrade in `WyckoffVolumeEngine`)**:
   - Upgrade Effort vs Result absorption formula using Synthetic Bearish Volume at 20 EMA.
   - Upgrade Delta Divergence veto across Pillars 3 & 4.
4. **Phase 4 (Shadow Telemetry Validation)**:
   - Run in shadow telemetry for 2 sessions to verify false-positive suppression without breaking green test suite.
