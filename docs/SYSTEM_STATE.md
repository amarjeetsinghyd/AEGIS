# SYSTEM STATE (Active Production Anchor)

> **Last Updated:** 14 November 2026 | **Active Production Engine:** AEGIS (v1.0 Locked) | **Status:** 100% De-bloated & Modularized

---

### Core Entry Points
- **AEGIS (Options Gamma Engine):** `main.py` (NIFTY/SENSEX 0DTE/1DTE options, PM2: `aegis-bot`, cwd: `/home/opc/aegis`).
- **Dashboard UI:** `dashboard/dashboard.py` (aiohttp telemetry port 8080, PM2: `aegis-dashboard`).
- **Backtest Simulator:** `backtest.py` (Full 5-pillar historical replay with tick/OHLC cache).

### Active Data Flow & IPC
- **Market Data Ingress:** Single Shoonya WebSocket feed demuxed via `market/tick_compiler.py` (L1 + top-5 L2 depth).
- **IPC & Runtime State:** Shared state written via `ipc/live_state.py` to `data/ipc/aegis_live_state.json`.
- **Persistent Storage:** Snappy Parquet partitioners (`core/l2_options_archiver.py`) flushing every 15 min.
- **Session Cache:** Single daily token in root `data/shoonya_session.json` (lockfile protected; weekly `tokens_cache.json` refresh).

### Ground Truth Invariants (AEGIS v1.0 Production Locked)
- **Strategy Architecture:** 5 Macro Price Action Pillars + Priority Mode B In-Flight Mid-Bar Breakthrough ($T:02s+$ onwards).
- **Execution Architecture:** Zero-Regression 5-Mixin Architecture (`execution/execution_manager.py` composing `EntryEngineMixin`, `TradeMonitorMixin`, `ExitChaserMixin`, `TradePersistenceMixin`, and `QualitySlotPolicy` / risk constants).
- **Order Execution:** Strict 1-Lot Maker TP at constant **+4.0%** Mon–Thu (**+3.0%** Friday 4-DTE), atomic 0.75s fill window, 0 order chasing.
- **Stop-Loss Invariant:** Universal Pure Price Action Spot Invalidation ($\text{CALL SL} = \text{Entry Candle Low} - 0.10\times\text{ATR}$, $\text{PUT SL} = \text{Entry Candle High} + 0.10\times\text{ATR}$). Option premium disaster floor is decommissioned by default (`AEGIS_ENABLE_PREMIUM_DISASTER_FLOOR = "0"`).
- **Risk Governance:** Strict 3 trades/day (`MAX_TRADES_PER_DAY = 3`), Near-Target Profit Lock (+3.5% peak, 0.8% pullback), Cost-Shield Scratch (+3.5% peak $\to$ 0.0% breakeven), 300s max hold, and 15:08:00 IST CAS force-flatten.
- **Decommissioned Invariants:** `kronos_bot` and `ARGUS` are permanently decommissioned. Legacy Mode A ladders are permanently decommissioned. CatBoost ML Gatekeeper is permanently removed. `_archive/` is strictly read-only history.
