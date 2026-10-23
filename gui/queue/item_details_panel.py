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

from core.i18n import Text, Translator
from gui.icons import set_button_icon
from gui.queue.model import format_size, DETAILS_ACTUAL_SIZE
from gui.queue.state import QueueItemRecord, QueueItemStatus, build_tags, status_text


DETAILS_CLOSE = Text(
    en='Close details',
    zh_cn='关闭详情',
)


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
        self._busy = False
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
        set_button_icon(self.close_button, "close")
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
        set_button_icon(self.open_source_button, "open_folder")
        set_button_icon(self.open_output_button, "open_folder")
        self.apply_translations(translator)

    def apply_translations(self, translator: Translator) -> None:
        self.translator = translator
        self.title_label.setText(translator.t(Text(en='Selected item', zh_cn='选中项目')))
        self.close_button.setToolTip(translator.t(DETAILS_CLOSE))
        self.close_button.setAccessibleName(translator.t(DETAILS_CLOSE))
        self.open_source_button.setText(translator.t(Text(en='Open source folder', zh_cn='打开源目录')))
        self.open_output_button.setText(translator.t(Text(en='Open output folder', zh_cn='打开输出目录')))
        self.compare_button.setText(translator.t(Text(en='Compare source and output', zh_cn='对比输入与输出')))
        self.resolve_button.setText(translator.t(Text(en='Resolve decision', zh_cn='处理决定')))
        self.copy_button.setText(translator.t(Text(en='Copy details', zh_cn='复制详情')))
        if self.record is not None:
            self.set_record(self.record, translator)

    def set_record(self, record: QueueItemRecord | None, translator: Translator) -> None:
        self.record = record
        self.translator = translator
        if record is None:
            self.setVisible(False)
            return
        self.setVisible(True)
        text = self._details_text(record, translator)
        if text != self.details_view.toPlainText():
            scrollbar = self.details_view.verticalScrollBar()
            position = scrollbar.value()
            self.details_view.setPlainText(text)
            scrollbar.setValue(position)
        can_compare = (
            record.status == QueueItemStatus.DONE
            or (record.result is not None and record.result.rejected_output_path is not None)
        )
        has_output = record.plan_item is not None
        self.open_output_button.setEnabled(has_output)
        self.compare_button.setEnabled(can_compare and record.source_path.exists())
        self.resolve_button.setEnabled(record.status == QueueItemStatus.NEEDS_DECISION and not self._busy)
        self.resolve_button.setVisible(record.status == QueueItemStatus.NEEDS_DECISION)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.resolve_button.setEnabled(
            not busy and self.record is not None and self.record.status == QueueItemStatus.NEEDS_DECISION
        )

    def _details_text(self, record: QueueItemRecord, translator: Translator) -> str:
        tr = translator.t
        lines = [
            f"{tr(Text(en='Status', zh_cn='状态'))}: {tr(status_text(record.status))}",
            f"{tr(Text(en='Source', zh_cn='输入源'))}: {record.source_path}",
        ]
        if record.plan_item is not None:
            lines.append(f"{tr(Text(en='Output', zh_cn='输出'))}: {record.output_path}")
            quality = record.bound_plan_item.quality_search_result
            segmented = record.bound_plan_item.segmented_analysis_result
            planned_quality: float | None = None
            planned_ratio: float | None = None
            measured_quality = segmented is not None and segmented.final_mean_vmaf is not None
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
                lines.append(f"{tr(Text(en='Measured VMAF', zh_cn='实测 VMAF')) if measured_quality else tr(Text(en='Planned VMAF', zh_cn='计划 VMAF'))}: {'' if measured_quality else '~'}{planned_quality:.1f}")
            if planned_ratio is not None:
                lines.append(f"{tr(Text(en='Planned size ratio', zh_cn='计划体积比例'))}: ~{planned_ratio * 100:.1f}%")
            if record.result is not None and record.result.rejected_output_path is not None:
                lines.append(f"{tr(Text(en='Preserved file', zh_cn='保留文件'))}: {record.result.rejected_output_path}")
                lines.append(f"{tr(Text(en='Preserved file size (not published)', zh_cn='保留文件体积（尚未发布）'))}: {format_size(record.result.actual_output_bytes)}")
            actual_bytes = self._actual_output_bytes(record)
            if actual_bytes is not None:
                lines.append(f"{tr(DETAILS_ACTUAL_SIZE)}: {format_size(actual_bytes)}")
                source_bytes = self._source_bytes(record)
                if source_bytes is not None:
                    lines.append(
                        f"{tr(Text(en='Saved space', zh_cn='节省空间'))}: {format_size(source_bytes - actual_bytes)}"
                    )
        tags = build_tags(record, translator)
        if tags:
            lines.append(f"{tr(Text(en='Tags', zh_cn='标签'))}: {', '.join(tags)}")
        warnings = record.bound_plan_item.warnings if record.plan_item is not None else []
        if warnings:
            lines.append(f"{tr(Text(en='Warnings', zh_cn='警告'))}: {'; '.join(warnings)}")
        reason = record.error_summary
        if not reason and record.plan_item is not None:
            quality = record.bound_plan_item.quality_search_result
            reason = quality.reason if quality is not None else None
        if reason:
            lines.append(f"{tr(Text(en='Reason', zh_cn='原因'))}: {reason}")
        return "\n".join(lines)

    @staticmethod
    def _actual_output_bytes(record: QueueItemRecord) -> int | None:
        if record.status != QueueItemStatus.DONE:
            return None
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
