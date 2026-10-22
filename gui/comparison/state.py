"""Qt-free timeline rules for a local video comparison session."""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from enum import Enum
import math


class ComparisonPhase(Enum):
    EMPTY = "empty"
    LOADING = "loading"
    PAUSED = "paused"
    PLAYING = "playing"
    SEEKING = "seeking"
    SCRUBBING = "scrubbing"
    ERROR = "error"
    CLOSED = "closed"


@dataclass(frozen=True)
class VideoDetails:
    """Video interval in container-relative seconds (duration is its endpoint)."""
    duration: float
    origin: float
    fps: float
    start: float = 0.0
    has_audio: bool = False
    width: int = 0
    height: int = 0

    def __post_init__(self) -> None:
        if not all(math.isfinite(value) for value in (self.duration, self.origin, self.fps, self.start)):
            raise ValueError("Invalid video timing metadata")
        if self.duration <= self.start or self.start < 0 or self.fps <= 0:
            raise ValueError("Video duration and frame rate must be positive")


@dataclass(frozen=True)
class ComparisonTimeline:
    duration_a: float
    duration_b: float
    offset: float = 0.0
    start_a: float = 0.0
    start_b: float = 0.0

    @property
    def start(self) -> float:
        return max(self.start_a, self.start_b - self.offset)

    @property
    def end(self) -> float:
        return min(self.duration_a, self.duration_b - self.offset)

    @property
    def available(self) -> bool:
        return self.end > self.start

    def clamp(self, position: float) -> float:
        return max(self.start, min(position, self.end))

    def positions(self, position: float) -> tuple[float, float]:
        a = self.clamp(position)
        return a, a + self.offset


def select_frame(timestamps: list[float], target: float, direction: int = 0) -> float:
    """Select the covering frame, or its neighbour, from real presentation times."""
    if not timestamps:
        raise ValueError("No video frames at the requested position")
    index = max(0, bisect_right(timestamps, target + 0.000001) - 1)
    return timestamps[max(0, min(len(timestamps) - 1, index + direction))]
