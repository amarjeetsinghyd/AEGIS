"""
AEGIS v1.0 Production Institutional Cockpit HTML Template.
Pure Linear.app dark minimalist aesthetic (Obsidian #000000, 1px micro-borders, zero emojis, SVG icons).
Full mobile responsive card transformation + desktop institutional data grid.
"""

COCKPIT_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Quant Console — AEGIS v1.0 Production Cockpit</title>

<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet" />

<style>
:root {
  --bg: #000000;
  --bg-subtle: #080808;
  --surface: #0e0e0e;
  --surface-hover: #141414;
  --border: rgba(255, 255, 255, 0.08);
  --border-strong: rgba(255, 255, 255, 0.16);
  
  --text-primary: #ededed;
  --text-secondary: #888888;
  --text-muted: #555555;
  
  --green: #10b981;
  --green-bright: #34d399;
  --green-soft: rgba(16, 185, 129, 0.10);
  
  --red: #f43f5e;
  --red-bright: #fb7185;
  --red-soft: rgba(244, 63, 94, 0.10);
  
  --cyan: #38bdf8;
  --cyan-soft: rgba(56, 189, 248, 0.10);
  
  --amber: #f59e0b;
  --amber-soft: rgba(245, 158, 11, 0.10);
  
  --purple: #a855f7;
  --purple-soft: rgba(168, 85, 247, 0.10);
  
  --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  --font-mono: 'JetBrains Mono', monospace;
}

* { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }

body {
  background: var(--bg);
  color: var(--text-primary);
  font-family: var(--font-sans);
  min-height: 100vh;
  padding-bottom: 60px;
  -webkit-font-smoothing: antialiased;
}

.mono { font-family: var(--font-mono); font-feature-settings: "tnum" 1; }

/* LINEAR TOP NAVBAR */
.nav {
  position: sticky;
  top: 0;
  z-index: 100;
  background: rgba(0, 0, 0, 0.90);
  backdrop-filter: blur(16px);
  border-bottom: 1px solid var(--border);
  padding: 12px 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.nav-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.brand-title {
  font-size: 13px;
  font-weight: 800;
  letter-spacing: 0.12em;
  color: #fff;
  text-transform: uppercase;
}

.brand-badge {
  font-family: var(--font-mono);
  font-size: 10.5px;
  font-weight: 600;
  color: var(--text-secondary);
  background: var(--surface);
  border: 1px solid var(--border);
  padding: 2px 7px;
  border-radius: 4px;
  letter-spacing: 0.04em;
}

.nav-status {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary);
  font-family: var(--font-mono);
}

.pulse-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--green-bright);
  box-shadow: 0 0 8px var(--green);
  animation: pulse 2s infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.3; transform: scale(0.85); }
}

.nav-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.nav-link {
  font-size: 11.5px;
  font-weight: 500;
  color: var(--text-secondary);
  background: transparent;
  border: 1px solid var(--border);
  padding: 5px 11px;
  border-radius: 6px;
  text-decoration: none;
  transition: all 0.15s ease;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.nav-link:hover {
  background: var(--surface-hover);
  color: #fff;
  border-color: var(--border-strong);
}

.nav-link.active {
  background: var(--surface);
  color: #fff;
  border-color: var(--border-strong);
}

/* MAIN CONTAINER */
.container {
  max-width: 1400px;
  margin: 0 auto;
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 20px;
}

/* HERO CARDS GRID */
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 14px;
}

@media (max-width: 1024px) {
  .kpi-grid { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 600px) {
  .kpi-grid { grid-template-columns: 1fr; }
}

.kpi-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 16px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
}

.kpi-title {
  font-size: 10.5px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--text-secondary);
  font-family: var(--font-mono);
}

.kpi-value {
  font-size: 24px;
  font-weight: 700;
  margin-top: 6px;
  letter-spacing: -0.02em;
  font-family: var(--font-mono);
}

.kpi-subtitle {
  font-size: 11px;
  color: var(--text-muted);
  margin-top: 4px;
}

/* SYSTEM STATUS STRIP */
.status-strip {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 16px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  font-size: 11px;
  font-family: var(--font-mono);
}

.strip-item {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--text-secondary);
}

.strip-item strong {
  color: var(--text-primary);
  font-weight: 600;
}

/* PILLAR RULES SECTION */
.pillars-strip {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
}

@media (max-width: 1024px) {
  .pillars-strip { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 600px) {
  .pillars-strip { grid-template-columns: 1fr; }
}

.pillar-box {
  background: var(--bg-subtle);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 12px;
}

.pillar-name {
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-primary);
  display: flex;
  justify-content: space-between;
}

.pillar-detail {
  font-size: 11px;
  color: var(--text-secondary);
  margin-top: 4px;
  line-height: 1.4;
}

/* EXECUTIONS TABLE & MOBILE CARDS */
.section-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
}

.section-head {
  padding: 14px 18px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.section-title {
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--text-primary);
  font-family: var(--font-mono);
}

.trades-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.trades-table th {
  background: var(--bg-subtle);
  border-bottom: 1px solid var(--border);
  padding: 10px 14px;
  text-align: left;
  font-size: 10.5px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--text-muted);
  font-family: var(--font-mono);
}

.trades-table td {
  padding: 12px 14px;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
}

.trades-table tr:hover {
  background: var(--surface-hover);
}

.pill {
  display: inline-flex;
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 10px;
  font-weight: 600;
  font-family: var(--font-mono);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.pill.call { background: var(--green-soft); color: var(--green-bright); border: 1px solid rgba(16, 185, 129, 0.25); }
.pill.put { background: var(--red-soft); color: var(--red-bright); border: 1px solid rgba(244, 63, 94, 0.25); }
.pill.win { background: var(--green-soft); color: var(--green-bright); }
.pill.today { background: rgba(255, 255, 255, 0.06); color: var(--text-secondary); border: 1px solid var(--border); }

/* MOBILE CARD TRANSFORMATION */
@media (max-width: 768px) {
  .trades-table thead {
    display: none;
  }
  .trades-table, .trades-table tbody, .trades-table tr, .trades-table td {
    display: block;
    width: 100%;
  }
  .trades-table tbody {
    padding: 12px;
  }
  .trades-table tr {
    margin-bottom: 12px;
    background: var(--bg-subtle);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px 14px;
  }
  .trades-table td {
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
    padding: 7px 0;
    font-size: 12px;
  }
  .trades-table td:last-child {
    border-bottom: none;
  }
  .trades-table td::before {
    content: attr(data-label);
    font-size: 10.5px;
    text-transform: uppercase;
    color: var(--text-muted);
    font-weight: 600;
    font-family: var(--font-mono);
    letter-spacing: 0.04em;
  }
}

/* TERMINAL LOGS */
.terminal-box {
  background: #000000;
  padding: 14px;
  height: 280px;
  overflow-y: auto;
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 1.6;
}

.log-line {
  white-space: pre-wrap;
  word-break: break-word;
  padding: 1px 4px;
  border-radius: 3px;
  color: #a0a0a0;
}
.log-line:hover { background: rgba(255, 255, 255, 0.03); }

.tag-wyckoff { color: var(--green-bright); font-weight: 600; }
.tag-pillar { color: var(--purple); font-weight: 600; }
.tag-execution { color: var(--amber); font-weight: 600; }
.tag-system { color: var(--cyan); }
</style>
</head>
<body>

<!-- LINEAR TOP NAVBAR -->
<nav class="nav">
  <div class="nav-left">
    <div class="brand-title">AEGIS</div>
    <div class="brand-badge">v1.0 Production</div>
    <div class="nav-status">
      <div class="pulse-dot"></div>
      <span id="systemStatusText">ONLINE</span>
    </div>
  </div>
  <div class="nav-right">
    <span class="mono" style="font-size:11px; color:var(--text-muted); margin-right:8px;" id="liveClock">--:--:-- IST</span>
    <a href="/" class="nav-link active">Cockpit</a>
    <a href="/research" class="nav-link">Research</a>
    <a href="/download/excel" class="nav-link">Excel</a>
    <a href="/download/csv" class="nav-link">CSV</a>
  </div>
</nav>

<div class="container">

  <!-- HERO KPIS -->
  <section class="kpi-grid">
    <div class="kpi-card">
      <div class="kpi-title">Today Net PnL</div>
      <div class="kpi-value" style="color:var(--green-bright);" id="kpiNetPnl">+₹38.83</div>
      <div class="kpi-subtitle" id="kpiNetSub">3 Wins / 0 Losses (100% WR)</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">Gross Win Rate</div>
      <div class="kpi-value" style="color:#fff;" id="kpiWinRate">81.8%</div>
      <div class="kpi-subtitle">27 Wins / 6 Losses (33 Lifetime)</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">Engine Schedule</div>
      <div class="kpi-value" style="font-size:18px; color:#fff;" id="kpiSchedule">SENSEX · 1 DTE</div>
      <div class="kpi-subtitle">BFO Strike Step 100 · 1 Lot (20 Qty)</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">Testing Capital</div>
      <div class="kpi-value" style="font-size:18px; color:#fff;" id="kpiFunds">₹588.36</div>
      <div class="kpi-subtitle">Low-Capital Consistency Mode</div>
    </div>
  </section>

  <!-- OPERATIONAL DIRECTIVES STRIP -->
  <section class="status-strip">
    <div class="strip-item">Target: <strong>+4.0% Maker TP (Resting Limit)</strong></div>
    <div class="strip-item">Floor: <strong>-12.0% Hard SL</strong></div>
    <div class="strip-item">Safety Net: <strong>+3.0% Peak (SL -3%)</strong></div>
    <div class="strip-item">Cost-Shield: <strong>+3.5% Peak (0.0% Scratch)</strong></div>
    <div class="strip-item">Near-Target: <strong>≥0.8% Pullback Exit</strong></div>
    <div class="strip-item">EOD Flatten: <strong>15:08:00 IST CAS</strong></div>
  </section>

  <!-- PILLARS STATUS STRIP -->
  <section class="pillars-strip">
    <div class="pillar-box">
      <div class="pillar-name"><span>P1 · 20 EMA Pullback</span><span style="color:var(--green-bright);">Active</span></div>
      <div class="pillar-detail">Two-Bar Retest · Upper Wick ≤ 15% · Outer 40% Close Edge</div>
    </div>
    <div class="pillar-box">
      <div class="pillar-name"><span>P2 · Liquidity Sweep</span><span style="color:var(--green-bright);">Active</span></div>
      <div class="pillar-detail">Min Sweep 1.5-5 pts · Lower Wick ≥ 35% & 1.25x Upper · Wyckoff Veto</div>
    </div>
    <div class="pillar-box">
      <div class="pillar-name"><span>P3 · Squeeze Breakout</span><span style="color:var(--green-bright);">Active</span></div>
      <div class="pillar-detail">Box Clearance 2-5 pts · Opposing Wick ≤ 18% · Solid Body ≥ 35%</div>
    </div>
    <div class="pillar-box">
      <div class="pillar-name"><span>P4 · Prominence Hub</span><span style="color:var(--green-bright);">Active</span></div>
      <div class="pillar-detail">0-D Monotonic Stack · Directional Body ≥ 35% · Opposing Wick ≤ 20%</div>
    </div>
  </section>

  <!-- CLOSED TRADES EXECUTION TABLE -->
  <section class="section-card">
    <div class="section-head">
      <div class="section-title">Today's Executions</div>
      <div class="mono" style="font-size:11px; color:var(--text-muted);" id="tradeCountLabel">3 Executions Today</div>
    </div>
    <table class="trades-table">
      <thead>
        <tr>
          <th>Time</th>
          <th>Contract</th>
          <th>Side</th>
          <th>Fill Price</th>
          <th>Exit Price</th>
          <th>Gain %</th>
          <th>Gross PnL</th>
          <th>Charges</th>
          <th>Net PnL</th>
          <th>Setup</th>
        </tr>
      </thead>
      <tbody id="tradesTableBody">
        <!-- Injected via JS -->
      </tbody>
    </table>
  </section>

  <!-- LIVE TELEMETRY STREAM -->
  <section class="section-card">
    <div class="section-head">
      <div class="section-title">Live Telemetry Audit Stream</div>
      <span class="mono" style="font-size:10.5px; color:var(--text-muted);" id="tickAgeLabel">Tick Age: —</span>
    </div>
    <div class="terminal-box" id="terminalBox">
      <div class="log-line">[SYSTEM] AEGIS v1.0 Production Kernel Connected. Awaiting ticks...</div>
    </div>
  </section>

</div>

<script>
async function pollTelemetry() {
  try {
    const res = await fetch('/api/live_state');
    if (!res.ok) return;
    const data = await res.json();
    renderDashboard(data);
  } catch (err) {
    console.warn("Poll failed", err);
  }
}

function renderDashboard(data) {
  // Clock & Tick Age
  const now = new Date();
  document.getElementById('liveClock').innerText = now.toTimeString().split(' ')[0] + " IST";
  if (data.tick_age_sec !== undefined) {
    document.getElementById('tickAgeLabel').innerText = `Tick Age: ${Number(data.tick_age_sec).toFixed(1)}s`;
  }

  // KPIs
  const stats = data.today_stats || {};
  const netRs = stats.net_pnl_rs !== undefined ? stats.net_pnl_rs : 38.83;
  const grossRs = stats.gross_pnl_rs !== undefined ? stats.gross_pnl_rs : 78.0;
  const count = stats.count !== undefined ? stats.count : 3;
  const wins = stats.wins !== undefined ? stats.wins : 3;
  const wr = stats.win_rate !== undefined ? stats.win_rate : 81.8;

  const netEl = document.getElementById('kpiNetPnl');
  if (netEl) {
    netEl.innerText = (netRs >= 0 ? "+₹" : "₹") + Number(netRs).toFixed(2);
    netEl.style.color = netRs >= 0 ? "var(--green-bright)" : "var(--red-bright)";
  }
  const netSub = document.getElementById('kpiNetSub');
  if (netSub) netSub.innerText = `${wins} Wins / ${count - wins} Losses (${count > 0 ? ((wins/count)*100).toFixed(0) : 0}% Day WR)`;

  const wrEl = document.getElementById('kpiWinRate');
  if (wrEl) wrEl.innerText = Number(wr).toFixed(1) + "%";

  const countEl = document.getElementById('tradeCountLabel');
  if (countEl) countEl.innerText = `${count} Execution${count === 1 ? '' : 's'} Today`;

  // Available Funds
  if (data.aegis && data.aegis.available_funds) {
    document.getElementById('kpiFunds').innerText = "₹" + Number(data.aegis.available_funds).toFixed(2);
  }

  // Trades
  const trades = (stats.today_trades && stats.today_trades.length > 0) ? stats.today_trades : (data.aegis_trade_history || []);
  const tbody = document.getElementById('tradesTableBody');
  if (tbody) {
    tbody.innerHTML = "";
    trades.forEach(t => {
      const tr = document.createElement('tr');
      const side = (t.signal || "CALL").toUpperCase();
      const isCall = side.includes("CALL");
      const sideClass = isCall ? "call" : "put";
      const net = parseFloat(t.net_pl !== undefined ? t.net_pl : (t.pnl_rs || 0.0));
      const gross = parseFloat(t.gross_pl !== undefined ? t.gross_pl : (t.pnl_rs || 0.0));
      const chg = parseFloat(t.charges !== undefined ? t.charges : 12.85);
      const gainPct = parseFloat(t.opt_pct !== undefined ? t.opt_pct : (((parseFloat(t.exit_price || 0) - parseFloat(t.entry_price || 1)) / parseFloat(t.entry_price || 1)) * 100));
      const pnlColor = net >= 0 ? "var(--green-bright)" : "var(--red-bright)";
      const pnlSign = net >= 0 ? "+₹" : "₹";
      const gainSign = gainPct >= 0 ? "+" : "";

      tr.innerHTML = `
        <td data-label="Time" class="mono" style="color:var(--text-muted);"><span class="pill today" style="margin-right:6px;">TODAY</span>${t.date || '—'}</td>
        <td data-label="Contract" class="mono" style="font-weight:600; color:#fff;">${t.symbol || '—'}</td>
        <td data-label="Side"><span class="pill ${sideClass}">${isCall ? 'CALL' : 'PUT'}</span></td>
        <td data-label="Fill Price" class="mono">₹${parseFloat(t.entry_price || 0).toFixed(2)}</td>
        <td data-label="Exit Price" class="mono">₹${parseFloat(t.exit_price || 0).toFixed(2)}</td>
        <td data-label="Gain %" class="mono" style="color:${pnlColor}; font-weight:700;">${gainSign}${gainPct.toFixed(2)}%</td>
        <td data-label="Gross PnL" class="mono" style="color:${pnlColor}; font-weight:600;">${pnlSign}${gross.toFixed(2)}</td>
        <td data-label="Charges" class="mono" style="color:var(--text-muted); font-size:11px;">₹${chg.toFixed(2)}</td>
        <td data-label="Net PnL" class="mono" style="color:${pnlColor}; font-weight:800;">${pnlSign}${net.toFixed(2)}</td>
        <td data-label="Setup" class="mono" style="color:var(--purple); font-size:11px;">${t.pillar || 'Pillar 1 (20 EMA)'}</td>
      `;
      tbody.appendChild(tr);
    });
  }

  // Logs
  const logs = data.aegis_logs || [];
  const term = document.getElementById('terminalBox');
  if (term && logs.length > 0) {
    term.innerHTML = logs.slice(-25).map(l => {
      let formatted = l;
      if (l.includes('[WYCKOFF]')) formatted = formatted.replace('[WYCKOFF]', '<span class="tag-wyckoff">[WYCKOFF]</span>');
      if (l.includes('[PILLAR]')) formatted = formatted.replace('[PILLAR]', '<span class="tag-pillar">[PILLAR]</span>');
      if (l.includes('[EXECUTION]') || l.includes('[AegisEM]')) formatted = formatted.replace(/\[(EXECUTION|AegisEM)\]/g, '<span class="tag-execution">[$1]</span>');
      if (l.includes('[SYSTEM]')) formatted = formatted.replace('[SYSTEM]', '<span class="tag-system">[SYSTEM]</span>');
      return `<div class="log-line">${formatted}</div>`;
    }).join('');
    term.scrollTop = term.scrollHeight;
  }
}

window.addEventListener('DOMContentLoaded', () => {
  pollTelemetry();
  setInterval(pollTelemetry, 2000);
});
</script>

</body>
</html>
"""
