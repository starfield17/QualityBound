from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.i18n import Translator
from gui.window_geometry import clamped_window_size


class ActivityLogWindow(QMainWindow):
    def __init__(self, tr: Translator, parent=None) -> None:
        super().__init__(parent)
        self.tr = tr
        self.entries: list[tuple[str, str]] = []
        self._pending_entries: list[tuple[str, str]] = []
        self._flush_interval_ms = 75

        self._build_ui()
        self.apply_translations(tr)

    def _build_ui(self) -> None:
        self.resize(clamped_window_size(980, 640, minimum_width=560, minimum_height=360))
        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        controls = QHBoxLayout()
        self.filter_label = QLabel()
        self.filter_combo = QComboBox()
        self.filter_combo.addItem("all", "all")
        self.filter_combo.addItem("command", "command")
        self.filter_combo.addItem("process", "process")
        self.filter_combo.addItem("error", "error")
        self.export_button = QPushButton()
        self.clear_button = QPushButton()

        controls.addWidget(self.filter_label)
        controls.addWidget(self.filter_combo)
        controls.addStretch(1)
        controls.addWidget(self.export_button)
        controls.addWidget(self.clear_button)

        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMaximumBlockCount(50000)

        layout.addLayout(controls)
        layout.addWidget(self.log_output, 1)

        self.flush_timer = QTimer(self)
        self.flush_timer.setInterval(self._flush_interval_ms)
        self.flush_timer.timeout.connect(self._flush_pending_logs)

        self.filter_combo.currentIndexChanged.connect(self._refresh_log_view)
        self.clear_button.clicked.connect(self.clear_messages)
        self.export_button.clicked.connect(self._export_logs)

    def apply_translations(self, tr: Translator) -> None:
        self.tr = tr
        self.setWindowTitle(self.tr.t("gui.window.activity_log"))
        self.filter_label.setText(self.tr.t("gui.label.log_filter"))
        self.filter_combo.setItemText(0, self.tr.t("gui.filter.all"))
        self.filter_combo.setItemText(1, self.tr.t("gui.filter.command"))
        self.filter_combo.setItemText(2, self.tr.t("gui.filter.process"))
        self.filter_combo.setItemText(3, self.tr.t("gui.filter.error"))
        self.export_button.setText(self.tr.t("gui.button.export_log"))
        self.clear_button.setText(self.tr.t("gui.button.clear_log"))

    def _classify_message(self, message: str) -> str:
        if message.startswith("$ "):
            return "command"
        lowered = message.lower()
        if "error" in lowered or "failed" in lowered or "traceback" in lowered or "cancelled" in lowered:
            return "error"
        return "process"

    def append_message(self, message: str) -> None:
        category = self._classify_message(message)
        self.entries.append((category, message))
        self._pending_entries.append((category, message))
        if not self.flush_timer.isActive():
            self.flush_timer.start()

    def clear_messages(self) -> None:
        self.entries.clear()
        self._pending_entries.clear()
        self.flush_timer.stop()
        self.log_output.clear()

    def _flush_pending_logs(self) -> None:
        if not self._pending_entries:
            self.flush_timer.stop()
            return

        selected_filter = self.filter_combo.currentData() or "all"
        pending_entries = self._pending_entries
        self._pending_entries = []
        lines = [
            message
            for category, message in pending_entries
            if selected_filter == "all" or category == selected_filter
        ]
        if lines:
            self.log_output.appendPlainText("\n".join(lines))
            scrollbar = self.log_output.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

        if not self._pending_entries:
            self.flush_timer.stop()

    def _refresh_log_view(self) -> None:
        self._pending_entries = []
        self.flush_timer.stop()
        selected_filter = self.filter_combo.currentData() or "all"
        lines = [
            message
            for category, message in self.entries
            if selected_filter == "all" or category == selected_filter
        ]
        self.log_output.setPlainText("\n".join(lines))
        scrollbar = self.log_output.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _export_logs(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            self.tr.t("gui.dialog.export_log"),
            str(Path.home() / "qualitybound.log"),
            "Log Files (*.log *.txt);;All Files (*)",
        )
        if not path:
            return
        selected_filter = self.filter_combo.currentData() or "all"
        lines = [
            message
            for category, message in self.entries
            if selected_filter == "all" or category == selected_filter
        ]
        Path(path).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
