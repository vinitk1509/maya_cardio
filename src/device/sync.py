"""Cloud synchronization worker: handles low-latency live streaming (< 5s deadline)
and high-throughput catch-up synchronization (> 6,400 samples/sec) with gzip compression and idempotent retry.
"""
import gzip
import json
import threading
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional, Tuple

from src.common.logging import setup_logging
from src.device.storage import DeviceStorage

logger = setup_logging("cloud-sync")

LIVE_BATCH_SIZE = 400
CATCHUP_BATCH_SIZE = 4000
SYNC_TIMEOUT = 5.0


class CloudSyncWorker:
    """Synchronizes device samples and loss records to the cloud endpoint."""

    def __init__(
        self,
        storage: DeviceStorage,
        cloud_url: str = "http://127.0.0.1:8080",
        poll_interval: float = 0.2
    ):
        self.storage = storage
        self.cloud_url = cloud_url.rstrip("/")
        self.ingest_endpoint = f"{self.cloud_url}/api/v1/ingest"
        self.poll_interval = poll_interval

        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.is_online = False

        # Metrics
        self.total_samples_synced = 0
        self.total_bytes_sent = 0
        self.last_sync_throughput: float = 0.0

    def post_batch(
        self,
        samples: List[Dict[str, Any]],
        losses: List[Dict[str, Any]],
        use_gzip: bool = True
    ) -> bool:
        """Send a batch to cloud ingest endpoint with idempotent retry."""
        payload = json.dumps({"samples": samples, "losses": losses}).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "MayaCardio-Device-Sync"
        }

        if use_gzip and len(payload) > 1024:
            payload = gzip.compress(payload)
            headers["Content-Encoding"] = "gzip"

        self.total_bytes_sent += len(payload)

        try:
            req = urllib.request.Request(
                self.ingest_endpoint,
                data=payload,
                headers=headers,
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=SYNC_TIMEOUT) as response:
                if response.status == 200:
                    return True
                logger.warning(f"Ingest endpoint returned status {response.status}")
                return False
        except Exception as e:
            logger.debug(f"Sync POST failed: {e}")
            return False

    def sync_cycle(self) -> int:
        """Perform one sync cycle: send unsynced samples and losses."""
        if not self.is_online:
            return 0

        # Check backlog size to adapt batch size (catch-up mode vs live stream)
        total, synced, backlog = self.storage.get_counts()
        batch_size = CATCHUP_BATCH_SIZE if backlog > LIVE_BATCH_SIZE * 2 else LIVE_BATCH_SIZE

        unsynced_samples = self.storage.get_unsynced_samples(limit=batch_size)
        unsynced_losses = self.storage.get_unsynced_losses(limit=100)

        if not unsynced_samples and not unsynced_losses:
            return 0

        t0 = time.monotonic()
        success = self.post_batch(unsynced_samples, unsynced_losses, use_gzip=(len(unsynced_samples) > 200))
        t1 = time.monotonic()

        if success:
            sample_keys = [
                (s["session_id"], s["sensor"], s["index"])
                for s in unsynced_samples
            ]
            self.storage.mark_samples_synced(sample_keys)

            loss_ids = [l["id"] for l in unsynced_losses]
            self.storage.mark_losses_synced(loss_ids)

            dt = max(t1 - t0, 0.001)
            self.total_samples_synced += len(sample_keys)
            self.last_sync_throughput = len(sample_keys) / dt
            logger.debug(f"Synced {len(sample_keys)} samples in {dt:.3f}s ({self.last_sync_throughput:.0f} samples/s)")
            return len(sample_keys)
        else:
            return -1

    def run(self):
        self.running = True
        logger.info("CloudSyncWorker started")
        while self.running:
            if self.is_online:
                count = self.sync_cycle()
                if count <= 0:
                    time.sleep(self.poll_interval)
            else:
                time.sleep(self.poll_interval)

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)
