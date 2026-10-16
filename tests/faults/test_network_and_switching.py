"""Fault injection test: network drop and restore during active data capture.
Validates Criterion 5 (< 10s mode switching) and Criterion 6 (complete cloud backlog catch-up without loss/duplication).
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


class TestNetworkAndSwitching(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.device_db = os.path.join(self.temp_dir, "dev_net.db")
        self.cloud_db = os.path.join(self.temp_dir, "cloud_net.db")

        self.mock_port = 7185
        self.cloud_port = 8092

        # Start mock server
        self.mock = BiosignalMockServer(mode="ramp", seed=100, duration=30, port=self.mock_port, realtime=False)
        import threading
        self.mock_thread = threading.Thread(target=self.mock.run_server, daemon=True)
        self.mock_thread.start()
        time.sleep(0.2)

        # Start cloud server
        self.cloud = CloudServer(port=self.cloud_port, db_path=self.cloud_db)
        self.cloud.start()
        time.sleep(0.2)

        # Start pipeline
        self.pipeline = PipelineService(
            db_path=self.device_db,
            mock_port=self.mock_port,
            cloud_url=f"http://127.0.0.1:{self.cloud_port}"
        )
        self.pipeline.start()

    def tearDown(self):
        self.pipeline.stop()
        self.cloud.stop()
        self.mock.stop()
        time.sleep(0.3)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_network_cut_and_restoration_cycle(self):
        """1. Pipeline starts online.
        2. Network is cut (cloud server stopped).
        3. Verify switch to offline happens within 10 seconds.
        4. Device collects in offline mode, computes offline results.
        5. Network is restored (cloud server started).
        6. Verify switch back to online happens within 10 seconds.
        7. Catch-up sync completes.
        8. Ground truth verification confirms zero loss and zero duplicates.
        """
        # Step 1: Let pipeline establish online mode and collect
        time.sleep(2.0)
        status_online = self.pipeline.get_status()
        self.assertEqual(status_online.mode, "online")

        # Step 2: Cut network (stop cloud server)
        t_cut = time.monotonic()
        self.cloud.stop()

        # Step 3: Verify switch to offline occurs within 10 seconds
        switched_offline = False
        while time.monotonic() - t_cut < 10.0:
            if self.pipeline.get_status().mode == "offline":
                switched_offline = True
                break
            time.sleep(0.3)

        offline_latency = time.monotonic() - t_cut
        self.assertTrue(switched_offline, f"Failed to switch to offline within 10s (latency: {offline_latency:.2f}s)")

        # Step 4: Keep collecting in offline mode
        time.sleep(2.0)
        status_offline = self.pipeline.get_status()
        self.assertEqual(status_offline.mode, "offline")
        self.assertGreater(status_offline.backlog, 0, "Backlog must accumulate while offline")

        # Step 5: Restore network (restart cloud server)
        t_restore = time.monotonic()
        self.cloud = CloudServer(port=self.cloud_port, db_path=self.cloud_db)
        self.cloud.start()

        # Step 6: Verify switch to online occurs within 10 seconds
        switched_online = False
        while time.monotonic() - t_restore < 10.0:
            if self.pipeline.get_status().mode == "online":
                switched_online = True
                break
            time.sleep(0.3)

        online_latency = time.monotonic() - t_restore
        self.assertTrue(switched_online, f"Failed to switch to online within 10s (latency: {online_latency:.2f}s)")

        # Step 7: Wait for catch-up synchronization
        t_catchup_start = time.monotonic()
        while time.monotonic() - t_catchup_start < 10.0:
            st = self.pipeline.get_status()
            if st.backlog == 0:
                break
            time.sleep(0.3)

        final_status = self.pipeline.get_status()
        self.assertEqual(final_status.backlog, 0, f"Expected backlog 0 after catch-up, got {final_status.backlog}")

        # Step 8: Validate cloud export against ground truth
        session_id = self.pipeline.collector.current_session_id
        cloud_export_file = os.path.join(self.temp_dir, "cloud_catchup_export.jsonl")
        with open(cloud_export_file, "w", encoding="utf-8") as f:
            for line in self.cloud.storage.stream_export(session_id):
                f.write(line)

        checker = GroundTruthChecker(mode="ramp", seed=100)
        res = checker.verify_export_file(cloud_export_file)
        self.assertTrue(res["passed"], f"Cloud export failed ground truth verification: {res}")
        self.assertEqual(res["duplicates"], 0)
        self.assertEqual(res["silent_losses"], 0)


if __name__ == "__main__":
    unittest.main()
