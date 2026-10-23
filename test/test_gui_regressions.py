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
        from PySide6.QtCore import Qt
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
