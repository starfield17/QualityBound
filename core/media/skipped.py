from __future__ import annotations

import os
import ctypes
import sys
import stat
import shutil
import uuid
from typing import Callable
from dataclasses import dataclass
from pathlib import Path

from core.media.subtitles import copy_external_subtitles
from core.models import EncodePlanItem, EncodeResult, SkipOrigin, SkippedOutputPolicy, SkippedOutputOutcome, OperationCancelledError
from core.media.paths import ensure_dir


@dataclass(frozen=True, slots=True)
class SkippedPublishResult:
    source_path: Path
    output_path: Path
    copied: bool
    reason: str | None = None


def _publish_without_overwrite(temporary: Path, destination: Path) -> None:
    """Atomic exclusive rename on the three supported desktop platforms.

    Renaming also works on removable filesystems without hard-link support.
    """
    if os.name == "nt":
        os.rename(temporary, destination)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        status = rename(os.fsencode(temporary), os.fsencode(destination), 0x4)  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        status = rename(-100, os.fsencode(temporary), -100, os.fsencode(destination), 1)  # RENAME_NOREPLACE
    else:
        os.link(temporary, destination)
        temporary.unlink()
        return
    if status != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def is_eligible_skipped_item(item: EncodePlanItem, result: EncodeResult) -> bool:
    """Return whether Smart analysis intentionally skipped this source.

    Planning failures and discarded full-encode size misses are deliberately
    outside the skipped-output policy.
    """

    if not result.skipped or result.needs_decision:
        return False
    if result.skip_origin not in {
        SkipOrigin.SMART_ANALYSIS,
        SkipOrigin.SMART_ANALYSIS_DECISION,
        SkipOrigin.SMART_PREDICTED_OVERSIZE,
    }:
        return False
    if item.skip_reason:
        return False
    try:
        return item.source_path.is_file()
    except OSError:
        return False


def publish_skipped_source(
    item: EncodePlanItem, result: EncodeResult | None = None,
    *, cancel_check: Callable[[], bool] | None = None,
) -> SkippedPublishResult:
    """Copy through a temporary path; record a terminal result exactly once."""
    source, destination = item.source_path, item.output_path
    if result is not None and result.skipped_output_outcome is not None:
        return SkippedPublishResult(source, destination,
                                    result.skipped_output_outcome == SkippedOutputOutcome.COPIED,
                                    result.error_message)
    temporary: Path | None = None
    try:
        if not source.is_file():
            raise OSError("source is missing")
        if source.resolve() == destination.resolve():
            raise OSError("source and output are the same file")
        if destination.exists() and not item.options.overwrite:
            raise OSError("output exists")
        ensure_dir(destination.parent)
        temporary = destination.with_name(f".{destination.name}.copy-{uuid.uuid4().hex}.tmp")
        with source.open("rb") as reader, temporary.open("xb") as writer:
            while True:
                if cancel_check is not None and cancel_check():
                    raise OperationCancelledError("Source copy cancelled.")
                chunk = reader.read(1024 * 1024)
                if not chunk:
                    break
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        shutil.copystat(source, temporary)
        if cancel_check is not None and cancel_check():
            raise OperationCancelledError("Source copy cancelled.")
        if item.options.overwrite:
            os.replace(temporary, destination)
        else:
            # Atomic no-clobber publication, including an output appearing mid-copy.
            _publish_without_overwrite(temporary, destination)
        temporary = None
        if item.options.copy_external_subtitles:
            copied, warnings = copy_external_subtitles(source, destination, overwrite=item.options.overwrite)
            if result is not None:
                result.copied_external_subtitle_paths.extend(copied)
                result.external_subtitle_warnings.extend(warnings)
        published = SkippedPublishResult(source, destination, True)
    except OSError as exc:
        published = SkippedPublishResult(source, destination, False, str(exc))
    finally:
        if temporary is not None:
            if os.name == "nt" and temporary.exists():
                temporary.chmod(temporary.stat().st_mode | stat.S_IWRITE)
            temporary.unlink(missing_ok=True)
    if result is not None:
        result.skipped_output_outcome = SkippedOutputOutcome.COPIED if published.copied else SkippedOutputOutcome.FAILED
        if not published.copied:
            result.success = result.skipped = result.needs_decision = False
            result.error_message = f"Source copy failed: {published.reason}"
    return published


def publish_skipped_sources(
    pairs: list[tuple[EncodePlanItem, EncodeResult]],
) -> list[SkippedPublishResult]:
    published: list[SkippedPublishResult] = []
    for item, result in pairs:
        if is_eligible_skipped_item(item, result) and result.skipped_output_outcome is None:
            published.append(publish_skipped_source(item, result))
    return published


def group_skipped_output_pairs(
    pairs: list[tuple[EncodePlanItem, EncodeResult]],
) -> dict[SkippedOutputPolicy, list[tuple[EncodePlanItem, EncodeResult]]]:
    grouped = {policy: [] for policy in SkippedOutputPolicy}
    for item, result in pairs:
        if is_eligible_skipped_item(item, result) and result.skipped_output_outcome is None:
            grouped[item.options.skipped_output_policy].append((item, result))
    return grouped
