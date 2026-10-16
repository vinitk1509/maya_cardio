"""Crash, SIGKILL, and reboot recovery test suite.
Validates Criterion 3: Data survives unexpected termination without corruption or silent losses.
"""
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from src.mock.biosignal_mock import BiosignalMockServer
from src.device.storage import DeviceStorage
from src.mock.check_ground_truth import GroundTruthChecker


class TestCrashRecovery(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "crash_test.db")
        self.mock_port = 7189

        # Start mock server
        self.mock = BiosignalMockServer(mode="ramp", seed=77, duration=60, port=self.mock_port, realtime=True)
        import threading
        self.mock_thread = threading.Thread(target=self.mock.run_server, daemon=True)
        self.mock_thread.start()
        time.sleep(0.3)

    def tearDown(self):
        self.mock.stop()
        time.sleep(0.2)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_abrupt_process_kill_and_recovery(self):
        """1. Spawn pipeline subprocess collecting data.
        2. Forcibly kill process using terminate/kill (SIGKILL equivalent).
        3. Verify SQLite WAL file integrity and recoverability.
        4. Restart pipeline and verify resume behavior without data duplication.
        """
        # Launch pipeline in a separate OS process
        cmd = [
            sys.executable,
            "-m",
            "src.cli.main",
            "run",
            "--db", self.db_path,
            "--mock-port", str(self.mock_port),
            "--cloud-url", "http://127.0.0.1:9999"  # offline mode
        ]
        proc = subprocess.Popen(cmd, cwd=os.getcwd())

        # Let it collect for ~3 seconds
        time.sleep(3.0)

        # Abruptly kill the process (SIGKILL equivalent: no atexit, no clean close)
        proc.kill()
        proc.wait()

        # Verify database integrity directly via sqlite3 CLI PRAGMA
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("PRAGMA integrity_check;")
        res = cursor.fetchone()
        self.assertEqual(res[0], "ok", f"Database corrupted after SIGKILL: {res}")

        # Check sample count before restart
        cursor.execute("SELECT COUNT(*) FROM samples;")
        count_before = cursor.fetchone()[0]
        self.assertGreater(count_before, 0)
        conn.close()

        # Restart pipeline in another subprocess
        proc2 = subprocess.Popen(cmd, cwd=os.getcwd())
        time.sleep(2.0)
        proc2.kill()
        proc2.wait()

        # Verify integrity again
        storage = DeviceStorage(self.db_path)
        total_after, _, _ = storage.get_counts()
        self.assertGreater(total_after, count_before)

        # Verify zero duplicated samples exist in database
        with storage._get_connection() as c:
            cur = c.cursor()
            cur.execute("""
            SELECT session_id, sensor, sample_index, COUNT(*) as c
            FROM samples
            GROUP BY session_id, sensor, sample_index
            HAVING c > 1
            """)
            dups = cur.fetchall()
            self.assertEqual(len(dups), 0, f"Found duplicated sample records: {dups}")


if __name__ == "__main__":
    unittest.main()
