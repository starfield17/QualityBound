from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QPainter, QPaintEvent
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.i18n import Translator
from core.models import ConstraintFailureKind, DecisionActionCode, DecisionOption
from gui.queue.state import QueueItemRecord
from gui.window_geometry import clamped_window_size
from gui.queue.model import format_size


class SizeMissDecision(str, Enum):
    ACCEPT = "accept"
    RETRY = "retry"
    DISCARD = "discard"


class _LimitBar(QProgressBar):
    def __init__(self, limit_fraction: float) -> None:
        super().__init__()
        self.limit_fraction = limit_fraction

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setPen(self.palette().text().color())
        x = round(self.contentsRect().width() * self.limit_fraction)
        painter.drawLine(x, 0, x, self.height())


def _quality_action_text(tr: Translator, option: DecisionOption) -> str:
    value = option.suggested_value
    if option.action_code == DecisionActionCode.RELAX_SIZE and isinstance(value, (int, float)):
        return tr.t("gui.decision.relax_size", value=f"{float(value) * 100:.1f}%")
    if option.action_code == DecisionActionCode.RELAX_QUALITY and isinstance(value, (int, float)):
        return tr.t("gui.decision.relax_quality", value=f"{float(value):.1f}")
    if option.action_code == DecisionActionCode.CHANGE_MEDIA_BUDGET:
        return tr.t("gui.decision.change_media_budget")
    if option.action_code == DecisionActionCode.REANALYZE:
        return tr.t("gui.decision.reanalyze")
    return tr.t("gui.decision.skip")


def _quality_action_description(tr: Translator, option: DecisionOption) -> str:
    if option.action_code == DecisionActionCode.RELAX_SIZE:
        return tr.t("gui.decision.relax_size_detail")
    if option.action_code == DecisionActionCode.RELAX_QUALITY:
        return tr.t("gui.decision.relax_quality_detail")
    if option.action_code == DecisionActionCode.CHANGE_MEDIA_BUDGET:
        return tr.t("gui.decision.change_media_budget_detail")
    if option.action_code == DecisionActionCode.REANALYZE:
        return tr.t("gui.decision.reanalyze_detail")
    return tr.t("gui.decision.skip_detail")


class _DecisionDialog(QDialog):
    def __init__(self, tr: Translator, parent: QWidget | None) -> None:
        super().__init__(parent)
        self.translator = tr
        self.choice: object | None = None
        self.setModal(True)
        self.resize(clamped_window_size(600, 470, minimum_width=520, minimum_height=380))
        outer = QVBoxLayout(self)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        scroll.setWidget(body)
        outer.addWidget(scroll)
        self.body_layout = QVBoxLayout(body)
        self.body_layout.setContentsMargins(18, 16, 18, 16)
        self.body_layout.setSpacing(10)
        self.title_label = QLabel()
        self.title_label.setObjectName("decisionTitle")
        self.file_label = QLabel()
        self.file_label.setObjectName("decisionFile")
        self.file_label.setWordWrap(True)
        self.intro_label = QLabel()
        self.intro_label.setWordWrap(True)
        self.body_layout.addWidget(self.title_label)
        self.body_layout.addWidget(self.file_label)
        self.body_layout.addWidget(self.intro_label)
        self.metrics = QFrame()
        self.metrics.setObjectName("statCard")
        self.metrics_layout = QGridLayout(self.metrics)
        self.metrics_layout.setContentsMargins(12, 10, 12, 10)
        self.metrics_layout.setHorizontalSpacing(12)
        self.metrics_layout.setVerticalSpacing(6)
        self.body_layout.addWidget(self.metrics)
        self.reason_label = QLabel()
        self.reason_label.setObjectName("decisionDetail")
        self.reason_label.setWordWrap(True)
        self.body_layout.addWidget(self.reason_label)
        self.body_layout.addStretch(1)
        self.actions_layout = QVBoxLayout()
        self.actions_layout.setSpacing(6)
        self.body_layout.addLayout(self.actions_layout)
        self.footer = QHBoxLayout()
        self.footer.addStretch(1)
        self.decide_later_button = QPushButton()
        self.decide_later_button.setDefault(True)
        self.decide_later_button.clicked.connect(self.reject)
        self.footer.addWidget(self.decide_later_button)
        self.body_layout.addLayout(self.footer)

    def add_metric(self, row: int, label: str, value: str) -> None:
        name = QLabel(label)
        name.setObjectName("summaryTitle")
        text = QLabel(value)
        text.setObjectName("summaryValue")
        text.setWordWrap(True)
        text.setMinimumWidth(0)
        text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.metrics_layout.addWidget(name, row, 0)
        self.metrics_layout.addWidget(text, row, 1)
        self.metrics_layout.setColumnStretch(1, 1)

    def add_action(
        self,
        text: str,
        description: str,
        choice: object,
        *,
        role: str = "",
    ) -> QPushButton:
        button = QPushButton(text)
        button.setToolTip(description)
        button.setAccessibleDescription(description)
        button.setAutoDefault(False)
        if role:
            button.setProperty("role", role)
        button.clicked.connect(lambda _checked=False, value=choice: self._choose(value))
        self.actions_layout.addWidget(button)
        detail = QLabel(description)
        detail.setObjectName("decisionDetail")
        detail.setWordWrap(True)
        self.actions_layout.addWidget(detail)
        return button

    def _choose(self, choice: object) -> None:
        self.choice = choice
        self.accept()


def _quality_metrics(dialog: _DecisionDialog, record: QueueItemRecord) -> None:
    item = record.bound_plan_item
    quality = item.quality_search_result
    segmented = item.segmented_analysis_result
    tr = dialog.translator
    row = 0
    dialog.add_metric(
        row,
        tr.t("gui.decision.quality_target"),
        f"{item.options.min_vmaf:.1f}",
    )
    row += 1
    measured: float | None = None
    if quality is not None and quality.min_vmaf is not None:
        measured = quality.min_vmaf
    elif segmented is not None:
        measured = segmented.final_mean_vmaf if segmented.final_mean_vmaf is not None else segmented.predicted_mean_vmaf
    if measured is not None:
        final = segmented is not None and segmented.final_mean_vmaf is not None
        dialog.add_metric(row, tr.t("gui.decision.measured_quality") if final else tr.t("gui.decision.estimated_quality"),
                          ("" if final else "~") + f"{measured:.1f}")
        row += 1
    if quality is not None and quality.predicted_output_ratio is not None:
        dialog.add_metric(
            row,
            tr.t("gui.decision.output_ratio"),
            f"~{quality.predicted_output_ratio * 100:.1f}%",
        )
        row += 1
    if quality is not None and quality.required_output_ratio is not None:
        dialog.add_metric(
            row,
            tr.t("gui.decision.required_ratio"),
            f"~{quality.required_output_ratio * 100:.1f}%",
        )
        row += 1
    reason = quality.reason if quality is not None else None
    if not reason and segmented is not None:
        reason = segmented.reason
    dialog.reason_label.setText(
        f"{tr.t('gui.decision.reason')}: {reason or record.error_summary or tr.t('gui.decision.no_metrics')}"
    )


def _size_metrics(dialog: _DecisionDialog, record: QueueItemRecord) -> None:
    result = record.result
    if result is None:
        return
    actual = int(result.actual_output_bytes or 0)
    allowed = int(result.allowed_output_bytes or 0)
    tr = dialog.translator
    dialog.add_metric(0, tr.t("gui.decision.actual_size"), format_size(actual))
    dialog.add_metric(1, tr.t("gui.decision.size_limit"), format_size(allowed))
    scale = max(actual, allowed, 1) * 1.1
    bar = _LimitBar(allowed / scale)
    bar.setRange(0, 1000)
    bar.setValue(round(actual / scale * 1000))
    bar.setTextVisible(True)
    ratio = actual / allowed * 100 if allowed > 0 else 0
    bar.setFormat(f"{format_size(actual)} / {format_size(allowed)} · {ratio:.1f}%")
    bar.setToolTip(tr.t("gui.decision.size_ratio", value=f"{max(0.0, ratio - 100):.1f}"))
    bar.setProperty("over", actual > allowed)
    dialog.metrics_layout.addWidget(bar, 2, 0, 1, 2)
    if result.rejected_output_path is not None:
        dialog.add_metric(3, tr.t("gui.decision.preserved_path"), str(result.rejected_output_path))


def choose_quality_decision(
    parent: QWidget,
    tr: Translator,
    record: QueueItemRecord,
    options: list[DecisionOption],
) -> DecisionOption | None:
    quality = record.bound_plan_item.quality_search_result or record.bound_plan_item.segmented_analysis_result
    quality_miss = quality is not None and quality.failure_kind == ConstraintFailureKind.QUALITY_UNREACHABLE
    dialog = _DecisionDialog(tr, parent)
    if quality_miss:
        dialog.setWindowTitle(tr.t("gui.decision.quality_miss_title"))
        dialog.title_label.setText(tr.t("gui.decision.quality_miss_title"))
        dialog.intro_label.setText(tr.t("gui.decision.quality_miss_intro", file=record.source_path.name))
    else:
        dialog.setWindowTitle(tr.t("gui.decision.size_conflict_title"))
        dialog.title_label.setText(tr.t("gui.decision.size_conflict_title"))
        dialog.intro_label.setText(tr.t("gui.decision.size_conflict_intro", file=record.source_path.name))
    dialog.file_label.setText(str(record.source_path))
    _quality_metrics(dialog, record)
    for option in options:
        dialog.add_action(
            _quality_action_text(tr, option),
            _quality_action_description(tr, option),
            option,
            role="danger" if option.action_code == DecisionActionCode.SKIP else "",
        )
    dialog.decide_later_button.setText(tr.t("gui.decision.decide_later"))
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog.choice if isinstance(dialog.choice, DecisionOption) else None


def choose_size_miss_decision(
    parent: QWidget,
    tr: Translator,
    record: QueueItemRecord,
    compare: Callable[[Path], None] | None = None,
) -> SizeMissDecision | None:
    result = record.result
    if result is None or result.rejected_output_path is None:
        return None
    dialog = _DecisionDialog(tr, parent)
    dialog.setWindowTitle(tr.t("gui.decision.size_miss_title"))
    dialog.title_label.setText(tr.t("gui.decision.size_miss_title"))
    dialog.intro_label.setText(tr.t("gui.decision.size_miss_intro", file=record.source_path.name))
    dialog.file_label.setText(str(record.source_path))
    _size_metrics(dialog, record)
    dialog.reason_label.setText(
        f"{tr.t('gui.decision.reason')}: {result.error_message or tr.t('gui.decision.no_metrics')}"
    )
    dialog.add_action(
        tr.t("gui.decision.accept_size_miss"),
        tr.t("gui.decision.accept_size_miss_detail"),
        SizeMissDecision.ACCEPT,
    )
    dialog.add_action(
        tr.t("gui.decision.retry_size_miss"),
        tr.t("gui.decision.retry_size_miss_detail"),
        SizeMissDecision.RETRY,
    )
    dialog.add_action(
        tr.t("gui.decision.discard_size_miss"),
        tr.t("gui.decision.discard_size_miss_detail"),
        SizeMissDecision.DISCARD,
        role="danger",
    )
    preserved_path = result.rejected_output_path
    preserved_folder = preserved_path.parent
    footer = QHBoxLayout()
    open_button = QPushButton(tr.t("gui.decision.open_preserved_folder"))
    open_button.clicked.connect(
        lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(preserved_folder)))
    )
    footer.addWidget(open_button)
    if compare is not None:
        compare_button = QPushButton(tr.t("gui.decision.compare_preserved"))
        compare_button.clicked.connect(
            lambda: compare(preserved_path)
        )
        footer.addWidget(compare_button)
    footer.addStretch(1)
    dialog.actions_layout.addLayout(footer)
    dialog.decide_later_button.setText(tr.t("gui.decision.decide_later"))
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog.choice if isinstance(dialog.choice, SizeMissDecision) else None
