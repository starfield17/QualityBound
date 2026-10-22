"""Cancellable, bounded local-tool operations used by comparison workers.

This module has no Qt dependency. It never publishes or modifies media files.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from pathlib import Path
import subprocess
import threading
import time

from core.ffmpeg import discover_ffmpeg_tools, noninteractive_run_kwargs, terminate_process
from gui.comparison.state import ComparisonTimeline, VideoDetails, select_frame


class DecodeCancelled(Exception):
    pass


class ToolRunner:
    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self.stderr = b""

    def cancel(self) -> None:
        self.cancelled.set()

    def run(self, command: list[str]) -> bytes:
        if self.cancelled.is_set():
            raise DecodeCancelled()
        with subprocess.Popen[bytes](command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              **noninteractive_run_kwargs()) as process:
            deadline = time.monotonic() + 60
            try:
                while True:
                    if self.cancelled.is_set():
                        raise DecodeCancelled()
                    if time.monotonic() > deadline:
                        raise TimeoutError("Video inspection exceeded 60 seconds")
                    try:
                        output, error = process.communicate(timeout=0.1)
                        self.stderr = error
                        break
                    except subprocess.TimeoutExpired:
                        continue
                if process.returncode:
                    raise ValueError(error.decode("utf-8", errors="replace")[-2000:])
                return output
            finally:
                terminate_process(process)


@dataclass(frozen=True)
class ComparisonMedia:
    paths: tuple[Path, Path]
    details: tuple[VideoDetails, VideoDetails]
    ffmpeg: Path
    ffprobe: Path


@dataclass(frozen=True)
class DecodedFrame:
    timestamp: float
    png: bytes


@dataclass(frozen=True)
class FramePair:
    a: DecodedFrame
    b: DecodedFrame
    offset: float
    position: float


def inspect_pair(paths: tuple[Path, Path], ffmpeg: str | None,
                 ffprobe: str | None, runner: ToolRunner) -> ComparisonMedia:
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(str(path))
    # Explicit tools use the application's existing discovery policy, without fallback.
    encoder, probe = discover_ffmpeg_tools(ffmpeg, ffprobe)
    details = tuple(inspect_video(path, probe, runner) for path in paths)
    return ComparisonMedia(paths, (details[0], details[1]), encoder, probe)


def inspect_video(path: Path, probe: Path, runner: ToolRunner) -> VideoDetails:
    payload = json.loads(runner.run([
        str(probe), "-v", "error", "-show_entries",
        "stream=codec_type,duration,start_time,avg_frame_rate,r_frame_rate,width,height:format=duration,start_time",
        "-of", "json", str(path),
    ]))
    streams = [stream for stream in payload.get("streams", []) if stream.get("codec_type", "video") == "video"]
    if not streams:
        raise ValueError(f"No video stream: {path.name}")
    stream = streams[0]
    has_audio = any(stream.get("codec_type") == "audio" for stream in payload.get("streams", []))
    container = payload.get("format", {})
    duration = float(stream.get("duration", container.get("duration", 0)))
    origin = float(container.get("start_time", stream.get("start_time", 0)))
    start = max(0.0, float(stream.get("start_time", origin)) - origin)
    if "duration" in stream:
        duration += start
    numerator, denominator = str(stream.get("avg_frame_rate", "0/1")).split("/")
    fps = float(numerator) / float(denominator) if float(denominator) else 0.0
    if fps <= 0:
        numerator, denominator = str(stream.get("r_frame_rate", "0/1")).split("/")
        fps = float(numerator) / float(denominator) if float(denominator) else 0.0
    width = int(stream.get("width", 0) or 0)
    height = int(stream.get("height", 0) or 0)
    details = VideoDetails(duration, origin, fps, start, has_audio, width, height)
    if "duration" not in stream:
        # Matroska and similar containers may report the last timestamp as their
        # duration, including a start offset or a longer audio tail. Measure the
        # final video frame rather than treating that container value as video.
        command = [str(probe), "-v", "error", "-select_streams", "v:0",
                   "-read_intervals", f"{max(origin, origin + duration - 2):.6f}%",
                   "-show_frames", "-show_entries",
                   "frame=best_effort_timestamp_time,duration_time,pkt_duration_time", "-of", "json", str(path)]
        tail = json.loads(runner.run(command)).get("frames", [])
        if not tail:
            # An audio-only tail can put the seek beyond the final video packet.
            # A scan is still cancellable and bounded by the same tool timeout.
            del command[command.index("-read_intervals"):command.index("-read_intervals") + 2]
            tail = json.loads(runner.run(command)).get("frames", [])
        frames = [frame for frame in tail if "best_effort_timestamp_time" in frame]
        if not frames:
            raise ValueError(f"No timestamped video frames: {path.name}")
        last = max(frames, key=lambda frame: float(frame["best_effort_timestamp_time"]))
        end = float(last["best_effort_timestamp_time"]) - origin
        frame_duration = float(last.get("duration_time", last.get("pkt_duration_time", 0)))
        details = VideoDetails(
            end + (frame_duration if frame_duration > 0 else 1 / fps),
            origin,
            fps,
            start,
            has_audio,
            width,
            height,
        )
    return details


def frame_time(media: ComparisonMedia, side: int, target: float,
               direction: int, runner: ToolRunner) -> float:
    details = media.details[side]
    # Absolute end (not a relative +duration): seeking can land at a much earlier
    # keyframe. Include that preroll and continue through the requested position.
    start = max(0, target - max(2, 4 / details.fps)) + details.origin
    horizon = max(2, 4 / details.fps)
    while True:
        end = target + horizon + details.origin
        at_end = end >= details.origin + details.duration
        interval = f"{start:.6f}%" if at_end else f"{start:.6f}%{end:.6f}"
        payload = json.loads(runner.run([
            str(media.ffprobe), "-v", "error", "-select_streams", "v:0",
            "-read_intervals", interval, "-show_frames",
            "-show_entries", "frame=best_effort_timestamp_time", "-of", "json",
            str(media.paths[side]),
        ]))
        timestamps = sorted({float(frame["best_effort_timestamp_time"]) - details.origin
                             for frame in payload.get("frames", [])
                             if "best_effort_timestamp_time" in frame})
        # A future frame proves which frame covers the target. Without it, a
        # sparse VFR gap (or decoder reorder delay) requires a longer horizon.
        # An open-ended final interval flushes the decoder through actual EOF.
        if at_end or (timestamps and timestamps[-1] > target + 0.000001):
            break
        horizon *= 2
    return select_frame(timestamps, target, direction)


def decode_frame(media: ComparisonMedia, side: int, timestamp: float,
                 runner: ToolRunner) -> DecodedFrame:
    png = runner.run([
        str(media.ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin",
        "-ss", f"{max(0, timestamp - 0.000001):.6f}", "-i", str(media.paths[side]),
        "-vf", "scale=round(iw*sar):ih,setsar=1",
        "-map", "0:v:0", "-frames:v", "1", "-an", "-sn", "-fps_mode", "passthrough",
        "-f", "image2pipe", "-c:v", "png", "pipe:1",
    ])
    if not png:
        raise ValueError("No decoded video frame")
    return DecodedFrame(timestamp, png)


def decode_pair(media: ComparisonMedia, position: float, offset: float,
                runner: ToolRunner, step_side: int = 0, direction: int = 0) -> FramePair:
    timeline = ComparisonTimeline(media.details[0].duration, media.details[1].duration, offset,
                                  media.details[0].start, media.details[1].start)
    position = timeline.clamp(position)
    a = frame_time(media, 0, position, direction if step_side == 0 else 0, runner)
    b = frame_time(media, 1, position + offset, direction if step_side == 1 else 0, runner)
    if direction and step_side == 0:
        position = timeline.clamp(a)
        a = frame_time(media, 0, position, 0, runner)
        b = frame_time(media, 1, position + offset, 0, runner)
    elif direction:
        offset = b - position
    return FramePair(decode_frame(media, 0, a, runner), decode_frame(media, 1, b, runner), offset, position)


def preview_pair(media: ComparisonMedia, position: float, offset: float, runner: ToolRunner) -> FramePair:
    """Fast normal-mode seeks: decode the first frame at/after each target."""
    frames: list[DecodedFrame] = []
    for side, target in enumerate((position, position + offset)):
        if target >= media.details[side].duration - 0.001:
            target = frame_time(media, side, target, 0, runner)
        png = runner.run([
            str(media.ffmpeg), "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info",
            "-copyts", "-start_at_zero", "-ss", f"{target:.6f}", "-i", str(media.paths[side]),
            "-map", "0:v:0", "-vf", "scale=round(iw*sar):ih,setsar=1,showinfo=checksum=0",
            "-frames:v", "1", "-an", "-sn", "-fps_mode", "passthrough",
            "-f", "image2pipe", "-c:v", "png", "pipe:1",
        ])
        match = re.search(rb"\bpts_time:([-+\d.eE]+)", runner.stderr)
        if not png or match is None:
            raise ValueError("No timestamped preview frame")
        frames.append(DecodedFrame(float(match.group(1)), png))
    return FramePair(frames[0], frames[1], offset, position)
