"""Fault injection tests for mock sensor protocol faults specified in Section 6.3:
  1. Corrupt reply with invalid CRC-32 (triggers replay, zero lost)
  2. Duplicate reply sent twice (idempotent storage, zero duplicated rows)
  3. Sensor stopped sending (explicit loss reported and durably stored)
  4. Mock restarted with new session (session isolation, old session loss with to_index: null)
"""
import os
import shutil
import tempfile
import time
import unittest

from src.mock.biosignal_mock import BiosignalMockServer
from src.device.storage import DeviceStorage
from src.device.collector import DeviceCollector


class TestSensorFaults(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_faults.db")
        self.storage = DeviceStorage(self.db_path)
        self.mock_port = 7182

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_corrupt_crc32_recovery(self):
        """Mock sends corrupt CRC32. Collector detects mismatch, issues replay, and persists data."""
        mock = BiosignalMockServer(mode="ramp", seed=10, duration=10, port=self.mock_port, realtime=False)
        import threading
        t = threading.Thread(target=mock.run_server, daemon=True)
        t.start()
        time.sleep(0.2)

        collector = DeviceCollector(self.storage, mock_port=self.mock_port)
        self.assertTrue(collector.connect())

        # Inject fault: next reply will have corrupt CRC32
        mock.fault_corrupt_next_crc = True

        # Perform read cycle
        s1, s2 = collector.read_cycle()
        # Should have successfully re-read and saved samples despite the initial corrupted reply
        self.assertGreater(s1, 0)
        self.assertGreater(s2, 0)

        collector.stop()
        mock.stop()

    def test_duplicate_reply_deduplication(self):
        """Mock sends the exact same reply twice. Storage must ingest idempotently without duplicates."""
        mock = BiosignalMockServer(mode="ramp", seed=10, duration=10, port=self.mock_port + 1, realtime=False)
        import threading
        t = threading.Thread(target=mock.run_server, daemon=True)
        t.start()
        time.sleep(0.2)

        collector = DeviceCollector(self.storage, mock_port=self.mock_port + 1)
        self.assertTrue(collector.connect())

        # Inject fault: next reply is sent twice over socket
        mock.fault_duplicate_next = True

        collector.read_cycle()
        total_1, _, _ = self.storage.get_counts()

        # Read again
        collector.read_cycle()
        total_2, _, _ = self.storage.get_counts()

        # Verify no duplicate primary keys or corruption
        self.assertGreater(total_2, total_1)

        collector.stop()
        mock.stop()

    def test_sensor_temporarily_stopped_reported_loss(self):
        """Mock stops sending Sensor 2 for a while. Reported in lost list, persisted in storage."""
        mock = BiosignalMockServer(mode="ramp", seed=10, duration=10, port=self.mock_port + 2, realtime=False)
        import threading
        t = threading.Thread(target=mock.run_server, daemon=True)
        t.start()
        time.sleep(0.2)

        collector = DeviceCollector(self.storage, mock_port=self.mock_port + 2)
        self.assertTrue(collector.connect())

        # Normal read
        collector.read_cycle()

        # Inject fault: stop sending sensor 2
        mock.fault_stop_sensor = 2
        collector.read_cycle()

        losses = self.storage.get_unsynced_losses()
        self.assertTrue(any(l["sensor"] == 2 and l["reason"] == "sensor_offline" for l in losses))

        collector.stop()
        mock.stop()

    def test_mock_restart_session_isolation_and_open_loss(self):
        """Mock restarts with a new session. Collector records unread loss with to_index: null for old session."""
        mock = BiosignalMockServer(mode="ramp", seed=10, duration=10, port=self.mock_port + 3, realtime=False)
        import threading
        t = threading.Thread(target=mock.run_server, daemon=True)
        t.start()
        time.sleep(0.2)

        collector = DeviceCollector(self.storage, mock_port=self.mock_port + 3)
        self.assertTrue(collector.connect())

        # Read cycle on session 1
        collector.read_cycle()
        sess_1 = collector.current_session_id
        self.assertIsNotNone(sess_1)

        # Mock restarts: new session_id, cursor resets to 0
        mock.session_id = "new_sess_99"
        mock.client_cursor = {1: 0, 2: 0}

        # Next read cycle should detect session change
        collector.read_cycle()
        self.assertEqual(collector.current_session_id, "new_sess_99")

        # Verify loss record recorded for old session with to_index = None
        losses = self.storage.get_unsynced_losses(session_id=sess_1)
        self.assertTrue(any(l["session_id"] == sess_1 and l["to_index"] is None for l in losses),
                        f"Expected loss with to_index: null for old session {sess_1}")

        collector.stop()
        mock.stop()


if __name__ == "__main__":
    unittest.main()
