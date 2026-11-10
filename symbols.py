"""AEGIS Index Specifications - Single Source of Truth for Index Symbols and Parameters"""
from __future__ import annotations

INDEX_SPECS = {
    "NIFTY": {
        "symbol": "NIFTY",
        "index_token": "26000",
        "strike_step": 50,
        "lot_size": 65,
        "trading_days": ["Monday", "Tuesday", "Friday"],  # Friday is 4-DTE opt-in via AEGIS_FRIDAY_TRADING_ENABLED
    },
    "SENSEX": {
        "symbol": "SENSEX",
        "index_token": "1",
        "strike_step": 100,
        "lot_size": 20,
        "trading_days": ["Wednesday", "Thursday"],
    },
}
