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

from core.i18n import Text, Translator
from core.models import ConstraintFailureKind, DecisionActionCode, DecisionOption
from gui.queue.state import QueueItemRecord
from gui.window_geometry import clamped_window_size
from gui.queue.model import format_size


DECISION_DECIDE_LATER = Text(
    en='Decide later',
    zh_cn='稍后决定',
    ja='後で決定',
)

DECISION_NO_METRICS = Text(
    en='No metric details are available for this decision.',
    zh_cn='此决定没有可用的指标详情。',
    ja='この決定に使用できるメトリクスの詳細がありません。',
)

DECISION_QUALITY_MISS_TITLE = Text(
    en='Quality target not reached',
    zh_cn='未达到画质目标',
    ja='品質目標に未到達',
)

DECISION_REASON = Text(
    en='Reason',
    zh_cn='原因',
    ja='理由',
)

DECISION_SIZE_CONFLICT_INTRO = Text(
    en='Tested candidates meet the quality target but exceed the size limit for {file}, or its media budget is too small.',
    zh_cn='{file} 已测试的候选达到画质目标，但超过体积上限，或其媒体预算不足。',
    ja='{file} のテスト候補は品質目標を達成していますがサイズ上限を超えているか、メディア予算が不足しています。',
)

DECISION_SIZE_CONFLICT_TITLE = Text(
    en='Quality and size conflict',
    zh_cn='画质与体积冲突',
    ja='品質とサイズの競合',
)

DECISION_SIZE_MISS_TITLE = Text(
    en='Encoded file exceeded the size limit',
    zh_cn='成片超过体积上限',
    ja='エンコード後のファイルがサイズ上限を超過',
)


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
        return tr.t(Text(en='Allow output up to {value}', zh_cn='允许成片最大到 {value}', ja='最大 {value} までの出力を許可'), value=f"{float(value) * 100:.1f}%")
    if option.action_code == DecisionActionCode.RELAX_QUALITY and isinstance(value, (int, float)):
        return tr.t(Text(en='Lower sampled-window mean VMAF target to {value}', zh_cn='把采样窗口平均 VMAF 下限降到 {value}', ja='サンプルウィンドウの平均VMAF目標を {value} に引き下げ'), value=f"{float(value):.1f}")
    if option.action_code == DecisionActionCode.CHANGE_MEDIA_BUDGET:
        return tr.t(Text(en='Encode audio as AAC and retry', zh_cn='音频改用 AAC 后重试', ja='音声をAACでエンコードして再試行'))
    if option.action_code == DecisionActionCode.REANALYZE:
        return tr.t(Text(en='Run Smart analysis again with the current encoder', zh_cn='使用当前编码器重新运行 Smart 分析', ja='現在のエンコーダーでSmart分析を再実行'))
    return tr.t(Text(en='Skip this file', zh_cn='跳过这个文件', ja='このファイルをスキップ'))


def _quality_action_description(tr: Translator, option: DecisionOption) -> str:
    if option.action_code == DecisionActionCode.RELAX_SIZE:
        return tr.t(Text(en='Keep the quality target and allow a larger output, up to the source size.', zh_cn='保持质量目标，允许更大的输出，最大到源文件体积。', ja='品質目標を維持し、ソースサイズを上限としてより大きな出力を許可します。'))
    if option.action_code == DecisionActionCode.RELAX_QUALITY:
        return tr.t(Text(en='Keep the size ceiling and lower the quality target to the measured value.', zh_cn='保持体积上限，把质量目标降到测量值。', ja='サイズ上限を維持し、品質目標を実測値まで引き下げます。'))
    if option.action_code == DecisionActionCode.CHANGE_MEDIA_BUDGET:
        return tr.t(Text(en='Encode audio as AAC to free bitrate for video, then retry.', zh_cn='把音频编码为 AAC，为视频腾出码率后重试。', ja='音声をAACでエンコードして映像のビットレートを確保し、再試行します。'))
    if option.action_code == DecisionActionCode.REANALYZE:
        return tr.t(Text(en='Run Smart analysis again with the current encoder.', zh_cn='使用当前编码器重新运行智能分析。', ja='現在のエンコーダーでSmart分析を再実行します。'))
    return tr.t(Text(en='Do not publish a file for this item.', zh_cn='不为该项目发布文件。', ja='この項目のファイルを出力しません。'))


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
        tr.t(Text(en='Quality target', zh_cn='质量目标', ja='品質目標')),
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
        dialog.add_metric(row, tr.t(Text(en='Measured quality', zh_cn='测量质量', ja='測定品質')) if final else tr.t(Text(en='Estimated quality', zh_cn='预估画质', ja='推定品質')),
                          ("" if final else "~") + f"{measured:.1f}")
        row += 1
    if quality is not None and quality.predicted_output_ratio is not None:
        dialog.add_metric(
            row,
            tr.t(Text(en='Predicted output', zh_cn='预测输出比例', ja='予測出力比率')),
            f"~{quality.predicted_output_ratio * 100:.1f}%",
        )
        row += 1
    if quality is not None and quality.required_output_ratio is not None:
        dialog.add_metric(
            row,
            tr.t(Text(en='Required output', zh_cn='所需输出比例', ja='必要出力比率')),
            f"~{quality.required_output_ratio * 100:.1f}%",
        )
        row += 1
    reason = quality.reason if quality is not None else None
    if not reason and segmented is not None:
        reason = segmented.reason
    dialog.reason_label.setText(
        f"{tr.t(DECISION_REASON)}: {reason or record.error_summary or tr.t(DECISION_NO_METRICS)}"
    )


def _size_metrics(dialog: _DecisionDialog, record: QueueItemRecord) -> None:
    result = record.result
    if result is None:
        return
    actual = int(result.actual_output_bytes or 0)
    allowed = int(result.allowed_output_bytes or 0)
    tr = dialog.translator
    dialog.add_metric(0, tr.t(Text(en='Actual size', zh_cn='实际体积', ja='実際のサイズ')), format_size(actual))
    dialog.add_metric(1, tr.t(Text(en='Size limit', zh_cn='体积上限', ja='サイズ上限')), format_size(allowed))
    scale = max(actual, allowed, 1) * 1.1
    bar = _LimitBar(allowed / scale)
    bar.setRange(0, 1000)
    bar.setValue(round(actual / scale * 1000))
    bar.setTextVisible(True)
    ratio = actual / allowed * 100 if allowed > 0 else 0
    bar.setFormat(f"{format_size(actual)} / {format_size(allowed)} · {ratio:.1f}%")
    bar.setToolTip(tr.t(Text(en='{value}% over the limit; the marker shows the limit.', zh_cn='超出限额 {value}%；竖线表示限额。', ja='上限を {value}% 超過。マーカーは上限値を示します。'), value=f"{max(0.0, ratio - 100):.1f}"))
    bar.setProperty("over", actual > allowed)
    dialog.metrics_layout.addWidget(bar, 2, 0, 1, 2)
    if result.rejected_output_path is not None:
        dialog.add_metric(3, tr.t(Text(en='Preserved file', zh_cn='保留文件', ja='保持されたファイル')), str(result.rejected_output_path))


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
        dialog.setWindowTitle(tr.t(DECISION_QUALITY_MISS_TITLE))
        dialog.title_label.setText(tr.t(DECISION_QUALITY_MISS_TITLE))
        dialog.intro_label.setText(tr.t(Text(en='Tested candidates did not meet the VMAF target for {file}.', zh_cn='{file} 已测试的候选未达到 VMAF 目标。', ja='{file} のテスト候補はVMAF目標を満たしませんでした。'), file=record.source_path.name))
    else:
        dialog.setWindowTitle(tr.t(DECISION_SIZE_CONFLICT_TITLE))
        dialog.title_label.setText(tr.t(DECISION_SIZE_CONFLICT_TITLE))
        dialog.intro_label.setText(tr.t(DECISION_SIZE_CONFLICT_INTRO, file=record.source_path.name))
    dialog.file_label.setText(str(record.source_path))
    _quality_metrics(dialog, record)
    for option in options:
        dialog.add_action(
            _quality_action_text(tr, option),
            _quality_action_description(tr, option),
            option,
            role="danger" if option.action_code == DecisionActionCode.SKIP else "",
        )
    dialog.decide_later_button.setText(tr.t(DECISION_DECIDE_LATER))
    try:
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.choice if isinstance(dialog.choice, DecisionOption) else None
    finally:
        dialog.deleteLater()


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
    dialog.setWindowTitle(tr.t(DECISION_SIZE_MISS_TITLE))
    dialog.title_label.setText(tr.t(DECISION_SIZE_MISS_TITLE))
    dialog.intro_label.setText(tr.t(Text(en='The complete encode for {file} was kept instead of deleted.', zh_cn='{file} 已经编码完成，程序没有删除它。', ja='{file} の完全なエンコード結果は削除されずに保持されました。'), file=record.source_path.name))
    dialog.file_label.setText(str(record.source_path))
    _size_metrics(dialog, record)
    dialog.reason_label.setText(
        f"{tr.t(DECISION_REASON)}: {result.error_message or tr.t(DECISION_NO_METRICS)}"
    )
    dialog.add_action(
        tr.t(Text(en='Accept this encoded file', zh_cn='接受这个成片', ja='このエンコードファイルを採用')),
        tr.t(Text(en='Publish the preserved file even though it exceeds the size limit.', zh_cn='即使超过体积上限，也发布保留的文件。', ja='サイズ上限を超過していますが、保持されたファイルを公開します。')),
        SizeMissDecision.ACCEPT,
    )
    dialog.add_action(
        tr.t(Text(en='Retry with a corrected bitrate', zh_cn='按实测体积修正码率后重试', ja='補正したビットレートで再試行')),
        tr.t(Text(en='Encode again with a corrected bitrate.', zh_cn='用修正后的码率重新编码。', ja='補正したビットレートで再エンコードします。')),
        SizeMissDecision.RETRY,
    )
    dialog.add_action(
        tr.t(Text(en='Delete it and skip', zh_cn='删除并跳过', ja='削除してスキップ')),
        tr.t(Text(en='Delete the preserved file and mark this item skipped.', zh_cn='删除保留文件，并把该项目标记为跳过。', ja='保持されたファイルを削除し、この項目をスキップ済みにします。')),
        SizeMissDecision.DISCARD,
        role="danger",
    )
    preserved_path = result.rejected_output_path
    preserved_folder = preserved_path.parent
    footer = QHBoxLayout()
    open_button = QPushButton(tr.t(Text(en='Open preserved folder', zh_cn='打开保留文件目录', ja='保持先フォルダーを開く')))
    open_button.clicked.connect(
        lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(preserved_folder)))
    )
    footer.addWidget(open_button)
    if compare is not None:
        compare_button = QPushButton(tr.t(Text(en='Compare source and preserved file', zh_cn='对比源文件与保留文件', ja='ソースと保持ファイルを比較')))
        compare_button.clicked.connect(
            lambda: compare(preserved_path)
        )
        footer.addWidget(compare_button)
    footer.addStretch(1)
    dialog.actions_layout.addLayout(footer)
    dialog.decide_later_button.setText(tr.t(DECISION_DECIDE_LATER))
    try:
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.choice if isinstance(dialog.choice, SizeMissDecision) else None
    finally:
        dialog.deleteLater()
