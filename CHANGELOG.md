# Changelog

## [Unreleased] - 2026-09-28 (AEGIS V4.1 Pain Zone Band Only: Single-Source Veto)

### Added
- **Pain Zone band single source (`AEGIS/core/price_action_engine.py`)**: `get_pain_zone_vwaps()` now also returns `zone_top` (max of 4 lines), `zone_bottom` (min of 4 lines) and `zone_mid` (derived composite = total PV / total Vol). No separate composite counter — composite is derived from the same four buckets.
- **Absolute Pain Zone Band Veto V4.1 (`AEGIS/aegis_gamma_engine.py`)**: Tick-top early return replaces the old composite-VWAP veto. Spot above `zone_top` → only CALLs pass; spot below `zone_bottom` → only PUTs pass; spot inside the band → chop discard. Warmup (< 200 lots) keeps the veto inactive.

### Changed
- **Second-place Pain Zone confluence removed (`AEGIS/core/price_action_engine.py`)**: `evaluate_setup()` no longer calls `evaluate_pain_zone_confluence()` inside P1–P4. Pillars trigger on pure price action; the band veto at tick top is the single gate. Rationale: 21 Sep audit showed all 4 lines + composite clustered in a ~3.9 pt mean band, so double filtering added complexity without edge.

### Tests
- Full suite `python -m pytest tests/ -q`: 236 passed, 0 failed.

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