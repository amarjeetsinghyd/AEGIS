import os
import sqlite3
import json
from datetime import datetime
import contextlib
from typing import Dict, List, Optional, Any, Tuple
from AEGIS.config.settings import DATA_DIR
from common.logger import get_logger

logger = get_logger("instrument_repository")

DB_PATH = os.path.join(DATA_DIR, "instruments.db")
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
                logger.info(f"Upgrading database schema from version {row['version']} to {SCHEMA_VERSION}")
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
            valid_options = [o for o in option_contracts if o[0] >= now]
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
        
        logger.info(f"Staging sync data for broker {broker_name} ({len(instruments_data)} records)...")
        
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
                
                logger.info(f"Atomic swap complete. Registry holds {self.get_count()} total instruments.")
            except Exception as e:
                logger.error(f"Failed to perform atomic swap transaction: {e}")
                raise e
            finally:
                try:
                    conn.execute("DROP TABLE IF EXISTS temp_instruments;")
                    conn.execute("DROP TABLE IF EXISTS temp_broker_mappings;")
                except sqlite3.OperationalError:
                    pass
