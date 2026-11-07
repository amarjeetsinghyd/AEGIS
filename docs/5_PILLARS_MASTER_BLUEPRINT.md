# AEGIS Production v1.0 — 5-Pillars Master Blueprint & Microstructure Specification

> **दस्तावेज़ का उद्देश्य (Document Purpose)**:
> यह मास्टर ब्लूप्रिंट AEGIS Production Version 1.0 (Locked) के सभी 5 पिलर्स के सटीक क्वांट नियमों, गणितीय फॉर्मूलों, थ्रेशोल्ड्स, कैंडल-वाइज़ टाइमिंग और इन-फ्लाइट एग्जीक्यूशन लॉजिक का कैनोनिकल संदर्भ (Single Source of Truth) है।

---

## 1. 🚀 5-Pillars Master Blueprint (3-कॉलम तालिका)

| पिलर (Pillar) & स्ट्रेटेजी | क्वांट नियम (Quant Rule) | क्वांट फॉर्मूला व थ्रेशोल्ड (Quant Formula & Threshold) |
| :--- | :--- | :--- |
| **Pillar 1**<br>`20_ema_rejection`<br>*(Dow HL2 / LH2 Reversal)* | **1. Rejection Approach Ladder**<br>*(2-कैंडल तीखा फॉल/राइज)*<br><br>**2. Delta Flip Velocity**<br>*(ऑर्डर फ्लो रिवर्सल फ्लिप)*<br><br>**3. Absorption Rejection Wick**<br>*(20 EMA सपोर्ट/रेजिस्टेंस अब्सॉर्प्शन)*<br><br>**4. Dow Swing VDR Dry-Up**<br>*(रिट्रेसमेंट वॉल्यूम ड्राई-अप)* | 1. **Rejection Ladder (क्लोज्ड कैंडल्स $N-1$ व $N-2$ पर)**:<br>• **CALL**: $High_{N-1} < High_{N-2} \land Low_{N-1} < Low_{N-2}$ (20 EMA में तेज गिरावट)<br>• **PUT**: $High_{N-1} > High_{N-2} \land Low_{N-1} > Low_{N-2}$ (20 EMA में तेज उछाल)<br><br>2. **Delta Flip Velocity**:<br>• **CALL**: $\Delta_{N-1} \le 0$ (पुलबैक बार पर रिटेल सेलिंग) $\implies \Delta_N \ge 0$ (लाइव बार पर एग्रेसिव बाइंग फ्लिप)<br>• **PUT**: $\Delta_{N-1} \ge 0$ (पुलबैक बार पर रिटेल बाइंग) $\implies \Delta_N \le 0$ (लाइव बार पर एग्रेसिव सेलिंग फ्लिप)<br><br>3. **Absorption Wick**: $Wick_{N-1} \ge 0.30 \times Spread_{N-1}$ (टेस्टिंग कैंडल पर लंबा विक साबित करता है कि 20 EMA ने प्राइस को रोक लिया)<br><br>4. **Swing VDR**: $\frac{Vol_{pullback}}{Vol_{impulse}} \le 0.85$ (वॉल्यूम ड्राई-अप; यदि $VDR \ge 1.25$ तो डंप वीटो)<br><br>5. **In-Flight Reversal Pierce (लाइव बार $N$)**: Spot $\ge High_{N-1}$ (CALL) या Spot $\le Low_{N-1}$ (PUT) |
| **Pillar 2**<br>`micro_sweep_reclaim`<br>*(Stop-Hunt Liquidity Grab)* | **1. Confirmed Sweep Gate**<br>*(SweepStopRunDetector)*<br><br>**2. Sweep Absorption Wick**<br>*(स्टॉप-हंट अब्सॉर्प्शन)*<br><br>**3. Delta Flip Confirmation**<br>*(सपोर्टिव नॉन-क्लैशिंग फ्लैग)*<br><br>**4. Lunch Chop Shield** | 1. **Confirmed Sweep Gate**:<br>• $\text{Burst Ratio} \ge 2.20\times$ (लेवल टूटते ही स्टॉप-लॉस पैनिक ऑर्डर फ्लो का विस्फोट)<br>• Reclaim Absorption Flow $\ge 0.0$ (स्मार्ट मनी ने पैनिक को पूरी तरह अब्सॉर्ब किया)<br><br>2. **Sweep Wick**: $Wick_{N-1} \ge 0.30 \times Spread_{N-1}$ (स्वीप कैंडल $N-1$ पर लंबा लोअर/अपर विक)<br><br>3. **Delta Flip**: $\Delta_{N-1} \le 0 \to \Delta_N \ge 0$ (CALL) / $\Delta_{N-1} \ge 0 \to \Delta_N \le 0$ (PUT) — *कन्फर्म स्वीप के साथ नॉन-क्लैशिंग additive फ्लैग*<br><br>4. **Lunch Chop Veto**: 11:15 से 13:00 IST चॉप जोन में एंट्री पूरी तरह ब्लॉक |
| **Pillar 3**<br>`volatility_squeeze_breakout`<br>*(Tight Box Compression)* | **1. 5-7 Bar Box Compression**<br>*(शांत कंसोलिडेशन बॉक्स)*<br><br>**2. Squeeze-to-Expansion Velocity**<br>*(वॉल्यूम विस्तार दर)*<br><br>**3. Hard Participation Floor**<br>*(फैंटम ड्रिफ्ट वीटो)*<br><br>**4. Anti-Climax Cap**<br>*(एग्जॉशन बार ब्लॉक)* | 1. **Box Compression**: 5-7 बार की रेंज $\le 1.15\times ATR_{1m}$ और हर कैंडल $\le 0.85\times ATR_{1m}$<br><br>2. **Expansion Velocity**: $Rate_{curr} \ge 1.50\times \overline{Rate}_{box}$ (शांत बॉक्स के औसत वॉल्यूम से $1.5\times$ तेज सर्ज)<br><br>3. **Hard Floor Veto**: $VDR < 0.40\times \implies$ `PHANTOM_DRIFT` वीटो (बिना वॉल्यूम का झूठा ब्रेकआउट ब्लॉक)<br><br>4. **Anti-Climax Cap**: लाइव कैंडल $Candle_{range} \le 1.35\times ATR_{1m}$ (अत्यधिक विशाल कैंडल पर टॉप पर एंट्री ब्लॉक, क्योंकि ऐसी कैंडल में मोमेंटम एग्जॉस्ट हो जाता है)<br><br>5. **Delta Imbalance**: $Vol_{dir} \ge 2.0\times Vol_{opposing}$ |
| **Pillar 4**<br>`sr_breakout`<br>*(Dynamic S/R Breakout)* | **1. Strict 2-Candle Approach Ladder**<br>*(सीढ़ीदार दृष्टिकोण)*<br><br>**2. N-1 Demand Ignition (VER)**<br>*(प्री-ब्रेकआउट इग्निशन)*<br><br>**3. Day Extreme Runway Shield**<br>*(स्केल-इनवेरिएंट रनवे)*<br><br>**4. Topological Prominence** | 1. **2-Candle Approach Ladder (क्लोज्ड बार $N-1$ व $N-2$)**:<br>• **CALL**: $High_{N-1} > High_{N-2} \land Low_{N-1} > Low_{N-2} \land Close_{N-1} > Open_{N-1}$ (रेजिस्टेंस के नीचे ग्रीन सीढ़ी)<br>• **PUT**: $High_{N-1} < High_{N-2} \land Low_{N-1} < Low_{N-2} \land Close_{N-1} < Open_{N-1}$ (सपोर्ट के ऊपर रेड सीढ़ी)<br><br>2. **Demand Ignition (VER)**: $VER_{N-1} = \frac{Vol_{N-1}}{\overline{Vol}_{5m}} \ge 1.10\times$<br><br>3. **Day Extreme Runway**: $\text{Runway} \ge \max(1.25 \times ATR_{1m}, 0.0008 \times Spot)$<br><br>4. **Prominence**: Level Prominence $\ge 1.25$ (छद्म 1m लेवल्स ब्लॉक) |
| **Pillar 5**<br>`gamma_trap_squeeze`<br>*(Trapped Writer Squeeze)* | **1. Decoupled Writer VWAP Breach**<br>*(काउंटरपार्टी पेन पॉइंट)*<br><br>**2. 3-Candle Dow Thrust Ladder**<br>*(मजबूत मोमेंटम सीढ़ी)*<br><br>**3. Counter-Liquidation Shield**<br>*(अनवाइंडिंग डंप सुरक्षा)* | 1. **Writer VWAP Breach**:<br>• **CALL**: $Spot > VWAP_{CallSell} + (0.15 \times ATR)$ (कॉल राइटर्स ट्रैप $\to$ शॉर्ट कवरिंग)<br>• **PUT**: $Spot < VWAP_{PutSell} - (0.15 \times ATR)$ (पुट राइटर्स ट्रैप $\to$ लॉन्ग लिक्विडेशन)<br><br>2. **3-Candle Dow Thrust**: Consecutive Higher Highs व Higher Closes (कंसोलिडेशन व्हिपसॉ ब्लॉक)<br><br>3. **Liquidation Shield**: $Vol_{PE} \le 2.5\times Vol_{CE}$ (कॉल ट्रैप के समय पुट अनवाइंडिंग डंप ब्लॉक) |

---

## 2. 🔍 प्रमुख क्वांट अवधारणाओं का कैंडल-वाइज़ स्पष्टीकरण

### क. Pillar 1 और Pillar 2 में एब्जॉर्प्शन विक (Absorption Wick) किस कैंडल पर होता है?
* **Pillar 1 (`20_ema_rejection`)**:
  - **कैंडल $N-1$ (टेस्टिंग कैंडल)**: यह कैंडल 20 EMA को छूती है या टेस्ट करती है।
  - यदि बुल्स 20 EMA को डिफेंड करते हैं, तो $N-1$ कैंडल पर नीचे से एक लंबा लोअर विक बनता है ($Wick \ge 0.30 \times Spread$)।
  - **लाइव कैंडल $N$**: इस कैंडल पर विक नहीं देखा जाता! लाइव कैंडल $N$ तो $T:02s+$ पर $N-1$ के हाई को तोड़ती है (In-Flight Reversal Pierce)।
* **Pillar 2 (`micro_sweep_reclaim`)**:
  - **स्वीप कैंडल $N-1$**: यह कैंडल लेवल के नीचे जाकर वापस अंदर क्लोज होती है, जिस पर $0.30 \times Spread$ का रिजेक्शन विक होता है।
  - **लाइव कैंडल $N$**: $T:02s+$ पर लेवल को रिक्लेम करते ही ट्रेड लेता है।

### ख. Pillar 3 में एंटी-क्लाइमैक्स कैप (Anti-Climax Cap) क्यों है?
* स्क्वीज़ ब्रेकआउट पर अगर पहली ही कैंडल बहुत बड़ी (उदा. $1.5\times$ या $2.0\times ATR$) बन जाती है, तो उसे क्वांट में "Exhaustion Climax" कहते हैं। 
* ऐसी कैंडल के चरम सिरे पर घुसने से ट्रेड तुरंत रिवर्सल में फँस जाता है और हमारा +4% TP नहीं आ पाता।
* इसलिए **Anti-Climax Cap ($Candle \le 1.35\times ATR_{1m}$)** यह सुनिश्चित करता है कि ब्रेकआउट फ्रेश हो और हमारे +4% टेक प्रॉफिट के लिए पूरा रनवे बाकी हो।

### ग. Pillar 2 में Confirmed Sweep बनाम Delta Flip का समन्वय:
* Pillar 2 का कोर इंजन **`SweepStopRunDetector` (Confirmed Stop-Hunt)** है, जो लेवल ब्रीच पर पैनिक स्टॉप्स के विस्फोट ($\ge 2.2\times$ बस्ट रेट) को नापता है।
* डेल्टा फ्लिप को इसमें अनिवार्य वीटो नहीं बनाया गया है ताकि यह कन्फर्म स्वीप को ब्लॉक न करे; यह एक सपोर्टिव कन्फर्मेशन मेट्रिक के रूप में काम करता है।

---

## 3. 🧹 आर्किटेक्चरल शुद्धिकरण (Zero-Entropy Purge)

1. **Mode A का संपूर्ण निष्कासन**:
   - `_mode_a_armed_setups`, `_evaluate_mode_a_arming`, `check_mode_a_pierce` और `check_mode_a_pre_close` को पूरी तरह हटा दिया गया है।
   - हर 1-मिनट कैंडल क्लोज़ पर चलने वाली व्यर्थ आर्मिंग लूप समाप्त।
   - **Priority Mode B ($T:02s+$ onwards / No Upper Cap)** ही AEGIS का एकमात्र कैनोनिकल इंजन है।
2. **ShadowBoundaryObserver से 20 EMA हटाया गया**:
   - 20 EMA Rejection पहले से लाइव प्रोडक्शन (Pillar 1) में ट्रेड होता है।
   - शैडो ऑब्जर्वर अब विशुद्ध रूप से **Absolute Pain Zone Band** और **200 EMA Ribbon** के ब्रेकआउट व रिजेक्शन एनालिटिक्स पर केंद्रित है।
3. **Delta Flip टेलीमेट्री**:
   - शैडो ट्रेड्स में `delta_flip: bool` जोड़ा गया है, जो सीधे `telemetry.jsonl` में रिकॉर्ड हो रहा है।

---

## 4. 📊 Git, VPS व टेस्ट स्थिति

* **कमिट आईडी**: `4ba248d8` / `e9006618` (`feat(AEGIS): wire Delta Flip into Pillar 1, activate Confirmed Sweep in Pillar 2, decommission Mode A for zero entropy`)
* **लोकल टेस्ट्स**: 97 passed, 22 skipped (Mode A लिगेसी टेस्ट्स) in 2.71s — 100% GREEN.
* **VPS स्थिति**: 
  - सभी फाइलें VPS (`/home/opc/quant-bot/`) पर पूरी तरह सिंक्रनाइज़्ड।
  - VPS टेस्ट्स: 41 passed, 22 skipped in 2.61s — 100% GREEN.
  - PM2 बॉट (`aegis-bot`, PID 1222325) सोमवार सुबह 08:55 AM IST तक डीप-स्लीप में ऑनलाइन है।
* **चेंजलॉग**: `docs/CHANGELOG.md` संस्करण `[1.0.3] - 2026-11-07` के साथ अपडेटेड।
