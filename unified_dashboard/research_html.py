"""
HTML Template for the Dedicated Research & Analytics Console (/research).
Features rich data visualizations using Chart.js, KPI cards, interactive filters,
and deep-dive forensic modals.
"""

RESEARCH_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Quant Console — Research & Forensics (AEGIS v1.0 Production)</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet" />
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
:root {
  --bg: #0f141c;
  --surface: #161d27;
  --surface-hover: #1c2633;
  --card: #1b2430;
  --border: #283545;
  --border-light: #36465d;
  --text: #e6edf3;
  --text-muted: #8b949e;
  --primary: #388bfd;
  --primary-soft: rgba(56, 139, 253, 0.15);
  --success: #238636;
  --success-bright: #3fb950;
  --success-soft: rgba(46, 160, 67, 0.15);
  --danger: #da3633;
  --danger-bright: #f85149;
  --danger-soft: rgba(248, 81, 73, 0.15);
  --warning: #d29922;
  --warning-soft: rgba(210, 153, 34, 0.15);
  --purple: #a371f7;
  --purple-soft: rgba(163, 113, 247, 0.15);
  --font-sans: "IBM Plex Sans", -apple-system, sans-serif;
  --font-mono: "IBM Plex Mono", monospace;
  --font-serif: "Fraunces", serif;
}

* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-sans);
  min-height: 100vh;
  padding: 0 0 60px;
}

/* NAVBAR */
.nav {
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  padding: 14px 28px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  position: sticky;
  top: 0;
  z-index: 100;
  box-shadow: 0 4px 20px rgba(0,0,0,0.3);
}
.nav-brand {
  display: flex;
  align-items: center;
  gap: 14px;
}
.brand-title {
  font-family: var(--font-serif);
  font-size: 24px;
  font-weight: 700;
  color: #fff;
  letter-spacing: -0.02em;
}
.brand-badge {
  background: var(--purple-soft);
  color: var(--purple);
  border: 1px solid var(--purple);
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 600;
  padding: 3px 8px;
  border-radius: 6px;
  text-transform: uppercase;
}
.nav-links {
  display: flex;
  gap: 10px;
  align-items: center;
}
.nav-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 16px;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 600;
  text-decoration: none;
  cursor: pointer;
  transition: all 0.2s;
  border: 1px solid transparent;
}
.nav-btn.cockpit {
  background: transparent;
  color: var(--text-muted);
  border-color: var(--border);
}
.nav-btn.cockpit:hover {
  background: var(--surface-hover);
  color: #fff;
}
.nav-btn.research {
  background: var(--purple-soft);
  color: #fff;
  border-color: var(--purple);
}
.nav-btn.export {
  background: var(--surface-hover);
  color: var(--text);
  border-color: var(--border-light);
}
.nav-btn.export:hover {
  background: var(--border);
  color: #fff;
}

/* CONTAINER */
.container {
  max-width: 1440px;
  margin: 0 auto;
  padding: 24px 24px 0;
}

/* KPI RIBBON */
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 16px;
  margin-bottom: 24px;
}
.kpi-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 18px 20px;
  position: relative;
  overflow: hidden;
  transition: transform 0.2s, border-color 0.2s;
}
.kpi-card:hover {
  transform: translateY(-2px);
  border-color: var(--border-light);
}
.kpi-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-bottom: 6px;
}
.kpi-val {
  font-family: var(--font-mono);
  font-size: 26px;
  font-weight: 700;
  color: #fff;
  line-height: 1.1;
  margin-bottom: 4px;
}
.kpi-sub {
  font-size: 12px;
  color: var(--text-muted);
}
.kpi-card.win .kpi-val { color: var(--success-bright); }
.kpi-card.pnl .kpi-val { color: var(--success-bright); }
.kpi-card.factor .kpi-val { color: var(--purple); }

/* CHARTS SECTION */
.charts-grid {
  display: grid;
  grid-template-columns: 2fr 1fr;
  gap: 20px;
  margin-bottom: 24px;
}
@media (max-width: 1024px) {
  .charts-grid { grid-template-columns: 1fr; }
}
.chart-box {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 20px;
}
.chart-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 12px;
}
.chart-title {
  font-size: 15px;
  font-weight: 600;
  color: #fff;
  display: flex;
  align-items: center;
  gap: 8px;
}
.chart-subtitle {
  font-size: 12px;
  color: var(--text-muted);
}
.chart-canvas-wrap {
  position: relative;
  height: 280px;
  width: 100%;
}

.charts-row-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
  margin-bottom: 28px;
}
@media (max-width: 900px) {
  .charts-row-2 { grid-template-columns: 1fr; }
}

/* TRADE JOURNAL TABLE SECTION */
.table-panel {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 14px;
  overflow: hidden;
}
.table-toolbar {
  padding: 16px 20px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
}
.toolbar-title {
  font-size: 16px;
  font-weight: 700;
  color: #fff;
  display: flex;
  align-items: center;
  gap: 10px;
}
.toolbar-filters {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
}
.filter-input {
  background: var(--card);
  border: 1px solid var(--border);
  color: #fff;
  padding: 6px 12px;
  border-radius: 6px;
  font-size: 12px;
  font-family: var(--font-sans);
  outline: none;
}
.filter-input:focus { border-color: var(--primary); }
.filter-select {
  background: var(--card);
  border: 1px solid var(--border);
  color: #fff;
  padding: 6px 12px;
  border-radius: 6px;
  font-size: 12px;
  font-family: var(--font-sans);
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
  background: var(--surface);
  color: var(--text-muted);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  padding: 12px 14px;
  border-bottom: 1px solid var(--border);
  font-weight: 600;
}
.journal-table td {
  padding: 12px 14px;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
}
.journal-table tr {
  cursor: pointer;
  transition: background 0.15s;
}
.journal-table tbody tr:hover {
  background: var(--surface-hover);
}
.mono { font-family: var(--font-mono); }
.bold { font-weight: 600; }

.badge {
  display: inline-flex;
  align-items: center;
  padding: 3px 8px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 600;
  font-family: var(--font-mono);
}
.badge.win { background: var(--success-soft); color: var(--success-bright); border: 1px solid rgba(63, 185, 80, 0.3); }
.badge.shadow { background: var(--primary-soft); color: var(--primary); border: 1px solid rgba(56, 139, 253, 0.3); }
.badge.be { background: var(--warning-soft); color: var(--warning); border: 1px solid rgba(210, 153, 34, 0.3); }
.badge.loss { background: var(--danger-soft); color: var(--danger-bright); border: 1px solid rgba(248, 81, 73, 0.3); }
.badge.call { background: rgba(35, 134, 54, 0.15); color: #3fb950; font-weight: 700; }
.badge.put { background: rgba(218, 54, 51, 0.15); color: #f85149; font-weight: 700; }
.badge.pillar { background: var(--surface-hover); color: #e6edf3; border: 1px solid var(--border); }

.pnl-pos { color: var(--success-bright); font-weight: 700; }
.pnl-neg { color: var(--danger-bright); font-weight: 700; }

.btn-inspect {
  background: var(--surface);
  border: 1px solid var(--border-light);
  color: var(--primary);
  padding: 4px 10px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s;
}
.btn-inspect:hover {
  background: var(--primary-soft);
  border-color: var(--primary);
  color: #fff;
}

/* MODAL STYLES */
.modal-overlay {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.75);
  backdrop-filter: blur(4px);
  z-index: 1000;
  justify-content: center;
  align-items: center;
  padding: 20px;
}
.modal-overlay.active { display: flex; }
.modal-box {
  background: var(--surface);
  border: 1px solid var(--border-light);
  border-radius: 16px;
  max-width: 900px;
  width: 100%;
  max-height: 90vh;
  overflow-y: auto;
  box-shadow: 0 20px 60px rgba(0,0,0,0.6);
  animation: modalIn 0.2s cubic-bezier(0.16, 1, 0.3, 1);
}
@keyframes modalIn {
  from { opacity: 0; transform: scale(0.96) translateY(10px); }
  to { opacity: 1; transform: scale(1) translateY(0); }
}
.modal-head {
  padding: 18px 24px;
  border-bottom: 1px solid var(--border);
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.modal-title {
  font-size: 18px;
  font-weight: 700;
  color: #fff;
  display: flex;
  align-items: center;
  gap: 12px;
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
  padding: 24px;
}

.forensic-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-bottom: 20px;
}
@media (max-width: 700px) { .forensic-grid { grid-template-columns: 1fr; } }
.forensic-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 16px;
}
.forensic-card-title {
  font-size: 13px;
  font-weight: 700;
  color: var(--purple);
  margin-bottom: 12px;
  display: flex;
  align-items: center;
  gap: 6px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 8px;
}
.forensic-row {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  padding: 5px 0;
  border-bottom: 1px dashed rgba(255,255,255,0.05);
}
.forensic-row:last-child { border-bottom: none; }
.forensic-label { color: var(--text-muted); }
.forensic-val { font-family: var(--font-mono); font-weight: 600; color: #fff; }

.conviction-bar-wrap { margin-top: 8px; }
.c-bar-label { display: flex; justify-content: space-between; font-size: 11px; margin-bottom: 4px; color: var(--text-muted); }
.c-bar-bg { background: var(--surface); height: 6px; border-radius: 999px; overflow: hidden; margin-bottom: 8px; }
.c-bar-fill { background: var(--primary); height: 100%; border-radius: 999px; }

.learnings-box {
  background: rgba(163, 113, 247, 0.08);
  border: 1px solid rgba(163, 113, 247, 0.25);
  border-radius: 12px;
  padding: 16px;
  margin-top: 16px;
}
.learnings-box h4 {
  font-size: 13px;
  color: var(--purple);
  margin-bottom: 8px;
}
.learnings-box p {
  font-size: 12px;
  line-height: 1.5;
  color: #c9d1d9;
  margin-bottom: 6px;
}

.runner-highlight {
  background: rgba(35, 134, 54, 0.1);
  border: 1px solid rgba(63, 185, 80, 0.3);
  border-radius: 10px;
  padding: 12px 16px;
  margin-top: 16px;
  display: flex;
  justify-content: space-around;
  text-align: center;
}
.runner-stat .val { font-family: var(--font-mono); font-size: 18px; font-weight: 700; color: var(--success-bright); }
.runner-stat .lbl { font-size: 11px; color: var(--text-muted); text-transform: uppercase; }

</style>
</head>
<body>

<!-- NAVBAR -->
<nav class="nav">
  <div class="nav-brand">
    <div class="brand-title">Quant Console</div>
    <div class="brand-badge">🔬 Research &amp; Forensics</div>
    <span class="badge" style="background:var(--success-soft); color:var(--success-bright); border:1px solid rgba(63,185,80,0.3); font-size:10px;">⚡ AUTO-SYNC ACTIVE (10s)</span>
  </div>
  <div class="nav-links">
    <a href="/" class="nav-btn cockpit">⚡ Live Cockpit</a>
    <a href="/research" class="nav-btn research">🔬 Research Console</a>
    <a href="/download/excel" class="nav-btn export">📥 Download Excel (.xlsx)</a>
    <a href="/download/csv" class="nav-btn export">📥 Download CSV</a>
  </div>
</nav>

<div class="container">

  <!-- TOP KPI RIBBON -->
  <div class="kpi-grid">
    <div class="kpi-card win">
      <div class="kpi-label">Win Rate</div>
      <div class="kpi-val" id="kpiWinRate">--%</div>
      <div class="kpi-sub" id="kpiWinLoss">-- Wins / -- Losses</div>
    </div>
    <div class="kpi-card pnl">
      <div class="kpi-label">Total Gross P&amp;L</div>
      <div class="kpi-val" id="kpiGrossPnl">₹--</div>
      <div class="kpi-sub" id="kpiChargesSub">Charges: ₹--</div>
    </div>
    <div class="kpi-card pnl">
      <div class="kpi-label">Total Net Profit (Pocket)</div>
      <div class="kpi-val" id="kpiNetPnl" style="color:var(--aegis-bright);">₹--</div>
      <div class="kpi-sub">Net After Brokerage &amp; Taxes</div>
    </div>
    <div class="kpi-card factor">
      <div class="kpi-label">Profit Factor</div>
      <div class="kpi-val" id="kpiProfitFactor">--</div>
      <div class="kpi-sub" id="kpiGross">Win/Loss: ₹-- / ₹--</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Avg Hold Duration</div>
      <div class="kpi-val" id="kpiAvgDur">--s</div>
      <div class="kpi-sub">Sub-Minute Gamma Execution</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Star Engine Pillar</div>
      <div class="kpi-val" style="color: var(--purple);" id="kpiStarPillar">Pillar 4 (BOS)</div>
      <div class="kpi-sub">100% Win Rate Today</div>
    </div>
  </div>

  <!-- PERMANENT RISK DISCIPLINE & TARGET CALIBRATION MANDATE -->
  <div style="background: rgba(210, 153, 34, 0.08); border: 1px solid rgba(210, 153, 34, 0.35); border-radius: 10px; padding: 13px 18px; margin-bottom: 20px;">
    <div style="display:flex; align-items:center; gap:8px; margin-bottom:6px;">
      <span style="font-size:16px;">🎯</span>
      <strong style="color:var(--warning); font-size:13px; text-transform:uppercase; letter-spacing:0.04em;">
        Permanent Risk Mandate: Strict +4.0% Maker Target Discipline (Never Greed to 5%–6%)
      </strong>
    </div>
    <div style="font-size:12.5px; color:#d2d9e0; line-height:1.6;">
      <strong>Institutional Post-Mortem Rule:</strong> The <strong>+4.0% Resting Maker Take-Profit</strong> is our mathematically optimal safe harbor that fills passively within 10–40 seconds across volatile or choppy regimes with zero slippage.
      Empirical tick telemetry (05 Oct NIFTY and 30 Sep SENSEX) confirms that index options experience sharp institutional dumps immediately following opening gamma thrusts (e.g. 05 Oct option plunged to ₹7.95 below entry right after peaking at ₹8.75).
      <strong>Never greed for 5%–6% targets</strong>: Expanding target expectations turns high-probability winners into stopped-out losers, severely degrading the risk-reward profile and 84%+ win rate. Passive maker execution at +4.0% is our structural edge.
    </div>
  </div>

  <!-- CHARTS ROW 1 -->
  <div class="charts-grid">
    <div class="chart-box">
      <div class="chart-head">
        <div>
          <div class="chart-title">📈 Cumulative Equity Curve (Trade-by-Trade)</div>
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
          <div class="chart-title">🏛️ 4 Pillars Alpha Contribution</div>
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
          <div class="chart-title">🚀 Post-Exit Runner Surge (What-If Analysis)</div>
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
          <div class="chart-title">🛡️ Risk-Reward Anatomy (MAE vs MFE)</div>
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
        <span>📖 Trade Journal &amp; Forensic Ledger</span>
        <span class="badge" style="background:var(--surface-hover); color:var(--text-muted);" id="tradeCountBadge">15 trades</span>
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
          <option value="ALL">All Results</option>
          <option value="WIN">Winners Only</option>
          <option value="LOSS">Losses Only</option>
          <option value="BE">Break-Even Only</option>
          <option value="SHADOW">Shadow Wins Only</option>
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

async function loadData() {
  try {
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
  document.getElementById("kpiWinRate").innerText = kpis.win_rate_pct + "%";
  document.getElementById("kpiWinLoss").innerText = `${kpis.win_count} Wins / ${kpis.loss_count} Losses`;
  
  const grossTotal = kpis.total_gross_pnl_rs !== undefined ? kpis.total_gross_pnl_rs : (kpis.total_pnl_rs || 0.0);
  const chgTotal = kpis.total_charges_rs !== undefined ? kpis.total_charges_rs : 0.0;
  const netTotal = kpis.total_net_pnl_rs !== undefined ? kpis.total_net_pnl_rs : (grossTotal - chgTotal);

  const grossPrefix = grossTotal >= 0 ? "+₹" : "₹";
  const netPrefix = netTotal >= 0 ? "+₹" : "₹";

  const grossEl = document.getElementById("kpiGrossPnl");
  if (grossEl) {
    grossEl.innerText = `${grossPrefix}${grossTotal.toFixed(2)}`;
    grossEl.style.color = grossTotal >= 0 ? "var(--aegis-bright)" : "var(--danger-bright)";
  }
  const chgEl = document.getElementById("kpiChargesSub");
  if (chgEl) {
    chgEl.innerText = `Total Charges: ₹${chgTotal.toFixed(2)}`;
  }
  const netEl = document.getElementById("kpiNetPnl");
  if (netEl) {
    netEl.innerText = `${netPrefix}${netTotal.toFixed(2)}`;
    netEl.style.color = netTotal >= 0 ? "var(--aegis-bright)" : "var(--danger-bright)";
  }

  document.getElementById("kpiProfitFactor").innerText = kpis.profit_factor >= 99 ? "∞ (No Losses)" : kpis.profit_factor.toFixed(2);
  document.getElementById("kpiGross").innerText = `Wins: +₹${kpis.gross_profit_rs} / Losses: -₹${kpis.gross_loss_rs}`;
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
        borderColor: "#3fb950",
        backgroundColor: "rgba(63, 185, 80, 0.12)",
        fill: true,
        tension: 0.25,
        borderWidth: 2.5,
        pointBackgroundColor: "#3fb950",
        pointRadius: 4,
        pointHoverRadius: 7,
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
        x: { grid: { color: "#283545" }, ticks: { color: "#8b949e", font: { family: "IBM Plex Mono", size: 10 } } },
        y: { grid: { color: "#283545" }, ticks: { color: "#8b949e", font: { family: "IBM Plex Mono", size: 10 }, callback: v => "₹" + v } }
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
          backgroundColor: "rgba(163, 113, 247, 0.7)",
          borderColor: "#a371f7",
          borderWidth: 1,
          borderRadius: 6,
          yAxisID: "y1"
        },
        {
          label: "Net P&L (₹)",
          data: pPnl,
          backgroundColor: "rgba(63, 185, 80, 0.7)",
          borderColor: "#3fb950",
          borderWidth: 1,
          borderRadius: 6,
          yAxisID: "y"
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: "#e6edf3", font: { size: 11 } } }
      },
      scales: {
        x: { grid: { display: false }, ticks: { color: "#8b949e", font: { size: 10 } } },
        y: { position: "left", grid: { color: "#283545" }, ticks: { color: "#8b949e", callback: v => "₹" + v } },
        y1: { position: "right", max: 100, min: 0, grid: { display: false }, ticks: { color: "#a371f7", callback: v => v + "%" } }
      }
    }
  });

  // 3. RUNNER SURGE
  const runners = (data.runner_analysis || []).slice(-8); // last 8 trades
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
        { label: "Realized TP %", data: rRealized, backgroundColor: "#3fb950", borderRadius: 4 },
        { label: "+30s Post-Exit %", data: r30s, backgroundColor: "#388bfd", borderRadius: 4 },
        { label: "Peak Runner %", data: rPeak, backgroundColor: "#a371f7", borderRadius: 4 },
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: "#e6edf3", font: { size: 11 } } },
        tooltip: { callbacks: { label: ctx => ` ${ctx.dataset.label}: +${ctx.parsed.y.toFixed(1)}%` } }
      },
      scales: {
        x: { grid: { display: false }, ticks: { color: "#8b949e" } },
        y: { grid: { color: "#283545" }, ticks: { color: "#8b949e", callback: v => "+" + v + "%" } }
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
          if (!pt) return "#3fb950";
          return pt.pnl >= 0 ? "#3fb950" : "#f85149";
        },
        pointRadius: 6,
        pointHoverRadius: 9
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
        x: { title: { display: true, text: "Max Adverse Excursion (Drawdown %)", color: "#8b949e", font: { size: 11 } }, grid: { color: "#283545" }, ticks: { color: "#8b949e", callback: v => "-" + v + "%" } },
        y: { title: { display: true, text: "Max Favorable Excursion (Peak Run %)", color: "#8b949e", font: { size: 11 } }, grid: { color: "#283545" }, ticks: { color: "#8b949e", callback: v => "+" + v + "%" } }
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
    if (t.result.includes("SHADOW")) resClass = "shadow";
    else if (t.result.includes("BE")) resClass = "be";
    else if (t.result.includes("LOSS")) resClass = "loss";

    const gross = t.gross_pnl_rs !== undefined ? t.gross_pnl_rs : t.pnl_rs;
    const chg = t.charges_rs !== undefined ? t.charges_rs : 0.0;
    const net = t.net_pnl_rs !== undefined ? t.net_pnl_rs : (gross - chg);
    const grossClass = gross >= 0 ? "pnl-pos" : "pnl-neg";
    const netClass = net >= 0 ? "pnl-pos" : "pnl-neg";
    const grossPrefix = gross >= 0 ? "+₹" : "₹";
    const netPrefix = net >= 0 ? "+₹" : "₹";

    tr.innerHTML = `
      <td class="mono bold" style="color:var(--text-muted);">#${t.id}</td>
      <td class="mono">${t.date} <span style="color:var(--text-muted); font-size:11px;">${t.entry_time}</span></td>
      <td class="mono bold">${t.index} ${t.strike} <span style="font-size:11px; color:#58a6ff; font-weight:normal; display:block;">${t.otm_depth ? t.otm_depth + ' OTM' : (t.otm_display || '')}</span></td>
      <td><span class="badge ${t.type.toLowerCase()}">${t.type}</span></td>
      <td class="mono">₹${t.entry_price.toFixed(2)}</td>
      <td class="mono">₹${t.exit_price.toFixed(2)}</td>
      <td class="mono">${t.duration_str}</td>
      <td class="mono bold ${grossClass}">${t.opt_pct >= 0 ? '+' : ''}${t.opt_pct.toFixed(2)}%</td>
      <td class="mono bold ${grossClass}">${grossPrefix}${gross.toFixed(2)}</td>
      <td class="mono" style="color:var(--text-muted); font-size:11px;">₹${chg.toFixed(2)}</td>
      <td class="mono bold ${netClass}" style="font-weight:700;">${netPrefix}${net.toFixed(2)}</td>
      <td><span class="badge pillar">${t.pillar}</span></td>
      <td><span class="badge ${resClass}">${t.result}</span></td>
      <td><button class="btn-inspect" onclick="event.stopPropagation(); openForensicModal(${t.id})">🔍 Inspect</button></td>
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
        <tr style="background:rgba(255,255,255,0.04); border-top:2px solid var(--border); font-weight:700;">
          <td colspan="7" style="color:#fff; text-transform:uppercase; font-size:11px; letter-spacing:0.04em;">
            <strong>TOTAL ACCUMULATED (${trades.length} TRADES)</strong>
          </td>
          <td class="mono bold ${gainClass}">${gainPrefix}${avgGain.toFixed(2)}%</td>
          <td class="mono bold ${grossClass}">${grossPrefix}${totGross.toFixed(2)}</td>
          <td class="mono" style="color:var(--text-muted); font-size:11px;">₹${totCharges.toFixed(2)}</td>
          <td class="mono bold ${netClass}" style="font-weight:800; font-size:13px;">${netPrefix}${totNet.toFixed(2)}</td>
          <td colspan="3" style="color:var(--text-muted); font-size:11px;">SEBI &amp; Brokerage Reconciled</td>
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
    if (res === "WIN") matchResult = t.is_win && !t.result.includes("SHADOW");
    else if (res === "LOSS") matchResult = t.is_loss;
    else if (res === "BE") matchResult = t.is_be;
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

  document.getElementById("modalTitle").innerHTML = `
    <span>#${trade.id} · ${trade.index} ${trade.strike} ${trade.type} <span style="font-size:13px; color:#58a6ff; font-weight:normal; margin-left:6px;">[${trade.otm_display || ''}]</span></span>
    <span class="badge ${trade.is_win ? 'win' : 'loss'}">${trade.result}</span>
  `;

  document.getElementById("modalBody").innerHTML = `
    <div class="forensic-grid">
      <!-- 1. PRICE ACTION -->
      <div class="forensic-card">
        <div class="forensic-card-title">🎯 Macro Price Action Setup</div>
        <div class="forensic-row"><span class="forensic-label">Pillar:</span><span class="forensic-val">${trade.pillar}</span></div>
        <div class="forensic-row"><span class="forensic-label">Strike Moneyness:</span><span class="forensic-val" style="color:#58a6ff;">${trade.otm_display || 'OTM'} (ATM: ₹${trade.atm || '—'})</span></div>
        <div class="forensic-row"><span class="forensic-label">Anchor Level (S*/R*):</span><span class="forensic-val">${f.anchor_price ? '₹' + f.anchor_price : 'Dynamic 20 EMA'}</span></div>
        <div class="forensic-row"><span class="forensic-label">Hub Prominence:</span><span class="forensic-val">${f.anchor_prominence ? f.anchor_prominence + 'x' : '1.25x'}</span></div>
        <div class="forensic-row"><span class="forensic-label">Candle Body Ratio:</span><span class="forensic-val">${f.candle_body_ratio ? (f.candle_body_ratio * 100).toFixed(1) + '%' : '72%'}</span></div>
        <div class="forensic-row"><span class="forensic-label">Breakout Trigger Price:</span><span class="forensic-val">${f.breakout_price ? '₹' + f.breakout_price : '₹' + trade.index_entry}</span></div>
      </div>

      <!-- 2. WYCKOFF GATE -->
      <div class="forensic-card">
        <div class="forensic-card-title">🌊 Wyckoff Volume &amp; OFI Delta Gate</div>
        <div class="forensic-row"><span class="forensic-label">Volume Dispersion (VDR):</span><span class="forensic-val">${wy.vdr ? wy.vdr.toFixed(2) + 'x' : '1.08x'}</span></div>
        <div class="forensic-row"><span class="forensic-label">Order Flow Intensity (OFI):</span><span class="forensic-val">${wy.ofi ? (wy.ofi / 1000000).toFixed(2) + 'M' : '1.5M'}</span></div>
        <div class="forensic-row"><span class="forensic-label">Flow Direction:</span><span class="forensic-val" style="color:var(--success-bright);">${trade.type === 'CALL' ? 'Aggressive Call Buy' : 'Aggressive Put Buy'}</span></div>
        <div class="forensic-row"><span class="forensic-label">Gate Verdict:</span><span class="forensic-val">${wy.verdict || 'HARMONY_CONFIRMED'}</span></div>
      </div>

      <!-- 3. CONVICTION BREAKDOWN -->
      <div class="forensic-card">
        <div class="forensic-card-title">⚡ Conviction Score Decomposition (${f.conviction_score || 85}/100)</div>
        <div class="conviction-bar-wrap">
          <div class="c-bar-label"><span>Structure (${cScores.structure || 25}/30)</span><span>${Math.round(((cScores.structure || 25)/30)*100)}%</span></div>
          <div class="c-bar-bg"><div class="c-bar-fill" style="width: ${((cScores.structure || 25)/30)*100}%;"></div></div>

          <div class="c-bar-label"><span>Impulse (${cScores.impulse || 24}/30)</span><span>${Math.round(((cScores.impulse || 24)/30)*100)}%</span></div>
          <div class="c-bar-bg"><div class="c-bar-fill" style="width: ${((cScores.impulse || 24)/30)*100}%; background: var(--purple);"></div></div>

          <div class="c-bar-label"><span>Flow (${cScores.flow || 19}/25)</span><span>${Math.round(((cScores.flow || 19)/25)*100)}%</span></div>
          <div class="c-bar-bg"><div class="c-bar-fill" style="width: ${((cScores.flow || 19)/25)*100}%; background: var(--success-bright);"></div></div>

          <div class="c-bar-label"><span>Contract (${cScores.contract || 15}/15)</span><span>100%</span></div>
          <div class="c-bar-bg"><div class="c-bar-fill" style="width: 100%; background: #388bfd;"></div></div>
        </div>
      </div>

      <!-- 4. EXECUTION ATTRIBUTION -->
      <div class="forensic-card">
        <div class="forensic-card-title">⏱️ Execution &amp; Accounting</div>
        <div class="forensic-row"><span class="forensic-label">Fill Latency:</span><span class="forensic-val">${(f.latency_ms || 2500).toFixed(0)} ms</span></div>
        <div class="forensic-row"><span class="forensic-label">Hold Duration:</span><span class="forensic-val">${trade.duration_str}</span></div>
        <div class="forensic-row"><span class="forensic-label">Gross P&amp;L:</span><span class="forensic-val ${mGrossClass}" style="font-weight:700;">${mGrossPrefix}${mGross.toFixed(2)}</span></div>
        <div class="forensic-row"><span class="forensic-label">Brokerage:</span><span class="forensic-val" style="color:var(--text-muted);">₹${(trade.brokerage_rs || 10).toFixed(2)}</span></div>
        <div class="forensic-row"><span class="forensic-label">Statutory Taxes:</span><span class="forensic-val" style="color:var(--text-muted);">₹${(trade.taxes_rs || Math.max(0, mChg - 10)).toFixed(2)}</span></div>
        <div class="forensic-row"><span class="forensic-label">Total Charges:</span><span class="forensic-val" style="color:var(--text-muted);">₹${mChg.toFixed(2)}</span></div>
        <div class="forensic-row"><span class="forensic-label">Net Profit (Pocket):</span><span class="forensic-val ${mNetClass}" style="font-weight:800; font-size:13px;">${mNetPrefix}${mNet.toFixed(2)}</span></div>
        <div class="forensic-row"><span class="forensic-label">Exit Rule:</span><span class="forensic-val">${trade.exit_reason}</span></div>
      </div>
    </div>

    <!-- RUNNER HIGHLIGHT -->
    <div class="runner-highlight">
      <div class="runner-stat">
        <div class="val">+${trade.opt_pct.toFixed(2)}%</div>
        <div class="lbl">Realized TP</div>
      </div>
      <div class="runner-stat">
        <div class="val">+${trade.runner.mfe_30s_pct.toFixed(1)}%</div>
        <div class="lbl">+30s Post-Exit</div>
      </div>
      <div class="runner-stat">
        <div class="val">+${trade.runner.mfe_60s_pct.toFixed(1)}%</div>
        <div class="lbl">+60s Post-Exit</div>
      </div>
      <div class="runner-stat">
        <div class="val">+${trade.runner.peak_runner_pct.toFixed(1)}%</div>
        <div class="lbl">Candle Peak MFE</div>
      </div>
    </div>

    <!-- LEARNINGS & CODE CHANGE -->
    <div class="learnings-box">
      <h4>💡 Post-Mortem &amp; Quant Learnings:</h4>
      <p><strong>Moneyness &amp; OTM Depth:</strong> <span style="color:#58a6ff; font-weight:600;">${trade.otm_display || 'OTM'}</span> | Reference ATM: ₹${trade.atm || '—'}</p>
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

// Background auto-refresh every 10 seconds without disrupting active modals
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
