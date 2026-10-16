"""Benchmark catch-up synchronization throughput.
Validates Criterion 6: Catch-up rate must exceed 6,400 samples/second.
"""
import os
import shutil
import tempfile
import time
import unittest

from src.cloud.app import CloudServer
from src.device.storage import DeviceStorage
from src.device.sync import CloudSyncWorker

TARGET_SAMPLES = 50000  # 50k samples benchmark
MIN_REQUIRED_RATE = 6400  # samples per second


class TestCatchupBenchmark(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.dev_db = os.path.join(self.temp_dir, "bench_dev.db")
        self.cloud_db = os.path.join(self.temp_dir, "bench_cloud.db")
        self.cloud_port = 8095

        self.cloud = CloudServer(port=self.cloud_port, db_path=self.cloud_db)
        self.cloud.start()
        time.sleep(0.3)

        self.storage = DeviceStorage(self.dev_db)
        self.sync_worker = CloudSyncWorker(
            self.storage,
            cloud_url=f"http://127.0.0.1:{self.cloud_port}",
            poll_interval=0.01
        )

    def tearDown(self):
        self.sync_worker.stop()
        self.cloud.stop()
        time.sleep(0.2)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_high_throughput_catchup(self):
        """Pre-populate device storage with 50,000 samples while disconnected,
        then measure catch-up sync rate to cloud.
        """
        # Populate device storage in large batch
        session_id = "bench_session_01"
        now = time.time()
        print(f"Pre-populating {TARGET_SAMPLES} samples into local storage...")

        # Half for sensor 1 (3 channels), half for sensor 2 (2 channels)
        s1_data = [[100 + (i % 500), 200 + (i % 500), 300 + (i % 500)] for i in range(TARGET_SAMPLES // 2)]
        s2_data = [[400000 + (i % 1000), 150000 + (i % 1000)] for i in range(TARGET_SAMPLES // 2)]

        self.storage.save_samples_and_losses(session_id, 1, 0, s1_data, [])
        self.storage.save_samples_and_losses(session_id, 2, 0, s2_data, [])

        total, synced, backlog = self.storage.get_counts(session_id)
        self.assertEqual(total, TARGET_SAMPLES)
        self.assertEqual(backlog, TARGET_SAMPLES)

        # Start catch-up sync
        print("Starting catch-up synchronization...")
        self.sync_worker.is_online = True
        t_start = time.monotonic()

        while True:
            n_synced = self.sync_worker.sync_cycle()
            if n_synced == 0:
                break

        t_end = time.monotonic()
        elapsed = max(t_end - t_start, 0.001)
        rate = TARGET_SAMPLES / elapsed

        print(f"Catch-up completed: {TARGET_SAMPLES} samples in {elapsed:.3f}s -> {rate:.0f} samples/second")

        # Verify cloud has all samples
        cloud_stats = self.cloud.storage.get_session_stats(session_id)
        self.assertEqual(cloud_stats["samples_in_cloud"], TARGET_SAMPLES)

        # Check backlog on device is 0
        _, _, final_backlog = self.storage.get_counts(session_id)
        self.assertEqual(final_backlog, 0)

        # Assert throughput requirement is met
        self.assertGreater(
            rate,
            MIN_REQUIRED_RATE,
            f"Catch-up rate {rate:.0f} samples/s failed to meet minimum requirement of {MIN_REQUIRED_RATE} samples/s"
        )


if __name__ == "__main__":
    unittest.main()
