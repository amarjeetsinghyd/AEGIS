import os
import json
import time
import threading
from datetime import datetime
from flask import Flask, render_template_string, request, jsonify, redirect, url_for
from astra_bot.config.settings import DATA_DIR, LOGS_DIR, BROKER, ENABLE_LIVE_BROKERAGE_EXECUTION

app = Flask(__name__)


CAPITAL_STATE_FILE = os.path.join(DATA_DIR, "capital_state.json")
ACTIVE_CONTEXT_FILE = os.path.join(DATA_DIR, "active_trade_context.json")
LOG_FILE = os.path.join(LOGS_DIR, "astra_bot.log")
HISTORY_FILE = os.path.join(DATA_DIR, "trades", "trade_history.json")

# Fast Inter-Process RAM Disk Cache File (/dev/shm on Linux VPS, DATA_DIR fallback)
STATE_CACHE_FILE = "/dev/shm/kdo_live_state.json" if os.path.exists("/dev/shm") else os.path.join(DATA_DIR, "kdo_live_state.json")

_dashboard_adapter = None
_dashboard_capital_cache = {"time": 0, "val": 0.0}

_state_lock = threading.Lock()
LIVE_KDO_STATE = {
    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "active_index": "NIFTY",
    "spot_price": 0.0,
    "gex_net": 0.0,
    "gex_regime": "NEUTRAL_GAMMA",
    "velocity": 0.0,
    "acceleration": 0.0,
    "signal_status": "🟡 NEUTRAL (CHOPPY / WAIT)",
    "reasoning": "Waiting for live market tick feed...",
    "kinematic_ready": False,
    "rollover_pause": False,
    "option_chain": [],
    "active_trade": None,
    "available_funds": 0.0
}


def update_kdo_live_state(data: dict):
    """Saves live state atomically to RAM disk /dev/shm for 0ms cross-process IPC."""
    global LIVE_KDO_STATE
    with _state_lock:
        LIVE_KDO_STATE.update(data)
        LIVE_KDO_STATE["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        shm_path = STATE_CACHE_FILE
        temp_path = shm_path + ".tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(LIVE_KDO_STATE, f)
            os.replace(temp_path, shm_path)
        except Exception:
            pass


def get_kdo_live_state() -> dict:
    """Reads live state from RAM disk /dev/shm."""
    if os.path.exists(STATE_CACHE_FILE):
        try:
            with open(STATE_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return LIVE_KDO_STATE


def get_capital_state():
    state = {"available_funds": 0.0, "locked_withdrawal_amount": 0.0}
    paths = [
        "/home/opc/astra_bot/data/capital_state.json",
        os.path.join(os.path.dirname(DATA_DIR), "data", "capital_state.json"),
        CAPITAL_STATE_FILE,
        os.path.join(DATA_DIR, "capital_state.json")
    ]
    for p in paths:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    state = json.load(f)
                    break
            except Exception:
                pass

    if ENABLE_LIVE_BROKERAGE_EXECUTION:
        now_ts = time.time()
        global _dashboard_adapter, _dashboard_capital_cache
        if now_ts - _dashboard_capital_cache["time"] > 60:
            try:
                from astra_bot.core.shoonya_adapter import ShoonyaAdapter
                if _dashboard_adapter is None:
                    _dashboard_adapter = ShoonyaAdapter()
                    _dashboard_adapter.session_provider._restore_or_login()
                live_cash = _dashboard_adapter.get_available_funds()
                if live_cash > 0:
                    _dashboard_capital_cache = {"time": now_ts, "val": round(live_cash, 2)}
                    state["available_funds"] = round(live_cash, 2)
            except Exception:
                pass
        elif _dashboard_capital_cache["val"] > 0:
            state["available_funds"] = _dashboard_capital_cache["val"]

    return state


def get_active_trade():
    paths = [
        "/home/opc/astra_bot/data/trades/active_trade_context.json",
        os.path.join(os.path.dirname(DATA_DIR), "data", "trades", "active_trade_context.json"),
        ACTIVE_CONTEXT_FILE,
        os.path.join(DATA_DIR, "trades", "active_trade_context.json")
    ]
    for p in paths:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data and data.get("status") == "OPEN":
                        return data
            except Exception:
                pass
    return None


def get_trade_history():
    paths = [
        "/home/opc/astra_bot/data/trades/trade_history.json",
        os.path.join(os.path.dirname(DATA_DIR), "data", "trades", "trade_history.json"),
        HISTORY_FILE,
        os.path.join(DATA_DIR, "trades", "trade_history.json")
    ]
    for p in paths:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    history = json.load(f)
                    if history:
                        return history[::-1]
            except Exception:
                pass
    return []


def colorize_log(line: str) -> str:
    """Bloomberg Console log colorizer."""
    import html
    escaped = html.escape(line)
    
    if "KDO SIGNAL TRIGGERED: CALL" in escaped or "CALL Entry" in escaped or "EXECUTED" in escaped:
        return f'<span style="color: #00ff00; font-weight: bold;">{escaped}</span>'
    elif "KDO SIGNAL TRIGGERED: PUT" in escaped or "PUT Entry" in escaped:
        return f'<span style="color: #ff0000; font-weight: bold;">{escaped}</span>'
    elif "NEGATIVE_GAMMA" in escaped or "GEX" in escaped:
        return f'<span style="color: #00ffff;">{escaped}</span>'
    elif "Velocity" in escaped or "Acceleration" in escaped or "Kinematic" in escaped:
        return f'<span style="color: #ffb000;">{escaped}</span>'
    elif "STRIKE ROLLOVER" in escaped or "Kinematic Pause" in escaped:
        return f'<span style="color: #ff8800; font-weight: bold;">{escaped}</span>'
    elif "Orderflow Exhaustion" in escaped or "TRADE CLOSED" in escaped:
        return f'<span style="color: #ffff00; font-weight: bold;">{escaped}</span>'
    elif "ERROR" in escaped or "Exception" in escaped:
        return f'<span style="color: #ff0000;">{escaped}</span>'
    else:
        return f'<span style="color: #aaaaaa;">{escaped}</span>'


def get_recent_logs(num_lines=40):
    if os.path.exists(LOG_FILE):
        try:
            import re
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
                clean_lines = [re.sub(r'\x1b\[[0-9;]*m|\033\[[0-9;]*m', '', line) for line in lines[-num_lines:]]
                return [colorize_log(line) for line in clean_lines]
        except Exception:
            pass
    return ["<span style='color: #666666;'>[TELEMETRY LOG WAITING]</span>"]


# --- BLOOMBERG TERMINAL ULTRA-DARK HTML & CSS TEMPLATE ---
BLOOMBERG_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ASTRA KINETIC TERMINAL | INSTITUTIONAL MICROSTRUCTURE ENGINE (ASTRA v3.0)</title>
    <style>
        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: 'Consolas', 'Courier New', 'Monaco', monospace;
        }

        body {
            background-color: #000000;
            color: #ffb000;
            padding: 10px;
            font-size: 13px;
            line-height: 1.3;
        }

        .terminal-container {
            max-width: 1400px;
            margin: 0 auto;
            border: 1px solid #333333;
            padding: 10px;
            background-color: #000000;
        }

        .header-bar {
            border-bottom: 2px solid #ffb000;
            padding-bottom: 5px;
            margin-bottom: 10px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .header-title {
            color: #ffffff;
            font-size: 16px;
            font-weight: bold;
            letter-spacing: 1px;
        }

        .header-status {
            color: #00ff00;
            font-size: 12px;
        }

        .signal-card {
            border: 2px solid #ffb000;
            padding: 12px;
            margin-bottom: 12px;
            background-color: #050505;
        }

        .signal-title-box {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 6px;
        }

        .signal-badge {
            font-size: 18px;
            font-weight: bold;
            padding: 4px 12px;
            letter-spacing: 1px;
        }

        .signal-bullish { color: #00ff00; border: 1px solid #00ff00; background: #002200; }
        .signal-bearish { color: #ff0000; border: 1px solid #ff0000; background: #220000; }
        .signal-neutral { color: #ffb000; border: 1px solid #ffb000; background: #221500; }
        .signal-pause   { color: #ff8800; border: 1px solid #ff8800; background: #221000; }

        .signal-reason {
            color: #ffffff;
            font-size: 13px;
            margin-top: 5px;
            padding: 6px;
            background-color: #111111;
            border-left: 3px solid #ffb000;
        }

        .metrics-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 10px;
            margin-bottom: 12px;
        }

        .metric-box {
            border: 1px solid #333333;
            background-color: #080808;
            padding: 8px 10px;
        }

        .metric-label {
            color: #888888;
            font-size: 11px;
            text-transform: uppercase;
        }

        .metric-value {
            color: #ffffff;
            font-size: 18px;
            font-weight: bold;
            margin-top: 2px;
        }

        .text-green { color: #00ff00 !important; }
        .text-red   { color: #ff0000 !important; }
        .text-cyan  { color: #00ffff !important; }
        .text-amber { color: #ffb000 !important; }

        .section-title {
            color: #00ffff;
            font-size: 13px;
            font-weight: bold;
            margin: 10px 0 5px 0;
            border-bottom: 1px solid #333333;
            padding-bottom: 3px;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 12px;
            font-size: 12px;
        }

        th {
            background-color: #111111;
            color: #ffb000;
            border: 1px solid #333333;
            padding: 5px 8px;
            text-align: right;
            font-weight: bold;
        }

        th.col-strike { text-align: center; color: #ffffff; background-color: #222222; }

        td {
            border: 1px solid #222222;
            padding: 5px 8px;
            text-align: right;
            color: #cccccc;
        }

        td.col-strike {
            text-align: center;
            font-weight: bold;
            color: #ffffff;
            background-color: #111111;
        }

        tr:nth-child(even) { background-color: #050505; }
        tr:hover { background-color: #151515; }

        .terminal-log-box {
            border: 1px solid #333333;
            background-color: #030303;
            height: 220px;
            overflow-y: auto;
            padding: 8px;
            font-size: 11px;
            white-space: pre-wrap;
            word-break: break-all;
        }

        .win-tag { color: #00ff00; font-weight: bold; }
        .loss-tag { color: #ff0000; font-weight: bold; }
    </style>
</head>
<body>

<div class="terminal-container">
    <div class="header-bar">
        <div class="header-title">ASTRA KINETIC TERMINAL | INSTITUTIONAL MICROSTRUCTURE ENGINE (ASTRA v3.0)</div>
        <div class="header-status">
            LIVE SYSTEM ONLINE | <span id="clock" style="color: #00ff00; font-weight: bold;">--:--:--</span>
            <button onclick="triggerKillSwitch()" style="background-color: #ff0000; color: #ffffff; border: none; padding: 3px 8px; font-weight: bold; cursor: pointer; border-radius: 3px; margin-left: 10px; font-size: 11px;">🔴 MOBILE KILL SWITCH</button>
            <button onclick="resetKillSwitch()" style="background-color: #222222; color: #00ff00; border: 1px solid #00ff00; padding: 3px 6px; font-size: 11px; cursor: pointer; border-radius: 3px; margin-left: 5px;">RESET</button>
        </div>
    </div>

    <div class="signal-card">
        <div class="signal-title-box">
            <span style="color: #888888; font-size: 11px;">CURRENT MARKET SIGNAL</span>
            <div id="signal-badge" class="signal-badge signal-neutral">🟡 NEUTRAL (CHOPPY / WAIT)</div>
        </div>
        <div id="signal-reason" class="signal-reason">Waiting for live tick stream evaluation...</div>
    </div>

    <div class="metrics-grid">
        <div class="metric-box">
            <div class="metric-label">ACTIVE INDEX SPOT</div>
            <div id="spot-price" class="metric-value text-amber">--.--</div>
        </div>
        <div class="metric-box">
            <div class="metric-label">NET GEX (GAMMA EXPOSURE)</div>
            <div id="gex-net" class="metric-value text-cyan">Rs 0.00</div>
        </div>
        <div class="metric-box">
            <div class="metric-label">VELOCITY (V_t)</div>
            <div id="velocity" class="metric-value text-amber">0</div>
        </div>
        <div class="metric-box">
            <div class="metric-label">ACCELERATION (A_t)</div>
            <div id="acceleration" class="metric-value text-amber">0</div>
        </div>
        <div class="metric-box">
            <div class="metric-label">AVAILABLE CAPITAL</div>
            <div id="avail-funds" class="metric-value text-green">Rs 0.00</div>
        </div>
    </div>

    <div class="section-title" style="color: #00ff00;">ACTIVE OPEN POSITIONS</div>
    <div id="active-positions-panel" class="signal-card" style="border-color: #333333;">
        <div id="active-positions-content" style="color: #888888; text-align: center; padding: 10px; font-weight: bold;">
            NO ACTIVE POSITIONS
        </div>
    </div>

    <div class="section-title">SENSIBULL CROSS-VERIFICATION OPTION CHAIN (ATM ± 2 STRIKES)</div>
    <table>
        <thead>
            <tr>
                <th style="color: #00ff00;">CALL LTP</th>
                <th style="color: #00ff00;">CALL OI</th>
                <th style="color: #00ff00;">CALL IV</th>
                <th style="color: #00ff00;">CALL Γ</th>
                <th class="col-strike">STRIKE</th>
                <th style="color: #ff0000;">PUT Γ</th>
                <th style="color: #ff0000;">PUT IV</th>
                <th style="color: #ff0000;">PUT OI</th>
                <th style="color: #ff0000;">PUT LTP</th>
            </tr>
        </thead>
        <tbody id="option-chain-body">
            <tr><td colspan="9" style="text-align: center; color: #666666;">Waiting for live WebSocket option chain data...</td></tr>
        </tbody>
    </table>

    <div class="section-title">REAL-TIME BOT TELEMETRY LOGS</div>
    <div id="terminal-logs" class="terminal-log-box">Loading live telemetry logs...</div>

    <div class="section-title" style="margin-top: 12px;">COMPLETED TRADES LEDGER</div>
    <table class="history-table">
        <thead>
            <tr>
                <th>TIMESTAMP</th>
                <th>SYMBOL</th>
                <th>ENTRY PX</th>
                <th>EXIT PX</th>
                <th>PNL (RS)</th>
                <th>REASON</th>
                <th>RESULT</th>
            </tr>
        </thead>
        <tbody id="history-body">
            <tr><td colspan="7" style="text-align: center; color: #666666;">No trade history logged today.</td></tr>
        </tbody>
    </table>
</div>

<script>
    function updateClock() {
        var now = new Date();
        var hrs = String(now.getHours()).padStart(2, '0');
        var mins = String(now.getMinutes()).padStart(2, '0');
        var secs = String(now.getSeconds()).padStart(2, '0');
        document.getElementById('clock').innerText = hrs + ':' + mins + ':' + secs;
    }
    setInterval(updateClock, 1000);
    updateClock();

    async function fetchLiveState() {
        try {
            var resp = await fetch('/api/live_state');
            var data = await resp.json();

            document.getElementById('spot-price').innerText = data.spot_price > 0 ? data.spot_price.toFixed(2) : '--.--';
            
            var gexVal = data.gex_net || 0;
            var gexElem = document.getElementById('gex-net');
            gexElem.innerText = (gexVal < 0 ? '-' : '+') + 'Rs ' + Math.abs(gexVal).toLocaleString('en-IN', {maximumFractionDigits: 0});
            gexElem.className = 'metric-value ' + (gexVal < 0 ? 'text-cyan' : 'text-amber');

            var vElem = document.getElementById('velocity');
            vElem.innerText = (data.velocity >= 0 ? '+' : '') + Math.round(data.velocity).toLocaleString();
            vElem.className = 'metric-value ' + (data.velocity > 0 ? 'text-green' : (data.velocity < 0 ? 'text-red' : 'text-amber'));

            var aElem = document.getElementById('acceleration');
            aElem.innerText = (data.acceleration >= 0 ? '+' : '') + Math.round(data.acceleration).toLocaleString();
            aElem.className = 'metric-value ' + (data.acceleration > 0 ? 'text-green' : (data.acceleration < 0 ? 'text-red' : 'text-amber'));

            document.getElementById('avail-funds').innerText = 'Rs ' + (data.available_funds || 0).toLocaleString('en-IN', {maximumFractionDigits: 2});

            var badge = document.getElementById('signal-badge');
            var status = data.signal_status || '🟡 NEUTRAL (CHOPPY / WAIT)';
            badge.innerText = status;
            
            if (status.indexOf('STRONG BULLISH') !== -1 || status.indexOf('CALL READY') !== -1) {
                badge.className = 'signal-badge signal-bullish';
            } else if (status.indexOf('STRONG BEARISH') !== -1 || status.indexOf('PUT READY') !== -1) {
                badge.className = 'signal-badge signal-bearish';
            } else if (status.indexOf('ROLLOVER') !== -1 || status.indexOf('PAUSE') !== -1) {
                badge.className = 'signal-badge signal-pause';
            } else {
                badge.className = 'signal-badge signal-neutral';
            }

            document.getElementById('signal-reason').innerText = data.reasoning || 'Evaluating tick kinematics...';

            var actPanel = document.getElementById('active-positions-panel');
            var actContent = document.getElementById('active-positions-content');
            if (data.active_trade && data.active_trade.legs && data.active_trade.legs.length > 0) {
                actPanel.style.borderColor = '#00ff00';
                var legHtml = '<table><thead><tr><th style="color:#00ff00; text-align:left;">STRATEGY</th><th style="color:#00ff00; text-align:center;">SIGNAL</th><th style="color:#00ff00; text-align:left;">OPTION SYMBOL</th><th style="color:#00ff00;">QTY</th><th style="color:#00ff00;">ENTRY PREMIUM</th><th style="color:#00ff00;">LIVE LTP</th><th style="color:#00ff00;">UNREALIZED PNL</th></tr></thead><tbody>';
                data.active_trade.legs.forEach(function(leg) {
                    var ltp = leg.current_price || leg.entry_price;
                    var pnl = (ltp - leg.entry_price) * leg.quantity;
                    var pnlClass = pnl >= 0 ? 'text-green' : 'text-red';
                    legHtml += '<tr><td style="color:#00ffff; text-align:left;">' + (data.active_trade.strategy_name || 'KDO') + '</td><td style="color:' + (data.active_trade.signal_type === 'CALL' ? '#00ff00' : '#ff0000') + '; font-weight:bold; text-align:center;">' + data.active_trade.signal_type + '</td><td style="color:#ffffff; font-weight:bold; text-align:left;">' + leg.symbol + '</td><td>' + leg.quantity + '</td><td>Rs ' + leg.entry_price.toFixed(2) + '</td><td>Rs ' + ltp.toFixed(2) + '</td><td class="' + pnlClass + '" style="font-weight:bold;">Rs ' + pnl.toFixed(2) + '</td></tr>';
                });
                legHtml += '</tbody></table>';
                actContent.innerHTML = legHtml;
            } else {
                actPanel.style.borderColor = '#333333';
                actContent.innerHTML = '<div style="color: #888888; font-weight: bold; text-align: center;">NO ACTIVE POSITIONS</div>';
            }

            var chainBody = document.getElementById('option-chain-body');
            if (data.option_chain && data.option_chain.length > 0) {
                var html = '';
                data.option_chain.forEach(function(row) {
                    var ceOiStr = (row.ce_oi && row.ce_oi > 0) ? Number(row.ce_oi).toLocaleString('en-IN') : '--';
                    var peOiStr = (row.pe_oi && row.pe_oi > 0) ? Number(row.pe_oi).toLocaleString('en-IN') : '--';
                    html += '<tr><td style="color:#00ff00;">' + (row.ce_ltp ? row.ce_ltp.toFixed(2) : '--') + '</td><td>' + ceOiStr + '</td><td>' + (row.ce_iv ? (row.ce_iv * 100).toFixed(1) + '%' : '--') + '</td><td>' + (row.ce_gamma ? row.ce_gamma.toFixed(5) : '--') + '</td><td class="col-strike">' + row.strike + '</td><td>' + (row.pe_gamma ? row.pe_gamma.toFixed(5) : '--') + '</td><td>' + (row.pe_iv ? (row.pe_iv * 100).toFixed(1) + '%' : '--') + '</td><td>' + peOiStr + '</td><td style="color:#ff0000;">' + (row.pe_ltp ? row.pe_ltp.toFixed(2) : '--') + '</td></tr>';
                });
                chainBody.innerHTML = html;
            }

            if (data.recent_logs && data.recent_logs.length > 0) {
                var logBox = document.getElementById('terminal-logs');
                logBox.innerHTML = data.recent_logs.join('<br>');
                logBox.scrollTop = logBox.scrollHeight;
            }

            if (data.history && data.history.length > 0) {
                var hHtml = '';
                data.history.forEach(function(t) {
                    var netPl = (t.net_pl !== undefined) ? t.net_pl : (t.total_pnl || 0);
                    var isWin = netPl >= 0;
                    var exitReason = t.reason || t.exit_reason || 'Flow Decay';
                    hHtml += '<tr><td>' + (t.timestamp || t.date || '--') + '</td><td>' + (t.symbol || t.trade_id || 'NIFTY') + '</td><td>' + (t.entry_price ? t.entry_price.toFixed(2) : '--') + '</td><td>' + (t.exit_price ? t.exit_price.toFixed(2) : '--') + '</td><td class="' + (isWin ? 'text-green' : 'text-red') + '">Rs ' + netPl.toFixed(2) + '</td><td>' + exitReason + '</td><td class="' + (isWin ? 'win-tag' : 'loss-tag') + '">' + (isWin ? 'WIN' : 'LOSS') + '</td></tr>';
                });
                document.getElementById('history-body').innerHTML = hHtml;
            }

        } catch (err) {
            console.error('Polling error:', err);
        }
    }

    function triggerKillSwitch() {
        if (confirm("EMERGENCY KILL SWITCH: Are you sure you want to HALT trading and CLOSE any active position?")) {
            fetch('/api/kill_switch', {method: 'POST'})
                .then(r => r.json())
                .then(d => { alert(d.message); fetchLiveState(); });
        }
    }

    function resetKillSwitch() {
        if (confirm("RESET KILL SWITCH: Are you sure you want to resume normal trading operations?")) {
            fetch('/api/reset_kill_switch', {method: 'POST'})
                .then(r => r.json())
                .then(d => { alert(d.message); fetchLiveState(); });
        }
    }

    setInterval(fetchLiveState, 2000);
    fetchLiveState();
</script>

</body>
</html>
"""


KILL_SWITCH_FILE = "/dev/shm/KILL_SWITCH_ACTIVE" if os.path.exists("/dev/shm") else os.path.join(DATA_DIR, "KILL_SWITCH_ACTIVE")


@app.route("/")
def index():
    return BLOOMBERG_HTML, 200, {'Content-Type': 'text/html; charset=utf-8'}


@app.route("/api/live_state")
def api_live_state():
    """Fast 0ms RAM disk JSON endpoint."""
    state_copy = get_kdo_live_state()
    cap = get_capital_state()
    state_copy["available_funds"] = cap.get("available_funds", 0.0)
    state_copy["active_trade"] = get_active_trade()
    state_copy["history"] = get_trade_history()
    state_copy["recent_logs"] = get_recent_logs(40)
    state_copy["kill_switch_active"] = os.path.exists(KILL_SWITCH_FILE)
    return jsonify(state_copy)


@app.route("/api/kill_switch", methods=["POST"])
def api_kill_switch():
    """Emergency Mobile Kill Switch Route."""
    try:
        with open(KILL_SWITCH_FILE, "w") as f:
            f.write(datetime.now().isoformat())
            
        update_kdo_live_state({
            "signal_status": "🛑 EMERGENCY KILL SWITCH ACTIVATED",
            "reasoning": "Mobile Kill Switch triggered via Web Dashboard. Trading halted."
        })
        return jsonify({"status": "ok", "message": "Emergency Mobile Kill Switch Activated! Trading Halted."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/reset_kill_switch", methods=["POST"])
def api_reset_kill_switch():
    """Resumes normal trading operations."""
    try:
        if os.path.exists(KILL_SWITCH_FILE):
            os.remove(KILL_SWITCH_FILE)
        update_kdo_live_state({
            "signal_status": "🟡 NEUTRAL (CHOPPY / WAIT)",
            "reasoning": "Kill switch reset. Resuming live microstructure evaluation."
        })
        return jsonify({"status": "ok", "message": "Kill Switch Reset. Normal trading resumed."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
