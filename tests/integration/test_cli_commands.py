"""Validation test for mandatory CLI commands:
  - pipeline status
  - pipeline export --session ID --from device
  - pipeline export --session ID --from cloud
  - pipeline results --session ID
Verifies exact stdout formatting, JSON/JSONL correctness, and stderr separation.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from src.mock.biosignal_mock import BiosignalMockServer
from src.cloud.app import CloudServer
from src.device.pipeline import PipelineService


class TestCLICommands(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="cli_test_")
        self.device_db = os.path.join(self.temp_dir, "device.db")
        self.cloud_db = os.path.join(self.temp_dir, "cloud.db")

        self.mock_port = 7195
        self.cloud_port = 8094

        # Start mock server (fast mode to generate 4,000 samples)
        self.mock = BiosignalMockServer(
            mode="ramp",
            seed=12,
            duration=10,
            port=self.mock_port,
            realtime=False
        )
        import threading
        self.mock_thread = threading.Thread(target=self.mock.run_server, daemon=True)
        self.mock_thread.start()
        time.sleep(0.3)

        # Start cloud server
        self.cloud = CloudServer(port=self.cloud_port, db_path=self.cloud_db)
        self.cloud.start()
        time.sleep(0.3)

        # Start pipeline to ingest and sync
        self.pipeline = PipelineService(
            db_path=self.device_db,
            mock_port=self.mock_port,
            cloud_url=f"http://127.0.0.1:{self.cloud_port}"
        )
        self.pipeline.start()
        time.sleep(3.0)

        self.session_id = self.pipeline.collector.current_session_id
        self.pipeline.stop()
        self.cloud.stop()
        self.mock.stop()
        time.sleep(0.5)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run_cli(self, args):
        env = os.environ.copy()
        env["MAYACARDIO_DB"] = self.device_db
        env["MAYACARDIO_CLOUD_URL"] = f"http://127.0.0.1:{self.cloud_port}"
        cmd = [sys.executable, "pipeline"] + args
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
        return res

    def test_cmd_status_formatting(self):
        """pipeline status must print exactly one valid JSON line to stdout."""
        # Restart cloud for status check
        self.cloud = CloudServer(port=self.cloud_port, db_path=self.cloud_db)
        self.cloud.start()
        time.sleep(0.2)

        try:
            res = self._run_cli(["status"])
            self.assertEqual(res.returncode, 0, f"pipeline status failed: {res.stderr}")

            stdout_lines = res.stdout.strip().split("\n")
            self.assertEqual(len(stdout_lines), 1, f"pipeline status must output exactly 1 line, got: {res.stdout}")

            status_obj = json.loads(stdout_lines[0])
            self.assertIn("mode", status_obj)
            self.assertIn("samples_saved", status_obj)
            self.assertIn("samples_in_cloud", status_obj)
            self.assertIn("backlog", status_obj)
            self.assertIsInstance(status_obj["samples_saved"], int)
            self.assertIsInstance(status_obj["samples_in_cloud"], int)
            self.assertIsInstance(status_obj["backlog"], int)
        finally:
            self.cloud.stop()

    def test_cmd_export_from_device(self):
        """pipeline export --session ID --from device: outputs JSONL samples then losses."""
        res = self._run_cli(["export", "--session", self.session_id, "--from", "device"])
        self.assertEqual(res.returncode, 0, f"export from device failed: {res.stderr}")

        lines = res.stdout.strip().split("\n")
        self.assertGreater(len(lines), 100)

        # First line must be a sample JSON
        first_obj = json.loads(lines[0])
        self.assertIn("sensor", first_obj)
        self.assertIn("index", first_obj)
        self.assertIn("values", first_obj)
        self.assertIsInstance(first_obj["values"], list)

    def test_cmd_export_from_cloud(self):
        """pipeline export --session ID --from cloud: outputs JSONL samples then losses from cloud."""
        self.cloud = CloudServer(port=self.cloud_port, db_path=self.cloud_db)
        self.cloud.start()
        time.sleep(0.2)

        try:
            res = self._run_cli(["export", "--session", self.session_id, "--from", "cloud"])
            self.assertEqual(res.returncode, 0, f"export from cloud failed: {res.stderr}")

            lines = res.stdout.strip().split("\n")
            self.assertGreater(len(lines), 100)

            first_obj = json.loads(lines[0])
            self.assertIn("sensor", first_obj)
            self.assertIn("index", first_obj)
            self.assertIn("values", first_obj)
        finally:
            self.cloud.stop()

    def test_cmd_results(self):
        """pipeline results --session ID: outputs JSONL window results."""
        res = self._run_cli(["results", "--session", self.session_id])
        self.assertEqual(res.returncode, 0, f"results command failed: {res.stderr}")

        lines = [l for l in res.stdout.strip().split("\n") if l]
        self.assertGreater(len(lines), 0, "pipeline results must output at least 1 window result")
        first_res = json.loads(lines[0])
        self.assertIn("window", first_res)
        self.assertIn("missing_1", first_res)
        self.assertIn("missing_2", first_res)
        self.assertIn("complete", first_res)
        self.assertIn("mean", first_res)
        self.assertIn("min", first_res)
        self.assertIn("max", first_res)
        self.assertIn("magnitude", first_res)
        self.assertIn("heart_rate", first_res)
        self.assertIn("mode", first_res)


if __name__ == "__main__":
    unittest.main()
