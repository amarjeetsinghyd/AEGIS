# Current Engine Compounded Option Simulation Report

This report simulates a **real-world compounding and strike scaling (pyramiding) scenario** for the **Current Engine's 3 Strategies** (Window Alignment, Rejection Window, VWAP Band Breakout) from **July 6, 2026, to July 17, 2026**.

---

## 📊 Summary Performance Metrics

* **Initial Capital**: **Rs 2,500.00**
* **Week 1 End Capital**: **Rs 25.60**
* **Week 2 End Capital**: **Rs 25.60**
* **Net Compounded Return**: **-98.98%**
* **Total Round-Trip Brokerage (Rs 17/trade)**: **Rs 867.00**

| Metric | Value |
| :--- | :--- |
| **Total Trades Executed** | **51** (Week 1: 51 | Week 2: 0) |
| **Win Rate** | **15.69%** (8 Wins / 43 Losses) |
| **Total Profit Generated** | **Rs 375.33** |
| **Total Losses Incurred** | **Rs 2,849.73** |

---

## 🛡️ Exit Reasons Breakdown

| Exit Reason | Count | Percentage |
| :--- | :--- | :--- |
| **Stop Loss (SL) Hit** | 37 | 72.55% |
| **Theta Decay Abort (at 3m)** | 14 | 27.45% |
| **5m Hard Cap Close** | 0 | 0.00% |

---

## 📈 Compounding & Strike selector scaling logic

1. **Compounding Capital**: Profit is added to capital, and losses are deducted. 
2. **Pyramid Strike selector**: As capital grows, the selector dynamically buys more lots and scales up position size. If capital decreases, the selector shifts deeper OTM to keep trading affordable.
3. **Brokerage Deduction**: Every round trip trades is charged Rs 17.00.
