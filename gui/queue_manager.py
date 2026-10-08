from __future__ import annotations

import copy
import threading
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from core.encoding import execute_plan, execute_plan_concurrent
from core.models import EncodePlan, EncodeResult, OperationCancelledError
from core.progress_events import ProgressEvent
from gui.queue_state import ACTIVE_ITEM_STATUSES, RUNNABLE_ITEM_STATUSES, QueueItemRecord, QueueItemStatus, create_queue_records
from gui.queue_model import QueueTableModel


@dataclass(slots=True)
class QueueExecutionItem:
    item_id: str
    record: QueueItemRecord


@dataclass(frozen=True, slots=True)
class QueueRunCompletion:
    run_id: str
    item_ids: tuple[str, ...]


class QueueExecuteWorker(QThread):
    log = Signal(str)
    progress = Signal(object)
    item_started = Signal(str, str, str)
    item_finished = Signal(str, object)
    paused = Signal()
    cancelled = Signal(str)
    failed = Signal(str)
    queue_finished = Signal()

    def __init__(
        self,
        items: list[QueueExecutionItem],
        max_workers: int,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.items = items
        self.max_workers = max_workers
        self._cancel_event = threading.Event()
        self._pause_after_current_event = threading.Event()
        self._current_processes: dict[str, subprocess.Popen[str]] = {}
        self._process_lock = threading.Lock()

    def _emit_log(self, message: str) -> None:
        self.log.emit(message)

    def _emit_progress(self, event: ProgressEvent) -> None:
        self.progress.emit(event)

    def _set_current_process(self, slot: str, proc: subprocess.Popen[str] | None) -> None:
        with self._process_lock:
            if proc is None:
                self._current_processes.pop(slot, None)
                return
            self._current_processes[slot] = proc

    def cancel(self) -> None:
        self._cancel_event.set()
        with self._process_lock:
            processes = list(self._current_processes.values())
        for proc in processes:
            try:
                proc.terminate()
            except Exception:
                pass

    def pause_after_current(self) -> None:
        self._pause_after_current_event.set()

    def _build_plan(self) -> EncodePlan:
        first_record = self.items[0].record
        return EncodePlan(
            items=[copy.deepcopy(item.record.plan_item) for item in self.items],
            ffmpeg_path=first_record.job_snapshot.ffmpeg_path,
            ffprobe_path=first_record.job_snapshot.ffprobe_path,
            input_root=first_record.source_path.parent,
            output_root=first_record.job_snapshot.output_root,
        )

    def run(self) -> None:
        try:
            if self.max_workers > 1:
                plan = self._build_plan()
                index_to_item_id = [item.item_id for item in self.items]
                results = execute_plan_concurrent(
                    plan,
                    self.items[0].record.job_snapshot.workdir,
                    max_workers=self.max_workers,
                    log_callback=self._emit_log,
                    progress_callback=self._emit_progress,
                    cancel_check=self._cancel_event.is_set,
                    pause_check=self._pause_after_current_event.is_set,
                    process_callback=self._set_current_process,
                    item_contexts=[{"queue_item_id": item.item_id} for item in self.items],
                    item_started_callback=lambda index, backend, encoder: self.item_started.emit(
                        index_to_item_id[index], backend, encoder
                    ),
                    item_result_callback=lambda index, result: self.item_finished.emit(index_to_item_id[index], result),
                )
                if self._pause_after_current_event.is_set() and len(results) < len(self.items):
                    self.paused.emit()
                    return
                self.queue_finished.emit()
                return
            plan = self._build_plan()
            index_to_item_id = [item.item_id for item in self.items]

            def started(index: int) -> None:
                item = self.items[index]
                encoder = plan.items[index].encoder_info or item.record.plan_item.encoder_info
                backend_name = encoder.backend.value if encoder else item.record.plan_item.options.backend.value
                encoder_name = encoder.encoder_name if encoder else "n/a"
                self.item_started.emit(item.item_id, backend_name, encoder_name)

            def finished(index: int, result: EncodeResult) -> None:
                self.item_finished.emit(index_to_item_id[index], result)

            results = execute_plan(
                plan,
                self.items[0].record.job_snapshot.workdir,
                log_callback=self._emit_log,
                progress_callback=self._emit_progress,
                cancel_check=self._cancel_event.is_set,
                process_callback=lambda proc: self._set_current_process("serial", proc),
                pause_check=self._pause_after_current_event.is_set,
                item_started_callback=started,
                item_result_callback=finished,
                extra_progress_contexts=[{"queue_item_id": item.item_id} for item in self.items],
            )
            if self._pause_after_current_event.is_set() and len(results) < len(self.items):
                self.paused.emit()
                return
            self.queue_finished.emit()
        except OperationCancelledError as exc:
            self.cancelled.emit(str(exc))
        except Exception as exc:
            self.failed.emit(str(exc))


class QueueManager(QObject):
    log = Signal(str)
    progress = Signal(object)
    busyChanged = Signal(bool)
    stateChanged = Signal(str)
    error = Signal(str)
    workerFinished = Signal()
    runCompleted = Signal(object)

    def __init__(self, model: QueueTableModel, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.model = model
        self._worker: QueueExecuteWorker | None = None
        self._active_item_ids: set[str] = set()
        self._pause_after_current_requested = False
        self._pending_run: QueueRunCompletion | None = None
        self._last_completed_run_id: str | None = None
        self._worker_outcome: str | None = None

    def is_busy(self) -> bool:
        return self._worker is not None

    def add_plan(self, plan: EncodePlan, workdir: Path) -> int:
        records = create_queue_records(plan, workdir)
        self.model.add_records(records)
        return len(records)

    def start(self, max_workers: int = 1) -> bool:
        if self._worker is not None:
            return False
        execution_records = self.model.execution_records()
        if not execution_records:
            return False

        items = [QueueExecutionItem(item_id=record.item_id, record=record) for record in execution_records]
        item_ids = tuple(item.item_id for item in items)
        if self._pending_run is None:
            self._pending_run = QueueRunCompletion(
                run_id=uuid.uuid4().hex,
                item_ids=item_ids,
            )
        else:
            known = set(self._pending_run.item_ids)
            extra = tuple(item_id for item_id in item_ids if item_id not in known)
            if extra:
                # Fold newly queued items into the current batch so a resumed or
                # decided run does not silently skip work added while it waited.
                self._pending_run = QueueRunCompletion(
                    run_id=self._pending_run.run_id,
                    item_ids=self._pending_run.item_ids + extra,
                )
        self.model.prepare_for_execution([item.item_id for item in items])
        self._pause_after_current_requested = False
        self._worker_outcome = None
        self._worker = QueueExecuteWorker(items, max_workers)
        self._worker.log.connect(self.log.emit)
        self._worker.progress.connect(self._on_worker_progress)
        self._worker.item_started.connect(self._on_item_started)
        self._worker.item_finished.connect(self._on_item_finished)
        self._worker.paused.connect(self._on_worker_paused)
        self._worker.cancelled.connect(self._on_worker_cancelled)
        self._worker.failed.connect(self._on_worker_failed)
        self._worker.queue_finished.connect(self._on_worker_queue_finished)
        self._worker.finished.connect(self._on_worker_thread_finished)
        self.busyChanged.emit(True)
        self.stateChanged.emit("running")
        self._worker.start()
        return True

    def pause_after_current(self) -> bool:
        if self._worker is None or self._pause_after_current_requested:
            return False
        self._pause_after_current_requested = True
        self._worker.pause_after_current()
        self.stateChanged.emit("pause_after_current")
        return True

    def stop(self) -> bool:
        if self._worker is None:
            return False
        self._worker.cancel()
        return True

    def remove_rows(self, rows: list[int]) -> int:
        targets = [
            row
            for row in rows
            if (record := self.model.record_for_row(row)) is not None and self._is_removable(record)
        ]
        if not targets:
            return 0
        return self.model.remove_rows_by_index(targets)

    def _pending_item_ids(self) -> set[str]:
        return set(self._pending_run.item_ids) if self._pending_run is not None else set()

    def _is_removable(self, record: QueueItemRecord) -> bool:
        # Active items belong to the worker, and an unresolved size miss owns a
        # preserved output that must be accepted, retried, or discarded first.
        if record.status in ACTIVE_ITEM_STATUSES:
            return False
        if record.status == QueueItemStatus.NEEDS_DECISION and record.item_id in self._pending_item_ids():
            return False
        return True

    def can_remove_rows(self, rows: list[int]) -> bool:
        return any(
            (record := self.model.record_for_row(row)) is not None and self._is_removable(record)
            for row in rows
        )

    def retry_rows(self, rows: list[int]) -> int:
        return self.model.retry_rows(rows)

    def clear_completed(self) -> int:
        protected_decisions = {
            record.item_id
            for record in self._pending_records()
            if record.status == QueueItemStatus.NEEDS_DECISION
        }
        return self.model.clear_completed(excluded_item_ids=protected_decisions)

    def has_pending_run(self) -> bool:
        return self._pending_run is not None

    def abandon_run(self) -> bool:
        """Drop the in-flight batch without touching item statuses or files.

        Used when a run is paused or waiting for decisions and no worker owns
        it anymore, so the user can clean up or start a fresh batch.
        """

        if self._worker is not None or self._pending_run is None:
            return False
        self._pending_run = None
        self._pause_after_current_requested = False
        self.stateChanged.emit("idle")
        return True

    def _on_item_started(self, item_id: str, backend: str, encoder: str) -> None:
        self._active_item_ids.add(item_id)
        self.model.assign_backend(item_id, backend, encoder)
        self.model.mark_running(item_id)

    def _on_worker_progress(self, event: ProgressEvent) -> None:
        self.model.apply_progress_event(event)
        self.progress.emit(event)

    def _on_item_finished(self, item_id: str, result: EncodeResult) -> None:
        self._active_item_ids.discard(item_id)
        self.model.apply_result(item_id, result)
        for warning in result.external_subtitle_warnings:
            self.log.emit(warning)

    def _on_worker_paused(self) -> None:
        self._worker_outcome = "paused"
        self.stateChanged.emit("paused")

    def _on_worker_cancelled(self, message: str) -> None:
        for record in self._pending_records():
            if record.status in {
                QueueItemStatus.QUEUED,
                QueueItemStatus.WAITING_ANALYSIS,
                QueueItemStatus.RUNNING,
                QueueItemStatus.ANALYZING,
                QueueItemStatus.ENCODING,
                QueueItemStatus.VALIDATING,
            }:
                self.model.mark_cancelled(record.item_id, message)
            self._active_item_ids.discard(record.item_id)
        self._pending_run = None
        self._worker_outcome = "cancelled"
        self.stateChanged.emit("cancelled")

    def _on_worker_failed(self, message: str) -> None:
        for record in self._pending_records():
            if record.status in {
                QueueItemStatus.QUEUED,
                QueueItemStatus.WAITING_ANALYSIS,
                QueueItemStatus.RUNNING,
                QueueItemStatus.ANALYZING,
                QueueItemStatus.ENCODING,
                QueueItemStatus.VALIDATING,
            }:
                self.model.mark_failed(record.item_id, message)
            self._active_item_ids.discard(record.item_id)
        self._pending_run = None
        self._worker_outcome = "failed"
        self.stateChanged.emit("failed")
        self.error.emit(message)

    def _on_worker_queue_finished(self) -> None:
        self._worker_outcome = "finished"

    def reconcile_after_decision(self) -> None:
        """Publish the terminal queue state after a local decision is applied."""
        if self._worker is not None:
            return
        self._reconcile_pending_run()

    def resume_after_decision(self, max_workers: int) -> bool:
        """Resume the pending run when a decision made work ready again."""
        if self._worker is not None or self._pending_run is None:
            return False
        records = self._pending_records()
        if not any(record.status in RUNNABLE_ITEM_STATUSES for record in records):
            self._reconcile_pending_run()
            return False
        return self.start(max_workers=max_workers)

    def _pending_records(self) -> list[QueueItemRecord]:
        if self._pending_run is None:
            return []
        records: list[QueueItemRecord] = []
        for item_id in self._pending_run.item_ids:
            _row, record = self.model.record_for_id(item_id)
            if record is not None:
                records.append(record)
        return records

    def _reconcile_pending_run(self) -> None:
        completion = self._pending_run
        if completion is None:
            self.stateChanged.emit("idle")
            return
        records = self._pending_records()
        if any(record.status == QueueItemStatus.NEEDS_DECISION for record in records):
            self.stateChanged.emit("awaiting_decision")
            return
        if any(record.status in RUNNABLE_ITEM_STATUSES for record in records):
            self.stateChanged.emit("idle")
            return
        self._pending_run = None
        self.stateChanged.emit("idle")
        if completion.run_id != self._last_completed_run_id:
            self._last_completed_run_id = completion.run_id
            self.runCompleted.emit(completion)

    def _on_worker_thread_finished(self) -> None:
        outcome = self._worker_outcome
        self._worker = None
        self._active_item_ids.clear()
        self._pause_after_current_requested = False
        self._worker_outcome = None
        self.busyChanged.emit(False)
        if outcome == "finished":
            self._reconcile_pending_run()
        self.workerFinished.emit()
