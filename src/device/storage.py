"""Crash-safe durable storage engine for device-side sample and loss persistence."""
import json
import os
import shutil
import sqlite3
import time
from typing import Any, Dict, Generator, List, Optional, Tuple

from src.common.logging import setup_logging
from src.common.models import LossRecord, Sample, WindowResult

logger = setup_logging("device-storage")

# Minimum free disk space required before safe operational shutdown / degraded mode (10 MB)
MIN_FREE_DISK_BYTES = 10 * 1024 * 1024


class DiskFullError(Exception):
    """Raised when available storage drops below safe operating threshold."""
    pass


class DeviceStorage:
    """Manages SQLite storage with Write-Ahead Logging (WAL) and strict ACID durability."""

    def __init__(self, db_path: str, synchronous_full: bool = True):
        self.db_path = db_path
        self.synchronous_full = synchronous_full
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode = WAL;")
        if self.synchronous_full:
            cursor.execute("PRAGMA synchronous = FULL;")
        else:
            cursor.execute("PRAGMA synchronous = NORMAL;")
        cursor.execute("PRAGMA busy_timeout = 10000;")
        cursor.close()
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Samples table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS samples (
                session_id TEXT NOT NULL,
                sensor INTEGER NOT NULL,
                sample_index INTEGER NOT NULL,
                ch1 INTEGER NOT NULL,
                ch2 INTEGER NOT NULL,
                ch3 INTEGER,
                synced INTEGER DEFAULT 0,
                created_at REAL NOT NULL,
                PRIMARY KEY (session_id, sensor, sample_index)
            );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_samples_sync ON samples (session_id, synced, sensor, sample_index);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_samples_win ON samples (session_id, sensor, sample_index);")

            # Losses table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS losses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                sensor INTEGER NOT NULL,
                from_index INTEGER NOT NULL,
                to_index INTEGER,
                reason TEXT NOT NULL,
                synced INTEGER DEFAULT 0,
                created_at REAL NOT NULL,
                UNIQUE (session_id, sensor, from_index, to_index, reason)
            );
            """)

            # Checkpoints table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS checkpoints (
                session_id TEXT NOT NULL,
                sensor INTEGER NOT NULL,
                last_index INTEGER NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (session_id, sensor)
            );
            """)

            # Window results table
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS window_results (
                session_id TEXT NOT NULL,
                window INTEGER NOT NULL,
                mode TEXT NOT NULL,
                payload TEXT NOT NULL,
                synced INTEGER DEFAULT 0,
                created_at REAL NOT NULL,
                PRIMARY KEY (session_id, window, mode)
            );
            """)
            conn.commit()

    def check_disk_space(self):
        """Verify that sufficient free disk space exists before writing."""
        target_dir = os.path.dirname(os.path.abspath(self.db_path))
        try:
            usage = shutil.disk_usage(target_dir)
            if usage.free < MIN_FREE_DISK_BYTES:
                raise DiskFullError(
                    f"Available disk space ({usage.free} bytes) is below safety margin ({MIN_FREE_DISK_BYTES} bytes)"
                )
        except OSError as e:
            logger.warning(f"Unable to check disk usage: {e}")

    def save_samples_and_losses(
        self,
        session_id: str,
        sensor: int,
        first_index: int,
        data: List[List[int]],
        lost: List[Dict[str, Any]]
    ) -> int:
        """Atomically persist a batch of samples and explicit loss records.
        
        Returns the count of new samples inserted.
        Idempotent: duplicate samples are ignored via PRIMARY KEY uniqueness.
        """
        self.check_disk_space()
        now = time.time()
        inserted_samples = 0

        with self._get_connection() as conn:
            cursor = conn.cursor()
            # 1. Insert losses
            for l in lost:
                cursor.execute("""
                INSERT OR IGNORE INTO losses (session_id, sensor, from_index, to_index, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """, (session_id, sensor, l["from_index"], l.get("to_index"), l["reason"], now))

            # 2. Insert samples
            if data:
                rows_to_insert = []
                for offset, vals in enumerate(data):
                    idx = first_index + offset
                    ch1 = vals[0]
                    ch2 = vals[1]
                    ch3 = vals[2] if len(vals) > 2 else None
                    rows_to_insert.append((session_id, sensor, idx, ch1, ch2, ch3, 0, now))

                cursor.executemany("""
                INSERT OR IGNORE INTO samples (session_id, sensor, sample_index, ch1, ch2, ch3, synced, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, rows_to_insert)
                inserted_samples = cursor.rowcount

                # Update checkpoint to latest sample
                last_idx = first_index + len(data) - 1
                cursor.execute("""
                INSERT INTO checkpoints (session_id, sensor, last_index, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id, sensor) DO UPDATE SET
                    last_index = MAX(last_index, excluded.last_index),
                    updated_at = excluded.updated_at
                """, (session_id, sensor, last_idx, now))

            conn.commit()

        return inserted_samples

    def record_loss(
        self,
        session_id: str,
        sensor: int,
        from_index: int,
        to_index: Optional[int],
        reason: str
    ):
        """Record an explicit loss range."""
        self.check_disk_space()
        now = time.time()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT OR IGNORE INTO losses (session_id, sensor, from_index, to_index, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """, (session_id, sensor, from_index, to_index, reason, now))
            conn.commit()

    def get_last_stored_index(self, session_id: str, sensor: int) -> Optional[int]:
        """Get the highest stored sample index for a sensor in a session."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT last_index FROM checkpoints WHERE session_id = ? AND sensor = ?
            """, (session_id, sensor))
            row = cursor.fetchone()
            if row:
                return row["last_index"]
            # Fallback to direct max
            cursor.execute("""
            SELECT MAX(sample_index) as m FROM samples WHERE session_id = ? AND sensor = ?
            """, (session_id, sensor))
            row = cursor.fetchone()
            return row["m"] if row and row["m"] is not None else None

    def get_counts(self, session_id: Optional[str] = None) -> Tuple[int, int, int]:
        """Returns (samples_saved, samples_in_cloud, backlog)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if session_id:
                cursor.execute("""
                SELECT 
                    COUNT(*) as total,
                    SUM(CASE WHEN synced = 1 THEN 1 ELSE 0 END) as synced_count
                FROM samples WHERE session_id = ?
                """, (session_id,))
            else:
                cursor.execute("""
                SELECT 
                    COUNT(*) as total,
                    SUM(CASE WHEN synced = 1 THEN 1 ELSE 0 END) as synced_count
                FROM samples
                """)
            row = cursor.fetchone()
            total = row["total"] or 0
            synced = row["synced_count"] or 0
            backlog = total - synced
            return total, synced, backlog

    def get_unsynced_samples(
        self,
        session_id: Optional[str] = None,
        limit: int = 1000
    ) -> List[Dict[str, Any]]:
        """Fetch unsynced samples for cloud streaming or catch-up sync."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if session_id:
                cursor.execute("""
                SELECT session_id, sensor, sample_index, ch1, ch2, ch3
                FROM samples
                WHERE session_id = ? AND synced = 0
                ORDER BY sensor, sample_index
                LIMIT ?
                """, (session_id, limit))
            else:
                cursor.execute("""
                SELECT session_id, sensor, sample_index, ch1, ch2, ch3
                FROM samples
                WHERE synced = 0
                ORDER BY session_id, sensor, sample_index
                LIMIT ?
                """, (limit,))
            rows = cursor.fetchall()
            out = []
            for r in rows:
                vals = [r["ch1"], r["ch2"]] if r["sensor"] == 2 else [r["ch1"], r["ch2"], r["ch3"]]
                out.append({
                    "session_id": r["session_id"],
                    "sensor": r["sensor"],
                    "index": r["sample_index"],
                    "values": vals
                })
            return out

    def get_unsynced_losses(
        self,
        session_id: Optional[str] = None,
        limit: int = 500
    ) -> List[Dict[str, Any]]:
        """Fetch unsynced loss records."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if session_id:
                cursor.execute("""
                SELECT id, session_id, sensor, from_index, to_index, reason
                FROM losses
                WHERE session_id = ? AND synced = 0
                ORDER BY id
                LIMIT ?
                """, (session_id, limit))
            else:
                cursor.execute("""
                SELECT id, session_id, sensor, from_index, to_index, reason
                FROM losses
                WHERE synced = 0
                ORDER BY id
                LIMIT ?
                """, (limit,))
            rows = cursor.fetchall()
            out = []
            for r in rows:
                out.append({
                    "id": r["id"],
                    "session_id": r["session_id"],
                    "sensor": r["sensor"],
                    "from_index": r["from_index"],
                    "to_index": r["to_index"],
                    "reason": r["reason"]
                })
            return out

    def mark_samples_synced(self, keys: List[Tuple[str, int, int]]):
        """Mark samples as durably synchronized to the cloud.
        keys is list of (session_id, sensor, sample_index).
        """
        if not keys:
            return
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.executemany("""
            UPDATE samples SET synced = 1
            WHERE session_id = ? AND sensor = ? AND sample_index = ?
            """, keys)
            conn.commit()

    def mark_losses_synced(self, loss_ids: List[int]):
        """Mark loss records as synced."""
        if not loss_ids:
            return
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.executemany("""
            UPDATE losses SET synced = 1 WHERE id = ?
            """, [(i,) for i in loss_ids])
            conn.commit()

    def get_window_samples(
        self,
        session_id: str,
        sensor: int,
        start_idx: int,
        end_idx: int
    ) -> Dict[int, List[int]]:
        """Fetch available samples for a specific window range [start_idx, end_idx]."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT sample_index, ch1, ch2, ch3
            FROM samples
            WHERE session_id = ? AND sensor = ? AND sample_index >= ? AND sample_index <= ?
            ORDER BY sample_index ASC
            """, (session_id, sensor, start_idx, end_idx))
            rows = cursor.fetchall()
            out = {}
            for r in rows:
                vals = [r["ch1"], r["ch2"]] if sensor == 2 else [r["ch1"], r["ch2"], r["ch3"]]
                out[r["sample_index"]] = vals
            return out

    def save_window_result(self, res: WindowResult):
        """Persist offline or online window result."""
        self.check_disk_space()
        now = time.time()
        payload = json.dumps(res.to_dict())
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT OR REPLACE INTO window_results (session_id, window, mode, payload, created_at)
            VALUES (?, ?, ?, ?, ?)
            """, (res.session_id, res.window, res.mode, payload, now))
            conn.commit()

    def stream_export(
        self,
        session_id: str
    ) -> Generator[str, None, None]:
        """Stream exported samples then loss records in JSONL format without loading full session in RAM."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # 1. Stream samples ordered deterministically
            cursor.execute("""
            SELECT sensor, sample_index, ch1, ch2, ch3
            FROM samples
            WHERE session_id = ?
            ORDER BY sensor, sample_index ASC
            """, (session_id,))
            for r in cursor:
                vals = [r["ch1"], r["ch2"]] if r["sensor"] == 2 else [r["ch1"], r["ch2"], r["ch3"]]
                record = {
                    "sensor": r["sensor"],
                    "index": r["sample_index"],
                    "values": vals
                }
                yield json.dumps(record) + "\n"

            # 2. Stream losses
            cursor.execute("""
            SELECT sensor, from_index, to_index, reason
            FROM losses
            WHERE session_id = ?
            ORDER BY sensor, from_index ASC
            """, (session_id,))
            for r in cursor:
                record = {
                    "sensor": r["sensor"],
                    "from_index": r["from_index"],
                    "to_index": r["to_index"],
                    "reason": r["reason"]
                }
                yield json.dumps(record) + "\n"

    def stream_results(
        self,
        session_id: str
    ) -> Generator[str, None, None]:
        """Stream window results in JSONL format."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT payload
            FROM window_results
            WHERE session_id = ?
            ORDER BY window ASC, mode ASC
            """, (session_id,))
            for r in cursor:
                yield r["payload"] + "\n"
