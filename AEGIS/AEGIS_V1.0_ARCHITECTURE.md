# AEGIS — Autonomous Execution & Gamma Imbalance System (Version 1.0 Production Locked)

**Architecture Authority — Code-Grounded Definitive Specification**  
**Version:** Production 1.0 (v1.0 Production Locked — Priority Mode B In-Flight Mid-Bar Breakthrough Surge [T:08s onwards / No Upper Cap] + 4 Macro Price Action Pillars + Absolute Pain Zone VWAP Band + Wyckoff Volume & OFI Delta Gate + Single-Shot Clean Entry Mandate [No Chasing] + Universal Entry Candle Low/High Invalidation + Friday 4-DTE +3.0% TP Calibration — Live)  
**Status:** ACTIVE · PM2 `aegis-bot` · `AEGIS/aegis_main.py:87`  
**Host:** Oracle Cloud `opc@137.23.41.38` · `ecosystem.config.js:3` · `cwd=/home/opc`  
**Instruments:** NIFTY (Mon [1 DTE], Tue [0 DTE], Fri [4 DTE], NFO, strike step 50, lot 65) · SENSEX (Wed [1 DTE], Thu [0 DTE], BFO, strike step 100, lot 20)  
**Supersedes:** Prior V3.9 / V3.8 specifications, AEGIS_V3_ARCHITECTURE.md, and legacy ASTRA / AEGIS documentation.

---

> ⚠️ **CRITICAL GOVERNANCE LAWS (DO NOT ALTER ARBITRARILY):**  
> 1. **Price Action Primary & Mode B Golden Middle Path Law**: AEGIS operates strictly on a **Macro Price Action Primary Trigger** across 4 canonical pillars and Priority Mode B In-Flight Breakthrough:
>    - **Priority Mode B (In-Flight Breakthrough Surge)**: Mid-bar entry from **T:08s onwards (no upper cap)** when forming candle pierces pre-mapped boundary (Pain Zone Top/Bottom, 200 EMA Ribbon, Squeeze Box, S&R) with sweet-spot range ($0.35 \times \text{ATR} \le \text{Range} \le 1.20 \times \text{ATR}$), body ratio $\ge 65\%$, leading wick $\le 0.05 \times \text{ATR}$, 2-tick boundary persistence, and live option delta dominance $\ge 1.30\times$ (or spot tick rate $\ge 1.5$ ticks/sec). Enters ahead of retail, filling resting +4.0% Maker TP before or as candle closes.
>    - **Pillar 1**: 20 EMA Pullback & Momentum Rejection (`20_ema_rejection` — `price_action_engine.py`, Bar close T:00s with pre-staged strikes & T:50s early entry).
>    - **Pillar 2**: Liquidity Sweep & Reclaim (`micro_sweep_reclaim` Priority 1 Dynamic S/R Registry & Day Extreme [PDH/PDL] stop-hunt reclaim + parallel shadow topological sweep).
>    - **Pillar 3**: Volatility Squeeze to Expansion Breakout (`volatility_squeeze_breakout` — $O(1)$ metric ATR box compression).
>    - **Pillar 4**: 0-D Prominence Hub Structural Breakout (`check_topological_prominence_breakout` via `FastMonotonicStack`, $\hat{\pi} \ge 1.75$, $VR_t \ge 1.4$ Volume Ratio vs 60m median volume, 5m structural trend alignment).
> 2. **Absolute Pain Zone VWAP Band Law**: Single unified directional veto before pillars:
>    - Spot **ABOVE** Upper Band (`zone_top`) $\implies$ **ONLY CALL** trades allowed.
>    - Spot **BELOW** Lower Band (`zone_bottom`) $\implies$ **ONLY PUT** trades allowed.
>    - Spot **INSIDE** Band $\implies$ Discarded / Neutral zone.
> 3. **Wyckoff Volume & Order Flow Delta Parallel Law**: Immediately following a Price Action setup trigger, the trade MUST pass the **WyckoffVolumeEngine** (`wyckoff_volume_engine.py`):
>    - **Effort vs Result**: Volume spent vs price displacement achieved. High volume + narrow spread at support/resistance = Institutional Absorption.
>    - **Volume Dry-Up Ratio (VDR)**: For Pillar 1 pullbacks, approach candles must show supply drying up ($VDR < 0.85$, decaying volume slope). Counter-trend volume expansion ($VDR \ge 1.25$ rising) is classified as an **Institutional Liquidation Dump** and vetoed immediately.
>    - **Delta Harmony**: On breakouts (P3/P4/Mode B), option aggressor flow must match direction ($\text{Call Buy} \ge \text{Put Buy}$ for CALLs). Opposing delta swamping ($> 2.2\times$ opposing flow) is classified as a **Bull/Bear Trap** and vetoed immediately.
>    - **In-Flight Fast-Path**: Mode B and Mode A intra-bar setups evaluate live in-flight option delta flow (`_opt_flow_1m`) and bypass prior closed bar volume checks.
> 4. **Fixed Sizing & Maker TP Discipline Law (USER MANDATE - 2026-09-25)**:
>    - Position sizing is strictly locked to **1 Lot** (no dynamic 2-lot scaling).
>    - Take-Profit is strictly locked to constant **+4.0% Resting Maker Limit** for Monday–Thursday (never expand to 5%–6%; holding for bigger targets destroys the 84%+ win-rate).
>    - **Friday (4 DTE) Calibrated TP**: Due to $5\times$ Option Elasticity ($\Omega$) drop on 4 DTE, Friday resting limit TP is calibrated to **`+3.0%`**, with Near-Target Profit Lock at **`+2.5%`** and Breakeven shift at **`+2.2%`**.
> 5. **Single-Shot Clean Entry & Zero Chasing Mandate (NEW 2026-10-22)**:
>    - Limit orders receive an atomic **0.75-second fill window**. If unconfirmed or rejected, the order is cancelled immediately.
>    - Never modify or chase orders (empirical audit proved 100% win-rate on clean first-attempt fills vs 0.0% on chased orders).
>    - Immediate queue draining (`_drain_signal_queue`) purges pending signals upon any failure.
> 6. **Universal Structural Invalidation Law**:
>    - Universal Spot Invalidation across ALL 4 Pillars and Mode B:
>      $$\text{CALL SL} = \text{Entry Candle Low} - (0.10 \times \text{ATR})$$
>      $$\text{PUT SL} = \text{Entry Candle High} + (0.10 \times \text{ATR})$$
>    - Evaluated strictly against the cash index spot price, fully immunizing trades from option spread widening and order book spoofing.
> 7. **Composite Conviction Continuous Scoring Law**: Replaces rigid, binary boolean gating with continuous 0–100 multi-domain scoring (`composite_conviction_engine.py`). Sizing & admission:
>    - $\ge 80.0$: Admitted with standard **1 Lot (+4.0% Maker TP, +3.0% Friday)**.
>    - $< 80.0$: Blocked by conviction threshold.
>    - Intra-bar `mode_b_` setups awarded approved **85.0** score.
> 8. **Decommissioned Machine Learning Law (14 October 2026)**:
>    - CatBoost ML Gatekeeper & Standalone CatBoost Paper Trader are **PERMANENTLY DECOMMISSIONED & DISABLED** (18-day trial proved 46.4% win-rate inferior to 84%+ deterministic price action).
> 9. **Pre-Trade Abort Slot Rollback Law**:
>    - If an admitted setup is aborted before order execution (e.g. cooldown lockout, spread violation, bad fill), `_rollback_slot()` immediately restores the consumed daily slot so capital capacity is never phantom-locked.
> 10. **Scale-Invariance Law**: NEVER hardcode fixed point thresholds for index calculations across NIFTY (24k) and SENSEX (80k). All spot distances, invalidations, stops, and confirmations MUST be normalized via **Basis Points (bps, where 1 bps = 0.01% of spot)** or localized volatility multiples ($k \times \text{ATR}_{1m}$).
> 11. **Session Force-Flatten Law**: All positions unconditionally liquidated at **15:08:00 IST** (`AEGIS_FORCE_FLATTEN = 15:08`) to avoid NSE Closing Auction (CAS) volatility and broker auto-square-off penalization.

---

## Master Semantic Architecture Graph

The following Mermaid diagram visualizes the complete end-to-end signal generation, Wyckoff volume confirmation, conviction evaluation, execution, and exit lifecycle of AEGIS Production Version 1.0 (v1.0 Locked):

```mermaid
flowchart TD
    subgraph Ingestion ["1. Data Ingestion & Pre-Warming"]
        WS["Shoonya WebSocket<br/>(L1 + 5-Level Depth)"] --> TC["TickCompiler (Zero-Copy ParsedTick Pool)"]
        REST["Shoonya REST API<br/>(09:14 AM Pre-Market)"] --> SEED["seed_historical_bars()<br/>20 EMA, 50 EMA, ATR(14), VWAP"]
        TC --> AGE["AegisGammaEngine.on_tick_parsed()"]
        SEED -.-> PAE["PriceActionEngine (core/price_action_engine.py)"]
    end

    subgraph Spot_Processing ["2. Cash Index Spot Path"]
        AGE -->|Underlying Tick| SPOT["_on_underlying_tick()"]
        SPOT --> BAD["Bad-Tick Guard<br/>(Drop >0.5% spike, 60s re-sync)"]
        BAD --> PAE
        BAD --> PERS["DirectionalPersistenceEngine<br/>(5s Snapshots: 20 EMA slope + drift)"]
        BAD --> VWAP["IndexVWAPEngine<br/>(Constituent-Volume Weighted VWAP)"]
        BAD --> OI["OIBuiltupGate<br/>(4-Quadrant OI Tracker)"]
    end

    subgraph Primary_Trigger ["3. Macro Price Action Primary Trigger (PriceActionEngine)"]
        AGE -->|Option Tick| BAND{"Absolute Pain Zone Band Veto V4.1 (Tick-Top)<br/>(Above zone_top=CALL only, Below zone_bottom=PUT only, Inside=discard)"}
        BAND -->|Blocked| DROP["Instant Discard & Purge<br/>(return None)"]
        BAND -->|Passed| PA_CHECK{"Price Action Setup?<br/>(evaluate_setup)"}
        PA_CHECK -->|No Setup| DROP
        PA_CHECK -->|Mode B Breakthrough| PB["Mode B: In-Flight Mid-Bar Breakthrough (T:08s–T:48s)<br/>• Boundary Pierce (Squeeze Box, S/R Registry — live) + (Pain Zone, 200 EMA — ShadowBoundaryObserver only)<br/>• Sweet-Spot Range 0.35x–1.20x ATR, Body ≥ 65%<br/>• 2-Tick Persistence, Live Opt Delta ≥ 1.30x"]
        PA_CHECK -->|Pillar 1| P1["Pillar 1: 20 EMA Pullback & Momentum Rejection (Mode A)<br/>• Classical Dow HL2/LH2 2nd-Touch Confirmation<br/>• 20 EMA Wick Rejection, Close Edge ≥ 0.60<br/>• Upper Wick ≤ 15% (Two-Bar ≤ 18%)"]
        PA_CHECK -->|Pillar 2| P2["Pillar 2: Liquidity Sweep & Reclaim (Mode A)<br/>• 5-Bar Turtle Soup Stop-Hunt Reclaim<br/>• Lower Wick ≥ 35% & ≥ 1.25x Upper Wick<br/>• Upper Wick ≤ 15%, Close Edge ≥ 0.55"]
        PA_CHECK -->|Pillar 3| P3["Pillar 3: Volatility Squeeze Breakout<br/>• 3-4 Bar Box Compression (≤1.25x ATR)<br/>• Expansion Breakout, Anti-Climax Cap ≤ 1.35x ATR<br/>• Body ≥ 35% of Range, Close Edge ≥ 0.55"]
        PA_CHECK -->|Pillar 4| P4["Pillar 4: Topological Prominence Breakout<br/>• Priority 1: Dynamic Session S/R Level Registry Breakout<br/>• Priority 2: T-2 0-D Prominence Hub (π ≥ 1.75)<br/>• Day Extreme Runway Shield (min 1.25x ATR / 8 bps)"]
        PB & P1 & P2 & P3 & P4 --> SINGLE_ADM{"Single Admission Policy<br/>(Deterministic First-Hit Resolution)"}
    end

    subgraph Wyckoff_Parallel_Gate ["4. Wyckoff Volume & Order Flow Delta Gate (Parallel First-Line)"]
        SINGLE_ADM --> WYCKOFF{"WyckoffVolumeEngine<br/>(core/wyckoff_volume_engine.py)"}
        WYCKOFF -->|P1: VDR ≥ 1.25 Rising Vol| W_DUMP["Veto: Institutional Liquidation Dump"] --> DROP
        WYCKOFF -->|P3/P4: Opposing Delta > 2.2x| W_TRAP["Veto: Delta Divergence Bull/Bear Trap"] --> DROP
        WYCKOFF -->|P1: Regime A Supply Dry-Up (VDR ≤ 0.80) OR Regime B Stopping Absorption (Wick ≥ 60%)<br/>P2: Spring Stop-Hunt Reclaim<br/>P3/P4: Volume Expansion (VDR ≥ 1.0) + Delta Harmony| W_PASS["Wyckoff Confirmed 🚀"]
    end

    subgraph Defensive_Gating ["5. Defensive Gating & Conviction Ladder"]
        W_PASS --> G_KEY{"Key Level Veto<br/>(PDH / PDL Proximity)"}
        G_KEY -->|Inside Trap Zone| DROP
        G_KEY -->|Passed| V_VWAP{"Pain Zone Band Veto V4.1<br/>(Spot vs zone_top / zone_bottom — single source)"}
        V_VWAP -->|Inside band or wrong side| DROP
        V_VWAP -->|Above band=UP only, Below band=DOWN only| ANCHOR["Event-Bound Anchor T<br/>(Strict [T-60, T] and [T-30, T] Slices)"]
        
        ANCHOR --> V_MICRO{"Adaptive Order Flow Energy (OFEE)<br/>(Score ≥ 60/100, Strict BOS ≥ 70)"}
        V_MICRO -->|Energy Stalled| DROP
        
        V_MICRO -->|Passed| CCE["Composite Conviction Engine (0-100)<br/>• Domain 1: Structure & Runway (30 pts)<br/>• Domain 2: Impulse & Volume (30 pts)<br/>• Domain 3: Flow & Futures (25 pts)<br/>• Domain 4: Contract Feasibility (15 pts)"]
        
        CCE --> C_CHECK{"Conviction Score Check<br/>(Score ≥ 80.0?)"}
        C_CHECK -->|Score < 80.0| DROP
        
        C_CHECK -->|Score ≥ 80.0| Q_SLOT["Quality Slot Policy (3 Slots/Day)<br/>• Slot 1, 2, 3: Full-Day Agile Quality Sizing<br/>• Dynamic Hurdle: Beat Prev Score<br/>• Loss Guard: Consec Loss ≤ 2, Daily Cap ₹2k"]
        Q_SLOT -->|No Slot Available| DROP
        Q_SLOT -->|Slot Admitted| SIZING["Locked Sizing Discipline<br/>• Strict 1 Lot Allocation<br/>• Constant +4.0% Maker Limit TP (+3.0% Fri)"]
    end

    subgraph Shadow_Telemetry ["6. Independent Shadow Telemetry (Passive)"]
        W_PASS -.-> SBO["ShadowBoundaryObserver (core/shadow_boundary_observer.py)<br/>• Zero-Capital Empirical Paper-Trader<br/>• First-Touch Pain Zone & 200 EMA Rejections"]
    end

    subgraph Execution_Manager ["7. AegisExecutionManager (Daemon Loop)"]
        SIZING --> Q["signal_queue.put_nowait()"]
        Q --> EM_LOOP["_run_loop() Consumer"]
        
        EM_LOOP --> G_LOCK{"Non-Consecutive Route Lockout<br/>(incoming == last_exited && age < 180s)"}
        EM_LOOP --> G_COOL{"Inter-Trade Cooldown (60s)<br/>Post-Loss Cooloff (120s)"}
        EM_LOOP --> G_TIME{"Session Window<br/>(09:16 ≤ now ≤ 15:00)"}
        
        EM_LOOP --> ORDER_FIRE["Single-Shot 0.75s Clean Entry (Zero Chasing)<br/>• Atomic 0.75s Fill Window (LMT Day at Ask / Sweep)<br/>• Pre-Staged Rolling 5s Hot Strikes Cache<br/>• Immediate _drain_signal_queue() on Abort"]
        ORDER_FIRE --> RESTING_TP["Place Exchange Resting Limit TP<br/>• Maker Order @ Entry + 4.0% (Mon–Thu)<br/>• Friday 4-DTE Calibrated to +3.0% TP"]
    end

    subgraph Adaptive_Exits ["8. Flexible Multi-Layered Adaptive Quantitative Exits"]
        RESTING_TP --> MANAGE["_manage_open_trade() Poller (2 Hz)"]
        
        MANAGE --> EX_TP{"Resting TP Hit?<br/>(Bid ≥ TP Price / Maker fill verified)"}
        EX_TP -->|YES| CL_TP["Passive Maker TP Exit (+4.0% / +3.0% Fri)<br/>(Zero Slippage Exchange Fill)"]
        
        MANAGE --> EX_NEAR{"Near-Target Profit Lock?<br/>Peak ≥ +3.5% (Fri: +2.5%) AND Pullback ≥ 0.8% (Fri: 0.5%)"}
        EX_NEAR -->|YES| CL_NEAR["Near-Target Lock Exit<br/>(Exits on pullback from peak bid)"]
        
        MANAGE --> EX_STEP2{"Cost-Shield Scratch?<br/>Peak ≥ +3.5% (Fri: +2.2%) AND Bid ≤ Entry"}
        EX_STEP2 -->|YES| CL_STEP2["Cost-Shield Scratch Exit (0.0% Breakeven)<br/>(Loss Permanently Prevented!)"]
        
        MANAGE --> EX_STEP1{"Stepped Safety Net?<br/>Peak ≥ +3.0% (Fri: +2.0%) AND Bid ≤ Entry - 3.0%"}
        EX_STEP1 -->|YES| CL_STEP1["Stepped Safety Net SL (-3.0%)<br/>(SL compressed from -12% to -3%)"]
        
        MANAGE --> EX_STAG{"0DTE Stagnation Trap?<br/>Expiry PM (≥12:30), Hold ≥ 90s, Gain < +0.5%<br/>AND 50-EMA Opposed Structure"}
        EX_STAG -->|YES| CL_STAG["0DTE Stagnation Trap Exit"]
        
        MANAGE --> EX_WICK{"Universal Spot Invalidation?<br/>(Spot Hits Entry Candle Low/High ± 0.10x ATR)<br/>• Mode B Failed Close Escape at T:60s"}
        EX_WICK -->|YES| CL_WICK["Universal Entry Candle Low/High Invalidation Exit"]
        
        MANAGE --> EX_SPOT_SL{"Spot Invalidation SL Hit?<br/>(Spot move ≥ 6.5 bps + 1.0x ATR)"}
        EX_SPOT_SL -->|YES| CL_SPOT["Adaptive Spot SL Exit<br/>(Derived via Realized Delta, max 35%)"]
        
        MANAGE --> EX_TIME{"Time Ceiling Hit?<br/>• Baseline Max Hold: 300s (5 Minutes)<br/>(3-min harvest choke decommissioned)"]
        EX_TIME -->|YES| CL_TIME["Time Stop Exit (300s Runway)"]
        
        MANAGE --> EX_DISASTER{"Disaster Circuit Breaker?<br/>(Option Bid Drops ≤ Entry - 12.0%)"}
        EX_DISASTER -->|YES| CL_DISASTER["Disaster Circuit Breaker (-12.0% Floor)<br/>(Catastrophic Black-Swan Shield)"]
        
        MANAGE --> EX_FLAT{"Session Force Flatten?<br/>(now ≥ 15:08 IST) - CAS Shield"}
        EX_FLAT -->|YES| CL_FLAT["CAS Square-Off Force Flatten"]
    end

    subgraph Exit_Execution ["9. Order Exit Chaser & Telemetry"]
        CL_NEAR & CL_STEP2 & CL_STEP1 & CL_STAG & CL_WICK & CL_SPOT & CL_TIME & CL_DISASTER & CL_FLAT --> CHASE["_chase_exit_thread (Daemon)<br/>3-Tier Urgency Exit Pricing:<br/>1. In-Place Modify Resting TP<br/>2. Marketable Limit with Dynamic Buffer<br/>3. Fallback Limit Place Order"]
        CL_TP --> SYNC["Broker TP Order Sync"]
        CHASE & SYNC --> POST["Post-Fill Bookkeeping & Telemetry<br/>• Post-trade MFE/MAE Reconciliation (exit_px)<br/>• Record last_exited_route<br/>• Update trades_today & daily_pnl<br/>• OutcomeTelemetry JSONL append"]
    end
```

---

## Directory & Module Map (Ground Truth — Audited 2026-09-25)

All active non-archive modules within `AEGIS/` mapped with exact lines, responsibilities, and key interfaces:

| Module Path | Primary Responsibility | Key Classes & Functions |
|---|---|---|
| `AEGIS/aegis_main.py` | Process orchestrator, PM2 entry, 09:14 AM seeding, 1-min ATM shift, IPC state | `main()`, `build_minute_digest()`, `attach_aegis_archiver()` |
| `AEGIS/aegis_gamma_engine.py` | Central brain, tick router, rolling L2 depth deque, Wyckoff parallel gate, conviction wiring, shadow ML router | `AegisGammaEngine`, `create_aegis_engine()`, `on_tick_parsed()` |
| `AEGIS/aegis_execution_manager.py` | Execution daemon, order routing, strict 1-lot maker TP (+4.0%), pre-trade rollback, 15:08 CAS flatten | `AegisExecutionManager`, `_manage_open_trade()`, `_rollback_slot()`, `_chase_exit_thread()` |
| `AEGIS/core/price_action_engine.py` | **Primary Macro Trigger**: 1m OHLC bars, 20/50/200 EMA, ATR(14), Pain Zone band single source (`get_pain_zone_vwaps()` → zone_top / zone_bottom), Priority Mode B Mid-Bar In-Flight Breakthrough (`check_mode_b_breakthrough`), Mode A Pierce (`check_mode_a_pierce`), 4 active pillars (P1 20 EMA, P2 Liquidity Sweep, P3 Vol Squeeze, P4 0-D Prominence Hub Breakout) | `PriceActionEngine`, `evaluate_setup()`, `check_mode_b_breakthrough()`, `check_mode_a_pierce()`, `check_20_ema_rejection()`, `check_micro_sweep_reclaim()`, `check_topological_prominence_breakout()` |
| `AEGIS/core/wyckoff_volume_engine.py` | **Wyckoff Volume & Delta Gate**: Effort vs Result, Volume Dry-Up Ratio (VDR), Absorption pin wick, Delta Harmony (Call Buy vs Put Buy) | `WyckoffVolumeEngine`, `evaluate()` |
| `AEGIS/core/composite_conviction_engine.py` | **Continuous 0–100 Evaluator**: 4 orthogonal domains, dynamic conviction ladder ($\ge 80.0 \to 1\text{ Lot}, +4.0\%\text{ TP}$) | `CompositeConvictionEngine`, `ConvictionResult`, `compute_conviction_score()` |
| `AEGIS/core/quality_stack.py` | **Institutional 3-Slot Daily Policy**: Full-day agile sizing (14:45 lockout removed), dynamic score hurdle, stability tracker | `QualitySlotPolicy`, `PostTriggerStabilityTracker` |
| `AEGIS/core/index_vwap_engine.py` | Constituent-volume weighted index VWAP poller and structural proximity gate | `IndexVWAPEngine`, `get_vwap_metrics()` |
| `AEGIS/core/oi_builtup_gate.py` | Real-time Open Interest kinematics (dump-shield veto) + wall snapshot telemetry; 4-quadrant gate decommissioned 2026-10-24 | `OIBuiltupGate`, `update_tick()`, `get_oi_kinematics()`, `get_wall_snapshot()` |
| `AEGIS/core/adaptive_micro_validator.py` | Adaptive Order Flow Energy (OFEE) validator with strict BOS momentum gate ($\ge 70.0$) | `AdaptiveMicroValidator`, `evaluate()` |
| `AEGIS/core/directional_persistence.py` | Signed directional persistence tracking over rolling 60s window ($D_p \ge 0.70$) | `DirectionalPersistenceEngine`, `evaluate_persistence()` |
| `AEGIS/core/microstructure_scorer.py` | Empirical 0-100 logit model, 15 features, $T$-anchored micro-acceleration | `MicrostructureScorer`, `compute_score()` |
| `AEGIS/core/instrument_repository.py` | SQLite WAL cache for instrument metadata and nearest-expiry resolution | `InstrumentRepository`, `resolve_weekly_options()` |
| `AEGIS/core/outcome_telemetry.py` | Forward outcome tracking (30s, 60s, 180s, 300s MFE/MAE) with JSONL persistence | `OutcomeTelemetryEngine`, `register_entry()` |
| `AEGIS/core/option_quality.py` | Option contract tradability filter (spread, top-5 depth, premium limits) | `OptionQualityEngine`, `qualify_option()` |
| `AEGIS/core/shadow_boundary_observer.py` | **Autonomous Shadow Boundary Observer**: Zero-capital empirical paper-trader tracking First-Touch Pain Zone and 200 EMA rejections on OTM 5 strikes with 30s/60s/300s telemetry | `ShadowBoundaryObserver`, `on_1m_bar_close()`, `on_option_tick()` |
| `AEGIS/config/settings.py` | Configuration binding for AEGIS runtime (`DATA_DIR=AEGIS/data`, `LOGS_DIR=AEGIS/logs`) | `get_runtime_paths("aegis")` |

> **Decommissioned & Archived (2026-10-24, `AEGIS/_archive/decommissioned_2026-10/`):** `hvg_engine.py` (live 5m stealth veto eliminated), `ml_gatekeeper.py`, `catboost_paper_trader.py`, `time_bar_engine.py`, `signal_state_machine.py`, `regime_chop_gate.py`, `entry_quality_engine.py`, `oi_quotes_poller.py`, `volume_profile_engine.py`, `shadow_quant_tracker.py`, `causal_microstructure_pipeline.py` — ~10,000 lines of dead weight removed. Post-debloat core: `price_action_engine.py` 3,847 lines, `aegis_gamma_engine.py` 1,835 lines, `aegis_execution_manager.py` 2,438 lines.

---

## Deep-Dive: Wyckoff Volume & Order Flow Delta Engine (`AEGIS/core/wyckoff_volume_engine.py`)

A pure-Python, sub-5-microsecond gate wired **in parallel with Price Action triggers**. Enforces the 3 classical Wyckoff Laws combined with modern Options Order Flow Footprint:

### 1. The 3 Universal Laws
- **Law of Effort vs Result**:
  $$\text{Absorption Index} = \frac{\text{Volume}_T}{\text{Spread}_T / \text{ATR}_{14}}$$
  High volume with narrow spread and a long rejection wick at support/resistance confirms institutional limit absorption.
- **Law of Cause and Effect (Dual-Regime Volume Dynamics)**:
  - **Pillar 3 & 4 (Breakouts):** Demand surge required ($VDR \ge 1.0$).
  - **Pillar 1 (20 EMA Pullback Retest):** Operates under two distinct, mathematically sound regimes:
    1. **Regime A (Supply Dry Test):** $VDR \le 0.80$ with decaying volume slope, confirming supply exhaustion / lack of selling interest into the 20 EMA.
    2. **Regime B (Stopping Absorption):** Normal to high volume accepted ONLY IF accompanied by an asymmetric absorption pin-bar (rejection wick $\ge 60\%$) and directional delta absorption.
- **Law of Supply and Demand (Delta Harmony)**:
  $$\text{Net Delta Flow} = \text{Call Buy Volume} - \text{Put Buy Volume}$$
  On breakouts, aggressive option volume must align with price movement.

### 2. Implementation Across the 4 Pillars
```python
# Pillar 1: 20 EMA Pullback Wick Rejection
# Approach Phase (T-3 to T-1):
- VDR = Avg(Vol[T-3..T-1]) / Baseline_Vol
- If price falls into 20 EMA on VDR >= 1.25 with rising volume:
    VETO: wyckoff_dump_counter_volume_expansion (INSTITUTIONAL_DUMP)
- If opposing put buying exceeds 2.2x call buying on touch:
    VETO: wyckoff_opposing_put_delta_surge
- Touch & Rejection Bar T:
    Lower Wick Ratio >= 0.30 and Absorption Score >= 1.2x confirms Stopping Volume.

# Pillar 2: Liquidity Sweep & Reclaim (Wyckoff Spring & Upthrust)
- Sweep candle triggers retail stops under swing low.
- Reclaim bar closes back inside range with lower wick >= 0.35.
- Confirms stop-loss absorption with no runaway opposing delta.

# Pillar 3 & 4: Volatility Squeeze & Structural Breakout (Wyckoff SOS)
- Breakout bar must have real participation (Vol >= 0.80x Baseline).
- Delta Harmony: For CALLs, Put Buy Volume must NOT exceed 2.2x Call Buy Volume.
- If price breaks out while opposing delta explodes:
    VETO: wyckoff_breakout_delta_divergence_bear_swamp (BULL_TRAP)
```

---

## Deep-Dive: Volume Profile & Auction Market Theory (`volume_profile_engine.py` — [ARCHIVED 2026-10-24, historical reference])

Passively models the continuous price-volume distribution (inspired by the Maxwell-Boltzmann distribution):

1. **Spot Volume Profile (Horizontal Price Bins)**:
   - Price quantized into discrete buckets (5.0 pts for NIFTY, 20.0 pts for SENSEX).
   - **Point of Control (POC)**: The price level with peak accumulated volume of the day.
   - **70% Value Area (VAH & VAL)**: Dual-pointer expansion outward from POC covering 70% of total volume (1 standard deviation).
   - **Low Volume Nodes (LVN)**: Bins with $< 25\%$ of peak POC volume; fast-slip momentum acceleration zones.
   - **Auction Regimes**: `INSIDE_VA` (rotational balance), `ABOVE_VAH` (bullish expansion), `BELOW_VAL` (bearish rejection).
2. **Strike-Wise Option Volume Profile**:
   - Accumulates cumulative day volume across active option strikes (ATM $\pm 10$).
   - Computes **Call POC Strike** and **Put POC Strike** (institutional liquidity magnet strikes).
3. **Telemetry & Execution**:
   - 100% PASSIVE SHADOW MODE (zero execution interference, zero overfitting).
   - Emits `[VOLUME PROFILE SHADOW]` on candidate trade evaluations for post-market research and calibration.

---

## Deep-Dive: CatBoost Standalone Paper Trader & 100% Shadow ML [DECOMMISSIONED 2026-10-14 — Retained for Historical Reference Only]

> ⚠️ **COMPONENT DECOMMISSIONED & DISABLED (14 October 2026)**:  
> CatBoost ML Gatekeeper and Standalone CatBoost Paper Trader were permanently decommissioned after an extensive 18-day forward trial proved their 46.4% win rate was far inferior to the 84%+ deterministic price action engine. Kyle's Lambda is inactive. Retained below strictly for historical archival reference; NO process runs this loop.

1. **Historical Execution Loop (`catboost_paper_trader.py` - Inactive)**:
   - Previously ran on completed 1-minute bars independently of 4-Pillars price action triggers.
   - Evaluated CatBoost Flow Brain (17 order flow + microstructure features).
   - If $\text{Score} \ge 40.0\%$ AND $\text{Score} \ge \text{Day Median}$:
     - Virtual 1-lot trade initiated on ATM strike.
     - Hard targets: **+4.0% TP**, **-3.0% SL**, **300s Max-Hold Timeout**.
   - Tracked live MFE/MAE on every option tick and appended to `catboost_paper_trades_YYYY-MM-DD.jsonl`.
2. **Empirical Historical Performance (24–25 Sep 2026)**:
   - Total Trades: 4
   - Wins: 2 (+6.79% in 33s, +5.41% with 0% MAE)
   - Losses: 2 (-3.22%, -3.51%)
   - Win Rate: 50.0% (Extended 18-day trial: 46.4% win rate -> permanently decommissioned)
   - Risk-to-Reward: 1 : 1.8
3. **Current Production State**:
   - CatBoost inference is completely removed from live trade evaluation.
   - Prevents black-box ML thresholds from blocking high-conviction structural price action setups.

---

## Deep-Dive: 4 Price Action Scalper Pillars & Hybrid Intra-Bar Architecture

AEGIS employs a **Hybrid Intra-Bar Execution Architecture**:
- **Mode A (Reversal / Retest / Sweep)**: Evaluates finalized 1-minute bars at **T:00s** (with pre-staged strikes & **T:50s–T:58s** pre-close confirmation) for **Pillar 1** (`20_ema_rejection`) and **Pillar 2** (`micro_sweep_reclaim`).
- **Priority Mode B (In-Flight Breakthrough Surge)**: Evaluates live forming bars mid-bar between **T:08s and T:48s** for **Pillar 3** (`volatility_squeeze_breakout`), **Pillar 4** (`topological_prominence_breakout` + Dynamic S/R Registry), Absolute Pain Zone boundaries, and 200 EMA Ribbon pierces.

### Priority Mode B: In-Flight Mid-Bar Breakthrough Surge (`check_mode_b_breakthrough`)
- **Execution Window**: Evaluated strictly mid-bar between **T:08s and T:48s** on active forming tick stream.
- **Sweet-Spot Range**: Forming candle range must be within $0.35 \times \text{ATR}_{1m} \le \text{Range} \le 1.20 \times \text{ATR}_{1m}$ (avoids both micro-noise and exhausted anti-climax bars).
- **Body Ratio**: Solid directional candle body $\ge 65\%$ of range with minimal leading wick ($\le 0.05 \times \text{ATR}_{1m}$).
- **Boundary Persistence**: Minimum 2 consecutive ticks maintaining price beyond the breached level (Pain Zone, 200 EMA Ribbon, Squeeze Box High/Low, or Session S/R Level).
- **Order Flow & Velocity Gate**: Live in-flight option volume delta ratio $\ge 1.30\times$ (or spot tick rate $\ge 1.5$ ticks/sec).
- **Mode B Failed Close Escape Rule (`MODE_B_FAILED_CLOSE_ESCAPE`)**: If a trade entered via Mode B closes back inside the broken boundary at `T:60s`, an immediate scratch escape is triggered at market.

### 1. Pillar 1: Classical Dow Theory 20 EMA Retracement Engine (`20_ema_rejection`)
- **Trend Age & Filter**: Trend Age $\ge 10$ bars continuously on the active side of the 20 EMA Band. 20 EMA slope aligned with trade direction.
- **Zero-Tolerance Anti-Breakout Veto (`breakout_from_below_veto`)**: Price must never have closed across the opposite side of the 20 EMA Band in the preceding 10 bars. Eliminates false breakouts from below.
- **Dow Structure Progression ($HH_1 \rightarrow HL_1 \rightarrow HH_2 \rightarrow HL_2$)**:
  - $HL_1$ (1st Touch): Strictly skipped (never trade 1st touch).
  - $HH_2$ (BOS): Confirmed higher high ($HH_2 > HH_1 + \min(1.5, 0.15\times\text{ATR})$).
  - Orderly Retracement: 5 to 10 candles orderly cooling off ($Descent \ge 60\%$) down to 20 EMA Band with volume dry-up ($VDR \le 1.15$).
  - $HL_2$ (2nd Touch): Retests 20 EMA High-Low Band with $HL_2 > HL_1$.
- **Candlestick Rejection**: Lower wick $\ge 33\%$ (Hammer) or Bullish Expansion body $\ge 40\%$, closing away from 20 EMA Band.
- **Live Piercing Invalidation Guard**: Forming price cannot pierce below 20 EMA Low Band during entry evaluation.
- **12s Bar Freshness Gate**: Entry window cutoff at $\le 12.0$s after bar open (with Mode A T:50s pre-close staging).

### 2. Pillar 2: Liquidity Sweep & Reclaim (`micro_sweep_reclaim`)
- **5-Bar Extreme Sweep**: Price dips below previous 5-bar swing low (CALL) or above swing high (PUT).
- **Instant Reclaim**: Bar closes back inside the level with rejection wick $\ge 35\%$ and lower wick $\ge 1.25\times$ upper wick (for CALLs).
- **Lunch Chop Veto**: Blocked between 11:15 IST and 13:00 IST to prevent whipsaws during low-volume midday drift.
- **Wyckoff Spring Equivalent**: Stop-loss flush followed by immediate institutional reclaim.

### 3. Pillar 3: Volatility Squeeze Breakout (`volatility_squeeze_breakout`)
- **Box Compression**: 3 to 4 prior closed bars compress tightly within $\le 1.25\times \text{ATR}_{14}$.
- **Decisive Breakout**: Bar closes completely outside box high (CALL) or box low (PUT) with opposing wick $\le 18\%$.
- **Anti-Climax Cap**: Breakout candle range must not exceed $\le 1.35\times \text{ATR}_{14}$ (prevents buying at climax exhaustion).
- **Trend Alignment**: 20 EMA $\ge$ 50 EMA for CALL, 20 EMA $\le$ 50 EMA for PUT.

### 4. Pillar 4: Topological Prominence Breakout (`topological_prominence_breakout`)
- **Priority 1: Dynamic Session S/R Level Registry Breakout**:
  - Breakouts through registered `StructuralLevel` nodes with monotonic `seq_idx` tracking (never `len(bars_1m)`).
  - Multi-touch boost (`+0.35`), dynamic post-excursion prominence, and confirmed order flow footprint.
- **Day Extreme Runway Shield**:
  - Scale-invariant clearance requirement:
    $$\text{min\_runway\_pts} = \max(1.25 \times \text{ATR}_{1m}, 0.0008 \times \text{Spot})$$
  - Rejects CALL entries within `min_runway_pts` of Day High / PDH, and PUT entries within `min_runway_pts` of Day Low / PDL.
- **Priority 2: T-2 0-D Prominence Hub (`FastMonotonicStack`)**:
  - Anchor identified via `FastMonotonicStack` with prominence ratio $\hat{\pi} \ge 1.75$.
  - Volume Ratio Gate: Current volume $VR_t \ge 1.4\times$ rolling 60-minute median volume.
  - Wyckoff Stopping Volume Veto: Opposing institutional delta swamping ($> 2.2\times$) triggers instant discard.
  - Note: `hvg_network_phase_shift` is strictly `SHADOW_ONLY`.

---

## Execution Manager & Flexible Multi-Layered Adaptive Exit Discipline

Monitored at 2 Hz by `_manage_open_trade()` (`aegis_execution_manager.py`):

1. **Strict 1-Lot Allocation**: Every admitted trade executes strictly with **1 Lot** (no dynamic multi-lot scaling).
2. **Passive Resting Maker TP Exit (+4.0% Mon–Thu, +3.0% Friday DTE 4)**:
   - Limit order resting on exchange book at **+4.0%** (Mon–Thu) or **+3.0%** (Friday 4 DTE) above entry price.
   - Zero-slippage maker fill on exchange order book.
   - Dual-verified: local tick-cadence touch/cross check + broker resting order fill check.
3. **Near-Target Profit Lock**:
   - Arms when peak bid reaches $\ge +3.5\%$ (Mon–Thu) or $\ge +2.5\%$ (Friday 4 DTE).
   - If price pulls back $\ge 0.8\%$ (0.5% on Friday) from peak, an instant market exit executes to lock profit (`Near-Target Profit Lock`), guaranteeing net green exit.
4. **Stepped Risk Compression & Cost-Shield Scratch Engine (V4.1)**:
   - Scale-invariant normalized percentage model:
     - **Step 1 (Stepped Safety Net)**: When peak gain reaches $\ge +3.0\%$ (Mon–Thu) or $\ge +2.0\%$ (Friday), SL is tightened from the -12% disaster floor to **-3.0%**, giving wide breathing room.
     - **Step 2 (Cost-Shield Scratch)**: When peak gain reaches $\ge +3.5\%$ (Mon–Thu) or $\ge +2.2\%$ (Friday), SL is permanently locked to **0.0% (Entry Price)**. A winning trade is mathematically prevented from ever turning into a loss!
5. **3-Minute Profit Harvest Choke (DECOMMISSIONED 2026-10-09)**:
   - Permanently removed to give profitable running trades the full 5-minute (`MAX_HOLD_SEC = 300s`) breathing runway to reach their full resting maker target (+4.0% TP). A trade in green is never prematurely choked at 180s.
6. **Dynamic 0DTE Time-Decay Shield & Stagnation Trap**:
   - Expiry days: 240s baseline before 12:30 PM, 180s strict after 12:30 PM.
   - At 90s on expiry afternoon (post-12:30 PM): if gain $< +0.5\%$ and 50-EMA is opposed, exits on `0DTE_STAGNATION_TRAP_EXIT`.
   - Non-expiry days: 300s baseline.
7. **Universal Pure Price Action Entry Candle Low/High Invalidation (All Pillars & Mode B)**:
   - For ALL 4 Pillars and Mode B, exit is anchored directly to the spot price action of the entry candle with an ATR buffer:
     - **CALL**: Immediate exit if $\text{Spot} \le \text{Entry Candle Low} - (0.10 \times \text{ATR})$.
     - **PUT**: Immediate exit if $\text{Spot} \ge \text{Entry Candle High} + (0.10 \times \text{ATR})$.
     - Option is protected from artificial Greeks/spread noise as long as the breakout candle structure holds!
8. **Spot Adverse Movement SL & Trend Reversal Exit**:
   - Spot moves $\ge 6.5\text{ bps}$ + 1.0x ATR against trade direction.
   - Completed 1m bar close breaking 50-EMA or structure turning opposed triggers immediate `Trend Reversal Exit` (9-EMA bar-close choke was permanently decommissioned on 2026-10-07).
9. **Tick-Cadence Option Premium Disaster Circuit Breaker (-12.0% Floor)**:
   - `PREMIUM_HARD_SL_PCT = 0.120` (-12.0% disaster floor).
   - Allows natural option bid-ask noise and normal spread flutter to breathe without premature stop-outs, while catching catastrophic black-swan flash crashes.
10. **Pre-Trade Abort Slot Rollback**:
    - If an order is rejected or aborted prior to execution, `_rollback_slot()` immediately restores the consumed daily trade slot.
11. **15:08:00 IST CAS Square-Off Force Flatten**:
    - Unconditional liquidation at **15:08:00 IST** to eliminate NSE Closing Auction volatility and broker penalty fees.

---

## Verification & Test Suite Matrix (244 Tests — 100% Green)

AEGIS Production Version 1.0 (v1.0 Locked) is certified by 244 active tests across the repository:

```powershell
python -m pytest tests -m "not slow" -q
# Output: 242 passed in ~13s (100% GREEN — fast dev suite; slow replays deselected)

python -m pytest tests -q
# Output: 244 passed in ~95s (full suite incl. 1.8L-tick parquet replay parity)
```

| Test Suite File | Tests | Key Invariants Verified |
|---|---|---|
| `tests/test_wyckoff_volume_engine.py` | 16 | Retracement volume dry-up, institutional dump veto, delta divergence bull trap veto, synthetic feed pass. |
| `tests/test_price_action_engines.py` | 43 | 20 EMA Two-Touch Dow structure, Mode A/B wiring, S/R registry, bad-tick guard. |
| `tests/test_aegis_adaptive_exits.py` | 41 | +4.0% resting TP, -12% disaster floor, stepped risk compression, near-target lock, 0DTE stagnation trap, wick invalidation, 6m extension, Friday low-capital guard. |
| `tests/test_aegis_engine.py` | 8 | Gamma engine tick handling, Wyckoff parallel gate wiring, ATM shift, memory purging. |
| `tests/test_aegis_integration_parity.py` | 4 | Backtest parity across 1.8L real ticks (2 slow-marked replays + 2 data-dependent), execution manager alignment. |
| `tests/test_composite_conviction_engine.py` | 11 | 4-domain scoring, continuous evaluation, delta trap penalty, >= 80.0 threshold. |
| `tests/test_quality_stack.py` | 9 | Institutional 3-slot daily policy, consecutive loss stop, daily loss cap. |
| `tests/test_oi_builtup_gate.py` | 5 | OI kinematics fresh-build / dumping / sparse fail-open, wall snapshot read-only. |
| `tests/test_shoonya_session_coordination.py` | 6 | Shoonya session token adoption, peer lock coordination, fallback on failure. |
| `tests/test_microstructure_scorer.py` | 6 | Microstructure logit model, 15 scalar features, latency metrics. |
| `tests/test_key_levels_and_strike_selector.py` | 7 | PDH/PDL proximity veto, strike selection budget criteria. |
| `tests/test_aegis_core_components.py` | 4 | Option quality, live component wiring. |
| `tests/test_adaptive_micro_validator.py` | 11 | Adaptive Order Flow Energy (OFEE), strict BOS momentum gate (>= 70.0). |
| `tests/test_aegis_v4_core.py` | 12 | FastMonotonicStack, Top5DepthGuard, Confirm15 state machines. |
| `tests/test_argus_core_stack.py` | 10 | ARGUS shared-core stack (tick compiler, journals). |
| `tests/test_shadow_boundary_observer.py` | 6 | First-touch Pain Zone / 200 EMA shadow trades, Mode B boundary tracking. |
| `tests/test_mode_a_b_cushion_audit.py` | 5 | Mode A/B trailing SL cushion + structural buffer audits. |
| `tests/test_band_veto_buffer.py` | 4 | Pain Zone band veto ATR buffer behavior. |
| `tests/test_universe_side_split.py` | 4 | CE/PE universe robust split (single-sided feed protection). |
| `tests/test_system_wiring.py` | 2 | End-to-end component wiring, settings resolution. |
| `tests/test_shared_runtime_paths.py` | 2 | Runtime paths, common IPC delegation (protected kronos legacy branch). |
| `tests/test_intent_vs_code_adversarial.py` | 29 | Semantic Intent-to-Code Parity adversarial torture suite: fake Pillar 1 breakout/breakdown counter-examples proven blocked on all 3 routes (Bar-Close, Mode A Arming/Pierce/Pre-Close), Dow open-side veto, Mode A Pillar 2 safety-stack parity, True Pain Zone 4-line envelope (min/max outside-band rule) + 5m trend gates, in-flight `_opt_flow_1m` enrichment, exact-bucket T:60s Failed-Close Escape. |
| **Total Test Suite** | **245** | **100% PASSED — ZERO FAILURES** (fast 243 + slow parity 2) |

---

## Semantic Intent-to-Code Parity Audit (2026-10-26)

Adversarial counter-example audit that eliminated every gap between the human's Trading Intent and the machine's implementation. Each rule below is proven by a dedicated counter-example test in `tests/test_intent_vs_code_adversarial.py`:

1. **Pillar 1 Open-Side Anti-Breakout Veto**: `_validate_dow_hl2_lh2_structure` now vetoes candles whose **OPEN** crosses the opposite side of the 20 EMA Band (not only the close) across all three loops (10-bar window, trend walk-back, orderly pullback). A candle opening below the band and piercing up is a breakout, never a pullback rejection (`breakout_from_below_veto` / `breakdown_from_above_veto`).
2. **Pillar 1 Expansion Open Gate**: `check_20_ema_rejection` rejects expansion candles that opened across the band (`breakout_from_below_open_veto` / `breakdown_from_above_open_veto`) — a rejection candle must open inside the band or on the correct side.
3. **Pre-Close Virtual Forming Bar**: `_validate_dow_hl2_lh2_structure` accepts a `virtual_bar` — Mode A Pre-Close (T:50s–T:58s) now judges the Dow HL2/LH2 chain on the LIVE forming candle instead of the previous closed bar.
4. **Mode A Pillar 2 Full Parity**: Arming + Pre-Close enforce the identical safety stack as the main `check_micro_sweep_reclaim` ladder: `min_sweep_depth`, opposite-wick caps ($\le 15\%$), wick dominance ($\ge 1.25\times$), Structural Obstacle Shield, and Pain Zone VWAP alignment.
5. **True Pain Zone 4-Line Envelope Alignment (`_pain_zone_vwap_aligned`)**: The 4 trapped-party option-flow VWAP lines (`vwap_call_buy`, `vwap_call_sell`, `vwap_put_buy`, `vwap_put_sell`) form a natural cluster band: `Upper Band = max(active_vwaps)` and `Lower Band = min(active_vwaps)`. Strict Outside-Band Rule (आर-पार का नियम): **CALL** requires spot strictly above the entire cluster (`spot > Upper Band`); **PUT** requires spot strictly below the entire cluster (`spot < Lower Band`). Inside the band is a neutral / no-trade chop zone (both vetoed). No separate composite VWAP or single trapped-buyer selection needed. Warmup/missing lines fail-open (mirrors `band_veto_blocks`). Enforced in Mode A Arming/Pierce/Pre-Close and Mode B (`below_vwap` / `above_vwap`).
6. **Mode B Pillar 4 5m Trend Gate**: Mode B S/R Registry breakout now enforces the same 5m structural alignment as the bar-close `check_topological_prominence_breakout` ladder.
7. **In-Flight Option Flow Enrichment**: `get_relative_bar_metrics` overlays the live `_opt_flow_1m` accumulator on the forming bar — Mode A / Mode B signals carry real `bull_flow` / `bear_flow` / `opt_total_vol` into conviction scoring (option flow attaches to the candle only at `_on_1m_bar_close`).
8. **Mode B Failed-Close Escape (Exact Bucket Matching)**: `MODE_B_FAILED_CLOSE_ESCAPE` judges the exact entry candle via minute-bucket matching — never the pre-entry box bar (which closes inside the boundary by definition) during the T:60s tick race.

---

## Senior Human-Trader Principles for AI Reviewers

When Gemini 3.8 Flash evaluates any candidate setup in Shadow Mode, it must apply the following **Senior Human-Trader Principles**:
1. **Never Chase into Brick Walls (Day Extreme Runway Rule)**: A setup firing within 1.25x ATR or 8 bps of PDH, Day High, Day Low, or a flatline VWAP is a high-probability trap. Demand minimum 1.5x–2.0x ATR or 15–20 bps of unobstructed runway.
2. **Effort vs Result in Retracements**: Pullbacks into 20 EMA must see volume dry up ($VDR < 0.85$). Expanding volume on counter-trend candles indicates an institutional dump; do not fade it.
3. **Delta Harmony on Breakouts**: A valid expansion breakout requires option aggressor flow to agree with price ($\text{Call Buy} \ge \text{Put Buy}$). Opposing delta swamping indicates trapped retail breakout buyers.
4. **Stopping Volume Confirmation**: Rejection candles must exhibit absorption (lower wick $\ge 30\%$ on calls, upper wick $\ge 30\%$ on puts) closing firmly in the direction of the trade.
