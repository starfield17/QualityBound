from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QRegularExpression, QTimer
from PySide6.QtGui import QColor, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.i18n import Translator
from gui.theme import current_tokens
from gui.window_geometry import clamped_window_size


@dataclass(frozen=True, slots=True)
class LogEntry:
    timestamp: str
    category: str
    message: str

    def display_text(self) -> str:
        label = {"command": "CMD", "error": "ERROR", "process": "INFO"}.get(self.category, "INFO")
        return f"{self.timestamp}  {label:<5}  {self.message}"


class LogHighlighter(QSyntaxHighlighter):
    _LINE = QRegularExpression(r"^\d{2}:\d{2}:\d{2}\s+(ERROR|CMD|INFO)")

    def highlightBlock(self, text: str) -> None:
        match = self._LINE.match(text)
        if not match.hasMatch():
            return
        label = match.captured(1)
        tokens = current_tokens()
        color = {
            "ERROR": tokens.danger,
            "CMD": tokens.accent,
            "INFO": tokens.muted,
        }.get(label, tokens.muted)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        fmt.setFontWeight(600)
        self.setFormat(match.capturedStart(1), len(label), fmt)


class ActivityLogWindow(QMainWindow):
    def __init__(self, tr: Translator, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.translator = tr
        self.entries: list[LogEntry] = []
        self._pending_entries: list[LogEntry] = []
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
        self.filter_combo.addItem("", "all")
        self.filter_combo.addItem("", "command")
        self.filter_combo.addItem("", "process")
        self.filter_combo.addItem("", "error")
        self.search_edit = QLineEdit()
        self.search_edit.setClearButtonEnabled(True)
        self.follow_check = QCheckBox()
        self.follow_check.setChecked(True)
        self.export_button = QPushButton()
        self.clear_button = QPushButton()

        controls.addWidget(self.filter_label)
        controls.addWidget(self.filter_combo)
        controls.addWidget(self.search_edit, 1)
        controls.addWidget(self.follow_check)
        controls.addWidget(self.export_button)
        controls.addWidget(self.clear_button)

        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMaximumBlockCount(50000)
        self.log_output.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.highlighter = LogHighlighter(self.log_output.document())

        layout.addLayout(controls)
        layout.addWidget(self.log_output, 1)

        self.flush_timer = QTimer(self)
        self.flush_timer.setInterval(self._flush_interval_ms)
        self.flush_timer.timeout.connect(self._flush_pending_logs)

        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(120)
        self.search_timer.timeout.connect(self._refresh_log_view)

        self.filter_combo.currentIndexChanged.connect(self._refresh_log_view)
        self.search_edit.textChanged.connect(lambda _text: self.search_timer.start())
        self.clear_button.clicked.connect(self.clear_messages)
        self.export_button.clicked.connect(self._export_logs)

    def apply_translations(self, tr: Translator) -> None:
        self.translator = tr
        self.setWindowTitle(self.translator.t("gui.window.activity_log"))
        self.filter_label.setText(self.translator.t("gui.label.log_filter"))
        self.filter_combo.setItemText(0, self.translator.t("gui.filter.all"))
        self.filter_combo.setItemText(1, self.translator.t("gui.filter.command"))
        self.filter_combo.setItemText(2, self.translator.t("gui.filter.process"))
        self.filter_combo.setItemText(3, self.translator.t("gui.filter.error"))
        self.search_edit.setPlaceholderText(self.translator.t("gui.log.search_placeholder"))
        self.follow_check.setText(self.translator.t("gui.log.follow"))
        self.export_button.setText(self.translator.t("gui.log.export_filtered"))
        self.clear_button.setText(self.translator.t("gui.button.clear_log"))
        self.log_output.setPlaceholderText(self.translator.t("gui.log.empty"))
        self._refresh_log_view()

    def _classify_message(self, message: str) -> str:
        if message.startswith("$ "):
            return "command"
        lowered = message.lower()
        if any(
            token in lowered
            for token in (
                "error",
                "failed",
                "traceback",
                "cancelled",
                "错误",
                "失败",
                "异常",
                "取消",
            )
        ):
            return "error"
        return "process"

    def append_message(self, message: str) -> None:
        entry = LogEntry(
            timestamp=time.strftime("%H:%M:%S"),
            category=self._classify_message(message),
            message=message,
        )
        self.entries.append(entry)
        self._pending_entries.append(entry)
        if not self.flush_timer.isActive():
            self.flush_timer.start()

    def clear_messages(self) -> None:
        self.entries.clear()
        self._pending_entries.clear()
        self.flush_timer.stop()
        self.log_output.clear()

    def _matches(self, entry: LogEntry, selected_filter: str, search: str) -> bool:
        if selected_filter != "all" and entry.category != selected_filter:
            return False
        return not search or search in entry.message.casefold()

    def _flush_pending_logs(self) -> None:
        if not self._pending_entries:
            self.flush_timer.stop()
            return

        selected_filter = str(self.filter_combo.currentData() or "all")
        search = self.search_edit.text().strip().casefold()
        pending_entries = self._pending_entries
        self._pending_entries = []
        lines = [
            entry.display_text()
            for entry in pending_entries
            if self._matches(entry, selected_filter, search)
        ]
        if lines:
            scrollbar = self.log_output.verticalScrollBar()
            was_at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
            self.log_output.appendPlainText("\n".join(lines))
            if self.follow_check.isChecked() or was_at_bottom:
                scrollbar.setValue(scrollbar.maximum())

        if not self._pending_entries:
            self.flush_timer.stop()

    def _refresh_log_view(self) -> None:
        self._pending_entries = []
        self.flush_timer.stop()
        selected_filter = str(self.filter_combo.currentData() or "all")
        search = self.search_edit.text().strip().casefold()
        lines = [
            entry.display_text()
            for entry in self.entries
            if self._matches(entry, selected_filter, search)
        ]
        self.log_output.setPlainText("\n".join(lines))
        scrollbar = self.log_output.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _export_logs(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            self.translator.t("gui.dialog.export_log"),
            str(Path.home() / "qualitybound.log"),
            "Log Files (*.log *.txt);;All Files (*)",
        )
        if not path:
            return
        selected_filter = str(self.filter_combo.currentData() or "all")
        search = self.search_edit.text().strip().casefold()
        lines = [
            entry.display_text()
            for entry in self.entries
            if self._matches(entry, selected_filter, search)
        ]
        Path(path).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
