from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from core.i18n import TranslationCatalog
from core.models import BackendChoice, EncodeOptions
from gui.gui_mainwindow import MainWindow
from gui.options.encode_options_panel import EncodeOptionsPanel
from gui.options.settings_dialog import SettingsDialog
from gui.queue.workers import QueueIntakeWorker


ROOT = Path(__file__).resolve().parent.parent
CAPABILITIES = {
    "codecs": {"hevc": [{"backend": "cpu", "encoder": "libx265", "preset_choices": ["slow", "fast"]}]},
    "vmaf": {"runnable": True},
}


class GuiRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.tr = TranslationCatalog(ROOT / "config/i18n").translator("en")

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        config = patch("core.config.store.app_config_path", return_value=self.root / "app.json")
        config.start()
        self.addCleanup(config.stop)

    def window(self) -> MainWindow:
        window = MainWindow(ROOT, language="en")
        window._startup_encoder_detection_started = True
        def close() -> None:
            window.queue_manager.abandon_run()
            with patch("gui.gui_mainwindow.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                window.close()
        self.addCleanup(close)
        return window

    def test_settings_preserves_custom_profiles(self) -> None:
        dialog = SettingsDialog(self.tr, {"analysis_profiles": {"precise": {"candidate_limit": 42}}})
        self.addCleanup(dialog.close)
        self.assertNotIn("analysis_profiles", dialog.values())

    def test_preset_survives_translation_and_detection_without_dirty_signal(self) -> None:
        panel = EncodeOptionsPanel(self.tr, {})
        self.addCleanup(panel.close)
        panel.apply_options(EncodeOptions(backend=BackendChoice.CPU, encoder_preset="slow"))
        changes: list[bool] = []
        panel.options_changed.connect(lambda: changes.append(True))
        panel.set_translator(self.tr)
        panel.begin_capability_detection()
        panel.set_runtime_capabilities(CAPABILITIES)
        self.assertEqual(panel.read_options().encoder_preset, "slow")
        self.assertFalse(changes)

    def test_pending_backend_is_not_read_as_auto(self) -> None:
        panel = EncodeOptionsPanel(self.tr, {})
        self.addCleanup(panel.close)
        panel.apply_options(EncodeOptions(backend=BackendChoice.NVENC, encoder_preset="p5"))
        with self.assertRaises(ValueError):
            panel.read_options()

    def test_invalid_default_preset_does_not_abort_startup(self) -> None:
        with patch("gui.gui_mainwindow.load_preset", side_effect=ValueError("Broken preset")), patch("gui.gui_mainwindow.QMessageBox.critical"):
            window = self.window()
        self.assertEqual(window.preset_combo.currentData(), "")
        self.assertTrue(any("Broken preset" in entry.message for entry in window.activity_log_window.entries))

    def test_new_workdir_is_valid_but_file_is_not(self) -> None:
        dialog = SettingsDialog(self.tr, {})
        self.addCleanup(dialog.close)
        self.assertIsNone(dialog._path_problem(str(self.root / "new"), expect_directory=True))
        file = self.root / "file"
        file.touch()
        self.assertIn("directory", dialog._path_problem(str(file), expect_directory=True) or "")

    def test_source_identity_keeps_distinct_case_on_sensitive_volumes(self) -> None:
        from core.models import VideoFileItem
        from gui.queue.state import QueueSourceDraft, create_draft_record
        window = self.window()
        records = [create_draft_record(QueueSourceDraft(VideoFileItem(self.root / name, Path(name)), None))
                   for name in ("Clip.mp4", "clip.mp4")]
        window._on_sources_ready(records)
        self.assertEqual(window.queue_model.rowCount(), 2)

    def test_multi_directory_intake_retains_each_root(self) -> None:
        folders = [self.root / name for name in ("one", "two")]
        for folder in folders:
            (folder / "nested").mkdir(parents=True)
            (folder / "nested/clip.mp4").touch()
        worker = QueueIntakeWorker(None, True, None, paths=folders)
        results: list = []
        worker.completed.connect(results.append)
        with patch("gui.queue.workers.find_binary", return_value=Path("ffprobe")), patch("gui.queue.workers.probe_media_info", return_value=None):
            worker.run()
        self.assertEqual(len(results), 1)
        self.assertEqual([record.draft.input_root for record in results[0]], folders)
        self.assertEqual([record.draft.file_item.relative_path for record in results[0]], [Path("nested/clip.mp4")] * 2)

    def test_busy_queue_rejects_keyboard_remove_and_reorder(self) -> None:
        from unittest.mock import MagicMock
        from test_gui_presentation import _record
        window = self.window()
        record = _record(self.root)
        from gui.queue.state import QueueItemStatus
        record.status = QueueItemStatus.QUEUED
        window.queue_model.add_records([record])
        window.table_view.selectRow(0)
        window.queue_manager._worker = MagicMock()
        try:
            window._refresh_action_state()
            window._remove_selected_queue_rows()
            self.assertEqual(window.queue_model.rowCount(), 1)
            self.assertFalse(window.queue_model.can_sort())
            self.assertFalse(window.queue_manager.can_remove_rows([0]))
        finally:
            window.queue_manager._worker = None
            window._refresh_action_state()

    def test_sort_preserves_selection_details_and_both_indicators(self) -> None:
        from PySide6.QtCore import Qt
        from test_gui_presentation import _record
        from gui.queue.model import QueueColumn
        window = self.window()
        a = _record(self.root)
        b = _record(self.root)
        b.item_id = "other"
        b.bound_plan_item.source_path = self.root / "z.mkv"
        b.bound_plan_item.output_path = self.root / "z.mp4"
        window.queue_model.add_records([b, a])
        window.table_view.selectRow(0)
        window.queue_model.sort(int(QueueColumn.NAME), Qt.SortOrder.AscendingOrder)
        self.assertIs(window._selected_record(), b)
        self.assertIs(window.details_panel.record, b)
        for view in (window.table_view, window.queue_window.table_view):
            self.assertTrue(view.horizontalHeader().isSortIndicatorShown())
        window.queue_model.reset_order()
        self.assertIs(window._selected_record(), b)
        for view in (window.table_view, window.queue_window.table_view):
            self.assertFalse(view.horizontalHeader().isSortIndicatorShown())

    def test_stopped_size_miss_remains_protected(self) -> None:
        from core.models import EncodeResult
        from gui.queue.state import QueueItemStatus
        from test_gui_presentation import _record
        window = self.window()
        record = _record(self.root, QueueItemStatus.NEEDS_DECISION)
        record.result = EncodeResult(record.source_path, record.output_path, False, needs_decision=True,
                                     rejected_output_path=self.root / "preserved.mp4")
        window.queue_model.add_records([record])
        window.queue_manager.include_prepared_items([record.item_id])
        window.queue_manager.abandon_run()
        self.assertEqual(window.queue_manager.remove_rows([0]), 0)

    def test_cancel_interrupts_tool_process(self) -> None:
        import sys
        import threading
        import time
        from core.ffmpeg.subprocess import run_cancellable
        from core.models import OperationCancelledError
        cancelled = threading.Event()
        timer = threading.Timer(0.1, cancelled.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(OperationCancelledError):
                run_cancellable([sys.executable, "-c", "import time; time.sleep(30)"],
                                cancel_check=cancelled.is_set, capture_output=True, text=True)
        finally:
            timer.join()
        self.assertLess(time.monotonic() - started, 3)

    def test_large_size_miss_and_predictions_are_honest(self) -> None:
        from PySide6.QtWidgets import QLabel, QProgressBar
        from core.models import EncodeResult
        from gui.constraint_decision_dialog import _DecisionDialog, _quality_metrics, _size_metrics
        from gui.queue.state import QueueItemStatus
        from test_gui_presentation import _record
        record = _record(self.root, QueueItemStatus.NEEDS_DECISION)
        record.result = EncodeResult(record.source_path, record.output_path, False, needs_decision=True,
                                     actual_output_bytes=3 * 1024**3, allowed_output_bytes=2 * 1024**3,
                                     rejected_output_path=self.root / "preserved.mp4")
        dialog = _DecisionDialog(self.tr, None)
        self.addCleanup(dialog.close)
        _size_metrics(dialog, record)
        self.assertTrue(dialog.findChildren(QProgressBar))
        quality = _DecisionDialog(self.tr, None)
        self.addCleanup(quality.close)
        _quality_metrics(quality, record)
        text = " ".join(label.text() for label in quality.findChildren(QLabel))
        self.assertNotIn("Measured quality", text)
        self.assertIn("~91.2", text)
        from gui.queue.item_details_panel import ItemDetailsPanel
        panel = ItemDetailsPanel(self.tr)
        self.addCleanup(panel.close)
        panel.set_record(record, self.tr)
        details = panel.details_view.toPlainText()
        self.assertIn("~91.2", details)
        self.assertNotIn("Saved space", details)
        self.assertIn("Preserved", details)

    def test_keyboard_slider_requests_a_seek(self) -> None:
        from gui.comparison.window import ComparisonWindow
        from gui.comparison.decode import ComparisonMedia
        from gui.comparison.state import VideoDetails, ComparisonPhase
        window = ComparisonWindow(self.tr)
        self.addCleanup(window.close)
        window.media = ComparisonMedia((Path("a"), Path("b")), (VideoDetails(10, 0, 25), VideoDetails(10, 0, 25)), Path("ffmpeg"), Path("ffprobe"))
        window.phase = ComparisonPhase.PAUSED
        window._update_controls()
        with patch.object(window, "_launch") as decode:
            window.slider.setValue(5000)
            self.assertEqual(window.position, 5)
            decode.assert_called_once()

    def test_unfollow_retains_log_position_and_classification_is_not_substring_based(self) -> None:
        from gui.activity_log_window import ActivityLogWindow
        log = ActivityLogWindow(self.tr)
        self.addCleanup(log.close)
        log.show()
        for i in range(100):
            log.append_message(f"line {i}")
        log._flush_pending_logs()
        self.app.processEvents()
        log.follow_check.setChecked(False)
        scrollbar = log.log_output.verticalScrollBar()
        before = scrollbar.value()
        log.append_message("failed=0; read error_in_filename.mkv")
        log._flush_pending_logs()
        self.assertEqual(scrollbar.value(), before)
        self.assertEqual(log.entries[-1].category, "process")
        log._refresh_log_view()
        self.assertEqual(scrollbar.value(), before)

    def test_queue_stays_in_view_with_expanded_setup(self) -> None:
        from gui.queue.state import QueueItemStatus
        from test_gui_presentation import _record
        window = self.window()
        window.queue_model.add_records([_record(self.root, QueueItemStatus.NEEDS_DECISION)])
        window.show()
        window.resize(1280, 800)
        window._set_setup_collapsed(False)
        window.table_view.selectRow(0)
        for _ in range(4):
            self.app.processEvents()
        from PySide6.QtCore import QPoint
        top = window.table_view.mapTo(window, QPoint(0, 0)).y()
        self.assertLess(top + 80, window.height())
        self.assertGreaterEqual(window.table_view.height(), 80)

    def test_native_window_gc_keeps_parent_before_child_ownership(self) -> None:
        import gc
        for _ in range(5):
            window = MainWindow(ROOT, language="en")
            window.close()
            del window
            gc.collect()
            self.app.processEvents()
        self.assertIsNotNone(QApplication.instance())

    def test_info_level_filters_debug_without_losing_records(self) -> None:
        from gui.activity_log_window import ActivityLogWindow
        log = ActivityLogWindow(self.tr)
        self.addCleanup(log.close)
        log.set_log_level("info")
        log.append_message("$ ffmpeg input")
        log.append_message("DEBUG: candidate details")
        log.append_message("Error: real failure", category="error")
        log._flush_pending_logs()
        self.assertNotIn("ffmpeg input", log.log_output.toPlainText())
        self.assertIn("real failure", log.log_output.toPlainText())
        self.assertEqual(len(log.entries), 3)
        log.set_log_level("debug")
        self.assertIn("ffmpeg input", log.log_output.toPlainText())
        self.assertIn("candidate details", log.log_output.toPlainText())
