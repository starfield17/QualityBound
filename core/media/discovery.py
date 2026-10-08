from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from core.models import VideoFileItem


VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".wmv",
    ".flv",
    ".webm",
    ".m4v",
    ".ts",
    ".m2ts",
    ".mts",
    ".mpg",
    ".mpeg",
    ".3gp",
    ".ogv",
}


def _report_skip(on_error: Callable[[str], None] | None, message: str) -> None:
    if on_error is not None:
        on_error(message)


def _iter_video_paths(
    input_root: Path,
    recursive: bool,
    on_error: Callable[[str], None] | None,
) -> list[Path]:
    """Walk ``input_root`` for video files, reporting folders we cannot read.

    ``pathlib`` silently swallows permission errors during ``glob``, which
    turns an unreadable folder into a quietly incomplete batch. Walking with
    ``os.scandir`` lets each failure surface through ``on_error`` instead.
    """

    found: list[Path] = []
    pending: list[Path] = [input_root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as handle:
                entries = list(handle)
        except OSError as exc:
            _report_skip(on_error, f"Skipping unreadable folder: {directory} | {exc}")
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if recursive:
                        pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=True) and Path(entry.path).suffix.lower() in VIDEO_EXTENSIONS:
                    found.append(Path(entry.path))
            except OSError as exc:
                _report_skip(on_error, f"Skipping unreadable entry: {entry.path} | {exc}")
    return found


def collect_video_files(
    input_path: Path,
    recursive: bool,
    on_error: Callable[[str], None] | None = None,
) -> list[VideoFileItem]:
    input_path = input_path.expanduser().resolve()
    if input_path.is_file():
        if input_path.suffix.lower() not in VIDEO_EXTENSIONS:
            raise ValueError(f"Input file is not a supported video format: {input_path}")
        return [VideoFileItem(path=input_path, relative_path=Path(input_path.name))]

    if not input_path.is_dir():
        raise FileNotFoundError(f"Input path does not exist: {input_path}")

    try:
        with os.scandir(input_path):
            pass
    except OSError as exc:
        raise RuntimeError(f"Cannot read folder: {input_path} | {exc}") from exc

    files: list[VideoFileItem] = []
    for path in _iter_video_paths(input_path, recursive, on_error):
        try:
            resolved = path.resolve()
            files.append(VideoFileItem(path=resolved, relative_path=resolved.relative_to(input_path)))
        except (OSError, ValueError) as exc:
            _report_skip(on_error, f"Skipping unreadable file: {path} | {exc}")
    # Stable traversal order: sort by relative path case-insensitively.
    files.sort(key=lambda item: str(item.relative_path).lower())
    return files
