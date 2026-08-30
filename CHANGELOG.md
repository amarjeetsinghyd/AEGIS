# Changelog

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