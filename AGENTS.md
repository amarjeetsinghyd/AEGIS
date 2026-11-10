# AI AGENT INSTRUCTIONS — AEGIS repo

> **Read this before making any change in this repository.**
> Applies to every AI coder (Cursor, Antigravity, Devin, deepseek-harness, etc.) and every human contributor.

---

## 💬 USER COMMUNICATION & PRESENTATION PROTOCOL (MANDATORY FOR ALL AI AGENTS)

> **Every AI agent must strictly follow these presentation guidelines when replying to the user (Amarjeet bhai):**
> 1. **Script & Language Mandate (देवनागरी लिपि अनिवार्य)**:
>    - Jawab **hamesha Devnagari script (देवनागरी लिपि)** mein hi dena hai, Devnagari aur English quant/technical terms ko naturally mix karke (जैसे: *"अमरजीत भाई, AEGIS ने Priority Mode B Mid-Bar Breakthrough डिटेक्ट किया है..."*). Pure Roman English ya Roman English script (Hinglish) mein reply nahi dena hai.
> 2. **Readability & Ultra-Clean Presentation (पढ़ने में सबसे आसान)**:
>    - Response ko padhne mein ultra-smooth aur easy banayein: short crisp paragraphs, clear bullet points, intuitive visual badges (🟢, 🔴, ⚡, 🛡️, 🚀), clear bold highlights. Complex dense wall-of-text bilkul nahi banana hai.
> 3. **Generative UI Policy (User-Requested Only)**:
>    - Generative UI / HTML widgets proactively bina maange nahi banane hain. Inline cards ya UI artifacts tabhi banayein jab Amarjeet bhai explicitly unki demand karein.
> 4. **Tables Over Flowcharts Mandate (टेबल्स को हमेशा सर्वोच्च प्राथमिकता — Primary Preference)**:
>    - **Amarjeet bhai ko structured Markdown Tables sabse zyada pasand hain.** Jab bhi kisi logic, comparison, parameters, decision gates, trade setups, ya data states ko samjhana ho, to **hamesha pehle clean, well-formatted Markdown Table** ka hi upyog karein.
>    - Table mein clear column headers, visual emoji badges (🟢, 🔴, ⚡, 🛡️, 🚀, 🎯, 📊), aur crisp bullet descriptions hone chahiye.
> 5. **Flowcharts As Secondary Fallback (फ़्लोचार्ट्स केवल सेकेंडरी विकल्प — Strictly Secondary)**:
>    - **फ़्लोचार्ट्स को हमेशा सेकेंडरी रखें**: Har response mein baar-baar Mermaid flowchart bilkul nahi banana hai. Pehle baat table aur bullet points mein samjhayein.
>    - Mermaid Flowchart ka upyog **sirf tabhi karein** jab user explicitly flowchart maange ya koi aisi complex multi-branch network graph ho jise table mein vyakt karna asambhav ho.
>    - **Agar flowchart use karna hi pade, to in rules ka palan karein**:
>      * **बड़ा और सुस्पष्ट टेक्स्ट (Large & Bold Font)**: फ़्लोचार्ट्स का टेक्स्ट हमेशा बड़ा और बोल्ड होना चाहिए ताकि दूर से आसानी से पढ़ा जा सके (हर `classDef` में `font-size:20px,font-weight:bold` अनिवार्य रूप से जोड़ें)।
>      * **विविध और आकर्षक आकृतियाँ (Diverse & Catchy Shapes — केवल सादे चौकोर बॉक्स नहीं)**:
>        - डेटाबेस/डेटा फीड/वेबसॉकेट: सिलिंडर `id[("...")]`
>        - एक्शन/एक्जीक्यूशन/फ्लो: स्टेडियम / पिल शेप `id(["..."])`
>        - कोर सबसिस्टम/इंजन मॉड्यूल: डबल-बॉर्डर सब-रूटीन `id[["..."]]`
>        - डिसीजन/कंडीशन/गेट: हेक्सागोन `id{{"..."}}` या डायमंड `id{"..."}`
>        - ट्रिगर/सिग्नल इवेंट: फ्लैग / टैग `id>"..."]` या पैरेललोग्राम `id[/"..."/]`
>        - अंतिम आउटकम/टारगेट: बड़ा सर्कल `id(("..."))` या डबल सर्कल `id((("...")))`
>      * **वाइब्रेंट कलर्स & विजुअल बैजेस**:
>        - कभी भी फीका, डल या सादा ग्रे बैकग्राउंड वाला फ़्लोचार्ट नहीं बनाना है।
>        - कलर्स: सक्सेस/बाय `fill:#064e3b,stroke:#10b981,stroke-width:3px,color:#fff,font-size:20px,font-weight:bold`, रिजेक्शन/ड्रॉप `fill:#7f1d1d,stroke:#f87171,stroke-width:3px,color:#fff,font-size:20px,font-weight:bold`, डिसीजन `fill:#1e3a8a,stroke:#60a5fa,stroke-width:3px,color:#fff,font-size:20px,font-weight:bold`, डेटा/फीड `fill:#3b0764,stroke:#c084fc,stroke-width:3px,color:#fff,font-size:20px,font-weight:bold`, वॉच/चेक `fill:#78350f,stroke:#fbbf24,stroke-width:3px,color:#fff,font-size:20px,font-weight:bold`।
>        - हर नोड में विजुअल बैजेस (🟢, 🔴, ⚡, 🛡️, 🚀, 🎯, 📊, 📡) का उपयोग करें।

---

## 🚫 DECOMMISSIONED ENGINES — DO NOT REVIVE
* **`kronos_bot` & `ARGUS/` are PERMANENTLY DECOMMISSIONED**:
  - Never re-create `kronos_bot` packages, stubs, configs, or dashboard references.
  - Dormant fallback branches in `common/` are kept as safe fallbacks; leave them untouched.
  - Historical code is preserved read-only in `_archive/`.

---

## 🚀 ACTIVE ENGINE: AEGIS (Production v1.0 Locked)

* **Single Source of Truth (Canonical Strategy Specs)**:
  - **Strategy & Rules Blueprint**: [`docs/5_PILLARS_MASTER_BLUEPRINT.md`](file:///c:/Users/Amarjeet%20Singh/quant-bot/docs/5_PILLARS_MASTER_BLUEPRINT.md)
  - **Architecture Authority**: [`docs/AEGIS_V1.0_ARCHITECTURE.md`](file:///c:/Users/Amarjeet%20Singh/quant-bot/docs/AEGIS_V1.0_ARCHITECTURE.md)
* **Core Trading Invariants**:
  - **Strategy**: 5 Macro Price Action Pillars + Priority Mode B Mid-Bar Breakthrough (`T:02s` onwards).
  - **Order Execution**: Strict 1-lot Maker TP (`+4.0%` Mon–Thu, `+3.0%` Fri), Universal Spot Invalidation, Atomic `0.75s` fill window, 0 order chasing.
  - **Risk Caps**: Strict 3 trades/day (`MAX_TRADES_PER_DAY = 3`), Hard Disaster Floor (`-8.0%` option premium), 15:08:00 IST CAS force flatten.
  - **Trading Schedule**:
    - **Mon (1 DTE) & Tue (0 DTE)**: NIFTY (strike step 50, lot 65)
    - **Wed (1 DTE) & Thu (0 DTE)**: SENSEX (strike step 100, lot 20)
    - **Fri (4 DTE)**: Deep Sleep Mode by default (`is_trading_day = False`).
    - **Sat & Sun**: Market Closed (Shoonya WebSocket disconnect is normal).
* **Testing Phase Discipline**:
  - Validate pipeline health using current available account funds only (do NOT top up live or paper funds).
  - **Scale-Invariance Mandatory**: All spot distances and thresholds must be normalized via Basis Points (bps) or percentage of spot. Never hardcode fixed points.

---

## 🧠 MANDATORY FIRST-PRINCIPLES QUANT REASONING PROTOCOL (FOR AI AGENTS)

Before writing or modifying ANY strategy, gatekeeper, or execution code, every AI agent MUST perform and briefly state a **5-Point Pre-Flight Quant Audit**:
1. **Unit & Lifecycle Parity (No Apples-to-Oranges)**: Verify exact units between forming bars (`_curr_1m_bar`) and closed bars (`bars_1m`). Remember: `_curr_1m_bar.volume` counts spot ticks during the minute and only becomes `opt_total_vol` at `_on_1m_bar_close`. Always use `self._opt_flow_1m` for in-flight option volume!
2. **Deque Saturation & Indexing Safety**: `self.bars_1m` has `maxlen=400` and is full at 09:14 AM after seeding. `len(self.bars_1m)` stays constant at `400`. NEVER use `len(self.bars_1m)` for bar age or time elapsed—always use monotonic `self._total_bars_seen` (`seq_idx`) or epoch timestamps (`ts_start`).
3. **End-to-End Pipeline Trace**: A trigger in `price_action_engine.py` must survive downstream gates. Always trace how a new/modified signal flows through:
   `PriceActionEngine` -> `WyckoffVolumeEngine` (fast-path needed for intra-bar?) -> `CompositeConvictionEngine` (score >= 80?) -> `AegisExecutionManager` (Last-Mile Hygiene & Slot Rollback).
4. **Scale-Invariance & Synthetic Test Compatibility**:
   - Never hardcode point values (NIFTY is 24k, SENSEX is 74k). Use `k * ATR_1m` or `bps` (`0.0008 * Spot`).
   - Unit tests often feed synthetic OHLC bars with `opt_total_vol == 0`. All option-flow/delta vetoes must gracefully bypass when `opt_total_vol <= 0`.
5. **Strict Pytest Frugality**:
   - `.md` docs, comments, log analysis -> **Run 0 tests**.
   - Single module edit -> **Run ONLY that module's targeted test file** (takes ~2s).
   - Full test suite -> **ONLY on multi-module core changes or pre-deployment**.

---

## CODING DISCIPLINE (applies to all active Python)
- **Pytest Execution Governance (Smart, Targeted, and Frugal)**:
  - **NEVER run pytest for non-engine tasks**: Do NOT run pytest when editing documentation (`.md`), writing or running analysis scripts (`scratch/`, `scripts/`), modifying data/JSON files, editing UI/HTML artifacts, or answering user questions/inspecting logs. Running tests for these wastes user time and burns tokens unnecessarily.
  - **Single-Module Edits $\implies$ Targeted Tests ONLY**: If you edit a single module, run ONLY its corresponding test file (e.g., `python -m pytest tests/test_shadow_boundary_observer.py -q`). It finishes in 2 seconds. Do NOT run the entire suite.
  - **Full Test Suite (`python -m pytest tests\ -q`) $\implies$ Core Multi-Module Changes ONLY**: Run the full suite only when refactoring core infrastructure (`execution/`, `market/`), modifying multi-module interfaces, or before a major production deployment.
  - **Suite Cleanliness**: The test suite must remain green when tests are appropriately run. As of 2026-08-16, Kronos is decommissioned; the obsolete `test_kronos_settings_use_kronos_package_paths` has been removed. If a kronos-related test failure ever appears, remove the obsolete reference—do NOT revive Kronos code.
- Hot-path modules (`market/tick_compiler.py`, `on_tick` handlers) must not
  import `pandas`/`scipy`/`sklearn`. Heavy libs are allowed only off-hot-path
  (e.g. `l2_options_archiver.py` flush).
- **Scale-Invariance & Normalization Mandatory**: NEVER hardcode fixed point thresholds
  (e.g. `15.0 pts`, `8.0 pts`) for index calculations. NIFTY (24k), SENSEX (80k),
  BANKNIFTY (51k), and FINNIFTY (23k) operate on radically different point scales. ALL
  spot distances, triggers, invalidations, and confirmation thresholds must be
  normalized via Basis Points (bps, where 1 bps = 0.01% of spot), percentage of spot,
  or localized volatility multiples ($k \times \text{ATR}_{1m}$).
- Single source of truth for symbols: `symbols.py` (`INDEX_SPECS`, `ACTIVE_CONFIG`). Do not hardcode symbol lists elsewhere.
- Never modify Shoonya/Noren API assumptions lightly; see harness doc §1.1.1
  (weekday-only, static IP, single session, ~24h expiry). See §SHOONYA below for the
  weekend rule.

---

## ⚡ VERSION CONTROL & CHANGE TRACKING (JUJUTSU / JJ PROTOCOL MANDATORY)

> **Jujutsu (`jj`) is the STRICT and ONLY version control CLI for all AI agents and contributors in this repo.**
> Never use `git add` or `git commit`. All operations must use `jj`.

### 1. Pure Automatic Snapshots (No Manual Commits During Work)
- **Continuous Background Auto-Snapshotting**: Jujutsu automatically snapshots every single file modification directly into the current working copy (`@`).
- **Never Run Commit Commands While Working**: Do NOT run `jj commit` or any commit command after editing files. Work freely—Jujutsu captures all edits automatically without any ceremony.
- **No Staging / No Staging Area**: Concept of staging (`git add`) does not exist in Jujutsu. Your workspace is always clean and tracked.

### 2. Pushing Completed Work to GitHub Remote (`jj git push`)
- Only when a feature or task is completely finished, tested, and ready to send to GitHub remote:
  ```powershell
  jj describe -m "feat(AEGIS): <concise summary>"
  jj bookmark set main -r '@'
  jj git push
  ```

### 3. Instant Rollback & History Audit (`jj undo`)
- **Instant Rollback**: If an operation creates unintended changes or breaks a test, execute:
  ```powershell
  jj undo
  ```
- **Operation History**: Inspect operational timeline with:
  ```powershell
  jj op log
  ```

---

## SHOONYA / NORENAPI RULES (broker seam)

- **Mandatory 90-Day Password Rotation Schedule**:
  - Shoonya enforces an involuntary password reset every 90 days.
  - **Last Rotated Date**: `08 October 2026` (Active: `Freedom@2026`).
  - **Next Mandatory Rotation Deadline**: `31 December 2026`.
  - **Human Ownership**: The user maintains a personal alarm/reminder and rotates this manually via the Shoonya web portal. AI agents must NEVER attempt automated credential rotation, scraping, or brute-force logins.
  - **When user rotates password**: Update **ONLY** the single canonical root `.env` (`c:\Users\Amarjeet Singh\aegis\.env` locally and `/home/opc/.env` on VPS). Clear cached session files (`data/shoonya_session.json` and `tokens_cache.json`) to trigger automatic fresh OAuth login.
- **Single Root `.env` Architecture (Subdirectory `.env` Strictly Prohibited)**:
  - There is exactly **ONE** `.env` file per environment:
    - Local: `c:\Users\Amarjeet Singh\aegis\.env`
    - VPS: `/home/opc/.env` (and repo root `/home/opc/aegis/.env`)
  - **Strict Prohibition**: Subdirectory `.env` files are **STRICTLY PROHIBITED**.
  - **Root Cause & Rationale**: All settings load solely from repo root / `/home/opc/.env`.
- **Weekend offline is normal.** The Shoonya WebSocket drops on Saturdays/Sundays and
  NSE holidays. A Sat/Sun 'no tick' or WS close is **not a bug** — do not file it,
  add alerts for it, or build weekday-specific reconnect logic for it. Reconnect /
  exponential backoff only matters on a *trading* day (see engine harness docs).
- **Static egress IP mandatory** on the VPS.
- **Single session / single credential set**; tokens expire in ~24h; refresh
  `tokens_cache.json` weekly.
- **No paper/sim mode** — noise-test off live ticks or history, not simulation.
- **History burst** ~10 req/s; websocket carries L1 + top-5-level L2 depth only.

---

## VPS & OPS ACCESS (LEAN PRODUCTION DEPLOYMENT MANDATE)

> **VPS Lean Deployment Rule**: ONLY active, production-critical runtime files are allowed on the VPS. All tests, test data, scratch experiments, historical archives, and forensics documentation MUST stay strictly on the local development machine. Never deploy or leave non-runtime files on the VPS.

- **Host:** `137.23.41.38` (Oracle Cloud, Ubuntu). **User:** `opc`. Repo on VPS: `/home/opc/aegis`.
- **Allowed on VPS (Runtime Only)**:
  - `core/` (pure price action, Wyckoff volume, composite conviction)
  - `execution/` (order lifecycle, strike selector, capital engine, trade context)
  - `broker/` (shoonya adapter, session manager, rate limiter)
  - `market/` (tick compiler, market calendar, instrument registry)
  - `dashboard/` (monitoring UI)
  - `config/` (paths, settings)
  - `ipc/` (live state cross-process bridge)
  - `main.py`, `backtest.py`, `logger.py`, `symbols.py`
  - `ecosystem.config.js` (PM2 process manifest)
  - `.env` (root credentials only — never in subdirectories)
- **Forbidden on VPS (Local-Only)**:
  - `tests/` (unit tests run locally before deployment)
  - `scratch/` (transient workspace — must remain 100% empty on VPS)
  - `*_archive/` or any `.tar.gz` backup dumps (zero archives on VPS)
  - Heavy ML datasets, raw parquets, or forensic notebooks
- **SSH key location (local):** repo-root `Oracle SSH keys/ssh-key-2026-07-11.key`. It is the ONLY copy — never replicate it into an engine subfolder.
- **SSH command:** `ssh -i "Oracle SSH keys/ssh-key-2026-07-11.key" opc@137.23.41.38`
- **Process manager:** PM2, config `ecosystem.config.js` — active apps: `aegis-bot`, `aegis-dashboard`.

---

## REPOSITORY LAYOUT & ZERO-ENTROPY HYGIENE ENFORCEMENT

> **Zero-Entropy Mandate**: The codebase must remain completely free of transient clutter, scratch files, one-shot diagnostics, and virtual environment leakage. Only core production code, validated unit tests, and canonical configs are committed.

Rules every AI agent and contributor must uphold:

- **Immediate Scratch & Temp Cleanup**: Any diagnostic or scratch script created in `scratch/` or `scripts/` MUST be deleted immediately after its purpose is served. Never leave temporary scripts (`check_*.py`, `patch_*.py`, `test_*.json`, `temp_*`) behind.
- **No Virtual Environment or Binary Package Tracking**: `venv/`, `.venv/`, `.pytest_cache/`, `*.pyc`, and `.coverage` are permanently ignored via `.gitignore` and must NEVER enter Jujutsu or Git tracking.
- **No Temp / Scratch Files at Repo Root**: Scratch goes in `scratch/` (which is gitignored). Never place `temp.json`, `scratch_state.json`, or ad-hoc log files at repo root.
- **No One-Shot Debug Scripts in `scripts/`**: `scripts/` is reserved ONLY for permanent operational tools (e.g. `trace_trade_pipeline.py`). Ad-hoc patch scripts must be executed in `scratch/` and promptly deleted.
- **Pristine Jujutsu Working Copy**: Before concluding a session, verify that `jj st` shows a zero-entropy state (no stray files, no untracked noise).
- **SSH key only at repo root** (`Oracle SSH keys/`), never inside a subfolder.
- **No nested `.env` files.** Credentials live ONLY in the root `.env` (locally) or `/home/opc/.env` (VPS).
- **Logs live in root `logs/`** (e.g. `logs/aegis.log`), never loose at root or scattered.

---

## 🛠️ QUANT-BOT DEVELOPER & DIAGNOSTIC TOOLBOX (PROFILING, DE-BLOATING & ASYNC AUDITING)

> **Mandatory Tool Usage for AI Agents and Contributors**:
> Do NOT guess, read thousands of lines manually, or speculate when debugging or de-bloating. Always use these specialized tools to inspect live processes, eliminate dead code, trace async tick latency, profile performance, and map execution flows.

### 🤖 AUTOMATED AGENT WORKFLOW: HOW TO USE THESE TOOLS TO EXPLAIN CODE & TRACE FLOWS (MANDATORY)

> **Mandate for All AI Agents**: Whenever the user (Amarjeet bhai) asks:
> *"यह कोड कैसे काम करता है?"*, *"इस पिलर का एंड-टू-एंड डेटा फ्लो बताओ"*, *"इस बग का कारण क्या है?"*, या *"इसमें कौन-कौन से गेट्स चेक होते हैं?"*:
> **Do NOT read thousands of lines manually** (burns context tokens and risks hallucinations).
> **Follow this exact automated diagnostic protocol**:
>
> 1. **To Trace Logic & Execution Flow (Pillars, Gates, Breakouts)** $\implies$ **`python-hunter`**:
>    - Run a targeted trace in `scratch/` or inline using `hunter.trace(Q(module_startswith="AEGIS.core", action=CallPrinter))`.
>    - In 0.05 seconds, it extracts the exact execution path, parameters passed, internal branch decisions, and return values.
>    - Present the verified logic trace to the user with exact line numbers and variables.
>
> 2. **To Map Function Callers, Callees & Semantic Hierarchy** $\implies$ **`SEMANTIC_MAP.md` & AST Graph**:
>    - Query caller/callee relationships to find which modules invoke or depend on a function without scanning whole packages.
>
> 3. **To Explain Latency, Bottlenecks & Execution Timing** $\implies$ **`viztracer`**:
>    - Record execution with `python -m viztracer --log_async -o trace.json <script>`.
>    - Inspect microsecond elapsed time per gate and deliver concrete empirical latency numbers.
>
> 4. **To Inspect Stuck or Slow Live Engine on VPS** $\implies$ **`py-spy`**:
>    - Run `sudo /home/opc/.local/bin/py-spy dump --pid <PID>` via SSH to get an instant live thread snapshot without pausing the process.
>
> 5. **To Find Dead Code or Zombie Logic** $\implies$ **`vulture` + `ruff` + `coverage --branch`**:
>    - Run `vulture` for dead functions, `ruff` for unused imports/variables, and `coverage` branch tracking for dead code islands.

---

### 1. `py-spy` — Zero-Overhead Live Process Profiler & Stuck-Process Inspector
* **Location on VPS**: `/home/opc/.local/bin/py-spy` (Installed & Verified v0.4.3).
* **When to use**: Inspecting the live running `aegis-bot` on VPS without stopping, restarting, or slowing down the process.
* **Key Commands**:
  - **Thread Stack Dump** (Instant snapshot of all active threads and exact line numbers):
    ```bash
    sudo /home/opc/.local/bin/py-spy dump --pid $(pgrep -f aegis_main.py | head -n 1)
    ```
  - **Live Function-Wise CPU Monitor** (htop-style real-time profile of hot functions):
    ```bash
    sudo /home/opc/.local/bin/py-spy top --pid <PID>
    ```
  - **Flame Graph Recording** (Generates interactive SVG flamegraph over N seconds):
    ```bash
    sudo /home/opc/.local/bin/py-spy record -o /home/opc/aegis_flame.svg --pid <PID> --duration 30
    ```

### 2. `vulture` — Dead & Obsolete Code Hunter (Static De-bloating Weapon)
* **Status**: Installed locally and on VPS (`vulture 2.16`).
* **When to use**: Scanning the codebase for dead functions, unused methods, orphaned classes, and dead variables to keep the project ultra-lean and zero-entropy.
* **Key Commands**:
  - **Core Engine High-Confidence Dead Code Scan**:
    ```powershell
    python -m vulture core/ execution/ broker/ market/ --min-confidence 80
    ```
  - **Full AEGIS Package Audit**:
    ```powershell
    python -m vulture core/ execution/ broker/ market/ dashboard/ config/ ipc/ main.py --min-confidence 85
    ```

### 3. `ruff` — Ultra-Fast AST Linter & Dead Import / Variable Eliminator
* **Status**: Installed locally (`ruff 0.15.20`).
* **When to use**: Instant automated detection and cleanup of unused imports (`F401`) and unused variables (`F841`) across hot-path modules in milliseconds.
* **Key Commands**:
  - **Detect Unused Imports and Dead Variables**:
    ```powershell
    python -m ruff check core/ execution/ --select F401,F841
    ```
  - **Safe Auto-Fix (Remove unused imports automatically)**:
    ```powershell
    python -m ruff check core/ execution/ --select F401 --fix
    ```

### 4. Asyncio Event Loop Latency & Blocking Detector (Built-in + `PYTHONASYNCIODEBUG=1`)
* **When to use**: Catching synchronous functions, heavy math, or disk operations that block the asyncio event loop for $> 20\text{ms}$ (preventing tick drops and delayed fills).
* **Key Usage**:
  - **In Code (Event Loop Debug Config)**:
    ```python
    loop.set_debug(True)
    loop.slow_callback_duration = 0.020  # Log warning on any callback taking > 20ms
    ```
  - **Environment Variable Flag**:
    ```powershell
    $env:PYTHONASYNCIODEBUG="1"; python main.py
    ```

### 5. `viztracer` — Microsecond Timeline & Async Call Graph Visualizer
* **Status**: Installed locally (`viztracer 1.1.1`).
* **When to use**: Recording microsecond-precision timelines of synthetic simulations or tick replays, viewing async task lifecycles in Google Chrome Perfetto (`ui.perfetto.dev`).
* **Key Commands**:
  - **Record Async Simulation Trace**:
    ```powershell
    python -m viztracer --log_async -o trace.json scratch/simulate_p1_pipeline.py
    ```
  - **Open Visual Timeline in Browser**:
    ```powershell
    vizviewer trace.json
    ```

### 6. `python-hunter` — Conditional Runtime Call Tracer (Logic X-Ray)
* **Status**: Installed locally and on VPS (`hunter 3.9.0`).
* **When to use**: Tracing the exact execution path, inputs, and outputs across pillars in 0.1s without manually opening or reading 3,000-line files.
* **Key Usage**:
  - **Targeted Pillar Call Trace**:
    ```python
    import hunter
    from hunter import Q, CallPrinter
    hunter.trace(Q(module_startswith="core.", action=CallPrinter))
    ```

### 7. `coverage.py` with Branch Tracking — Dead Island & Zombie Code Hunter
* **Status**: Installed locally and on VPS (`coverage 7.17.2`).
* **When to use**: Detecting genuine **Zombie Code** that escapes `vulture` (e.g. dead islands where dead functions call dead functions, or unused dynamic mixin paths).
* **Key Commands**:
  - **Run Full Branch Coverage across Production Tests / Tick Replays**:
    ```powershell
    python -m coverage run --branch --source=core,execution -m pytest tests/ -q
    ```
  - **Generate Itemized Zero-Hit Zombie Report**:
    ```powershell
    python -m coverage report --include="core/*,execution/*"
    ```

### 8. `hypothesis` — Property-Based Market Fuzzer (Edge-Case Bombardment)
* **Status**: Installed locally and on VPS (`hypothesis 6.170.15`).
* **When to use**: Fuzzing the 5 pillars against 1,000+ extreme synthetic market edge cases (Zero-spread doji, NaN depth, 0 ATR, 50 ticks/ms burst).

---

## WHEN IN DOUBT
- Trust the harness doc (v3.5) over older docs; older docs can be deleted when stale.
- Kronos is dead. If a change re-introduces Kronos, it is wrong by definition.
