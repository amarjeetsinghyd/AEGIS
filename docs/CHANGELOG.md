# Changelog

## [Unreleased] - 2026-10-27 (Zero-Entropy Core Modularization, AegisPipelineOrchestrator Elevation & Pure Tape-Reading Consolidation)

### Architectural Restructuring & Consolidation
- **`AegisGammaEngine` Permanently Purged & Replaced by `AegisPipelineOrchestrator`** (`AEGIS/core/aegis_pipeline_orchestrator.py`):
  - Completely erased legacy `aegis_gamma_engine.py` file, class `AegisGammaEngine`, and all backward-compatibility facade re-exports and aliases across the entire repository.
  - Formally established `AegisPipelineOrchestrator` as the sole master conductor for Shoonya L2 market data ingestion, spot vs option tick distribution, 6-gate pipeline synchronization, and execution dispatch.
  - Purged all dead attributes (`gamma_tensor`, `primed_strikes`, `required_bursts`, `v4_futures_buffer`, etc.) from the orchestrator.
- **Decomposition of Monolithic `PriceActionEngine` into Modular Mixins** (`AEGIS/core/price_action_engine.py`):
  - Streamlined `price_action_engine.py` from 4,118 lines to 2,332 lines while retaining its role as the single source of truth for 1m/5m/15m OHLC candle construction and 4 Macro Price Action Pillars + Priority Mode B Mid-Bar Breakthrough.
  - **Dynamic S/R Registry Extraction** (`AEGIS/core/structural_registry.py`):
    - Encapsulated `StructuralRegistryMixin`, `StructuralLevel` dataclass, 5m historical candle seeding, cluster merging, and level role classification (`OUTERMOST_ROOF`, `INTERIOR_CLUSTER`) with monotonic `seq_idx` tracking.
  - **Dow Theory & Macro Obstacle Shields Extraction** (`AEGIS/core/dow_macro_shields.py`):
    - Encapsulated `DowMacroShieldsMixin`, `DowStructureChain` dataclass, classical 4-point HL2/LH2 sequence, 20 EMA High/Low bands, Day Extreme Runway Shield (`min_runway_pts`), and Obstacle Wall Shield.
  - **Unified Order Flow, Microstructure & Conviction Engine** (`AEGIS/core/option_order_flow.py`):
    - Consolidated `OptionOrderFlowMixin` (ATM $\pm 25$ strikes tape reading, PCR, OFI Delta, Pain Zone VWAP), contract L1-L5 depth (`MicrostructureScorer`), and continuous 0–100 conviction scoring (`CompositeConvictionEngine`, `ConvictionResult`, 40-35-25 model) into a unified module.
    - Permanently purged standalone `composite_conviction_engine.py` and `microstructure_scorer.py` files.
- **Purge of Dead Code & Legacy Setups**:
  - Permanently excised `FastMonotonicStack` from `price_action_engine.py` and unit tests.
  - Permanently excised `LegacyBarCloseSetupsMixin` and `legacy_bar_close_setups.py` along with 12 obsolete closed-bar breakout unit tests.
- **Verification**:
  - Full pytest test suite executed: **211 passed, 4 skipped in 17.23s** across the entire repository with 0 errors and 0 failures.

## [Unreleased] - 2026-10-27 (50-Trade Quantitative Milestone, Zero-Futures Architecture, Event-Driven Broker Capital & Data Integrity Shield)

### Milestone
- **Historic 50-Trade Quantitative Milestone Reached**:
  - Validated AEGIS autonomous execution across 50 real-market trades (1 DTE & 0 DTE NIFTY/SENSEX options) with zero manual parameter tampering.
  - **Overall Performance**: 50 Total Trades | 37 Wins | 13 Losses | **74.0% Win Rate** | **Gross Profit: +₹229.40** (Gross Profit: ₹689.15, Gross Loss: ₹459.75, **Profit Factor: 1.50**).
  - **Star Pillar Breakdown**:
    - **Pillar 3 (`volatility_squeeze_breakout`) [Undefeated Star Pillar]**: 9 Trades | 9 Wins | 0 Losses | **100.0% Win Rate** | Gross P&L: **+₹190.90** | Avg Option Gain: **+3.45%**.
    - **Pillar 1 (`20_ema_rejection`) [Dow HL2/LH2 Foundation]**: 16 Trades | 12 Wins | 4 Losses | **75.0% Win Rate** | Gross P&L: **+₹70.50** | Avg Option Gain: **+0.98%**.
    - **Pillar 2 (`micro_sweep_reclaim`) [Institutional Liquidity Reclaim]**: 10 Trades | 7 Wins | 3 Losses | **70.0% Win Rate** | Gross P&L: **+₹58.00** | Avg Option Gain: **+1.13%**.
    - **Pillar 4 (`topological_prominence_breakout`) [Registry S/R Breakout]**: 15 Trades | 9 Wins | 6 Losses | **60.0% Win Rate** | Gross P&L: -₹90.00 (catalyst for 26 Oct debloating of HVG 5m stealth veto and re-anchoring to Dynamic S/R Registry).
    - **Combined P1, P2 & P3 Performance**: 35 Trades | 28 Wins | 7 Losses | **80.0% Win Rate**.

### Added
- **Composite Conviction Engine Dual-Profile & Consolidated Microstructure Gate** (`AEGIS/core/composite_conviction_engine.py` & `AEGIS/aegis_gamma_engine.py`):
  - Consolidated standalone `AdaptiveMicroValidator` and its redundant 60-point energy gate directly into `CompositeConvictionEngine` (Zero-Entropy Architecture).
  - Implemented Dual-Profile Option Microstructure & Order Flow scoring (0–100 continuous scale, 40-35-25 domain model):
    - **Mode B In-Flight Profile**: Instantaneous breakout scoring via Touch Aggression $I_1$ (40 pts) + Live Tick Arrival Intensity (35 pts) + Spread Tradability (25 pts) with Wyckoff OFI Delta floor boost ($\ge 34.0\text{ pts}$).
    - **Mode A Structural Rejection Profile**: Bar-close confirmation scoring via Order Book Support & $L_3-L_5$ Deep Pressure (40 pts) + 30s Option Return Velocity (35 pts) + Contract Depth & Spread (25 pts).
  - Retired Domain 1 Spot Structure ($0.0\text{ pts}$) eliminating redundant scoring loops already validated upstream by `PriceActionEngine`.
  - Embedded hard sanity circuit breakers directly into `extract_micro_metrics`: spread blowout ($> 2.5\%$), ghost book ($< 200$ depth), and severe dump ($< -1.5\%$).
  - Enforced continuous admission hurdles: $\ge 85.0 \implies \text{FULL\_SIZE}$ (2 lots, +4.0% Maker TP), $80.0 - 84.9 \implies \text{STANDARD\_SIZE}$ (1 lot, +4.0% Maker TP), $< 80.0 \implies \text{BLOCKED}$ with itemized drags (`aggression_drag`, `momentum_drag`, `tick_rate_drag`, `tradability_drag`).
- **Dynamic Event-Driven Broker Funds Integration** (`common/execution/capital_engine.py`):
  - Completely decoupled paper capital state from stale local JSON files (`capital_state.json`).
  - Implemented one-shot startup query to Shoonya broker API (`portfolio_provider.get_available_funds()`) fetching live ledger balance (₹486.71) with zero polling loops.
  - Subsequent requests are served instantly from memory, with broker re-sync triggered strictly upon trade closure events via `add_funds()`.
- **Trade Journal Forensic Ingestion Shield & Telemetry Hardening** (`unified_dashboard/telemetry_loader.py`):
  - Added multi-layer validation to `reconcile_and_auto_journal()` preventing synthetic test runs from corrupting historical records:
    - Weekend Filter: Strictly discards Saturday and Sunday timestamps.
    - Market Hours Guard: Enforces 09:14 to 15:35 IST trading window.
    - Extreme PnL Plausibility Cap: Discards single-lot retail option micro-scalps with $|PnL| > \text{₹3,000}$ or synthetic dummy patterns (`en=8.15, ex=1.0`).
- **Far-OTM Execution Guard** (`AEGIS/aegis_gamma_engine.py`):
  - Added strike distance filter in `on_tick_parsed` gating trade candidate evaluation to strikes within 8 steps of ATM (`abs(strike - current_atm_strike) <= 8 * strike_step`), preventing illiquid deep-OTM contracts from triggering fills during rapid breakouts while preserving full 25-OTM depth for OFI/PCR volume compilation.

### Changed
- **Live Brokerage Execution Activation (`ShoonyaOrderLifecycle` Active)** (`common/config.py`, `ecosystem.config.js`, `.env`):
  - Enabled live brokerage execution (`ENABLE_LIVE_BROKERAGE_EXECUTION=True`, `AEGIS_LIVE_TRADING=TRUE`) across local and VPS environments.
  - Hardened `common/config.py` to auto-load `.env` before variable evaluation and accept either flag name, ensuring PM2 process reloads reliably bind to live order routing.
  - Verified live engine transition: `[AegisEM] LIVE mode — Shoonya LMT / NRML path` confirmed active on VPS.
- **Hot-Strike Live Re-Validation Dynamic Premium Floor Alignment** (`AEGIS/aegis_execution_manager.py`):
  - Applied `effective_premium_floor(_pmin, avail_funds, lot_size)` in `_process_aegis_signal()` at line 720.
  - Aligned live re-validation with `StrikeSelector`'s dynamic floor logic, resolving a critical testing-mode deadlock where affordable OTM strikes (e.g. ₹5.00–₹8.50 selected for testing capital ₹486) were falsely rejected against the production ₹15.00 floor (`premium_too_low`). Production accounts with full capital remain bounded by the configured ₹15.00 floor.
- **Decommissioning of `Top5DepthGuard` (Zero-Entropy Logic De-Bloat)** (`AEGIS/aegis_gamma_engine.py`):
  - Completely eliminated the redundant legacy `Top5DepthGuard` and its pre-dispatch veto block at line 1069.
  - Root cause resolved: Shoonya `t` (trade) ticks do not carry top-5 depth levels, causing local `bp1` and `sp1` variables to evaluate to `0.0`, which previously triggered spurious `INVALID_QUOTES` vetoes on genuine Mode B breakouts (such as 09:19, 09:28, 10:20, and 11:10 today).
  - All order-book depth, bid-ask spread tradability, and ghost-book sanity checks remain exclusively and robustly governed by `CompositeConvictionEngine` and `AegisExecutionManager`.
- **Decommissioning of `OI Kinematics` Live Blocking Veto** (`AEGIS/aegis_gamma_engine.py`):
  - Decommissioned the live entry blocking veto (`_oi_dumping ... return None`) at line 1032.
  - Root cause resolved: NSE exchanges update Open Interest on a 3-minute snapshot frequency (not tick-by-tick). Enforcing tick-cadence kinematics created execution friction and false telemetry drops.
  - `OIBuiltupGate` is preserved purely for passive dashboard telemetry and strike wall depth profiling without blocking scalper entries.
- **Decommissioning of `Directional Persistence` 60s Spot Tick Veto** (`AEGIS/aegis_gamma_engine.py`):
  - Eliminated the redundant 60-second spot tick-counting persistence veto at line 746 (`pers_confirmed = True`, `pers_score = 1.0`).
  - `PriceActionEngine` already verifies complete macro trend alignment, candle body geometry ($\ge 65\%$), wicks ($\le 0.05 \times \text{ATR}$), and ATR expansion multiples. The legacy tick-counting check was 100% duplicate logic.
- **Canonical Version Standardization to `AEGIS V1.0` Across All Banners & Headers**:
  - Replaced all lingering legacy "AEGIS V2.0" log banners, docstrings, and IPC schemas across `AEGIS/aegis_main.py`, `AEGIS/aegis_execution_manager.py`, `AEGIS/aegis_gamma_engine.py`, and `common/ipc/live_state.py` with the canonical **`AEGIS V1.0`** (Production Version 1.0 Locked).
- **Universal Index Futures Elimination (100% Native Options Engine)**:
  - Removed all index futures dependencies, REST queries, and WebSocket token tracking across `execution/data_fetcher.py`, `core/instrument_repository.py`, `aegis_gamma_engine.py`, and `aegis_main.py`.
  - AEGIS now operates exclusively on cash index spot prices and option chain L1/L2 microstructure, saving CPU overhead and eliminating spurious warning logs.
- **Journal & Trade History Sanitization**:
  - Excised 7 synthetic mock records injected over the 24–25 Oct weekend from `trade_history.json` and `docs/AEGIS_TRADE_JOURNAL.csv`.
  - Recompiled styled `docs/AEGIS_TRADE_JOURNAL.xlsx` and synchronized state across local development and Oracle Cloud VPS.

### Added
- **Pillar 2 (`micro_sweep_reclaim`) Dynamic S/R Level Registry & Day Extreme Re-Anchoring** (`AEGIS/core/price_action_engine.py`):
  - Completely re-anchored Liquidity Sweep detection and Mode A arming to the **Dynamic S/R Registry (`self.session_levels`)** and Day Extremes (Previous Day High / Low, `PDH` / `PDL`), eliminating noisy 5-bar swing wiggles.
  - Implemented `_find_swept_sr_level()` resolving Priority 1: `SR_REGISTRY` (`SUPPORT` for CALL, `RESISTANCE` for PUT) with scale-invariant sweep depth and multi-touch prominence.
  - Recorded rich telemetry in trade details: `sweep_source` (`SR_REGISTRY` vs `DAY_EXTREME`), `swept_level`, `level_prominence`, and `level_touches`.
  - Maintained graceful 5-bar fallback solely for early-morning bootstrap before intraday session levels form.
- **Pillar 1 (`20_ema_rejection`) Wyckoff Candle $N-1$ Smart Safe Gate** (`AEGIS/core/price_action_engine.py` & `AEGIS/core/wyckoff_volume_engine.py`):
  - Added preceding candle $N-1$ micro-footprint verification for Volume Dry-Up ($VDR \le 0.85$ or $VR \le 0.70$), Synthetic Net Option Delta Absorption, and Stopping Volume ($Lower Wick \ge 40\%$ with volume surge).
  - Implemented Smart Safe OR/AND gate: if candle $N-1$ Wyckoff footprint confirms, or if standard VDR/OFI delta confirms, setup passes cleanly without false self-vetos.

### Changed
- **Universal Removal of 48s Upper Cap on Priority Mode B Breakthroughs**:
  - Eliminated the arbitrary 48-second upper ceiling (`T:08s–T:48s` $\to$ `T:08s onwards / No Upper Cap`) universally across Pillar 3 (`volatility_squeeze_breakout`), Pillar 4 (`topological_prominence_breakout`), and `ShadowBoundaryObserver`.
  - In-flight breakouts occurring in the final seconds of candle formation (T:49s–T:58s) with verified momentum, minimal leading wick, and order-flow velocity are now executed in-flight ahead of retail bar-close crowds.
- **Sub-50ms Execution Latency & Zero-Chasing Verification**:
  - Re-audited and confirmed sub-50ms execution path: rolling 5-second hot-strikes pre-staging cache (0ms lookup), single-shot 0.75s atomic limit fill window, zero order chasing/modification, and instant signal queue draining on any rejection.
- **Weekly Index Switching & Lifecycle Parity**:
  - Re-verified self-contained intraday S/R lifecycle: clean daily reset prevents cross-instrument pollution (NIFTY vs SENSEX) and weekend gap distortions, while PDH/PDL anchors are permanently loaded from 09:14 AM boot.

## [Unreleased] - 2026-10-26 (Semantic Intent-to-Code Parity Audit & Adversarial Torture Testing)

### Added
- **Adversarial Torture Suite `tests/test_intent_vs_code_adversarial.py` (29 tests)**: counter-example simulations proving the machine never trades against the human's Trading Intent — fake Pillar 1 breakout-from-below / breakdown-from-above proven blocked on all 3 trigger routes (Bar-Close, Mode A Arming, Mode A Pre-Close), Mode A Pillar 2 safety-stack parity, True Pain Zone 4-Line Envelope outside-band rule, Mode B vs 5m trend, in-flight option flow enrichment, and the T:60s exact-bucket Failed-Close Escape. Suite runs 100% green on local Python.
- **True Pain Zone Band Geometry & Outside-Band Rule (`_pain_zone_vwap_aligned()` & `get_pain_zone_band()`)** (`AEGIS/core/price_action_engine.py` & `AEGIS/aegis_gamma_engine.py`): the 4 trapped-party option-flow VWAP lines (`vwap_call_buy`, `vwap_call_sell`, `vwap_put_buy`, `vwap_put_sell`) naturally form a tight cluster band: `Upper Band = max(active_vwaps)` and `Lower Band = min(active_vwaps)`. Strict Outside-Band Rule (आर-पार का नियम): **CALL** strictly requires `spot > Upper Band` (above all 4 lines); **PUT** strictly requires `spot < Lower Band` (below all 4 lines); spot inside the band is a neutral / no-trade zone for both directions. Composite VWAP eliminated in favor of direct 4-line min/max envelope. Fallback to single-line on warmup/synthetic mocks, fail-open when no lines.

### Fixed (8 Semantic Intent-to-Code Gaps)
- **Pillar 1 Dow Anti-Breakout Veto now checks OPEN crossings** across all 3 loops (10-bar window, trend walk-back, orderly pullback) — a candle opening below the 20 EMA band and piercing up was bypassing the close-only veto (`breakout_from_below_veto`).
- **Pillar 1 expansion open gate**: `is_bullish_expansion` / `is_bearish_expansion` accepted big-body candles piercing from the wrong side; new `breakout_from_below_open_veto` / `breakdown_from_above_open_veto` gates require the rejection candle to open inside the band or on the correct side.
- **Mode A Pre-Close Dow validation judged the WRONG bar**: the forming candle is not yet in `bars_1m`, so `_validate_dow_hl2_lh2_structure` was validating the previous closed bar as touch2 — new `virtual_bar` parameter makes Pre-Close judge the LIVE forming candle.
- **Mode A Pillar 2 was missing 3 safety layers** vs the main ladder: `min_sweep_depth`, Pain Zone VWAP alignment and the Structural Obstacle Shield (plus opposite-wick caps and wick dominance) are now enforced identically in Arming and Pre-Close.
- **Mode B ignored its `external_vwap` parameter entirely** — spot could fire a breakout against the composite option-flow VWAP. The gate is now live (`below_vwap` / `above_vwap`).
- **Mode B Pillar 4 lacked the 5m structural alignment gate** enforced by the bar-close `check_topological_prominence_breakout` ladder — parity restored (`trend_5m_bullish_mb` / `trend_5m_bearish_mb`).
- **In-flight signals carried zero option flow**: option flow attaches to the candle only at `_on_1m_bar_close`, so Mode A / Mode B `pa_details` shipped `bull_flow` / `bear_flow` / `opt_total_vol` = 0 into conviction scoring — `get_relative_bar_metrics` now overlays the live `_opt_flow_1m` accumulator on the forming bar.
- **Mode B Failed-Close Escape T:60s race**: the wall-clock monitor could judge the PRE-entry box bar (which closes inside the boundary by definition) before the entry candle's first post-boundary tick closed it — premature scratch of healthy breakouts. The escape now matches the exact entry-candle minute bucket (`MODE_B_FAILED_CLOSE_ESCAPE`).

### Tests
- Suite **216 → 245** (29 new adversarial tests; fast dev suite 243 in ~13s, full suite 245 in ~95s — 100% PASSED on local).

### Ops
- Fixes deployed to VPS (`137.23.41.38`) at 03:50 IST (safe pre-market window): blob-verified file sync, `py_compile` OK, `aegis-bot` PM2 restarted and stable ahead of Monday 08:55 IST NIFTY 1-DTE wakeup.
- VPS git repo object store repaired and re-synced to `origin/main` (see commit history for details).

## [Unreleased] - 2026-10-25 (Monorepo Zero-Entropy Cleanup, Core De-Bloat, Friday Deep Sleep & Local–VPS Parity)

### Added
- **Friday 4-DTE Deep Sleep**: `is_trading_day()` Friday gate (`AEGIS_FRIDAY_TRADING_ENABLED` env, default OFF) + EM Friday low-capital guard (₹3,000) — engine sleeps Thursday 15:17 close straight through to Monday 08:55 IST.
- **`_archive/ARCHIVE_MANIFEST.md`**: one-glance inventory of every archived folder for the 2–3 month deletion policy.
- **`scratch/`, `astra_bot/`, `kronos_bot/` gitignore shielding** — throwaway workspace and legacy engine shells can never be committed again.
- **Mode A / Mode B wiring**: `mode` field persisted in `decision_payload` + `trade_context.execution_mode`; Conviction/Wyckoff intra-bar fast-paths now mode-aware (`pa_details["mode"]`).

### Changed
- **Core De-Bloat (~10,000 lines removed)**: `price_action_engine.py` 4,875 → 3,847 lines; `aegis_gamma_engine.py` 2,478 → 1,835; `aegis_execution_manager.py` 2,506 → 2,438; dead runner-trailing + non-pillar 9-EMA reversal blocks + 6 unreachable gamma gate-blocks removed.
- **11 modules archived** to `AEGIS/_archive/decommissioned_2026-10/`: `hvg_engine`, `ml_gatekeeper`, `catboost_paper_trader`, `time_bar_engine`, `signal_state_machine`, `regime_chop_gate`, `entry_quality_engine`, `oi_quotes_poller`, `volume_profile_engine`, `shadow_quant_tracker`, `causal_microstructure_pipeline`.
- **Single Shadow Experiment**: `ShadowBoundaryObserver` (Pain Zone + 200 EMA first-touch/breakthrough) is the ONLY active shadow; its boundaries are live-Mode-B-excluded by policy.
- **Subscription universe widened**: ATM ±25 strikes (51 strikes / 102 tokens), `TESTING_DEEP_DEPTH = 32`, minimum premium floor restored to **₹4.00** (charge-viability breakeven).
- **Workspace sweep**: 100 one-shot scratch scripts permanently deleted; 41 research artifacts (CSV/parquet/JSONL + analyzers) archived to `_archive/2026-10_scratch_research/`; 4 reusable diagnostics promoted to `scripts/` (smoke_signal_fire, friday_check_vps, shadow-trade analyzers).
- **Legacy engines consolidated**: `astra_bot/` archived (14.9 MB legacy telemetry + runtime data); `kronos_bot/` empty husk deleted; default runtime fallback `"astra"` → `"aegis"` in `common/config.py` (unconfigured processes now hit the ACTIVE engine's data dir).
- **Docs synced to code**: AGENTS.md (active engines, 25-OTM universe, decommissioned components, stray-dir policy), `AEGIS_V1.0_ARCHITECTURE.md` (module map, Mermaid, decommissioned block, 216-test matrix), `SYSTEM_ARCHITECTURE.md` §8 canonical tree.

### Fixed
- **HVG 5m Stealth Veto eliminated**: ungated live `check_5m_structural_alignment` veto inside `_check_structural_obstacle` was silently blocking all 4 pillars + Mode B — cut at the root.
- **CRITICAL trade-fire NameError**: `ml_score`/`ml_passed`/`ml_reason`/`self.ml_gatekeeper_mode` vestiges crashed `live_signal_metrics` before `_emit_signal` — would have blocked ALL Monday trades; fixed + runtime smoke-proofed.
- **`vp_metrics` NameError** in outcome-telemetry snapshot merge.
- **PDH/PDL/PDC seeding**: daily-levels block un-indented from the fallback `else:` (now seeds on every boot) + pandas-free date parsing.
- **Main-loop digest race**: state-mutating `evaluate_setup()` calls removed from `build_minute_digest()` (passive `PA:ARMED(n)` display).
- **Mode B Bar-Close Escape** now mode/pillar-aware (fires reliably on `MODE_B_FAILED_CLOSE_ESCAPE`); crash-path entry gets `_rollback_slot()` + `_drain_signal_queue()`.
- **Friday calibration alignment**: Step-2 (+2.2%) & Near-Target (+2.5%) doc↔code parity on 4 DTE.
- **VPS drift**: stale repo copies fixed (market_calendar Friday gate, strike_selector 0.50→4.00, shoonya_adapter), nested `common/common/` leftover deleted, SMC/ICT research junk + 7 stale test files purged from VPS `tests/`.

### Tests
- Suite **318 → 216 canonical** (76 retired tests → `tests/_archive/`; VPS/local 100% parity: 216 collected both sides, 0 failures; 2 data-dependent skips on VPS documented).
- **Fast dev suite ~15s** (was 210s): 1.8L-tick parity replay segregated behind `@pytest.mark.slow` (registered in `pytest.ini`).

### Ops
- Full VPS sync (both `/home/opc/` and `/home/opc/quant-bot/`); `aegis-bot` restarted, error-free boot, deep sleep until Monday 08:55 IST (NIFTY 1-DTE).

## [Unreleased] - 2026-10-16 (Dow Theory HL2 Strict Retracement Engine, Anti-Breakout Vetoes & ShadowBoundaryObserver)

### Added
- **Pillar 1 Classical Dow Theory HL2 Re-engineering (`AEGIS/core/price_action_engine.py`)**:
  - Re-architected `_validate_dow_hl2_lh2_structure()` to enforce the strict 5-phase Classical Dow Theory retracement lifecycle for 20 EMA trades:
    - **Established Uptrend Requirement**: Trend Age $\ge 10$ bars above the 20 EMA Band.
    - **Swing 1 ($HH_1 \rightarrow HL_1$)**: Impulse to $HH_1$ followed by first pullback touch $HL_1$ into 20 EMA Band (Strictly Skipped — no entry on 1st touch).
    - **Swing 2 ($HH_2$ BOS)**: Break of Structure confirming higher high ($HH_2 > HH_1 + \min(1.5, 0.15\times \text{ATR})$).
    - **Orderly Retracement**: 5 to 10 candles orderly retracement ($Descent \ge 60\%$) down to 20 EMA Band with institutional volume dry-up ($VDR \le 1.15$).
    - **Touch 2 ($HL_2$)**: Higher Low ($HL_2 > HL_1$) testing the 20 EMA Band with a confirmed Green Rejection Candle (Hammer or Bullish Expansion).
- **Zero-Tolerance Anti-Breakout Veto (`breakout_from_below_veto`)**:
  - Price must never have closed across the opposite side of the 20 EMA Band in the preceding 10 bars. Permanently eliminates "breakouts from below" masquerading as bullish pullbacks.
- **Live Price Piercing Invalidation Guard (`live_price_piercing_below_20_ema` / `live_price_piercing_above_20_ema`)**:
  - Live forming tick cannot pierce across the 20 EMA opposite band during entry evaluation, immunizing entries against intra-candle breakdown.
- **12.0s Bar Close Freshness Window (`stale_bar_entry_window_expired`)**:
  - Enforces execution within the first 12 seconds of candle formation, preventing stale momentum fills.
- **New `ShadowBoundaryObserver` Module (`AEGIS/core/shadow_boundary_observer.py`)**:
  - Deployed autonomous shadow paper trading engine tracking First-Touch Pain Zone and 200 EMA rejections on OTM 5 strikes with empirical 30s, 60s, and 300s MFE/MAE telemetry.
  - Seamlessly wired into `AEGIS/aegis_gamma_engine.py` utilizing live shared indicator feeds (zero duplicate computation).
  - 3 unit tests added in `tests/test_shadow_boundary_observer.py` (100% green).
- **L2 Archiver Cross-Strike Forensic Audit (`scripts/audit_cross_strike_mfe_1311.py`)**:
  - Analyzed 12,290 ticks across 29 distinct CALL strikes (from 22400 to 23800 CE) for Trade #37.
  - Discovered Delta Elasticity Sweet Spot at OTM 4-5 (+2.96% MFE) and proved underlying spot move (+3.5 pts) was too shallow for +4% TP.
- **Trade Ledger & Journal Reconciliation (`docs/AEGIS_TRADE_JOURNAL.csv`, `docs/TRADE_JOURNAL.md`, `docs/AEGIS_TRADE_JOURNAL.xlsx`)**:
  - Synchronized Trades 25 through 37 (covering 09 Oct through 16 Oct 2026), bringing total recorded institutional trades to 37.

### Tests
- Full test suite `python -m pytest tests/ -q`: 282+ tests passing 100% green.

## [Unreleased] - 2026-10-15 (AEGIS v1.0 Production: 4-Pillar Human Geometry, Telemetry Integrity & 3-Tier Exit Pricing)

### Added
- **Human-Eye Geometry Upgrades Across 4 Pillars (`AEGIS/core/price_action_engine.py`)**:
  - **Pillar 1 (20 EMA Pullback)**: Two-bar retest confirmation path added (`two_bar_retest_confirmed`), strict opposing wick cap $\le 15\%$ (two-bar $\le 18\%$), outer 40% close edge $\ge 0.60$.
  - **Pillar 2 (Liquidity Sweep & Reclaim)**: Normalized sweep depth floor (`min_sweep_depth = max(1.5, (ref_spot / 24000.0) * 1.5, 0.08 * atr)`), opposing wick cap $\le 15\%$, close edge $\ge 0.55$, lower wick $\ge 1.25\times$ upper wick.
  - **Pillar 3 (Volatility Squeeze Breakout)**: Normalized box clearance (`min_box_clearance = max(2.0, (ref_spot / 24000.0) * 2.0, 0.08 * atr)`), opposing wick cap $\le 18\%$, close edge $\ge 0.55$, body $\ge 0.35\times$ range.
  - **Pillar 4 (0-D Prominence Hub Breakout)**: Normalized hub clearance (`min_hub_clearance = max(2.0, (ref_spot / 24000.0) * 2.0, 0.08 * atr)`), directional expansion body $\ge 0.35\times$ range, close edge $\ge 0.55$, opposing wick $\le 20\%$.
- **Post-Trade MFE & MAE Reconciliation (`AEGIS/aegis_execution_manager.py`)**:
  - Aligned peak MFE and MAE with realized exit execution price (`highest_px = max(highest_px, exit_px)` and `peak_mfe_pct = max(peak_mfe_pct, pnl_pct)`), eliminating telemetry sampling artifacts where fast limit order fills (3–15s) showed $Exit > MFE$.
- **Wyckoff Volume & OFI Gate Dual-Regime Formalization (`AEGIS/core/wyckoff_volume_engine.py`)**:
  - Explicitly differentiated breakout volume thrust ($VDR \ge 1.0$) from 20 EMA pullback dynamics (Regime A: Supply Dry Test $VDR \le 0.80$ vs Regime B: Stopping Absorption with rejection wick $\ge 60\%$).

### Changed
- **Smart Maker TP Floor & 3-Tier Urgency Exit Pricing (`AEGIS/aegis_execution_manager.py`)**:
  - Replaced hard-sleep chaser with 3-tier cascade: (1) In-place modify resting TP order, (2) Marketable limit order with dynamic tick buffer (`get_marketable_exit_limit_price`), (3) Fallback limit order.
  - Near-target profit lock pullback threshold updated to `0.8%` (`NEAR_TARGET_PULLBACK_TOLERANCE = 0.008`) from peak bid $\ge +3.5\%$.
- **Permanent Decommissioning & Cleanup**:
  - Permanently removed premature 3-Minute Profit Harvest choke (`EX_HARVEST`), granting green trades full 300s baseline runway.
  - Disabled live CatBoost ML Gatekeeper (shadow telemetry only; Kyle Lambda inactive).
  - Deleted legacy Gemini 3.8 / `ai_shadow_reviewer.py`.
- **Dual-Side Option Universe Robustness (`AEGIS/execution/data_fetcher.py`)**:
  - Enforced continuous concurrent CE + PE option side polling and subscription, permanently preventing single-sided universe blindness and zero-volume synthetic delta artifacts (`test_universe_side_split.py`).

### Tests
- Full test suite `python -m pytest tests/ -q`: 133 core tests passing 100% green.

## [Unreleased] - 2026-09-28 (AEGIS V4.1 Pain Zone Band Only: Single-Source Veto)

### Added
- **Pain Zone band single source (`AEGIS/core/price_action_engine.py`)**: `get_pain_zone_vwaps()` now also returns `zone_top` (max of 4 lines), `zone_bottom` (min of 4 lines) and `zone_mid` (derived composite = total PV / total Vol). No separate composite counter — composite is derived from the same four buckets.
- **Absolute Pain Zone Band Veto V4.1 (`AEGIS/aegis_gamma_engine.py`)**: Tick-top early return replaces the old composite-VWAP veto. Spot above `zone_top` → only CALLs pass; spot below `zone_bottom` → only PUTs pass; spot inside the band → chop discard. Warmup (< 200 lots) keeps the veto inactive.

### Changed
- **Second-place Pain Zone confluence removed (`AEGIS/core/price_action_engine.py`)**: `evaluate_setup()` no longer calls `evaluate_pain_zone_confluence()` inside P1–P4. Pillars trigger on pure price action; the band veto at tick top is the single gate. Rationale: 21 Sep audit showed all 4 lines + composite clustered in a ~3.9 pt mean band, so double filtering added complexity without edge.

### Tests
- Full suite `python -m pytest tests/ -q`: 236 passed, 0 failed.

### Fixed (V4.1b, same day)
- **Single-side universe blindness (`AEGIS/aegis_main.py`, `AEGIS/execution/data_fetcher.py`)**: CE/PE split used `str.contains(r'CE|CALL')` which silently drops single-letter `C` series (NIFTY29SEP26C22700); a partial searchscrip fetch could then blind half the flow with zero log noise. Split now uses format-robust `is_call_symbol` + `split_option_sides`; loop never adopts a single-sided universe (keeps last good); boot retries until both sides present.
- **Band veto hair-trigger (`AEGIS/core/price_action_engine.py`, `AEGIS/aegis_gamma_engine.py`)**: raw-edge gating vetoed ~90% of ticks and whole minutes single-sided on 1-2 pt margins. New `band_veto_blocks()` adds a 0.3x ATR buffer (UP blocked only below top-buf, DOWN only above bottom+buf) plus per-minute BAND DIAG observability. Veto rate now ~10-25% with both sides flowing every minute.

## [Unreleased] - 2026-09-21 (AEGIS V4.0 Live Wiring: Premium SL, Confirm15, Single-Target)

### Added
- **Premium Hard SL circuit breaker (-3.5%, `AEGIS/aegis_execution_manager.py`)**: Tick-cadence (2 Hz poll) option premium floor evaluated immediately after the TP checks and before all lagging spot-level exits. Closes 100% quantity the instant bid drops >= 3.5% under entry — fixes the 2026-09-21 Trade 3 leak (-8.2% via delayed Trend Reversal Exit).
- **Confirm15 futures-confirmation gate (wired, `AEGIS/aegis_gamma_engine.py`)**: Pillar fire now ARMs a per-symbol `Confirm15StateMachine`; entry proceeds only after continuous front-month futures flow TRIGGERs (z15 >= 0.75, ER >= 0.55, elastic phi, r_opt >= +1.0%, 0.45x ATR invalidation, 45s timeout). Fail-open while the futures feed is blind; fail-closed once armed. Mode via `confirm15_mode` param (`ACTIVE`/`SHADOW`/`DISABLED`).
- **V4 core robustness (`AEGIS/core/aegis_v4_core.py`)**: P4 anchors use genuine 0-D persistence prominence (pi-hat >= 1.75) instead of the degree-ratio proxy; P2 sweep requires a full 17-bar window; Top-5 spread guard enforces an absolute 1.0%-of-mid cap while unprimed; `arm()` refuses uninitialized futures references; standalone exit sender flagged as research-harness stub.

### Changed
- **Retired ladder stages removed (`AEGIS/core/price_action_engine.py`)**: `albrooks_2legged_pullback` and standalone `break_of_structure` no longer participate in `evaluate_setup` — strict P1 -> P2 -> P3 -> P4 single-admission ladder only.
- **Runner trailing disabled in live exits**: `V4_SINGLE_TARGET_MODE = True` gates the legacy Dynamic Runner Trailing block — 100% quantity TP/SL/time exits only.
- **Confirm15 relocated post-gates/pre-commit (2026-09-22)**: ARM now happens after conviction/ML/quality pass on the same tick (snapshot stored for telemetry); TRIGGER path is fresh depth guard + fire only. Slot consume, FSM registration and outcome telemetry run solely on TRIGGER pass-through, never on ARM. Resilient optional imports — one missing module can never kill engine init.
- **Shield v2 sweep-trap guards (2026-09-22, in existing files)**: `delta_inflection_check` in `aegis_gamma_engine.py` (I1 sign-flip ≥ 0.15 against direction vetoes, fail-open under 6 ticks) and `get_oi_kinematics` in `oi_builtup_gate.py` (60s OI velocity + acceleration, fresh-dumping veto below -1.0%/min, fail-open on sparse history). Both wired post-TRIGGER/pre-depth as blocking vetoes. 6 new tests (201 green total).
- **Backtest/live parity (`AEGIS/backtest_engine.py`)**: Default `stop_loss_pct` 5.0 -> 3.5 plus per-tick `PREMIUM_HARD_SL` matching the live breaker.

### Tests
- Full suite `python -m pytest tests/ -q`: 195 passed, 0 failed.

## [Unreleased] - 2026-09-20 (AEGIS V3.9 Dual-Timeframe HVG Topological Engine & Architecture Renaming)

### Added
- **AEGIS V3.9 Architecture Authority Specification (`AEGIS/AEGIS_V3_ARCHITECTURE.md`)**: Comprehensive, code-grounded definitive specification superseding `AEGIS V3.md` and `AEGIS V2.md`. Eliminates whitespace in file paths, updates line counts across all 28 AEGIS modules, details the active 4 Price Action Scalper Pillars, and documents the full 277-test suite matrix (100% green).
- **Dual-Timeframe (1m+5m) Horizontal Visibility Graph (HVG) Engine (`AEGIS/core/hvg_engine.py`)**: Strict equality line-of-sight in $O(N)$ monotonic stack, IST clock-aligned 5m aggregation, day rollover separation, macro structural alignment & overhead proximity shield, and 400ms Anti-Spoofing L2 Wall Shield.
- **Topological Structural Sweep & Reclaim (`check_topological_sweep_reclaim`)**: Causal $T-1$ frozen graph, $T-1$ seeded unbroken anchor tracking (`post_anchor_min = lows[pre_n - 1]`), local extremum validation, signed VWAP distance $D_{VWAP}$, and explicit immutable A/B experiment arms (`TOPO_P2` vs `LEGACY_P2`) logged to `logs/hvg_shadow_telemetry.jsonl`.
- **Dispatcher Single-Admission Invariant**: Deterministic first-hit priority resolution (P1 $\to$ P2 $\to$ P3 $\to$ P4) ensuring exactly one setup is admitted per candle without duplicate orders.

### Changed
- Renamed architecture documentation authority from `AEGIS/AEGIS V3.md` to `AEGIS/AEGIS_V3_ARCHITECTURE.md` across `SYSTEM_ARCHITECTURE.md`, `README.md`, and `AEGIS/AEGIS V2.md`.

## [Unreleased] - 2026-09-16 (BOS Breakout Quality + Strict Micro Gate)

### Added
- **BOS Confirmation Quality Filter (`AEGIS/core/price_action_engine.py`)**: Pending-BOS break bars must now carry momentum — range >= 0.30x ATR(14), body >= 40% of range, close in the outer 35% edge, extension past the level <= max(1.0x ATR, buffer). Weak breaks are discarded (`bos_weak_breakout_discarded` / `bos_weak_breakdown_discarded`) so a genuine retest can arrive via a fresh signal instead of chasing chop.
- **BOS-Strict Micro Gate (`AEGIS/core/adaptive_micro_validator.py`, `strict_bos` flag)**: BOS-breakout confirmations need energy >= 70.0 plus three vetoes — 10s momentum acceleration >= 0, direction-aligned touch (I1 >= +0.10 CALL / <= -0.10 PUT), no deep L3-L5 wall (PUT/CALL mirrored at +/-0.60). Wired in `aegis_gamma_engine.py` for `bos_breakout_confirmation` only; all other setups keep legacy 60.0 behaviour. Exit logic untouched (full-lot model, no partial exits).

### Tests
- New weak-break discard tests (CALL + PUT) in `tests/test_price_action_engines.py`; new strict-gate tests (aggression veto, deep-wall veto, genuine pass) in `tests/test_adaptive_micro_validator.py`.

## [Unreleased] - 2026-09-11 (AEGIS V3.5 Pure Price-Action Primary + Quantitative Exits & Semantic Architecture Authority)

### Added
- **AEGIS V3 Architecture Authority Specification (`AEGIS/AEGIS V3.md`)**: Comprehensive, code-grounded L1 architecture specification superseding `AEGIS V2.md`. Documents all 3 Price Action Scalper Pillars, 09:14 AM pre-market auto-seeding, secondary microstructure shield, and Claude's 4-pillar adaptive quantitative exits with exact code line numbers, mathematical formulations, and governance laws.
- **Master Semantic Architecture Graph**: End-to-end Mermaid flowchart embedded in `AEGIS/AEGIS V3.md` visualizing tick ingestion, spot path, macro PA trigger, event-bound anchor $T$, microstructure shield, execution manager, and quantitative exit lifecycle.
- **Pre-Market 09:14 AM Bar Seeding (`aegis_main.py:260-312`)**: Eliminates 15-minute cold-start warmup by pre-warming `PriceActionEngine` with historical 1-minute bars from Shoonya REST API, ensuring mature 20 EMA, 50 EMA, and ATR from tick 1 (09:15:01 AM).
- **Execution Guardrails (`aegis_execution_manager.py`)**:
  - Strict non-consecutive setup lockout (`incoming_route == last_exited_route` blocked for 180s until an alternate route executes).
  - 30-second Squeeze Momentum Timeout (+1.5% hurdle) to cut false breakouts before theta decay.
  - Resting Maker Limit TP at +4.0% with zero slippage.
  - Two-key breakeven guard (2.5x noise floor hurdle + 60s hold / 3.5 bps spot move).

### Changed
- Promoted `PriceActionEngine` to primary macro signal trigger; non-PA ticks are immediately discarded without buffering or scoring.
- Updated `SYSTEM_ARCHITECTURE.md` and `README.md` to reference `AEGIS/AEGIS V3.md`.
- Added deprecation/redirection banner to `AEGIS/AEGIS V2.md`.

## [Unreleased] - 2026-08-30 (AEGIS V3.2 Macro-Alignment & 5-Min Consistency Calibration)

### Added
- **Macro-Alignment Gate (`aegis_gamma_engine.py:316-330`)**: Zero-copy $O(1)$ cash index trend agreement check. Requires 60s and 180s underlying spot momentum to agree with trade direction before firing signals (CE blocked if spot drift negative, PE blocked if spot drift positive). Discovered to provide a massive +76.5% win rate edge in multi-day empirical replays.
- **5-Minute Consistency Retraining Lab (`AEGIS/research/aegis_5min_consistency_lab.py`)**: Standalone cross-validation and feature gain ranking harness covering 96,098 candidate trade decisions across 5 days (NIFTY + SENSEX) under realistic SEBI statutory friction (`calculate_nse_option_charges`).
- **Put-Call Parity Synthetic Spot Engine**: Automatically synthesizes continuous millisecond-precise spot index streams for days where explicit spot ticks are missing in L2 archives ($S_t = K + C_t - P_t$).
- **5-Min Calibrated Weights (`microstructure_weights_v2_5min.json` & updated `microstructure_weights.json`)**: Empirical drop-in linear logit weights aligned to +5% TP / -3.5% SL / 300s holding horizon. Resolves previously deferred item **Q-1**.

### Changed
- **Empirical Feature Hierarchy**: Proved mathematically via LightGBM gain ranking that Macro / Spot Trend dominates Microstructure by 15x–25x in gain for a 5-minute holding horizon (`min_from_open`, `spot_range_pos`, `spot_ret_300_signed`, `spot_ret_180_signed` take top ranks).

## [Unreleased] - 2026-08-29 (Quant Audit Round 2)

### Fixed
- **Critical (Q-5)**: Futures token was routed into option scorer as `strike=0 CE`, corrupting cross-strike coherence + `microstructure_weights` dataset. `on_tick_parsed` now skips `is_futures` tokens.
- **High (Q-4)**: TimeBar permission slope veto used absolute `±0.5` points (non-scale-invariant across NIFTY 25k vs SENSEX 78k). Now normalized to basis points via `last_price`, threshold `slope_veto_bps=2.0` (reproduces prior NIFTY behavior).
- **High (Q-7)**: Backtest used lot_size 25 (should be 65 NIFTY / 20 SENSEX) and flat ₹16.50 charges (no STT/exchange/SEBI/stamp/GST). Now SEBI-correct lot + `calculate_nse_option_charges` statutory model.
- **Medium (Q-6)**: FSM `last_trigger_ts` was reset every poll, so `trigger_age` was always ≈0 and the 3s/8s freshness windows were dead. Now `last_trigger_ts` records trigger inception (set on PRIME transition).
- **Medium (Q-3)**: ATR baseline used all 100 history bars (labeled "30-min"); now uses `atr_baseline_bars=30` correctly.

### Deferred (require data/calibration, not code)
- Q-1: scorer weight sign-contradiction (`tick_intensity +4.53` vs `pre_ret -1.77` × OTM returns negative) — needs OOS recalibration.
- Q-2: 9 computed-but-weightless features in hot path — retained for forward research data.
- R-1: bounded hot-path dict alloc (`otm_rets`, `features`) — refactor risk > benefit.
- A-1: `ARGUS.tick_compiler` cross-engine coupling — architectural.

## [Unreleased] - 2026-08-20

### Fixed
- **Critical**: `AegisGammaEngine.on_fill` signature mismatch with `AegisExecutionManager._chase_exit_thread` call (extra `sym` kwarg caused TypeError on every fill, potentially leaving zombie open positions).
- **Critical**: Exit chaser Step 3 incorrectly polled the cancelled order id; now captures new order id from aggressive exit placement and confirms fill before marking closed.
- **High**: Engine feedback latency (`signal_ts`) now uses the signal queue timestamp stored in trade context at entry, improving latency-decay accuracy.
- **Medium**: Bucket map literal `5_00_00_0.0` cleaned to `500_000.0` with comment.
- **Low**: Minute digest and IPC payload updated to reflect empirical microstructure scorer (removed misleading gamma-sum/hawkes-B zeros from console log; IPC retains keys for dashboard schema compatibility with zeros/scoring flags).

### Changed
- Updated `AEGIS/AEGIS V2.md` module map, ownership rules, and responsibility boundaries to match current flat repo structure with `core/` and `execution/` subdirectories.
- Engine now relies on `AEGIS.core.microstructure_scorer` for per-tick confidence scoring; hot path remains O(1) with zero allocation in scoring update.
- Execution manager uses shared `common.execution` components (capital engine, order lifecycle, strike selector) unchanged.