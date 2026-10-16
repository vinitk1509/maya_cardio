"""End-to-end integration test: mock -> device collector -> storage -> sync -> cloud server.
Validates Criteria 1, 4, 5, 6, 7 using GroundTruthChecker.
"""
import json
import os
import shutil
import tempfile
import time
import unittest

from src.mock.biosignal_mock import BiosignalMockServer
from src.cloud.app import CloudServer
from src.device.pipeline import PipelineService
from src.mock.check_ground_truth import GroundTruthChecker


class TestEndToEndPipeline(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.device_db = os.path.join(self.temp_dir, "device.db")
        self.cloud_db = os.path.join(self.temp_dir, "cloud.db")

        self.mock_port = 7180
        self.cloud_port = 8089

        # 1. Start mock server (fast mode for testing)
        self.mock = BiosignalMockServer(
            mode="ramp",
            seed=42,
            duration=30,
            port=self.mock_port,
            realtime=False
        )
        self.mock_thread = None
        import threading
        self.mock_thread = threading.Thread(target=self.mock.run_server, daemon=True)
        self.mock_thread.start()
        time.sleep(0.3)

        # 2. Start cloud server
        self.cloud = CloudServer(port=self.cloud_port, db_path=self.cloud_db)
        self.cloud.start()
        time.sleep(0.3)

        # 3. Start pipeline service
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
        time.sleep(0.5)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_pipeline_streaming_and_ground_truth(self):
        """Run pipeline for several seconds, then verify exports against ground truth."""
        # Allow pipeline to collect and sync samples
        time.sleep(4.0)

        # Check status
        status = self.pipeline.get_status()
        self.assertEqual(status.mode, "online")
        self.assertGreater(status.samples_saved, 100)

        # Wait briefly for sync to catch up
        time.sleep(1.5)

        session_id = self.pipeline.collector.current_session_id
        self.assertIsNotNone(session_id)

        # Export from device
        device_export_file = os.path.join(self.temp_dir, "device_export.jsonl")
        with open(device_export_file, "w", encoding="utf-8") as f:
            for line in self.pipeline.storage.stream_export(session_id):
                f.write(line)

        # Export from cloud
        cloud_export_file = os.path.join(self.temp_dir, "cloud_export.jsonl")
        with open(cloud_export_file, "w", encoding="utf-8") as f:
            for line in self.cloud.storage.stream_export(session_id):
                f.write(line)

        # Validate device export against Ground Truth
        checker = GroundTruthChecker(mode="ramp", seed=42)
        dev_res = checker.verify_export_file(device_export_file)
        self.assertTrue(dev_res["passed"], f"Device export failed ground truth: {dev_res}")
        self.assertEqual(dev_res["duplicates"], 0)
        self.assertEqual(dev_res["value_mismatches"], 0)
        self.assertEqual(dev_res["silent_losses"], 0)

        # Validate cloud export against Ground Truth
        cloud_res = checker.verify_export_file(cloud_export_file)
        self.assertTrue(cloud_res["passed"], f"Cloud export failed ground truth: {cloud_res}")
        self.assertEqual(cloud_res["duplicates"], 0)
        self.assertEqual(cloud_res["value_mismatches"], 0)
        self.assertEqual(cloud_res["silent_losses"], 0)

        # Verify cloud results exist and follow Section 3 format
        cloud_results = list(self.cloud.storage.stream_results(session_id))
        self.assertGreater(len(cloud_results), 0)
        first_res = json.loads(cloud_results[0])
        self.assertIn("window", first_res)
        self.assertIn("missing_1", first_res)
        self.assertIn("missing_2", first_res)
        self.assertIn("complete", first_res)
        self.assertIn("mean", first_res)
        self.assertIn("min", first_res)
        self.assertIn("max", first_res)
        self.assertIn("magnitude", first_res)
        self.assertEqual(first_res["mode"], "online")


if __name__ == "__main__":
    unittest.main()
