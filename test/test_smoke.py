from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QAbstractButton, QApplication, QMessageBox, QScrollArea
from PySide6.QtWidgets import QComboBox, QGroupBox, QHeaderView, QLabel, QLineEdit

from core.config.paths import app_root, config_dir
from core.i18n import get_translator
from core.models import (
    BackendChoice,
    CodecChoice,
    EncodeOptions,
    EncodePlan,
    EncodePlanItem,
    EncoderInfo,
    MediaInfo,
)
from core.config.store import app_config_path
from gui.gui_mainwindow import MainWindow
from gui.queue_manager import QueueManager
from gui.queue_model import QueueColumn, QueueTableModel
from gui.queue_view import create_queue_view
from main import main


class SmokeTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = app_root()
        cls._sample_dir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._sample_dir.cleanup)
        cls.sample_video = Path(cls._sample_dir.name) / "smoke-source.mp4"
        cls.sample_video.write_bytes(b"portable-smoke-fixture")
        cls.app = QApplication.instance() or QApplication([])

    def _sample_plan(self) -> EncodePlan:
        options = EncodeOptions(overwrite=True)
        media = MediaInfo(
            path=self.sample_video,
            duration=12.0,
            format_bitrate_bps=1_128_000,
            video_bitrate_bps=1_000_000,
            audio_bitrate_bps=128_000,
            width=640,
            height=360,
            fps=30.0,
            video_codec="h264",
            audio_codec="aac",
            audio_stream_count=1,
            pix_fmt="yuv420p",
            bit_depth=8,
        )
        encoder = EncoderInfo(
            codec=CodecChoice.HEVC,
            backend=BackendChoice.CPU,
            encoder_name="libx265",
            supports_two_pass=True,
            default_preset="slow",
        )
        output_root = self.sample_video.parent / "output"
        return EncodePlan(
            items=[
                EncodePlanItem(
                    source_path=self.sample_video,
                    output_path=output_root / "smoke-source_hevc.mp4",
                    media_info=media,
                    encoder_info=encoder,
                    options=options,
                )
            ],
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            input_root=self.sample_video.parent,
            output_root=output_root,
        )

    def test_app_config_path_uses_workdir(self) -> None:
        config_path = app_config_path(config_dir())
        self.assertEqual(config_path, self.repo_root / "workdir" / "app_config.json")

    def test_smoke_fixture_is_isolated_from_project_workdir(self) -> None:
        self.assertTrue(self.sample_video.is_file())
        self.assertNotEqual(self.sample_video.parent, self.repo_root / "workdir")

    def test_cli_plan_smoke(self) -> None:
        stdout = io.StringIO()
        with patch("cli.cli_entry.build_encode_plan", return_value=self._sample_plan()), contextlib.redirect_stdout(
            stdout
        ):
            exit_code = main(["--cli", "plan", str(self.sample_video), "--overwrite"])
        self.assertEqual(exit_code, 0)
        output = stdout.getvalue()
        self.assertIn("Plan items:", output)
        self.assertIn(str(self.sample_video), output)

    def test_queue_metrics_after_plan_add(self) -> None:
        plan = self._sample_plan()
        model = QueueTableModel(get_translator("en", self.repo_root / "config"))
        manager = QueueManager(model)
        added = manager.add_plan(plan, self.repo_root / "workdir")
        metrics = model.metrics()
        self.assertEqual(added, 1)
        self.assertEqual(metrics.total_items, 1)
        self.assertEqual(metrics.ready_items, 1)
        self.assertGreater(metrics.total_duration_sec, 0.0)
        self.assertEqual(metrics.queue_percent, 0.0)

    def test_main_window_offscreen_init(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            self.assertEqual(window.queue_model.rowCount(), 0)
            self.assertFalse(window.queue_busy)
            self.assertEqual(window.queue_progress_bar.value(), 0)
        finally:
            window.close()

    def test_start_encoder_detection_uses_selected_paths(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            with patch("gui.gui_mainwindow.EncoderCapabilityDetectWorker") as worker_cls:
                window._start_encoder_capability_detection(force_refresh=False)
            worker_cls.assert_called_once()
            worker_cls.return_value.start.assert_called_once()
        finally:
            window.encoder_detection_worker = None
            window.close()

    def test_build_context_reads_selected_paths_and_options(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            window.app_config["ffmpeg_path"] = ""
            window.app_config["ffprobe_path"] = ""
            with tempfile.TemporaryDirectory() as temp_dir:
                source = Path(temp_dir) / "source.mp4"
                source.write_bytes(b"source")
                window.source_combo.setEditText(str(source))
                window.output_edit.setText(temp_dir)
                with patch("gui.gui_mainwindow.update_app_config"):
                    input_path, options, output_dir, workdir, ffmpeg_path, ffprobe_path = window._build_context()
            self.assertEqual(input_path, source.resolve())
            self.assertEqual(output_dir, Path(temp_dir).resolve())
            self.assertEqual(workdir, window.default_workdir.resolve())
            self.assertIsNone(ffmpeg_path)
            self.assertIsNone(ffprobe_path)
            self.assertIsInstance(options, EncodeOptions)
        finally:
            window.close()

    def test_main_window_applies_desktop_polish_contract(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            self.assertIn("QualityBoundTheme", window.styleSheet())
            self.assertEqual(window.toolbar.toolButtonStyle(), Qt.ToolButtonTextBesideIcon)
            for action in [
                window.add_files_action,
                window.add_folder_action,
                window.plan_action,
                window.start_queue_action,
                window.pause_after_current_action,
                window.stop_action,
                window.queue_action,
                window.activity_log_action,
                window.presets_action,
                window.settings_action,
            ]:
                self.assertFalse(action.icon().isNull(), action.text())
                self.assertTrue(action.toolTip(), action.text())
                self.assertTrue(action.statusTip(), action.text())
            for label in [
                window.total_items_title,
                window.total_duration_title,
                window.states_title,
                window.saved_space_title,
            ]:
                self.assertEqual(label.objectName(), "summaryTitle")
            for label in [
                window.total_items_value,
                window.total_duration_value,
                window.states_value,
                window.saved_space_value,
            ]:
                self.assertEqual(label.objectName(), "summaryValue")
        finally:
            window.close()

    def test_main_window_minimum_size_fits_low_resolution_contract(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            self.assertLessEqual(window.minimumWidth(), 800)
            self.assertLessEqual(window.minimumHeight(), 600)
        finally:
            window.close()

    def test_main_window_initial_size_is_clamped_to_available_screen(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            available = self.app.primaryScreen().availableGeometry()
            self.assertLessEqual(window.width(), available.width())
            self.assertLessEqual(window.height(), available.height())
        finally:
            window.close()

    def test_auxiliary_window_initial_sizes_are_clamped_to_available_screen(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            available = self.app.primaryScreen().availableGeometry()
            for child_window in [window.queue_window, window.activity_log_window]:
                self.assertLessEqual(child_window.width(), available.width())
                self.assertLessEqual(child_window.height(), available.height())
        finally:
            window.close()

    def test_main_window_central_content_is_scrollable_for_small_screens(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            self.assertIsInstance(window.centralWidget(), QScrollArea)
        finally:
            window.close()

    def _raw_keys_displayed(self, window: MainWindow) -> list[str]:
        texts: list[str] = [window.windowTitle()]
        for child in window.findChildren(QAbstractButton):
            texts.append(child.text())
        for child in window.findChildren(QLabel):
            texts.append(child.text())
        for child in window.findChildren(QGroupBox):
            texts.append(child.title())
        for child in window.findChildren(QComboBox):
            texts.extend(child.itemText(index) for index in range(child.count()))
        for child in window.findChildren(QLineEdit):
            texts.append(child.placeholderText())
        return [text for text in texts if any(text.startswith(p) for p in ("gui.", "app.", "cli."))]

    def test_gui_en_shows_no_raw_keys(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            self.assertEqual([], self._raw_keys_displayed(window))
        finally:
            window.close()

    def test_gui_zh_cn_shows_no_raw_keys(self) -> None:
        window = MainWindow(self.repo_root, language="zh_cn")
        try:
            self.assertEqual([], self._raw_keys_displayed(window))
        finally:
            window.close()

    def test_close_event_accepts_when_idle_without_prompt(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            event = QCloseEvent()
            with patch("gui.gui_mainwindow.QMessageBox.question") as question:
                window.closeEvent(event)
            question.assert_not_called()
            self.assertTrue(event.isAccepted())
        finally:
            window.close()

    def test_close_event_ignores_when_busy_close_is_cancelled(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            window.queue_busy = True
            event = QCloseEvent()
            with patch("gui.gui_mainwindow.QMessageBox.question", return_value=QMessageBox.No):
                window.closeEvent(event)
            self.assertFalse(event.isAccepted())
        finally:
            window.queue_busy = False
            window.close()

    def test_close_event_requests_stop_and_waits_when_busy_close_is_confirmed(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            window.queue_busy = True
            event = QCloseEvent()
            with patch("gui.gui_mainwindow.QMessageBox.question", return_value=QMessageBox.Yes), patch.object(
                window, "_stop_active_task"
            ) as stop_active_task:
                window.closeEvent(event)
            stop_active_task.assert_called_once()
            self.assertFalse(event.isAccepted())
            self.assertTrue(window._close_after_running_task_stops)
        finally:
            window.queue_busy = False
            window._close_after_running_task_stops = False
            window.close()

    def test_responsive_queue_view_fills_viewport_when_space_is_available(self) -> None:
        model = QueueTableModel(get_translator("en", self.repo_root / "config"))
        view = create_queue_view()
        view.setModel(model)
        view.resize(1700, 420)
        view.show()
        try:
            self.app.processEvents()
            self.app.processEvents()
            header = view.horizontalHeader()
            actual_total = sum(
                header.sectionSize(column)
                for column in range(model.columnCount())
                if not view.isColumnHidden(column)
            )
            viewport_width = view.viewport().width()
            self.assertLessEqual(abs(actual_total - viewport_width), 1)
        finally:
            view.close()

    def test_flex_columns_are_user_resizable(self) -> None:
        view = create_queue_view()
        model = QueueTableModel(get_translator("en", self.repo_root / "config"))
        view.setModel(model)
        try:
            header = view.horizontalHeader()
            self.assertEqual(header.sectionResizeMode(int(QueueColumn.NAME)), QHeaderView.Interactive)
            self.assertEqual(header.sectionResizeMode(int(QueueColumn.FOLDER)), QHeaderView.Interactive)
            self.assertEqual(header.sectionResizeMode(int(QueueColumn.RESOLUTION)), QHeaderView.Fixed)
        finally:
            view.close()

    def test_manual_resize_survives_reflow(self) -> None:
        model = QueueTableModel(get_translator("en", self.repo_root / "config"))
        view = create_queue_view()
        view.setModel(model)
        view.resize(1700, 420)
        view.show()
        try:
            self.app.processEvents()
            header = view.horizontalHeader()
            target_width = header.sectionSize(int(QueueColumn.NAME)) + 90
            header.resizeSection(int(QueueColumn.NAME), target_width)
            self.app.processEvents()
            self.app.processEvents()
            self.assertGreaterEqual(header.sectionSize(int(QueueColumn.NAME)), target_width)
        finally:
            view.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
