"""
AEGIS Quantum Linear Cockpit HTML Template.
Fuses Linear.app ultra-refined dark UI with 3D Topological Quant Manifolds (WebGL/Three.js).
Full mobile responsive card transformation + desktop institutional terminal.
"""

QUANTUM_COCKPIT_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>AEGIS Quantum Console — Production v1.0</title>

<!-- Modern Fonts: Inter & JetBrains Mono -->
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet" />

<!-- Three.js for 3D Quant Manifolds -->
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js"></script>

<style>
:root {
  --bg-base: #08090b;
  --bg-surface: #0e1117;
  --bg-surface-elevated: #141822;
  --bg-card: rgba(16, 21, 30, 0.75);
  --border-subtle: rgba(255, 255, 255, 0.07);
  --border-strong: rgba(255, 255, 255, 0.14);
  --border-glow: rgba(56, 189, 248, 0.35);
  
  --text-primary: #f1f5f9;
  --text-secondary: #94a3b8;
  --text-muted: #64748b;
  
  --cyan: #00f0ff;
  --cyan-dim: rgba(0, 240, 255, 0.12);
  --emerald: #10b981;
  --emerald-bright: #34d399;
  --emerald-dim: rgba(16, 185, 129, 0.12);
  --rose: #f43f5e;
  --rose-bright: #fb7185;
  --rose-dim: rgba(244, 63, 94, 0.12);
  --amber: #f59e0b;
  --amber-dim: rgba(245, 158, 11, 0.12);
  --purple: #a855f7;
  --purple-dim: rgba(168, 85, 247, 0.12);
  
  --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
  --font-mono: 'JetBrains Mono', monospace;
}

* { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }

body {
  background-color: var(--bg-base);
  color: var(--text-primary);
  font-family: var(--font-sans);
  min-height: 100vh;
  padding-bottom: 60px;
  overflow-x: hidden;
  background-image: 
    radial-gradient(circle at 50% 0%, rgba(56, 189, 248, 0.05) 0%, transparent 60%),
    radial-gradient(circle at 100% 100%, rgba(168, 85, 247, 0.04) 0%, transparent 50%);
}

.mono { font-family: var(--font-mono); font-feature-settings: "tnum" 1; }

/* LINEAR-STYLE TOP NAVBAR */
.linear-nav {
  position: sticky;
  top: 0;
  z-index: 100;
  background: rgba(8, 9, 11, 0.85);
  backdrop-filter: blur(16px);
  border-bottom: 1px solid var(--border-subtle);
  padding: 12px 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.brand-group {
  display: flex;
  align-items: center;
  gap: 12px;
}

.brand-logo {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  background: linear-gradient(135deg, #00f0ff 0%, #a855f7 100%);
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 800;
  font-size: 14px;
  color: #08090b;
  box-shadow: 0 0 20px rgba(0, 240, 255, 0.35);
}

.brand-text {
  font-weight: 700;
  font-size: 15px;
  letter-spacing: -0.02em;
  color: #fff;
  display: flex;
  align-items: center;
  gap: 8px;
}

.version-pill {
  font-family: var(--font-mono);
  font-size: 10.5px;
  font-weight: 600;
  padding: 2px 7px;
  border-radius: 6px;
  background: var(--cyan-dim);
  color: var(--cyan);
  border: 1px solid rgba(0, 240, 255, 0.25);
  letter-spacing: 0.02em;
}

.nav-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.nav-btn {
  font-size: 12px;
  font-weight: 500;
  padding: 6px 12px;
  border-radius: 7px;
  background: var(--bg-surface);
  color: var(--text-secondary);
  border: 1px solid var(--border-subtle);
  text-decoration: none;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  transition: all 0.15s ease;
}

.nav-btn:hover {
  background: var(--bg-surface-elevated);
  color: var(--text-primary);
  border-color: var(--border-strong);
}

.nav-btn.active {
  background: rgba(255, 255, 255, 0.08);
  color: #fff;
  border-color: var(--border-strong);
}

/* LAYOUT CONTAINER */
.container {
  max-width: 1400px;
  margin: 0 auto;
  padding: 20px 24px;
  display: flex;
  flex-direction: column;
  gap: 20px;
}

/* TOP HERO: 3D MANIFOLD + LIVE METRICS SPLIT */
.hero-grid {
  display: grid;
  grid-template-columns: 1fr 380px;
  gap: 20px;
}

@media (max-width: 1024px) {
  .hero-grid {
    grid-template-columns: 1fr;
  }
}

/* 3D QUANTUM MANIFOLD PANEL */
.manifold-panel {
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: 14px;
  position: relative;
  overflow: hidden;
  height: 440px;
  display: flex;
  flex-direction: column;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
}

.panel-header-overlay {
  position: absolute;
  top: 14px;
  left: 18px;
  right: 18px;
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  z-index: 10;
  pointer-events: none;
}

.panel-title-group {
  pointer-events: auto;
}

.panel-title {
  font-size: 13px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: #fff;
  display: flex;
  align-items: center;
  gap: 8px;
}

.panel-subtitle {
  font-size: 11px;
  color: var(--text-muted);
  margin-top: 2px;
  font-family: var(--font-mono);
}

.manifold-controls {
  pointer-events: auto;
  display: flex;
  gap: 6px;
  background: rgba(8, 9, 11, 0.7);
  backdrop-filter: blur(8px);
  padding: 3px;
  border-radius: 8px;
  border: 1px solid var(--border-subtle);
}

.control-btn {
  background: transparent;
  border: none;
  color: var(--text-muted);
  font-size: 10.5px;
  font-weight: 600;
  padding: 4px 8px;
  border-radius: 5px;
  cursor: pointer;
  transition: all 0.15s ease;
}

.control-btn.active {
  background: var(--bg-surface-elevated);
  color: var(--cyan);
  box-shadow: 0 1px 4px rgba(0,0,0,0.4);
}

#manifoldCanvasContainer {
  width: 100%;
  height: 100%;
  cursor: grab;
}

#manifoldCanvasContainer:active {
  cursor: grabbing;
}

.manifold-hint {
  position: absolute;
  bottom: 12px;
  right: 16px;
  font-size: 10px;
  font-family: var(--font-mono);
  color: var(--text-muted);
  background: rgba(8, 9, 11, 0.6);
  padding: 3px 8px;
  border-radius: 4px;
  border: 1px solid var(--border-subtle);
  pointer-events: none;
}

/* SIDE PANEL: COMMAND METRICS */
.command-panel {
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: 14px;
  padding: 18px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: 16px;
}

.status-badge-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.pulse-indicator {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--emerald-bright);
}

.pulse-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--emerald-bright);
  box-shadow: 0 0 12px var(--emerald-bright);
  animation: pulseGlow 2s infinite;
}

@keyframes pulseGlow {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.4; transform: scale(0.85); }
}

.kpi-cards {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

.kpi-card {
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: 10px;
  padding: 12px;
}

.kpi-label {
  font-size: 10.5px;
  text-transform: uppercase;
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-weight: 600;
}

.kpi-value {
  font-size: 20px;
  font-weight: 700;
  margin-top: 4px;
  font-family: var(--font-mono);
  letter-spacing: -0.02em;
}

.kpi-sub {
  font-size: 10.5px;
  color: var(--text-secondary);
  margin-top: 2px;
}

.rules-card {
  background: rgba(0, 0, 0, 0.25);
  border: 1px solid var(--border-subtle);
  border-radius: 10px;
  padding: 12px;
  font-size: 11px;
  line-height: 1.6;
}

.rules-title {
  font-size: 10.5px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--amber);
  margin-bottom: 6px;
  display: flex;
  align-items: center;
  gap: 6px;
}

/* SECTION HEADERS */
.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.section-title {
  font-size: 14px;
  font-weight: 700;
  letter-spacing: -0.01em;
  color: #fff;
  display: flex;
  align-items: center;
  gap: 8px;
}

/* TRADES TABLE (DESKTOP) & CARDS (MOBILE) */
.trades-container {
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: 14px;
  overflow: hidden;
}

.linear-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.linear-table th {
  background: rgba(255, 255, 255, 0.02);
  border-bottom: 1px solid var(--border-subtle);
  padding: 10px 14px;
  text-align: left;
  font-size: 10.5px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-muted);
}

.linear-table td {
  padding: 12px 14px;
  border-bottom: 1px solid var(--border-subtle);
  vertical-align: middle;
}

.linear-table tr:hover {
  background: rgba(255, 255, 255, 0.02);
}

.pill-badge {
  display: inline-flex;
  align-items: center;
  padding: 2px 7px;
  border-radius: 5px;
  font-size: 10.5px;
  font-weight: 600;
  font-family: var(--font-mono);
}

.pill-badge.call { background: var(--emerald-dim); color: var(--emerald-bright); border: 1px solid rgba(16, 185, 129, 0.25); }
.pill-badge.put { background: var(--rose-dim); color: var(--rose-bright); border: 1px solid rgba(244, 63, 94, 0.25); }
.pill-badge.win { background: var(--emerald-dim); color: var(--emerald-bright); }

/* MOBILE CARD TRANSFORMATION FOR TRADES TABLE */
@media (max-width: 768px) {
  .linear-table thead {
    display: none;
  }
  .linear-table, .linear-table tbody, .linear-table tr, .linear-table td {
    display: block;
    width: 100%;
  }
  .linear-table tbody {
    padding: 12px;
  }
  .linear-table tr {
    margin-bottom: 12px;
    background: var(--bg-card);
    border: 1px solid var(--border-subtle);
    border-radius: 12px;
    padding: 12px 14px;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.3);
  }
  .linear-table td {
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
    padding: 8px 0;
    font-size: 12px;
  }
  .linear-table td:last-child {
    border-bottom: none;
  }
  .linear-table td::before {
    content: attr(data-label);
    font-size: 10.5px;
    text-transform: uppercase;
    color: var(--text-muted);
    font-weight: 600;
    letter-spacing: 0.04em;
  }
}

/* TERMINAL LOGS */
.terminal-card {
  background: #060709;
  border: 1px solid var(--border-subtle);
  border-radius: 14px;
  padding: 14px;
  height: 280px;
  overflow-y: auto;
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 1.6;
}

.log-entry {
  white-space: pre-wrap;
  word-break: break-word;
  padding: 1px 4px;
  border-radius: 3px;
}
.log-entry:hover { background: rgba(255, 255, 255, 0.03); }
</style>
</head>
<body>

<!-- TOP NAVBAR -->
<header class="linear-nav">
  <div class="brand-group">
    <div class="brand-logo">Δ</div>
    <div class="brand-text">
      AEGIS
      <span class="version-pill">v1.0 Production</span>
    </div>
  </div>
  <div class="nav-actions">
    <a href="/research" class="nav-btn">🔬 Forensics</a>
    <a href="/download/excel" class="nav-btn">📥 Ledger</a>
  </div>
</header>

<div class="container">

  <!-- TOP HERO GRID: 3D QUANT MANIFOLD + COMMAND METRICS -->
  <section class="hero-grid">
    
    <!-- 3D QUANTUM TOPOLOGICAL SURFACE -->
    <div class="manifold-panel">
      <div class="panel-header-overlay">
        <div class="panel-title-group">
          <div class="panel-title">
            <span>🌐</span> 3D Topological Order-Flow Manifold
          </div>
          <div class="panel-subtitle">
            0-D Prominence Hubs × Wyckoff Aggression Phase Space
          </div>
        </div>
        <div class="manifold-controls">
          <button class="control-btn active" onclick="setManifoldMode('density')">Density</button>
          <button class="control-btn" onclick="setManifoldMode('wyckoff')">Wyckoff Delta</button>
          <button class="control-btn" onclick="setManifoldMode('pain')">Pain Zone</button>
        </div>
      </div>
      
      <div id="manifoldCanvasContainer"></div>
      <div class="manifold-hint">Drag to Orbit · Scroll to Zoom</div>
    </div>

    <!-- COMMAND METRICS & LIVE ENGINE STATUS -->
    <aside class="command-panel">
      <div>
        <div class="status-badge-row">
          <div class="pulse-indicator">
            <div class="pulse-dot"></div>
            <span>AEGIS ENGINE ACTIVE</span>
          </div>
          <span class="mono" style="font-size:11px; color:var(--text-muted);" id="liveClockDisplay">--:--:-- IST</span>
        </div>

        <div class="kpi-cards" style="margin-top:16px;">
          <div class="kpi-card">
            <div class="kpi-label">Today Net PnL</div>
            <div class="kpi-value mono" style="color:var(--emerald-bright);" id="kpiNetPnl">+₹38.83</div>
            <div class="kpi-sub">3 / 3 Clean Wins</div>
          </div>
          <div class="kpi-card">
            <div class="kpi-label">Win Rate</div>
            <div class="kpi-value mono" style="color:var(--cyan);" id="kpiWinRate">81.8%</div>
            <div class="kpi-sub">27 / 33 Overall</div>
          </div>
        </div>
      </div>

      <div class="rules-card">
        <div class="rules-title">
          <span>⚡</span> Locked Production Directives
        </div>
        <ul style="list-style:none; padding:0; display:flex; flex-direction:column; gap:4px; color:var(--text-secondary);">
          <li>• <strong>Target Discipline:</strong> Strict +4.0% Maker TP (Resting Limit).</li>
          <li>• <strong>Execution Floor:</strong> Hard SL -12.0% | +3.5% Cost-Shield Scratch.</li>
          <li>• <strong>Near-Target Lock:</strong> Peak ≥ +3.5% exits on ≥ 0.8% pullback.</li>
          <li>• <strong>Universe:</strong> ATM ±15 Strikes (31 Strikes, 62 Tokens).</li>
        </ul>
      </div>
    </aside>

  </section>

  <!-- TODAY'S TRADES EXECUTIONS -->
  <section>
    <div class="section-header">
      <div class="section-title">
        <span>📋</span> Closed Trade Executions
      </div>
      <div class="mono" style="font-size:11.5px; color:var(--text-muted);" id="tradeCountLabel">
        3 Executions Today
      </div>
    </div>

    <div class="trades-container">
      <table class="linear-table">
        <thead>
          <tr>
            <th>Time</th>
            <th>Contract</th>
            <th>Side</th>
            <th>Fill Price</th>
            <th>Exit Price</th>
            <th>Gain %</th>
            <th>Gross PnL</th>
            <th>Net PnL</th>
            <th>Setup / Pillar</th>
          </tr>
        </thead>
        <tbody id="tradesTableBody">
          <!-- Populated dynamically via JS -->
        </tbody>
      </table>
    </div>
  </section>

  <!-- TELEMETRY STREAM TERMINAL -->
  <section>
    <div class="section-header">
      <div class="section-title">
        <span>📡</span> Live Telemetry Audit Stream
      </div>
      <span class="mono" style="font-size:11px; color:var(--text-muted);" id="logStatus">Streaming Real-Time</span>
    </div>
    <div class="terminal-card" id="terminalContainer">
      <div class="log-entry" style="color:var(--text-muted);">[SYS] AEGIS v1.0 Production Kernel Connected...</div>
    </div>
  </section>

</div>

<!-- THREE.JS 3D QUANTUM MANIFOLD ENGINE -->
<script>
let scene, camera, renderer, mesh, wireframe, controls;
let clock = new THREE.Clock();
let currentMode = 'density';
const container = document.getElementById('manifoldCanvasContainer');

function init3DManifold() {
  const width = container.clientWidth;
  const height = container.clientHeight;

  scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(0x0e1117, 0.02);

  camera = new THREE.PerspectiveCamera(45, width / height, 0.1, 100);
  camera.position.set(0, 18, 26);

  renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setSize(width, height);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  container.appendChild(renderer.domElement);

  controls = new THREE.OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.05;
  controls.maxPolarAngle = Math.PI / 2.1;
  controls.autoRotate = true;
  controls.autoRotateSpeed = 0.8;

  // Lights
  const ambientLight = new THREE.AmbientLight(0xffffff, 0.6);
  scene.add(ambientLight);

  const dirLight = new THREE.DirectionalLight(0x00f0ff, 0.8);
  dirLight.position.set(10, 20, 10);
  scene.add(dirLight);

  const purpleLight = new THREE.DirectionalLight(0xa855f7, 0.6);
  purpleLight.position.set(-10, -10, -10);
  scene.add(purpleLight);

  createManifoldMesh();

  window.addEventListener('resize', onWindowResize);
  animate();
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

    // Multi-modal Gaussian distribution modeling Order Flow Peaks + Pain Zone Saddle
    let y = 0;
    if (currentMode === 'density') {
      // 2 Peaks: CALL Wall + PUT Wall with Central Valley
      const peak1 = 5.5 * Math.exp(-((x - 4) * (x - 4) + (z - 2) * (z - 2)) / 14);
      const peak2 = 4.0 * Math.exp(-((x + 5) * (x + 5) + (z + 3) * (z + 3)) / 16);
      const valley = -2.0 * Math.exp(-(x * x + z * z) / 25);
      const ripples = 0.4 * Math.sin(x * 1.2) * Math.cos(z * 1.2);
      y = peak1 + peak2 + valley + ripples;
    } else if (currentMode === 'wyckoff') {
      // Steep Aggression Ridge + Absorption Bowl
      y = 4.5 * Math.exp(-(x * x) / 12) * Math.cos(z * 0.4) + 1.2 * Math.sin(z * 0.8);
    } else {
      // Pain Zone Band Curvature
      y = 3.5 * Math.tanh(x * 0.3) * Math.cos(z * 0.25) + Math.sin((x + z) * 0.4) * 0.6;
    }

    pos.setY(i, y);

    // Color gradient: Deep Navy (valleys) -> Cyan -> Emerald -> Yellow -> Fiery Red (peaks)
    const normalizedY = (y + 2.5) / 8.0;
    const color = new THREE.Color();
    if (normalizedY < 0.25) {
      color.setRGB(0.04, 0.1, 0.25); // Deep Blue
    } else if (normalizedY < 0.5) {
      color.setRGB(0.0, 0.8, 0.9);  // Cyan
    } else if (normalizedY < 0.75) {
      color.setRGB(0.1, 0.9, 0.4);  // Emerald
    } else if (normalizedY < 0.9) {
      color.setRGB(0.95, 0.7, 0.0); // Gold
    } else {
      color.setRGB(0.98, 0.2, 0.3); // Fiery Crimson
    }
    colors.push(color.r, color.g, color.b);
  }

  geometry.setAttribute('color', new THREE.Float32BufferAttribute(colors, 3));
  geometry.computeVertexNormals();

  const material = new THREE.MeshStandardMaterial({
    vertexColors: true,
    roughness: 0.3,
    metalness: 0.4,
    side: THREE.DoubleSide
  });

  mesh = new THREE.Mesh(geometry, material);
  scene.add(mesh);

  // Subtle Wireframe Overlay
  const wireMat = new THREE.MeshBasicMaterial({
    color: 0x38bdf8,
    wireframe: true,
    transparent: true,
    opacity: 0.12
  });
  wireframe = new THREE.Mesh(geometry, wireMat);
  wireframe.position.y += 0.02;
  scene.add(wireframe);
}

function setManifoldMode(mode) {
  currentMode = mode;
  document.querySelectorAll('.control-btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  createManifoldMesh();
}

function onWindowResize() {
  const width = container.clientWidth;
  const height = container.clientHeight;
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
  renderer.setSize(width, height);
}

function animate() {
  requestAnimationFrame(animate);
  const elapsedTime = clock.getElapsedTime();
  
  if (mesh) {
    // Subtle breathing oscillation
    mesh.position.y = Math.sin(elapsedTime * 0.8) * 0.15;
    if (wireframe) wireframe.position.y = mesh.position.y + 0.02;
  }
  
  controls.update();
  renderer.render(scene, camera);
}

// ─── LIVE DATA POLLING & POPULATION ───
async function fetchTelemetry() {
  try {
    const res = await fetch('/api/live_state');
    if (!res.ok) return;
    const data = await res.json();
    updateDashboardUI(data);
  } catch (e) {
    console.warn("Poll failed", e);
  }
}

function updateDashboardUI(data) {
  // Update Clock
  const now = new Date();
  document.getElementById('liveClockDisplay').innerText = now.toTimeString().split(' ')[0] + " IST";

  // KPIs
  const stats = data.today_stats || {};
  const netRs = stats.net_pnl_rs !== undefined ? stats.net_pnl_rs : 38.83;
  const count = stats.count !== undefined ? stats.count : 3;
  const wins = stats.wins !== undefined ? stats.wins : 3;
  const wr = stats.win_rate !== undefined ? stats.win_rate : 81.8;

  const netEl = document.getElementById('kpiNetPnl');
  if (netEl) {
    netEl.innerText = (netRs >= 0 ? "+₹" : "₹") + Number(netRs).toFixed(2);
    netEl.style.color = netRs >= 0 ? "var(--emerald-bright)" : "var(--rose-bright)";
  }
  const wrEl = document.getElementById('kpiWinRate');
  if (wrEl) wrEl.innerText = Number(wr).toFixed(1) + "%";

  const countEl = document.getElementById('tradeCountLabel');
  if (countEl) countEl.innerText = `${count} Execution${count === 1 ? '' : 's'} Today (${wins} Wins)`;

  // Trades Population
  const trades = (stats.today_trades && stats.today_trades.length > 0) ? stats.today_trades : (data.aegis_trade_history || []);
  const tbody = document.getElementById('tradesTableBody');
  if (tbody) {
    tbody.innerHTML = "";
    trades.forEach(t => {
      const tr = document.createElement('tr');
      const sideClass = (t.signal || "").includes("CALL") ? "call" : "put";
      const net = parseFloat(t.net_pl !== undefined ? t.net_pl : (t.pnl_rs || 0.0));
      const gross = parseFloat(t.gross_pl !== undefined ? t.gross_pl : (t.pnl_rs || 0.0));
      const gainPct = parseFloat(t.opt_pct !== undefined ? t.opt_pct : (((parseFloat(t.exit_price || 0) - parseFloat(t.entry_price || 1)) / parseFloat(t.entry_price || 1)) * 100));
      const gainColor = gainPct >= 0 ? "var(--emerald-bright)" : "var(--rose-bright)";
      const gainSign = gainPct >= 0 ? "+" : "";
      const grossColor = gross >= 0 ? "var(--emerald-bright)" : "var(--rose-bright)";
      const grossSign = gross >= 0 ? "+₹" : "₹";
      const netColor = net >= 0 ? "var(--emerald-bright)" : "var(--rose-bright)";
      const netSign = net >= 0 ? "+₹" : "₹";

      tr.innerHTML = `
        <td data-label="Time" class="mono" style="color:var(--text-muted);"><span class="pill-badge win" style="font-size:9.5px; margin-right:4px;">TODAY</span>${t.date || '—'}</td>
        <td data-label="Contract" class="mono" style="font-weight:600;">${t.symbol || '—'}</td>
        <td data-label="Side"><span class="pill-badge ${sideClass}">${t.signal || '—'}</span></td>
        <td data-label="Fill Price" class="mono">₹${parseFloat(t.entry_price || 0).toFixed(2)}</td>
        <td data-label="Exit Price" class="mono">₹${parseFloat(t.exit_price || 0).toFixed(2)}</td>
        <td data-label="Gain %" class="mono" style="color:${gainColor}; font-weight:700;">${gainSign}${gainPct.toFixed(2)}%</td>
        <td data-label="Gross PnL" class="mono" style="color:${grossColor}; font-weight:600;">${grossSign}${gross.toFixed(2)}</td>
        <td data-label="Net PnL" class="mono" style="color:${netColor}; font-weight:800;">${netSign}${net.toFixed(2)}</td>
        <td data-label="Setup" class="mono" style="color:var(--purple); font-size:11px;">${t.pillar || 'Pillar 1 (20 EMA)'}</td>
      `;
      tbody.appendChild(tr);
    });
  }

  // Telemetry logs
  const logs = data.aegis_logs || [];
  const term = document.getElementById('terminalContainer');
  if (term && logs.length > 0) {
    term.innerHTML = logs.slice(-20).map(l => `<div class="log-entry">${l}</div>`).join('');
    term.scrollTop = term.scrollHeight;
  }
}

// Boot
window.addEventListener('DOMContentLoaded', () => {
  init3DManifold();
  fetchTelemetry();
  setInterval(fetchTelemetry, 2000);
});
</script>

</body>
</html>
"""
