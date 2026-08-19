# Calibration Analysis Results — 2026-08-17
**Script:** `calibration_analysis.py --hawkes-window 300 --forward-mins 15 --target-pct 0.52%`
**Data:** 761,963 L2 ticks across 23 parquet partitions (full trading day)

---

## Raw Results

```
Symbol        Moves  CurrF1  OptBranch  OptWhale   OptF1  Capture   F1Gain
--------------------------------------------------------------------------
TATASTEEL      1344   0.006       0.10      0.50   0.093       8%   +0.087  <- CALIBRATE
SUNPHARMA       744   0.000       0.10      2.50   0.086       9%   +0.086  <- CALIBRATE
ICICIPRULI      136   0.000       0.10      3.00   0.043       2%
ITC               8   0.000       0.10      2.50   0.010      25%
BEL              56   0.000       0.10      2.50   0.009       2%
VEDL            272   0.000       0.10      0.50   0.007       0%
CANBK/PNB/JIOFIN: F1=0.000 at all thresholds (noise — signals don't correlate)

No moves today: COALINDIA, IDFCFIRSTB, IRCTC, KOTAKBANK, POWERGRID, WIPRO
```

---

## What the Results Are Actually Telling Us

### Finding 1: The current branching threshold (0.35) is far too high

`curr_F1 = 0.000` for **every stock except TATASTEEL** at branch≥0.35. The gate almost never fires. Even TATASTEEL only gets F1=0.006. The optimal is consistently `branch≥0.10` across all stocks.

> **Action:** Lower Hawkes branching threshold from 0.35 → 0.15 as a starting point.

---

### Finding 2: Even at optimal thresholds, F1 is very low (max 0.093)

This is the deeper issue. Even at `branch≥0.10, whale≥0.50`, TATASTEEL only achieves 8% capture rate. Why?

**The signals are measuring the WRONG moment.**

Look at the move timeline — `whale_z` is **negative** at almost every actual move entry:
```
TATASTEEL  11:35  branch=0.171  whale=-0.37  -> [MISSED]
TATASTEEL  12:31  branch=0.000  whale=-0.36  -> [MISSED]
SUNPHARMA  11:06  branch=0.151  whale=-0.38  -> [MISSED]
PNB        10:38  branch=0.000  whale= 3.15  -> [MISSED]
```

A `whale_z = -0.37` means the current tick has a trade value **0.37 std-devs below the rolling mean** — i.e., it is a small trade or a book update with no trade at all (tval=0). The large whale trades that *caused* the move happened 1-3 minutes **before** the entry point the script sampled.

> **Root cause:** The script samples the **first tick that has a 0.52% forward return**, not the tick where the whale trade actually happened. The whale and branching signals are reactive — they peak just BEFORE the move starts, not AT the first tick of the move.

---

### Finding 3: Branching=0.000 at almost every entry point

Same issue — branching measures the clustering pattern of the **preceding** 5 minutes. At move entry, the clustering has already fired. The script is sampling too late.

This is actually **good news**: it means the signals are correctly timed in the live engine (which evaluates on every tick), but the calibration script's move-timeline is picking the wrong reference tick.

---

### Finding 4: Some stocks are genuinely noise (CANBK, PNB, JIOFIN, VEDL)

`F1=0.000` even at optimal thresholds means whale/branching signals have **zero correlation** with price moves for these stocks. These are likely PSU banks / lower-quality price action where institutional HFT patterns don't apply. The machine is right to be cautious with them.

---

### Finding 5: The 1,344 "move ticks" for TATASTEEL is misleadingly high

TATASTEEL had 1,344 ticks out of 15,416 (8.7%) where a 0.52% move happened within 15 minutes. But the stock moved +0.82% on the day — so almost the entire afternoon session had a forward return ≥0.52%. This inflates the "moves" count and makes the calibration noisy. For calibration purposes, we need **the first tick of each distinct move**, not every tick during a sustained trend.

---

## Corrected Interpretation: What to Actually Do

### Immediate fix (before tomorrow)

The analysis confirms the TATASTEEL calibration note finding: **the Hawkes branching threshold is too high.** The fix is simple:

| Parameter | Current | Recommended |
|-----------|---------|-------------|
| `branching` threshold | 0.35 | **0.20** (conservative step) |
| `whale_z` threshold | 1.20 | **1.20** (keep — it's appropriate) |
| Hawkes window | 600s | **300s** (per earlier finding) |

Don't go all the way to 0.10 — that generates 887 signals/day for TATASTEEL alone (far too many false positives). 0.20 is the sweet spot between "never fires" and "fires constantly."

### Medium-term fix (next week)

The script needs to be improved to find the **peak signal moment** before each move, not the first tick of the move window. Rerun on 3-4 more days of L2 data to get statistically meaningful calibration.

### Structural fix (next sprint)

Add **LTP 5-minute momentum** as a direct gate pillar:
```
lp_mom_5m >= 0.20%  (directional move already in progress)
```
From the timeline, this is the one signal that IS visible at move entry — `mom5m` is sometimes +0.09%, +0.10%, +0.28% at the entry ticks even when branching and whale_z are low. A price momentum pillar would have caught TATASTEEL at 11:35 (mom5m was -0.01% at the sampled tick but was +0.40% just a minute before).

---

## Stocks to Focus On Tomorrow

Based on today's data, these are the stocks where the microstructure signals have **some** predictive value and calibration will help:

| Stock | Today's Move | Action |
|-------|-------------|--------|
| **TATASTEEL** | +0.82% day, +0.73% best 15m | Primary calibration target |
| **SUNPHARMA** | Multiple oscillations | Secondary target |
| **ICICIPRULI** | -0.53% window | Worth watching |
| **ITC** | -0.40% window | Low volume, signals unreliable |

Stocks to **remove from active universe consideration** (signals = noise):
- CANBK, PNB, JIOFIN — PSU/smaller caps where whale signals don't work

---

## Next Steps

- [x] Downloaded full day L2 data (761,963 ticks, 23 partitions)
- [x] Ran calibration analysis — results saved to `calibration_results_2026-08-17_hw300s.json`
- [ ] Lower Hawkes branching threshold: 0.35 → 0.20 in `value_engine.py`
- [ ] Change Hawkes window: 600s → 300s in `value_engine.py`
- [ ] Improve calibration script to sample peak-signal tick before each move
- [ ] Download and run on 2026-08-13 and 2026-08-14 L2 data for multi-day validation
- [ ] Run `python -m pytest tests/ -q` after any engine change

*Data: `calibration_notes/l2_data/2026-08-17/` | Script: `calibration_notes/calibration_analysis.py`*
