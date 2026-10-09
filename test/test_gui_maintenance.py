from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
from PySide6.QtCore import QEventLoop, QTimer

from core.media import PostEncodeAction, SystemPowerResult
from core.models import (
    BackendChoice,
    CodecChoice,
    EncodeOptions,
    EncodePlanItem,
    EncodeResult,
    EncoderInfo,
    MediaInfo,
    ConstraintFailureKind,
    QualitySearchResult,
    QualitySearchStatus,
    SkipOrigin,
    SkippedOutputPolicy,
    SkippedOutputOutcome,
)
from gui.gui_mainwindow import MainWindow
from gui.queue_manager import QueueRunCompletion
from gui.queue_state import (
    QueueItemRecord,
    QueueItemStatus,
    QueueJobSnapshot,
    apply_progress_event,
)


class MainWindowMaintenanceTestCase(unittest.TestCase):
    def test_fresh_default_mixed_batch_prompts_then_copies_despite_cancelled_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("core.config.store.app_config_path", return_value=root / "app_config.json"):
                window = MainWindow(self.repo_root, language="en")
                window.app_config["desktop_notifications"] = False
                options = window.options_panel.read_options()
                self.assertEqual(options.size_blocked_policy.value, "relax_size")
                self.assertEqual(options.quality_unreachable_policy.value, "skip")
                self.assertEqual(options.skipped_output_policy.value, "copy")
                pending = []
                for kind in (ConstraintFailureKind.SIZE_BLOCKED, ConstraintFailureKind.QUALITY_UNREACHABLE):
                    record = self._record(root, kind.value, QueueItemStatus.NEEDS_DECISION)
                    record.result.needs_decision = True
                    record.plan_item.quality_search_result = QualitySearchResult(
                        encoder_name="libx265", backend=BackendChoice.CPU,
                        status=QualitySearchStatus.CONSTRAINT_UNSATISFIED,
                        failure_kind=kind, reason=kind.value,
                    )
                    pending.append(record)
                copied = self._record(root, "copy", QueueItemStatus.SKIPPED)
                asked = self._record(root, "ask", QueueItemStatus.SKIPPED)
                for record in (copied, asked):
                    record.result.skipped = True
                    record.result.skip_origin = SkipOrigin.SMART_PREDICTED_OVERSIZE
                asked.plan_item.options.skipped_output_policy = SkippedOutputPolicy.ASK
                records = pending + [copied, asked]
                window.queue_model.add_records(records)
                completion = QueueRunCompletion("mixed", tuple(record.item_id for record in records))
                window.queue_manager._pending_run = completion
                loop = QEventLoop()
                timer = QTimer()
                timer.setSingleShot(True)
                timer.timeout.connect(loop.quit)
                window.queue_manager.busyChanged.connect(lambda busy: loop.quit() if not busy else None)
                try:
                    with (
                        patch("gui.gui_mainwindow.choose_quality_decision", return_value=None) as choose,
                        patch("gui.queue_completion.QMessageBox.question", return_value=QMessageBox.StandardButton.No) as copy_ask,
                    ):
                        window.queue_manager._worker_outcome = "finished"
                        window.queue_manager._on_worker_thread_finished()
                        timer.start(5000)
                        loop.exec()
                        timer.stop()
                        self.assertFalse(window._postprocessing)
                        self.assertEqual(choose.call_count, 2)
                        self.assertEqual(copied.output_path.read_bytes(), copied.source_path.read_bytes())
                        self.assertEqual(copied.result.skipped_output_outcome, SkippedOutputOutcome.COPIED)
                        self.assertEqual(asked.result.skipped_output_outcome, SkippedOutputOutcome.IGNORED)
                        self.assertTrue(window.queue_manager.has_pending_run())
                        self.assertTrue(all(record.status == QueueItemStatus.NEEDS_DECISION for record in pending))
                        window.queue_manager._worker_outcome = "finished"
                        window.queue_manager._on_worker_thread_finished()
                        self.assertEqual(choose.call_count, 2)
                        copy_ask.assert_called_once()
                finally:
                    window.queue_manager.abandon_run()
                    window.close()

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.repo_root = Path(__file__).resolve().parent.parent

    def _record(
        self,
        root: Path,
        item_id: str,
        status: QueueItemStatus,
    ) -> QueueItemRecord:
        source = root / f"{item_id}.mov"
        source.write_bytes(b"source-data")
        output = root / f"{item_id}.mp4"
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
            options=EncodeOptions(overwrite=True),
        )
        result = EncodeResult(
            source_path=source,
            output_path=output,
            success=status == QueueItemStatus.DONE,
            actual_output_bytes=5,
        )
        return QueueItemRecord(
            item_id=item_id,
            plan_item=item,
            job_snapshot=QueueJobSnapshot(
                root / "work", root / "ffmpeg", root / "ffprobe", root
            ),
            status=status,
            total_passes=1,
            result=result,
        )

    def test_runtime_config_patch_preserves_worker_owned_and_unknown_fields(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        persisted: dict[str, object] = {}

        def update_config(updater):
            current = {
                "encoder_capabilities": {"source": "worker"},
                "future_config_field": "preserve-me",
            }
            updated = updater(current)
            persisted.update(updated if updated is not None else current)
            return Path("app_config.json")

        try:
            window.app_config["language"] = "zh_cn"
            window.app_config["encoder_capabilities"] = {"source": "stale-window"}
            window.app_config["future_config_field"] = "stale-window"
            with patch("gui.gui_mainwindow.update_app_config", side_effect=update_config):
                window._save_app_config_preserving_capabilities()
        finally:
            window.close()

        self.assertEqual(persisted["language"], "zh_cn")
        self.assertEqual(persisted["encoder_capabilities"], {"source": "worker"})
        self.assertEqual(persisted["future_config_field"], "preserve-me")

    def test_ui_builders_create_the_documented_composition_points(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            self.assertIs(window.centralWidget(), window.main_scroll_area)
            self.assertIsNotNone(window.source_box)
            self.assertIsNotNone(window.options_panel)
            self.assertIsNotNone(window.jobs_box)
            self.assertIsNotNone(window.statusBar())
        finally:
            window.close()

    def test_queue_progress_transition_is_qt_free(self) -> None:
        item = EncodePlanItem(
            source_path=Path("source.mp4"),
            output_path=Path("output.mp4"),
            media_info=None,
            encoder_info=None,
            options=EncodeOptions(),
        )
        record = QueueItemRecord(
            item_id="item-1",
            plan_item=item,
            job_snapshot=QueueJobSnapshot(
                Path("workdir"),
                Path("ffmpeg"),
                Path("ffprobe"),
                Path("output"),
            ),
            status=QueueItemStatus.WAITING_ANALYSIS,
            total_passes=1,
        )
        apply_progress_event(
            record,
            {
                "state": "analyzing",
                "candidate_index": 2,
                "candidate_limit": 4,
                "file_progress": 37.5,
            },
        )
        self.assertEqual(record.status, QueueItemStatus.ANALYZING)
        self.assertEqual(record.analysis_candidate_index, 2)
        self.assertEqual(record.analysis_candidate_limit, 4)
        self.assertEqual(record.file_progress, 37.5)

    def test_settings_post_action_updates_main_combo_before_persisting(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        dialog = MagicMock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.redetect_requested = False
        dialog.values.return_value = {
            "language": "en",
            "workdir_path": str(window.default_workdir),
            "ffmpeg_path": "",
            "ffprobe_path": "",
            "log_level": "info",
            "encode_workers": 1,
            "post_encode_action": PostEncodeAction.SLEEP.value,
            "desktop_notifications": False,
            "size_blocked_policy": "relax_size",
            "quality_unreachable_policy": "skip",
            "skipped_output_policy": "copy",
            "analysis_profile": "balance",
            "analysis_profiles": {},
        }
        try:
            with (
                patch("gui.gui_mainwindow.SettingsDialog", return_value=dialog),
                patch("gui.gui_mainwindow.update_app_config"),
            ):
                window._open_settings_dialog()

            self.assertEqual(
                window.post_encode_combo.currentData(), PostEncodeAction.SLEEP.value
            )
            self.assertEqual(
                window.app_config["post_encode_action"], PostEncodeAction.SLEEP.value
            )
        finally:
            window.close()

    def test_busy_queue_rejects_dropped_paths_before_planning(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            window.queue_busy = True
            with patch.object(window, "_start_plan_for_files") as start_plan:
                window._handle_dropped_paths([Path(__file__)])
            start_plan.assert_not_called()
        finally:
            window.queue_busy = False
            window.close()

    def test_run_completion_uses_only_the_current_run_records(self) -> None:
        import tempfile

        window = MainWindow(self.repo_root, language="en")
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                historical = self._record(root, "historical", QueueItemStatus.DONE)
                current = self._record(root, "current", QueueItemStatus.FAILED)
                window.queue_model.add_records([historical, current])
                completion = QueueRunCompletion("run", (current.item_id,))
                with patch.object(window.queue_completion_handler, "handle") as finish:
                    window._on_queue_run_completed(completion)

                finish.assert_called_once_with([current], window.translator, window.app_config)
        finally:
            window.close()

    def test_power_action_failure_is_logged_and_shown(self) -> None:
        import tempfile

        window = MainWindow(self.repo_root, language="en")
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                record = self._record(Path(temp_dir), "done", QueueItemStatus.DONE)
                window.app_config["desktop_notifications"] = False
                window.app_config["post_encode_action"] = PostEncodeAction.SLEEP.value
                dialog = MagicMock()
                dialog.exec.return_value = QDialog.DialogCode.Accepted
                failure = SystemPowerResult(
                    success=False,
                    action=PostEncodeAction.SLEEP,
                    error="permission denied",
                )
                with (
                    patch(
                        "gui.queue_completion.PowerActionCountdownDialog",
                        return_value=dialog,
                    ),
                    patch("gui.queue_completion.execute_power_action", return_value=failure),
                    patch("gui.queue_completion.QMessageBox.critical") as critical,
                    patch.object(window.queue_completion_handler, "_append_log") as append_log,
                ):
                    window.queue_completion_handler.handle([record], window.translator, window.app_config)

                critical.assert_called_once()
                self.assertIn("permission denied", critical.call_args.args[2])
                self.assertTrue(
                    any("permission denied" in str(call.args[0]) for call in append_log.call_args_list)
                )
        finally:
            window.close()


    def test_source_persistence_skips_nonexistent_history_and_redundant_writes(self) -> None:
        import tempfile

        window = MainWindow(self.repo_root, language="en")
        try:
            window.app_config["recent_paths"] = []
            window.app_config["last_source_path"] = ""
            window.source_combo.setEditText("/definitely/not/a/real/file.mov")
            with patch("gui.gui_mainwindow.update_app_config") as update:
                window._persist_runtime_state()
            update.assert_called_once()
            merged = update.call_args.args[0]({})
            self.assertEqual(merged["last_source_path"], "/definitely/not/a/real/file.mov")
            self.assertEqual(merged["recent_paths"], [])

            with tempfile.TemporaryDirectory() as temp_dir:
                source = Path(temp_dir) / "movie.mov"
                source.write_bytes(b"source")
                window.source_combo.setEditText(str(source))
                with patch("gui.gui_mainwindow.update_app_config") as update:
                    window._persist_runtime_state()
                update.assert_called_once()
                merged = update.call_args.args[0]({})
                self.assertEqual(merged["recent_paths"], [str(source)])

                with patch("gui.gui_mainwindow.update_app_config") as update:
                    window._persist_runtime_state()
                update.assert_not_called()
        finally:
            window.close()


    def test_queue_state_is_visible_and_translated(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            window.app_config["desktop_notifications"] = False
            window._on_queue_state_changed("awaiting_decision")
            self.assertEqual(
                window.queue_state_label.text(),
                window.translator.t("gui.queue_state.awaiting_decision"),
            )
            with patch("gui.gui_mainwindow.update_app_config"):
                window._language_changed("zh_cn")
            self.assertEqual(window.translator.language, "zh_cn")
            self.assertEqual(
                window.queue_state_label.text(),
                window.translator.t("gui.queue_state.awaiting_decision"),
            )
        finally:
            window.close()

    def test_stop_abandons_a_pending_run_when_no_worker_owns_it(self) -> None:
        import tempfile

        window = MainWindow(self.repo_root, language="en")
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                record = self._record(Path(temp_dir), "queued", QueueItemStatus.QUEUED)
                window.queue_model.add_records([record])
                window.queue_manager._pending_run = QueueRunCompletion("run", (record.item_id,))
                window._asked_analysis_decisions[("run", record.item_id)] = record.result
                window._stop_active_task()
                self.assertFalse(window.queue_manager.has_pending_run())
                self.assertFalse(window._asked_analysis_decisions)
        finally:
            window.close()

    def test_closing_after_execution_does_not_start_postprocessing(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            completion = QueueRunCompletion("closing", ())
            window.queue_manager._pending_run = completion
            window._close_after_running_task_stops = True
            with patch.object(window, "_process_stopped_records") as process:
                window._on_queue_execution_stopped(completion)
            process.assert_not_called()
            self.assertFalse(window.queue_manager.has_pending_run())
        finally:
            window._close_after_running_task_stops = False
            window.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
