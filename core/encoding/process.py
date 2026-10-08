"""FFmpeg process lifecycle, cancellation, logging, and progress parsing."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable, TextIO, cast

from core.ffmpeg.subprocess import hidden_popen_kwargs, terminate_process
from core.models import OperationCancelledError
from core.progress_events import ProgressCallback, ProgressEvent

# Shared inside core/encoding only; the underscore stays because these are not
# part of the public core interface. Listing them makes that contract explicit
# so sibling-module imports are checked rather than assumed.
__all__ = (
    "_emit",
    "_emit_progress",
    "_run_logged_command",
    "_cleanup_passlog",
)


def _emit(log_callback: Callable[[str], None] | None, message: str) -> None:
    if log_callback is not None:
        log_callback(message)


def _emit_progress(progress_callback: ProgressCallback | None, **event: object) -> None:
    if progress_callback is not None:
        progress_callback(cast(ProgressEvent, event))


def _parse_time_to_seconds(raw: str) -> float | None:
    try:
        hours, minutes, seconds = raw.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError:
        return None


def _parse_ffmpeg_progress(line: str, duration_sec: float | None) -> dict[str, object] | None:
    if "time=" not in line and "speed=" not in line:
        return None

    match_time = re.search(r"time=(\d+:\d+:\d+(?:\.\d+)?)", line)
    match_speed = re.search(r"speed=\s*([0-9.]+x)", line)
    match_frame = re.search(r"frame=\s*(\d+)", line)
    elapsed_sec = _parse_time_to_seconds(match_time.group(1)) if match_time else None
    percent = None
    if duration_sec and elapsed_sec is not None and duration_sec > 0:
        percent = max(0.0, min(100.0, (elapsed_sec / duration_sec) * 100.0))
    event: dict[str, object] = {
        "state": "running",
        "elapsed_sec": elapsed_sec,
        "percent": percent,
        "speed": match_speed.group(1) if match_speed else "",
        "frame": int(match_frame.group(1)) if match_frame else None,
    }
    return event


def _cancel_process(proc: subprocess.Popen[str]) -> None:
    # Try graceful termination first, then force-kill if the process stays alive.
    terminate_process(proc)


def _close_process_pipes(proc: subprocess.Popen[str]) -> None:
    # Close the streams we opened so long sessions do not accumulate handles
    # when a run was cancelled before Python's GC got to them.
    for stream in (proc.stdout, proc.stdin):
        if stream is None:
            continue
        try:
            stream.close()
        except OSError:
            pass


def _cancel_requested(cancel_check: Callable[[], bool] | None) -> bool:
    return cancel_check is not None and cancel_check()


def _stop_running_command(
    proc: subprocess.Popen[str],
    log_callback: Callable[[str], None] | None,
    progress_callback: ProgressCallback | None,
    progress_context: dict[str, object] | None,
) -> None:
    message = "Cancellation requested. Stopping ffmpeg..."
    _emit(log_callback, message)
    _emit_progress(
        progress_callback,
        category="status",
        state="cancelling",
        message=message,
        **(progress_context or {}),
    )
    _cancel_process(proc)
    raise OperationCancelledError("Operation cancelled.")


def _emit_command_line(
    log_file: TextIO,
    cmd: list[str],
    log_callback: Callable[[str], None] | None,
    progress_callback: ProgressCallback | None,
    progress_context: dict[str, object] | None,
) -> None:
    command_line = "$ " + " ".join(cmd)
    log_file.write(command_line + "\n")
    log_file.flush()
    _emit(log_callback, command_line)
    _emit_progress(
        progress_callback,
        category="command",
        message=command_line,
        **(progress_context or {}),
    )


def _start_command_process(cmd: list[str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **hidden_popen_kwargs(),
    )


def _apply_pass_progress(
    event: dict[str, object],
    progress_context: dict[str, object] | None,
) -> dict[str, object]:
    if not progress_context:
        return event
    current_pass_index = progress_context.get("current_pass_index")
    total_passes = progress_context.get("total_passes")
    if not isinstance(current_pass_index, int) or not isinstance(total_passes, int) or total_passes <= 0:
        return event
    raw_percent = event.get("percent")
    pass_percent = float(raw_percent) if isinstance(raw_percent, (int, float)) else 0.0
    file_progress = (((current_pass_index - 1) + (pass_percent / 100.0)) / total_passes) * 100.0
    event["pass_percent"] = pass_percent
    event["file_progress"] = max(0.0, min(100.0, file_progress))
    event["percent"] = event["file_progress"]
    return event


def _emit_output_event(
    normalized: str,
    progress_callback: ProgressCallback | None,
    progress_context: dict[str, object] | None,
) -> None:
    raw_duration = progress_context.get("duration_sec") if progress_context else None
    duration_sec = float(raw_duration) if isinstance(raw_duration, (int, float)) else None
    parsed = _parse_ffmpeg_progress(normalized, duration_sec)
    if parsed is None:
        _emit_progress(progress_callback, category="log", message=normalized, **(progress_context or {}))
        return
    event = _apply_pass_progress(dict(parsed), progress_context)
    _emit_progress(
        progress_callback,
        category="ffmpeg",
        message=normalized,
        **(progress_context or {}),
        **event,
    )


def _handle_output_line(
    line: str,
    log_file: TextIO,
    output_chunks: list[str],
    log_callback: Callable[[str], None] | None,
    progress_callback: ProgressCallback | None,
    progress_context: dict[str, object] | None,
) -> None:
    normalized = line.rstrip("\r\n")
    output_chunks.append(line)
    log_file.write(line)
    log_file.flush()
    if not normalized:
        return
    _emit(log_callback, normalized)
    _emit_output_event(normalized, progress_callback, progress_context)


def _run_logged_command(
    cmd: list[str],
    log_path: Path,
    log_callback: Callable[[str], None] | None = None,
    progress_callback: ProgressCallback | None = None,
    cancel_check: Callable[[], bool] | None = None,
    process_callback: Callable[[subprocess.Popen[str] | None], None] | None = None,
    progress_context: dict[str, object] | None = None,
) -> subprocess.CompletedProcess[str]:
    with log_path.open("a", encoding="utf-8") as log_file:
        _emit_command_line(log_file, cmd, log_callback, progress_callback, progress_context)
        proc = _start_command_process(cmd)
        if process_callback is not None:
            process_callback(proc)
        output_chunks: list[str] = []
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                if _cancel_requested(cancel_check):
                    _stop_running_command(proc, log_callback, progress_callback, progress_context)
                _handle_output_line(line, log_file, output_chunks, log_callback, progress_callback, progress_context)
            return_code = proc.wait()
            log_file.write("\n")
            log_file.flush()
        finally:
            if process_callback is not None:
                process_callback(None)
            _close_process_pipes(proc)
    stdout_text = "".join(output_chunks)
    if _cancel_requested(cancel_check):
        raise OperationCancelledError("Operation cancelled.")
    if return_code != 0:
        raise subprocess.CalledProcessError(return_code, cmd, output=stdout_text)
    return subprocess.CompletedProcess(cmd, return_code, stdout=stdout_text, stderr="")


def _cleanup_passlog(passlog: Path | None) -> None:
    if not passlog:
        return
    for candidate in passlog.parent.glob(passlog.name + "*"):
        try:
            candidate.unlink()
        except OSError:
            pass
