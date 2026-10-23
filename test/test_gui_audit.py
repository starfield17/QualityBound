"""Behavioral regressions found by the GUI interaction/lifetime audit."""
from __future__ import annotations

import json
import os
from pathlib import Path
import unittest
from collections.abc import Callable
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMenu, QMessageBox

from core.i18n import LANGUAGES, Translator
from core.models import VideoFileItem
from gui.activity_log_window import ActivityLogWindow
from gui.comparison.decode import ComparisonMedia, DecodedFrame, FramePair, ToolRunner, preview_pair
from gui.comparison.playback import PlaybackSession
from gui.comparison.state import ComparisonPhase, VideoDetails
from gui.comparison.window import ComparisonWindow
from gui.gui_mainwindow import MainWindow
from gui.options.settings_dialog import SettingsDialog
from gui.queue.state import QueueSourceDraft, create_draft_record
from qt_lifetime import dispose_window


class ComparisonInteractionAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.window = ComparisonWindow(Translator('en'))
        self.addCleanup(dispose_window, self.window)
        self.window.media = ComparisonMedia(
            (Path('a.mp4'), Path('b.mp4')),
            (VideoDetails(10, 0, 25), VideoDetails(12, 0, 30)), Path('ffmpeg'), Path('ffprobe'),
        )
        self.window.paths = list(self.window.media.paths)
        self.window.phase = ComparisonPhase.PAUSED
        self.window._update_controls()
        self.session = MagicMock(spec=PlaybackSession)
        self.session.audio_available = True
        self.session.tick.return_value = 0
        player = patch.object(self.window, 'playback', self.session)
        player.start()
        self.addCleanup(player.stop)
        self.pending: list[Callable[[object], None]] = []
        launch = patch.object(self.window, '_launch', side_effect=lambda _task, callback: self.pending.append(callback))
        launch.start()
        self.addCleanup(launch.stop)
        self.window.show()
        self.window.activateWindow()
        self.app.processEvents()

    def finish_seek(self) -> None:
        png = b'P6\n2 2\n255\n' + b'\x00\x00\xff' * 4
        position = self.window.position
        self.pending[-1](FramePair(DecodedFrame(position, png), DecodedFrame(position, png), 0, position))

    def test_tab_and_t_switch_single_view_but_shift_tab_navigates(self) -> None:
        self.window.mode.setCurrentIndex(1)
        self.window.play_button.setFocus()
        self.app.processEvents()
        QTest.keyClick(self.window.play_button, Qt.Key.Key_Tab)
        self.assertTrue(self.window.panes[0].isHidden())
        QTest.keyClick(self.window.play_button, Qt.Key.Key_T)
        self.assertTrue(self.window.panes[1].isHidden())
        QTest.keyClick(self.window.play_button, Qt.Key.Key_Tab, Qt.KeyboardModifier.ShiftModifier)
        self.assertFalse(self.window.play_button.hasFocus())
        self.assertTrue(self.window.panes[1].isHidden())

    def test_space_works_after_slider_drag_and_canvas_click(self) -> None:
        slider = self.window.slider
        QTest.mouseClick(slider, Qt.MouseButton.LeftButton, pos=QPoint(slider.width() // 2, 5))
        self.finish_seek()
        slider.setFocus()
        self.app.processEvents()
        QTest.keyClick(slider, Qt.Key.Key_Space)
        self.assertEqual(self.window.phase, ComparisonPhase.PLAYING)
        self.window.pause()
        self.finish_seek()
        self.window.offset.setFocus()
        self.app.processEvents()
        canvas = self.window.panes[0].canvas
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertTrue(canvas.hasFocus())
        QTest.keyClick(canvas, Qt.Key.Key_Space)
        self.assertEqual(self.window.phase, ComparisonPhase.PLAYING)

    def test_editing_and_popup_keep_their_keys(self) -> None:
        self.window.mode.setCurrentIndex(1)
        self.window.offset.setFocus()
        self.app.processEvents()
        QTest.keyClick(self.window.offset, Qt.Key.Key_Tab)
        self.assertFalse(self.window.offset.hasFocus())
        self.assertTrue(self.window.panes[1].isHidden())
        self.window.mode.setFocus()
        self.window.mode.showPopup()
        self.app.processEvents()
        try:
            QTest.keyClick(self.window.mode.view(), Qt.Key.Key_T)
            self.assertTrue(self.window.panes[1].isHidden())
            self.assertEqual(self.window.phase, ComparisonPhase.PAUSED)
        finally:
            self.window.mode.hidePopup()

    def test_pause_during_seek_does_not_restart_playback(self) -> None:
        self.window._play()
        self.window.seek(3)
        self.session.start.reset_mock()
        self.assertIn('Pause', self.window.play_button.text())
        self.window.toggle_play()
        self.assertEqual(self.window.phase, ComparisonPhase.SEEKING)
        self.session.start.assert_not_called()
        self.finish_seek()
        self.assertEqual(self.window.phase, ComparisonPhase.PAUSED)
        self.session.start.assert_not_called()

    def test_repeated_seeks_and_toggles_use_latest_intent(self) -> None:
        self.window._play()
        self.window.seek(2)
        self.window.toggle_play()
        self.window.seek(3)
        self.finish_seek()
        self.assertEqual(self.window.phase, ComparisonPhase.PAUSED)
        self.window.seek(4)
        self.window.toggle_play()
        self.finish_seek()
        self.assertEqual(self.window.phase, ComparisonPhase.PLAYING)

    def test_pause_during_scrub_survives_release(self) -> None:
        self.window._play()
        self.window._scrub_started()
        self.window.toggle_play()
        self.window._scrub_finished()
        self.finish_seek()
        self.assertEqual(self.window.phase, ComparisonPhase.PAUSED)


class PreviewTailAuditTests(unittest.TestCase):
    def test_normal_preview_uses_covering_frame_when_forward_seek_hits_eof(self) -> None:
        media = ComparisonMedia((Path('a'), Path('b')), (VideoDetails(1, 0, 25), VideoDetails(2, 0, 25)),
                                Path('explicit-ffmpeg'), Path('explicit-ffprobe'))
        runner = MagicMock(spec=ToolRunner)
        runner.stderr = b'[showinfo] pts_time:0.99'
        runner.run.side_effect = [b'', json.dumps({'frames': [
            {'best_effort_timestamp_time': '0.0'}, {'best_effort_timestamp_time': '0.96'},
        ]}).encode(), b'png-a', b'png-b']
        result = preview_pair(media, .99, 0, runner)
        self.assertEqual(result.a, DecodedFrame(.96, b'png-a'))
        self.assertEqual(result.b, DecodedFrame(.99, b'png-b'))
        self.assertEqual(result.position, .99)
        self.assertEqual([call.args[0][0] for call in runner.run.call_args_list],
                         ['explicit-ffmpeg', 'explicit-ffprobe', 'explicit-ffmpeg', 'explicit-ffmpeg'])

    def test_normal_preview_preserves_tool_failures(self) -> None:
        media = ComparisonMedia((Path('a'), Path('b')), (VideoDetails(1, 0, 25), VideoDetails(1, 0, 25)),
                                Path('explicit-ffmpeg'), Path('explicit-ffprobe'))
        runner = MagicMock(spec=ToolRunner)
        runner.run.side_effect = ValueError('broken explicit tool')
        with self.assertRaisesRegex(ValueError, 'broken explicit tool'):
            preview_pair(media, .99, 0, runner)
        runner.run.assert_called_once()


class WindowLifetimeAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        for target, kwargs in (
            ('gui.gui_mainwindow.load_app_config', {'return_value': {}}),
            ('gui.gui_mainwindow.MainWindow._persist_runtime_state', {}),
            ('gui.gui_mainwindow.MainWindow._save_window_geometry', {}),
        ):
            mock = patch(target, **kwargs)
            mock.start()
            self.addCleanup(mock.stop)
        self.window = MainWindow(Path(__file__).resolve().parent.parent, language='en')
        self.window._startup_encoder_detection_started = True
        self.addCleanup(dispose_window, self.window)

    def test_detection_does_not_bypass_unfinished_queue_close_warning(self) -> None:
        self.window.queue_model.add_records([create_draft_record(
            QueueSourceDraft(VideoFileItem(Path('unstarted.mp4'), Path('unstarted.mp4')), None))])
        worker = MagicMock()
        self.window.encoder_detection_worker = worker
        with patch('gui.gui_mainwindow.QMessageBox.question', return_value=QMessageBox.StandardButton.No) as ask:
            event = QCloseEvent()
            self.window.closeEvent(event)
            ask.assert_called_once()
            self.assertFalse(event.isAccepted())
            self.assertFalse(self.window._close_after_running_task_stops)
            worker.cancel.assert_not_called()
        with (patch('gui.gui_mainwindow.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes) as ask,
              patch.object(self.window, '_maybe_close_after_running_task')):
            self.window.closeEvent(QCloseEvent())
            ask.assert_called_once()
            worker.cancel.assert_called_once()
            self.assertTrue(self.window._close_after_running_task_stops)
        self.window.encoder_detection_worker = None

    def test_cancelled_settings_and_context_menus_are_released(self) -> None:
        baseline = len(self.window.findChildren(QMenu))
        def close_menus() -> None:
            for menu in self.window.findChildren(QMenu):
                menu.close()
        def reject_dialog() -> None:
            dialog = QApplication.activeModalWidget()
            if isinstance(dialog, QDialog):
                dialog.reject()
        for _ in range(3):
            QTimer.singleShot(0, close_menus)
            self.window._show_queue_context_menu(self.window.table_view, QPoint())
            QTimer.singleShot(0, close_menus)
            self.window._show_header_context_menu(self.window.table_view, QPoint())
            QTimer.singleShot(0, reject_dialog)
            self.window._open_settings_dialog()
            QTimer.singleShot(0, reject_dialog)
            self.window._open_preset_manager()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.assertEqual(len(self.window.findChildren(QMenu)), baseline)
            self.assertEqual(self.window.findChildren(SettingsDialog), [])
            self.assertEqual(self.window.findChildren(QDialog), [])


class AuxiliaryLayoutAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_comparison_and_log_fit_narrow_windows_in_every_language(self) -> None:
        for language in LANGUAGES:
            for widget_type in (ComparisonWindow, ActivityLogWindow):
                with self.subTest(language=language.code, widget=widget_type.__name__):
                    widget = widget_type(Translator(language.code))
                    try:
                        font = widget.font()
                        font.setPointSize(14)
                        widget.setFont(font)
                        widget.resize(760, 520)
                        widget.show()
                        for _ in range(3):
                            self.app.processEvents()
                        self.assertLessEqual(widget.width(), 760)
                        self.assertLessEqual(widget.minimumSizeHint().width(), 760)
                        if isinstance(widget, ComparisonWindow):
                            widget.help_button.setChecked(True)
                            self.app.processEvents()
                            self.assertLessEqual(widget.width(), 760)
                            self.assertLessEqual(widget.height(), 520)
                            self.assertTrue(widget.help_label.isVisible())
                    finally:
                        dispose_window(widget)
