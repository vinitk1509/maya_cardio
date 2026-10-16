"""Device pipeline service: orchestrates collector, storage, state machine,
sync worker, and local offline signal processing.
"""
import argparse
import os
import signal
import sys
import threading
import time
from typing import Dict, Optional, Set

from src.common.logging import setup_logging
from src.common.models import PipelineStatus
from src.device.collector import DeviceCollector
from src.device.state_machine import ConnectivityMode, StateMachine
from src.device.storage import DeviceStorage
from src.device.sync import CloudSyncWorker
from src.processing.engine import SignalProcessingEngine, WINDOW_SIZE

logger = setup_logging("device-pipeline")


class PipelineService:
    """Core daemon managing continuous data capture, storage, sync, and offline processing."""

    def __init__(
        self,
        db_path: str = "device_data.db",
        mock_host: str = "127.0.0.1",
        mock_port: int = 7100,
        cloud_url: str = "http://127.0.0.1:8080"
    ):
        self.db_path = db_path
        self.storage = DeviceStorage(db_path)
        self.collector = DeviceCollector(self.storage, mock_host=mock_host, mock_port=mock_port)
        self.sync_worker = CloudSyncWorker(self.storage, cloud_url=cloud_url)
        self.state_machine = StateMachine(cloud_url=cloud_url, on_mode_change=self._on_mode_change)
        self.processing_engine = SignalProcessingEngine()

        self.running = False
        self.collector_thread: Optional[threading.Thread] = None
        self.state_thread: Optional[threading.Thread] = None
        self.offline_proc_thread: Optional[threading.Thread] = None

        # Track processed offline windows: session_id -> Set[int]
        self._processed_offline_windows: Dict[str, Set[int]] = {}
        self.lock = threading.Lock()

    def _on_mode_change(self, new_mode: ConnectivityMode, latency: float):
        logger.info(f"Pipeline acknowledged mode change: {new_mode.value.upper()}")
        self.sync_worker.is_online = (new_mode == ConnectivityMode.ONLINE)

    def get_status(self, session_id: Optional[str] = None) -> PipelineStatus:
        """Required command 1: pipeline status."""
        total, synced, backlog = self.storage.get_counts(session_id)
        return PipelineStatus(
            mode=self.state_machine.mode,
            samples_saved=total,
            samples_in_cloud=synced,
            backlog=backlog
        )

    def _run_state_monitor(self):
        while self.running:
            try:
                self.state_machine.evaluate_transition()
            except Exception as e:
                logger.error(f"State machine error: {e}")
            time.sleep(1.0)

    def _run_offline_processor(self):
        """Processes windows locally when offline (or persists offline results)."""
        while self.running:
            # When offline, run Section 3 processing locally
            if self.state_machine.current_mode == ConnectivityMode.OFFLINE:
                session_id = self.collector.current_session_id
                if session_id:
                    self._process_offline_windows_for_session(session_id)
            time.sleep(0.5)

    def _process_offline_windows_for_session(self, session_id: str):
        with self.lock:
            if session_id not in self._processed_offline_windows:
                self._processed_offline_windows[session_id] = set()
            processed = self._processed_offline_windows[session_id]

        last_s1 = self.storage.get_last_stored_index(session_id, 1)
        last_s2 = self.storage.get_last_stored_index(session_id, 2)
        if last_s1 is None or last_s2 is None:
            return

        min_last = min(last_s1, last_s2)
        latest_window = min_last // WINDOW_SIZE

        for w in range(0, latest_window):
            if w in processed:
                continue

            w_start = w * WINDOW_SIZE
            w_end = w_start + WINDOW_SIZE - 1

            s1_samples = self.storage.get_window_samples(session_id, 1, w_start, w_end)
            s2_samples = self.storage.get_window_samples(session_id, 2, w_start, w_end)

            s1_history = None
            if w >= 4:
                hist_start = max(0, 400 * (w - 4) - 800)
                hist_end = 400 * (w + 1)
                s1_history = self.storage.get_window_samples(session_id, 1, hist_start, hist_end)

            res = self.processing_engine.process_window(
                session_id=session_id,
                window=w,
                mode="offline",
                sensor1_samples=s1_samples,
                sensor2_samples=s2_samples,
                all_sensor1_history=s1_history
            )

            self.storage.save_window_result(res)
            with self.lock:
                processed.add(w)

            logger.debug(f"Computed offline result for session {session_id} window {w} (HR={res.heart_rate})")

    def start(self):
        """Start all pipeline sub-services."""
        self.running = True
        logger.info("Starting MayaCardio Pipeline Service...")

        # 1. Start sync worker
        self.sync_worker.start()

        # 2. Start collector thread
        self.collector_thread = threading.Thread(target=self.collector.run, daemon=True)
        self.collector_thread.start()

        # 3. Start state monitor
        self.state_thread = threading.Thread(target=self._run_state_monitor, daemon=True)
        self.state_thread.start()

        # 4. Start offline processor
        self.offline_proc_thread = threading.Thread(target=self._run_offline_processor, daemon=True)
        self.offline_proc_thread.start()

        logger.info("All pipeline sub-services started successfully.")

    def stop(self):
        """Gracefully stop all pipeline sub-services."""
        logger.info("Stopping Pipeline Service...")
        self.running = False
        self.collector.stop()
        self.sync_worker.stop()
        logger.info("Pipeline Service stopped cleanly.")


def main():
    parser = argparse.ArgumentParser(description="MayaCardio Pipeline Service")
    parser.add_argument("--db", default="device_data.db", help="Path to device SQLite database")
    parser.add_argument("--mock-host", default="127.0.0.1", help="Mock host")
    parser.add_argument("--mock-port", type=int, default=7100, help="Mock port")
    parser.add_argument("--cloud-url", default="http://127.0.0.1:8080", help="Cloud API endpoint")
    args = parser.parse_args()

    service = PipelineService(
        db_path=args.db,
        mock_host=args.mock_host,
        mock_port=args.mock_port,
        cloud_url=args.cloud_url
    )

    def handle_signal(sig, frame):
        logger.info(f"Received exit signal {sig}")
        service.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    service.start()
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        service.stop()


if __name__ == "__main__":
    main()
