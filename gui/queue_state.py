from __future__ import annotations

import copy
import re
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping

from core.i18n import Translator
from core.models import CompressionMode, EncodePlan, EncodePlanItem, EncodeResult, MediaInfo, QualitySearchResult, SegmentedAnalysisResult
from core.progress_events import ProgressEvent


class QueueItemStatus(str, Enum):
    DRAFT = "draft"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_ANALYSIS = "waiting_analysis"
    ANALYZING = "analyzing"
    ENCODING = "encoding"
    VALIDATING = "validating"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    NEEDS_DECISION = "needs_decision"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


STATUS_KEY_BY_VALUE = {
    QueueItemStatus.DRAFT: "gui.status.draft",
    QueueItemStatus.QUEUED: "gui.status.ready",
    QueueItemStatus.RUNNING: "gui.status.running",
    QueueItemStatus.WAITING_ANALYSIS: "gui.status.waiting_analysis",
    QueueItemStatus.ANALYZING: "gui.status.analyzing",
    QueueItemStatus.ENCODING: "gui.status.encoding",
    QueueItemStatus.VALIDATING: "gui.status.validating",
    QueueItemStatus.PAUSED: "gui.status.paused",
    QueueItemStatus.DONE: "gui.status.done",
    QueueItemStatus.FAILED: "gui.status.failed",
    QueueItemStatus.NEEDS_DECISION: "gui.status.needs_decision",
    QueueItemStatus.SKIPPED: "gui.status.skip",
    QueueItemStatus.CANCELLED: "gui.status.cancelled",
}

ACTIVE_ITEM_STATUSES = {
    QueueItemStatus.RUNNING,
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
class QueueItemRecord:
    item_id: str
    plan_item: EncodePlanItem
    job_snapshot: QueueJobSnapshot
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

    @property
    def source_path(self) -> Path:
        return self.plan_item.source_path

    @property
    def output_path(self) -> Path:
        return self.plan_item.output_path

    @property
    def media_info(self) -> MediaInfo | None:
        return self.plan_item.media_info

    @property
    def duration_sec(self) -> float:
        if self.plan_item.media_info is None:
            return 0.0
        return float(self.plan_item.media_info.duration or 0.0)

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


def status_key(status: QueueItemStatus) -> str:
    return STATUS_KEY_BY_VALUE[status]


def short_error(message: str | None, limit: int = 120) -> str:
    if not message:
        return ""
    normalized = " ".join(message.strip().split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def build_tags(record: QueueItemRecord, translator: Translator) -> list[str]:
    tags: list[str] = []
    if record.total_passes > 1:
        tags.append(translator.t("gui.tag.two_pass"))
    if record.plan_item.options.overwrite:
        tags.append(translator.t("gui.tag.overwrite"))
    if record.plan_item.options.copy_external_subtitles:
        tags.append(translator.t("gui.tag.external_subtitles"))
    if record.plan_item.warnings:
        tags.append(translator.t("gui.tag.warning"))
    if record.status == QueueItemStatus.SKIPPED:
        tags.append(translator.t("gui.tag.skipped"))
    if record.status == QueueItemStatus.FAILED:
        tags.append(translator.t("gui.tag.failed"))
    if record.status == QueueItemStatus.NEEDS_DECISION:
        tags.append(translator.t("gui.tag.decision"))
    if record.error_summary and record.status not in {
        QueueItemStatus.SKIPPED,
        QueueItemStatus.FAILED,
        QueueItemStatus.NEEDS_DECISION,
    }:
        tags.append(translator.t("gui.tag.note"))
    return tags


def build_tooltip(record: QueueItemRecord, translator: Translator) -> str:
    lines = [
        translator.t("gui.tooltip.source", path=record.source_path),
        translator.t("gui.tooltip.output", path=record.output_path),
    ]
    if record.plan_item.warnings:
        lines.append(
            translator.t("gui.tooltip.warnings", warnings="; ".join(record.plan_item.warnings))
        )
    if record.error_summary:
        lines.append(translator.t("gui.tooltip.detail", detail=record.error_summary))
    quality = record.plan_item.quality_search_result
    segmented = record.plan_item.segmented_analysis_result
    if segmented is not None:
        mean = segmented.final_mean_vmaf if segmented.final_mean_vmaf is not None else segmented.predicted_mean_vmaf
        lines.append(translator.t("gui.tooltip.smart_v2_shots", count=len(segmented.shots)))
        if record.analysis_measurement_budget:
            lines.append(
                translator.t(
                    "gui.tooltip.measurement_budget",
                    budget=record.analysis_measurement_budget,
                )
            )
        if mean is not None:
            if segmented.final_mean_vmaf is not None:
                lines.append(translator.t("gui.tooltip.measured_mean_vmaf", value=f"{mean:.2f}"))
            else:
                lines.append(translator.t("gui.tooltip.predicted_mean_vmaf", value=f"{mean:.2f}"))
        if segmented.approximate:
            lines.append(translator.t("gui.tooltip.allocation_compressed"))
        if segmented.reason:
            lines.append(translator.t("gui.tooltip.smart_analysis", reason=segmented.reason))
    if quality is not None:
        if quality.min_vmaf is not None:
            lines.append(
                translator.t("gui.tooltip.smart_quality_score", value=f"{quality.min_vmaf:.2f}")
            )
        if quality.predicted_output_ratio is not None:
            lines.append(
                translator.t(
                    "gui.tooltip.predicted_output",
                    value=f"{quality.predicted_output_ratio * 100:.2f}",
                )
            )
        if quality.required_output_ratio is not None:
            lines.append(
                translator.t(
                    "gui.tooltip.required_output",
                    value=f"{quality.required_output_ratio * 100:.2f}",
                )
            )
        if quality.reason:
            lines.append(translator.t("gui.tooltip.smart_analysis", reason=quality.reason))
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


def estimate_saved_bytes(records: list[QueueItemRecord]) -> int | None:
    total_saved = 0
    has_estimate = False
    for record in records:
        media = record.media_info
        if media is None or media.duration <= 0 or record.plan_item.target_video_bitrate_bps <= 0:
            continue

        if record.plan_item.options.audio_mode.value == "copy":
            audio_bitrate_bps = max(int(media.audio_bitrate_bps or 0), 0)
        else:
            parsed_audio = parse_bitrate_to_bps(record.plan_item.options.audio_bitrate)
            audio_bitrate_bps = max(parsed_audio or 0, 0)
        estimated_output_bytes = int(media.duration * (record.plan_item.target_video_bitrate_bps + audio_bitrate_bps) / 8.0)

        try:
            source_bytes = record.source_path.stat().st_size
        except OSError:
            source_bytes = int(media.duration * max(int(media.format_bitrate_bps or 0), 0) / 8.0)

        if source_bytes <= 0 or estimated_output_bytes <= 0:
            continue
        total_saved += source_bytes - estimated_output_bytes
        has_estimate = True
    return total_saved if has_estimate else None


def output_will_be_written(record: QueueItemRecord) -> bool:
    """Return whether this record still targets a file that will be produced.

    A planning failure (probe or validation) leaves ``skip_reason`` set and is
    published as a skipped item without an encode, so its reserved output path
    must not participate in queue-wide collision checks.
    """

    return record.plan_item.skip_reason is None


def processed_weight(record: QueueItemRecord) -> float:
    weight = record.effective_weight
    if weight <= 0:
        return 0.0
    if record.status in TERMINAL_ITEM_STATUSES:
        return weight
    if record.status == QueueItemStatus.NEEDS_DECISION:
        return weight * 0.99
    return weight * max(0.0, min(100.0, record.file_progress)) / 100.0


def compute_metrics(records: list[QueueItemRecord]) -> QueueMetrics:
    metrics = QueueMetrics()
    metrics.total_items = len(records)
    metrics.total_duration_sec = sum(record.duration_sec for record in records if record.duration_sec > 0)
    metrics.estimated_saved_bytes = estimate_saved_bytes(records)

    total_weight = 0.0
    completed_weight = 0.0
    running_record: QueueItemRecord | None = None
    for record in records:
        weight = record.effective_weight
        total_weight += weight
        completed_weight += processed_weight(record)
        if record.status in {QueueItemStatus.QUEUED, QueueItemStatus.WAITING_ANALYSIS}:
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

    if running_record is not None:
        metrics.current_item_id = running_record.item_id
        metrics.current_file_name = running_record.source_path.name
        metrics.current_file_percent = running_record.file_progress
        metrics.current_speed = running_record.last_speed or "-"
        metrics.current_elapsed_sec = running_record.elapsed_sec
        metrics.current_total_duration_sec = running_record.duration_sec or None
        speed_factor = parse_speed_factor(running_record.last_speed or "")
        if speed_factor:
            remaining_weight = max(0.0, total_weight - completed_weight)
            metrics.eta_sec = remaining_weight / speed_factor
    return metrics


def mark_started(record: QueueItemRecord) -> None:
    record.status = (
        QueueItemStatus.WAITING_ANALYSIS
        if record.plan_item.options.compression_mode == CompressionMode.SMART
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
    record.status = (
        QueueItemStatus.WAITING_ANALYSIS
        if record.plan_item.options.compression_mode == CompressionMode.SMART
        else QueueItemStatus.QUEUED
    )
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
        record.plan_item.segmented_analysis_result = segmented_result
    if isinstance(quality_result, QualitySearchResult):
        record.plan_item.quality_search_result = quality_result
    target_bitrate = event.get("target_video_bitrate_bps")
    if isinstance(target_bitrate, int) and target_bitrate > 0:
        record.plan_item.target_video_bitrate_bps = target_bitrate
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
        record.plan_item.segmented_analysis_result = result.segmented_analysis_result
    record.result = result
    record.log_path = result.log_path
    record.finished_at = time.time()
    if result.quality_search_result is not None:
        record.plan_item.quality_search_result = result.quality_search_result
        if result.quality_search_result.selected_video_bitrate_bps > 0:
            record.plan_item.target_video_bitrate_bps = result.quality_search_result.selected_video_bitrate_bps
    if result.effective_min_vmaf is not None:
        record.plan_item.options.min_vmaf = result.effective_min_vmaf
    if result.effective_max_output_ratio is not None:
        record.plan_item.options.max_output_ratio = result.effective_max_output_ratio
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
