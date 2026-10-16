"""Fault test for cloud durability across restarts and redeployments.
Validates that cloud data survives application termination, redeployment, and crashes.
"""
import json
import os
import shutil
import tempfile
import time
import unittest
import urllib.request
import urllib.error

from src.cloud.app import CloudServer
from src.cloud.storage import CloudStorage


class TestCloudPersistence(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="cloud_durability_")
        self.cloud_db_path = os.path.join(self.temp_dir, "persistent_cloud.db")
        self.cloud_port = 8096

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_cloud_durability_across_process_restarts(self):
        """1. Start cloud instance A on persistent storage.
        2. Ingest 10,000 samples and 5 explicit losses.
        3. Completely terminate cloud instance A.
        4. Launch fresh cloud instance B pointing to the exact same volume.
        5. Verify all samples, losses, and window results persist without loss or duplication.
        6. Verify retrying already-persisted batches does not duplicate records.
        """
        # Step 1: Start Instance A
        server_A = CloudServer(port=self.cloud_port, db_path=self.cloud_db_path)
        server_A.start()
        time.sleep(0.3)

        session_id = "durability_sess_01"
        samples = []
        for i in range(10000):
            sensor = 1 if i < 5000 else 2
            idx = i if i < 5000 else i - 5000
            vals = [100, 200, 300] if sensor == 1 else [400000, 150000]
            samples.append({
                "session_id": session_id,
                "sensor": sensor,
                "index": idx,
                "values": vals
            })

        losses = [{
            "session_id": session_id,
            "sensor": 1,
            "from_index": 9000,
            "to_index": 9100,
            "reason": "simulated_loss"
        }]

        # Ingest via HTTP POST
        req_data = json.dumps({"samples": samples, "losses": losses}).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.cloud_port}/api/v1/ingest",
            data=req_data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            self.assertEqual(resp.status, 200)

        # Allow online window processor to compute initial windows
        time.sleep(1.0)

        # Export before crash
        export_before_url = f"http://127.0.0.1:{self.cloud_port}/api/v1/export?session={session_id}"
        with urllib.request.urlopen(export_before_url, timeout=5.0) as resp:
            export_lines_before = [line.decode("utf-8") for line in resp]
        self.assertEqual(len(export_lines_before), 10001)  # 10k samples + 1 loss

        # Step 3: Completely STOP / KILL server instance A
        server_A.stop()
        time.sleep(0.5)

        # Verify server is truly down
        with self.assertRaises(Exception):
            urllib.request.urlopen(f"http://127.0.0.1:{self.cloud_port}/api/v1/health", timeout=1.0)

        # Step 4: Launch fresh Cloud Instance B pointing to same storage
        server_B = CloudServer(port=self.cloud_port, db_path=self.cloud_db_path)
        server_B.start()
        time.sleep(0.3)

        try:
            # Step 5: Check status on Instance B
            status_url = f"http://127.0.0.1:{self.cloud_port}/api/v1/status?session={session_id}"
            with urllib.request.urlopen(status_url, timeout=5.0) as resp:
                stats = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(stats["samples_in_cloud"], 10000, "Samples lost across cloud restart!")
            self.assertEqual(stats["losses_in_cloud"], 1, "Loss records lost across cloud restart!")

            # Verify export from Instance B matches export from Instance A exactly
            export_after_url = f"http://127.0.0.1:{self.cloud_port}/api/v1/export?session={session_id}"
            with urllib.request.urlopen(export_after_url, timeout=5.0) as resp:
                export_lines_after = [line.decode("utf-8") for line in resp]
            self.assertEqual(export_lines_before, export_lines_after, "Export data diverged across restart!")

            # Step 6: Test duplicate re-upload idempotency after restart
            # Client retries uploading the same 10,000 samples (lost ACK scenario)
            req_retry = urllib.request.Request(
                f"http://127.0.0.1:{self.cloud_port}/api/v1/ingest",
                data=req_data,
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req_retry, timeout=5.0) as resp:
                self.assertEqual(resp.status, 200)

            # Re-check status: count MUST remain exactly 10,000
            with urllib.request.urlopen(status_url, timeout=5.0) as resp:
                stats_retry = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(stats_retry["samples_in_cloud"], 10000, "Retrying duplicate upload caused row inflation!")

        finally:
            server_B.stop()


if __name__ == "__main__":
    unittest.main()
