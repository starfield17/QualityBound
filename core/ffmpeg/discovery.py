from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Optional

from core.config.paths import app_root, bundle_root
from core.ffmpeg.subprocess import noninteractive_run_kwargs


PROJECT_FFMPEG_DIRNAME = "FFmpeg"
PROJECT_BINARY_SEARCH_DIRS = (
    Path(),
    Path("bin"),
    Path("current") / "bin",
)
COMMON_HOMEBREW_PREFIXES = (
    Path("/opt/homebrew"),
    Path("/usr/local"),
    Path("/home/linuxbrew/.linuxbrew"),
)


def is_windows() -> bool:
    return os.name == "nt"


def candidate_binary_names(binary_name: str) -> tuple[str, ...]:
    if is_windows():
        return (f"{binary_name}.exe", binary_name)
    return (binary_name,)


def _resolve_existing_file(candidate: Path) -> Optional[Path]:
    if candidate.exists() and candidate.is_file():
        return candidate.resolve()
    return None


def _find_binary_under(
    base_dirs: Iterable[Path],
    relative_dirs: Iterable[Path],
    binary_name: str,
) -> Optional[Path]:
    for base_dir in base_dirs:
        for relative_dir in relative_dirs:
            for name in candidate_binary_names(binary_name):
                resolved = _resolve_existing_file(base_dir / relative_dir / name)
                if resolved is not None:
                    return resolved
    return None


def project_ffmpeg_dirs() -> list[Path]:
    # Deduplicate because app_root and bundle_root can resolve to the same
    # directory (e.g. in a non-bundled dev run).
    candidates: list[Path] = []
    seen: set[Path] = set()
    for root in (app_root(), bundle_root()):
        directory = (root / PROJECT_FFMPEG_DIRNAME).resolve()
        if directory in seen:
            continue
        seen.add(directory)
        candidates.append(directory)
    return candidates


def detect_project_binary(binary_name: str) -> Optional[Path]:
    return _find_binary_under(project_ffmpeg_dirs(), PROJECT_BINARY_SEARCH_DIRS, binary_name)


def detect_path_binary(binary_name: str) -> Optional[Path]:
    for name in candidate_binary_names(binary_name):
        found = shutil.which(name)
        if found:
            return Path(found).resolve()
    return None


def detect_scoop_ffmpeg(binary_name: str) -> Optional[Path]:
    scoop = shutil.which("scoop")
    if not scoop:
        return None
    try:
        proc = subprocess.run(
            [scoop, "prefix", "ffmpeg"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **noninteractive_run_kwargs(),
        )
        if proc.returncode != 0:
            return None
        prefix = proc.stdout.strip().strip('"')
        if not prefix:
            return None
        return _find_binary_under((Path(prefix),), PROJECT_BINARY_SEARCH_DIRS, binary_name)
    except OSError:
        return None


def detect_homebrew_binary(binary_name: str) -> Optional[Path]:
    brew = shutil.which("brew")
    if brew:
        try:
            proc = subprocess.run(
                [brew, "--prefix", "ffmpeg"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                **noninteractive_run_kwargs(),
            )
            if proc.returncode == 0:
                prefix = proc.stdout.strip().strip('"')
                if prefix:
                    resolved = _find_binary_under((Path(prefix),), (Path("bin"),), binary_name)
                    if resolved is not None:
                        return resolved
        except OSError:
            pass

    return _find_binary_under(COMMON_HOMEBREW_PREFIXES, (Path("bin"),), binary_name)


def find_binary(user_path: Optional[str], binary_name: str) -> Path:
    # Discovery precedence: explicit user path > bundled project dir >
    # system PATH > platform package manager (Scoop on Windows, Homebrew
    # on macOS/Linux).
    if user_path:
        candidate = Path(user_path).expanduser()
        if candidate.exists() and candidate.is_file():
            return candidate.resolve()
        raise FileNotFoundError(f"Cannot find the specified {binary_name}: {candidate}")

    project_binary = detect_project_binary(binary_name)
    if project_binary is not None:
        return project_binary

    path_binary = detect_path_binary(binary_name)
    if path_binary is not None:
        return path_binary

    platform_binary = detect_scoop_ffmpeg(binary_name) if is_windows() else detect_homebrew_binary(binary_name)
    if platform_binary is not None:
        return platform_binary

    raise FileNotFoundError(
        f"Cannot find {binary_name}. Checked explicit path, project '{PROJECT_FFMPEG_DIRNAME}/', PATH, "
        "and platform-specific locations."
    )


def _find_sibling_binary(binary: Path, binary_name: str) -> Optional[Path]:
    # Prefer the tool shipped next to the one we already resolved (same build),
    # so an explicit ffmpeg is not silently paired with a different ffprobe.
    candidates = (binary.parent, binary.parent.parent)
    return _find_binary_under(candidates, (Path(), Path("bin")), binary_name)


def discover_ffmpeg_tools(
    ffmpeg_path: Optional[str] = None,
    ffprobe_path: Optional[str] = None,
) -> tuple[Path, Path]:
    ffmpeg = find_binary(ffmpeg_path, "ffmpeg")
    if ffprobe_path:
        return ffmpeg, find_binary(ffprobe_path, "ffprobe")
    sibling = _find_sibling_binary(ffmpeg, "ffprobe")
    if sibling is not None:
        return ffmpeg, sibling
    return ffmpeg, find_binary(None, "ffprobe")
