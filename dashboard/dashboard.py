"""
Quant Console — High-Performance Precision Web Server for AEGIS Production Version 1.0 (v1.0 Locked).
Pure AEGIS autonomous execution cockpit with dedicated /research console.
"""

from __future__ import annotations

import html
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_file
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[1]  # project root
load_dotenv(BASE_DIR / ".env")

# Ensure project root is first on sys.path and remove self-dir so 'dashboard' package resolves correctly
_self_dir = str(Path(__file__).resolve().parent)
while _self_dir in sys.path:
    sys.path.remove(_self_dir)
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# ─── COMMON IMPORTS ───
from config.settings import INITIAL_PAPER_CAPITAL
from ipc.live_state import (
    get_aegis_live_state,
    SHARED_TICK_FILE,
    AEGIS_STATE_FILE,
)
from config.paths import get_runtime_paths

# ─── MODULAR HTML TEMPLATES ───
from dashboard.cockpit_html import COCKPIT_HTML as CONSOLE_HTML
from dashboard.research_html import RESEARCH_HTML
from dashboard.telemetry_loader import (
    get_journal_data,
    get_trade_detail,
    CSV_PATH,
    XLSX_PATH,
)

app = Flask(__name__)

AEGIS_PATHS = get_runtime_paths("aegis")
AEGIS_DATA = AEGIS_PATHS["data_dir"]
AEGIS_LOGS = AEGIS_PATHS["logs_dir"]


# ─── HELPERS ───
def _read_json(path, default=None):
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            try:
                with open(path, "r", encoding="utf-16") as f:
                    return json.load(f)
            except Exception:
                pass
    return default


def _capital(data_dir: str, force_paper: bool = False) -> float:
    state = _read_json(os.path.join(data_dir, "capital_state.json"), {}) or {}
    return float(state.get("available_funds") or 588.36)


def _active_trade_aegis(data_dir: str):
    """AEGIS writes its active context to aegis_active_trade_context.json."""
    data = _read_json(os.path.join(data_dir, "trades", "aegis_active_trade_context.json"), None)
    if isinstance(data, dict) and data.get("status") == "OPEN":
        return {"main": data}
    return None


ANSI_REGEX = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')

# ─── P2 IN-MEMORY TTL CACHE CONTAINERS (SUB-MILLISECOND UI SERVING) ───
_LOGS_CACHE: dict[str, Any] = {"ts": 0.0, "lines": [], "escaped": [], "n": 200}
_TRADE_HIST_CACHE: dict[str, Any] = {"ts": 0.0, "data": [], "limit": 10}
_CAPITAL_CACHE: dict[str, Any] = {"ts": 0.0, "val": 588.36}
_ACTIVE_TRADE_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}


def _clean_log_line(line: str) -> str:
    """Strips raw ANSI color escape codes and returns clean text."""
    if not line:
        return ""
    return ANSI_REGEX.sub("", line).strip()


def _get_aegis_logs(n: int = 200) -> list[str]:
    """
    Returns the latest enriched, de-noised operational telemetry log lines.
    Uses 1.0s in-memory TTL caching to eliminate repetitive 384KB disk reads
    and 3,000+ regex sweeps on rapid dashboard polls.
    """
    now = time.time()
    if now - _LOGS_CACHE["ts"] < 1.0 and _LOGS_CACHE["n"] == n and _LOGS_CACHE["lines"]:
        return _LOGS_CACHE["lines"]

    candidate_paths = [
        "/home/opc/.pm2/logs/aegis-bot-out.log",
        os.path.join(AEGIS_LOGS, "aegis_bot.log"),
        os.path.join(AEGIS_LOGS, "aegis.log"),
        str(BASE_DIR / "AEGIS" / "logs" / "aegis_bot.log"),
    ]
    for p in candidate_paths:
        if os.path.exists(p) and os.path.getsize(p) > 0:
            try:
                # Fast chunked binary tail read (up to 384 KB)
                with open(p, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    chunk = min(size, 384 * 1024)
                    f.seek(size - chunk, os.SEEK_SET)
                    raw_bytes = f.read(chunk)

                raw_lines = raw_bytes.decode("utf-8", errors="replace").splitlines()
                cleaned = []
                for ln in raw_lines:
                    c = _clean_log_line(ln)
                    if not c:
                        continue
                    # Filter out spam poller noise so real telemetry is never displaced
                    if "[OIQuotesPoller]" in c:
                        continue
                    cleaned.append(c)

                if cleaned:
                    result = cleaned[-n:]
                    _LOGS_CACHE["ts"] = now
                    _LOGS_CACHE["n"] = n
                    _LOGS_CACHE["lines"] = result
                    # Pre-escape HTML once so live_state doesn't loop 200 items on every poll
                    _LOGS_CACHE["escaped"] = [html.escape(line) for line in result]
                    return result
            except Exception:
                pass
    return []


def _trade_history_aegis(limit: int = 20) -> list[dict]:
    now = time.time()
    if now - _TRADE_HIST_CACHE["ts"] < 2.0 and _TRADE_HIST_CACHE["limit"] == limit and _TRADE_HIST_CACHE["data"]:
        return _TRADE_HIST_CACHE["data"]

    try:
        jdata = get_journal_data()
        trades = jdata.get("trades", [])
        IST = timezone(timedelta(hours=5, minutes=30))
        today_ist = datetime.now(IST).strftime("%Y-%m-%d")
        res = []
        for t in trades[:limit]:
            iso_d = str(t.get("iso_date", "")).strip()
            gross_val = float(t.get("gross_pnl_rs", t.get("pnl_rs", 0.0)))
            charges_val = float(t.get("charges_rs", 0.0))
            net_val = float(t.get("net_pnl_rs", round(gross_val - charges_val, 2)))
            raw_res = str(t.get("result", ""))
            if gross_val > 0 or "PROFIT" in raw_res or "WIN" in raw_res:
                res_val = "PROFIT"
            elif gross_val == 0 and ("BE" in raw_res or "PROFIT" in raw_res or "WIN" in raw_res):
                res_val = "BE PROFIT"
            elif gross_val < 0:
                res_val = "LOSS"
            else:
                res_val = raw_res
            res.append({
                "id": t.get("id"),
                "date": f"{t.get('date')} {t.get('entry_time')}",
                "iso_date": iso_d,
                "is_today": (iso_d == today_ist),
                "symbol": f"{t.get('index')} {t.get('strike')}",
                "signal": t.get("type"),
                "entry_price": t.get("entry_price"),
                "exit_price": t.get("exit_price"),
                "gross_pl": gross_val,
                "brokerage": float(t.get("brokerage_rs", 10.0)),
                "taxes": float(t.get("taxes_rs", round(charges_val - 10.0, 2))),
                "charges": charges_val,
                "net_pl": net_val,
                "opt_pct": t.get("opt_pct"),
                "spot_peak_mfe_pts": t.get("spot_peak_mfe_pts", 0.0),
                "spot_peak_mae_pts": t.get("spot_peak_mae_pts", 0.0),
                "spot_time_to_mfe_sec": t.get("spot_time_to_mfe_sec", 0.0),
                "spot_time_to_mfe_ms": t.get("spot_time_to_mfe_ms", 0),
                "reason": t.get("exit_reason"),
                "result": res_val,
                "pillar": t.get("pillar"),
            })
        _TRADE_HIST_CACHE["ts"] = now
        _TRADE_HIST_CACHE["limit"] = limit
        _TRADE_HIST_CACHE["data"] = res
        return res
    except Exception:
        return []


def _tick_age_seconds() -> float | None:
    target = AEGIS_STATE_FILE if os.path.exists(AEGIS_STATE_FILE) else SHARED_TICK_FILE
    if not os.path.exists(target):
        return None
    try:
        return max(0.0, time.time() - os.path.getmtime(target))
    except Exception:
        return None


# ─── CORE ROUTES ───
@app.route("/")
def index():
    """Renders the dedicated, revamped AEGIS Production Version 1.0 Live Cockpit."""
    return CONSOLE_HTML


@app.route("/quantum")
def quantum_cockpit():
    """Renders the Linear-aesthetic Cockpit."""
    return CONSOLE_HTML


@app.route("/research")
def research_page():
    """Renders the dedicated Research & Forensics Console with Chart.js visualizations."""
    return RESEARCH_HTML


@app.route("/api/health")
def health():
    target = AEGIS_STATE_FILE if os.path.exists(AEGIS_STATE_FILE) else SHARED_TICK_FILE
    return jsonify({
        "ok": True,
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tick_file": target,
        "tick_age_sec": _tick_age_seconds(),
    })


@app.route("/api/state")
@app.route("/api/live_state")
def live_state():
    """Real-time operational telemetry for the Live Cockpit."""
    aegis = get_aegis_live_state()
    aegis["available_funds"] = _capital(AEGIS_DATA, force_paper=True)
    aegis["active_trade"] = _active_trade_aegis(AEGIS_DATA)

    aegis_logs = _get_aegis_logs(200)
    aegis_hist = _trade_history_aegis(limit=10)

    # Dynamic IST date computation for today's scorecard
    IST = timezone(timedelta(hours=5, minutes=30))
    today_ist = datetime.now(IST).strftime("%Y-%m-%d")
    today_trades = [t for t in aegis_hist if t.get("is_today")]

    today_count = len(today_trades)
    today_profits = sum(1 for t in today_trades if "PROFIT" in str(t.get("result", "")) or "WIN" in str(t.get("result", "")))
    today_losses = sum(1 for t in today_trades if "LOSS" in str(t.get("result", "")))
    today_gross_pnl = sum(float(t.get("gross_pl", 0.0)) for t in today_trades)
    today_charges = sum(float(t.get("charges", 0.0)) for t in today_trades)
    today_net_pnl = sum(float(t.get("net_pl", 0.0)) for t in today_trades)
    today_pr = round((today_profits / today_count * 100.0), 1) if today_count > 0 else 0.0

    today_stats = {
        "date_ist": today_ist,
        "trades_count": today_count,
        "count": today_count,
        "wins": today_profits,
        "profits": today_profits,
        "losses": today_losses,
        "win_rate_pct": today_pr,
        "profit_rate_pct": today_pr,
        "pnl_rs": round(today_net_pnl, 2),
        "gross_pnl_rs": round(today_gross_pnl, 2),
        "charges_rs": round(today_charges, 2),
        "net_pnl_rs": round(today_net_pnl, 2),
        "today_trades": today_trades,
    }

    return jsonify({
        "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tick_age_sec": _tick_age_seconds(),
        "initial_paper_capital": INITIAL_PAPER_CAPITAL,
        "aegis": aegis,
        "aegis_logs": _LOGS_CACHE.get("escaped", [html.escape(line) for line in aegis_logs]),
        "aegis_trade_history": aegis_hist,
        "today_stats": today_stats,
        # Backward compatibility stubs for legacy components
        "kronos": {},
        "of": {},
        "of_logs": [],
        "kronos_logs": [],
        "of_trade_history": [],
        "kronos_trade_history": [],
        "default_log_engine": "aegis",
    })


def _nan_to_none(obj: Any) -> Any:
    """Recursively replace NaN/±Infinity floats with None → strict-JSON-safe payload.

    Belt-and-braces for the Research console (2026-11-05): Flask's jsonify emits literal `NaN`
    for NaN floats, which browsers reject inside JSON.parse — the /research console rendered
    blank (silent catch) while curl probes returned HTTP 200. Root cause is NaN-guarded at the
    source in telemetry_loader; this defends the whole API surface (incl. trade_detail's
    telemetry JSONL records, which can also carry NaN).
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _nan_to_none(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_nan_to_none(v) for v in obj]
    return obj


# ─── RESEARCH & FORENSICS API ROUTES ───
@app.route("/api/research_data")
def api_research_data():
    """Returns aggregated KPIs, equity timeline, pillar stats, and enriched trades."""
    data = get_journal_data()
    return jsonify(_nan_to_none(data))


@app.route("/api/trade_detail/<int:trade_id>")
def api_trade_detail(trade_id: int):
    """Returns deep-dive 4-gate forensic payload for a specific trade."""
    detail = get_trade_detail(trade_id)
    if not detail:
        return jsonify({"error": "Trade not found"}), 404
    return jsonify(_nan_to_none(detail))


@app.route("/download/excel")
def download_excel():
    """Direct one-click download for formatted Excel trade ledger."""
    if not XLSX_PATH.exists():
        get_journal_data(force_reload=True)
    return send_file(XLSX_PATH, as_attachment=True, download_name="AEGIS_TRADE_JOURNAL.xlsx")


@app.route("/download/csv")
def download_csv():
    """Direct one-click download for CSV trade ledger."""
    if not CSV_PATH.exists():
        get_journal_data(force_reload=True)
    return send_file(CSV_PATH, as_attachment=True, download_name="AEGIS_TRADE_JOURNAL.csv")


def main():
    app.run(host="0.0.0.0", port=5000, debug=False)


if __name__ == "__main__":
    main()
