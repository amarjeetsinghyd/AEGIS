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
- **Locked AEGIS Production v1.0 Parameters:** Strict 1-Lot Maker TP at constant **+4.0%** (resting limit order at entry + 4.0%), Hard Disaster SL Floor at **-12.0%**, Step 1 Safety Net (peak >= +3.0% compresses SL to -3.0%), Step 2 Cost-Shield Scratch (peak >= +3.5% compresses SL to 0.0% breakeven), Near-Target Profit Lock (peak >= +3.5% exits on >= 0.8% pullback from peak bid), 300s max hold, 60s inter-trade cooldown / 120s post-loss cooloff, 15:08:00 IST CAS force-flatten.
- **Human-Eye Geometry across 4 Pillars:**
  - Pillar 1 (20 EMA Rejection): Two-bar confirmation path added, upper wick <= 15% (two-bar <= 18%), close edge >= 0.60.
  - Pillar 2 (Liquidity Sweep & Reclaim): min_sweep_depth normalized, upper wick <= 15%, close edge >= 0.55, lower wick >= 1.25x upper wick.
  - Pillar 3 (Volatility Squeeze Breakout): min_box_clearance normalized, opposing wick <= 18%, close edge >= 0.55, body >= 0.35x range.
  - Pillar 4 (0-D Prominence Hub Breakout): min_hub_clearance normalized, directional expansion body >= 0.35x range, close edge >= 0.55, opposing wick <= 20%.
- **Wyckoff Volume & OFI Gate:** Dual-regime P1 (Supply Dry Test VDR <= 0.80 vs Stopping Absorption with rejection wick >= 60%), Breakout volume thrust (VDR >= 1.0) on P3/P4.
- **Decommissioned Invariants:** `kronos_bot` is permanently decommissioned. CatBoost ML Gatekeeper is permanently disabled / shadow-only. Gemini 3.8 / `ai_shadow_reviewer.py` is deleted. 3-Minute Time Harvest is permanently removed. `_archive/` is strictly read-only history.
