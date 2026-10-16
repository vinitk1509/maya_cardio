"""Automatic online/offline connectivity state machine with bounded transition latency (< 10s)."""
import enum
import time
import urllib.request
import urllib.error
from typing import Callable, Optional

from src.common.logging import setup_logging

logger = setup_logging("state-machine")

# Maximum permitted transition delay per Section 2.4 of assessment: 10 seconds
SWITCH_TIMEOUT_SECONDS = 10.0
HEALTH_CHECK_INTERVAL = 1.0
HEALTH_TIMEOUT = 2.0


class ConnectivityMode(enum.Enum):
    ONLINE = "online"
    OFFLINE = "offline"


class StateMachine:
    """Manages online/offline mode transitions using active cloud health probing."""

    def __init__(
        self,
        cloud_url: str = "http://127.0.0.1:8080",
        on_mode_change: Optional[Callable[[ConnectivityMode, float], None]] = None
    ):
        self.cloud_url = cloud_url.rstrip("/")
        self.health_endpoint = f"{self.cloud_url}/api/v1/health"
        self.on_mode_change = on_mode_change

        self.current_mode = ConnectivityMode.OFFLINE
        self.last_probe_success_mono = 0.0
        self.first_failure_mono: Optional[float] = None
        self.first_success_mono: Optional[float] = None

        # Telemetry: track transition latencies
        self.last_switch_latency: float = 0.0

    def check_cloud_health(self) -> bool:
        """Probe cloud health endpoint."""
        try:
            req = urllib.request.Request(self.health_endpoint, headers={"User-Agent": "MayaCardio-Device"})
            with urllib.request.urlopen(req, timeout=HEALTH_TIMEOUT) as response:
                return response.status == 200
        except Exception:
            return False

    def evaluate_transition(self) -> ConnectivityMode:
        """Evaluate connectivity and transition states within 10 seconds."""
        now_mono = time.monotonic()
        is_healthy = self.check_cloud_health()

        if is_healthy:
            self.last_probe_success_mono = now_mono
            self.first_failure_mono = None

            if self.current_mode == ConnectivityMode.OFFLINE:
                if self.first_success_mono is None:
                    self.first_success_mono = now_mono

                # Fast transition to online once connectivity is confirmed
                switch_latency = now_mono - self.first_success_mono
                if switch_latency <= SWITCH_TIMEOUT_SECONDS:
                    self._transition_to(ConnectivityMode.ONLINE, switch_latency)
                    self.first_success_mono = None
        else:
            self.first_success_mono = None

            if self.current_mode == ConnectivityMode.ONLINE:
                if self.first_failure_mono is None:
                    self.first_failure_mono = now_mono

                # Switch to offline after consecutive failures or timeout
                fail_duration = now_mono - self.first_failure_mono
                # To guarantee switching within 10s while tolerating transient blips
                if fail_duration >= 2.0:  # Switch promptly within 2-3 seconds, well under 10s
                    self._transition_to(ConnectivityMode.OFFLINE, fail_duration)
                    self.first_failure_mono = None

        return self.current_mode

    def _transition_to(self, new_mode: ConnectivityMode, latency: float):
        old_mode = self.current_mode
        self.current_mode = new_mode
        self.last_switch_latency = latency
        logger.info(f"Mode switch: {old_mode.value.upper()} -> {new_mode.value.upper()} "
                    f"(transition time: {latency:.2f}s)")
        if self.on_mode_change:
            try:
                self.on_mode_change(new_mode, latency)
            except Exception as e:
                logger.error(f"Error in on_mode_change callback: {e}")

    @property
    def mode(self) -> str:
        return self.current_mode.value
