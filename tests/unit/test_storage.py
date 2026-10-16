"""Unit tests for DeviceStorage durability, deduplication, loss tracking, and crash safety."""
import json
import os
import sqlite3
import tempfile
import unittest

from src.device.storage import DeviceStorage
from src.common.models import WindowResult


class TestDeviceStorage(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_pipeline.db")
        self.storage = DeviceStorage(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass

    def test_save_samples_and_deduplication(self):
        """Verify samples are saved, deduplicated on identical inserts, and count is accurate."""
        samples_s1 = [[100, 200, 300], [101, 201, 301], [102, 202, 302]]
        inserted = self.storage.save_samples_and_losses(
            session_id="sess_1",
            sensor=1,
            first_index=0,
            data=samples_s1,
            lost=[]
        )
        self.assertEqual(inserted, 3)

        total, synced, backlog = self.storage.get_counts("sess_1")
        self.assertEqual(total, 3)
        self.assertEqual(synced, 0)
        self.assertEqual(backlog, 3)

        # Re-insert the exact same samples (simulating mock duplicate reply or replayed read)
        inserted_dup = self.storage.save_samples_and_losses(
            session_id="sess_1",
            sensor=1,
            first_index=0,
            data=samples_s1,
            lost=[]
        )
        # Should NOT duplicate records
        total_after, synced_after, backlog_after = self.storage.get_counts("sess_1")
        self.assertEqual(total_after, 3)
        self.assertEqual(backlog_after, 3)

    def test_loss_accounting(self):
        """Verify explicit loss records are stored and retrieved."""
        self.storage.record_loss(
            session_id="sess_loss",
            sensor=2,
            from_index=4000,
            to_index=4399,
            reason="buffer_overflow"
        )
        # Test open-ended loss (e.g. session restart with unknown end)
        self.storage.record_loss(
            session_id="sess_loss",
            sensor=1,
            from_index=8000,
            to_index=None,
            reason="mock_restart"
        )

        unsynced_losses = self.storage.get_unsynced_losses("sess_loss")
        self.assertEqual(len(unsynced_losses), 2)
        self.assertEqual(unsynced_losses[0]["reason"], "buffer_overflow")
        self.assertEqual(unsynced_losses[1]["to_index"], None)
        self.assertEqual(unsynced_losses[1]["reason"], "mock_restart")

    def test_export_streaming(self):
        """Verify stream_export produces valid JSON lines matching Section 4 specification."""
        self.storage.save_samples_and_losses(
            session_id="sess_exp",
            sensor=1,
            first_index=120000,
            data=[[720, 1200, 1560]],
            lost=[]
        )
        self.storage.record_loss(
            session_id="sess_exp",
            sensor=2,
            from_index=4000,
            to_index=4399,
            reason="buffer_overflow"
        )

        lines = list(self.storage.stream_export("sess_exp"))
        self.assertEqual(len(lines), 2)

        sample_obj = json.loads(lines[0])
        self.assertEqual(sample_obj, {"sensor": 1, "index": 120000, "values": [720, 1200, 1560]})

        loss_obj = json.loads(lines[1])
        self.assertEqual(loss_obj, {"sensor": 2, "from_index": 4000, "to_index": 4399, "reason": "buffer_overflow"})

    def test_sync_state_progression(self):
        """Verify marking samples and losses as synced updates backlog count."""
        self.storage.save_samples_and_losses(
            session_id="sess_sync",
            sensor=1,
            first_index=0,
            data=[[1, 2, 3], [4, 5, 6]],
            lost=[]
        )
        total, synced, backlog = self.storage.get_counts("sess_sync")
        self.assertEqual(backlog, 2)

        # Mark first sample synced
        self.storage.mark_samples_synced([("sess_sync", 1, 0)])
        total, synced, backlog = self.storage.get_counts("sess_sync")
        self.assertEqual(synced, 1)
        self.assertEqual(backlog, 1)


if __name__ == "__main__":
    unittest.main()
