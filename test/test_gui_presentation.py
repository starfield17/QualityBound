from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from core.i18n import Translator
from core.models import (
    BackendChoice,
    CodecChoice,
    CompressionMode,
    EncodeOptions,
    EncodePlanItem,
    EncoderInfo,
    MediaInfo,
    QualitySearchResult,
    QualitySearchStatus,
)
from gui.activity_log_window import ActivityLogWindow
from gui.comparison.window import ComparisonWindow
from gui.gui_mainwindow import MainWindow
from gui.options.preset_manager_dialog import PresetManagerDialog
from gui.queue.model import QueueColumn, QueueTableModel, format_duration
from gui.queue.state import QueueItemRecord, QueueItemStatus, QueueJobSnapshot, build_tags
from gui.options.settings_dialog import SettingsDialog
from gui.theme import DARK, LIGHT, apply_theme, build_stylesheet


ROOT = Path(__file__).resolve().parent.parent


def _qapp() -> QApplication:
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication([])
    return app


def _record(root: Path, status: QueueItemStatus = QueueItemStatus.DONE) -> QueueItemRecord:
    source = root / "source.mkv"
    source.write_bytes(b"source")
    output = root / "output.mp4"
    item = EncodePlanItem(
        source_path=source,
        output_path=output,
        media_info=MediaInfo(source, 60.0, 2_000_000, 1_800_000, 128_000, 1280, 720, 30.0, "h264", "aac"),
        encoder_info=EncoderInfo(CodecChoice.HEVC, BackendChoice.CPU, "libx265", True, "slow"),
        options=EncodeOptions(compression_mode=CompressionMode.SMART, codec=CodecChoice.HEVC),
        quality_search_result=QualitySearchResult(
            encoder_name="libx265",
            backend=BackendChoice.CPU,
            status=QualitySearchStatus.FOUND,
            min_vmaf=91.25,
            predicted_output_ratio=0.61,
        ),
    )
    return QueueItemRecord(
        item_id="item",
        plan_item=item,
        job_snapshot=QueueJobSnapshot(root / "work", root / "ffmpeg", root / "ffprobe", root),
        status=status,
        total_passes=1,
    )


class GuiPresentationTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _qapp()

        apply_theme()

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def test_post_encode_action_text_covers_every_action_in_both_languages(self) -> None:
        from core.media import PostEncodeAction
        from gui.power_action_dialog import POST_ENCODE_ACTION_TEXT, post_encode_action_text
        self.assertEqual(set(POST_ENCODE_ACTION_TEXT), set(PostEncodeAction))
        for action, en, zh in (
            (PostEncodeAction.DO_NOTHING, "Do nothing", "无操作"),
            (PostEncodeAction.SLEEP, "Sleep system", "系统睡眠"),
            (PostEncodeAction.SHUTDOWN, "Shut down system", "系统关机"),
            (PostEncodeAction.QUIT, "Quit app", "退出程序"),
        ):
            self.assertEqual(Translator("en").t(post_encode_action_text(action)), en)
            self.assertEqual(Translator("zh_cn").t(post_encode_action_text(action)), zh)

    def test_light_and_dark_stylesheets_use_distinct_surfaces(self) -> None:
        self.assertIn("#F6F8FB", build_stylesheet(LIGHT))
        self.assertIn("#16181D", build_stylesheet(DARK))
        self.assertIn("QualityBound theme", self.app.styleSheet())

    def test_status_bar_keeps_live_state_when_logging(self) -> None:
        window = MainWindow(ROOT, language="en")
        try:
            window._set_status_snapshot("Encoding", "source.mkv", "1.2x", "0:10", 40.0)
            window._append_log("a log line")
            self.assertEqual(window.statusBar().currentMessage(), "")
            self.assertIn("Encoding", window.status_stage_label.text())
            self.assertIn("source.mkv", window.status_file_label.text())
        finally:
            window.close()

    def test_empty_state_and_details_panel_follow_the_queue(self) -> None:
        window = MainWindow(ROOT, language="en")
        try:
            self.assertIs(window.queue_stack.currentWidget(), window.empty_state)
            record = _record(self.root)
            window.queue_model.add_records([record])
            self.assertIs(window.queue_stack.currentWidget(), window.table_view)
            window.table_view.selectRow(0)
            window._update_details_panel()
            self.assertFalse(window.details_panel.isHidden())
            self.assertIn(record.source_path.name, window.details_panel.details_view.toPlainText())
            window._clear_details_panel()
            self.assertTrue(window.details_panel.isHidden())
        finally:
            window.close()

    def test_table_marks_estimated_quality_and_keeps_status_legible(self) -> None:
        model = QueueTableModel(Translator('en'))
        record = _record(self.root)
        model.add_records([record])
        quality = model.data(model.index(0, int(QueueColumn.QUALITY)))
        status = model.data(model.index(0, int(QueueColumn.STATUS)))
        self.assertTrue(str(quality).startswith("~"))
        self.assertEqual(status, "Done")
        self.assertEqual(format_duration(None), "—")
        self.assertNotIn("Decision", build_tags(record, Translator('en')))
        self.assertEqual(
            model.headerData(
                int(QueueColumn.STATUS),
                Qt.Orientation.Horizontal,
                int(Qt.ItemDataRole.DisplayRole),
            ),
            "Status",
        )

    def test_activity_log_timestamps_and_classifies_localized_errors(self) -> None:
        log = ActivityLogWindow(Translator('zh_cn'))
        try:
            log.append_message("错误：编码失败")
            entry = log.entries[-1]
            self.assertEqual(entry.category, "error")
            self.assertRegex(entry.display_text(), r"^\d\d:\d\d:\d\d\s+ERROR")
            log.follow_check.setChecked(False)
            self.assertFalse(log.follow_check.isChecked())
        finally:
            log.close()

    def test_preset_manager_uses_item_data_and_confirms_delete(self) -> None:
        dialog = PresetManagerDialog(
            Translator('en'),
            ["default_hevc", "anime"],
            "default_hevc",
        )
        try:
            self.assertEqual(dialog._selected_preset_name(), "default_hevc")
            dialog.list_widget.setCurrentRow(1)
            self.assertEqual(dialog._selected_preset_name(), "anime")
            with patch("gui.options.preset_manager_dialog.QMessageBox.question", return_value=QMessageBox.StandardButton.No):
                dialog._request_delete()
            self.assertIsNone(dialog.selected_action)
            with patch("gui.options.preset_manager_dialog.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                dialog._request_delete()
            self.assertEqual(dialog.selected_action, {"action": "delete", "name": "anime"})
        finally:
            dialog.close()

    def test_settings_redetect_stays_open_and_validation_marks_missing_paths(self) -> None:
        dialog = SettingsDialog(Translator('en'), {"language": "en"})
        try:
            dialog._request_redetect()
            self.assertTrue(dialog.redetect_requested)
            self.assertFalse(dialog.detection_hint_label.isHidden())
            dialog.ffmpeg_edit.setText(str(self.root / "missing-ffmpeg"))
            dialog._check_paths()
            self.assertIn("not a file", dialog.path_status_label.text().lower())
            self.assertTrue(dialog.path_status_label.property("invalid"))
        finally:
            dialog.close()

    def test_comparison_starts_with_placeholders_and_collapsed_help(self) -> None:
        window = ComparisonWindow(Translator('en'))
        try:
            self.assertIs(window.panes[0].stack.currentWidget(), window.panes[0].placeholder)
            self.assertTrue(window.help_label.isHidden())
            window.help_button.setChecked(True)
            self.assertFalse(window.help_label.isHidden())
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
