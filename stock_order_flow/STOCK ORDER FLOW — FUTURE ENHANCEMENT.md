# STOCK ORDER FLOW ENGINE — FUTURE ENHANCEMENTS & ROADMAP
**Status:** V1 PRODUCTION FROZEN (Awaiting Monday Live Validation)
**Next Review:** Monday Evening / Tuesday (Post-Initial Live Data)
**Philosophy:** Stability First. Iterate only after real-market validation.

---

## 1. CORE PHILOSOPHY FOR FUTURE ITERATIONS
हमारा V1 system architecture institutional-grade है, लेकिन live market ही हमें बताएगा कि असली edge कहाँ है। इसलिए आगे का हर enhancement इस rule को follow करेगा:
1. **Live Data First:** कोई भी logic change बिना 50+ live trades के data के नहीं होगा।
2. **Safety First:** जो changes core logic को break कर सकते हैं, वे केवल weekend maintenance window में होंगे।
3. **Measurable Edge:** हर नए feature का backtest या forward-test होगा। "Institutional सुनने वाला" concept तब तक नहीं जुड़ेगा जब तक वह costs के बाद extra alpha न दे।

---

## 2. PRIORITY 1: MICROSTRUCTURE & DATA QUALITY (Quick Wins)
ये safe, low-risk changes हैं जो core logic को break किए बिना data quality बढ़ाएंगे। इन्हें Monday evening या Tuesday के maintenance window में implement किया जा सकता है।

### 2.1 Quote Age Tracking (CVD Reliability)
- **Problem:** अगर tick का `lp` पुराने `bid/ask` से compare हो, तो tick-rule classification गलत हो सकती है, जिससे CVD unreliable हो जाता है।
- **Solution:** `tick_compiler.py` में Shoonya feed timestamp (`ftm0` या `ft`) track करें। अगर `quote_age > 3000ms`, तो tick को `NEUTRAL` classify करें (CVD में न जोड़ें)।
- **Impact:** False absorption signals में कमी।

### 2.2 Depth-Weighted L2 (Spoofing Protection)
- **Problem:** Simple average L2 depth दूर के levels को best bid/ask के बराबर weight देता है, जिससे spoofed liquidity का प्रभाव पड़ता है।
- **Solution:** Distance-weighted imbalance use करें (Level 1 = 1.0x, Level 2 = 0.5x, Level 3 = 0.33x...)।
- **Impact:** Best bid/ask को priority, spoofing protection।

---

## 3. PRIORITY 2: SETUP REFINEMENTS (Post-Live Validation)
ये changes setups की win-rate बढ़ाने के लिए हैं, लेकिन इन्हें implement करने से पहले यह देखना होगा कि V1 में false breakouts कितने हो रहे हैं।

### 3.1 Breakout Acceptance Score (Setup A Guard)
- **Problem:** Zone से बाहर close होना काफ़ी नहीं है; price को वहाँ टिकना भी चाहिए (failed auction/rejection से बचने के लिए)।
- **Solution:** Setup A में `acceptance_score` add करें (पिछली 3 candles में से कितनी close > upper हुईं)। अगर score < 0.66, तो reject करें।
- **Impact:** False breakouts से बचाव।

### 3.2 Tick-Level Volume Profile (Accurate POC/VAH/VAL)
- **Problem:** Current Volume Profile candle range में volume evenly distribute करता है, जो approximation है।
- **Solution:** `_update_volume_profile()` को candle-range से हटाकर actual `footprints` (tick-level price distribution) से build करें।
- **Impact:** Accurate POC/VAH/VAL levels, बेहतर overhead-supply guard।

---

## 4. PRIORITY 3: RISK & EXECUTION ANALYTICS
यह section trading को stop करने के लिए नहीं, बल्कि system की health measure करने के लिए है।

### 4.1 Risk Units Circuit Breaker
- **Problem:** 5% daily loss cap अच्छा है, लेकिन "3 लगातार SL = pause" जैसा behavioral circuit breaker missing है।
- **Solution:** `_day_risk_units_used` track करें। अगर 3 full SLs hit हों, तो बाकी दिन के लिए entries pause कर दें (भले ही 5% cap hit न हुआ हो)।
- **Impact:** Revenge trading / tilt prevention।

### 4.2 MAE/MFE Attribution (Trade Quality)
- **Problem:** केवल Win/Loss से पता नहीं चलता कि stop गलत था, target गलत था, या entry timing।
- **Solution:** हर trade में Maximum Adverse Excursion (MAE) और Maximum Favorable Excursion (MFE) log करें।
- **Impact:** Data-driven TP/SL optimization (50+ trades के बाद)।

### 4.3 Execution Quality Model (Slippage & Latency)
- **Problem:** Strategy alpha और execution cost को अलग-अलग measure करना ज़रूरी है।
- **Solution:** Order send timestamp, ack timestamp, और expected vs actual fill price (slippage in bps) log करें।
- **Impact:** Broker quality और market impact analysis।

---

## 5. PRIORITY 4: EXTERNAL CONTEXT (Phase 2+)
ये features external APIs या complex ML models require करते हैं। इन्हें तभी implement करेंगे जब V1 consistently profitable हो जाए।

### 5.1 News & Event Filter
- **Logic:** Earnings, RBI policy, index rebalance के दिन trading pause या size reduce करें।
- **Requirement:** External NSE/BSE calendar API integration।

### 5.2 Market Regime Classifier
- **Logic:** Market को Trend, Balance, High-Vol News, या Choppy regime में classify करें। Regime के हिसाब से Setup A/B/D को weight दें।
- **Requirement:** ADX + Volume + Volatility clustering model।

---

## 6. PRIORITY 5: VALIDATION INFRASTRUCTURE
बिना rigorous testing के कोई भी नया feature production में नहीं जाएगा।

### 6.1 Walk-Forward Backtesting Engine
- **Logic:** Historical 3m + tick data पर हर setup को अलग-अलग test करें। In-sample और out-of-sample performance compare करें।
- **Requirement:** Dedicated `backtest_engine.py` और historical tick data storage (PostgreSQL या S3)।

---

## 7. OUT OF SCOPE (Why we rejected certain Prop-Desk features)
ये features हमारे setup (Retail/Small Prop, NSE Equities, 5x MIS) पर लागू नहीं होते:
- **HFT Queue Position / Limit Order Routing:** हम MKT orders use करते हैं (execution certainty > queue priority)।
- **Dark Pool Data:** Indian equities में उपलब्ध नहीं है।
- **Full L3 Order Book Reconstruction:** Shoonya केवल 5-level depth देता है, full order-by-order data नहीं।
- **Multi-Exchange Arbitrage:** हम केवल NSE cash equities trade करते हैं।

---

**END OF ROADMAP DOCUMENT**