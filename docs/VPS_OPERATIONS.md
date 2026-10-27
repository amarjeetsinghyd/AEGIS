# VPS Operations Guide

**Host:** `137.23.41.38` (Oracle Cloud, Ubuntu)  
**User:** `opc`  
**Repo path on VPS:** `/home/opc`  
**SSH key (local):** `Oracle SSH keys/ssh-key-2026-07-11.key`  

---

## 1. Accessing the VPS

```bash
ssh -i "Oracle SSH keys/ssh-key-2026-07-11.key" opc@137.23.41.38
```

> **Note:** The SSH key is the **only copy**; never duplicate it into an engine subfolder.

---

## 2. Process Manager (PM2)

The VPS uses **PM2** to manage all services. The configuration file is `ecosystem.config.js` at the repo root.

### Common PM2 Commands

| Command | Description |
|---------|-------------|
| `pm2 list` | Show all managed apps and their status (online, stopped, errored, etc.) |
| `pm2 start <app-name|ecosystem.config.js>` | Start an app or all apps from the ecosystem file |
| `pm2 restart <app-name|all>` | Restart one or all apps |
| `pm2 stop <app-name|all>` | Stop one or all apps |
| `pm2 delete <app-name|all>` | Remove an app from PM2's process list |
| `pm2 logs <app-name>` | Stream logs for an app (add `--lines 100` to see recent lines) |
| `pm2 flush` | Clear all logs |
| `pm2 update` | Update PM2 itself (run as needed) |
| `pm2 startup` | Generate and enable a startup script to resurrect PM2 on reboot |
| `pm2 save` | Save the current process list for auto‑resurrection |

### Active Services (as per `ecosystem.config.js`)

| App Name | Role | Notes |
|----------|------|-------|
| `aegis-bot` | AEGIS options strategy bot | Primary execution engine for AEGIS V2.0 |
| `argus` | ARGUS HFT microstructure equities engine | Primary active system for equities |
| `unified-dashboard` | Monitoring UI (Quant Console) | Reads `/dev/shm` + IPC files; shows live P&L, state, etc. |
| `l2-archiver` | Level‑2 data archiver | Writes raw L2 ticks to parquet for post‑market analysis |
| `eod-calibrator` | End‑of‑day calibration runner | Runs walk‑forward optimization overnight; produces calibrated param files |
| `shoonya-data-hub` | Broker WebSocket daemon (standalone) | **Must be started before** any bot that consumes ticks |

> **Important:** The `shoonya-data-hub` process is **not** managed by PM2; it is a standalone daemon that must be started manually (see Section 3).

---

## 3. Starting / Stopping the Broker Daemon

The Shoonya WebSocket daemon (`shoonya-data-hub`) runs outside PM2. It publishes L1/L2 tick data to `/dev/shm/shoonya_live_ticks.json` at 1 Hz.

### To start it manually:

```bash
# From the repo root on the VPS:
cd /home/opc
nohup python shoonya_data_hub/main.py > shoonya_data_hub.log 2>&1 &
```

> **Tip:** You can wrap this in a simple systemd service if you prefer automatic restarts.

### To stop it:

```bash
pkill -f "shoonya_data_hub/main.py"
```

### Restart order (important)

1. **Start** `shoonya-data-hub` (daemon).
2. **Start** the bots (`aegis-bot`, `argus-bot`, etc.) via PM2.
3. **Start** the dashboard (`unified-dashboard`) – it can run anytime but will show no data until the hub and bots are running.

When stopping, reverse the order: stop bots first, then the hub (though stopping the hub while bots are running will cause them to warn about missing ticks; they will attempt reconnects).

---

## 4. Logs

All engine logs are stored in each engine's `logs/` directory:

- AEGIS: `/home/opc/AEGIS/logs/aegis_bot.log` (also mirrored in PM2’s `out`/`err` logs)
- ARGUS: `/home/opc/ARGUS/logs/argus.log`
- Unified dashboard: `/home/opc/unified-dashboard/logs/`
- L2 archiver: `/home/opc/AEGIS/logs/l2_archiver.log` (if separate)

PM2 also captures `stdout`/`stderr` for each app in:

```
/home/opc/.pm2/logs/<app-name>-out.log
/home/opc/.pm2/logs/<app-name>-error.log
```

Use `pm2 logs <app-name>` to tail the combined output.

---

## 5. Configuration & Constants

### Engine‑specific settings

- `AEGIS/config/settings.py` – AEGIS‑specific constants (lot sizes, tick sizes, paths, e.g., `NIFTY_LOT_SIZE`, `OPTION_TICK_SIZE`).
- `AEGIS/config/market_calendar.py` – Market‑hours helpers (`is_market_open`, `is_bot_active_hours`, etc.).
- `common/config/settings.py` – Shared constants used by multiple engines (e.g., `ENABLE_LIVE_BROKERAGE_EXECUTION`).

### Parameter files (produced by calibration)

- AEGIS: `/home/opc/AEGIS/data/params/aegis_calibrated_params.json`
- Microstructure scorer weights: `/home/opc/AEGIS/data/params/microstructure_weights.json`

> **Never edit these JSON files by hand.** They are the output of `calibration_runner.py` (see Section 6).

---

## 6. Calibration & Parameter Updates

AEGIS uses a **calibration runner** to walk‑forward optimize parameters on 6 months of L2 data.

### To run a calibration (example for BANKNIFTY):

```bash
cd /home/opc/AEGIS
python -m calibration_runner --symbol BANKNIFTY --window 6M
```

The runner will:

1. Load historical L2 data from the parquet archive (`/home/opc/AEGIS/data/aegis_local_l2_raw/`).
2. Optimize the empirical microstructure scorer weights (or other tunable parameters) using Optuna / scikit‑optimize.
3. Evaluate the candidate parameter set on an out‑of‑sample month.
4. If the validation report passes all guards (Sharpe > 1.5, gate precision > 85%, etc.), it will:
   - Write `aegis_calibrated_params_new.json` + an HTML validation report.
   - Prompt you to **atomically swap** the new file into place:
     ```bash
     mv aegis_calibrated_params_new.json aegis_calibrated_params.json
     ```
5. Optionally, trigger a hot‑reload of the running bot via the admin endpoint:
   ```bash
   curl -X POST http://localhost:8080/admin/reload_params
   ```
   (See Section 7 for the admin server.)

> **Safety:** The calibration runner never overwrites the live param file unless you explicitly approve the swap.

---

## 7. Admin Server (Hot‑Reload & Kill Switch)

AEGIS ships a lightweight Flask‑based admin server (`aegis_bot/admin/server.py`) that exposes HTTP endpoints for operational control.

### Endpoints (default port 8080; bind to localhost only for security)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/admin/reload_params` | `POST` | Hot‑reloads `aegis_calibrated_params.json` (validates schema first). |
| `/admin/force_flatten` | `POST` | Sets `EXIT_PENDING`, cancels TP, and chases exit immediately. |
| `/admin/ws_reconnect` | `POST` | Forces the Shoonya WebSocket to reconnect and resubscribe. |
| `/admin/clear_iv_cache` | `POST` | Resets the IV tensor to NaN, forcing a re‑solve on the next bucket. |
| `/admin/state` | `GET` | Returns a JSON snapshot of the engine state (spot, atm_iv, gamma_sum, etc.). |
| `/admin/health` | `GET` | Simple liveness check (used by Kubernetes/liveness probes). |

> **Security:** The admin server binds to `127.0.0.1` only. To access it from your local machine, create an SSH tunnel:
> ```bash
> ssh -i "Oracle SSH keys/ssh-key-2026-07-11.key" -L 8080:localhost:8080 opc@137.23.41.38
> ```
> Then visit `http://localhost:8080/admin/state` in your browser.

---

## 8. Daily Operational Routine

### Pre‑market (≈08:55 IST)

1. Ensure `shoonya-data-hub` is running (start if not).
2. Verify the bots are running (`pm2 list`; they should be online but in deep sleep until market opens).
3. Optionally, check the admin `/state` endpoint to confirm the engine has loaded params and is ready.

### Market open (09:15 – 15:30 IST)

- Monitor the unified dashboard for live P&L, signal frequency, fill latency, etc.
- Watch the console logs (`pm2 logs aegis-bot`) for any warnings (e.g., spread too thin, entry blocked).
- If you need to halt trading immediately, use the admin `/force_flatten` endpoint.
- If you suspect bad params (e.g., sudden drawdown), consider triggering a reload via `/admin/reload_params` (after verifying a new param file has been swapped in).

### Post‑market (after 15:30 IST)

1. The bots will automatically stop entering new trades at 15:00 and force‑flatten at 15:10.
2. After the bots flatten, you may:
   - Run the EOD calibrator (`pm2 restart eod-calibrator`) to generate fresh params for the next day.
   - Archive or back up the day’s logs and raw L2 data (the l2-archiver already writes partitioned parquet files).
3. Optionally, stop the `shoonya-data-hub` daemon if you wish to free resources (it will restart automatically at the next pre‑market if you have a cron or systemd timer).

---

## 9. Troubleshooting Common Issues

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| Bot logs show `ShoonyaWebSocketAdapter: WS connection lost` intermittently | Temporary network blip or broker side issue | The adapter includes automatic reconnect; ensure `shoonya-data-hub` is running and the VPS has stable internet. If persistent, check VPS firewall/router. |
| `aegis_bot` logs: `AEGIS on_tick_parsed error: …` | Exception in the tick processor (should be caught and logged) | Check the traceback; if it repeats, consider disabling the bot and investigating the offending tick (often a malformed Shoonya message). |
| Bot shows `PAPER mode` but you expected live | `ENABLE_LIVE_BROKERAGE_EXECUTION` is `False` in `common/config/settings.py` | Set to `True` **only** when you have verified the system on paper and are ready to go live. Remember: live mode uses real money and Shoonya credentials. |
| Dashboard shows stale data or no updates | `/dev/shm/shoonya_live_ticks.json` not updating | Verify `shoonya-data-hub` is running and publishing. Check its log (`shoonya_data_hub.log`). |
| Bot does not emit signals despite ticks flowing | Scorer threshold too high or no microstructure signal in current regime | Check the admin `/state` for `scorer_active` and the minute‑digest for the conviction threshold. Consider temporarily lowering the threshold for testing (via a new param file and reload). |
| Position appears “stuck” (bot says OPEN but broker shows flat) | The exit chaser failed to confirm a fill (Step‑3 bug was fixed; if you see this, ensure you are running the patched code) | Use `/admin/force_flatten` to close any zombie positions; then verify your code includes the fixes for `on_fill` signature and Step‑3 ordno capture. |

---

## 10. Safety & Best Practices

- **Never run live with untested parameter changes.** Always run a calibration, validate the report, and swap the new file only after approval.
- **Always keep the Shoonya credentials (`tokens_cache.json`) secure and backed up** (they expire ≈24 h; refresh weekly).
- **The VPS should have a static egress IP** (required by the broker). Do not change the VPS’s network configuration without consulting the infrastructure owner.
- **Paper trading first.** Even though the system has no explicit “paper” flag, you can run in `PAPER mode` (set `ENABLE_LIVE_BROKERAGE_EXECUTION=False`) to validate logic against live ticks without sending orders.
- **Monitor fill latency and edge survival** via the admin `/state` endpoint or the unified dashboard; sudden increases may indicate broker slippage or network issues.
- **Respect market holidays.** The `market_calendar` helpers automatically skip weekends and NSE holidays; the bot will go into deep sleep. Do not attempt to force trades on a closed market—the engine will reject them.
- **Keep the system updated.** Periodically pull the latest changes from the Git repository (after reviewing) and restart the affected services.

---

## 11. Quick Reference Cheat Sheet

```bash
# === Access ===
ssh -i "Oracle SSH keys/ssh-key-2026-07-11.key" opc@137.23.41.38

# === PM2 ===
pm2 list
pm2 restart aegis-bot
pm2 logs aegis-bot

# === Broker Daemon (shoonya-data-hub) ===
# Start:
nohup python shoonya_data_hub/main.py > shoonya_data_hub.log 2>&1 &
# Stop:
pkill -f "shoonya_data_hub/main.py"

# === Admin Server (localhost tunnel) ===
ssh -L 8080:localhost:8080 opc@137.23.41.38
# Then open: http://localhost:8080/admin/state

# === Calibration Example ===
cd /home/opc/AEGIS
python -m calibration_runner --symbol BANKNIFTY --window 6M
# After review:
mv aegis_calibrated_params_new.json aegis_calibrated_params.json
curl -X POST http://localhost:8080/admin/reload_params   # via tunnel

# === Force Flatten (emergency) ===
curl -X POST http://localhost:8080/admin/force_flatten   # via tunnel

# === Check VPS time (IST) ===
date   # Should show IST (UTC+5:30)
```

--- 

*This document is intended as a living guide. Update it whenever the VPS layout, service list, or operational procedures change.*