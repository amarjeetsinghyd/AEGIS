"""AEGIS infra — instrument master (unified SQLite repository + registry), option tradability filter, O(1) hot structures
(BarRing / Ema / Latest), L2 Parquet archiver, shadow boundary observer.

Consolidated from instrument_repository.py (+ the diverged instrument_repository_1.py), instrument_registry.py,
option_quality.py, hot_structures.py, l2_options_archiver.py, shadow_boundary_observer.py, l2_guard.py.
"""
from __future__ import annotations
import numpy as np
import os
import sqlite3
import json
import contextlib
import sys
import time as pytime
import queue
import threading
import pandas as pd
import time
import logging
from typing import Dict, List, Optional, Tuple, Any, Generic, TypeVar, Iterable
from dataclasses import dataclass, asdict
from datetime import datetime, time as dtime, timedelta
from common.logger import get_logger
from common.config import get_data_dir

class OptionQualityEngine:
    """
    Qualifies the specific Option Contract for retail buying execution.
    """

    def __init__(
        self,
        max_spread_pct: float = 2.5,        # Max 2.5% bid-ask spread
        min_top5_depth_qty: float = 500.0,  # At least 500 units in L2 depth
        min_premium: float = 4.0,           # Testing phase: allow Rs 4+ (was 5.0)
        max_premium: float = 400.0,         # Cap ultra-deep ITM options
    ):
        self.max_spread_pct = max_spread_pct
        self.min_top5_depth_qty = min_top5_depth_qty
        self.min_premium = min_premium
        self.max_premium = max_premium

    def effective_floor(self, available_funds: float = 0.0, lot_size: int = 0) -> float:
        """Dynamic floor override for Tiny Funds / Testing Mode.

        Mirrors common/execution/strike_selector.effective_premium_floor:
        Effective Floor = min(env_premium_min, budget_ceiling - safety_buffer),
        applied only when tiny funds would otherwise put the configured floor
        above the per-lot budget ceiling (dead zone). Without funds context the
        configured floor is returned unchanged.
        """
        try:
            import os as _os
            funds = float(available_funds or 0)
            lot = int(lot_size or 0)
            tiny = float(_os.getenv("AEGIS_TESTING_TINY_FUNDS", "2000.0"))
            if funds <= 0 or lot <= 0 or funds >= tiny:
                return float(self.min_premium)
            env_floor = float(_os.getenv("AEGIS_MIN_PREMIUM", "4.0"))
            buf = float(_os.getenv("AEGIS_PREMIUM_SAFETY_BUFFER", "0.50"))
            dyn = min(env_floor, funds / lot - buf)
            if dyn < float(self.min_premium):
                return max(0.0, dyn)
            return float(self.min_premium)
        except Exception:
            return float(self.min_premium)

    def qualify_option(
        self,
        sym: str,
        lp: float,
        bp1: float,
        sp1: float,
        top5_bid_qty: float,
        top5_ask_qty: float,
        available_funds: float = 0.0,
        lot_size: int = 0,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Validates tradability and liquidity of the target option strike.
        When available_funds/lot_size are provided in Tiny Funds mode, the
        premium floor is dynamically clamped so it can never exceed the
        per-lot budget ceiling (dead-zone fix). Callers without funds context
        keep the configured floor exactly as before.
        Returns:
            (is_qualified, reason, metrics_dict)
        """
        metrics: Dict[str, Any] = {
            "symbol": sym,
            "ltp": lp,
            "bp1": bp1,
            "sp1": sp1,
            "spread_pct": 0.0,
            "top5_bid_qty": top5_bid_qty,
            "top5_ask_qty": top5_ask_qty,
        }

        # 1. Price range check (dynamic floor in testing mode)
        _eff_min = self.effective_floor(available_funds, lot_size)
        if lp < _eff_min:
            return False, f"premium_too_low ({lp:.2f} < {_eff_min})", metrics
        if lp > self.max_premium:
            return False, f"premium_too_high ({lp:.2f} > {self.max_premium})", metrics

        # 2. Spread calculation (Fail-closed: missing quote or crossed quote rejects immediately)
        if bp1 <= 0 or sp1 <= 0 or sp1 <= bp1:
            return False, f"invalid_or_missing_l1_quote (bp1={bp1}, sp1={sp1})", metrics

        mid = (bp1 + sp1) / 2.0
        spread = sp1 - bp1
        spread_pct = (spread / mid) * 100.0
        metrics["spread_pct"] = round(spread_pct, 2)

        if spread_pct > self.max_spread_pct:
            return False, f"spread_too_wide ({spread_pct:.2f}% > {self.max_spread_pct}%)", metrics

        # 3. Depth liquidity check (Fail-closed: zero depth rejects immediately, scale-invariant)
        total_depth = top5_bid_qty + top5_ask_qty
        eff_min_depth = max(50.0, 5.0 * lot_size)  # Scale-invariant (5 lots minimum depth)
        if total_depth < eff_min_depth:
            return False, f"insufficient_depth ({total_depth:.0f} < {eff_min_depth:.0f})", metrics

        return True, "option_qualified", metrics

T = TypeVar("T")

class BarRing:
    __slots__ = ("cap", "head", "count", "t", "o", "h", "l", "c", "v")

    def __init__(self, cap: int = 512):
        self.cap, self.head, self.count = cap, 0, 0
        for name in ("t", "o", "h", "l", "c", "v"):
            setattr(self, name, np.zeros(2 * cap))     # doubled: slot i and i+cap

    def push(self, t: float, o: float, h: float, l: float, c: float, v: float) -> None:
        i, cap = self.head, self.cap
        self.t[i] = self.t[i + cap] = t
        self.o[i] = self.o[i + cap] = o
        self.h[i] = self.h[i + cap] = h
        self.l[i] = self.l[i + cap] = l
        self.c[i] = self.c[i + cap] = c
        self.v[i] = self.v[i + cap] = v
        self.head = (i + 1) % cap
        if self.count < cap:
            self.count += 1

    def view(self, field: np.ndarray, n: int) -> np.ndarray:
        """Last n values of `field` (ring.c, ring.h, ...), oldest first. Zero-copy."""
        if n > self.count:
            n = self.count
        end = self.head + self.cap
        return field[end - n:end]

class Ema:
    __slots__ = ("a", "v", "ready")

    def __init__(self, period: int):
        self.a, self.v, self.ready = 2.0 / (period + 1.0), 0.0, False

    def push(self, x: float) -> float:
        """Advance on a CLOSED bar."""
        if self.ready:
            self.v += self.a * (x - self.v)
        else:
            self.v, self.ready = x, True
        return self.v

    def peek(self, x: float) -> float:
        """Value if the forming bar closed at x. No state change."""
        return self.v + self.a * (x - self.v) if self.ready else x

@dataclass(frozen=True, slots=True)
class StrikeTargets:
    """Immutable snapshot published by the 10 s budget worker."""
    version: int
    built_at: float
    tokens: tuple[str, ...]      # the 3 budget strikes
    symbols: tuple[str, ...]
    premiums: tuple[float, ...]

class Latest(Generic[T]):
    __slots__ = ("_v",)

    def __init__(self, initial: T):
        self._v = initial

    def set(self, v: T) -> None:      # writer: ONE store of a fully built object
        self._v = v

    def get(self) -> T:               # reader: ONE load; keep the local reference
        return self._v

_log_repo = get_logger("instrument_repository")

try:                                     # live DataFetcher path used AEGIS.config.settings.DATA_DIR; the common.market copy used get_data_dir()
    from AEGIS.config.settings import DATA_DIR as _REPO_DATA_DIR
except ImportError:
    _REPO_DATA_DIR = get_data_dir()

DB_PATH = os.path.join(_REPO_DATA_DIR, "instruments.db")

SCHEMA_VERSION = 1

class InstrumentRepository:
    """Manages SQLite storage, staging tables, atomic swaps, and queries for universal instruments."""
    
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(InstrumentRepository, cls).__new__(cls)
            cls._instance._init_db()
        return cls._instance

    @contextlib.contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(DB_PATH, timeout=60.0)
        conn.row_factory = sqlite3.Row
        # Enable WAL mode and foreign keys
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        with self._get_connection() as conn:
            # 1. Schema Info Table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_info (
                    version INTEGER PRIMARY KEY,
                    updated_at TEXT
                );
            """)
            
            # 2. Instruments Table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS instruments (
                    instrument_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT UNIQUE,
                    name TEXT,
                    exch_seg TEXT,
                    expiry TEXT,
                    strike REAL,
                    lotsize INTEGER,
                    instrumenttype TEXT,
                    tick_size REAL,
                    description TEXT,
                    metadata_json TEXT,
                    last_updated TEXT
                );
            """)
            
            # 3. Broker Mappings Table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS broker_mappings (
                    mapping_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instrument_id INTEGER REFERENCES instruments(instrument_id) ON DELETE CASCADE,
                    broker_name TEXT,
                    broker_token TEXT,
                    broker_symbol TEXT,
                    last_updated TEXT,
                    UNIQUE(broker_name, broker_token)
                );
            """)
            
            # 4. Sync Metadata Table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_metadata (
                    sync_id TEXT PRIMARY KEY,
                    broker_name TEXT,
                    master_version TEXT,
                    source_hash TEXT,
                    sync_timestamp TEXT,
                    records_added INTEGER,
                    records_updated INTEGER
                );
            """)
            
            # Create indexes
            conn.execute("CREATE INDEX IF NOT EXISTS idx_inst_lookup ON instruments (name, exch_seg, instrumenttype);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mapping_lookup ON broker_mappings (broker_name, broker_token);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mapping_instrument ON broker_mappings (instrument_id);")

            # Verify/Set Schema Version
            cursor = conn.cursor()
            cursor.execute("SELECT version FROM schema_info ORDER BY version DESC LIMIT 1;")
            row = cursor.fetchone()
            if not row:
                conn.execute("INSERT INTO schema_info (version, updated_at) VALUES (?, ?);", 
                             (SCHEMA_VERSION, datetime.utcnow().isoformat()))
            elif row['version'] < SCHEMA_VERSION:
                _log_repo.info(f"Upgrading database schema from version {row['version']} to {SCHEMA_VERSION}")
                # Future migration logic can go here. For now, reset tables on version mismatch.
                self._recreate_tables(conn)

    def _recreate_tables(self, conn: sqlite3.Connection) -> None:
        conn.execute("DROP TABLE IF EXISTS broker_mappings;")
        conn.execute("DROP TABLE IF EXISTS instruments;")
        conn.execute("DROP TABLE IF EXISTS sync_metadata;")
        conn.execute("DROP TABLE IF EXISTS schema_info;")
        self._init_db()

    def get_instrument_by_symbol(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Retrieve instrument info by universal symbol."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM instruments WHERE symbol = ?;", (symbol.upper(),))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_instrument_by_token(self, broker_name: str, token: str) -> Optional[Dict[str, Any]]:
        """Find instrument by broker-specific token."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT i.*, m.broker_token, m.broker_symbol
                FROM instruments i
                JOIN broker_mappings m ON i.instrument_id = m.instrument_id
                WHERE m.broker_name = ? AND m.broker_token = ?;
            """, (broker_name.upper(), str(token)))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_token_by_symbol(self, broker_name: str, symbol: str) -> Optional[str]:
        """Find broker token by universal symbol."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT m.broker_token
                FROM broker_mappings m
                JOIN instruments i ON i.instrument_id = m.instrument_id
                WHERE m.broker_name = ? AND i.symbol = ?;
            """, (broker_name.upper(), symbol.upper()))
            row = cursor.fetchone()
            return row['broker_token'] if row else None



    def get_futures_token(self, broker_name: str, name: str, exch_seg: str) -> Optional[Tuple[str, str]]:
        """Query nearest future contract details."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT m.broker_token, i.symbol, i.expiry
                FROM instruments i
                JOIN broker_mappings m ON i.instrument_id = m.instrument_id
                WHERE m.broker_name = ? AND i.name = ? AND i.exch_seg = ? AND i.instrumenttype = 'FUTIDX'
            """, (broker_name.upper(), name.upper(), exch_seg.upper()))
            
            rows = cursor.fetchall()
            if not rows:
                return None
                
            future_contracts = []
            for r in rows:
                try:
                    # Try both Angel (28JUL2026) and Shoonya (28-JUL-2026) expiry formats
                    try:
                        expiry_dt = datetime.strptime(r['expiry'], '%d-%b-%Y')
                    except ValueError:
                        expiry_dt = datetime.strptime(r['expiry'], '%d%b%Y')
                    future_contracts.append((expiry_dt, r['broker_token'], r['symbol']))
                except ValueError:
                    pass
            
            if not future_contracts:
                return None
                
            now = datetime.now()
            valid_futures = [f for f in future_contracts if f[0] >= now]
            if not valid_futures:
                return None
                
            # Return nearest expiry
            nearest = min(valid_futures, key=lambda x: x[0])
            return nearest[1], nearest[2]  # (token, symbol)

    def get_weekly_option_tokens(self, broker_name: str, name: str, exch_seg: str) -> List[Dict[str, Any]]:
        """Query nearest weekly option tokens list."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT m.broker_token as token, i.symbol, i.expiry, i.strike
                FROM instruments i
                JOIN broker_mappings m ON i.instrument_id = m.instrument_id
                WHERE m.broker_name = ? AND i.name = ? AND i.exch_seg = ? AND i.instrumenttype = 'OPTIDX'
            """, (broker_name.upper(), name.upper(), exch_seg.upper()))
            
            rows = cursor.fetchall()
            if not rows:
                return []
                
            option_contracts = []
            for r in rows:
                try:
                    try:
                        expiry_dt = datetime.strptime(r['expiry'], '%d-%b-%Y')
                    except ValueError:
                        expiry_dt = datetime.strptime(r['expiry'], '%d%b%Y')
                    option_contracts.append((expiry_dt, dict(r)))
                except ValueError:
                    pass
            
            now = datetime.now()
            valid_options = [o for o in option_contracts if o[0].date() >= now.date()]
            if not valid_options:
                return []
                
            # Filter for nearest expiry date
            nearest_expiry = min(valid_options, key=lambda x: x[0])[0]
            weekly_options = [o[1] for o in valid_options if o[0] == nearest_expiry]
            return weekly_options

    def get_sync_hash(self, broker_name: str) -> Optional[str]:
        """Fetch the source hash of the last successful sync run."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT source_hash FROM sync_metadata WHERE broker_name = ? ORDER BY sync_timestamp DESC LIMIT 1;", 
                           (broker_name.upper(),))
            row = cursor.fetchone()
            return row['source_hash'] if row else None

    def get_all_symbols(self) -> List[Dict[str, Any]]:
        """Fetch all instruments sorted by symbol."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM instruments ORDER BY symbol ASC;")
            return [dict(r) for r in cursor.fetchall()]

    def get_count(self) -> int:
        """Fetch total count of instruments in the registry."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) as cnt FROM instruments;")
            row = cursor.fetchone()
            return row['cnt'] if row else 0

    def stage_and_swap_instruments(self, broker_name: str, instruments_data: List[Dict[str, Any]], 
                                   mappings_data: List[Dict[str, Any]], sync_record: Dict[str, Any]) -> None:
        """Performs atomic swap by importing all records into staging tables and swapping them within a transaction."""
        
        _log_repo.info(f"Staging sync data for broker {broker_name} ({len(instruments_data)} records)...")
        
        with self._get_connection() as conn:
            # 1. Create Staging Tables
            conn.execute("DROP TABLE IF EXISTS temp_instruments;")
            conn.execute("DROP TABLE IF EXISTS temp_broker_mappings;")
            
            conn.execute("""
                CREATE TEMP TABLE temp_instruments (
                    symbol TEXT UNIQUE,
                    name TEXT,
                    exch_seg TEXT,
                    expiry TEXT,
                    strike REAL,
                    lotsize INTEGER,
                    instrumenttype TEXT,
                    tick_size REAL,
                    description TEXT,
                    metadata_json TEXT,
                    last_updated TEXT
                );
            """)
            
            conn.execute("""
                CREATE TEMP TABLE temp_broker_mappings (
                    symbol TEXT,
                    broker_name TEXT,
                    broker_token TEXT,
                    broker_symbol TEXT,
                    last_updated TEXT,
                    UNIQUE(broker_name, broker_token)
                );
            """)
            
            # 2. Insert into staging
            conn.executemany("""
                INSERT OR IGNORE INTO temp_instruments 
                (symbol, name, exch_seg, expiry, strike, lotsize, instrumenttype, tick_size, description, metadata_json, last_updated)
                VALUES (:symbol, :name, :exch_seg, :expiry, :strike, :lotsize, :instrumenttype, :tick_size, :description, :metadata_json, :last_updated);
            """, instruments_data)
            
            conn.executemany("""
                INSERT OR IGNORE INTO temp_broker_mappings
                (symbol, broker_name, broker_token, broker_symbol, last_updated)
                VALUES (:symbol, :broker_name, :broker_token, :broker_symbol, :last_updated);
            """, mappings_data)
            
            # 3. Transaction for Atomic Swap
            try:
                # Delete existing mappings and instruments belonging to this broker to support incremental rebuild/replacement
                # Since multiple brokers map to the same instruments, we delete instruments that won't have any mappings left
                conn.execute("""
                    DELETE FROM broker_mappings 
                    WHERE broker_name = ?;
                """, (broker_name.upper(),))
                
                conn.execute("""
                    DELETE FROM instruments
                    WHERE instrument_id NOT IN (SELECT DISTINCT instrument_id FROM broker_mappings);
                """)
                
                # Insert new instruments from temp
                conn.execute("""
                    INSERT OR IGNORE INTO instruments 
                    (symbol, name, exch_seg, expiry, strike, lotsize, instrumenttype, tick_size, description, metadata_json, last_updated)
                    SELECT symbol, name, exch_seg, expiry, strike, lotsize, instrumenttype, tick_size, description, metadata_json, last_updated
                    FROM temp_instruments;
                """)
                
                # Insert new mappings resolving instrument_id from instruments
                conn.execute("""
                    INSERT OR IGNORE INTO broker_mappings (instrument_id, broker_name, broker_token, broker_symbol, last_updated)
                    SELECT i.instrument_id, t.broker_name, t.broker_token, t.broker_symbol, t.last_updated
                    FROM temp_broker_mappings t
                    JOIN instruments i ON t.symbol = i.symbol;
                """)
                
                # Record sync metadata
                conn.execute("""
                    INSERT OR REPLACE INTO sync_metadata (sync_id, broker_name, master_version, source_hash, sync_timestamp, records_added, records_updated)
                    VALUES (:sync_id, :broker_name, :master_version, :source_hash, :sync_timestamp, :records_added, :records_updated);
                """, sync_record)
                
                _log_repo.info(f"Atomic swap complete. Registry holds {self.get_count()} total instruments.")
            except Exception as e:
                _log_repo.error(f"Failed to perform atomic swap transaction: {e}")
                raise e
            finally:
                try:
                    conn.execute("DROP TABLE IF EXISTS temp_instruments;")
                    conn.execute("DROP TABLE IF EXISTS temp_broker_mappings;")
                except sqlite3.OperationalError:
                    pass

try:                      # fail-isolated: the registry helpers need it; OptionQuality / archiver / shadow observer must not
    from common.market.instrument import UniversalInstrument
except ImportError:
    UniversalInstrument = None

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

_log_l2 = get_logger("aegis_l2_archiver")

DATA = get_data_dir()

RAW_DIR = os.path.join(DATA, "l2_raw")          # Partitioned raw options data

LOGS = os.path.join(DATA, "logs")                # Auction / IV snapshot logs

os.makedirs(RAW_DIR, exist_ok=True)

os.makedirs(LOGS, exist_ok=True)

RETENTION_DAYS = 30

FLUSH_INTERVAL_SEC = 900  # 15 minutes = 1 partition file

AUCTION_LOG_INTERVAL = 2.0

ARCHIVE_QUEUE_MAXSIZE = 10000

# Underlying cash-index tokens (Nifty 26000, Sensex 1, Nifty Midcap 99919000)
UNDERLYING_TOKENS = {"26000", "1", "99919000"}

# Futures classification is fully dynamic (2026-10-26): token metadata `is_futures`
# or "FUT" in the symbol name. The previously hardcoded token IDs belonged to
# expired contracts (NIFTY29SEP26F etc.) and were silently dead weight.

# ─── Schema Definition (Options-Enriched) ───
# Adds `oi` (open interest) and `kind` (0=index, 1=CE, 2=PE, 3=FUT) vs the equity archiver.
SCHEMA_DTYPES = {
    "ts": "int64",        # Unix ms
    "tok": "int64",       # Token (options tokens can exceed int32 range)
    "sym": "object",      # Option/Futures symbol e.g. NIFTY25AUG24500CE / NIFTY28AUGFUT
    "kind": "int8",       # 0=index, 1=CE, 2=PE, 3=FUT
    "strike": "float32",  # Strike price (0 for index)
    "lp": "float32",      # Last Price (premium for options)
    "vol": "float32",     # Day Volume
    "oi": "float32",       # Open Interest (options only)
    "bp1": "float32", "bq1": "int32",
    "bp2": "float32", "bq2": "int32",
    "bp3": "float32", "bq3": "int32",
    "bp4": "float32", "bq4": "int32",
    "bp5": "float32", "bq5": "int32",
    "sp1": "float32", "sq1": "int32",
    "sp2": "float32", "sq2": "int32",
    "sp3": "float32", "sq3": "int32",
    "sp4": "float32", "sq4": "int32",
    "sp5": "float32", "sq5": "int32",
}

class AegisL2Archiver:
    """Passive raw options L2 recorder.

    Consumes the raw `std_msg` dict dispatched by `ShoonyaAdapter.add_tick_listener`
    (NOT the ParsedTick path) so it is decoupled from the engine's zero-copy contract
    and cannot perturb the hot path. Token metadata (side/strike) is resolved from
    the `token_map` built by aegis_main at startup and refreshed on ATM shifts.
    """

    def __init__(self, token_to_symbol: dict, token_map: dict | None = None):
        # token_to_symbol: {token: symbol}  (e.g. {"26000": "NIFTY", "12345": "NIFTY25AUG24500CE"})
        # token_map:       {token: {'side':0/1, 'strike':float, 'expiry':...}}  (options only)
        self.tok_to_sym = {str(k): str(v) for k, v in token_to_symbol.items()}
        self.token_meta = {str(k): v for k, v in (token_map or {}).items()}

        # Target tokens = everything we know about (index + options).
        # The archiver does NOT hard-filter; it records any token present in the map,
        # so dynamic ATM±10 re-subscriptions are captured automatically once
        # `update_token_maps()` is called by aegis_main on ATM shifts.
        self.target_tokens = set(self.tok_to_sym.keys()) | UNDERLYING_TOKENS
        _log_l2.info(
            f"AegisL2Archiver: tracking {len(self.target_tokens)} tokens "
            f"({sum(1 for t in self.target_tokens if t in UNDERLYING_TOKENS)} index, "
            f"{len(self.target_tokens) - sum(1 for t in self.target_tokens if t in UNDERLYING_TOKENS)} options)"
        )

        self.buffer: list[dict] = []
        self.auction_buf: list[dict] = []
        self.last_flush = pytime.time()
        self.last_aflush = pytime.time()
        self.current_date = datetime.now().strftime("%Y-%m-%d")
        self.partition_seq = self._find_next_partition_seq()
        self._last_auction_ts = 0.0
        self._last_known_oi: dict[str, float] = {}
        self._prune_old_files()
        self._tick_queue = queue.Queue(maxsize=ARCHIVE_QUEUE_MAXSIZE)
        self._stop_worker = threading.Event()
        self._dropped_ticks = 0
        self._worker = threading.Thread(
            target=self._archive_worker,
            name="aegis-l2-archiver",
            daemon=True,
        )
        self._worker.start()

    def _find_next_partition_seq(self) -> int:
        """Finds max existing partition sequence for today to avoid overwriting files on restart."""
        try:
            target_dir = self._get_partition_dir(self.current_date)
            max_seq = -1
            if os.path.exists(target_dir):
                for f in os.listdir(target_dir):
                    if f.startswith(f"aegis_l2_raw_{self.current_date}_part_") and f.endswith(".parquet"):
                        try:
                            seq_part = f.replace(f"aegis_l2_raw_{self.current_date}_part_", "").replace(".parquet", "")
                            max_seq = max(max_seq, int(seq_part))
                        except Exception:
                            pass
            return max_seq + 1
        except Exception:
            return 0

    def update_token_maps(self, token_to_symbol: dict, token_map: dict | None = None):
        """Hot-refresh token metadata on ATM shift / expiry rollover.

        Called by aegis_main when the dynamic subscription set changes.
        Safe to call mid-session; does not flush or block.
        """
        self.tok_to_sym = {str(k): str(v) for k, v in token_to_symbol.items()}
        if token_map:
            self.token_meta = {str(k): v for k, v in token_map.items()}
        self.target_tokens = set(self.tok_to_sym.keys()) | UNDERLYING_TOKENS
        _log_l2.info(
            f"AegisL2Archiver: token maps refreshed — now tracking {len(self.target_tokens)} tokens"
        )

    def _get_index_subfolder(self, date_str: str | None = None) -> str:
        d_str = date_str or self.current_date
        try:
            dt = datetime.strptime(d_str, "%Y-%m-%d")
            # Wed (2), Thu (3) -> Sensex; Mon (0), Tue (1), Fri (4) -> Nifty
            return "Sensex" if dt.weekday() in (2, 3) else "Nifty"
        except Exception:
            return "Nifty"

    def _get_partition_dir(self, date_str: str | None = None) -> str:
        sub = self._get_index_subfolder(date_str)
        target_dir = os.path.join(RAW_DIR, sub)
        os.makedirs(target_dir, exist_ok=True)
        return target_dir

    def _prune_old_files(self):
        try:
            cut = datetime.now() - timedelta(days=RETENTION_DAYS)
            dirs_to_check = [RAW_DIR, os.path.join(RAW_DIR, "Nifty"), os.path.join(RAW_DIR, "Sensex")]
            for dpath in dirs_to_check:
                if not os.path.exists(dpath):
                    continue
                for f in os.listdir(dpath):
                    if f.startswith("aegis_l2_raw_") and f.endswith(".parquet"):
                        # aegis_l2_raw_YYYY-MM-DD_part_NNN.parquet
                        parts = f.replace("aegis_l2_raw_", "").replace(".parquet", "").split("_part_")
                        if len(parts) == 2:
                            d = datetime.strptime(parts[0], "%Y-%m-%d")
                            if d < cut:
                                os.remove(os.path.join(dpath, f))
        except Exception as e:
            _log_l2.debug(f"Prune failed: {e}")

    def _get_partition_path(self) -> str:
        target_dir = self._get_partition_dir(self.current_date)
        return os.path.join(
            target_dir, f"aegis_l2_raw_{self.current_date}_part_{self.partition_seq:03d}.parquet"
        )

    def _flush_buffer(self, force=False, tick_ts_sec=None):
        nows = pytime.time()
        if not force and (nows - self.last_flush < FLUSH_INTERVAL_SEC) and len(self.buffer) < 50000:
            return

        if not self.buffer:
            self.last_flush = nows
            return

        try:
            df = pd.DataFrame(self.buffer)
            # FIX 2026-09-02: dedup on (ts, tok) keep-last — WS reconnects push
            # duplicate ticks (today 12% dups); store only the latest row per key.
            df = df.drop_duplicates(subset=["ts", "tok"], keep="last").reset_index(drop=True)
            # Enforce Schema / Types
            for col, dtype in SCHEMA_DTYPES.items():
                if col in df.columns:
                    df[col] = df[col].astype(dtype)

            path = self._get_partition_path()
            df.to_parquet(path, index=False, compression="snappy")
            size_mb = os.path.getsize(path) / 1e6
            _log_l2.info(
                f"AegisL2Archiver: Flushed Partition {self.partition_seq} | "
                f"{len(df)} rows | {size_mb:.1f} MB"
            )

            self.buffer = []
            self.partition_seq += 1
            self.last_flush = nows

            # Date rollover check - USE TICK TIMESTAMP not datetime.now() to avoid race condition
            if tick_ts_sec is not None:
                new_date = datetime.fromtimestamp(tick_ts_sec).strftime("%Y-%m-%d")
            else:
                new_date = datetime.now().strftime("%Y-%m-%d")
            if new_date != self.current_date:
                self.current_date = new_date
                self.partition_seq = 0
                self._prune_old_files()

        except Exception as e:
            _log_l2.error(f"AegisL2Archiver partition flush failed: {e}")

    def _flush_auction(self, now: datetime):
        if not self.auction_buf:
            self.last_aflush = pytime.time()
            return
        try:
            kind = "preopen" if now.hour < 12 else "closeauction"
            path = os.path.join(LOGS, f"aegis_{kind}_{now.strftime('%Y-%m-%d')}.jsonl")
            with open(path, "a") as f:
                for r in self.auction_buf:
                    f.write(json.dumps(r) + "\n")
            self.auction_buf = []
            self.last_aflush = pytime.time()
        except Exception as exc:
            _log_l2.error(f"AegisL2Archiver auction flush failed: {exc}")

    def _archive_worker(self):
        """Drain in-memory tick records and perform all persistence off the WS thread."""
        while not self._stop_worker.is_set() or not self._tick_queue.empty():
            try:
                row, auction_row, ts_sec = self._tick_queue.get(timeout=0.1)
            except queue.Empty:
                if pytime.time() - self.last_flush >= FLUSH_INTERVAL_SEC:
                    self._flush_buffer(force=True)
                if pytime.time() - self.last_aflush >= 30.0:
                    self._flush_auction(datetime.now())
                continue

            try:
                self.buffer.append(row)
                if auction_row is not None:
                    self.auction_buf.append(auction_row)
                if ts_sec - self.last_aflush >= 30.0:
                    self._flush_auction(datetime.fromtimestamp(ts_sec))
                self._flush_buffer(tick_ts_sec=ts_sec)
            except Exception as exc:
                _log_l2.error(f"AegisL2Archiver worker error: {exc}")
            finally:
                self._tick_queue.task_done()

        self._flush_buffer(force=True)
        self._flush_auction(datetime.now())

    # ═══════════════════════════════════════════════════════════
    # HOT PATH — RAW TICK INGESTION (ZERO MATH)
    # ═══════════════════════════════════════════════════════════
    def on_tick(self, msg: dict):
        """Raw dict listener — matches ShoonyaAdapter std_msg shape."""
        if self._stop_worker.is_set():
            return
        try:
            tok = str(msg.get("token") or msg.get("tk") or "")
            if not tok or tok not in self.target_tokens:
                return

            sym = self.tok_to_sym.get(tok) or ("NIFTY" if tok == "26000" else "SENSEX" if tok == "1" else "NIFTY29SEP26F" if tok == "68407" else "SENSEX26SEPFUT" if tok == "844615" else "")
            if not sym:
                return

            lp = float(msg.get("last_traded_price") or msg.get("lp") or 0)
            if lp <= 0:
                return

            now = datetime.now()
            ts_ms = int(now.timestamp() * 1000)
            ts_sec = now.timestamp()
            ts_time = now.time()

            # Resolve kind + strike from token metadata
            meta = self.token_meta.get(tok, {})
            is_underlying = tok in UNDERLYING_TOKENS
            is_futures = (isinstance(meta, dict) and meta.get("is_futures", False)) or (sym and ("FUT" in sym.upper()))

            if is_underlying:
                kind = 0
                strike = np.float32(0.0)
            elif is_futures:
                kind = 3  # 3=FUT (Index Futures)
                strike = np.float32(0.0)
            else:
                side = int(meta.get("side", -1)) if isinstance(meta, dict) else -1
                # side: 0=CE, 1=PE  →  kind: 1=CE, 2=PE
                kind = 1 if side == 0 else 2 if side == 1 else 0
                strike = np.float32(meta.get("strike", 0.0)) if isinstance(meta, dict) else np.float32(0.0)

            # Auction Logging (Pre-open / Close) — index only
            auction_row = None
            if is_underlying and (
                dtime(9, 0) <= ts_time < dtime(9, 15)
                or dtime(15, 30) <= ts_time < dtime(15, 40)
            ):
                if ts_sec - self._last_auction_ts >= AUCTION_LOG_INTERVAL:
                    self._last_auction_ts = ts_sec
                    sb = sum(float(msg.get(f"bq{i}") or 0) for i in range(1, 6))
                    sa = sum(float(msg.get(f"sq{i}") or 0) for i in range(1, 6))
                    auction_row = {"ts": ts_sec, "sym": sym, "lp": lp, "imb": (sb - sa) / max(sb + sa, 1.0)}

            # Open interest (options only; forward-fill last known non-zero OI)
            raw_oi = msg.get("oi") or msg.get("poi") or msg.get("oiv")
            if raw_oi is not None:
                try:
                    oi_f = float(raw_oi)
                    if oi_f > 0:
                        self._last_known_oi[tok] = oi_f
                except (ValueError, TypeError):
                    pass
            oi = np.float32(self._last_known_oi.get(tok, 0.0))

            # Raw Row Construction (Unrolled for Speed)
            row = {
                "ts": ts_ms,
                "tok": int(tok),
                "sym": sym,
                "kind": np.int8(kind),
                "strike": strike,
                "lp": np.float32(lp),
                "vol": np.float32(msg.get("volume_trade_for_the_day") or msg.get("v") or 0),
                "oi": oi,
            }

            for i in range(1, 6):
                row[f"bp{i}"] = np.float32(msg.get(f"bp{i}") or 0)
                row[f"bq{i}"] = np.int32(msg.get(f"bq{i}") or 0)
                row[f"sp{i}"] = np.float32(msg.get(f"sp{i}") or 0)
                row[f"sq{i}"] = np.int32(msg.get(f"sq{i}") or 0)

            try:
                self._tick_queue.put_nowait((row, auction_row, ts_sec))
            except queue.Full:
                self._dropped_ticks += 1
                if self._dropped_ticks == 1 or self._dropped_ticks % 1000 == 0:
                    _log_l2.warning(
                        f"AegisL2Archiver queue full; dropped {self._dropped_ticks} tick records"
                    )

        except Exception as e:
            _log_l2.debug(f"AegisL2Archiver tick error: {e}")

    def shutdown(self):
        """Call at EOD to flush final buffer."""
        _log_l2.info("AegisL2Archiver: Shutdown signal received; draining archive queue...")
        self._stop_worker.set()
        self._worker.join(timeout=10.0)
        if self._worker.is_alive():
            _log_l2.error("AegisL2Archiver worker did not stop within 10 seconds")
            return
        _log_l2.info("AegisL2Archiver: Final flush complete.")

def attach_aegis_archiver(broker, token_to_symbol: dict, token_map: dict | None = None):
    """Attach the AEGIS options archiver as a tick listener on the broker.

    Mirrors stock_order_flow/l2_archiver.py:attach_archiver so aegis_main can
    wire it with the same one-liner pattern.
    """
    arch = AegisL2Archiver(token_to_symbol, token_map)
    candidates = [broker]
    for attr in ("market_data", "mdp", "market_data_provider", "_mdp", "_api", "api", "session_provider"):
        obj = getattr(broker, attr, None)
        if obj:
            candidates.append(obj)
    for c in candidates:
        if hasattr(c, "add_tick_listener"):
            c.add_tick_listener(arch.on_tick)
            _log_l2.info("AegisL2Archiver ATTACHED (Raw Options Partitioned Logger)")
            return arch
    _log_l2.warning("AegisL2Archiver: No add_tick_listener found.")
    return arch

_log_shadow = logging.getLogger("aegis.shadow_boundary")

@dataclass
class ShadowTrade:
    trade_id: str
    boundary: str            # 'PAIN_ZONE_BAND' or 'EMA_200_BAND'
    direction: str           # 'UP' (CALL) or 'DOWN' (PUT)
    entry_ts: float          # Epoch timestamp in seconds
    entry_time_str: str      # HH:MM:SS
    spot_entry: float
    spot_anchor_high: float
    spot_anchor_low: float
    spot_sl_price: float
    
    # OTM 5 Option Details
    opt_symbol: str
    opt_strike: float
    opt_side: str            # 'CE' or 'PE'
    opt_entry: float
    opt_tp_price: float      # Entry + 4.0%
    opt_sl_price: float      # Entry - 12.0%
    
    # Telemetry Tracking
    spot_mfe_30s: float = 0.0
    spot_mae_30s: float = 0.0
    spot_mfe_60s: float = 0.0
    spot_mae_60s: float = 0.0
    spot_peak_mfe: float = 0.0
    spot_peak_mae: float = 0.0
    
    opt_mfe_30s_pct: float = 0.0
    opt_mae_30s_pct: float = 0.0
    opt_mfe_60s_pct: float = 0.0
    opt_mae_60s_pct: float = 0.0
    opt_peak_mfe_pct: float = 0.0
    opt_peak_mae_pct: float = 0.0
    
    # N-1 Testing Candle Wyckoff & Order Flow Footprint (Empirical Shadow Tracking)
    n1_vol: float = 0.0
    n1_baseline_vol: float = 0.0
    n1_vol_dryup: bool = False
    n1_net_delta: float = 0.0
    n1_stopping_vol: bool = False

    # Pain Zone 4-Line Snapshot & Band Spread (Empirical Tracking)
    pz_top: float = 0.0
    pz_bottom: float = 0.0
    pz_mid: float = 0.0
    pz_spread_pts: float = 0.0
    pz_call_buy: float = 0.0
    pz_call_sell: float = 0.0
    pz_put_buy: float = 0.0
    pz_put_sell: float = 0.0
    
    # State & Outcome
    captured_30s: bool = False
    captured_60s: bool = False
    is_active: bool = True
    exit_ts: float = 0.0
    exit_reason: str = "RUNNING"
    outcome: str = "PENDING"  # 'WINNER' (+4% TP), 'LOSER' (SL), 'EXPIRED'
    duration_sec: float = 0.0

class ShadowBoundaryObserver:
    """
    Evaluates real-time First-Touch Boundary Rejections on Pain Zone and 200 EMA,
    paper trades OTM 5 options, and tracks sub-minute (30s/60s/300s) forward telemetry.
    """

    def __init__(
        self,
        price_action_engine: Any = None,
        wyckoff_engine: Any = None,
        strike_step: float = 50.0,
        log_dir: Optional[str] = None,
    ):
        self.pa_engine = price_action_engine
        self.wyckoff_engine = wyckoff_engine
        self.strike_step = float(strike_step)
        
        # Paths
        default_dir = os.path.join(os.path.dirname(__file__), "..", "data", "logs", "telemetry")
        self.log_dir = log_dir or os.getenv("AEGIS_TELEMETRY_DIR", default_dir)
        try:
            os.makedirs(self.log_dir, exist_ok=True)
        except Exception:
            pass
            
        self.active_trades: List[ShadowTrade] = []
        
        # Armed Boundary Setup State
        # boundary_name -> {'direction': 'UP'/'DOWN', 'anchor_high': float, 'anchor_low': float, 'armed_bar_idx': int, 'ts': float}
        self.armed_setups: Dict[str, Dict[str, Any]] = {}
        self.last_trigger_bar_idx: Dict[str, int] = {}
        self._mode_b_up_ticks: int = 0
        self._mode_b_down_ticks: int = 0
        
        # References populated by AegisPipelineOrchestrator
        self.option_cache: Dict[str, float] = {}
        self.strike_to_symbol: Dict[Tuple[int, float], str] = {}
        self.current_atm_strike: float = 0.0
        
        _log_shadow.info(f"[ShadowBoundaryObserver] Initialized (StrikeStep={self.strike_step}, LogDir={self.log_dir})")

    def update_atm(self, atm: float) -> None:
        self.current_atm_strike = float(atm)

    def select_otm5_symbol(self, direction: str, spot: float) -> Tuple[str, float, str]:
        """
        Selects OTM 5 contract based on current ATM and direction.
        Returns (symbol, strike, side_str).
        """
        atm = self.current_atm_strike or (round(spot / self.strike_step) * self.strike_step)
        if direction.upper() in ("UP", "CALL", "CE"):
            side_int = 0
            side_str = "CE"
            target_strike = atm + 5.0 * self.strike_step
        else:
            side_int = 1
            side_str = "PE"
            target_strike = atm - 5.0 * self.strike_step
            
        sym = self.strike_to_symbol.get((side_int, target_strike), "")
        return sym, target_strike, side_str

    def on_option_tick(self, sym: str, lp: float, now_sec: float) -> None:
        """Updates active shadow trades with option tick prices."""
        if lp <= 0 or not self.active_trades:
            return
            
        for trade in self.active_trades:
            if not trade.is_active or trade.opt_symbol != sym:
                continue
                
            entry_opt = trade.opt_entry
            if entry_opt <= 0:
                continue
                
            pnl_pct = ((lp - entry_opt) / entry_opt) * 100.0
            
            # Update peak MFE / MAE
            if pnl_pct > trade.opt_peak_mfe_pct:
                trade.opt_peak_mfe_pct = pnl_pct
            if pnl_pct < trade.opt_peak_mae_pct:
                trade.opt_peak_mae_pct = pnl_pct
                
            elapsed = now_sec - trade.entry_ts
            
            # 30s Snapshot
            if elapsed >= 30.0 and not trade.captured_30s:
                trade.opt_mfe_30s_pct = trade.opt_peak_mfe_pct
                trade.opt_mae_30s_pct = trade.opt_peak_mae_pct
                trade.captured_30s = True
                
            # 60s Snapshot
            if elapsed >= 60.0 and not trade.captured_60s:
                trade.opt_mfe_60s_pct = trade.opt_peak_mfe_pct
                trade.opt_mae_60s_pct = trade.opt_peak_mae_pct
                trade.captured_60s = True
                
            # Exit Conditions
            # 1. Target Hit (+4.0% Maker TP)
            if lp >= trade.opt_tp_price:
                self._close_trade(trade, now_sec, "TAKE_PROFIT_HIT", "WINNER")
            # 2. Disaster SL Hit (-12.0%)
            elif lp <= trade.opt_sl_price:
                self._close_trade(trade, now_sec, "DISASTER_SL_HIT", "LOSER")
            # 3. Max hold time expired (300s / 5 mins)
            elif elapsed >= 300.0:
                outcome = "WINNER" if pnl_pct > 0 else "LOSER"
                self._close_trade(trade, now_sec, "TIME_EXPIRED_300S", outcome)

    def on_underlying_tick(self, spot: float, now_sec: float) -> None:
        """
        Processes underlying spot index tick:
          1. Updates active shadow trades with spot MFE/MAE.
          2. Checks In-Flight Intrabar piercing of armed boundary setups.
          3. Evaluates boundary touches at bar close.
        """
        if spot <= 0:
            return
            
        # 1. Update Active Trades Spot Excursion
        for trade in list(self.active_trades):
            if not trade.is_active:
                continue
                
            elapsed = now_sec - trade.entry_ts
            if trade.direction == "UP":
                mfe_pts = spot - trade.spot_entry
                mae_pts = trade.spot_entry - spot
            else:
                mfe_pts = trade.spot_entry - spot
                mae_pts = spot - trade.spot_entry
                
            if mfe_pts > trade.spot_peak_mfe:
                trade.spot_peak_mfe = mfe_pts
            if mae_pts > trade.spot_peak_mae:
                trade.spot_peak_mae = mae_pts
                
            if elapsed >= 30.0 and not trade.captured_30s:
                trade.spot_mfe_30s = trade.spot_peak_mfe
                trade.spot_mae_30s = trade.spot_peak_mae
                
            if elapsed >= 60.0 and not trade.captured_60s:
                trade.spot_mfe_60s = trade.spot_peak_mfe
                trade.spot_mae_60s = trade.spot_peak_mae
                
            # Spot Invalidation Check
            if trade.direction == "UP" and spot <= trade.spot_sl_price:
                self._close_trade(trade, now_sec, "SPOT_INVALIDATION_SL", "LOSER")
            elif trade.direction == "DOWN" and spot >= trade.spot_sl_price:
                self._close_trade(trade, now_sec, "SPOT_INVALIDATION_SL", "LOSER")
            elif elapsed >= 300.0:
                self._close_trade(trade, now_sec, "TIME_EXPIRED_300S", "EXPIRED")

        # 2. Check In-Flight Intrabar Trigger on Armed Boundaries (Mode A: Rejection Bounce & Pierce)
        self._check_inflight_piercing(spot, now_sec)

        # 3. Check In-Flight Breakthrough Surge on Active Forming Candle (Mode B: Breakout through Pain Zone / 200 EMA)
        self._check_inflight_breakthrough(spot, now_sec)

        # 4. Check for New Armed Setups at Bar Transitions
        self._evaluate_boundary_arming(spot, now_sec)

    def _check_inflight_piercing(self, spot: float, now_sec: float) -> None:
        """
        Fires Mode A In-Flight Entry when live tick pierces anchor price.
        Active during T:00s - T:20s of Bar N+1.
        """
        if not self.armed_setups:
            return

        sec_in_min = int(now_sec % 60)
        atr = getattr(self.pa_engine, "current_atr_1m", 10.0) or 10.0
        pz_top, pz_bot = self._get_pain_zone_levels(atr)

        for b_name in list(self.armed_setups.keys()):
            armed = self.armed_setups[b_name]
            direction = armed["direction"]
            anchor_h = armed["anchor_high"]
            anchor_l = armed["anchor_low"]

            # 1. Priority Window: Disarm after 20s if not pierced
            if sec_in_min > 20:
                del self.armed_setups[b_name]
                continue

            # 2. Auto-Flip Disarm Rule: If new candle reverses across opposite boundary by 0.15*ATR
            if direction == "UP" and pz_bot > 0 and spot <= (pz_bot - 0.15 * atr):
                del self.armed_setups[b_name]
                continue
            elif direction == "DOWN" and pz_top > 0 and spot >= (pz_top + 0.15 * atr):
                del self.armed_setups[b_name]
                continue

            triggered = False
            if direction == "UP" and spot >= anchor_h:
                triggered = True
            elif direction == "DOWN" and spot <= anchor_l:
                triggered = True

            if triggered:
                # Fire Shadow Trade!
                del self.armed_setups[b_name]
                self._open_shadow_trade(b_name, direction, spot, anchor_h, anchor_l, now_sec)

    def _check_inflight_breakthrough(self, spot: float, now_sec: float) -> None:
        """
        Mode B: Live In-Flight Intra-Bar Breakthrough Surge with 4-Point Trap Filter.
        Triggers when an active forming 1-minute candle breaks through the Pain Zone, 200 EMA,
        or 20 EMA with strong directional expansion from T:08s onwards (upper cap removed per user mandate).
        """
        if self.pa_engine is None:
            return
        curr_bar = getattr(self.pa_engine, "_curr_1m_bar", None)
        if curr_bar is None or curr_bar.tick_count < 2:
            return

        sec_in_min = int(now_sec % 60)
        # Active intra-bar expansion window from T:02s onwards (rollover guard T:00s-T:01s)
        if sec_in_min < 2:
            return

        atr = getattr(self.pa_engine, "current_atr_1m", 10.0) or 10.0
        if atr <= 0:
            atr = 10.0

        curr_rng = curr_bar.high - curr_bar.low
        # Zero-latency: activity floor and min_clearance removed per Mode B parity

        # 2. In-Flight Option Flow & Spot Tick Arrival Rate
        tick_rate = curr_bar.tick_count / max(float(sec_in_min), 1.0)
        opt_flow = getattr(self.pa_engine, "_opt_flow_1m", None)
        if not isinstance(opt_flow, dict):
            opt_flow = {}
        cb = float(opt_flow.get("call_buy_vol", 0.0) or getattr(curr_bar, "call_buy_vol", 0.0) or 0.0)
        cs = float(opt_flow.get("call_sell_vol", 0.0) or getattr(curr_bar, "call_sell_vol", 0.0) or 0.0)
        pb = float(opt_flow.get("put_buy_vol", 0.0) or getattr(curr_bar, "put_buy_vol", 0.0) or 0.0)
        ps = float(opt_flow.get("put_sell_vol", 0.0) or getattr(curr_bar, "put_sell_vol", 0.0) or 0.0)
        inflight_opt_vol = cb + cs + pb + ps

        # Cache 60m median volume at 1m bar transitions to eliminate per-tick list allocation & GC pressure
        total_bars = getattr(self.pa_engine, "_total_bars_seen", None)
        bar_count = int(total_bars) if isinstance(total_bars, (int, float)) else len(getattr(self.pa_engine, "bars_1m", []))
        if bar_count != getattr(self, "_cached_med_vol_bar_count", -1):
            bars = self.pa_engine.bars_1m
            n_bars = len(bars)
            start_idx = max(0, n_bars - 60)
            vols = [float(getattr(bars[i], "volume", 0.0) or 0.0) for i in range(start_idx, n_bars)]
            if vols:
                vols.sort()
                self._cached_med_vol_60 = float(vols[len(vols) // 2])
            else:
                self._cached_med_vol_60 = 1.0
            self._cached_med_vol_bar_count = bar_count

        med_vol = getattr(self, "_cached_med_vol_60", 1.0)
        if med_vol > 0 and inflight_opt_vol > 0:
            expected_opt_vol = max(1.0, med_vol * (float(sec_in_min) / 60.0))
            opt_pace_ok = (inflight_opt_vol / expected_opt_vol) >= 1.15
        else:
            opt_pace_ok = True

        if not (opt_pace_ok or tick_rate >= 1.5):
            return

        bar_idx = bar_count
        bars = getattr(self.pa_engine, "bars_1m", [])
        if not bars:
            return
        prior_bar = bars[-1]
        n1_fp = self._compute_n1_footprint(prior_bar, atr)

        # 1. Pain Zone Rejection-Breakout (LRRB)
        # Prior bar tested Pain Zone level, live candle breaks prior bar extreme (Strict 1-Bar Expiry)
        pz_top, pz_bot = self._get_pain_zone_levels(atr)
        if pz_top > 0 and pz_bot > 0:
            last_trig = max(self.last_trigger_bar_idx.get("PAIN_ZONE_REJECTION_BREAKOUT", -100), self.last_trigger_bar_idx.get("PAIN_ZONE_BREAKOUT", -100))
            if (bar_idx - last_trig) >= 3:
                # Bullish Rejection Breakout: Prior (or current) bar tested Pain Zone Top, live spot breaks prior bar High
                pz_up_tested = (prior_bar.low <= pz_top + 0.25 * atr and prior_bar.high >= pz_top and prior_bar.close >= pz_bot - 0.25 * atr)
                pz_up_action = (prior_bar.close <= prior_bar.open) or ((prior_bar.close - prior_bar.low) >= 0.15 * max(prior_bar.high - prior_bar.low, 0.1))
                if pz_up_tested and pz_up_action and spot >= prior_bar.high:
                    if self._check_wyckoff_harmony("UP", curr_bar):
                        self.last_trigger_bar_idx["PAIN_ZONE_REJECTION_BREAKOUT"] = bar_idx
                        lowest_low = min(prior_bar.low, curr_bar.low)
                        _log_shadow.info(f"[SHADOW_INTRABAR_SURGE] PAIN_ZONE_REJECTION_BREAKOUT CALL | Spot: {spot:.2f} >= AnchorHigh: {prior_bar.high:.2f} at T+{sec_in_min}s")
                        self._open_shadow_trade("PAIN_ZONE_REJECTION_BREAKOUT", "UP", spot, prior_bar.high, lowest_low, now_sec, n1_fp)
                        return

                # Bearish Rejection Breakdown: Prior (or current) bar tested Pain Zone Bottom, live spot breaks prior bar Low
                pz_dn_tested = (prior_bar.high >= pz_bot - 0.25 * atr and prior_bar.low <= pz_bot and prior_bar.close <= pz_top + 0.25 * atr)
                pz_dn_action = (prior_bar.close >= prior_bar.open) or ((prior_bar.high - prior_bar.close) >= 0.15 * max(prior_bar.high - prior_bar.low, 0.1))
                if pz_dn_tested and pz_dn_action and spot <= prior_bar.low:
                    if self._check_wyckoff_harmony("DOWN", curr_bar):
                        self.last_trigger_bar_idx["PAIN_ZONE_REJECTION_BREAKOUT"] = bar_idx
                        highest_high = max(prior_bar.high, curr_bar.high)
                        _log_shadow.info(f"[SHADOW_INTRABAR_SURGE] PAIN_ZONE_REJECTION_BREAKOUT PUT | Spot: {spot:.2f} <= AnchorLow: {prior_bar.low:.2f} at T+{sec_in_min}s")
                        self._open_shadow_trade("PAIN_ZONE_REJECTION_BREAKOUT", "DOWN", spot, highest_high, prior_bar.low, now_sec, n1_fp)
                        return

        # 2. 200 EMA Rejection-Breakout (LRRB)
        # Prior bar tested 200 EMA level, live candle breaks prior bar extreme (Strict 1-Bar Expiry)
        ema200 = float(getattr(self.pa_engine, "ema_200_1m", 0.0) or 0.0)
        if ema200 > 0:
            last_trig_ema = max(self.last_trigger_bar_idx.get("EMA_200_REJECTION_BREAKOUT", -100), self.last_trigger_bar_idx.get("EMA_200_BREAKOUT", -100))
            if (bar_idx - last_trig_ema) >= 3:
                # Bullish Rejection Breakout: Prior (or current) bar tested 200 EMA, live spot breaks prior bar High
                ema_up_tested = (prior_bar.low <= ema200 + 0.25 * atr and prior_bar.high >= ema200)
                ema_up_action = (prior_bar.close <= prior_bar.open) or ((prior_bar.close - prior_bar.low) >= 0.15 * max(prior_bar.high - prior_bar.low, 0.1))
                if ema_up_tested and ema_up_action and spot >= prior_bar.high:
                    if self._check_wyckoff_harmony("UP", curr_bar):
                        self.last_trigger_bar_idx["EMA_200_REJECTION_BREAKOUT"] = bar_idx
                        lowest_low = min(prior_bar.low, curr_bar.low)
                        _log_shadow.info(f"[SHADOW_INTRABAR_SURGE] EMA_200_REJECTION_BREAKOUT CALL | Spot: {spot:.2f} >= AnchorHigh: {prior_bar.high:.2f} at T+{sec_in_min}s")
                        self._open_shadow_trade("EMA_200_REJECTION_BREAKOUT", "UP", spot, prior_bar.high, lowest_low, now_sec, n1_fp)
                        return

                # Bearish Rejection Breakdown: Prior (or current) bar tested 200 EMA from below, live spot breaks prior bar Low
                ema_dn_tested = (prior_bar.high >= ema200 - 0.25 * atr and prior_bar.low <= ema200)
                ema_dn_action = (prior_bar.close >= prior_bar.open) or ((prior_bar.high - prior_bar.close) >= 0.15 * max(prior_bar.high - prior_bar.low, 0.1))
                if ema_dn_tested and ema_dn_action and spot <= prior_bar.low:
                    if self._check_wyckoff_harmony("DOWN", curr_bar):
                        self.last_trigger_bar_idx["EMA_200_REJECTION_BREAKOUT"] = bar_idx
                        highest_high = max(prior_bar.high, curr_bar.high)
                        _log_shadow.info(f"[SHADOW_INTRABAR_SURGE] EMA_200_REJECTION_BREAKOUT PUT | Spot: {spot:.2f} <= AnchorLow: {prior_bar.low:.2f} at T+{sec_in_min}s")
                        self._open_shadow_trade("EMA_200_REJECTION_BREAKOUT", "DOWN", spot, highest_high, prior_bar.low, now_sec, n1_fp)
                        return

        # 3. 20 EMA Rejection-Breakout (LRRB Shadow Observer)
        # Prior bar tested 20 EMA level, live candle breaks prior bar extreme (Strict 1-Bar Expiry)
        ema20 = float(getattr(self.pa_engine, "ema_20_1m", 0.0) or 0.0)
        if ema20 > 0:
            last_trig_ema20 = self.last_trigger_bar_idx.get("EMA_20_REJECTION_BREAKOUT", -100)
            if (bar_idx - last_trig_ema20) >= 3:
                # Bullish Rejection Breakout: Prior (or current) bar tested 20 EMA from above, live spot breaks prior bar High
                ema20_up_tested = (prior_bar.low <= ema20 + 0.25 * atr and prior_bar.high >= ema20)
                ema20_up_action = (prior_bar.close <= prior_bar.open) or ((prior_bar.close - prior_bar.low) >= 0.15 * max(prior_bar.high - prior_bar.low, 0.1))
                if ema20_up_tested and ema20_up_action and spot >= prior_bar.high:
                    if self._check_wyckoff_harmony("UP", curr_bar):
                        self.last_trigger_bar_idx["EMA_20_REJECTION_BREAKOUT"] = bar_idx
                        lowest_low = min(prior_bar.low, curr_bar.low)
                        _log_shadow.info(f"[SHADOW_INTRABAR_SURGE] EMA_20_REJECTION_BREAKOUT CALL | Spot: {spot:.2f} >= AnchorHigh: {prior_bar.high:.2f} at T+{sec_in_min}s")
                        self._open_shadow_trade("EMA_20_REJECTION_BREAKOUT", "UP", spot, prior_bar.high, lowest_low, now_sec, n1_fp)
                        return

                # Bearish Rejection Breakdown: Prior (or current) bar tested 20 EMA from below, live spot breaks prior bar Low
                ema20_dn_tested = (prior_bar.high >= ema20 - 0.25 * atr and prior_bar.low <= ema20)
                ema20_dn_action = (prior_bar.close >= prior_bar.open) or ((prior_bar.high - prior_bar.close) >= 0.15 * max(prior_bar.high - prior_bar.low, 0.1))
                if ema20_dn_tested and ema20_dn_action and spot <= prior_bar.low:
                    if self._check_wyckoff_harmony("DOWN", curr_bar):
                        self.last_trigger_bar_idx["EMA_20_REJECTION_BREAKOUT"] = bar_idx
                        highest_high = max(prior_bar.high, curr_bar.high)
                        _log_shadow.info(f"[SHADOW_INTRABAR_SURGE] EMA_20_REJECTION_BREAKOUT PUT | Spot: {spot:.2f} <= AnchorLow: {prior_bar.low:.2f} at T+{sec_in_min}s")
                        self._open_shadow_trade("EMA_20_REJECTION_BREAKOUT", "DOWN", spot, highest_high, prior_bar.low, now_sec, n1_fp)
                        return

        # 4. Pain Zone Direct Breakout / Breakdown (Mode B In-Flight Surge)
        # S/R Breakout parity: Triggers instantly when spot breaks highest line (zone_top) for CALL
        # or lowest line (zone_bottom) for PUT from T:02s onwards with zero debounce
        if pz_top > 0 and pz_bot > 0:
            last_trig_pz_bo = max(self.last_trigger_bar_idx.get("PAIN_ZONE_BREAKOUT", -100), self.last_trigger_bar_idx.get("PAIN_ZONE_REJECTION_BREAKOUT", -100))
            if (bar_idx - last_trig_pz_bo) >= 3:
                # Bullish Breakout: Candle opened inside/below Pain Zone, spot breaks above highest line (zone_top)
                if (curr_bar.open <= pz_top or prior_bar.close <= pz_top) and spot > pz_top:
                    if self._check_dow_thrust_progression("UP", curr_bar, spot, atr):
                        if self._check_wyckoff_harmony("UP", curr_bar):
                            self.last_trigger_bar_idx["PAIN_ZONE_BREAKOUT"] = bar_idx
                            lowest_low = min(prior_bar.low, curr_bar.low)
                            highest_high = max(spot, curr_bar.high)
                            _log_shadow.info(f"[SHADOW_INTRABAR_SURGE] PAIN_ZONE_BREAKOUT CALL | Spot: {spot:.2f} > ZoneTop: {pz_top:.2f} at T+{sec_in_min}s | Dow Thrust OK")
                            self._open_shadow_trade("PAIN_ZONE_BREAKOUT", "UP", spot, highest_high, lowest_low, now_sec, n1_fp)
                            return

                # Bearish Breakdown: Candle opened inside/above Pain Zone, spot breaks below lowest line (zone_bottom)
                if (curr_bar.open >= pz_bot or prior_bar.close >= pz_bot) and spot < pz_bot:
                    if self._check_dow_thrust_progression("DOWN", curr_bar, spot, atr):
                        if self._check_wyckoff_harmony("DOWN", curr_bar):
                            self.last_trigger_bar_idx["PAIN_ZONE_BREAKOUT"] = bar_idx
                            highest_high = max(prior_bar.high, curr_bar.high)
                            lowest_low = min(spot, curr_bar.low)
                            _log_shadow.info(f"[SHADOW_INTRABAR_SURGE] PAIN_ZONE_BREAKOUT PUT | Spot: {spot:.2f} < ZoneBottom: {pz_bot:.2f} at T+{sec_in_min}s | Dow Thrust OK")
                            self._open_shadow_trade("PAIN_ZONE_BREAKOUT", "DOWN", spot, highest_high, lowest_low, now_sec, n1_fp)
                            return

        # 4b. Pain Zone Mid (Central Institutional VWAP) Direct Breakout / Breakdown
        # Triggers at the true market equilibrium inflection point when spot crosses Zone Mid
        pz_mid = self._get_pain_zone_mid()
        if pz_mid > 0:
            last_trig_pz_mid = max(self.last_trigger_bar_idx.get("PAIN_ZONE_MID_BREAKOUT", -100), self.last_trigger_bar_idx.get("PAIN_ZONE_BREAKOUT", -100))
            if (bar_idx - last_trig_pz_mid) >= 3:
                # Bullish Breakout: Candle opened at/below Zone Mid, spot breaks above Zone Mid
                if (curr_bar.open <= pz_mid or prior_bar.close <= pz_mid) and spot > pz_mid:
                    if self._check_dow_thrust_progression("UP", curr_bar, spot, atr):
                        if self._check_wyckoff_harmony("UP", curr_bar):
                            self.last_trigger_bar_idx["PAIN_ZONE_MID_BREAKOUT"] = bar_idx
                            lowest_low = min(prior_bar.low, curr_bar.low)
                            highest_high = max(spot, curr_bar.high)
                            _log_shadow.info(f"[SHADOW_INTRABAR_SURGE] PAIN_ZONE_MID_BREAKOUT CALL | Spot: {spot:.2f} > ZoneMid: {pz_mid:.2f} at T+{sec_in_min}s | Dow Thrust OK")
                            self._open_shadow_trade("PAIN_ZONE_MID_BREAKOUT", "UP", spot, highest_high, lowest_low, now_sec, n1_fp)
                            return

                # Bearish Breakdown: Candle opened at/above Zone Mid, spot breaks below Zone Mid
                if (curr_bar.open >= pz_mid or prior_bar.close >= pz_mid) and spot < pz_mid:
                    if self._check_dow_thrust_progression("DOWN", curr_bar, spot, atr):
                        if self._check_wyckoff_harmony("DOWN", curr_bar):
                            self.last_trigger_bar_idx["PAIN_ZONE_MID_BREAKOUT"] = bar_idx
                            highest_high = max(prior_bar.high, curr_bar.high)
                            lowest_low = min(spot, curr_bar.low)
                            _log_shadow.info(f"[SHADOW_INTRABAR_SURGE] PAIN_ZONE_MID_BREAKOUT PUT | Spot: {spot:.2f} < ZoneMid: {pz_mid:.2f} at T+{sec_in_min}s | Dow Thrust OK")
                            self._open_shadow_trade("PAIN_ZONE_MID_BREAKOUT", "DOWN", spot, highest_high, lowest_low, now_sec, n1_fp)
                            return

        # 4c. Trapped Writer Penetration Breakout / Breakdown (Decoupled Corridors)
        # Specifically attacks the counterparty's pain point:
        # CALL: Spot breaks above Call Sell VWAP (Call Writers Trapped -> Short Covering Squeeze)
        # PUT: Spot breaks below Put Sell VWAP (Put Writers Trapped -> Long Liquidation Flush)
        cs, ps = self._get_pain_zone_writer_levels()
        if cs > 0:
            last_trig_call_trap = self.last_trigger_bar_idx.get("CALL_WRITER_TRAP_BREAKOUT", -100)
            if (bar_idx - last_trig_call_trap) >= 3:
                # Bullish Breakout: Candle opened at/below Call Sell VWAP, spot crosses above Call Sell VWAP
                if (curr_bar.open <= cs or prior_bar.close <= cs) and spot > cs:
                    if self._check_dow_thrust_progression("UP", curr_bar, spot, atr):
                        if self._check_wyckoff_harmony("UP", curr_bar):
                            self.last_trigger_bar_idx["CALL_WRITER_TRAP_BREAKOUT"] = bar_idx
                            lowest_low = min(prior_bar.low, curr_bar.low)
                            highest_high = max(spot, curr_bar.high)
                            _log_shadow.info(
                                f"[SHADOW_INTRABAR_SURGE] CALL_WRITER_TRAP_BREAKOUT CALL | "
                                f"Spot: {spot:.2f} > CallSellVWAP: {cs:.2f} at T+{sec_in_min}s | Dow Thrust OK"
                            )
                            self._open_shadow_trade(
                                "CALL_WRITER_TRAP_BREAKOUT", "UP", spot, highest_high, lowest_low, now_sec, n1_fp
                            )
                            return

        if ps > 0:
            last_trig_put_trap = self.last_trigger_bar_idx.get("PUT_WRITER_TRAP_BREAKOUT", -100)
            if (bar_idx - last_trig_put_trap) >= 3:
                # Bearish Breakdown: Candle opened at/above Put Sell VWAP, spot breaks below Put Sell VWAP
                if (curr_bar.open >= ps or prior_bar.close >= ps) and spot < ps:
                    if self._check_dow_thrust_progression("DOWN", curr_bar, spot, atr):
                        if self._check_wyckoff_harmony("DOWN", curr_bar):
                            self.last_trigger_bar_idx["PUT_WRITER_TRAP_BREAKOUT"] = bar_idx
                            highest_high = max(prior_bar.high, curr_bar.high)
                            lowest_low = min(spot, curr_bar.low)
                            _log_shadow.info(
                                f"[SHADOW_INTRABAR_SURGE] PUT_WRITER_TRAP_BREAKOUT PUT | "
                                f"Spot: {spot:.2f} < PutSellVWAP: {ps:.2f} at T+{sec_in_min}s | Dow Thrust OK"
                            )
                            self._open_shadow_trade(
                                "PUT_WRITER_TRAP_BREAKOUT", "DOWN", spot, highest_high, lowest_low, now_sec, n1_fp
                            )
                            return

        # 5. 200 EMA Direct Breakout / Breakdown (Mode B In-Flight Surge)
        # S/R Breakout parity: Triggers instantly when spot crosses 200 EMA to the upside (CALL)
        # or breaks below 200 EMA low / ribbon lower boundary (PUT)
        if ema200 > 0:
            last_trig_ema_bo = max(self.last_trigger_bar_idx.get("EMA_200_BREAKOUT", -100), self.last_trigger_bar_idx.get("EMA_200_REJECTION_BREAKOUT", -100))
            if (bar_idx - last_trig_ema_bo) >= 3:
                _, ema_bot = self._get_ema200_levels(atr)
                ema_breakdown_level = ema_bot if ema_bot > 0 else ema200

                # Bullish Breakout: Candle opened at/below 200 EMA, spot crosses above 200 EMA
                if (curr_bar.open <= ema200 or prior_bar.close <= ema200) and spot > ema200:
                    if self._check_dow_thrust_progression("UP", curr_bar, spot, atr):
                        if self._check_wyckoff_harmony("UP", curr_bar):
                            self.last_trigger_bar_idx["EMA_200_BREAKOUT"] = bar_idx
                            lowest_low = min(prior_bar.low, curr_bar.low)
                            highest_high = max(spot, curr_bar.high)
                            _log_shadow.info(f"[SHADOW_INTRABAR_SURGE] EMA_200_BREAKOUT CALL | Spot: {spot:.2f} > EMA200: {ema200:.2f} at T+{sec_in_min}s | Dow Thrust OK")
                            self._open_shadow_trade("EMA_200_BREAKOUT", "UP", spot, highest_high, lowest_low, now_sec, n1_fp)
                            return

                # Bearish Breakdown: Candle opened at/above 200 EMA low, spot breaks below 200 EMA low
                if (curr_bar.open >= ema_breakdown_level or prior_bar.close >= ema_breakdown_level) and spot < ema_breakdown_level:
                    if self._check_dow_thrust_progression("DOWN", curr_bar, spot, atr):
                        if self._check_wyckoff_harmony("DOWN", curr_bar):
                            self.last_trigger_bar_idx["EMA_200_BREAKOUT"] = bar_idx
                            highest_high = max(prior_bar.high, curr_bar.high)
                            lowest_low = min(spot, curr_bar.low)
                            _log_shadow.info(f"[SHADOW_INTRABAR_SURGE] EMA_200_BREAKOUT PUT | Spot: {spot:.2f} < EMA200Low: {ema_breakdown_level:.2f} at T+{sec_in_min}s | Dow Thrust OK")
                            self._open_shadow_trade("EMA_200_BREAKOUT", "DOWN", spot, highest_high, lowest_low, now_sec, n1_fp)
                            return

    def _evaluate_boundary_arming(self, spot: float, now_sec: float) -> None:
        """Evaluates boundary rejection on latest closed 1m candle and arms Mode A."""
        if self.pa_engine is None or len(self.pa_engine.bars_1m) < 20:
            return
            
        bars = self.pa_engine.bars_1m
        bar_idx = len(bars) - 1
        bar = bars[-1]
        
        atr = getattr(self.pa_engine, "current_atr_1m", 10.0) or 10.0
        if atr <= 0:
            atr = 10.0
            
        # Candle Geometry
        c_range = bar.high - bar.low
        if c_range <= 0 or (c_range / atr) > 1.25:  # Filter out exhaustion bars > 1.25 ATR
            return
            
        lower_wick = (min(bar.open, bar.close) - bar.low)
        upper_wick = (bar.high - max(bar.open, bar.close))
        lower_wick_ratio = lower_wick / c_range
        upper_wick_ratio = upper_wick / c_range
        
        boundaries_to_check = [
            ("PAIN_ZONE_BAND", self._get_pain_zone_levels(atr)),
            ("EMA_200_BAND", self._get_ema200_levels(atr)),
        ]
        
        for b_name, (b_top, b_bottom) in boundaries_to_check:
            if b_top <= 0 or b_bottom <= 0:
                continue
                
            # Prevent multi-triggering within 3 bars
            last_idx = self.last_trigger_bar_idx.get(b_name, -100)
            if (bar_idx - last_idx) < 3:
                continue
                
            # CALL Bounce Check (Support Rejection)
            call_touch = (bar.low <= b_top + 0.35 * atr) and (bar.close >= b_bottom - 0.25 * atr)
            call_rej = (lower_wick_ratio >= 0.20) or (bar.close > bar.open)
            
            if call_touch and call_rej:
                # Wyckoff Delta Harmony
                if self._check_wyckoff_harmony("UP", bar):
                    self.armed_setups[b_name] = {
                        "direction": "UP",
                        "anchor_high": bar.high,
                        "anchor_low": bar.low,
                        "armed_bar_idx": bar_idx,
                        "ts": now_sec,
                    }
                    self.last_trigger_bar_idx[b_name] = bar_idx
                    _log_shadow.info(f"[SHADOW_ARMED] {b_name} CALL Rejection Armed | AnchorHigh: {bar.high:.2f} | Await in-flight pierce")
                    continue
                    
            # PUT Rejection Check (Resistance Rejection)
            put_touch = (bar.high >= b_bottom - 0.35 * atr) and (bar.close <= b_top + 0.25 * atr)
            put_rej = (upper_wick_ratio >= 0.20) or (bar.close < bar.open)
            
            if put_touch and put_rej:
                if self._check_wyckoff_harmony("DOWN", bar):
                    self.armed_setups[b_name] = {
                        "direction": "DOWN",
                        "anchor_high": bar.high,
                        "anchor_low": bar.low,
                        "armed_bar_idx": bar_idx,
                        "ts": now_sec,
                    }
                    self.last_trigger_bar_idx[b_name] = bar_idx
                    _log_shadow.info(f"[SHADOW_ARMED] {b_name} PUT Rejection Armed | AnchorLow: {bar.low:.2f} | Await in-flight pierce")

    def _compute_n1_footprint(self, prior_bar: Any, atr: float) -> Dict[str, Any]:
        """
        Computes Wyckoff Volume & Order Flow Footprint on testing candle N-1:
          1. Volume Dry-Up (Supply/Demand exhaustion)
          2. Synthetic Net Delta: (Call_Buy + Put_Sell) - (Put_Buy + Call_Sell)
          3. Stopping Volume (Effort vs Result: high volume into support with small range)
        """
        bars = getattr(self.pa_engine, "bars_1m", [])
        n_bars = len(bars)
        start_idx = max(0, n_bars - 21)
        # Exclude prior_bar (which is bars[-1]) from baseline
        sample_vols = [float(getattr(bars[i], "volume", 0.0) or 0.0) for i in range(start_idx, max(0, n_bars - 1))]
        pos_vols = [v for v in sample_vols if v > 0]
        base_vol = (sum(pos_vols) / len(pos_vols)) if pos_vols else 1.0

        n1_vol = float(getattr(prior_bar, "volume", 0.0) or 0.0)
        cb = float(getattr(prior_bar, "call_buy_vol", 0.0) or 0.0)
        cs = float(getattr(prior_bar, "call_sell_vol", 0.0) or 0.0)
        pb = float(getattr(prior_bar, "put_buy_vol", 0.0) or 0.0)
        ps = float(getattr(prior_bar, "put_sell_vol", 0.0) or 0.0)

        # 1. Volume Dry-Up: volume is lower than 80% of baseline (proves sellers/buyers exhausted)
        vol_dryup = (n1_vol < 0.80 * base_vol) if (base_vol > 0 and n1_vol > 0) else False

        # 2. Synthetic Net Delta: Bulls (CB + PS) vs Bears (PB + CS)
        bulls = cb + ps
        bears = pb + cs
        net_delta = bulls - bears

        # 3. Stopping Volume: Volume expanded >= 1.25x baseline, but candle range stayed <= 0.80x ATR
        c_range = float(getattr(prior_bar, "high", 0.0) - getattr(prior_bar, "low", 0.0))
        stopping_vol = (n1_vol >= 1.25 * base_vol and c_range <= 0.80 * atr) if base_vol > 0 else False

        return {
            "n1_vol": round(n1_vol, 1),
            "n1_baseline_vol": round(base_vol, 1),
            "n1_vol_dryup": vol_dryup,
            "n1_net_delta": round(net_delta, 1),
            "n1_bulls": round(bulls, 1),
            "n1_bears": round(bears, 1),
            "n1_stopping_vol": stopping_vol,
        }

    def _get_pain_zone_levels(self, atr: float) -> Tuple[float, float]:
        """Fetches live Pain Zone levels directly from PriceActionEngine single source."""
        try:
            pz = self.pa_engine.get_pain_zone_vwaps()
            return float(pz.get("zone_top", 0.0) or 0.0), float(pz.get("zone_bottom", 0.0) or 0.0)
        except Exception:
            return 0.0, 0.0

    def _get_pain_zone_mid(self) -> float:
        """Fetches live Pain Zone composite midpoint (Zone Mid / Central Institutional VWAP)."""
        try:
            pz = self.pa_engine.get_pain_zone_vwaps()
            return float(pz.get("zone_mid", 0.0) or 0.0)
        except Exception:
            return 0.0

    def _get_pain_zone_writer_levels(self) -> Tuple[float, float]:
        """Fetches live Call Writer VWAP (vwap_call_sell) and Put Writer VWAP (vwap_put_sell)."""
        try:
            pz = self.pa_engine.get_pain_zone_vwaps()
            cs = float(pz.get("vwap_call_sell", 0.0) or 0.0)
            ps = float(pz.get("vwap_put_sell", 0.0) or 0.0)
            return cs, ps
        except Exception:
            return 0.0, 0.0

    def _get_ema200_levels(self, atr: float) -> Tuple[float, float]:
        """Fetches live 200 EMA ribbon levels from PriceActionEngine."""
        try:
            ema200 = float(getattr(self.pa_engine, "ema_200_1m", 0.0) or 0.0)
            if ema200 > 0:
                return (ema200 + 0.25 * atr), (ema200 - 0.25 * atr)
        except Exception:
            pass
        return 0.0, 0.0

    def _check_dow_thrust_progression(self, direction: str, curr_bar: Any, spot: float, atr: float) -> bool:
        """
        Dow Theory 3-Candle Directional Thrust Validator for Boundary Breakouts:
        Filters out false/whipsaw breakouts when price coils / ranges around VWAP (Pain Zone) or 200 EMA.

        Requires a clean 3-candle ascending (CALL) or descending (PUT) ladder:
          - CALL (HH/HL Thrust from below):
              1. Bar N-1 High > Bar N-2 High
              2. Bar N-1 Low > Bar N-2 Low
              3. Forming Bar N Low >= (Bar N-1 Low - 0.05 * atr)
              4. Live Spot > Bar N-1 High (or Forming Bar N High > Bar N-1 High)
          - PUT (LH/LL Thrust from above):
              1. Bar N-1 Low < Bar N-2 Low
              2. Bar N-1 High < Bar N-2 High
              3. Forming Bar N High <= (Bar N-1 High + 0.05 * atr)
              4. Live Spot < Bar N-1 Low (or Forming Bar N Low < Bar N-1 Low)
        """
        bars = getattr(self.pa_engine, "bars_1m", [])
        if len(bars) < 2:
            return False

        bar_n1 = bars[-1]
        bar_n2 = bars[-2]

        h_n1, l_n1 = float(getattr(bar_n1, "high", 0.0)), float(getattr(bar_n1, "low", 0.0))
        h_n2, l_n2 = float(getattr(bar_n2, "high", 0.0)), float(getattr(bar_n2, "low", 0.0))
        curr_h = float(getattr(curr_bar, "high", 0.0))
        curr_l = float(getattr(curr_bar, "low", 0.0))

        tol = 0.05 * max(atr, 1.0)

        if direction.upper() in ("UP", "CALL"):
            # 1. Bar N-1 made Higher High & Higher Low relative to Bar N-2
            n1_ascending = (h_n1 > h_n2) and (l_n1 > l_n2)
            # 2. Forming Bar N Low holds above Bar N-1 Low (within micro-wick tolerance)
            curr_hl = curr_l >= (l_n1 - tol)
            # 3. Forming Bar / Live Spot breaks above Bar N-1 High
            curr_hh = (spot > h_n1) or (curr_h > h_n1)
            return bool(n1_ascending and curr_hl and curr_hh)
        else:
            # 1. Bar N-1 made Lower Low & Lower High relative to Bar N-2
            n1_descending = (l_n1 < l_n2) and (h_n1 < h_n2)
            # 2. Forming Bar N High holds below Bar N-1 High (within micro-wick tolerance)
            curr_lh = curr_h <= (h_n1 + tol)
            # 3. Forming Bar / Live Spot breaks below Bar N-1 Low
            curr_ll = (spot < l_n1) or (curr_l < l_n1)
            return bool(n1_descending and curr_lh and curr_ll)

    def _check_wyckoff_harmony(self, direction: str, bar: Any) -> bool:
        """Verifies institutional order flow delta harmony (no opposing liquidation dump)."""
        opt_flow = getattr(self.pa_engine, "_opt_flow_1m", {}) if self.pa_engine else {}
        if not isinstance(opt_flow, dict):
            opt_flow = {}
        cb = float(opt_flow.get("call_buy_vol", 0.0) or getattr(bar, "call_buy_vol", 0.0) or 0.0)
        pb = float(opt_flow.get("put_buy_vol", 0.0) or getattr(bar, "put_buy_vol", 0.0) or 0.0)
        
        # If option flow volume is active
        if cb > 1000.0 or pb > 1000.0:
            if direction == "UP" and pb > 2.5 * max(cb, 1.0):
                return False  # Heavy put buying into support (dumping)
            elif direction == "DOWN" and cb > 2.5 * max(pb, 1.0):
                return False  # Heavy call buying into resistance (short squeeze)
        return True

    def _open_shadow_trade(
        self,
        boundary: str,
        direction: str,
        spot: float,
        anchor_high: float,
        anchor_low: float,
        now_sec: float,
        n1_fp: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Creates a new OTM 5 paper trade with sub-minute telemetry and N-1 Wyckoff footprint."""
        opt_sym, opt_strike, opt_side = self.select_otm5_symbol(direction, spot)
        opt_lp = self.option_cache.get(opt_sym, 0.0)
        if opt_lp <= 0.0:
            return
        
        time_str = time.strftime("%H:%M:%S", time.localtime(now_sec))
        trade_id = f"SHADOW_{boundary}_{direction}_{int(now_sec)}"
        
        atr = getattr(self.pa_engine, "current_atr_1m", 10.0) or 10.0
        invalidation_cushion = max(2.0, (spot / 24000.0) * 2.0, 0.10 * atr)
        spot_sl = (anchor_low - invalidation_cushion) if direction == "UP" else (anchor_high + invalidation_cushion)
        tp_price = opt_lp * 1.04 if opt_lp > 0 else 0.0
        sl_price = opt_lp * 0.88 if opt_lp > 0 else 0.0

        pz_dict = self.pa_engine.get_pain_zone_vwaps() if hasattr(self.pa_engine, "get_pain_zone_vwaps") else {}
        pz_top_val = float(pz_dict.get("zone_top", 0.0) or 0.0)
        pz_bot_val = float(pz_dict.get("zone_bottom", 0.0) or 0.0)
        pz_spread = round(pz_top_val - pz_bot_val, 2) if (pz_top_val > 0 and pz_bot_val > 0) else 0.0

        n1 = n1_fp or {}
        trade = ShadowTrade(
            trade_id=trade_id,
            boundary=boundary,
            direction=direction,
            entry_ts=now_sec,
            entry_time_str=time_str,
            spot_entry=spot,
            spot_anchor_high=anchor_high,
            spot_anchor_low=anchor_low,
            spot_sl_price=spot_sl,
            opt_symbol=opt_sym,
            opt_strike=opt_strike,
            opt_side=opt_side,
            opt_entry=opt_lp,
            opt_tp_price=tp_price,
            opt_sl_price=sl_price,
            n1_vol=float(n1.get("n1_vol", 0.0)),
            n1_baseline_vol=float(n1.get("n1_baseline_vol", 0.0)),
            n1_vol_dryup=bool(n1.get("n1_vol_dryup", False)),
            n1_net_delta=float(n1.get("n1_net_delta", 0.0)),
            n1_stopping_vol=bool(n1.get("n1_stopping_vol", False)),
            pz_top=pz_top_val,
            pz_bottom=pz_bot_val,
            pz_mid=float(pz_dict.get("zone_mid", 0.0) or 0.0),
            pz_spread_pts=pz_spread,
            pz_call_buy=float(pz_dict.get("vwap_call_buy", 0.0) or 0.0),
            pz_call_sell=float(pz_dict.get("vwap_call_sell", 0.0) or 0.0),
            pz_put_buy=float(pz_dict.get("vwap_put_buy", 0.0) or 0.0),
            pz_put_sell=float(pz_dict.get("vwap_put_sell", 0.0) or 0.0),
        )
        
        self.active_trades.append(trade)
        _log_shadow.info(
            f"⚡ [SHADOW_ENTRY] {boundary} {direction} Triggered @ {time_str} | "
            f"Spot: {spot:.2f} (SL: {spot_sl:.2f}) | "
            f"OTM5: {opt_sym} @ ₹{opt_lp:.2f} (TP +4%: ₹{tp_price:.2f}, SL -12%: ₹{sl_price:.2f}) | "
            f"N-1 Footprint: Vol={trade.n1_vol:.0f} (Base={trade.n1_baseline_vol:.0f}, Dry={trade.n1_vol_dryup}) "
            f"NetDelta={trade.n1_net_delta:+.0f} StoppingVol={trade.n1_stopping_vol}"
        )

    def _close_trade(self, trade: ShadowTrade, now_sec: float, exit_reason: str, outcome: str) -> None:
        """Finalizes shadow trade and appends telemetry record to JSONL."""
        trade.is_active = False
        trade.exit_ts = now_sec
        trade.exit_reason = exit_reason
        trade.outcome = outcome
        trade.duration_sec = round(now_sec - trade.entry_ts, 1)
        
        # Log outcome
        log_badge = "🟢 WINNER" if outcome == "WINNER" else ("🔴 LOSER" if outcome == "LOSER" else "⚪ EXPIRED")
        _log_shadow.info(
            f"{log_badge} [SHADOW_OUTCOME] {trade.boundary} {trade.direction} | "
            f"Exit: {exit_reason} in {trade.duration_sec}s | "
            f"Option: Peak MFE +{trade.opt_peak_mfe_pct:.1f}%, MAE {trade.opt_peak_mae_pct:.1f}% | "
            f"Spot: Peak MFE +{trade.spot_peak_mfe:.1f}pts, MAE {trade.spot_peak_mae:.1f}pts | "
            f"30s Move: Opt {trade.opt_mfe_30s_pct:+.1f}%, Spot {trade.spot_mfe_30s:+.1f}pts | "
            f"60s Move: Opt {trade.opt_mfe_60s_pct:+.1f}%, Spot {trade.spot_mfe_60s:+.1f}pts | "
            f"N-1 Footprint: NetDelta={trade.n1_net_delta:+.0f}, Dry={trade.n1_vol_dryup}, StopVol={trade.n1_stopping_vol}"
        )
        
        # Save to disk
        self._persist_trade(trade)
        if trade in self.active_trades:
            self.active_trades.remove(trade)

    def _persist_trade(self, trade: ShadowTrade) -> None:
        """Appends trade telemetry to daily JSONL file."""
        try:
            date_str = time.strftime("%Y-%m-%d", time.localtime(trade.entry_ts))
            file_path = os.path.join(self.log_dir, f"aegis_boundary_shadow_trades_{date_str}.jsonl")
            with open(file_path, "a") as f:
                f.write(json.dumps(asdict(trade)) + "\n")
        except Exception as e:
            _log_shadow.debug(f"[ShadowBoundaryObserver] Error persisting trade: {e}")

# DEPRECATED — 2026-08-29 audit P1.7: live V3 execution inlines spread/depth checks
# at aegis_execution_manager.py:382 (spread 2% / sq1<3) and option_quality.py does
# the canonical veto. This helper is kept for backward import compat only.
# Do not call on hot path; if needed, call OptionQualityEngine.qualify_option instead.
def depth_guard_passes(broker, token, side, ratio_limit=2.0):
    """[DEPRECATED SHADOW] On-demand 5-level depth. Fail-safe: any error => block."""
    try:
        if hasattr(broker, 'get_depth'):
            depth = broker.get_depth(token)
        elif hasattr(broker, 'market_data_provider') and hasattr(broker.market_data_provider, 'get_quotes'):
            quotes = broker.market_data_provider.get_quotes([token])
            depth = quotes[0] if quotes else {}
        else:
            return False
        asks = depth.get('asks', [])[:5]
        bids = depth.get('bids', [])[:5]
        if not asks or not bids:
            return False
        avg_ask = sum(a.get('qty', a.get('size', 0)) for a in asks) / len(asks)
        avg_bid = sum(b.get('qty', b.get('size', 0)) for b in bids) / len(bids)
        if side == "CALL":
            return not (avg_ask > ratio_limit * avg_bid)
        return not (avg_bid > ratio_limit * avg_ask)
    except Exception:
        return False
