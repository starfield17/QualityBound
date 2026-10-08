from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from core.models import MediaInfo, OperationCancelledError
from core.ffmpeg.subprocess import noninteractive_run_kwargs, terminate_process
from core.media.metadata import infer_bit_depth_from_pix_fmt


# A hung ffprobe (dead network mount, stalled device) must not freeze planning
# forever; 60 s is far above the sub-second cost of reading a normal header.
DEFAULT_PROBE_TIMEOUT_SEC = 60.0
_PROBE_POLL_SEC = 0.25


def _run_command(
    cmd: list[str],
    *,
    timeout_sec: float | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> subprocess.CompletedProcess[str]:
    effective_timeout = DEFAULT_PROBE_TIMEOUT_SEC if timeout_sec is None else timeout_sec
    # Redirect to temp files instead of PIPE so a child that writes more than the
    # pipe buffer cannot deadlock against us while we wait for it.
    with (
        tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as stdout_file,
        tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as stderr_file,
    ):
        proc = subprocess.Popen(
            cmd,
            stdout=stdout_file,
            stderr=stderr_file,
            **noninteractive_run_kwargs(),
        )
        deadline = time.monotonic() + effective_timeout
        try:
            while True:
                try:
                    proc.wait(timeout=_PROBE_POLL_SEC)
                    break
                except subprocess.TimeoutExpired:
                    if cancel_check is not None and cancel_check():
                        raise OperationCancelledError("ffprobe was cancelled.") from None
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            f"ffprobe timed out after {effective_timeout:.0f}s: {cmd[-1]}"
                        ) from None
        finally:
            if proc.poll() is None:
                terminate_process(proc)
        returncode = proc.wait()
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read()
        stderr = stderr_file.read()
    if returncode != 0:
        raise subprocess.CalledProcessError(returncode, cmd, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


def _parse_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_int(value: Any) -> int:
    try:
        if value is None:
            return 0
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _guess_fps(stream: dict[str, Any]) -> float | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key)
        if not raw or raw in ("0/0", "N/A"):
            continue
        if "/" in str(raw):
            num, den = str(raw).split("/", 1)
            try:
                den_value = float(den)
                if den_value == 0:
                    continue
                return float(num) / den_value
            except ValueError:
                continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def ffprobe_json(
    ffprobe_path: Path,
    input_path: Path,
    *,
    timeout_sec: float | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    cmd = [
        str(ffprobe_path),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(input_path),
    ]
    proc = _run_command(cmd, timeout_sec=timeout_sec, cancel_check=cancel_check)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe did not return valid JSON for: {input_path}") from exc


def probe_media_info(
    ffprobe_path: Path,
    input_path: Path,
    *,
    timeout_sec: float | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> MediaInfo:
    data = ffprobe_json(
        ffprobe_path,
        input_path,
        timeout_sec=timeout_sec,
        cancel_check=cancel_check,
    )
    streams = data.get("streams", [])
    fmt = data.get("format", {}) or {}

    video_stream = next((item for item in streams if item.get("codec_type") == "video"), None)
    if not video_stream:
        raise RuntimeError(f"No video stream found in: {input_path}")

    audio_streams = [item for item in streams if item.get("codec_type") == "audio"]
    first_audio = audio_streams[0] if audio_streams else None

    duration = _parse_float(fmt.get("duration")) or _parse_float(video_stream.get("duration")) or 0.0
    if duration <= 0:
        raise RuntimeError(f"Cannot determine media duration for: {input_path}")

    format_bitrate_bps = _parse_int(fmt.get("bit_rate"))
    if format_bitrate_bps <= 0:
        # Some containers (e.g. raw .265, certain MKV) omit the format-level
        # bitrate. Estimate it from total file size / duration as a fallback.
        format_bitrate_bps = max(1, int(round(input_path.stat().st_size * 8 / duration)))

    video_bitrate_bps = _parse_int(video_stream.get("bit_rate"))
    audio_bitrate_bps = sum(_parse_int(item.get("bit_rate")) for item in audio_streams)

    if video_bitrate_bps <= 0:
        # Video stream metadata may lack a bitrate field. Subtract known audio
        # bitrates from the format total first; if that fails, assume video
        # consumes roughly 85 % of the format bitrate.
        estimated = format_bitrate_bps - audio_bitrate_bps
        if estimated > 0:
            video_bitrate_bps = estimated
        else:
            video_bitrate_bps = max(300_000, int(round(format_bitrate_bps * 0.85)))

    width = video_stream.get("width")
    height = video_stream.get("height")
    fps = _guess_fps(video_stream)
    video_codec = str(video_stream.get("codec_name") or "unknown")
    audio_codec = str(first_audio.get("codec_name")) if first_audio else None
    pix_fmt = str(video_stream.get("pix_fmt")) if video_stream.get("pix_fmt") else None
    raw_bit_depth = _parse_int(video_stream.get("bits_per_raw_sample"))
    bit_depth = raw_bit_depth if raw_bit_depth > 0 else infer_bit_depth_from_pix_fmt(pix_fmt)

    return MediaInfo(
        path=input_path,
        duration=duration,
        format_bitrate_bps=format_bitrate_bps,
        video_bitrate_bps=video_bitrate_bps,
        audio_bitrate_bps=audio_bitrate_bps,
        width=width if isinstance(width, int) else None,
        height=height if isinstance(height, int) else None,
        fps=fps,
        video_codec=video_codec,
        audio_codec=audio_codec,
        audio_stream_count=len(audio_streams),
        pix_fmt=pix_fmt,
        bit_depth=bit_depth,
        color_transfer=str(video_stream.get("color_transfer")) if video_stream.get("color_transfer") else None,
    )
