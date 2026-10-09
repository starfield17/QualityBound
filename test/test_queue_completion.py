from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from PySide6.QtCore import QEventLoop, QTimer

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from core.i18n import get_translator
from core.media import PostEncodeAction, SystemPowerResult
from core.models import EncodeOptions, EncodePlanItem, EncodeResult, SkipOrigin, SkippedOutputPolicy, SkippedOutputOutcome
from gui.queue_completion import QueueCompletionHandler
from gui.queue_state import QueueItemRecord, QueueItemStatus, QueueJobSnapshot


class QueueCompletionTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.config_dir = Path(__file__).resolve().parent.parent / "config"

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.logs: list[str] = []
        self.notify = Mock()
        self.close = Mock()
        self.handler = QueueCompletionHandler(None, self.logs.append, self.notify, self.close)
        self.tr = get_translator("en", self.config_dir)

    def _record(
        self,
        name: str,
        status: QueueItemStatus,
        policy: SkippedOutputPolicy = SkippedOutputPolicy.IGNORE,
    ) -> QueueItemRecord:
        source, output = self.root / f"{name}.mov", self.root / f"{name}.mp4"
        source.write_bytes(b"source-data")
        if status == QueueItemStatus.DONE:
            output.write_bytes(b"small")
        item = EncodePlanItem(
            source_path=source,
            output_path=output,
            media_info=None,
            encoder_info=None,
            options=EncodeOptions(skipped_output_policy=policy),
        )
        return QueueItemRecord(
            item_id=name,
            plan_item=item,
            status=status,
            total_passes=1,
            job_snapshot=QueueJobSnapshot(self.root, self.root / "ffmpeg", self.root / "ffprobe", self.root),
            result=EncodeResult(
                source_path=source,
                output_path=output,
                success=status == QueueItemStatus.DONE,
                skipped=status == QueueItemStatus.SKIPPED,
                skip_origin=SkipOrigin.SMART_ANALYSIS if status == QueueItemStatus.SKIPPED else None,
                needs_decision=status == QueueItemStatus.NEEDS_DECISION,
                actual_output_bytes=5 if status == QueueItemStatus.DONE else None,
            ),
        )

    def test_mixed_skip_policies_publish_only_eligible_and_confirmed_files(self) -> None:
        for answer in (QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No):
            with self.subTest(answer=answer):
                prefix = answer.name
                copied = self._record(f"{prefix}-copy", QueueItemStatus.SKIPPED, SkippedOutputPolicy.COPY)
                asked = self._record(f"{prefix}-ask", QueueItemStatus.SKIPPED, SkippedOutputPolicy.ASK)
                ignored = self._record(f"{prefix}-ignore", QueueItemStatus.SKIPPED)
                discarded = self._record(f"{prefix}-discard", QueueItemStatus.SKIPPED, SkippedOutputPolicy.COPY)
                discarded.result.skip_origin = SkipOrigin.SIZE_MISS_DISCARD
                with (
                    patch("gui.queue_completion.QMessageBox.question", return_value=answer) as question,
                    patch("gui.queue_completion.PowerActionCountdownDialog") as countdown,
                ):
                    self.handler.handle([copied, asked, ignored, discarded], self.tr, {})
                self.assertEqual(copied.output_path.read_bytes(), copied.source_path.read_bytes())
                self.assertEqual(asked.output_path.exists(), answer == QMessageBox.StandardButton.Yes)
                self.assertFalse(ignored.output_path.exists())
                self.assertFalse(discarded.output_path.exists())
                question.assert_called_once()
                self.assertIn(asked.source_path.name, question.call_args.args[2])
                self.assertNotIn(copied.source_path.name, question.call_args.args[2])
                countdown.assert_not_called()
        self.notify.assert_not_called()

    def test_notification_and_report_use_current_translator_and_settings(self) -> None:
        record = self._record("done", QueueItemStatus.DONE)
        self.handler.handle([record], self.tr, {"desktop_notifications": False})
        self.notify.assert_not_called()
        chinese = get_translator("zh_cn", self.config_dir)
        self.logs.clear()
        self.handler.handle([record], chinese, {"desktop_notifications": True, "language": "en"})
        self.assertEqual(self.logs[0], chinese.t("gui.log.encode_done"))
        self.assertIn(chinese.t("gui.report.batch_complete"), self.logs[1])
        self.notify.assert_called_once()
        self.assertEqual(self.notify.call_args.args[0], chinese.t("app.title"))

    def test_skipped_publication_is_not_repeated_when_run_resumes(self) -> None:
        record = self._record("copy-once", QueueItemStatus.SKIPPED, SkippedOutputPolicy.COPY)
        with patch("gui.queue_completion.publish_skipped_source", wraps=__import__(
            "core.media", fromlist=["publish_skipped_source"]
        ).publish_skipped_source) as publish:
            self.handler.handle([record], self.tr, {"desktop_notifications": False})
            self.handler.handle([record], self.tr, {"desktop_notifications": False})
        publish.assert_called_once()
        self.assertEqual(record.output_path.read_bytes(), record.source_path.read_bytes())


    def test_failed_run_reports_and_notifies_without_power_action(self) -> None:
        records = [
            self._record(status.value, status)
            for status in (
                QueueItemStatus.FAILED,
                QueueItemStatus.CANCELLED,
                QueueItemStatus.NEEDS_DECISION,
            )
        ]
        with (
            patch("gui.queue_completion.PowerActionCountdownDialog") as countdown,
            patch("gui.queue_completion.execute_power_action") as power,
        ):
            self.handler.handle(records, self.tr, {"post_encode_action": "shutdown"})
        self.notify.assert_called_once()
        self.assertEqual(self.notify.call_args.args[0], self.tr.t("app.title"))
        self.assertEqual(
            self.notify.call_args.args[1],
            self.tr.t("gui.notification.batch_incomplete", failed=1, cancelled=1),
        )
        self.assertIn(self.tr.t("gui.report.batch_incomplete"), "\n".join(self.logs))
        self.assertNotIn(self.tr.t("gui.report.batch_complete"), "\n".join(self.logs))
        self.close.assert_not_called()
        countdown.assert_not_called()
        power.assert_not_called()

    def test_stopped_mixed_run_copies_and_asks_without_waiting_for_decisions(self) -> None:
        copied = self._record("copy", QueueItemStatus.SKIPPED, SkippedOutputPolicy.COPY)
        asked = self._record("ask", QueueItemStatus.SKIPPED, SkippedOutputPolicy.ASK)
        ignored = self._record("ignore", QueueItemStatus.SKIPPED)
        pending = self._record("decision", QueueItemStatus.NEEDS_DECISION)
        records = [copied, asked, ignored, pending]
        completions = []
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        def apply_result(item_id, result):
            record = next(record for record in records if record.item_id == item_id)
            record.result = result
        def finished(cancelled):
            completions.append(cancelled)
            loop.quit()
        with patch("gui.queue_completion.QMessageBox.question", return_value=QMessageBox.StandardButton.No) as question:
            self.handler.process_stopped(records, self.tr, apply_result, finished)
            timer.start(5000)
            loop.exec()
            timer.stop()
            self.assertEqual(completions, [False])
            self.assertEqual(copied.output_path.read_bytes(), copied.source_path.read_bytes())
            self.assertEqual(copied.result.skipped_output_outcome, SkippedOutputOutcome.COPIED)
            self.assertEqual(asked.result.skipped_output_outcome, SkippedOutputOutcome.IGNORED)
            self.assertEqual(ignored.result.skipped_output_outcome, SkippedOutputOutcome.IGNORED)
            self.assertTrue(pending.result.needs_decision)
            self.handler.process_stopped(records, self.tr, apply_result, finished)
            self.assertEqual(completions, [False, False])
            question.assert_called_once()

    def test_power_actions_require_confirmation_and_quit_uses_close_callback(self) -> None:
        record = self._record("done", QueueItemStatus.DONE)
        for action in (PostEncodeAction.QUIT, PostEncodeAction.SLEEP, PostEncodeAction.SHUTDOWN):
            for accepted in (False, True):
                with self.subTest(action=action, accepted=accepted):
                    self.close.reset_mock()
                    with (
                        patch("gui.queue_completion.PowerActionCountdownDialog") as countdown,
                        patch(
                            "gui.queue_completion.execute_power_action", return_value=SystemPowerResult(True, action)
                        ) as power,
                    ):
                        countdown.return_value.exec.return_value = (
                            QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected
                        )
                        self.handler.handle(
                            [record],
                            self.tr,
                            {
                                "post_encode_action": action.value,
                                "desktop_notifications": False,
                            },
                        )
                    countdown.assert_called_once_with(self.tr, action, timeout_sec=30, parent=None)
                    if accepted and action == PostEncodeAction.QUIT:
                        self.close.assert_called_once_with()
                    else:
                        self.close.assert_not_called()
                    if accepted and action != PostEncodeAction.QUIT:
                        power.assert_called_once_with(action)
                    else:
                        power.assert_not_called()


if __name__ == "__main__":
    unittest.main()
