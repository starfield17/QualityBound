from __future__ import annotations

import hashlib
import re
from pathlib import Path

from core.models import CodecChoice, ContainerChoice


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def choose_output_root(input_path: Path, output_dir: Path | None, codec: CodecChoice) -> Path:
    # When no explicit output directory, place output next to input for single files,
    # or in a sibling "_compressed_<codec>" directory for whole directories.
    if output_dir:
        return output_dir.expanduser().resolve()
    input_path = input_path.expanduser().resolve()
    if input_path.is_file():
        return input_path.parent
    return (input_path.parent / f"{input_path.name}_compressed_{codec.value}").resolve()


def build_output_path(
    source_path: Path,
    input_root: Path,
    output_root: Path,
    codec: CodecChoice,
    container: ContainerChoice,
    *, create_directories: bool = True,
) -> Path:
    if input_root.is_dir():
        try:
            relative_parent = source_path.parent.relative_to(input_root)
        except ValueError:
            # Source is outside input_root; place directly in output_root with no subdirectory.
            relative_parent = Path()
    else:
        relative_parent = Path()
    destination_dir = output_root / relative_parent
    if create_directories:
        ensure_dir(destination_dir)
    return destination_dir / f"{source_path.stem}_{codec.value}.{container.value}"


def build_explicit_file_output_path(
    source_path: Path,
    relative_path: Path,
    output_dir: Path | None,
    codec: CodecChoice,
    container: ContainerChoice,
    *, create_directories: bool = True,
) -> Path:
    """Build an output path for an explicitly supplied file-list item."""

    if output_dir is None:
        destination_dir = source_path.parent
    else:
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(
                f"Explicit file relative path must stay below the output directory: {relative_path}"
            )
        output_root = output_dir.expanduser().resolve()
        destination_dir = (output_root / relative_path.parent).resolve()
        if not destination_dir.is_relative_to(output_root):
            raise ValueError(
                f"Explicit file relative path escapes the output directory: {relative_path}"
            )
    if create_directories:
        ensure_dir(destination_dir)
    return destination_dir / f"{source_path.stem}_{codec.value}.{container.value}"


def disambiguate_output_path(output_path: Path, source_path: Path) -> Path:
    """Add a stable source-derived suffix to a colliding output filename."""

    digest = hashlib.sha1(str(source_path.resolve()).encode("utf-8")).hexdigest()[:8]
    return output_path.with_name(f"{output_path.stem}-{digest}{output_path.suffix}")


def _safe_name(value: str) -> str:
    # Sanitize a string for use in a filename: replace unsafe chars with underscores,
    # strip leading/trailing dots and underscores, fall back to "item".
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "item"


def _source_token(source_path: Path) -> str:
    # Stable unique token for per-file temp directories and logs.
    # Uses a truncated SHA1 so two files with the same stem in different directories
    # get distinct tokens without path separators in filenames.
    digest = hashlib.sha1(str(source_path).encode("utf-8")).hexdigest()[:10]
    return f"{_safe_name(source_path.stem)}_{digest}"


def log_file_path(workdir: Path, source_path: Path, stage: str) -> Path:
    log_root = ensure_dir(workdir / "logs")
    return log_root / f"{_source_token(source_path)}_{stage}.log"


def passlog_prefix(workdir: Path, source_path: Path, stage: str) -> Path:
    temp_root = ensure_dir(workdir / "temp")
    return temp_root / f"{_source_token(source_path)}_{stage}.ffpass"
