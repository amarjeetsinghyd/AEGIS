"""
Quant Console — dual-brand light precision dashboard for Astra + Kronos.
"""

from __future__ import annotations

import html
import json
import os
from datetime import datetime

from flask import Flask, jsonify

from common.config import INITIAL_PAPER_CAPITAL
from common.ipc import (
    get_astra_live_state,
    get_kronos_live_state,
    SHARED_TICK_FILE,
)
from common.runtime.paths import get_runtime_paths

app = Flask(__name__)

ASTRA_PATHS = get_runtime_paths("astra")
KRONOS_PATHS = get_runtime_paths("kronos")
ASTRA_DATA = ASTRA_PATHS["data_dir"]
KRONOS_DATA = KRONOS_PATHS["data_dir"]
ASTRA_LOGS = ASTRA_PATHS["logs_dir"]
KRONOS_LOGS = KRONOS_PATHS["logs_dir"]
ORDERFLOW_PATHS = get_runtime_paths("stock_order_flow")
ORDERFLOW_DATA = ORDERFLOW_PATHS["data_dir"]
ORDERFLOW_LOGS = ORDERFLOW_PATHS["logs_dir"]


def _read_json(path, default=None):
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def _capital(data_dir: str) -> float:
    state = _read_json(os.path.join(data_dir, "capital_state.json"), {}) or {}
    return float(state.get("available_funds") or 0.0)


def _active_trade(data_dir: str):
    data = _read_json(os.path.join(data_dir, "trades", "active_trade_context.json"), None)
    if isinstance(data, dict) and data.get("status") == "OPEN":
        return data
    return None


def _normalize_trade(row: dict) -> dict:
    """Flatten mixed Kronos / Astra / Order Flow history schemas into one dashboard row."""
    if not isinstance(row, dict):
        return {}
    # Prefer explicit rupee fields; ignore legacy SOF fractional `pnl` when net_pl missing
    pnl = row.get("net_pl")
    if pnl is None:
        raw_pnl = row.get("pnl")
        # Old Order Flow wrote pnl as a fraction (e.g. 0.005); do not treat as rupees
        if isinstance(raw_pnl, (int, float)) and abs(float(raw_pnl)) < 1 and row.get("strategy") == "order_flow":
            pnl = None
        else:
            pnl = raw_pnl
    if pnl is None:
        pnl = (row.get("post_trade_attribution") or {}).get("pnl_rs")
    reason = (
        row.get("reason")
        or row.get("exit_reason")
        or (row.get("post_trade_attribution") or {}).get("exit_reason")
        or "—"
    )
    return {
        "date": row.get("date") or row.get("entry_time") or "—",
        "exit_time": row.get("exit_time") or "",
        "signal": row.get("signal_type") or row.get("signal") or "—",
        "symbol": row.get("symbol") or "—",
        "strategy": row.get("strategy") or "—",
        "entry_price": row.get("entry_price"),
        "exit_price": row.get("exit_price"),
        "net_pl": pnl,
        "reason": reason,
        "result": row.get("result")
        or (row.get("post_trade_attribution") or {}).get("outcome")
        or "",
    }


def _trade_history(data_dir: str, limit: int = 80) -> list[dict]:
    """Read closed trades from disk (not shm/cache). Newest first."""
    path = os.path.join(data_dir, "trades", "trade_history.json")
    data = _read_json(path, []) or []
    if not isinstance(data, list):
        return []
    rows = [_normalize_trade(t) for t in data if isinstance(t, dict)]
    rows = [r for r in rows if r]
    return list(reversed(rows[-limit:]))


def _tail_log(path: str, n: int = 40) -> list[str]:
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [ln.rstrip("\n") for ln in lines[-n:]]
    except Exception:
        return []


def _tick_age_seconds() -> float | None:
    if not os.path.exists(SHARED_TICK_FILE):
        return None
    try:
        import time

        return max(0.0, time.time() - os.path.getmtime(SHARED_TICK_FILE))
    except Exception:
        return None


CONSOLE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Quant Console — Kronos / Astra</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet" />
<style>
:root {
  --bg: #f4f6f8;
  --paper: #ffffff;
  --ink: #1a1f24;
  --muted: #5c6770;
  --line: #d7dee5;
  --astra: #0b6e4f;
  --astra-soft: #e6f4ef;
  --kronos: #1f5f8b;
  --kronos-soft: #e8f1f8;
  --of: #6e0b4f;
  --of-soft: #f4e6ef;
  --ok: #1f7a4c;
  --warn: #a15c00;
  --bad: #b42318;
  --call: #1f7a4c;
  --call-bg: #e8f6ee;
  --call-border: #9fd4b5;
  --put: #b42318;
  --put-bg: #fdeceb;
  --put-border: #f0b5af;
  --shadow: 0 18px 40px rgba(26, 31, 36, 0.06);
}
* { box-sizing: border-box; }
body {
  margin: 0;
  color: var(--ink);
  background:
    radial-gradient(1200px 500px at 10% -10%, #e8f1f8 0%, transparent 55%),
    radial-gradient(900px 420px at 100% 0%, #eef6f2 0%, transparent 50%),
    var(--bg);
  font-family: "IBM Plex Sans", sans-serif;
  min-height: 100vh;
}
.shell { max-width: 1280px; margin: 0 auto; padding: 28px 22px 48px; }
.top {
  display: flex; justify-content: space-between; align-items: end; gap: 16px;
  margin-bottom: 22px; padding-bottom: 16px; border-bottom: 1px solid var(--line);
}
.brand {
  font-family: "Fraunces", serif;
  font-size: clamp(28px, 4vw, 40px);
  font-weight: 700;
  letter-spacing: -0.03em;
  line-height: 1;
}
.brand span { color: var(--muted); font-weight: 500; font-size: 0.52em; margin-left: 10px; letter-spacing: 0.04em; }
.meta { text-align: right; color: var(--muted); font-size: 13px; }
.meta strong { color: var(--ink); font-weight: 600; }
.grid {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 18px;
  margin-bottom: 18px;
}
@media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
.lane {
  background: var(--paper);
  border: 1px solid var(--line);
  border-radius: 18px;
  box-shadow: var(--shadow);
  overflow: hidden;
  min-height: 320px;
}
.lane.astra { border-top: 4px solid var(--astra); }
.lane.of { border-top: 4px solid var(--of); }
.lane.kronos { border-top: 4px solid var(--kronos); }
.lane-head {
  display: flex; justify-content: space-between; align-items: center;
  padding: 16px 18px 12px;
}
.lane.astra .lane-head { background: linear-gradient(180deg, var(--astra-soft), transparent); }
.lane.of .lane-head { background: linear-gradient(180deg, var(--of-soft), transparent); }
.lane.kronos .lane-head { background: linear-gradient(180deg, var(--kronos-soft), transparent); }
.lane-title {
  font-family: "Fraunces", serif;
  font-size: 22px;
  font-weight: 700;
}
.lane.astra .lane-title { color: var(--astra); }
.lane.of .lane-title { color: var(--of); }
.lane.kronos .lane-title { color: var(--kronos); }
.pill {
  font-family: "IBM Plex Mono", monospace;
  font-size: 11px;
  font-weight: 500;
  padding: 5px 10px;
  border-radius: 999px;
  border: 1px solid var(--line);
  background: #fff;
}
.pill.ok { color: var(--ok); border-color: #b7e0c7; background: #eef9f2; }
.pill.warn { color: var(--warn); border-color: #f0d4a8; background: #fff7eb; }
.pill.bad { color: var(--bad); border-color: #f0bdb8; background: #fff1ef; }
.lane-body { padding: 4px 18px 18px; }
.metrics {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 14px;
}
.metric.signal-call {
  border-color: var(--call-border);
  background: var(--call-bg);
  box-shadow: inset 0 0 0 1px var(--call-border);
}
.metric.signal-put {
  border-color: var(--put-border);
  background: var(--put-bg);
  box-shadow: inset 0 0 0 1px var(--put-border);
}
.metric.signal-call .value { color: var(--call); font-size: 22px; font-weight: 700; letter-spacing: 0.04em; }
.metric.signal-put .value { color: var(--put); font-size: 22px; font-weight: 700; letter-spacing: 0.04em; }
.metric.signal-call .label,
.metric.signal-call .hint { color: #2f6b4a; }
.metric.signal-put .label,
.metric.signal-put .hint { color: #8a3a34; }
.sig-call {
  color: var(--call);
  font-weight: 700;
  background: var(--call-bg);
  border: 1px solid var(--call-border);
  border-radius: 6px;
  padding: 2px 8px;
}
.sig-put {
  color: var(--put);
  font-weight: 700;
  background: var(--put-bg);
  border: 1px solid var(--put-border);
  border-radius: 6px;
  padding: 2px 8px;
}
.metric {
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 12px;
  background: #fbfcfd;
}
.metric .label {
  color: var(--muted);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  margin-bottom: 6px;
}
.metric .value {
  font-family: "IBM Plex Mono", monospace;
  font-size: 18px;
  font-weight: 500;
}
.metric .hint {
  margin-top: 4px;
  color: var(--muted);
  font-size: 11px;
  line-height: 1.3;
}
.reason {
  font-size: 13px;
  color: var(--muted);
  line-height: 1.45;
  min-height: 38px;
}
.shared {
  background: var(--paper);
  border: 1px solid var(--line);
  border-radius: 18px;
  box-shadow: var(--shadow);
  padding: 16px 18px;
  margin-bottom: 18px;
}
.shared h2 {
  margin: 0 0 6px;
  font-family: "Fraunces", serif;
  font-size: 18px;
}
.shared p { margin: 0; color: var(--muted); font-size: 13px; line-height: 1.45; }
.panel {
  background: var(--paper);
  border: 1px solid var(--line);
  border-radius: 18px;
  box-shadow: var(--shadow);
  overflow: hidden;
  margin-bottom: 18px;
}
.panel-head {
  display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap;
  padding: 14px 18px; border-bottom: 1px solid var(--line);
}
.panel-head h2 { margin: 0; font-family: "Fraunces", serif; font-size: 18px; }
.engine-switch {
  display: inline-flex;
  border: 1px solid var(--line);
  border-radius: 999px;
  overflow: hidden;
  background: #fff;
}
.engine-switch button {
  appearance: none;
  border: 0;
  background: transparent;
  color: var(--muted);
  font-family: "IBM Plex Sans", sans-serif;
  font-size: 13px;
  font-weight: 600;
  padding: 8px 16px;
  cursor: pointer;
}
.engine-switch button.active.kronos {
  background: var(--kronos-soft);
  color: var(--kronos);
}
.engine-switch button.active.astra {
  background: var(--astra-soft);
  color: var(--astra);
}
.engine-switch button.active.of {
  background: var(--of-soft);
  color: var(--of);
}
.logs-body {
  max-height: 360px;
  overflow: auto;
  padding: 12px 16px 18px;
  font-family: "IBM Plex Mono", monospace;
  font-size: 12px;
  line-height: 1.55;
  color: #33404a;
  background: linear-gradient(180deg, #fafbfc, #fff);
}
.log-line { white-space: pre-wrap; word-break: break-word; margin: 0 0 4px; }
.log-line.astra { border-left: 3px solid var(--astra); padding-left: 8px; }
.log-line.of { border-left: 3px solid var(--of); padding-left: 8px; }
.log-line.kronos { border-left: 3px solid var(--kronos); padding-left: 8px; }
.trades-body { max-height: 420px; overflow: auto; }
.trades-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.trades-table th, .trades-table td {
  padding: 10px 12px;
  border-bottom: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
}
.trades-table th {
  position: sticky; top: 0;
  background: #f7f9fb;
  color: var(--muted);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-weight: 600;
}
.trades-table td.mono { font-family: "IBM Plex Mono", monospace; }
.pnl-pos { color: var(--ok); font-weight: 600; }
.pnl-neg { color: var(--bad); font-weight: 600; }
.empty {
  padding: 28px 18px;
  color: var(--muted);
  font-size: 13px;
}
</style>
</head>
<body>
  <div class="shell">
    <div class="top">
      <div class="brand">Quant Console<span>KRONOS × ASTRA</span></div>
      <div class="meta">
        <div><strong id="clock">--:--:--</strong> IST</div>
        <div>Tick bus age: <strong id="tickAge">—</strong></div>
      </div>
    </div>

    <div class="shared">
      <h2>Shared market bus</h2>
      <p>
        Shoonya data hub feeds both engines through shared memory ticks.
        Each engine has its own <strong>paper wallet on disk</strong>
        (<code>capital_state.json</code>), starting at <strong id="paperStart">₹2,500</strong>.
        The Funds number is that wallet after paper P&amp;L — not live broker cash.
      </p>
    </div>

    <div class="grid">
      <section class="lane kronos">
        <div class="lane-head">
          <div class="lane-title">Kronos</div>
          <div id="kronosPill" class="pill">—</div>
        </div>
        <div class="lane-body">
          <div class="metrics">
            <div class="metric"><div class="label">Index</div><div class="value" id="kronosIndex">—</div></div>
            <div class="metric"><div class="label">Spot</div><div class="value" id="kronosSpot">—</div></div>
            <div class="metric" id="kronosSignalMetric">
              <div class="label">Signal</div>
              <div class="value" id="kronosSignal">—</div>
              <div class="hint" id="kronosSignalHint">Green CALL / red PUT when live</div>
            </div>
            <div class="metric">
              <div class="label">Paper funds</div>
              <div class="value" id="kronosFunds">—</div>
              <div class="hint">Disk wallet for Kronos paper trades</div>
            </div>
            <div class="metric"><div class="label">Pred close</div><div class="value" id="kronosPred">—</div></div>
            <div class="metric"><div class="label">Return %</div><div class="value" id="kronosRet">—</div></div>
          </div>
          <div class="reason" id="kronosReason">Waiting for live state…</div>
        </div>
      </section>

      <section class="lane astra">
        <div class="lane-head">
          <div class="lane-title">Astra</div>
          <div id="astraPill" class="pill">—</div>
        </div>
        <div class="lane-body">
          <div class="metrics">
            <div class="metric"><div class="label">Index</div><div class="value" id="astraIndex">—</div></div>
            <div class="metric"><div class="label">Spot</div><div class="value" id="astraSpot">—</div></div>
            <div class="metric" id="astraSignalMetric">
              <div class="label">Signal</div>
              <div class="value" id="astraSignal">—</div>
              <div class="hint">Waiting = no CALL/PUT yet this bar</div>
            </div>
            <div class="metric">
              <div class="label">Paper funds</div>
              <div class="value" id="astraFunds">—</div>
              <div class="hint">Disk wallet for Astra paper trades</div>
            </div>
            <div class="metric"><div class="label">GEX</div><div class="value" id="astraGex">—</div></div>
            <div class="metric"><div class="label">Velocity</div><div class="value" id="astraVel">—</div></div>
            <div class="metric"><div class="label">Accel</div><div class="value" id="astraAcc">—</div></div>
          </div>
          <div class="reason" id="astraReason">Waiting for live state…</div>
        </div>
      </section>

      <section class="lane of">
        <div class="lane-head">
          <div class="lane-title">Order Flow</div>
          <div id="ofPill" class="pill">—</div>
        </div>
        <div class="lane-body">
          <div class="metrics">
            <div class="metric"><div class="label">Signal</div><div class="value" id="ofSignal">—</div></div>
            <div class="metric">
              <div class="label">Paper funds</div>
              <div class="value" id="ofFunds">—</div>
              <div class="hint">Disk wallet for OF trades</div>
            </div>
          </div>
          <div class="reason" id="ofReason">See Trade History/Logs below</div>
        </div>
      </section>
    </div>

    <section class="panel">
      <div class="panel-head">
        <h2>Telemetry</h2>
        <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
          <div class="engine-switch" role="tablist" aria-label="Engine logs">
            <button type="button" id="logTabKronos" class="active kronos" data-engine="kronos">Kronos</button>
            <button type="button" id="logTabAstra" class="astra" data-engine="astra">Astra</button>
            <button type="button" id="logTabOF" class="of" data-engine="of">Order Flow</button>
          </div>
          <div class="pill" id="logCount">0 lines</div>
        </div>
      </div>
      <div class="logs-body" id="logs"></div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2>Trade history</h2>
        <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
          <div class="engine-switch" role="tablist" aria-label="Trade history engine">
            <button type="button" id="histTabKronos" class="active kronos" data-engine="kronos">Kronos</button>
            <button type="button" id="histTabAstra" class="astra" data-engine="astra">Astra</button>
            <button type="button" id="histTabOF" class="of" data-engine="of">Order Flow</button>
          </div>
          <div class="pill" id="histCount">0 trades · disk</div>
        </div>
      </div>
      <div class="trades-body" id="trades"></div>
    </section>
  </div>

<script>
const fmt = (n, d=2) => (n === null || n === undefined || Number.isNaN(Number(n))) ? '—' : Number(n).toLocaleString(undefined, {maximumFractionDigits:d, minimumFractionDigits:0});
const money = (n) => (n === null || n === undefined) ? '—' : '₹' + fmt(n, 2);

let activeLogEngine = 'kronos';
let activeHistEngine = 'kronos';
let latestLogs = { kronos: [], astra: [], of: [] };
let latestHistory = { kronos: [], astra: [], of: [] };

function signalLabel(raw) {
  const t = String(raw || '').toUpperCase();
  if (t.includes('CALL')) return 'CALL';
  if (t.includes('PUT')) return 'PUT';
  if (!t || t === '—' || t.includes('NEUTRAL') || t.includes('IDLE') || t.includes('WAIT') || t.includes('BOOT')) {
    return 'Waiting';
  }
  return String(raw);
}

function pillClass(text) {
  const t = String(text || '').toUpperCase();
  if (t.includes('CALL')) return 'pill ok';
  if (t.includes('PUT')) return 'pill bad';
  if (t.includes('WAITING') || t.includes('NEUTRAL') || t.includes('IDLE')) return 'pill warn';
  return 'pill';
}

function signalMetricClass(text) {
  const t = String(text || '').toUpperCase();
  if (t.includes('CALL')) return 'metric signal-call';
  if (t.includes('PUT')) return 'metric signal-put';
  return 'metric';
}

function signalCellHtml(raw) {
  const label = signalLabel(raw);
  const t = String(label || '').toUpperCase();
  if (t.includes('CALL')) return `<span class="sig-call">${label}</span>`;
  if (t.includes('PUT')) return `<span class="sig-put">${label}</span>`;
  return label || '—';
}

function renderLogs() {
  const logs = latestLogs[activeLogEngine] || [];
  const box = document.getElementById('logs');
  box.innerHTML = logs.map(l => `<div class="log-line ${activeLogEngine}">${l}</div>`).join('')
    || `<div class="log-line">No ${activeLogEngine} telemetry yet.</div>`;
  document.getElementById('logCount').textContent = logs.length + ' lines · ' + activeLogEngine.toUpperCase();
}

function setLogEngine(engine) {
  activeLogEngine = engine;
  document.getElementById('logTabKronos').className = activeLogEngine === 'kronos' ? 'active kronos' : 'kronos';
  document.getElementById('logTabAstra').className = activeLogEngine === 'astra' ? 'active astra' : 'astra';
  document.getElementById('logTabOF').className = activeLogEngine === 'of' ? 'active of' : 'of';
  renderLogs();
}

function pnlClass(v) {
  const n = Number(v);
  if (Number.isNaN(n) || n === 0) return 'mono';
  return n > 0 ? 'mono pnl-pos' : 'mono pnl-neg';
}

function renderHistory() {
  const rows = latestHistory[activeHistEngine] || [];
  const box = document.getElementById('trades');
  document.getElementById('histCount').textContent = rows.length + ' trades · disk · ' + activeHistEngine.toUpperCase();
  if (!rows.length) {
    const histPath = activeHistEngine === 'of'
      ? 'stock_order_flow/data/trades/trade_history.json'
      : `${activeHistEngine}_bot/data/trades/trade_history.json`;
    box.innerHTML = `<div class="empty">No closed ${activeHistEngine} trades on disk yet (<code>${histPath}</code>).</div>`;
    return;
  }
  const body = rows.map(t => `
    <tr>
      <td class="mono" style="white-space:nowrap">${t.date || '—'}</td>
      <td class="mono" style="white-space:nowrap">${t.exit_time || '—'}</td>
      <td>${signalCellHtml(t.signal)}</td>
      <td class="mono">${t.symbol || '—'}</td>
      <td class="mono">${fmt(t.entry_price, 2)}</td>
      <td class="mono">${fmt(t.exit_price, 2)}</td>
      <td class="${pnlClass(t.net_pl)}">${money(t.net_pl)}</td>
      <td>${t.reason || '—'}</td>
    </tr>`).join('');
  box.innerHTML = `
    <table class="trades-table">
      <thead>
        <tr>
          <th>Entry Time</th><th>Exit Time</th><th>Side</th><th>Symbol</th><th>Entry</th><th>Exit</th><th>P&amp;L</th><th>Exit reason</th>
        </tr>
      </thead>
      <tbody>${body}</tbody>
    </table>`;
}

function setHistEngine(engine) {
  activeHistEngine = engine;
  document.getElementById('histTabKronos').className = activeHistEngine === 'kronos' ? 'active kronos' : 'kronos';
  document.getElementById('histTabAstra').className = activeHistEngine === 'astra' ? 'active astra' : 'astra';
  document.getElementById('histTabOF').className = activeHistEngine === 'of' ? 'active of' : 'of';
  renderHistory();
}

async function refresh() {
  const res = await fetch('/api/live_state');
  const data = await res.json();
  document.getElementById('clock').textContent = data.server_time || '—';
  const age = data.tick_age_sec;
  document.getElementById('tickAge').textContent = (age === null || age === undefined) ? 'missing' : (age < 5 ? age.toFixed(1) + 's' : Math.round(age) + 's stale');
  if (data.initial_paper_capital != null) {
    document.getElementById('paperStart').textContent = money(data.initial_paper_capital);
  }

  const a = data.astra || {};
  const o = data.of || {};
  document.getElementById('ofFunds').textContent = money(o.available_funds);
  const k = data.kronos || {};

  const kSig = signalLabel(k.signal || k.signal_status);
  document.getElementById('kronosIndex').textContent = k.active_index || '—';
  document.getElementById('kronosSpot').textContent = fmt(k.spot_price, 2);
  document.getElementById('kronosSignal').textContent = kSig;
  document.getElementById('kronosSignalMetric').className = signalMetricClass(kSig);
  document.getElementById('kronosFunds').textContent = money(k.available_funds);
  document.getElementById('kronosPred').textContent = fmt(k.predicted_close, 2);
  document.getElementById('kronosRet').textContent = fmt(k.predicted_return_pct, 3);
  document.getElementById('kronosReason').textContent = k.reasoning || (k.last_candle ? ('Last candle ' + k.last_candle) : '—');
  const kp = document.getElementById('kronosPill');
  kp.textContent = kSig;
  kp.className = pillClass(kSig);

  const aSig = signalLabel(a.signal_status || a.signal);
  document.getElementById('astraIndex').textContent = a.active_index || '—';
  document.getElementById('astraSpot').textContent = fmt(a.spot_price, 2);
  document.getElementById('astraSignal').textContent = aSig;
  document.getElementById('astraSignalMetric').className = signalMetricClass(aSig);
  document.getElementById('astraFunds').textContent = money(a.available_funds);
  document.getElementById('astraGex').textContent = fmt(a.gex_net, 0);
  document.getElementById('astraVel').textContent = fmt(a.velocity, 0);
  document.getElementById('astraAcc').textContent = fmt(a.acceleration, 0);
  document.getElementById('astraReason').textContent = a.reasoning || '—';
  const ap = document.getElementById('astraPill');
  ap.textContent = aSig;
  ap.className = pillClass(aSig);

  latestLogs = {
    of: data.of_logs || [],
    kronos: data.kronos_logs || [],
    astra: data.astra_logs || [],
  };
  latestHistory = {
    of: data.of_trade_history || [],
    kronos: data.kronos_trade_history || [],
    astra: data.astra_trade_history || [],
  };
  renderLogs();
  renderHistory();
}

document.getElementById('logTabKronos').addEventListener('click', () => setLogEngine('kronos'));
document.getElementById('logTabAstra').addEventListener('click', () => setLogEngine('astra'));
document.getElementById('logTabOF').addEventListener('click', () => setLogEngine('of'));
document.getElementById('histTabKronos').addEventListener('click', () => setHistEngine('kronos'));
document.getElementById('histTabAstra').addEventListener('click', () => setHistEngine('astra'));
document.getElementById('histTabOF').addEventListener('click', () => setHistEngine('of'));

setLogEngine('kronos');
setHistEngine('kronos');
refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return CONSOLE_HTML


@app.route("/api/health")
def health():
    return jsonify(
        {
            "ok": True,
            "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "tick_file": SHARED_TICK_FILE,
            "tick_age_sec": _tick_age_seconds(),
        }
    )


@app.route("/api/live_state")
def live_state():
    astra = get_astra_live_state()
    kronos = get_kronos_live_state()
    
    astra["available_funds"] = _capital(ASTRA_DATA)
    kronos["available_funds"] = _capital(KRONOS_DATA)
    of_funds = _capital(ORDERFLOW_DATA)
    
    of_logs = [html.escape(line) for line in _tail_log(os.path.join(ORDERFLOW_LOGS, "stock_order_flow.log"), 80)]
    of_hist = _trade_history(ORDERFLOW_DATA)

    astra["active_trade"] = _active_trade(ASTRA_DATA)
    kronos["active_trade"] = _active_trade(KRONOS_DATA)

    astra_logs = [html.escape(line) for line in _tail_log(os.path.join(ASTRA_LOGS, "astra_bot.log"), 80)]
    kronos_logs = [html.escape(line) for line in _tail_log(os.path.join(KRONOS_LOGS, "kronos_bot.log"), 80)]

    astra_hist = _trade_history(ASTRA_DATA)
    kronos_hist = _trade_history(KRONOS_DATA)
    # Escape free-text fields for safe HTML embedding via JSON → DOM text/innerHTML table
    for rows in (astra_hist, kronos_hist, of_hist):
        for row in rows:
            for key in ("date", "exit_time", "signal", "symbol", "strategy", "reason", "result"):
                if key in row and row[key] is not None:
                    row[key] = html.escape(str(row[key]))

    return jsonify(
        {
            "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "tick_age_sec": _tick_age_seconds(),
            "initial_paper_capital": INITIAL_PAPER_CAPITAL,
            "astra": astra,
            "of": {"available_funds": of_funds},
            "kronos": kronos,
            "astra_logs": astra_logs,
            "of_logs": of_logs,
            "kronos_logs": kronos_logs,
            "astra_trade_history": astra_hist,
            "of_trade_history": of_hist,
            "kronos_trade_history": kronos_hist,
            "default_log_engine": "kronos",
        }
    )


def main():
    app.run(host="0.0.0.0", port=5000, debug=False)


if __name__ == "__main__":
    main()
