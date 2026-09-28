import json
import os

json_path = 'scripts/nifty_1m_with_5_vwaps_2026-09-21.json'
with open(json_path, 'r') as f:
    candles_data = json.load(f)

print(f"Loaded {len(candles_data)} candles from {json_path}")

# Detect setups on each candle
for i, c in enumerate(candles_data):
    close = c['close']
    cb = c['vwap_call_buy']
    cs = c['vwap_call_sell']
    pb = c['vwap_put_buy']
    ps = c['vwap_put_sell']
    comp = c['vwap_composite']
    ofi = c['ofi']
    
    setup = None
    setup_badge = ""
    setup_color = ""
    
    # Setup 1: CE Bullish Momentum (Put Buyers trapped + Call Buyers in profit)
    if close > cb and close > pb and ofi > 1000000:
        setup = "CE Bullish Momentum (Put Buyers Trapped)"
        setup_badge = "CE_MOMENTUM"
        setup_color = "#10b981"
    # Setup 2: PE Bearish Breakdown (Call Buyers trapped in pain)
    elif close < cb and close < pb and ofi < -1000000:
        setup = "PE Bearish Breakdown (Call Buyers Trapped)"
        setup_badge = "PE_BREAKDOWN"
        setup_color = "#ef4444"
    # Setup 3: Call Wall Rejection (Resistance at CallSell)
    elif abs(close - cs) <= 5.0 and ofi < 0:
        setup = "Call Writer Wall Rejection"
        setup_badge = "CALL_WALL_REJ"
        setup_color = "#f97316"
    # Setup 4: Put Wall Bounce (Support at PutSell)
    elif abs(close - ps) <= 5.0 and ofi > 0:
        setup = "Put Writer Wall Bounce"
        setup_badge = "PUT_WALL_BOUNCE"
        setup_color = "#06b6d4"
        
    c['setup'] = setup
    c['setup_badge'] = setup_badge
    c['setup_color'] = setup_color

data_json_str = json.dumps(candles_data)

html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>NIFTY 1M Candlestick + 5 VWAP Microstructure Pain Zone Chart (2026-09-21)</title>
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
  <style>
    body {{
      user-select: none;
      -webkit-user-select: none;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }}
    .custom-scrollbar::-webkit-scrollbar {{
      height: 6px;
      width: 6px;
    }}
    .custom-scrollbar::-webkit-scrollbar-track {{
      background: #1e293b;
    }}
    .custom-scrollbar::-webkit-scrollbar-thumb {{
      background: #475569;
      border-radius: 3px;
    }}
  </style>
</head>
<body class="bg-[#0b0f19] text-slate-100 p-4 min-h-screen">
  <!-- Header / Control Bar -->
  <div class="max-w-[1700px] mx-auto space-y-4">
    <div class="flex flex-wrap items-center justify-between gap-4 bg-[#111827] border border-slate-800 rounded-xl p-4 shadow-lg">
      <div class="flex items-center gap-3">
        <div class="p-2.5 bg-indigo-500/10 border border-indigo-500/30 rounded-lg text-indigo-400">
          <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6"></path></svg>
        </div>
        <div>
          <div class="flex items-center gap-2">
            <h1 class="font-bold text-xl text-white tracking-wide">NIFTY 50 • 1-Minute Microstructure Pain Zones</h1>
            <span class="px-2 py-0.5 text-xs font-semibold bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 rounded">2026-09-21 (Audited)</span>
            <span class="px-2 py-0.5 text-xs font-semibold bg-blue-500/20 text-blue-400 border border-blue-500/30 rounded">348 Bars (0 Gaps)</span>
          </div>
          <p class="text-xs text-slate-400 mt-0.5">Dual-Timeframe 4-Line Trapped-Buyer Pain Zone VWAP Engine + 1M Order Flow Imbalance</p>
        </div>
      </div>

      <!-- Quick Metrics -->
      <div class="flex items-center gap-4 text-xs">
        <div class="bg-slate-900/80 border border-slate-800 rounded-lg px-3 py-1.5 text-center">
          <div class="text-slate-400">Day Open</div>
          <div class="font-bold text-white text-sm" id="metric-open">23,328.05</div>
        </div>
        <div class="bg-slate-900/80 border border-slate-800 rounded-lg px-3 py-1.5 text-center">
          <div class="text-slate-400">Day High</div>
          <div class="font-bold text-emerald-400 text-sm" id="metric-high">23,446.50</div>
        </div>
        <div class="bg-slate-900/80 border border-slate-800 rounded-lg px-3 py-1.5 text-center">
          <div class="text-slate-400">Day Low</div>
          <div class="font-bold text-rose-400 text-sm" id="metric-low">23,317.15</div>
        </div>
        <div class="bg-slate-900/80 border border-slate-800 rounded-lg px-3 py-1.5 text-center">
          <div class="text-slate-400">Day Close</div>
          <div class="font-bold text-cyan-400 text-sm" id="metric-close">23,414.30</div>
        </div>
        <div class="bg-slate-900/80 border border-slate-800 rounded-lg px-3 py-1.5 text-center">
          <div class="text-slate-400">Day Range</div>
          <div class="font-bold text-yellow-400 text-sm">+129.35 pts</div>
        </div>
      </div>
    </div>

    <!-- Legend & Line Toggles -->
    <div class="flex flex-wrap items-center justify-between gap-3 bg-[#111827] border border-slate-800 rounded-xl px-4 py-2.5 text-xs">
      <div class="flex flex-wrap items-center gap-4">
        <span class="font-semibold text-slate-300">Active Overlays:</span>
        <label class="flex items-center gap-1.5 cursor-pointer">
          <input type="checkbox" id="toggle-cs" checked class="rounded border-slate-700 text-rose-500 focus:ring-0">
          <span class="w-3 h-0.5 bg-rose-500 inline-block"></span>
          <span class="text-rose-400 font-medium">VWAP_CallSell (Ceiling / Writers)</span>
        </label>
        <label class="flex items-center gap-1.5 cursor-pointer">
          <input type="checkbox" id="toggle-cb" checked class="rounded border-slate-700 text-amber-500 focus:ring-0">
          <span class="w-3 h-0.5 bg-amber-400 inline-block"></span>
          <span class="text-amber-300 font-medium">VWAP_CallBuy (Trapped CB Level)</span>
        </label>
        <label class="flex items-center gap-1.5 cursor-pointer">
          <input type="checkbox" id="toggle-comp" checked class="rounded border-slate-700 text-yellow-400 focus:ring-0">
          <span class="w-3 h-0.5 bg-yellow-400 inline-block"></span>
          <span class="text-yellow-300 font-medium">Composite Spot VWAP</span>
        </label>
        <label class="flex items-center gap-1.5 cursor-pointer">
          <input type="checkbox" id="toggle-ps" checked class="rounded border-slate-700 text-cyan-400 focus:ring-0">
          <span class="w-3 h-0.5 bg-cyan-400 inline-block"></span>
          <span class="text-cyan-300 font-medium">VWAP_PutSell (Floor / Writers)</span>
        </label>
        <label class="flex items-center gap-1.5 cursor-pointer">
          <input type="checkbox" id="toggle-pb" checked class="rounded border-slate-700 text-emerald-400 focus:ring-0">
          <span class="w-3 h-0.5 bg-emerald-400 inline-block"></span>
          <span class="text-emerald-400 font-medium">VWAP_PutBuy (Trapped PB Fuel)</span>
        </label>
      </div>

      <!-- Window Preset Buttons -->
      <div class="flex items-center gap-1.5">
        <span class="text-slate-400 text-xs mr-1">Zoom View:</span>
        <button id="btn-zoom-30" class="px-2.5 py-1 bg-slate-800 hover:bg-slate-700 text-slate-200 rounded text-xs transition">30m</button>
        <button id="btn-zoom-60" class="px-2.5 py-1 bg-slate-800 hover:bg-slate-700 text-slate-200 rounded text-xs transition">1h</button>
        <button id="btn-zoom-120" class="px-2.5 py-1 bg-slate-800 hover:bg-slate-700 text-slate-200 rounded text-xs transition">2h</button>
        <button id="btn-zoom-all" class="px-2.5 py-1 bg-indigo-600 hover:bg-indigo-500 text-white font-semibold rounded text-xs transition">All (Full Day)</button>
        <button id="btn-reset" class="px-2.5 py-1 bg-slate-800 hover:bg-slate-700 text-slate-400 rounded text-xs transition" title="Reset View">⟲ Reset</button>
      </div>
    </div>

    <!-- Canvas Container -->
    <div class="bg-[#111827] border border-slate-800 rounded-xl p-4 shadow-xl relative">
      <!-- Floating Tooltip Box -->
      <div id="tooltip" class="absolute hidden z-20 pointer-events-none bg-slate-900/95 border border-slate-700 rounded-lg p-3 text-xs shadow-2xl backdrop-blur-sm space-y-1.5 min-w-[240px]">
        <div class="flex justify-between items-center border-b border-slate-800 pb-1">
          <span id="tt-time" class="font-bold text-white text-sm">--:--</span>
          <span id="tt-badge" class="px-1.5 py-0.5 text-[10px] font-semibold rounded bg-slate-800 text-slate-400">NORMAL</span>
        </div>
        <div class="grid grid-cols-2 gap-x-3 gap-y-0.5 text-[11px]">
          <span class="text-slate-400">Open:</span> <span id="tt-open" class="text-right font-mono text-white">0.0</span>
          <span class="text-slate-400">High:</span> <span id="tt-high" class="text-right font-mono text-emerald-400">0.0</span>
          <span class="text-slate-400">Low:</span> <span id="tt-low" class="text-right font-mono text-rose-400">0.0</span>
          <span class="text-slate-400">Close:</span> <span id="tt-close" class="text-right font-mono text-white font-bold">0.0</span>
        </div>
        <div class="border-t border-slate-800 pt-1 space-y-0.5 text-[11px]">
          <div class="flex justify-between text-rose-400"><span>CS Wall:</span><span id="tt-cs" class="font-mono">0.0</span></div>
          <div class="flex justify-between text-amber-300"><span>CB Trapped:</span><span id="tt-cb" class="font-mono">0.0</span></div>
          <div class="flex justify-between text-yellow-300"><span>Composite VWAP:</span><span id="tt-comp" class="font-mono">0.0</span></div>
          <div class="flex justify-between text-cyan-300"><span>PS Wall:</span><span id="tt-ps" class="font-mono">0.0</span></div>
          <div class="flex justify-between text-emerald-400"><span>PB Fuel:</span><span id="tt-pb" class="font-mono">0.0</span></div>
        </div>
        <div class="border-t border-slate-800 pt-1 flex justify-between text-[11px]">
          <span class="text-slate-400">1M OFI (Net Flow):</span>
          <span id="tt-ofi" class="font-mono font-bold">0</span>
        </div>
      </div>

      <!-- Main Price & VWAP Canvas -->
      <canvas id="chartCanvas" class="w-full cursor-crosshair rounded" style="height: 480px;"></canvas>

      <!-- OFI & Volume Canvas -->
      <canvas id="volumeCanvas" class="w-full cursor-crosshair rounded mt-2" style="height: 140px;"></canvas>

      <!-- Horizontal Time Range Slider (User requested sliding feature) -->
      <div class="mt-4 pt-3 border-t border-slate-800/80 flex items-center gap-4">
        <span class="text-xs text-slate-400 font-medium whitespace-nowrap">Time Scrubber (Slide):</span>
        <input type="range" id="time-slider" min="0" max="347" value="0" class="w-full h-1.5 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-indigo-500">
        <span id="slider-time-label" class="text-xs font-mono text-indigo-400 whitespace-nowrap min-w-[70px] text-right">09:15 - 15:30</span>
      </div>
    </div>

    <!-- Educational Mechanics & Setup Cheat Sheet -->
    <div class="grid grid-cols-1 md:grid-cols-4 gap-4 text-xs">
      <div class="bg-[#111827] border border-emerald-500/20 rounded-xl p-3.5 space-y-1">
        <div class="font-bold text-emerald-400 flex items-center gap-1.5">
          <span class="w-2 h-2 rounded-full bg-emerald-400"></span> Setup 1: CE Bullish Momentum
        </div>
        <p class="text-slate-300">Spot &gt; VWAP_CallBuy AND Spot &gt; VWAP_PutBuy with positive OFI. Put buyers trapped in deep loss cover positions, fueling upward expansion.</p>
      </div>

      <div class="bg-[#111827] border border-cyan-500/20 rounded-xl p-3.5 space-y-1">
        <div class="font-bold text-cyan-400 flex items-center gap-1.5">
          <span class="w-2 h-2 rounded-full bg-cyan-400"></span> Setup 2: Put Wall Bounce
        </div>
        <p class="text-slate-300">Spot pulls back into VWAP_PutSell (or Composite VWAP) and holds. Put writers defend break-even line, repelling downside attempts.</p>
      </div>

      <div class="bg-[#111827] border border-rose-500/20 rounded-xl p-3.5 space-y-1">
        <div class="font-bold text-rose-400 flex items-center gap-1.5">
          <span class="w-2 h-2 rounded-full bg-rose-400"></span> Setup 3: PE Bearish Breakdown
        </div>
        <p class="text-slate-300">Spot &lt; VWAP_CallBuy AND Spot &lt; VWAP_PutBuy with negative OFI. Trapped Call buyers forced to liquidate, driving cascading selloffs.</p>
      </div>

      <div class="bg-[#111827] border border-amber-500/20 rounded-xl p-3.5 space-y-1">
        <div class="font-bold text-amber-400 flex items-center gap-1.5">
          <span class="w-2 h-2 rounded-full bg-amber-400"></span> Setup 4: Call Wall Rejection
        </div>
        <p class="text-slate-300">Spot rallies into VWAP_CallSell and exhausts. Heavy Call institutional writers sell fresh contracts, capping upside breakout.</p>
      </div>
    </div>
  </div>

  <script>
    const RAW_DATA = {data_json_str};
    const TOTAL_BARS = RAW_DATA.length;

    // Viewport State
    let startIndex = 0;
    let visibleCount = TOTAL_BARS; // default full day
    let hoveredIndex = -1;
    let isDragging = false;
    let dragStartX = 0;
    let dragStartStartIndex = 0;

    // Canvas References
    const chartCanvas = document.getElementById('chartCanvas');
    const chartCtx = chartCanvas.getContext('2d');
    const volCanvas = document.getElementById('volumeCanvas');
    const volCtx = volCanvas.getContext('2d');
    const tooltip = document.getElementById('tooltip');
    const timeSlider = document.getElementById('time-slider');
    const sliderLabel = document.getElementById('slider-time-label');

    // Overlays State
    const overlays = {{
      cs: true,
      cb: true,
      comp: true,
      ps: true,
      pb: true
    }};

    document.getElementById('toggle-cs').addEventListener('change', e => {{ overlays.cs = e.target.checked; render(); }});
    document.getElementById('toggle-cb').addEventListener('change', e => {{ overlays.cb = e.target.checked; render(); }});
    document.getElementById('toggle-comp').addEventListener('change', e => {{ overlays.comp = e.target.checked; render(); }});
    document.getElementById('toggle-ps').addEventListener('change', e => {{ overlays.ps = e.target.checked; render(); }});
    document.getElementById('toggle-pb').addEventListener('change', e => {{ overlays.pb = e.target.checked; render(); }});

    // Resize handling (High-DPI / Retina Crispness)
    function resizeCanvases() {{
      const dpr = window.devicePixelRatio || 1;
      const rectC = chartCanvas.getBoundingClientRect();
      const rectV = volCanvas.getBoundingClientRect();

      chartCanvas.width = rectC.width * dpr;
      chartCanvas.height = rectC.height * dpr;
      chartCtx.resetTransform();
      chartCtx.scale(dpr, dpr);

      volCanvas.width = rectV.width * dpr;
      volCanvas.height = rectV.height * dpr;
      volCtx.resetTransform();
      volCtx.scale(dpr, dpr);

      render();
    }}
    window.addEventListener('resize', resizeCanvases);

    // Zoom Buttons
    document.getElementById('btn-zoom-30').addEventListener('click', () => setWindowSize(30));
    document.getElementById('btn-zoom-60').addEventListener('click', () => setWindowSize(60));
    document.getElementById('btn-zoom-120').addEventListener('click', () => setWindowSize(120));
    document.getElementById('btn-zoom-all').addEventListener('click', () => setWindowSize(TOTAL_BARS));
    document.getElementById('btn-reset').addEventListener('click', () => {{ startIndex = 0; visibleCount = TOTAL_BARS; updateSlider(); render(); }});

    function setWindowSize(count) {{
      visibleCount = Math.min(count, TOTAL_BARS);
      if (startIndex + visibleCount > TOTAL_BARS) {{
        startIndex = Math.max(0, TOTAL_BARS - visibleCount);
      }}
      updateSlider();
      render();
    }}

    // Time Slider
    timeSlider.max = TOTAL_BARS - 10;
    timeSlider.addEventListener('input', (e) => {{
      startIndex = parseInt(e.target.value);
      if (startIndex + visibleCount > TOTAL_BARS) {{
        startIndex = TOTAL_BARS - visibleCount;
      }}
      render();
    }});

    function updateSlider() {{
      timeSlider.max = Math.max(0, TOTAL_BARS - visibleCount);
      timeSlider.value = startIndex;
      const t1 = RAW_DATA[startIndex] ? RAW_DATA[startIndex].time : '09:15';
      const endI = Math.min(TOTAL_BARS - 1, startIndex + visibleCount - 1);
      const t2 = RAW_DATA[endI] ? RAW_DATA[endI].time : '15:30';
      sliderLabel.textContent = `${{t1}} - ${{t2}}`;
    }}

    // Mouse / Touch Navigation (Pan & Zoom)
    chartCanvas.addEventListener('mousedown', (e) => {{
      isDragging = true;
      dragStartX = e.clientX;
      dragStartStartIndex = startIndex;
    }});

    window.addEventListener('mouseup', () => isDragging = false);

    chartCanvas.addEventListener('mousemove', (e) => {{
      const rect = chartCanvas.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;

      if (isDragging) {{
        const dx = e.clientX - dragStartX;
        const candleWidth = rect.width / visibleCount;
        const shiftBars = Math.round(dx / candleWidth);
        let newStart = dragStartStartIndex - shiftBars;
        newStart = Math.max(0, Math.min(TOTAL_BARS - visibleCount, newStart));
        startIndex = newStart;
        updateSlider();
        render();
      }} else {{
        handleHover(x, y, rect.width, rect.height);
      }}
    }});

    chartCanvas.addEventListener('mouseleave', () => {{
      hoveredIndex = -1;
      tooltip.classList.add('hidden');
      render();
    }});

    // Scroll Wheel Zoom
    chartCanvas.addEventListener('wheel', (e) => {{
      e.preventDefault();
      const zoomFactor = e.deltaY < 0 ? 0.85 : 1.15;
      let newCount = Math.round(visibleCount * zoomFactor);
      newCount = Math.max(15, Math.min(TOTAL_BARS, newCount));
      visibleCount = newCount;
      if (startIndex + visibleCount > TOTAL_BARS) {{
        startIndex = Math.max(0, TOTAL_BARS - visibleCount);
      }}
      updateSlider();
      render();
    }}, {{ passive: false }});

    function handleHover(mouseX, mouseY, width, height) {{
      const paddingRight = 60;
      const plotWidth = width - paddingRight;
      if (mouseX < 0 || mouseX > plotWidth) {{
        tooltip.classList.add('hidden');
        hoveredIndex = -1;
        render();
        return;
      }}

      const candleWidth = plotWidth / visibleCount;
      const indexInView = Math.floor(mouseX / candleWidth);
      const actualIndex = startIndex + indexInView;

      if (actualIndex >= 0 && actualIndex < TOTAL_BARS) {{
        hoveredIndex = actualIndex;
        const d = RAW_DATA[actualIndex];
        
        // Populate Tooltip
        document.getElementById('tt-time').textContent = d.time;
        const badge = document.getElementById('tt-badge');
        if (d.setup_badge) {{
          badge.textContent = d.setup_badge;
          badge.style.backgroundColor = d.setup_color + '33';
          badge.style.color = d.setup_color;
          badge.classList.remove('hidden');
        }} else {{
          badge.textContent = 'NEUTRAL';
          badge.style.backgroundColor = '#1e293b';
          badge.style.color = '#94a3b8';
        }}

        document.getElementById('tt-open').textContent = d.open.toFixed(2);
        document.getElementById('tt-high').textContent = d.high.toFixed(2);
        document.getElementById('tt-low').textContent = d.low.toFixed(2);
        document.getElementById('tt-close').textContent = d.close.toFixed(2);
        document.getElementById('tt-cs').textContent = d.vwap_call_sell.toFixed(2);
        document.getElementById('tt-cb').textContent = d.vwap_call_buy.toFixed(2);
        document.getElementById('tt-comp').textContent = d.vwap_composite.toFixed(2);
        document.getElementById('tt-ps').textContent = d.vwap_put_sell.toFixed(2);
        document.getElementById('tt-pb').textContent = d.vwap_put_buy.toFixed(2);

        const ofiEl = document.getElementById('tt-ofi');
        ofiEl.textContent = (d.ofi > 0 ? '+' : '') + d.ofi.toLocaleString();
        ofiEl.className = 'font-mono font-bold ' + (d.ofi >= 0 ? 'text-emerald-400' : 'text-rose-400');

        // Position Tooltip
        tooltip.classList.remove('hidden');
        const ttWidth = tooltip.offsetWidth || 240;
        let leftPos = mouseX + 25;
        if (leftPos + ttWidth > width - paddingRight) {{
          leftPos = mouseX - ttWidth - 20;
        }}
        tooltip.style.left = leftPos + 'px';
        tooltip.style.top = Math.max(10, Math.min(height - 240, mouseY - 40)) + 'px';

        render();
      }}
    }}

    // Render Engine
    function render() {{
      const rectC = chartCanvas.getBoundingClientRect();
      const rectV = volCanvas.getBoundingClientRect();
      const width = rectC.width;
      const heightC = rectC.height;
      const heightV = rectV.height;
      const paddingRight = 60;
      const plotWidth = width - paddingRight;

      chartCtx.clearRect(0, 0, width, heightC);
      volCtx.clearRect(0, 0, width, heightV);

      const viewData = RAW_DATA.slice(startIndex, startIndex + visibleCount);
      if (viewData.length === 0) return;

      // Calculate Min & Max for Price Scaling
      let minP = Infinity;
      let maxP = -Infinity;
      let maxAbsOFI = 1000;

      viewData.forEach(d => {{
        minP = Math.min(minP, d.low);
        maxP = Math.max(maxP, d.high);
        if (overlays.cs) {{ minP = Math.min(minP, d.vwap_call_sell); maxP = Math.max(maxP, d.vwap_call_sell); }}
        if (overlays.cb) {{ minP = Math.min(minP, d.vwap_call_buy); maxP = Math.max(maxP, d.vwap_call_buy); }}
        if (overlays.comp) {{ minP = Math.min(minP, d.vwap_composite); maxP = Math.max(maxP, d.vwap_composite); }}
        if (overlays.ps) {{ minP = Math.min(minP, d.vwap_put_sell); maxP = Math.max(maxP, d.vwap_put_sell); }}
        if (overlays.pb) {{ minP = Math.min(minP, d.vwap_put_buy); maxP = Math.max(maxP, d.vwap_put_buy); }}
        maxAbsOFI = Math.max(maxAbsOFI, Math.abs(d.ofi));
      }});

      // Add padding to price scale (3 pts)
      const padP = (maxP - minP) * 0.08 || 5.0;
      minP -= padP;
      maxP += padP;

      const candleW = plotWidth / visibleCount;
      const getY = (p) => heightC - ((p - minP) / (maxP - minP)) * heightC;
      const getVolY = (ofi) => {{
        const mid = heightV / 2;
        return mid - (ofi / maxAbsOFI) * (mid * 0.9);
      }};

      // 1. Draw Grid Lines on Price Chart
      chartCtx.strokeStyle = '#1e293b';
      chartCtx.lineWidth = 1;
      const priceSteps = 6;
      for (let i = 0; i <= priceSteps; i++) {{
        const p = minP + (i / priceSteps) * (maxP - minP);
        const y = getY(p);
        chartCtx.beginPath();
        chartCtx.moveTo(0, y);
        chartCtx.lineTo(plotWidth, y);
        chartCtx.stroke();

        // Price Axis Label
        chartCtx.fillStyle = '#64748b';
        chartCtx.font = '10px monospace';
        chartCtx.textAlign = 'left';
        chartCtx.fillText(p.toFixed(1), plotWidth + 8, y + 3);
      }}

      // 2. Draw 5 VWAP Curves
      function drawVWAPLine(key, color, dashed = false, lineWidth = 2) {{
        chartCtx.save();
        chartCtx.strokeStyle = color;
        chartCtx.lineWidth = lineWidth;
        if (dashed) chartCtx.setLineDash([4, 4]);
        chartCtx.beginPath();

        viewData.forEach((d, i) => {{
          const x = i * candleW + candleW / 2;
          const y = getY(d[key]);
          if (i === 0) chartCtx.moveTo(x, y);
          else chartCtx.lineTo(x, y);
        }});
        chartCtx.stroke();
        chartCtx.restore();
      }}

      if (overlays.cs) drawVWAPLine('vwap_call_sell', '#f43f5e', true, 1.8);
      if (overlays.cb) drawVWAPLine('vwap_call_buy', '#f59e0b', false, 2.0);
      if (overlays.comp) drawVWAPLine('vwap_composite', '#facc15', false, 2.2);
      if (overlays.ps) drawVWAPLine('vwap_put_sell', '#06b6d4', true, 1.8);
      if (overlays.pb) drawVWAPLine('vwap_put_buy', '#10b981', false, 2.0);

      // 3. Draw Candlesticks
      viewData.forEach((d, i) => {{
        const x = i * candleW;
        const cx = x + candleW / 2;
        const isUp = d.close >= d.open;
        const bodyColor = isUp ? '#10b981' : '#f43f5e';

        const yO = getY(d.open);
        const yC = getY(d.close);
        const yH = getY(d.high);
        const yL = getY(d.low);

        // Wick
        chartCtx.strokeStyle = bodyColor;
        chartCtx.lineWidth = 1;
        chartCtx.beginPath();
        chartCtx.moveTo(cx, yH);
        chartCtx.lineTo(cx, yL);
        chartCtx.stroke();

        // Candle Body
        const bodyTop = Math.min(yO, yC);
        const bodyH = Math.max(1.5, Math.abs(yO - yC));
        const barPad = Math.max(1, candleW * 0.15);
        chartCtx.fillStyle = bodyColor;
        chartCtx.fillRect(x + barPad, bodyTop, candleW - barPad * 2, bodyH);

        // Setup Marker Dot
        if (d.setup_badge) {{
          chartCtx.fillStyle = d.setup_color;
          chartCtx.beginPath();
          chartCtx.arc(cx, isUp ? yL + 8 : yH - 8, 3, 0, Math.PI * 2);
          chartCtx.fill();
        }}
      }});

      // 4. Draw OFI Sub-Chart
      const midY = heightV / 2;
      volCtx.strokeStyle = '#334155';
      volCtx.lineWidth = 1;
      volCtx.beginPath();
      volCtx.moveTo(0, midY);
      volCtx.lineTo(plotWidth, midY);
      volCtx.stroke();

      // Zero Label
      volCtx.fillStyle = '#64748b';
      volCtx.font = '10px monospace';
      volCtx.textAlign = 'left';
      volCtx.fillText('OFI: 0', plotWidth + 8, midY + 3);

      viewData.forEach((d, i) => {{
        const x = i * candleW;
        const barPad = Math.max(1, candleW * 0.2);
        const ofiY = getVolY(d.ofi);
        const isPos = d.ofi >= 0;

        volCtx.fillStyle = isPos ? '#10b981' : '#f43f5e';
        const barH = Math.abs(ofiY - midY);
        const top = isPos ? ofiY : midY;
        volCtx.fillRect(x + barPad, top, candleW - barPad * 2, Math.max(1, barH));
      }});

      // 5. Crosshair on Hover
      if (hoveredIndex >= startIndex && hoveredIndex < startIndex + visibleCount) {{
        const relI = hoveredIndex - startIndex;
        const cx = relI * candleW + candleW / 2;
        const d = RAW_DATA[hoveredIndex];
        const cy = getY(d.close);

        // Vertical Line on both charts
        chartCtx.strokeStyle = '#94a3b8';
        chartCtx.setLineDash([3, 3]);
        chartCtx.beginPath();
        chartCtx.moveTo(cx, 0);
        chartCtx.lineTo(cx, heightC);
        chartCtx.stroke();

        volCtx.strokeStyle = '#94a3b8';
        volCtx.setLineDash([3, 3]);
        volCtx.beginPath();
        volCtx.moveTo(cx, 0);
        volCtx.lineTo(cx, heightV);
        volCtx.stroke();
        chartCtx.setLineDash([]);
        volCtx.setLineDash([]);

        // Horizontal Line on Price chart
        chartCtx.strokeStyle = '#94a3b8';
        chartCtx.setLineDash([3, 3]);
        chartCtx.beginPath();
        chartCtx.moveTo(0, cy);
        chartCtx.lineTo(plotWidth, cy);
        chartCtx.stroke();
        chartCtx.setLineDash([]);
      }}

      // 6. Time Labels on Bottom
      const timeInterval = Math.max(1, Math.floor(visibleCount / 8));
      chartCtx.fillStyle = '#64748b';
      chartCtx.font = '10px monospace';
      chartCtx.textAlign = 'center';
      viewData.forEach((d, i) => {{
        if (i % timeInterval === 0) {{
          const cx = i * candleW + candleW / 2;
          chartCtx.fillText(d.time, cx, heightC - 6);
        }}
      }});
    }}

    // Initial setup
    updateSlider();
    setTimeout(resizeCanvases, 50);
  </script>
</body>
</html>
"""

artifact_dir = r"C:\Users\Amarjeet Singh\.gemini\antigravity\brain\016d80a9-f0e3-41b7-9f7e-cc5431357f65"
os.makedirs(artifact_dir, exist_ok=True)
artifact_file = os.path.join(artifact_dir, "nifty_5vwap_interactive_chart_2026-09-21.html")
with open(artifact_file, 'w', encoding='utf-8') as f:
    f.write(html_content)

dashboard_file = 'scripts/nifty_5vwap_interactive_chart_2026-09-21.html'
with open(dashboard_file, 'w', encoding='utf-8') as f:
    f.write(html_content)

print(f"Interactive Chart Artifact saved to:")
print(f"  Artifact: {artifact_file} ({os.path.getsize(artifact_file):,} bytes)")
print(f"  Local Script: {dashboard_file} ({os.path.getsize(dashboard_file):,} bytes)")
