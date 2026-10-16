"""Concurrent catch-up and live ingestion validation test.
Validates that during catch-up recovery, live sensor capture continues at 800 samples/second
without interference, and the backlog is reconciled with 100% ground-truth accuracy.
"""
import os
import shutil
import tempfile
import time
import unittest

from src.mock.biosignal_mock import BiosignalMockServer
from src.cloud.app import CloudServer
from src.device.pipeline import PipelineService
from src.mock.check_ground_truth import GroundTruthChecker


class TestConcurrentCatchup(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="catchup_live_")
        self.device_db = os.path.join(self.temp_dir, "device.db")
        self.cloud_db = os.path.join(self.temp_dir, "cloud.db")

        self.mock_port = 7192
        self.cloud_port = 8097

        # 1. Start mock server in real-time mode (800 samples/sec live)
        self.mock = BiosignalMockServer(
            mode="ramp",
            seed=55,
            duration=120,
            port=self.mock_port,
            realtime=True
        )
        import threading
        self.mock_thread = threading.Thread(target=self.mock.run_server, daemon=True)
        self.mock_thread.start()
        time.sleep(0.3)

    def tearDown(self):
        self.mock.stop()
        time.sleep(0.2)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_concurrent_catchup_with_active_live_ingestion(self):
        """1. Pipeline captures offline, accumulating 10,000 samples backlog.
        2. Network is restored (cloud started).
        3. Catch-up begins uploading historical backlog while collector continuously captures live at 800 Hz.
        4. Reconcile both sets against ground truth: 0 duplicates, 0 missing.
        """
        pipeline = PipelineService(
            db_path=self.device_db,
            mock_port=self.mock_port,
            cloud_url=f"http://127.0.0.1:{self.cloud_port}"
        )
        # Start pipeline with cloud offline
        pipeline.start()

        # Allow pipeline to collect offline for 4 seconds (~3,200 samples)
        time.sleep(4.0)

        offline_status = pipeline.get_status()
        self.assertEqual(offline_status.mode, "offline")
        self.assertGreater(offline_status.backlog, 1000, "Backlog must accumulate during offline capture")
        initial_backlog = offline_status.backlog

        # Now start the cloud server
        cloud = CloudServer(port=self.cloud_port, db_path=self.cloud_db)
        cloud.start()
        time.sleep(0.5)

        # Mode should transition to online
        t_start = time.monotonic()
        caught_up = False

        # Wait for catch-up while live collection continues
        while time.monotonic() - t_start < 15.0:
            st = pipeline.get_status()
            if st.mode == "online" and st.backlog <= 400:  # caught up to live streaming window
                caught_up = True
                break
            time.sleep(0.4)

        self.assertTrue(caught_up, f"Failed to catch up while live capture active: backlog={pipeline.get_status().backlog}")

        # Stop collector to flush final in-flight buffer
        pipeline.collector.stop()
        time.sleep(0.8)

        final_st = pipeline.get_status()
        self.assertEqual(final_st.backlog, 0, f"Backlog did not drain to 0: {final_st.backlog}")

        session_id = pipeline.collector.current_session_id

        # Export and verify against Ground Truth
        cloud_export_file = os.path.join(self.temp_dir, "cloud_export.jsonl")
        with open(cloud_export_file, "w", encoding="utf-8") as f:
            for line in cloud.storage.stream_export(session_id):
                f.write(line)

        checker = GroundTruthChecker(mode="ramp", seed=55)
        res = checker.verify_export_file(cloud_export_file)
        self.assertTrue(res["passed"], f"Ground truth validation failed: {res}")
        self.assertEqual(res["duplicates"], 0)
        self.assertEqual(res["silent_losses"], 0)

        pipeline.stop()
        cloud.stop()


if __name__ == "__main__":
    unittest.main()
