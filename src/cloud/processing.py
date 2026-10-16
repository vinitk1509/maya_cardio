"""Online cloud processing runner executing Section 3 window algorithms within 5 seconds."""
import threading
import time
from typing import Dict, List, Optional, Set

from src.cloud.storage import CloudStorage
from src.common.logging import setup_logging
from src.processing.engine import SignalProcessingEngine, WINDOW_SIZE

logger = setup_logging("cloud-processing")


class CloudProcessor:
    """Computes online window processing results as samples arrive."""

    def __init__(self, storage: CloudStorage, check_interval: float = 0.5):
        self.storage = storage
        self.check_interval = check_interval
        self.engine = SignalProcessingEngine()
        self.running = False
        self.thread: Optional[threading.Thread] = None

        # Track processed windows per session: session_id -> Set[int]
        self._processed_windows: Dict[str, Set[int]] = {}
        self.lock = threading.Lock()

    def process_available_windows(self, session_id: str):
        """Check for completed or timed-out windows and compute online results."""
        with self.lock:
            if session_id not in self._processed_windows:
                self._processed_windows[session_id] = set()
            processed = self._processed_windows[session_id]

        max_idx = self.storage.get_max_sample_index(session_id)
        if max_idx is None:
            return

        # Windows range from 0 up to max_idx // WINDOW_SIZE
        # We can process windows that have completed (i.e. window < max_idx // WINDOW_SIZE)
        # or where samples up to 400*w + 399 are stored.
        latest_window = max_idx // WINDOW_SIZE
        for w in range(0, latest_window):
            if w in processed:
                continue

            w_start = w * WINDOW_SIZE
            w_end = w_start + WINDOW_SIZE - 1

            s1_samples = self.storage.get_window_samples(session_id, 1, w_start, w_end)
            s2_samples = self.storage.get_window_samples(session_id, 2, w_start, w_end)

            # If w >= 4, we also need history for sensor 1 channel 2 peak detection
            s1_history = None
            if w >= 4:
                hist_start = max(0, 400 * (w - 4) - 800)
                hist_end = 400 * (w + 1)
                s1_history = self.storage.get_window_samples(session_id, 1, hist_start, hist_end)

            res = self.engine.process_window(
                session_id=session_id,
                window=w,
                mode="online",
                sensor1_samples=s1_samples,
                sensor2_samples=s2_samples,
                all_sensor1_history=s1_history
            )

            self.storage.save_window_result(res)
            with self.lock:
                processed.add(w)

            logger.debug(f"Computed online result for session {session_id} window {w} (HR={res.heart_rate})")

    def run_worker(self):
        self.running = True
        logger.info("CloudProcessor background worker started")
        while self.running:
            # Periodically scan active sessions
            # In a lightweight setup, we can fetch active sessions
            with self.storage._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT DISTINCT session_id FROM cloud_samples")
                sessions = [r["session_id"] for r in cursor.fetchall()]

            for sess in sessions:
                try:
                    self.process_available_windows(sess)
                except Exception as e:
                    logger.error(f"Error processing windows for {sess}: {e}")

            time.sleep(self.check_interval)

    def start(self):
        self.thread = threading.Thread(target=self.run_worker, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)
