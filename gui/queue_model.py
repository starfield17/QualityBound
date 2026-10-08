from __future__ import annotations

import copy
from collections.abc import Callable
from enum import IntEnum
from pathlib import Path
from typing import cast

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    QObject,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QStyle

from core.i18n import Translator
from core.media import human_kbps, validate_unique_output_paths
from core.models import DecisionOption, EncodeOptions, EncodeResult
from core.progress_events import ProgressEvent
from gui.queue_actions import (
    accept_size_miss as accept_size_miss_action,
    apply_options_to_record as apply_options_to_record_action,
    apply_output_dir_to_record as apply_output_dir_to_record_action,
    apply_quality_decision as apply_quality_decision_action,
    decision_options_for_record,
    discard_size_miss as discard_size_miss_action,
    can_edit_record,
    record_quality_result,
    retry_size_miss as retry_size_miss_action,
)
from gui.queue_state import (
    ACTIVE_ITEM_STATUSES,
    RUNNABLE_ITEM_STATUSES,
    QueueItemRecord,
    QueueItemStatus,
    QueueMetrics,
    assign_runtime_backend,
    apply_progress_event as apply_record_progress_event,
    build_tags,
    build_tooltip,
    compute_metrics,
    mark_cancelled,
    mark_failed,
    mark_finished,
    mark_started,
    prepare_record_for_execution,
    reset_for_retry,
    status_key,
)
from gui.qt_optionals import maybe_none


def format_duration(seconds: float | None) -> str:
    if not seconds:
        return "n/a"
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:d}:{secs:02d}"


def format_size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "n/a"
    negative = size_bytes < 0
    value = float(abs(size_bytes))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            formatted = f"{value:.2f} {unit}"
            return "-" + formatted if negative else formatted
        value /= 1024.0
    return str(size_bytes)


class QueueColumn(IntEnum):
    NAME = 0
    FOLDER = 1
    RESOLUTION = 2
    DURATION = 3
    SOURCE_BITRATE = 4
    TARGET_BITRATE = 5
    QUALITY = 6
    ENCODER = 7
    OUTPUT = 8
    TAGS = 9
    STATUS = 10
    PROGRESS = 11


COLUMN_COUNT = len(QueueColumn)


FIXED_COLUMN_WIDTHS: dict[QueueColumn, int] = {
    QueueColumn.RESOLUTION: 96,
    QueueColumn.DURATION: 84,
    QueueColumn.SOURCE_BITRATE: 110,
    QueueColumn.TARGET_BITRATE: 110,
    QueueColumn.QUALITY: 126,
    QueueColumn.STATUS: 108,
    QueueColumn.PROGRESS: 92,
}

FLEX_COLUMN_SPECS: dict[QueueColumn, tuple[int, int]] = {
    QueueColumn.NAME: (28, 180),
    QueueColumn.FOLDER: (20, 160),
    QueueColumn.ENCODER: (14, 130),
    QueueColumn.OUTPUT: (20, 150),
    QueueColumn.TAGS: (18, 120),
}


def _transient_index(index: QModelIndex | QPersistentModelIndex) -> QModelIndex:
    """Reduce the index union accepted by the base model to a QModelIndex.

    Qt hands QModelIndex to every model callback, while the PySide6 signatures
    declare the union because QPersistentModelIndex converts implicitly in C++.
    Normalising at the override boundary keeps the helpers and the Qt calls below
    them on a single index type.
    """
    if isinstance(index, QPersistentModelIndex):
        return QModelIndex(cast("QModelIndex", index))
    return index


class QueueTableModel(QAbstractTableModel):
    metricsChanged = Signal(object)

    def __init__(self, tr: Translator, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.translator = tr
        self._records: list[QueueItemRecord] = []
        self._metrics = QueueMetrics()

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        parent = _transient_index(parent)
        if parent.isValid():
            return 0
        return len(self._records)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        parent = _transient_index(parent)
        if parent.isValid():
            return 0
        return COLUMN_COUNT

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Vertical:
            return section + 1
        labels = {
            QueueColumn.NAME: self.translator.t("gui.table.name"),
            QueueColumn.FOLDER: self.translator.t("gui.table.folder"),
            QueueColumn.RESOLUTION: self.translator.t("gui.table.resolution"),
            QueueColumn.DURATION: self.translator.t("gui.table.duration"),
            QueueColumn.SOURCE_BITRATE: self.translator.t("gui.table.source_bitrate"),
            QueueColumn.TARGET_BITRATE: self.translator.t("gui.table.target_bitrate"),
            QueueColumn.QUALITY: self.translator.t("gui.table.quality"),
            QueueColumn.ENCODER: self.translator.t("gui.table.encoder"),
            QueueColumn.OUTPUT: self.translator.t("gui.table.output"),
            QueueColumn.TAGS: self.translator.t("gui.table.tags"),
            QueueColumn.STATUS: self.translator.t("gui.table.status"),
            QueueColumn.PROGRESS: self.translator.t("gui.table.progress"),
        }
        return labels.get(QueueColumn(section), "")

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        index = _transient_index(index)
        if not index.isValid():
            return None
        record = self._records[index.row()]
        column = QueueColumn(index.column())
        media = record.media_info

        if role == Qt.ItemDataRole.DisplayRole:
            if column == QueueColumn.NAME:
                return record.source_path.name
            if column == QueueColumn.FOLDER:
                return str(record.source_path.parent)
            if column == QueueColumn.RESOLUTION:
                if media and media.width and media.height:
                    return f"{media.width}x{media.height}"
                return "n/a"
            if column == QueueColumn.DURATION:
                return format_duration(media.duration if media else None)
            if column == QueueColumn.SOURCE_BITRATE:
                return human_kbps(media.video_bitrate_bps) if media else "n/a"
            if column == QueueColumn.TARGET_BITRATE:
                return human_kbps(record.plan_item.target_video_bitrate_bps) if record.plan_item.target_video_bitrate_bps else "n/a"
            if column == QueueColumn.QUALITY:
                segmented = record.plan_item.segmented_analysis_result
                if segmented is not None:
                    mean = segmented.final_mean_vmaf if segmented.final_mean_vmaf is not None else segmented.predicted_mean_vmaf
                    return f"{mean:.1f}" if mean is not None else "-"
                quality = record.plan_item.quality_search_result
                if quality is None or quality.min_vmaf is None:
                    return "-"
                ratio = quality.predicted_output_ratio or quality.required_output_ratio
                return (
                    f"{quality.min_vmaf:.1f} / {ratio * 100:.1f}%"
                    if ratio is not None
                    else f"{quality.min_vmaf:.1f}"
                )
            if column == QueueColumn.ENCODER:
                if record.assigned_encoder and record.assigned_backend:
                    return f"{record.assigned_encoder} ({record.assigned_backend})"
                encoder = record.plan_item.encoder_info
                return f"{encoder.encoder_name} ({encoder.backend.value})" if encoder else "n/a"
            if column == QueueColumn.OUTPUT:
                return record.output_path.name
            if column == QueueColumn.TAGS:
                return " ".join(build_tags(record, self.translator))
            if column == QueueColumn.STATUS:
                if record.status == QueueItemStatus.ANALYZING and record.analysis_candidate_limit:
                    return (
                        f"{self.translator.t(status_key(record.status))} "
                        f"{record.analysis_candidate_index}/{record.analysis_candidate_limit}"
                    )
                return self.translator.t(status_key(record.status))
            if column == QueueColumn.PROGRESS:
                if record.status in {
                    QueueItemStatus.QUEUED,
                    QueueItemStatus.WAITING_ANALYSIS,
                    QueueItemStatus.DRAFT,
                }:
                    return "-"
                return f"{max(0.0, min(100.0, record.file_progress)):.1f}%"
        elif role == Qt.ItemDataRole.ToolTipRole:
            if column == QueueColumn.FOLDER:
                return str(record.source_path.parent)
            if column == QueueColumn.OUTPUT:
                return str(record.output_path)
            return build_tooltip(record, self.translator)
        elif role == Qt.ItemDataRole.TextAlignmentRole:
            if column in {
                QueueColumn.RESOLUTION,
                QueueColumn.DURATION,
                QueueColumn.SOURCE_BITRATE,
                QueueColumn.TARGET_BITRATE,
                QueueColumn.QUALITY,
                QueueColumn.STATUS,
                QueueColumn.PROGRESS,
            }:
                return int(Qt.AlignmentFlag.AlignCenter)
        elif role == Qt.ItemDataRole.ForegroundRole and column in {QueueColumn.STATUS, QueueColumn.PROGRESS}:
            palette = {
                QueueItemStatus.RUNNING: QColor("#0B5394"),
                QueueItemStatus.WAITING_ANALYSIS: QColor("#666666"),
                QueueItemStatus.ANALYZING: QColor("#674EA7"),
                QueueItemStatus.ENCODING: QColor("#0B5394"),
                QueueItemStatus.VALIDATING: QColor("#134F5C"),
                QueueItemStatus.DONE: QColor("#38761D"),
                QueueItemStatus.FAILED: QColor("#A61C00"),
                QueueItemStatus.NEEDS_DECISION: QColor("#B45F06"),
                QueueItemStatus.CANCELLED: QColor("#7F6000"),
                QueueItemStatus.SKIPPED: QColor("#666666"),
                QueueItemStatus.PAUSED: QColor("#7F6000"),
            }
            return palette.get(record.status)
        elif role == Qt.ItemDataRole.DecorationRole and column == QueueColumn.STATUS:
            style = maybe_none(QApplication.style())
            if style is None:
                return None
            if record.status in ACTIVE_ITEM_STATUSES:
                return style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
            if record.status == QueueItemStatus.DONE:
                return style.standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton)
            if record.status == QueueItemStatus.FAILED:
                return style.standardIcon(QStyle.StandardPixmap.SP_MessageBoxCritical)
            if record.status == QueueItemStatus.NEEDS_DECISION:
                return style.standardIcon(QStyle.StandardPixmap.SP_MessageBoxQuestion)
            if record.status == QueueItemStatus.CANCELLED:
                return style.standardIcon(QStyle.StandardPixmap.SP_DialogCancelButton)
            if record.status == QueueItemStatus.SKIPPED:
                return style.standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning)
            if record.status == QueueItemStatus.PAUSED:
                return style.standardIcon(QStyle.StandardPixmap.SP_MediaPause)
        elif role == Qt.ItemDataRole.UserRole:
            return record.item_id
        return None

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        index = _transient_index(index)
        default_flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if not index.isValid():
            return default_flags | Qt.ItemFlag.ItemIsDropEnabled
        record = self._records[index.row()]
        if record.status not in ACTIVE_ITEM_STATUSES:
            default_flags |= Qt.ItemFlag.ItemIsDragEnabled
        return default_flags | Qt.ItemFlag.ItemIsDropEnabled

    def supportedDropActions(self) -> Qt.DropAction:
        return Qt.DropAction.MoveAction

    def moveRows(
        self,
        source_parent: QModelIndex | QPersistentModelIndex,
        source_row: int,
        count: int,
        destination_parent: QModelIndex | QPersistentModelIndex,
        destination_child: int,
    ) -> bool:
        source_parent = _transient_index(source_parent)
        destination_parent = _transient_index(destination_parent)
        if count <= 0:
            return False
        if source_parent.isValid() or destination_parent.isValid():
            return False
        if source_row < 0 or source_row + count > len(self._records):
            return False
        if destination_child < 0 or destination_child > len(self._records):
            return False
        if destination_child >= source_row and destination_child <= source_row + count:
            return False
        moving = self._records[source_row : source_row + count]
        if any(record.status in ACTIVE_ITEM_STATUSES for record in moving):
            return False

        self.beginMoveRows(source_parent, source_row, source_row + count - 1, destination_parent, destination_child)
        del self._records[source_row : source_row + count]
        if destination_child > source_row:
            destination_child -= count
        for offset, record in enumerate(moving):
            self._records.insert(destination_child + offset, record)
        self.endMoveRows()
        self._emit_metrics_changed()
        return True

    def set_translator(self, tr: Translator) -> None:
        self.translator = tr
        if self.rowCount() > 0:
            top_left = self.index(0, 0)
            bottom_right = self.index(self.rowCount() - 1, self.columnCount() - 1)
            self.dataChanged.emit(top_left, bottom_right)
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, self.columnCount() - 1)
        self._emit_metrics_changed()

    def records(self) -> list[QueueItemRecord]:
        return self._records

    def metrics(self) -> QueueMetrics:
        return self._metrics

    def record_for_row(self, row: int) -> QueueItemRecord | None:
        if row < 0 or row >= len(self._records):
            return None
        return self._records[row]

    def record_for_id(self, item_id: str) -> tuple[int, QueueItemRecord] | tuple[None, None]:
        for row, record in enumerate(self._records):
            if record.item_id == item_id:
                return row, record
        return None, None

    def add_records(self, records: list[QueueItemRecord]) -> None:
        if not records:
            return
        validate_unique_output_paths(
            (record.source_path, record.output_path)
            for record in [*self._records, *records]
        )
        start = len(self._records)
        end = start + len(records) - 1
        self.beginInsertRows(QModelIndex(), start, end)
        self._records.extend(records)
        self.endInsertRows()
        self._emit_metrics_changed()

    def remove_rows_by_index(self, rows: list[int]) -> int:
        targets = sorted({row for row in rows if 0 <= row < len(self._records)}, reverse=True)
        removed = 0
        for row in targets:
            if self._records[row].status in ACTIVE_ITEM_STATUSES:
                continue
            self.beginRemoveRows(QModelIndex(), row, row)
            del self._records[row]
            self.endRemoveRows()
            removed += 1
        if removed:
            self._emit_metrics_changed()
        return removed

    def clear_completed(self, *, excluded_item_ids: set[str] | None = None) -> int:
        excluded = excluded_item_ids or set()
        targets = [
            row
            for row, record in enumerate(self._records)
            if record.item_id not in excluded
            and record.status
            in {
                QueueItemStatus.DONE,
                QueueItemStatus.FAILED,
                QueueItemStatus.SKIPPED,
                QueueItemStatus.CANCELLED,
            }
        ]
        return self.remove_rows_by_index(targets)

    def retry_rows(self, rows: list[int]) -> int:
        retried = 0
        changed_rows: list[int] = []
        for row in sorted(set(rows)):
            record = self.record_for_row(row)
            if record is None:
                continue
            if record.status not in {QueueItemStatus.FAILED, QueueItemStatus.CANCELLED}:
                continue
            reset_for_retry(record)
            retried += 1
            changed_rows.append(row)
        self._emit_rows_changed(changed_rows)
        return retried

    def decision_options_for_row(self, row: int) -> list[DecisionOption]:
        record = self.record_for_row(row)
        return decision_options_for_record(record) if record is not None else []

    def apply_quality_decision(self, row: int, decision: DecisionOption) -> bool:
        record = self.record_for_row(row)
        if record is None or record.status != QueueItemStatus.NEEDS_DECISION:
            return False
        if record_quality_result(record) is None:
            return False
        resolved = apply_quality_decision_action(record, decision)
        self._emit_rows_changed([row])
        return resolved

    def accept_size_miss(self, row: int) -> bool:
        record = self.record_for_row(row)
        if (
            record is None
            or record.status != QueueItemStatus.NEEDS_DECISION
            or record.result is None
            or record.result.rejected_output_path is None
        ):
            return False
        resolved = accept_size_miss_action(record)
        self._emit_rows_changed([row])
        return resolved

    def discard_size_miss(self, row: int) -> bool:
        record = self.record_for_row(row)
        if (
            record is None
            or record.status != QueueItemStatus.NEEDS_DECISION
            or record.result is None
            or record.result.rejected_output_path is None
        ):
            return False
        resolved = discard_size_miss_action(record)
        self._emit_rows_changed([row])
        return resolved

    def retry_size_miss(self, row: int) -> bool:
        record = self.record_for_row(row)
        if (
            record is None
            or record.status != QueueItemStatus.NEEDS_DECISION
            or record.result is None
            or record.result.rejected_output_path is None
        ):
            return False
        resolved = retry_size_miss_action(record)
        self._emit_rows_changed([row])
        return resolved

    def prepare_for_execution(self, item_ids: list[str]) -> None:
        changed_rows: list[int] = []
        for item_id in item_ids:
            row, record = self.record_for_id(item_id)
            if row is None or record is None:
                continue
            if record.status in RUNNABLE_ITEM_STATUSES:
                prepare_record_for_execution(record)
                changed_rows.append(row)
        self._emit_rows_changed(changed_rows)

    def execution_records(self) -> list[QueueItemRecord]:
        return [
            record for record in self._records
            if record.status in RUNNABLE_ITEM_STATUSES
        ]

    def mark_running(self, item_id: str) -> None:
        row, record = self.record_for_id(item_id)
        if row is None or record is None:
            return
        mark_started(record)
        self._emit_rows_changed([row])

    def mark_cancelled(self, item_id: str, message: str | None = None) -> None:
        row, record = self.record_for_id(item_id)
        if row is None or record is None:
            return
        mark_cancelled(record, message)
        self._emit_rows_changed([row])

    def mark_failed(self, item_id: str, message: str | None = None) -> None:
        row, record = self.record_for_id(item_id)
        if row is None or record is None:
            return
        mark_failed(record, message)
        self._emit_rows_changed([row])

    def apply_progress_event(self, event: ProgressEvent) -> None:
        item_id = str(event.get("queue_item_id") or "")
        if not item_id:
            return
        row, record = self.record_for_id(item_id)
        if row is None or record is None:
            return
        apply_record_progress_event(record, event)
        self._emit_rows_changed([row])

    def apply_result(self, item_id: str, result: EncodeResult) -> None:
        row, record = self.record_for_id(item_id)
        if row is None or record is None:
            return
        mark_finished(record, result)
        self._emit_rows_changed([row])

    def assign_backend(self, item_id: str, backend: str, encoder: str) -> None:
        row, record = self.record_for_id(item_id)
        if row is None or record is None:
            return
        assign_runtime_backend(record, backend, encoder)
        self._emit_rows_changed([row])

    def can_remove_rows(self, rows: list[int]) -> bool:
        for row in rows:
            record = self.record_for_row(row)
            if record is not None and record.status in ACTIVE_ITEM_STATUSES:
                return False
        return True

    def can_retry_rows(self, rows: list[int]) -> bool:
        return any(
            (record := self.record_for_row(row)) is not None
            and record.status in {QueueItemStatus.FAILED, QueueItemStatus.CANCELLED}
            for row in rows
        )

    def can_edit_rows(self, rows: list[int]) -> bool:
        if not rows:
            return False
        for row in rows:
            record = self.record_for_row(row)
            if record is None or not can_edit_record(record):
                return False
        return True

    def _atomic_edit_rows(
        self,
        rows: list[int],
        edit: Callable[[QueueItemRecord], bool],
    ) -> int:
        targets = sorted(set(rows))
        if not targets or not self.can_edit_rows(targets):
            raise RuntimeError("Every selected queue item must be editable.")
        replacements: dict[int, QueueItemRecord] = {}
        for row in targets:
            record = self.record_for_row(row)
            assert record is not None
            candidate = copy.deepcopy(record)
            if not edit(candidate):
                raise RuntimeError(f"Queue item {record.source_path.name} is not editable.")
            replacements[row] = candidate
        combined = [replacements.get(row, record) for row, record in enumerate(self._records)]
        validate_unique_output_paths(
            (record.source_path, record.output_path) for record in combined
        )
        for row, replacement in replacements.items():
            self._records[row] = replacement
        self._emit_rows_changed(targets)
        return len(targets)

    def apply_options_to_rows(
        self,
        rows: list[int],
        options: EncodeOptions,
        *,
        config_dir: Path | None = None,
        runtime_capabilities: dict | None = None,
    ) -> int:
        return self._atomic_edit_rows(
            rows,
            lambda record: apply_options_to_record_action(
                record,
                options,
                config_dir=config_dir,
                runtime_capabilities=runtime_capabilities,
            ),
        )

    def apply_output_dir_to_rows(self, rows: list[int], output_dir: Path) -> int:
        return self._atomic_edit_rows(
            rows,
            lambda record: apply_output_dir_to_record_action(record, output_dir),
        )

    def can_resolve_row(self, row: int) -> bool:
        record = self.record_for_row(row)
        return (
            record is not None
            and record.status == QueueItemStatus.NEEDS_DECISION
            and record.result is not None
        )

    def _emit_rows_changed(self, rows: list[int]) -> None:
        clean_rows = sorted({row for row in rows if 0 <= row < len(self._records)})
        if clean_rows:
            for row in clean_rows:
                self.dataChanged.emit(self.index(row, 0), self.index(row, self.columnCount() - 1))
        self._emit_metrics_changed()

    def _emit_metrics_changed(self) -> None:
        self._metrics = compute_metrics(self._records)
        self.metricsChanged.emit(self._metrics)
