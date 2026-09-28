# SYSTEM STATE (Active Production Anchor)

### Core Entry Points
- **ARGUS (Equities Scalper):** `ARGUS/main.py` (NSE Cash, zero-copy `TickCompiler`, PM2: `argus`).
- **AEGIS (Options Gamma Engine):** `AEGIS/aegis_main.py` (NIFTY/SENSEX 0DTE options, PM2: `aegis-bot`).
- **Dashboard UI:** `unified_dashboard/dashboard.py` (Flask telemetry port 5000, PM2: `unified-dashboard`).
- **EOD Calibration:** `ARGUS/eod_calibrator.py` (Daily 16:00 IST cron, PM2: `eod-calibrator`).

### Active Data Flow
- **Market Data Ingress:** Single Shoonya WebSocket feed demuxed via `TickCompiler` (L1 + top-5 L2 depth).
- **IPC & Runtime State:** Shared state written via `common/ipc/live_state.py` to `/dev/shm/*.json` and `data/ipc/`.
- **Persistent Storage:** Passive Snappy Parquet partitioners (`ARGUS/l2_archiver.py`, `AEGIS/l2_options_archiver.py`) flushing every 15 min.
- **Session Cache:** Single daily token in `data/shoonya_session.json` (lockfile protected; weekly `tokens_cache.json` refresh).

### Ground Truth Rules
- **Locked AEGIS Parameters:** +6.5% Baseline TP (DAY Limit, dynamic 5.5%–10.0%), -5.0% Hard SL, Breakeven Guard (+3.0% arm / +0.5% fee lock), Runner Lock (+7.0% arm / +5.0% floor, 2.0% trail), 300s max hold, 120s cooldown, max 3 trades/day.
- **45s Microstructure Impulse Memory Window (`impulse_validity_window_sec = 45.0s`):** Bridges the sub-second order book spike with the 30s–60s macro spot confirmation lag. Caches strong impulses (score $\ge 75.0$) for 45s; permits execution at moderate score ($\ge 60.0$) or active memory ($\ge 55.0$) once macro drift confirms $\ge 1.0$ bps.
- **Real-Time OI Built-Up & Wall Gate (`OIBuiltupGate`):** 4-quadrant real-time classification (Long Buildup, Short Covering, Short Buildup, Long Liquidation). Tracks ceiling resistance (max CE OI strike) and floor support (max PE OI strike). Normalized strike-step wall defense ($D_{\text{strikes}} = \text{dist}/\text{strike\_step}$: Zone 1 $\le 0.40$ strikes, Zone 2 $\le 1.00$ strike) with 3-minute rolling unwinding velocity ($\le -1.0\%$ Zone 1, $\le -0.5\%$ Zone 2 in 3m) and squeeze breakout acceleration.
- **Microstructure Gates:** EQS $\ge 70.0$, 60s Spot Drift $\ge \pm 1.0$ bps (agile momentum target), Directional Displacement Override $\ge 2.5$ pts (NIFTY) / $\ge 5.0$ pts (SENSEX), KER floor 0.04, dynamic 9-EMA slope aligned, market structure unopposed.
- **Decommissioned Invariant:** `kronos_bot` is permanently retired. `_archive/` is strictly read-only history.
