from __future__ import annotations

from qt_lifetime import dispose_window

import os
import ntpath
import posixpath
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from core.i18n import Translator
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
        cls.tr = Translator('en')

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        config = patch("core.config.store.app_config_path", return_value=self.root / "app.json")
        config.start()
        self.addCleanup(config.stop)

    def window(self, language: str = "en") -> MainWindow:
        window = MainWindow(ROOT, language=language)
        window._startup_encoder_detection_started = True
        def close() -> None:
            window.queue_manager.abandon_run()
            with patch("gui.gui_mainwindow.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                dispose_window(window)
        self.addCleanup(close)
        return window

    def test_settings_preserves_custom_profiles(self) -> None:
        dialog = SettingsDialog(self.tr, {"analysis_profiles": {"precise": {"candidate_limit": 42}}})
        self.addCleanup(dispose_window, dialog)
        self.assertNotIn("analysis_profiles", dialog.values())

    def test_preset_survives_translation_and_detection_without_dirty_signal(self) -> None:
        panel = EncodeOptionsPanel(self.tr, {})
        self.addCleanup(dispose_window, panel)
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
        self.addCleanup(dispose_window, panel)
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
        self.addCleanup(dispose_window, dialog)
        self.assertIsNone(dialog._path_problem(str(self.root / "new"), expect_directory=True))
        file = self.root / "file"
        file.touch()
        self.assertIn("directory", dialog._path_problem(str(file), expect_directory=True) or "")

    def test_source_identity_respects_case_normalization(self) -> None:
        from core.models import VideoFileItem
        from gui.queue.state import QueueSourceDraft, create_draft_record
        # Exercise both policies on every OS; the host's normcase is not
        # evidence that the temporary volume supports distinct case variants.
        for normalize, expected in ((posixpath.normcase, 2), (ntpath.normcase, 1)):
            with self.subTest(normalize=normalize.__module__):
                window = self.window()
                def records() -> list:
                    return [create_draft_record(QueueSourceDraft(VideoFileItem(self.root / name, Path(name)), None))
                            for name in ("Clip.mp4", "clip.mp4", "Clip.mp4")]
                with patch("gui.gui_mainwindow.os.path.normcase", normalize):
                    window._on_sources_ready(records())
                    self.assertEqual(window.queue_model.rowCount(), expected)
                    window._on_sources_ready(records())
                    self.assertEqual(window.queue_model.rowCount(), expected)

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
        self.addCleanup(dispose_window, dialog)
        _size_metrics(dialog, record)
        self.assertTrue(dialog.findChildren(QProgressBar))
        quality = _DecisionDialog(self.tr, None)
        self.addCleanup(dispose_window, quality)
        _quality_metrics(quality, record)
        text = " ".join(label.text() for label in quality.findChildren(QLabel))
        self.assertNotIn("Measured quality", text)
        self.assertIn("~91.2", text)
        from gui.queue.item_details_panel import ItemDetailsPanel
        panel = ItemDetailsPanel(self.tr)
        self.addCleanup(dispose_window, panel)
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
        self.addCleanup(dispose_window, window)
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
        self.addCleanup(dispose_window, log)
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
        self.assertGreaterEqual(window.table_view.height(), 150)
        gap = window.jobs_box.y() - window.main_scroll_area.geometry().bottom() - 1
        self.assertLessEqual(gap, window.workspace_splitter.handleWidth())

    def test_expanded_setup_fits_its_content_in_a_large_window(self) -> None:
        for language in ("en", "zh_cn", "ja"):
            with self.subTest(language=language):
                window = self.window(language)
                self.assertEqual(window.translator.language, language)
                self.assertEqual(window.options_panel.translator.language, language)
                if language == "ja":
                    self.assertEqual(window.options_panel.codec_label.text(), "コーデック")
                window.show()
                window._set_setup_collapsed(False)
                for width, height in ((1520, 960), (760, 520), (1520, 960)):
                    window.resize(width, height)
                    for index in range(window.options_panel.options_tabs.count()):
                        with self.subTest(size=(width, height), tab=index):
                            window.options_panel.options_tabs.setCurrentIndex(index)
                            for _ in range(8):
                                self.app.processEvents()
                            content = window.main_scroll_area.widget()
                            assert content is not None
                            diagnostic = (
                                f"language={language} size={width}x{height} tab={index} "
                                f"viewport={window.main_scroll_area.viewport().size()} "
                                f"content_minimum={content.minimumSizeHint()}"
                            )
                            self.assertEqual(window.main_scroll_area.horizontalScrollBar().maximum(), 0, diagnostic)
                            if height == 960:
                                self.assertEqual(window.main_scroll_area.verticalScrollBar().maximum(), 0, diagnostic)
                                self.assertGreaterEqual(window.queue_stack.height(), 150)
                            else:
                                self.assertGreater(window.main_scroll_area.verticalScrollBar().maximum(), 0)
                                self.assertGreaterEqual(window.queue_stack.height(), 90)

    def test_options_tabs_fit_narrow_viewports_after_font_and_language_changes(self) -> None:
        from PySide6.QtWidgets import QScrollArea
        for language in ("en", "zh_cn", "ja"):
            with self.subTest(language=language):
                scroll = QScrollArea()
                scroll.setWidgetResizable(True)
                panel = EncodeOptionsPanel(Translator(language), {})
                scroll.setWidget(panel)
                panel.set_translator(Translator(language))
                panel.apply_options(EncodeOptions())
                scroll.resize(440, 700)
                scroll.show()
                try:
                    original_font = panel.font()
                    for scale in (1.0, 1.4, 1.0):
                        font = panel.font()
                        font.setPointSizeF(original_font.pointSizeF() * scale)
                        panel.setFont(font)
                        for index in range(panel.options_tabs.count()):
                            panel.options_tabs.setCurrentIndex(index)
                            for _ in range(8):
                                self.app.processEvents()
                            page = panel.options_tabs.currentWidget()
                            assert page is not None
                            diagnostic = (
                                f"language={language} font={font.pointSizeF()} tab={index} "
                                f"viewport={scroll.viewport().size()} panel={panel.size()} "
                                f"minimum={panel.minimumSizeHint()}"
                            )
                            self.assertEqual(scroll.horizontalScrollBar().maximum(), 0, diagnostic)
                            self.assertLessEqual(page.minimumSizeHint().width(), page.width(), diagnostic)
                finally:
                    dispose_window(scroll)

    def test_small_setup_can_scroll_to_controls_and_recovers_on_resize(self) -> None:
        window = self.window()
        window.show()
        window.resize(760, 520)
        window._set_setup_collapsed(False)
        for _ in range(8):
            self.app.processEvents()
        scrollbar = window.main_scroll_area.verticalScrollBar()
        self.assertGreater(scrollbar.maximum(), 0)
        control = window.options_panel.recursive_check
        window.main_scroll_area.ensureWidgetVisible(control)
        for _ in range(4):
            self.app.processEvents()
        from PySide6.QtCore import QPoint
        viewport = window.main_scroll_area.viewport()
        position = control.mapTo(viewport, QPoint(0, 0))
        self.assertGreaterEqual(position.y(), 0)
        self.assertLessEqual(position.y() + control.height(), viewport.height())
        self.assertGreaterEqual(window.queue_stack.height(), 90)
        self.assertEqual(window.main_scroll_area.horizontalScrollBar().maximum(), 0)
        window.resize(1520, 960)
        for _ in range(8):
            self.app.processEvents()
        self.assertEqual(scrollbar.maximum(), 0)

    def test_setup_drag_preference_survives_resize_and_collapse(self) -> None:
        window = self.window()
        window.show()
        window.resize(1520, 960)
        window._set_setup_collapsed(False)
        for _ in range(8):
            self.app.processEvents()
        window.workspace_splitter.moveSplitter(340, 1)
        preferred = window.workspace_splitter.sizes()[0]
        self.assertGreaterEqual(preferred, 330)
        window._set_setup_collapsed(True)
        for _ in range(4):
            self.app.processEvents()
        window._set_setup_collapsed(False)
        window.resize(1600, 1040)
        for _ in range(8):
            self.app.processEvents()
        self.assertEqual(window.workspace_splitter.sizes()[0], preferred)

    def test_source_actions_share_a_row_only_when_there_is_room(self) -> None:
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import QGridLayout, QWidget
        wrapped = False
        for language in ("en", "zh_cn", "ja"):
            window = self.window(language)
            window.show()
            window._set_setup_collapsed(False)
            for point_size in (10, 16, 20):
                font = QFont("Arial", point_size)
                window.source_box.setFont(font)
                for control in window.source_box.findChildren(QWidget):
                    control.setFont(font)
                self.assertEqual(window.post_encode_label.font().pointSize(), point_size)
                for width, height in ((1520, 960), (760, 520), (820, 960), (1520, 960)):
                    with self.subTest(language=language, font=point_size, width=width):
                        window.resize(width, height)
                        for _ in range(12):
                            self.app.processEvents()
                        layout = window.source_box.layout()
                        self.assertIsInstance(layout, QGridLayout)
                        assert isinstance(layout, QGridLayout)
                        preset_position = layout.getItemPosition(layout.indexOf(window.preset_combo))
                        action_position = layout.getItemPosition(layout.indexOf(window.source_add_button))
                        assert isinstance(preset_position, tuple) and isinstance(action_position, tuple)
                        same_row = action_position[0] == preset_position[0]
                        wrapped |= not same_row
                        diagnostic = (
                            f"language={language} font={point_size} width={width} inline={same_row} "
                            f"source_minimum={window.source_box.minimumSizeHint()} "
                            f"viewport={window.main_scroll_area.viewport().size()}"
                        )
                        self.assertEqual(window.main_scroll_area.horizontalScrollBar().maximum(), 0, diagnostic)
                        self.assertLessEqual(
                            window.source_box.minimumSizeHint().width(),
                            window.main_scroll_area.viewport().width(), diagnostic,
                        )
                        if width == 1520:
                            self.assertTrue(same_row, diagnostic)
        self.assertTrue(wrapped, "Source actions must move to another row when their complete grid cannot fit")

    def test_source_setup_fits_narrow_windows_with_larger_fonts_in_every_language(self) -> None:
        from PySide6.QtCore import QPoint, QRect
        for language in ("en", "zh_cn", "ja"):
            with self.subTest(language=language):
                window = self.window(language)
                font = window.font()
                font.setPointSizeF(18.0)
                window.setFont(font)
                window.show()
                window.resize(760, 520)
                window._set_setup_collapsed(False)
                for _ in range(12):
                    self.app.processEvents()
                diagnostic = (
                    f"language={language} font={font.pointSizeF()} "
                    f"viewport={window.main_scroll_area.viewport().size()} "
                    f"source_minimum={window.source_box.minimumSizeHint()}"
                )
                self.assertEqual(window.main_scroll_area.horizontalScrollBar().maximum(), 0, diagnostic)
                for widget in (
                    window.preset_combo, window.manage_presets_button,
                    window.post_encode_label, window.post_encode_combo, window.source_add_button,
                ):
                    rect = QRect(widget.mapTo(window.source_box, QPoint()), widget.size())
                    self.assertTrue(window.source_box.rect().contains(rect), diagnostic)
                self.assertGreaterEqual(window.queue_stack.height(), 90)

    def test_setup_refits_after_mode_and_theme_changes(self) -> None:
        from core.models import CompressionMode
        from gui.theme import theme_manager
        window = self.window()
        window.show()
        window.resize(1520, 960)
        window._set_setup_collapsed(False)
        manager = theme_manager()
        assert manager is not None
        for dark, mode in ((True, CompressionMode.FIXED_BITRATE), (False, CompressionMode.SMART)):
            with self.subTest(dark=dark, mode=mode):
                with patch("gui.theme._scheme_is_dark", return_value=dark):
                    manager.refresh()
                options = window.options_panel.read_options()
                options.compression_mode = mode
                window.options_panel.apply_options(options)
                for _ in range(8):
                    self.app.processEvents()
                self.assertEqual(window.options_panel.read_options().compression_mode, mode)
                self.assertEqual(window.main_scroll_area.verticalScrollBar().maximum(), 0)
                self.assertGreaterEqual(window.queue_stack.height(), 150)

    def test_empty_queue_controls_do_not_overlap_at_small_sizes(self) -> None:
        from PySide6.QtCore import QPoint, QRect
        window = self.window()
        window.show()
        window._set_setup_collapsed(False)
        for width, height in ((1280, 800), (760, 520)):
            with self.subTest(size=(width, height)):
                window.resize(width, height)
                for _ in range(8):
                    self.app.processEvents()
                self.assertGreaterEqual(
                    window.empty_state.height(), window.empty_state.minimumSizeHint().height()
                )
                buttons = (window.empty_add_files_button, window.empty_add_folder_button)
                for button in buttons:
                    rect = QRect(button.mapTo(window.empty_state, QPoint()), button.size())
                    self.assertTrue(window.empty_state.rect().contains(rect))
                    for label in (
                        window.empty_icon_label, window.empty_title_label,
                        window.empty_body_label, window.empty_drop_label,
                    ):
                        if label.isVisible():
                            label_rect = QRect(label.mapTo(window.empty_state, QPoint()), label.size())
                            self.assertFalse(rect.intersects(label_rect))

    def test_toolbar_overflow_uses_application_icon_and_keeps_hidden_actions(self) -> None:
        from PySide6.QtGui import QIcon
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QToolButton
        from gui.icons import action_icon
        from gui.theme import current_tokens, theme_manager
        window = self.window()
        window.show()
        window.resize(760, 520)
        # The detached toolbar normally has only three widget actions. Add real
        # actions so it also overflows at its minimum width.
        for action in (window.add_files_action, window.add_folder_action, window.settings_action):
            window.queue_window.action_toolbar.addAction(action)
        window.queue_window.show()
        window.queue_window.resize(260, 420)
        manager = theme_manager()
        assert manager is not None
        for dark in (False, True):
            with self.subTest(dark=dark):
                with patch("gui.theme._scheme_is_dark", return_value=dark):
                    manager.refresh()
                for _ in range(8):
                    self.app.processEvents()
                tokens = current_tokens()
                expected = action_icon("overflow", tokens.icon, tokens.icon_disabled)
                for toolbar in (window.toolbar, window.queue_window.action_toolbar):
                    extension = toolbar.findChild(QToolButton, "qt_toolbar_ext_button")
                    assert extension is not None
                    self.assertTrue(extension.isVisible())
                    self.assertEqual(extension.icon().cacheKey(), expected.cacheKey())
                    for mode in (QIcon.Mode.Normal, QIcon.Mode.Disabled):
                        self.assertFalse(extension.icon().pixmap(18, 18, mode).isNull())
                    rendered = extension.grab().toImage()
                    icon = extension.icon()
                    try:
                        extension.setIcon(QIcon())
                        without_icon = extension.grab().toImage()
                    finally:
                        extension.setIcon(icon)
                    self.assertEqual(rendered.size(), without_icon.size())
                    self.assertTrue(any(
                        rendered.pixel(x, y) != without_icon.pixel(x, y)
                        for x in range(rendered.width())
                        for y in range(rendered.height())
                    ), "The application overflow icon must change the rendered button pixels")
                extension = window.toolbar.findChild(QToolButton, "qt_toolbar_ext_button")
                assert extension is not None
                settings_button = window.toolbar.widgetForAction(window.settings_action)
                assert settings_button is not None
                self.assertFalse(settings_button.isVisible())
                extension.click()
                QTest.qWait(300)  # Allow Qt's toolbar expansion animation to finish.
                self.assertTrue(settings_button.isVisible())
                self.assertTrue(window.toolbar.rect().contains(settings_button.geometry()))
                extension.click()
                QTest.qWait(300)
                self.assertFalse(settings_button.isVisible())
        window.queue_window.close()

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
        self.addCleanup(dispose_window, log)
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

    def test_unavailable_preset_can_be_changed_to_default(self) -> None:
        panel = EncodeOptionsPanel(self.tr, {})
        self.addCleanup(dispose_window, panel)
        panel.set_runtime_capabilities({"codecs": {"hevc": [{"backend": "videotoolbox", "encoder": "hevc_videotoolbox", "preset_choices": []}]}, "vmaf": {"runnable": True}})
        panel.apply_options(EncodeOptions(backend=BackendChoice.VIDEOTOOLBOX, encoder_preset="slow"))
        with self.assertRaises(ValueError):
            panel.read_options()
        self.assertTrue(panel.encoder_preset_combo.isEnabled())
        panel.encoder_preset_combo.setCurrentIndex(0)
        self.assertIsNone(panel.read_options().encoder_preset)

    def test_capability_errors_are_visible_in_errors_filter(self) -> None:
        window = self.window()
        window._on_encoder_capability_detection_failed("probe unavailable")
        log = window.activity_log_window
        log.filter_combo.setCurrentIndex(log.filter_combo.findData("error"))
        self.assertIn("probe unavailable", log.log_output.toPlainText())

    def test_canvas_preserves_physical_pixels_at_double_scale(self) -> None:
        from PySide6.QtGui import QImage, QPainter, QColor
        from PySide6.QtCore import Qt, QPoint
        from PySide6.QtWidgets import QWidget
        from gui.comparison.window import FrameCanvas
        parent = QWidget()
        self.addCleanup(dispose_window, parent)
        canvas = FrameCanvas(parent)
        canvas.resize(64, 64)
        source = QImage(128, 128, QImage.Format.Format_RGB32)
        painter = QPainter(source)
        for x in range(128):
            painter.setPen(QColor("white") if x % 2 else QColor("black"))
            painter.drawLine(x, 0, x, 127)
        painter.end()
        canvas.image = source
        target = QImage(128, 128, QImage.Format.Format_RGB32)
        target.setDevicePixelRatio(2)
        target.fill(Qt.GlobalColor.red)
        painter = QPainter(target)
        canvas.render(painter, QPoint())
        painter.end()
        values = [target.pixelColor(x, 64).red() for x in range(128)]
        self.assertGreater(sum(a != b for a, b in zip(values, values[1:])), 100)

    def test_comparison_shortcuts_leave_editing_and_tab_navigation_available(self) -> None:
        from gui.comparison.window import ComparisonWindow
        window = ComparisonWindow(self.tr)
        self.addCleanup(dispose_window, window)
        window.show()
        window.activateWindow()
        window.offset.setEnabled(True)
        window.offset.setFocus()
        self.app.processEvents()
        window._update_shortcuts()
        self.assertTrue(window.offset.hasFocus() or window.offset.lineEdit().hasFocus())
        self.assertTrue(all(not shortcut.isEnabled() for shortcut in window._shortcuts))
        self.assertNotIn("Tab", [shortcut.key().toString() for shortcut in window._shortcuts])
