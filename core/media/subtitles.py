from __future__ import annotations

import shutil
from pathlib import Path

from core.media.paths import ensure_dir


SIDECAR_SUBTITLE_EXTENSIONS = {
    ".srt",
    ".ass",
    ".ssa",
    ".vtt",
    ".sub",
    ".idx",
    ".sup",
    ".ttml",
    ".dfxp",
    ".smi",
    ".sami",
    ".usf",
}


def is_external_subtitle_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in SIDECAR_SUBTITLE_EXTENSIONS


def discover_external_subtitles(source_path: Path) -> list[Path]:
    # Sidecar matching: same stem (movie.srt for movie.mkv) or stem-dot-prefixed
    # (movie.en.srt for movie.mkv). Sorted case-insensitively for stable order.
    source_path = source_path.resolve()
    prefix = source_path.stem
    try:
        entries = sorted(source_path.parent.iterdir(), key=lambda item: item.name.lower())
    except OSError:
        # An unreadable source directory must not fail an otherwise good encode.
        return []
    matches: list[Path] = []
    for candidate in entries:
        if not is_external_subtitle_file(candidate):
            continue
        if candidate.stem == prefix or candidate.name.startswith(prefix + "."):
            matches.append(candidate.resolve())
    return matches


def build_external_subtitle_output_path(source_path: Path, subtitle_path: Path, output_path: Path) -> Path:
    # Remap subtitle filenames: movie.en.srt -> output.en.srt; plain movie.srt -> output.srt.
    subtitle_name = subtitle_path.name
    if subtitle_name.startswith(source_path.stem):
        suffix = subtitle_name[len(source_path.stem):]
        if suffix:
            return output_path.with_name(output_path.stem + suffix)
    return output_path.with_name(output_path.stem + subtitle_path.suffix)


def copy_external_subtitles(
    source_path: Path,
    output_path: Path,
    *,
    overwrite: bool,
) -> tuple[list[Path], list[str]]:
    copied_paths: list[Path] = []
    warnings: list[str] = []
    candidates = discover_external_subtitles(source_path)
    if not candidates:
        return copied_paths, warnings

    ensure_dir(output_path.parent)
    for subtitle_path in candidates:
        target_path = build_external_subtitle_output_path(source_path, subtitle_path, output_path)
        try:
            if target_path.exists() and not overwrite:
                warnings.append(f"External subtitle exists and overwrite is disabled: {target_path}")
                continue
            shutil.copy2(subtitle_path, target_path)
            copied_paths.append(target_path)
        except OSError as exc:
            warnings.append(f"Failed to copy external subtitle {subtitle_path.name}: {exc}")
    return copied_paths, warnings
