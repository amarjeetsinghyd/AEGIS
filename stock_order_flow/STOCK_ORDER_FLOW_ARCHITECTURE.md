```markdown
# STOCK ORDER FLOW ENGINE 3 — PRODUCTION ARCHITECTURE
**Version:** FINAL v2 (2026-08-09)  
**Status:** LIVE on Oracle VPS (1GB RAM)  
**Owner:** Amarjeet Singh  
**Architect:** Qwen + Kimi K3 + Amarjeet

---

## 0. EXECUTIVE SUMMARY

This is a **LIVE institutional-grade intraday order-flow trading engine** that:
- Trades **dynamic universe** (top-15 most volatile NIFTY-100 stocks, refreshed daily at 16:05 IST)
- Uses **3-minute candles** with **VWAP zones** (not single-line VWAP)
- Detects **absorption, breakouts, imbalances** via tick-rule footprints + L2 depth
- Exits with **ATR15-based TP/SL + Chandelier trailing stop**
- Runs **5x full leverage** (MIS product, no cap)
- Has **5% daily loss cap** on day-start snapshot
- **Non-binary confidence scoring** (0-1) for setup evaluation
- **CVD divergence** as HARD GATE for Setup B (absorption)
- **Volume Profile guard** (POC overhead-supply protection)
- **Shadow candidate logging** for future ML training data
- **Impact efficiency** and **acceptance score** metrics for setup quality
- **Regime-weighted scoring** based on intraday volatility

---

## 1. SYSTEM PHILOSOPHY

### 1.1 Why VWAP Zone (Not Single Line)?
Traditional VWAP breaks easily on noise. Our system uses:
- **Upper Band** = `cumsum(high × volume) / cumsum(volume)` (VWAP-of-Highs)
- **Lower Band** = `cumsum(low × volume) / cumsum(volume)` (VWAP-of-Lows)
- **VWAP Line** = `cumsum(typical_price × volume) / cumsum(volume)`

**Breakout must EXIT the zone** (not just cross a line) → dramatically reduces false signals.

### 1.2 Order Flow Classification
Every tick classified by **who initiated** (tick-rule):
- `lp >= sp1` → BUY-initiated (aggressive buyer hit ask)
- `lp <= bp1` → SELL-initiated (aggressive seller hit bid)
- Otherwise → NEUTRAL (ignored)

**Normalized Delta** = `(buy_vol - sell_vol) / (buy_vol + sell_vol)`  
Range: -100% (all sells) to +100% (all buys)  
Enables cross-stock comparability (RELIANCE vs M&M on same scale).

### 1.3 Context Gate
Engine only evaluates when price is in **meaningful zones**:
- **IN_ZONE**: Within ±0.20% of VWAP zone (where most volume traded)
- **AT_EXTREME**: Within 0.15% of day high/low (exhaustion detection)
- **NO_MANS_LAND**: Between zones (engine idle, no chasing runaway moves)

### 1.4 Enhanced Metrics (Kimi K3 Additions)
- **Impact Efficiency**: Price change per unit of aggressive flow → detects absorption vs thin-book fragility
- **Acceptance Score**: Measures whether breakout level is sustained (not just poked through)
- **Regime-Weighted Scoring**: Delta thresholds adapt to intraday volatility
- **Shadow Logging**: All evaluated-but-rejected candidates logged for future ML training

---

## 2. DATA FLOW TOPOLOGY

```
Shoonya WebSocket (t + d feeds, 15 tokens)
    ↓ on_message(tick_dict)
TickCompiler (alias-tolerant parsing, tick-rule, L2 depth)
    ↓
    ├→ BarBook 3m (symbol-keyed) ← strategy queries by symbol
    ├→ BarBook 15m (symbol-keyed) ← ATR15 calculation
    └→ Footprints (token-keyed) ← main translates to symbol
    ↓
    3m boundary (minute%3==0, second>=2)
    ↓
StockStrategyEngine.evaluate_all(footprints, book3, book15, now)
    ↓
Returns: (best_symbol, signal, reason, metrics) OR (None, None, None, {})
    ↓
StockExecutionManager (5x leverage, ATR15 exits, Chandelier trail)
```

**Separate process:**
```
UniverseScanner (pm2, daily 16:05 IST)
    ↓ REST daily bars (NIFTY-100)
    ↓ Score by NATR7 (volatility ranking)
    ↓ Hysteresis buffer (12/20 bands)
    ↓ Write universe.json (top-15 stocks)
    ↓ Pre-fetch 3m history (warm boot)
```

---

## 3. FILE-BY-FILE SEMANTIC MAP

### 3.1 `main.py` — THE ORCHESTRATOR

**Purpose:** Boot sequence, universe load, WS subscription, 3m boundary clock, global mutex, EOD sync.

**Key Functions:**
```python
def load_universe():
    """Read universe.json (scanner output) or fallback to DEFAULT_UNIVERSE."""
    # If file missing/corrupt/empty → DEFAULT_UNIVERSE (15 hardcoded stocks)
    # Returns: {symbol: {token, search}}

def seed_books(broker_hist, compiler, token_to_symbol):
    """Seed BOTH bar books (3m + 15m) keyed by SYMBOL."""
    for token_str, df in broker_hist.items():
        sym = token_to_symbol.get(str(token_str), str(token_str))
        compiler.bar_book.seed(sym, df)           # SYMBOL key
        df15 = resample_bars(df, 15)
        compiler.bar_book_15.seed(sym, df15)      # SYMBOL key

def main():
    """
    Boot:
    1. Load universe (universe.json or DEFAULT)
    2. Fetch 6-day 3m REST history → parquet cache
    3. Seed both bar books (symbol-keyed)
    4. Subscribe WS (touchline + depth)
    
    Main loop:
    - EOD sync at 15:17 (gap-fill REST → parquet)
    - Non-trading hours: sleep 60s
    - Trading hours: manage open trades (TP/SL/time/EOD exits)
    - 3m boundary (minute%3==0, second>=2):
        * Global Mutex check (skip if any open trade)
        * force_close_forming() all tokens → complete candles
        * evaluate_all() → (best_sym, signal, reason, metrics)
        * Execute entry if before 15:00 cutoff + live price available
        * reset_for_new_candle() → clear footprint matrix
    """
```

**Critical Logic:**
- `bucket_key` deduplication prevents re-evaluation on same 3m window
- `current_prices.get(best_sym, 0.0)` guard prevents KeyError
- `allow_entry = now.time() < TRADE_ENTRY_CUTOFF_TIME` (15:00) prevents late entries
- `compiler.set_symbol_map(token_to_symbol)` ensures symbol-keyed books

---

### 3.2 `tick_compiler.py` — THE DATA PIPELINE

**Purpose:** Convert raw WS ticks into (a) canonical 3m/15m OHLCV bars and (b) per-candle buy/sell footprint with L2 depth.

**Key Class: `TickCompiler`**
```python
class TickCompiler:
    def __init__(self, broker):
        self.bar_book = ThreeMinuteBarBook()      # 3m bars
        self.bar_book_15 = BarBook(15)            # 15m bars (for ATR15)
        self.footprints = defaultdict(list)       # token → list of trades
        self.market_depth = {}                    # token → {lp, v, bp1, sp1}
        self._t2s = {}                            # token → symbol map (set by main)
    
    def set_symbol_map(self, token_to_symbol: dict):
        """Books are SYMBOL-keyed; main calls this after loading universe."""
        self._t2s = {str(k): str(v) for k, v in token_to_symbol.items()}
    
    def _as_float(self, message: dict, *keys, default=0.0):
        """Alias-tolerant float extraction from tick message."""
        for k in keys:
            v = message.get(k, None)
            if v not in (None, ""):
                try:
                    return float(v)
                except Exception:
                    pass
        return float(default)
    
    def on_message(self, message: dict):
        """
        Process each WS tick:
        1. Extract lp, bp1, sp1, v (alias-tolerant: lp/last_traded_price, etc.)
        2. Calculate vol_delta = new_vol - old_vol (only if cumulative rises)
        3. Tick-rule classification (BUY/SELL/NEUTRAL)
        4. Extract L2 depth (bq1-5 + sq1-5, bv/sv fallback)
        5. Feed BOTH bar books with SYMBOL key (not token)
        6. Append to footprints (token-keyed, main translates later)
        """
        key = self._t2s.get(token, token)  # SYMBOL key for books
        self.bar_book.on_tick(key, lp, ts, volume_delta=vol_delta)
        self.bar_book_15.on_tick(key, lp, ts, volume_delta=vol_delta)
    
    def get_compiled_footprints(self) → dict:
        """
        Compile footprint matrix per token:
        {
            token: {
                "total_buy_vol": int,
                "total_sell_vol": int,
                "delta": buy - sell,
                "avg_l2_bid": sum(l2_bid) / tick_count,       # simple 5-level sum average (legacy)
                "avg_l2_ask": sum(l2_ask) / tick_count,       # simple 5-level sum average (legacy)
                "ew_l2_bid": sum(bq×w) / tick_count,          # end-weighted (w=[1,0.5,0.33,0.25,0.2])
                "ew_l2_ask": sum(sq×w) / tick_count,          # end-weighted
                "bid_persistence": avg(1 if bid_wall held ≥98% else 0),  # wall replenishment flag
                "ask_persistence": avg(1 if ask_wall held ≥98% else 0)
            }
        }
        """
    
    def get_market_depth_snapshot(self) → dict:
        """Return current market depth for all tokens (future use)."""
        with self.lock:
            return {k: dict(v) for k, v in self.market_depth.items()}
    
    def subscribe(self, tokens: list, exchange: str = "NSE"):
        """
        Hardened subscription:
        - Try broker.market_data, broker.mdp, broker._mdp, broker (chain)
        - If no add_tick_listener/subscribe found → log ERROR
        - Subscribe touchline (t) + depth (d) feeds
        - Handle API differences (feed_type vs positional arg)
        """
```

**Critical Design Decisions:**
- **Alias tolerance**: `_as_float()` handles both normalized (`lp`, `bid`, `ask`) and raw (`last_traded_price`, `bp1`, `sp1`)
- **Symbol-keyed books**: Strategy queries by symbol; tick_compiler translates token→symbol via `_t2s` map
- **Cumulative volume**: `vol_delta` only when `new_vol > old_vol` prevents double-counting
- **End-weighted L2 (v7)**: `_LEVEL_WEIGHTS = (1.0, 0.5, 0.33, 0.25, 0.2)` — best bid/ask level gets full weight, decays outward. Stored as `ew_l2_bid` / `ew_l2_ask` alongside legacy simple sums.
- **L2 Persistence (v7)**: `_prev_l2w` tracks previous tick's weighted bid/ask. `bid_persist=1.0` if wall held ≥ 98% of previous value, else 0.0. Initialised at 0.5 on first tick.
- **L2 fallback**: If bq1-5 missing, uses `bv`/`sv` (total bid/ask volume); ew values equal simple values in fallback.
- **Thread safety**: Lock protects footprint/bar_book from concurrent WS ticks
- **Hardened subscribe**: Tries multiple attribute names, handles TypeError for positional args

---

### 3.3 `bar_book.py` — THE CANDLE BUILDER

**Purpose:** Maintain per-token 3m/15m OHLCV bars (completed + forming), seeded from REST history.

**Key Classes:**
```python
def floor_3m(ts: datetime) → datetime:
    """Module-level helper (kept for historical_data_manager import compatibility)."""
    m = ts.minute - (ts.minute % 3)
    return ts.replace(minute=m, second=0, microsecond=0)

class BarBook:
    def __init__(self, minutes: int = 3):
        self.minutes = minutes
        self.completed = defaultdict(list)  # symbol → list of completed bars
        self.forming = {}                   # symbol → forming bar dict
        self.lock = threading.Lock()
    
    def seed(self, token: str, df: pd.DataFrame):
        """Load historical bars, filter to session hours, dedup, cap at 2000."""
        # Convert timestamp to pydatetime for session check
        # Deduplicate by timestamp
        # Cap at 2000 bars to prevent memory bloat
    
    def on_tick(self, token: str, ltp: float, ts: datetime, volume_delta: float):
        """Update forming bar; if window ended, move to completed."""
        bucket = self.floor_bar(ts)
        # If bucket changed, finalize previous forming bar
        # Update high/low/close/volume
    
    def force_close_forming(self, token: str, now: datetime):
        """Close forming bar only if window ended (3m book at each boundary)."""
        # Only finalize if timestamp < floor_bar(now)
    
    def completed_df(self, token: str, min_bars: int = 2) → pd.DataFrame:
        """Return DataFrame of completed bars (timestamp, open, high, low, close, volume)."""

class ThreeMinuteBarBook(BarBook):
    def __init__(self):
        super().__init__(3)

def resample_bars(df: pd.DataFrame, minutes: int = 15) → pd.DataFrame:
    """Resample 3m bars into 15m bars (for seeding 15m book)."""
    return df.resample(f"{minutes}min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["close"])
```

**Critical Logic:**
- **Session-hours filter**: Excludes pre-market/post-market bars from history
- **Forming bar lifecycle**: Created on first tick, updated on every tick, finalized at boundary
- **Volume accumulation**: Uses `volume_delta` (not cumulative) to avoid double-counting
- **2000-bar cap**: Prevents memory bloat on long-running sessions
- **Symbol-keyed**: All operations use symbol (not token) as key

---

### 3.4 `strategy_engine.py` — THE BRAIN

**Purpose:** Compute indicators, evaluate setups, return single best signal with ML metrics, log shadow candidates.

**Key Class: `StockStructure`**
```python
class StockStructure:
    def __init__(self):
        self.vwap = 0.0
        self.upper = 0.0       # VWAP-of-Highs
        self.lower = 0.0       # VWAP-of-Lows
        self.ema9 = 0.0
        self.ema200 = 0.0
        self.day_high = 0.0
        self.day_low = 0.0
        # Phase-2: Volume Profile levels
        self.poc = 0.0         # Point of Control (max volume price)
        self.vah = 0.0         # Value Area High (70% volume range upper)
        self.val = 0.0         # Value Area Low (70% volume range lower)
        # Phase-1: Delta tracking
        self.prev_delta = 0.0
        self.delta_hist = []   # Last 10 deltas for trend analysis
    
    def update_from_df(self, df: pd.DataFrame):
        """
        Vectorized calculation:
        1. EMA9 / EMA200 via talib
        2. Daily-anchored VWAP zone (upper/lower/vwap)
        3. Day high/low (cummax/cummin)
        4. Volume Profile (POC / VAH / VAL) from 24-bin histogram
        """
    
    def _update_volume_profile(self, df, today):
        """24-bin intraday volume histogram → POC + 70% Value Area."""
        # Distribute each bar's volume across bins it touches
        # Find POC (max volume bin)
        # Expand outward to capture 70% of total volume → VAH/VAL
    
    def context_state(self, price: float) → str:
        """Return 'IN_ZONE' / 'AT_EXTREME' / 'NO_MANS_LAND'."""
```

**Key Class: `StockStrategyEngine`**
```python
class StockStrategyEngine:
    def __init__(self):
        self.structures = {}      # symbol → StockStructure
        self.veto_until = {}      # symbol → timestamp (Setup C cooldown)
        # Phase-2: CVD tracking
        self._day_key = None
        self._cvd = {}            # symbol → cumulative delta
        self._cvd_snap = {}       # symbol → list of (cvd, close, norm_delta) snapshots
        # Kimi K3 additions
        self._shadow_log = []     # rejected candidates for ML training
        self._last_scores = {}    # last metrics per symbol
    
    def roll_day(self, now: datetime):
        """Reset daily state at day rollover."""
    
    @staticmethod
    def _atr15(bar_book_15, symbol):
        """ATR(14) on 15m completed bars (min 16 bars required)."""
    
    def _shadow(self, record: dict):
        """Log shadow candidate (rejected but evaluated)."""
        self._shadow_log.append(record)
        if len(self._shadow_log) > 1000:
            self._shadow_log = self._shadow_log[-1000:]
    
    def get_shadow_log(self):
        """Return shadow log for analysis."""
        return list(self._shadow_log)
    
    def _flow_quality(self, fpdata: dict) → float:
        """L2 bid/ask ratio as flow quality metric. Uses ew_l2_bid/ask (v7) with fallback to avg_l2_bid/ask."""
    
    def _impact_efficiency(self, close, prev_close, delta, atr) → float:
        """Price change per unit of aggressive flow (absorption detector)."""
        denom = max(abs(delta), 1.0)
        move = abs(close - prev_close)
        scale = atr if atr > 0 else max(close * 0.005, 1.0)
        return (move / denom) / max(scale, FLOW_EPS)
    
    def _regime_weight(self, df, norm_delta, ctx) → tuple[float, float]:
        """Adapt delta thresholds to intraday volatility regime."""
        # Calculate 20-bar return volatility
        # Scale delta threshold accordingly
    
    def evaluate_all(self, footprints: dict, bar_book, bar_book_15, now: datetime):
        """
        Returns: (best_symbol, signal_type, reason, metrics) or (None, None, None, {})
        
        For each symbol:
        1. Fetch completed 3m bars (min 2 bars)
        2. Update StockStructure (indicators + VP)
        3. Compute context_state (IN_ZONE / AT_EXTREME / NO_MANS_LAND)
        4. Compute norm_delta = (buy - sell) / (buy + sell)
        5. Update delta_hist (last 10 deltas)
        6. Compute delta_trend = mean(last 3 deltas)
        7. Compute ATR15 from 15m book
        
        Phase-2: CVD Divergence
        8. cvd = cumsum(candle_delta) across day
        9. window_delta = cvd - cvd_4_bars_ago
        10. div_buy = (window_delta < 0) and (close >= close_4_bars_ago × 0.999)
        11. div_sell = (window_delta > 0) and (close <= close_4_bars_ago × 1.001)
        
        Setup Evaluation:
        - Setup C (Veto): AT_EXTREME + |delta|>20% + progress<0.10% → veto 300s
        - Setup B (Absorption): IN_ZONE + |delta|>10% + CVD divergence (HARD GATE) + 
          impact efficiency + confidence ≥ 0.65
        - Setup A (Breakout): IN_ZONE + |delta|>8% + trend filter + POC guard + 
          VAH/VAL confluence + acceptance score + confidence ≥ 0.60
        - Setup D (Imbalance): |delta|>40% + directional close + delta_score ≥ 0.60
        
        Priority Sort: B (0) > A (1) > D (2), tie-break by confidence (descending)
        Returns single best candidate.
        
        Shadow Logging:
        - All rejected candidates logged with status, metrics, confidence
        - Selected candidate logged with full metrics
        
        ML Metrics Snapshot (v7 — includes persistence keys):
        {
            "ctx": "IN_ZONE",
            "norm_delta": 0.1234,
            "delta_trend": 0.0567,
            "cvd": -45000.0,
            "cvd_window_delta": -12000.0,
            "cvd_div_buy": true,
            "poc": 2948.5,
            "vah": 2951.2,
            "val": 2940.8,
            "vwap": 2947.3,
            "upper": 2952.1,
            "lower": 2945.6,
            "atr": 15.2,
            "l2_bid_ask": 2.25,
            "l2_bid_persist": 0.85,      ← NEW v7: bid wall persistence (0-1)
            "l2_ask_persist": 0.42,      ← NEW v7: ask wall persistence (0-1)
            "impact_eff": 0.000234,
            "time": "10:30"
        }
        """
```

**SETUP LOGIC (DETAILED):**

**SETUP B: ABSORPTION (CVD DIVERGENCE = HARD GATE + IMPACT EFFICIENCY)**
```
BUY conditions:
- ctx == IN_ZONE
- norm_delta < -10% (heavy selling)
- delta_trend < 0 (selling persistent)
- div_buy == TRUE (CVD falling but price not falling) ← HARD GATE
- confidence ≥ 0.65

Confidence = 0.28 × delta_score + 0.24 × progress_score + 0.26 × l2_score + 0.22 × impact_score
- delta_score = min(1.0, |norm_delta| / max(0.20, delta_scale))
- progress_score = max(0.0, 1.0 - (progress / 0.0015))
- l2_score = max(0.0, min(1.0, ew_l2_ratio / 2.0)) × (0.5 + 0.5 × bid_persist)  ← v7: persistence-weighted
  (BUY uses bid_persist; SELL uses ask_persist — persistent wall boosts score, pulled wall penalises)
- impact_score = max(0.0, min(1.0, 1.0 - min(1.0, impact_efficiency * 10.0)))

SELL conditions: mirror (norm_delta > 10%, delta_trend > 0, div_sell == TRUE)
```

**SETUP A: BREAKOUT + POC GUARD + VAH/VAL + ACCEPTANCE SCORE + REGIME-AWARE**
```
BUY conditions:
- ctx == IN_ZONE
- close > upper (VWAP-of-Highs breakout)
- close > EMA9 AND close > EMA200 (up trend)
- norm_delta > 8% (buyers dominant)
- delta_trend > 0 (momentum building)
- avg_ask < avg_bid × 2 (L2 guard: no sell wall)
- close > POC (VP guard: no overhead supply)
- confidence ≥ 0.60

Confidence = 0.22 × delta_score + 0.22 × trend_score + 0.24 × l2_score + 0.14 × vp_bonus + 0.18 × acceptance
- delta_score = min(1.0, norm_delta / max(0.15, delta_scale))
- trend_score = min(1.0, max(0.0, ((c - EMA200) / EMA200) / 0.02))
- l2_score = max(0.0, 1.0 - (avg_ask / (avg_bid × 2)))
- vp_bonus = 0.15 if close > VAH (above value area) else 0.0
- acceptance = max(0.0, min(1.0, 1.0 - abs(c - upper) / max(atr15, c * 0.005)))

SELL conditions: mirror (close < lower, down trend, delta < -8%, close < POC, vp_bonus if close < VAL)
```

**SETUP C: VETO SHIELD**
```
Trigger:
- ctx == AT_EXTREME (near day high/low)
- |norm_delta| > 20% (heavy aggression)
- progress < 0.10% (no result)

Effect: veto_until[symbol] = now + 300s → blocks Setup A and D (Setup B bypasses)
```

**SETUP D: STACKED IMBALANCE (LOWER PRIORITY)**
```
BUY conditions:
- norm_delta > 40% (3:1 buy:sell ratio)
- close > open (bullish candle)
- delta_score ≥ 0.60 (explicit threshold)

SELL conditions: norm_delta < -40%, close < open

Note: Setup D is lower priority (2) and serves as impulse evidence, not primary entry.
```

---

### 3.5 `execution_manager.py` — THE RISK MANAGER

**Purpose:** Execute LIVE orders, manage exits, persist trades, log ML features.

**Key Class: `StockExecutionManager`**
```python
class StockExecutionManager:
    def __init__(self, broker):
        self.open_trades = {}
        self.leverage = 5.0  # FULL 5x, no cap
        self.capital_engine = CapitalEngine(mode="LIVE", ...)
        # Day tracking — snapshot margin at day start for FIXED loss cap
        self._day_key = None
        self._day_start_margin = 0.0   # FIXED reference (snapshot at day start)
        self._day_loss = 0.0            # Running loss for the day
    
    def _ensure_day_snapshot(self):
        """Snapshot day-start margin once per day (for fixed loss cap).
        CRITICAL ORDER: reset _day_loss FIRST, then snapshot current funds."""
        _today = datetime.now().date()
        if self._day_key != _today:
            self._day_key = _today
            self._day_loss = 0.0  # STEP 1: clear yesterday's leftover FIRST
            current_funds = float(self.capital_engine.get_available_funds())
            self._day_start_margin = max(current_funds, 1.0)  # STEP 2: today's true start
    
    def _place_order(self, kwargs: dict):
        """Hardened order placement (handles API differences)."""
        if hasattr(self.api, "place_order"):
            return self.api.place_order(**kwargs)
        if hasattr(self.api, "placeorder"):
            return self.api.placeorder(**kwargs)
        return None
    
    def _order_history(self, ordno: str):
        """Hardened order history (handles API differences)."""
        if hasattr(self.api, "single_order_history"):
            return self.api.single_order_history(ordno)
        if hasattr(self.api, "singleorderhistory"):
            return self.api.singleorderhistory(ordno)
        return None
    
    def _confirm_fill(self, ordno: str, fallback_price: float):
        """Poll order history to confirm fill, return (filled, avg_price)."""
        avgprc = fallback_price
        for _ in range(3):
            pytime.sleep(1.0)
            hist = self._order_history(ordno)
            if hist and isinstance(hist, list):
                row = hist[0]
                status = str(row.get("status", "")).upper()
                if status in {"FILLED", "COMPLETE", "COMPLETED"}:
                    try:
                        avgprc = float(row.get("avgprc", fallback_price))
                    except Exception:
                        avgprc = fallback_price
                    return True, avgprc
        return False, avgprc
    
    def execute_entry(self, signal, symbol, spot_price, reason, metrics=None):
        """
        Pre-flight checks:
        1. Global Mutex: len(open_trades) < MAX_OPEN_POSITIONS (1)
        2. Time gate: 09:15 <= now < 15:00
        3. Margin budget >= ₹50
        4. Daily loss cap: _day_loss > -5% of day_start_margin → reject
        5. Notional = margin × 5 (FULL 5x, no cap)
        
        ATR-based TP/SL:
        - TP = 1 × ATR15
        - Initial SL = 1 × ATR15
        - Chandelier = highest - 1 × ATR15 (ratchet up only for BUY)
        - Fallback (ATR missing): TP 1.0%, SL 0.8%
        
        Order placement:
        - MKT order, MIS product (intraday), 5x leverage
        - Shoonya API: place_order(buy_or_sell, product_type="I", exchange="NSE", ...)
        
        Fill confirmation:
        - Poll single_order_history 3×1s
        - If status == "FILLED"/"COMPLETE": use avg_prc
        - Else: use spot_price (warning logged)
        
        Trade persistence:
        - Save to open_positions.json
        - Include ML metrics snapshot for future training
        """
    
    def manage_open_trades(self, current_prices: dict):
        """
        For each open trade:
        1. Compute pnl_pct
        2. Determine exit_reason:
           - EOD_FLATTEN if now >= 15:10
           - TAKE_PROFIT if current_price >= entry + ATR × 1.0
           - STOP_LOSS / CHANDELIER_EXIT based on stop_price
           - TIME_STOP if minutes_held >= 60
        3. If exit_reason: _close_trade()
        """
    
    def _close_trade(self, symbol, current_price, exit_reason):
        """
        1. Place MKT exit order (opposite action)
        2. Confirm fill (3×1s polling)
        3. Compute PnL: qty × (exit - entry) for BUY, qty × (entry - exit) for SELL
        4. Deduct transaction costs (~0.105% notional)
        5. Update daily loss ledger: _day_loss += net_pnl
        6. Save to trade_history.json (with ml_features)
        7. Remove from open_trades
        """
```

**RISK PARAMETER MASTER TABLE:**

| Parameter | Value | Where | Reasoning |
|-----------|-------|-------|-----------|
| MAX_OPEN_POSITIONS | 1 | execution | Global mutex - one trade at a time |
| LEVERAGE | 5.0 | execution | MIS product standard, NO cap |
| TP_ATR_MULT | 1.0 | execution | Fixed target = 1 × ATR15 |
| SL_ATR_MULT | 1.0 | execution | Initial stop = 1 × ATR15 |
| CHANDELIER_ATR_MULT | 1.0 | execution | Trailing stop = highest - 1 × ATR15 |
| FALLBACK_TP_PCT | 1.0% | execution | If ATR missing |
| FALLBACK_SL_PCT | 0.8% | execution | If ATR missing |
| MAX_HOLD_MINUTES | 60 | execution | Intraday only |
| Daily Loss Cap | -5% of day_start_margin | execution | Fixed reference, reset-first bugfix |
| Entry Cutoff | 15:00 | main | No late entries |
| EOD Flatten | 15:10 | execution | No overnight risk |

**Transaction Cost Model:**
```python
BROKERAGE_PCT = 0.0003   # 0.03% per trade
STT_PCT = 0.00025        # 0.025% STT on sell side
SLIPPAGE_PCT = 0.0005    # 0.05% average slippage
TOTAL_COST_PCT = 0.00105 # ~0.105% per round trip
```

---

### 3.6 `historical_data_manager.py` — THE DATA HISTORIAN

**Purpose:** Fetch 6-day 3m REST history at boot + 15:17, cache in parquet, seed bar_book.

**Key Functions:**
```python
def _parse_rows(rows):
    """Parse REST response rows into standardized format."""
    # Handle multiple timestamp formats (time, ts, timestamp)
    # Handle multiple OHLCV key formats (into/inth/intl/intc/intv vs open/high/low/close/volume)

def fetch_and_seed_history(broker, tokens: list, token_map: dict) → dict:
    """
    Returns: {token: DataFrame} of 3m bars
    
    For each token:
    1. Load local parquet (if exists)
    2. Compute last_local_ts
    3. Fetch REST gap: get_time_price_series(interval='3', starttime=last_local_ts+1, endtime=now)
    4. Parse rows: timestamp, open, high, low, close, volume
    5. Merge with local, deduplicate by timestamp
    6. Prune to 6-day window
    7. Save parquet
    """
```

**Critical Logic:**
- **Alias tolerance**: `_parse_rows()` handles multiple key formats
- **6-day rolling window**: `cutoff = now - 6 days` — older bars auto-pruned
- **Boot + 15:17 sync**: Ensures parquet is always current
- **Deduplication**: `drop_duplicates(subset=["timestamp"], keep="last")`

---

### 3.7 `universe_scanner.py` — THE DAILY HUNTER

**Purpose:** Daily scanner (16:05 IST) ranks NIFTY-100 by volatility, writes universe.json, pre-fetches 3m history.

**Key Functions:**
```python
def resolve_tokens(api, cache):
    """Resolve NIFTY-100 tokens via searchscrip/search_scrip (tries both; cached to disk).
    v2 fix: _resolve_api() walks broker chain to find the object that has searchscrip.
    _search_values() tries both method names, handles kwargs/positional, parses r['values'] list."""

def fetch_daily(api, token, days=60):
    """Daily bars from our own adapter REST, sorted old → new."""

def score_symbol(recs):
    """
    Volatility metrics + hard gates + score
    
    Gates:
    - NATR7 >= 1.2% (volatility floor, NO upper cap)
    - avg_vol(20d) >= 10L (liquidity floor)
    - close <= ₹2000 (upper price cap, NO lower cap)
    
    Score:
    - Base = NATR7 (recent heat)
    - Bonus = NATR7 × (1 + 2 × max(0, ATR7/ATR14 - 1))  # volatility expansion
    - Bonus = +0.5 if BB-width squeeze (width <= 20th percentile)
    """

def run_scan(broker):
    """
    1. Resolve tokens (cache)
    2. Fetch daily bars for NIFTY-100
    3. Score each symbol
    4. Apply hysteresis buffer:
       - Newcomers must rank <= 12
       - Incumbents survive if rank <= 20
       - Fill to TOP_N = 15
    5. Write universe.json
    6. Pre-fetch 3m history for tomorrow (warm boot)
    """

def main():
    """
    Sleep-loop process:
    - If --now: one-shot scan
    - Else: daily 16:05 IST on weekdays
    """
```

**Hysteresis Buffer:**
```
FORCE_BAND = 12    # newcomers must rank <= 12 to enter
KEEP_BAND = 20     # existing members survive if rank <= 20
TOP_N = 15         # final universe size
```

---

### 3.8 `common/broker/shoonya_adapter.py` — THE BROKER GATEWAY

**Purpose:** Unified interface for session, market data, execution, historical data.

**Key Class: `ShoonyaMarketDataProvider`**
```python
class ShoonyaMarketDataProvider:
    def __init__(self, api):
        self._api = api
        self._live_quotes = {}
        self._tick_listeners = []  # extra consumers (Stock Order Flow)
    
    def add_tick_listener(self, callback):
        """Register extra tick consumer without opening 2nd WS."""
        self._tick_listeners.append(callback)
    
    def start_live_feed(self, on_tick_callback, ...):
        """
        WS event handler:
        1. Extract token, lp, v, bp1, sp1, bq1-5, sq1-5
        2. Build std_msg (normalized keys)
        3. Call on_tick_callback(std_msg) (primary consumer)
        4. For each listener in _tick_listeners:
           listener(std_msg) (Stock Order Flow TickCompiler)
        """
```

**Critical Audit:**
- `add_tick_listener` method (line ~458) — enables TickCompiler to receive ticks without duplicate WS
- Normalized std_msg keys: `last_traded_price`, `volume_trade_for_the_day`, `bid`, `ask`, `bq1`-`bq5`, `sq1`-`sq5`

---

## 4. DEPLOYMENT ARCHITECTURE

### 4.1 VPS Configuration
- **Provider:** Oracle Cloud Free Tier (ARM Ampere A1, 1GB RAM, 4 vCPUs)
- **OS:** Ubuntu 22.04 LTS
- **Python:** 3.11
- **Process Manager:** PM2 v7.0.3

### 4.2 Directory Structure
```
/home/opc/
├── astra_bot/
│   └── stock_order_flow/        # ← ACTUAL RUNNING PATH (PM2 points here)
│       ├── main.py
│       ├── tick_compiler.py
│       ├── bar_book.py
│       ├── strategy_engine.py
│       ├── execution_manager.py
│       ├── historical_data_manager.py
│       ├── universe_scanner.py
│       ├── STOCK_ORDER_FLOW_ARCHITECTURE.md
│       ├── data/
│       │   ├── history/         # Parquet cache (6-day rolling, per symbol)
│       │   ├── trades/          # open_positions.json + trade_history.json
│       │   └── universe.json    # Daily scanner output
│       └── logs/
│           └── stock_order_flow.log
└── stock_order_flow/            # SCP landing zone (copied to astra_bot/ above)
```

### 4.3 PM2 Processes
```bash
pm2 list
# Output:
# ┌────┬────────────────────┬──────┬─────────┬──────────┐
# │ id │ name               │ mode │ status  │ restarts │
# ├────┼────────────────────┼──────┼─────────┼──────────┤
# │ 5  │ stock-order-flow   │ fork │ online  │ 0        │  ← Engine
# │ 10 │ universe-scanner   │ fork │ online  │ 0        │  ← Scanner
# └────┴────────────────────┴──────┴─────────┴──────────┘

pm2 logs stock-order-flow --lines 50
pm2 logs universe-scanner --lines 30
```

### 4.4 Deployment Workflow
```bash
# 1. Local PC (Windows PowerShell)
scp -i "Oracle SSH keys/ssh-key-2026-07-11.key" \
    stock_order_flow/*.py \
    opc@137.23.41.38:/home/opc/stock_order_flow/

# 2. SSH into VPS
ssh -i "Oracle SSH keys/ssh-key-2026-07-11.key" opc@137.23.41.38

# 3. Copy to actual running path
cp /home/opc/stock_order_flow/*.py /home/opc/astra_bot/stock_order_flow/

# 4. First universe scan (Sunday manual run)
cd /home/opc/astra_bot
python3 stock_order_flow/universe_scanner.py --now

# 5. Restart engine + start scanner daemon
pm2 restart stock-order-flow
pm2 start stock_order_flow/universe_scanner.py --name universe-scanner --interpreter python3
pm2 save

# 6. Verify logs
pm2 logs stock-order-flow --lines 30
pm2 logs universe-scanner --lines 20
```

---

## 5. MONDAY MORNING RITUAL (08:45 - 09:30)

### 5.1 Pre-Market Check (08:45)
```bash
# Check both processes are online
pm2 status

# Should show:
# stock-order-flow   online
# universe-scanner   online
```

### 5.2 Market Open Verification (09:15)
```bash
# Tail engine logs
pm2 logs stock-order-flow --lines 50

# Look for these canaries:
# ✅ "tick keys sample: ['bp1', 'lp', ...]"  ← WS ticks flowing
# ✅ "Subscribed 15 equities on NSE"         ← WS subscription successful
# ✅ "Seeded 600 completed bars for key=RELIANCE"  ← SYMBOL key (not token)
# ✅ "Seeded 125 completed bars for key=RELIANCE"  ← 15m book seeded
```

### 5.3 First Boundary Evaluation (09:18)
```bash
# At 09:18:02, you should see:
# ========== 3m BOUNDARY EVALUATION ==========

# If no setup fires:
# (no candidates, engine idle)

# If setup fires:
# 🏆 MUTEX WINNER: RELIANCE -> BUY | Setup A Breakout (Up). Delta 12.5%, Conf 0.72
# [StockExecution] Placing LIVE B MKT for 33x RELIANCE...
# [StockExecution] ✅ Entry FILLED @ 2950.00
# ATR15 exits: TP=15.00, InitSL=15.00, Chandelier=15.00
```

### 5.4 Scanner Verification (16:05)
```bash
# At 16:05, scanner should run automatically
pm2 logs universe-scanner --lines 30

# Look for:
# ✅ "Universe scan done: 15 stocks -> /home/opc/astra_bot/stock_order_flow/data/universe.json"
# ✅ Top stocks: RELIANCE: score=2.5 natr7=1.8% ratio=1.2 squeeze=False
# ✅ "Pre-fetch of 3m history complete."
```

---

## 6. KNOWN ISSUES & TROUBLESHOOTING

### 6.1 Weekend Scanner Artifact (Expected)
**Issue:** Scanner returns 0 stocks on Saturday/Sunday  
**Cause:** Shoonya REST `interval='D'` (daily bars) unavailable on weekends  
**Behavior:** Engine falls back to DEFAULT_UNIVERSE (15 hardcoded stocks)  
**Fix:** None needed — Monday 16:05 scanner will populate real universe

### 6.2 No Ticks Arriving
**Symptoms:** No footprint accumulation, no boundary evaluations  
**Fix:**
```bash
# Check WS subscription
pm2 logs stock-order-flow | grep "Subscribed"
# Should show: "Subscribed 15 equities on NSE (touchline+depth)"

# Check tick keys
pm2 logs stock-order-flow | grep "tick keys sample"
# Should show: "tick keys sample: ['bp1', 'lp', ...]"
```

### 6.3 Setup A Blocked Frequently
**Symptoms:** Logs show "Setup A BUY blocked — close below POC"  
**Fix:** Normal behavior — VP guard working. If too aggressive, relax to `c < struct.poc × 0.995`

### 6.4 Setup B Not Firing
**Symptoms:** Heavy delta but no trade  
**Fix:** Check CVD divergence flag; ensure 4-candle window populated (`len(snaps) >= 4`)

### 6.5 Daily Loss Cap Hit
**Symptoms:** "Daily loss cap hit — no new entries today"  
**Fix:** Review `trade_history.json` for losing streak; consider pausing for manual review

### 6.6 VPS RAM Usage High
**Symptoms:** `free -h` shows <200MB available  
**Fix:**
```bash
# Check for orphan Python processes
pgrep python3

# Stop if needed
pm2 stop stock-order-flow
pm2 delete stock-order-flow
```

### 6.7 ImportError: floor_3m
**Symptoms:** Engine crashes on boot with `ImportError: cannot import name 'floor_3m'`  
**Fix:** Ensure `bar_book.py` has module-level `def floor_3m()` function (added for compatibility)

### 6.8 Token vs Symbol Key Mismatch
**Symptoms:** Strategy queries `completed_df("RELIANCE")` but gets empty DataFrame  
**Cause:** Bar books were token-keyed, strategy queries by symbol  
**Fix:** Ensure `tick_compiler.set_symbol_map()` is called by main after loading universe

### 6.9 Dunder/Underscore Stripping (Kimi K3 Bug)
**Symptoms:** `ImportError: cannot import name 'getlogger'` (should be `get_logger`)  
**Cause:** LLM code generation strips `__` and `_` from identifiers  
**Fix:** PowerShell regex replacement to restore dunders:
```powershell
$c = $c -replace 'from future import', 'from __future__ import'
$c = $c -replace 'getlogger', 'get_logger'
$c = $c -replace 'def init\(', 'def __init__('
```

---

## 7. ROLLBACK PROCEDURE

```bash
# If Phase-3 causes issues:
cd /home/opc/astra_bot
git checkout pre-stock-live -- stock_order_flow/
pm2 restart stock-order-flow

# Or SCP V4.4 base files from GitHub tag
```

---

## 8. APPENDIX: COMPLETE TRADE LIFECYCLE

### Example: RELIANCE Setup B BUY at 10:30 AM

**10:27–10:30 (Candle Formation):**
- 15,000 ticks arrive via WS
- TickCompiler classifies: 40,000 shares BUY-initiated, 1,60,000 shares SELL-initiated
- BarBook updates: open 2950.0, high 2951.5, low 2947.8, close 2949.6, volume 2,00,000
- Footprint: avg_l2_bid 9,000, avg_l2_ask 4,000

**10:30:02 (Boundary Evaluation):**
- `force_close_forming()` → candle finalized
- `evaluate_all()`:
  - StockStructure.update_from_df():
    - upper = 2952.1, lower = 2945.6, vwap = 2947.3
    - POC = 2948.5, VAH = 2951.2, VAL = 2940.8
  - context_state(2949.6) → IN_ZONE (within upper/lower)
  - norm_delta = (40k - 160k) / 200k = -60%
  - progress = |2949.6 - 2950.0| / 2949.6 = 0.014%
  - CVD: prev 4 candles = -45,000, current = -48,000 → window_delta = -3,000
  - div_buy = (window_delta < 0) and (2949.6 >= 2950.0 × 0.999) = TRUE
  - impact_efficiency = |2949.6 - 2950.0| / 120,000 / 15.0 = 0.000002
  - impact_score = 1.0 - min(1.0, 0.000002 × 10.0) = 1.0
  - Setup B check:
    - IN_ZONE ✓
    - |delta| > 10% ✓ (-60%)
    - delta_trend < 0 ✓
    - div_buy ✓ (HARD GATE)
    - confidence = 0.28 × 1.0 + 0.24 × 0.91 + 0.26 × 1.0 + 0.22 × 1.0 = 0.97 ≥ 0.65 ✓
  - → Candidate: (0, "RELIANCE", "BUY", "Setup B Absorption+CVD-div: Delta -60.0%, Conf 0.97", metrics, 0.97)

**10:30:03 (Entry Execution):**
- Global Mutex: no open trades ✓
- Time: 10:30 < 15:00 cutoff ✓
- Margin: ₹50,000 available ✓
- Daily loss: -₹500 (well within -5% cap of ₹1,00,000) ✓
- Notional: ₹50,000 × 5 = ₹2,50,000 → qty = 84 shares
- ATR15: ₹15.2
- Order: MKT BUY 84 RELIANCE @ 0 (MKT), MIS product
- Fill: 3×1s polling → FILLED at ₹2949.8
- Trade saved: {symbol, type, entry_price, qty, margin, reason, broker_order_id, atr, ml_features}

**10:30:03–11:30:03 (Trade Management):**
- 10:52 AM: price reaches ₹2965.0 (+15.2, = 1×ATR15) → TAKE_PROFIT triggered
- Exit: MKT SELL 84 RELIANCE → FILLED at ₹2965.2
- Gross PnL: 84 × (2965.2 - 2949.8) = ₹1,293.60
- Txn cost: 84 × ((2949.8 + 2965.2) / 2) × 0.00105 = ₹259.56
- Net PnL: ₹1,293.60 - ₹259.56 = ₹1,034.04
- Daily loss ledger: -500 + 1034 = +534
- Trade appended to trade_history.json with exit_reason = "TAKE_PROFIT"

---

## 9. GLOSSARY

| Term | Definition |
|------|------------|
| **Normalized Delta** | (buy_vol - sell_vol) / (buy_vol + sell_vol), ranges -100% to +100% |
| **VWAP Zone** | Range between VWAP-of-Highs and VWAP-of-Lows |
| **POC** | Point of Control - price level with max volume in Volume Profile |
| **Value Area** | 70% volume range around POC (VAH = upper, VAL = lower) |
| **CVD** | Cumulative Volume Delta - running sum of (buy - sell) across day |
| **CVD Divergence** | CVD moving opposite to price (e.g., CVD falling but price stable) |
| **ATR15** | ATR(14) on 15m bars (for TP/SL/Chandelier) |
| **Chandelier Exit** | Trailing stop = highest - 1×ATR15 (ratchet up only) |
| **Setup A** | Momentum Breakout (upper/lower + trend filter + POC guard + VAH/VAL + acceptance + confidence) |
| **Setup B** | Absorption (heavy delta + CVD divergence HARD GATE + impact efficiency + confidence) |
| **Setup C** | Veto Shield (exhaustion at extremes → 5-min cooldown) |
| **Setup D** | Stacked Imbalance (sustained high delta + directional close, lower priority) |
| **IN_ZONE** | Price within ±0.20% of VWAP zone envelope |
| **AT_EXTREME** | Price within 0.15% of day high/low |
| **NO_MANS_LAND** | Price between zones (engine inactive) |
| **NATR7** | Normalized ATR(7) % - recent volatility heat |
| **Hysteresis Buffer** | Universe stability rule (newcomers rank <= 12, incumbents survive to rank <= 20) |
| **Impact Efficiency** | Price change per unit of aggressive flow (absorption detector) |
| **Acceptance Score** | Measures whether breakout level is sustained (not just poked) |
| **Shadow Logging** | Logging all evaluated-but-rejected candidates for ML training |
| **Regime Weight** | Adapting delta thresholds to intraday volatility |

---

## 10. VERSION HISTORY

| Version | Date | Changes |
|---------|------|---------|
| V4.4 Base | 2026-08-08 | Initial deployment: VWAP zones, 4 setups, risk caps, exits |
| V4.4 + Phase-2 | 2026-08-09 | CVD divergence, Volume Profile guard, ML feature logging |
| FINAL | 2026-08-09 | 5x full leverage, ATR15 exits + Chandelier, 5% cap snapshot-fix, dual bar books (3m+15m), dynamic universe scanner (NATR7 gate, TOP_N 15, hysteresis), symbol-keyed books, floor_3m compat |
| FINAL v2 | 2026-08-09 | Kimi K3 enhancements: shadow logging, impact efficiency, acceptance score, regime-weighted scoring, hardened subscribe, dunder/underscore bug fixes |
| v7 (tick+strategy) | 2026-08-10 | End-weighted L2 (_LEVEL_WEIGHTS), bid/ask persistence tracking (_prev_l2w), 4 new footprint keys (ew_l2_bid/ask, bid/ask_persistence), Setup B l2_score persistence-multiplied, 2 new ML metrics keys (l2_bid/ask_persist) |
| v2 (scanner) | 2026-08-10 | Fixed Noren token resolution: _resolve_api() chain walk, _search_values() tries searchscrip+search_scrip, parses r['values'] key correctly |
| dashboard fix | 2026-08-10 | Removed ShoonyaAdapter/Selenium/Chrome crash; _capital() reads capital_state.json directly; OF log wired to stock_order_flow.log; of{} block now includes signal/reasoning/top_setup/open_pnl/data_seeded; default_log_engine='of' |

---

## 11. COMMON ERRORS & AUTO-FIX GUIDE (FOR ANTIGRAVITY/SONNET)

### Error: `ImportError: cannot import name 'floor_3m'`
**File:** `bar_book.py`  
**Fix:** Add module-level function:
```python
def floor_3m(ts: datetime) -> datetime:
    """Module-level helper (kept for historical_data_manager import compatibility)."""
    m = ts.minute - (ts.minute % 3)
    return ts.replace(minute=m, second=0, microsecond=0)
```

### Error: Strategy queries `completed_df("RELIANCE")` but gets empty DataFrame
**File:** `tick_compiler.py` + `main.py`  
**Cause:** Bar books were token-keyed, strategy queries by symbol  
**Fix:**
1. In `tick_compiler.py`, ensure `set_symbol_map()` is called by main
2. In `on_message()`, use `key = self._t2s.get(token, token)` for bar books
3. In `main.py`, call `compiler.set_symbol_map(token_to_symbol)` after loading universe

### Error: `Universe scan done: 0 stocks`
**File:** `universe_scanner.py`  
**Cause:** Weekend (Saturday/Sunday) — daily REST unavailable  
**Fix:** None needed — engine falls back to DEFAULT_UNIVERSE; Monday 16:05 scanner will populate real universe

### Error: `Subscribe failed` or no ticks
**File:** `tick_compiler.py`  
**Cause:** Broker chain doesn't have `add_tick_listener` or `subscribe`  
**Fix:** Hardened `subscribe()` method tries multiple attribute names; if all fail, logs ERROR

### Error: `Daily loss cap hit` too early
**File:** `execution_manager.py`  
**Cause:** `_day_loss` not reset before snapshot  
**Fix:** Ensure `_ensure_day_snapshot()` resets `_day_loss = 0.0` BEFORE snapshotting `current_funds`

### Error: ATR15 returns 0.0
**File:** `strategy_engine.py`  
**Cause:** 15m book has < 16 bars  
**Fix:** Ensure `resample_bars()` is called during seeding; fallback to fixed % exits (TP 1.0%, SL 0.8%)

### Error: `ImportError: cannot import name 'getlogger'`
**File:** All `.py` files  
**Cause:** LLM code generation stripped underscores  
**Fix:** PowerShell regex replacement (see Section 6.9)

---

## 12. FINAL NOTES

This document is the **single source of truth** for the Stock Order Flow Engine. Any AI reading this should be able to:
1. Narrate the entire system architecture
2. Debug common issues using the troubleshooting guide
3. Propose enhancements without breaking existing logic
4. Understand the Monday morning ritual
5. Auto-fix common errors using the guide in Section 11

**Last Updated:** 2026-08-10  
**Status:** PRODUCTION READY — v7 deployed pending market close

---

**END OF DOCUMENT**
```