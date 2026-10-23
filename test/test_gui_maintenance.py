from __future__ import annotations

import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QApplication, QDialog, QMenu, QMessageBox
from PySide6.QtCore import QEventLoop, QPoint, QTimer

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
from gui.queue.manager import QueueRunCompletion
from gui.queue.state import (
    QueueItemRecord,
    QueueItemStatus,
    QueueJobSnapshot,
    apply_progress_event,
    QueueSourceDraft,
    create_draft_record,
)
from core.models import CompressionMode, VideoFileItem
from gui.activity_log_window import ActivityLogWindow


def result_of(record: QueueItemRecord) -> EncodeResult:
    """Return a fixture record's encode result; a record without one fails the test."""

    assert record.result is not None
    return record.result


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
                    result_of(record).needs_decision = True
                    record.bound_plan_item.quality_search_result = QualitySearchResult(
                        encoder_name="libx265", backend=BackendChoice.CPU,
                        status=QualitySearchStatus.CONSTRAINT_UNSATISFIED,
                        failure_kind=kind, reason=kind.value,
                    )
                    pending.append(record)
                copied = self._record(root, "copy", QueueItemStatus.SKIPPED)
                asked = self._record(root, "ask", QueueItemStatus.SKIPPED)
                for record in (copied, asked):
                    result_of(record).skipped = True
                    result_of(record).skip_origin = SkipOrigin.SMART_PREDICTED_OVERSIZE
                asked.bound_plan_item.options.skipped_output_policy = SkippedOutputPolicy.ASK
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
                        patch("gui.queue.completion.QMessageBox.question", return_value=QMessageBox.StandardButton.No) as copy_ask,
                    ):
                        window.queue_manager._worker_outcome = "finished"
                        window.queue_manager._on_worker_thread_finished()
                        timer.start(5000)
                        loop.exec()
                        timer.stop()
                        self.assertFalse(window._postprocessing)
                        self.assertEqual(choose.call_count, 1)
                        self.assertEqual(copied.output_path.read_bytes(), copied.source_path.read_bytes())
                        self.assertEqual(result_of(copied).skipped_output_outcome, SkippedOutputOutcome.COPIED)
                        self.assertEqual(result_of(asked).skipped_output_outcome, SkippedOutputOutcome.IGNORED)
                        self.assertTrue(window.queue_manager.has_pending_run())
                        self.assertTrue(all(record.status == QueueItemStatus.NEEDS_DECISION for record in pending))
                        window.queue_manager._worker_outcome = "finished"
                        window.queue_manager._on_worker_thread_finished()
                        self.assertEqual(choose.call_count, 2)
                        copy_ask.assert_called_once()
                finally:
                    window.queue_manager.abandon_run()
                    with patch("gui.gui_mainwindow.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
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
            self.assertIs(window.centralWidget(), window.main_content_widget)
            self.assertIsNot(window.main_scroll_area.widget(), window.main_content_widget)
            self.assertIsNotNone(window.source_box)
            self.assertIsNotNone(window.options_panel)
            self.assertIsNotNone(window.jobs_box)
            self.assertIsNotNone(window.statusBar())
        finally:
            window.close()

    def test_comparison_toolbar_opens_independent_manual_window(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            with patch("gui.gui_mainwindow.ComparisonWindow") as comparison:
                window.compare_action.trigger()
                comparison.assert_called_once_with(
                    window.translator, window,
                    ffmpeg=window._selected_ffmpeg(), ffprobe=window._selected_ffprobe(),
                )
                comparison.return_value.load_paths.assert_not_called()
                comparison.return_value.show.assert_called_once()
        finally:
            window.close()

    def test_queue_comparison_passes_source_output_and_bound_tools(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            with tempfile.TemporaryDirectory() as directory:
                record = self._record(Path(directory), "comparison", QueueItemStatus.DONE)
                with patch("gui.gui_mainwindow.ComparisonWindow") as comparison:
                    window._show_comparison(record)
                    comparison.assert_called_once_with(
                        window.translator, window,
                        ffmpeg=str(record.job_snapshot.ffmpeg_path) if record.job_snapshot else None,
                        ffprobe=str(record.job_snapshot.ffprobe_path) if record.job_snapshot else None,
                    )
                    comparison.return_value.load_paths.assert_called_once_with(
                        record.source_path, record.output_path,
                    )
        finally:
            window.close()

    def test_comparison_queue_action_is_only_enabled_for_done_item(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            with tempfile.TemporaryDirectory() as directory:
                statuses = [QueueItemStatus.DONE, QueueItemStatus.FAILED, QueueItemStatus.SKIPPED,
                            QueueItemStatus.NEEDS_DECISION]
                window.queue_model.add_records([
                    self._record(Path(directory), f"compare-{index}", status)
                    for index, status in enumerate(statuses)
                ])
                for row, status in enumerate(statuses):
                    window.table_view.selectRow(row)
                    menu = QMenu(window)
                    with (patch("gui.gui_mainwindow.QMenu", return_value=menu),
                          patch.object(menu, "exec", return_value=None)):
                        window._show_queue_context_menu(window.table_view, QPoint())
                    action = next(action for action in menu.actions()
                                  if action.text() == 'Compare source and output')
                    self.assertEqual(action.isEnabled(), status == QueueItemStatus.DONE)
        finally:
            with patch("gui.gui_mainwindow.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
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
                        "gui.queue.completion.PowerActionCountdownDialog",
                        return_value=dialog,
                    ),
                    patch("gui.queue.completion.execute_power_action", return_value=failure),
                    patch("gui.queue.completion.QMessageBox.critical") as critical,
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
                'Awaiting decision',
            )
            with patch("gui.gui_mainwindow.update_app_config"):
                window._language_changed("zh_cn")
            self.assertEqual(window.translator.language, "zh_cn")
            self.assertEqual(
                window.queue_state_label.text(),
                '等待决定',
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
                window._asked_analysis_decisions[("run", record.item_id)] = result_of(record)
                window._stop_active_task()
                self.assertFalse(window.queue_manager.has_pending_run())
                self.assertFalse(window._asked_analysis_decisions)
        finally:
            with patch("gui.gui_mainwindow.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
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


class GuiRegressionTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config_path = self.root / "app_config.json"
        config_patch = patch("core.config.store.app_config_path", return_value=self.config_path)
        config_patch.start()
        self.addCleanup(config_patch.stop)
        self.window = MainWindow(Path(__file__).resolve().parent.parent, language="en")
        self.window._startup_encoder_detection_started = True
        self.addCleanup(self._close_window)

    def _close_window(self) -> None:
        with patch("gui.gui_mainwindow.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            self.window.close()

    def _record(self, name: str, status: QueueItemStatus) -> QueueItemRecord:
        return MainWindowMaintenanceTestCase()._record(self.root, name, status)

    def test_settings_only_change_survives_reopening(self) -> None:
        window = self.window
        window._persist_runtime_state()
        dialog = MagicMock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.redetect_requested = False
        dialog.values.return_value = {
            "language": "en", "encode_workers": 4, "desktop_notifications": False,
            "size_blocked_policy": "ask", "analysis_profiles": {},
        }
        with patch("gui.gui_mainwindow.SettingsDialog", return_value=dialog):
            window._open_settings_dialog()
        saved = json.loads(self.config_path.read_text())
        self.assertEqual(saved["encode_workers"], 4)
        self.assertFalse(saved["desktop_notifications"])
        self.assertEqual(saved["size_blocked_policy"], "ask")

    def test_geometry_is_persisted_on_the_same_close(self) -> None:
        self.window._persist_runtime_state()
        self.window.resize(1030, 710)
        self.window.close()
        saved = json.loads(self.config_path.read_text())
        for key in ("main_window_geometry", "queue_window_geometry", "activity_log_geometry"):
            self.assertTrue(saved.get(key))
            self.assertEqual(saved[key], self.window.app_config[key])

    def test_new_source_queue_warns_before_close(self) -> None:
        source = self.root / "source.mp4"
        source.touch()
        self.window.queue_model.add_records([create_draft_record(
            QueueSourceDraft(VideoFileItem(source, Path(source.name)), None, None)
        )])
        with patch("gui.gui_mainwindow.QMessageBox.question", return_value=QMessageBox.StandardButton.No) as ask:
            self.assertFalse(self.window.close())
        ask.assert_called_once()

    def test_detached_selection_drives_details_and_folder_action(self) -> None:
        a = self._record("a", QueueItemStatus.DONE)
        b = self._record("b", QueueItemStatus.DONE)
        self.window.queue_model.add_records([a, b])
        self.window.table_view.selectRow(0)
        self.window.queue_window.table_view.selectRow(1)
        self.assertIs(self.window.details_panel.record, b)
        self.assertEqual(self.window.table_view.selectionModel().selectedRows()[0].row(), 1)
        with patch.object(self.window, "_open_folder") as open_folder:
            self.window._open_selected_output_folder()
        open_folder.assert_called_once_with(b.output_path.parent)

    def test_details_follow_failure_and_model_reset(self) -> None:
        record = self._record("a", QueueItemStatus.QUEUED)
        self.window.queue_model.add_records([record])
        self.window.table_view.selectRow(0)
        self.window.queue_model.mark_failed(record.item_id, "test failure")
        self.assertIn("test failure", self.window.details_panel.details_view.toPlainText())
        self.assertIn("Failed", self.window.details_panel.details_view.toPlainText())
        self.window.queue_model.sort(0)
        self.assertIs(self.window.details_panel.record, record)
        self.assertFalse(self.window.details_panel.isHidden())
        self.window.queue_model.beginResetModel()
        self.window.queue_model.endResetModel()
        self.assertIsNone(self.window.details_panel.record)
        self.assertTrue(self.window.details_panel.isHidden())

    def test_comparison_prefers_preserved_file_over_old_output(self) -> None:
        record = self._record("a", QueueItemStatus.NEEDS_DECISION)
        record.output_path.write_bytes(b"old output")
        preserved = self.root / "preserved.mp4"
        preserved.write_bytes(b"new output")
        result_of(record).rejected_output_path = preserved
        with patch("gui.gui_mainwindow.ComparisonWindow") as comparison:
            self.window._show_comparison(record)
            comparison.return_value.load_paths.assert_called_once_with(record.source_path, preserved)
        self.window.comparison_windows.clear()

    def test_invalid_fixed_ratio_is_reported_before_start(self) -> None:
        record = self._record("a", QueueItemStatus.QUEUED)
        from gui.queue.state import unbind_for_retry
        unbind_for_retry(record)
        self.window.queue_model.add_records([record])
        panel = self.window.options_panel
        panel.compression_mode_combo.setCurrentIndex(panel.compression_mode_combo.findData(CompressionMode.FIXED_BITRATE.value))
        for value in ("abc", "nan", "inf", "0", "-1"):
            with self.subTest(value=value):
                panel.ratio_edit.setText(value)
                with patch("gui.gui_mainwindow.QMessageBox.warning") as warning, patch.object(self.window, "_start_worker") as start:
                    self.window._start_queue()
                warning.assert_called_once()
                start.assert_not_called()
                self.assertTrue(panel.ratio_edit.property("invalid"))
        panel.ratio_edit.setText("0.75")
        self.assertEqual(panel.read_options().ratio, 0.75)
        self.assertFalse(panel.ratio_edit.property("invalid"))

    def test_smart_and_directory_intake_ignore_unused_ratio(self) -> None:
        panel = self.window.options_panel
        panel.ratio_edit.setText("abc")
        self.assertIsNone(panel.read_options().ratio)
        panel.compression_mode_combo.setCurrentIndex(panel.compression_mode_combo.findData(CompressionMode.FIXED_BITRATE.value))
        panel.recursive_check.setChecked(True)
        self.window.source_combo.setEditText(str(self.root))
        with patch("gui.gui_mainwindow.QueueIntakeWorker") as worker, patch.object(self.window, "_start_worker"):
            self.window._plan_current_source()
        self.assertTrue(worker.call_args.args[1])

    def test_manual_decisions_are_blocked_during_preparation(self) -> None:
        record = self._record("a", QueueItemStatus.NEEDS_DECISION)
        result_of(record).rejected_output_path = self.root / "preserved.mp4"
        self.window.queue_model.add_records([record])
        self.window.table_view.selectRow(0)
        self.window.queue_manager.begin_preparation()
        try:
            with patch("gui.gui_mainwindow.choose_size_miss_decision", return_value=None) as choose:
                self.window._resolve_selected_decision()
                self.window._resolve_next_decision()
            choose.assert_not_called()
            self.assertFalse(self.window.details_panel.resolve_button.isEnabled())
            self.assertFalse(self.window.notification_banner._actions[0].isEnabled())
        finally:
            self.window.queue_manager.finish_preparation()

    def test_mixed_directory_drop_does_not_parse_encoding_options(self) -> None:
        panel = self.window.options_panel
        panel.compression_mode_combo.setCurrentIndex(panel.compression_mode_combo.findData(CompressionMode.FIXED_BITRATE.value))
        panel.ratio_edit.setText("abc")
        panel.recursive_check.setChecked(True)
        first = self.root / "first"
        second = self.root / "second"
        first.mkdir()
        second.mkdir()
        a = first / "a.mp4"
        b = second / "b.mp4"
        a.touch()
        b.touch()
        with patch.object(self.window, "_start_worker") as intake:
            self.window._handle_dropped_paths([first, second])
        intake.assert_called_once()
        worker = intake.call_args.args[0]
        self.assertEqual(worker.paths, [first.resolve(), second.resolve()])
        self.assertTrue(worker.recursive)

    def test_export_failure_is_visible_and_retains_log(self) -> None:
        log = ActivityLogWindow(self.window.translator)
        self.addCleanup(log.close)
        log.append_message("test message")
        with patch("gui.activity_log_window.QFileDialog.getSaveFileName", return_value=(str(self.root), "")), patch("gui.activity_log_window.QMessageBox.warning") as warning:
            log._export_logs()
        warning.assert_called_once()
        self.assertIn(str(self.root), warning.call_args.args[2])
        self.assertEqual(len(log.entries), 1)

    def test_banner_preserves_button_identity_on_progress_and_retranslates(self) -> None:
        self.window._on_smart_unavailable("VMAF missing")
        button = self.window.notification_banner._actions[0]
        self.window._update_queue_metrics(self.window.queue_model.metrics())
        self.assertIs(self.window.notification_banner._actions[0], button)
        self.window._language_changed("zh_cn")
        self.assertEqual(self.window.notification_banner._actions[0].text(), "设置")
        self.assertEqual(self.window.notification_banner.text_label.text(), "智能模式不可用：VMAF missing")
        self.window._on_smart_unavailable("所选 FFmpeg 无法运行 libvmaf，智能模式不可用。")
        self.window._language_changed("en")
        self.assertEqual(self.window.notification_banner.text_label.text(), "Smart mode is unavailable: Smart mode is unavailable because the selected FFmpeg cannot run libvmaf.")

    def test_completion_banner_retranslates_without_losing_dismissal(self) -> None:
        record = self._record("a", QueueItemStatus.DONE)
        self.window._register_completion_banner([record])
        original_button = self.window.notification_banner._actions[0]
        self.window._update_queue_metrics(self.window.queue_model.metrics())
        self.assertIs(self.window.notification_banner._actions[0], original_button)
        self.window._language_changed("zh_cn")
        self.assertIn("批处理完成", self.window.notification_banner.text_label.text())
        self.assertEqual(self.window.notification_banner._actions[0].text(), "打开输出目录")
        self.window.notification_banner._dismiss()
        self.window._language_changed("en")
        self.assertTrue(self.window.notification_banner.isHidden())

    def test_basic_layout_reflows_without_changing_options(self) -> None:
        from PySide6.QtWidgets import QGridLayout
        panel = self.window.options_panel
        before = panel.read_options()
        panel.resize(440, 500)
        panel._reflow_tabs()
        layout = panel.basic_tab.layout()
        self.assertIsInstance(layout, QGridLayout)
        assert isinstance(layout, QGridLayout)
        codec_position = layout.getItemPosition(layout.indexOf(panel.codec_combo))
        backend_position = layout.getItemPosition(layout.indexOf(panel.backend_combo))
        assert isinstance(codec_position, tuple) and isinstance(backend_position, tuple)
        self.assertGreater(backend_position[0], codec_position[0])
        panel.resize(1100, 500)
        panel._reflow_tabs()
        backend_position = layout.getItemPosition(layout.indexOf(panel.backend_combo))
        assert isinstance(backend_position, tuple)
        self.assertEqual(backend_position[0], codec_position[0])
        self.assertEqual(panel.read_options(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
