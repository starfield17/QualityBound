from __future__ import annotations

import copy
import re
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping

from core.i18n import Text, Translator
from core.models import CompressionMode, EncodeOptions, EncodePlan, EncodePlanItem, EncodeResult, MediaInfo, QualitySearchResult, SegmentedAnalysisResult, VideoFileItem
from core.progress_events import ProgressEvent
from core.smart import resolve_max_output_ratio


TOOLTIP_SMART_ANALYSIS = Text(
    en='Smart analysis: {reason}',
    zh_cn='Smart 分析：{reason}',
    ja='Smart分析: {reason}',
)

TOOLTIP_START_BINDING = Text(
    en='Encoding settings and output are selected when Start Queue is clicked. Per-item overrides are preserved.',
    zh_cn='点击 Start Queue 时确定编码参数和输出路径；保留单项自定义设置。',
    ja='「キューを開始」をクリックした時にエンコード設定と出力先が確定します。項目ごとの個別設定は保持されます。',
)


class QueueItemStatus(str, Enum):
    AWAITING_START = "awaiting_start"
    QUEUED = "queued"
    WAITING_ANALYSIS = "waiting_analysis"
    ANALYZING = "analyzing"
    ENCODING = "encoding"
    VALIDATING = "validating"
    DONE = "done"
    FAILED = "failed"
    NEEDS_DECISION = "needs_decision"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


STATUS_TEXT_BY_VALUE = {
    QueueItemStatus.AWAITING_START: Text(en='Awaiting Start', zh_cn='待启动', ja='開始待ち'),
    QueueItemStatus.QUEUED: Text(en='Ready', zh_cn='就绪', ja='準備完了'),
    QueueItemStatus.WAITING_ANALYSIS: Text(en='Waiting analysis', zh_cn='等待分析', ja='分析待ち'),
    QueueItemStatus.ANALYZING: Text(en='Analyzing', zh_cn='分析中', ja='分析中'),
    QueueItemStatus.ENCODING: Text(en='Encoding', zh_cn='编码中', ja='エンコード中'),
    QueueItemStatus.VALIDATING: Text(en='Validating', zh_cn='验收中', ja='検証中'),
    QueueItemStatus.DONE: Text(en='Done', zh_cn='完成', ja='完了'),
    QueueItemStatus.FAILED: Text(en='Failed', zh_cn='失败', ja='失敗'),
    QueueItemStatus.NEEDS_DECISION: Text(en='Needs decision', zh_cn='等待决定', ja='決定待ち'),
    QueueItemStatus.SKIPPED: Text(en='Skipped', zh_cn='已跳过', ja='スキップ済み'),
    QueueItemStatus.CANCELLED: Text(en='Cancelled', zh_cn='已取消', ja='キャンセル済み'),
}

PROGRESS_STAGE_TEXT = {
    "planning": Text(en='Planning', zh_cn='规划', ja='計画中'),
    "analysis": Text(en='Analysis', zh_cn='分析', ja='分析'),
    "encode": Text(en='Encoding', zh_cn='编码', ja='エンコード'),
}

PROGRESS_STATE_TEXT = {
    "scanning": Text(en='Scanning folders', zh_cn='正在扫描目录', ja='フォルダーをスキャン中'),
    "analyzing": Text(en='Measuring candidates', zh_cn='正在测量候选项', ja='候補を測定中'),
    "candidate_finished": Text(en='Candidate measured', zh_cn='候选项测量完成', ja='候補の測定完了'),
    "scouting": Text(en='Scanning scenes', zh_cn='正在扫描场景', ja='シーンをスキャン中'),
    "scout_finished": Text(en='Scene scan complete', zh_cn='场景扫描完成', ja='シーンスキャン完了'),
    "sample_plan_ready": Text(en='Samples selected', zh_cn='采样窗口已选定', ja='サンプルが選択されました'),
    "boundary_alignment": Text(en='Aligning boundaries', zh_cn='正在对齐边界', ja='境界を整列中'),
    "searching": Text(en='Searching candidates', zh_cn='正在搜索候选项', ja='候補を検索中'),
    "holdout_verification": Text(en='Validating holdouts', zh_cn='正在验证留出窗口', ja='ホールドアウトを検証中'),
    "refining": Text(en='Refining search', zh_cn='正在细化搜索', ja='検索を絞り込み中'),
    "receipt_loaded": Text(en='Reusing measurements', zh_cn='正在复用测量结果', ja='測定結果を再利用中'),
    "receipt_write_failed": Text(en='Could not save measurements', zh_cn='无法保存测量结果', ja='測定結果を保存できませんでした'),
    "running": Text(en='Running', zh_cn='运行中', ja='実行中'),
    "failed": Text(en='Failed', zh_cn='失败', ja='失敗'),

    "probing": Text(en='Probing', zh_cn='探测中', ja='プローブ中'),
    "started": Text(en='Started', zh_cn='已开始', ja='開始済み'),
    "starting_file": Text(en='Starting file', zh_cn='开始处理文件', ja='ファイルの処理を開始'),
    "running_pass": Text(en='Running pass', zh_cn='执行编码遍', ja='パスを実行中'),
    "validating": Text(en='Validating', zh_cn='验证中', ja='検証中'),
    "needs_decision": Text(en='Needs decision', zh_cn='等待决定', ja='決定待ち'),
    "finished_file": Text(en='Finished file', zh_cn='文件完成', ja='ファイル完了'),
    "cancelled_file": Text(en='Cancelled', zh_cn='已取消', ja='キャンセル済み'),
    "failed_file": Text(en='Failed', zh_cn='失败', ja='失敗'),
    "paused": Text(en='Paused', zh_cn='已暂停', ja='一時停止中'),
    "cancelled": Text(en='Cancelled', zh_cn='已取消', ja='キャンセル済み'),
    "cancelling": Text(en='Stopping', zh_cn='正在停止', ja='停止中'),
    "finished": Text(en='Finished', zh_cn='已完成', ja='完了'),
    "skipped": Text(en='Skipped', zh_cn='已跳过', ja='スキップ済み'),
    "waiting_analysis": Text(en='Waiting for analysis', zh_cn='等待分析', ja='分析を待機中'),
    "analysis_finished": Text(en='Analysis finished', zh_cn='分析完成', ja='分析完了'),
}


def progress_stage_text(stage: str) -> Text:
    return PROGRESS_STAGE_TEXT.get(stage, Text(en='Working', zh_cn='处理中', ja='処理中'))


def progress_state_text(state: str) -> Text:
    return PROGRESS_STATE_TEXT.get(state, Text(en='Working', zh_cn='处理中', ja='処理中'))


ACTIVE_ITEM_STATUSES = {
    QueueItemStatus.ANALYZING,
    QueueItemStatus.ENCODING,
    QueueItemStatus.VALIDATING,
}

# Items that can still be scheduled for the current or a future run.
RUNNABLE_ITEM_STATUSES = {
    QueueItemStatus.QUEUED,
    QueueItemStatus.WAITING_ANALYSIS,
}

# Items whose file work is finished; only a user decision changes them further.
TERMINAL_ITEM_STATUSES = {
    QueueItemStatus.DONE,
    QueueItemStatus.FAILED,
    QueueItemStatus.SKIPPED,
    QueueItemStatus.CANCELLED,
}


@dataclass(slots=True)
class QueueJobSnapshot:
    workdir: Path
    ffmpeg_path: Path
    ffprobe_path: Path
    output_root: Path


@dataclass(slots=True)
class QueueSourceDraft:
    file_item: VideoFileItem
    input_root: Path | None
    media_info: MediaInfo | None = None
    options_override: EncodeOptions | None = None
    output_dir_override: Path | None = None


@dataclass(slots=True)
class QueueItemRecord:
    item_id: str
    plan_item: EncodePlanItem | None
    job_snapshot: QueueJobSnapshot | None
    status: QueueItemStatus
    total_passes: int
    current_pass_index: int = 0
    pass_percent: float = 0.0
    file_progress: float = 0.0
    error_summary: str | None = None
    log_path: Path | None = None
    last_speed: str = ""
    elapsed_sec: float | None = None
    started_at: float | None = None
    finished_at: float | None = None
    assigned_backend: str | None = None
    assigned_encoder: str | None = None
    result: EncodeResult | None = None
    analysis_candidate_index: int = 0
    analysis_candidate_limit: int = 0
    analysis_measurement_budget: int = 0
    draft: QueueSourceDraft | None = None
    # -1 means not inspected yet; zero also represents unavailable size.
    source_size_bytes: int = -1
    output_size_bytes: int = -1

    def __post_init__(self) -> None:
        if (self.plan_item is None) != (self.job_snapshot is None):
            raise ValueError("A queue binding requires both a plan and a tool snapshot.")
        if self.plan_item is None:
            if self.draft is None or self.status in RUNNABLE_ITEM_STATUSES | ACTIVE_ITEM_STATUSES | {QueueItemStatus.NEEDS_DECISION}:
                raise ValueError("An unbound queue item requires a source draft and a non-executing status.")
        elif self.status == QueueItemStatus.AWAITING_START:
            raise ValueError("An awaiting-start source must not have an execution binding.")
        if self.plan_item is not None and self.draft is not None and self.plan_item.source_path != self.draft.file_item.path:
            raise ValueError("Queue source and execution binding identities differ.")

    @property
    def bound_plan_item(self) -> EncodePlanItem:
        if self.plan_item is None:
            raise RuntimeError("Queue source is not bound to an execution plan.")
        return self.plan_item

    @property
    def bound_job_snapshot(self) -> QueueJobSnapshot:
        if self.job_snapshot is None:
            raise RuntimeError("Queue source has no execution tool snapshot.")
        return self.job_snapshot

    @property
    def source_path(self) -> Path:
        if self.draft is not None:
            return self.draft.file_item.path
        return self.bound_plan_item.source_path

    @property
    def output_path(self) -> Path:
        return self.bound_plan_item.output_path

    @property
    def media_info(self) -> MediaInfo | None:
        if self.plan_item is not None:
            return self.plan_item.media_info
        return self.draft.media_info if self.draft is not None else None

    @property
    def duration_sec(self) -> float:
        if self.media_info is None:
            return 0.0
        return float(self.media_info.duration or 0.0)

    @property
    def effective_weight(self) -> float:
        return self.duration_sec * max(self.total_passes, 1)


@dataclass(slots=True)
class QueueMetrics:
    total_items: int = 0
    queued_items: int = 0
    running_items: int = 0
    failed_items: int = 0
    needs_decision_items: int = 0
    done_items: int = 0
    skipped_items: int = 0
    cancelled_items: int = 0
    ready_items: int = 0
    total_duration_sec: float = 0.0
    queue_percent: float = 0.0
    eta_sec: float | None = None
    completed_items: int = 0
    estimated_saved_bytes: int | None = None
    estimated_saved_is_floor: bool = False
    actual_saved_bytes: int | None = None
    actual_source_bytes: int | None = None
    actual_output_bytes: int | None = None
    measured_saved_items: int = 0
    current_item_id: str | None = None
    current_file_name: str = "-"
    current_file_percent: float | None = None
    current_speed: str = "-"
    current_elapsed_sec: float | None = None
    current_total_duration_sec: float | None = None


def build_queue_job_snapshot(plan: EncodePlan, workdir: Path) -> QueueJobSnapshot:
    return QueueJobSnapshot(
        workdir=workdir,
        ffmpeg_path=plan.ffmpeg_path,
        ffprobe_path=plan.ffprobe_path,
        output_root=plan.output_root,
    )


def clone_plan_item(plan_item: EncodePlanItem) -> EncodePlanItem:
    return copy.deepcopy(plan_item)


def create_draft_record(draft: QueueSourceDraft, error: str | None = None) -> QueueItemRecord:
    return QueueItemRecord(uuid.uuid4().hex, None, None,
                           QueueItemStatus.FAILED if error else QueueItemStatus.AWAITING_START,
                           1, error_summary=error, draft=copy.deepcopy(draft))


def unbind_for_retry(record: QueueItemRecord) -> None:
    """A manual retry returns a source to start-time selection, not a resume."""
    if record.draft is None:
        record.draft = QueueSourceDraft(VideoFileItem(record.source_path, Path(record.source_path.name)),
                                        None, record.media_info)
    record.plan_item = None
    record.job_snapshot = None
    reset_for_retry(record)
    record.total_passes = 1


def create_queue_records(plan: EncodePlan, workdir: Path) -> list[QueueItemRecord]:
    job_snapshot = build_queue_job_snapshot(plan, workdir)
    records: list[QueueItemRecord] = []
    for item in plan.items:
        total_passes = 2 if item.options.two_pass and item.encoder_info and item.encoder_info.supports_two_pass else 1
        if item.skip_reason:
            status = QueueItemStatus.SKIPPED
        elif item.options.compression_mode == CompressionMode.SMART:
            status = QueueItemStatus.WAITING_ANALYSIS
        else:
            status = QueueItemStatus.QUEUED
        file_progress = 100.0 if status == QueueItemStatus.SKIPPED else 0.0
        records.append(
            QueueItemRecord(
                item_id=uuid.uuid4().hex,
                plan_item=clone_plan_item(item),
                job_snapshot=job_snapshot,
                status=status,
                total_passes=total_passes,
                current_pass_index=0 if status != QueueItemStatus.SKIPPED else total_passes,
                pass_percent=100.0 if status == QueueItemStatus.SKIPPED else 0.0,
                file_progress=file_progress,
                error_summary=item.skip_reason,
            )
        )
    return records


def status_text(status: QueueItemStatus) -> Text:
    return STATUS_TEXT_BY_VALUE[status]


def short_error(message: str | None, limit: int = 120) -> str:
    if not message:
        return ""
    normalized = " ".join(message.strip().split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def build_tags(record: QueueItemRecord, translator: Translator) -> list[str]:
    tags: list[str] = []
    if record.plan_item is None:
        if record.draft is not None and record.draft.options_override is not None:
            tags.append(translator.t(Text(en='Custom options', zh_cn='自定义参数', ja='カスタム設定')))
        if record.draft is not None and record.draft.output_dir_override is not None:
            tags.append(translator.t(Text(en='Custom output', zh_cn='自定义目录', ja='カスタム出力先')))
        return tags
    if record.total_passes > 1:
        tags.append(translator.t(Text(en='2-pass', zh_cn='双遍', ja='2パス')))
    if record.bound_plan_item.options.overwrite:
        tags.append(translator.t(Text(en='Overwrite', zh_cn='覆盖', ja='上書き')))
    if record.bound_plan_item.options.copy_external_subtitles:
        tags.append(translator.t(Text(en='ExtSub', zh_cn='外挂字幕', ja='外部字幕')))
    if record.bound_plan_item.warnings:
        tags.append(translator.t(Text(en='Warn', zh_cn='警告', ja='警告')))
    # Status is already shown in the dedicated Status column; repeating it in
    # Tags only steals width from the flags a user cannot see elsewhere.
    if record.error_summary and record.status not in {
        QueueItemStatus.SKIPPED,
        QueueItemStatus.FAILED,
        QueueItemStatus.NEEDS_DECISION,
    }:
        tags.append(translator.t(Text(en='Note', zh_cn='备注', ja='備考')))
    return tags


def build_tooltip(record: QueueItemRecord, translator: Translator) -> str:
    if record.plan_item is None:
        return "\n".join([str(record.source_path), translator.t(TOOLTIP_START_BINDING), record.error_summary or ""])
    lines = [
        translator.t(Text(en='Source: {path}', zh_cn='源文件：{path}', ja='ソース: {path}'), path=record.source_path),
        translator.t(Text(en='Output: {path}', zh_cn='输出：{path}', ja='出力: {path}'), path=record.output_path),
    ]
    if record.bound_plan_item.warnings:
        lines.append(
            translator.t(Text(en='Warnings: {warnings}', zh_cn='警告：{warnings}', ja='警告: {warnings}'), warnings="; ".join(record.bound_plan_item.warnings))
        )
    if record.error_summary:
        lines.append(translator.t(Text(en='Detail: {detail}', zh_cn='详情：{detail}', ja='詳細: {detail}'), detail=record.error_summary))
    quality = record.bound_plan_item.quality_search_result
    segmented = record.bound_plan_item.segmented_analysis_result
    if segmented is not None:
        mean = segmented.final_mean_vmaf if segmented.final_mean_vmaf is not None else segmented.predicted_mean_vmaf
        lines.append(translator.t(Text(en='Smart v2 (experimental): {count} shots', zh_cn='Smart v2（实验性）：{count} 个镜头', ja='Smart v2 (実験的): {count} ショット'), count=len(segmented.shots)))
        if record.analysis_measurement_budget:
            lines.append(
                translator.t(
                    Text(en='Initial sample encode budget: {budget}; holdouts and repairs are additional.', zh_cn='初始样本编码预算：{budget}；留出验证与修复为额外开销。', ja='初期サンプルエンコード予算: {budget}。ホールドアウトと修復は追加となります。'),
                    budget=record.analysis_measurement_budget,
                )
            )
        if mean is not None:
            if segmented.final_mean_vmaf is not None:
                lines.append(translator.t(Text(en='Measured mean VMAF: {value}', zh_cn='实测平均 VMAF：{value}', ja='測定平均VMAF: {value}'), value=f"{mean:.2f}"))
            else:
                lines.append(translator.t(Text(en='Predicted mean VMAF: {value}', zh_cn='预测平均 VMAF：{value}', ja='予測平均VMAF: {value}'), value=f"{mean:.2f}"))
        if segmented.approximate:
            lines.append(translator.t(Text(en='Allocation used bounded state compression.', zh_cn='码率分配使用了有界状态压缩。', ja='割り当てに有界状態圧縮を使用しました。')))
        if segmented.reason:
            lines.append(translator.t(TOOLTIP_SMART_ANALYSIS, reason=segmented.reason))
    if quality is not None:
        if quality.min_vmaf is not None:
            lines.append(
                translator.t(Text(en='SMART quality score: {value}', zh_cn='SMART 质量分：{value}', ja='SMART品質スコア: {value}'), value=f"{quality.min_vmaf:.2f}")
            )
        if quality.predicted_output_ratio is not None:
            lines.append(
                translator.t(
                    Text(en='Predicted output: {value}%', zh_cn='预测输出体积：{value}%', ja='予測出力サイズ: {value}%'),
                    value=f"{quality.predicted_output_ratio * 100:.2f}",
                )
            )
        if quality.required_output_ratio is not None:
            lines.append(
                translator.t(
                    Text(en='Estimated required output: {value}%', zh_cn='预计所需输出体积：{value}%', ja='推定必要出力サイズ: {value}%'),
                    value=f"{quality.required_output_ratio * 100:.2f}",
                )
            )
        if quality.reason:
            lines.append(translator.t(TOOLTIP_SMART_ANALYSIS, reason=quality.reason))
    return "\n".join(lines)


def parse_speed_factor(speed: str) -> float | None:
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)x\s*", speed)
    if not match:
        return None
    value = float(match.group(1))
    if value <= 0:
        return None
    return value


def parse_bitrate_to_bps(raw: str) -> int | None:
    value = raw.strip().lower()
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([kmg]?)", value)
    if not match:
        return None
    number = float(match.group(1))
    suffix = match.group(2)
    scale = {"": 1, "k": 1000, "m": 1000_000, "g": 1000_000_000}[suffix]
    return int(number * scale)


def estimate_saved_bytes(records: list[QueueItemRecord]) -> tuple[int | None, bool]:
    """Estimate saved bytes, plus whether the value is only a lower bound.

    A Smart item before analysis has no selected bitrate yet, but the executor
    still enforces the codec's output-size ceiling at publication. Its saving is
    therefore at least ``source - ceiling``, which marks the total as a floor.
    Items that produced no output contribute nothing.
    """

    no_output_statuses = {
        QueueItemStatus.SKIPPED,
        QueueItemStatus.FAILED,
        QueueItemStatus.CANCELLED,
        QueueItemStatus.NEEDS_DECISION,
    }
    total_saved = 0
    has_estimate = False
    is_floor = False
    for record in records:
        if record.plan_item is None:
            continue
        media = record.media_info
        if media is None or media.duration <= 0 or record.bound_plan_item.skip_reason:
            continue

        if record.status in no_output_statuses:
            continue
        source_bytes = _record_source_bytes(record) or 0
        if source_bytes <= 0:
            continue

        target_video_bitrate_bps = record.bound_plan_item.target_video_bitrate_bps
        if target_video_bitrate_bps > 0:
            if record.bound_plan_item.options.audio_mode.value == "copy":
                audio_bitrate_bps = max(int(media.audio_bitrate_bps or 0), 0)
            else:
                parsed_audio = parse_bitrate_to_bps(record.bound_plan_item.options.audio_bitrate)
                audio_bitrate_bps = max(parsed_audio or 0, 0)
            estimated_output_bytes = int(
                media.duration * (target_video_bitrate_bps + audio_bitrate_bps) / 8.0
            )
        elif (
            record.bound_plan_item.options.compression_mode == CompressionMode.SMART
            and record.status not in no_output_statuses
        ):
            try:
                ceiling_ratio = resolve_max_output_ratio(
                    record.bound_plan_item.options.codec,
                    record.bound_plan_item.options.max_output_ratio,
                )
            except ValueError:
                continue
            estimated_output_bytes = min(int(source_bytes * ceiling_ratio), source_bytes)
            is_floor = True
        else:
            continue

        if estimated_output_bytes <= 0:
            continue
        total_saved += source_bytes - estimated_output_bytes
        has_estimate = True
    return (total_saved if has_estimate else None, is_floor)


def output_will_be_written(record: QueueItemRecord) -> bool:
    """Return whether this record still targets a file that will be produced.

    A planning failure (probe or validation) leaves ``skip_reason`` set and is
    published as a skipped item without an encode, so its reserved output path
    must not participate in queue-wide collision checks.
    """

    return record.plan_item is not None and record.plan_item.skip_reason is None


def processed_weight(record: QueueItemRecord) -> float:
    weight = record.effective_weight
    if weight <= 0:
        return 0.0
    if record.status in TERMINAL_ITEM_STATUSES:
        return weight
    if record.status == QueueItemStatus.NEEDS_DECISION:
        return weight * 0.99
    return weight * max(0.0, min(100.0, record.file_progress)) / 100.0


def _record_source_bytes(record: QueueItemRecord) -> int | None:
    if record.source_size_bytes < 0:
        try:
            record.source_size_bytes = record.source_path.stat().st_size
        except OSError:
            media = record.media_info
            record.source_size_bytes = int(media.duration * (media.format_bitrate_bps or 0) / 8) if media else 0
    return record.source_size_bytes or None


def _record_output_bytes(record: QueueItemRecord) -> int | None:
    if record.result is not None and record.result.actual_output_bytes is not None:
        return record.result.actual_output_bytes
    if record.output_size_bytes < 0:
        try:
            record.output_size_bytes = record.output_path.stat().st_size if record.status == QueueItemStatus.DONE else 0
        except OSError:
            record.output_size_bytes = 0
    return record.output_size_bytes or None


def compute_metrics(records: list[QueueItemRecord]) -> QueueMetrics:
    metrics = QueueMetrics()
    metrics.total_items = len(records)
    metrics.total_duration_sec = sum(record.duration_sec for record in records if record.duration_sec > 0)
    metrics.estimated_saved_bytes, metrics.estimated_saved_is_floor = estimate_saved_bytes(records)

    total_weight = 0.0
    completed_weight = 0.0
    running_record: QueueItemRecord | None = None
    for record in records:
        weight = record.effective_weight
        total_weight += weight
        completed_weight += processed_weight(record)
        if record.status in {QueueItemStatus.AWAITING_START, QueueItemStatus.QUEUED, QueueItemStatus.WAITING_ANALYSIS}:
            metrics.queued_items += 1
        elif record.status in ACTIVE_ITEM_STATUSES:
            metrics.running_items += 1
            if running_record is None:
                running_record = record
        elif record.status == QueueItemStatus.FAILED:
            metrics.failed_items += 1
        elif record.status == QueueItemStatus.NEEDS_DECISION:
            metrics.needs_decision_items += 1
        elif record.status == QueueItemStatus.DONE:
            metrics.done_items += 1
        elif record.status == QueueItemStatus.SKIPPED:
            metrics.skipped_items += 1
        elif record.status == QueueItemStatus.CANCELLED:
            metrics.cancelled_items += 1

    metrics.ready_items = metrics.queued_items
    metrics.completed_items = metrics.done_items + metrics.skipped_items
    if total_weight > 0:
        metrics.queue_percent = max(0.0, min(100.0, (completed_weight / total_weight) * 100.0))

    actual_saved = 0
    actual_source = 0
    actual_output = 0
    measured_items = 0
    for record in records:
        if record.status != QueueItemStatus.DONE:
            continue
        source_bytes = _record_source_bytes(record)
        output_bytes = _record_output_bytes(record)
        if source_bytes is None or output_bytes is None:
            continue
        actual_source += source_bytes
        actual_output += output_bytes
        actual_saved += source_bytes - output_bytes
        measured_items += 1
    if measured_items:
        metrics.actual_saved_bytes = actual_saved
        metrics.actual_source_bytes = actual_source
        metrics.actual_output_bytes = actual_output
        metrics.measured_saved_items = measured_items

    if running_record is not None:
        metrics.current_item_id = running_record.item_id
        metrics.current_file_name = running_record.source_path.name
        metrics.current_file_percent = running_record.file_progress
        metrics.current_speed = running_record.last_speed or "-"
        metrics.current_elapsed_sec = running_record.elapsed_sec
        metrics.current_total_duration_sec = running_record.duration_sec or None
        speed_factor = sum(parse_speed_factor(record.last_speed or "") or 0 for record in records if record.status in ACTIVE_ITEM_STATUSES)
        if speed_factor:
            remaining_weight = max(0.0, total_weight - completed_weight)
            metrics.eta_sec = remaining_weight / speed_factor
    return metrics


def mark_started(record: QueueItemRecord) -> None:
    record.status = (
        QueueItemStatus.WAITING_ANALYSIS
        if record.bound_plan_item.options.compression_mode == CompressionMode.SMART
        else QueueItemStatus.ENCODING
    )
    record.started_at = time.time()
    record.finished_at = None
    record.error_summary = None
    record.result = None
    record.analysis_candidate_index = 0
    record.analysis_candidate_limit = 0
    record.analysis_measurement_budget = 0
    record.log_path = None
    record.current_pass_index = 1
    record.pass_percent = 0.0
    record.file_progress = 0.0


def reset_for_retry(record: QueueItemRecord) -> None:
    record.status = QueueItemStatus.AWAITING_START if record.plan_item is None else (
        QueueItemStatus.WAITING_ANALYSIS
        if record.bound_plan_item.options.compression_mode == CompressionMode.SMART
        else QueueItemStatus.QUEUED
    )
    record.source_size_bytes = record.output_size_bytes = -1
    record.current_pass_index = 0
    record.pass_percent = 0.0
    record.file_progress = 0.0
    record.error_summary = None
    record.log_path = None
    record.last_speed = ""
    record.elapsed_sec = None
    record.started_at = None
    record.finished_at = None
    record.assigned_backend = None
    record.assigned_encoder = None
    record.result = None
    record.analysis_candidate_index = 0
    record.analysis_candidate_limit = 0
    record.analysis_measurement_budget = 0


def prepare_record_for_execution(record: QueueItemRecord) -> None:
    """Clear transient progress values before a queue execution starts."""

    record.last_speed = ""
    record.elapsed_sec = None


def apply_progress_event(record: QueueItemRecord, event: Mapping[str, object] | ProgressEvent) -> None:
    """Apply one core progress payload to a record.

    The payload remains a normal dictionary for compatibility with existing
    producers.  This function is deliberately Qt-free; callers decide how to
    notify views after the record has been updated.
    """

    state = str(event.get("state") or "")
    backend = event.get("queue_backend")
    encoder = event.get("queue_encoder")
    if isinstance(backend, str) or isinstance(encoder, str):
        assign_runtime_backend(
            record,
            backend if isinstance(backend, str) else record.assigned_backend,
            encoder if isinstance(encoder, str) else record.assigned_encoder,
        )
    if state == "waiting_analysis":
        record.status = QueueItemStatus.WAITING_ANALYSIS
    elif state in {
        "analyzing",
        "candidate_finished",
        "scouting",
        "scout_finished",
        "sample_plan_ready",
        "boundary_alignment",
        "searching",
        "holdout_verification",
        "refining",
    }:
        record.status = QueueItemStatus.ANALYZING
    elif state == "analysis_finished":
        record.status = QueueItemStatus.QUEUED
    elif state in {"starting_file", "running_pass"}:
        record.status = QueueItemStatus.ENCODING
    elif state == "validating":
        record.status = QueueItemStatus.VALIDATING
    elif state == "needs_decision":
        record.status = QueueItemStatus.NEEDS_DECISION
    elif state == "failed":
        record.status = QueueItemStatus.FAILED

    candidate_index = event.get("candidate_index")
    if isinstance(candidate_index, int):
        record.analysis_candidate_index = candidate_index
    candidate_limit = event.get("candidate_limit")
    if isinstance(candidate_limit, int):
        record.analysis_candidate_limit = candidate_limit
    quality_result = event.get("quality_search_result")
    segmented_result = event.get("segmented_analysis_result")
    budget = event.get("measurement_budget")
    if isinstance(budget, int):
        record.analysis_measurement_budget = budget
    if isinstance(segmented_result, SegmentedAnalysisResult):
        record.bound_plan_item.segmented_analysis_result = segmented_result
    if isinstance(quality_result, QualitySearchResult):
        record.bound_plan_item.quality_search_result = quality_result
    target_bitrate = event.get("target_video_bitrate_bps")
    if isinstance(target_bitrate, int) and target_bitrate > 0:
        record.bound_plan_item.target_video_bitrate_bps = target_bitrate
    current_pass_index = event.get("current_pass_index")
    if isinstance(current_pass_index, int):
        record.current_pass_index = current_pass_index
    total_passes = event.get("total_passes")
    if isinstance(total_passes, int) and total_passes > 0:
        record.total_passes = total_passes
    pass_percent = event.get("pass_percent")
    if isinstance(pass_percent, (int, float)):
        record.pass_percent = max(0.0, min(100.0, float(pass_percent)))
    file_progress = event.get("file_progress")
    if isinstance(file_progress, (int, float)):
        record.file_progress = max(0.0, min(100.0, float(file_progress)))
    percent = event.get("percent")
    if isinstance(percent, (int, float)) and state not in {"finished_file", "failed_file"}:
        record.file_progress = max(0.0, min(100.0, float(percent)))
    speed = event.get("speed")
    if isinstance(speed, str) and speed:
        record.last_speed = speed
    elapsed_sec = event.get("elapsed_sec")
    if isinstance(elapsed_sec, (int, float)):
        record.elapsed_sec = float(elapsed_sec)
    message = short_error(str(event.get("message") or "").strip())
    if message and state in {"failed_file", "cancelled_file"}:
        record.error_summary = message


def mark_finished(record: QueueItemRecord, result: EncodeResult) -> None:
    if result.segmented_analysis_result is not None:
        record.bound_plan_item.segmented_analysis_result = result.segmented_analysis_result
    record.result = result
    record.log_path = result.log_path
    record.finished_at = time.time()
    if result.quality_search_result is not None:
        record.bound_plan_item.quality_search_result = result.quality_search_result
        if result.quality_search_result.selected_video_bitrate_bps > 0:
            record.bound_plan_item.target_video_bitrate_bps = result.quality_search_result.selected_video_bitrate_bps
    if result.effective_min_vmaf is not None:
        record.bound_plan_item.options.min_vmaf = result.effective_min_vmaf
    if result.effective_max_output_ratio is not None:
        record.bound_plan_item.options.max_output_ratio = result.effective_max_output_ratio
    if result.needs_decision:
        record.status = QueueItemStatus.NEEDS_DECISION
        record.file_progress = 100.0
        record.pass_percent = 100.0
        record.error_summary = short_error(result.error_message)
        return
    if result.skipped:
        record.status = QueueItemStatus.SKIPPED
        record.file_progress = 100.0
        record.error_summary = short_error(result.error_message)
        return
    if result.success:
        record.status = QueueItemStatus.DONE
        record.file_progress = 100.0
        record.pass_percent = 100.0
        record.current_pass_index = max(record.total_passes, 1)
        record.error_summary = None
        return
    record.status = QueueItemStatus.FAILED
    record.error_summary = short_error(result.error_message)


def assign_runtime_backend(record: QueueItemRecord, backend: str | None, encoder: str | None) -> None:
    record.assigned_backend = backend
    record.assigned_encoder = encoder


def mark_cancelled(record: QueueItemRecord, message: str | None = None) -> None:
    record.status = QueueItemStatus.CANCELLED
    record.finished_at = time.time()
    record.error_summary = short_error(message)


def mark_failed(record: QueueItemRecord, message: str | None = None) -> None:
    record.status = QueueItemStatus.FAILED
    record.finished_at = time.time()
    record.error_summary = short_error(message)
