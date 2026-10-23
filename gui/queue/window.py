from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Slot
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QProgressBar,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.i18n import Translator
from gui.queue.model import QueueTableModel, format_duration
from gui.queue.state import QueueMetrics
from gui.queue.view import create_queue_view
from gui.window_geometry import clamped_window_size


class QueueWindow(QMainWindow):
    def __init__(self, tr: Translator, model: QueueTableModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.translator = tr
        self.model = model
        self._build_ui()
        self.apply_translations(tr)

    def _build_ui(self) -> None:
        self.resize(clamped_window_size(1280, 760, minimum_width=640, minimum_height=420))
        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.action_toolbar = QToolBar(self)
        self.action_toolbar.setMovable(False)
        self.action_toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.action_toolbar.setIconSize(QSize(18, 18))
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.action_toolbar)

        self.summary_label = QLabel()
        self.queue_progress_label = QLabel()
        self.queue_progress_bar = QProgressBar()
        self.queue_progress_bar.setRange(0, 1000)
        self.hint_label = QLabel()
        self.hint_label.setObjectName("setupSummary")
        self.hint_label.setWordWrap(True)

        self.table_view = create_queue_view(self)
        self.table_view.setModel(self.model)
        self.drop_overlay = QLabel(self)
        self.drop_overlay.setObjectName("dropOverlay")
        self.drop_overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop_overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.drop_overlay.hide()

        layout.addWidget(self.summary_label)
        layout.addWidget(self.queue_progress_label)
        layout.addWidget(self.queue_progress_bar)
        layout.addWidget(self.table_view, 1)
        layout.addWidget(self.hint_label)

    def attach_actions(self, actions: list[QAction]) -> None:
        for index, action in enumerate(actions):
            button = QToolButton(self.action_toolbar)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setDefaultAction(action)
            if index in (0, 2):
                button.setObjectName("primaryAction" if index == 0 else "dangerAction")
            self.action_toolbar.addWidget(button)

    @Slot(bool)
    def set_drop_overlay_visible(self, visible: bool) -> None:
        self.drop_overlay.setText(self.translator.t("gui.drop.overlay"))
        viewport = self.table_view.viewport()
        self.drop_overlay.setGeometry(QRect(viewport.mapTo(self, QPoint()), viewport.size()))
        self.drop_overlay.setVisible(visible and self.table_view.acceptDrops())
        self.drop_overlay.raise_()

    def apply_translations(self, tr: Translator) -> None:
        self.translator = tr
        self.setWindowTitle(self.translator.t("gui.window.queue"))
        self.hint_label.setText(self.translator.t("gui.window.queue_hint"))
        self.model.set_translator(tr)
        self.table_view.horizontalHeader().setToolTip(self.translator.t("gui.tooltip.table_header"))

    def update_metrics(self, metrics: QueueMetrics) -> None:
        states_text = self.translator.t(
            "gui.summary.queue_states",
            ready=metrics.ready_items,
            running=metrics.running_items,
            failed=metrics.failed_items,
            decision=metrics.needs_decision_items,
        )
        total_duration = format_duration(metrics.total_duration_sec)
        self.summary_label.setText(
            self.translator.t(
                "gui.summary.queue_window",
                total=metrics.total_items,
                states=states_text,
                duration=total_duration,
            )
        )
        eta_text = format_duration(metrics.eta_sec) if metrics.eta_sec else self.translator.t("gui.value.unknown")
        self.queue_progress_label.setText(
            self.translator.t(
                "gui.summary.queue_progress",
                percent=f"{metrics.queue_percent:.1f}",
                completed=metrics.completed_items,
                total=metrics.total_items,
                eta=eta_text,
            )
        )
        self.queue_progress_bar.setValue(int(round(metrics.queue_percent * 10)))
