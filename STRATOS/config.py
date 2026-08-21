"""
config.py — STRATOS Central Configuration.
Pure 15-Minute Classical Market Structure & S/R Flip Architecture.
Multi-Timeframe: 1H Macro Trend + 15m HH2/LL2 Structure Zones + 1:4 RR & 15m Trailing Exits.
"""
from datetime import time

STRATOS_CONFIG = {
    # Universe & Execution
    "universe_size": 110,
    "max_open_positions": 1,
    "max_trades_per_day": 8,
    "leverage": 5.0,
    "margin_safety": 1.0,
    
    # Multi-Timeframe Time Candlestick Engine
    "max_history_bars": 200,
    "primary_timeframe": "15m",
    "macro_timeframe": "1h",
    "trigger_timeframe": "5m",
    
    # Macro Trend (1H EMA & Price Action)
    "macro_ema_fast": 20,
    "macro_ema_slow": 50,
    "min_macro_spread_bps": 1.5,
    
    # 15m Market Structure Swings (HH1 -> HH2 & LL1 -> LL2)
    "swing_lookback_bars": 1,          # Lookback bars for pivot detection
    "min_hh_count": 2,                 # Minimum consecutive Higher Highs for Bullish Breakout
    "min_ll_count": 2,                 # Minimum consecutive Lower Lows for Bearish Breakdown
    "breakout_buffer_pct": 0.0002,     # 0.02% clean break above/below prior pivot
    
    # 15m High-Low S/R Zones
    "support_zone_low_mult": 0.9970,   # [HH2 * 0.9970, HH2 * 1.0025]
    "support_zone_high_mult": 1.0025,
    "resistance_zone_low_mult": 0.9975,# [LL2 * 0.9975, LL2 * 1.0030]
    "resistance_zone_high_mult": 1.0030,
    
    # Pullback & Order Flow Reversal Trigger on S/R Zone
    "require_positive_delta": True,    # Trigger candle must have buyer delta for Longs
    "min_rejection_wick_ratio": 0.35,  # Rejection wick >= 35% of bar range for absorption
    "max_pullback_bars": 12,           # Max bars allowed for pullback to support
    
    # Anti-Liquidity Hunting & Risk Rules
    "anti_hunt_buffer_pct": 0.0015,    # 0.15% breathing room below lowest pullback wick
    "anti_hunt_atr_fraction": 0.25,    # 0.25x ATR floor buffer
    "min_reward_risk_ratio": 2.0,      # Minimum 1:2 RR for TP1
    "tp2_target_rr": 4.0,              # 1:4 RR for runner (Optimized canonical target)
    
    # Session-Adaptive Trailing Engine (15m Candle Low/High + Close Pierce)
    "entry_start_time": time(9, 20),   # No entry in first 5 mins (opening volatility)
    "entry_cutoff_time": time(15, 0),  # No new entries after 15:00 IST (3:00 PM)
    "session_split_time": time(13, 30), # 13:30 IST split
    "morning_trailing_ema": 20,        # 20-period 15m EMA for wide trend ride (09:15-13:30)
    "afternoon_trailing_ema": 9,       # 9-period 15m EMA for tight profit lock (13:30-15:15)
    "eod_squareoff_time": time(15, 10), # Force close remaining positions at 15:10
}
