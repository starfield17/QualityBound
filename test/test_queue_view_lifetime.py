from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QWidget

from gui.queue.view import ResponsiveQueueTableView


class ReflowProbe(ResponsiveQueueTableView):
    def __init__(self, parent: QWidget | None = None) -> None:
        self.reflows = 0
        super().__init__(parent)

    def _apply_reflow(self) -> None:
        self.reflows += 1
        super()._apply_reflow()


class QueueViewLifetimeTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_destroyed_view_cancels_pending_reflow(self) -> None:
        for delete_parent in (False, True):
            with self.subTest(delete_parent=delete_parent):
                parent = QWidget()
                view = ReflowProbe(parent)
                view.schedule_reflow()
                (parent if delete_parent else view).deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                with patch.object(sys, "excepthook") as errors:
                    self.app.processEvents()
                errors.assert_not_called()
                self.assertEqual(view.reflows, 0)
                if not delete_parent:
                    parent.deleteLater()
                    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_live_view_reflows_once_per_event_turn(self) -> None:
        view = ReflowProbe()
        try:
            view.schedule_reflow()
            view.schedule_reflow()
            self.app.processEvents()
            self.assertEqual(view.reflows, 1)
            view.schedule_reflow()
            self.app.processEvents()
            self.assertEqual(view.reflows, 2)
        finally:
            view.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
