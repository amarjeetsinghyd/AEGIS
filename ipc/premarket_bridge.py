"""
Pre-Market IPC Bridge: ARGUS -> AEGIS
Shares 09:08-09:14 Cash Stock Auction Data & Indicative Index Gap Bias
with AEGIS Gamma Engine for pre-warming ATM strikes and early opening trades.
"""
from __future__ import annotations
import os
import json
import time
from datetime import datetime, date
from typing import Optional, Dict, Any

from config.settings import get_data_dir
from logger import get_logger

logger = get_logger("premarket_bridge")

IPC_DIR = os.path.join(get_data_dir(), "ipc")
PREMARKET_FILE = os.path.join(IPC_DIR, "premarket_state.json")

# Top heavyweights and approximate weights for NIFTY / SENSEX
NIFTY_WEIGHTS = {
    "HDFCBANK": 0.135,
    "RELIANCE": 0.100,
    "ICICIBANK": 0.085,
    "INFY": 0.060,
    "ITC": 0.045,
    "TCS": 0.040,
    "LT": 0.038,
    "AXISBANK": 0.035,
    "KOTAKBANK": 0.030,
    "SBIN": 0.030,
    "BHARTIARTL": 0.030
}


def publish_premarket_state(
    indicative_spot: float,
    gap_pct: float,
    direction: str,
    constituents_bias: float,
    movers: Optional[Dict[str, Any]] = None
) -> None:
    """ARGUS calls this between 09:08 and 09:14 IST to publish cash auction equilibrium."""
    try:
        os.makedirs(IPC_DIR, exist_ok=True)
        payload = {
            "timestamp": datetime.now().isoformat(),
            "date": datetime.now().date().isoformat(),
            "indicative_spot": round(float(indicative_spot), 2),
            "gap_pct": round(float(gap_pct), 3),
            "direction": direction,
            "constituents_bias": round(float(constituents_bias), 3),
            "movers": movers or {}
        }
        # Atomic write
        tmp_file = f"{PREMARKET_FILE}.tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_file, PREMARKET_FILE)
        logger.info(f"[PreMarketBridge] Published snapshot: Spot={indicative_spot:.2f} | Gap={gap_pct:+.2f}% | Dir={direction}")
    except Exception as e:
        logger.error(f"[PreMarketBridge] Failed to publish snapshot: {e}")


def read_premarket_state() -> Optional[Dict[str, Any]]:
    """AEGIS calls this between 09:08 and 09:15 IST to read pre-market equilibrium."""
    if not os.path.exists(PREMARKET_FILE):
        return None
    try:
        with open(PREMARKET_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Verify date is today
        file_date = data.get("date")
        if file_date != datetime.now().date().isoformat():
            return None
        return data
    except Exception as e:
        logger.debug(f"[PreMarketBridge] Read failed: {e}")
        return None
