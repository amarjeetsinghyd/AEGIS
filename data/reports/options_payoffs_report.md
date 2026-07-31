# Nifty Options Non-Linear Payoffs Simulation Report

This report evaluates the performance of the **Kronos Options Bot** on the **95 momentum signals** triggered during the period **July 6, 2026, to July 17, 2026**. 
It extrapolates Spot Nifty moves to the weekly options contract level using a **non-linear Delta-Gamma option pricing model** (incorporating gamma acceleration) and runs the **finalized trailing stop loss & breakeven rules**.

---

## 📊 Summary Performance Metrics

* **Option contract type**: ATM Weekly Contract ($P_0 = 40.0$, Delta $\approx 0.5$, Gamma $\approx 0.002$)
* **Lot Size**: 25 contracts (Nifty)

| Metric | Value |
| :--- | :--- |
| **Total Trades Executed** | **153** |
| **Win Rate** | **60.13%** (92 Wins / 61 Losses) |
| **Average Winning Trade** | **+17.38%** |
| **Average Losing Trade** | **-7.74%** |
| **Total Gross Profit** | **Rs 15,989.69** |
| **Total Gross Loss** | **Rs 4,718.98** |
| **Net P&L (1 Lot Nifty)** | **Rs +11,270.71** |

---

## 📈 Non-Linear Option Pricing & Gamma Burst Physics

Weekly option contracts possess highly non-linear price characteristics due to **Gamma ($G$)**:
$$\Delta P = \Delta \times \Delta S + \frac12 G \times (\Delta S)^2$$

### 1. Gamma Acceleration (Upward Surge)
When Nifty moves in the trade's direction:
* Delta increases.
* The option premium rises at an accelerating speed.
* This allows the bot to capture **large +30% to +50% gains** during sharp 5-minute index moves (gamma bursts).

### 2. Gamma Cushioning (Downward Mitigation)
When Nifty moves against the trade:
* Delta decreases toward zero (the option moves further OTM).
* The rate of option premium decline slows down, shielding the trader from catastrophic losses.

---

## 🛡️ Exit Strategy Effectiveness

1. **Fast Breakeven (+5% HWM -> 0% Risk)**: Protected multiple trades from turning into losses when Nifty spiked and immediately reversed.
2. **Trailing locks (+10% HWM -> Lock +5%, etc.)**: Captured profits on sudden spike candles that closed flat.
3. **Initial -20% Stop Loss**: Hard-capped losses, preventing major drawdown on large opposing gap candles.
