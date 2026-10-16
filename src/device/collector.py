"""Device collector: multiplexes continuous dual-sensor reading over TCP, verifies raw CRC32,
handles retries/replays, detects mock session resets, and persists directly to DeviceStorage.
"""
import json
import socket
import sys
import threading
import time
from typing import Any, Dict, Optional, Tuple

from src.common.logging import setup_logging
from src.common.protocol import (
    CRCValidationError,
    MalformedMessageError,
    extract_data_array_slice,
    format_read_request,
    parse_mock_reply
)
from src.device.storage import DeviceStorage, DiskFullError

logger = setup_logging("device-collector")


class DeviceCollector:
    """Manages the continuous TCP connection to biosignal-mock."""

    def __init__(
        self,
        storage: DeviceStorage,
        mock_host: str = "127.0.0.1",
        mock_port: int = 7100,
        socket_timeout: float = 3.0
    ):
        self.storage = storage
        self.mock_host = mock_host
        self.mock_port = mock_port
        self.socket_timeout = socket_timeout

        self.running = False
        self.sock: Optional[socket.socket] = None
        self.current_session_id: Optional[str] = None

        # Next expected index per sensor (for replay requests upon CRC failure)
        self.next_index = {1: 0, 2: 0}
        self.recv_buffer = b""

    def connect(self) -> bool:
        """Establish TCP connection to mock server with retry."""
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(self.socket_timeout)
            s.connect((self.mock_host, self.mock_port))
            self.sock = s
            self.recv_buffer = b""
            logger.info(f"Connected to biosignal-mock on {self.mock_host}:{self.mock_port}")
            return True
        except Exception as e:
            logger.warning(f"Connection to biosignal-mock failed: {e}")
            return False

    def _read_line(self) -> Optional[bytes]:
        """Read a single newline-terminated line from the TCP socket."""
        while b"\n" not in self.recv_buffer:
            if not self.sock:
                return None
            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    logger.warning("Mock closed TCP connection")
                    return None
                self.recv_buffer += chunk
            except socket.timeout:
                return None
            except Exception as e:
                logger.warning(f"Socket error reading: {e}")
                return None

        line, self.recv_buffer = self.recv_buffer.split(b"\n", 1)
        return line.strip()

    def _request_and_parse(
        self,
        sensor: int,
        from_index: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """Send a read command and parse the reply, verifying CRC32."""
        if not self.sock:
            if not self.connect():
                return None

        req_bytes = format_read_request(sensor, from_index)
        try:
            self.sock.sendall(req_bytes)
        except Exception as e:
            logger.warning(f"Failed to send request for sensor {sensor}: {e}")
            self.sock = None
            return None

        reply_line = self._read_line()
        if not reply_line:
            return None

        try:
            return parse_mock_reply(reply_line, verify_crc=True)
        except CRCValidationError as e:
            logger.error(f"CRC32 mismatch on sensor {sensor}: {e}. Requesting replay...")
            # Issue immediate replay request with from_index
            retry_idx = from_index if from_index is not None else self.next_index[sensor]
            replay_req = format_read_request(sensor, from_index=retry_idx)
            try:
                self.sock.sendall(replay_req)
                retry_line = self._read_line()
                if retry_line:
                    return parse_mock_reply(retry_line, verify_crc=True)
            except Exception as re_err:
                logger.error(f"Replay request failed: {re_err}")
            return None
        except MalformedMessageError as e:
            logger.error(f"Malformed reply from mock: {e}")
            return None

    def read_cycle(self) -> Tuple[int, int]:
        """Perform one read cycle alternating across Sensor 1 and Sensor 2.
        
        Returns (samples_saved_sensor_1, samples_saved_sensor_2).
        """
        saved_1 = 0
        saved_2 = 0

        for sensor in (1, 2):
            reply = self._request_and_parse(sensor)
            if not reply:
                continue

            session_id = reply["session_id"]
            first_idx = reply["first_index"]
            count = reply["count"]
            data = reply["data"]
            lost = reply["lost"]
            session_end = reply.get("session_end", False)

            # Detect session change (mock restarted)
            if self.current_session_id is not None and self.current_session_id != session_id:
                logger.warning(
                    f"Mock session changed from {self.current_session_id} to {session_id}! "
                    f"Recording unread loss for old session with to_index: null"
                )
                # Old session loss record per requirement Section 6.3:
                # "restart, which starts a new session. Unread data of the old session is then lost
                # and must be listed, with 'to_index': null when its end is unknown."
                self.storage.record_loss(
                    session_id=self.current_session_id,
                    sensor=sensor,
                    from_index=self.next_index[sensor],
                    to_index=None,
                    reason="mock_restarted_new_session"
                )
                # Reset next_index for new session
                self.next_index = {1: 0, 2: 0}

            self.current_session_id = session_id

            try:
                n_inserted = self.storage.save_samples_and_losses(
                    session_id=session_id,
                    sensor=sensor,
                    first_index=first_idx,
                    data=data,
                    lost=lost
                )
                if sensor == 1:
                    saved_1 += n_inserted
                else:
                    saved_2 += n_inserted

                # Update next expected index
                self.next_index[sensor] = first_idx + count
            except DiskFullError as e:
                logger.critical(f"DISK FULL ENCOUNTERED: {e}. Entering safe degraded mode.")
                time.sleep(1.0)
            except Exception as e:
                logger.error(f"Failed to persist samples for sensor {sensor}: {e}")

        return saved_1, saved_2

    def run(self):
        """Main collection loop."""
        self.running = True
        logger.info("Collector thread running")

        while self.running:
            if not self.sock:
                if not self.connect():
                    time.sleep(0.5)
                    continue

            s1, s2 = self.read_cycle()
            if s1 == 0 and s2 == 0:
                # Mock had no new samples available right now, sleep briefly
                time.sleep(0.05)

    def stop(self):
        """Stop collector thread."""
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
