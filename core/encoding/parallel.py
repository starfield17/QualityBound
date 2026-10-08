"""Concurrent queue execution over already-bound plan items."""

from __future__ import annotations

import copy
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Callable, Sequence

from core.encoding.analysis import run_analysis_phase
from core.encoding.executor import execute_plan_item
from core.media.validation import validate_workdir
from core.models import (
    CompressionMode,
    ConstraintPolicy,
    EncodePlan,
    EncodePlanItem,
    EncodeResult,
    OperationCancelledError,
)
from core.progress_events import ProgressCallback


ProgressContext = dict[str, object]
ProcessCallback = Callable[[str, subprocess.Popen[str] | None], None]
ItemStartedCallback = Callable[[int, str, str], None]
ItemResultCallback = Callable[[int, EncodeResult], None]


def _validated_worker_count(max_workers: object) -> int:
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= 8:
        raise ValueError("Concurrent encode workers must be an integer from 1 to 8.")
    return max_workers


def _clone_bound_items(items: Sequence[EncodePlanItem]) -> list[EncodePlanItem]:
    cloned = [copy.deepcopy(item) for item in items]
    for item in cloned:
        if item.skip_reason is None and item.encoder_info is None:
            raise ValueError(f"Concurrent encoding requires a bound encoder: {item.source_path}")
    return cloned


def _context_for_item(
    contexts: Sequence[ProgressContext] | None,
    index: int,
    item: EncodePlanItem,
) -> ProgressContext:
    context = dict(contexts[index]) if contexts and index < len(contexts) else {}
    encoder = item.encoder_info
    if encoder is not None:
        context["queue_backend"] = encoder.backend.value
        context["queue_encoder"] = encoder.encoder_name
    return context


def _first_exception(exceptions: list[BaseException]) -> BaseException | None:
    return exceptions[0] if exceptions else None


def execute_plan_concurrent(
    plan: EncodePlan,
    workdir: Path,
    *,
    max_workers: int,
    log_callback: Callable[[str], None] | None = None,
    progress_callback: ProgressCallback | None = None,
    cancel_check: Callable[[], bool] | None = None,
    process_callback: ProcessCallback | None = None,
    item_contexts: Sequence[ProgressContext] | None = None,
    pause_check: Callable[[], bool] | None = None,
    item_started_callback: ItemStartedCallback | None = None,
    item_result_callback: ItemResultCallback | None = None,
    constraint_policy: ConstraintPolicy | None = None,
) -> list[EncodeResult]:
    """Analyze Smart items first, then encode ready items concurrently.

    Every item is cloned once and retains the encoder chosen during planning.
    Workers dynamically claim ready items and never share a mutable plan item.
    """

    workdir = validate_workdir(workdir)
    configured_workers = _validated_worker_count(max_workers)
    items = _clone_bound_items(plan.items)
    total = len(items)
    contexts = [_context_for_item(item_contexts, index, item) for index, item in enumerate(items)]

    def started(index: int) -> None:
        if item_started_callback is None:
            return
        item = items[index]
        encoder = item.encoder_info
        item_started_callback(
            index,
            encoder.backend.value if encoder is not None else item.options.backend.value,
            encoder.encoder_name if encoder is not None else "",
        )

    if log_callback is not None:
        log_callback("Concurrent execution started; Smart analysis runs before full encoding.")
    results = run_analysis_phase(
        plan.ffmpeg_path,
        items,
        workdir,
        log_callback=log_callback,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
        process_callback=process_callback,
        item_contexts=contexts,
        pause_check=pause_check,
        item_started_callback=started,
        item_result_callback=item_result_callback,
        constraint_policy=constraint_policy,
    )
    if pause_check is not None and pause_check():
        if log_callback is not None:
            log_callback("Concurrent encode execution paused after analysis.")
        if progress_callback is not None:
            progress_callback(
                {"stage": "encode", "state": "paused", "parallel": False, "percent": None}
            )
        return [result for result in results if result is not None]

    pending = deque((index, item) for index, item in enumerate(items) if results[index] is None)
    worker_count = min(configured_workers, len(pending))
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
                index, item = pending.popleft()
            try:
                if item.options.compression_mode != CompressionMode.SMART:
                    started(index)
                callback = None
                if process_callback is not None:
                    bound_process_callback: ProcessCallback = process_callback

                    def slot_process(
                        proc: subprocess.Popen[str] | None,
                        worker_slot: str = slot,
                        callback_sink: ProcessCallback = bound_process_callback,
                    ) -> None:
                        callback_sink(worker_slot, proc)

                    callback = slot_process
                result = execute_plan_item(
                    plan.ffmpeg_path,
                    item,
                    workdir,
                    queue_index=index + 1,
                    queue_total=total,
                    log_callback=log_callback,
                    progress_callback=progress_callback,
                    cancel_check=should_stop,
                    process_callback=callback,
                    extra_progress_context=contexts[index],
                    constraint_policy=constraint_policy,
                    smart_analysis_validated=item.options.compression_mode == CompressionMode.SMART,
                )
                results[index] = result
                if item_result_callback is not None:
                    item_result_callback(index, result)
            except BaseException as exc:
                with lock:
                    exceptions.append(exc)
                stop_event.set()
                return

    if worker_count > 0:
        if log_callback is not None:
            log_callback(f"Encode phase started with {worker_count} concurrent worker(s).")
        if progress_callback is not None:
            progress_callback(
                {
                    "stage": "encode",
                    "state": "started",
                    "parallel": worker_count > 1,
                    "worker_count": worker_count,
                    "percent": 0.0,
                }
            )
    elif log_callback is not None:
        log_callback("Encode phase has no pending items left to encode.")

    threads = [
        threading.Thread(target=worker, args=(f"encode-{index + 1}",), daemon=True)
        for index in range(worker_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    if cancel_check is not None and cancel_check():
        raise OperationCancelledError("Encoding cancelled.")
    first_error = _first_exception(exceptions)
    if first_error is not None:
        raise first_error

    ordered_results = [result for result in results if result is not None]
    paused = pause_check is not None and pause_check() and len(ordered_results) < total
    if progress_callback is not None:
        progress_callback(
            {
                "stage": "encode",
                "state": "paused" if paused else "finished",
                "parallel": worker_count > 1,
                "worker_count": worker_count,
                "percent": 100.0 if not paused else None,
            }
        )
    if log_callback is not None:
        log_callback("Concurrent encode execution paused." if paused else "Concurrent encode execution finished.")
    return ordered_results
