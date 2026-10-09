"""Smart-analysis policy and the analysis-first execution phase."""

from __future__ import annotations

import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Callable

from core.media.paths import log_file_path
from core.media.validation import validate_workdir
from core.models import (
    CompressionMode,
    ConstraintPolicy,
    EncodePlanItem,
    EncodeResult,
    OperationCancelledError,
    QualitySearchStatus,
    SmartAlgorithm,
)
from core.progress_events import ProgressCallback, ProgressEvent
from core.smart.v1.concurrency import analysis_concurrency_limit, analysis_slot
from core.smart.v1.bitrate import resolve_max_output_ratio
from core.smart.v1.decisions import (
    resolve_analysis_policy,
)
from core.smart.v1.workflow import analyze_quality

from .item_results import (
    _assert_quality_encoder_matches_item,
    _encode_progress_context,
    _skipped_encode_result,
)
from .process import _emit, _emit_progress
from .segmented import analyze_segmented_plan_item


__all__ = ["analyze_plan_item", "item_needs_smart_analysis", "run_analysis_phase", "_validate_prepared_analysis_results"]


def item_needs_smart_analysis(item: EncodePlanItem) -> bool:
    return item.skip_reason is None and item.options.compression_mode == CompressionMode.SMART


def _validate_prepared_analysis_results(items: list[EncodePlanItem], results: list[EncodeResult | None]) -> list[EncodeResult | None]:
    """Accept the terminal results produced for this exact, analyzed plan."""
    if len(results) != len(items):
        raise ValueError("Prepared analysis results do not match the plan.")
    for item, result in zip(items, results, strict=True):
        if result is not None and (result.source_path != item.source_path or result.output_path != item.output_path or result.success):
            raise ValueError("Prepared analysis result identity does not match its item.")
    return list(results)


def _record_effective_smart_options(result: EncodeResult, item: EncodePlanItem) -> None:
    result.effective_min_vmaf = float(item.options.min_vmaf)
    result.effective_max_output_ratio = resolve_max_output_ratio(
        item.options.codec,
        item.options.max_output_ratio,
    )


def analyze_plan_item(
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
    active_cpu_vmaf_jobs: int = 1,
) -> EncodeResult | None:
    """Run Smart analysis and attach an accepted result to ``item``.

    A terminal skip, unsupported outcome, or decision request is returned. A
    ready-to-encode item returns ``None`` with its selected bitrate populated.
    """
    workdir = validate_workdir(workdir)
    log_path = log_file_path(workdir, item.source_path, "encode")
    base_context = _encode_progress_context(item, queue_index, queue_total, extra_progress_context)
    if item.skip_reason:
        return _skipped_encode_result(
            item, log_path, base_context, queue_index, queue_total, log_callback, progress_callback
        )
    if item.options.compression_mode != CompressionMode.SMART:
        return None
    if item.options.smart_algorithm == SmartAlgorithm.V2_EXPERIMENTAL:
        with analysis_slot(cancel_check):
            return analyze_segmented_plan_item(
                ffmpeg_path, item, workdir, queue_index=queue_index, queue_total=queue_total,
                log_callback=log_callback, progress_callback=progress_callback, cancel_check=cancel_check,
                process_callback=process_callback, extra_progress_context=extra_progress_context,
                constraint_policy=constraint_policy, active_cpu_vmaf_jobs=active_cpu_vmaf_jobs,
            )

    result = EncodeResult(
        source_path=item.source_path,
        output_path=item.output_path,
        success=True,
        log_path=log_path,
    )
    _emit(log_callback, f"[{queue_index}/{queue_total}] Waiting for smart analysis: {item.source_path.name}")
    _emit_progress(
        progress_callback,
        state="waiting_analysis",
        percent=0.0,
        file_progress=0.0,
        **base_context,
    )
    with analysis_slot(cancel_check):
        _emit(log_callback, f"[{queue_index}/{queue_total}] Smart analysis started: {item.source_path.name}")

        def analysis_progress(event: ProgressEvent) -> None:
            _emit_progress(progress_callback, **{**base_context, **event})

        quality_result = analyze_quality(
            ffmpeg_path,
            item,
            workdir,
            log_path,
            progress_callback=analysis_progress,
            cancel_check=cancel_check,
            process_callback=process_callback,
        )

    item.quality_search_result = quality_result
    result.quality_search_result = quality_result
    _assert_quality_encoder_matches_item(item, quality_result)
    quality_result, skip_origin = resolve_analysis_policy(ffmpeg_path, item, quality_result, constraint_policy)
    item.quality_search_result = quality_result
    result.quality_search_result = quality_result
    if not quality_result.success:
        result.success = False
        result.error_message = quality_result.reason or "Smart compression constraints could not be satisfied."
        result.skipped = skip_origin is not None
        result.skip_origin = skip_origin
        result.needs_decision = quality_result.status == QualitySearchStatus.CONSTRAINT_UNSATISFIED and not result.skipped
        _record_effective_smart_options(result, item)
        progress_state = "needs_decision" if result.needs_decision else ("skipped" if result.skipped else "failed")
        outcome = "requires a decision" if result.needs_decision else ("skipped" if result.skipped else "failed")
        _emit(
            log_callback,
            f"[{queue_index}/{queue_total}] Smart analysis {outcome} "
            f"for {item.source_path.name}: {result.error_message}",
        )
        _emit_progress(
            progress_callback,
            state=progress_state,
            percent=100.0,
            file_progress=100.0,
            message=result.error_message,
            quality_search_result=quality_result,
            **base_context,
        )
        return result

    item.target_video_bitrate_bps = quality_result.selected_video_bitrate_bps
    _emit_progress(
        progress_callback,
        state="analysis_finished",
        quality_search_result=quality_result,
        target_video_bitrate_bps=item.target_video_bitrate_bps,
        **base_context,
    )
    return None


def run_analysis_phase(
    ffmpeg_path: Path,
    items: list[EncodePlanItem],
    workdir: Path,
    *,
    log_callback: Callable[[str], None] | None = None,
    progress_callback: ProgressCallback | None = None,
    cancel_check: Callable[[], bool] | None = None,
    process_callback: Callable[[str, subprocess.Popen[str] | None], None] | None = None,
    item_contexts: list[dict[str, object]] | None = None,
    pause_check: Callable[[], bool] | None = None,
    item_started_callback: Callable[[int], None] | None = None,
    item_result_callback: Callable[[int, EncodeResult], None] | None = None,
    constraint_policy: ConstraintPolicy | None = None,
) -> list[EncodeResult | None]:
    """Analyze every Smart item before any encode begins."""
    results: list[EncodeResult | None] = [None] * len(items)
    pending = deque(
        index for index, item in enumerate(items) if item_needs_smart_analysis(item) or item.skip_reason
    )
    if not pending:
        return results
    workers = max(1, min(analysis_concurrency_limit(), len(pending)))
    lock = threading.Lock()
    stop_event = threading.Event()
    exceptions: list[BaseException] = []

    def should_stop() -> bool:
        return stop_event.is_set() or (cancel_check is not None and cancel_check())

    def worker(slot: str) -> None:
        while not should_stop():
            if pause_check is not None and pause_check():
                return
            with lock:
                if not pending:
                    return
                index = pending.popleft()
            item = items[index]
            context = dict(item_contexts[index]) if item_contexts and index < len(item_contexts) else {}
            if item_started_callback is not None:
                item_started_callback(index)

            def slot_process(proc: subprocess.Popen[str] | None, worker_slot: str = slot) -> None:
                if process_callback is not None:
                    process_callback(worker_slot, proc)

            try:
                terminal = analyze_plan_item(
                    ffmpeg_path,
                    item,
                    workdir,
                    queue_index=index + 1,
                    queue_total=len(items),
                    log_callback=log_callback,
                    progress_callback=progress_callback,
                    cancel_check=should_stop,
                    process_callback=slot_process if process_callback is not None else None,
                    active_cpu_vmaf_jobs=workers,
                    extra_progress_context=context,
                    constraint_policy=constraint_policy,
                )
            except BaseException as exc:
                with lock:
                    exceptions.append(exc)
                stop_event.set()
                return
            if terminal is not None:
                results[index] = terminal
                if item_result_callback is not None:
                    item_result_callback(index, terminal)

    _emit(log_callback, f"Smart analysis phase started ({len(pending)} file(s), concurrency={workers}).")
    _emit_progress(progress_callback, stage="analysis", state="started", parallel=workers > 1, percent=0.0)
    threads = [
        threading.Thread(target=worker, args=(f"analysis-{slot}",), daemon=True)
        for slot in range(workers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if cancel_check is not None and cancel_check():
        raise OperationCancelledError("Smart analysis cancelled.")
    if exceptions:
        raise exceptions[0]
    _emit(log_callback, "Smart analysis phase finished.")
    _emit_progress(progress_callback, stage="analysis", state="finished", parallel=workers > 1, percent=100.0)
    return results
