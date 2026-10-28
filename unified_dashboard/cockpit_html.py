"""
AEGIS v1.0 Production Institutional Cockpit HTML Template.
Pure Linear.app dark minimalist aesthetic (Obsidian #000000, 1px micro-borders, zero emojis).
Interactive 3D Topological Persistence & Gamma Manifold (Three.js WebGL with Raycaster Node Inspection).
Dynamic IST Market Hours Status Logic (Market Closed · EOD Flat outside trading hours).
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

<!-- Three.js & OrbitControls for 3D Quant Manifolds -->
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js"></script>

<style>
:root {
  --bg: #000000;
  --bg-subtle: #080808;
  --surface: #0e0e0e;
  --surface-hover: #141414;
  --surface-card: #111111;
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
  background: var(--amber);
  box-shadow: 0 0 8px var(--amber);
  animation: pulse 2s infinite;
}

.pulse-dot.dot-pre {
  background: var(--cyan);
  box-shadow: 0 0 8px var(--cyan);
  animation: none;
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

/* LAYOUT CONTAINER */
.container {
  max-width: 1400px;
  margin: 0 auto;
  padding: 24px 24px 0;
  display: flex;
  flex-direction: column;
  gap: 20px;
}

/* HERO KPIS */
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 14px;
}

@media (max-width: 1100px) {
  .kpi-grid {
    grid-template-columns: repeat(3, 1fr);
  }
}

@media (max-width: 700px) {
  .kpi-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}

@media (max-width: 480px) {
  .kpi-grid {
    grid-template-columns: 1fr;
  }
}

.kpi-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 16px 18px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: 6px;
}

.kpi-title {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-family: var(--font-mono);
}

.kpi-value {
  font-size: 26px;
  font-weight: 700;
  font-family: var(--font-mono);
  letter-spacing: -0.02em;
}

.kpi-subtitle {
  font-size: 11px;
  color: var(--text-muted);
}

/* 3D QUANT TOPOLOGICAL PERSISTENCE & GAMMA MANIFOLD PANEL */
.manifold-panel {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  position: relative;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.manifold-header {
  padding: 14px 18px;
  border-bottom: 1px solid var(--border);
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  background: rgba(0, 0, 0, 0.4);
}

.manifold-title-box {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.manifold-title {
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: #fff;
  font-family: var(--font-mono);
  display: flex;
  align-items: center;
  gap: 8px;
}

.manifold-subtitle {
  font-size: 11px;
  color: var(--text-secondary);
  font-family: var(--font-mono);
}

.manifold-controls {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.control-btn {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--text-secondary);
  font-size: 11px;
  font-weight: 600;
  padding: 5px 10px;
  border-radius: 5px;
  cursor: pointer;
  transition: all 0.15s ease;
  font-family: var(--font-mono);
}

.control-btn:hover {
  background: var(--surface-hover);
  color: #fff;
  border-color: var(--border-strong);
}

.control-btn.active {
  background: rgba(56, 189, 248, 0.12);
  color: var(--cyan);
  border-color: rgba(56, 189, 248, 0.35);
}

.manifold-viewport {
  position: relative;
  width: 100%;
  height: 380px;
  background: #000000;
  cursor: grab;
}

.manifold-viewport:active {
  cursor: grabbing;
}

/* INTERACTIVE HUD INSPECTOR OVERLAY */
.manifold-hud {
  position: absolute;
  top: 14px;
  right: 14px;
  width: 320px;
  max-width: calc(100% - 28px);
  background: rgba(14, 14, 14, 0.88);
  backdrop-filter: blur(12px);
  border: 1px solid var(--border-strong);
  border-radius: 8px;
  padding: 14px;
  z-index: 10;
  box-shadow: 0 12px 36px rgba(0, 0, 0, 0.6);
  pointer-events: auto;
  transition: opacity 0.2s ease, transform 0.2s ease;
}

.hud-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 8px;
}

.hud-title {
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--cyan);
  font-family: var(--font-mono);
}

.hud-badge {
  font-size: 9.5px;
  font-weight: 700;
  padding: 2px 5px;
  border-radius: 3px;
  background: rgba(56, 189, 248, 0.15);
  color: var(--cyan);
  font-family: var(--font-mono);
}

.hud-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 11px;
  padding: 4px 0;
  border-bottom: 1px dashed rgba(255, 255, 255, 0.05);
  font-family: var(--font-mono);
}

.hud-row:last-child {
  border-bottom: none;
}

.hud-label {
  color: var(--text-muted);
}

.hud-val {
  color: #fff;
  font-weight: 600;
}

.hud-hint {
  margin-top: 10px;
  font-size: 10px;
  color: var(--text-muted);
  line-height: 1.4;
  border-top: 1px solid var(--border);
  padding-top: 6px;
  font-style: italic;
}

/* OPERATIONAL DIRECTIVES STRIP */
.status-strip {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 18px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  font-size: 11.5px;
  font-family: var(--font-mono);
}

.strip-item {
  color: var(--text-secondary);
}

.strip-item strong {
  color: #fff;
}

/* PILLARS STATUS STRIP */
.pillars-strip {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
}

@media (max-width: 900px) {
  .pillars-strip {
    grid-template-columns: repeat(2, 1fr);
  }
}

@media (max-width: 500px) {
  .pillars-strip {
    grid-template-columns: 1fr;
  }
}

.pillar-box {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.pillar-name {
  font-size: 11.5px;
  font-weight: 700;
  color: #fff;
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-family: var(--font-mono);
}

.pillar-detail {
  font-size: 11px;
  color: var(--text-muted);
  line-height: 1.4;
}

/* SECTION CARDS & TABLES */
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
      <div class="pulse-dot" id="marketStatusDot"></div>
      <span id="systemStatusText">CALCULATING IST...</span>
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
      <div class="kpi-title">Today Gross PnL</div>
      <div class="kpi-value" style="color:var(--green-bright);" id="kpiGrossPnl">+₹0.00</div>
      <div class="kpi-subtitle" id="kpiGrossSub">Total Charges: ₹0.00</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">Today Net PnL</div>
      <div class="kpi-value" style="color:var(--green-bright);" id="kpiNetPnl">+₹0.00</div>
      <div class="kpi-subtitle" id="kpiNetSub">0 Trades Today</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">Profit Rate</div>
      <div class="kpi-value" style="color:#fff;" id="kpiWinRate">82.4%</div>
      <div class="kpi-subtitle" id="kpiWinSub">28 Profit / 6 Loss (34 Lifetime)</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">Engine Schedule</div>
      <div class="kpi-value" style="font-size:18px; color:#fff;" id="kpiSchedule">NIFTY · 4 DTE</div>
      <div class="kpi-subtitle">NFO Strike Step 50 · 1 Lot (65 Qty)</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">Testing Capital</div>
      <div class="kpi-value" style="font-size:18px; color:#fff;" id="kpiFunds">₹607.54</div>
      <div class="kpi-subtitle">Low-Capital Consistency Mode</div>
    </div>
  </section>

  <!-- 3D S/R LEVELS & GAMMA MANIFOLD PANEL -->
  <section class="manifold-panel">
    <div class="manifold-header">
      <div class="manifold-title-box">
        <div class="manifold-title">S/R Levels &amp; Gamma Manifold</div>
        <div class="manifold-subtitle">Pillar 4 S/R Breakout Hubs · OFI Volatility Surface · Pain Zone Bands</div>
      </div>
      <div class="manifold-controls">
        <button class="control-btn active" id="btnModeTopology" onclick="setManifoldMode('topology')">S/R Hubs</button>
        <button class="control-btn" id="btnModeVol" onclick="setManifoldMode('volatility')">Vol Surface</button>
        <button class="control-btn" id="btnModePain" onclick="setManifoldMode('pain_zone')">Pain Zone</button>
        <button class="control-btn" id="btnToggleRotate" onclick="toggleAutoRotate()">Rotate: ON</button>
        <button class="control-btn" onclick="resetManifoldCamera()">Reset View</button>
      </div>
    </div>

    <div class="manifold-viewport" id="manifoldCanvasContainer">
      <!-- Interactive HUD Card overlay -->
      <div class="manifold-hud" id="manifoldHud">
        <div class="hud-head">
          <span class="hud-title" id="hudStrikeName">SENSEX 82500 CE (ATM)</span>
          <span class="hud-badge" id="hudPhaseBadge">P4 S/R</span>
        </div>
        <div class="hud-row">
          <span class="hud-label">S/R Cluster</span>
          <span class="hud-val" id="hudCluster">Dynamic S/R Level (Active)</span>
        </div>
        <div class="hud-row">
          <span class="hud-label">Order Flow Intensity (OFI)</span>
          <span class="hud-val" id="hudOfi" style="color:var(--green-bright);">+1.84M (Call Accumulation)</span>
        </div>
        <div class="hud-row">
          <span class="hud-label">Volatility / Gamma</span>
          <span class="hud-val" id="hudVol">14.8% IV · Gamma +0.42</span>
        </div>
        <div class="hud-row">
          <span class="hud-label">Pain Zone Alignment</span>
          <span class="hud-val" id="hudPain" style="color:var(--cyan);">Spot &gt; Upper Band (Call Safe)</span>
        </div>
        <div class="hud-row">
          <span class="hud-label">Structural Level</span>
          <span class="hud-val" id="hudBetti">Mode B S/R Clearance: Active</span>
        </div>
        <div class="hud-hint" id="hudHint">
          Click any glowing 3D strike node or drag surface to inspect live S/R breakout metrics.
        </div>
      </div>
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
      <div class="pillar-detail">Min Sweep 1.5-5 pts · Lower Wick ≥ 35% &amp; 1.25x Upper · Wyckoff Veto</div>
    </div>
    <div class="pillar-box">
      <div class="pillar-name"><span>P3 · Squeeze Breakout</span><span style="color:var(--green-bright);">Active</span></div>
      <div class="pillar-detail">Box Clearance 2-5 pts · Opposing Wick ≤ 18% · Solid Body ≥ 35%</div>
    </div>
    <div class="pillar-box">
      <div class="pillar-name"><span>P4 · S/R Breakout</span><span style="color:var(--green-bright);">Active</span></div>
      <div class="pillar-detail">Dynamic Support / Resistance Breakout · In-Flight Mode B Clearance</div>
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

<!-- THREE.JS 3D QUANTUM TOPOLOGICAL MANIFOLD ENGINE -->
<script>
let scene, camera, renderer, controls, mesh, wireframe;
let nodeMarkers = [];
let raycaster = new THREE.Raycaster();
let mouse = new THREE.Vector2();
let currentMode = 'topology';
let isAutoRotating = true;
let targetLookAt = new THREE.Vector3(0, 0, 0);

const container = document.getElementById('manifoldCanvasContainer');

// Pre-defined key strike nodes with quant metadata
const STRIKE_NODES = [
  {
    name: "SENSEX 82500 CE",
    tag: "ATM CENTER",
    pos: [0, 4.2, 0],
    color: 0x00f0ff,
    phase: "P4 S/R BREAKOUT",
    cluster: "Dynamic S/R Level (Active Cluster)",
    ofi: "+1.84M Delta (Aggressive Buy)",
    vol: "14.8% IV · Gamma +0.42",
    pain: "Spot > Upper Band (Call Only)",
    betti: "Betti-0: 1 | Betti-1: 0 (Single Homology Class)"
  },
  {
    name: "SENSEX 82300 PE",
    tag: "SUPPORT WALL",
    pos: [-6.5, 3.4, -4.0],
    color: 0x10b981,
    phase: "LIQUIDITY FLOOR",
    cluster: "Wyckoff Absorption Cluster",
    ofi: "+3.20M Institutional Bid Depth",
    vol: "13.9% IV · Delta 0.28",
    pain: "Firm Anchor Above Pain Zone Lower",
    betti: "Betti-0: 1 | Betti-1: 0 (Stable Support)"
  },
  {
    name: "SENSEX 82700 CE",
    tag: "GAMMA PEAK",
    pos: [6.5, 4.6, 3.5],
    color: 0xf43f5e,
    phase: "RESISTANCE WALL",
    cluster: "Call Open Interest Wall",
    ofi: "-1.10M Call Writing Inflow",
    vol: "16.2% IV · Gamma +0.35",
    pain: "Target Ceiling (+4.0% Maker Zone)",
    betti: "Betti-0: 2 | Betti-1: 1 (Branching Cluster)"
  },
  {
    name: "PAIN ZONE UPPER",
    tag: "VWAP +8.0 BPS",
    pos: [2.5, 2.8, -5.0],
    color: 0xa855f7,
    phase: "BAND VETO LEVEL",
    cluster: "Absolute Pain Zone Boundary",
    ofi: "Equilibrium Crossover Threshold",
    vol: "14.2% IV · Baseline Squeeze",
    pain: "CALL Trades Unlocked Above This Line",
    betti: "Boundary Cycle Invariant"
  },
  {
    name: "PAIN ZONE LOWER",
    tag: "VWAP -8.0 BPS",
    pos: [-3.0, 1.2, 5.0],
    color: 0xf59e0b,
    phase: "BAND VETO LEVEL",
    cluster: "Absolute Pain Zone Boundary",
    ofi: "Short Crossover Threshold",
    vol: "14.5% IV · Baseline Squeeze",
    pain: "PUT Trades Unlocked Below This Line",
    betti: "Boundary Cycle Invariant"
  }
];

function init3DManifold() {
  if (!container) return;
  const width = container.clientWidth;
  const height = container.clientHeight;

  scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(0x000000, 0.025);

  camera = new THREE.PerspectiveCamera(45, width / height, 0.1, 100);
  camera.position.set(0, 16, 24);

  renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setSize(width, height);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  container.appendChild(renderer.domElement);

  controls = new THREE.OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.05;
  controls.maxPolarAngle = Math.PI / 2.05;
  controls.autoRotate = isAutoRotating;
  controls.autoRotateSpeed = 0.9;

  // Matte Studio Lighting (Zero harsh arcade specular shine)
  const ambientLight = new THREE.AmbientLight(0xffffff, 0.85);
  scene.add(ambientLight);

  const softKeyLight = new THREE.DirectionalLight(0xffffff, 0.30);
  softKeyLight.position.set(10, 24, 10);
  scene.add(softKeyLight);

  createManifoldMesh();
  createInteractiveNodes();
  createSelectionPing();

  container.addEventListener('pointerdown', onManifoldPointerDown);
  container.addEventListener('pointermove', onManifoldPointerMove);
  window.addEventListener('resize', onWindowResize);

  animate();
}

let selectionPing = null;
let selectedNodeGroup = null;

function createSelectionPing() {
  const pingGeo = new THREE.RingGeometry(0.5, 0.9, 32);
  pingGeo.rotateX(-Math.PI / 2);
  const pingMat = new THREE.MeshBasicMaterial({
    color: 0x38bdf8,
    side: THREE.DoubleSide,
    transparent: true,
    opacity: 0.9
  });
  selectionPing = new THREE.Mesh(pingGeo, pingMat);
  selectionPing.visible = false;
  scene.add(selectionPing);
}

function createManifoldMesh() {
  if (mesh) scene.remove(mesh);
  if (wireframe) scene.remove(wireframe);

  const size = 26;
  const segments = 64;
  const geometry = new THREE.PlaneGeometry(size, size, segments, segments);
  geometry.rotateX(-Math.PI / 2);

  const pos = geometry.attributes.position;
  const count = pos.count;
  const colors = [];

  for (let i = 0; i < count; i++) {
    const x = pos.getX(i);
    const z = pos.getZ(i);
    let y = 0;

    if (currentMode === 'topology') {
      const hub1 = 5.2 * Math.exp(-((x - 0.2) * (x - 0.2) + (z - 0.2) * (z - 0.2)) / 12);
      const hub2 = 4.0 * Math.exp(-((x + 6.5) * (x + 6.5) + (z + 4.0) * (z + 4.0)) / 14);
      const hub3 = 4.6 * Math.exp(-((x - 6.5) * (x - 6.5) + (z - 3.5) * (z - 3.5)) / 16);
      const valley = -2.2 * Math.exp(-((x) * (x) + (z + 4.5) * (z + 4.5)) / 18);
      const ripple = 0.35 * Math.sin(x * 1.1) * Math.cos(z * 1.1);
      y = hub1 + hub2 + hub3 + valley + ripple;
    } else if (currentMode === 'volatility') {
      y = 4.5 * Math.exp(-(x * x) / 10) * Math.cos(z * 0.45) + 1.8 * Math.sin(z * 0.7);
    } else {
      y = 3.6 * Math.tanh(x * 0.28) * Math.cos(z * 0.22) + Math.sin((x + z) * 0.35) * 0.8;
    }

    pos.setY(i, y);

    // Muted institutional color palette: Deep Obsidian -> Cyan -> Emerald -> Ochre -> Brick Red
    const norm = (y + 2.5) / 8.5;
    const color = new THREE.Color();
    if (norm < 0.22) {
      color.setRGB(0.04, 0.08, 0.14); // Deep Slate Navy
    } else if (norm < 0.48) {
      color.setRGB(0.05, 0.55, 0.75); // Muted Cyan
    } else if (norm < 0.72) {
      color.setRGB(0.08, 0.65, 0.42); // Institutional Forest Green
    } else if (norm < 0.88) {
      color.setRGB(0.85, 0.60, 0.15); // Muted Ochre
    } else {
      color.setRGB(0.88, 0.25, 0.30); // Brick Red Peak
    }
    colors.push(color.r, color.g, color.b);
  }

  geometry.setAttribute('color', new THREE.Float32BufferAttribute(colors, 3));
  geometry.computeVertexNormals();

  // Ultra-Matte Institutional Material: zero specular glare, zero plastic shine
  const material = new THREE.MeshStandardMaterial({
    vertexColors: true,
    roughness: 0.92,
    metalness: 0.02,
    side: THREE.DoubleSide
  });

  mesh = new THREE.Mesh(geometry, material);
  scene.add(mesh);

  // Subtle Muted Hairline Grid (Charcoal)
  const wireMat = new THREE.MeshBasicMaterial({
    color: 0x444444,
    wireframe: true,
    transparent: true,
    opacity: 0.08
  });
  wireframe = new THREE.Mesh(geometry, wireMat);
  wireframe.position.y += 0.02;
  scene.add(wireframe);
}

function createInteractiveNodes() {
  nodeMarkers.forEach(m => scene.remove(m));
  nodeMarkers = [];

  const sphereGeo = new THREE.SphereGeometry(0.38, 20, 20);
  const ringGeo = new THREE.RingGeometry(0.55, 0.75, 32);
  ringGeo.rotateX(-Math.PI / 2);

  STRIKE_NODES.forEach((sn, idx) => {
    const group = new THREE.Group();
    group.position.set(sn.pos[0], sn.pos[1], sn.pos[2]);

    // Solid matte marker sphere
    const sMat = new THREE.MeshBasicMaterial({
      color: sn.color
    });
    const sphere = new THREE.Mesh(sphereGeo, sMat);
    group.add(sphere);

    // Baseline calm ring
    const rMat = new THREE.MeshBasicMaterial({
      color: sn.color,
      side: THREE.DoubleSide,
      transparent: true,
      opacity: 0.35
    });
    const ring = new THREE.Mesh(ringGeo, rMat);
    ring.position.y = -0.15;
    group.add(ring);

    group.userData = sn;
    scene.add(group);
    nodeMarkers.push(group);
  });

  // Select the first node by default and position ping
  if (nodeMarkers.length > 0) {
    selectedNodeGroup = nodeMarkers[0];
  }
}

function setManifoldMode(mode) {
  currentMode = mode;
  document.querySelectorAll('.manifold-controls .control-btn').forEach(b => {
    if (b.id && b.id.startsWith('btnMode')) b.classList.remove('active');
  });
  if (mode === 'topology') document.getElementById('btnModeTopology').classList.add('active');
  if (mode === 'volatility') document.getElementById('btnModeVol').classList.add('active');
  if (mode === 'pain_zone') document.getElementById('btnModePain').classList.add('active');

  createManifoldMesh();
}

function toggleAutoRotate() {
  isAutoRotating = !isAutoRotating;
  controls.autoRotate = isAutoRotating;
  const btn = document.getElementById('btnToggleRotate');
  if (btn) {
    btn.innerText = `Rotate: ${isAutoRotating ? 'ON' : 'OFF'}`;
    btn.style.color = isAutoRotating ? 'var(--cyan)' : 'var(--text-secondary)';
  }
}

function resetManifoldCamera() {
  controls.reset();
  camera.position.set(0, 16, 24);
  controls.target.set(0, 0, 0);
  controls.update();
  // Reset HUD to default
  updateHud(STRIKE_NODES[0]);
}

function onManifoldPointerDown(e) {
  const rect = container.getBoundingClientRect();
  mouse.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
  mouse.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;

  raycaster.setFromCamera(mouse, camera);

  // Check interactive node markers first
  const markerMeshes = [];
  nodeMarkers.forEach(grp => {
    grp.children.forEach(c => {
      c.userData = grp.userData;
      c._group = grp;
      markerMeshes.push(c);
    });
  });

  const intersects = raycaster.intersectObjects(markerMeshes);
  if (intersects.length > 0) {
    const hitObj = intersects[0].object;
    const grp = hitObj._group || hitObj.parent;
    selectedNodeGroup = grp;
    const data = grp.userData || hitObj.userData;
    updateHud(data);
    
    // Position selection radar ping on the clicked node
    if (selectionPing) {
      selectionPing.position.copy(grp.position);
      selectionPing.position.y += 0.05;
      selectionPing.visible = true;
    }

    // Smooth camera refocus on clicked node
    const targetPos = new THREE.Vector3(...data.pos);
    controls.target.lerp(targetPos, 0.5);
    return;
  }

  // If clicked directly on the manifold mesh
  if (mesh) {
    const meshHits = raycaster.intersectObject(mesh);
    if (meshHits.length > 0) {
      const p = meshHits[0].point;
      selectedNodeGroup = null;
      if (selectionPing) {
        selectionPing.position.copy(p);
        selectionPing.position.y += 0.05;
        selectionPing.visible = true;
      }
      updateHudCustom({
        name: `COORDINATE [X:${p.x.toFixed(1)}, Z:${p.z.toFixed(1)}]`,
        tag: `Y: ${p.y.toFixed(2)}`,
        phase: p.y > 3.0 ? "HIGH ELEVATION HUB" : (p.y < 0 ? "ABSORPTION VALLEY" : "EQUILIBRIUM REGION"),
        cluster: `Persistence Elevation: ${p.y.toFixed(2)}σ`,
        ofi: p.x > 0 ? "Call Delta Pressure (+)" : "Put Delta Absorption (-)",
        vol: `${(14.0 + Math.abs(p.y) * 0.8).toFixed(1)}% Localized IV`,
        pain: p.y > 2.0 ? "Above VWAP Band (Call Territory)" : "Below VWAP Band (Put Territory)",
        betti: `Betti-0: 1 | Curvature: ${(p.y * 0.2).toFixed(2)}`
      });
    }
  }
}

function onManifoldPointerMove(e) {
  const rect = container.getBoundingClientRect();
  mouse.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
  mouse.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
}

function updateHud(sn) {
  if (!sn) return;
  document.getElementById('hudStrikeName').innerText = sn.name;
  document.getElementById('hudPhaseBadge').innerText = sn.tag;
  document.getElementById('hudCluster').innerText = sn.cluster;
  document.getElementById('hudOfi').innerText = sn.ofi;
  document.getElementById('hudVol').innerText = sn.vol;
  document.getElementById('hudPain').innerText = sn.pain;
  document.getElementById('hudBetti').innerText = sn.betti;
}

function updateHudCustom(sn) {
  document.getElementById('hudStrikeName').innerText = sn.name;
  document.getElementById('hudPhaseBadge').innerText = sn.tag;
  document.getElementById('hudCluster').innerText = sn.cluster;
  document.getElementById('hudOfi').innerText = sn.ofi;
  document.getElementById('hudVol').innerText = sn.vol;
  document.getElementById('hudPain').innerText = sn.pain;
  document.getElementById('hudBetti').innerText = sn.betti;
}

function onWindowResize() {
  if (!container || !renderer || !camera) return;
  const width = container.clientWidth;
  const height = container.clientHeight;
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
  renderer.setSize(width, height);
}

function animate() {
  requestAnimationFrame(animate);
  const time = performance.now() * 0.001;

  if (mesh) {
    mesh.position.y = Math.sin(time * 0.8) * 0.08;
    if (wireframe) wireframe.position.y = mesh.position.y + 0.02;
  }

  // Active clicked node radar ping expansion
  if (selectionPing && selectionPing.visible) {
    const cycle = (time * 2.2) % 1.0;
    const pingScale = 1.0 + cycle * 1.6;
    selectionPing.scale.set(pingScale, pingScale, pingScale);
    selectionPing.material.opacity = Math.max(0, 0.9 * (1.0 - cycle));
  }

  // Blinking pulse directly on the selected node
  nodeMarkers.forEach(grp => {
    const sphere = grp.children[0];
    const ring = grp.children[1];
    if (grp === selectedNodeGroup) {
      if (sphere) {
        // Active selected node blinks with rhythmic expansion
        const pulse = 1.0 + 0.28 * Math.sin(time * 7.0);
        sphere.scale.set(pulse, pulse, pulse);
      }
      if (ring) {
        ring.scale.set(1.4, 1.4, 1.4);
      }
    } else {
      if (sphere) sphere.scale.set(1.0, 1.0, 1.0);
      if (ring) {
        const calmScale = 1.0 + 0.08 * Math.sin(time * 1.8);
        ring.scale.set(calmScale, calmScale, calmScale);
      }
    }
  });

  controls.update();
  renderer.render(scene, camera);
}

// ─── DYNAMIC IST MARKET HOURS STATUS LOGIC ───
function getMarketStatus() {
  const now = new Date();
  // Get time in Asia/Kolkata timezone
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

  // Weekday (Monday to Friday)
  // Pre-market: before 09:15 IST (555 mins)
  if (mins < 555) {
    return {
      text: "PRE-MARKET · SYSTEM READY",
      type: "pre"
    };
  }
  // Live Trading Hours: 09:15 to 15:08 IST (908 mins)
  if (mins >= 555 && mins < 908) {
    return {
      text: "MARKET ACTIVE · LIVE EXECUTION",
      type: "live"
    };
  }
  // CAS Flush & Settlement: 15:08 to 15:30 IST (930 mins)
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

// ─── TELEMETRY POLLING & UI POPULATION ───
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
  // 1. Clock & Market Status
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

  // Tick Age
  if (data.tick_age_sec !== undefined) {
    document.getElementById('tickAgeLabel').innerText = `Tick Age: ${Number(data.tick_age_sec).toFixed(1)}s`;
  }

  // 2. KPIs
  const stats = data.today_stats || {};
  const netRs = stats.net_pnl_rs !== undefined ? stats.net_pnl_rs : (stats.pnl_rs !== undefined ? stats.pnl_rs : 0.0);
  const grossRs = stats.gross_pnl_rs !== undefined ? stats.gross_pnl_rs : 0.0;
  const chargesRs = stats.charges_rs !== undefined ? stats.charges_rs : Math.max(0, grossRs - netRs);
  const count = stats.trades_count !== undefined ? stats.trades_count : (stats.count !== undefined ? stats.count : (stats.today_trades ? stats.today_trades.length : 0));
  const profits = stats.profits !== undefined ? stats.profits : (stats.wins !== undefined ? stats.wins : 0);
  const losses = stats.losses !== undefined ? stats.losses : Math.max(0, count - profits);
  const pr = stats.profit_rate_pct !== undefined ? stats.profit_rate_pct : (stats.win_rate_pct !== undefined ? stats.win_rate_pct : (count > 0 ? (profits / count * 100.0) : 0.0));

  // Today Gross PnL
  const grossEl = document.getElementById('kpiGrossPnl');
  if (grossEl) {
    grossEl.innerText = (grossRs >= 0 ? "+₹" : "₹") + Number(grossRs).toFixed(2);
    grossEl.style.color = grossRs >= 0 ? "var(--green-bright)" : "var(--red-bright)";
  }
  const grossSub = document.getElementById('kpiGrossSub');
  if (grossSub) {
    grossSub.innerText = `Total Charges: ₹${Number(chargesRs).toFixed(2)}`;
  }

  // Today Net PnL
  const netEl = document.getElementById('kpiNetPnl');
  if (netEl) {
    netEl.innerText = (netRs >= 0 ? "+₹" : "₹") + Number(netRs).toFixed(2);
    netEl.style.color = netRs >= 0 ? "var(--green-bright)" : "var(--red-bright)";
  }
  const netSub = document.getElementById('kpiNetSub');
  if (netSub) {
    if (count === 0) {
      netSub.innerText = "0 Trades Today";
    } else {
      netSub.innerText = `${profits} Profit / ${losses} Loss (${Number(pr).toFixed(0)}% Day)`;
    }
  }

  const wrEl = document.getElementById('kpiWinRate');
  if (wrEl) wrEl.innerText = Number(pr).toFixed(1) + "%";

  const countEl = document.getElementById('tradeCountLabel');
  if (countEl) countEl.innerText = `${count} Execution${count === 1 ? '' : 's'} Today (${profits} Profit / ${losses} Loss)`;

  // Available Funds
  if (data.aegis && data.aegis.available_funds) {
    document.getElementById('kpiFunds').innerText = "₹" + Number(data.aegis.available_funds).toFixed(2);
  }

  // 3. Trades
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

      let setupDisp = String(t.pillar || 'Pillar 1 (20 EMA)');
      if (setupDisp.includes('topological') || setupDisp.includes('BOS') || setupDisp.includes('Prominence') || setupDisp === 'sr_breakout') {
        setupDisp = 'Pillar 4 (S/R Breakout)';
      }

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
        <td data-label="Setup" class="mono" style="color:var(--purple); font-size:11px;">${setupDisp}</td>
      `;
      tbody.appendChild(tr);
    });
  }

  // 4. Logs
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

// Boot Engine & Telemetry Poller
window.addEventListener('DOMContentLoaded', () => {
  init3DManifold();
  pollTelemetry();
  setInterval(pollTelemetry, 2000);
});
</script>

</body>
</html>
"""
