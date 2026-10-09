from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from core.i18n import get_translator
from core.models import (
    BackendChoice,
    CodecChoice,
    CompressionMode,
    EncodeOptions,
    EncodePlanItem,
    EncodeResult,
    EncoderInfo,
    MediaInfo,
)
from gui.queue_manager import QueueManager, QueueRunCompletion
from gui.queue_model import QueueTableModel
from gui.queue_state import QueueItemRecord, QueueItemStatus, QueueJobSnapshot


def _record(root: Path, name: str, status: QueueItemStatus) -> QueueItemRecord:
    source = root / f"{name}.mov"
    source.write_bytes(b"source")
    output = root / f"{name}.mp4"
    item = EncodePlanItem(
        source_path=source,
        output_path=output,
        media_info=MediaInfo(
            path=source,
            duration=10.0,
            format_bitrate_bps=2_000_000,
            video_bitrate_bps=1_800_000,
            audio_bitrate_bps=128_000,
            width=1280,
            height=720,
            fps=30.0,
            video_codec="h264",
            audio_codec="aac",
        ),
        encoder_info=EncoderInfo(
            codec=CodecChoice.HEVC,
            backend=BackendChoice.CPU,
            encoder_name="libx265",
            supports_two_pass=True,
            default_preset="slow",
        ),
        options=EncodeOptions(compression_mode=CompressionMode.SMART),
    )
    result = None
    if status in {
        QueueItemStatus.DONE,
        QueueItemStatus.FAILED,
        QueueItemStatus.SKIPPED,
        QueueItemStatus.CANCELLED,
        QueueItemStatus.NEEDS_DECISION,
    }:
        result = EncodeResult(
            source_path=source,
            output_path=output,
            success=status == QueueItemStatus.DONE,
            skipped=status == QueueItemStatus.SKIPPED,
            needs_decision=status == QueueItemStatus.NEEDS_DECISION,
        )
    return QueueItemRecord(
        item_id=name,
        plan_item=item,
        job_snapshot=QueueJobSnapshot(root / "work", root / "ffmpeg", root / "ffprobe", root),
        status=status,
        total_passes=1,
        result=result,
    )


class QueueRunCompletionTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.repo_root = Path(__file__).resolve().parent.parent

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.model = QueueTableModel(get_translator("en", self.repo_root / "config"))
        self.manager = QueueManager(self.model)

    def test_historical_decision_does_not_block_current_run_completion(self) -> None:
        historical = _record(self.root, "historical", QueueItemStatus.NEEDS_DECISION)
        current = _record(self.root, "current", QueueItemStatus.FAILED)
        self.model.add_records([historical, current])
        completion = QueueRunCompletion("run-current", (current.item_id,))
        self.manager._pending_run = completion
        emitted: list[QueueRunCompletion] = []
        self.manager.runCompleted.connect(emitted.append)

        self.manager._reconcile_pending_run()

        self.assertEqual(emitted, [completion])

    def test_current_decision_delays_completion_and_emits_once(self) -> None:
        current = _record(self.root, "decision", QueueItemStatus.NEEDS_DECISION)
        self.model.add_records([current])
        completion = QueueRunCompletion("run-decision", (current.item_id,))
        self.manager._pending_run = completion
        emitted: list[QueueRunCompletion] = []
        states: list[str] = []
        self.manager.runCompleted.connect(emitted.append)
        self.manager.stateChanged.connect(states.append)

        self.manager._reconcile_pending_run()
        self.assertEqual(emitted, [])
        self.assertEqual(states[-1], "awaiting_decision")

        current.status = QueueItemStatus.DONE
        current.result = EncodeResult(
            source_path=current.source_path,
            output_path=current.output_path,
            success=True,
        )
        self.manager.reconcile_after_decision()
        self.manager.reconcile_after_decision()

        self.assertEqual(emitted, [completion])

    def test_execution_stopped_is_emitted_even_with_pending_decisions(self) -> None:
        record = _record(self.root, "decision", QueueItemStatus.NEEDS_DECISION)
        self.model.add_records([record])
        completion = QueueRunCompletion("pending", (record.item_id,))
        self.manager._pending_run = completion
        self.manager._worker_outcome = "finished"
        stopped = []
        completed = []
        self.manager.executionStopped.connect(stopped.append)
        self.manager.runCompleted.connect(completed.append)
        self.manager._on_worker_thread_finished()
        self.assertEqual(stopped, [completion])
        self.assertEqual(completed, [])


    def test_resume_includes_new_queue_items_in_the_pending_run(self) -> None:
        retry = _record(self.root, "retry", QueueItemStatus.WAITING_ANALYSIS)
        newly_added = _record(self.root, "new", QueueItemStatus.WAITING_ANALYSIS)
        self.model.add_records([retry, newly_added])
        completion = QueueRunCompletion("run-retry", (retry.item_id,))
        self.manager._pending_run = completion

        self.manager._reconcile_pending_run()
        self.assertIs(self.manager._pending_run, completion)

        with patch("gui.queue_manager.QueueExecuteWorker") as worker_class:
            self.assertTrue(self.manager.start(max_workers=2))

        execution_items = worker_class.call_args.args[0]
        self.assertEqual(
            [item.item_id for item in execution_items],
            [retry.item_id, newly_added.item_id],
        )
        pending = self.manager._pending_run
        assert pending is not None
        self.assertEqual(pending.run_id, completion.run_id)
        self.assertEqual(pending.item_ids, (retry.item_id, newly_added.item_id))
        self.manager._worker = None

    def test_resume_after_decision_runs_ready_items_even_when_another_decision_remains(self) -> None:
        ready = _record(self.root, "ready", QueueItemStatus.WAITING_ANALYSIS)
        waiting = _record(self.root, "waiting", QueueItemStatus.NEEDS_DECISION)
        self.model.add_records([ready, waiting])
        completion = QueueRunCompletion("run-mixed", (ready.item_id, waiting.item_id))
        self.manager._pending_run = completion
        with patch("gui.queue_manager.QueueExecuteWorker") as worker_class:
            self.assertTrue(self.manager.resume_after_decision(2))
        execution_items = worker_class.call_args.args[0]
        self.assertEqual([item.item_id for item in execution_items], [ready.item_id])
        self.manager._worker = None

    def test_pending_terminal_items_can_be_removed_but_decisions_stay_protected(self) -> None:
        done = _record(self.root, "done", QueueItemStatus.DONE)
        decision = _record(self.root, "decision", QueueItemStatus.NEEDS_DECISION)
        historical = _record(self.root, "historical-done", QueueItemStatus.DONE)
        self.model.add_records([done, decision, historical])
        self.manager._pending_run = QueueRunCompletion(
            "run-protected", (done.item_id, decision.item_id)
        )

        self.assertTrue(self.manager.can_remove_rows([0]))
        self.assertFalse(self.manager.can_remove_rows([1]))
        self.assertEqual(self.manager.remove_rows([0, 1]), 1)
        self.assertEqual(self.manager.clear_completed(), 1)
        self.assertEqual(
            [record.item_id for record in self.model.records()],
            [decision.item_id],
        )

    def test_abandon_run_clears_pending_and_keeps_item_statuses(self) -> None:
        queued = _record(self.root, "queued", QueueItemStatus.QUEUED)
        decision = _record(self.root, "decision", QueueItemStatus.NEEDS_DECISION)
        self.model.add_records([queued, decision])
        self.manager._pending_run = QueueRunCompletion(
            "run-abandon", (queued.item_id, decision.item_id)
        )
        states: list[str] = []
        self.manager.stateChanged.connect(states.append)

        self.assertFalse(self.manager.can_remove_rows([1]))
        self.assertTrue(self.manager.abandon_run())

        self.assertFalse(self.manager.has_pending_run())
        self.assertEqual(states, ["idle"])
        self.assertTrue(self.manager.can_remove_rows([1]))
        self.assertEqual(queued.status, QueueItemStatus.QUEUED)
        self.assertEqual(decision.status, QueueItemStatus.NEEDS_DECISION)
        self.assertFalse(self.manager.abandon_run())

    def test_completion_waits_for_worker_thread_finished(self) -> None:
        current = _record(self.root, "thread", QueueItemStatus.DONE)
        self.model.add_records([current])
        completion = QueueRunCompletion("run-thread", (current.item_id,))
        self.manager._pending_run = completion
        self.manager._worker = object()  # type: ignore[assignment]
        emitted: list[QueueRunCompletion] = []
        busy: list[bool] = []
        self.manager.runCompleted.connect(emitted.append)
        self.manager.busyChanged.connect(busy.append)

        self.manager._on_worker_queue_finished()
        self.assertTrue(self.manager.is_busy())
        self.assertEqual(emitted, [])

        self.manager._on_worker_thread_finished()

        self.assertFalse(self.manager.is_busy())
        self.assertEqual(busy, [False])
        self.assertEqual(emitted, [completion])


if __name__ == "__main__":
    unittest.main(verbosity=2)
