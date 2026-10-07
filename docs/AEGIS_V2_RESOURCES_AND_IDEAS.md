# 🔬 AEGIS Version 2.0 (Future Architecture) — Resources, Evidence & Quantitative Ideas

> **Current Production Architecture Notice**:
> **AEGIS Version 1.0 is LOCKED in Production.** All live trading on NIFTY and SENSEX follows the v1.0 canonical specification (4 Price Action Pillars, Absolute Pain Zone Band, Wyckoff Volume & OFI Delta Gate, Strict +4.0% Maker TP, Universal Entry Candle Low/High Invalidation).
> 
> This document serves as a **dedicated research repository and idea incubator** for future **Version 2.0**. No unverified logic from this document is to be introduced into the production hot-path without comprehensive backtesting and live paper calibration.

---

## 📌 Research Motivation & Objective

As observed during live forward testing on BSE SENSEX and NSE NIFTY, institutional market makers heavily anchor their multi-timeframe liquidity and risk parameters around major institutional moving averages—most notably the **200 Exponential Moving Average (200 EMA)** on intraday timeframes (1-minute and 5-minute charts).

While Version 1.0 relies primarily on the 20 EMA (Pillar 1) and Bollinger/Keltner bands (Pillar 3), real market price action frequently exhibits pristine, high-conviction structural rejections off the **200 EMA**, creating explosive trend continuations and liquidity sweeps.

---

## 📊 Evidence Docket #1: Multi-Touch 200 EMA Rejection Architecture (BSE SENSEX 1m)

* **Date of Live Observation**: 07 October 2026 (Wednesday — SENSEX 1 DTE)
* **Instrument**: BSE SENSEX Spot (1-Minute Bar Chart)
* **Indicators Active**:
  * **200 EMA (close)**: Blue Line sitting between ~72,832.0 and ~72,853.4
  * **9 EMA (close)**: Fast line oscillating between 72,629.0 and 72,975.0
* **Chart Artifact Reference**: `media_1791358836409.png`

```
  SENSEX 1m Price
       ▲
73,025 │                                      ▲ Peak
       │                                     / \
72,950 │              ▲                     /   \
       │             / \        ▲          /     \
72,900 │            /   \      / \        /       \
       │           /     \    /   \      /         \
72,850 ┼──(Touch 1)───────(Touch 2)─────(Touch 3)──(Touch 4)──(Touch 5 - Trade #21)───► 200 EMA Line (~72,835 - 72,853)
       │     10:45        11:30         11:45      12:15       12:20 (Reclaim & Bounce)
72,800 │
```

### Forensic Analysis of the 5 Rejections

1. **Touch 1 (~10:45 IST — The Opening Support Test)**:
   * Morning price action dipped directly into the rising 200 EMA line near 72,780–72,800.
   * *Reaction*: Massive institutional buyer absorption occurred. Spot erupted vertically from 72,800 to 73,000+ (+200 points in 15 minutes).

2. **Touch 2 (~11:30 IST — Long Lower Wick Spring)**:
   * Price retraced sharply from the morning highs. The 11:30 bar flushed below the 200 EMA, trapping retail breakout shorts.
   * *Reaction*: Immediate spring reclaim with a long lower rejection wick, bouncing +100 points back to 72,950.

3. **Touch 3 (~11:45 IST — Midday Retest & Continuation)**:
   * Shallow consolidation pullback retesting the 200 EMA dynamic floor.
   * *Reaction*: The 200 EMA held cleanly without candle closes below it; market pushed upwards toward 73,025.

4. **Touch 4 (~12:15 IST — Deep Liquidity Sweep)**:
   * Sharp institutional flush hunting stops into 12:15. Spot tapped the 200 EMA at 72,840.
   * *Reaction*: Immediate bid reaction off the 200 EMA, producing another strong rebound wick.

5. **Touch 5 (~12:20 IST — Trade #21 Entry Point)**:
   * 12:20 bar performed a classic Pillar 2 Liquidity Sweep Reclaim directly at the 200 EMA floor (72,868 low).
   * Trade #21 entered on CALL (`SENSEX26O0873800CE` @ ₹28.85).
   * *Post-Touch Behavior*: The 200 EMA floor held firmly once again, and the 12:21 candle expanded upward green.

---

## 💡 Candidate Ideas & Hypotheses for AEGIS Version 2.0

### Idea 1: The 200 EMA Institutional Confluence Booster
* **Hypothesis**: Any Pillar setup (P1 20 EMA, P2 Sweep Reclaim, P3 Squeeze Breakout, or P4 Prominence Hub) that occurs in close proximity to the 200 EMA ($\pm k \times \text{ATR}_{1m}$ or within $\pm 8\text{ bps}$) possesses a vastly higher statistical win rate due to macro institutional backing.
* **Proposed Mechanism**:
  * Add a `200_ema_confluence_score` (+5.0 to +10.0 points) to the Composite Conviction Engine when setup proximity aligns with the 200 EMA.

### Idea 2: Dynamic Multi-Touch Rejection Counter
* **Hypothesis**: The 1st, 2nd, and 3rd touches of the 200 EMA during a session carry the highest momentum and lowest probability of failure. Subsequent touches (4th, 5th) indicate weakening of the level.
* **Proposed Mechanism**:
  * Track `ema_200_touch_count` per trading session.
  * Weight entries higher on Touches 1–3, while requiring stricter Wyckoff volume confirmation on Touches 4+.

### Idea 3: Multi-Timeframe EMA Hierarchy (9 / 20 / 50 / 200)
* **Current V1.0 Hierarchy**:
  * 20 EMA: Primary trend bounce and pullback reference.
  * 50 EMA: Secondary trend regime and structural direction.
* **Future V2.0 Hierarchy**:
  * **9 EMA**: Micro-momentum speed guide (never used as an arbitrary exit choke).
  * **20 EMA**: Primary swing momentum anchor.
  * **50 EMA**: Intermediate trend filter.
  * **200 EMA**: Macro intraday battleground & institutional support/resistance foundation.

### Idea 4: 200 EMA Liquidity Vacuum Breakout
* When price consolidates between the 20 EMA and 200 EMA (compression channel), an expansion breakout away from the 200 EMA often creates an institutional vacuum, providing rapid delta expansion for gamma scalping.

---

## 🔬 Research Next Steps & Offline Backtesting Plan

1. **Historical Data Extraction**:
   * Collect 6 months of 1-minute tick data for NIFTY and SENSEX.
   * Compute rolling 200 EMA and measure forward return ($\Delta P_{1m}, \Delta P_{3m}, \Delta P_{5m}$) upon wick touches.
2. **MAE / MFE Quantification**:
   * Quantify whether entries near the 200 EMA show lower Maximum Adverse Excursion (MAE) compared to generic midpoint entries.
3. **Drafting V2 Architecture Document**:
   * As additional evidence is gathered, maintain this document as the single source of truth for all V2 quantitative proposals.
