"""
AEGIS Instrument Repository re-export stub.
Canonical single source of truth is common.market.instrument_repository.
"""
from common.market.instrument_repository import (
    InstrumentRepository,
    DB_PATH,
    SCHEMA_VERSION,
)

__all__ = ["InstrumentRepository", "DB_PATH", "SCHEMA_VERSION"]
