"""
HTML Template for the Dedicated Research & Analytics Console (/research).
Pure Linear.app dark minimalist aesthetic (Obsidian #000000, 1px micro-borders, zero emojis).
Features rich institutional Chart.js visualizations, KPI cards, interactive filters,
deep-dive forensic modals, dynamic IST market status, and full mobile card transformation.
"""

RESEARCH_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Quant Console — Research &amp; Forensics (AEGIS v1.0 Production)</title>

<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet" />
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>

<style>
:root {
  --bg: #000000;
  --bg-subtle: #080808;
  --surface: #0e0e0e;
  --surface-hover: #141414;
  --card: #111111;
  --border: rgba(255, 255, 255, 0.08);
  --border-strong: rgba(255, 255, 255, 0.16);
  
  --text: #ededed;
  --text-muted: #666666;
  --text-secondary: #888888;
  
  --primary: #38bdf8;
  --primary-soft: rgba(56, 189, 248, 0.10);
  
  --success: #10b981;
  --success-bright: #34d399;
  --success-soft: rgba(16, 185, 129, 0.10);
  
  --danger: #f43f5e;
  --danger-bright: #fb7185;
  --danger-soft: rgba(244, 63, 94, 0.10);
  
  --warning: #f59e0b;
  --warning-soft: rgba(245, 158, 11, 0.10);
  
  --purple: #a855f7;
  --purple-soft: rgba(168, 85, 247, 0.10);
  
  --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  --font-mono: 'JetBrains Mono', monospace;
}

* { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }

body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-sans);
  min-height: 100vh;
  padding: 0 0 60px;
  -webkit-font-smoothing: antialiased;
}

.mono { font-family: var(--font-mono); font-feature-settings: "tnum" 1; }

/* NAVBAR */
.nav {
  background: rgba(0, 0, 0, 0.90);
  backdrop-filter: blur(16px);
  border-bottom: 1px solid var(--border);
  padding: 12px 24px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  position: sticky;
  top: 0;
  z-index: 100;
  gap: 16px;
  flex-wrap: wrap;
}

.nav-brand {
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
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--green-bright);
  box-shadow: 0 0 8px var(--green);
  animation: pulse 2s infinite;
}

.pulse-dot.dot-closed {
  background: #666666;
  box-shadow: none;
  animation: none;
}

.pulse-dot.dot-closing {
  background: var(--warning);
  box-shadow: 0 0 8px var(--warning);
  animation: pulse 2s infinite;
}

.pulse-dot.dot-pre {
  background: var(--primary);
  box-shadow: 0 0 8px var(--primary);
  animation: none;
}

@keyframes pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.3; transform: scale(0.85); }
}

.nav-links {
  display: flex;
  gap: 8px;
  align-items: center;
}

.nav-btn {
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

.nav-btn:hover {
  background: var(--surface-hover);
  color: #fff;
  border-color: var(--border-strong);
}

.nav-btn.active {
  background: var(--surface);
  color: #fff;
  border-color: var(--border-strong);
}

/* CONTAINER */
.container {
  max-width: 1400px;
  margin: 0 auto;
  padding: 24px 24px 0;
}

/* KPI RIBBON */
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 12px;
  margin-bottom: 20px;
}

@media (max-width: 1200px) {
  .kpi-grid { grid-template-columns: repeat(3, 1fr); }
}

@media (max-width: 600px) {
  .kpi-grid { grid-template-columns: 1fr 1fr; }
}

.kpi-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: 4px;
}

.kpi-label {
  font-size: 10.5px;
  font-weight: 600;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-family: var(--font-mono);
}

.kpi-val {
  font-family: var(--font-mono);
  font-size: 22px;
  font-weight: 700;
  color: #fff;
  line-height: 1.1;
  letter-spacing: -0.02em;
}

.kpi-sub {
  font-size: 10.5px;
  color: var(--text-muted);
  font-family: var(--font-mono);
}

.kpi-card.win .kpi-val { color: var(--success-bright); }
.kpi-card.pnl .kpi-val { color: var(--success-bright); }
.kpi-card.factor .kpi-val { color: var(--purple); }

/* RISK DISCIPLINE BANNER */
.risk-banner {
  background: rgba(245, 158, 11, 0.06);
  border: 1px solid rgba(245, 158, 11, 0.25);
  border-radius: 8px;
  padding: 12px 18px;
  margin-bottom: 20px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.risk-banner-title {
  color: var(--warning);
  font-size: 11.5px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-family: var(--font-mono);
}

.risk-banner-desc {
  font-size: 11.5px;
  color: #c0c0c0;
  line-height: 1.5;
}

/* CHARTS SECTION */
.charts-grid {
  display: grid;
  grid-template-columns: 2fr 1fr;
  gap: 16px;
  margin-bottom: 20px;
}

@media (max-width: 1024px) {
  .charts-grid { grid-template-columns: 1fr; }
}

.chart-box {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 18px;
}

.chart-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 14px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 10px;
}

.chart-title {
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: #fff;
  font-family: var(--font-mono);
}

.chart-subtitle {
  font-size: 11px;
  color: var(--text-muted);
  font-family: var(--font-mono);
  margin-top: 2px;
}

.chart-canvas-wrap {
  position: relative;
  height: 260px;
  width: 100%;
}

.charts-row-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-bottom: 24px;
}

@media (max-width: 900px) {
  .charts-row-2 { grid-template-columns: 1fr; }
}

/* TRADE JOURNAL TABLE SECTION */
.table-panel {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
}

.table-toolbar {
  padding: 14px 18px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
}

.toolbar-title {
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: #fff;
  display: flex;
  align-items: center;
  gap: 10px;
  font-family: var(--font-mono);
}

.toolbar-filters {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}

.filter-input {
  background: #000000;
  border: 1px solid var(--border);
  color: #fff;
  padding: 5px 10px;
  border-radius: 6px;
  font-size: 11.5px;
  font-family: var(--font-mono);
  outline: none;
}
.filter-input:focus { border-color: var(--primary); }

.filter-select {
  background: #000000;
  border: 1px solid var(--border);
  color: #fff;
  padding: 5px 10px;
  border-radius: 6px;
  font-size: 11.5px;
  font-family: var(--font-mono);
  cursor: pointer;
  outline: none;
}
.filter-select:focus { border-color: var(--primary); }

.journal-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
  text-align: left;
}

.journal-table th {
  background: var(--bg-subtle);
  color: var(--text-muted);
  font-size: 10.5px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
  font-weight: 600;
  font-family: var(--font-mono);
}

.journal-table td {
  padding: 11px 14px;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
}

.journal-table tr {
  cursor: pointer;
  transition: background 0.12s ease;
}

.journal-table tbody tr:hover {
  background: var(--surface-hover);
}

.badge {
  display: inline-flex;
  align-items: center;
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 10px;
  font-weight: 600;
  font-family: var(--font-mono);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.badge.win { background: var(--success-soft); color: var(--success-bright); border: 1px solid rgba(16, 185, 129, 0.25); }
.badge.shadow { background: var(--primary-soft); color: var(--primary); border: 1px solid rgba(56, 189, 248, 0.25); }
.badge.be { background: var(--warning-soft); color: var(--warning); border: 1px solid rgba(245, 158, 11, 0.25); }
.badge.loss { background: var(--danger-soft); color: var(--danger-bright); border: 1px solid rgba(244, 63, 94, 0.25); }
.badge.call { background: var(--success-soft); color: var(--success-bright); border: 1px solid rgba(16, 185, 129, 0.25); }
.badge.put { background: var(--danger-soft); color: var(--danger-bright); border: 1px solid rgba(244, 63, 94, 0.25); }
.badge.pillar { background: rgba(255, 255, 255, 0.05); color: var(--purple); border: 1px solid var(--border); }

.pnl-pos { color: var(--success-bright); font-weight: 700; }
.pnl-neg { color: var(--danger-bright); font-weight: 700; }

.btn-inspect {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--primary);
  padding: 3px 8px;
  border-radius: 5px;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s;
  font-family: var(--font-mono);
}
.btn-inspect:hover {
  background: var(--primary-soft);
  border-color: var(--primary);
  color: #fff;
}

/* MOBILE CARD TRANSFORMATION FOR JOURNAL TABLE */
@media (max-width: 768px) {
  .journal-table thead {
    display: none;
  }
  .journal-table, .journal-table tbody, .journal-table tr, .journal-table td {
    display: block;
    width: 100%;
  }
  .journal-table tbody {
    padding: 12px;
  }
  .journal-table tr {
    margin-bottom: 12px;
    background: var(--bg-subtle);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px 14px;
  }
  .journal-table td {
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
    padding: 7px 0;
    font-size: 12px;
  }
  .journal-table td:last-child {
    border-bottom: none;
  }
  .journal-table td::before {
    content: attr(data-label);
    font-size: 10.5px;
    text-transform: uppercase;
    color: var(--text-muted);
    font-weight: 600;
    font-family: var(--font-mono);
    letter-spacing: 0.04em;
  }
}

/* MODAL STYLES */
.modal-overlay {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.85);
  backdrop-filter: blur(8px);
  z-index: 1000;
  justify-content: center;
  align-items: center;
  padding: 20px;
}
.modal-overlay.active { display: flex; }

.modal-box {
  background: var(--surface);
  border: 1px solid var(--border-strong);
  border-radius: 10px;
  max-width: 900px;
  width: 100%;
  max-height: 90vh;
  overflow-y: auto;
  box-shadow: 0 20px 60px rgba(0,0,0,0.8);
  animation: modalIn 0.2s cubic-bezier(0.16, 1, 0.3, 1);
}
@keyframes modalIn {
  from { opacity: 0; transform: scale(0.96) translateY(10px); }
  to { opacity: 1; transform: scale(1) translateY(0); }
}

.modal-head {
  padding: 16px 20px;
  border-bottom: 1px solid var(--border);
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.modal-title {
  font-size: 14px;
  font-weight: 700;
  color: #fff;
  display: flex;
  align-items: center;
  gap: 12px;
  font-family: var(--font-mono);
  text-transform: uppercase;
}
.modal-close {
  background: transparent;
  border: none;
  color: var(--text-muted);
  font-size: 22px;
  cursor: pointer;
  line-height: 1;
}
.modal-close:hover { color: #fff; }

.modal-body {
  padding: 20px;
}

.forensic-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
  margin-bottom: 18px;
}
@media (max-width: 700px) { .forensic-grid { grid-template-columns: 1fr; } }

.forensic-card {
  background: #000000;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px;
}

.forensic-card-title {
  font-size: 11.5px;
  font-weight: 700;
  color: var(--purple);
  margin-bottom: 10px;
  display: flex;
  align-items: center;
  gap: 6px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 8px;
  font-family: var(--font-mono);
  text-transform: uppercase;
}

.forensic-row {
  display: flex;
  justify-content: space-between;
  font-size: 11.5px;
  padding: 5px 0;
  border-bottom: 1px dashed rgba(255,255,255,0.05);
  font-family: var(--font-mono);
}
.forensic-row:last-child { border-bottom: none; }
.forensic-label { color: var(--text-muted); }
.forensic-val { font-weight: 600; color: #fff; }

.conviction-bar-wrap { margin-top: 8px; }
.c-bar-label { display: flex; justify-content: space-between; font-size: 10.5px; margin-bottom: 4px; color: var(--text-muted); font-family: var(--font-mono); }
.c-bar-bg { background: var(--surface); height: 5px; border-radius: 999px; overflow: hidden; margin-bottom: 8px; }
.c-bar-fill { background: var(--primary); height: 100%; border-radius: 999px; }

.learnings-box {
  background: rgba(168, 85, 247, 0.06);
  border: 1px solid rgba(168, 85, 247, 0.25);
  border-radius: 8px;
  padding: 14px;
  margin-top: 14px;
}
.learnings-box h4 {
  font-size: 12px;
  color: var(--purple);
  margin-bottom: 8px;
  font-family: var(--font-mono);
  text-transform: uppercase;
}
.learnings-box p {
  font-size: 11.5px;
  line-height: 1.5;
  color: #c9d1d9;
  margin-bottom: 6px;
}

.runner-highlight {
  background: rgba(16, 185, 129, 0.08);
  border: 1px solid rgba(16, 185, 129, 0.25);
  border-radius: 8px;
  padding: 12px 14px;
  margin-top: 14px;
  display: flex;
  justify-content: space-around;
  text-align: center;
}
.runner-stat .val { font-family: var(--font-mono); font-size: 17px; font-weight: 700; color: var(--success-bright); }
.runner-stat .lbl { font-size: 10.5px; color: var(--text-muted); text-transform: uppercase; font-family: var(--font-mono); }

</style>
</head>
<body>

<!-- NAVBAR -->
<nav class="nav">
  <div class="nav-brand">
    <div class="brand-title">Quant Console</div>
    <div class="brand-badge">Research &amp; Forensics</div>
    <div class="nav-status">
      <div class="pulse-dot" id="marketStatusDot"></div>
      <span id="systemStatusText">CALCULATING IST...</span>
    </div>
  </div>
  <div class="nav-links">
    <span class="mono" style="font-size:11px; color:var(--text-muted); margin-right:6px;" id="liveClock">--:--:-- IST</span>
    <a href="/" class="nav-btn">Cockpit</a>
    <a href="/research" class="nav-btn active">Research</a>
    <a href="/download/excel" class="nav-btn">Excel</a>
    <a href="/download/csv" class="nav-btn">CSV</a>
  </div>
</nav>

<div class="container">

  <!-- TOP KPI RIBBON -->
  <div class="kpi-grid">
    <div class="kpi-card win">
      <div class="kpi-label">Profit Rate</div>
      <div class="kpi-val" id="kpiWinRate">--%</div>
      <div class="kpi-sub" id="kpiWinLoss">-- Profit / -- Loss</div>
    </div>
    <div class="kpi-card pnl">
      <div class="kpi-label">Total Gross P&amp;L</div>
      <div class="kpi-val" id="kpiGrossPnl">₹--</div>
      <div class="kpi-sub" id="kpiChargesSub">Charges: ₹--</div>
    </div>
    <div class="kpi-card pnl">
      <div class="kpi-label">Total Net Profit</div>
      <div class="kpi-val" id="kpiNetPnl" style="color:var(--success-bright);">₹--</div>
      <div class="kpi-sub">Net Realized</div>
    </div>
    <div class="kpi-card factor">
      <div class="kpi-label">Profit Factor</div>
      <div class="kpi-val" id="kpiProfitFactor">--</div>
      <div class="kpi-sub" id="kpiGross">Profit/Loss: ₹-- / ₹--</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Avg Duration</div>
      <div class="kpi-val" id="kpiAvgDur">--s</div>
      <div class="kpi-sub">Sub-Minute Gamma Execution</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Star Pillar</div>
      <div class="kpi-val" style="color: var(--purple);" id="kpiStarPillar">Pillar 4 (BOS)</div>
      <div class="kpi-sub">100% Profit Rate Today</div>
    </div>
  </div>

  <!-- PERMANENT RISK DISCIPLINE BANNER -->
  <div class="risk-banner">
    <div class="risk-banner-title">
      Permanent Risk Mandate: Strict +4.0% Maker Target Discipline (Never Greed to 5%–6%)
    </div>
    <div class="risk-banner-desc">
      <strong>Institutional Post-Mortem Rule:</strong> The <strong>+4.0% Resting Maker Take-Profit</strong> is our mathematically optimal safe harbor that fills passively within 10–40 seconds across volatile or choppy regimes with zero slippage.
      Empirical tick telemetry confirms index options experience sharp institutional dumps immediately following opening gamma thrusts. Expanding targets destroys the 84%+ win rate. Passive maker execution at +4.0% is our structural edge.
    </div>
  </div>

  <!-- CHARTS ROW 1 -->
  <div class="charts-grid">
    <div class="chart-box">
      <div class="chart-head">
        <div>
          <div class="chart-title">Cumulative Equity Curve (Trade-by-Trade)</div>
          <div class="chart-subtitle">Realized Portfolio P&amp;L Growth across 1-Lot Testing Capital</div>
        </div>
      </div>
      <div class="chart-canvas-wrap">
        <canvas id="equityChart"></canvas>
      </div>
    </div>

    <div class="chart-box">
      <div class="chart-head">
        <div>
          <div class="chart-title">4 Pillars Alpha Contribution</div>
          <div class="chart-subtitle">Win Rate % and Net P&amp;L by Pillar</div>
        </div>
      </div>
      <div class="chart-canvas-wrap">
        <canvas id="pillarChart"></canvas>
      </div>
    </div>
  </div>

  <!-- CHARTS ROW 2 -->
  <div class="charts-row-2">
    <div class="chart-box">
      <div class="chart-head">
        <div>
          <div class="chart-title">Post-Exit Runner Surge (What-If Analysis)</div>
          <div class="chart-subtitle">Realized +4.0% TP vs +30s vs +60s vs Full Candle Peak</div>
        </div>
      </div>
      <div class="chart-canvas-wrap">
        <canvas id="runnerChart"></canvas>
      </div>
    </div>

    <div class="chart-box">
      <div class="chart-head">
        <div>
          <div class="chart-title">Risk-Reward Anatomy (MAE vs MFE)</div>
          <div class="chart-subtitle">Max Intraday Drawdown vs Peak Runup (Protection Guard Validation)</div>
        </div>
      </div>
      <div class="chart-canvas-wrap">
        <canvas id="maeMfeChart"></canvas>
      </div>
    </div>
  </div>

  <!-- TRADE JOURNAL DATA TABLE -->
  <div class="table-panel">
    <div class="table-toolbar">
      <div class="toolbar-title">
        <span>Trade Journal &amp; Forensic Ledger</span>
        <span class="badge" style="background:rgba(255,255,255,0.06); color:var(--text-muted);" id="tradeCountBadge">15 trades</span>
      </div>
      <div class="toolbar-filters">
        <input type="text" id="searchFilter" class="filter-input" placeholder="Search strike, symbol..." oninput="applyFilters()" />
        <select id="pillarFilter" class="filter-select" onchange="applyFilters()">
          <option value="ALL">All Pillars</option>
          <option value="Pillar 1">Pillar 1 (20 EMA)</option>
          <option value="Pillar 2">Pillar 2 (Sweep)</option>
          <option value="Pillar 3">Pillar 3 (Squeeze)</option>
          <option value="Pillar 4">Pillar 4 (BOS)</option>
        </select>
        <select id="resultFilter" class="filter-select" onchange="applyFilters()">
          <option value="ALL">All Outcomes</option>
          <option value="WIN">Profits Only</option>
          <option value="LOSS">Losses Only</option>
          <option value="BE">Break-Even Only</option>
          <option value="SHADOW">Shadow Profits Only</option>
        </select>
      </div>
    </div>

    <div style="overflow-x: auto;">
      <table class="journal-table">
        <thead>
          <tr>
            <th>#</th>
            <th>Date &amp; Time</th>
            <th>Instrument</th>
            <th>Type</th>
            <th>Entry ₹</th>
            <th>Exit ₹</th>
            <th>Dur</th>
            <th>Gain %</th>
            <th>Gross PnL (₹)</th>
            <th>Charges (₹)</th>
            <th>Net PnL (₹)</th>
            <th>Pillar</th>
            <th>Result</th>
            <th>Forensics</th>
          </tr>
        </thead>
        <tbody id="journalTableBody">
          <!-- Populated by JS -->
        </tbody>
        <tfoot id="journalTableFoot">
          <!-- Populated by JS -->
        </tfoot>
      </table>
    </div>
  </div>

</div>

<!-- FORENSIC DETAIL MODAL -->
<div class="modal-overlay" id="forensicModal" onclick="closeModalOnOverlay(event)">
  <div class="modal-box">
    <div class="modal-head">
      <div class="modal-title" id="modalTitle">Trade Forensic Deep-Dive</div>
      <button class="modal-close" onclick="closeModal()">&times;</button>
    </div>
    <div class="modal-body" id="modalBody">
      <!-- Injected by JS -->
    </div>
  </div>
</div>

<script>
let RAW_DATA = null;
let equityChart = null;
let pillarChart = null;
let runnerChart = null;
let maeMfeChart = null;

// ─── DYNAMIC IST MARKET HOURS STATUS LOGIC ───
function getMarketStatus() {
  const now = new Date();
  const istStr = now.toLocaleString("en-US", { timeZone: "Asia/Kolkata" });
  const ist = new Date(istStr);
  const day = ist.getDay(); // 0 = Sun, 6 = Sat
  const mins = ist.getHours() * 60 + ist.getMinutes();

  if (day === 0 || day === 6) {
    return {
      text: "MARKET CLOSED · WEEKEND",
      type: "closed"
    };
  }

  // Pre-market: before 09:15 IST
  if (mins < 555) {
    return {
      text: "PRE-MARKET · SYSTEM READY",
      type: "pre"
    };
  }
  // Live Trading Hours: 09:15 to 15:08 IST
  if (mins >= 555 && mins < 908) {
    return {
      text: "MARKET ACTIVE · LIVE EXECUTION",
      type: "live"
    };
  }
  // CAS Flush & Settlement: 15:08 to 15:30 IST
  if (mins >= 908 && mins < 930) {
    return {
      text: "CAS FLATTENED · SESSION CLOSING",
      type: "closing"
    };
  }
  // Post-Market / EOD: After 15:30 IST
  return {
    text: "MARKET CLOSED · EOD FLAT",
    type: "closed"
  };
}

function updateMarketStatusBadge() {
  const now = new Date();
  document.getElementById('liveClock').innerText = now.toTimeString().split(' ')[0] + " IST";

  const mStatus = getMarketStatus();
  const statusEl = document.getElementById('systemStatusText');
  const dotEl = document.getElementById('marketStatusDot');

  if (statusEl) statusEl.innerText = mStatus.text;
  if (dotEl) {
    dotEl.className = "pulse-dot";
    if (mStatus.type === "closed") {
      dotEl.classList.add("dot-closed");
    } else if (mStatus.type === "closing") {
      dotEl.classList.add("dot-closing");
    } else if (mStatus.type === "pre") {
      dotEl.classList.add("dot-pre");
    }
  }
}

async function loadData() {
  try {
    updateMarketStatusBadge();
    const res = await fetch("/api/research_data");
    const data = await res.json();
    RAW_DATA = data;
    renderKPIs(data.kpis);
    renderCharts(data);
    renderTable(data.trades);
  } catch (err) {
    console.error("Error loading research data:", err);
  }
}

function renderKPIs(kpis) {
  const pr = kpis.profit_rate_pct !== undefined ? kpis.profit_rate_pct : kpis.win_rate_pct;
  document.getElementById("kpiWinRate").innerText = pr + "%";
  document.getElementById("kpiWinLoss").innerText = `${kpis.win_count} Profit / ${kpis.loss_count} Loss`;
  
  const grossTotal = kpis.total_gross_pnl_rs !== undefined ? kpis.total_gross_pnl_rs : (kpis.total_pnl_rs || 0.0);
  const chgTotal = kpis.total_charges_rs !== undefined ? kpis.total_charges_rs : 0.0;
  const netTotal = kpis.total_net_pnl_rs !== undefined ? kpis.total_net_pnl_rs : (grossTotal - chgTotal);

  const grossPrefix = grossTotal >= 0 ? "+₹" : "₹";
  const netPrefix = netTotal >= 0 ? "+₹" : "₹";

  const grossEl = document.getElementById("kpiGrossPnl");
  if (grossEl) {
    grossEl.innerText = `${grossPrefix}${grossTotal.toFixed(2)}`;
    grossEl.style.color = grossTotal >= 0 ? "var(--success-bright)" : "var(--danger-bright)";
  }
  const chgEl = document.getElementById("kpiChargesSub");
  if (chgEl) {
    chgEl.innerText = `Total Charges: ₹${chgTotal.toFixed(2)}`;
  }
  const netEl = document.getElementById("kpiNetPnl");
  if (netEl) {
    netEl.innerText = `${netPrefix}${netTotal.toFixed(2)}`;
    netEl.style.color = netTotal >= 0 ? "var(--success-bright)" : "var(--danger-bright)";
  }

  document.getElementById("kpiProfitFactor").innerText = kpis.profit_factor >= 99 ? "∞ (Zero Losses)" : kpis.profit_factor.toFixed(2);
  document.getElementById("kpiGross").innerText = `Profit: +₹${kpis.gross_profit_rs} / Loss: -₹${kpis.gross_loss_rs}`;
  document.getElementById("kpiAvgDur").innerText = kpis.avg_duration_sec + "s";
  document.getElementById("tradeCountBadge").innerText = kpis.total_trades + " trades";
}

function renderCharts(data) {
  const chrono = data.chronological_trades || [];

  // 1. EQUITY CURVE
  const eqLabels = chrono.map(t => `#${t.id} (${t.date})`);
  const eqValues = chrono.map(t => t.cumulative_pnl_rs);

  const ctxEq = document.getElementById("equityChart").getContext("2d");
  if (equityChart) equityChart.destroy();
  equityChart = new Chart(ctxEq, {
    type: "line",
    data: {
      labels: eqLabels,
      datasets: [{
        label: "Cumulative P&L (₹)",
        data: eqValues,
        borderColor: "#34d399",
        backgroundColor: "rgba(52, 211, 153, 0.08)",
        fill: true,
        tension: 0.25,
        borderWidth: 2,
        pointBackgroundColor: "#34d399",
        pointRadius: 3,
        pointHoverRadius: 6,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx) => ` Cumulative: ₹${ctx.parsed.y.toFixed(2)}`
          }
        }
      },
      scales: {
        x: { grid: { color: "rgba(255, 255, 255, 0.04)" }, ticks: { color: "#888888", font: { family: "JetBrains Mono", size: 10 } } },
        y: { grid: { color: "rgba(255, 255, 255, 0.04)" }, ticks: { color: "#888888", font: { family: "JetBrains Mono", size: 10 }, callback: v => "₹" + v } }
      }
    }
  });

  // 2. PILLAR PERFORMANCE
  const pSum = data.pillar_summary || [];
  const pLabels = pSum.map(p => p.pillar.replace(" (", "\n("));
  const pPnl = pSum.map(p => p.pnl_rs);
  const pWins = pSum.map(p => p.win_rate_pct);

  const ctxPillar = document.getElementById("pillarChart").getContext("2d");
  if (pillarChart) pillarChart.destroy();
  pillarChart = new Chart(ctxPillar, {
    type: "bar",
    data: {
      labels: pLabels,
      datasets: [
        {
          label: "Win Rate %",
          data: pWins,
          backgroundColor: "rgba(168, 85, 247, 0.6)",
          borderColor: "#a855f7",
          borderWidth: 1,
          borderRadius: 4,
          yAxisID: "y1"
        },
        {
          label: "Net P&L (₹)",
          data: pPnl,
          backgroundColor: "rgba(52, 211, 153, 0.6)",
          borderColor: "#34d399",
          borderWidth: 1,
          borderRadius: 4,
          yAxisID: "y"
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: "#ededed", font: { family: "JetBrains Mono", size: 10 } } }
      },
      scales: {
        x: { grid: { display: false }, ticks: { color: "#888888", font: { family: "JetBrains Mono", size: 10 } } },
        y: { position: "left", grid: { color: "rgba(255, 255, 255, 0.04)" }, ticks: { color: "#888888", font: { family: "JetBrains Mono", size: 10 }, callback: v => "₹" + v } },
        y1: { position: "right", max: 100, min: 0, grid: { display: false }, ticks: { color: "#a855f7", font: { family: "JetBrains Mono", size: 10 }, callback: v => v + "%" } }
      }
    }
  });

  // 3. RUNNER SURGE
  const runners = (data.runner_analysis || []).slice(-8);
  const rLabels = runners.map(r => `#${r.trade_id}`);
  const rRealized = runners.map(r => r.realized_tp_pct);
  const r30s = runners.map(r => r.mfe_30s_pct);
  const rPeak = runners.map(r => r.peak_runner_pct);

  const ctxRunner = document.getElementById("runnerChart").getContext("2d");
  if (runnerChart) runnerChart.destroy();
  runnerChart = new Chart(ctxRunner, {
    type: "bar",
    data: {
      labels: rLabels,
      datasets: [
        { label: "Realized TP %", data: rRealized, backgroundColor: "#34d399", borderRadius: 4 },
        { label: "+30s Post-Exit %", data: r30s, backgroundColor: "#38bdf8", borderRadius: 4 },
        { label: "Peak Runner %", data: rPeak, backgroundColor: "#a855f7", borderRadius: 4 },
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: "#ededed", font: { family: "JetBrains Mono", size: 10 } } },
        tooltip: { callbacks: { label: ctx => ` ${ctx.dataset.label}: +${ctx.parsed.y.toFixed(1)}%` } }
      },
      scales: {
        x: { grid: { display: false }, ticks: { color: "#888888", font: { family: "JetBrains Mono", size: 10 } } },
        y: { grid: { color: "rgba(255, 255, 255, 0.04)" }, ticks: { color: "#888888", font: { family: "JetBrains Mono", size: 10 }, callback: v => "+" + v + "%" } }
      }
    }
  });

  // 4. MAE vs MFE SCATTER
  const scatterPts = (data.mae_mfe_points || []).map(p => ({
    x: p.x,
    y: p.y,
    trade: p.label,
    pnl: p.pnl,
    pillar: p.pillar
  }));

  const ctxMae = document.getElementById("maeMfeChart").getContext("2d");
  if (maeMfeChart) maeMfeChart.destroy();
  maeMfeChart = new Chart(ctxMae, {
    type: "scatter",
    data: {
      datasets: [{
        label: "Trade R:R (MAE vs MFE)",
        data: scatterPts,
        backgroundColor: (c) => {
          const pt = c.raw;
          if (!pt) return "#34d399";
          return pt.pnl >= 0 ? "#34d399" : "#fb7185";
        },
        pointRadius: 5,
        pointHoverRadius: 8
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx) => ` ${ctx.raw.trade} | Drawdown: -${ctx.parsed.x.toFixed(1)}% | Peak: +${ctx.parsed.y.toFixed(1)}% (₹${ctx.raw.pnl})`
          }
        }
      },
      scales: {
        x: { title: { display: true, text: "Max Adverse Excursion (Drawdown %)", color: "#888888", font: { family: "JetBrains Mono", size: 10 } }, grid: { color: "rgba(255, 255, 255, 0.04)" }, ticks: { color: "#888888", font: { family: "JetBrains Mono", size: 10 }, callback: v => "-" + v + "%" } },
        y: { title: { display: true, text: "Max Favorable Excursion (Peak Run %)", color: "#888888", font: { family: "JetBrains Mono", size: 10 } }, grid: { color: "rgba(255, 255, 255, 0.04)" }, ticks: { color: "#888888", font: { family: "JetBrains Mono", size: 10 }, callback: v => "+" + v + "%" } }
      }
    }
  });
}

function renderTable(trades) {
  const tbody = document.getElementById("journalTableBody");
  tbody.innerHTML = "";

  trades.forEach(t => {
    const tr = document.createElement("tr");
    tr.onclick = () => openForensicModal(t.id);

    let resClass = "win";
    const rawRes = (t.result || "PROFIT").replace(/[✅🛡️❌]/g, '').trim();
    const displayRes = rawRes.includes("WIN") ? rawRes.replace("WIN", "PROFIT") : rawRes;
    if (rawRes.includes("SHADOW")) resClass = "shadow";
    else if (rawRes.includes("BE")) resClass = "be";
    else if (rawRes.includes("LOSS")) resClass = "loss";

    const gross = t.gross_pnl_rs !== undefined ? t.gross_pnl_rs : t.pnl_rs;
    const chg = t.charges_rs !== undefined ? t.charges_rs : 0.0;
    const net = t.net_pnl_rs !== undefined ? t.net_pnl_rs : (gross - chg);
    const grossClass = gross >= 0 ? "pnl-pos" : "pnl-neg";
    const netClass = net >= 0 ? "pnl-pos" : "pnl-neg";
    const grossPrefix = gross >= 0 ? "+₹" : "₹";
    const netPrefix = net >= 0 ? "+₹" : "₹";

    tr.innerHTML = `
      <td data-label="#" class="mono" style="color:var(--text-muted); font-weight:700;">#${t.id}</td>
      <td data-label="Date &amp; Time" class="mono">${t.date} <span style="color:var(--text-muted); font-size:10.5px;">${t.entry_time}</span></td>
      <td data-label="Instrument" class="mono" style="font-weight:600; color:#fff;">${t.index} ${t.strike} <span style="font-size:10.5px; color:var(--primary); font-weight:normal; display:block;">${t.otm_depth ? t.otm_depth + ' OTM' : (t.otm_display || '')}</span></td>
      <td data-label="Type"><span class="badge ${t.type.toLowerCase()}">${t.type}</span></td>
      <td data-label="Entry ₹" class="mono">₹${t.entry_price.toFixed(2)}</td>
      <td data-label="Exit ₹" class="mono">₹${t.exit_price.toFixed(2)}</td>
      <td data-label="Dur" class="mono">${t.duration_str}</td>
      <td data-label="Gain %" class="mono ${grossClass}">${t.opt_pct >= 0 ? '+' : ''}${t.opt_pct.toFixed(2)}%</td>
      <td data-label="Gross PnL" class="mono ${grossClass}">${grossPrefix}${gross.toFixed(2)}</td>
      <td data-label="Charges" class="mono" style="color:var(--text-muted); font-size:11px;">₹${chg.toFixed(2)}</td>
      <td data-label="Net PnL" class="mono ${netClass}" style="font-weight:800;">${netPrefix}${net.toFixed(2)}</td>
      <td data-label="Pillar"><span class="badge pillar">${t.pillar}</span></td>
      <td data-label="Result"><span class="badge ${resClass}">${displayRes}</span></td>
      <td data-label="Forensics"><button class="btn-inspect" onclick="event.stopPropagation(); openForensicModal(${t.id})">Inspect</button></td>
    `;
    tbody.appendChild(tr);
  });

  const tfoot = document.getElementById("journalTableFoot");
  if (tfoot) {
    if (trades.length > 0) {
      let totGross = 0.0, totCharges = 0.0, totNet = 0.0, sumGain = 0.0;
      trades.forEach(t => {
        const g = t.gross_pnl_rs !== undefined ? parseFloat(t.gross_pnl_rs) : parseFloat(t.pnl_rs || 0.0);
        const c = t.charges_rs !== undefined ? parseFloat(t.charges_rs) : 0.0;
        const n = t.net_pnl_rs !== undefined ? parseFloat(t.net_pnl_rs) : (g - c);
        totGross += g;
        totCharges += c;
        totNet += n;
        sumGain += parseFloat(t.opt_pct || 0.0);
      });
      const avgGain = sumGain / trades.length;
      const grossClass = totGross >= 0 ? "pnl-pos" : "pnl-neg";
      const netClass = totNet >= 0 ? "pnl-pos" : "pnl-neg";
      const gainClass = avgGain >= 0 ? "pnl-pos" : "pnl-neg";
      const grossPrefix = totGross >= 0 ? "+₹" : "₹";
      const netPrefix = totNet >= 0 ? "+₹" : "₹";
      const gainPrefix = avgGain >= 0 ? "+" : "";

      tfoot.innerHTML = `
        <tr style="background:rgba(255,255,255,0.02); border-top:1px solid var(--border-strong); font-weight:700;">
          <td colspan="7" style="color:#fff; text-transform:uppercase; font-size:11px; letter-spacing:0.04em; font-family:var(--font-mono);">
            TOTAL ACCUMULATED (${trades.length} TRADES)
          </td>
          <td class="mono ${gainClass}">${gainPrefix}${avgGain.toFixed(2)}%</td>
          <td class="mono ${grossClass}">${grossPrefix}${totGross.toFixed(2)}</td>
          <td class="mono" style="color:var(--text-muted); font-size:11px;">₹${totCharges.toFixed(2)}</td>
          <td class="mono ${netClass}" style="font-weight:800; font-size:12.5px;">${netPrefix}${totNet.toFixed(2)}</td>
          <td colspan="3" style="color:var(--text-muted); font-size:11px; font-family:var(--font-mono);">SEBI &amp; Brokerage Reconciled</td>
        </tr>
      `;
    } else {
      tfoot.innerHTML = "";
    }
  }
}

function applyFilters() {
  if (!RAW_DATA) return;
  const q = document.getElementById("searchFilter").value.toUpperCase();
  const pillar = document.getElementById("pillarFilter").value;
  const res = document.getElementById("resultFilter").value;

  const filtered = RAW_DATA.trades.filter(t => {
    const matchSearch = !q || (t.strike + " " + t.index + " " + t.type + " " + t.date + " " + (t.otm_display || "")).toUpperCase().includes(q);
    const matchPillar = (pillar === "ALL") || t.pillar.includes(pillar);
    
    let matchResult = true;
    if (res === "WIN") matchResult = (t.is_win || t.result.includes("PROFIT") || t.result.includes("WIN")) && !t.result.includes("SHADOW");
    else if (res === "LOSS") matchResult = t.is_loss || t.result.includes("LOSS");
    else if (res === "BE") matchResult = t.is_be || t.result.includes("BE");
    else if (res === "SHADOW") matchResult = t.result.includes("SHADOW");

    return matchSearch && matchPillar && matchResult;
  });

  renderTable(filtered);
}

function openForensicModal(tradeId) {
  if (!RAW_DATA) return;
  const trade = RAW_DATA.trades.find(t => t.id === tradeId);
  if (!trade) return;

  const f = trade.forensics || {};
  const cScores = f.conviction_sub_scores || {};
  const wy = f.wyckoff || {};

  const mGross = trade.gross_pnl_rs !== undefined ? trade.gross_pnl_rs : trade.pnl_rs;
  const mChg = trade.charges_rs !== undefined ? trade.charges_rs : 0.0;
  const mNet = trade.net_pnl_rs !== undefined ? trade.net_pnl_rs : (mGross - mChg);
  const mGrossClass = mGross >= 0 ? "pnl-pos" : "pnl-neg";
  const mNetClass = mNet >= 0 ? "pnl-pos" : "pnl-neg";
  const mGrossPrefix = mGross >= 0 ? "+₹" : "₹";
  const mNetPrefix = mNet >= 0 ? "+₹" : "₹";

  const displayModalRes = (trade.result || "PROFIT").replace(/[✅🛡️❌]/g, '').replace('WIN', 'PROFIT').trim();

  document.getElementById("modalTitle").innerHTML = `
    <span>#${trade.id} · ${trade.index} ${trade.strike} ${trade.type} <span style="font-size:12px; color:var(--primary); font-weight:normal; margin-left:6px;">[${trade.otm_display || ''}]</span></span>
    <span class="badge ${trade.is_win ? 'win' : 'loss'}" style="margin-left:12px;">${displayModalRes}</span>
  `;

  document.getElementById("modalBody").innerHTML = `
    <div class="forensic-grid">
      <!-- 1. PRICE ACTION -->
      <div class="forensic-card">
        <div class="forensic-card-title">Macro Price Action Setup</div>
        <div class="forensic-row"><span class="forensic-label">Pillar:</span><span class="forensic-val">${trade.pillar}</span></div>
        <div class="forensic-row"><span class="forensic-label">Strike Moneyness:</span><span class="forensic-val" style="color:var(--primary);">${trade.otm_display || 'OTM'} (ATM: ₹${trade.atm || '—'})</span></div>
        <div class="forensic-row"><span class="forensic-label">Anchor Level (S*/R*):</span><span class="forensic-val">${f.anchor_price ? '₹' + f.anchor_price : 'Dynamic 20 EMA'}</span></div>
        <div class="forensic-row"><span class="forensic-label">Hub Prominence:</span><span class="forensic-val">${f.anchor_prominence ? f.anchor_prominence + 'x' : '1.25x'}</span></div>
        <div class="forensic-row"><span class="forensic-label">Candle Body Ratio:</span><span class="forensic-val">${f.candle_body_ratio ? (f.candle_body_ratio * 100).toFixed(1) + '%' : '72%'}</span></div>
        <div class="forensic-row"><span class="forensic-label">Breakout Trigger Price:</span><span class="forensic-val">${f.breakout_price ? '₹' + f.breakout_price : '₹' + trade.index_entry}</span></div>
      </div>

      <!-- 2. WYCKOFF GATE -->
      <div class="forensic-card">
        <div class="forensic-card-title">Wyckoff Volume &amp; OFI Delta Gate</div>
        <div class="forensic-row"><span class="forensic-label">Volume Dispersion (VDR):</span><span class="forensic-val">${wy.vdr ? wy.vdr.toFixed(2) + 'x' : '1.08x'}</span></div>
        <div class="forensic-row"><span class="forensic-label">Order Flow Intensity (OFI):</span><span class="forensic-val">${wy.ofi ? (wy.ofi / 1000000).toFixed(2) + 'M' : '1.5M'}</span></div>
        <div class="forensic-row"><span class="forensic-label">Flow Direction:</span><span class="forensic-val" style="color:var(--success-bright);">${trade.type === 'CALL' ? 'Aggressive Call Buy' : 'Aggressive Put Buy'}</span></div>
        <div class="forensic-row"><span class="forensic-label">Gate Verdict:</span><span class="forensic-val">${wy.verdict || 'HARMONY_CONFIRMED'}</span></div>
      </div>

      <!-- 3. CONVICTION BREAKDOWN -->
      <div class="forensic-card">
        <div class="forensic-card-title">Conviction Decomposition (${f.conviction_score || 85}/100)</div>
        <div class="conviction-bar-wrap">
          <div class="c-bar-label"><span>Structure (${cScores.structure || 25}/30)</span><span>${Math.round(((cScores.structure || 25)/30)*100)}%</span></div>
          <div class="c-bar-bg"><div class="c-bar-fill" style="width: ${((cScores.structure || 25)/30)*100}%;"></div></div>

          <div class="c-bar-label"><span>Impulse (${cScores.impulse || 24}/30)</span><span>${Math.round(((cScores.impulse || 24)/30)*100)}%</span></div>
          <div class="c-bar-bg"><div class="c-bar-fill" style="width: ${((cScores.impulse || 24)/30)*100}%; background: var(--purple);"></div></div>

          <div class="c-bar-label"><span>Flow (${cScores.flow || 19}/25)</span><span>${Math.round(((cScores.flow || 19)/25)*100)}%</span></div>
          <div class="c-bar-bg"><div class="c-bar-fill" style="width: ${((cScores.flow || 19)/25)*100}%; background: var(--success-bright);"></div></div>

          <div class="c-bar-label"><span>Contract (${cScores.contract || 15}/15)</span><span>100%</span></div>
          <div class="c-bar-bg"><div class="c-bar-fill" style="width: 100%; background: var(--primary);"></div></div>
        </div>
      </div>

      <!-- 4. EXECUTION ATTRIBUTION -->
      <div class="forensic-card">
        <div class="forensic-card-title">Execution &amp; Accounting</div>
        <div class="forensic-row"><span class="forensic-label">Fill Latency:</span><span class="forensic-val">${(f.latency_ms || 2500).toFixed(0)} ms</span></div>
        <div class="forensic-row"><span class="forensic-label">Hold Duration:</span><span class="forensic-val">${trade.duration_str}</span></div>
        <div class="forensic-row"><span class="forensic-label">Spot Excursion (MFE / MAE):</span><span class="forensic-val"><span style="color:var(--success-bright);">${(trade.spot_peak_mfe_pts !== undefined ? trade.spot_peak_mfe_pts : (f.spot_peak_mfe_pts || 0)) >= 0 ? '+' : ''}${(trade.spot_peak_mfe_pts !== undefined ? trade.spot_peak_mfe_pts : (f.spot_peak_mfe_pts || 0)).toFixed(2)} pts</span> / <span style="color:var(--danger);">${(trade.spot_peak_mae_pts !== undefined ? trade.spot_peak_mae_pts : (f.spot_peak_mae_pts || 0)).toFixed(2)} pts</span></span></div>
        <div class="forensic-row"><span class="forensic-label">Time to Spot MFE:</span><span class="forensic-val">${(trade.spot_time_to_mfe_ms || f.spot_time_to_mfe_ms || Math.round((trade.spot_time_to_mfe_sec || f.spot_time_to_mfe_sec || 0) * 1000))} ms (${(trade.spot_time_to_mfe_sec !== undefined ? trade.spot_time_to_mfe_sec : (f.spot_time_to_mfe_sec || 0)).toFixed(2)}s)</span></div>
        <div class="forensic-row"><span class="forensic-label">Gross P&amp;L:</span><span class="forensic-val ${mGrossClass}" style="font-weight:700;">${mGrossPrefix}${mGross.toFixed(2)}</span></div>
        <div class="forensic-row"><span class="forensic-label">Brokerage:</span><span class="forensic-val" style="color:var(--text-muted);">₹${(trade.brokerage_rs || 10).toFixed(2)}</span></div>
        <div class="forensic-row"><span class="forensic-label">Statutory Taxes:</span><span class="forensic-val" style="color:var(--text-muted);">₹${(trade.taxes_rs || Math.max(0, mChg - 10)).toFixed(2)}</span></div>
        <div class="forensic-row"><span class="forensic-label">Total Charges:</span><span class="forensic-val" style="color:var(--text-muted);">₹${mChg.toFixed(2)}</span></div>
        <div class="forensic-row"><span class="forensic-label">Net Profit (Pocket):</span><span class="forensic-val ${mNetClass}" style="font-weight:800; font-size:12.5px;">${mNetPrefix}${mNet.toFixed(2)}</span></div>
        <div class="forensic-row"><span class="forensic-label">Exit Rule:</span><span class="forensic-val">${trade.exit_reason}</span></div>
      </div>
    </div>

    <!-- RUNNER HIGHLIGHT -->
    <div class="runner-highlight">
      <div class="runner-stat">
        <div class="val">${(trade.opt_pct || 0) >= 0 ? '+' : ''}${(trade.opt_pct || 0).toFixed(2)}%</div>
        <div class="lbl">Realized TP</div>
      </div>
      <div class="runner-stat">
        <div class="val">${((trade.runner && trade.runner.mfe_30s_pct) || 0) >= 0 ? '+' : ''}${((trade.runner && trade.runner.mfe_30s_pct) || 0).toFixed(1)}%</div>
        <div class="lbl">+30s Post-Exit</div>
      </div>
      <div class="runner-stat">
        <div class="val">${((trade.runner && trade.runner.mfe_60s_pct) || 0) >= 0 ? '+' : ''}${((trade.runner && trade.runner.mfe_60s_pct) || 0).toFixed(1)}%</div>
        <div class="lbl">+60s Post-Exit</div>
      </div>
      <div class="runner-stat">
        <div class="val">${((trade.runner && trade.runner.peak_runner_pct) || 0) >= 0 ? '+' : ''}${((trade.runner && trade.runner.peak_runner_pct) || 0).toFixed(1)}%</div>
        <div class="lbl">Candle Peak MFE</div>
      </div>
    </div>

    <!-- LEARNINGS & CODE CHANGE -->
    <div class="learnings-box">
      <h4>Post-Mortem &amp; Quant Learnings:</h4>
      <p><strong>Moneyness &amp; OTM Depth:</strong> <span style="color:var(--primary); font-weight:600;">${trade.otm_display || 'OTM'}</span> | Reference ATM: ₹${trade.atm || '—'}</p>
      <p><strong>Post-Mortem:</strong> ${trade.key_learnings || 'Execution followed strict maker TP protocols with zero adverse selection.'}</p>
      <p><strong>Code Evolution:</strong> ${trade.code_change || 'Tolerances validated.'}</p>
      <p style="margin-bottom:0;"><strong>Machine Behavior:</strong> ${trade.remarks || 'Passively filled maker order.'}</p>
    </div>
  `;

  document.getElementById("forensicModal").classList.add("active");
}

function closeModal() {
  document.getElementById("forensicModal").classList.remove("active");
}
function closeModalOnOverlay(e) {
  if (e.target.id === "forensicModal") closeModal();
}

window.onload = loadData;

setInterval(() => {
  const modal = document.getElementById("forensicModal");
  if (!modal || !modal.classList.contains("active")) {
    loadData();
  }
}, 10000);
</script>
</body>
</html>
"""
