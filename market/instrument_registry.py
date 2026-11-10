from __future__ import annotations
from typing import Dict, Iterable, List, Optional
from .instrument import UniversalInstrument
from market.instrument_repository import InstrumentRepository

# In-memory overlay for backward compatibility and test mocks
_in_memory_registry: Dict[str, UniversalInstrument] = {}

def register(instrument: UniversalInstrument) -> None:
    """Add or replace an instrument in the in-memory mock registry."""
    _in_memory_registry[instrument.symbol.upper()] = instrument

def bulk_register(instruments: Iterable[UniversalInstrument]) -> None:
    """Register multiple instruments in the in-memory registry."""
    for inst in instruments:
        register(inst)

def get(symbol: str) -> UniversalInstrument:
    """Retrieve an instrument by its universal symbol (checks memory mock first, then database)."""
    symbol_upper = symbol.upper()
    if symbol_upper in _in_memory_registry:
        return _in_memory_registry[symbol_upper]
        
    repo = InstrumentRepository()
    row = repo.get_instrument_by_symbol(symbol_upper)
    if not row:
        raise KeyError(f"Symbol '{symbol}' not found in registry or database.")
        
    return UniversalInstrument(
        symbol=row['symbol'],
        exchange=row['exch_seg'],
        description=row['description']
    )

def all_instruments() -> List[UniversalInstrument]:
    """Return all registered instruments (combines database and in-memory mock)."""
    repo = InstrumentRepository()
    rows = repo.get_all_symbols()
    
    db_instruments = {
        row['symbol']: UniversalInstrument(
            symbol=row['symbol'],
            exchange=row['exch_seg'],
            description=row['description']
        )
        for row in rows
    }
    
    # Merge with memory registry taking priority
    combined = {**db_instruments, **_in_memory_registry}
    return [combined[key] for key in sorted(combined)]

def clear_mock_registry() -> None:
    """Helper to clear the memory registry (useful for unit tests)."""
    _in_memory_registry.clear()
