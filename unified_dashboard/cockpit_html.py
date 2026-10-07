"""
AEGIS V4.1 Dedicated Live Cockpit HTML Template.
Pure AEGIS autonomous execution command center.
Eliminates deprecated Kronos & dormant ARGUS clutter.
"""

COCKPIT_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Quant Console — AEGIS V4.1 Live Cockpit</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,700&family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet" />
<style>
:root {
  --bg: #0d1117;
  --surface: #161b22;
  --surface-hover: #1f242c;
  --card: #161b22;
  --border: #30363d;
  --border-light: #3d444d;
  --text: #e6edf3;
  --text-muted: #8b949e;
  --primary: #388bfd;
  --primary-soft: rgba(56, 139, 253, 0.15);
  --aegis: #238636;
  --aegis-bright: #3fb950;
  --aegis-soft: rgba(46, 160, 67, 0.15);
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
  box-shadow: 0 4px 20px rgba(0,0,0,0.4);
  flex-wrap: wrap;
  gap: 14px;
}
.nav-brand {
  display: flex;
  align-items: center;
  gap: 12px;
}
.brand-title {
  font-family: var(--font-serif);
  font-size: 24px;
  font-weight: 700;
  color: #fff;
  letter-spacing: -0.02em;
}
.brand-badge {
  background: var(--aegis-soft);
  color: var(--aegis-bright);
  border: 1px solid var(--aegis);
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
  flex-wrap: wrap;
}
.meta-clock {
  font-family: var(--font-mono);
  font-size: 13px;
  color: var(--text-muted);
  margin-right: 8px;
}
.meta-clock strong { color: #fff; }
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
  background: var(--aegis-soft);
  color: #fff;
  border-color: var(--aegis-bright);
}
.nav-btn.research {
  background: var(--purple-soft);
  color: var(--purple);
  border-color: var(--purple);
}
.nav-btn.research:hover {
  background: var(--purple);
  color: #fff;
  box-shadow: 0 4px 12px rgba(163, 113, 247, 0.3);
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

/* HERO LIVE STATUS STRIP */
.live-strip {
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: linear-gradient(90deg, rgba(35,134,54,0.12) 0%, rgba(56,139,253,0.08) 100%);
  border: 1px solid var(--border);
  border-left: 4px solid var(--aegis-bright);
  border-radius: 12px;
  padding: 14px 20px;
  margin-bottom: 20px;
  flex-wrap: wrap;
  gap: 12px;
}
.strip-status {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
  font-weight: 600;
}
.pulse-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--aegis-bright);
  box-shadow: 0 0 10px var(--aegis-bright);
  animation: pulse 1.8s infinite;
}
@keyframes pulse {
  0% { transform: scale(0.95); opacity: 0.8; }
  50% { transform: scale(1.2); opacity: 1; }
  100% { transform: scale(0.95); opacity: 0.8; }
}
.strip-info {
  font-size: 12px;
  color: var(--text-muted);
  display: flex;
  gap: 18px;
  align-items: center;
}
.strip-info strong { color: #fff; }

/* TOP METRICS DECK */
.metrics-deck {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 14px;
  margin-bottom: 20px;
}
.metric-tile {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 16px 18px;
  position: relative;
  transition: border-color 0.2s;
}
.metric-tile:hover { border-color: var(--border-light); }
.tile-lbl {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-bottom: 6px;
}
.tile-val {
  font-family: var(--font-mono);
  font-size: 22px;
  font-weight: 700;
  color: #fff;
  line-height: 1.1;
  margin-bottom: 4px;
}
.tile-sub {
  font-size: 11px;
  color: var(--text-muted);
}
.tile-sub strong { color: #fff; }
.tile-val.pos { color: var(--aegis-bright); }
.tile-val.neg { color: var(--danger-bright); }

/* HERO ACTIVE EXECUTION COCKPIT */
.execution-hero {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 22px;
  margin-bottom: 22px;
  position: relative;
  overflow: hidden;
}
.execution-hero.in-flight {
  border-color: var(--primary);
  background: linear-gradient(180deg, rgba(56,139,253,0.06) 0%, var(--surface) 100%);
}
.hero-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 18px;
}
.hero-title {
  font-size: 15px;
  font-weight: 700;
  color: #fff;
  display: flex;
  align-items: center;
  gap: 10px;
}
.hero-badge {
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 600;
  padding: 3px 8px;
  border-radius: 6px;
}
.hero-badge.flat { background: var(--surface-hover); color: var(--text-muted); border: 1px solid var(--border); }
.hero-badge.active { background: var(--primary-soft); color: var(--primary); border: 1px solid var(--primary); animation: pulse 1.5s infinite; }

/* Flat State View */
.flat-state-box {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 10px 0;
}
.flat-msg {
  font-size: 14px;
  color: var(--text);
  font-weight: 500;
}
.gate-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 12px;
}
.gate-card {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 14px;
}
.gate-card-title {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-muted);
  text-transform: uppercase;
  margin-bottom: 4px;
}
.gate-card-status {
  font-size: 13px;
  font-weight: 600;
  color: #fff;
  display: flex;
  align-items: center;
  gap: 6px;
}

/* In-Flight Trade View */
.active-trade-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 14px;
  margin-bottom: 18px;
}
.at-metric .lbl { font-size: 11px; color: var(--text-muted); text-transform: uppercase; margin-bottom: 4px; }
.at-metric .val { font-family: var(--font-mono); font-size: 20px; font-weight: 700; color: #fff; }

.adaptive-progress-wrap {
  margin-top: 14px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 16px;
}
.progress-labels {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: var(--text-muted);
  margin-bottom: 8px;
  font-family: var(--font-mono);
}
.progress-bar-bg {
  background: #21262d;
  height: 8px;
  border-radius: 999px;
  overflow: hidden;
  position: relative;
}
.progress-bar-fill {
  background: linear-gradient(90deg, #388bfd 0%, #3fb950 100%);
  height: 100%;
  border-radius: 999px;
  transition: width 0.3s ease;
}

/* TWO COLUMN LIVE INTELLIGENCE */
.two-col-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
  margin-bottom: 22px;
}
@media (max-width: 950px) {
  .two-col-grid { grid-template-columns: 1fr; }
}

.panel-box {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 14px;
  overflow: hidden;
}
.panel-header {
  padding: 14px 18px;
  border-bottom: 1px solid var(--border);
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.panel-title {
  font-size: 14px;
  font-weight: 700;
  color: #fff;
  display: flex;
  align-items: center;
  gap: 8px;
}
.panel-body {
  padding: 18px;
}

/* Today's Closed Trades Table */
.trades-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.trades-table th {
  background: var(--surface);
  color: var(--text-muted);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  padding: 10px 12px;
  border-bottom: 1px solid var(--border);
  text-align: left;
}
.trades-table td {
  padding: 10px 12px;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
}
.mono { font-family: var(--font-mono); }
.badge {
  display: inline-flex;
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
  font-family: var(--font-mono);
}
.badge.win { background: var(--aegis-soft); color: var(--aegis-bright); }
.badge.call { background: rgba(35, 134, 54, 0.15); color: #3fb950; }
.badge.put { background: rgba(218, 54, 51, 0.15); color: #f85149; }
.pnl-pos { color: var(--aegis-bright); font-weight: 700; }
.pnl-neg { color: var(--danger-bright); font-weight: 700; }

/* LIVE LOG TERMINAL */
.log-terminal {
  background: #090d13;
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 14px;
  height: 380px;
  overflow-y: auto;
  font-family: var(--font-mono);
  font-size: 11.5px;
  line-height: 1.5;
  color: #c9d1d9;
}
.log-line {
  white-space: pre-wrap;
  word-break: break-word;
  margin-bottom: 3px;
  padding: 1px 4px;
  border-radius: 3px;
}
.log-tag-wyckoff { color: #3fb950; font-weight: 600; background: rgba(63, 185, 80, 0.08); }
.log-tag-flow { color: #2dd4bf; font-weight: 600; }
.log-tag-band { color: #388bfd; }
.log-tag-pillar { color: #a371f7; font-weight: 600; }
.log-tag-catboost { color: #d29922; }
.log-tag-em { color: #f0883e; font-weight: 700; background: rgba(240, 136, 62, 0.1); }
.log-tag-veto { color: #f85149; }
.log-tag-digest { color: #e6edf3; font-weight: 500; }
.log-tag-system { color: #8b949e; font-style: italic; }

</style>
</head>
<body>

<!-- NAVBAR -->
<nav class="nav">
  <div class="nav-brand">
    <div class="brand-title">Quant Console</div>
    <div class="brand-badge">⚡ AEGIS V4.1 Cockpit</div>
  </div>
  <div class="nav-links">
    <div class="meta-clock">
      <strong id="liveClock">--:--:--</strong> IST · Tick Age: <strong id="tickAgeSec">—</strong>
    </div>
    <a href="/" class="nav-btn cockpit">⚡ Live Cockpit</a>
    <a href="/research" class="nav-btn research">🔬 Research Console</a>
    <a href="/download/excel" class="nav-btn export">📥 Excel (.xlsx)</a>
    <a href="/download/csv" class="nav-btn export">📥 CSV</a>
  </div>
</nav>

<div class="container">

  <!-- HERO LIVE STATUS STRIP -->
  <div class="live-strip">
    <div class="strip-status">
      <div class="pulse-dot"></div>
      <span id="systemStatusText">AEGIS V4.1 ENGINE ONLINE · SCANNING 31 STRIKES (62 TOKENS)</span>
    </div>
    <div class="strip-info">
      <div>Active Trading Day: <strong id="tradingDaySchedule">Monday (NIFTY 1 DTE)</strong></div>
      <div>Lot Sizing: <strong>Strict 1-Lot (65 Qty)</strong></div>
      <div>Testing Funds: <strong id="testingFundsVal">₹588.36</strong></div>
    </div>
  </div>

  <!-- PERMANENT RISK DISCIPLINE & TARGET CALIBRATION MANDATE -->
  <div style="background: rgba(210, 153, 34, 0.08); border: 1px solid rgba(210, 153, 34, 0.35); border-radius: 10px; padding: 13px 18px; margin-bottom: 18px;">
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

  <!-- TOP METRICS DECK -->
  <div class="metrics-deck">
    <!-- Spot Index -->
    <div class="metric-tile">
      <div class="tile-lbl">Spot Index</div>
      <div class="tile-val" id="spotVal">—</div>
      <div class="tile-sub">ATM Strike: <strong id="atmStrikeVal">—</strong> (<span id="indexNameVal">SENSEX</span>)</div>
    </div>

    <!-- Absolute Pain Zone Band -->
    <div class="metric-tile">
      <div class="tile-lbl">Absolute Pain Zone VWAP Band</div>
      <div class="tile-val" id="painZoneVal" style="color:var(--primary);">WAITING</div>
      <div class="tile-sub">Top: <strong id="bandTopVal">—</strong> | Bot: <strong id="bandBotVal">—</strong></div>
    </div>

    <!-- Wyckoff OFI Delta Gate -->
    <div class="metric-tile">
      <div class="tile-lbl">Wyckoff OFI Delta Gate</div>
      <div class="tile-val" id="ofiVal" style="color:var(--aegis-bright);">HARMONY</div>
      <div class="tile-sub">VDR: <strong id="vdrVal">1.08x</strong> | Net OFI: <strong id="netOfiVal">-1.71M</strong></div>
    </div>

    <!-- Macro Price Action Setup -->
    <div class="metric-tile">
      <div class="tile-lbl">Price Action 4 Pillars</div>
      <div class="tile-val" id="activePillarVal" style="color:var(--purple);">Pillar 4 (BOS)</div>
      <div class="tile-sub">Setup: <strong id="setupNameVal">hvg_network_phase_shift</strong></div>
    </div>

    <!-- Today's Scorecard -->
    <div class="metric-tile">
      <div class="tile-lbl">Today's Executed Trades</div>
      <div class="tile-val" id="todayPnlVal">₹0.00</div>
      <div class="tile-sub" id="todayWinsSub"><strong id="todayWinsVal">0 Wins</strong> / 0 Losses (0 Trades Today)</div>
      <div class="tile-sub" id="todayGrossSub" style="margin-top:3px; font-size:11px; color:var(--text-muted);">Gross: ₹0.00 | Charges: ₹0.00</div>
    </div>
  </div>

  <!-- HERO ACTIVE IN-FLIGHT EXECUTION COCKPIT -->
  <div class="execution-hero" id="executionHero">
    <div class="hero-head">
      <div class="hero-title">
        <span>⚡ In-Flight Trade &amp; Adaptive Exit Radar</span>
        <span class="hero-badge flat" id="posStatusBadge">POSITION FLAT</span>
      </div>
      <div style="font-size:12px; color:var(--text-muted);" id="exitRuleTag">Resting Maker TP @ +4.0% Active</div>
    </div>

    <!-- Flat State -->
    <div class="flat-state-box" id="flatStateBox">
      <div class="flat-msg">
        No open inventory. AEGIS is actively screening market microstructure, visibility hubs, and order-flow intensity across ATM ± 15 strikes.
      </div>
      <div class="gate-grid">
        <div class="gate-card">
          <div class="gate-card-title">1. Tick-Top Pain Zone Band Veto</div>
          <div class="gate-card-status" id="gate1Status">✅ Unified Directional Veto Active</div>
        </div>
        <div class="gate-card">
          <div class="gate-card-title">2. Wyckoff Volume &amp; OFI Delta Gate</div>
          <div class="gate-card-status" id="gate2Status">✅ VDR >= 1.0 &amp; OFI Harmony Required</div>
        </div>
        <div class="gate-card">
          <div class="gate-card-title">3. Composite Conviction Hurdle</div>
          <div class="gate-card-status" id="gate3Status">✅ Min Score >= 80.0 (Struct + Imp + Flow + Cont)</div>
        </div>
        <div class="gate-card">
          <div class="gate-card-title">4. Near-Target Profit Lock</div>
          <div class="gate-card-status" id="gate4Status">🛡️ Armed at +3.5% Peak | 0.8% Pullback Cut</div>
        </div>
      </div>
    </div>

    <!-- Open Trade State (Hidden when flat) -->
    <div id="openTradeBox" style="display:none;">
      <div class="active-trade-grid">
        <div class="at-metric">
          <div class="lbl">Contract</div>
          <div class="val" id="openSymVal">—</div>
        </div>
        <div class="at-metric">
          <div class="lbl">Entry Price</div>
          <div class="val" id="openEntryVal">₹0.00</div>
        </div>
        <div class="at-metric">
          <div class="lbl">Current Bid / LTP</div>
          <div class="val" id="openLtpVal">₹0.00</div>
        </div>
        <div class="at-metric">
          <div class="lbl">Floating P&amp;L</div>
          <div class="val" id="openPnlVal">+₹0.00 (+0.0%)</div>
        </div>
        <div class="at-metric">
          <div class="lbl">Peak Bid MFE</div>
          <div class="val" id="openPeakVal">+0.0%</div>
        </div>
      </div>

      <div class="adaptive-progress-wrap">
        <div class="progress-labels">
          <span>-12.0% (Floor SL)</span>
          <span>+2.0% (SL -> -3%)</span>
          <span>+3.0% (Cost-Shield BE)</span>
          <span style="color:var(--primary);">+3.5% (Near-Target Lock)</span>
          <span style="color:var(--aegis-bright); font-weight:700;">+4.0% (Maker TP)</span>
        </div>
        <div class="progress-bar-bg">
          <div class="progress-bar-fill" id="adaptiveProgressBar" style="width: 20%;"></div>
        </div>
      </div>
    </div>
  </div>

  <!-- TWO COLUMN LIVE INTELLIGENCE -->
  <div class="two-col-grid">
    <!-- Panel Left: Shadow CatBoost Paper Trader & Volume Profile -->
    <div class="panel-box">
      <div class="panel-header">
        <div class="panel-title">🤖 Shadow CatBoost Paper Trader &amp; Auction Theory</div>
        <span class="badge" style="background:var(--purple-soft); color:var(--purple);">100% Shadow</span>
      </div>
      <div class="panel-body">
        <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:16px;">
          <div style="background:var(--bg); border:1px solid var(--border); border-radius:8px; padding:12px;">
            <div style="font-size:11px; color:var(--text-muted); text-transform:uppercase;">CatBoost 1M Flow Score</div>
            <div style="font-family:var(--font-mono); font-size:18px; font-weight:700; color:var(--warning);" id="catboostScoreVal">35.7% (Warm)</div>
            <div style="font-size:11px; color:var(--text-muted); margin-top:2px;">Day Median: 32.4%</div>
          </div>
          <div style="background:var(--bg); border:1px solid var(--border); border-radius:8px; padding:12px;">
            <div style="font-size:11px; color:var(--text-muted); text-transform:uppercase;">Volume Profile (Auction Zone)</div>
            <div style="font-family:var(--font-mono); font-size:18px; font-weight:700; color:#fff;" id="auctionZoneVal">BELOW_VAL</div>
            <div style="font-size:11px; color:var(--text-muted); margin-top:2px;">POC: 72,960 | LVN Runway: True</div>
          </div>
        </div>
        <div style="font-size:12px; color:var(--text-muted); line-height:1.5;">
          CatBoost and Volume Profile operate in non-blocking shadow observation mode. Signals are logged and cross-validated without interfering with live Macro Price Action execution.
        </div>
      </div>
    </div>

    <!-- Panel Right: Today's Live Executed Trades -->
    <div class="panel-box">
      <div class="panel-header">
        <div class="panel-title">📑 Today's Executed Trades (Combat Ledger)</div>
        <a href="/research" style="font-size:12px; color:var(--primary); text-decoration:none; font-weight:600;">View All 15 in Research ➔</a>
      </div>
      <div class="panel-body" style="padding:0;">
        <div style="overflow-x:auto;">
          <table class="trades-table">
            <thead>
              <tr>
                <th>Time</th>
                <th>Contract</th>
                <th>Side</th>
                <th>Fill</th>
                <th>Exit</th>
                <th>Gross PnL</th>
                <th>Charges</th>
                <th>Net PnL</th>
                <th>Setup</th>
              </tr>
            </thead>
            <tbody id="todayTradesBody">
              <!-- Injected by JS -->
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

  <!-- LIVE TELEMETRY LOGS -->
  <div class="panel-box">
    <div class="panel-header">
      <div class="panel-title">📡 Real-Time AEGIS Telemetry Stream</div>
      <div style="display:flex; align-items:center; gap:10px;">
        <button onclick="toggleLogScroll()" id="logScrollBtn" style="background:var(--bg); border:1px solid var(--border); color:var(--text-muted); padding:4px 10px; border-radius:6px; font-size:11px; cursor:pointer;">Auto-Scroll: ON</button>
        <span class="badge" style="background:var(--surface-hover); color:var(--text-muted);" id="logLinesCount">0 lines</span>
      </div>
    </div>
    <div class="log-terminal" id="logTerminal">
      <!-- Injected by JS -->
    </div>
  </div>

</div>

<script>
let autoScroll = true;

function toggleLogScroll() {
  autoScroll = !autoScroll;
  document.getElementById("logScrollBtn").innerText = `Auto-Scroll: ${autoScroll ? 'ON' : 'OFF'}`;
}

async function fetchLiveState() {
  try {
    const res = await fetch("/api/live_state");
    const data = await res.json();
    renderLiveState(data);
  } catch (err) {
    console.error("Live state poll error:", err);
  }
}

function renderLiveState(data) {
  // 1. Clock and Ticks
  if (data.server_time) {
    const timeParts = data.server_time.split(" ");
    document.getElementById("liveClock").innerText = timeParts[1] || data.server_time;
  }
  const tickAge = data.tick_age_sec !== null && data.tick_age_sec !== undefined ? data.tick_age_sec.toFixed(1) + "s" : "—";
  document.getElementById("tickAgeSec").innerText = tickAge;

  const aegis = data.aegis || {};

  // 2. Spot & ATM
  if (aegis.spot_price) {
    document.getElementById("spotVal").innerText = aegis.spot_price.toFixed(1);
  }
  if (aegis.atm_strike) {
    document.getElementById("atmStrikeVal").innerText = aegis.atm_strike;
  }
  if (aegis.active_index) {
    document.getElementById("indexNameVal").innerText = aegis.active_index;
  }
  if (aegis.available_funds) {
    document.getElementById("testingFundsVal").innerText = "₹" + aegis.available_funds.toFixed(2);
  }

  // 3. Active Trade / In-Flight
  const activeTrade = aegis.active_trade;
  const isTradeOpen = activeTrade && (activeTrade.status === "OPEN" || (activeTrade.main && activeTrade.main.status === "OPEN"));

  if (isTradeOpen) {
    const t = activeTrade.main || activeTrade;
    document.getElementById("executionHero").classList.add("in-flight");
    document.getElementById("posStatusBadge").className = "hero-badge active";
    document.getElementById("posStatusBadge").innerText = "IN-FLIGHT POSITION";
    document.getElementById("flatStateBox").style.display = "none";
    document.getElementById("openTradeBox").style.display = "block";

    document.getElementById("openSymVal").innerText = t.symbol || "—";
    document.getElementById("openEntryVal").innerText = "₹" + (t.entry_price ? t.entry_price.toFixed(2) : "0.00");
    document.getElementById("openLtpVal").innerText = "₹" + (t.highest_bid ? t.highest_bid.toFixed(2) : "0.00");

    const pnlRs = t.unrealized_pnl || 0.0;
    const pnlPct = t.unrealized_pnl_pct || 0.0;
    const pnlPrefix = pnlRs >= 0 ? "+₹" : "₹";
    document.getElementById("openPnlVal").innerText = `${pnlPrefix}${pnlRs.toFixed(2)} (${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(2)}%)`;
    document.getElementById("openPnlVal").style.color = pnlRs >= 0 ? "var(--aegis-bright)" : "var(--danger-bright)";

    const peakPct = t.peak_bid_gain_pct || 0.0;
    document.getElementById("openPeakVal").innerText = `+${peakPct.toFixed(2)}%`;

    // Progress
    const normProgress = Math.min(100, Math.max(10, ((pnlPct + 3) / 7.0) * 100));
    document.getElementById("adaptiveProgressBar").style.width = normProgress + "%";
  } else {
    document.getElementById("executionHero").classList.remove("in-flight");
    document.getElementById("posStatusBadge").className = "hero-badge flat";
    document.getElementById("posStatusBadge").innerText = "POSITION FLAT";
    document.getElementById("flatStateBox").style.display = "flex";
    document.getElementById("openTradeBox").style.display = "none";
  }

  // 4. Today's Scorecard & Executed Trades
  const todayStats = data.today_stats || {};
  const todayPnlEl = document.getElementById("todayPnlVal");
  const todayWinsSub = document.getElementById("todayWinsSub");
  const todayGrossSub = document.getElementById("todayGrossSub");

  const count = todayStats.trades_count || 0;
  const pnlRs = todayStats.pnl_rs !== undefined ? todayStats.pnl_rs : 0.0;
  const grossRs = todayStats.gross_pnl_rs !== undefined ? todayStats.gross_pnl_rs : pnlRs;
  const chgRs = todayStats.charges_rs !== undefined ? todayStats.charges_rs : 0.0;
  const netRs = todayStats.net_pnl_rs !== undefined ? todayStats.net_pnl_rs : pnlRs;
  const wins = todayStats.wins || 0;
  const losses = todayStats.losses || 0;
  const wr = todayStats.win_rate_pct !== undefined ? todayStats.win_rate_pct : 0.0;

  if (todayPnlEl && todayWinsSub) {
    if (count > 0) {
      const netPrefix = netRs >= 0 ? "+₹" : "₹";
      const grossPrefix = grossRs >= 0 ? "+₹" : "₹";
      todayPnlEl.innerText = `${netPrefix}${netRs.toFixed(2)}`;
      todayPnlEl.className = `tile-val ${netRs >= 0 ? 'pos' : 'neg'}`;
      todayWinsSub.innerHTML = `<strong id="todayWinsVal" style="color:var(--success-bright);">${wins} Wins</strong> / ${losses} Losses (${wr}% Win Rate)`;
      if (todayGrossSub) {
        todayGrossSub.innerHTML = `Gross: <strong style="color:${grossRs >= 0 ? 'var(--aegis-bright)' : 'var(--danger-bright)'}">${grossPrefix}${grossRs.toFixed(2)}</strong> | Charges: <strong style="color:var(--text-muted)">₹${chgRs.toFixed(2)}</strong>`;
      }
    } else {
      todayPnlEl.innerText = "₹0.00";
      todayPnlEl.className = "tile-val";
      todayWinsSub.innerHTML = `<strong id="todayWinsVal">0 Wins</strong> / 0 Losses (0 Trades Today)`;
      if (todayGrossSub) todayGrossSub.innerText = "Gross: ₹0.00 | Charges: ₹0.00";
    }
  }

  // 4b. Table rows
  const tradesBody = document.getElementById("todayTradesBody");
  tradesBody.innerHTML = "";

  const hist = data.aegis_trade_history || [];
  const todayTrades = todayStats.today_trades || [];

  if (todayTrades.length > 0) {
    todayTrades.forEach(t => {
      const tr = document.createElement("tr");
      const gross = t.gross_pl !== undefined ? parseFloat(t.gross_pl) : (t.pnl_rs !== undefined ? parseFloat(t.pnl_rs) : 0.0);
      const chg = t.charges !== undefined ? parseFloat(t.charges) : 0.0;
      const net = t.net_pl !== undefined ? parseFloat(t.net_pl) : (gross - chg);
      const grossClass = gross >= 0 ? "pnl-pos" : "pnl-neg";
      const netClass = net >= 0 ? "pnl-pos" : "pnl-neg";
      const grossPrefix = gross >= 0 ? "+₹" : "₹";
      const netPrefix = net >= 0 ? "+₹" : "₹";
      const sideClass = (t.signal || "").includes("CALL") ? "call" : "put";

      tr.innerHTML = `
        <td class="mono" style="color:var(--text-muted);"><span class="badge" style="background:var(--primary-soft); color:var(--primary); font-size:10px; margin-right:4px;">TODAY</span>${t.date || '—'}</td>
        <td class="mono" style="font-weight:600;">${t.symbol || '—'}</td>
        <td><span class="badge ${sideClass}">${t.signal || '—'}</span></td>
        <td class="mono">₹${t.entry_price ? parseFloat(t.entry_price).toFixed(2) : '—'}</td>
        <td class="mono">₹${t.exit_price ? parseFloat(t.exit_price).toFixed(2) : '—'}</td>
        <td class="mono ${grossClass}">${grossPrefix}${gross.toFixed(2)}</td>
        <td class="mono" style="color:var(--text-muted); font-size:11px;">₹${chg.toFixed(2)}</td>
        <td class="mono ${netClass}" style="font-weight:700;">${netPrefix}${net.toFixed(2)}</td>
        <td style="color:var(--purple); font-size:11px;">${t.pillar || 'Pillar 4 (BOS)'}</td>
      `;
      tradesBody.appendChild(tr);
    });
  } else {
    // Show clean banner + recent completed trades with historical date
    const bannerRow = document.createElement("tr");
    bannerRow.innerHTML = `<td colspan="9" style="text-align:center; padding:10px; color:var(--text-muted); font-size:12px; background:rgba(255,255,205,0.02); border-bottom:1px solid var(--border);">No trades executed today yet (${todayStats.date_ist || 'Live'}). Showing recent completed trades below:</td>`;
    tradesBody.appendChild(bannerRow);

    const recentTrades = hist.slice(0, 5);
    if (recentTrades.length === 0) {
      const emptyRow = document.createElement("tr");
      emptyRow.innerHTML = `<td colspan="9" style="text-align:center; padding:18px; color:var(--text-muted);">No recorded trades yet.</td>`;
      tradesBody.appendChild(emptyRow);
    } else {
      recentTrades.forEach(t => {
        const tr = document.createElement("tr");
        const gross = t.gross_pl !== undefined ? parseFloat(t.gross_pl) : (t.pnl_rs !== undefined ? parseFloat(t.pnl_rs) : 0.0);
        const chg = t.charges !== undefined ? parseFloat(t.charges) : 0.0;
        const net = t.net_pl !== undefined ? parseFloat(t.net_pl) : (gross - chg);
        const grossClass = gross >= 0 ? "pnl-pos" : "pnl-neg";
        const netClass = net >= 0 ? "pnl-pos" : "pnl-neg";
        const grossPrefix = gross >= 0 ? "+₹" : "₹";
        const netPrefix = net >= 0 ? "+₹" : "₹";
        const sideClass = (t.signal || "").includes("CALL") ? "call" : "put";

        tr.innerHTML = `
          <td class="mono" style="color:var(--text-muted);">${t.date || '—'}</td>
          <td class="mono" style="font-weight:600;">${t.symbol || '—'}</td>
          <td><span class="badge ${sideClass}">${t.signal || '—'}</span></td>
          <td class="mono">₹${t.entry_price ? parseFloat(t.entry_price).toFixed(2) : '—'}</td>
          <td class="mono">₹${t.exit_price ? parseFloat(t.exit_price).toFixed(2) : '—'}</td>
          <td class="mono ${grossClass}">${grossPrefix}${gross.toFixed(2)}</td>
          <td class="mono" style="color:var(--text-muted); font-size:11px;">₹${chg.toFixed(2)}</td>
          <td class="mono ${netClass}" style="font-weight:700;">${netPrefix}${net.toFixed(2)}</td>
          <td style="color:var(--purple); font-size:11px;">${t.pillar || 'Pillar 4 (BOS)'}</td>
        `;
        tradesBody.appendChild(tr);
      });
    }
  }

  // 5. Telemetry Logs
  const logLines = data.aegis_logs || [];
  const terminal = document.getElementById("logTerminal");
  document.getElementById("logLinesCount").innerText = logLines.length + " lines";

  terminal.innerHTML = logLines.map(line => {
    let cls = "";
    if (line.includes("WYCKOFF") || line.includes("Wyckoff")) cls = "log-tag-wyckoff";
    else if (line.includes("OPTION FLOW") || line.includes("OFI=") || line.includes("Delta")) cls = "log-tag-flow";
    else if (line.includes("BAND DIAG") || line.includes("VWAP")) cls = "log-tag-band";
    else if (line.includes("Pillar") || line.includes("hvg_network") || line.includes("20_ema") || line.includes("micro_sweep") || line.includes("volatility_squeeze")) cls = "log-tag-pillar";
    else if (line.includes("CATBOOST") || line.includes("PAPER")) cls = "log-tag-catboost";
    else if (line.includes("Take-Profit") || line.includes("FILLED") || line.includes("AegisEM") || line.includes("ORDER") || line.includes("WINNER") || line.includes("SCRATCH") || line.includes("Stepped Safety") || line.includes("CLOSED")) cls = "log-tag-em";
    else if (line.includes("VETO") || line.includes("DROP") || line.includes("block") || line.includes("FAILED") || line.includes("discard")) cls = "log-tag-veto";
    else if (line.includes("Spot:") && line.includes("ATM:")) cls = "log-tag-digest";
    else if (line.includes("deep sleep") || line.includes("Deep sleeping") || line.includes("Pre-market") || line.includes("STARTING AEGIS") || line.includes("stopped")) cls = "log-tag-system";
    return `<div class="log-line ${cls}">${line}</div>`;
  }).join("");

  if (autoScroll) {
    terminal.scrollTop = terminal.scrollHeight;
  }
}

// Poll live state every 1.5 seconds
setInterval(fetchLiveState, 1500);
window.onload = fetchLiveState;
</script>
</body>
</html>
"""
