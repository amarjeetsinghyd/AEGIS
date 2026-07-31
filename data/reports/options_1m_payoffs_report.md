# Nifty Options High-Fidelity 1-Minute Payoffs Simulation Report

This report presents a **high-fidelity historical simulation** evaluating the performance of the **Kronos Options Bot** on the **153 momentum signals** triggered during **July 6, 2026, to July 17, 2026**.

Unlike the previous report which estimated intra-candle movements, this simulation **downloads and evaluates the actual 1-minute historical Spot Nifty data** for each trade. 
For every signal, it steps through the next **5 individual 1-minute candles** sequentially to track stop loss triggers, dynamic breakeven adjustments, and theta-decay exits.

---

## 📊 Summary Performance Metrics

* **Option contract type**: ATM Weekly Contract ($P_0 = 40.0$, Delta $\approx 0.5$, Gamma $\approx 0.002$)
* **Lot Size**: 25 contracts (Nifty)

| Metric | Value |
| :--- | :--- |
| **Total Trades Executed** | **153** |
| **Win Rate** | **64.71%** (99 Wins / 54 Losses) |
| **Average Winning Trade** | **+20.03%** |
| **Average Losing Trade** | **-8.04%** |
| **Total Gross Profit** | **Rs 19,828.78** |
| **Total Gross Loss** | **Rs 4,340.86** |
| **Net P&L (1 Lot Nifty)** | **Rs +15,487.93** |

---

## 🛡️ Exit Reasons Breakdown

| Exit Reason | Count | Percentage |
| :--- | :--- | :--- |
| **Stop Loss (SL) Hit** | 137 | 89.54% |
| **Theta Decay Abort (at 3m)** | 16 | 10.46% |
| **5m Hard Cap Close** | 0 | 0.00% |

---

## 📉 Dynamic Risk Strategy Insights (1-Minute Tick level)

1. **Theta Abort at 3 Minutes**: Successfully kicked the bot out of sideways trades where Nifty remained flat, saving options premium from eroding due to time decay.
2. **Fast Breakeven (+5% HWM)**: Provided immediate protection, stopping out trades at 0% loss (cost) when Nifty experienced false breakouts.
3. **Delta-Gamma Cushioning**: Confirming physical options speed behavior, when Nifty went against the trade, the option value decreased slower, while favorable moves accelerated due to positive Gamma.
