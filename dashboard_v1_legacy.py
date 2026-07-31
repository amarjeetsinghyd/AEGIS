import os
import json
import datetime
from flask import Flask, render_template_string, request, jsonify, redirect, url_for
from astra_bot.config.settings import DATA_DIR, LOGS_DIR, BROKER, ENABLE_LIVE_BROKERAGE_EXECUTION

app = Flask(__name__)

CAPITAL_STATE_FILE = os.path.join(DATA_DIR, "capital_state.json")
ACTIVE_CONTEXT_FILE = os.path.join(DATA_DIR, "active_trade_context.json")
LOG_FILE = os.path.join(LOGS_DIR, "astra_bot.log")

HISTORY_FILE = os.path.join(DATA_DIR, "trades", "trade_history.json")

def get_capital_state():
    state = {"available_funds": 0.0, "locked_withdrawal_amount": 0.0}
    paths = [
        CAPITAL_STATE_FILE,
        os.path.join(DATA_DIR, "capital_state.json"),
        os.path.join(os.path.dirname(DATA_DIR), "data", "capital_state.json"),
        os.path.join(os.path.dirname(os.path.dirname(DATA_DIR)), "data", "capital_state.json"),
        "/home/opc/astra_bot/data/capital_state.json"
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
        try:
            from astra_bot.core.shoonya_adapter import ShoonyaAdapter, ShoonyaPortfolioProvider
            adapter = ShoonyaAdapter()
            provider = ShoonyaPortfolioProvider(adapter._api)
            live_cash = provider.get_available_funds()
            if live_cash > 0:
                state["available_funds"] = live_cash
                save_capital_state(state)
        except Exception as err:
            logger.warning(f"Failed to fetch live Shoonya capital for dashboard: {err}")
            
    return state

def save_capital_state(state):
    try:
        with open(CAPITAL_STATE_FILE, "w") as f:
            json.dump(state, f, indent=4)
    except Exception:
        pass

def get_active_trade():
    paths = [
        ACTIVE_CONTEXT_FILE,
        os.path.join(DATA_DIR, "trades", "active_trade_context.json"),
        os.path.join(os.path.dirname(DATA_DIR), "data", "trades", "active_trade_context.json"),
        os.path.join(os.path.dirname(os.path.dirname(DATA_DIR)), "data", "trades", "active_trade_context.json"),
        "/home/opc/astra_bot/data/trades/active_trade_context.json"
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
        os.path.join(os.path.dirname(os.path.dirname(DATA_DIR)), "data", "trades", "trade_history.json"),
        os.path.join(os.path.dirname(DATA_DIR), "data", "trades", "trade_history.json"),
        HISTORY_FILE,
        "/home/opc/astra_bot/data/trades/trade_history.json"
    ]
    for p in paths:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    history = json.load(f)
                    if history:
                        return history[::-1]  # Return newest first
            except Exception:
                pass
    return []

def colorize_log(line: str) -> str:
    """Format log line with HTML spans for rich color rendering on Web Dashboard."""
    import html
    escaped = html.escape(line)
    
    if "SIGNAL GENERATED: Buy CALL" in escaped or "CALL setup" in escaped or "EXECUTED" in escaped:
        return f'<span style="color: #2ecc71; font-weight: 600;">{escaped}</span>'
    elif "SIGNAL GENERATED: Buy PUT" in escaped or "PUT setup" in escaped:
        return f'<span style="color: #e74c3c; font-weight: 600;">{escaped}</span>'
    elif "Signal is NEUTRAL" in escaped:
        return f'<span style="color: #f39c12; font-weight: 500;">{escaped}</span>'
    elif "ERROR" in escaped or "Exception" in escaped or "Aborted" in escaped:
        return f'<span style="color: #e74c3c; font-weight: 600;">{escaped}</span>'
    elif "WARNING" in escaped:
        return f'<span style="color: #f39c12;">{escaped}</span>'
    elif "[SYNTHETIC VOLUME]" in escaped:
        return f'<span style="color: #00bcd4; font-weight: 500;">{escaped}</span>'
    elif "[NET OPTION DELTA OI]" in escaped:
        return f'<span style="color: #9b59b6; font-weight: 600;">{escaped}</span>'
    elif "[FALSE BREAKOUT FILTER]" in escaped:
        return f'<span style="color: #e67e22; font-weight: 600;">{escaped}</span>'
    elif "[REST API CANDLE]" in escaped or "[SLIDING WINDOW]" in escaped:
        return f'<span style="color: #3498db;">{escaped}</span>'
    else:
        return f'<span style="color: #8b9bb4;">{escaped}</span>'

def get_recent_logs(num_lines=50):
    if os.path.exists(LOG_FILE):
        try:
            import re
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
                clean_lines = [re.sub(r'\x1b\[[0-9;]*m|\033\[[0-9;]*m', '', line) for line in lines[-num_lines:]]
                return [colorize_log(line) for line in clean_lines]
        except Exception:
            pass
    return ["<span style='color: #8b9bb4;'>No logs available.</span>"]

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Kronos Trading Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #0b0c10;
            --card-bg: #1f2833;
            --text-color: #c5c6c7;
            --text-title: #ffffff;
            --primary: #45f3ff;
            --primary-hover: #00bcd4;
            --success: #2ecc71;
            --danger: #e74c3c;
            --warning: #f39c12;
            --border-color: #2c3540;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            font-family: 'Inter', sans-serif;
            background-color: var(--bg-color);
            color: var(--text-color);
            padding: 0.85rem;
            line-height: 1.5;
        }

        @media (min-width: 768px) {
            body {
                padding: 2.5rem;
            }
        }

        .container {
            max-width: 1200px;
            margin: 0 auto;
        }

        header {
            display: flex;
            flex-direction: column;
            align-items: flex-start;
            gap: 0.75rem;
            margin-bottom: 1.5rem;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 1rem;
        }

        @media (min-width: 600px) {
            header {
                flex-direction: row;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 2.5rem;
                padding-bottom: 1.5rem;
            }
        }

        h1 {
            font-size: 1.4rem;
            font-weight: 700;
            color: var(--text-title);
            background: linear-gradient(to right, #45f3ff, #0077ff);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        @media (min-width: 768px) {
            h1 { font-size: 2rem; }
        }

        .status-badge {
            padding: 0.35rem 0.85rem;
            border-radius: 2rem;
            font-size: 0.8rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        .status-online {
            background-color: rgba(46, 204, 113, 0.15);
            color: var(--success);
            border: 1px solid var(--success);
        }

        .grid {
            display: grid;
            grid-template-columns: 1fr;
            gap: 1rem;
            margin-bottom: 1.5rem;
        }

        @media (min-width: 768px) {
            .grid {
                grid-template-columns: repeat(2, 1fr);
                gap: 1.5rem;
                margin-bottom: 2.5rem;
            }
            .grid-span-2 {
                grid-column: span 2;
            }
        }

        .card {
            background-color: var(--card-bg);
            border-radius: 0.75rem;
            padding: 1.25rem;
            border: 1px solid var(--border-color);
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.5);
            overflow: hidden;
        }

        @media (min-width: 768px) {
            .card { padding: 1.75rem; }
        }

        h2 {
            font-size: 1.1rem;
            font-weight: 600;
            color: var(--text-title);
            margin-bottom: 1rem;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }

        .metric-group {
            display: flex;
            flex-direction: column;
            gap: 0.85rem;
        }

        .metric-summary-bar {
            display: flex;
            flex-wrap: wrap;
            gap: 0.75rem;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1rem;
            padding-bottom: 1rem;
            border-bottom: 1px solid var(--border-color);
        }

        .metric-item {
            display: flex;
            flex-direction: column;
            gap: 0.25rem;
        }

        .metric {
            display: flex;
            justify-content: space-between;
            align-items: center;
            width: 100%;
        }

        .metric-label {
            font-size: 0.85rem;
            color: #8b9bb4;
        }

        .metric-value {
            font-size: 1rem;
            font-weight: 600;
            color: var(--text-title);
        }

        .highlight-value {
            color: var(--primary);
        }

        .form-group {
            margin-top: 1rem;
            padding-top: 1rem;
            border-top: 1px solid var(--border-color);
        }

        label {
            display: block;
            font-size: 0.85rem;
            color: #8b9bb4;
            margin-bottom: 0.5rem;
        }

        .input-group {
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
        }

        @media (min-width: 480px) {
            .input-group {
                flex-direction: row;
            }
        }

        input[type="number"] {
            background-color: #0b0c10;
            border: 1px solid var(--border-color);
            color: var(--text-title);
            padding: 0.65rem 0.85rem;
            border-radius: 0.4rem;
            font-size: 16px;
            width: 100%;
            outline: none;
        }

        input[type="number"]:focus {
            border-color: var(--primary);
        }

        button {
            background-color: var(--primary);
            color: #0b0c10;
            border: none;
            padding: 0.65rem 1.25rem;
            border-radius: 0.4rem;
            font-weight: 600;
            font-size: 0.95rem;
            cursor: pointer;
            transition: background-color 0.2s;
            white-space: nowrap;
            width: 100%;
        }

        @media (min-width: 480px) {
            button { width: auto; }
        }

        button:hover {
            background-color: var(--primary-hover);
        }

        .log-container {
            background-color: #090a0f;
            border-radius: 0.4rem;
            padding: 0.85rem;
            font-family: 'Courier New', Courier, monospace;
            font-size: 0.8rem;
            height: 250px;
            overflow-y: auto;
            white-space: pre-wrap;
            color: #45f3ff;
            border: 1px solid var(--border-color);
            -webkit-overflow-scrolling: touch;
        }

        .table-wrapper {
            width: 100%;
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
            margin-top: 1rem;
            border-radius: 0.5rem;
            border: 1px solid var(--border-color);
        }

        .position-table {
            width: 100%;
            min-width: 650px;
            border-collapse: collapse;
        }

        .position-table th, .position-table td {
            text-align: left;
            padding: 0.75rem 0.6rem;
            border-bottom: 1px solid var(--border-color);
            font-size: 0.85rem;
            white-space: nowrap;
        }

        .position-table th {
            color: #8b9bb4;
            text-transform: uppercase;
            font-size: 0.75rem;
            background-color: rgba(0,0,0,0.2);
        }

        .profit-green {
            color: var(--success) !important;
            font-weight: 600;
        }

        .loss-red {
            color: var(--danger) !important;
            font-weight: 600;
        }

        .result-badge {
            padding: 0.25rem 0.5rem;
            border-radius: 0.25rem;
            font-size: 0.75rem;
            font-weight: 700;
            display: inline-block;
        }
        .badge-win { background-color: rgba(46, 204, 113, 0.2); color: var(--success); }
        .badge-loss { background-color: rgba(231, 76, 60, 0.2); color: var(--danger); }
        .badge-minloss { background-color: rgba(243, 156, 18, 0.2); color: var(--warning); }

        .toast {
            position: fixed;
            bottom: 1.5rem;
            right: 1.5rem;
            left: 1.5rem;
            max-width: 400px;
            margin: 0 auto;
            background-color: var(--success);
            color: #000;
            font-weight: 600;
            padding: 0.85rem 1.5rem;
            border-radius: 0.5rem;
            display: none;
            z-index: 1000;
            text-align: center;
        }

        @media (min-width: 600px) {
            .toast {
                left: auto;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div>
                <h1>Kronos Options Trading Dashboard</h1>
                <p style="font-size: 0.85rem; color: #8b9bb4; margin-top: 0.25rem">Real-time status monitoring & capital manager</p>
            </div>
            <span class="status-badge status-online">Active</span>
        </header>

        <div class="grid">
            <div class="card">
                <h2>System Parameters</h2>
                <div class="metric-group">
                    <div class="metric">
                        <span class="metric-label">Active Gateway</span>
                        <span class="metric-value highlight-value">{{ broker }}</span>
                    </div>
                    <div class="metric">
                        <span class="metric-label">Execution Mode</span>
                        <span class="metric-value">{{ "LIVE TRADING" if live_trading else "PAPER TRADING" }}</span>
                    </div>
                    <div class="metric">
                        <span class="metric-label">Target Asset</span>
                        <span class="metric-value">{{ active_asset }}</span>
                    </div>
                </div>
            </div>

            <div class="card">
                <h2>Capital Engine</h2>
                <div class="metric-group">
                    <div class="metric">
                        <span class="metric-label">Total Capital</span>
                        <span class="metric-value">Rs {{ "%.2f"|format(capital.available_funds) }}</span>
                    </div>
                    <div class="metric">
                        <span class="metric-label">Locked Capital</span>
                        <span class="metric-value" style="color: var(--danger)">Rs {{ "%.2f"|format(capital.locked_withdrawal_amount) }}</span>
                    </div>
                    <div class="metric">
                        <span class="metric-label">Tradable Margin</span>
                        <span class="metric-value" style="color: var(--success)">Rs {{ "%.2f"|format(tradable_funds) }}</span>
                    </div>
                </div>
                <form action="{{ url_for('update_locked_capital') }}" method="POST">
                    <div class="form-group">
                        <label for="locked_amount">Lock Funds from Trading (Rs)</label>
                        <div class="input-group">
                            <input type="number" step="0.01" name="locked_amount" id="locked_amount" value="{{ capital.locked_withdrawal_amount }}" required>
                            <button type="submit">Update Lock</button>
                        </div>
                    </div>
                </form>
            </div>

            <div class="card grid-span-2">
                <h2>Live Positions</h2>
                {% if active_trade %}
                <div class="metric-group">
                    <div class="metric">
                        <span class="metric-label">Active Strategy</span>
                        <span class="metric-value highlight-value">{{ active_trade.strategy }} ({{ active_trade.signal_type }})</span>
                    </div>
                    <div class="metric">
                        <span class="metric-label">Entry Timestamp</span>
                        <span class="metric-value">{{ active_trade.entry_time }}</span>
                    </div>
                </div>
                <div class="table-wrapper">
                    <table class="position-table">
                        <thead>
                            <tr>
                                <th>Option Symbol</th>
                                <th>Status</th>
                                <th>Entry Px</th>
                                <th>Current Px</th>
                                <th>Highest Px</th>
                                <th>PnL (Rs)</th>
                                <th>Return %</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for leg in active_trade.legs %}
                            <tr>
                                <td>{{ leg.symbol }}</td>
                                <td><span style="color: {% if leg.status == 'OPEN' %}var(--success){% else %}#8b9bb4{% endif %}; font-weight: 500;">{{ leg.status }}</span></td>
                                <td>Rs {{ "%.2f"|format(leg.entry_price) }}</td>
                                <td>Rs {{ "%.2f"|format(leg.current_price) }}</td>
                                <td>Rs {{ "%.2f"|format(leg.highest_price) }}</td>
                                <td class="{% if leg.pnl >= 0 %}profit-green{% else %}loss-red{% endif %}">Rs {{ "%.2f"|format(leg.pnl) }}</td>
                                <td class="{% if leg.pnl >= 0 %}profit-green{% else %}loss-red{% endif %}">{% if leg.entry_price > 0 %}{{ "%+.2f"|format(((leg.current_price - leg.entry_price) / leg.entry_price) * 100) }}%{% else %}0.00%{% endif %}</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
                {% else %}
                <p style="color: #8b9bb4; font-size: 0.95rem; margin-top: 0.5rem">No active positions open at present.</p>
                {% endif %}
            </div>

            <div class="card grid-span-2">
                <h2>Trade Performance Ledger (Historical Records)</h2>
                {% if history %}
                <div class="metric-summary-bar">
                    <div class="metric-item">
                        <span class="metric-label">Total Trades</span>
                        <span class="metric-value">{{ total_trades }}</span>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Win Rate</span>
                        <span class="metric-value highlight-value">{{ "%.1f"|format(win_rate) }}%</span>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Realized PnL</span>
                        <span class="metric-value {% if total_pnl >= 0 %}profit-green{% else %}loss-red{% endif %}">Rs {{ "%.2f"|format(total_pnl) }}</span>
                    </div>
                </div>
                <div class="table-wrapper">
                    <table class="position-table">
                        <thead>
                            <tr>
                                <th># ID</th>
                                <th>Entry Time</th>
                                <th>Exit Time</th>
                                <th>Option Symbol</th>
                                <th>Capital Used</th>
                                <th>Exit Reason</th>
                                <th>Net PnL</th>
                                <th>Return %</th>
                                <th>Result</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for trade in history %}
                            <tr>
                                <td>#{{ trade.id }}</td>
                                <td>{{ trade.get("entry_time", trade.date) }}</td>
                                <td>{{ trade.get("exit_time", trade.date) }}</td>
                                <td><strong>{{ trade.symbol }}</strong></td>
                                <td>Rs {{ "%.2f"|format(trade.get("capital_used", 0.0)) }}</td>
                                <td style="font-size: 0.85rem; color: #8b9bb4;">{{ trade.reason }}</td>
                                <td class="{% if trade.net_pl >= 0 %}profit-green{% else %}loss-red{% endif %}">Rs {{ "%.2f"|format(trade.net_pl) }}</td>
                                <td class="{% if trade.net_pl >= 0 %}profit-green{% else %}loss-red{% endif %}">{% if trade.get("capital_used", 0.0) > 0 %}{{ "%+.2f"|format((trade.net_pl / trade.get("capital_used", 1.0)) * 100) }}%{% else %}0.00%{% endif %}</td>
                                <td>
                                    <span class="result-badge {% if trade.result == 'WIN' %}badge-win{% elif trade.result == 'LOSS' %}badge-loss{% else %}badge-minloss{% endif %}">
                                        {{ trade.result }}
                                    </span>
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
                {% else %}
                <p style="color: #8b9bb4; font-size: 0.95rem; margin-top: 0.5rem">No historical trades recorded yet.</p>
                {% endif %}
            </div>
        </div>

        <div class="card" style="margin-bottom: 2rem">
            <h2>Real-time Bot Logs</h2>
            <div class="log-container" id="log-box">{% for log in logs %}{{ log|safe }}{% endfor %}</div>
        </div>
    </div>

    <div class="toast" id="toast">Capital Lock configured successfully!</div>

    <script>
        const logBox = document.getElementById('log-box');
        logBox.scrollTop = logBox.scrollHeight;

        setInterval(() => {
            fetch('/api/state')
                .then(res => res.json())
                .then(data => {
                    // Surgically update spot price without full page reload
                    const spotEl = document.getElementById('spot-price-display');
                    if (spotEl && data.index_ltp && data.index_ltp > 0) {
                        spotEl.textContent = data.index_ltp.toFixed(2);
                    }
                    // Full reload only for capital / trade state changes
                    const prevFunds = parseFloat(spotEl ? spotEl.dataset.prevFunds || 0 : 0);
                    const newFunds = data.tradable_funds || 0;
                    if (Math.abs(newFunds - prevFunds) > 1 || data.active_trade !== null) {
                        window.location.reload();
                    }
                    if (spotEl) spotEl.dataset.prevFunds = newFunds;
                })
                .catch(() => {}); // Silently ignore if bot is offline
        }, 3000);

        const urlParams = new URLSearchParams(window.location.search);
        if (urlParams.get('success') === 'true') {
            const toast = document.getElementById('toast');
            toast.style.display = 'block';
            setTimeout(() => { toast.style.display = 'none'; }, 3000);
        }
    </script>
</body>
</html>
"""

@app.route("/")
def index():
    capital = get_capital_state()
    active_trade = get_active_trade()
    history = get_trade_history()
    logs = get_recent_logs()
    
    # Calculate tradable funds
    available = float(capital.get("available_funds", 0.0))
    locked = float(capital.get("locked_withdrawal_amount", 0.0))
    tradable_funds = max(0.0, available - locked)
    
    # Calculate Trade History metrics
    total_trades = len(history)
    wins = sum(1 for t in history if t.get("result") == "WIN")
    win_rate = (wins / total_trades * 100.0) if total_trades > 0 else 0.0
    total_pnl = sum(t.get("net_pl", 0.0) for t in history)
    
    # Determine active index asset
    day = datetime.datetime.now().weekday()
    active_asset = "SENSEX" if day in [2, 3] else "NIFTY"
    
    live_state = get_live_state()
    index_ltp = live_state.get("index_ltp", 0.0)

    return render_template_string(
        DASHBOARD_HTML,
        broker=BROKER.upper(),
        live_trading=ENABLE_LIVE_BROKERAGE_EXECUTION,
        active_asset=active_asset,
        capital=capital,
        tradable_funds=tradable_funds,
        active_trade=active_trade,
        history=history,
        total_trades=total_trades,
        win_rate=win_rate,
        total_pnl=total_pnl,
        logs=logs,
        index_ltp=index_ltp
    )

@app.route("/update_locked_capital", methods=["POST"])
def update_locked_capital():
    try:
        locked_amount = float(request.form.get("locked_amount", 0.0))
        state = get_capital_state()
        state["locked_withdrawal_amount"] = locked_amount
        save_capital_state(state)
    except Exception:
        pass
    return redirect(url_for("index", success="true"))

@app.route("/api/state")
def api_state():
    capital = get_capital_state()
    active_trade = get_active_trade()
    history = get_trade_history()
    available = float(capital.get("available_funds", 0.0))
    locked = float(capital.get("locked_withdrawal_amount", 0.0))
    tradable_funds = max(0.0, available - locked)
    
    return jsonify({
        "capital": capital,
        "active_trade": active_trade,
        "history_count": len(history),
        "tradable_funds": tradable_funds
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
