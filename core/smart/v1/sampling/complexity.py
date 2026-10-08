"""Lightweight FFmpeg content-complexity probes for Smart sampling.

The module deliberately does not run subprocesses.  Smart orchestration owns
process lifetime, cancellation and logging; this boundary builds deterministic
commands and validates the metadata they produce.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path


SCOUT_FPS = 12
SCENE_CHANGE_THRESHOLD = 10.0
SCOUT_MAX_WIDTH = 480

_FRAME_RE = re.compile(r"^frame:\s*(\d+)\b")
_METADATA_RE = re.compile(
    r"^(lavfi\.(?:siti\.(?:si|ti)|scd\.(?:score|time)|signalstats\.YAVG))=(.+)$"
)


class ComplexityProbeError(ValueError):
    """FFmpeg's scout metadata is unavailable, malformed, or not usable."""


@dataclass(frozen=True, slots=True)
class ScoutMetrics:
    frame_count: int
    si_p90: float
    ti_p90: float
    scene_cut_times: tuple[float, ...]
    max_scene_score: float
    luma_mean: float = 128.0


def _filter_path(path: Path) -> str:
    """Escape the small subset of FFmpeg filter-value metacharacters we use."""

    return str(path).replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")


def _metadata_filter(metadata_path: Path) -> str:
    return f"metadata=mode=print:file='{_filter_path(metadata_path)}'"


def _scout_prefix_filters() -> str:
    # Both dimensions stay even.  `ow/dar` preserves display aspect ratio after
    # limiting the width, including for portrait inputs.
    return (
        f"setpts=PTS-STARTPTS,fps={SCOUT_FPS},"
        f"scale=w='min({SCOUT_MAX_WIDTH},trunc(iw/2)*2)':h='trunc(ow/dar/2)*2':flags=bicubic"
    )


def build_scout_command(
    ffmpeg_path: Path,
    source_path: Path,
    *,
    start_sec: float,
    duration_sec: float,
    metadata_path: Path,
) -> list[str]:
    """Build a low-cost SI/TI/scene probe that writes frame metadata to a file."""

    if not math.isfinite(start_sec) or start_sec < 0:
        raise ValueError("Scout start must be finite and non-negative.")
    if not math.isfinite(duration_sec) or duration_sec <= 0:
        raise ValueError("Scout duration must be finite and positive.")
    filters = ",".join(
        (
            _scout_prefix_filters(),
            "siti",
            "signalstats",
            f"scdet=threshold={SCENE_CHANGE_THRESHOLD:g}",
            _metadata_filter(metadata_path),
        )
    )
    return [
        str(ffmpeg_path),
        "-hide_banner",
        "-nostdin",
        "-ss",
        f"{start_sec:.6f}",
        "-i",
        str(source_path),
        "-map",
        "0:v:0",
        "-t",
        f"{duration_sec:.6f}",
        "-an",
        "-vf",
        filters,
        "-f",
        "null",
        "-",
    ]


def build_scene_guard_command(
    ffmpeg_path: Path,
    source_path: Path,
    *,
    start_sec: float,
    duration_sec: float,
    metadata_path: Path,
) -> list[str]:
    """Build the smaller scene-only probe used to align a planned window."""

    if not math.isfinite(start_sec) or start_sec < 0:
        raise ValueError("Scene-guard start must be finite and non-negative.")
    if not math.isfinite(duration_sec) or duration_sec <= 0:
        raise ValueError("Scene-guard duration must be finite and positive.")
    filters = ",".join(
        (
            _scout_prefix_filters(),
            f"scdet=threshold={SCENE_CHANGE_THRESHOLD:g}",
            _metadata_filter(metadata_path),
        )
    )
    return [
        str(ffmpeg_path),
        "-hide_banner",
        "-nostdin",
        "-ss",
        f"{start_sec:.6f}",
        "-i",
        str(source_path),
        "-map",
        "0:v:0",
        "-t",
        f"{duration_sec:.6f}",
        "-an",
        "-vf",
        filters,
        "-f",
        "null",
        "-",
    ]


def _finite_value(raw: str, key: str) -> float:
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ComplexityProbeError(f"Invalid {key} metadata value: {raw!r}") from exc
    if not math.isfinite(value):
        raise ComplexityProbeError(f"Non-finite {key} metadata value: {raw!r}")
    return value


def _metadata_frames(metadata: str) -> list[dict[str, float]]:
    frames: list[dict[str, float]] = []
    current: dict[str, float] | None = None
    for line in metadata.splitlines():
        if _FRAME_RE.match(line):
            if current is not None:
                frames.append(current)
            current = {}
            continue
        match = _METADATA_RE.match(line)
        if match is None:
            continue
        if current is None:
            raise ComplexityProbeError("Scout metadata contains values before its first frame.")
        current[match.group(1)] = _finite_value(match.group(2), match.group(1))
    if current is not None:
        frames.append(current)
    if not frames:
        raise ComplexityProbeError("Scout metadata did not contain any frames.")
    return frames


def _percentile_90(values: list[float]) -> float:
    if not values:
        raise ComplexityProbeError("Scout metadata did not contain enough valid frame metrics.")
    ordered = sorted(values)
    # Nearest-rank percentile makes the value stable across Python versions and
    # avoids synthetic interpolation for the deliberately short scout clips.
    index = max(0, math.ceil(len(ordered) * 0.90) - 1)
    return ordered[index]


def _scene_cuts(frames: list[dict[str, float]]) -> tuple[tuple[float, ...], float]:
    cuts: list[float] = []
    scores: list[float] = []
    for frame in frames:
        if "lavfi.scd.score" not in frame:
            raise ComplexityProbeError("Scout metadata is missing lavfi.scd.score.")
        score = frame["lavfi.scd.score"]
        scores.append(score)
        scene_time = frame.get("lavfi.scd.time")
        if score >= SCENE_CHANGE_THRESHOLD:
            if scene_time is None:
                raise ComplexityProbeError("Scene-cut metadata is missing lavfi.scd.time.")
            cuts.append(scene_time)
        elif scene_time is not None:
            cuts.append(scene_time)
    return tuple(sorted(set(cuts))), max(scores)


def parse_scout_metadata(metadata: str) -> ScoutMetrics:
    """Validate scout metadata and aggregate SI/TI by the specified policy."""

    frames = _metadata_frames(metadata)
    scene_cut_times, max_scene_score = _scene_cuts(frames)
    si_values: list[float] = []
    ti_values: list[float] = []
    cut_times = set(scene_cut_times)
    for index, frame in enumerate(frames):
        if "lavfi.siti.si" not in frame or "lavfi.siti.ti" not in frame:
            raise ComplexityProbeError("Scout metadata is missing lavfi.siti.si or lavfi.siti.ti.")
        si_values.append(frame["lavfi.siti.si"])
        # The first TI has no prior frame.  Scene-cut TI is deliberately not a
        # proxy for motion and therefore must not influence ranking.
        scene_time = frame.get("lavfi.scd.time")
        if index != 0 and scene_time not in cut_times:
            ti_values.append(frame["lavfi.siti.ti"])
    return ScoutMetrics(
        frame_count=len(frames),
        si_p90=_percentile_90(si_values),
        ti_p90=_percentile_90(ti_values),
        scene_cut_times=scene_cut_times,
        max_scene_score=max_scene_score,
        luma_mean=(
            sum(frame.get("lavfi.signalstats.YAVG", 128.0) for frame in frames)
            / len(frames)
        ),
    )


def parse_scene_guard_metadata(metadata: str) -> tuple[float, ...]:
    """Return validated scene-cut timestamps from a scene-only probe."""

    frames = _metadata_frames(metadata)
    scene_cut_times, _max_scene_score = _scene_cuts(frames)
    return scene_cut_times
