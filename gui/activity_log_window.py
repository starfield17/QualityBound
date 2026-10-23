from __future__ import annotations

import time
import re
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
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.i18n import Text, Translator
from gui.theme import current_tokens
from gui.icons import action_icon
from gui.window_geometry import clamped_window_size


LOG_EXPORT_FAILED = Text(
    en='Could not export the log to {path}:\n{error}',
    zh_cn='无法将日志导出至 {path}：\n{error}',
    ja='ログを {path} にエクスポートできませんでした:\n{error}',
)


@dataclass(frozen=True, slots=True)
class LogEntry:
    timestamp: str
    category: str
    message: str

    def display_text(self) -> str:
        label = {"command": "CMD", "error": "ERROR", "process": "INFO", "debug": "DEBUG"}.get(self.category, "INFO")
        return f"{self.timestamp}  {label:<5}  {self.message}"


class LogHighlighter(QSyntaxHighlighter):
    _LINE = QRegularExpression(r"^\d{2}:\d{2}:\d{2}\s+(ERROR|CMD|INFO|DEBUG)")

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
        self._log_level = "debug"

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
        tokens = current_tokens()
        self.search_icon_action = self.search_edit.addAction(action_icon("search", tokens.icon, tokens.icon_disabled), QLineEdit.ActionPosition.LeadingPosition)
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
        self.search_edit.textChanged.connect(self._on_search_changed)
        self.clear_button.clicked.connect(self.clear_messages)
        self.follow_check.toggled.connect(self._follow_changed)
        self.export_button.clicked.connect(self._export_logs)

    def apply_translations(self, tr: Translator) -> None:
        self.translator = tr
        self.setWindowTitle(self.translator.t(Text(en='Activity Log', zh_cn='活动日志', ja='アクティビティログ')))
        self.filter_label.setText(self.translator.t(Text(en='Log filter', zh_cn='日志过滤', ja='ログフィルター')))
        self.filter_combo.setItemText(0, self.translator.t(Text(en='All', zh_cn='全部', ja='すべて')))
        self.filter_combo.setItemText(1, self.translator.t(Text(en='Command', zh_cn='命令', ja='コマンド')))
        self.filter_combo.setItemText(2, self.translator.t(Text(en='Process', zh_cn='过程', ja='プロセス')))
        self.filter_combo.setItemText(3, self.translator.t(Text(en='Error', zh_cn='错误', ja='エラー')))
        self.search_edit.setPlaceholderText(self.translator.t(Text(en='Search log…', zh_cn='搜索日志…', ja='ログを検索…')))
        self.follow_check.setText(self.translator.t(Text(en='Follow output', zh_cn='跟随输出', ja='出力を自動スクロール')))
        self.export_button.setText(self.translator.t(Text(en='Export filtered log', zh_cn='导出筛选后的日志', ja='フィルター適用ログをエクスポート')))
        self.clear_button.setText(self.translator.t(Text(en='Clear Log', zh_cn='清空日志', ja='ログを消去')))
        self.log_output.setPlaceholderText(self.translator.t(Text(en='No activity yet.', zh_cn='暂无活动。', ja='アクティビティはまだありません。')))
        self._refresh_log_view()

    def refresh_icons(self) -> None:
        tokens = current_tokens()
        self.search_icon_action.setIcon(action_icon("search", tokens.icon, tokens.icon_disabled))

    def _follow_changed(self, enabled: bool) -> None:
        if enabled:
            self.log_output.verticalScrollBar().setValue(self.log_output.verticalScrollBar().maximum())

    def _on_search_changed(self, _text: str) -> None:
        self.search_timer.start()

    def _classify_message(self, message: str) -> str:
        if message.startswith("$ "):
            return "command"
        # Only controlled severity prefixes count; filenames and counters do not.
        if re.match(r"^\s*(?:\[debug\]|debug:)", message, re.IGNORECASE):
            return "debug"
        if re.match(r"^\s*(?:\[?(?:error|fatal|failed|traceback)\]?(?:[:：\s]|$)|(?:错误|失败|异常|エラー|失敗|例外)[:：\s])", message, re.IGNORECASE):
            return "error"
        return "process"

    def set_log_level(self, level: str) -> None:
        self._log_level = "debug" if level == "debug" else "info"
        self._refresh_log_view()

    def append_message(self, message: str, *, category: str | None = None) -> None:
        entry = LogEntry(
            timestamp=time.strftime("%H:%M:%S"),
            category=category or self._classify_message(message),
            message=message,
        )
        self.entries.append(entry)
        if len(self.entries) > 50_000:
            del self.entries[:1_000]
        self._pending_entries.append(entry)
        if not self.flush_timer.isActive():
            self.flush_timer.start()

    def clear_messages(self) -> None:
        self.entries.clear()
        self._pending_entries.clear()
        self.flush_timer.stop()
        self.log_output.clear()

    def _matches(self, entry: LogEntry, selected_filter: str, search: str) -> bool:
        if self._log_level == "info" and entry.category in {"command", "debug"}:
            return False
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
            position = scrollbar.value()
            horizontal = self.log_output.horizontalScrollBar().value()
            self.log_output.appendPlainText("\n".join(lines))
            scrollbar.setValue(scrollbar.maximum() if self.follow_check.isChecked() else position)
            self.log_output.horizontalScrollBar().setValue(horizontal)

        if not self._pending_entries:
            self.flush_timer.stop()

    def _refresh_log_view(self) -> None:
        scrollbar = self.log_output.verticalScrollBar()
        position = scrollbar.value()
        horizontal = self.log_output.horizontalScrollBar().value()
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
        scrollbar.setValue(scrollbar.maximum() if self.follow_check.isChecked() else position)
        self.log_output.horizontalScrollBar().setValue(horizontal)

    def _export_logs(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            self.translator.t(Text(en='Export log', zh_cn='导出日志', ja='ログのエクスポート')),
            str(Path.home() / "qualitybound.log"),
            self.translator.t(Text(en='Log files (*.log *.txt);;All files (*)', zh_cn='日志文件 (*.log *.txt);;所有文件 (*)', ja='ログファイル (*.log *.txt);;すべてのファイル (*)')),
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
        try:
            Path(path).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(
                self, self.translator.t(Text(en='Warning', zh_cn='警告', ja='警告')),
                self.translator.t(LOG_EXPORT_FAILED, path=path, error=str(exc)),
            )
