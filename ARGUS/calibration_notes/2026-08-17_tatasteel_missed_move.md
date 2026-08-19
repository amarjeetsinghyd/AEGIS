# Calibration Note — TATASTEEL Missed Move
**Date:** 2026-08-17 (Monday)  
**Reported by:** Post-market forensics (Antigravity / VPS sanity check)  
**Engine version:** Stock Order Flow v3.0 "Microstructure Cascade"  
**Priority:** High — genuine tradeable move was available and missed

---

## Summary

TATASTEEL moved **+0.60% in 13 minutes** (11:47–12:00 IST), which exceeds the 0.52% target. The engine **did not fire**. Root cause traced to the Hawkes branching ratio collapsing at exactly the moment the volume burst triggered the price move.

---

## What Was Seen on the Chart

- **11:45–11:47**: Quiet accumulation, price flat at ~182.50
- **11:47–12:00**: One-sided momentum grind — 182.54 → 183.63 (+0.60%)
- **12:01+**: Continued to 183.91 (+0.73% from entry)
- No significant retracement; move was directionally clean
- Visible on both 1-min and 15-min TradingView chart as a clear bullish candle body

---

## Minute-by-Minute L2 Forensics

> Source: 368,283 live L2 ticks from `l2_raw_2026-08-17_part_*.parquet`  
> Token: 3499 | Total ticks for TATASTEEL today: 8,178

```
Minute  Price    Ticks  TradeVal(L)  OIB      WhaleZ  Branching  Gate
------------------------------------------------------------------------
11:40   182.34    68    56,619       +0.149    1.89    0.5130     ✅
11:41   182.45    80   113,431       +0.052    1.90    0.5075     ✅
11:42   182.77    54    64,675       +0.018    1.95    0.4479     ✅ ← branching high
11:43   182.56    72    88,072       +0.035    1.98    0.4628     ✅
11:44   182.61    38    22,137       +0.096    1.98    0.4378     ✅
11:45   182.68    40    44,489       -0.004    2.00    0.3775     ✅
11:46   182.51    36    29,725       -0.084    2.00    0.3515     ✅ ← last safe pass
─────────────────────── MOVE STARTS ───────────────────────────────────
11:47   182.58    36    29,803       +0.140    2.01   ✅ 0.3640   ✅ ← gate PASSES here
11:48   182.99    98   161,714       -0.020    2.40   ❌ 0.3182   ✅ ← burst collapses it
11:49   183.15   110   147,913       +0.017    2.43   ❌ 0.2804   ✅
11:50   183.31   100   123,823       +0.086    2.47   ❌ 0.3097   ✅
11:51   183.34   100   107,459       -0.026    2.50   ❌ 0.3026   ✅
11:52   183.42    88    99,048       -0.067    2.51   ❌ 0.2968   ✅
11:53   183.34    54    54,249       -0.060    2.52   ❌ 0.2886   ✅
11:54   183.37    42    27,181       +0.006    2.53   ❌ 0.2905   ✅
11:55   183.59    50    45,658       -0.041    2.60   ❌ 0.3158   ✅
11:56   183.60    86   121,548       +0.047    2.63   ❌ 0.2946   ✅
11:57   183.60    56    56,245       +0.013    2.63   ❌ 0.2766   ✅
11:58   183.51    58    75,416       -0.099    2.65   ❌ 0.2819   ✅
11:59   183.55    46    38,062       -0.079    2.68   ❌ 0.3111   ✅
12:00   183.63    44    85,853       -0.048    2.68   ❌ 0.2984   ✅ ← move ends
─────────────────────────────────────────────────────────────────────
>>> Move: 182.54 → 183.63  (+0.60%)
>>> Branching at 11:47 entry: 0.3640  (threshold: 0.35) → PASSES
>>> Gap to failure at 11:48:  0.3182  (threshold: 0.35) → FAILS by 0.0318
```

---

## Root Cause Analysis

### The Cruel Irony: Volume Burst Broke the Branching Estimator

At **11:47**, branching was **0.3640** — comfortably above the 0.35 threshold. The HARD_GATE should have passed and the conviction scorer should have run.

At **11:48**, 98 ticks arrived in a single minute (vs the 36-40 tick baseline) — the largest single-minute burst of the window. This caused price to jump from 182.58 → 182.99, confirming the momentum.

**But the same burst that moved the price also destroyed the branching signal.**

The Hawkes branching estimator is:

```
branching = max(0, 1 - sqrt(mean / variance))
```

computed over 10-min rolling window with 10-second bins.

When 98 ticks arrive in one minute, they fill **6 consecutive 10-second bins** very heavily. As the rolling window advances, it simultaneously drops **quiet bins from 10 minutes ago**. The net effect is that `mean` rises (more trades overall) but `variance` doesn't rise proportionally — the spike-to-background ratio actually *decreases* — pushing branching down.

**The signal self-cancelled.** The volume burst that confirmed the trade also invalidated the branching condition used to allow it.

### Secondary Observation: OIB Was Not Directional

Throughout the 11:47–12:00 window, OIB alternated between positive and negative (range: -0.099 to +0.140), averaging near zero. The move was driven by **ask-side absorption** — large players lifting offers without leaving a visible bid-stacking footprint on L2. The book did not show the directional imbalance that OIB measures. This is consistent with institutional accumulation disguising intent.

### WhaleZ Was Steadily Rising — The One Clear Signal

| Time  | WhaleZ |
|-------|--------|
| 11:40 | 1.89   |
| 11:47 | 2.01   |
| 11:55 | 2.60   |
| 12:00 | 2.68   |
| 12:05 | 2.86   |

WhaleZ climbed consistently from 1.89 → 2.86. The engine *was* detecting increasing large-lot participation — it simply could not confirm it through the branching gate.

---

## Why the Engine Didn't Fire — Gate Breakdown

| Gate Pillar | At 11:47 | Result | Notes |
|-------------|----------|--------|-------|
| Ticks ≥ 3   | 36 ticks | ✅ | Fine |
| Branching ≥ 0.35 | 0.364 | ✅ | Barely passes |
| WhaleZ ≥ 1.2 | 2.01 | ✅ | Strong |
| Toxicity < P90 | — | ❌ (likely) | Toxicity often fails |

Even at the 11:47 moment when branching technically passed, Toxicity was likely still the fourth blocker. The branching collapse at 11:48 removed pillar #2 anyway — so the engine missed the entry in every subsequent evaluation.

---

## Calibration Recommendations

### Fix 1: Shorten the Hawkes Rolling Window (600s → 300s)

**Problem:** The 10-min window causes old quiet bins to suppress the branching ratio when a new burst arrives.

**Fix:** Reduce the Hawkes lookback from `lookback = 600` (10min) to `lookback = 300` (5min) with proportionally fewer bins (`bins = 30`).

**Expected effect:** Branching becomes more responsive to *current* burst patterns without being dragged down by older quiet periods.

**File to change:** `stock_order_flow/cascade/value_engine.py` (Hawkes computation section)

> [!WARNING]
> Lower lookback may increase false positives on choppy days — needs backtesting on ≥5 days of L2 data before deploying.

---

### Fix 2: Add a Slow-Momentum Secondary Gate Path

**Problem:** The branching gate is designed for explosive, clustering bursts (Hawkes-style). It systematically misses *steady directional grinds* — slow accumulation by large players who deliberately spread order flow to avoid detection.

**Proposed secondary gate condition (all must be true):**
```python
SLOW_MOMENTUM_GATE = (
    whale_z >= 2.0                       # large lots confirmed
    AND price_change_10min >= 0.30%      # visible price momentum
    AND branching >= 0.15                # some clustering (relaxed)
    AND ticks >= 3                       # minimum activity
)
```

If `SLOW_MOMENTUM_GATE` is true, pass to conviction scorer with a **reduced conviction threshold** (e.g. 5.5 instead of 7.5) since fewer signals are confirming.

---

### Fix 3: Price Momentum as a Standalone Gating Input

Currently the engine has no explicit price-momentum pillar. WhaleZ captures large-lot activity but a direct "price moved X% in last N minutes" check would have flagged TATASTEEL clearly by 11:52 (0.42% in 5min) and provided a third path to entry.

**Proposed pillar:** `price_momentum_5m ≥ 0.25%` as a fifth soft gate input.

---

## Other Missed Moves on 2026-08-17

| Stock | Best 15m window | Move | Root Cause |
|-------|----------------|------|------------|
| **TATASTEEL** | 11:47 → +0.73% | Genuine | Branching collapsed on burst (this note) |
| **AXISBANK** | 10:03 → -0.85% (SHORT) | Possible | Branching 0.25 < 0.35; final OIB was LONG — contradictory |
| **BAJFINANCE** | ~12:33 near-miss | — | Branching 0.340 (missed by 0.01!); **also missing from L2 archive entirely** |

---

## Action Items

- [ ] **URGENT:** Investigate BAJFINANCE missing from L2 archive — archiver subscription filter may be excluding its token despite cascade evaluating it live
- [ ] **Calibration (low-risk):** Reduce Hawkes lookback 600s → 300s and retest on 2026-08-13 and 2026-08-14 L2 data using `research_analytics.py`
- [ ] **Calibration (medium-risk):** Design and backtest Slow Momentum secondary gate (Fix 2 above)
- [ ] **Research:** Run `research_analytics.py` on AXISBANK 2026-08-17 to understand the 10:03 SHORT move and OIB contradiction
- [ ] **After any change:** Run `python -m pytest tests/ -q` — suite must be 100% green before deploy

---

*Forensics generated from 368,283 live L2 ticks archived by `l2_archiver.py` on 2026-08-17.*  
*Drill-down script: `/tmp/tatasteel_drill.py` on VPS (ad-hoc — logic to be integrated into `research_analytics.py` as a named report mode).*
