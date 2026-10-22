from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.i18n import Translator
from gui.queue.model import format_size
from gui.queue.state import QueueItemRecord, QueueItemStatus, build_tags, status_key


class ItemDetailsPanel(QFrame):
    """Read-only details and actions for the selected queue row."""

    openSource = Signal()
    openOutput = Signal()
    compare = Signal()
    resolve = Signal()
    closeRequested = Signal()

    def __init__(self, translator: Translator, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.translator = translator
        self.record: QueueItemRecord | None = None
        self.setObjectName("detailsPanel")
        self.setVisible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        header = QHBoxLayout()
        self.title_label = QLabel()
        self.title_label.setObjectName("summaryValue")
        header.addWidget(self.title_label)
        header.addStretch(1)
        self.close_button = QToolButton()
        self.close_button.setText("×")
        self.close_button.setAutoRaise(True)
        self.close_button.clicked.connect(self.closeRequested.emit)
        header.addWidget(self.close_button)
        layout.addLayout(header)

        self.details_view = QPlainTextEdit()
        self.details_view.setReadOnly(True)
        self.details_view.setMaximumHeight(112)
        self.details_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        layout.addWidget(self.details_view, 1)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        self.open_source_button = QPushButton()
        self.open_output_button = QPushButton()
        self.compare_button = QPushButton()
        self.resolve_button = QPushButton()
        self.resolve_button.setProperty("role", "primary")
        self.copy_button = QPushButton()
        actions.addWidget(self.open_source_button)
        actions.addWidget(self.open_output_button)
        actions.addWidget(self.compare_button)
        actions.addWidget(self.resolve_button)
        actions.addStretch(1)
        actions.addWidget(self.copy_button)
        layout.addLayout(actions)

        self.open_source_button.clicked.connect(self.openSource.emit)
        self.open_output_button.clicked.connect(self.openOutput.emit)
        self.compare_button.clicked.connect(self.compare.emit)
        self.resolve_button.clicked.connect(self.resolve.emit)
        self.copy_button.clicked.connect(self._copy_details)
        self.apply_translations(translator)

    def apply_translations(self, translator: Translator) -> None:
        self.translator = translator
        self.title_label.setText(translator.t("gui.details.title"))
        self.close_button.setToolTip(translator.t("gui.details.close"))
        self.open_source_button.setText(translator.t("gui.details.open_source"))
        self.open_output_button.setText(translator.t("gui.details.open_output"))
        self.compare_button.setText(translator.t("gui.details.compare"))
        self.resolve_button.setText(translator.t("gui.details.resolve"))
        self.copy_button.setText(translator.t("gui.button.copy_details"))
        if self.record is not None:
            self.set_record(self.record, translator)

    def set_record(self, record: QueueItemRecord | None, translator: Translator) -> None:
        self.record = record
        self.translator = translator
        if record is None:
            self.setVisible(False)
            return
        self.setVisible(True)
        self.details_view.setPlainText(self._details_text(record, translator))
        can_compare = (
            record.status == QueueItemStatus.DONE
            or (record.result is not None and record.result.rejected_output_path is not None)
        )
        has_output = record.plan_item is not None
        self.open_output_button.setEnabled(has_output)
        self.compare_button.setEnabled(can_compare and record.source_path.exists())
        self.resolve_button.setEnabled(record.status == QueueItemStatus.NEEDS_DECISION)
        self.resolve_button.setVisible(record.status == QueueItemStatus.NEEDS_DECISION)

    def _details_text(self, record: QueueItemRecord, translator: Translator) -> str:
        tr = translator.t
        lines = [
            f"{tr('gui.details.status')}: {tr(status_key(record.status))}",
            f"{tr('gui.details.source')}: {record.source_path}",
        ]
        if record.plan_item is not None:
            lines.append(f"{tr('gui.details.output')}: {record.output_path}")
            quality = record.bound_plan_item.quality_search_result
            segmented = record.bound_plan_item.segmented_analysis_result
            planned_quality: float | None = None
            planned_ratio: float | None = None
            if segmented is not None:
                planned_quality = (
                    segmented.final_mean_vmaf
                    if segmented.final_mean_vmaf is not None
                    else segmented.predicted_mean_vmaf
                )
            if quality is not None:
                planned_quality = quality.min_vmaf
                planned_ratio = quality.predicted_output_ratio or quality.required_output_ratio
            if planned_quality is not None:
                lines.append(f"{tr('gui.details.planned_quality')}: {planned_quality:.1f}")
            if planned_ratio is not None:
                lines.append(f"{tr('gui.details.planned_size')}: {planned_ratio * 100:.1f}%")
            actual_bytes = self._actual_output_bytes(record)
            if actual_bytes is not None:
                lines.append(f"{tr('gui.details.actual_size')}: {format_size(actual_bytes)}")
                source_bytes = self._source_bytes(record)
                if source_bytes is not None:
                    lines.append(
                        f"{tr('gui.details.saved_space')}: {format_size(source_bytes - actual_bytes)}"
                    )
        tags = build_tags(record, translator)
        if tags:
            lines.append(f"{tr('gui.details.tags')}: {', '.join(tags)}")
        warnings = record.bound_plan_item.warnings if record.plan_item is not None else []
        if warnings:
            lines.append(f"{tr('gui.details.warnings')}: {'; '.join(warnings)}")
        reason = record.error_summary
        if not reason and record.plan_item is not None:
            quality = record.bound_plan_item.quality_search_result
            reason = quality.reason if quality is not None else None
        if reason:
            lines.append(f"{tr('gui.details.reason')}: {reason}")
        return "\n".join(lines)

    @staticmethod
    def _actual_output_bytes(record: QueueItemRecord) -> int | None:
        if record.result is not None and record.result.actual_output_bytes is not None:
            return record.result.actual_output_bytes
        try:
            if record.status == QueueItemStatus.DONE and record.output_path.exists():
                return record.output_path.stat().st_size
        except OSError:
            return None
        return None

    @staticmethod
    def _source_bytes(record: QueueItemRecord) -> int | None:
        try:
            return record.source_path.stat().st_size
        except OSError:
            media = record.media_info
            if media is not None and media.duration > 0 and media.format_bitrate_bps:
                return int(media.duration * media.format_bitrate_bps / 8.0)
        return None

    def _copy_details(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self.details_view.toPlainText())
