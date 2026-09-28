"""
Quant Console — dual-brand light precision dashboard for ARGUS + AEGIS.
UPDATED: Integrated Research Mode (On-Demand Microstructure Forensics).
"""

from __future__ import annotations

import html
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[1]  # .../project_root
load_dotenv(BASE_DIR / ".env")

# ─── COMMON IMPORTS ───
from common.config import INITIAL_PAPER_CAPITAL
from common.ipc import (
    get_kronos_live_state,
    get_aegis_live_state,
    get_argus_live_state,
    SHARED_TICK_FILE,
)
from common.runtime.paths import get_runtime_paths

app = Flask(__name__)

KRONOS_PATHS = get_runtime_paths("kronos")
AEGIS_PATHS = get_runtime_paths("aegis")
KRONOS_DATA = KRONOS_PATHS["data_dir"]
AEGIS_DATA = AEGIS_PATHS["data_dir"]
KRONOS_LOGS = KRONOS_PATHS["logs_dir"]
AEGIS_LOGS = AEGIS_PATHS["logs_dir"]
ORDERFLOW_PATHS = get_runtime_paths("argus")
ORDERFLOW_DATA = ORDERFLOW_PATHS["data_dir"]
ORDERFLOW_LOGS = ORDERFLOW_PATHS["logs_dir"]

# ─── HELPERS (Unchanged from your v10) ───
def _read_json(path, default=None):
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f: return json.load(f)
        except UnicodeError:
            try:
                with open(path, "r", encoding="utf-16") as f: return json.load(f)
            except Exception: pass
        except Exception: pass
    return default

def _capital(data_dir: str, force_paper: bool = False) -> float:
    state = _read_json(os.path.join(data_dir, "capital_state.json"), {}) or {}
    return float(state.get("available_funds") or 0.0)

def _active_trade(data_dir: str):
    data = _read_json(os.path.join(data_dir, "trades", "active_trade_context.json"), None)
    if isinstance(data, dict) and data.get("status") == "OPEN": return {"main": data}
    return None

def _active_trade_aegis(data_dir: str):
    """AEGIS writes its active context to aegis_active_trade_context.json."""
    data = _read_json(os.path.join(data_dir, "trades", "aegis_active_trade_context.json"), None)
    if isinstance(data, dict) and data.get("status") == "OPEN": return {"main": data}
    return None

def _open_positions(data_dir: str):
    return _read_json(os.path.join(data_dir, "trades", "open_positions.json"), None)

def _normalize_trade(row: dict) -> dict:
    if not isinstance(row, dict): return {}
    pnl = row.get("net_pl")
    if pnl is None:
        raw_pnl = row.get("pnl")
        if isinstance(raw_pnl, (int, float)) and abs(float(raw_pnl)) < 1 and row.get("strategy") == "order_flow": pnl = None
        else: pnl = raw_pnl
    if pnl is None: pnl = (row.get("post_trade_attribution") or {}).get("pnl_rs")
    reason = row.get("reason") or row.get("exit_reason") or (row.get("post_trade_attribution") or {}).get("exit_reason") or "—"
    return {
        "date": row.get("date") or row.get("entry_time") or "—", "exit_time": row.get("exit_time") or "",
        "signal": row.get("signal_type") or row.get("signal") or row.get("type") or "—",
        "symbol": row.get("symbol") or "—", "strategy": row.get("strategy") or "—",
        "entry_price": row.get("entry_price"), "exit_price": row.get("exit_price"),
        "net_pl": pnl, "daily_total_pnl": row.get("daily_total_pnl"),
        "reason": reason, "result": row.get("result") or (row.get("post_trade_attribution") or {}).get("outcome") or "",
    }

def _trade_history(data_dir: str, limit: int = 80) -> list[dict]:
    path = os.path.join(data_dir, "trades", "trade_history.json")
    data = _read_json(path, []) or []
    if not isinstance(data, list): return []
    rows = [_normalize_trade(t) for t in data if isinstance(t, dict)]
    return list(reversed([r for r in rows if r][-limit:]))

def _tail_log(path: str, n: int = 40) -> list[str]:
    if not path or not os.path.exists(path): return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            buffer = bytearray(); pointer = f.tell(); lines_found = 0
            while pointer > 0 and lines_found <= n:
                pointer -= 1; f.seek(pointer); char = f.read(1)
                if char == b'\n': lines_found += 1
                buffer.extend(char)
            lines = buffer[::-1].decode("utf-8", errors="replace").splitlines()
            return [ln for ln in lines if ln][-n:]
    except Exception: return []

def _tick_age_seconds() -> float | None:
    if not os.path.exists(SHARED_TICK_FILE): return None
    try: import time; return max(0.0, time.time() - os.path.getmtime(SHARED_TICK_FILE))
    except Exception: return None

# ─── RESEARCH API ENDPOINT ───
@app.route("/api/research")
def api_research():
    """VORTEX research: spawns research_analytics.py subprocess to avoid RAM bloat in main Flask process.
    Supports Single Date (deep-dive raw partitions) and Range/All-Time (pre-aggregated snapshots)."""
    symbol = request.args.get("symbol", "").upper()
    target_date = request.args.get("date")
    start = request.args.get("start")
    end = request.args.get("end")
    all_time = request.args.get("all_time", "").lower() in ("1", "true", "yes")

    if not symbol:
        return jsonify({"error": "Symbol required"}), 400

    script_path = BASE_DIR / "ARGUS" / "research_analytics.py"
    if not script_path.exists():
        return jsonify({"error": "ARGUS research engine not found"}), 500

    cmd = [sys.executable, str(script_path), symbol]
    if all_time:
        cmd.append("--all-time")
    elif start and end:
        cmd += ["--start", start, "--end", end]
    elif target_date:
        cmd += ["--date", target_date]
    else:
        cmd += ["--date", datetime.now().strftime("%Y-%m-%d")]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=BASE_DIR)
        if result.returncode != 0:
            return jsonify({"error": f"ARGUS research script failed: {result.stderr}"}), 500
        data = json.loads(result.stdout)
        return jsonify(data)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "ARGUS research timeout (120s)"}), 504
    except json.JSONDecodeError:
        return jsonify({"error": "Invalid JSON from ARGUS research script", "raw": result.stdout[:500]}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/research_aegis")
def api_research_aegis():
    """AEGIS research: spawns AEGIS/research_analytics.py subprocess.
    Supports Single Date (raw options L2 deep-dive) and Range/All-Time (pre-aggregated snapshots)."""
    target_date = request.args.get("date")
    start = request.args.get("start")
    end = request.args.get("end")
    all_time = request.args.get("all_time", "").lower() in ("1", "true", "yes")

    script_path = BASE_DIR / "AEGIS" / "research_analytics.py"
    if not script_path.exists():
        return jsonify({"error": "AEGIS research engine not found"}), 500

    cmd = [sys.executable, str(script_path)]
    if all_time:
        cmd.append("--all-time")
    elif start and end:
        cmd += ["--start", start, "--end", end]
    elif target_date:
        cmd += ["--date", target_date]
    else:
        cmd += ["--date", datetime.now().strftime("%Y-%m-%d")]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=BASE_DIR)
        if result.returncode != 0:
            return jsonify({"error": f"AEGIS research script failed: {result.stderr}"}), 500
        data = json.loads(result.stdout)
        return jsonify(data)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "AEGIS research timeout (120s)"}), 504
    except json.JSONDecodeError:
        return jsonify({"error": "Invalid JSON from AEGIS research script", "raw": result.stdout[:500]}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/analytics_dates")
def api_analytics_dates():
    """Lists available aggregated snapshot dates for an engine (for date selector population)."""
    engine = request.args.get("engine", "argus").lower()
    try:
        from common.analytics_persistence import list_available_dates
        dates = list_available_dates(engine)
        return jsonify({"engine": engine, "dates": dates})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── HTML TEMPLATE (UPDATED) ───
CONSOLE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Quant Console — ARGUS × AEGIS / Research</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet" />
<style>
:root {
  --bg: #f4f6f8; --paper: #ffffff; --ink: #1a1f24; --muted: #5c6770; --line: #d7dee5;
  --aegis: #0b6e4f; --aegis-soft: #e6f4ef; --kronos: #1f5f8b; --kronos-soft: #e8f1f8;
  --of: #6e0b4f; --of-soft: #f4e6ef; --research: #4b2a8a; --research-soft: #f0eaff;
  --ok: #1f7a4c; --warn: #a15c00; --bad: #b42318;
  --call: #1f7a4c; --call-bg: #e8f6ee; --call-border: #9fd4b5;
  --put: #b42318; --put-bg: #fdeceb; --put-border: #f0b5af;
  --shadow: 0 18px 40px rgba(26, 31, 36, 0.06);
}
* { box-sizing: border-box; }
body { margin: 0; color: var(--ink); background: radial-gradient(1200px 500px at 10% -10%, #e8f1f8 0%, transparent 55%), radial-gradient(900px 420px at 100% 0%, #eef6f2 0%, transparent 50%), var(--bg); font-family: "IBM Plex Sans", sans-serif; min-height: 100vh; }
.shell { max-width: 1400px; margin: 0 auto; padding: 28px 22px 48px; }
.top { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 22px; padding-bottom: 16px; border-bottom: 1px solid var(--line); flex-wrap: wrap; }
.brand { font-family: "Fraunces", serif; font-size: clamp(28px, 4vw, 40px); font-weight: 700; letter-spacing: -0.03em; line-height: 1; }
.brand span { color: var(--muted); font-weight: 500; font-size: 0.52em; margin-left: 10px; letter-spacing: 0.04em; }
.meta { text-align: right; color: var(--muted); font-size: 13px; }
.meta strong { color: var(--ink); font-weight: 600; }

/* Research Toggle Button */
.research-toggle { 
  background: var(--research-soft); color: var(--research); border: 1px solid var(--research); 
  padding: 8px 16px; border-radius: 999px; font-weight: 600; font-family: "IBM Plex Sans", sans-serif; font-size: 13px; cursor: pointer; transition: all 0.2s;
}
.research-toggle:hover { background: var(--research); color: white; }
.research-toggle.active { background: var(--research); color: white; box-shadow: 0 0 0 3px var(--research-soft); }

.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; margin-bottom: 18px; }
@media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
.lane { background: var(--paper); border: 1px solid var(--line); border-radius: 18px; box-shadow: var(--shadow); overflow: hidden; min-height: 320px; }
.lane.aegis { border-top: 4px solid var(--aegis); } .lane.of { border-top: 4px solid var(--of); } .lane.kronos { border-top: 4px solid var(--kronos); }
.lane-head { display: flex; justify-content: space-between; align-items: center; padding: 16px 18px 12px; }
.lane.aegis .lane-head { background: linear-gradient(180deg, var(--aegis-soft), transparent); } .lane.of .lane-head { background: linear-gradient(180deg, var(--of-soft), transparent); } .lane.kronos .lane-head { background: linear-gradient(180deg, var(--kronos-soft), transparent); }
.lane-title { font-family: "Fraunces", serif; font-size: 22px; font-weight: 700; }
.lane.aegis .lane-title { color: var(--aegis); } .lane.of .lane-title { color: var(--of); } .lane.kronos .lane-title { color: var(--kronos); }
.pill { font-family: "IBM Plex Mono", monospace; font-size: 11px; font-weight: 500; padding: 5px 10px; border-radius: 999px; border: 1px solid var(--line); background: #fff; }
.pill.ok { color: var(--ok); border-color: #b7e0c7; background: #eef9f2; } .pill.warn { color: var(--warn); border-color: #f0d4a8; background: #fff7eb; } .pill.bad { color: var(--bad); border-color: #f0bdb8; background: #fff1ef; }
.lane-body { padding: 4px 18px 18px; }
.metrics { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin-bottom: 14px; }
.metric.signal-call { border-color: var(--call-border); background: var(--call-bg); box-shadow: inset 0 0 0 1px var(--call-border); }
.metric.signal-put { border-color: var(--put-border); background: var(--put-bg); box-shadow: inset 0 0 0 1px var(--put-border); }
.metric.signal-call .value { color: var(--call); font-size: 22px; font-weight: 700; letter-spacing: 0.04em; } .metric.signal-put .value { color: var(--put); font-size: 22px; font-weight: 700; letter-spacing: 0.04em; }
.metric.signal-call .label, .metric.signal-call .hint { color: #2f6b4a; } .metric.signal-put .label, .metric.signal-put .hint { color: #8a3a34; }
.sig-call { color: var(--call); font-weight: 700; background: var(--call-bg); border: 1px solid var(--call-border); border-radius: 6px; padding: 2px 8px; }
.sig-put { color: var(--put); font-weight: 700; background: var(--put-bg); border: 1px solid var(--put-border); border-radius: 6px; padding: 2px 8px; }
.metric { border: 1px solid var(--line); border-radius: 12px; padding: 12px; background: #fbfcfd; }
.metric .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 6px; }
.metric .value { font-family: "IBM Plex Mono", monospace; font-size: 18px; font-weight: 500; }
.metric .hint { margin-top: 4px; color: var(--muted); font-size: 11px; line-height: 1.3; }
.reason { font-size: 13px; color: var(--muted); line-height: 1.45; min-height: 38px; }
.shared { background: var(--paper); border: 1px solid var(--line); border-radius: 18px; box-shadow: var(--shadow); padding: 16px 18px; margin-bottom: 18px; }
.shared h2 { margin: 0 0 6px; font-family: "Fraunces", serif; font-size: 18px; } .shared p { margin: 0; color: var(--muted); font-size: 13px; line-height: 1.45; }
.panel { background: var(--paper); border: 1px solid var(--line); border-radius: 18px; box-shadow: var(--shadow); overflow: hidden; margin-bottom: 18px; }
.panel-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; padding: 14px 18px; border-bottom: 1px solid var(--line); }
.panel-head h2 { margin: 0; font-family: "Fraunces", serif; font-size: 18px; }
.engine-switch { display: inline-flex; border: 1px solid var(--line); border-radius: 999px; overflow: hidden; background: #fff; }
.engine-switch button { appearance: none; border: 0; background: transparent; color: var(--muted); font-family: "IBM Plex Sans", sans-serif; font-size: 13px; font-weight: 600; padding: 8px 16px; cursor: pointer; }
.engine-switch button.active.kronos { background: var(--kronos-soft); color: var(--kronos); } .engine-switch button.active.aegis { background: var(--aegis-soft); color: var(--aegis); } .engine-switch button.active.of { background: var(--of-soft); color: var(--of); } .engine-switch button.active.research { background: var(--research-soft); color: var(--research); }
.logs-body { max-height: 360px; overflow: auto; padding: 12px 16px 18px; font-family: "IBM Plex Mono", monospace; font-size: 12px; line-height: 1.55; color: #33404a; background: linear-gradient(180deg, #fafbfc, #fff); }
.log-line { white-space: pre-wrap; word-break: break-word; margin: 0 0 4px; }
.log-line.aegis { border-left: 3px solid var(--aegis); padding-left: 8px; } .log-line.of { border-left: 3px solid var(--of); padding-left: 8px; } .log-line.kronos { border-left: 3px solid var(--kronos); padding-left: 8px; }
.trades-body { max-height: 420px; overflow: auto; }
.trades-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.trades-table th, .trades-table td { padding: 10px 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
.trades-table th { position: sticky; top: 0; background: #f7f9fb; color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }
.trades-table td.mono { font-family: "IBM Plex Mono", monospace; }
.pnl-pos { color: var(--ok); font-weight: 600; } .pnl-neg { color: var(--bad); font-weight: 600; }
.empty { padding: 28px 18px; color: var(--muted); font-size: 13px; }

/* RESEARCH PANEL STYLES */
.research-panel { display: none; } /* Hidden by default */
.research-panel.visible { display: block; animation: fadeIn 0.3s ease; }
@keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
.research-tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--line); padding: 0 18px; margin-bottom: 0; overflow-x: auto; }
.research-tab { padding: 12px 16px; font-weight: 600; font-size: 13px; color: var(--muted); cursor: pointer; border-bottom: 3px solid transparent; margin-bottom: -1px; transition: all 0.15s; white-space: nowrap; }
.research-tab:hover { color: var(--ink); background: var(--research-soft); }
.research-tab.active { color: var(--research); border-bottom-color: var(--research); background: var(--research-soft); }
.research-content { padding: 18px; }
.chart-container { width: 100%; height: 500px; margin-bottom: 24px; }
.chart-container.small { height: 350px; }
.research-meta { display: flex; gap: 24px; padding: 12px 18px; background: var(--research-soft); border-radius: 12px; margin: 0 18px 18px; font-family: "IBM Plex Mono", monospace; font-size: 12px; color: var(--research); flex-wrap: wrap; }
.research-loader { display: flex; align-items: center; justify-content: center; height: 300px; color: var(--muted); }
.spinner { border: 3px solid var(--line); border-top-color: var(--research); border-radius: 50%; width: 24px; height: 24px; animation: spin 1s linear infinite; margin-right: 12px; }
@keyframes spin { to { transform: rotate(360deg); } }

/* DUAL-ENGINE RESEARCH TABS + DATE SELECTOR */
.research-engine-tabs { display: flex; gap: 4px; padding: 8px 18px 0; }
.research-engine-tab { padding: 10px 18px; font-weight: 700; font-size: 13px; color: var(--muted); cursor: pointer; border: 1px solid var(--line); border-bottom: none; border-radius: 10px 10px 0 0; background: #fbfcfd; transition: all 0.15s; }
.research-engine-tab:hover { color: var(--ink); }
.research-engine-tab.active[data-engine="argus"],
.research-engine-tab.active[data-engine="vortex"] { color: var(--of); background: var(--of-soft); border-color: var(--of); }
.research-engine-tab.active[data-engine="aegis"] { color: var(--aegis); background: var(--aegis-soft); border-color: var(--aegis); }
.research-controls { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; padding: 14px 18px; background: #fafbfc; border-bottom: 1px solid var(--line); }
.date-mode-switch { display: inline-flex; gap: 14px; }
.date-mode-switch label { font-size: 13px; color: var(--muted); cursor: pointer; font-weight: 600; }
.date-mode-switch input { margin-right: 4px; cursor: pointer; }
.date-input-wrap { display: inline-flex; gap: 10px; align-items: center; }
.date-input-wrap label { font-size: 13px; color: var(--muted); font-weight: 600; }
.date-input-wrap input[type="date"] { padding: 6px 10px; border: 1px solid var(--line); border-radius: 8px; font-family: "IBM Plex Mono", monospace; font-size: 13px; }
.symbol-wrap label { font-size: 13px; color: var(--muted); font-weight: 600; }
.symbol-wrap input[type="text"] { padding: 6px 10px; border: 1px solid var(--line); border-radius: 8px; font-family: "IBM Plex Mono", monospace; font-size: 13px; width: 140px; text-transform: uppercase; }
.research-run-btn { margin-left: auto; background: var(--research); color: white; border: none; padding: 9px 22px; border-radius: 999px; font-weight: 700; font-size: 13px; cursor: pointer; transition: all 0.2s; }
.research-run-btn:hover { background: #3a1f70; box-shadow: 0 4px 12px rgba(75, 42, 138, 0.3); }
.research-run-btn:disabled { opacity: 0.5; cursor: wait; }
</style>
</head>
<body>
  <div class="shell">
    <div class="top">
      <div class="brand">Quant Console<span>ARGUS × AEGIS</span></div>
      <div style="display:flex; align-items:center; gap:12px;">
        <div class="meta" style="text-align:left;">
          <div><strong id="clock">--:--:--</strong> IST</div>
          <div>Tick bus age: <strong id="tickAge">—</strong></div>
        </div>
        <button id="researchToggle" class="research-toggle" onclick="toggleResearchMode()">🔬 Research Mode</button>
      </div>
    </div>

    <div class="shared">
      <h2>Shared market bus</h2>
      <p> Shoonya data hub feeds both engines through shared memory ticks. Each engine has its own <strong>paper wallet on disk</strong> (<code>capital_state.json</code>), starting at <strong id="paperStart">₹2,500</strong>. The Funds number is that wallet after paper P&amp;L — not live broker cash. </p>
    </div>

    <div class="grid">
      <section class="lane of">
        <div class="lane-head"><div><div class="lane-title">ARGUS <span style="font-size:11px; font-weight:600; color:var(--muted); font-family:'IBM Plex Sans',sans-serif; text-transform:none;">(Algorithmic Real-Time Granular Ultra-Microstructure Scalper)</span></div></div><div id="ofPill" class="pill">—</div></div>
        <div class="lane-body">
          <div class="metrics">
            <div class="metric"><div class="label">Universe</div><div class="value" id="ofUniverse">15</div></div>
            <div class="metric"><div class="label">Top Setup</div><div class="value" id="ofSpot">—</div></div>
            <div class="metric"><div class="label">Spot Price</div><div class="value" id="ofPrice">—</div></div>
            <div class="metric" id="ofSignalMetric"><div class="label">Signal</div><div class="value" id="ofSignal">—</div><div class="hint" id="ofSignalHint">Top setup signal</div></div>
            <div class="metric"><div class="label">Conviction</div><div class="value" id="ofConviction">—</div></div>
            <div class="metric"><div class="label">Adv. Shield</div><div class="value" id="ofShield">CLEAR</div></div>
            <div class="metric"><div class="label">Kyle's λ</div><div class="value" id="ofKyle">—</div></div>
            <div class="metric"><div class="label">Branching</div><div class="value" id="ofBranching">—</div></div>
            <div class="metric"><div class="label">Whale Z</div><div class="value" id="ofWhale">—</div></div>
            <div class="metric"><div class="label">Live Funds</div><div class="value" id="ofFunds">—</div><div class="hint">Live Shoonya margin</div></div>
            <div class="metric"><div class="label">Fills Today</div><div class="value" id="ofFills">0</div></div>
            <div class="metric"><div class="label">Daily P&amp;L</div><div class="value" id="ofPnl">₹0.00</div></div>
          </div>
          <div class="reason" id="ofReason">Waiting for live state…</div>
          <hr style="border: 0; border-top: 1px dashed var(--line); margin: 10px 0;" />
          <div class="row" id="argus-ctx" style="font-size:11px; margin-bottom:4px; font-weight:600;">Active Trade: —</div>
        </div>
      </section>

      <section class="lane aegis">
        <div class="lane-head"><div class="lane-title">AEGIS (Options)</div><div id="aegisPill" class="pill">—</div></div>
        <div class="lane-body">
          <div class="metrics">
            <div class="metric"><div class="label">Index</div><div class="value" id="aegisIndex">—</div></div>
            <div class="metric"><div class="label">Spot</div><div class="value" id="aegisSpot">—</div></div>
            <div class="metric"><div class="label">ATM Strike</div><div class="value" id="aegisAtm">—</div></div>
            <div class="metric"><div class="label">ATM IV</div><div class="value" id="aegisAtmIv">—</div></div>
            <div class="metric"><div class="label">Gamma Sum</div><div class="value" id="aegisGammaSum">—</div></div>
            <div class="metric"><div class="label">Speed Sum</div><div class="value" id="aegisSpeedSum">—</div></div>
            <div class="metric"><div class="label">Hawkes B</div><div class="value" id="aegisHawkesB">—</div></div>
            <div class="metric"><div class="label">Conviction</div><div class="value" id="aegisConviction">—</div></div>
            <div class="metric"><div class="label">Funds</div><div class="value" id="aegisFunds">—</div><div class="hint">Paper trade</div></div>
            <div class="metric"><div class="label">Fills Today</div><div class="value" id="aegisFills">—</div></div>
            <div class="metric"><div class="label">Daily P&L</div><div class="value" id="aegisPnl">—</div></div>
            <div class="metric"><div class="label">Params Source</div><div class="value" id="aegisParamsSource">—</div></div>
          </div>
          <div class="reason" id="aegisEdges" style="font-size:11px;">Edges: waiting for live state…</div>
          <div class="reason" id="aegisReason">Waiting for live state…</div>
          <hr style="border: 0; border-top: 1px dashed var(--line); margin: 10px 0;" />
          <div class="row" id="aegis-ctx" style="font-size:11px; margin-bottom:4px; font-weight:600;">Active Trade: —</div>
        </div>
      </section>
    </div>

    <section class="panel">
      <div class="panel-head">
        <h2>Telemetry</h2>
        <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
          <div class="engine-switch" role="tablist" aria-label="Engine logs">
            <button type="button" id="logTabOF" class="active of" data-engine="of" onclick="setEngine('of')" title="ARGUS: Algorithmic Real-Time Granular Ultra-Microstructure Scalper">ARGUS</button>
            <button type="button" id="logTabAegis" class="aegis" data-engine="aegis" onclick="setEngine('aegis')">AEGIS</button>
          </div>
          <div class="pill" id="logCount">0 lines</div>
        </div>
      </div>
      <div class="logs-body" id="logs"></div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2>Active trades</h2>
        <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
          <div class="engine-switch" role="tablist" aria-label="Active trades engine">
            <button type="button" id="activeTabOF" class="active of" data-engine="of" onclick="setEngine('of')" title="ARGUS: Algorithmic Real-Time Granular Ultra-Microstructure Scalper">ARGUS</button>
            <button type="button" id="activeTabAegis" class="aegis" data-engine="aegis" onclick="setEngine('aegis')">AEGIS</button>
          </div>
          <div class="pill" id="activeCount">0 trades</div>
        </div>
      </div>
      <div class="trades-body" id="activeTrades"></div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2>Trade history</h2>
        <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
          <div class="engine-switch" role="tablist" aria-label="Trade history engine">
            <button type="button" id="histTabOF" class="active of" data-engine="of" onclick="setEngine('of')" title="ARGUS: Algorithmic Real-Time Granular Ultra-Microstructure Scalper">ARGUS</button>
            <button type="button" id="histTabAegis" class="aegis" data-engine="aegis" onclick="setEngine('aegis')">AEGIS</button>
          </div>
          <div class="pill" id="histCount">0 trades · disk</div>
        </div>
      </div>
      <div class="trades-body" id="trades"></div>
    </section>

    <!-- RESEARCH PANEL (DUAL-ENGINE: ARGUS + AEGIS) -->
    <section id="researchPanel" class="panel research-panel">
      <div class="panel-head">
        <h2>🔬 Research Lab</h2>
        <div id="researchMeta" class="research-meta" style="display:none;"></div>
      </div>

      <!-- Engine sub-tabs -->
      <div class="research-engine-tabs">
        <button class="research-engine-tab active" data-engine="argus" onclick="setResearchEngine('argus')">ARGUS · Algorithmic Real-Time Granular Ultra-Microstructure Scalper (Depth & Forensics)</button>
        <button class="research-engine-tab" data-engine="aegis" onclick="setResearchEngine('aegis')">AEGIS · Gamma · IV · Strike Heatmaps</button>
      </div>

      <!-- Date Selector -->
      <div class="research-controls">
        <div class="date-mode-switch">
          <label><input type="radio" name="dateMode" value="single" checked onchange="onDateModeChange('single')"> Single Date</label>
          <label><input type="radio" name="dateMode" value="range" onchange="onDateModeChange('range')"> Date Range</label>
          <label><input type="radio" name="dateMode" value="alltime" onchange="onDateModeChange('alltime')"> All-Time</label>
        </div>
        <div id="singleDateWrap" class="date-input-wrap">
          <label>Date: <input type="date" id="researchDate"></label>
        </div>
        <div id="rangeDateWrap" class="date-input-wrap" style="display:none;">
          <label>Start: <input type="date" id="researchStart"></label>
          <label>End: <input type="date" id="researchEnd"></label>
        </div>
        <div id="allTimeWrap" class="date-input-wrap" style="display:none;">
          <span class="hint">Aggregating all available daily snapshots…</span>
        </div>
        <div id="argusSymbolWrap" class="symbol-wrap">
          <label>Symbol: <input type="text" id="researchSymbol" placeholder="RELIANCE" list="universeList"></label>
          <datalist id="universeList"></datalist>
        </div>
        <button id="researchRunBtn" class="research-run-btn" onclick="runResearch()">Run Research</button>
      </div>

      <!-- ARGUS chart tabs -->
      <div class="research-tabs" id="argusTabs">
        <button class="research-tab active" data-tab="volume">Volume Profile</button>
        <button class="research-tab" data-tab="bid_depth">Bid Depth (L1-L5)</button>
        <button class="research-tab" data-tab="ask_depth">Ask Depth (L1-L5)</button>
        <button class="research-tab" data-tab="micro_dist">Microstructure Dist.</button>
        <button class="research-tab" data-tab="auction">Auction Imbalance</button>
      </div>

      <!-- AEGIS chart tabs -->
      <div class="research-tabs" id="aegisTabs" style="display:none;">
        <button class="research-tab active" data-tab="strike_heatmap">Strike Heatmap</button>
        <button class="research-tab" data-tab="iv_crush">IV Crush</button>
        <button class="research-tab" data-tab="oi_buildup">OI Buildup</button>
        <button class="research-tab" data-tab="premium_decay">Premium Decay</button>
        <button class="research-tab" data-tab="oib_dist">OIB Distribution</button>
      </div>

      <div class="research-content">
        <div id="researchLoader" class="research-loader"><div class="spinner"></div>Select a date and click Run Research…</div>
        <div id="chartVolume" class="chart-container" style="display:none;"></div>
        <div id="chartBidDepth" class="chart-container" style="display:none;"></div>
        <div id="chartAskDepth" class="chart-container" style="display:none;"></div>
        <div id="chartMicroDist" class="chart-container" style="display:none;"></div>
        <div id="chartAuction" class="chart-container small" style="display:none;"></div>
        <div id="chartStrikeHeatmap" class="chart-container" style="display:none;"></div>
        <div id="chartIvCrush" class="chart-container" style="display:none;"></div>
        <div id="chartOiBuildup" class="chart-container" style="display:none;"></div>
        <div id="chartPremiumDecay" class="chart-container" style="display:none;"></div>
        <div id="chartOibDist" class="chart-container" style="display:none;"></div>
      </div>
    </section>

  </div>

  <!-- PLOTLY.JS CDN (Lazy Load) -->
  <script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>

<script>
const fmt = (n, d=2) => (n === null || n === undefined || Number.isNaN(Number(n))) ? '—' : Number(n).toLocaleString(undefined, {maximumFractionDigits:d, minimumFractionDigits:0});
const money = (n) => (n === null || n === undefined) ? '—' : '₹' + fmt(n, 2);

let activeLogEngine = 'of'; let activeHistEngine = 'of'; let activeTradeEngine = 'of';
let latestLogs = { kronos: [], aegis: [], of: [] };
let latestHistory = { kronos: [], aegis: [], of: [] };
let latestActive = { kronos: {}, aegis: {}, of: {} };

function signalLabel(raw) { const t = String(raw || '').toUpperCase(); if (t.includes('CALL')) return 'CALL'; if (t.includes('PUT')) return 'PUT'; if (!t || t === '—' || t.includes('NEUTRAL') || t.includes('IDLE') || t.includes('WAIT') || t.includes('BOOT')) return 'Waiting'; return String(raw); }
function pillClass(text) { const t = String(text || '').toUpperCase(); if (t.includes('CALL')) return 'pill ok'; if (t.includes('PUT')) return 'pill bad'; if (t.includes('WAITING') || t.includes('NEUTRAL') || t.includes('IDLE')) return 'pill warn'; return 'pill'; }
function signalMetricClass(text) { const t = String(text || '').toUpperCase(); if (t.includes('CALL')) return 'metric signal-call'; if (t.includes('PUT')) return 'metric signal-put'; return 'metric'; }
function signalCellHtml(raw) { const label = signalLabel(raw); const t = String(label || '').toUpperCase(); if (t.includes('CALL')) return `<span class="sig-call">${label}</span>`; if (t.includes('PUT')) return `<span class="sig-put">${label}</span>`; return label || '—'; }
function pnlClass(v) { const n = Number(v); if (Number.isNaN(n) || n === 0) return 'mono'; return n > 0 ? 'mono pnl-pos' : 'mono pnl-neg'; }

function renderLogs() { if (window.getSelection && window.getSelection().toString().length > 0) return; const logs = latestLogs[activeLogEngine] || []; const box = document.getElementById('logs'); box.innerHTML = logs.map(l => `<div class="log-line ${activeLogEngine}">${l}</div>`).join('') || `<div class="log-line">No ${activeLogEngine === 'of' ? 'ARGUS' : activeLogEngine.toUpperCase()} telemetry yet.</div>`; document.getElementById('logCount').textContent = logs.length + ' lines · ' + (activeLogEngine === 'of' ? 'ARGUS' : activeLogEngine.toUpperCase()); }

function setEngine(engine) { activeLogEngine = engine; activeHistEngine = engine; activeTradeEngine = engine; document.getElementById('logTabAegis').className = engine === 'aegis' ? 'active aegis' : 'aegis'; document.getElementById('logTabOF').className = engine === 'of' ? 'active of' : 'of'; document.getElementById('histTabAegis').className = engine === 'aegis' ? 'active aegis' : 'aegis'; document.getElementById('histTabOF').className = engine === 'of' ? 'active of' : 'of'; document.getElementById('activeTabAegis').className = engine === 'aegis' ? 'active aegis' : 'aegis'; document.getElementById('activeTabOF').className = engine === 'of' ? 'active of' : 'of'; renderLogs(); renderHistory(); renderActiveTrades(); }

function renderHistory() { const rows = latestHistory[activeHistEngine] || []; const box = document.getElementById('trades'); let totalPnlHtml = ''; if (rows.length > 0) { const today = new Date(); const monthNames = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]; const datePrefix = ('0' + today.getDate()).slice(-2) + ' ' + monthNames[today.getMonth()] + ' ' + today.getFullYear(); const isoPrefix = today.getFullYear() + '-' + ('0' + (today.getMonth() + 1)).slice(-2) + '-' + ('0' + today.getDate()).slice(-2); let sum = 0; for (const t of rows) { if (t.date && (t.date.startsWith(datePrefix) || t.date.startsWith(isoPrefix))) { sum += Number(t.net_pl || 0); } } totalPnlHtml = ` &middot; Today's P&amp;L: <span class="${pnlClass(sum)}">${money(sum)}</span>`; } document.getElementById('histCount').innerHTML = rows.length + ' trades &middot; disk &middot; ' + (activeHistEngine === 'of' ? 'ARGUS' : activeHistEngine.toUpperCase()) + totalPnlHtml; if (!rows.length) { const histPath = activeHistEngine === 'of' ? 'ARGUS/data/trades/trade_history.json' : (activeHistEngine === 'aegis' ? 'AEGIS/data/trades/trade_history.json' : `${activeHistEngine}_bot/data/trades/trade_history.json`); box.innerHTML = `<div class="empty">No closed ${activeHistEngine === 'of' ? 'ARGUS' : activeHistEngine} trades on disk yet (<code>${histPath}</code>).</div>`; return; } const body = rows.map(t => ` <tr> <td class="mono" style="white-space:nowrap">${t.date || '—'}</td> <td class="mono" style="white-space:nowrap">${t.exit_time || '—'}</td> <td>${signalCellHtml(t.signal)}</td> <td class="mono">${t.symbol || '—'}</td> <td class="mono">${fmt(t.entry_price, 2)}</td> <td class="mono">${fmt(t.exit_price, 2)}</td> <td class="${pnlClass(t.net_pl)}">${money(t.net_pl)}</td> <td>${t.reason || '—'}</td> </tr>`).join(''); box.innerHTML = ` <table class="trades-table"> <thead> <tr> <th>Entry Time</th><th>Exit Time</th><th>Side</th><th>Symbol</th><th>Entry</th><th>Exit</th><th>P&amp;L</th><th>Exit reason</th> </tr> </thead> <tbody>${body}</tbody> </table>`; }

function renderActiveTrades() { const activeObj = latestActive[activeTradeEngine] || {}; const rows = Object.values(activeObj); const box = document.getElementById('activeTrades'); document.getElementById('activeCount').textContent = rows.length + ' active trades · ' + (activeTradeEngine === 'of' ? 'ARGUS' : activeTradeEngine.toUpperCase()); if (!rows.length) { box.innerHTML = `<div class="empty">No active ${activeTradeEngine === 'of' ? 'ARGUS' : activeTradeEngine} trades.</div>`; return; } const body = rows.map(t => { let price = t.entry_price; if (price === undefined && t.legs && t.legs.length) { price = t.legs[0].entry_price; } const symbol = t.symbol || (t.legs && t.legs.length ? t.legs[0].symbol : '—'); const type = t.type || t.signal_type || (t.legs && t.legs.length ? t.legs[0].signal_type : '—'); const qty = t.qty || (t.legs && t.legs.length ? t.legs[0].qty : '—'); return ` <tr> <td class="mono" style="white-space:nowrap">${(t.entry_time || '—').replace('T', ' ').substring(0, 19)}</td> <td>${signalCellHtml(type)}</td> <td class="mono">${symbol}</td> <td class="mono">${fmt(price, 2)}</td> <td class="mono">${qty}</td> <td class="mono">${money(t.margin || t.notional)}</td> <td>${t.reason || '—'}</td> </tr>`; }).join(''); box.innerHTML = ` <table class="trades-table"> <thead> <tr> <th>Entry Time</th><th>Side</th><th>Symbol</th><th>Entry Price</th><th>Qty</th><th>Margin / Notional</th><th>Reason</th> </tr> </thead> <tbody>${body}</tbody> </table>`; }

async function refresh() {
  const res = await fetch('/api/live_state'); const data = await res.json();
  document.getElementById('clock').textContent = data.server_time || '—';
  const age = data.tick_age_sec; document.getElementById('tickAge').textContent = (age === null || age === undefined) ? 'missing' : (age < 5 ? age.toFixed(1) + 's' : Math.round(age) + 's stale');
  if (data.initial_paper_capital != null) { document.getElementById('paperStart').textContent = money(data.initial_paper_capital); }
  
  const a = data.aegis || {}; const o = data.of || {};
  const oSig = signalLabel(o.signal || o.signal_status);
  document.getElementById('ofUniverse').textContent = (o.universe_count || 15) + ' Stocks';
  document.getElementById('ofSpot').textContent = o.top_setup || o.active_symbol || '—';
  document.getElementById('ofPrice').textContent = o.spot_price ? fmt(o.spot_price, 2) : '—';
  document.getElementById('ofSignal').textContent = oSig;
  document.getElementById('ofSignalMetric').className = signalMetricClass(oSig);
  document.getElementById('ofConviction').textContent = o.conviction ? fmt(o.conviction, 2) : '—';
  document.getElementById('ofShield').textContent = o.adv_shield || (o.regime || 'CLEAR');
  document.getElementById('ofKyle').textContent = o.kyle_lambda ? fmt(o.kyle_lambda, 4) : '—';
  document.getElementById('ofBranching').textContent = o.branching ? fmt(o.branching, 2) : '—';
  document.getElementById('ofWhale').textContent = o.whale_z ? fmt(o.whale_z, 2) + 'σ' : '—';
  document.getElementById('ofFunds').textContent = money(o.available_funds);
  document.getElementById('ofFills').textContent = fmt(o.trades_today, 0);
  document.getElementById('ofPnl').textContent = money(o.daily_pnl);
  document.getElementById('ofReason').textContent = o.reasoning || 'Waiting for live state…';
  const op = document.getElementById('ofPill'); op.textContent = oSig; op.className = pillClass(oSig);
  
  const oCtxEl = document.getElementById('argus-ctx');
  const ot = o.active_trade ? (Object.values(o.active_trade)[0] || {}) : {};
  const hasOpenArgus = o.has_open_trade || Boolean(Object.keys(o.active_trade || {}).length);
  oCtxEl.textContent = 'Active Trade: ' + (hasOpenArgus ? (ot.symbol || '—') + ' · ' + (ot.type || ot.signal || '—') + ' · entry ' + fmt(ot.entry_price || ot.price, 2) + (ot.reason ? ' · ' + ot.reason : '') : '—');
  
  document.getElementById('aegisIndex').textContent = a.active_index || '—'; document.getElementById('aegisSpot').textContent = fmt(a.spot_price, 2); document.getElementById('aegisAtm').textContent = fmt(a.atm_strike, 0); document.getElementById('aegisAtmIv').textContent = fmt(a.atm_iv, 2); document.getElementById('aegisGammaSum').textContent = fmt(a.gamma_sum, 2); document.getElementById('aegisSpeedSum').textContent = fmt(a.speed_sum, 2); document.getElementById('aegisHawkesB').textContent = fmt(a.hawkes_B, 3); document.getElementById('aegisConviction').textContent = fmt(a.conviction_eff, 2); document.getElementById('aegisFunds').textContent = money(a.available_funds); document.getElementById('aegisFills').textContent = fmt(a.trades_today, 0); document.getElementById('aegisPnl').textContent = money(a.daily_pnl); document.getElementById('aegisParamsSource').textContent = a.params_source || '—'; document.getElementById('aegisReason').textContent = a.has_open_trade ? 'Open trade active' : 'Idle — scanning for gamma burst'; try { const e = a.edges || {}; const m = e.macro || {}; const mi = e.micro || {}; const r = e.risk || {}; document.getElementById('aegisEdges').textContent = 'Edges · Macro EMA20 ' + fmt(m.ema20, 0) + ' / EMA50 ' + fmt(m.ema50, 0) + ' / ATR ' + fmt(m.atr, 1) + ' / VWAP ' + fmt(m.vwap, 0) + ' · Micro P1/P2/P3 persist ' + mi.p1_persist + '/' + mi.p2_persist + '/' + mi.p3_persist + ' · spread ' + mi.p1_spread + '/' + mi.p2_spread + '/' + mi.p3_spread + ' · Risk max ' + r.max_trades + ' / today ' + r.trades_today; } catch (err) {} const ap = document.getElementById('aegisPill'); ap.textContent = a.has_open_trade ? 'ACTIVE' : 'IDLE'; ap.className = a.has_open_trade ? 'pill ok' : 'pill warn';
  const ctxEl = document.getElementById('aegis-ctx'); const at = a.active_trade || {}; ctxEl.textContent = 'Active Trade: ' + (a.has_open_trade ? (at.symbol || '—') + ' · ' + (at.signal_type || at.type || '—') + ' · entry ' + fmt(at.entry_price, 2) : '—');

  latestLogs = { of: data.of_logs || [], kronos: data.kronos_logs || [], aegis: data.aegis_logs || [] };
  latestHistory = { of: data.of_trade_history || [], kronos: data.kronos_trade_history || [], aegis: data.aegis_trade_history || [] };
  latestActive = { of: data.of?.active_trade || {}, kronos: data.kronos?.active_trade || {}, aegis: data.aegis?.active_trade || {} };
  renderLogs(); renderHistory(); renderActiveTrades();
}

document.getElementById('logTabOF').addEventListener('click', () => setEngine('of')); document.getElementById('logTabAegis').addEventListener('click', () => setEngine('aegis')); document.getElementById('histTabOF').addEventListener('click', () => setEngine('of')); document.getElementById('histTabAegis').addEventListener('click', () => setEngine('aegis')); document.getElementById('activeTabOF').addEventListener('click', () => setEngine('of')); document.getElementById('activeTabAegis').addEventListener('click', () => setEngine('aegis'));

setEngine('of'); refresh(); setInterval(refresh, 2000);

/* ═══════════════════════════════════════════════════════════════
   RESEARCH MODE LOGIC — DUAL ENGINE (ARGUS + AEGIS) + DATE SELECTOR
   ═══════════════════════════════════════════════════════════════ */

let researchMode = false;
let researchEngine = 'argus';  // 'argus' | 'aegis'
let plotlyLoaded = true; // CDN loaded in head

function toggleResearchMode() {
  researchMode = !researchMode;
  const panel = document.getElementById('researchPanel');
  const btn = document.getElementById('researchToggle');
  if (researchMode) {
    panel.classList.add('visible');
    btn.classList.add('active');
    btn.textContent = '🔬 Research Mode (ON)';
    initResearchDefaults();
    loadUniverseList();
  } else {
    panel.classList.remove('visible');
    btn.classList.remove('active');
    btn.textContent = '🔬 Research Mode';
  }
}

function initResearchDefaults() {
  const today = new Date().toISOString().slice(0, 10);
  const d = document.getElementById('researchDate');
  if (!d.value) d.value = today;
}

async function loadUniverseList() {
  try {
    const res = await fetch('/api/analytics_dates?engine=argus');
    const data = await res.json();
    if (data.dates && data.dates.length) {
      const dl = document.getElementById('universeList');
      dl.innerHTML = data.dates.map(d => `<option value="${d}">`).join('');
    }
  } catch (e) { console.error('dates load', e); }
}

function setResearchEngine(engine) {
  researchEngine = (engine === 'vortex' || engine === 'of') ? 'argus' : engine;
  document.querySelectorAll('.research-engine-tab').forEach(t => t.classList.remove('active'));
  const activeTabBtn = document.querySelector(`.research-engine-tab[data-engine="${engine}"]`) || document.querySelector(`.research-engine-tab[data-engine="${researchEngine}"]`);
  if (activeTabBtn) activeTabBtn.classList.add('active');

  // Toggle tab strips + symbol input visibility
  const isArgus = researchEngine === 'argus';
  const argusTabsEl = document.getElementById('argusTabs') || document.getElementById('vortexTabs');
  const aegisTabsEl = document.getElementById('aegisTabs');
  const argusSymbolEl = document.getElementById('argusSymbolWrap') || document.getElementById('vortexSymbolWrap');
  
  if (argusTabsEl) argusTabsEl.style.display = isArgus ? 'flex' : 'none';
  if (aegisTabsEl) aegisTabsEl.style.display = isArgus ? 'none' : 'flex';
  if (argusSymbolEl) argusSymbolEl.style.display = isArgus ? 'inline-flex' : 'none';

  // Reset chart visibility
  document.querySelectorAll('.chart-container').forEach(c => c.style.display = 'none');
  document.getElementById('researchLoader').style.display = 'flex';
  document.getElementById('researchLoader').innerHTML = `<div class="spinner"></div>Select date and click Run Research…`;
  // Activate first tab of the visible strip
  const stripId = isArgus ? (document.getElementById('argusTabs') ? 'argusTabs' : 'vortexTabs') : 'aegisTabs';
  const firstTab = document.querySelector(`#${stripId} .research-tab`);
  if (firstTab) {
    document.querySelectorAll(`#${stripId} .research-tab`).forEach(t => t.classList.remove('active'));
    firstTab.classList.add('active');
  }
}

function onDateModeChange(mode) {
  document.getElementById('singleDateWrap').style.display = mode === 'single' ? 'inline-flex' : 'none';
  document.getElementById('rangeDateWrap').style.display = mode === 'range' ? 'inline-flex' : 'none';
  document.getElementById('allTimeWrap').style.display = mode === 'alltime' ? 'inline-flex' : 'none';
}

// Research Tab Switching (delegated for both strips)
document.addEventListener('click', (e) => {
  if (!e.target.classList.contains('research-tab')) return;
  const strip = e.target.closest('.research-tabs');
  if (!strip) return;
  strip.querySelectorAll('.research-tab').forEach(t => t.classList.remove('active'));
  e.target.classList.add('active');
  // Hide all charts, show the one for this tab
  document.querySelectorAll('.chart-container').forEach(c => c.style.display = 'none');
  const target = 'chart' + e.target.dataset.tab.charAt(0).toUpperCase() + e.target.dataset.tab.slice(1).replace(/_([a-z])/g, (_, c) => c.toUpperCase());
  const el = document.getElementById(target);
  if (el) el.style.display = 'block';
});

async function runResearch() {
  const mode = document.querySelector('input[name="dateMode"]:checked').value;
  const btn = document.getElementById('researchRunBtn');
  btn.disabled = true;
  document.getElementById('researchLoader').style.display = 'flex';
  document.getElementById('researchLoader').innerHTML = `<div class="spinner"></div>Crunching ${researchEngine.toUpperCase()}…`;
  document.querySelectorAll('.chart-container').forEach(c => c.style.display = 'none');

  let url;
  if (researchEngine === 'argus' || researchEngine === 'vortex') {
    const symbol = (document.getElementById('researchSymbol').value || '').toUpperCase();
    if (!symbol) { document.getElementById('researchLoader').innerHTML = `<span style="color:var(--bad)">Symbol required for ARGUS</span>`; btn.disabled = false; return; }
    const params = new URLSearchParams({ symbol });
    if (mode === 'alltime') params.set('all_time', '1');
    else if (mode === 'range') { params.set('start', document.getElementById('researchStart').value); params.set('end', document.getElementById('researchEnd').value); }
    else params.set('date', document.getElementById('researchDate').value);
    url = `/api/research?${params}`;
  } else {
    const params = new URLSearchParams();
    if (mode === 'alltime') params.set('all_time', '1');
    else if (mode === 'range') { params.set('start', document.getElementById('researchStart').value); params.set('end', document.getElementById('researchEnd').value); }
    else params.set('date', document.getElementById('researchDate').value);
    url = `/api/research_aegis?${params}`;
  }

  try {
    const res = await fetch(url);
    const data = await res.json();
    if (data.error) { document.getElementById('researchLoader').innerHTML = `<span style="color:var(--bad)">${data.error}</span>`; return; }
    renderResearchMeta(data.meta);
    if (researchEngine === 'argus' || researchEngine === 'vortex') renderArgusCharts(data);
    else renderAegisCharts(data);
    document.getElementById('researchLoader').style.display = 'none';
    // Show active tab's chart
    const isArgus = (researchEngine === 'argus' || researchEngine === 'vortex');
    const stripId = isArgus ? (document.getElementById('argusTabs') ? 'argusTabs' : 'vortexTabs') : 'aegisTabs';
    const activeTab = document.querySelector(`#${stripId} .research-tab.active`);
    if (activeTab) {
      const target = 'chart' + activeTab.dataset.tab.charAt(0).toUpperCase() + activeTab.dataset.tab.slice(1).replace(/_([a-z])/g, (_, c) => c.toUpperCase());
      document.querySelectorAll('.chart-container').forEach(c => c.style.display = 'none');
      const el = document.getElementById(target);
      if (el) el.style.display = 'block';
    }
  } catch (e) {
    document.getElementById('researchLoader').innerHTML = `<span style="color:var(--bad)">Error: ${e.message}</span>`;
  } finally {
    btn.disabled = false;
  }
}

function renderResearchMeta(meta) {
  const el = document.getElementById('researchMeta');
  el.style.display = 'flex';
  if (!meta) { el.innerHTML = ''; return; }
  const parts = [];
  if (meta.symbol) parts.push(`<span><b>${meta.symbol}</b></span>`);
  if (meta.date) parts.push(`<span>${meta.date}</span>`);
  if (meta.mode) parts.push(`<span>Mode: ${meta.mode}</span>`);
  if (meta.range) parts.push(`<span>Range: ${meta.range}</span>`);
  if (meta.days != null) parts.push(`<span>Days: ${meta.days}</span>`);
  if (meta.ticks != null) parts.push(`<span>Ticks: ${meta.ticks.toLocaleString()}</span>`);
  if (meta.timerange) parts.push(`<span>Time: ${meta.timerange}</span>`);
  if (meta.price_range) parts.push(`<span>Price: ${meta.price_range}</span>`);
  el.innerHTML = parts.join('');
}

function renderArgusCharts(data) {
  renderPlotly('chartVolume', data.volume_profile);
  renderPlotly('chartBidDepth', data.bid_depth);
  renderPlotly('chartAskDepth', data.ask_depth);
  renderPlotly('chartMicroDist', data.micro_dist);
  renderPlotly('chartAuction', data.auction);
  // Range/all-time: render time-series if present
  if (data.time_series) {
    const ts = data.time_series;
    if (ts.oib_mean) renderPlotly('chartMicroDist', ts.oib_mean);
    if (ts.spread_mean) renderPlotly('chartAuction', ts.spread_mean);
  }
}
const renderVortexCharts = renderArgusCharts;

function renderAegisCharts(data) {
  renderPlotly('chartStrikeHeatmap', data.strike_heatmap);
  renderPlotly('chartIvCrush', data.iv_crush);
  renderPlotly('chartOiBuildup', data.oi_buildup);
  renderPlotly('chartPremiumDecay', data.premium_decay);
  renderPlotly('chartOibDist', data.oib_dist);
}

function renderPlotly(containerId, figJson) {
  const el = document.getElementById(containerId);
  if (!el || !figJson || !figJson.data) { if (el) el.style.display = 'none'; return; }
  el.style.display = 'block';
  Plotly.react(el, figJson.data, figJson.layout, {responsive: true, displayModeBar: true, displaylogo: false});
}

// Quick Symbol Picker: Click any Symbol in Active/History tables to load ARGUS research
document.addEventListener('click', (e) => {
  if (researchMode && (researchEngine === 'argus' || researchEngine === 'vortex') && e.target.tagName === 'TD' && e.target.classList.contains('mono')) {
    const symbol = e.target.textContent.trim();
    if (/^[A-Z&]{2,15}$/.test(symbol)) {
      document.getElementById('researchSymbol').value = symbol;
      runResearch();
    }
  }
});

</script>
</body>
</html>"""

# ─── FLASK ROUTES (Unchanged Logic) ───
@app.route("/")
def index(): return CONSOLE_HTML

@app.route("/api/health")
def health(): return jsonify({ "ok": True, "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "tick_file": SHARED_TICK_FILE, "tick_age_sec": _tick_age_seconds() })

@app.route("/api/live_state")
def live_state():
    kronos = get_kronos_live_state()
    aegis = get_aegis_live_state()
    argus = get_argus_live_state()

    kronos["available_funds"] = _capital(KRONOS_DATA, force_paper=True)
    aegis["available_funds"] = _capital(AEGIS_DATA, force_paper=True)
    of_funds = _capital(ORDERFLOW_DATA)
    of_positions = _open_positions(ORDERFLOW_DATA) or {}
    of_open_pnl = None
    if of_positions:
        try:
            of_open_pnl = sum(float(v.get("margin", 0)) for v in of_positions.values() if isinstance(v, dict))
        except Exception: pass

    of_log_path = os.path.join(ORDERFLOW_LOGS, "argus.log") if os.path.exists(os.path.join(ORDERFLOW_LOGS, "argus.log")) else os.path.join(ORDERFLOW_LOGS, "stock_order_flow.log")
    of_logs = [html.escape(line) for line in _tail_log(of_log_path, 80)]
    of_hist = _trade_history(ORDERFLOW_DATA)
    kronos["active_trade"] = _active_trade(KRONOS_DATA)
    aegis["active_trade"] = _active_trade_aegis(AEGIS_DATA)
    kronos_logs = [html.escape(line) for line in _tail_log(os.path.join(KRONOS_LOGS, "kronos_bot.log"), 80)]
    aegis_logs = [html.escape(line) for line in _tail_log(os.path.join(AEGIS_LOGS, "aegis.log"), 80)]
    kronos_hist = _trade_history(KRONOS_DATA)
    aegis_hist = _trade_history(AEGIS_DATA)
    for rows in (kronos_hist, aegis_hist, of_hist):
        for row in rows:
            for key in ("date", "exit_time", "signal", "symbol", "strategy", "reason", "result"):
                if key in row and row[key] is not None: row[key] = html.escape(str(row[key]))

    # Compute ARGUS today stats
    today_iso = datetime.now().strftime("%Y-%m-%d")
    today_fmt = datetime.now().strftime("%d %b %Y")
    of_today_fills = len(of_positions)
    of_daily_pnl = 0.0
    for t in of_hist:
        d = str(t.get("date", ""))
        if d.startswith(today_iso) or d.startswith(today_fmt):
            of_today_fills += 1
            try:
                of_daily_pnl += float(t.get("net_pl") or 0.0)
            except Exception:
                pass

    # Read universe count if universe.json exists
    uni_path = os.path.join(ORDERFLOW_DATA, "universe.json")
    uni_count = 15
    if os.path.exists(uni_path):
        try:
            with open(uni_path, "r") as f:
                ud = json.load(f)
                uni_count = len(ud.get("stocks", [])) or 15
        except Exception:
            pass

    of_signal = argus.get("signal", "WAITING")
    of_reason = argus.get("reasoning", "Scanning universe for microstructure conviction…")
    of_top_setup = argus.get("active_symbol", "—")
    of_price = argus.get("spot_price", 0.0)
    of_conv = argus.get("conviction", 0.0)

    if of_positions:
        first = next(iter(of_positions.values()))
        if isinstance(first, dict):
            of_signal = first.get("type") or first.get("signal", "BUY")
            of_reason = first.get("reason", "Open position active")
            of_top_setup = first.get("symbol", "—")
            of_price = float(first.get("entry_price") or first.get("price") or 0.0)
            of_conv = float(first.get("conviction") or (first.get("metrics") or {}).get("conviction") or 0.0)

    argus_payload = {
        "available_funds": of_funds,
        "active_trade": of_positions,
        "signal": of_signal,
        "reasoning": of_reason,
        "top_setup": of_top_setup,
        "spot_price": of_price,
        "conviction": of_conv,
        "adv_shield": argus.get("adv_shield", "CLEAR"),
        "regime": argus.get("regime", "BURST"),
        "kyle_lambda": argus.get("kyle_lambda", 0.0),
        "branching": argus.get("branching", 0.0),
        "whale_z": argus.get("whale_z", 0.0),
        "universe_count": uni_count,
        "trades_today": of_today_fills,
        "daily_pnl": of_daily_pnl,
        "open_pnl": of_open_pnl,
        "data_seeded": bool(of_logs),
        "has_open_trade": bool(of_positions),
    }

    return jsonify({
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tick_age_sec": _tick_age_seconds(),
        "initial_paper_capital": INITIAL_PAPER_CAPITAL,
        "kronos": kronos,
        "aegis": aegis,
        "of": argus_payload,
        "of_logs": of_logs,
        "kronos_logs": kronos_logs,
        "aegis_logs": aegis_logs,
        "of_trade_history": of_hist,
        "kronos_trade_history": kronos_hist,
        "aegis_trade_history": aegis_hist,
        "default_log_engine": "of",
    })

def main(): app.run(host="0.0.0.0", port=5000, debug=False)

if __name__ == "__main__": main()
