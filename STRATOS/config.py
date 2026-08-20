"""
config.py — STRATOS Central Configuration.
Pure Information-Time (Turnover / Volume Bucket) Architecture.
No clock-based candle slicing.
"""
from datetime import time

STRATOS_CONFIG = {
    # Universe & Execution
    "universe_size": 110,
    "max_open_positions": 1,
    "max_trades_per_day": 8,
    "leverage": 5.0,
    "margin_safety": 1.0,
    
    # Information-Time Turnover Bucket Engine
    # ₹2.0 Crore (Rs. 20,000,000) default bucket size for institutional wave resolution
    "turnover_bucket_inr": 20_000_000.0,
    "min_bucket_ticks": 10,
    "max_history_buckets": 200,
    
    # Macro Trend (20-Bucket Trend EMA)
    "macro_ema_fast": 20,
    "macro_ema_slow": 50,
    "min_macro_spread_bps": 1.5,
    
    # Turnover-Wave Market Structure (HH2 & HL Tracking)
    "swing_lookback_buckets": 2,       # Buckets to the left/right for pivot detection
    "min_hh_count": 2,                 # Minimum consecutive Higher Highs on turnover waves
    "min_hl_count": 1,                 # Minimum Higher Lows
    "breakout_buffer_pct": 0.0005,     # 0.05% clean break above prior high
    
    # Pullback & Volume Contraction
    "max_pullback_buckets": 12,        # Max buckets allowed for pullback to support
    "max_pullback_vol_ratio": 0.85,    # Pullback volume must contract relative to breakout wave
    "retest_touch_tolerance_pct": 0.0025, # Within 0.25% of support line
    "strict_invalidation_buffer_pct": 0.0020, # Max 0.20% breach below support before invalidation
    
    # Order Flow & Reversal Trigger on Support Retest
    "require_positive_delta": True,    # Trigger bucket must have positive buyer delta
    "min_delta_ratio": 0.05,           # Delta / Total Volume >= 5%
    "min_rejection_wick_ratio": 0.35,  # Lower wick >= 35% of bucket range for absorption
    "require_close_above_open": True,  # Bullish closing bucket
    
    # Anti-Liquidity Hunting & Risk Rules
    "anti_hunt_buffer_pct": 0.0015,    # 0.15% breathing room below lowest pullback wick
    "anti_hunt_atr_fraction": 0.25,    # 0.25x ATR floor buffer
    "min_reward_risk_ratio": 2.0,      # Minimum 1:2 RR for TP1
    "tp2_target_rr": 3.0,              # 1:3 RR for runner
    
    # Session-Adaptive Trailing Engine
    "entry_start_time": time(9, 20),   # No entry in first 5 mins (opening volatility)
    "entry_cutoff_time": time(14, 30),  # No new entries after 14:30 IST
    "session_split_time": time(13, 30), # 13:30 IST split
    "morning_trailing_ema": 20,        # 20-bucket EMA for wide trend ride (09:15-13:30)
    "afternoon_trailing_ema": 9,       # 9-bucket EMA for tight profit lock (13:30-15:15)
    "eod_squareoff_time": time(15, 10), # Force close remaining positions at 15:10
}
