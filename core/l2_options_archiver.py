"""AEGIS L2 Options Archiver v1.0 — Raw Options Data Logger (Partitioned Parquet)

Subscribes to ALL option tokens AEGIS is tracking (dynamic ATM±10 CE/PE) plus
the underlying cash index. Writes raw L2 ticks to partitioned Snappy Parquet
(15-min files) under AEGIS/data/l2_raw/.

ZERO Greeks math. Pure Passive Recorder. Mirrors ARGUS/l2_archiver.py
discipline (no heavy imports in hot path, partitioned writes, no in-memory
accumulation) but is enriched with the options-specific OI column and a
`kind` discriminator (index/CE/PE) so post-market forensics can reconstruct
the full gamma surface.

Hardware Target: Oracle 1GB RAM / 1 OCPU (Zero-Allocation Hot Paths Enforced).
"""
from __future__ import annotations
import os
import sys

# ─── BOOTSTRAP ───
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import json
import time as pytime
import queue
import threading
from datetime import datetime, time as dtime, timedelta

import numpy as np
import pandas as pd

from config.settings import get_data_dir
from logger import get_logger

logger = get_logger("aegis_l2_archiver")

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
        self._map_lock = threading.Lock()
        logger.info(
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
        with self._map_lock:
            self.tok_to_sym = {str(k): str(v) for k, v in token_to_symbol.items()}
            if token_map:
                self.token_meta = {str(k): v for k, v in token_map.items()}
            self.target_tokens = set(self.tok_to_sym.keys()) | UNDERLYING_TOKENS
            count = len(self.target_tokens)
        logger.info(
            f"AegisL2Archiver: token maps refreshed — now tracking {count} tokens"
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
            logger.debug(f"Prune failed: {e}")

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
            logger.info(
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
            logger.error(f"AegisL2Archiver partition flush failed: {e}")

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
            logger.error(f"AegisL2Archiver auction flush failed: {exc}")

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
                logger.error(f"AegisL2Archiver worker error: {exc}")
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
            if not tok:
                return

            with self._map_lock:
                if tok not in self.target_tokens:
                    return
                sym = self.tok_to_sym.get(tok) or ("NIFTY" if tok == "26000" else "SENSEX" if tok == "1" else "NIFTY29SEP26F" if tok == "68407" else "SENSEX26SEPFUT" if tok == "844615" else "")
                meta = self.token_meta.get(tok, {})

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

            # Raw Row Construction (Unrolled for Speed)
            row = {
                "ts": ts_ms,
                "tok": int(tok),
                "sym": sym,
                "kind": np.int8(kind),
                "strike": strike,
                "lp": np.float32(lp),
                "vol": np.float32(msg.get("volume_trade_for_the_day") or msg.get("v") or 0),
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
                    logger.warning(
                        f"AegisL2Archiver queue full; dropped {self._dropped_ticks} tick records"
                    )

        except Exception as e:
            logger.debug(f"AegisL2Archiver tick error: {e}")

    def shutdown(self):
        """Call at EOD to flush final buffer."""
        logger.info("AegisL2Archiver: Shutdown signal received; draining archive queue...")
        self._stop_worker.set()
        self._worker.join(timeout=10.0)
        if self._worker.is_alive():
            logger.error("AegisL2Archiver worker did not stop within 10 seconds")
            return
        logger.info("AegisL2Archiver: Final flush complete.")


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
            logger.info("AegisL2Archiver ATTACHED (Raw Options Partitioned Logger)")
            return arch
    logger.warning("AegisL2Archiver: No add_tick_listener found.")
    return arch
