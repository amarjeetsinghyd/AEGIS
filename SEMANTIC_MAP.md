# AEGIS QUANT-BOT — SEMANTIC ARCHITECTURE MAP
> **Comprehensive Codebase Inventory, Lines of Code (LOC), and Subsystem Responsibilities**  
> *Updated on: 14 November 2026 | Active Production Engine: AEGIS (v1.0 Locked) | Zero-Entropy Flattened Architecture*

---

## 📊 कुल सिस्टम सारांश (Total Codebase Metrics)

| सब-सिस्टम (Subsystem) | मुख्य ज़िम्मेदारी | सक्रिय फ़ाइलें | कोड की लाइनें (LOC) | स्थिति (Status) |
| :--- | :--- | :---: | :---: | :--- |
| 🧠 **1. Core Pipeline (`core/`)** | 5 मैक्रो पिलर्स, Priority Mode B ब्रेकथ्रू, Wyckoff वॉल्यूम गेट, कनविक्शन स्कोरिंग, L2 टिक प्रोसेसिंग | 15 | **11,282** | 🟢 **De-bloated & Mode A Purged** |
| ⚡ **2. Execution & Lifecycle (`execution/`)** | Zero-Regression 5-Mixin आर्किटेक्चर, 0.75s सिंगल-शॉट एंट्री, +4% मेकर TP, यूनिवर्सल स्टॉप, CAS फ्लैटन | 14 | **5,520** | 🚀 **Modularized into 5 Mixins** |
| 🌐 **3. Broker & Market Seam (`broker/`, `market/`)** | Finvasia Shoonya एडेप्टर, 2FA, थ्रॉटलिंग, 1-min टिक-टू-कैंडल कंपाइलर, स्क्रिप रिपॉजिटरी | 11 | **2,595** | 🛡️ **Footprint & Bloat Purged** |
| 📊 **4. Unified Dashboard (`dashboard/`)** | रियल-टाइम कॉकपिट UI, रिसर्च विज़ुअलाइज़ेशन, टेलीमेट्री लोडर, Flask सर्वर | 5 | **4,176** | 🟢 **Active Telemetry & UI (De-bloated)** |
| 🚀 **5. Top-level Core & Config** | `main.py`, `backtest.py`, `symbols.py`, `logger.py`, `config/`, `ipc/` | 9 | **2,005** | 🛡️ **Single Root Configuration** |
| 🎯 **ग्रैंड टोटल (Clean Production Total)** | **पूर्ण प्रोडक्शन आर्किटेक्चर (Strict Production Only)** | **54** | **25,578** | 🟢 **100% Test Green (237/237 Passed)** |

---

## 🧠 1. AEGIS Core Pipeline (रणनीति, प्राइस एक्शन और डिसीजन मेकिंग — `core/`)

यह सिस्टम का कोर मस्तिष्क है, जहाँ लाइव टिक डेटा प्रोसेस होकर 5 पिलर्स और कनविक्शन स्कोरिंग के ज़रिए हाई-प्रोबेबिलिटी सिग्नल्स में तब्दील होता है:

| फ़ाइल का नाम (File Path) | लाइनें (LOC) | सिमेंटिक ज़िम्मेदारी (Semantic Role) |
| :--- | :---: | :--- |
| `core/price_action_engine.py` | **1,972** | **कोर प्राइस एक्शन**: 1m/5m/15m OHLC बार निर्माण, पाँचों मैक्रो पिलर्स (P1 Dow 20-EMA, P2 Micro-Sweep Reclaim, P3 Squeeze Box, P4 S/R Breakout, P5 Gamma Trap Writer Squeeze) और **Priority Mode B** इन-फ्लाइट ($T:02s+$) ब्रेकथ्रू। *(Mode A पूरी तरह डीकमीशन होकर -757 लाइन्स हल्का)* |
| `core/pipeline_orchestrator.py` | **1,888** | **सेंट्रल पाइपलाइन ऑर्केस्ट्रेटर**: L2 टिक डिस्ट्रीब्यूशन, 6-गेट पाइपलाइन को सिंक्रनाइज़ करना, 3-स्ट्राइक बजट क्लस्टर प्री-स्टेजिंग और एग्जीक्यूशन क्यू में सिंगल-शॉट सिग्नल पुश करना। |
| `core/structural_registry.py` | **1,042** | **डायनामिक S/R लेवल रजिस्ट्री**: मोनोटोनिक `seq_idx` से सपोर्ट/रेजिस्टेंस लेवल ट्रैकिंग, 5m क्लस्टरिंग, मल्टी-टच स्कोरिंग और रूफ/फ्लोर क्लासिफिकेशन। |
| `core/option_order_flow.py` | **1,010** | **ऑप्शन माइक्रोस्ट्रक्चर & कनविक्शन**: ATM $\pm 5$ स्ट्राइक फ्लो, OFI डेल्टा, PCR, एब्सोल्यूट पेन ज़ोन 4-लाइन VWAP बैंड्स और **कम्पोज़िट कनविक्शन स्कोरिंग ($\ge 80.0$)**। |
| `core/shadow_boundary_observer.py` | **927** | **शैडो रिसर्चर**: एब्सोल्यूट पेन ज़ोन और 200 EMA ब्रेकआउट्स को बैकग्राउंड में बिना रियल फंड्स रिस्क के ट्रैक करना और लेटेंसी/सक्सेस रेट रिकॉर्ड करना। |
| `core/sweep_stop_run_detector.py` | **697** | **स्वीप & स्टॉप-रन डिटेक्टर**: लाइव माइक्रोस्ट्रक्चर स्टॉप-लॉस बर्स्ट ($\ge 2.2\times$ बर्स्ट रेशियो) और एब्जॉर्प्शन फॉर्मूला द्वारा लिक्विडिटी स्वीप की पहचान। |
| `core/outcome_telemetry.py` | **691** | **ट्रेड टेलीमेट्री**: सिग्नल्स, एंट्री/एग्जिट स्लिपेज, लेटेंसी और एग्जीक्यूशन मैट्रिक्स को JSONL फॉर्मेट में रिकॉर्ड करना। |
| `core/dow_macro_shields.py` | **633** | **डाउ मैक्रो शील्ड्स**: क्लासिकल डाउ थ्योरी (HL2/LH2 स्विंग्स), 20 EMA हाई-लो बैंड्स, और स्केल-इनवेरिएंट डे एक्सट्रीम रनवे शील्ड (`min_runway_pts = max(1.25 * ATR_1m, 0.0008 * Spot)` )। |
| `core/gex_engine.py` | **593** | **गामा एक्सपोज़र (GEX)**: नेट मार्केट मेकर गामा पोज़िशनिंग, ज़ीरो गामा लेवल और स्केल-इनवेरिएंट प्रॉक्सिमिटी डिटेक्शन। |
| `core/wyckoff_volume_engine.py` | **442** | **वाइकोफ़ वॉल्यूम गेट**: एफर्ट वर्सेज रिजल्ट, वॉल्यूम ड्राई-अप रेशियो ($VDR \le 0.85$), एब्जॉर्प्शन पिन विक और डेल्टा हार्मनी। *(ऑडिट में 59% हल्का होकर अल्ट्रा-लीन)* |
| `core/iceberg_detector.py` | **439** | **आइसबर्ग ऑर्डर डिटेक्टर**: हिडन लिक्विडिटी और लार्ज इंस्टीट्यूशनल रीलोड्स को ऑर्डर बुक डेप्थ से डिटेक्ट करना। |
| `core/atm_sync.py` | **419** | **ATM स्ट्राइक सिंक**: स्पॉट मूवमेंट के साथ रियल-टाइम ATM स्ट्राइक स्ट्राइक-स्टेप को सिंक में रखना। |
| `core/l2_options_archiver.py` | **397** | **डेटा आर्काइवर**: 15-मिनट इंटरवल पर लाइव L2 ऑप्शन टिक डेटा को कंप्रेस करके बैकटेस्टिंग और रिसर्च के लिए सेव करना। |
| `core/option_quality.py` | **131** | **लिक्विडिटी चेकर**: ऑप्शन कॉन्ट्रैक्ट में टू-साइडेड डेप्थ फ्लोर और मिनिमम प्रीमियम फ्लोर वैलिडेशन। |
| `core/__init__.py` | **1** | **पैकेज इनिशियलाइज़र**। |

---

## ⚡ 2. AEGIS Execution & Lifecycle (ऑर्डर प्लेसमेंट और रिस्क मैनेजमेंट — `execution/`)

यह लेयर सिग्नल्स को मार्केट में 1-लॉट ऑर्डर्स में बदलती है और ट्रेड को प्रोटेक्ट करती है। 14 नवंबर 2026 को इसे **Zero-Regression Mixin आर्किटेक्चर** में मॉड्यूलर किया गया:

| फ़ाइल का नाम (File Path) | लाइनें (LOC) | आर्किटेक्चरल पैटर्न | सिमेंटिक ज़िम्मेदारी (Semantic Role) |
| :--- | :---: | :--- | :--- |
| `execution/entry_engine.py` | **959** | `EntryEngineMixin` | **सिग्नल इनटेक & एंट्री**: `_process_aegis_signal`, स्ट्राइक बजट रिफ्रेश (`_refresh_budget_strike_targets`), एडमिशन गेट्स (`can_enter`), मेकर TP प्लेसमेंट, स्लॉट रोलबैक (`_rollback_slot`), और ब्रोकर रिजेक्शन लॉगिंग। |
| `execution/exit_chaser.py` | **923** | `ExitChaserMixin` | **एग्जिट चेज़र & रिकॉन्सिलिएशन**: 4-स्टेप लिमिट एग्जिट चेज़ (`_do_chase_exit`), ऑर्डर इवेंट्स (`on_order_update`), फिल पोलिंग (`_order_fill`), TP कैंसलेशन, ब्रोकर पोजीशन रिकॉन्सिलिएशन, और क्रिटिकल अलार्म। |
| `execution/trade_monitor.py` | **817** | `TradeMonitorMixin` | **ट्रेड मॉनिटर & एग्जिट्स**: लाइव टिक मॉनिटरिंग (`_manage_open_trade`), डायनामिक नॉइज़ फ्लोर (`_compute_noise_floor`), रियलाइज्ड डेल्टा, 0DTE टाइम स्टॉप, डंप डिटेक्टर, और यूनिवर्सल स्पॉट इनवैलिडेशन SL। |
| `execution/trade_persistence.py` | **567** | `TradePersistenceMixin` | **स्टेट & हिस्ट्री पर्सिस्टेंस**: डेली स्टेट (`_load_trades_today`, `_save_trades_today`), डेली PnL व NSE चार्जेस कैलकुलेशन, ट्रेड हिस्ट्री अपेंड (`_append_history`), और क्रैश रिकवरी एक्टिव कॉन्टेक्स्ट सेव/लोड। |
| `execution/execution_manager.py` | **409** | `AegisExecutionManager` *(Orchestrator)* | **स्लिम ऑर्केस्ट्रेटर**: चारों Mixins का इनहेरिटेंस, `__init__`, मुख्य थ्रेड लूप (`_run_loop`), L1 बुक स्नैपशॉट (`_latest_option_book`), `get_slot_state`, `get_instrument_params`, और कॉन्स्टेंट्स री-एक्सपोर्ट। |
| `execution/order_lifecycle.py` | **364** | State Machine | **एटॉमिक ऑर्डर लाइफसाइकिल**: 0.75s टाइमआउट, लिमिट प्राइसिंग कैलकुलेशन, और मेकर TP प्राइसिंग। |
| `execution/data_fetcher.py` | **302** | Data Seeder | **कोल्ड-बूट हिस्टोरिकल सीडर**: सुबह 09:14 AM पर पिछले 400 कैंडल्स ब्रोकर से फेच करके इंजन की मेमोरी को वॉर्म-अप करना। |
| `execution/strike_selector.py` | **293** | Strike Filter | **स्ट्राइक सेलेक्टर**: स्पॉट के पास 3-स्ट्राइक बजट क्लस्टर और लो-कैपिटल टेस्टिंग मोड में सबसे अफोर्डेबल OTM स्ट्राइक चुनना। |
| `execution/slot_and_risk_constants.py` | **239** | Policies & Constants | **रिस्क नियम & स्लॉट पॉलिसी**: `QualitySlotPolicy`, `PostTriggerStabilityTracker`, रिस्क कॉन्स्टेंट्स (`MAX_HOLD_SEC`, `AEGIS_ENTRY_CUTOFF`, `RESTING_TP_LIMIT_PCT`), और स्केल-इनवेरिएंट Bps/ATR थ्रेशोल्ड्स। |
| `execution/dump_detector.py` | **229** | Micro Protection | **सब-मिलीसेकंड डंप डिटेक्टर**: +2.0% प्रॉफिट ज़ोन में बड़े प्लेयर्स के अचानक एग्जिट को $O(1)$ में पकड़कर प्रोटेक्ट करना। |
| `execution/trade_context.py` | **166** | Data Model | **ट्रेड कॉन्टेक्स्ट**: एक्टिव पोजीशन्स, लेग्स, एंट्री टाइम, हाई-वाटरमार्क, और `resolve_leg_net_qty` (यूनिट इनवेरिएंट NIFTY 65 / SENSEX 20)। |
| `execution/capital_engine.py` | **134** | Risk Gate | **कैपिटल प्रोटेक्शन**: अकाउंट बैलेंस और मार्जिन चेक्स (लो-कैपिटल शील्ड्स)। |
| `execution/realistic_paper_broker.py` | **115** | Broker Math | **NSE चार्जेस & ब्रोकरेज**: वास्तविक SEBI, STT, और एक्सचेंज टर्नओवर फीस का सटीक मॉडेलर। |
| `execution/__init__.py` | **3** | Package Init | **पैकेज इनिशियलाइज़र**। |

---

## 🌐 3. Broker & Market Seam (ब्रोकर API, टिक कंपाइलेशन — `broker/`, `market/`)

यह लेयर ब्रोकर API, मार्केट टिक कंपाइलेशन और डेटा सीम संभालती है:

| फ़ाइल का नाम (File Path) | लाइनें (LOC) | सिमेंटिक ज़िम्मेदारी (Semantic Role) |
| :--- | :---: | :--- |
| `broker/shoonya_adapter.py` | **1,106** | **Shoonya API एडेप्टर**: NorenAPI ऑर्डर्स (Buy/Sell/Cancel), WebSocket L2 टिक सब्सक्रिप्शन और नेटिव ऑर्डर अपडेट्स (`om` पुश)। |
| `market/tick_compiler.py` | **382** | **टिक-टू-कैंडल कंपाइलर**: लाइव टिक्स से रियल-टाइम 1-मिनट OHLC बार्स बनाना और बार-क्लोज़ इवेंट्स ट्रिगर करना। |
| `market/instrument_repository.py` | **348** | **मार्केट इंस्ट्रूमेंट्स**: मास्टर स्क्रिप सिम्बल्स, टोकन लुकअप और कॉन्ट्रैक्ट सर्च। |
| `broker/shoonya_auth.py` | **178** | **ऑटोमैटिक 2FA लॉगिन**: TOTP जनरेशन और ब्रोकर सेशन टोकन ऑटो-रिफ्रेश। |
| `broker/rate_limiter.py` | **167** | **रेट लिमिटर**: Shoonya API लिमिट्स और कॉलबैक थ्रॉटलिंग का लीन प्रिवेंशन। |
| `market/market_calendar.py` | **166** | **मार्केट कैलेंडर**: NSE/BSE ट्रेडिंग हॉलिडेज और वीकली एक्सपायरी डे कैलकुलेशन (Mon-Tue NIFTY, Wed-Thu SENSEX, Fri Sleep)। |
| `broker/manifest.py` | **147** | **ब्रोकर मैनिफेस्ट**: ब्रोकर फीचर्स और कैपेबिलिटीज की मैपिंग। |
| `broker/interfaces.py` | **72** | **ब्रोकर इंटरफेसेस**: एब्सट्रैक्ट बेस क्लासेस और ब्रोकर प्रोटोकॉल्स। |
| `broker/exceptions.py` | **21** | **एक्सेप्शन डेफिनिशन्स**: ब्रोकर और सेशन से संबंधित कस्टम एरर्स। |
| `broker/__init__.py` | **4** | **पैकेज इनिशियलाइज़र**। |
| `market/__init__.py` | **4** | **पैकेज इनिशियलाइज़र**। |

*(नोट: अप्रचलित `broker/footprint_compiler.py` और `market/hot_structures.py` को पूर्णतः डिलीट कर दिया गया है)*

---

## 📊 4. Unified Dashboard (लाइव मॉनिटरिंग और विज़ुअलाइज़ेशन UI — `dashboard/`)

यह हमारा वेब-बेस्ड डार्क-थीम मॉनिटरिंग कॉकपिट है, जो ब्राउज़र में इंजन की हेल्थ और सिग्नल्स दिखाता है:

| फ़ाइल का नाम (File Path) | लाइनें (LOC) | सिमेंटिक ज़िम्मेदारी (Semantic Role) |
| :--- | :---: | :--- |
| `dashboard/cockpit_html.py` | **1,425** | **कॉकपिट UI**: लाइव PnL, एक्टिव पिलर, स्पॉट चार्ट्स, L2 ऑर्डर बुक और कनविक्शन मीटर का मुख्य HTML/JS लेआउट। |
| `dashboard/research_html.py` | **1,370** | **रिसर्च UI**: पोस्ट-ट्रेड एनालिसिस, शैडो ऑब्जर्वर स्टेट्स और कैंडल चार्ट विज़ुअलाइज़ेशन। |
| `dashboard/telemetry_loader.py` | **1,015** | **डेटा लोडर**: इंजन द्वारा लिखे गए JSONL और लाइव स्टेट को पार्स करके UI एंडपॉइंट्स को डेटा फीड करना। |
| `dashboard/dashboard.py` | **365** | **वेब सर्वर**: Flask बैकएंड जो पोर्ट 8080 पर डैशबोर्ड सर्व करता है। |
| `dashboard/__init__.py` | **1** | **पैकेज इनिशियलाइज़र**। |

---

## 🚀 5. Top-Level Core, Config & Runtime Bridge

| फ़ाइल का नाम (File Path) | लाइनें (LOC) | सिमेंटिक ज़िम्मेदारी (Semantic Role) |
| :--- | :---: | :--- |
| `backtest.py` | **882** | **बैकटेस्ट सिमुलेटर**: रिकॉर्डेड हिस्टोरिकल डेटा पर 5 पिलर्स और एग्जीक्यूशन रूल्स को रीप्ले करने वाला बैकटेस्ट इंजन। |
| `main.py` | **822** | **इंजन बूटस्ट्रैप**: PM2 का मुख्य रनटाइम प्रोसेस, ट्रेडिंग कैलेंडर शेड्यूलर, WebSocket इनिशियलाइज़र, और सिग्नल लूप। |
| `ipc/live_state.py` | **107** | **डिस्क IPC ब्रिज**: इंजन की लाइव स्टेट (`aegis_live_state.json`) लिखना ताकि डैशबोर्ड बिना DB के रीयल-टाइम पढ़ सके। |
| `logger.py` | **78** | **सेंट्रलाइज्ड लॉगर**: कलरफुल कंसोल आउटपुट और रोटेटिंग फ़ाइल लॉग्स (`logs/aegis.log`)। |
| `config/settings.py` | **56** | **कॉन्फ़िगरेशन**: थ्रेशोल्ड्स, टाइमआउट्स और 3-ट्रेड डेली कैप (`MAX_TRADES_PER_DAY = 3`) की सेटिंग्स। |
| `config/paths.py` | **20** | **पाथ्स कॉन्फ़िगरेशन**: OS-एग्नॉस्टिक फ़ाइल और डेटा पाथ्स रिज़ॉल्यूशन। |
| `ipc/__init__.py` | **20** | **IPC पैकेज इनिशियलाइज़र**। |
| `symbols.py` | **19** | **सिंगल सोर्स ऑफ ट्रुथ**: इंडेक्स नाम, स्ट्राइक स्टेप्स (NIFTY 50, SENSEX 100) और लॉट साइज़ (NIFTY 65, SENSEX 20)। |
| `config/__init__.py` | **1** | **कॉन्फ़िग पैकेज इनिशियलाइज़र**। |

---

## 🛡️ आर्किटेक्चरल मुख्य सिद्धांत (Core Design Principles)

1. **सिंगल एक्टिव इंजन (Zero-Entropy Focus)**: रिपॉजिटरी में सिर्फ और सिर्फ **AEGIS (v1.0 Locked)** एक्टिव है। लेगेसी बॉट्स (Kronos, Argus) और मोड A पूरी तरह डीकमीशन हैं।
2. **5 Macro Pillars + Priority Mode B Surge**: ट्रेडिंग सिग्नल्स $T:02s+$ इन-फ्लाइट ब्रेकथ्रू पर 5 मैक्रो पिलर्स (Dow 20-EMA, Micro-Sweep, Squeeze, Dynamic S/R, Writer Squeeze) द्वारा उत्पन्न होते हैं।
3. **Zero-Regression Mixin Architecture**: `execution/execution_manager.py` (409 LOC) पाँच सुस्पष्ट Mixin मॉड्यूल्स (`entry_engine`, `trade_monitor`, `exit_chaser`, `trade_persistence`, `slot_and_risk_constants`) को कंपोज़ करता है।
4. **स्केल-इनवेरिएंस (Scale-Invariant Metrics)**: NIFTY (24k) और SENSEX (80k) के सभी थ्रेशोल्ड्स Basis Points ($1\text{ bps} = 0.01\%$) और $\text{ATR}_{1m}$ मल्टीपल्स पर नॉर्मलाइज़्ड हैं।
5. **यूनिवर्सल स्पॉट इनवैलिडेशन**: एग्जिट्स कभी भी ऑप्शन प्रीमियम के फ्लिकर पर निर्भर नहीं होते; जब तक कैश इंडेक्स स्पॉट में एंट्री कैंडल हाई/लो इंटैक्ट है, ट्रेड सुरक्षित रहता है।
6. **स्ट्रिक्ट रिस्क गवर्नेंस**: 3-ट्रेड डेली लिमिट (`MAX_TRADES_PER_DAY = 3`), 0.75s सिंगल-शॉट कटऑफ, +4% मेकर TP (+3% शुक्र), और 15:08 IST CAS फोर्स-फ्लैटन।
