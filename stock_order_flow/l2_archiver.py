"""L2 Archiver v3.0 — Raw Data Logger (Partitioned Parquet)
Subscribes ONLY to hardcoded 110 liquid symbols (< 3000 INR).
Writes raw L2 ticks to partitioned Snappy Parquet (15-min files).
ZERO Microstructure Math. Pure Passive Recorder.
Optimized for 1GB RAM VPS: Partitioned writes, no in-memory accumulation."""
from __future__ import annotations
import os
import sys

# ─── BOOTSTRAP ───
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import json, time as pytime
from datetime import datetime, time as dtime, timedelta
import numpy as np
import pandas as pd
from common.config import get_data_dir
from common.logger import get_logger
from common.symbols import SUBSCRIPTION_UNIVERSE

logger = get_logger("l2_archiver")

DATA = get_data_dir()
RAW_DIR = os.path.join(DATA, "l2_raw")       # Partitioned raw data
LOGS = os.path.join(DATA, "logs")             # Auction logs
os.makedirs(RAW_DIR, exist_ok=True)
os.makedirs(LOGS, exist_ok=True)

RETENTION_DAYS = 30
FLUSH_INTERVAL_SEC = 900  # 15 minutes = 1 partition file
AUCTION_LOG_INTERVAL = 2.0
TICK_SIZE = 0.05

# ─── Schema Definition (PyArrow/Parquet Optimized) ───
SCHEMA_DTYPES = {
    "ts": "int64",        # Unix ms
    "tok": "int32",       # Token
    "lp": "float32",      # Last Price
    "vol": "float32",     # Day Volume
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

class L2Archiver:
    def __init__(self, token_to_symbol: dict):
        # Reverse Map: Symbol -> Token (for Subscription Filter)
        self.sym_to_tok = {str(v): str(k) for k, v in token_to_symbol.items()}
        # Filter: Only keep tokens for our 110 symbols
        self.target_tokens = {self.sym_to_tok[s] for s in SUBSCRIPTION_UNIVERSE if s in self.sym_to_tok}
        logger.info(f"L2Archiver: Subscription Filter Active. Target Tokens: {len(self.target_tokens)}")

        # Token -> Symbol for writing
        self.tok_to_sym = {str(k): str(v) for k, v in token_to_symbol.items() if str(k) in self.target_tokens}
        
        self.buffer: list[dict] = []
        self.auction_buf: list[dict] = []
        self.last_flush = pytime.time()
        self.last_aflush = pytime.time()
        self.partition_seq = 0
        self.current_date = datetime.now().strftime("%Y-%m-%d")
        self._prune_old_files()

    def _prune_old_files(self):
        try:
            cut = datetime.now() - timedelta(days=RETENTION_DAYS)
            for f in os.listdir(RAW_DIR):
                if f.startswith("l2_raw_") and f.endswith(".parquet"):
                    # Parse date from filename l2_raw_YYYY-MM-DD_part_N.parquet
                    parts = f.replace("l2_raw_", "").replace(".parquet", "").split("_part_")
                    if len(parts) == 2:
                        d = datetime.strptime(parts[0], "%Y-%m-%d")
                        if d < cut: os.remove(os.path.join(RAW_DIR, f))
            # Prune Auction Logs
            cut_l = datetime.now() - timedelta(days=7)
            for f in os.listdir(LOGS):
                if (f.startswith("preopen_") or f.startswith("closeauction_")) and f.endswith(".jsonl"):
                    d = datetime.strptime(f.split("_")[1].replace(".jsonl", ""), "%Y-%m-%d")
                    if d < cut_l: os.remove(os.path.join(LOGS, f))
        except Exception as e:
            logger.debug(f"Prune failed: {e}")

    def _get_partition_path(self) -> str:
        return os.path.join(RAW_DIR, f"l2_raw_{self.current_date}_part_{self.partition_seq:03d}.parquet")

    def _flush_buffer(self, force=False, tick_ts_sec=None):
        nows = pytime.time()
        if not force and (nows - self.last_flush < FLUSH_INTERVAL_SEC) and len(self.buffer) < 50000:
            return
        
        if not self.buffer:
            self.last_flush = nows
            return

        try:
            df = pd.DataFrame(self.buffer)
            # Enforce Schema / Types
            for col, dtype in SCHEMA_DTYPES.items():
                if col in df.columns:
                    df[col] = df[col].astype(dtype)
            
            path = self._get_partition_path()
            df.to_parquet(path, index=False, compression="snappy")
            size_mb = os.path.getsize(path) / 1e6
            logger.info(f"L2Archiver: Flushed Partition {self.partition_seq} | {len(df)} rows | {size_mb:.1f} MB")
            
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
            logger.error(f"Partition flush failed: {e}")

    def _flush_auction(self, now: datetime):
        if not self.auction_buf: return
        try:
            kind = "preopen" if now.hour < 12 else "closeauction"
            path = os.path.join(LOGS, f"{kind}_{now.strftime('%Y-%m-%d')}.jsonl")
            with open(path, "a") as f:
                for r in self.auction_buf: f.write(json.dumps(r) + "\n")
            self.auction_buf = []
            self.last_aflush = pytime.time()
        except Exception: pass

    # ═══════════════════════════════════════════════════════════
    # HOT PATH — RAW TICK INGESTION (ZERO MATH)
    # ═══════════════════════════════════════════════════════════
    def on_tick(self, msg: dict):
        try:
            # Shoonya may send token as "token" or "tk" - handle both, normalize to string
            tok = str(msg.get("token") or msg.get("tk") or "")
            # HARD SUBSCRIPTION FILTER - normalize both sides to string for comparison
            if tok not in self.target_tokens:
                return

            sym = self.tok_to_sym.get(tok)
            if not sym: return

            lp = float(msg.get("last_traded_price") or msg.get("lp") or 0)
            if lp <= 0: return

            now = datetime.now()
            ts_ms = int(now.timestamp() * 1000)
            ts_sec = now.timestamp()
            ts_time = now.time()

            # Auction Logging (Pre-open / Close)
            if (dtime(9,0) <= ts_time < dtime(9,15) or dtime(15,30) <= ts_time < dtime(15,40)):
                if ts_sec - getattr(self, '_last_auction_ts', 0) >= AUCTION_LOG_INTERVAL:
                    self._last_auction_ts = ts_sec
                    sb = sum(float(msg.get(f"bq{i}") or 0) for i in range(1, 6))
                    sa = sum(float(msg.get(f"sq{i}") or 0) for i in range(1, 6))
                    self.auction_buf.append({"ts": ts_sec, "sym": sym, "lp": lp, "imb": (sb-sa)/max(sb+sa,1.0)})

            # Raw Row Construction (Unrolled for Speed)
            row = {"ts": ts_ms, "tok": int(tok), "lp": np.float32(lp), 
                   "vol": np.float32(msg.get("volume_trade_for_the_day") or msg.get("v") or 0)}
            
            for i in range(1, 6):
                row[f"bp{i}"] = np.float32(msg.get(f"bp{i}") or 0)
                row[f"bq{i}"] = np.int32(msg.get(f"bq{i}") or 0)
                row[f"sp{i}"] = np.float32(msg.get(f"sp{i}") or 0)
                row[f"sq{i}"] = np.int32(msg.get(f"sq{i}") or 0)

            self.buffer.append(row)

            # Periodic Flush Checks
            if ts_sec - self.last_aflush >= 30:
                self._flush_auction(now)
            self._flush_buffer(tick_ts_sec=ts_sec)

        except Exception as e:
            logger.debug(f"Archiver tick error: {e}")

    def shutdown(self):
        """Call at EOD (15:31) to flush final buffer."""
        logger.info("L2Archiver: Shutdown signal received. Final flush...")
        self._flush_buffer(force=True)
        self._flush_auction(datetime.now())
        logger.info("L2Archiver: Final flush complete.")


def attach_archiver(broker, token_to_symbol):
    arch = L2Archiver(token_to_symbol)
    candidates = [broker]
    for attr in ("market_data","mdp","market_data_provider","_mdp","_api","api","session_provider"):
        obj = getattr(broker, attr, None)
        if obj: candidates.append(obj)
    for c in candidates:
        if hasattr(c, "add_tick_listener"):
            c.add_tick_listener(arch.on_tick)
            logger.info("L2Archiver ATTACHED (Raw Partitioned Logger | 110 Symbol Filter Active)")
            return arch
    logger.warning("L2Archiver: No add_tick_listener found.")
    return arch


def main():
    """Standalone entry point for PM2 - runs archiver independently."""
    import signal
    from common.broker import ShoonyaAdapter
    from common.config import configure_runtime
    
    configure_runtime("stock_order_flow")
    
    logger.info("=" * 60)
    logger.info("L2 ARCHIVER STANDALONE MODE - Starting at 08:55 IST")
    logger.info("=" * 60)
    
    # Initialize broker
    broker = ShoonyaAdapter()
    if not broker.connect():
        logger.critical("Failed to connect to broker. Exiting.")
        sys.exit(1)
    
    # Load token map from cache
    from common.config import get_data_dir
    import json
    token_cache_path = os.path.join(get_data_dir(), "tokens_cache.json")
    try:
        with open(token_cache_path, "r") as f:
            token_cache = json.load(f)
        # Build token_to_symbol for our 110 symbols
        token_to_symbol = {}
        for sym in SUBSCRIPTION_UNIVERSE:
            if sym in token_cache and "token" in token_cache[sym]:
                token_to_symbol[str(token_cache[sym]["token"])] = sym
        logger.info(f"Loaded {len(token_to_symbol)} tokens for archiver subscription")
    except Exception as e:
        logger.error(f"Failed to load token cache: {e}")
        sys.exit(1)
    
    # Attach archiver
    arch = attach_archiver(broker, token_to_symbol)
    if not arch:
        logger.critical("Failed to attach archiver. Exiting.")
        sys.exit(1)
    
    # Signal handlers for graceful shutdown
    def signal_handler(signum, frame):
        logger.info(f"Signal {signum} received. Shutting down...")
        arch.shutdown()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Keep running
    logger.info("L2 Archiver running. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(60)
            # Log heartbeat
            logger.info(f"[L2Archiver HB] Buffer: {len(arch.buffer)} rows | Partition: {arch.partition_seq}")
    except KeyboardInterrupt:
        arch.shutdown()


if __name__ == "__main__":
    main()
