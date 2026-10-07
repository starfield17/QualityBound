from __future__ import annotations

from PySide6.QtWidgets import QLabel, QMainWindow, QProgressBar, QVBoxLayout, QWidget

from core.i18n import Translator
from gui.queue_model import QueueTableModel, format_duration
from gui.queue_view import create_queue_view
from gui.window_geometry import clamped_window_size


class QueueWindow(QMainWindow):
    def __init__(self, tr: Translator, model: QueueTableModel, parent=None) -> None:
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

        self.summary_label = QLabel()
        self.queue_progress_label = QLabel()
        self.queue_progress_bar = QProgressBar()
        self.queue_progress_bar.setRange(0, 1000)

        self.table_view = create_queue_view(self)
        self.table_view.setModel(self.model)

        layout.addWidget(self.summary_label)
        layout.addWidget(self.queue_progress_label)
        layout.addWidget(self.queue_progress_bar)
        layout.addWidget(self.table_view, 1)

    def apply_translations(self, tr: Translator) -> None:
        self.translator = tr
        self.setWindowTitle(self.translator.t("gui.window.queue"))
        self.model.set_translator(tr)

    def update_metrics(self, metrics) -> None:
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
