"""Explicit Qt fixture disposal; native lifetime failures remain test failures."""

from __future__ import annotations

import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QWidget

from gui.gui_mainwindow import MainWindow


def dispose_window(window: QWidget) -> None:
    """Finish owned workers and destroy the fixture before Python collects it."""
    if not shiboken6.isValid(window):
        return
    if isinstance(window, MainWindow):
        window.shutdown()
    window.close()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
