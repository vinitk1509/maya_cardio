"""Cloud-side durable persistence for samples, losses, and online window results."""
import json
import os
import sqlite3
import time
from typing import Any, Dict, Generator, List, Optional, Tuple

from src.common.logging import setup_logging
from src.common.models import WindowResult

logger = setup_logging("cloud-storage")


class CloudStorage:
    """Manages cloud SQLite persistence with strict idempotency and ACID guarantees."""

    def __init__(self, db_path: str = "cloud_data.db"):
        self.db_path = db_path
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode = WAL;")
        cursor.execute("PRAGMA synchronous = NORMAL;")
        cursor.execute("PRAGMA busy_timeout = 10000;")
        cursor.close()
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS cloud_samples (
                session_id TEXT NOT NULL,
                sensor INTEGER NOT NULL,
                sample_index INTEGER NOT NULL,
                ch1 INTEGER NOT NULL,
                ch2 INTEGER NOT NULL,
                ch3 INTEGER,
                created_at REAL NOT NULL,
                PRIMARY KEY (session_id, sensor, sample_index)
            );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_cloud_samples_win ON cloud_samples (session_id, sensor, sample_index);")

            cursor.execute("""
            CREATE TABLE IF NOT EXISTS cloud_losses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                sensor INTEGER NOT NULL,
                from_index INTEGER NOT NULL,
                to_index INTEGER,
                reason TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE (session_id, sensor, from_index, to_index, reason)
            );
            """)

            cursor.execute("""
            CREATE TABLE IF NOT EXISTS cloud_window_results (
                session_id TEXT NOT NULL,
                window INTEGER NOT NULL,
                mode TEXT NOT NULL,
                payload TEXT NOT NULL,
                computed_at REAL NOT NULL,
                PRIMARY KEY (session_id, window, mode)
            );
            """)
            conn.commit()

    def ingest_batch(
        self,
        samples: List[Dict[str, Any]],
        losses: List[Dict[str, Any]]
    ) -> Tuple[int, int]:
        """Atomically ingest batches of samples and losses.
        
        Idempotent: duplicated samples or loss records are safely ignored.
        Returns (samples_inserted, losses_inserted).
        """
        now = time.time()
        samples_inserted = 0
        losses_inserted = 0

        with self._get_connection() as conn:
            cursor = conn.cursor()
            if samples:
                rows = []
                for s in samples:
                    vals = s["values"]
                    ch1 = vals[0]
                    ch2 = vals[1]
                    ch3 = vals[2] if len(vals) > 2 else None
                    rows.append((s["session_id"], s["sensor"], s["index"], ch1, ch2, ch3, now))

                cursor.executemany("""
                INSERT OR IGNORE INTO cloud_samples (session_id, sensor, sample_index, ch1, ch2, ch3, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """, rows)
                samples_inserted = cursor.rowcount

            if losses:
                loss_rows = []
                for l in losses:
                    loss_rows.append((
                        l["session_id"],
                        l["sensor"],
                        l["from_index"],
                        l.get("to_index"),
                        l["reason"],
                        now
                    ))
                cursor.executemany("""
                INSERT OR IGNORE INTO cloud_losses (session_id, sensor, from_index, to_index, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """, loss_rows)
                losses_inserted = cursor.rowcount

            conn.commit()

        return samples_inserted, losses_inserted

    def get_session_stats(self, session_id: Optional[str] = None) -> Dict[str, Any]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if session_id:
                cursor.execute("""
                SELECT COUNT(*) as cnt FROM cloud_samples WHERE session_id = ?
                """, (session_id,))
                samples_count = cursor.fetchone()["cnt"]

                cursor.execute("""
                SELECT COUNT(*) as cnt FROM cloud_losses WHERE session_id = ?
                """, (session_id,))
                losses_count = cursor.fetchone()["cnt"]
            else:
                cursor.execute("SELECT COUNT(*) as cnt FROM cloud_samples")
                samples_count = cursor.fetchone()["cnt"]

                cursor.execute("SELECT COUNT(*) as cnt FROM cloud_losses")
                losses_count = cursor.fetchone()["cnt"]

            return {
                "samples_in_cloud": samples_count,
                "losses_in_cloud": losses_count
            }

    def get_window_samples(
        self,
        session_id: str,
        sensor: int,
        start_idx: int,
        end_idx: int
    ) -> Dict[int, List[int]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT sample_index, ch1, ch2, ch3
            FROM cloud_samples
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
        now = time.time()
        payload = json.dumps(res.to_dict())
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            INSERT OR REPLACE INTO cloud_window_results (session_id, window, mode, payload, computed_at)
            VALUES (?, ?, ?, ?, ?)
            """, (res.session_id, res.window, res.mode, payload, now))
            conn.commit()

    def get_max_sample_index(self, session_id: str) -> Optional[int]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT MAX(sample_index) as m FROM cloud_samples WHERE session_id = ?
            """, (session_id,))
            row = cursor.fetchone()
            return row["m"] if row and row["m"] is not None else None

    def stream_export(self, session_id: str) -> Generator[str, None, None]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT sensor, sample_index, ch1, ch2, ch3
            FROM cloud_samples
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

            cursor.execute("""
            SELECT sensor, from_index, to_index, reason
            FROM cloud_losses
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

    def stream_results(self, session_id: str) -> Generator[str, None, None]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT payload
            FROM cloud_window_results
            WHERE session_id = ?
            ORDER BY window ASC, mode ASC
            """, (session_id,))
            for r in cursor:
                yield r["payload"] + "\n"
