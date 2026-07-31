# Kronos Compounded Pyramid Option Simulation Report

This report simulates a **real-world compounding and strike scaling (pyramiding) scenario** for the **Kronos Options Bot** on the **153 momentum signals** from **July 6, 2026, to July 17, 2026**.

---

## 📊 Summary Performance Metrics

* **Initial Capital**: **Rs 2,500.00**
* **Week 1 End Capital**: **Rs 765,611,417.14**
* **Week 2 End Capital**: **Rs 13,927,369,777.87**
* **Net Compounded Return**: **+557,094,691.11%**
* **Total Round-Trip Brokerage (Rs 17/trade)**: **Rs 2,601.00**

| Metric | Value |
| :--- | :--- |
| **Total Trades Executed** | **153** (Week 1: 90 | Week 2: 63) |
| **Win Rate** | **64.71%** (99 Wins / 54 Losses) |
| **Total Profit Generated** | **Rs 19,500,393,529.56** |
| **Total Losses Incurred** | **Rs 5,573,026,251.69** |

---

## 🛡️ Exit Reasons Breakdown

| Exit Reason | Count | Percentage |
| :--- | :--- | :--- |
| **Stop Loss (SL) Hit** | 140 | 91.50% |
| **Theta Decay Abort (at 3m)** | 13 | 8.50% |
| **5m Hard Cap Close** | 0 | 0.00% |

---

## 📈 Compounding & Strike selector scaling logic

1. **Compounding Capital**: Profit is added to capital, and losses are deducted. 
2. **Pyramid Strike selector**: As capital grows, the selector dynamically buys more lots and scales up position size. If capital decreases, the selector shifts deeper OTM to keep trading affordable.
3. **Brokerage Deduction**: Every round trip trades is charged Rs 17.00.
