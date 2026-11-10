"""AEGIS Index Specifications - Single Source of Truth for Index Symbols and Parameters"""
from __future__ import annotations

INDEX_SPECS = {
    "NIFTY": {
        "symbol": "NIFTY",
        "index_token": "26000",
        "strike_step": 50,
        "lot_size": 65,
        "trading_days": ["Monday", "Tuesday"],
    },
    "SENSEX": {
        "symbol": "SENSEX",
        "index_token": "1",
        "strike_step": 100,
        "lot_size": 20,
        "trading_days": ["Wednesday", "Thursday"],
    },
}

# Decommissioned equities universe stubs (kept empty for zero-entropy)
SUBSCRIPTION_UNIVERSE: set[str] = set()
NIFTY100: list[str] = []
