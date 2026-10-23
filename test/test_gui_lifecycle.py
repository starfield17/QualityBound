from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class GuiLifecycleTests(unittest.TestCase):
    def test_gui_help_uses_the_selected_language_without_starting_qt(self) -> None:
        self.run_child('''
            import contextlib
            import io
            from unittest.mock import patch
            from gui.gui_entry import run_gui

            with patch('gui.gui_entry.ensure_runtime_layout'), patch('gui.gui_entry.QApplication') as app:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    try:
                        run_gui(['--lang', 'ja', '--help'])
                    except SystemExit as exc:
                        assert exc.code == 0
                assert '表示言語' in output.getvalue(), output.getvalue()
                app.assert_not_called()
        ''')

    def run_child(self, source: str) -> None:
        result = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(source)],
            cwd=ROOT,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_entry_cleans_running_detection_after_early_exit_and_exception(self) -> None:
        for exceptional in (False, True):
            with self.subTest(exceptional=exceptional):
                self.run_child(f'''
                    import gc
                    import tempfile
                    import threading
                    from pathlib import Path
                    from unittest.mock import patch
                    import shiboken6
                    from PySide6.QtCore import QTimer
                    from PySide6.QtWidgets import QApplication
                    from core.models import OperationCancelledError
                    from gui.gui_entry import run_gui
                    from gui.gui_mainwindow import MainWindow

                    app = QApplication([])
                    started = threading.Event()
                    cancelled = threading.Event()
                    windows = []
                    real_exec = app.exec

                    def probe(*args, cancel_check, **kwargs):
                        started.set()
                        while not cancel_check():
                            cancelled.wait(0.01)
                        cancelled.set()
                        raise OperationCancelledError()

                    def make_window(*args, **kwargs):
                        window = MainWindow(*args, **kwargs)
                        windows.append(window)
                        return window

                    def exit_when_started():
                        if started.is_set():
                            app.exit(7)
                        else:
                            QTimer.singleShot(10, exit_when_started)

                    def execute():
                        QTimer.singleShot(0, exit_when_started)
                        result = real_exec()
                        assert windows[0].encoder_detection_worker.isRunning()
                        if {exceptional!r}:
                            raise RuntimeError('event loop failed')
                        return result

                    with tempfile.TemporaryDirectory() as directory, \
                         patch('core.config.store.app_config_path', return_value=Path(directory) / 'app.json'), \
                         patch('gui.gui_entry.ensure_runtime_layout'), \
                         patch('gui.gui_entry.app_icon_path', return_value=None), \
                         patch('gui.gui_entry.QApplication', return_value=app), \
                         patch('gui.gui_entry.MainWindow', side_effect=make_window), \
                         patch('gui.options.capability_worker.find_binary', return_value=Path('ffmpeg')), \
                         patch('gui.options.capability_worker.ensure_encoder_capabilities', side_effect=probe), \
                         patch.object(app, 'exec', side_effect=execute):
                        try:
                            assert run_gui(['--lang', 'ja']) == 7
                            assert not {exceptional!r}
                        except RuntimeError as exc:
                            assert {exceptional!r} and str(exc) == 'event loop failed'
                        assert cancelled.is_set(), 'worker was not cancelled and joined'
                        assert not shiboken6.isValid(windows[0]), 'window was not disposed'
                    app.processEvents()
                    gc.collect()
                ''')

    def test_shutdown_cancels_all_owners_and_is_idempotent(self) -> None:
        self.run_child('''
            import tempfile
            import threading
            from pathlib import Path
            from unittest.mock import patch
            from PySide6.QtCore import QCoreApplication, QEvent, QThread
            from PySide6.QtWidgets import QApplication
            from gui.gui_mainwindow import MainWindow

            class Worker(QThread):
                def __init__(self):
                    super().__init__()
                    self.cancelled = threading.Event()
                def cancel(self):
                    self.cancelled.set()
                def run(self):
                    self.cancelled.wait()

            app = QApplication([])
            with tempfile.TemporaryDirectory() as directory, patch(
                'core.config.store.app_config_path', return_value=Path(directory) / 'app.json'
            ):
                window = MainWindow(Path.cwd(), language='ja')
                workers = [Worker() for _ in range(4)]
                window.encoder_detection_worker, window.active_worker = workers[:2]
                window.queue_manager._worker = workers[2]
                window.queue_completion_handler._copy_worker = workers[3]
                for worker in workers:
                    worker.start()
                window.shutdown()
                window.shutdown()
                assert all(worker.cancelled.is_set() and not worker.isRunning() for worker in workers)
                window._start_encoder_capability_detection(force_refresh=True)
                assert window.encoder_detection_worker is None, 'shutdown restarted detection'
                window.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        ''')

    def test_pending_finished_callback_cannot_restart_detection_after_shutdown(self) -> None:
        self.run_child('''
            import tempfile
            from pathlib import Path
            from unittest.mock import patch
            from PySide6.QtCore import QCoreApplication, QEvent, QTimer
            from PySide6.QtWidgets import QApplication
            from gui.gui_mainwindow import MainWindow

            app = QApplication([])
            with tempfile.TemporaryDirectory() as directory, patch(
                'core.config.store.app_config_path', return_value=Path(directory) / 'app.json'
            ):
                window = MainWindow(Path.cwd(), language='ja')
                # A finished callback already queued before shutdown must not
                # undo its terminal state when pending Qt work is delivered.
                QTimer.singleShot(0, window, window._on_encoder_capability_detection_finished)
                window.shutdown()
                app.processEvents()
                with patch('gui.gui_mainwindow.EncoderCapabilityDetectWorker') as detector:
                    window._start_encoder_capability_detection(force_refresh=True)
                    assert not detector.called, 'pending finished callback restarted detection after shutdown'
                window.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        ''')

    def test_closed_windows_can_be_collected_together(self) -> None:
        self.run_child('''
            import gc
            import tempfile
            from pathlib import Path
            from unittest.mock import patch
            from PySide6.QtWidgets import QApplication
            from gui.gui_mainwindow import MainWindow

            app = QApplication([])
            with tempfile.TemporaryDirectory() as directory, patch(
                'core.config.store.app_config_path', return_value=Path(directory) / 'app.json'
            ):
                windows = []
                for index in range(10):
                    window = MainWindow(Path.cwd(), language='ja')
                    window._startup_encoder_detection_started = True
                    window.show()
                    window.resize(760, 520)
                    window._set_setup_collapsed(False)
                    for _ in range(8):
                        app.processEvents()
                    window.close()
                    windows.append(window)
                windows.clear()
                del window
                gc.collect()
                app.processEvents()
        ''')
