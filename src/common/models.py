"""Domain models for MayaCardio data pipeline."""
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Any, Dict


@dataclass(frozen=True)
class Sample:
    session_id: str
    sensor: int
    index: int
    values: List[int]

    def to_export_dict(self) -> Dict[str, Any]:
        """Format matching candidate assessment export:
        {"sensor": 1, "index": 120000, "values": [720, 1200, 1560]}
        """
        return {
            "sensor": self.sensor,
            "index": self.index,
            "values": list(self.values)
        }


@dataclass(frozen=True)
class LossRecord:
    session_id: str
    sensor: int
    from_index: int
    to_index: Optional[int]
    reason: str

    def to_export_dict(self) -> Dict[str, Any]:
        """Format matching candidate assessment export:
        {"sensor": 2, "from_index": 4000, "to_index": 4399, "reason": "..."}
        """
        return {
            "sensor": self.sensor,
            "from_index": self.from_index,
            "to_index": self.to_index,
            "reason": self.reason
        }


@dataclass
class WindowResult:
    session_id: str
    window: int
    missing_1: int
    missing_2: int
    complete: bool
    mean: Optional[List[Optional[float]]]      # 3 channels for S1, 2 channels for S2 -> combined or per-sensor
    min: Optional[List[Optional[float]]]
    max: Optional[List[Optional[float]]]
    magnitude: Optional[float]
    heart_rate: Optional[float]
    mode: str                                  # 'online' or 'offline'

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "window": self.window,
            "missing_1": self.missing_1,
            "missing_2": self.missing_2,
            "complete": self.complete,
            "mean": self.mean,
            "min": self.min,
            "max": self.max,
            "magnitude": self.magnitude,
            "heart_rate": self.heart_rate,
            "mode": self.mode
        }


@dataclass
class PipelineStatus:
    mode: str               # "online" or "offline"
    samples_saved: int
    samples_in_cloud: int
    backlog: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "samples_saved": self.samples_saved,
            "samples_in_cloud": self.samples_in_cloud,
            "backlog": self.backlog
        }
