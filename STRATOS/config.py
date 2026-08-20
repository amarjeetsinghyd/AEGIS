"""
STRATOS Configuration — Parameters for Market Structure, Multi-Timeframe Confluence,
TA-Lib Candlestick Detection, Order-Flow Absorption, and Anti-Hunt Risk.
"""
from datetime import time

STRATOS_CONFIG = {
    # Universe & Execution
    "universe_size": 110,
    "max_open_positions": 1,
    "max_trades_per_day": 5,
    "leverage": 5.0,
    "margin_safety": 1.0,
    
    # Macro Trend (1-Hour Chart)
    "macro_ema_fast": 20,
    "macro_ema_slow": 50,
    "min_macro_spread_bps": 2.0,      # Minimum widening spread between 20 and 50 EMA
    
    # Market Structure (15-Min & 5-Min Swings)
    "swing_lookback_bars": 3,         # Bars to the left/right for pivot high/low detection
    "min_hh_count": 2,                # Minimum consecutive Higher Highs required
    "min_hl_count": 2,                # Minimum consecutive Higher Lows required
    "breakout_buffer_pct": 0.0005,    # 0.05% clean close above prior high
    
    # Pullback & Retest Quality (5-Min Bars)
    "max_pullback_bars": 12,          # Max 5-min candles allowed for retest (1 hour)
    "max_pullback_vol_ratio": 0.70,   # Pullback volume must be < 70% of breakout volume
    "retest_touch_tolerance_pct": 0.0015, # Within 0.15% of support line
    "strict_invalidation_buffer_pct": 0.0005, # Max 0.05% breach allowed before invalidation
    
    # Candlestick & Confluence Trigger
    "rsi_period": 14,
    "rsi_oversold_max": 45.0,         # RSI on pullback must be <= 45.0 (oversold territory)
    "rsi_oversold_min": 25.0,
    "trigger_ema": 9,                 # Price must cross & close above 9 EMA
    
    # Order Flow & Microstructure (On-Demand at Support Line)
    "min_support_delta_ticks": 15,    # Last 15 ticks delta check
    "min_whale_z_score": 0.6,         # Institutional buying footprint at support
    "min_bid_replenishment_ratio": 1.1, # Bid depth expanding over ask depth
    
    # Anti-Liquidity Hunting & Risk Rules
    "anti_hunt_buffer_pct": 0.0015,   # 0.15% breathing room below rejection wick
    "anti_hunt_atr_fraction": 0.30,   # 0.3x ATR15 floor buffer
    "min_reward_risk_ratio": 2.0,     # Minimum 1:2 RR for TP1
    "tp2_target_rr": 3.0,             # 1:3 RR for runner
    
    # Session-Adaptive Trailing Engine
    "session_split_time": time(13, 30), # 13:30 IST split
    "morning_trailing_ema": 20,       # 20 EMA on 5-min for wide multi-hour ride (09:15-13:30)
    "afternoon_trailing_ema": 9,      # 9 EMA on 5-min for tight profit lock (13:30-15:15)
    "eod_squareoff_time": time(15, 10),# Force close remaining runners at 15:10
}
