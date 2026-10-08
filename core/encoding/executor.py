"""Serial encode execution and validated Smart-output publication."""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path
from typing import Callable

from core.ffmpeg.commands import build_encode_commands
from core.media.paths import log_file_path
from core.media.validation import validate_workdir
from core.models import (
    CompressionMode,
    ConstraintPolicy,
    EncodePlan,
    EncodePlanItem,
    EncodeResult,
    OperationCancelledError,
    SmartAlgorithm,
)
from core.progress_events import ProgressCallback
from core.smart.v1.bitrate import resolve_max_output_ratio

from .analysis import analyze_plan_item, run_analysis_phase
from .item_results import (
    _assert_quality_encoder_matches_item,
    _copy_external_subtitles_for_result,
    _encode_progress_context,
    _size_miss_output_path,
    _skipped_encode_result,
    _write_command_failure_log,
)
from .process import _cleanup_passlog, _emit, _emit_progress, _run_logged_command
from .segmented import execute_segmented_item


def execute_plan_item(
    ffmpeg_path: Path,
    item: EncodePlanItem,
    workdir: Path,
    *,
    queue_index: int = 1,
    queue_total: int = 1,
    log_callback: Callable[[str], None] | None = None,
    progress_callback: ProgressCallback | None = None,
    cancel_check: Callable[[], bool] | None = None,
    process_callback: Callable[[subprocess.Popen[str] | None], None] | None = None,
    extra_progress_context: dict[str, object] | None = None,
    constraint_policy: ConstraintPolicy | None = None,
    smart_analysis_validated: bool = False,
) -> EncodeResult:
    workdir = validate_workdir(workdir)
    log_path = log_file_path(workdir, item.source_path, "encode")
    base_context = _encode_progress_context(item, queue_index, queue_total, extra_progress_context)
    if item.skip_reason:
        return _skipped_encode_result(
            item, log_path, base_context, queue_index, queue_total, log_callback, progress_callback
        )

    if item.options.compression_mode == CompressionMode.SMART and item.options.smart_algorithm == SmartAlgorithm.V2_EXPERIMENTAL:
        return execute_segmented_item(
            ffmpeg_path, item, workdir, queue_index=queue_index, queue_total=queue_total,
            log_callback=log_callback, progress_callback=progress_callback, cancel_check=cancel_check,
            process_callback=process_callback, extra_progress_context=extra_progress_context,
            constraint_policy=constraint_policy, smart_analysis_validated=smart_analysis_validated,
        )

    result = EncodeResult(
        source_path=item.source_path,
        output_path=item.output_path,
        success=True,
        log_path=log_path,
    )
    passlog: Path | None = None
    temporary_output: Path | None = None
    commands: list[list[str]] = []
    total_passes = 1
    current_pass_index = 1
    try:
        if item.options.compression_mode == CompressionMode.SMART:
            if smart_analysis_validated:
                quality_result = item.quality_search_result
                if quality_result is None or not quality_result.success:
                    raise RuntimeError("Validated Smart encoding is missing a successful analysis result.")
                _assert_quality_encoder_matches_item(item, quality_result)
                item.target_video_bitrate_bps = quality_result.selected_video_bitrate_bps
            else:
                terminal = analyze_plan_item(
                    ffmpeg_path,
                    item,
                    workdir,
                    queue_index=queue_index,
                    queue_total=queue_total,
                    log_callback=log_callback,
                    progress_callback=progress_callback,
                    cancel_check=cancel_check,
                    process_callback=process_callback,
                    extra_progress_context=extra_progress_context,
                    constraint_policy=constraint_policy,
                )
                if terminal is not None:
                    return terminal
            result.quality_search_result = item.quality_search_result
            result.effective_min_vmaf = float(item.options.min_vmaf)
            result.effective_max_output_ratio = resolve_max_output_ratio(
                item.options.codec,
                item.options.max_output_ratio,
            )
            temporary_output = item.output_path.parent / (
                f".{item.output_path.stem}.smart-{uuid.uuid4().hex}{item.output_path.suffix}"
            )

        item.output_path.parent.mkdir(parents=True, exist_ok=True)
        commands, passlog = build_encode_commands(
            ffmpeg_path,
            item,
            workdir,
            output_path=temporary_output,
        )
        result.commands = commands
        total_passes = max(len(commands), 1)
        _emit(log_callback, f"[{queue_index}/{queue_total}] Encoding {item.source_path.name} -> {item.output_path}")
        _emit_progress(
            progress_callback,
            state="starting_file",
            percent=0.0,
            pass_percent=0.0,
            file_progress=0.0,
            current_pass_index=1,
            total_passes=total_passes,
            **base_context,
        )
        for pass_index, cmd in enumerate(commands, start=1):
            current_pass_index = pass_index
            file_progress = ((pass_index - 1) / total_passes) * 100.0
            _emit_progress(
                progress_callback,
                state="running_pass",
                percent=file_progress,
                pass_percent=0.0,
                file_progress=file_progress,
                current_pass_index=pass_index,
                total_passes=total_passes,
                **base_context,
            )
            _run_logged_command(
                cmd,
                log_path,
                log_callback,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
                process_callback=process_callback,
                progress_context={
                    **base_context,
                    "current_pass_index": pass_index,
                    "total_passes": total_passes,
                },
            )
        if temporary_output is not None:
            if result.quality_search_result is not None:
                _assert_quality_encoder_matches_item(item, result.quality_search_result)
            _emit_progress(
                progress_callback,
                state="validating",
                percent=100.0,
                file_progress=100.0,
                current_pass_index=total_passes,
                total_passes=total_passes,
                **base_context,
            )
            max_output_bytes = int(
                item.source_path.stat().st_size
                * resolve_max_output_ratio(item.options.codec, item.options.max_output_ratio)
            )
            actual_size = temporary_output.stat().st_size
            if actual_size > max_output_bytes:
                result.success = False
                result.needs_decision = True
                result.actual_output_bytes = actual_size
                result.allowed_output_bytes = max_output_bytes
                rejected_output = _size_miss_output_path(item.output_path)
                os.replace(temporary_output, rejected_output)
                temporary_output = None
                result.rejected_output_path = rejected_output
                result.error_message = (
                    f"Actual output size {actual_size} bytes exceeds the smart limit "
                    f"of {max_output_bytes} bytes. The encoded file was preserved at {rejected_output}."
                )
                _emit(log_callback, f"[{queue_index}/{queue_total}] {result.error_message}")
                _emit_progress(
                    progress_callback,
                    state="needs_decision",
                    message=result.error_message,
                    quality_search_result=result.quality_search_result,
                    **base_context,
                )
                return result
            if item.output_path.exists() and not item.options.overwrite:
                raise FileExistsError(
                    f"Output appeared during encoding and overwrite is disabled: {item.output_path}"
                )
            os.replace(temporary_output, item.output_path)
            temporary_output = None
        _copy_external_subtitles_for_result(item, result, queue_index, queue_total, log_callback)
        _emit(log_callback, f"[{queue_index}/{queue_total}] Finished {item.source_path.name}")
        _emit_progress(
            progress_callback,
            state="finished_file",
            percent=100.0,
            pass_percent=100.0,
            file_progress=100.0,
            current_pass_index=total_passes,
            total_passes=total_passes,
            **base_context,
        )
        return result
    except OperationCancelledError:
        _emit_progress(
            progress_callback,
            state="cancelled_file",
            percent=None,
            current_pass_index=current_pass_index,
            total_passes=total_passes,
            **base_context,
        )
        raise
    except (subprocess.CalledProcessError, OSError, ValueError, RuntimeError) as exc:
        result.success = False
        if isinstance(exc, subprocess.CalledProcessError):
            result.return_code = exc.returncode
            result.error_message = exc.stderr or exc.stdout or str(exc)
            _write_command_failure_log(log_path, exc)
        else:
            result.return_code = 1
            result.error_message = str(exc)
        failure_activity = (
            f"[{queue_index}/{queue_total}] Failed {item.source_path.name} "
            f"(exit code {result.return_code}); see log: {log_path}"
        )
        _emit(log_callback, failure_activity)
        _emit_progress(
            progress_callback,
            state="failed_file",
            message=failure_activity,
            current_pass_index=total_passes,
            total_passes=total_passes,
            **base_context,
        )
        return result
    finally:
        _cleanup_passlog(passlog)
        if temporary_output is not None:
            try:
                temporary_output.unlink(missing_ok=True)
            except OSError:
                pass


def execute_plan(
    plan: EncodePlan,
    workdir: Path,
    log_callback: Callable[[str], None] | None = None,
    progress_callback: ProgressCallback | None = None,
    cancel_check: Callable[[], bool] | None = None,
    process_callback: Callable[[subprocess.Popen[str] | None], None] | None = None,
    constraint_policy: ConstraintPolicy | None = None,
    pause_check: Callable[[], bool] | None = None,
    item_started_callback: Callable[[int], None] | None = None,
    item_result_callback: Callable[[int, EncodeResult], None] | None = None,
    extra_progress_contexts: list[dict[str, object]] | None = None,
) -> list[EncodeResult]:
    workdir = validate_workdir(workdir)
    total = len(plan.items)

    def analysis_process(slot: str, proc: subprocess.Popen[str] | None) -> None:
        del slot
        if process_callback is not None:
            process_callback(proc)

    results = run_analysis_phase(
        plan.ffmpeg_path,
        plan.items,
        workdir,
        log_callback=log_callback,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
        process_callback=analysis_process if process_callback is not None else None,
        item_contexts=extra_progress_contexts,
        pause_check=pause_check,
        item_started_callback=item_started_callback,
        item_result_callback=item_result_callback,
        constraint_policy=constraint_policy,
    )
    if pause_check is not None and pause_check():
        return [result for result in results if result is not None]

    _emit(log_callback, "Encode phase started.")
    _emit_progress(progress_callback, stage="encode", state="started", percent=0.0)
    for index, item in enumerate(plan.items):
        if results[index] is not None:
            continue
        if cancel_check is not None and cancel_check():
            _emit(log_callback, "Encode execution cancelled by user.")
            _emit_progress(progress_callback, stage="encode", state="cancelled")
            raise OperationCancelledError("Encoding cancelled.")
        if pause_check is not None and pause_check():
            break
        if item_started_callback is not None and item.options.compression_mode != CompressionMode.SMART:
            item_started_callback(index)
        context = (
            extra_progress_contexts[index]
            if extra_progress_contexts and index < len(extra_progress_contexts)
            else None
        )
        encoded = execute_plan_item(
            plan.ffmpeg_path,
            item,
            workdir,
            queue_index=index + 1,
            queue_total=total,
            log_callback=log_callback,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
            process_callback=process_callback,
            extra_progress_context=context,
            constraint_policy=constraint_policy,
            smart_analysis_validated=item.options.compression_mode == CompressionMode.SMART,
        )
        results[index] = encoded
        if item_result_callback is not None:
            item_result_callback(index, encoded)
    completed = [result for result in results if result is not None]
    paused = pause_check is not None and pause_check() and len(completed) < total
    _emit(log_callback, "Encode execution paused." if paused else "Encode execution finished.")
    _emit_progress(
        progress_callback,
        stage="encode",
        state="paused" if paused else "finished",
        percent=100.0 if not paused else None,
    )
    return completed
