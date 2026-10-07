"""Result construction, encoding context, and publication helpers."""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Callable

from core.media.subtitles import copy_external_subtitles
from core.models import EncodePlanItem, EncodeResult, QualitySearchResult, SkipOrigin
from core.progress_events import ProgressCallback

from .process import _emit, _emit_progress

# Shared inside core/encoding only; the underscore stays because these are not
# part of the public core interface. Listing them makes that contract explicit
# so sibling-module imports are checked rather than assumed.
__all__ = (
    "_size_miss_output_path",
    "_assert_quality_encoder_matches_item",
    "_encode_progress_context",
    "_skipped_encode_result",
    "_copy_external_subtitles_for_result",
    "_write_command_failure_log",
)


def _size_miss_output_path(output_path: Path) -> Path:
    return output_path.with_name(
        f"{output_path.stem}.size-miss-{uuid.uuid4().hex[:8]}{output_path.suffix}"
    )


def _assert_quality_encoder_matches_item(
    item: EncodePlanItem,
    quality_result: QualitySearchResult,
) -> None:
    encoder = item.encoder_info
    if encoder is None:
        raise ValueError("Smart encoding requires a bound encoder.")
    if quality_result.encoder_name != encoder.encoder_name or quality_result.backend != encoder.backend:
        raise RuntimeError(
            "Smart analysis result was produced by a different encoder "
            f"({quality_result.encoder_name}/{quality_result.backend.value}); expected "
            f"{encoder.encoder_name}/{encoder.backend.value}."
        )


def _encode_progress_context(
    item: EncodePlanItem,
    queue_index: int,
    queue_total: int,
    extra_progress_context: dict[str, object] | None,
) -> dict[str, object]:
    base_context: dict[str, object] = {
        "stage": "encode",
        "file_name": item.source_path.name,
        "file_path": str(item.source_path),
        "output_path": str(item.output_path),
        "current": queue_index,
        "total": queue_total,
        "duration_sec": item.media_info.duration if item.media_info else None,
    }
    if extra_progress_context:
        base_context.update(extra_progress_context)
    return base_context


def _skipped_encode_result(
    item: EncodePlanItem,
    log_path: Path,
    base_context: dict[str, object],
    queue_index: int,
    queue_total: int,
    log_callback: Callable[[str], None] | None,
    progress_callback: ProgressCallback | None,
) -> EncodeResult:
    _emit(
        log_callback,
        f"[{queue_index}/{queue_total}] Skipping {item.source_path.name}: {item.skip_reason}",
    )
    _emit_progress(
        progress_callback,
        state="skipped",
        percent=100.0,
        pass_percent=100.0,
        file_progress=100.0,
        current_pass_index=0,
        total_passes=0,
        message=item.skip_reason,
        **base_context,
    )
    return EncodeResult(
        source_path=item.source_path,
        output_path=item.output_path,
        success=False,
        skipped=True,
        skip_origin=SkipOrigin.PLANNING,
        error_message=item.skip_reason,
        log_path=log_path,
    )


def _copy_external_subtitles_for_result(
    item: EncodePlanItem,
    result: EncodeResult,
    queue_index: int,
    queue_total: int,
    log_callback: Callable[[str], None] | None,
) -> None:
    if not item.options.copy_external_subtitles:
        return
    copied_paths, warnings = copy_external_subtitles(
        item.source_path,
        item.output_path,
        overwrite=item.options.overwrite,
    )
    result.copied_external_subtitle_paths.extend(copied_paths)
    result.external_subtitle_warnings.extend(warnings)
    for copied_path in copied_paths:
        _emit(log_callback, f"[{queue_index}/{queue_total}] Copied external subtitle -> {copied_path}")
    for warning in warnings:
        _emit(log_callback, f"[{queue_index}/{queue_total}] External subtitle warning: {warning}")


def _write_command_failure_log(log_path: Path, exc: subprocess.CalledProcessError) -> None:
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"[command failed] returncode={exc.returncode}\n")
        if exc.stdout:
            fh.write(exc.stdout + "\n")
        if exc.stderr:
            fh.write(exc.stderr + "\n")
