"""Reference implementation of biosignal-mock per Section 6 of MayaCardio assessment.

Supports:
  - Modes: 'signal' (realistic ECG/physiological) and 'ramp' (deterministic formula)
  - Fixed reproducible seeds
  - Synchronous dual-sensor 400 Hz generation
  - 10-second retained circular buffer (4000 samples)
  - TCP newline-delimited JSON interface (1 client at a time)
  - Raw zlib CRC-32 checksum calculation
  - Configurable fault injection (corrupt CRC, paused replies, duplicated replies, session restarts)
"""
import argparse
import json
import math
import random
import socket
import sys
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from src.common.protocol import (
    compute_crc32_hex,
    extract_data_array_slice,
    format_mock_reply,
    parse_mock_reply,
    format_read_request
)
from src.common.logging import setup_logging

logger = setup_logging("biosignal-mock")

RATE = 400  # samples per second
BUFFER_SECONDS = 10
BUFFER_SAMPLES = RATE * BUFFER_SECONDS  # 4000 samples
MOD_24BIT = 8_388_608


class SignalGenerator:
    """Generates synthetic sensor data deterministically based on mode and seed."""

    def __init__(self, mode: str, seed: int):
        self.mode = mode
        self.seed = seed
        self.rng = random.Random(seed)

    def get_ramp_sample(self, sensor: int, index: int) -> List[int]:
        """Mode ramp:
        Sensor 1 channel c at sample i = (3i + c) mod 8,388,608 for c in [1, 2, 3]
        Sensor 2 channel c at sample i = (2i + c) mod 8,388,608 for c in [1, 2]
        """
        if sensor == 1:
            return [
                (3 * index + 1) % MOD_24BIT,
                (3 * index + 2) % MOD_24BIT,
                (3 * index + 3) % MOD_24BIT,
            ]
        elif sensor == 2:
            return [
                (2 * index + 1) % MOD_24BIT,
                (2 * index + 2) % MOD_24BIT,
            ]
        else:
            raise ValueError(f"Unknown sensor {sensor}")

    def get_ecg_sample(self, index: int) -> Tuple[List[int], List[int]]:
        """Mode signal:
        Sensor 1, channel 2 looks like an ECG with one sharp peak per heartbeat.
        Heart rate schedule:
          - Minutes 0-2 (0-120s): 60 bpm (1 beat/sec, peak every 400 samples)
          - Minutes 2-4 (120-240s): 90 bpm (1.5 beats/sec, peak every ~266.67 samples)
          - Minutes 4-6 (240-360s): 72 bpm (1.2 beats/sec, peak every ~333.33 samples)
          repeating every 6 minutes (360 seconds).
        
        Sensor 1, channels 1 and 3 are channel 2 * 0.6 and * 1.3.
        Sensor 2 has channel 1 near 400,000 and channel 2 near 150,000, varying slowly.
        """
        sec = index / RATE
        cycle_sec = sec % 360.0

        if cycle_sec < 120.0:
            bpm = 60.0
        elif cycle_sec < 240.0:
            bpm = 90.0
        else:
            bpm = 72.0

        samples_per_beat = (RATE * 60.0) / bpm
        phase = (index % samples_per_beat) / samples_per_beat  # 0.0 to 1.0

        # Baseline noise and wander
        wander = 50.0 * math.sin(2.0 * math.pi * sec / 5.0)
        noise = (self.rng.random() - 0.5) * 10.0
        baseline = 200.0 + wander + noise

        # ECG waveform synthesis: P wave, Q dip, R peak (sharp), S dip, T wave
        # R peak centered around phase = 0.4
        r_peak_center = 0.4
        r_dist = phase - r_peak_center
        # Sharp R peak: width ~ 0.015, amplitude ~ 2000
        r_wave = 2200.0 * math.exp(- (r_dist / 0.012) ** 2)
        q_wave = -250.0 * math.exp(- ((phase - 0.38) / 0.010) ** 2)
        s_wave = -350.0 * math.exp(- ((phase - 0.43) / 0.015) ** 2)
        p_wave = 150.0 * math.exp(- ((phase - 0.20) / 0.04) ** 2)
        t_wave = 300.0 * math.exp(- ((phase - 0.65) / 0.07) ** 2)

        ecg_val = baseline + p_wave + q_wave + r_wave + s_wave + t_wave
        ch2 = int(round(ecg_val))

        ch1 = int(round(ch2 * 0.6))
        ch3 = int(round(ch2 * 1.3))
        s1 = [ch1, ch2, ch3]

        # Sensor 2: channel 1 near 400,000, channel 2 near 150,000, varying slowly
        s2_ch1 = int(round(400_000 + 5000 * math.sin(2 * math.pi * sec / 60.0) + (self.rng.random() - 0.5) * 50))
        s2_ch2 = int(round(150_000 + 3000 * math.cos(2 * math.pi * sec / 45.0) + (self.rng.random() - 0.5) * 50))
        s2 = [s2_ch1, s2_ch2]

        return s1, s2

    def get_sample(self, sensor: int, index: int) -> List[int]:
        if self.mode == "ramp":
            return self.get_ramp_sample(sensor, index)
        elif self.mode == "signal":
            s1, s2 = self.get_ecg_sample(index)
            return s1 if sensor == 1 else s2
        else:
            raise ValueError(f"Unsupported mode {self.mode}")


class BiosignalMockServer:
    """Multi-threaded TCP server emulating biosignal-mock."""

    def __init__(
        self,
        mode: str = "signal",
        seed: int = 7,
        duration: int = 3600,
        host: str = "127.0.0.1",
        port: int = 7100,
        realtime: bool = True
    ):
        self.mode = mode
        self.seed = seed
        self.duration = duration
        self.host = host
        self.port = port
        self.realtime = realtime

        self.session_id = uuid.uuid4().hex[:8]
        self.generator = SignalGenerator(mode, seed)
        self.start_time: float = 0.0
        self.running = False
        self.server_socket: Optional[socket.socket] = None

        # Per-sensor state:
        # Client read cursor index for next sequential read
        self.client_cursor = {1: 0, 2: 0}
        self.lock = threading.Lock()

        # Fault injection controls
        self.fault_corrupt_next_crc = False
        self.fault_duplicate_next = False
        self.fault_stop_sensor: Optional[int] = None
        self.fault_pause_seconds = 0.0

    def get_current_head_index(self) -> int:
        """Returns the current real-time sample index produced by the hardware clock."""
        if not self.realtime or self.start_time == 0.0:
            return 0
        elapsed = time.monotonic() - self.start_time
        head = int(elapsed * RATE)
        max_samples = self.duration * RATE
        return min(head, max_samples)

    def handle_read(self, sensor: int, from_index: Optional[int]) -> bytes:
        with self.lock:
            head = self.get_current_head_index()
            oldest_retained = max(0, head - BUFFER_SAMPLES)

            lost_entries: List[Dict[str, Any]] = []

            if from_index is not None:
                req_index = from_index
            else:
                req_index = self.client_cursor[sensor]

            # Check if sensor is faulted to stop sending
            if self.fault_stop_sensor == sensor:
                # Sensor temporarily stopped
                lost_entries.append({
                    "from_index": req_index,
                    "to_index": head,
                    "reason": "sensor_offline"
                })
                self.client_cursor[sensor] = head
                return format_mock_reply(
                    session_id=self.session_id,
                    sensor=sensor,
                    first_index=req_index,
                    data=[],
                    lost=lost_entries,
                    session_end=(head >= self.duration * RATE)
                )

            # Check if requested index is behind the 10-second buffer
            if req_index < oldest_retained:
                lost_entries.append({
                    "from_index": req_index,
                    "to_index": oldest_retained - 1,
                    "reason": "buffer_overflow"
                })
                req_index = oldest_retained

            # Read up to 1 second (400 samples)
            available_count = max(0, head - req_index)
            batch_count = min(RATE, available_count)

            data: List[List[int]] = []
            for i in range(req_index, req_index + batch_count):
                data.append(self.generator.get_sample(sensor, i))

            next_cursor = req_index + batch_count
            self.client_cursor[sensor] = next_cursor
            session_end = (next_cursor >= self.duration * RATE)

            corrupt_crc = self.fault_corrupt_next_crc
            self.fault_corrupt_next_crc = False

            reply = format_mock_reply(
                session_id=self.session_id,
                sensor=sensor,
                first_index=req_index,
                data=data,
                lost=lost_entries,
                session_end=session_end,
                corrupt_crc=corrupt_crc
            )
            return reply

    def run_server(self):
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind((self.host, self.port))
        self.server_socket.listen(1)
        self.server_socket.settimeout(1.0)
        self.running = True
        self.start_time = time.monotonic()

        logger.info(f"biosignal-mock started: session={self.session_id}, mode={self.mode}, "
                    f"seed={self.seed}, duration={self.duration}s on {self.host}:{self.port}")

        while self.running:
            try:
                client_sock, client_addr = self.server_socket.accept()
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    logger.error(f"Accept error: {e}")
                break

            logger.info(f"Accepted client connection from {client_addr}")
            self._handle_client(client_sock)

    def _handle_client(self, client_sock: socket.socket):
        client_sock.settimeout(5.0)
        recv_buf = b""
        try:
            with client_sock:
                while self.running:
                    try:
                        chunk = client_sock.recv(4096)
                    except socket.timeout:
                        continue
                    if not chunk:
                        logger.info("Client disconnected")
                        break

                    recv_buf += chunk
                    while b"\n" in recv_buf:
                        line, recv_buf = recv_buf.split(b"\n", 1)
                        line = line.strip()
                        if not line:
                            continue

                        if self.fault_pause_seconds > 0:
                            time.sleep(self.fault_pause_seconds)
                            self.fault_pause_seconds = 0.0

                        try:
                            req = json.loads(line.decode("utf-8"))
                        except Exception as e:
                            logger.error(f"Cannot decode client request: {line}")
                            continue

                        cmd = req.get("cmd")
                        if cmd == "read":
                            sensor = int(req["sensor"])
                            from_index = req.get("from_index")
                            reply_bytes = self.handle_read(sensor, from_index)

                            client_sock.sendall(reply_bytes)

                            if self.fault_duplicate_next:
                                self.fault_duplicate_next = False
                                # Send same reply a second time as fault injection
                                client_sock.sendall(reply_bytes)
        except Exception as e:
            logger.info(f"Connection ended: {e}")

    def stop(self):
        self.running = False
        if self.server_socket:
            try:
                self.server_socket.close()
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="MayaCardio biosignal-mock service")
    parser.add_argument("--mode", choices=["signal", "ramp"], default="signal", help="Signal generation mode")
    parser.add_argument("--seed", type=int, default=7, help="Deterministic RNG seed")
    parser.add_argument("--duration", type=int, default=3600, help="Duration in seconds")
    parser.add_argument("--port", type=int, default=7100, help="TCP port to listen on")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    parser.add_argument("--fast", action="store_true", help="Accelerated test mode (non-realtime)")

    args = parser.parse_args()
    server = BiosignalMockServer(
        mode=args.mode,
        seed=args.seed,
        duration=args.duration,
        host=args.host,
        port=args.port,
        realtime=not args.fast
    )
    try:
        server.run_server()
    except KeyboardInterrupt:
        logger.info("Stopping mock server on interrupt")
        server.stop()


if __name__ == "__main__":
    main()
